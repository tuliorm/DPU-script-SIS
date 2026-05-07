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

from pathlib import Path
from collections.abc import Callable

_MIN_NATIVE_CHARS = 200


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
        resultado += (
            f"\n[OCR abortado pelo usuario na pagina {abortado_em}/{total_paginas}]\n"
        )
    return resultado
