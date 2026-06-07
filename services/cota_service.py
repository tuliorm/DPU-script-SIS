"""Re-disparo automatico da elaboracao apos a renovacao da cota de uso do Claude.

Quando o limite de uso estoura no meio de uma elaboracao (o CLI emite algo como
"You've hit your session limit · resets 1am (America/Sao_Paulo)"), o painel:
  1. extrai o horario de renovacao da mensagem (parse_reset);
  2. congela a fila — nao adianta iniciar novos PAJs, estourariam tambem;
  3. agenda o re-disparo dos pendentes pra COTA_MARGEM_MIN min apos o reset,
     retomando de onde pararam (--resume) quando ha session_id;
  4. repete em cascata ate zerar os pendentes (limitado a COTA_MAX_CICLOS).

Estado in-memory protegido por RLock e espelhado em disco
(PAJS_DIR/.cota_redisparo.json) pra sobreviver a reinicios do servidor — o
scheduler e' rearmado no startup (app.py -> carregar_e_rearmar).

Imports de chat_service/paj_service sao TARDIOS (dentro das funcoes) pra evitar
ciclo de importacao (chat_service importa este modulo tardiamente tambem).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import re
import threading
import time
from zoneinfo import ZoneInfo

from config import (
    COTA_FALLBACK_MIN,
    COTA_MARGEM_MIN,
    COTA_MAX_CICLOS,
    COTA_TICK_SEG,
    PAJS_DIR,
)

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
_ARQUIVO = PAJS_DIR / ".cota_redisparo.json"
# Trilha dos eventos stream-json que terminaram em erro/cota — usada pra
# calibrar a deteccao na 1a ocorrencia real (ver logar_evento_bruto).
_ARQUIVO_EVENTOS = PAJS_DIR / ".cota_eventos.jsonl"

_ESTADO_PADRAO: dict = {
    "reset_em": None,  # ISO str — horario de renovacao informado pelo CLI
    "redisparo_em": None,  # ISO str — reset_em + COTA_MARGEM_MIN
    "pendentes": {},  # paj_norm -> {skill, session_id, motivo, desde}
    "ciclo": 0,
    "pausado": False,  # True => _process_queue nao promove (fila congelada)
    "ultimo_log": "",
}
_estado: dict = json.loads(json.dumps(_ESTADO_PADRAO))
_scheduler_iniciado = False

# Sinais de estouro de COTA/sessao (que reseta em horas) — distingue do 429
# transitorio (system/api_retry), que o CLI ja re-tenta sozinho.
_SINAIS_COTA = ("session limit", "usage limit", "hit your", "limit reached")
_RE_RESET = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)


def _agora() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    with contextlib.suppress(Exception):
        return _dt.datetime.fromisoformat(s)
    return None


def texto_indica_cota(texto: str | None) -> bool:
    """True se o texto contem um sinal conhecido de estouro de cota/sessao."""
    if not texto:
        return False
    low = texto.lower()
    return any(s in low for s in _SINAIS_COTA)


def parse_reset(texto: str | None, agora: _dt.datetime | None = None) -> _dt.datetime | None:
    """Extrai o horario de renovacao da mensagem de cota. Retorna a PROXIMA
    ocorrencia daquele horario (datetime aware) ou None se nao parsear.

    Ex.: "resets 1am (America/Sao_Paulo)", "resets 1:30pm", "resets 11 PM".
    """
    if not texto:
        return None
    m = _RE_RESET.search(texto)
    if not m:
        return None
    hora = int(m.group(1))
    minuto = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    tzname = (m.group(4) or "").strip()
    if ampm == "pm" and hora != 12:
        hora += 12
    elif ampm == "am" and hora == 12:
        hora = 0
    if not (0 <= hora <= 23 and 0 <= minuto <= 59):
        return None
    base = agora or _agora()
    if tzname:
        with contextlib.suppress(Exception):
            base = base.astimezone(ZoneInfo(tzname))
    alvo = base.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if alvo <= base:
        alvo += _dt.timedelta(days=1)
    return alvo


def _salvar() -> None:
    with contextlib.suppress(Exception):
        if PAJS_DIR.exists():
            _ARQUIVO.write_text(
                json.dumps(_estado, ensure_ascii=False, indent=2), encoding="utf-8"
            )


def _carregar() -> None:
    global _estado
    with contextlib.suppress(Exception):
        if _ARQUIVO.exists():
            dados = json.loads(_ARQUIVO.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                _estado = {**json.loads(json.dumps(_ESTADO_PADRAO)), **dados}


def esta_pausado() -> bool:
    """Consultado por chat_service._process_queue pra congelar a fila enquanto
    a cota esta esgotada (nao adianta iniciar novos PAJs)."""
    return bool(_estado.get("pausado"))


def logar_evento_bruto(paj_norm: str, event: dict) -> None:
    """Append do evento stream-json bruto que terminou em erro/cota, em
    PAJs/.cota_eventos.jsonl. Serve pra CALIBRAR a deteccao na 1a ocorrencia
    real (confirmar se o CLI traz subtype/api_error_status estruturado ou so a
    string). Best-effort — nunca quebra o fluxo de elaboracao."""
    with contextlib.suppress(Exception):
        if PAJS_DIR.exists():
            registro = {"ts": _agora().isoformat(), "paj": paj_norm, "event": event}
            with _ARQUIVO_EVENTOS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    with contextlib.suppress(Exception):
        log.warning(
            "[cota] evento de erro/cota: paj=%s subtype=%s is_error=%s api_status=%s",
            paj_norm,
            event.get("subtype"),
            event.get("is_error"),
            event.get("api_error_status"),
        )


def registrar_estouro(
    paj_norm: str,
    texto_erro: str,
    skill: str | None = None,
    session_id: str | None = None,
) -> None:
    """Registra o estouro de cota de um PAJ: agenda/atualiza o re-disparo e
    congela a fila. Chamado pelo chat_service ao detectar o limite."""
    with _LOCK:
        agora = _agora()
        reset = parse_reset(texto_erro, agora)
        if reset is None:
            reset = agora + _dt.timedelta(minutes=COTA_FALLBACK_MIN)
            origem = f"fallback {COTA_FALLBACK_MIN}min (horario nao detectado)"
        else:
            origem = "horario informado pelo CLI"
        redisparo = reset + _dt.timedelta(minutes=COTA_MARGEM_MIN)
        # Mantem o re-disparo mais TARDIO entre os informados nesta janela
        # (varios PAJs podem estourar com horarios ligeiramente diferentes).
        atual = _parse_iso(_estado.get("redisparo_em"))
        if atual is None or redisparo > atual:
            _estado["reset_em"] = reset.isoformat()
            _estado["redisparo_em"] = redisparo.isoformat()
        _estado["pendentes"][paj_norm] = {
            "skill": skill or "",
            "session_id": session_id or "",
            "motivo": "cota esgotada durante a elaboração",
            "desde": agora.isoformat(),
        }
        _estado["pausado"] = True
        _estado["ultimo_log"] = (
            f"cota esgotada ({origem}); re-disparo em {_estado['redisparo_em']}"
        )
        _salvar()
        log.warning("[cota] %s — %s", paj_norm, _estado["ultimo_log"])
    pausar_fila()
    iniciar_scheduler()


def pausar_fila() -> None:
    """Move pra os pendentes os PAJs que estavam na fila (ainda nao iniciados) —
    eles estourariam tambem. Nao mexe nos que ja estao rodando."""
    from services import chat_service as cs

    with _LOCK:
        for paj in list(cs._queue):
            sess = cs._sessions.get(paj)
            _estado["pendentes"].setdefault(
                paj,
                {
                    "skill": (sess.skill_slug if sess else "") or "",
                    "session_id": (getattr(sess, "session_id", "") if sess else "") or "",
                    "motivo": "estava na fila quando a cota esgotou",
                    "desde": _agora().isoformat(),
                },
            )
        cs._queue.clear()
        _salvar()


def executar_redisparo(forcado: bool = False) -> dict:
    """Re-dispara os pendentes que ainda nao tem peca. Chamado pelo scheduler
    (na hora) ou manualmente (disparar_agora). Em cascata: se estourar de novo,
    registrar_estouro re-arma o agendamento."""
    from services import chat_service as cs
    from services.paj_service import listar_pajs

    with _LOCK:
        if not _estado["pendentes"]:
            _limpar_agendamento_locked()
            return {"disparados": 0, "pendentes": 0}
        if _estado["ciclo"] >= COTA_MAX_CICLOS and not forcado:
            _estado["pausado"] = False  # destrava a fila; aguarda acao manual
            _estado["ultimo_log"] = (
                f"teto de {COTA_MAX_CICLOS} ciclos atingido — re-disparo automatico "
                "pausado; use 'Disparar agora' pra continuar"
            )
            _salvar()
            log.warning("[cota] %s", _estado["ultimo_log"])
            return {"disparados": 0, "pendentes": len(_estado["pendentes"]), "teto": True}
        pendentes = dict(_estado["pendentes"])
        _estado["pausado"] = False  # libera a fila pra promover os re-disparados
        _estado["ciclo"] += 1
        ciclo = _estado["ciclo"]
        # Limpa o agendamento; se algum estourar de novo, registrar_estouro re-arma.
        _estado["reset_em"] = None
        _estado["redisparo_em"] = None
        _estado["ultimo_log"] = f"re-disparo ciclo {ciclo}: {len(pendentes)} PAJ(s)"
        _salvar()
    log.info("[cota] re-disparo ciclo %s — %s pendentes", ciclo, len(pendentes))

    # Pula os que ja tem peca gerada (concluidos em algum ciclo anterior).
    com_peca = {
        p["paj_norm"]
        for p in listar_pajs(incluir_concluidos=True)
        if p.get("n_pecas", 0) > 0
    }
    disparados = 0
    for paj, info in pendentes.items():
        with _LOCK:
            _estado["pendentes"].pop(paj, None)  # sai da lista; re-entra se estourar
            _salvar()
        if paj in com_peca:
            continue
        with contextlib.suppress(Exception):
            cs.start_or_queue(
                paj,
                skill_slug=(info.get("skill") or None),
                resume_session_id=(info.get("session_id") or None),
            )
            disparados += 1
    return {"disparados": disparados, "pendentes": len(_estado["pendentes"])}


def _limpar_agendamento_locked() -> None:
    _estado["reset_em"] = None
    _estado["redisparo_em"] = None
    _estado["pausado"] = False
    _salvar()


def disparar_agora() -> dict:
    """Forca o re-disparo imediato (botao da UI), ignorando o agendamento."""
    return executar_redisparo(forcado=True)


def cancelar() -> dict:
    """Cancela o agendamento e descarta os pendentes (botao da UI)."""
    global _estado
    with _LOCK:
        n = len(_estado.get("pendentes", {}))
        _estado = json.loads(json.dumps(_ESTADO_PADRAO))
        _salvar()
    log.info("[cota] agendamento cancelado pelo usuario (%s pendentes descartados)", n)
    return {"cancelados": n}


def status() -> dict:
    """Resumo pro dashboard (banner + botoes)."""
    with _LOCK:
        pend = _estado.get("pendentes", {})
        return {
            "ativo": bool(_estado.get("redisparo_em")) or bool(pend),
            "pausado": bool(_estado.get("pausado")),
            "reset_em": _estado.get("reset_em"),
            "redisparo_em": _estado.get("redisparo_em"),
            "ciclo": _estado.get("ciclo", 0),
            "max_ciclos": COTA_MAX_CICLOS,
            "n_pendentes": len(pend),
            "pendentes": [{"paj": k, **v} for k, v in pend.items()],
            "ultimo_log": _estado.get("ultimo_log", ""),
        }


def _tick() -> None:
    with _LOCK:
        redisparo = _parse_iso(_estado.get("redisparo_em"))
        tem_pendentes = bool(_estado.get("pendentes"))
    if redisparo and tem_pendentes and _agora() >= redisparo:
        executar_redisparo()


def _loop_scheduler() -> None:
    intervalo = max(5, COTA_TICK_SEG)
    while True:
        time.sleep(intervalo)
        with contextlib.suppress(Exception):
            _tick()


def iniciar_scheduler() -> None:
    """Inicia o verificador periodico (idempotente)."""
    global _scheduler_iniciado
    with _LOCK:
        if _scheduler_iniciado:
            return
        _scheduler_iniciado = True
    threading.Thread(target=_loop_scheduler, daemon=True, name="cota-scheduler").start()
    log.info("[cota] scheduler iniciado (tick %ss)", max(5, COTA_TICK_SEG))


def carregar_e_rearmar() -> None:
    """Chamado no startup do app: le o estado persistido e arma o scheduler.
    Se o horario de re-disparo ja passou (servidor estava fora do ar), o
    proximo _tick dispara imediatamente."""
    _carregar()
    iniciar_scheduler()
    with _LOCK:
        if _estado.get("pendentes"):
            log.info(
                "[cota] estado restaurado: %s pendente(s), re-disparo %s",
                len(_estado["pendentes"]),
                _estado.get("redisparo_em") or "(sem horario)",
            )
