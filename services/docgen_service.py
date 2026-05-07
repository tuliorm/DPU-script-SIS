"""Geracao de DOCX/PDF via gerar_docx.py / gerar_peticao.py do workspace.

Os scripts gravam em `SAIDA_DIR` hardcoded (Google Drive). Este servico:
1. Invoca o script passando o .txt de entrada (path absoluto dentro do PAJ)
2. Faz streaming linha-por-linha do stdout/stderr pro SSE
3. Ao final, copia o arquivo gerado da SAIDA_DIR pra pasta do PAJ
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
from typing import Literal
from collections.abc import AsyncGenerator

from config import (
    DOCGEN_OUT_DIR,
    GERAR_DOCX_SCRIPT,
    GERAR_PECA_SCRIPT,
    OFICIO_GERAL,
    PAJS_DIR,
)
from services import historico


# Pasta onde os scripts gerar_docx.py / gerar_peticao.py gravam a saida.
# Configuravel via DOCGEN_OUT_DIR no .env. Default: <OFICIO_GERAL>/Peças Feitas.
_SAIDA_DIR_EXTERNA = DOCGEN_OUT_DIR


async def gerar_artefato(
    paj_norm: str,
    arquivo_txt: str,
    formato: Literal["docx", "pdf"],
) -> AsyncGenerator[str, None]:
    """Gera DOCX ou PDF a partir de um .txt dentro da pasta do PAJ.

    Yields linhas de log pra streaming (SSE).
    """
    pasta = PAJS_DIR / paj_norm
    txt_path = pasta / arquivo_txt

    # Seguranca contra path traversal
    try:
        txt_path.resolve().relative_to(pasta.resolve())
    except ValueError:
        yield f"[ERRO] caminho invalido: {arquivo_txt}\n"
        return

    if not txt_path.exists() or not txt_path.is_file():
        yield f"[ERRO] arquivo nao encontrado: {txt_path}\n"
        return

    if formato == "docx":
        script = GERAR_DOCX_SCRIPT
    elif formato == "pdf":
        script = GERAR_PECA_SCRIPT
    else:
        yield f"[ERRO] formato invalido: {formato} (use docx ou pdf)\n"
        return

    if not script.exists():
        yield f"[ERRO] script nao encontrado: {script}\n"
        return

    nome_saida = f"{txt_path.stem}.{formato}"

    yield f"[docgen] gerando {formato.upper()} de {arquivo_txt}...\n"
    yield f"[docgen] script: {script.name}\n"

    cmd = [
        sys.executable,
        str(script),
        str(txt_path),
        nome_saida,
    ]

    q: asyncio.Queue[str | None] = asyncio.Queue()

    def _run() -> None:
        """Roda subprocess em thread separada e joga linhas na queue."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(OFICIO_GERAL),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            for linha in iter(proc.stdout.readline, ""):
                asyncio.run_coroutine_threadsafe(
                    q.put(linha.rstrip("\n")), loop
                )
            proc.wait()
            if proc.returncode != 0:
                asyncio.run_coroutine_threadsafe(
                    q.put(f"[ERRO] script saiu com codigo {proc.returncode}"),
                    loop,
                )
        except FileNotFoundError as e:
            asyncio.run_coroutine_threadsafe(
                q.put(f"[ERRO] comando nao encontrado: {e}"), loop
            )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                q.put(f"[ERRO] {type(e).__name__}: {e}"), loop
            )
        finally:
            asyncio.run_coroutine_threadsafe(q.put(None), loop)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run)

    while True:
        linha = await q.get()
        if linha is None:
            break
        yield linha + "\n"

    # Copia arquivo da SAIDA_DIR externa pra pasta do PAJ
    origem = _SAIDA_DIR_EXTERNA / nome_saida
    destino = pasta / nome_saida

    if origem.exists():
        try:
            shutil.copy2(origem, destino)
            yield f"[docgen] arquivo copiado para o PAJ: {destino.name}\n"
            # Sucesso: registra no historico do PAJ (best-effort).
            with contextlib.suppress(Exception):
                historico.registrar(
                    paj_norm,
                    f"gerar_{formato}",
                    origem_txt=arquivo_txt,
                    arquivo=destino.name,
                )
        except Exception as e:
            yield f"[AVISO] gerado em {origem} mas falhou ao copiar: {e}\n"
    else:
        yield (
            f"[AVISO] arquivo esperado nao encontrado em {origem} — "
            "veja os logs acima.\n"
        )
