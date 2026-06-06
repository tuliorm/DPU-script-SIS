"""Rota do dashboard — lista PAJs do workspace Oficio Geral."""

import contextlib
import json

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
async def api_pajs(concluidos: int = 0, arquivados: int = 0):
    """Lista PAJs pro dashboard.

    ?concluidos=1 -> inclui tambem PAJs que sairam da caixa em sync anterior.
    Por padrao so retorna em_caixa_atual.

    O parametro legado `arquivados` continua sendo aceito (alias do
    `concluidos`) para nao quebrar bookmarks/scripts antigos.
    """
    incluir = bool(concluidos or arquivados)
    pajs = listar_pajs(incluir_concluidos=incluir)
    # Conta total de concluidos no workspace (pra mostrar no toggle)
    total_concluidos = 0
    if not incluir:
        total_concluidos = len(listar_pajs(incluir_concluidos=True)) - len(pajs)

    # "ultima_execucao" reflete a ultima vez que a CAIXA INTEIRA foi
    # sincronizada (rapida ou completa). Le do marcador gravado pelo
    # sincronizador.rodar(). Sync de um PAJ unico, busca leve via watchlist
    # ou download de anexos-desde-data NAO atualizam esse campo —
    # propositalmente, para que o dashboard nao "pareça" mais fresco do que
    # realmente está. Fallback para o mtime mais recente apenas se o
    # marcador ainda nao existir (workspace antigo, primeiro uso).
    ultima_execucao = ""
    marcador = PAJS_DIR / ".ultima_sync_caixa.json"
    if marcador.exists():
        with contextlib.suppress(Exception):
            dados = json.loads(marcador.read_text(encoding="utf-8"))
            ultima_execucao = dados.get("em", "")
    if not ultima_execucao:
        # Fallback de compatibilidade: workspace sem marcador (nunca rodou
        # sync nesta versao do painel). Pega o mtime mais recente como
        # aproximacao.
        try:
            mtimes = [p.stat().st_mtime for p in PAJS_DIR.glob("PAJ-*/metadata.json")]
            if mtimes:
                import datetime as _dt

                ultima_execucao = _dt.datetime.fromtimestamp(max(mtimes)).isoformat()
        except Exception:
            pass

    return {
        "pajs": pajs,
        "ultima_execucao": ultima_execucao,
        "total_concluidos": total_concluidos,
    }
