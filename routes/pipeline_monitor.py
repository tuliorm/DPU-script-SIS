"""Rotas de monitoramento do pipeline (sincronizacao SISDPU) — pagina /pipeline.

A 2a Cat nao tem subprocess externo — a sincronizacao roda dentro deste app via
`services.sync_service`. Esta tela centraliza visibilidade do estado atual e
historico de execucoes lendo de `logs/app.log` (rotacao diaria).
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.pipeline_monitor_service import (
    ler_log_atual,
    ler_run,
    ler_state,
    listar_runs,
)

router = APIRouter()


@router.get("/pipeline", response_class=HTMLResponse)
async def pagina_pipeline(request: Request):
    template = request.app.state.jinja.get_template("pipeline.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    return HTMLResponse(template.render(request=request, ambiente=ambiente))


@router.get("/api/pipeline/state", response_class=JSONResponse)
async def api_state():
    return ler_state()


@router.get("/api/pipeline/log", response_class=JSONResponse)
async def api_log(max_linhas: int = 500):
    return ler_log_atual(max_linhas=max_linhas)


@router.get("/api/pipeline/runs", response_class=JSONResponse)
async def api_runs(max_runs: int = 20):
    return {"runs": listar_runs(max_runs=max_runs)}


@router.get("/api/pipeline/runs/{nome}", response_class=JSONResponse)
async def api_run(nome: str, max_linhas: int = 2000):
    return ler_run(nome, max_linhas=max_linhas)
