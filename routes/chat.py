"""Rotas de elaboracao de peca (background + polling) e chat interativo."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse

import re

from services.chat_service import (
    ChatSession,
    get_or_create_session,
    stop_session,
    start_or_queue,
    start_lote,
    get_stats,
    ler_elaboracao_disco,
    _sessions,
)
from services.skills_catalog import listar_skills, skills_para_area
from services.paj_service import PAJ_NORM_REGEX, PajNorm, ler_paj
from services import cota_service
from config import PAJS_DIR

router = APIRouter()


# ----- Elaborar Peca (background + polling + correcao multi-turn) -----


@router.post("/api/elaborar/start/{paj_norm}")
async def elaborar_start(paj_norm: PajNorm, payload: dict = Body(default={})):
    """Inicia elaboracao (ou enfileira se limite de paralelos atingido).

    Body opcional: {"skill": "<slug>"} para invocar uma skill especifica
    do Oficio Geral. Sem skill, o Claude decide o proximo passo autonomamente
    (comportamento legado).
    """
    skill_slug = (payload.get("skill") or "").strip() or None
    return start_or_queue(paj_norm, skill_slug=skill_slug)


@router.post("/api/elaborar/start-lote")
async def elaborar_start_lote(payload: dict = Body(...)):
    """Elabora em lote todos os PAJs informados que forem elegiveis.

    Body: {"pajs": ["PAJ-YYYY-NNN-XXXXX", ...], "skill": "<slug>"}.
    A lista de PAJs vem do dashboard (os filtrados na tela). O backend faz a
    triagem autoritativa (anexos baixados + sincronia + nao-elaborado) e
    devolve os enviados e os pulados agrupados por motivo — vide start_lote.
    """
    pajs_raw = payload.get("pajs") or []
    skill_slug = (payload.get("skill") or "").strip() or None
    auto_rotear = bool(payload.get("auto_rotear"))
    # So aceita identificadores no formato canonico — ignora lixo silenciosamente.
    pajs = [p for p in pajs_raw if isinstance(p, str) and re.match(PAJ_NORM_REGEX, p)]
    return start_lote(pajs, skill_slug=skill_slug, auto_rotear=auto_rotear)


@router.get("/api/skills")
async def api_skills(paj: str | None = None):
    """Retorna o catalogo de skills agrupadas.

    Se ?paj=PAJ-YYYY-044-XXXXX for informado, as skills recebem flag
    `destaque=True` quando casam com a area/classificacao do PAJ, pro
    frontend ordenar/estilizar.
    """
    area = ""
    if paj:
        dados = ler_paj(paj)
        if dados:
            meta = dados.get("metadata", {}) or {}
            area = meta.get("foro_detectado") or meta.get("classificacao") or ""
    lista = skills_para_area(area) if area else [{**s, "destaque": False} for s in listar_skills()]
    return {"area": area, "skills": lista}


@router.get("/api/elaborar/stats")
async def elaborar_stats():
    """Retorna {running, queued, max_parallel} — pro dashboard."""
    return get_stats()


# ----- Cota / re-disparo automatico -----


@router.get("/api/cota/status")
async def cota_status():
    """Estado do re-disparo por cota (pro banner do dashboard)."""
    return cota_service.status()


@router.post("/api/cota/disparar-agora")
async def cota_disparar_agora():
    """Força o re-disparo imediato dos pendentes, ignorando o agendamento."""
    return cota_service.disparar_agora()


@router.post("/api/cota/cancelar")
async def cota_cancelar():
    """Cancela o agendamento e descarta os pendentes."""
    return cota_service.cancelar()


@router.get("/api/elaborar/status")
async def elaborar_status_all():
    """Retorna status de todas as sessoes — RAM + persistencia em disco.

    Busca status em memoria (sessoes ativas) e tambem PAJs que ja foram
    elaborados antes (tem elaboracao.json no disco). Prefere RAM sobre disco
    quando os dois existem.
    """
    result: dict[str, dict] = {}

    # 1. PAJs com elaboracao persistida em disco (resultado de runs anteriores)
    if PAJS_DIR.exists():
        for pasta in PAJS_DIR.iterdir():
            if not pasta.is_dir() or not pasta.name.startswith("PAJ-"):
                continue
            persist = ler_elaboracao_disco(pasta.name)
            if persist:
                result[pasta.name] = {
                    "status": persist.get("status", "done"),
                    "last_action": persist.get("last_action", ""),
                    "alive": False,
                    "persisted": True,
                }

    # 2. Sessoes em memoria sobrescrevem (dados mais frescos)
    for paj_norm, session in _sessions.items():
        result[paj_norm] = {
            "status": session.status,
            "last_action": session.last_action,
            "alive": session.is_alive(),
            "persisted": False,
        }
    return result


@router.get("/api/elaborar/status/{paj_norm}")
async def elaborar_status(paj_norm: PajNorm):
    """Retorna status atual: le sessao em memoria OU elaboracao.json do disco."""
    session = _sessions.get(paj_norm)
    if session:
        return {
            "status": session.status,
            "last_action": session.last_action,
            "summary": session.summary,
            "error": session.error,
            "alive": session.is_alive(),
            "persisted": False,
        }
    # Fallback: le do disco (persistido de run anterior)
    persist = ler_elaboracao_disco(paj_norm)
    if persist:
        return {
            "status": persist.get("status", "done"),
            "last_action": persist.get("last_action", ""),
            "summary": persist.get("summary", ""),
            "error": "",
            "alive": False,
            "persisted": True,
            "concluido_em": persist.get("concluido_em", ""),
        }
    return {"status": "idle", "last_action": "", "summary": "", "error": ""}


@router.post("/api/elaborar/correcao/{paj_norm}")
async def elaborar_correcao(paj_norm: PajNorm, payload: dict = Body(...)):
    """Envia correcao/discordancia pro Claude refazer.

    Se a sessao ainda estiver viva (multi-turn), envia a mensagem diretamente.
    Se o subprocess ja terminou (caso comum apos uma elaboracao concluida),
    reinicia automaticamente com um prompt que instrui o Claude a ler os
    arquivos ja gerados e aplicar a correcao solicitada.
    """
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"erro": "texto vazio"}, status_code=400)

    session = _sessions.get(paj_norm)

    # Sessao viva: envia mensagem multi-turn normalmente
    if session and session.is_alive():
        session.send_message(text)
        return {"status": session.status, "last_action": session.last_action}

    # Sessao inativa (subprocess saiu apos elaboracao concluida):
    # reinicia com contexto de correcao — Claude le os arquivos gerados e refaz.
    if session:
        session.stop()
    session = ChatSession(paj_norm)
    _sessions[paj_norm] = session

    ok = session.start_correcao(text)
    if not ok:
        return JSONResponse(
            {"erro": session.error or "falha ao reiniciar sessão de correção"},
            status_code=500,
        )
    return {"status": session.status, "last_action": session.last_action}


@router.post("/api/elaborar/stop/{paj_norm}")
async def elaborar_stop(paj_norm: PajNorm):
    stop_session(paj_norm)
    return {"status": "stopped"}


# ----- Chat Interativo (WebSocket — uso direto se quiser log completo) -----


@router.get("/chat/{paj_norm}", response_class=HTMLResponse)
async def chat_page(request: Request, paj_norm: PajNorm):
    template = request.app.state.jinja.get_template("chat.html")
    html = template.render(request=request, paj_norm=paj_norm)
    return HTMLResponse(html)


@router.websocket("/ws/chat/{paj_norm}")
async def chat_websocket(websocket: WebSocket, paj_norm: str):
    await websocket.accept()

    session = get_or_create_session(paj_norm)
    if not session.is_alive():
        session.start()

    try:

        async def send_output():
            while True:
                try:
                    event = session.output_queue.get_nowait()
                    await websocket.send_json(event)
                    if event.get("type") == "done":
                        break
                except Exception:
                    await asyncio.sleep(0.05)

        output_task = asyncio.create_task(send_output())

        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
                if data.get("type") == "message":
                    text = data.get("text", "")
                    if text.strip():
                        session.send_message(text)
                        await websocket.send_json({"type": "user", "text": text})
                elif data.get("type") == "stop":
                    stop_session(paj_norm)
                    break
            except TimeoutError:
                if output_task.done():
                    break
                continue
            except WebSocketDisconnect:
                break

        output_task.cancel()

    except WebSocketDisconnect:
        pass
