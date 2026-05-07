"""Servico para executar Claude Code CLI como subprocess com streaming (non-interactive)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import queue
from collections.abc import AsyncGenerator

from config import OFICIO_GERAL, PAJS_DIR

CLAUDE_CMD = "claude"


async def elaborar_peca(paj_norm: str) -> AsyncGenerator[str, None]:
    """Roda `claude -p` com o PROMPT_MAX do PAJ e faz yield do texto gerado."""
    prompt_path = PAJS_DIR / paj_norm / "PROMPT_MAX.md"
    if not prompt_path.exists():
        yield "[ERRO] PROMPT_MAX.md nao encontrado para este PAJ.\n"
        return

    prompt_content = prompt_path.read_text(encoding="utf-8", errors="replace")

    instrucao = (
        "Analise o PAJ abaixo conforme as instrucoes do CLAUDE.md deste workspace "
        "(Oficio Geral). Decida o proximo passo cabivel (despacho, peticao, recurso, "
        "manifestacao, etc.) e produza o TEXTO pronto da peca. "
        f"Salve o resultado em `PAJs/{paj_norm}/`.\n\n"
        "---\n\n"
        f"{prompt_content}"
    )

    cmd = [
        CLAUDE_CMD,
        "-p",
        "--verbose",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--permission-mode", "bypassPermissions",
    ]

    yield f"[dpuscript-ui] Elaborando peca para PAJ {paj_norm}...\n"

    q: queue.Queue[str | None] = queue.Queue()

    def _run():
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(OFICIO_GERAL),
            )
            proc.stdin.write(instrucao.encode("utf-8"))
            proc.stdin.close()

            for line in iter(proc.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    etype = event.get("type", "")

                    if etype == "stream_event":
                        inner = event.get("event", {})
                        if inner.get("type") == "content_block_delta":
                            delta = inner.get("delta", {})
                            if delta.get("type") == "text_delta":
                                q.put(delta.get("text", ""))
                        elif inner.get("type") == "content_block_start":
                            block = inner.get("content_block", {})
                            if block.get("type") == "tool_use":
                                q.put(f"\n[tool: {block.get('name', '?')}] ")
                    elif etype == "result":
                        result_text = event.get("result", "")
                        if isinstance(result_text, dict):
                            result_text = result_text.get("text", "")
                        if result_text:
                            q.put(f"\n\n{result_text}")
                        session_id = event.get("session_id", "")
                        q.put(f"\n\n[dpuscript-ui] Concluido (session={session_id[:8]})\n")
                except json.JSONDecodeError:
                    q.put(text + "\n")

            stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
            proc.wait()
            if proc.returncode != 0:
                q.put(f"\n[ERRO] Claude Code saiu com codigo {proc.returncode}\n")
                if stderr_out:
                    q.put(f"[stderr] {stderr_out[:500]}\n")
        except FileNotFoundError:
            q.put("[ERRO] Comando 'claude' nao encontrado no PATH.\n")
        except Exception as e:
            q.put(f"[ERRO] {type(e).__name__}: {e}\n")
        finally:
            q.put(None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while True:
        try:
            chunk = q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        if chunk is None:
            break
        yield chunk
