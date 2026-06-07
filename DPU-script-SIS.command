#!/bin/bash
# ============================================================
#  DPU-script-SIS — inicia o servidor (se precisar) e abre o browser
#  Equivalente macOS do DPU-script-SIS.bat
#  Uso: duplo clique no Finder, ou  ./DPU-script-SIS.command  no terminal
# ============================================================
set -euo pipefail

# Raiz do projeto = pasta deste script (duplo clique nao herda o cwd certo)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
PORT=8001
URL="http://127.0.0.1:$PORT/"

# Abre a URL trazendo o navegador padrao PARA A FRENTE.
# `open URL` sozinho as vezes so cria uma aba em segundo plano sem ativar o
# navegador. Por isso descobrimos o navegador padrao (handler de http/https no
# LaunchServices) e usamos `open -b <bundle-id>`, que ativa o app. Se a
# deteccao falhar, cai no `open` simples (comportamento antigo, sem regressao).
abrir_browser() {
  local url="$1" bid=""
  if [ -x "$PYTHON" ]; then
    bid="$("$PYTHON" -c '
import os, plistlib, sys
p = os.path.expanduser("~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist")
try:
    d = plistlib.load(open(p, "rb"))
except Exception:
    sys.exit(0)
for h in d.get("LSHandlers", []):
    if h.get("LSHandlerURLScheme") in ("http", "https"):
        b = h.get("LSHandlerRoleAll") or h.get("LSHandlerRoleViewer") or ""
        if b:
            print(b)
            break
' 2>/dev/null)" || bid=""
  fi
  if [ -n "$bid" ] && open -b "$bid" "$url" 2>/dev/null; then
    return 0
  fi
  open "$url"
}

# Servidor ja esta de pe? (porta em LISTEN) -> so abre o browser
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Servidor ja esta rodando. Abrindo o browser..."
  abrir_browser "$URL"
  exit 0
fi

# Sanidade: a venv precisa existir
if [ ! -x "$PYTHON" ]; then
  echo "ERRO: ambiente virtual nao encontrado em:"
  echo "  $PYTHON"
  echo
  echo "Crie uma vez com:"
  echo "  python3.12 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  echo "  .venv/bin/python -m playwright install chromium"
  echo
  read -n 1 -s -r -p "Pressione qualquer tecla para fechar..."
  exit 1
fi

echo "Iniciando o servidor DPU-script-SIS..."
# Sobe desacoplado do terminal (sobrevive a fechar a janela); log em arquivo
nohup "$PYTHON" "$ROOT/app.py" >"$ROOT/.server.log" 2>&1 &

# Aguarda o servidor responder (polling, em vez de espera fixa)
printf "Aguardando o servidor subir"
for _ in $(seq 1 40); do
  if curl -fsS --max-time 1 "$URL" >/dev/null 2>&1; then
    printf " pronto!\n"
    echo "Abrindo $URL"
    abrir_browser "$URL"
    exit 0
  fi
  printf "."
  sleep 0.5
done

printf "\n"
echo "O servidor nao respondeu a tempo. Confira o log:"
echo "  $ROOT/.server.log"
read -n 1 -s -r -p "Pressione qualquer tecla para fechar..."
exit 1
