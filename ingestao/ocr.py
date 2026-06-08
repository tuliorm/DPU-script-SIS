"""Extracao de texto de PDFs via fitz (PyMuPDF) + Tesseract fallback.

Funcao publica: `extrair_texto(pdf_path, *, deve_cancelar=None, timeout_por_pagina_seg=30)`.

Se o PDF tem texto nativo (>200 chars totais), usa fitz direto. Caso contrario,
renderiza cada pagina como imagem e roda Tesseract (lang="por"). Se o Tesseract
nao estiver instalado, devolve placeholder indicando OCR indisponivel.

Suporta cancelamento cooperativo entre paginas (`deve_cancelar` e' consultado antes
de processar cada pagina) e timeout por pagina no Tesseract (evita travar em PDFs
gigantes/corrompidos).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

_MIN_NATIVE_CHARS = 200


@lru_cache(maxsize=1)
def _ocrmypdf_exe() -> str | None:
    """Caminho do binario `ocrmypdf` (ou None se ausente). Cacheado."""
    return shutil.which("ocrmypdf")


def _preproc_config() -> tuple[bool, int]:
    """(ativo, timeout_seg) do pre-processamento ocrmypdf. Import tardio de
    config pra manter este modulo desacoplado/testavel."""
    try:
        from config import OCR_PREPROC_ATIVO, OCR_PREPROC_TIMEOUT_SEG

        return bool(OCR_PREPROC_ATIVO), int(OCR_PREPROC_TIMEOUT_SEG)
    except Exception:
        return True, 600


def _ocr_com_ocrmypdf(pdf_path: Path, timeout_seg: int) -> str | None:
    """OCR de 1a linha via `ocrmypdf` (deskew/clean/rotate + Tesseract sob
    Ghostscript). Devolve o texto (formatado por paginas) ou None se o binario
    estiver ausente, der timeout, falhar ou sair vazio — nesses casos o chamador
    cai no Tesseract pagina-a-pagina (comportamento antigo).

    Usa `--force-ocr` porque so' chamamos isto quando o texto nativo e'
    insuficiente (PDF escaneado): forcar garante OCR mesmo em paginas com
    texto-imagem residual, e evita o erro "page already has text".
    """
    exe = _ocrmypdf_exe()
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="ocrmypdf_") as td:
        out_pdf = Path(td) / "out.pdf"
        sidecar = Path(td) / "sidecar.txt"
        cmd = [
            exe,
            "--language",
            "por",
            "--force-ocr",
            "--deskew",
            "--rotate-pages",
            "--clean",
            "--quiet",
            str(pdf_path),
            str(out_pdf),
            "--sidecar",
            str(sidecar),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_seg)
        except Exception:
            # TimeoutExpired (o run mata o processo) ou qualquer falha de spawn.
            return None
        if proc.returncode != 0 or not sidecar.exists():
            return None
        try:
            texto = sidecar.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    texto = (texto or "").strip()
    if len(texto) < 20:
        return None
    # ocrmypdf/Tesseract separam paginas com form-feed (\f). Reescreve no padrao
    # "=== pagina N ===" usado pelo extrator nativo (consistencia p/ o resto).
    blocos: list[str] = []
    n = 0
    for pg in texto.split("\f"):
        pg = pg.strip()
        if not pg:
            continue
        n += 1
        blocos.append(f"=== pagina {n} ===\n{pg}\n")
    if not blocos:
        return None
    return "\n".join(blocos)


def _ocr_pagina(pagina, timeout_seg: int) -> tuple[str, bool]:
    """Renderiza pagina e roda Tesseract com timeout.

    Retorna (texto, tesseract_presente). `texto` pode ser "" se der timeout ou
    erro. `tesseract_presente` vira False so quando Tesseract nao esta instalado
    (pra sinalizar pro chamador que nao vale tentar as proximas paginas).
    """
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError:
        return ("", False)

    try:
        pix = pagina.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        # pytesseract aceita timeout (em segundos) — aborta o subprocess se
        # estourar, levantando RuntimeError("Tesseract process timeout").
        txt = pytesseract.image_to_string(img, lang="por", timeout=timeout_seg)
        return (txt, True)
    except pytesseract.TesseractNotFoundError:  # type: ignore[attr-defined]
        return ("", False)
    except RuntimeError as e:
        # Timeout do Tesseract — pagina pulada, mas OCR continua disponivel.
        if "timeout" in str(e).lower():
            return ("[pagina ilegivel — timeout OCR]", True)
        return ("", True)
    except Exception:
        return ("", True)


def extrair_texto(
    pdf_path: Path,
    *,
    deve_cancelar: Callable[[], bool] | None = None,
    timeout_por_pagina_seg: int = 30,
) -> str:
    """Extrai texto do PDF. Usa OCR so se texto nativo for insuficiente.

    Retorna texto concatenado com marcadores `=== pagina N ===`. Se nenhuma
    pagina produzir texto, devolve string com `[OCR indisponivel]`.

    `deve_cancelar`: callable que, se retornar True, aborta o loop e devolve
    o parcial + marcador `[OCR abortado pelo usuario na pagina X/N]`.
    `timeout_por_pagina_seg`: limite por pagina no Tesseract (default 30s).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "[PyMuPDF nao instalado — instale 'pymupdf' para habilitar OCR]\n"

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return f"[erro abrindo PDF: {type(e).__name__}: {e}]\n"

    paginas_texto: list[str] = []
    total_paginas = 0
    abortado_em = 0
    try:
        nativos: list[str] = []
        for pagina in doc:
            t = pagina.get_text() or ""
            nativos.append(t)
        total_nativo = sum(len(t.strip()) for t in nativos)
        total_paginas = len(nativos)

        usar_ocr = total_nativo < _MIN_NATIVE_CHARS

        # OCR de 1a linha: ocrmypdf (pre-processamento robusto). So' p/ PDFs
        # escaneados, quando ativo e com o binario disponivel. Processa o
        # documento inteiro de uma vez (nao da p/ cancelar pagina-a-pagina) —
        # por isso checamos o cancelamento ANTES de iniciar. Se sair vazio,
        # indisponivel ou falhar, segue no Tesseract cru pagina-a-pagina abaixo.
        preproc_ativo, preproc_timeout = _preproc_config()
        if usar_ocr and preproc_ativo and (deve_cancelar is None or not deve_cancelar()):
            texto_pp = _ocr_com_ocrmypdf(pdf_path, preproc_timeout)
            if texto_pp:
                return texto_pp  # o finally fecha o doc

        ocr_disponivel = True
        for i, pagina in enumerate(doc, start=1):
            # Cancelamento cooperativo — checado antes de cada pagina.
            if deve_cancelar is not None and deve_cancelar():
                abortado_em = i
                break

            if usar_ocr:
                texto, tesseract_presente = _ocr_pagina(pagina, timeout_por_pagina_seg)
                if not tesseract_presente and i == 1:
                    ocr_disponivel = False
            else:
                texto = nativos[i - 1]
            paginas_texto.append(f"=== pagina {i} ===\n{texto.strip()}\n")

        if usar_ocr and not ocr_disponivel:
            return (
                "[OCR indisponivel: Tesseract nao instalado ou idioma 'por' ausente. "
                "PDF parece escaneado — texto nativo insuficiente.]\n"
            )
    finally:
        doc.close()

    resultado = "\n".join(paginas_texto)
    if abortado_em > 0:
        resultado += f"\n[OCR abortado pelo usuario na pagina {abortado_em}/{total_paginas}]\n"
    return resultado
