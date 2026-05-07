"""Busca full-text no acervo de PAJs.

Varre `sisdpu.txt` + `pecas/*.txt` (OCRs) + campos-chave do metadata.json.
Cache em memoria com TTL pra nao re-ler disco a cada pesquisa.

Volume tipico: ~400 PAJs x ~50KB texto = ~20MB. Primeira busca 1-2s;
demais <200ms enquanto o cache esta quente (60s TTL).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

from config import PAJS_DIR


_CACHE_TTL_SEG = 60
_cache: dict = {"ts": 0.0, "corpus": []}


def _sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "")
        if unicodedata.category(c) != "Mn"
    ).lower()


def _ler_metadata(pasta: Path) -> dict:
    f = pasta / "metadata.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ler_texto(f: Path, limite_bytes: int = 500_000) -> str:
    """Le um arquivo texto; se for grande demais, le so o inicio."""
    try:
        tam = f.stat().st_size
        if tam > limite_bytes:
            with open(f, encoding="utf-8", errors="replace") as fp:
                return fp.read(limite_bytes)
        return f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _montar_corpus() -> list[dict]:
    """Le todos os PAJs e retorna lista de documentos indexaveis.

    Cada doc: {
        "paj_norm": str,
        "meta_blob": str,         # assistido, etiqueta, classificacao, foro, etc
        "sisdpu_blob": str,       # conteudo de sisdpu.txt
        "ocr_docs": [{"arquivo": str, "texto": str}],
    }
    """
    corpus: list[dict] = []
    if not PAJS_DIR.exists():
        return corpus

    for pasta in sorted(PAJS_DIR.iterdir()):
        if not pasta.is_dir():
            continue
        paj_norm = pasta.name
        if not paj_norm.startswith("PAJ-"):
            continue

        meta = _ler_metadata(pasta)
        meta_partes = [
            meta.get("paj", ""),
            meta.get("assistido_caixa", ""),
            meta.get("oficio_caixa", ""),
            meta.get("etiqueta_sisdpu", ""),
            meta.get("classificacao", ""),
            meta.get("foro_detectado", ""),
            meta.get("processo_judicial", ""),
            meta.get("desc_mov_caixa", ""),
        ]
        det = meta.get("detalhes_sisdpu", {}) or {}
        meta_partes.append(det.get("status_paj", "") or "")
        # Cabecalho e partes contadas tambem sao uteis
        for parte in (det.get("partes", []) or []):
            meta_partes.append(str(parte))

        meta_blob = " | ".join(p for p in meta_partes if p)

        sisdpu_txt = pasta / "sisdpu.txt"
        sisdpu_blob = _ler_texto(sisdpu_txt) if sisdpu_txt.exists() else ""

        ocr_docs: list[dict] = []
        pecas = pasta / "pecas"
        if pecas.exists() and pecas.is_dir():
            for f in sorted(pecas.iterdir()):
                if f.is_file() and f.suffix.lower() == ".txt":
                    ocr_docs.append({"arquivo": f.name, "texto": _ler_texto(f)})

        corpus.append({
            "paj_norm": paj_norm,
            "meta_blob": meta_blob,
            "sisdpu_blob": sisdpu_blob,
            "ocr_docs": ocr_docs,
            "assistido": meta.get("assistido_caixa", ""),
            "etiqueta": meta.get("etiqueta_sisdpu", ""),
        })

    return corpus


def _get_corpus() -> list[dict]:
    agora = time.time()
    if _cache["corpus"] and (agora - _cache["ts"]) < _CACHE_TTL_SEG:
        return _cache["corpus"]
    _cache["corpus"] = _montar_corpus()
    _cache["ts"] = agora
    return _cache["corpus"]


def invalidar_cache() -> None:
    """Forca re-leitura do disco na proxima busca."""
    _cache["corpus"] = []
    _cache["ts"] = 0.0


def _extrair_trecho(texto_normalizado: str, texto_original: str, termo_norm: str, janela: int = 120) -> str:
    """Acha match em texto_normalizado, retorna trecho do texto_original com <mark>."""
    i = texto_normalizado.find(termo_norm)
    if i < 0:
        return ""
    inicio = max(0, i - janela // 2)
    fim = min(len(texto_original), i + len(termo_norm) + janela // 2)
    prefixo = "..." if inicio > 0 else ""
    sufixo = "..." if fim < len(texto_original) else ""
    trecho = texto_original[inicio:fim]
    # highlight case-insensitive no trecho original (aproximado — destaca a
    # primeira ocorrencia case-insensitive do termo, preservando case do trecho)
    try:
        trecho_limpo = trecho.replace("\n", " ").replace("\r", " ")
        termo_variante = re.compile(re.escape(termo_norm), re.IGNORECASE)
        trecho_highlighted = termo_variante.sub(
            lambda m: f"<mark>{m.group(0)}</mark>", trecho_limpo
        )
        return prefixo + trecho_highlighted + sufixo
    except (re.error, AttributeError):
        return prefixo + trecho.replace("\n", " ") + sufixo


def buscar(q: str, limite: int = 50) -> list[dict]:
    """Procura termo `q` no corpus e retorna lista ranqueada.

    Score:
      - Match em meta_blob: +10 por ocorrencia
      - Match em sisdpu.txt: +3
      - Match em OCR: +1

    Retorna [{paj_norm, assistido, etiqueta, score, fonte, arquivo, trecho}].
    """
    termo_norm = _sem_acento(q.strip())
    if len(termo_norm) < 2:
        return []

    resultados: list[dict] = []

    for doc in _get_corpus():
        score = 0
        melhor_trecho = ""
        melhor_fonte = ""
        melhor_arquivo = ""

        meta_norm = _sem_acento(doc["meta_blob"])
        hits_meta = meta_norm.count(termo_norm)
        if hits_meta:
            score += 10 * hits_meta
            trecho = _extrair_trecho(meta_norm, doc["meta_blob"], termo_norm)
            if trecho:
                melhor_trecho = trecho
                melhor_fonte = "metadata"
                melhor_arquivo = ""

        sisdpu_norm = _sem_acento(doc["sisdpu_blob"])
        hits_sisdpu = sisdpu_norm.count(termo_norm)
        if hits_sisdpu:
            score += 3 * hits_sisdpu
            if not melhor_trecho:
                trecho = _extrair_trecho(sisdpu_norm, doc["sisdpu_blob"], termo_norm)
                if trecho:
                    melhor_trecho = trecho
                    melhor_fonte = "sisdpu"
                    melhor_arquivo = "sisdpu.txt"

        for ocr in doc["ocr_docs"]:
            ocr_norm = _sem_acento(ocr["texto"])
            hits_ocr = ocr_norm.count(termo_norm)
            if hits_ocr:
                score += 1 * hits_ocr
                if not melhor_trecho:
                    trecho = _extrair_trecho(ocr_norm, ocr["texto"], termo_norm)
                    if trecho:
                        melhor_trecho = trecho
                        melhor_fonte = "ocr"
                        melhor_arquivo = ocr["arquivo"]

        if score > 0:
            resultados.append({
                "paj_norm": doc["paj_norm"],
                "assistido": doc["assistido"],
                "etiqueta": doc["etiqueta"],
                "score": score,
                "fonte": melhor_fonte,
                "arquivo": melhor_arquivo,
                "trecho": melhor_trecho,
            })

    resultados.sort(key=lambda r: (-r["score"], r["paj_norm"]))
    return resultados[:limite]
