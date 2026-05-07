"""Rota de sincronizacao da caixa SISDPU via SSE."""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from services.sync_service import (
    esta_rodando,
    rodar_sync,
    rodar_sync_paj,
    rodar_sync_anexos_desde_data,
    solicitar_cancelamento,
)


router = APIRouter(prefix="/api")


async def _stream(baixar_anexos: bool):
    async for linha in rodar_sync(baixar_anexos=baixar_anexos):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Sincronizacao finalizada"}


async def _stream_paj(paj_identificador: str, baixar_anexos: bool):
    async for linha in rodar_sync_paj(paj_identificador, baixar_anexos=baixar_anexos):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Sincronizacao do PAJ finalizada"}


async def _stream_anexos_desde(paj_identificador: str, data_inicio: _dt.date):
    async for linha in rodar_sync_anexos_desde_data(paj_identificador, data_inicio):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Download de anexos finalizado"}


@router.get("/sync")
async def sync(anexos: int = 1):
    """Sincroniza a caixa SISDPU.

    ?anexos=0 => modo rapido (pula download/OCR dos anexos).
    """
    return EventSourceResponse(_stream(baixar_anexos=bool(anexos)))


@router.get("/sync/paj/{paj_norm}")
async def sync_paj(paj_norm: str, anexos: int = 1):
    """Sincroniza apenas UM PAJ (uso de teste / refresh pontual).

    `paj_norm` aceita 'PAJ-2021-044-00635' ou '2021/044-00635' (URL-encoded).
    ?anexos=0 => modo rapido (pula download/OCR dos anexos).
    """
    return EventSourceResponse(_stream_paj(paj_norm, baixar_anexos=bool(anexos)))


@router.get("/sync/paj/{paj_norm}/anexos-desde")
async def sync_anexos_desde(paj_norm: str, data: str = ""):
    """Baixa anexos de um PAJ a partir de uma data de corte (inclusive).

    Usado quando o sync padrao detecta overflow (PAJ tem >MAX_ANEXOS_POR_PAJ
    anexos) e o usuario quer completar a partir de uma data especifica.

    Query: ?data=YYYY-MM-DD (obrigatorio).
    """
    try:
        data_inicio = _dt.date.fromisoformat(data)
    except (ValueError, TypeError):
        return JSONResponse(
            {"erro": "parametro 'data' invalido — use formato YYYY-MM-DD"},
            status_code=400,
        )
    return EventSourceResponse(_stream_anexos_desde(paj_norm, data_inicio))


@router.post("/sync/cancel")
async def sync_cancel():
    """Sinaliza o worker de sync atual para parar na proxima oportunidade.

    O sincronizador consulta o flag entre PAJs e aborta o laco de forma limpa
    (sem arquivar PAJs por engano). Se nao houver sync rodando, retorna 404.
    """
    if not esta_rodando():
        return {"ok": False, "motivo": "nenhuma sync em andamento"}
    sinalizado = solicitar_cancelamento()
    return {"ok": sinalizado}
