"""Ingestao automatizada da caixa SISDPU.

Modulos:
- sisdpu_client: cliente Playwright headless para o SISDPU
- ocr:           extracao de texto de PDFs (fitz + Tesseract fallback)
- parser:        converte sisdpu.txt em metadata.json
- sincronizador: orquestrador (caixa -> PAJs/<PAJ>/sisdpu.txt + pecas + metadata)
"""
