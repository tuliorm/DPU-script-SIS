"""Rotas de geracao de DOCX/PDF via SSE."""

from __future__ import annotations

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from services.docgen_service import gerar_artefato
from services.paj_service import PajNorm

router = APIRouter(prefix="/api/gerar")


async def _stream(paj_norm: str, arquivo: str, formato: str):
    async for linha in gerar_artefato(paj_norm, arquivo, formato):  # type: ignore[arg-type]
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Geracao finalizada"}


@router.get("/{paj_norm}")
async def gerar(
    paj_norm: PajNorm,
    arquivo: str = Query(..., description="nome do .txt dentro da pasta do PAJ"),
    formato: str = Query("docx", pattern="^(docx|pdf)$"),
):
    return EventSourceResponse(_stream(paj_norm, arquivo, formato))
