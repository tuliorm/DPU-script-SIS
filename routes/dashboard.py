"""Rota do dashboard — lista PAJs do workspace Oficio Geral."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import PAJS_DIR
from services.paj_service import listar_pajs

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    template = request.app.state.jinja.get_template("dashboard.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(
        request=request,
        ambiente=ambiente,
    )
    return HTMLResponse(html)


@router.get("/api/pajs", response_class=JSONResponse)
async def api_pajs(arquivados: int = 0):
    """Lista PAJs pro dashboard.

    ?arquivados=1 -> inclui tambem PAJs que sairam da caixa em sync anterior.
    Por padrao so retorna em_caixa_atual.
    """
    pajs = listar_pajs(incluir_arquivados=bool(arquivados))
    # Conta total de arquivados no workspace (pra mostrar no toggle)
    total_arquivados = 0
    if not arquivados:
        total_arquivados = len(listar_pajs(incluir_arquivados=True)) - len(pajs)
    # "ultima_execucao" vira mtime mais recente de qualquer metadata.json do workspace
    ultima_execucao = ""
    try:
        mtimes = [
            p.stat().st_mtime
            for p in PAJS_DIR.glob("PAJ-*/metadata.json")
        ]
        if mtimes:
            import datetime as _dt
            ultima_execucao = _dt.datetime.fromtimestamp(max(mtimes)).isoformat()
    except Exception:
        pass
    return {
        "pajs": pajs,
        "ultima_execucao": ultima_execucao,
        "total_arquivados": total_arquivados,
    }
