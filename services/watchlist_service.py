"""Watchlist — lista de PAJs marcados pelo defensor para acompanhamento prioritario.

Diferente do dashboard (que mostra tudo que esta na caixa), a watchlist e' uma
camada de "minha selecao": o defensor marca explicitamente os PAJs que quer
manter visiveis em uma tela dedicada. Util para focar nos processos do dia.

Persistencia: `<PAJS_DIR>/.watchlist.json`. Cada item tem motivo livre,
adicionado_em e status (`ativo` | `encerrado`). Sem verificacao automatica
externa — o defensor adiciona/encerra manualmente.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR

WATCHLIST_FILE = PAJS_DIR / ".watchlist.json"


def _carregar() -> dict:
    if not WATCHLIST_FILE.exists():
        return {"itens": {}, "atualizada_em": None}
    try:
        return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"itens": {}, "atualizada_em": None}


def _salvar(wl: dict) -> None:
    wl["atualizada_em"] = datetime.now().isoformat(timespec="seconds")
    PAJS_DIR.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(
        json.dumps(wl, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def listar() -> list[dict]:
    """Retorna lista plana de itens, com `paj_norm` embutido em cada um."""
    wl = _carregar()
    itens = []
    for paj_norm, item in wl.get("itens", {}).items():
        itens.append({"paj_norm": paj_norm, **item})

    # Ordem: ativos primeiro (mais recentes no topo), encerrados depois.
    def _ord(x: dict) -> tuple:
        st = x.get("status", "ativo")
        return (0 if st == "ativo" else 1, -datetime.fromisoformat(x.get("adicionado_em", "1970-01-01T00:00:00")).timestamp() if x.get("adicionado_em") else 0)

    with contextlib.suppress(Exception):
        itens.sort(key=_ord)
    return itens


def stats() -> dict:
    wl = _carregar()
    itens = wl.get("itens", {})
    return {
        "total": len(itens),
        "ativo": sum(1 for v in itens.values() if v.get("status") == "ativo"),
        "encerrado": sum(1 for v in itens.values() if v.get("status") == "encerrado"),
        "atualizada_em": wl.get("atualizada_em"),
    }


def is_watched(paj_norm: str) -> bool:
    """True se o PAJ esta na watchlist (independente de status)."""
    wl = _carregar()
    return paj_norm in wl.get("itens", {})


def get(paj_norm: str) -> dict | None:
    wl = _carregar()
    item = wl.get("itens", {}).get(paj_norm)
    if item is None:
        return None
    return {"paj_norm": paj_norm, **item}


def adicionar(paj_norm: str, motivo: str = "") -> dict:
    """Adiciona um PAJ a' watchlist. Se ja existir, mantem o registro existente
    (nao sobrescreve motivo/data) — para alterar, use `atualizar_motivo` ou
    remova e adicione novamente.
    """
    wl = _carregar()
    itens = wl.setdefault("itens", {})
    if paj_norm in itens:
        return {"paj_norm": paj_norm, **itens[paj_norm]}
    itens[paj_norm] = {
        "motivo": (motivo or "").strip(),
        "adicionado_em": datetime.now().isoformat(timespec="seconds"),
        "status": "ativo",
        "encerrado_em": None,
    }
    _salvar(wl)
    return {"paj_norm": paj_norm, **itens[paj_norm]}


def atualizar_motivo(paj_norm: str, motivo: str) -> bool:
    wl = _carregar()
    itens = wl.get("itens", {})
    if paj_norm not in itens:
        return False
    itens[paj_norm]["motivo"] = (motivo or "").strip()
    _salvar(wl)
    return True


def encerrar(paj_norm: str) -> bool:
    """Marca como encerrado (mantem na lista, mas com status diferente)."""
    wl = _carregar()
    itens = wl.get("itens", {})
    if paj_norm not in itens:
        return False
    itens[paj_norm]["status"] = "encerrado"
    itens[paj_norm]["encerrado_em"] = datetime.now().isoformat(timespec="seconds")
    _salvar(wl)
    return True


def reativar(paj_norm: str) -> bool:
    wl = _carregar()
    itens = wl.get("itens", {})
    if paj_norm not in itens:
        return False
    itens[paj_norm]["status"] = "ativo"
    itens[paj_norm]["encerrado_em"] = None
    _salvar(wl)
    return True


def remover(paj_norm: str) -> bool:
    """Remove definitivamente do JSON. Sem historico."""
    wl = _carregar()
    itens = wl.get("itens", {})
    if paj_norm not in itens:
        return False
    del itens[paj_norm]
    _salvar(wl)
    return True
