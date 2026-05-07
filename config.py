"""Configuracao central do painel — aponta para o workspace Oficio Geral."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

OFICIO_GERAL = Path(os.getenv(
    "OFICIO_GERAL",
    r"C:\Users\tulio.rmartins\Desktop\Claude\Ofício Geral",
))
PAJS_DIR = OFICIO_GERAL / "PAJs"
PECAS_FEITAS_DIR = OFICIO_GERAL / "Peças Feitas"

# Scripts do workspace usados pelo painel
GERAR_DOCX_SCRIPT = OFICIO_GERAL / "gerar_docx.py"
GERAR_PECA_SCRIPT = OFICIO_GERAL / "gerar_peticao.py"

# Credenciais SISDPU (usadas pela ingestao automatica — carregadas lazy, so
# validadas quando a sincronizacao e' disparada).
SISDPU_USERNAME = os.getenv("SISDPU_USERNAME", "")
SISDPU_PASSWORD = os.getenv("SISDPU_PASSWORD", "")
RATE_LIMIT_SISDPU = int(os.getenv("RATE_LIMIT_SISDPU_SEG", "2"))
TIMEOUT_TOTAL = int(os.getenv("TIMEOUT_TOTAL_SEG", "3600"))
# Limite de anexos baixados do SISDPU por PAJ (protege contra PAJs gigantes).
# Se um PAJ tiver mais que isso, o sync baixa os N mais recentes e avisa no log.
MAX_ANEXOS_POR_PAJ = int(os.getenv("MAX_ANEXOS_POR_PAJ", "30"))

# Timeout por pagina no Tesseract (segundos). PDFs escaneados grandes ou
# corrompidos podem travar o OCR — esse limite garante progresso.
TIMEOUT_OCR_POR_PAGINA_SEG = int(os.getenv("TIMEOUT_OCR_POR_PAGINA_SEG", "30"))

# Pasta onde DOCX/PDF gerados pelo docgen sao salvos.
# Default: <OFICIO_GERAL>/Peças Feitas
DOCGEN_OUT_DIR = Path(os.getenv("DOCGEN_OUT_DIR", str(OFICIO_GERAL / "Peças Feitas")))


def validar_paths() -> list[str]:
    """Verifica que OFICIO_GERAL/PAJS_DIR existem. Retorna lista de avisos
    (vazia se tudo ok). Usada no startup pelo app.py — nao crasha a importacao
    para que ferramentas como ruff/pytest possam importar sem precisar do
    workspace montado."""
    erros: list[str] = []
    if not OFICIO_GERAL.exists():
        erros.append(f"OFICIO_GERAL nao encontrado: {OFICIO_GERAL}")
    if not PAJS_DIR.exists():
        erros.append(f"PAJS_DIR nao encontrado: {PAJS_DIR}")
    return erros
