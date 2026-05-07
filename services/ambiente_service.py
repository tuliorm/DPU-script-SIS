"""Healthcheck de dependencias externas (Tesseract, Chromium).

Rodado no startup de app.py — resultado fica em `app.state.ambiente` pra ser
exibido no dashboard como banner amarelo quando algo falta.

Barato (cache em memoria), nao refaz check durante a vida do processo.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from config import validar_paths


_cache: dict | None = None


def _tesseract_ok() -> tuple[bool, str]:
    """Retorna (ok, versao_ou_motivo)."""
    exe = shutil.which("tesseract")
    if not exe:
        return (False, "tesseract nao encontrado no PATH")
    # Tenta executar --version sem travar
    try:
        import subprocess
        out = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        linha_1 = (out.stdout or out.stderr or "").splitlines()[0] if (out.stdout or out.stderr) else ""
        return (True, linha_1.strip() or "ok")
    except Exception as e:
        return (False, f"erro ao executar: {type(e).__name__}: {e}")


def _chromium_ok() -> tuple[bool, str]:
    """Retorna (ok, motivo).

    Checa se o Chromium do Playwright foi instalado via
    `python -m playwright install chromium` — procura o diretorio de cache
    padrao (`~/AppData/Local/ms-playwright` no Windows, `~/.cache/ms-playwright`
    em outros).
    """
    candidatos = [
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for c in candidatos:
        if not c.exists():
            continue
        # Precisa ter pelo menos um sub-diretorio chromium-*
        try:
            for sub in c.iterdir():
                if sub.is_dir() and sub.name.lower().startswith("chromium"):
                    return (True, f"encontrado em {sub}")
        except Exception:
            continue
    return (
        False,
        "chromium do playwright nao encontrado — rode `python -m playwright install chromium`",
    )


def verificar_ambiente(forcar: bool = False) -> dict:
    """Retorna dict com status das dependencias externas.

    {
        "tesseract_ok": bool,
        "tesseract_info": str,
        "chromium_ok": bool,
        "chromium_info": str,
        "avisos": [str],  # lista pra UI
    }

    Cacheia em memoria — so refaz se `forcar=True`.
    """
    global _cache
    if _cache is not None and not forcar:
        return _cache

    t_ok, t_info = _tesseract_ok()
    c_ok, c_info = _chromium_ok()
    erros_path = validar_paths()

    avisos: list[str] = []
    for erro in erros_path:
        avisos.append(f"Workspace ausente — {erro}. Ajuste OFICIO_GERAL no .env.")
    if not t_ok:
        avisos.append(
            f"Tesseract ausente — OCR desligado. {t_info}. "
            "Instale pelo README (UB-Mannheim) e reinicie o painel."
        )
    if not c_ok:
        avisos.append(
            f"Chromium do Playwright ausente — sync SISDPU vai falhar. {c_info}."
        )

    _cache = {
        "tesseract_ok": t_ok,
        "tesseract_info": t_info,
        "chromium_ok": c_ok,
        "chromium_info": c_info,
        "avisos": avisos,
    }
    return _cache
