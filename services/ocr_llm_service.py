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

import contextlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from config import (
    OCR_LLM_ATIVO,
    OCR_LLM_CHUNK_PAGINAS,
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
    except Exception as e:
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


def transcrever_pdf_em_blocos(
    pdf_path, chunk_paginas: int, npag: int | None = None
) -> tuple[str | None, str]:
    """Transcreve um PDF GRANDE em blocos de `chunk_paginas` paginas (1 chamada
    Sonnet por bloco) e concatena. PDFs que cabem num bloco (ou sem contagem
    confiavel) caem na chamada unica `transcrever_pdf`.

    Substitui o antigo "pular anexos grandes": agora nao ha teto que descarte o
    anexo — fatia-se com PyMuPDF em PDFs temporarios. Retorna (texto, motivo),
    motivo ∈ {"ok","cota","erro","vazio"}.

    - cota em QUALQUER bloco => aborta e retorna (None,"cota") sem salvar parcial
      (a idempotencia re-tenta o anexo inteiro quando a cota renovar).
    - falha pontual nao-cota num bloco => insere marcador e segue nos demais.
    """
    if npag is None:
        npag = _contar_paginas(pdf_path)
    # Cabe num bloco (ou nao deu p/ contar): caminho simples, 1 chamada.
    if not npag or npag <= chunk_paginas:
        return transcrever_pdf(pdf_path)

    try:
        import fitz  # PyMuPDF
    except Exception:
        # Sem fitz nao da p/ fatiar — tenta o PDF inteiro de uma vez.
        return transcrever_pdf(pdf_path)

    try:
        src = fitz.open(pdf_path)
    except Exception:
        return None, "erro"

    partes: list[str] = []
    houve_ok = False
    try:
        total = src.page_count
        for ini in range(0, total, chunk_paginas):
            fim = min(ini + chunk_paginas, total)  # exclusivo
            sub = fitz.open()
            tmp = None
            try:
                sub.insert_pdf(src, from_page=ini, to_page=fim - 1)
                fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="ocrllm_blk_")
                os.close(fd)
                sub.save(tmp)
            finally:
                sub.close()
            try:
                texto_blk, motivo_blk = transcrever_pdf(Path(tmp))
            finally:
                if tmp:
                    with contextlib.suppress(Exception):
                        os.unlink(tmp)
            if motivo_blk == "cota":
                return None, "cota"
            cab = f"\n\n===== paginas {ini + 1}-{fim} de {total} =====\n\n"
            if motivo_blk == "ok" and texto_blk:
                houve_ok = True
                partes.append(cab + texto_blk)
            else:
                partes.append(cab + f"[bloco não transcrito: {motivo_blk}]")
    finally:
        src.close()

    if not houve_ok:
        return None, "vazio"
    return "".join(partes).strip(), "ok"


def _ocr_local_robusto(pdf_path) -> str | None:
    """CAMADA 1 (gratis): re-extrai o texto via OCR local robusto reusando
    `ingestao.ocr.extrair_texto` (que tenta ocrmypdf e cai no Tesseract cru).
    Usado antes do LLM pra resolver escaneados offline sem gastar token. Import
    tardio pra nao acoplar este modulo a `ingestao` no topo (e evitar ciclo)."""
    try:
        from ingestao.ocr import extrair_texto

        return extrair_texto(pdf_path) or None
    except Exception:
        return None


def melhorar_ocr_paj(paj_norm: str, log_fn=None) -> dict:
    """Melhora o OCR dos anexos PDF do PAJ cujo texto esta fraco/ausente.

    Cascata por anexo: CAMADA 1 = OCR local robusto (ocrmypdf, gratis) via
    `extrair_texto`; se AINDA ficar fraco, CAMADA 2 = transcricao por visao
    (Sonnet), em blocos quando o PDF e' grande. Idempotente (pula quem ja tem o
    marcador `[OCR-LLM:` ou cujo .txt ja nao e' fraco). Best-effort. Retorna
    {transcritos, ocr_local, pulados, grandes, falhas, cota}.
    """

    def _log(msg: str) -> None:
        if log_fn:
            with __import__("contextlib").suppress(Exception):
                log_fn(msg)
        log.info(msg)

    res: dict = {
        "transcritos": [],
        "ocr_local": [],
        "pulados": [],
        "grandes": [],
        "falhas": [],
        "cota": False,
    }
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

        # CAMADA 1 (gratis): tenta o OCR local robusto (ocrmypdf) antes do LLM.
        # Resolve a maioria dos escaneados offline; so' cai no Sonnet (camada 2)
        # se AINDA ficar fraco. O .txt salvo aqui nao leva o marcador [OCR-LLM:
        # (e' OCR local), mas deixa de ser "fraco" — idempotente nas proximas.
        texto_local = _ocr_local_robusto(pdf)
        if texto_local and not ocr_fraco(texto_local):
            with contextlib.suppress(Exception):
                companion.write_text(texto_local, encoding="utf-8")
            res["ocr_local"].append(pdf.name)
            _log(
                f"  [ocr-local] ocrmypdf resolveu sem LLM: {pdf.name} "
                f"({len(texto_local.strip())} chars)"
            )
            continue

        # Conta paginas: decide entre 1 chamada ou transcricao EM BLOCOS, e
        # aplica o teto de SEGURANCA (OCR_LLM_MAX_PAGINAS>0). Autos acima do teto
        # ficam pro Opus ler direto; sem teto (0) transcreve tudo, em blocos.
        npag = _contar_paginas(pdf)
        if OCR_LLM_MAX_PAGINAS and npag is not None and npag > OCR_LLM_MAX_PAGINAS:
            res["grandes"].append({"nome": pdf.name, "paginas": npag})
            _log(
                f"  [ocr-llm] pulado (teto de segurança: {npag} pág > "
                f"{OCR_LLM_MAX_PAGINAS}) — Opus lerá o PDF direto: {pdf.name}"
            )
            continue

        if npag and npag > OCR_LLM_CHUNK_PAGINAS:
            nblocos = -(-npag // OCR_LLM_CHUNK_PAGINAS)  # ceil
            _log(
                f"  [ocr-llm] transcrevendo (Sonnet) em {nblocos} blocos de "
                f"{OCR_LLM_CHUNK_PAGINAS} pág: {pdf.name} ({npag} pág)"
            )
        else:
            _log(f"  [ocr-llm] transcrevendo (Sonnet): {pdf.name}")
        texto, motivo = transcrever_pdf_em_blocos(pdf, OCR_LLM_CHUNK_PAGINAS, npag)
        if motivo == "cota":
            res["cota"] = True
            res["falhas"].append({"nome": pdf.name, "motivo": "cota"})
            _log("  [ocr-llm] cota esgotada — interrompendo transcrições")
            break
        if motivo != "ok" or not texto:
            res["falhas"].append({"nome": pdf.name, "motivo": motivo})
            _log(f"  [ocr-llm] falha ({motivo}): {pdf.name}")
            continue

        if npag and npag > OCR_LLM_CHUNK_PAGINAS:
            nb = -(-npag // OCR_LLM_CHUNK_PAGINAS)
            cabecalho = (
                f"{_MARCADOR} {OCR_LLM_MODELO} — transcrição por visão, "
                f"{npag} pág em {nb} blocos]\n\n"
            )
        else:
            cabecalho = f"{_MARCADOR} {OCR_LLM_MODELO} — transcrição por visão]\n\n"
        with contextlib.suppress(Exception):
            companion.write_text(cabecalho + texto, encoding="utf-8")
        res["transcritos"].append(pdf.name)
        _log(f"  [ocr-llm] ok: {pdf.name} ({len(texto)} chars)")

    return res
