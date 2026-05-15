"""Detector de prazos processuais a partir de movimentacoes do SISDPU.

Compara movimentacoes antes/depois da sync e identifica intimacoes/citacoes
novas. Para cada uma, tenta extrair um prazo em dias e calcular a data-alvo
APLICANDO AS REGRAS PROCESSUAIS via `services.prazo_processual` (dias uteis
no civel/JEF, dias corridos no penal, dobra DPU, recesso forense, etc).

O resultado e' passado ao `services.calendar_service.append_prazo(...)` que
grava num JSONL. Esse JSONL alimenta tanto a aba interna /prazos do painel
quanto a skill Claude /sync_calendar (Google Calendar).

Heuristicas sao conservadoras — melhor ter falso positivo (usuario descarta
antes de enviar) do que falso negativo (perde prazo).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

from services.prazo_processual import Rito, calcular_data_alvo, inferir_rito


# Padroes que indicam intimacao/citacao/prazo
_RGX_INTIMACAO = re.compile(
    r"intima[cç][aã]o|cita[cç][aã]o|notifica[cç][aã]o",
    re.IGNORECASE,
)
_RGX_DIAS = re.compile(
    r"prazo\s+d[eoa]?\s*(\d{1,3})\s*(?:dias?|\(\s*\d+\s*\))?",
    re.IGNORECASE,
)
_RGX_DIAS_SIMPLES = re.compile(
    r"\b(\d{1,3})\s*dias?\s*(?:uteis|úteis)?\b",
    re.IGNORECASE,
)
_RGX_DATA = re.compile(r"\bat[ée]\s+(\d{2}/\d{2}/\d{4})\b", re.IGNORECASE)


def _parse_data_br(s: str) -> dt.date | None:
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _extrair_prazo_dias(descricao: str) -> int | None:
    m = _RGX_DIAS.search(descricao or "")
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 365:
                return n
        except Exception:
            pass
    m = _RGX_DIAS_SIMPLES.search(descricao or "")
    if m:
        try:
            n = int(m.group(1))
            # Heuristica conservadora: so considera se o texto tem tambem
            # alguma palavra de intimacao/citacao proxima
            if 1 <= n <= 60 and _RGX_INTIMACAO.search(descricao or ""):
                return n
        except Exception:
            pass
    return None


def _extrair_data_alvo_explicita(descricao: str) -> dt.date | None:
    m = _RGX_DATA.search(descricao or "")
    if not m:
        return None
    return _parse_data_br(m.group(1))


def _id_prazo(paj_norm: str, seq: int | str) -> str:
    agora_iso = dt.datetime.now().replace(microsecond=0).isoformat()
    return f"{agora_iso}-{paj_norm}-seq{seq}"


def detectar_prazos_novos(
    paj_norm: str,
    movs_antigas: Iterable[dict],
    movs_novas: Iterable[dict],
    assistido: str = "",
    foro: str = "",
    classificacao: str = "",
) -> list[dict]:
    """Compara movs e retorna lista de prazos novos detectados.

    Cada entrada:
        {
            "id": str,
            "paj_norm": str,
            "titulo": str,
            "descricao": str,
            "data_mov": "YYYY-MM-DD",
            "prazo_dias": int | None,
            "data_alvo": "YYYY-MM-DD",  # calculada via prazo_processual
            "rito": "civel" | "jef" | "penal" | "administrativo",
            "em_dobro": bool,
            "assistido": str,
            "fonte_mov_seq": int,
            "status": "pendente",
            "detectado_em": ISO,
        }
    """
    seqs_antigas = set()
    for m in (movs_antigas or []):
        try:
            seqs_antigas.add(int(m.get("seq", 0) or 0))
        except Exception:
            continue

    novos: list[dict] = []
    for m in (movs_novas or []):
        try:
            seq = int(m.get("seq", 0) or 0)
        except Exception:
            continue
        if seq in seqs_antigas:
            continue  # ja existia, nao e' novo

        desc = m.get("descricao", "") or ""
        if not _RGX_INTIMACAO.search(desc):
            continue  # so rastreia intimacao/citacao/notificacao

        data_mov = _parse_data_br(m.get("data_original") or m.get("data") or "")

        # Inferencia de rito combina foro/classificacao da PAJ + heuristica
        # da descricao da movimentacao.
        rito = inferir_rito(
            area_paj=classificacao or "",
            descricao_mov=desc,
            foro=foro or "",
        )

        # Tenta extrair data-alvo explicita ("ate dd/mm/aaaa") primeiro;
        # se nao houver, calcula a partir de prazo_dias + data_mov + rito.
        data_alvo_explicita = _extrair_data_alvo_explicita(desc)
        dias = _extrair_prazo_dias(desc) if data_alvo_explicita is None else None

        em_dobro = rito in (Rito.CIVEL, Rito.ADMINISTRATIVO)
        data_alvo: dt.date | None = data_alvo_explicita
        if data_alvo is None and dias is not None and data_mov is not None:
            data_alvo = calcular_data_alvo(data_mov, dias, rito, em_dobro=em_dobro)

        titulo = f"[Prazo {dias}d] {paj_norm}" if dias else f"[Intimacao] {paj_norm}"
        if assistido:
            titulo += f" — {assistido[:60]}"

        novos.append({
            "id": _id_prazo(paj_norm, seq),
            "paj_norm": paj_norm,
            "titulo": titulo,
            "descricao": desc[:500],
            "data_mov": data_mov.isoformat() if data_mov else "",
            "prazo_dias": dias,
            "data_alvo": data_alvo.isoformat() if data_alvo else "",
            "rito": rito.value,
            "em_dobro": em_dobro,
            "assistido": assistido,
            "fonte_mov_seq": seq,
            "status": "pendente",
            "detectado_em": dt.datetime.now().isoformat(timespec="seconds"),
        })

    return novos
