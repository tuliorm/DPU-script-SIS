"""Aba interna de Prazos — controle de prazos judiciais ativos.

Mostra apenas prazos cujo PAJ ainda esta NA CAIXA do defensor (em_caixa_atual=True).
PAJs concluidos/arquivados saem da caixa e seus prazos automaticos somem da tela
sem deixar historico — controle leve, dinamico.

Prazos manuais (`manual=True`) sao excecao: aparecem mesmo se o paj_norm nao
estiver na caixa (ou nem existir no workspace), pois o defensor explicitamente
escolheu monitora-los. Use POST /api/prazos para criar e DELETE /api/prazos/{id}
para marcar como cumprido (apaga do JSONL).
"""

from __future__ import annotations

import datetime as dt
import re
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services.calendar_service import append_prazo, listar_pendentes, remover_prazo
from services.paj_service import PAJ_NORM_REGEX, listar_pajs
from services.prazo_processual import dias_restantes

router = APIRouter()

_PAJ_RE = re.compile(PAJ_NORM_REGEX)
_DATA_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PrazoManualIn(BaseModel):
    paj_norm: str = Field(..., min_length=1, max_length=64)
    descricao: str = Field("", max_length=500)
    data_alvo: str = Field(..., description="Data-alvo em ISO (YYYY-MM-DD)")
    assistido: str = Field("", max_length=200)


@router.get("/prazos", response_class=HTMLResponse)
async def pagina_prazos(request: Request):
    template = request.app.state.jinja.get_template("prazos.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(request=request, ambiente=ambiente)
    return HTMLResponse(html)


def _set_pajs_ativos() -> set[str]:
    """paj_norm dos PAJs em_caixa_atual=True (cache curto via paj_service)."""
    return {p["paj_norm"] for p in listar_pajs(incluir_arquivados=False)}


@router.get("/api/prazos", response_class=JSONResponse)
async def api_prazos():
    """Lista prazos pendentes filtrados por PAJ ativo na caixa.

    Regra:
      - Prazos automaticos (sem `manual=True`): so passam se paj_norm pertence
        a um PAJ em_caixa_atual=True. PAJs concluidos/arquivados removem seus
        prazos da tela sem deixar historico.
      - Prazos manuais (manual=True): sempre passam. O defensor optou por
        monitora-los e a logica de "PAJ fora da caixa = sem interesse" nao
        se aplica a entradas inseridas manualmente.

    `dias_restantes` e' recalculado on-the-fly (diff de datas, barato).
    """
    hoje = dt.date.today()
    pendentes = listar_pendentes()
    ativos = _set_pajs_ativos()

    out: list[dict] = []
    for p in pendentes:
        manual = bool(p.get("manual"))
        if not manual and p.get("paj_norm") not in ativos:
            continue

        data_alvo_str = (p.get("data_alvo") or "").strip()
        restantes: int | None = None
        if data_alvo_str:
            try:
                d = dt.date.fromisoformat(data_alvo_str)
                restantes = dias_restantes(d, hoje)
            except ValueError:
                restantes = None
        out.append({
            **p,
            "rito": p.get("rito") or ("manual" if manual else "civel"),
            "manual": manual,
            "dias_restantes": restantes,
        })

    def _chave(item: dict):
        da = (item.get("data_alvo") or "").strip()
        return (da == "", da)
    out.sort(key=_chave)
    return {"prazos": out, "hoje": hoje.isoformat()}


@router.post("/api/prazos", response_class=JSONResponse)
async def criar_prazo_manual(payload: PrazoManualIn):
    """Cria um prazo manual no JSONL.

    O defensor pode adicionar prazos para qualquer PAJ que pretenda monitorar,
    inclusive PAJs fora da caixa ou nao cadastrados no workspace. So validamos
    o formato do paj_norm (suave — aceita o padrao DPU) e o formato da data.
    """
    paj_norm = payload.paj_norm.strip()
    data_alvo = payload.data_alvo.strip()

    if not _DATA_ISO_RE.match(data_alvo):
        raise HTTPException(status_code=400, detail="data_alvo deve ser YYYY-MM-DD")
    try:
        dt.date.fromisoformat(data_alvo)
    except ValueError:
        raise HTTPException(status_code=400, detail="data_alvo invalida")

    if not _PAJ_RE.match(paj_norm):
        raise HTTPException(
            status_code=400,
            detail="paj_norm deve seguir o formato PAJ-YYYY-NNN-NNNNN",
        )

    descricao = (payload.descricao or "").strip()
    assistido = (payload.assistido or "").strip()

    prazo = {
        "id": f"manual-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}",
        "paj_norm": paj_norm,
        "titulo": f"[Prazo manual] {paj_norm}" + (f" — {assistido[:60]}" if assistido else ""),
        "descricao": descricao,
        "data_mov": "",
        "prazo_dias": None,
        "data_alvo": data_alvo,
        "rito": "manual",
        "em_dobro": False,
        "assistido": assistido,
        "fonte_mov_seq": 0,
        "status": "pendente",
        "manual": True,
        "detectado_em": dt.datetime.now().isoformat(timespec="seconds"),
    }
    append_prazo(prazo)
    return {"ok": True, "prazo": prazo}


@router.delete("/api/prazos/{prazo_id}", response_class=JSONResponse)
async def apagar_prazo(prazo_id: str):
    """Remove definitivamente um prazo do JSONL (defensor marcou como cumprido).

    Sem historico — alinhado com a politica "controle leve, esvai-se apos
    cumprido". Funciona tanto para prazos automaticos quanto manuais.
    """
    if not remover_prazo(prazo_id):
        raise HTTPException(status_code=404, detail="prazo nao encontrado")
    return {"ok": True}
