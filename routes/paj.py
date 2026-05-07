"""Rota de detalhe do PAJ."""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.paj_service import (
    PajNorm,
    ler_paj,
    ler_notas,
    limpar_anexos_paj,
    salvar_notas,
)

router = APIRouter()


@router.get("/paj/{paj_norm}", response_class=HTMLResponse)
async def paj_detail(request: Request, paj_norm: PajNorm):
    dados = ler_paj(paj_norm)
    if not dados:
        return HTMLResponse("<h1>PAJ não encontrado</h1>", status_code=404)
    template = request.app.state.jinja.get_template("paj_detail.html")
    html = template.render(request=request, dados=dados, paj_norm=paj_norm)
    return HTMLResponse(html)


@router.get("/api/paj/{paj_norm}", response_class=JSONResponse)
async def api_paj(paj_norm: PajNorm):
    dados = ler_paj(paj_norm)
    if not dados:
        return JSONResponse({"erro": "PAJ não encontrado"}, status_code=404)
    # Remove prompt_max do JSON (muito grande) — tem endpoint proprio
    dados_resumo = {k: v for k, v in dados.items() if k != "prompt_max"}
    return dados_resumo


@router.get("/api/paj/{paj_norm}/limpar-anexos/preview", response_class=JSONResponse)
async def limpar_anexos_preview(paj_norm: PajNorm):
    """Dry-run: lista o que seria apagado e mostra bloqueios de seguranca."""
    resultado = limpar_anexos_paj(paj_norm, dry_run=True)
    if not resultado.get("ok"):
        return JSONResponse(resultado, status_code=404)
    return resultado


@router.post("/api/paj/{paj_norm}/limpar-anexos", response_class=JSONResponse)
async def limpar_anexos_executar(paj_norm: PajNorm, forcar: bool = False):
    """Executa a limpeza. Se forcar=True, ignora os bloqueios de seguranca."""
    resultado = limpar_anexos_paj(paj_norm, dry_run=False, forcar=forcar)
    if not resultado.get("ok"):
        return JSONResponse(resultado, status_code=400)
    return resultado


@router.get("/api/paj/{paj_norm}/notas", response_class=JSONResponse)
async def api_notas_ler(paj_norm: PajNorm):
    """Le NOTAS.md do PAJ (string vazia se nao existe)."""
    return {"paj_norm": paj_norm, "texto": ler_notas(paj_norm)}


@router.post("/api/paj/{paj_norm}/notas", response_class=JSONResponse)
async def api_notas_salvar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Salva NOTAS.md do PAJ. Body: {"texto": "..."}."""
    texto = payload.get("texto", "") if isinstance(payload, dict) else ""
    ok = salvar_notas(paj_norm, texto)
    if not ok:
        return JSONResponse({"ok": False, "erro": "PAJ inválido"}, status_code=400)
    return {"ok": True}
