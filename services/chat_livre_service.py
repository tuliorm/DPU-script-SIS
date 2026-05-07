"""Chat livre VINCULADO a PAJ — modo discussao.

Cada conversa pertence a um PAJ e existe pra apoiar o trabalho desse caso
especifico: explorar tese, fazer pergunta, testar hipotese, brainstorm
estrategico. Diferente do chat de ELABORACAO (`chat_service.ChatSession`),
este NAO obriga a produzir peca — o Claude responde livre, com o contexto
do PAJ ja na mesa.

Diferencas chave do chat de elaboracao:
  - Carrega o PROMPT_MAX do PAJ como contexto inicial (nao como gatilho de
    producao de peca)
  - Instrui explicitamente "modo discussao" — sem produzir peca a menos
    que o Defensor peca
  - Persiste conversa em `PAJs/<paj_norm>/conversas/<uuid>.json` — varias
    conversas independentes podem coexistir no mesmo PAJ
  - Sobrevive a reinicio do servidor: reabrir uma conversa antiga injeta
    contexto de retomada (historico das ultimas mensagens) na primeira
    mensagem nova ao Claude

Cwd do subprocess continua sendo OFICIO_GERAL — Claude tem acesso a
CLAUDE.md, skills, hooks, MEMORY.md e bases.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from config import OFICIO_GERAL, PAJS_DIR
from services.skills_catalog import skill_descricao, skill_valida


# ---------------------------------------------------------------------------
# Persistencia em disco (em PAJs/<paj_norm>/conversas/<id>.json)
# ---------------------------------------------------------------------------

def _validar_paj_norm(paj_norm: str) -> bool:
    """Aceita apenas o formato canonico PAJ-YYYY-NNN-NNNNN. Bloqueia path
    traversal."""
    if not paj_norm or "/" in paj_norm or "\\" in paj_norm or ".." in paj_norm:
        return False
    if not paj_norm.startswith("PAJ-"):
        return False
    pasta = PAJS_DIR / paj_norm
    return pasta.exists() and pasta.is_dir()


def _validar_id(conv_id: str) -> bool:
    """Aceita apenas UUIDs canonicos."""
    if not conv_id:
        return False
    try:
        uuid.UUID(conv_id)
        return True
    except (ValueError, AttributeError):
        return False


def _pasta_conversas(paj_norm: str) -> Path | None:
    if not _validar_paj_norm(paj_norm):
        return None
    return PAJS_DIR / paj_norm / "conversas"


def _arquivo_em_paj(paj_norm: str, conv_id: str) -> Path | None:
    if not _validar_id(conv_id):
        return None
    pasta = _pasta_conversas(paj_norm)
    if pasta is None:
        return None
    return pasta / f"{conv_id}.json"


def _localizar_conversa(conv_id: str) -> tuple[str, Path] | None:
    """Varre todos os PAJs procurando uma conversa pelo ID.

    Retorna (paj_norm, path) ou None. Custo O(numero de PAJs); aceitavel pra
    UI single-user. Quando o caller ja sabe o paj_norm, usar `_arquivo_em_paj`
    direto e' mais rapido.
    """
    if not _validar_id(conv_id):
        return None
    if not PAJS_DIR.exists():
        return None
    nome = f"{conv_id}.json"
    for paj_dir in PAJS_DIR.iterdir():
        if not paj_dir.is_dir() or not paj_dir.name.startswith("PAJ-"):
            continue
        candidato = paj_dir / "conversas" / nome
        if candidato.exists():
            return (paj_dir.name, candidato)
    return None


def _agora_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def criar_conversa(paj_norm: str, *, titulo: str = "",
                   skill_slug: str | None = None) -> dict | None:
    """Cria conversa nova vinculada a um PAJ. Retorna dict completo ou None
    se paj_norm invalido."""
    pasta = _pasta_conversas(paj_norm)
    if pasta is None:
        return None
    pasta.mkdir(parents=True, exist_ok=True)

    conv_id = str(uuid.uuid4())
    skill = skill_slug if skill_slug and skill_valida(skill_slug) else None
    agora = _agora_iso()
    conversa = {
        "id": conv_id,
        "paj_norm": paj_norm,
        "titulo": titulo.strip() or "Nova conversa",
        "skill_slug": skill,
        "criado_em": agora,
        "atualizado_em": agora,
        "mensagens": [],
    }
    _gravar(paj_norm, conv_id, conversa)
    return conversa


def listar_conversas_paj(paj_norm: str, limit: int = 200) -> list[dict]:
    """Listagem leve das conversas de um PAJ (sem array de mensagens)."""
    pasta = _pasta_conversas(paj_norm)
    if pasta is None or not pasta.exists():
        return []
    out: list[dict] = []
    for f in pasta.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or "id" not in data:
            continue
        out.append({
            "id": data.get("id", ""),
            "paj_norm": data.get("paj_norm", paj_norm),
            "titulo": data.get("titulo") or "Sem título",
            "skill_slug": data.get("skill_slug"),
            "criado_em": data.get("criado_em", ""),
            "atualizado_em": data.get("atualizado_em", ""),
            "n_mensagens": len(data.get("mensagens") or []),
        })
    out.sort(key=lambda c: c.get("atualizado_em") or c.get("criado_em") or "", reverse=True)
    return out[:limit]


def ler_conversa(conv_id: str, paj_norm: str | None = None) -> dict | None:
    """Le a conversa do disco. Se paj_norm conhecido, vai direto; senao
    varre todos os PAJs."""
    if paj_norm:
        f = _arquivo_em_paj(paj_norm, conv_id)
        if f and f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("id") == conv_id:
                    return data
            except Exception:
                return None
        return None

    achado = _localizar_conversa(conv_id)
    if not achado:
        return None
    _, f = achado
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id") == conv_id:
            return data
    except Exception:
        return None
    return None


def remover_conversa(conv_id: str) -> bool:
    """Apaga conversa do disco e encerra sessao em memoria, se houver."""
    if not _validar_id(conv_id):
        return False
    if conv_id in _sessions:
        with contextlib.suppress(Exception):
            _sessions[conv_id].stop()
        _sessions.pop(conv_id, None)
    achado = _localizar_conversa(conv_id)
    if achado:
        _, f = achado
        with contextlib.suppress(Exception):
            f.unlink()
    return True


def atualizar_metadata(conv_id: str, *, titulo: str | None = None,
                       skill_slug: str | None = None) -> dict | None:
    """Renomeia titulo / muda skill default. Convencao:
      - parametro None  => nao mexe
      - parametro "" em skill_slug => remove skill
      - string nao vazia em titulo => substitui (vazia preserva atual)
    """
    achado = _localizar_conversa(conv_id)
    if not achado:
        return None
    paj_norm, _ = achado
    conversa = ler_conversa(conv_id, paj_norm=paj_norm)
    if not conversa:
        return None
    if titulo is not None:
        novo = (titulo or "").strip()
        conversa["titulo"] = novo or conversa.get("titulo") or "Sem título"
    if skill_slug is not None:
        conversa["skill_slug"] = (
            skill_slug if skill_slug and skill_valida(skill_slug) else None
        )
    conversa["atualizado_em"] = _agora_iso()
    _gravar(paj_norm, conv_id, conversa)
    return conversa


def _adicionar_mensagem(paj_norm: str, conv_id: str, role: str, texto: str) -> None:
    """Append de mensagem. Auto-titulo a partir da 1a mensagem do user."""
    conversa = ler_conversa(conv_id, paj_norm=paj_norm)
    if not conversa:
        return
    msg = {"role": role, "texto": texto or "", "ts": _agora_iso()}
    msgs = conversa.setdefault("mensagens", [])
    msgs.append(msg)
    if (
        role == "user"
        and (conversa.get("titulo") or "").strip().lower() == "nova conversa"
        and texto.strip()
    ):
        primeira = texto.strip().splitlines()[0].strip()
        if primeira:
            conversa["titulo"] = primeira[:60] + ("..." if len(primeira) > 60 else "")
    conversa["atualizado_em"] = _agora_iso()
    _gravar(paj_norm, conv_id, conversa)


def _gravar(paj_norm: str, conv_id: str, conversa: dict) -> None:
    f = _arquivo_em_paj(paj_norm, conv_id)
    if f is None:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        f.write_text(json.dumps(conversa, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Sessao Claude
# ---------------------------------------------------------------------------

def _resolver_claude_cmd() -> list[str]:
    """Resolve `claude` via shutil.which; no Windows envolve com cmd.exe /c
    se for batch (.cmd/.bat)."""
    resolved = shutil.which("claude")
    if not resolved:
        return ["claude"]
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]
    return [resolved]


CLAUDE_CMD: list[str] = _resolver_claude_cmd()


def _ler_prompt_max(paj_norm: str) -> str:
    """Le o PROMPT_MAX.md do PAJ (se existir; gera sob demanda se ausente)."""
    pasta = PAJS_DIR / paj_norm
    prompt_path = pasta / "PROMPT_MAX.md"
    if not prompt_path.exists():
        with contextlib.suppress(Exception):
            from services.prompt_builder import gerar_prompt_max
            gerar_prompt_max(paj_norm)
    if prompt_path.exists():
        try:
            return prompt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _montar_contexto_retomada(mensagens: list[dict]) -> str:
    """Bloco de contexto pra Claude continuar conversa apos restart do
    subprocess. Limita a ultimas 20 mensagens."""
    if not mensagens:
        return ""
    LIMITE = 20
    janela = mensagens[-LIMITE:]
    foi_truncado = len(mensagens) > LIMITE

    linhas: list[str] = []
    linhas.append(
        "Esta e' a continuacao de uma conversa anterior. A sessao do Claude foi "
        "reiniciada — voce nao tem o estado interno; use o historico abaixo "
        "para retomar naturalmente."
    )
    if foi_truncado:
        linhas.append(
            f"\n[A conversa tem {len(mensagens)} mensagens no total — "
            f"mostrando as {LIMITE} mais recentes.]"
        )
    linhas.append("")
    linhas.append("--- INICIO DO HISTORICO ---")
    for msg in janela:
        rotulo = "Defensor" if msg.get("role") == "user" else "Claude"
        texto = (msg.get("texto") or "").strip()
        if not texto:
            continue
        linhas.append(f"\n[{rotulo}]:")
        linhas.append(texto)
    linhas.append("\n--- FIM DO HISTORICO ---")
    linhas.append("")
    linhas.append(
        "Aguarde a proxima mensagem do Defensor (vai chegar logo apos esta) "
        "e responda dando continuidade natural a conversa. Nao precisa "
        "resumir o historico — apenas continue."
    )
    return "\n".join(linhas)


def _montar_prompt_inicial(paj_norm: str, skill_slug: str | None) -> str:
    """Cabecalho da PRIMEIRA mensagem da conversa (sem historico previo).

    Carrega o PROMPT_MAX do PAJ como contexto e instrui modo discussao
    (sem obrigacao de produzir peca).
    """
    prompt_max = _ler_prompt_max(paj_norm)

    skill_bloco = ""
    if skill_slug:
        desc = skill_descricao(skill_slug)
        skill_bloco = (
            f"\nSkill default desta conversa: `/{skill_slug}` — {desc}\n"
            "Invoque-a se a pergunta do Defensor pedir o produto que ela faz; "
            "caso contrario, responda livre.\n"
        )

    return (
        f"Voce esta em **modo discussao** sobre o PAJ `{paj_norm}` no workspace "
        "Oficio Geral (CLAUDE.md aplica — terminologia DPU, estilo, bases, "
        "MEMORY.md, hooks).\n\n"
        "Esta NAO e' uma sessao de elaboracao de peca. O Defensor vai usar "
        "este chat para explorar tese, testar hipotese, fazer pergunta sobre "
        "o caso, brainstorm estrategico, esclarecer duvida juridica vinculada "
        "ao PAJ. **Nao produza peca formal** a menos que o Defensor peca "
        "explicitamente; nesse caso, siga as skills do workspace normalmente.\n"
        f"{skill_bloco}\n"
        "Contexto do PAJ (gerado pelo painel — pode ler `sisdpu.txt` na pasta "
        "do PAJ se precisar de mais detalhes):\n\n"
        "--- INICIO DO PROMPT_MAX ---\n"
        f"{prompt_max.strip() if prompt_max else '(PROMPT_MAX nao disponivel)'}\n"
        "--- FIM DO PROMPT_MAX ---\n\n"
        "Aguarde a pergunta do Defensor logo abaixo e responda no estilo "
        "tecnico-direto esperado, citando precedente/doutrina apenas quando "
        "houver na base local ou no proprio PAJ (CLAUDE.md proibe inventar)."
    )


class ChatLivreSession:
    """Sessao Claude vinculada a uma conversa de PAJ."""

    def __init__(self, conv_id: str, paj_norm: str):
        self.conv_id = conv_id
        self.paj_norm = paj_norm
        self.output_queue: queue.Queue[dict] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._alive = False
        self.status: str = "idle"
        self.last_action: str = ""
        self.accumulated_text: str = ""
        self.error: str = ""
        self._contexto_injetado = False

    def _start_subprocess(self) -> bool:
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
                f"Comando Claude CLI nao encontrado (tentou: {' '.join(CLAUDE_CMD)}). "
                "Verifique se `claude` esta no PATH."
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
        conversa = ler_conversa(self.conv_id, paj_norm=self.paj_norm)
        if not conversa:
            self.error = "Conversa nao encontrada."
            self.output_queue.put({"type": "error", "text": self.error})
            self.output_queue.put({"type": "done"})
            return False
        if not self._start_subprocess():
            return False
        self.status = "idle"
        self.last_action = "aguardando primeira pergunta"
        self._contexto_injetado = False
        return True

    def send_message(self, texto: str) -> None:
        if not self.proc or not self._alive:
            self.output_queue.put({"type": "error", "text": "Sessao nao esta ativa."})
            return

        # Persiste a mensagem do user antes de enviar
        _adicionar_mensagem(self.paj_norm, self.conv_id, "user", texto)
        self.output_queue.put({"type": "user", "text": texto})

        if not self._contexto_injetado:
            conversa = ler_conversa(self.conv_id, paj_norm=self.paj_norm) or {}
            mensagens = conversa.get("mensagens") or []
            historico_anterior = mensagens[:-1] if mensagens else []
            skill_slug = conversa.get("skill_slug")

            if historico_anterior:
                # Conversa reaberta: contexto de retomada (sem PROMPT_MAX porque
                # o historico ja contem a discussao em andamento).
                contexto = _montar_contexto_retomada(historico_anterior)
                payload = (
                    contexto
                    + "\n\n--- NOVA MENSAGEM DO DEFENSOR ---\n\n"
                    + texto
                )
            else:
                # Conversa nova: cabecalho com PROMPT_MAX como contexto.
                cabecalho = _montar_prompt_inicial(self.paj_norm, skill_slug)
                payload = cabecalho + "\n\n--- PERGUNTA DO DEFENSOR ---\n\n" + texto

            self._contexto_injetado = True
        else:
            payload = texto

        self.accumulated_text = ""
        self.status = "running"
        self.last_action = "Claude pensando..."

        msg = {"type": "user", "message": {"role": "user", "content": payload}}
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._alive = False
            self.status = "error"
            self.error = "Subprocess Claude morreu."
            self.output_queue.put({"type": "error", "text": self.error})

    def _read_output(self) -> None:
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
            with contextlib.suppress(Exception):
                _process_queue()

    def _parse_event(self, event: dict) -> dict | None:
        etype = event.get("type", "")

        if etype == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")

            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    self.accumulated_text += chunk
                    self.last_action = "escrevendo resposta..."
                    return {"type": "text", "text": chunk}

            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "?")
                    self.last_action = f"usando {tool_name}"
                    return {"type": "tool", "text": f"[{tool_name}]"}
            return None

        if etype == "result":
            if self.accumulated_text.strip():
                _adicionar_mensagem(
                    self.paj_norm, self.conv_id, "assistant", self.accumulated_text
                )
            self.status = "idle"
            self.last_action = "aguardando proxima pergunta"
            return {"type": "result", "session_id": event.get("session_id", "")[:8]}

        return None

    def is_alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._alive = False
        if self.proc:
            with contextlib.suppress(Exception):
                self.proc.terminate()


# ---------------------------------------------------------------------------
# Pool de sessoes em memoria
# ---------------------------------------------------------------------------

_sessions: dict[str, ChatLivreSession] = {}
_queue: list[str] = []
MAX_PARALLEL = 5


def _count_running() -> int:
    return sum(1 for s in _sessions.values() if s.status == "running")


def _process_queue() -> None:
    while _queue and _count_running() < MAX_PARALLEL:
        next_id = _queue.pop(0)
        sess = _sessions.get(next_id)
        if not sess:
            continue
        sess.status = "idle"
        sess.start()


def get_or_create_session(conv_id: str) -> ChatLivreSession | None:
    """Reusa sessao existente ou cria uma nova. Retorna None se conv_id
    invalido ou conversa nao encontrada em nenhum PAJ."""
    if not _validar_id(conv_id):
        return None
    if conv_id in _sessions and _sessions[conv_id].is_alive():
        return _sessions[conv_id]
    if conv_id in _sessions:
        _sessions[conv_id].stop()

    achado = _localizar_conversa(conv_id)
    if not achado:
        return None
    paj_norm, _ = achado

    sess = ChatLivreSession(conv_id, paj_norm)
    _sessions[conv_id] = sess
    return sess


def stop_session(conv_id: str) -> None:
    if conv_id in _queue:
        _queue.remove(conv_id)
    if conv_id in _sessions:
        _sessions[conv_id].stop()
        _sessions.pop(conv_id, None)
    _process_queue()


def get_stats() -> dict:
    return {
        "running": _count_running(),
        "active_sessions": len(_sessions),
        "queued": len(_queue),
        "max_parallel": MAX_PARALLEL,
    }
