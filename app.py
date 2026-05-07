"""Painel web do workspace Oficio Geral — dashboard, detalhe de PAJ, chat, docgen.

Auto-gerencia processo: mata qualquer python na porta antes de iniciar,
grava PID file. Evita duplicatas.
"""

import contextlib
import logging
import os
import subprocess
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import jinja2
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routes.busca import router as busca_router
from routes.calendar import router as calendar_router
from routes.chat import router as chat_router
from routes.chat_livre import router as chat_livre_router
from routes.dashboard import router as dashboard_router
from routes.docgen import router as docgen_router
from routes.files import router as files_router
from routes.paj import router as paj_router
from routes.sync import router as sync_router
from services.ambiente_service import verificar_ambiente

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".server.pid"
LOGS_DIR = BASE_DIR / "logs"
PORT = 8001


def _configurar_logging() -> None:
    """Configura logging com rotacao diaria em logs/app.log (mantem 7 dias).

    Mantem stdout tambem (uvicorn ja usa). Print()s pre-existentes nao sao
    redirecionados — app continua imprimindo no console como antes."""
    LOGS_DIR.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "app.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Evita duplicar handlers se a funcao for chamada mais de uma vez (reload).
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


_configurar_logging()

# Versao do painel — fonte unica. Usada pelo FastAPI (OpenAPI/docs) e tambem
# exposta a todos os templates via Jinja globals (renderizada no rodape da
# sidebar). Para incrementar: mude aqui e so aqui.
APP_VERSION = "0.3.12"

app = FastAPI(title="oficio-geral-ui", version=APP_VERSION)
app.state.jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    auto_reload=True,
)
# Expoe a versao a todos os templates sem precisar passar no render() de cada rota
app.state.jinja.globals["app_version"] = APP_VERSION

# Mapeamento de exibicao para nomes de area (foro). Os valores armazenados em
# metadata.json vem sem acentos (parser do SISDPU); o front-end formata para
# ortografia portuguesa correta. Comparacoes/filtros continuam usando o valor
# cru — so o RENDER usa o filtro abaixo.
_AREA_DISPLAY = {
    "civel": "Cível",
    "previdenciario": "Previdenciário",
    "saude": "Saúde",
    "execucao": "Execução",
    "criminal": "Criminal",
    "familia": "Família",
    "tributario": "Tributário",
}


def _formatar_area(valor: str | None) -> str:
    if not valor:
        return ""
    return _AREA_DISPLAY.get(str(valor).lower(), valor)


app.state.jinja.filters["formatar_area"] = _formatar_area
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(dashboard_router)
app.include_router(paj_router)
app.include_router(files_router)
app.include_router(chat_router)
app.include_router(chat_livre_router)
app.include_router(docgen_router)
app.include_router(sync_router)
app.include_router(busca_router)
app.include_router(calendar_router)

# Healthcheck de dependencias externas — expoe em app.state.ambiente pra
# o dashboard renderizar banner quando algo falta.
app.state.ambiente = verificar_ambiente()
for _aviso in app.state.ambiente.get("avisos", []):
    print(f"[AMBIENTE] {_aviso}")


def _cleanup_port(port: int) -> None:
    """Mata TODOS processos LISTEN na porta informada (Windows-only)."""
    if os.name != "nt":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True
        )
    except Exception:
        return
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid <= 4 or pid == os.getpid() or pid in seen:
            continue
        seen.add(pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"[app] killed stale process on port {port}: PID {pid}")


def _kill_pid_file() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
            print(f"[app] killed previous server PID {old_pid}")
    except (ValueError, FileNotFoundError):
        pass
    with contextlib.suppress(Exception):
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    _kill_pid_file()
    _cleanup_port(PORT)
    import time
    time.sleep(1)  # aguarda SO liberar porta

    PID_FILE.write_text(str(os.getpid()))
    print(f"[app] servidor PID={os.getpid()} — http://127.0.0.1:{PORT}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False)
    finally:
        with contextlib.suppress(Exception):
            PID_FILE.unlink(missing_ok=True)
