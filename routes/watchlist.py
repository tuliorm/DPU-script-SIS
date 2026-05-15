"""Rotas da watchlist — lista de PAJs marcados pelo defensor para
acompanhamento prioritario. Persistencia em `<PAJS_DIR>/.watchlist.json`.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services import watchlist_service
from services.paj_service import PAJ_NORM_REGEX

router = APIRouter()

_PAJ_RE = re.compile(PAJ_NORM_REGEX)


class WatchlistAddIn(BaseModel):
    paj_norm: str = Field(..., min_length=1, max_length=64)
    motivo: str = Field("", max_length=500)


class WatchlistMotivoIn(BaseModel):
    motivo: str = Field("", max_length=500)


@router.get("/watchlist", response_class=HTMLResponse)
async def pagina_watchlist(request: Request):
    template = request.app.state.jinja.get_template("watchlist.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    return HTMLResponse(template.render(request=request, ambiente=ambiente))


@router.get("/api/watchlist", response_class=JSONResponse)
async def api_listar():
    return {
        "itens": watchlist_service.listar(),
        "stats": watchlist_service.stats(),
    }


@router.get("/api/watchlist/check/{paj_norm}", response_class=JSONResponse)
async def api_check(paj_norm: str):
    """Pergunta se um PAJ esta na watchlist (usado pelo paj_detail pra
    decidir se mostra "Adicionar" ou "Remover")."""
    item = watchlist_service.get(paj_norm)
    return {"watched": item is not None, "item": item}


@router.post("/api/watchlist", response_class=JSONResponse)
async def api_adicionar(payload: WatchlistAddIn):
    paj_norm = payload.paj_norm.strip().upper()
    if not _PAJ_RE.match(paj_norm):
        raise HTTPException(
            status_code=400,
            detail="paj_norm deve seguir o formato PAJ-YYYY-NNN-NNNNN",
        )
    item = watchlist_service.adicionar(paj_norm, motivo=payload.motivo)
    return {"ok": True, "item": item}


@router.put("/api/watchlist/{paj_norm}/motivo", response_class=JSONResponse)
async def api_atualizar_motivo(paj_norm: str, payload: WatchlistMotivoIn):
    if not watchlist_service.atualizar_motivo(paj_norm, payload.motivo):
        raise HTTPException(status_code=404, detail="paj nao encontrado na watchlist")
    return {"ok": True}


@router.post("/api/watchlist/{paj_norm}/encerrar", response_class=JSONResponse)
async def api_encerrar(paj_norm: str):
    if not watchlist_service.encerrar(paj_norm):
        raise HTTPException(status_code=404, detail="paj nao encontrado na watchlist")
    return {"ok": True}


@router.post("/api/watchlist/{paj_norm}/reativar", response_class=JSONResponse)
async def api_reativar(paj_norm: str):
    if not watchlist_service.reativar(paj_norm):
        raise HTTPException(status_code=404, detail="paj nao encontrado na watchlist")
    return {"ok": True}


@router.delete("/api/watchlist/{paj_norm}", response_class=JSONResponse)
async def api_remover(paj_norm: str):
    if not watchlist_service.remover(paj_norm):
        raise HTTPException(status_code=404, detail="paj nao encontrado na watchlist")
    return {"ok": True}
