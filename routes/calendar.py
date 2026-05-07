"""Gestao de prazos pendentes (detectados pelo sync, aguardando envio manual
ao Google Calendar via skill Claude)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.calendar_service import (
    listar_pendentes,
    marcar_processado,
    contar_pendentes,
)

router = APIRouter()


@router.get("/api/calendar/pendentes", response_class=JSONResponse)
async def api_pendentes():
    return {"pendentes": listar_pendentes()}


@router.get("/api/calendar/pendentes/contagem", response_class=JSONResponse)
async def api_contagem():
    return {"total": contar_pendentes()}


@router.post("/api/calendar/marcar-processado/{prazo_id}", response_class=JSONResponse)
async def api_marcar(prazo_id: str, event_id: str = ""):
    ok = marcar_processado(prazo_id, event_id=event_id)
    if not ok:
        return JSONResponse({"ok": False, "erro": "prazo não encontrado"}, status_code=404)
    return {"ok": True}
