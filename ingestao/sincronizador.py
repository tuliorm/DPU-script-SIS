"""Sincronizador SISDPU -> PAJs/<PAJ>/.

Orquestra:
1. Login no SISDPU (Playwright)
2. Lista caixa de entrada
3. Para cada PAJ: abre detalhamento, extrai movs, baixa PDFs linkados e OCRa
4. Monta sisdpu.txt no formato conhecido e metadata.json via ingestao.parser
5. Remove PROMPT_MAX.md (forca regeneracao no proximo acesso)

Versao enxuta do preparar_pajs.py original, SEM blocos DataJud/TNU/STJ — esses
ficariam redundantes para PAJs de 1a instancia DPU-AP.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from collections.abc import Callable

from config import (
    MAX_ANEXOS_POR_PAJ,
    PAJS_DIR,
    RATE_LIMIT_SISDPU,
    TIMEOUT_OCR_POR_PAGINA_SEG,
    TIMEOUT_TOTAL,
)
from ingestao import ocr, parser, sisdpu_client
import contextlib


PAJ_REGEX = re.compile(r"^(\d{4})/(\d{3})-(\d+)$")


def _decompor_paj(paj: str) -> tuple[str, str, str] | None:
    """'2026/044-00311' -> ('2026', '044', '00311'). None se invalido."""
    m = PAJ_REGEX.match(paj.strip())
    if not m:
        return None
    ano = m.group(1)
    unidade = m.group(2)
    numero = m.group(3)
    return ano, unidade, numero


def _normalizar_paj(paj: str) -> str:
    """'2026/044-00311' -> 'PAJ-2026-044-00311'."""
    return "PAJ-" + paj.replace("/", "-")


def _montar_sisdpu_txt(item_caixa: dict, det: dict) -> str:
    """Monta sisdpu.txt no mesmo formato dos arquivos existentes.

    Formato:
        PAJ: <id>
        Assistido: <nome>
        Status: <status>
        Pretensão: <pretensao>
        Data de Abertura: <dd/mm/yyyy>
        Ofício: <oficio>

        MOVIMENTAÇÕES RELEVANTES:
        [data] <descricao>
        ...
    """
    linhas: list[str] = []

    paj_id = item_caixa.get("paj") or det.get("paj", "")
    assistido = det.get("assistido") or item_caixa.get("assistido") or ""
    status = det.get("status_paj") or "ATIVO"
    pretensao = det.get("pretensao", "")
    data_abertura = det.get("data_abertura", "")
    # Oficio atual: SEMPRE da caixa de entrada (parenteses na linha do PAJ). A pagina
    # de detalhamento tem a palavra "Ofício" em varios lugares (cabecalho + textos de
    # movimentacao mencionando oficios expedidos), e o regex de captura pega a primeira
    # ocorrencia — as vezes e' descricao de movimentacao, nao o oficio atual do PAJ.
    # A caixa, em contraste, tem "( A. 03º OFÍCIO GERAL )" limpo e inequivoco.
    # Fallback para det.oficio so se a caixa vier vazia E o det.oficio nao for lixo.
    oficio_caixa = (item_caixa.get("oficio") or "").strip()
    oficio_det = (det.get("oficio") or "").strip()
    if oficio_caixa:
        oficio = oficio_caixa
    elif oficio_det and not oficio_det.lower().startswith("anterior:"):
        oficio = oficio_det
    else:
        oficio = ""
    processo_judicial = det.get("processo_judicial", "")
    juizo = det.get("juizo", "")

    linhas.append(f"PAJ: {paj_id}")
    if assistido:
        linhas.append(f"Assistido: {assistido}")
    if status:
        linhas.append(f"Status: {status}")
    if pretensao:
        linhas.append(f"Pretensão: {pretensao}")
    if data_abertura:
        linhas.append(f"Data de Abertura: {data_abertura}")
    if oficio:
        linhas.append(f"Ofício: {oficio}")
    if processo_judicial:
        sufixo = f" ({juizo})" if juizo else ""
        linhas.append(f"PROCESSO JUDICIAL VINCULADO: {processo_judicial}{sufixo}")

    linhas.append("")
    linhas.append("MOVIMENTAÇÕES RELEVANTES:")

    movs = det.get("movimentacoes", []) or []
    # ordenadas: a tela ja devolve mais recentes no topo
    for m in movs:
        data = (m.get("data") or "").split()[0] if m.get("data") else ""
        tipo = (m.get("movimentacao") or "").strip()
        desc = (m.get("descricao") or "").strip()
        bloco = f"[{data}] {tipo + ': ' if tipo else ''}{desc}".strip()
        linhas.append(bloco)

    return "\n".join(linhas) + "\n"


_NOME_VALIDO_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


def _sanitizar_nome(nome: str) -> str:
    """Normaliza nome de arquivo pra Windows: remove caracteres invalidos,
    corta acentos, limita tamanho."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", nome or "")
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = _NOME_VALIDO_RE.sub("_", sem_acento).strip(" ._")
    if len(limpo) > 120:
        stem, dot, ext = limpo.rpartition(".")
        if dot and len(ext) <= 5:  # noqa: SIM108 - ternario fica menos legivel aqui
            limpo = stem[:115 - len(ext)] + "." + ext
        else:
            limpo = limpo[:120]
    return limpo or "anexo"


def _nome_anexo(idx: int, descricao: str, arquivo: str) -> str:
    """Nome determinista pro anexo baixado: <seq>_<descricao-slug>[_<nome-original>].

    Indice no inicio garante ordenacao estavel; descricao + nome_original ajudam
    a reconhecer visualmente na pasta. A EXTENSAO vai ser apendida depois pelo
    cliente a partir do `download.suggested_filename` (Content-Disposition).

    Quando `arquivo` ja traz extensao conhecida (.pdf/.docx/.jpg/etc), ela e'
    removida aqui pra nao duplicar — o cliente anexa a real no final.
    """
    prefixo = f"{idx:02d}"
    desc_slug = _sanitizar_nome(descricao)[:40].strip("_") if descricao else ""
    # Remove qualquer extensao conhecida do nome_orig pra evitar "arquivo.pdf.pdf"
    nome_orig = _sanitizar_nome(arquivo) if arquivo else ""
    if nome_orig:
        stem, dot, ext = nome_orig.rpartition(".")
        if dot and ext.lower() in {"pdf", "docx", "doc", "jpg", "jpeg", "png", "gif", "xlsx", "xls", "txt", "zip"}:
            nome_orig = stem
    partes = [prefixo]
    if desc_slug:
        partes.append(desc_slug)
    if nome_orig:
        partes.append(nome_orig)
    nome = "_".join(partes)
    return nome


_MAGIC_BYTES = [
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),   # tambem e' a assinatura de docx/xlsx/pptx
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"%!PS", ".ps"),
]


def _detectar_ext_por_magic(path: Path) -> str | None:
    """Le os primeiros bytes e devolve a extensao real (com ponto).

    Para arquivos zip-based (docx/xlsx/pptx/zip), faz distincao adicional
    procurando pela estrutura interna — aqui simplificado: se [Content_Types].xml
    estiver nos primeiros 2KB, consideramos docx.
    """
    try:
        head = path.read_bytes()[:2048]
    except Exception:
        return None
    for sig, ext in _MAGIC_BYTES:
        if head.startswith(sig):
            if ext == ".zip":
                # Heuristica: docx/xlsx tem "word/" ou "xl/" perto do comeco
                if b"word/" in head:
                    return ".docx"
                if b"xl/" in head:
                    return ".xlsx"
                if b"ppt/" in head:
                    return ".pptx"
                return ".zip"
            return ext
    return None


# Extensoes "conhecidas" que nao precisam de rebatizacao por magic bytes
_EXTS_LEGITIMAS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp",
    ".txt", ".json", ".md", ".zip", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov",
    ".html", ".xml", ".csv",
}


def rebatizar_bins_existentes(pasta_pecas: Path, log: Callable[[str], None]) -> int:
    """Rebatiza arquivos com extensao suspeita (.bin, sem ext, ou sufixo bizarro
    como .3100) via magic bytes. Retorna quantos foram renomeados.

    Critério: um arquivo entra em rebatizacao se seu suffix.lower() NAO esta
    em `_EXTS_LEGITIMAS`. Isso cobre:
      - .bin (placeholder legado)
      - sem extensao (ex: "arquivo_sem_ponto")
      - pseudo-extensoes (ex: "nome_1009620-02.2019.4.01.3100" — suffix=".3100")
    """
    renomeados = 0
    if not pasta_pecas.exists():
        return 0
    for f in list(pasta_pecas.iterdir()):
        if not f.is_file():
            continue
        # Pula companions OCR (.txt tem stem=mesmo do PDF, nao deve ser rebatizado)
        if f.suffix.lower() == ".txt":
            continue
        if f.suffix.lower() in _EXTS_LEGITIMAS:
            continue
        ext_real = _detectar_ext_por_magic(f)
        if not ext_real:
            continue
        # Stem preserva o nome sem o suffix atual (ex: "arquivo.3100" -> "arquivo")
        # Mas se o suffix "falso" era parte do nome (tipo numero de processo),
        # melhor preservar o nome completo. Regra: se suffix tem so digitos ou
        # parece numero de processo, mantem; senao, substitui.
        suffix_atual = f.suffix
        if suffix_atual and (suffix_atual[1:].isdigit() or len(suffix_atual) > 5):
            # Trata suffix como parte do stem (ex: "...3100" deve virar "...3100.pdf")
            novo = f.parent / (f.name + ext_real)
        else:
            novo = f.with_suffix(ext_real)
        if novo.exists():
            log(f"    [bin] conflito ao renomear {f.name} -> {novo.name} (já existe)")
            continue
        try:
            f.rename(novo)
            log(f"    [bin] {f.name} -> {novo.name}")
            renomeados += 1
        except Exception as e:
            log(f"    [bin] falha ao renomear {f.name}: {type(e).__name__}: {e}")
    return renomeados


def limpar_ocr_vazios(pasta_pecas: Path, log: Callable[[str], None]) -> int:
    """Remove .txt companions vazios ou com placeholder '[OCR indisponivel'.

    Nao apaga nada que nao seja .txt. Retorna quantos foram removidos.
    """
    removidos = 0
    if not pasta_pecas.exists():
        return 0
    for f in list(pasta_pecas.iterdir()):
        if not f.is_file() or f.suffix.lower() != ".txt":
            continue
        try:
            tam = f.stat().st_size
            if tam == 0:
                f.unlink()
                removidos += 1
                log(f"    [ocr] removido .txt vazio: {f.name}")
                continue
            # Le so os primeiros bytes pra conferir placeholder
            head = f.read_bytes()[:200].decode("utf-8", errors="ignore")
            if "[OCR indisponivel" in head and tam < 100:
                f.unlink()
                removidos += 1
                log(f"    [ocr] removido placeholder: {f.name}")
        except Exception as e:
            log(f"    [ocr] falha ao avaliar {f.name}: {type(e).__name__}: {e}")
    return removidos


def _parse_data_arquivo(s: str):
    """Converte 'DD/MM/YYYY' do campo data do anexo SISDPU em date.

    Retorna `date.min` quando nao consegue interpretar — itens nessa situacao
    ficam no fim do sort decrescente (sao os menos prioritarios).
    """
    import datetime as _dt
    try:
        return _dt.datetime.strptime((s or "").strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError, TypeError):
        return _dt.date.min


async def _baixar_anexos_sisdpu(
    pasta_pecas: Path,
    log: Callable[[str], None],
    limite: int,
    deve_cancelar: Callable[[], bool] | None = None,
    filtro_data_min=None,
) -> dict:
    """Abre o dialogo 'Arquivos' do PAJ atual e baixa os anexos listados.

    PRE-CONDICAO: a page do Playwright ja deve estar no detalhamento do PAJ
    (chamar `movimentacoes_paj(...)` antes).

    Comportamento:
      - Lista TODAS as tabelas de anexos da pagina (multi-categoria).
      - Ordena os itens por DATA decrescente (mais recentes primeiro).
      - Se `filtro_data_min` (date) for informado, baixa SO os com data >= filtro
        — sem aplicar `limite` (modo "completar a partir de uma data").
      - Caso contrario, aplica `limite`: baixa os `limite` mais recentes; se
        houver mais que isso no SISDPU, registra overflow no resultado.

    Retorna dict:
      {
        "total_no_sisdpu": int,         # anexos visiveis na pagina
        "baixados": int,
        "ocr_ok": int,
        "overflow": dict | None         # presente se total > limite (e nao houve filtro)
            {
              "total_no_sisdpu": int,
              "baixados": int,
              "data_min_baixada": "YYYY-MM-DD",   # data mais antiga que entrou
              "data_min_disponivel": "YYYY-MM-DD",# data mais antiga existente
              "limite_aplicado": int,
            }
      }
    """
    pasta_pecas.mkdir(parents=True, exist_ok=True)

    # Legado: renomeia .bin pra extensao real detectada por magic bytes.
    # Isso cobre anexos baixados por versoes anteriores do sincronizador que
    # salvaram tudo como .bin.
    try:
        n_renomeados = rebatizar_bins_existentes(pasta_pecas, log)
        if n_renomeados:
            log(f"    [bin] {n_renomeados} arquivo(s) .bin rebatizados")
    except Exception as e:
        log(f"    [bin] erro ao rebatizar legados: {type(e).__name__}: {e}")

    try:
        resultado = await sisdpu_client.listar_arquivos_paj()
    except Exception as e:
        log(f"  [anexos] erro ao listar: {type(e).__name__}: {e}")
        return (0, 0, 0)

    # Nova assinatura: {"ok": bool, "itens": [...], "diag": {...}}
    lista = resultado.get("itens", []) if isinstance(resultado, dict) else (resultado or [])
    diag = resultado.get("diag", {}) if isinstance(resultado, dict) else {}

    total = len(lista)
    if total == 0:
        log("  [anexos] nenhum anexo na tela 'Arquivos'")
        # Diagnostico detalhado — imprime o que o scraper viu
        if diag:
            candidatos = diag.get("candidatos_botao_arquivos", []) or []
            log(f"    [diag] candidatos ao botão Arquivos: {len(candidatos)}")
            for c in candidatos[:5]:
                log(f"      - <{c.get('tag')}> '{c.get('texto')}' id={c.get('id')} cls={c.get('classes', '')[:60]}")
            log(f"    [diag] clique: {diag.get('clicou', 'N/A')}")
            dlgs = diag.get("dialogs_visiveis", []) or []
            log(f"    [diag] dialogs visíveis: {len(dlgs)}")
            for d in dlgs[:5]:
                log(f"      - id={d.get('id')} título='{d.get('titulo')}' linhas={d.get('linhas_tabela')}")
            log(f"    [diag] estrategia_localizacao: {diag.get('estrategia', 'N/A')}")
            log(f"    [diag] total_linhas_vistas: {diag.get('total_linhas', 0)} filtradas: {diag.get('linhas_filtradas', 0)}")
            snippet = (diag.get("html_snippet") or "").replace("\n", " ")[:400]
            if snippet:
                log(f"    [diag] html[:400]: {snippet}")
        return {"total_no_sisdpu": 0, "baixados": 0, "ocr_ok": 0, "overflow": None}

    log(f"  [anexos] {total} anexos listados no SISDPU")

    # Ordena por data DESC (mais recentes primeiro). Anexos sem data parseavel
    # vao pro fim — sao os menos prioritarios.
    # Estabilidade: mantem ordem do DOM como tiebreaker (sorted e' stable).
    lista_ordenada = sorted(
        lista,
        key=lambda i: _parse_data_arquivo(i.get("descricao", "")),
        reverse=True,
    )

    overflow_info = None

    if filtro_data_min is not None:
        # Modo "completar desde data": baixa todos com data >= filtro_data_min,
        # sem aplicar limite.
        lista_para_baixar = [
            i for i in lista_ordenada
            if _parse_data_arquivo(i.get("descricao", "")) >= filtro_data_min
        ]
        log(
            f"  [anexos] modo 'desde data': filtrando data >= "
            f"{filtro_data_min.isoformat()} → {len(lista_para_baixar)} de {total}"
        )
    elif total > limite:
        # Modo padrao com overflow: baixa os `limite` mais recentes
        lista_para_baixar = lista_ordenada[:limite]
        cortados = lista_ordenada[limite:]
        data_min_baixada = _parse_data_arquivo(
            lista_para_baixar[-1].get("descricao", "")
        )
        data_min_disponivel = _parse_data_arquivo(
            cortados[-1].get("descricao", "")
        )
        overflow_info = {
            "total_no_sisdpu": total,
            "baixados": len(lista_para_baixar),
            "data_min_baixada": data_min_baixada.isoformat(),
            "data_min_disponivel": data_min_disponivel.isoformat(),
            "limite_aplicado": limite,
        }
        log(
            f"  [anexos] {total} > limite {limite} — baixando os {limite} mais "
            f"recentes (data >= {data_min_baixada.isoformat()})"
        )
        log(
            f"  [anexos] {len(cortados)} anexos mais antigos NAO baixados "
            f"(data >= {data_min_disponivel.isoformat()} ate "
            f"{_parse_data_arquivo(cortados[0].get('descricao', '')).isoformat()})"
        )
    else:
        lista_para_baixar = lista_ordenada

    lista = lista_para_baixar

    baixados = 0
    ocr_ok = 0

    import re as _re

    def _stem_sem_prefixo_idx(stem: str) -> str:
        """Remove prefixo numerico inicial 'NN_' (ex: '01_', '23_') do stem.

        Permite comparar arquivos baixados em runs diferentes — se o numero de
        anexos crescer (nova categoria aparecer no SISDPU), o `idx` pode mudar
        sem que o conteudo do arquivo tenha mudado. Comparamos o restante.
        """
        return _re.sub(r"^\d{2,4}_", "", stem)

    interrompido = False

    for idx, item in enumerate(lista, start=1):
        # Checkpoint de cancelamento ENTRE anexos: o usuario nao precisa esperar
        # o PAJ inteiro terminar pra sincronizacao parar. Custo de espera fica
        # limitado ao anexo atual (segundos) + OCR atual (que ja e' cancelavel
        # pagina a pagina dentro do ocr.extrair_texto).
        if deve_cancelar is not None and deve_cancelar():
            log(
                f"  [anexos] CANCELADO pelo usuario apos {baixados} de "
                f"{len(lista)} anexos — PAJ ficara com sincronizacao incompleta"
            )
            interrompido = True
            break

        descricao = (item.get("descricao") or "").strip()
        arquivo = (item.get("arquivo") or "").strip()
        nome = _nome_anexo(idx, descricao, arquivo)
        destino = pasta_pecas / nome

        # Skip se ja foi baixado antes. A extensao so e' conhecida DEPOIS do
        # download (vem do Content-Disposition), entao procuramos por qualquer
        # arquivo na pasta cujo stem (sem prefixo NN_) bata com o nosso.
        # Comparacao tolerante ao prefixo numerico — necessaria porque novos
        # anexos no SISDPU podem renumerar a sequencia. Sem isso, qualquer
        # categoria nova faria todos os anexos antigos serem re-baixados.
        stem_alvo = _stem_sem_prefixo_idx(destino.stem)
        existentes = [
            p for p in pasta_pecas.iterdir()
            if p.is_file()
            and p.suffix.lower() != ".txt"
            and p.stat().st_size > 0
            and _stem_sem_prefixo_idx(p.stem) == stem_alvo
        ]
        if existentes:
            log(f"    [anexo {idx}] já existe — pulando {existentes[0].name}")
            continue

        log(f"    [anexo {idx}] baixando {descricao[:60]} ({arquivo[:40]})")
        try:
            salvo = await sisdpu_client.baixar_anexo_por_indice(
                item.get("row_index", idx - 1), destino
            )
        except Exception as e:
            log(f"    [anexo {idx}] erro: {type(e).__name__}: {e}")
            salvo = None

        if not salvo:
            log(f"    [anexo {idx}] FALHA no download")
            continue
        baixados += 1
        log(f"    [anexo {idx}] salvo: {salvo.name}")

        # OCR apenas em PDFs
        if salvo.suffix.lower() == ".pdf":
            txt_path = salvo.with_suffix(".txt")
            if txt_path.exists() and txt_path.stat().st_size > 0:
                continue
            # Se ja cancelou ate chegar aqui, pula OCR (mas deixa o PDF salvo)
            if deve_cancelar is not None and deve_cancelar():
                log(f"    [anexo {idx}] OCR pulado (cancelado)")
                continue
            try:
                texto = ocr.extrair_texto(
                    salvo,
                    deve_cancelar=deve_cancelar,
                    timeout_por_pagina_seg=TIMEOUT_OCR_POR_PAGINA_SEG,
                )
                txt_path.write_text(texto, encoding="utf-8")
                if "[OCR abortado pelo usuario" in texto:
                    log(f"    [anexo {idx}] OCR abortado pelo usuário")
                elif texto and "[OCR indisponivel" not in texto:
                    ocr_ok += 1
                    log(f"    [anexo {idx}] OCR ok ({len(texto)} chars)")
                else:
                    log(f"    [anexo {idx}] OCR indisponível")
            except Exception as e:
                log(f"    [anexo {idx}] erro OCR: {type(e).__name__}: {e}")

    # Fecha o dialogo pra nao atrapalhar proximo PAJ (quando usado no loop)
    with contextlib.suppress(Exception):
        await sisdpu_client.fechar_dialogo_arquivos()

    # Limpeza automatica: remove .txt OCR vazios/placeholder (evita poluicao).
    # Camada 1 de gestao de disco — rebatizar_bins_existentes ja rodou no inicio.
    try:
        n_ocr_limpos = limpar_ocr_vazios(pasta_pecas, log)
        if n_ocr_limpos:
            log(f"    [ocr] {n_ocr_limpos} .txt vazio(s)/placeholder removido(s)")
    except Exception as e:
        log(f"    [ocr] erro ao limpar vazios: {type(e).__name__}: {e}")

    return {
        "total_no_sisdpu": total,
        "baixados": baixados,
        "ocr_ok": ocr_ok,
        "overflow": overflow_info,
        "interrompido": interrompido,
    }


async def _processar_paj(
    item: dict,
    log: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
) -> tuple[str, bool]:
    """Processa um PAJ. Retorna (paj_norm, novo_ou_atualizado).

    `deve_cancelar` e' propagado ate o OCR pra permitir cancelamento imediato
    durante processamento de um unico PAJ com PDFs grandes.

    `baixar_anexos=False` pula o download/OCR da aba 'Arquivos' — util pra
    sincronizacao rapida (so movs/metadata).
    """
    paj = item.get("paj", "")
    dec = _decompor_paj(paj)
    if not dec:
        log(f"  [skip] PAJ mal-formado: {paj!r}")
        return ("", False)
    ano, unidade, numero = dec
    paj_norm = _normalizar_paj(paj)
    pasta = PAJS_DIR / paj_norm
    ja_existia = pasta.exists()
    pasta.mkdir(parents=True, exist_ok=True)

    # Captura movs antigas ANTES de sobrescrever metadata.json — usado pra
    # detectar intimacoes/citacoes novas e enviar pro calendar-pendentes.
    movs_antigas: list[dict] = []
    assistido_prev = ""
    sync_incompleto_anterior = False
    sync_incompleto_em_anterior = ""
    try:
        meta_path_antiga = pasta / "metadata.json"
        if meta_path_antiga.exists():
            meta_antiga = json.loads(meta_path_antiga.read_text(encoding="utf-8"))
            det_antiga = meta_antiga.get("detalhes_sisdpu", {}) or {}
            movs_antigas = det_antiga.get("movimentacoes", []) or []
            assistido_prev = meta_antiga.get("assistido_caixa", "") or ""
            sync_incompleto_anterior = bool(meta_antiga.get("sync_incompleto", False))
            sync_incompleto_em_anterior = meta_antiga.get("sync_incompleto_em", "")
    except Exception:
        movs_antigas = []

    log(f"  [sisdpu] abrindo detalhamento {paj}")
    try:
        det = await sisdpu_client.movimentacoes_paj(numero, ano, unidade)
    except Exception as e:
        log(f"  [ERRO] movimentacoes_paj {paj}: {type(e).__name__}: {e}")
        return (paj_norm, False)

    if det.get("erro"):
        log(f"  [ERRO] {det['erro']}")
        return (paj_norm, False)

    texto_sisdpu = _montar_sisdpu_txt(item, det)
    (pasta / "sisdpu.txt").write_text(texto_sisdpu, encoding="utf-8")

    try:
        metadata = parser.montar_metadata(paj_norm, texto_sisdpu)
    except Exception as e:
        log(f"  [ERRO] parser metadata: {type(e).__name__}: {e}")
        metadata = {"paj": paj, "paj_norm": paj_norm, "erro_parser": str(e)}

    # Snapshot da estrutura bruta devolvida pelo SISDPU para diagnostico/posteridade
    metadata["sisdpu_raw"] = det

    # Marca explicitamente: este PAJ esta na caixa atual
    metadata["em_caixa_atual"] = True

    # Etiqueta SISDPU (rotulo livre aplicado pelo Defensor na caixa, tipo "Aleg finais")
    etiqueta = (item.get("etiqueta") or "").strip()
    metadata["etiqueta_sisdpu"] = etiqueta

    (pasta / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Detecta prazos novos (intimacao/citacao/notificacao aparecidas nesta sync)
    # e grava no JSONL de pendentes pro calendar.
    try:
        from ingestao import prazos as _prazos_mod
        from services.calendar_service import append_prazo as _append_prazo

        det_novo = metadata.get("detalhes_sisdpu", {}) or {}
        movs_novas = det_novo.get("movimentacoes", []) or []
        assistido_novo = metadata.get("assistido_caixa", "") or assistido_prev
        prazos_detectados = _prazos_mod.detectar_prazos_novos(
            paj_norm=paj_norm,
            movs_antigas=movs_antigas,
            movs_novas=movs_novas,
            assistido=assistido_novo,
        )
        for p in prazos_detectados:
            _append_prazo(p)
        if prazos_detectados:
            log(f"  [prazos] {len(prazos_detectados)} prazo(s) novo(s) detectado(s) — aguardando /sync_calendar")
    except Exception as e:
        log(f"  [prazos] erro ao detectar: {type(e).__name__}: {e}")

    # Anexos SISDPU: usa o botao "Arquivos" do PAJ (lista consolidada de todos
    # os anexos, tipo/descricao/arquivo). Pre-condicao: movimentacoes_paj foi
    # chamado acima e a page do Playwright esta no detalhamento deste PAJ.
    if baixar_anexos:
        # Checkpoint de cancelamento ANTES de comecar o download de anexos.
        # Se o usuario ja sinalizou cancel apos movimentacoes_paj() retornar,
        # marca o PAJ como incompleto e nao tenta baixar nada.
        if deve_cancelar is not None and deve_cancelar():
            log("  [anexos] CANCELADO pelo usuario antes do download — marcando PAJ incompleto")
            metadata["n_anexos_sisdpu"] = metadata.get("n_anexos_sisdpu", 0)
            metadata["sync_incompleto"] = True
            import datetime as _dt
            metadata["sync_incompleto_em"] = _dt.datetime.now().isoformat()
            metadata["sync_incompleto_motivo"] = "cancelado antes do download de anexos"
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            log(f"  [ok] {paj_norm} {'(novo)' if not ja_existia else '(atualizado parcialmente)'}")
            return (paj_norm, True)

        resultado_anexos = await _baixar_anexos_sisdpu(
            pasta / "pecas", log, limite=MAX_ANEXOS_POR_PAJ,
            deve_cancelar=deve_cancelar,
        )
        total_anexos = resultado_anexos["total_no_sisdpu"]
        baixados = resultado_anexos["baixados"]
        ocr_ok = resultado_anexos["ocr_ok"]
        overflow = resultado_anexos["overflow"]
        interrompido = resultado_anexos.get("interrompido", False)
        metadata["n_anexos_sisdpu"] = total_anexos
        if overflow:
            metadata["anexos_extras"] = overflow
        else:
            # Tudo baixado nesta rodada — limpa flag antigo se existia
            metadata.pop("anexos_extras", None)

        # Flag de sincronizacao incompleta: setada quando o usuario cancela
        # NO MEIO do download de anexos. Bloqueia "Elaborar" na UI ate ser
        # ressincronizado com sucesso.
        if interrompido:
            import datetime as _dt
            metadata["sync_incompleto"] = True
            metadata["sync_incompleto_em"] = _dt.datetime.now().isoformat()
            metadata["sync_incompleto_motivo"] = (
                f"cancelado durante download de anexos ({baixados} de "
                f"{total_anexos} baixados)"
            )
        else:
            # Sync completou — limpa qualquer flag antigo
            metadata.pop("sync_incompleto", None)
            metadata.pop("sync_incompleto_em", None)
            metadata.pop("sync_incompleto_motivo", None)

        # Re-grava metadata.json com a contagem atualizada
        (pasta / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log(f"  [anexos] total={total_anexos} baixados={baixados} ocr={ocr_ok}")
        if overflow:
            log(
                f"  [anexos] OVERFLOW: {overflow['total_no_sisdpu'] - overflow['baixados']} "
                f"anexo(s) mais antigos pendentes (use 'Baixar mais antigos' "
                f"a partir de {overflow['data_min_disponivel']})"
            )
        if interrompido:
            log("  [warn] PAJ marcado como SINCRONIZACAO INCOMPLETA — re-sincronize antes de elaborar")
    else:
        # Modo rapido: nao toca no flag sync_incompleto. Se ja era incompleto,
        # continua incompleto (rapido nao baixa anexos novos).
        if sync_incompleto_anterior:
            metadata["sync_incompleto"] = True
            if sync_incompleto_em_anterior:
                metadata["sync_incompleto_em"] = sync_incompleto_em_anterior
            metadata["sync_incompleto_motivo"] = (
                "anexos pendentes (modo rapido nao baixa anexos)"
            )
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        log("  [anexos] modo rápido — download pulado")

    # Forca regeneracao do PROMPT_MAX na proxima visita
    prompt_max = pasta / "PROMPT_MAX.md"
    if prompt_max.exists():
        with contextlib.suppress(Exception):
            prompt_max.unlink()

    log(f"  [ok] {paj_norm} {'(novo)' if not ja_existia else '(atualizado)'}")
    return (paj_norm, True)


def _apagar_anexos_pasta(pasta: Path, log: Callable[[str], None]) -> tuple[int, int]:
    """Apaga binarios em pasta/pecas/, preservando .txt OCR companions.

    Retorna (n_removidos, bytes_liberados). Usado na transicao de PAJ para
    "arquivado" — libera espaco mantendo OCR extraido e registros na raiz.
    """
    pasta_pecas = pasta / "pecas"
    if not pasta_pecas.exists() or not pasta_pecas.is_dir():
        return (0, 0)
    removidos = 0
    bytes_lib = 0
    for f in list(pasta_pecas.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() == ".txt":
            continue  # preserva OCR companion
        try:
            tam = f.stat().st_size
            f.unlink()
            removidos += 1
            bytes_lib += tam
        except Exception as e:
            log(f"    [limpa] falha ao remover {f.name}: {type(e).__name__}: {e}")
    return (removidos, bytes_lib)


def _reconciliar_arquivados(
    pajs_na_caixa: set[str],
    log: Callable[[str], None],
) -> int:
    """Marca em_caixa_atual=False para pastas PAJs/PAJ-* que nao estao na caixa atual.

    - PAJs na caixa ja foram marcados True durante _processar_paj.
    - PAJs no disco mas fora da caixa (antigos, arquivados, sairam do ofício): False.
    - Na TRANSICAO de ativo->arquivado, apaga automaticamente os anexos binarios
      (pecas/*.pdf, etc.), preservando OCR companions e registros na raiz.
    Retorna numero de PAJs arquivados nesta rodada.
    """
    if not PAJS_DIR.exists():
        return 0

    from datetime import datetime

    arquivados = 0
    for pasta in PAJS_DIR.iterdir():
        if not pasta.is_dir() or not pasta.name.startswith("PAJ-"):
            continue
        if pasta.name in pajs_na_caixa:
            continue  # ja marcado True no processamento

        meta_path = pasta / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if meta.get("em_caixa_atual") is False:
            continue  # ja arquivado em sync anterior

        # Transicao ativo -> arquivado
        agora = datetime.now().isoformat(timespec="seconds")
        meta["em_caixa_atual"] = False
        meta["arquivado_em"] = agora

        # Auto-delete dos anexos binarios (preservando OCR e registros)
        n_removidos, bytes_lib = _apagar_anexos_pasta(pasta, log)
        if n_removidos > 0:
            meta["n_anexos_removidos"] = meta.get("n_anexos_removidos", 0) + n_removidos
            hist = meta.get("anexos_removidos_em") or []
            hist.append(agora)
            meta["anexos_removidos_em"] = hist
            log(
                f"  [arquivado] {pasta.name}: {n_removidos} anexo(s) removido(s) "
                f"({bytes_lib / 1024:.0f} KB liberados)"
            )
        else:
            log(f"  [arquivado] {pasta.name}")

        try:
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            arquivados += 1
        except Exception as e:
            log(f"  [warn] falha ao arquivar {pasta.name}: {e}")

    return arquivados


async def rodar(
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
) -> dict:
    """Sincroniza caixa SISDPU com PAJs/<PAJ>/.

    log_callback: funcao que recebe uma linha de log por chamada.
    deve_cancelar: callback opcional consultado entre PAJs; se True,
        o laco abort e a funcao retorna o resumo parcial.
    baixar_anexos: se False, pula download/OCR dos anexos (sync rapido).
    Retorna dict com resumo: {"total_caixa": N, "processados": M, "erros": [...]}.
    """
    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    def _cancelado() -> bool:
        try:
            return bool(deve_cancelar()) if deve_cancelar else False
        except Exception:
            return False

    resumo: dict = {"total_caixa": 0, "processados": 0, "arquivados": 0, "erros": [], "cancelado": False}
    pajs_na_caixa: set[str] = set()

    log("[sync] iniciando sincronização SISDPU")
    log(f"[sync] timeout global: {TIMEOUT_TOTAL}s, rate-limit: {RATE_LIMIT_SISDPU}s/PAJ")
    if not baixar_anexos:
        log("[sync] modo RÁPIDO — anexos NÃO serão baixados (só movs/metadata)")

    try:
        log("[sync] abrindo caixa de entrada...")
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        resumo["erros"].append(f"caixa: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    itens_raw = data.get("itens_tabela", []) or []
    itens: list[dict] = []
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it:
            itens.append(it)
    resumo["total_caixa"] = len(itens)
    log(f"[sync] caixa: {len(itens)} PAJs válidos")

    if not itens:
        log("[sync] nada a sincronizar — encerrando")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    for i, item in enumerate(itens, start=1):
        if _cancelado():
            log(f"[sync] CANCELADO pelo usuário após {i-1}/{len(itens)} PAJs")
            resumo["cancelado"] = True
            break
        log(f"[{i}/{len(itens)}] {item.get('paj','?')}")
        try:
            paj_norm, ok = await _processar_paj(
                item, log, deve_cancelar=_cancelado, baixar_anexos=baixar_anexos,
            )
            if paj_norm:
                pajs_na_caixa.add(paj_norm)
            if ok:
                resumo["processados"] += 1
        except Exception as e:
            log(f"  [EXC] {type(e).__name__}: {e}")
            resumo["erros"].append(f"{item.get('paj','?')}: {e}")
        await asyncio.sleep(RATE_LIMIT_SISDPU)

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    # Se foi cancelado, pula reconciliacao (dados incompletos — nao queremos
    # arquivar PAJs que ainda nao foram visitados)
    if resumo["cancelado"]:
        log("[sync] reconciliação SALTADA (sync cancelada — dados parciais)")
        return resumo

    # Reconciliacao: PAJs no disco fora da caixa sao arquivados (em_caixa_atual=False)
    log("[sync] reconciliando PAJs arquivados (fora da caixa)...")
    arquivados = _reconciliar_arquivados(pajs_na_caixa, log)
    resumo["arquivados"] = arquivados
    if arquivados:
        log(f"[sync] {arquivados} PAJ(s) marcados como arquivados")
    else:
        log("[sync] nenhum PAJ novo a arquivar")

    # Resumo de PAJs com anexos pendentes (overflow do limite de 30) e
    # PAJs com sincronizacao incompleta (cancelados mid-PAJ).
    pajs_overflow: list[dict] = []
    pajs_incompletos: list[dict] = []
    for paj_norm in pajs_na_caixa:
        meta_path = PAJS_DIR / paj_norm / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ov = meta.get("anexos_extras")
        if ov and isinstance(ov, dict):
            pajs_overflow.append({
                "paj_norm": paj_norm,
                "total_no_sisdpu": ov.get("total_no_sisdpu"),
                "baixados": ov.get("baixados"),
                "data_min_baixada": ov.get("data_min_baixada"),
                "data_min_disponivel": ov.get("data_min_disponivel"),
            })
        if meta.get("sync_incompleto"):
            pajs_incompletos.append({
                "paj_norm": paj_norm,
                "motivo": meta.get("sync_incompleto_motivo", ""),
                "em": meta.get("sync_incompleto_em", ""),
            })

    if pajs_overflow:
        log("=" * 60)
        log(f"[sync] {len(pajs_overflow)} PAJ(s) com mais de {MAX_ANEXOS_POR_PAJ} anexos:")
        for p in pajs_overflow:
            log(
                f"  - {p['paj_norm']}: {p['baixados']}/{p['total_no_sisdpu']} baixados "
                f"(mais antigo baixado: {p['data_min_baixada']}; "
                f"mais antigo disponivel: {p['data_min_disponivel']})"
            )
        log("[sync] DICA: abra o PAJ → aba Anexos → 'Baixar mais antigos' "
            "para informar a data de corte e completar.")
    resumo["pajs_overflow"] = pajs_overflow

    if pajs_incompletos:
        log("=" * 60)
        log(f"[sync] {len(pajs_incompletos)} PAJ(s) com SINCRONIZACAO INCOMPLETA "
            "(elaboracao bloqueada ate ressincronizar):")
        for p in pajs_incompletos:
            log(f"  - {p['paj_norm']}: {p['motivo']}")
        log("[sync] DICA: abra cada PAJ e clique em 'Re-sincronizar agora' "
            "no banner vermelho do topo.")
    resumo["pajs_incompletos"] = pajs_incompletos

    log("=" * 60)
    log(f"[sync] FIM: {resumo['processados']}/{resumo['total_caixa']} processados, "
        f"{resumo['arquivados']} arquivados, {len(resumo['erros'])} erros")
    return resumo


async def rodar_anexos_desde_data(
    paj_identificador: str,
    data_inicio,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
) -> dict:
    """Baixa anexos de UM PAJ a partir de uma data de corte (inclusive).

    `data_inicio` e' um `datetime.date`. Baixa todos os anexos com
    `data >= data_inicio`, sem aplicar o limite padrao de 30. Os 17 que ja
    estiverem no disco sao detectados pelo dedup e pulados.

    Reuso do fluxo: login → caixa → abrir detalhamento do PAJ → baixar anexos.
    Nao mexe em movs/metadata textuais; so completa o conjunto de anexos.

    Retorna dict com {paj_norm, total_no_sisdpu, baixados, ocr_ok, novos,
    overflow_residual}.
    """
    import datetime as _dt
    if not isinstance(data_inicio, _dt.date):
        raise TypeError("data_inicio deve ser datetime.date")

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    log(f"[anexos-desde] alvo: {paj_identificador} (data_inicio={data_inicio.isoformat()})")

    # 1) Localiza o PAJ na caixa atual (mesmo modus operandi de rodar_paj_unico)
    log("[anexos-desde] abrindo caixa de entrada...")
    try:
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": f"caixa: {e}"}

    itens_raw = data.get("itens_tabela", []) or []
    alvo = _normalizar_paj(paj_identificador)
    item_alvo = None
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it and _normalizar_paj(it.get("paj", "")) == alvo:
            item_alvo = it
            break

    if not item_alvo:
        log(f"[anexos-desde] PAJ {alvo} NÃO encontrado na caixa atual")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": "paj_nao_na_caixa"}

    # 2) Abre detalhamento (necessario antes de listar arquivos)
    paj_txt = item_alvo.get("paj", "")
    dec = _decompor_paj(paj_txt)
    if not dec:
        log(f"[anexos-desde] PAJ mal-formado: {paj_txt!r}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": "paj_mal_formado"}
    ano, unidade, numero = dec

    log(f"  [sisdpu] abrindo detalhamento {paj_txt}")
    try:
        det = await sisdpu_client.movimentacoes_paj(numero, ano, unidade)
    except Exception as e:
        log(f"  [ERRO] movimentacoes_paj: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": f"detalhamento: {e}"}

    if det.get("erro"):
        log(f"  [ERRO] {det['erro']}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": det["erro"]}

    # 3) Baixa anexos com filtro por data (ignora limite)
    pasta = PAJS_DIR / alvo
    pasta.mkdir(parents=True, exist_ok=True)
    resultado_anexos = await _baixar_anexos_sisdpu(
        pasta / "pecas",
        log,
        limite=MAX_ANEXOS_POR_PAJ,  # ignorado quando filtro_data_min e' usado
        deve_cancelar=deve_cancelar,
        filtro_data_min=data_inicio,
    )

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    # 4) Atualiza metadata: pode reduzir/eliminar overflow
    meta_path = pasta / "metadata.json"
    overflow_residual = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ov_anterior = meta.get("anexos_extras", {}) or {}
            data_min_disponivel_str = ov_anterior.get("data_min_disponivel", "")
            try:
                data_min_disponivel = _dt.date.fromisoformat(data_min_disponivel_str)
            except (ValueError, TypeError):
                data_min_disponivel = None
            # Se o usuario escolheu uma data <= data_min_disponivel, agora todos
            # os anexos foram baixados — limpa o flag.
            if data_min_disponivel and data_inicio <= data_min_disponivel:
                meta.pop("anexos_extras", None)
            else:
                # Ainda restam anexos mais antigos que data_inicio. Atualiza
                # data_min_baixada para a nova data de corte do usuario.
                if "anexos_extras" in meta:
                    meta["anexos_extras"]["data_min_baixada"] = data_inicio.isoformat()
                    meta["anexos_extras"]["baixados"] = (
                        ov_anterior.get("baixados", 0) + resultado_anexos["baixados"]
                    )
                    overflow_residual = meta["anexos_extras"]
            meta["n_anexos_sisdpu"] = resultado_anexos["total_no_sisdpu"]
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            log(f"  [warn] falha ao atualizar metadata: {type(e).__name__}: {e}")

    log("=" * 60)
    log(
        f"[anexos-desde] FIM: {resultado_anexos['baixados']} anexo(s) baixado(s) "
        f"(de {resultado_anexos['total_no_sisdpu']} no SISDPU)"
    )
    return {
        "paj_norm": alvo,
        "total_no_sisdpu": resultado_anexos["total_no_sisdpu"],
        "baixados": resultado_anexos["baixados"],
        "ocr_ok": resultado_anexos["ocr_ok"],
        "overflow_residual": overflow_residual,
    }


async def rodar_paj_unico(
    paj_identificador: str,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
) -> dict:
    """Sincroniza UM unico PAJ (util pra teste/re-sync pontual sem rodar a caixa inteira).

    `paj_identificador` aceita tanto '2021/044-00635' quanto 'PAJ-2021-044-00635'.

    Exige que o PAJ esteja na caixa de entrada atual (para localizar o link
    na tela da caixa e clicar nele).
    """
    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    resumo: dict = {"paj": "", "processado": False, "anexos": 0, "baixados": 0, "erros": []}

    # Normaliza identificador para o formato "2021/044-00635"
    alvo = paj_identificador.strip()
    if alvo.startswith("PAJ-"):
        resto = alvo[4:]  # '2021-044-00635'
        partes = resto.split("-", 1)  # ['2021', '044-00635']
        if len(partes) == 2:
            alvo = f"{partes[0]}/{partes[1]}"

    resumo["paj"] = alvo

    log(f"[sync-paj] alvo: {alvo}")

    try:
        log("[sync-paj] abrindo caixa de entrada...")
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        resumo["erros"].append(f"caixa: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    itens_raw = data.get("itens_tabela", []) or []
    item_alvo = None
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it and it.get("paj") == alvo:
            item_alvo = it
            break

    if not item_alvo:
        log(f"[sync-paj] PAJ {alvo} NÃO encontrado na caixa atual")
        resumo["erros"].append("PAJ fora da caixa")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    if not baixar_anexos:
        log("[sync-paj] modo RÁPIDO — anexos NÃO serão baixados")

    try:
        paj_norm, ok = await _processar_paj(
            item_alvo, log, deve_cancelar=deve_cancelar, baixar_anexos=baixar_anexos,
        )
        if ok:
            resumo["processado"] = True
            # Recupera contadores do metadata recem-gravado
            pasta = PAJS_DIR / paj_norm
            try:
                meta = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
                resumo["anexos"] = meta.get("n_anexos_sisdpu", 0)
            except Exception:
                pass
            # Conta arquivos em pecas/
            pasta_pecas = pasta / "pecas"
            if pasta_pecas.exists():
                resumo["baixados"] = sum(
                    1 for f in pasta_pecas.iterdir()
                    if f.is_file() and f.suffix.lower() != ".txt"
                )
    except Exception as e:
        log(f"  [EXC] {type(e).__name__}: {e}")
        resumo["erros"].append(f"{alvo}: {e}")

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    log("=" * 60)
    log(f"[sync-paj] FIM: processado={resumo['processado']}, "
        f"anexos_listados={resumo['anexos']}, "
        f"arquivos_baixados={resumo['baixados']}, "
        f"erros={len(resumo['erros'])}")
    return resumo
