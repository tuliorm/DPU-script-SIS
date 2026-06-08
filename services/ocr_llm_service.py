"""Fallback OCR-LLM: transcreve PDFs cujo OCR local (Tesseract) falhou.

Quando o `.txt` companion de um anexo esta vazio/ilegivel/curto, um modelo BARATO
(Sonnet, por visao) le o PDF e transcreve o texto. O `.txt` melhorado fica
cacheado (com marcador de procedencia) e e' reusado pelo Opus na elaboracao —
sem precisar o Opus xhigh ler o PDF com visao toda vez (caro e repetido).

Acionado ANTES da elaboracao (chat_service.start -> melhorar_ocr_paj) e tambem
sob demanda (endpoint /api/paj/{paj}/melhorar-ocr). Idempotente: nao retranscreve
um `.txt` que ja traz o marcador `[OCR-LLM:`.

Best-effort: qualquer falha (inclusive cota) e' registrada e nao interrompe a
elaboracao — o Opus ainda pode ler o PDF com visao como ultimo recurso.
"""

from __future__ import annotations

import json
import logging
import subprocess

from config import (
    OCR_LLM_ATIVO,
    OCR_LLM_EFFORT,
    OCR_LLM_MAX_PAGINAS,
    OCR_LLM_MODELO,
    OCR_LLM_TIMEOUT_SEG,
    OFICIO_GERAL,
    PAJS_DIR,
)

# Reusa a resolucao do binario `claude` ja feita pelo chat_service (Windows
# .cmd etc). chat_service NAO importa este modulo no topo (so' tardiamente em
# start()), entao nao ha ciclo de import.
from services.chat_service import CLAUDE_CMD

log = logging.getLogger(__name__)

# Primeira linha gravada no .txt transcrito — serve de marcador de procedencia
# E de flag de idempotencia (nao retranscreve quem ja tem isto).
_MARCADOR = "[OCR-LLM:"


def ocr_fraco(texto: str | None) -> bool:
    """True se o texto OCR e' insuficiente e vale tentar o fallback LLM.

    Criterio alinhado ao usado no prompt_builder pra sinalizar 'OCR fraco':
    vazio, placeholder de OCR indisponivel, paginas ilegiveis, ou < 120 chars
    uteis. Um .txt JA transcrito por LLM (comeca com o marcador) NAO e' fraco.
    """
    if not texto:
        return True
    if texto.lstrip().startswith(_MARCADOR):
        return False
    t = texto.strip()
    if len(t) < 120:
        return True
    marcas = (
        "[OCR indisponivel",
        "[OCR indisponível",
        "[pagina ilegivel",
        "[página ilegível",
        "[PyMuPDF nao instalado",
    )
    return any(m in texto for m in marcas)


def _ja_transcrito(texto: str | None) -> bool:
    return bool(texto) and texto.lstrip().startswith(_MARCADOR)


def _contar_paginas(pdf_path) -> int | None:
    """Numero de paginas do PDF (via PyMuPDF). None se nao conseguir contar."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None
    try:
        doc = fitz.open(pdf_path)
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return None


def transcrever_pdf(pdf_path) -> tuple[str | None, str]:
    """Transcreve um PDF via Claude (Sonnet) por visao. Retorna (texto, motivo).

    motivo ∈ {"ok", "cota", "erro", "vazio"}. Best-effort: nunca levanta.
    """
    nome = pdf_path.name
    prompt = (
        f"Transcreva, de forma integral e fiel, TODO o texto do documento PDF "
        f"`{pdf_path}` — leia-o com sua capacidade de visão.\n\n"
        "Inclua cabeçalhos, corpo, datas, valores, nomes das partes, número de "
        "processo, assinaturas, carimbos e tabelas, preservando a ordem das "
        "páginas. NÃO resuma, NÃO comente, NÃO adicione nada além da transcrição. "
        "Para trechos ilegíveis, escreva [trecho ilegível]. "
        "Responda APENAS com o texto transcrito."
    )
    cmd = [
        *CLAUDE_CMD,
        "-p",
        prompt,
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
    ]
    if OCR_LLM_MODELO:
        cmd += ["--model", OCR_LLM_MODELO]
    if OCR_LLM_EFFORT:
        cmd += ["--effort", OCR_LLM_EFFORT]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(OFICIO_GERAL),
            capture_output=True,
            timeout=OCR_LLM_TIMEOUT_SEG,
        )
    except subprocess.TimeoutExpired:
        log.warning("[ocr-llm] timeout transcrevendo %s", nome)
        return None, "erro"
    except Exception as e:  # noqa: BLE001
        log.warning("[ocr-llm] falha ao invocar Claude p/ %s: %s", nome, e)
        return None, "erro"

    saida = proc.stdout.decode("utf-8", errors="replace").strip()
    if not saida:
        return None, "erro"
    try:
        data = json.loads(saida)
    except json.JSONDecodeError:
        # Sem --output-format json valido: usa o stdout cru como ultimo recurso.
        data = {"result": saida, "is_error": False}

    texto = data.get("result")
    if isinstance(texto, dict):
        texto = texto.get("text", "")
    texto = (texto or "").strip()
    low = texto.lower()
    if any(s in low for s in ("session limit", "usage limit", "hit your")):
        return None, "cota"
    if data.get("is_error") or str(data.get("subtype") or "").startswith("error"):
        return None, "erro"
    if len(texto) < 40:  # transcricao vazia/inutil
        return None, "vazio"
    return texto, "ok"


def melhorar_ocr_paj(paj_norm: str, log_fn=None) -> dict:
    """Transcreve, via Sonnet, os anexos PDF do PAJ cujo OCR esta fraco/ausente.

    Idempotente (pula quem ja tem o marcador `[OCR-LLM:`). Best-effort. Retorna
    {transcritos:[...], pulados:[...], falhas:[{nome,motivo}], cota:bool}.
    """

    def _log(msg: str) -> None:
        if log_fn:
            with __import__("contextlib").suppress(Exception):
                log_fn(msg)
        log.info(msg)

    res: dict = {"transcritos": [], "pulados": [], "grandes": [], "falhas": [], "cota": False}
    if not OCR_LLM_ATIVO:
        return res
    pasta = PAJS_DIR / paj_norm
    pecas = pasta / "pecas"
    if not pecas.is_dir():
        return res

    for pdf in sorted(pecas.iterdir()):
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            continue
        companion = pdf.with_suffix(".txt")
        conteudo = ""
        if companion.exists():
            with __import__("contextlib").suppress(Exception):
                conteudo = companion.read_text(encoding="utf-8", errors="replace")
        if _ja_transcrito(conteudo) or not ocr_fraco(conteudo):
            res["pulados"].append(pdf.name)
            continue

        # Teto de paginas: autos gigantes ficam pro Opus ler direto na
        # elaboracao (evita falha do leitor de PDF / timeout / custo). 0 = sem
        # limite. Se nao der pra contar, segue e tenta (o timeout protege).
        if OCR_LLM_MAX_PAGINAS:
            npag = _contar_paginas(pdf)
            if npag is not None and npag > OCR_LLM_MAX_PAGINAS:
                res["grandes"].append({"nome": pdf.name, "paginas": npag})
                _log(
                    f"  [ocr-llm] pulado (grande: {npag} pág > {OCR_LLM_MAX_PAGINAS}) "
                    f"— Opus lerá o PDF direto: {pdf.name}"
                )
                continue

        _log(f"  [ocr-llm] transcrevendo (Sonnet): {pdf.name}")
        texto, motivo = transcrever_pdf(pdf)
        if motivo == "cota":
            res["cota"] = True
            res["falhas"].append({"nome": pdf.name, "motivo": "cota"})
            _log("  [ocr-llm] cota esgotada — interrompendo transcrições")
            break
        if motivo != "ok" or not texto:
            res["falhas"].append({"nome": pdf.name, "motivo": motivo})
            _log(f"  [ocr-llm] falha ({motivo}): {pdf.name}")
            continue

        cabecalho = f"{_MARCADOR} {OCR_LLM_MODELO} — transcrição por visão]\n\n"
        with __import__("contextlib").suppress(Exception):
            companion.write_text(cabecalho + texto, encoding="utf-8")
        res["transcritos"].append(pdf.name)
        _log(f"  [ocr-llm] ok: {pdf.name} ({len(texto)} chars)")

    return res
