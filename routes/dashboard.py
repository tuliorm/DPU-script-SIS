"""Rota do dashboard — lista PAJs do workspace Oficio Geral."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import PAJS_DIR
from services.paj_service import listar_pajs, classificar_pendencias
from services.calendar_service import contar_pendentes as contar_prazos_pendentes

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
    # Categorias "Precisa acao" (rodadas so em cima dos ATIVOS, nao arquivados)
    pajs_ativos = pajs if not arquivados else [p for p in pajs if p.get("em_caixa_atual") is not False]
    try:
        pendencias = classificar_pendencias(pajs_ativos)
    except Exception:
        pendencias = {"prazo": [], "despacho": [], "juntada": []}

    # Prazos pendentes de envio ao Google Calendar (JSONL detectado pelo sync)
    try:
        prazos_calendar = contar_prazos_pendentes()
    except Exception:
        prazos_calendar = 0

    return {
        "pajs": pajs,
        "ultima_execucao": ultima_execucao,
        "total_arquivados": total_arquivados,
        "pendencias": pendencias,
        "prazos_calendar_pendentes": prazos_calendar,
    }
