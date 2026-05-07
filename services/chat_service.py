"""Servico de chat interativo com Claude Code CLI via stream-json bidirecional."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import queue

from config import OFICIO_GERAL, PAJS_DIR
from services import historico
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
        tipo_saida = "[DESPACHO | PETICAO | RECURSO | MANIFESTACAO | OFICIO | ORIENTACAO | OUTRO — <tipo>]"

    return (
        cabecalho
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
        self.skill_slug: str | None = skill_slug if skill_slug and skill_valida(skill_slug) else None
        self.output_queue: queue.Queue[dict] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._alive = False
        # Estado pra UI de background:
        # "idle" | "running" | "done" | "error"
        self.status: str = "idle"
        self.last_action: str = ""       # "Usando Glob", "Escrevendo peca", etc.
        self.accumulated_text: str = ""  # texto acumulado da resposta atual
        self.summary: str = ""           # resumo final (ultima resposta do Claude)
        self.error: str = ""

    def _start_subprocess(self) -> bool:
        """Inicia o subprocess Claude Code CLI e a thread leitora.

        Retorna True se o processo foi aberto com sucesso; em caso de falha,
        preenche self.error e enfileira os eventos de erro/done.
        """
        cmd = [
            *CLAUDE_CMD,
            "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
        ]
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

    def start(self) -> bool:
        """Inicia o subprocess Claude Code em modo stream-json."""
        prompt_path = PAJS_DIR / self.paj_norm / "PROMPT_MAX.md"
        if not prompt_path.exists():
            self.output_queue.put({"type": "error", "text": "PROMPT_MAX.md nao encontrado."})
            self.output_queue.put({"type": "done"})
            return False

        if not self._start_subprocess():
            return False

        # Envia PROMPT_MAX como prompt inicial
        prompt_content = prompt_path.read_text(encoding="utf-8", errors="replace")
        paj_pasta = PAJS_DIR / self.paj_norm
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
            # Guarda o texto acumulado como summary (e o texto final desse turno)
            if self.accumulated_text.strip():
                self.summary = self.accumulated_text
            elif isinstance(result_text, str) and result_text:
                self.summary = result_text
            self.status = "done"
            self.last_action = "aguardando sua resposta"
            session_id = event.get("session_id", "")[:8]
            # Persiste em disco pra sobreviver reinicio do servidor
            self._persist()
            return {
                "type": "result",
                "text": result_text if isinstance(result_text, str) else "",
                "session_id": session_id,
            }

        # assistant_full — ignora (ja temos via deltas)
        if etype == "assistant":
            return None

        # System events — ignora
        if etype == "system":
            return None

        # rate_limit_event — ignora
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
                resumo=historico._primeira_linha_util(self.summary),
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
        IGNORAR = {"metadata.json", "eventos_tnu.json", "datajud.json", "PROMPT_MAX.md", "elaboracao.json"}
        gerados = [x for x in pasta.iterdir() if x.is_file() and x.name not in IGNORAR]
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


def _process_queue() -> None:
    """Promove proximos da fila enquanto houver slots livres."""
    while _queue and _count_running() < MAX_PARALLEL:
        next_paj = _queue.pop(0)
        session = _sessions.get(next_paj)
        if not session:
            continue
        # Promove: inicia o subprocess agora
        session.status = "idle"  # reset pra start() funcionar
        session.start()


def get_or_create_session(paj_norm: str, skill_slug: str | None = None) -> ChatSession:
    """Retorna sessao existente ou cria nova (sem iniciar)."""
    if paj_norm in _sessions and _sessions[paj_norm].is_alive():
        return _sessions[paj_norm]
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
    session = ChatSession(paj_norm, skill_slug=skill_slug)
    _sessions[paj_norm] = session
    return session


def start_or_queue(paj_norm: str, skill_slug: str | None = None) -> dict:
    """Inicia sessao se houver slot, senao enfileira. Retorna status atual."""
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
    _sessions[paj_norm] = session

    if _count_running() >= MAX_PARALLEL:
        # Enfileira
        session.status = "queued"
        session.last_action = f"aguardando slot (fila: {len(_queue) + 1})"
        _queue.append(paj_norm)
        return {"status": "queued", "last_action": session.last_action}

    # Ha slot livre — inicia imediatamente
    session.start()
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
