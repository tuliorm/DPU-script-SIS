"""Servico de chat interativo com Claude Code CLI via stream-json bidirecional."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import queue

from config import ELABORACAO_EFFORT, ELABORACAO_MODELO, OFICIO_GERAL, PAJS_DIR
from services import historico
from services.paj_service import IGNORAR as ARQUIVOS_NAO_PECAS
from services.paj_service import listar_pajs
from services.skills_catalog import skill_descricao, skill_valida
import contextlib


def _resolver_claude_cmd() -> list[str]:
    """Resolve o comando base pra invocar o Claude CLI.

    No Windows, o binario `claude` instalado via npm vem como `claude.cmd`
    (batch wrapper). subprocess.Popen sem shell=True NAO resolve .cmd/.bat
    via PATH — so .exe — entao resolvemos o caminho completo via shutil.which
    (que respeita PATHEXT) e, se for batch, prefixamos com `cmd.exe /c`
    (exigencia do CreateProcess pra scripts .cmd/.bat).
    """
    resolved = shutil.which("claude")
    if not resolved:
        return ["claude"]  # fallback — Popen vai falhar com mensagem clara
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]
    return [resolved]


CLAUDE_CMD: list[str] = _resolver_claude_cmd()


# Diretrizes de profundidade injetadas em toda elaboracao. O NIVEL de raciocinio
# e' controlado pela flag --effort (config.ELABORACAO_EFFORT), nao por palavra no
# prompt ("ultrathink" e' soft-guidance do Claude Code interativo, redundante com
# --effort). Aqui orientamos o METODO: (1) analisar a fundo antes de redigir;
# (2) ler EXAUSTIVAMENTE os anexos OCR; (3) usar visao no PDF quando o OCR falha.
# O inventario de anexos vem no PROMPT_MAX (secao "Anexos baixados").
_DIRETRIZES_PROFUNDIDADE = (
    "## Como trabalhar este PAJ (não economize)\n"
    "1. **Analise a fundo antes de redigir**: estado processual, prazos, o polo "
    "ocupado pela DPU/assistido e as teses cabíveis; pondere alternativas e "
    "escolha a melhor.\n"
    "2. **Leia TODOS os anexos** baixados do SIS — estão listados na seção "
    "'Anexos baixados do SIS' do contexto, como arquivos `pecas/*.txt` (texto "
    "OCR). Não se baseie só nas movimentações: abra e leia cada anexo relevante "
    "na íntegra antes de decidir o produto.\n"
    "3. **OCR fraco/ausente**: para anexos assim sinalizados (ou cujo `.txt` "
    "esteja ilegível/curto), **abra o PDF original em `pecas/` e leia com sua "
    "capacidade de visão** — não confie apenas no OCR.\n\n"
)


def _montar_instrucao(paj_pasta, prompt_content: str, skill_slug: str | None) -> str:
    """Monta o prompt inicial enviado ao Claude Code CLI.

    Se `skill_slug` vier preenchida, instrui o Claude a invocar a skill correspondente
    do Oficio Geral (ex: `/peticoes-iniciais`). A skill ja sabe estrutura, tom,
    checklists e bases — o painel so fornece o contexto do PAJ e pede o texto pronto.

    Se `skill_slug` vier None (fallback), mantem o comportamento antigo de decisao
    autonoma pelo Claude.
    """
    if skill_slug:
        desc = skill_descricao(skill_slug)
        cabecalho = (
            f"Use a skill `/{skill_slug}` do workspace Oficio Geral para elaborar "
            f"o que for cabivel para este PAJ.\n"
            f"  Skill escolhida: {desc}\n\n"
            "A skill ja contem as regras de estrutura, tom, checklists e bases de "
            "conhecimento. Siga-as. Siga tambem todas as regras gerais do "
            "`CLAUDE.md` do workspace (terminologia DPU, estilo, endereçamento, "
            "assinatura etc).\n\n"
            "Nao me pergunte nada; execute a skill ate o fim com base no contexto "
            "do PAJ fornecido abaixo. Se faltar informacao critica, faca a melhor "
            "hipotese e sinalize no resumo final.\n\n"
        )
        tipo_saida = "<TIPO DE PRODUTO — conforme a skill>"
    else:
        cabecalho = (
            "Analise o PAJ abaixo seguindo o `CLAUDE.md` deste workspace (Oficio Geral) "
            "e **decida autonomamente** (sem me perguntar) o proximo passo processual adequado: "
            "despacho no SISDPU, peticao, recurso, manifestacao, memoriais, contestacao, etc. "
            "Use as skills/bases de conhecimento disponiveis no workspace conforme apropriado.\n\n"
        )
        tipo_saida = (
            "[DESPACHO | PETICAO | RECURSO | MANIFESTACAO | OFICIO | ORIENTACAO | OUTRO — <tipo>]"
        )

    return (
        cabecalho + _DIRETRIZES_PROFUNDIDADE
        + "**OBRIGATORIO**: produzir o TEXTO da peca/despacho/oficio/orientacao, em "
        "linguagem apropriada, pronto pra copiar no SISDPU / protocolar / expedir. "
        "Nao basta dizer o que fazer — redija o produto final.\n\n"
        f"Salve o(s) arquivo(s) gerado(s) em `{paj_pasta}\\` "
        "(ex: `despacho.txt`, `peticao.txt`, `recurso.txt`, `oficio.txt`, "
        "`orientacao.txt`, `parecer.txt`). Esforco proporcional ao produto. "
        "Geracao de .docx/.pdf e feita depois, pelo botao do painel.\n\n"
        "Ao final, apresente um **RESUMO ESTRUTURADO**:\n\n"
        "```\n"
        f"## Produto: {tipo_saida}\n\n"
        "### Justificativa\n"
        "<3-5 linhas explicando POR QUE este e o produto cabivel agora>\n\n"
        "### Texto do produto\n"
        "```\n"
        "<TEXTO COMPLETO aqui, formatado, pronto pra uso>\n"
        "```\n\n"
        "### Arquivos gerados\n"
        "- <caminho absoluto do arquivo .txt gerado>\n\n"
        "### Pontos-chave\n"
        "- <bullet 1>\n"
        "- <bullet 2>\n\n"
        "### Se discordar\n"
        "Me diga o que mudar e eu refaco.\n"
        "```\n\n"
        "Se eu responder com discordancia, refaca conforme instruido.\n\n"
        "---\n\n"
        f"{prompt_content}"
    )


class ChatSession:
    """Sessao interativa com Claude Code CLI."""

    def __init__(self, paj_norm: str, skill_slug: str | None = None):
        self.paj_norm = paj_norm
        # Skill do Oficio Geral a ser invocada (ex: "peticoes-iniciais").
        # Se None, o Claude decide autonomamente (comportamento antigo).
        self.skill_slug: str | None = (
            skill_slug if skill_slug and skill_valida(skill_slug) else None
        )
        self.output_queue: queue.Queue[dict] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._alive = False
        # Estado pra UI de background:
        # "idle" | "running" | "done" | "error"
        self.status: str = "idle"
        self.last_action: str = ""  # "Usando Glob", "Escrevendo peca", etc.
        self.accumulated_text: str = ""  # texto acumulado da resposta atual
        self.summary: str = ""  # resumo final (ultima resposta do Claude)
        self.error: str = ""
        # Elaboracao em background (start_or_queue): ao concluir, encerra o
        # subprocess pra liberar o slot e destravar a fila do lote. Sessoes de
        # chat interativo (get_or_create_session) deixam isto False, pois
        # precisam manter o stdin aberto pra continuar a conversa multi-turn.
        self.encerrar_ao_concluir: bool = False
        # session_id do Claude CLI (capturado nos eventos system/result). Usado
        # pra retomar a sessao via --resume apos estouro de cota.
        self.session_id: str = ""
        # Quando setado, esta sessao retoma uma anterior (--resume) em vez de
        # comecar do zero — preenchido pelo re-disparo de cota.
        self.resume_session_id: str | None = None
        # Override de modelo/esforco por sessao (cai nos globais ELABORACAO_* se
        # None). Usado pela triagem inteligente: a Fase 2 de um HARD CASE roda em
        # effort "max"; as demais mantem o global.
        self.effort_override: str | None = None
        self.modelo_override: str | None = None
        # Triagem em 2 fases: quando True, ao concluir (Fase 1 = analisar-processo)
        # o orquestrador encadeia a Fase 2 (elaboracao) com o effort da classificacao.
        self.encadear_elaboracao: bool = False
        self._fase2_disparada: bool = False
        # Se setado, ao iniciar a sessao envia ESTA instrucao (Fase 2) em vez de
        # montar o prompt padrao + PROMPT_MAX (Fase 1 / elaboracao normal).
        self.instrucao_elaboracao: str | None = None
        # Fase 1 da triagem: instrucao custom de CLASSIFICACAO (sem elaborar) que
        # o start() envia ANEXADA ao PROMPT_MAX (mantem o contexto + regenera
        # OCR-LLM/PROMPT_MAX). Diferente de instrucao_elaboracao (Fase 2), que NAO
        # reenvia o PROMPT_MAX.
        self.instrucao_inicial: str | None = None

    def _start_subprocess(self) -> bool:
        """Inicia o subprocess Claude Code CLI e a thread leitora.

        Retorna True se o processo foi aberto com sucesso; em caso de falha,
        preenche self.error e enfileira os eventos de erro/done.
        """
        cmd = [
            *CLAUDE_CMD,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-mode",
            "bypassPermissions",
        ]
        # Modelo/esforco: override por sessao (triagem inteligente) tem prioridade
        # sobre os globais. Fixar no comando garante determinismo (nao herda o
        # default da conta nem o CLAUDE_EFFORT do shell, que e' read-only de hook).
        modelo = self.modelo_override or ELABORACAO_MODELO
        effort = self.effort_override or ELABORACAO_EFFORT
        if modelo:
            cmd += ["--model", modelo]
        if effort:
            cmd += ["--effort", effort]
        # Re-disparo apos cota: retoma a sessao interrompida de onde parou
        # (mantem contexto: anexos ja lidos, analise feita).
        if self.resume_session_id:
            cmd += ["--resume", self.resume_session_id]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(OFICIO_GERAL),
            )
            self._alive = True
            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()
            return True
        except FileNotFoundError:
            self.status = "error"
            self.error = (
                f"Comando Claude CLI não encontrado (tentou: {' '.join(CLAUDE_CMD)}). "
                "Verifique se o `claude` está instalado e no PATH do processo do servidor."
            )
            self.output_queue.put({"type": "error", "text": self.error})
            self.output_queue.put({"type": "done"})
            return False
        except Exception as e:
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
            self.output_queue.put({"type": "error", "text": self.error})
            self.output_queue.put({"type": "done"})
            return False

    def start(self, resume_session_id: str | None = None) -> bool:
        """Inicia o subprocess Claude Code em modo stream-json.

        Se `resume_session_id` vier setado (re-disparo apos estouro de cota),
        retoma a sessao anterior via --resume e envia um prompt curto de
        continuacao — o contexto (anexos lidos, analise) ja esta na sessao do
        CLI, entao nao reenviamos o PROMPT_MAX.
        """
        if resume_session_id:
            self.resume_session_id = resume_session_id

        # --- Retomada (continuar de onde parou) ---
        if self.resume_session_id:
            if not self._start_subprocess():
                return False
            self.status = "running"
            self.last_action = "retomando (cota renovada)..."
            self.accumulated_text = ""
            self.send_message(
                "Sua sessão anterior foi interrompida pelo limite de uso (cota), que "
                "já foi renovado. Continue exatamente de onde parou e finalize o "
                "produto deste PAJ: salve o(s) arquivo(s) na pasta do PAJ e apresente "
                "o RESUMO ESTRUTURADO ao final."
            )
            return True

        # --- Fluxo normal (do zero) ---
        prompt_path = PAJS_DIR / self.paj_norm / "PROMPT_MAX.md"
        # Fallback OCR-LLM: ANTES de montar o prompt, transcreve com Sonnet (por
        # visao) os anexos cujo OCR local ficou fraco/ausente, pra o Opus ler o
        # texto melhorado em vez de abrir o PDF com visao toda vez. Idempotente e
        # best-effort. Pula se a cota esta esgotada (Sonnet tambem falharia) — a
        # elaboracao Opus a seguir registra o estouro e dispara o re-disparo.
        with contextlib.suppress(Exception):
            import services.cota_service as _cota_chk

            if not _cota_chk.esta_pausado():
                from services.ocr_llm_service import melhorar_ocr_paj

                melhorar_ocr_paj(self.paj_norm)
        # Regenera o PROMPT_MAX SEMPRE antes de elaborar: garante o inventario de
        # anexos e demais melhorias do prompt_builder mesmo em PAJs cujo arquivo
        # foi gerado por uma versao anterior, e reflete anexos recem-baixados
        # (inclusive o OCR-LLM acima). No lote o PAJ pode nunca ter sido aberto —
        # sem isto start() saia sem fazer nada e a sessao ficava presa em "idle".
        with contextlib.suppress(Exception):
            from services.prompt_builder import gerar_prompt_max

            gerar_prompt_max(self.paj_norm)
        if not prompt_path.exists():
            self.status = "error"
            self.error = "PROMPT_MAX.md nao encontrado (e nao pode ser gerado)."
            self.output_queue.put({"type": "error", "text": self.error})
            self.output_queue.put({"type": "done"})
            return False

        if not self._start_subprocess():
            return False

        # Envia PROMPT_MAX como prompt inicial
        prompt_content = prompt_path.read_text(encoding="utf-8", errors="replace")
        paj_pasta = PAJS_DIR / self.paj_norm
        if self.instrucao_inicial:
            # Fase 1 da triagem: instrução custom (classificar/recomendar, SEM
            # elaborar) + o contexto do PROMPT_MAX anexado.
            instrucao = f"{self.instrucao_inicial}\n\n---\n\n{prompt_content}"
        else:
            instrucao = _montar_instrucao(
                paj_pasta=paj_pasta,
                prompt_content=prompt_content,
                skill_slug=self.skill_slug,
            )
        # NAO fecha stdin — mantem aberto pra multi-turn
        self.status = "running"
        self.last_action = "iniciando..."
        self.accumulated_text = ""
        self.send_message(instrucao)
        return True

    def start_elaboracao(self, instrucao: str) -> bool:
        """Fase 2 da triagem inteligente: inicia a sessao e envia uma instrucao
        de elaboracao custom, SEM reenviar o PROMPT_MAX inline. Usa os overrides
        de effort/modelo (ex: 'max' em hard case) e o --resume eventualmente
        setados. A instrucao referencia os arquivos do PAJ (analise.md,
        PROMPT_MAX.md) — o Claude os le do disco."""
        if not self._start_subprocess():
            return False
        self.status = "running"
        self.last_action = "elaborando (pós-triagem)..."
        self.accumulated_text = ""
        self.send_message(instrucao)
        return True

    def start_correcao(self, correcao: str) -> bool:
        """Reinicia a sessao para aplicar uma correcao ao produto anterior.

        Como o Claude CLI sai apos cada rodada (-p), este metodo cria um novo
        subprocess e envia um prompt que instrui o Claude a:
          1. Ler os arquivos ja gerados na pasta do PAJ
          2. Aplicar a correcao solicitada
          3. Salvar a versao corrigida
          4. Apresentar o RESUMO ESTRUTURADO padrao
        """
        if not self._start_subprocess():
            return False

        paj_pasta = PAJS_DIR / self.paj_norm
        instrucao = (
            f"Voce elaborou anteriormente uma peca/despacho para o PAJ {self.paj_norm}. "
            f"O(s) arquivo(s) gerado(s) estao salvos em `{paj_pasta}\\`.\n\n"
            f"O defensor solicita a seguinte **CORRECAO**:\n\n"
            f"{correcao}\n\n"
            f"Por favor:\n"
            f"1. Leia o(s) arquivo(s) ja gerados em `{paj_pasta}\\` para recuperar "
            f"   o texto elaborado anteriormente\n"
            f"2. Aplique a correcao solicitada\n"
            f"3. Salve a versao atualizada sobrescrevendo o arquivo anterior\n"
            f"4. Apresente o **RESUMO ESTRUTURADO** ao final no mesmo formato padrao\n\n"
            f"Siga o `CLAUDE.md` do workspace Oficio Geral (terminologia DPU, estilo, "
            f"assinatura etc). Nao pergunte nada — refaca diretamente."
        )
        self.status = "running"
        self.last_action = "aplicando correção..."
        self.accumulated_text = ""

        # Registra ANTES de enviar — assim o evento aparece no historico mesmo
        # que a sessao falhe na sequencia. O `elaborar` posterior cobrira o
        # resultado.
        with contextlib.suppress(Exception):
            historico.registrar(
                self.paj_norm,
                "correcao",
                texto=correcao,
            )

        self.send_message(instrucao)
        return True

    def send_message(self, text: str):
        """Envia mensagem do usuario pro Claude via stdin (formato stream-json)."""
        if not self.proc or not self._alive:
            return
        msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
            # Reset estado pra novo turno
            self.status = "running"
            self.last_action = "processando mensagem..."
            self.accumulated_text = ""
        except (BrokenPipeError, OSError):
            self._alive = False
            self.status = "error"
            self.error = "Subprocess morto."

    def _read_output(self):
        """Le stdout do Claude e coloca eventos na queue."""
        try:
            for line in iter(self.proc.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    parsed = self._parse_event(event)
                    if parsed:
                        self.output_queue.put(parsed)
                except json.JSONDecodeError:
                    self.output_queue.put({"type": "text", "text": text + "\n"})

            self.proc.wait()
            self._alive = False
            self.output_queue.put({"type": "done"})
        except Exception as e:
            self.output_queue.put({"type": "error", "text": str(e)})
            self.output_queue.put({"type": "done"})
            self._alive = False
        finally:
            # Subprocess morreu — libera slot e promove proximo da fila
            with contextlib.suppress(Exception):
                _process_queue()

    def _parse_event(self, event: dict) -> dict | None:
        """Converte evento stream-json do Claude em formato simplificado pro frontend."""
        etype = event.get("type", "")

        # stream_event — wrapper dos eventos da API Anthropic
        if etype == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")

            # Texto parcial (streaming em tempo real)
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    self.accumulated_text += chunk
                    self.last_action = "escrevendo resposta..."
                    return {"type": "text", "text": chunk}

            # Tool use start
            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "?")
                    self.last_action = f"usando {tool_name}"
                    return {"type": "tool", "text": f"[usando: {tool_name}]"}

            # Tool use input — captura o comando/arquivo sendo acessado
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "input_json_delta":
                    # Concatena partial inputs (Claude vai mandando em chunks)
                    partial = delta.get("partial_json", "")
                    # Simples heuristica: se parece com comando/file path, atualiza last_action
                    if partial and len(partial) > 3:
                        snippet = partial.strip().strip('{},:"').strip()[:60]
                        if snippet:
                            self.last_action = (self.last_action or "") + " " + snippet
                            self.last_action = self.last_action[-120:]  # limita tamanho

            return None

        # Resultado final de um turno (Claude terminou de responder)
        if etype == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, dict):
                result_text = result_text.get("text", "")
            # Texto final do turno: deltas acumulados ou, na falta, o campo result.
            texto_final = self.accumulated_text.strip() or (
                result_text if isinstance(result_text, str) else ""
            )
            self.summary = texto_final
            sid_full = event.get("session_id", "") or ""
            if sid_full:
                self.session_id = sid_full  # completo — necessario pro --resume
            session_id = sid_full[:8]  # curto, so pra exibicao
            # Detecta turno encerrado SEM sucesso: erro do CLI (is_error/subtype)
            # ou ESTOURO DE COTA (mensagem do Claude / api_error_status 429).
            # Nesse caso NAO marca "done" — senao a peca nunca foi gerada, mas o
            # PAJ apareceria como concluido (botao "Ver Resumo") e seria pulado em
            # novos lotes. "error" deixa claro na UI e mantem elegivel.
            import services.cota_service as _cota

            # A mensagem de cota ("session limit · resets HH") vem no campo
            # `result` do evento — o texto_final pode ter só a análise parcial do
            # turno. Considera AMBOS pra detectar a cota E pra extrair o horário
            # de reset (sem isto, o parse caía no fallback de 60min).
            result_str = result_text if isinstance(result_text, str) else ""
            texto_cota = " ".join(s for s in (result_str, texto_final) if s)
            limite_cota = _cota.texto_indica_cota(texto_cota) or (
                event.get("api_error_status") == 429
            )
            subtype = str(event.get("subtype") or "")
            falhou = bool(event.get("is_error")) or subtype.startswith("error") or limite_cota
            if falhou:
                # Registra o evento bruto pra calibrar a deteccao na 1a ocorrencia
                # real de cota (confirmar campos estruturados do CLI).
                with contextlib.suppress(Exception):
                    _cota.logar_evento_bruto(self.paj_norm, event)
                self.status = "error"
                self.error = (
                    "Limite de uso do Claude atingido — re-disparo agendado para após o reset."
                    if limite_cota
                    else (texto_final[:200] or f"Claude terminou com erro ({subtype or '?'}).")
                )
                self.last_action = "erro: limite de uso" if limite_cota else "erro"
            else:
                self.status = "done"
                self.last_action = "aguardando sua resposta"
            # Persiste em disco pra sobreviver reinicio do servidor
            self._persist()
            # Estouro de cota numa elaboracao em background (individual/lote):
            # agenda o re-disparo automatico pra apos a renovacao da cota e
            # congela a fila. Chat interativo (encerrar_ao_concluir=False) nao
            # entra no re-disparo — o usuario esta presente.
            if limite_cota and self.encerrar_ao_concluir:
                with contextlib.suppress(Exception):
                    _cota.registrar_estouro(
                        self.paj_norm, texto_cota, self.skill_slug, self.session_id
                    )
            # Elaboracao em background: encerra o subprocess agora que terminou.
            # Sem isto o CLI fica vivo (stdin aberto pra multi-turn) e o slot
            # nunca e' liberado — travando a fila do lote. Fechar o stdin faz o
            # CLI encerrar; alem disso promovemos a fila explicitamente (a sessao
            # ja esta "done", entao nao conta como running), pra nao depender do
            # tempo ate o processo morrer. Idempotente com o finally de
            # _read_output. Correcoes posteriores reabrem via start_correcao().
            if self.encerrar_ao_concluir:
                with contextlib.suppress(Exception):
                    if self.proc and self.proc.stdin:
                        self.proc.stdin.close()
                with contextlib.suppress(Exception):
                    _process_queue()
            # Fim da Fase 1 (analisar-processo) COM sucesso: encadeia a Fase 2
            # (elaboracao) com o effort da classificacao (max se HARD CASE). Em
            # thread daemon pra nao travar o leitor de stdout. Idempotente.
            if self.status == "done" and self.encadear_elaboracao and not self._fase2_disparada:
                self._fase2_disparada = True
                with contextlib.suppress(Exception):
                    from services import triagem_lote

                    threading.Thread(
                        target=triagem_lote.encadear_fase2,
                        args=(self.paj_norm, self.summary, self.session_id),
                        daemon=True,
                    ).start()
            return {
                "type": "result",
                "text": result_text if isinstance(result_text, str) else "",
                "session_id": session_id,
            }

        # assistant_full — ignora (ja temos via deltas)
        if etype == "assistant":
            return None

        # System events (ex: init) — captura o session_id pro --resume; ignora o resto.
        if etype == "system":
            sid = event.get("session_id", "") or ""
            if sid:
                self.session_id = sid
            return None

        # rate_limit_event — 429 transitorio (o CLI re-tenta sozinho); ignora.
        if etype == "rate_limit_event":
            return None

        return None

    def is_alive(self) -> bool:
        return self._alive

    def stop(self):
        """Encerra o subprocess."""
        self._alive = False
        if self.proc:
            with contextlib.suppress(Exception):
                self.proc.terminate()

    def _persist(self) -> None:
        """Salva status + summary em PAJs/{paj}/elaboracao.json e registra
        evento no historico.jsonl.

        Assim o resultado sobrevive a reinicio do servidor — a UI le do disco
        quando nao ha sessao em memoria. O historico mantem o rastro de
        TODAS as elaboracoes/correcoes feitas no PAJ ao longo do tempo,
        diferente do elaboracao.json que so guarda a ultima.
        """
        try:
            pasta = PAJS_DIR / self.paj_norm
            if not pasta.exists():
                return
            import datetime as _dt

            data = {
                "status": self.status,
                "summary": self.summary,
                "last_action": self.last_action,
                "concluido_em": _dt.datetime.now().isoformat(),
            }
            (pasta / "elaboracao.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        # Historico estruturado — best-effort, nao bloqueia o fluxo se falhar.
        with contextlib.suppress(Exception):
            historico.registrar(
                self.paj_norm,
                "elaborar",
                skill=self.skill_slug or "",
                status=self.status,
                resumo=historico.primeira_linha_util(self.summary),
            )


def ler_elaboracao_disco(paj_norm: str) -> dict | None:
    """Le o estado persistido da elaboracao (ou None se nao existe).

    Precedencia:
    1. elaboracao.json (salvo automaticamente por _persist — tem resumo completo)
    2. Se nao tem elaboracao.json mas TEM arquivo gerado (despacho.txt, *.docx,
       *.pdf na raiz), considera "done" sem resumo detalhado.
    """
    try:
        pasta = PAJS_DIR / paj_norm
        if not pasta.exists():
            return None

        f = pasta / "elaboracao.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))

        # Fallback: tem arquivo gerado na raiz?
        # ARQUIVOS_NAO_PECAS reusa a constante canonica `IGNORAR` definida em
        # paj_service — fonte unica de verdade. Sem isso, a lista local ficava
        # defasada e produzia falsos avisos em PAJs recem-sincronizados que
        # tinham sisdpu.txt/NOTAS.md mas nenhuma peça gerada pela IA.
        gerados = [x for x in pasta.iterdir() if x.is_file() and x.name not in ARQUIVOS_NAO_PECAS]
        if gerados:
            nomes = ", ".join(sorted(x.name for x in gerados))
            return {
                "status": "done",
                "last_action": "arquivos já gerados",
                "summary": (
                    "(Resumo detalhado não está disponível — este PAJ foi "
                    "elaborado antes da implementação da persistência em disco.)\n\n"
                    f"Arquivos gerados na pasta: {nomes}\n\n"
                    "Abra a aba 'Peças Geradas' do PAJ pra ver/baixar os arquivos, "
                    "ou clique em 'Elaborar' de novo pra regerar o resumo."
                ),
                "concluido_em": "",
            }
        return None
    except Exception:
        return None


# Sessoes ativas (in-memory, single user) + fila de espera
_sessions: dict[str, ChatSession] = {}
_queue: list[str] = []  # paj_norms aguardando slot livre
MAX_PARALLEL = 5


def _count_running() -> int:
    return sum(1 for s in _sessions.values() if s.status == "running")


def _iniciar_sessao(session: ChatSession) -> bool:
    """Inicia a sessao no modo certo: Fase 2 da triagem (instrucao_elaboracao
    setada) usa start_elaboracao (instrucao custom, sem PROMPT_MAX inline);
    demais usam start() (regenera PROMPT_MAX + instrucao padrao)."""
    if session.instrucao_elaboracao:
        return session.start_elaboracao(session.instrucao_elaboracao)
    return session.start()


def _process_queue() -> None:
    """Promove proximos da fila enquanto houver slots livres."""
    # Cota esgotada: a fila fica CONGELADA — iniciar novos PAJs so' geraria mais
    # estouros. O cota_service re-dispara quando a cota renova.
    with contextlib.suppress(Exception):
        import services.cota_service as _cota

        if _cota.esta_pausado():
            return
    while _queue and _count_running() < MAX_PARALLEL:
        next_paj = _queue.pop(0)
        session = _sessions.get(next_paj)
        if not session:
            continue
        # Promove: inicia o subprocess agora
        session.status = "idle"  # reset pra iniciar
        _iniciar_sessao(session)


def get_or_create_session(paj_norm: str, skill_slug: str | None = None) -> ChatSession:
    """Retorna sessao existente ou cria nova (sem iniciar)."""
    if paj_norm in _sessions and _sessions[paj_norm].is_alive():
        return _sessions[paj_norm]
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
    session = ChatSession(paj_norm, skill_slug=skill_slug)
    _sessions[paj_norm] = session
    return session


def start_or_queue(
    paj_norm: str,
    skill_slug: str | None = None,
    resume_session_id: str | None = None,
    *,
    effort_override: str | None = None,
    modelo_override: str | None = None,
    encadear_elaboracao: bool = False,
    instrucao_elaboracao: str | None = None,
    instrucao_inicial: str | None = None,
) -> dict:
    """Inicia sessao se houver slot, senao enfileira. Retorna status atual.

    `resume_session_id`: retoma uma sessao anterior via --resume (re-disparo de
    cota / Fase 2 nao-hard da triagem).
    `effort_override`/`modelo_override`: forcam effort/modelo desta sessao (ex:
    'max' na Fase 2 de um HARD CASE).
    `encadear_elaboracao`: ao concluir (Fase 1 analisar-processo), dispara a
    Fase 2 (elaboracao) via triagem_lote.
    `instrucao_elaboracao`: se setada, inicia em modo Fase 2 (instrucao custom
    em vez do PROMPT_MAX padrao).
    """
    existing = _sessions.get(paj_norm)
    # Se ja esta rodando, nao faz nada
    if existing and existing.is_alive() and existing.status == "running":
        return {"status": "running", "last_action": existing.last_action}
    # Se esta na fila, permanece
    if paj_norm in _queue:
        return {"status": "queued", "last_action": "aguardando slot"}

    # Recria sessao limpa (com a skill escolhida — pode ser diferente da anterior)
    if existing:
        existing.stop()
    session = ChatSession(paj_norm, skill_slug=skill_slug)
    # Elaboracao em background: encerra o subprocess ao concluir pra liberar o
    # slot (essencial pro lote — senao a fila trava). Correcoes posteriores
    # reabrem a sessao via start_correcao().
    session.encerrar_ao_concluir = True
    session.effort_override = effort_override
    session.modelo_override = modelo_override
    session.encadear_elaboracao = encadear_elaboracao
    session.instrucao_elaboracao = instrucao_elaboracao
    session.instrucao_inicial = instrucao_inicial
    if resume_session_id:
        session.resume_session_id = resume_session_id
    _sessions[paj_norm] = session

    if _count_running() >= MAX_PARALLEL:
        # Enfileira
        session.status = "queued"
        session.last_action = f"aguardando slot (fila: {len(_queue) + 1})"
        _queue.append(paj_norm)
        return {"status": "queued", "last_action": session.last_action}

    # Ha slot livre — inicia imediatamente
    _iniciar_sessao(session)
    return {"status": session.status, "last_action": session.last_action}


def stop_session(paj_norm: str):
    if paj_norm in _queue:
        _queue.remove(paj_norm)
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
        del _sessions[paj_norm]
    _process_queue()


def get_stats() -> dict:
    return {
        "running": _count_running(),
        "queued": len(_queue),
        "max_parallel": MAX_PARALLEL,
    }


# ----- Elaboracao em lote (toda a caixa de uma vez) -----


def _em_andamento(paj_norm: str) -> bool:
    """True se ja ha sessao rodando ou enfileirada para este PAJ."""
    if paj_norm in _queue:
        return True
    s = _sessions.get(paj_norm)
    return bool(s and s.status in ("running", "queued"))


def start_lote(
    pajs: list[str], skill_slug: str | None = None, auto_rotear: bool = False
) -> dict:
    """Triagem + disparo de elaboracao para varios PAJs de uma vez.

    Modos:
      - `skill_slug` setada: aplica a MESMA skill a todos (override manual).
      - `auto_rotear=True` (sem skill): TRIAGEM INTELIGENTE — roteia por PAJ via
        services.triagem_lote (com processo -> analisar-processo + pipeline de 2
        fases com effort condicional; sem processo -> firac-triagem).
      - nenhum dos dois: modo autonomo legado (Claude decide).
    Reusa start_or_queue/MAX_PARALLEL: os primeiros iniciam, o excedente enfileira.

    Elegibilidade (ordem de checagem = prioridade do motivo no relatorio):
      1. esta na caixa (em_caixa_atual != False) e e' conhecido
      2. NAO esta com sincronizacao incompleta (sync_incompleto)
      3. TEM anexos baixados (n_anexos_sisdpu > 0)
      4. ainda NAO foi elaborado com sucesso
      5. NAO esta ja rodando/na fila

    Os inelegiveis sao devolvidos agrupados por motivo, para o popup do front
    informar quais PAJs nao foram elaborados (e por que).
    """
    # Snapshot do estado de disco (anexos, sync, em_caixa, pecas) sem gerar
    # PROMPT_MAX para cada PAJ — reusa listar_pajs (cacheado, ja traz tudo).
    mapa = {p["paj_norm"]: p for p in listar_pajs(incluir_concluidos=True)}

    enviados: list[dict] = []
    sem_anexos: list[str] = []
    sync_incompleto: list[dict] = []
    ja_elaborados: list[str] = []
    em_andamento: list[str] = []
    indisponiveis: list[str] = []

    for paj_norm in pajs:
        item = mapa.get(paj_norm)
        if not item or item.get("em_caixa_atual") is False:
            indisponiveis.append(paj_norm)
            continue
        if item.get("sync_incompleto"):
            sync_incompleto.append(
                {
                    "paj": paj_norm,
                    "motivo": item.get("sync_incompleto_motivo") or "sincronização incompleta",
                }
            )
            continue
        if not item.get("n_anexos_sisdpu", 0):
            sem_anexos.append(paj_norm)
            continue
        # "Ja elaborado" = TEM peca gerada na pasta. Nao basta o status "done"
        # do disco: uma rodada interrompida (ex: limite de uso do Claude)
        # conclui sem produto — e precisa continuar elegivel pra reelaboracao.
        if item.get("n_pecas", 0) > 0:
            ja_elaborados.append(paj_norm)
            continue
        if _em_andamento(paj_norm):
            em_andamento.append(paj_norm)
            continue
        if auto_rotear and not skill_slug:
            # Triagem inteligente: roteia skill/fluxo por PAJ (com processo ->
            # pipeline analisar-processo; sem -> firac-triagem).
            from services import triagem_lote

            res = triagem_lote.disparar_paj(paj_norm, item)
            enviados.append(
                {
                    "paj": paj_norm,
                    "status": res.get("status", "running"),
                    "skill": res.get("skill"),
                    "fluxo": res.get("fluxo"),
                }
            )
        else:
            res = start_or_queue(paj_norm, skill_slug=skill_slug)
            enviados.append({"paj": paj_norm, "status": res.get("status", "running")})

    return {
        "ok": True,
        "skill": skill_slug or "",
        "auto_rotear": bool(auto_rotear and not skill_slug),
        "total": len(pajs),
        "n_enviados": len(enviados),
        "enviados": enviados,
        "pulados": {
            "sync_incompleto": sync_incompleto,
            "sem_anexos": sem_anexos,
            "ja_elaborados": ja_elaborados,
            "em_andamento": em_andamento,
            "indisponiveis": indisponiveis,
        },
        "stats": get_stats(),
    }
