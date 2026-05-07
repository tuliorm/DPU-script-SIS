"""Historico estruturado de eventos por PAJ — append-only em JSONL.

Cada PAJ tem um `historico.jsonl` na sua pasta. Cada linha e' um dict com no
minimo `ts` (ISO 8601) e `evento`. Demais campos sao livres por evento.

Eventos atualmente registrados:
  - elaborar    : sessao Claude concluida (skill, status, summary_first_line)
  - correcao    : usuario pediu correcao/refazer (texto_correcao truncado)
  - gerar_docx  : DOCX/PDF gerado (formato, arquivo_origem, arquivo_saida)

A escrita e' best-effort: tudo embrulhado em try/suppress, append em modo
texto, sem flock/lock — o painel e' single-user (uvicorn workers=1) e os
eventos sao raros o suficiente pra colisao ser improvavel. Se algum dia
virar concorrente, troca-se por filelock.

Leitura: `ler(paj_norm, limit=100)` retorna lista de eventos (mais recentes
primeiro). Util pra UI exibir um timeline do PAJ.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
from pathlib import Path
from typing import Any

from config import PAJS_DIR


_NOME_ARQUIVO = "historico.jsonl"
_MAX_TEXTO_LIVRE = 1000  # limita campos longos pra arquivo nao virar log gigante


def _pasta_paj(paj_norm: str) -> Path | None:
    """Valida e retorna a pasta do PAJ (ou None se invalido)."""
    if not paj_norm or "/" in paj_norm or "\\" in paj_norm or ".." in paj_norm:
        return None
    if not paj_norm.startswith("PAJ-"):
        return None
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not pasta.is_dir():
        return None
    return pasta


def _truncar(valor: Any) -> Any:
    """Trunca strings longas pra evitar inflar o JSONL com PROMPT_MAX inteiro."""
    if isinstance(valor, str) and len(valor) > _MAX_TEXTO_LIVRE:
        return valor[:_MAX_TEXTO_LIVRE].rstrip() + "..."
    return valor


def registrar(paj_norm: str, evento: str, **dados: Any) -> bool:
    """Append nao-bloqueante de um evento no historico.jsonl do PAJ.

    Retorna True se gravou, False se PAJ invalido ou falha de IO. Falhas sao
    silenciosas — historico nunca deve interromper o fluxo principal.
    """
    pasta = _pasta_paj(paj_norm)
    if pasta is None:
        return False
    if not evento:
        return False

    registro: dict[str, Any] = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "evento": str(evento),
    }
    for k, v in dados.items():
        if v is None or v == "":
            continue
        registro[str(k)] = _truncar(v)

    arquivo = pasta / _NOME_ARQUIVO
    try:
        with arquivo.open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def ler(paj_norm: str, limit: int | None = None) -> list[dict]:
    """Le o historico em ordem cronologica reversa (mais recente primeiro).

    `limit=None` retorna tudo. Linhas malformadas sao puladas silenciosamente.
    """
    pasta = _pasta_paj(paj_norm)
    if pasta is None:
        return []
    arquivo = pasta / _NOME_ARQUIVO
    if not arquivo.exists():
        return []

    eventos: list[dict] = []
    with contextlib.suppress(Exception):
        for linha in arquivo.read_text(encoding="utf-8", errors="replace").splitlines():
            linha = linha.strip()
            if not linha:
                continue
            try:
                eventos.append(json.loads(linha))
            except json.JSONDecodeError:
                continue

    eventos.reverse()
    if limit is not None and limit > 0:
        eventos = eventos[:limit]
    return eventos


def _primeira_linha_util(texto: str) -> str:
    """Extrai a primeira linha nao-vazia de um texto. Util pra resumir
    `summary` de elaboracao em uma frase no historico."""
    if not texto:
        return ""
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha:
            return linha[:200]
    return ""
