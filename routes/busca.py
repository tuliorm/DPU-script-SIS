"""Busca full-text no acervo de PAJs (sisdpu.txt + OCRs)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.busca_service import buscar

router = APIRouter()


@router.get("/api/busca", response_class=JSONResponse)
async def api_busca(q: str = "", limite: int = 50):
    q = (q or "").strip()
    if len(q) < 2:
        return {"q": q, "resultados": [], "aviso": "digite pelo menos 2 caracteres"}
    resultados = buscar(q, limite=max(1, min(200, int(limite))))
    return {"q": q, "resultados": resultados, "total": len(resultados)}
