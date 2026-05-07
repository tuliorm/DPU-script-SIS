# dpuscript-ui

Interface web para o pipeline **dpuscript** — sistema de monitoramento e elaboração de peças para Defensores Públicos Federais atuantes na TNU e no STJ.

> Desenvolvido para uso interno na DPU. Projeto pessoal, sem vínculo institucional oficial.

---

## O que é

O **dpuscript** é um pipeline que:
1. Autentica no e-Proc TNU e baixa automaticamente as peças dos processos da caixa do Defensor
2. Classifica cada processo (decisão monocrática, acórdão, vista ao MP, vitória, etc.)
3. Extrai texto dos PDFs — com OCR automático para documentos escaneados
4. Gera um prompt otimizado para elaboração de peças via Claude AI

O **dpuscript-ui** é o painel web que dá visibilidade a esse pipeline: mostra todos os PAJs (Processos de Assistência Jurídica), seu estado de processamento, peças baixadas, classificação automática e facilita o acesso a cada processo para elaboração das peças.

---

## Pré-requisitos

- **Python 3.11+**
- **Workspace Ofício Geral** — a pasta onde ficam `PAJs/`, `Peças Feitas/`, `gerar_docx.py`, `gerar_peticao.py` e o `CLAUDE.md` do Defensor
- **Claude Code** com modelo Max (claude-opus ou claude-sonnet) — a elaboração das peças é feita pelo Claude via CLI, não por API paga
- **Playwright + Chromium** — para sincronizar a caixa SISDPU automaticamente
- **Tesseract OCR (idioma `por`)** — para extrair texto de PDFs escaneados das peças baixadas

---

## Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/jppicanco/dpuscript-ui.git
cd dpuscript-ui

# 2. Crie o ambiente virtual
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/Mac

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Instale o Chromium do Playwright
python -m playwright install chromium

# 5. Instale o Tesseract OCR (Windows)
# Baixe em: https://github.com/UB-Mannheim/tesseract/wiki
# Marque o idioma "Portuguese" durante a instalacao
# Caminho padrao: C:\Program Files\Tesseract-OCR\
# Verifique:
tesseract --version

# 6. Configure o .env
copy .env.example .env
# Edite .env e preencha:
#   OFICIO_GERAL    = caminho para o workspace do Defensor
#   SISDPU_USERNAME = seu usuario do SISDPU
#   SISDPU_PASSWORD = sua senha do SISDPU
```

---

## Configuração

Edite o arquivo `.env`:

```env
OFICIO_GERAL=C:\Users\tulio.rmartins\Desktop\Claude\Ofício Geral
SISDPU_USERNAME=seu.usuario
SISDPU_PASSWORD=sua.senha
```

`OFICIO_GERAL` aponta para a pasta do Defensor — é onde ficam `PAJs/`, `Peças Feitas/`, os scripts `gerar_docx.py` / `gerar_peticao.py` e o `CLAUDE.md`. As credenciais SISDPU são usadas pela funcionalidade "Sincronizar caixa" do dashboard.

---

## Uso

```bash
# Iniciar o servidor (porta 8001)
.venv\Scripts\python.exe app.py

# Acessar no navegador
# http://localhost:8001
```

O painel mostra todos os PAJs detectados em `OFICIO_GERAL/PAJs/PAJ-*/`. Para cada PAJ você pode:
- Ver os detalhes extraídos do SISDPU (cabeçalho + movimentações)
- Ver peças baixadas em PDF (com texto extraído via OCR quando necessário)
- Acessar `.txt` extraídos e o `PROMPT_MAX.md` gerado dinamicamente
- Acionar a elaboração de peças pelo Claude via chat
- Gerar DOCX/PDF a partir de um `.txt` usando `gerar_docx.py` / `gerar_peticao.py`

**Sincronizar caixa:** o botão "Sincronizar caixa" no dashboard autentica no SISDPU via Playwright, lê a caixa de entrada, extrai movimentações, baixa peças em PDF e roda OCR. O progresso aparece em tempo real num modal com log de eventos (SSE).

---

## Estrutura

```
dpuscript-ui/
├── app.py              # FastAPI + Uvicorn
├── config.py           # Configurações (lê .env)
├── ingestao/           # Pipeline de sincronização da caixa SISDPU
│   ├── sisdpu_client.py  # Playwright headless
│   ├── ocr.py            # Fitz + Tesseract
│   ├── parser.py         # sisdpu.txt → metadata.json
│   └── sincronizador.py  # orquestrador
├── routes/             # Endpoints (dashboard, PAJs, chat, docgen, sync)
├── services/           # Lógica de negócio
├── templates/          # HTML (Jinja2 + DaisyUI + Alpine.js)
├── static/             # CSS e JS estáticos
└── requirements.txt
```

---

## Workspace Ofício Geral

Este painel lê os dados da pasta apontada por `OFICIO_GERAL`, que contém:
- `PAJs/PAJ-YYYY-044-XXXXX/` — uma subpasta por PAJ com `sisdpu.txt`, `metadata.json`, `pecas/`, peças geradas
- `Peças Feitas/` — peças anteriores do Defensor (usadas como contexto no PROMPT_MAX)
- `CLAUDE.md` — instruções do Defensor para o Claude Code
- `gerar_docx.py` e `gerar_peticao.py` — scripts que o botão "Gerar DOCX/PDF" invoca

---

## Tecnologias

- **Backend:** FastAPI + Uvicorn
- **Frontend:** DaisyUI (Tailwind CSS) + Alpine.js + HTMX
- **Ingestão:** Playwright (SISDPU), PyMuPDF, Tesseract OCR
- **IA:** Claude Code (claude-opus/sonnet) via CLI — sem API paga

---

## Aviso

Este projeto foi desenvolvido para uso pessoal por um Defensor Público Federal. Ele **não armazena dados de assistidos** — os arquivos de processos ficam apenas na máquina local do Defensor e são ignorados pelo Git. Consulte o `.gitignore` para confirmar o que é e não é versionado.

Contribuições são bem-vindas. Abra uma issue ou PR.

---

## Licença

[MIT](LICENSE) — uso livre, sem garantias.
