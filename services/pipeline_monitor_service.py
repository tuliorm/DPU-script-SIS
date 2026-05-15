"""Servico de leitura do estado e logs da sincronizacao SISDPU.

Adaptado do `pipeline_monitor_service` do ScriptTNU-STJ. La' o pipeline e' um
subprocess externo (`preparar_pajs.py` em `dpu-workspace`) que escreve um
`state.json`. Aqui o "pipeline" e' a propria sincronizacao SISDPU rodando
dentro deste app via `services.sync_service`.

Sem instrumentar o sync_service: lemos `esta_rodando()` em runtime e os
arquivos de `logs/app.log` (rotacionados diariamente pelo TimedRotatingFileHandler
configurado em app.py).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from services.sync_service import esta_rodando

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_ATUAL = LOG_DIR / "app.log"

_LINHA_DATA_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def ler_state() -> dict:
    """Estado atual da sincronizacao.

    Sem `state.json` persistente — devolvemos apenas se ha' uma sync rodando
    e o timestamp atual. Para ter contadores (processados/total/falhas) seria
    necessario instrumentar `sync_service.py` — fora do escopo aqui.
    """
    rodando = esta_rodando()
    return {
        "status": "rodando" if rodando else "parado",
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
        "log_file": str(LOG_ATUAL) if LOG_ATUAL.exists() else None,
    }


def ler_log_atual(max_linhas: int = 500) -> dict:
    """Ultimas N linhas de `logs/app.log` (a execucao atual)."""
    if not LOG_ATUAL.exists():
        return {"linhas": [], "caminho": None, "tamanho": 0}
    try:
        linhas = LOG_ATUAL.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {"linhas": [], "caminho": str(LOG_ATUAL), "tamanho": 0}
    return {
        "linhas": linhas[-max_linhas:],
        "caminho": str(LOG_ATUAL),
        "tamanho": len(linhas),
    }


def listar_runs(max_runs: int = 20) -> list[dict]:
    """Lista os arquivos de log rotacionados (`app.log.YYYY-MM-DD`).

    O `TimedRotatingFileHandler` rotaciona a meia-noite e mantem `backupCount=7`
    dias (configurado em app.py). Retornamos em ordem decrescente de data.
    """
    if not LOG_DIR.exists():
        return []

    runs: list[dict] = []
    # Padroes possiveis: app.log.YYYY-MM-DD, app.log.2026-04-16, etc.
    for f in sorted(LOG_DIR.glob("app.log.*"), reverse=True)[:max_runs]:
        if not f.is_file():
            continue
        try:
            stat = f.stat()
            sufixo = f.name.replace("app.log.", "", 1)
            try:
                data = datetime.strptime(sufixo, "%Y-%m-%d")
            except ValueError:
                data = datetime.fromtimestamp(stat.st_mtime)

            # Tenta detectar resumo a partir das ultimas linhas
            resumo = ""
            n_linhas = 0
            try:
                conteudo = f.read_text(encoding="utf-8", errors="replace")
                n_linhas = conteudo.count("\n")
                for linha in reversed(conteudo.splitlines()):
                    txt = linha.strip()
                    if not txt:
                        continue
                    upper = txt.upper()
                    if (
                        "FIM" in upper
                        or "CONCLUI" in upper
                        or "PROCESSADO" in upper
                        or "[ERRO" in upper
                    ):
                        resumo = txt[:200]
                        break
            except Exception:
                pass

            runs.append({
                "nome": f.name,
                "caminho": str(f),
                "data": data.isoformat(),
                "tamanho_bytes": stat.st_size,
                "tamanho_linhas": n_linhas,
                "resumo": resumo,
                "modificado": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except Exception:
            continue

    return runs


def ler_run(nome_arquivo: str, max_linhas: int = 2000) -> dict:
    """Le um log rotacionado pelo nome (com path traversal safety)."""
    if not nome_arquivo.startswith("app.log."):
        return {"linhas": [], "erro": "arquivo invalido"}
    f = LOG_DIR / nome_arquivo
    try:
        f.resolve().relative_to(LOG_DIR.resolve())
    except ValueError:
        return {"linhas": [], "erro": "caminho invalido"}
    if not f.exists():
        return {"linhas": [], "erro": "arquivo nao encontrado"}
    try:
        linhas = f.read_text(encoding="utf-8", errors="replace").splitlines()
        return {
            "nome": nome_arquivo,
            "linhas": linhas[-max_linhas:],
            "total_linhas": len(linhas),
            "caminho": str(f),
        }
    except Exception as e:
        return {"linhas": [], "erro": str(e)}
