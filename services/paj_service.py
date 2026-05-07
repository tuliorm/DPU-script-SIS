"""Servico de leitura de PAJs do workspace Oficio Geral.

Cada PAJ fica em `PAJS_DIR/<PAJ-YYYY-044-XXXXX>/`, com pelo menos:
- `metadata.json` — gerado pelo script de migracao
- `sisdpu.txt`   — texto bruto do SISDPU
- `PROMPT_MAX.md` — gerado on-demand pelo prompt_builder
- arquivos gerados pelo Claude (despacho.txt, peticao.txt, .docx, .pdf, etc.)
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Annotated

from fastapi import Path as FastApiPath

from config import PAJS_DIR, PECAS_FEITAS_DIR


# Type alias para parametros de rota — FastAPI valida o pattern e retorna 422
# com mensagem clara automaticamente. Use em toda rota que recebe paj_norm:
#     async def handler(paj_norm: PajNorm): ...
PAJ_NORM_REGEX = r"^PAJ-\d{4}-\d{3}-\d{5}$"
PajNorm = Annotated[
    str,
    FastApiPath(
        pattern=PAJ_NORM_REGEX,
        description="Identificador do PAJ no formato PAJ-YYYY-044-XXXXX",
    ),
]


IGNORAR = {
    "metadata.json",
    "PROMPT_MAX.md",
    "elaboracao.json",
    "sisdpu.txt",
    "NOTAS.md",
}

PREFIXOS_DESPACHO = ("despacho",)

PREFIXOS_PECA_JUDICIAL = (
    "peticao",
    "petição",
    "peca",
    "peça",
    "recurso",
    "agravo",
    "apelacao",
    "apelação",
    "embargos",
    "contestacao",
    "contestação",
    "manifestacao",
    "manifestação",
    "memoriais",
    "relatorio",
    "relatório",
    "oficio",
    "ofício",
)


def _ler_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalizar(texto: str) -> str:
    """Remove acentos e lowercase — pra comparar nomes."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _e_pasta_paj(p: Path) -> bool:
    return p.is_dir() and p.name.startswith("PAJ-")


def _garantir_prompt_max(pasta: Path) -> None:
    """Gera PROMPT_MAX.md se faltar ou se estiver mais antigo que metadata.json."""
    try:
        from services.prompt_builder import gerar_prompt_max
    except ImportError:
        return

    prompt_path = pasta / "PROMPT_MAX.md"
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        return
    if prompt_path.exists() and prompt_path.stat().st_mtime >= meta_path.stat().st_mtime:
        return
    with contextlib.suppress(Exception):
        gerar_prompt_max(pasta.name)


MAX_ARQUIVADOS_VISIVEIS = 100


# Cache em memoria de listar_pajs() — dashboard chama isso a cada navegacao,
# entao vale evitar a varredura de ~309 stat() + 103 read JSON quando nada
# mudou. Invalida por TTL curto (5s) + assinatura do diretorio (mtime + count
# de entradas) para detectar criacao/remocao de pastas. Sync_service tambem
# chama invalidar_cache_listagem() explicitamente apos cada sync.
_LIST_CACHE_TTL_SEG = 5.0
_list_cache: dict = {"ts": 0.0, "key": None, "result": None}


def _dir_signature() -> tuple[float, int]:
    """Assinatura barata do estado de PAJS_DIR: (mtime, n_entries).

    mtime do diretorio muda quando arquivos sao adicionados/removidos. A
    contagem ajuda a pegar reorgs sem mudanca de mtime (alguns FS sao
    inconsistentes). Edicoes internas em metadata.json NAO invalidam — pra
    isso o TTL curto basta, e o sync chama invalidar_cache_listagem()."""
    if not PAJS_DIR.exists():
        return (0.0, 0)
    st = PAJS_DIR.stat()
    n = sum(1 for _ in PAJS_DIR.iterdir())
    return (st.st_mtime, n)


def invalidar_cache_listagem() -> None:
    """Zera o cache de listar_pajs(). Chamada pelo sync_service ao terminar."""
    _list_cache["result"] = None
    _list_cache["ts"] = 0.0
    _list_cache["key"] = None


def listar_pajs(incluir_arquivados: bool = False) -> list[dict]:
    """Retorna lista resumida de PAJs pro dashboard.

    Por padrao retorna apenas PAJs em_caixa_atual (True ou campo ausente —
    legado sem o flag ainda conta como ativo ate a proxima sync). Quando
    incluir_arquivados=True, devolve tambem os que sairam da caixa, limitado
    aos MAX_ARQUIVADOS_VISIVEIS mais recentes (ordenados por arquivado_em
    desc). PAJs mais antigos permanecem no disco (com registros/OCR) mas
    nao aparecem mais na lista.

    Resultado cacheado em memoria por _LIST_CACHE_TTL_SEG segundos (e ate o
    PAJS_DIR mudar de assinatura — vide _dir_signature)."""
    chave = ("incluir" if incluir_arquivados else "ativos", *_dir_signature())
    agora = time.monotonic()
    if (
        _list_cache["result"] is not None
        and _list_cache["key"] == chave
        and agora - _list_cache["ts"] < _LIST_CACHE_TTL_SEG
    ):
        return _list_cache["result"]

    ativos: list[dict] = []
    arquivados: list[dict] = []

    if not PAJS_DIR.exists():
        return []

    for pasta in sorted(PAJS_DIR.iterdir()):
        if not _e_pasta_paj(pasta):
            continue

        metadata = _ler_json(pasta / "metadata.json")
        if not metadata:
            continue

        # Filtro de arquivados: so exclui se o flag esta EXPLICITAMENTE False
        em_caixa = metadata.get("em_caixa_atual")
        esta_arquivado = em_caixa is False
        if esta_arquivado and not incluir_arquivados:
            continue

        paj_norm = pasta.name

        det = metadata.get("detalhes_sisdpu", {}) or {}
        movs = det.get("movimentacoes", []) or []
        movs_sorted = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0))
        ultima_mov = movs_sorted[0] if movs_sorted else {}
        max_seq_mov = max((int(m.get("seq", 0) or 0) for m in movs), default=0)

        # Contagem de pecas GERADAS pelo defensor/Claude (na raiz do PAJ)
        n_pecas_geradas = 0
        for f in pasta.iterdir():
            if not f.is_file() or f.name in IGNORAR:
                continue
            n_pecas_geradas += 1

        # Contagem de ANEXOS SISDPU (baixados pelo sync em pecas/, excluindo
        # os .txt de OCR — contamos so o arquivo-fonte, nao o OCR companion).
        pasta_pecas = pasta / "pecas"
        n_anexos_sisdpu = 0
        if pasta_pecas.exists() and pasta_pecas.is_dir():
            for f in pasta_pecas.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() == ".txt":
                    # e' o OCR companion — pula, ja contamos o PDF/original
                    continue
                n_anexos_sisdpu += 1

        item = {
            "paj": metadata.get("paj", paj_norm),
            "paj_norm": paj_norm,
            "assistido": metadata.get("assistido_caixa", ""),
            "oficio": metadata.get("oficio_caixa", ""),
            "etiqueta": metadata.get("etiqueta_sisdpu", ""),
            "foro": metadata.get("foro_detectado", "?"),
            "classificacao": metadata.get("classificacao", "?"),
            "data_caixa": metadata.get("data_mov_caixa", ""),
            "desc_caixa": metadata.get("desc_mov_caixa", ""),
            "processo_judicial": metadata.get("processo_judicial", ""),
            "n_pecas": n_pecas_geradas,
            "n_anexos_sisdpu": n_anexos_sisdpu,
            "n_decisoes": 0,
            "ultima_preparacao": metadata.get("migrado_em", ""),
            "prazos_abertos": metadata.get("prazos_abertos", []),
            "ultima_mov_desc": (ultima_mov.get("descricao") or "")[:120],
            "ultima_mov_data": ultima_mov.get("data", ""),
            "status_sisdpu": (det.get("status_paj") or "").strip(),
            "em_caixa_atual": not esta_arquivado,
            "arquivado_em": metadata.get("arquivado_em", "") if esta_arquivado else "",
            "sync_incompleto": bool(metadata.get("sync_incompleto", False)),
            "sync_incompleto_motivo": metadata.get("sync_incompleto_motivo", ""),
            # Estado atual usado pelo frontend pra detectar novidades desde a
            # ultima visita (snapshot guardado em localStorage). Vide
            # static/js/app.js -> calcularNovidades().
            "max_seq_mov": max_seq_mov,
        }
        if esta_arquivado:
            arquivados.append(item)
        else:
            ativos.append(item)

    # Cap: mostra so os MAX_ARQUIVADOS_VISIVEIS mais recentes (por arquivado_em desc).
    # PAJs legados sem arquivado_em caem no fim (sort por string vazia ordena antes).
    arquivados.sort(key=lambda p: p.get("arquivado_em") or "", reverse=True)
    arquivados = arquivados[:MAX_ARQUIVADOS_VISIVEIS]

    resultado = ativos + arquivados
    _list_cache["ts"] = agora
    _list_cache["key"] = chave
    _list_cache["result"] = resultado
    return resultado


def ler_paj(paj_norm: str) -> dict | None:
    """Retorna dados completos de um PAJ."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not _e_pasta_paj(pasta):
        return None

    metadata = _ler_json(pasta / "metadata.json")
    if not metadata:
        return None

    _garantir_prompt_max(pasta)

    prompt_max_path = pasta / "PROMPT_MAX.md"
    prompt_max = (
        prompt_max_path.read_text(encoding="utf-8") if prompt_max_path.exists() else ""
    )

    # Categorizar arquivos na pasta do PAJ:
    #   despachos       — nomes comecando com "despacho"
    #   pecas_judiciais — demais arquivos gerados (peticao*, recurso*, .docx, .pdf, etc.)
    despachos: list[dict] = []
    pecas_judiciais: list[dict] = []

    for f in sorted(pasta.iterdir()):
        if not f.is_file() or f.name in IGNORAR:
            continue

        item = {
            "nome": f.name,
            "caminho": f.name,
            "tipo": f.suffix.lstrip(".").lower(),
            "tamanho": f.stat().st_size,
            "modificado": f.stat().st_mtime,
        }

        nome_norm = _normalizar(f.name)
        if nome_norm.startswith(PREFIXOS_DESPACHO):
            despachos.append(item)
        else:
            pecas_judiciais.append(item)

    det = metadata.get("detalhes_sisdpu", {}) or {}
    movs = det.get("movimentacoes", []) or []
    movs_sorted = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0))
    max_seq_mov = max((int(m.get("seq", 0) or 0) for m in movs), default=0)

    # Anexos SISDPU: arquivos em `pecas/` baixados pelo sincronizador.
    # O OCR gera um `.txt` companion com o mesmo stem — associamos pro frontend
    # poder exibir "abrir PDF" + "ler OCR" lado a lado.
    anexos_sisdpu: list[dict] = []
    pasta_pecas = pasta / "pecas"
    if pasta_pecas.exists() and pasta_pecas.is_dir():
        # Mapeia companheiros OCR por stem
        txts_ocr: dict[str, Path] = {}
        for f in pasta_pecas.iterdir():
            if f.is_file() and f.suffix.lower() == ".txt":
                txts_ocr[f.stem] = f

        for f in sorted(pasta_pecas.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower() == ".txt":
                continue  # o .txt e' listado junto com seu arquivo-fonte
            ocr_companion = txts_ocr.get(f.stem)
            anexos_sisdpu.append({
                "nome": f.name,
                # caminho relativo a pasta do PAJ (usado por /files/<paj>/<path>)
                "caminho": f"pecas/{f.name}",
                "tipo": f.suffix.lstrip(".").lower(),
                "tamanho": f.stat().st_size,
                "modificado": f.stat().st_mtime,
                "tem_ocr": ocr_companion is not None and ocr_companion.stat().st_size > 0,
                "caminho_ocr": f"pecas/{ocr_companion.name}" if ocr_companion else None,
            })

    return {
        "metadata": metadata,
        "prompt_max": prompt_max,
        "pecas": [],
        "pecas_por_categoria": [],
        "decisoes": [],
        "despachos": despachos,
        "pecas_judiciais": pecas_judiciais,
        "pecas_geradas": despachos + pecas_judiciais,
        "anexos_sisdpu": anexos_sisdpu,
        "movimentacoes": movs_sorted,
        "max_seq_mov": max_seq_mov,
        "n_anexos_sisdpu": len(anexos_sisdpu),
        "prazos_abertos": metadata.get("prazos_abertos", []),
        "pasta": str(pasta),
    }


def ler_arquivo(paj_norm: str, caminho_relativo: str) -> tuple[Path | None, str]:
    """Retorna (path_absoluto, content_type) de um arquivo do PAJ."""
    pasta = PAJS_DIR / paj_norm
    arquivo = pasta / caminho_relativo

    try:
        arquivo.resolve().relative_to(pasta.resolve())
    except ValueError:
        return None, ""

    if not arquivo.exists() or not arquivo.is_file():
        return None, ""

    ext = arquivo.suffix.lower()
    content_types = {
        ".pdf": "application/pdf",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return arquivo, content_types.get(ext, "application/octet-stream")


def limpar_anexos_paj(
    paj_norm: str,
    dry_run: bool = True,
    forcar: bool = False,
) -> dict:
    """Prepara (dry_run=True) ou executa (dry_run=False) a limpeza dos anexos
    SISDPU de um PAJ.

    Politica: so apaga o binario original (PDF/docx/imagem). MANTEM:
      - .txt OCR companion (texto extraido fica permanente)
      - metadata.json, sisdpu.txt, PROMPT_MAX.md (dados estruturais)
      - qualquer arquivo fora de pecas/ (pecas geradas pelo Claude, etc)

    Safeguards (consultados via dry_run; execute requer `forcar=True` se
    algum seguranca falhar):
      1. Todos os anexos PDF/docx precisam ter .txt OCR nao-vazio ao lado
      2. PAJ precisa estar ARQUIVADO (em_caixa_atual=False)
         — PAJ ativo requer forcar=True (o usuario clicou "tenho certeza")

    Retorno:
        {
          "ok": bool,               # operacao bem-sucedida
          "pode_limpar": bool,      # safeguards passaram automaticamente
          "motivos_bloqueio": [...],# motivos pelos quais nao pode limpar sem forcar
          "em_caixa_atual": bool,
          "arquivos_a_remover": [{nome, tamanho, tem_ocr}],
          "arquivos_preservados": [{nome, tamanho, motivo}],
          "removidos": N,           # 0 em dry_run
          "bytes_liberados": N,
        }
    """
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not _e_pasta_paj(pasta):
        return {
            "ok": False,
            "erro": "PAJ nao encontrado",
            "arquivos_a_remover": [],
            "arquivos_preservados": [],
        }

    metadata = _ler_json(pasta / "metadata.json") or {}
    em_caixa = metadata.get("em_caixa_atual", True)  # default: ativo

    pasta_pecas = pasta / "pecas"
    arquivos_a_remover: list[dict] = []
    arquivos_preservados: list[dict] = []

    if pasta_pecas.exists() and pasta_pecas.is_dir():
        # Mapeia OCRs existentes por stem
        txts_ocr: dict[str, Path] = {}
        for f in pasta_pecas.iterdir():
            if f.is_file() and f.suffix.lower() == ".txt":
                txts_ocr[f.stem] = f

        for f in sorted(pasta_pecas.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower() == ".txt":
                # Preserva TODOS os .txt (sao os OCRs)
                arquivos_preservados.append({
                    "nome": f.name,
                    "tamanho": f.stat().st_size,
                    "motivo": "OCR companion (preservado)",
                })
                continue
            ocr_companion = txts_ocr.get(f.stem)
            tem_ocr = ocr_companion is not None and ocr_companion.stat().st_size > 0
            arquivos_a_remover.append({
                "nome": f.name,
                "caminho": f"pecas/{f.name}",
                "tamanho": f.stat().st_size,
                "tem_ocr": tem_ocr,
            })

    # Avalia safeguards
    motivos_bloqueio: list[str] = []
    sem_ocr = [a for a in arquivos_a_remover if not a["tem_ocr"]]
    if sem_ocr:
        motivos_bloqueio.append(
            f"{len(sem_ocr)} arquivo(s) sem OCR — perder o original apagaria o conteudo "
            "(OCR ausente/vazio pode indicar Tesseract nao instalado ou arquivo nao-PDF)"
        )
    if em_caixa:
        motivos_bloqueio.append(
            "PAJ esta ATIVO na caixa (em_caixa_atual=True) — limpar cedo demais pode "
            "dificultar elaboracao de pecas; recomendado aguardar conclusao"
        )

    pode_limpar = len(motivos_bloqueio) == 0
    bytes_total = sum(a["tamanho"] for a in arquivos_a_remover)

    resultado = {
        "ok": True,
        "pode_limpar": pode_limpar,
        "motivos_bloqueio": motivos_bloqueio,
        "em_caixa_atual": em_caixa,
        "arquivos_a_remover": arquivos_a_remover,
        "arquivos_preservados": arquivos_preservados,
        "removidos": 0,
        "bytes_liberados": 0,
        "bytes_total_disponivel": bytes_total,
    }

    if dry_run:
        return resultado

    # Execucao: exige pode_limpar OU forcar
    if not pode_limpar and not forcar:
        resultado["ok"] = False
        resultado["erro"] = "bloqueado por safeguards — passe forcar=True para override"
        return resultado

    removidos = 0
    bytes_liberados = 0
    for a in arquivos_a_remover:
        caminho = pasta_pecas / a["nome"]
        try:
            tam = caminho.stat().st_size
            caminho.unlink()
            removidos += 1
            bytes_liberados += tam
        except Exception:
            pass

    resultado["removidos"] = removidos
    resultado["bytes_liberados"] = bytes_liberados

    # Atualiza n_anexos_sisdpu no metadata pra refletir que foram apagados
    # (mantem o valor historico num campo separado pra nao perder)
    if removidos > 0:
        metadata["n_anexos_removidos"] = metadata.get("n_anexos_removidos", 0) + removidos
        metadata["anexos_removidos_em"] = metadata.get("anexos_removidos_em", [])
        from datetime import datetime
        metadata["anexos_removidos_em"].append(datetime.now().isoformat(timespec="seconds"))
        with contextlib.suppress(Exception):
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

    return resultado


def listar_pecas_assistido(assistido: str) -> list[dict]:
    """Lista arquivos em Pecas Feitas/ cujo nome contenha termos do assistido."""
    if not assistido or not PECAS_FEITAS_DIR.exists():
        return []

    tokens = [
        _normalizar(t)
        for t in re.split(r"[\s,]+", assistido)
        if len(t) >= 4
    ]
    if not tokens:
        return []

    resultado: list[dict] = []
    for f in PECAS_FEITAS_DIR.iterdir():
        if not f.is_file():
            continue
        nome_norm = _normalizar(f.name)
        if any(tok in nome_norm for tok in tokens):
            resultado.append({
                "nome": f.name,
                "caminho": str(f),
                "tipo": f.suffix.lstrip(".").lower(),
                "tamanho": f.stat().st_size,
                "modificado": f.stat().st_mtime,
            })
    resultado.sort(key=lambda x: x["modificado"], reverse=True)
    return resultado


# ---------------------------------------------------------------------------
# Painel "Precisa acao" — classificador de pendencias baseado em heuristicas
# ---------------------------------------------------------------------------

def _parse_data_br(s: str) -> float | None:
    """Converte 'DD/MM/YYYY' ou 'DD/MM/YYYY HH:MM' em timestamp unix (float)."""
    import datetime as _dt
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _dias_desde(ts: float | None) -> float:
    """Dias (float) entre ts e agora. Retorna 1e9 se ts invalido (= nunca)."""
    import time as _t
    if ts is None:
        return 1e9
    return max(0.0, (_t.time() - ts) / 86400.0)


def classificar_pendencias(pajs: list[dict]) -> dict:
    """Classifica PAJs ativos em categorias de pendencia.

    Recebe a lista ja montada por `listar_pajs()` (sem arquivados). Retorna
    um dict:
        {
            "prazo":    [paj_norm, ...]   # intimacao/citacao recente sem resposta
            "despacho": [paj_norm, ...]   # decisao/despacho recente sem peca posterior
            "juntada":  [paj_norm, ...]   # peca gerada, sem mov "juntada" depois
        }

    As regras sao grosseiras — servem pra chamar atencao, nao pra substituir
    julgamento. O usuario confere em seguida.
    """
    buckets: dict[str, list[str]] = {"prazo": [], "despacho": [], "juntada": []}

    for p in pajs:
        paj_norm = p.get("paj_norm", "")
        if not paj_norm:
            continue

        # Re-le metadata pra pegar movs (o resumo listar_pajs nao tras tudo)
        pasta = PAJS_DIR / paj_norm
        meta = _ler_json(pasta / "metadata.json") or {}
        det = meta.get("detalhes_sisdpu", {}) or {}
        movs = det.get("movimentacoes", []) or []
        if not movs:
            continue

        # seq=1 = mais recente (SISDPU exporta do mais novo para o mais antigo)
        movs_sorted = sorted(
            movs, key=lambda m: int(m.get("seq", 0) or 0)
        )

        # Cada mov normalizada: desc_norm + data_ts
        for m in movs_sorted:
            m["_desc_norm"] = _normalizar(m.get("descricao", "") or "")
            m["_data_ts"] = _parse_data_br(m.get("data_original") or m.get("data") or "")

        ultima = movs_sorted[0]

        # --- 1. Prazo em andamento ---
        # Vascula as ULTIMAS 5 movimentacoes procurando intimacao / citacao /
        # notificacao sem resposta registrada posteriormente.
        #
        # Criterio de saida: presenca de resposta no sistema (juntada,
        # manifestacao, peticao, recurso, contestacao) em qualquer mov
        # com seq MAIOR — NAO uma janela de tempo. PAJ fica no card ate que
        # o SISDPU registre o protocolo de resposta.
        #
        # Fluxo interno DPU (NAO conta como resposta):
        #   "tramito a assessoria", "tramito ao nucleo", "tramito", "remeto",
        #   "envio", "solicito minuta" e congeneres. Esses termos nao estao
        #   em _RESP propositalmente. Alem disso, se uma mov de tramitacao
        #   mencionar "peticao" ou "manifestacao" de passagem (ex: "remeto
        #   para elaboracao de peticao"), ela seria falso-positivo de resposta.
        #   Por isso, se a candidata a resposta contiver termos de tramitacao
        #   interna (_INTERNO), ela e' descartada mesmo que tenha token de _RESP.
        #
        # Limite de 5 movs: evita pescar intimacoes muito antigas cujo ciclo
        # ja se encerrou sem registro formal de resposta.
        _INTIM   = ("intima", "cita", "notifica")
        _RESP    = ("juntada", "manifest", "petic", "recurso", "contesta")
        _INTERNO = ("tramito", "remeto", "envio",
                    "solicito minuta", "solicito petic",
                    "solicito elabora", "solicito peca",
                    "encaminho", "redistribu", "remessa")
        for m in movs_sorted[:5]:
            desc = m["_desc_norm"]
            if not desc or not any(t in desc for t in _INTIM):
                continue
            seq_desta = int(m.get("seq", 0) or 0)
            # Verifica resposta em QUALQUER mov com seq maior (nao so as 5),
            # excluindo movs de tramitacao interna que mencionem peca de passagem.
            respondida = False
            for outra in movs_sorted:
                d_outra = outra.get("_desc_norm") or ""
                if int(outra.get("seq", 0) or 0) <= seq_desta:
                    continue
                if not any(t in d_outra for t in _RESP):
                    continue
                if any(t in d_outra for t in _INTERNO):
                    continue  # tramitacao interna com mencao casual a peca — ignora
                respondida = True
                break
            if not respondida:
                buckets["prazo"].append(paj_norm)
                break  # uma categoria basta por PAJ

        # --- 2. Despacho novo ---
        ult_desc = ultima["_desc_norm"]
        if ("despacho" in ult_desc) or ("decis" in ult_desc):
            dias = _dias_desde(ultima["_data_ts"])
            if dias <= 15:
                # Sem peca gerada depois? (peca = arquivo na raiz do PAJ
                # cujo mtime > data_ts da mov)
                ts_mov = ultima["_data_ts"] or 0
                tem_peca_posterior = False
                for f in pasta.iterdir():
                    if not f.is_file() or f.name in IGNORAR:
                        continue
                    if f.stat().st_mtime > ts_mov:
                        tem_peca_posterior = True
                        break
                if not tem_peca_posterior and paj_norm not in buckets["despacho"]:
                    buckets["despacho"].append(paj_norm)

        # --- 3. Aguardando juntada ---
        # Peca gerada (na raiz) com mtime > data da ultima mov E ultima mov
        # nao contem "juntada".
        if "juntada" not in ult_desc:
            ts_ultima = ultima["_data_ts"] or 0
            for f in pasta.iterdir():
                if not f.is_file() or f.name in IGNORAR:
                    continue
                if f.stat().st_mtime > ts_ultima and ts_ultima > 0:
                    if paj_norm not in buckets["juntada"]:
                        buckets["juntada"].append(paj_norm)
                    break

    return buckets


# ---------------------------------------------------------------------------
# Notas pessoais por PAJ — NOTAS.md na pasta do PAJ
# ---------------------------------------------------------------------------

def _validar_paj_norm(paj_norm: str) -> Path | None:
    """Valida paj_norm e retorna a pasta (ou None se invalido)."""
    if not paj_norm or "/" in paj_norm or "\\" in paj_norm or ".." in paj_norm:
        return None
    if not paj_norm.startswith("PAJ-"):
        return None
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not pasta.is_dir():
        return None
    return pasta


def ler_notas(paj_norm: str) -> str:
    """Retorna conteudo de NOTAS.md (string vazia se nao existe)."""
    pasta = _validar_paj_norm(paj_norm)
    if pasta is None:
        return ""
    f = pasta / "NOTAS.md"
    if not f.exists():
        return ""
    try:
        return f.read_text(encoding="utf-8")
    except Exception:
        return ""


def salvar_notas(paj_norm: str, texto: str) -> bool:
    """Salva NOTAS.md. Retorna False se PAJ invalido."""
    pasta = _validar_paj_norm(paj_norm)
    if pasta is None:
        return False
    try:
        (pasta / "NOTAS.md").write_text(texto or "", encoding="utf-8")
        return True
    except Exception:
        return False
