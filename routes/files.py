"""Rota para servir arquivos de PAJs (PDFs, TXTs, JSONs)."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse

from services.paj_service import PajNorm, ler_arquivo

router = APIRouter()


@router.get("/files/{paj_norm}/{path:path}")
async def serve_file(paj_norm: PajNorm, path: str):
    arquivo, content_type = ler_arquivo(paj_norm, path)
    if not arquivo:
        return PlainTextResponse("Arquivo nao encontrado", status_code=404)

    if "pdf" in content_type:
        return FileResponse(arquivo, media_type=content_type, filename=arquivo.name)

    # TXT, JSON, MD — retorna como texto
    if "text" in content_type or "json" in content_type:
        conteudo = arquivo.read_text(encoding="utf-8", errors="replace")
        return PlainTextResponse(conteudo, media_type=content_type)

    return FileResponse(arquivo, media_type=content_type)
