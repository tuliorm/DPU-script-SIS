"""Gestao do arquivo de prazos pendentes (JSONL).

O sincronizador detecta intimacoes/citacoes e grava linhas em
`PAJS_DIR/.pendentes_calendar.jsonl`. Uma skill Claude (/sync_calendar) le
esse arquivo, cria eventos via MCP de calendario e marca status="enviado".

Este service expoe:
- listar_pendentes()
- contar_pendentes()
- marcar_processado(id, event_id)
- append_prazo(...)  [usado pelo sincronizador]
"""

from __future__ import annotations

import json

from config import PAJS_DIR
import contextlib


JSONL = PAJS_DIR / ".pendentes_calendar.jsonl"


def _ler_todos() -> list[dict]:
    if not JSONL.exists():
        return []
    out: list[dict] = []
    try:
        for linha in JSONL.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha:
                continue
            try:
                out.append(json.loads(linha))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _regravar(lista: list[dict]) -> None:
    if not lista:
        if JSONL.exists():
            with contextlib.suppress(Exception):
                JSONL.unlink()
        return
    linhas = [json.dumps(d, ensure_ascii=False) for d in lista]
    JSONL.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def listar_pendentes() -> list[dict]:
    """Retorna apenas os que ainda nao foram enviados ao calendario."""
    return [d for d in _ler_todos() if d.get("status") != "enviado"]


def contar_pendentes() -> int:
    return len(listar_pendentes())


def marcar_processado(prazo_id: str, event_id: str = "") -> bool:
    """Marca um prazo como enviado. Retorna False se nao achou o id."""
    todos = _ler_todos()
    achado = False
    for d in todos:
        if d.get("id") == prazo_id:
            d["status"] = "enviado"
            if event_id:
                d["event_id"] = event_id
            achado = True
            break
    if achado:
        _regravar(todos)
    return achado


def append_prazo(prazo: dict) -> None:
    """Adiciona um prazo novo ao JSONL (chamado pelo sincronizador)."""
    # Evita duplicar se ja existe um com mesmo id
    existentes = _ler_todos()
    if any(d.get("id") == prazo.get("id") for d in existentes):
        return
    PAJS_DIR.mkdir(parents=True, exist_ok=True)
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(prazo, ensure_ascii=False) + "\n")


def remover_prazo(prazo_id: str) -> bool:
    """Remove definitivamente um prazo do JSONL. Retorna False se nao achou.

    Diferente de marcar_processado() (que mantem o registro com status="enviado"),
    aqui apagamos a linha — usado quando o defensor marca como cumprido na UI
    (controle leve, sem historico)."""
    todos = _ler_todos()
    novo = [d for d in todos if d.get("id") != prazo_id]
    if len(novo) == len(todos):
        return False
    _regravar(novo)
    return True
