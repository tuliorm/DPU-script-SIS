"""Catalogo dinamico de skills do workspace Oficio Geral.

Skills reais vivem em `OFICIO_GERAL/.claude/skills/<slug>/SKILL.md` (cada uma
com frontmatter YAML contendo ao menos `description`). Este modulo le esses
arquivos em runtime — assim, criar/renomear/remover skill no workspace
reflete imediatamente no painel, sem editar Python.

Para metadata que NAO esta no SKILL.md (grupo da UI, areas tematicas, label
curto), um dict de overrides por slug supre. Slugs sem override caem em
defaults seguros (grupo "Utilidades", areas ["*"], label = slug humanizado).

Skills internas/utilitarias do workspace que nao fazem sentido no dropdown
(ex: sis-caixa, sync_calendar) recebem `areas: []` no override — assim o
auto-discovery as ignora silenciosamente.

Cache em memoria: 60s + invalidacao por mtime do diretorio. Ler 30+ arquivos
pequenos e barato, mas o dashboard chama isso muitas vezes — vale a pena.
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from threading import Lock

from config import OFICIO_GERAL


GRUPOS = [
    "Triagem e análise",
    "Ação extrajudicial",
    "Petição inicial",
    "Peça processual",
    "Despacho SIS",
    "Utilidades",
]


# Overrides por slug. Tudo que aparece aqui sobrescreve os defaults derivados
# do nome do diretorio. Skills sem entrada aqui aparecem com label derivado do
# slug, areas=["*"] e grupo="Utilidades" — nao quebra, so e' menos polido.
#
# Para esconder uma skill da UI sem mexer no .claude/skills/ do workspace,
# basta colocar `areas: []` no override. Util para skills internas (sis-caixa,
# dpu-digital, ocr-documentos, exportar-documento, sync_calendar) que sao
# acionadas por outros caminhos, nao pelo dropdown "Elaborar".
SKILL_OVERRIDES: dict[str, dict] = {
    # --- Triagem e análise ---
    "firac-triagem":              {"label": "FIRAC / triagem",                   "areas": ["*"],                                    "grupo": "Triagem e análise"},
    "analisar-processo":          {"label": "Analisar processo",                 "areas": ["*"],                                    "grupo": "Triagem e análise"},
    "hipossuficiencia":           {"label": "Avaliar hipossuficiência",          "areas": ["*"],                                    "grupo": "Triagem e análise"},

    # --- Ação extrajudicial ---
    "oficios":                    {"label": "Ofício extrajudicial",              "areas": ["*"],                                    "grupo": "Ação extrajudicial"},

    # --- Petição inicial ---
    "peticoes-iniciais":          {"label": "Petição inicial (cível)",           "areas": ["Civel", "Administrativo"],              "grupo": "Petição inicial"},
    "saude-tema-1234":            {"label": "Inicial de saúde (Tema 1234/6)",    "areas": ["Saude"],                                "grupo": "Petição inicial"},
    "saude-geral":                {"label": "Saúde — peça geral",                "areas": ["Saude"],                                "grupo": "Petição inicial"},
    "previdenciario-geral":       {"label": "Previdenciário — inicial",          "areas": ["Previdenciario"],                       "grupo": "Petição inicial"},
    "ri-previdenciario":          {"label": "Recurso Inominado previdenciário",  "areas": ["Previdenciario"],                       "grupo": "Petição inicial"},
    "ri-pbf":                     {"label": "Recurso Inominado PBF",             "areas": ["Previdenciario"],                       "grupo": "Petição inicial"},
    "curadoria-especial":         {"label": "Curadoria especial",                "areas": ["Curadoria", "Civel"],                   "grupo": "Petição inicial"},

    # --- Peça processual ---
    "recursos-civel":             {"label": "Recurso cível (genérico)",          "areas": ["Civel", "Previdenciario", "Saude"],     "grupo": "Peça processual"},
    "apelacao-criminal":          {"label": "Apelação criminal",                 "areas": ["Criminal"],                             "grupo": "Peça processual"},
    "alegacoes-finais-criminal":  {"label": "Alegações finais criminais",        "areas": ["Criminal"],                             "grupo": "Peça processual"},
    "audiencia-custodia":         {"label": "Audiência de custódia",             "areas": ["Criminal"],                             "grupo": "Peça processual"},
    "criminal-geral":             {"label": "Criminal — peça geral",             "areas": ["Criminal"],                             "grupo": "Peça processual"},
    "relatorio-pre-audiencia":    {"label": "Relatório pré-audiência",           "areas": ["Criminal"],                             "grupo": "Peça processual"},
    "impugnacao-civel":           {"label": "Impugnação cível",                  "areas": ["Civel", "Execucao"],                    "grupo": "Peça processual"},
    "embargos-declaracao":        {"label": "Embargos de declaração",            "areas": ["*"],                                    "grupo": "Peça processual"},
    "contrarrazoes-ed":           {"label": "Contrarrazões a ED",                "areas": ["*"],                                    "grupo": "Peça processual"},
    "cumprimento-sentenca":       {"label": "Cumprimento de sentença",           "areas": ["Execucao", "Civel", "Previdenciario"],  "grupo": "Peça processual"},
    "desbloqueio-sisbajud":       {"label": "Desbloqueio SISBAJUD",              "areas": ["Execucao", "Civel"],                    "grupo": "Peça processual"},
    "execucao-fiscal":            {"label": "Execução fiscal",                   "areas": ["Execucao"],                             "grupo": "Peça processual"},
    "cdc-cef":                    {"label": "CDC / CEF",                         "areas": ["Civel"],                                "grupo": "Peça processual"},
    "civel-geral":                {"label": "Cível — peça geral",                "areas": ["Civel"],                                "grupo": "Peça processual"},

    # --- Despacho SIS ---
    "despacho-sis":               {"label": "Despacho SIS",                      "areas": ["*"],                                    "grupo": "Despacho SIS"},

    # --- Utilidades ---
    "melhorar-textos":            {"label": "Melhorar textos",                   "areas": ["*"],                                    "grupo": "Utilidades"},
    "cross-examination":          {"label": "Cross examination (preparo)",       "areas": ["Criminal"],                             "grupo": "Utilidades"},
    "superacao-barreiras":        {"label": "Superar barreiras processuais",     "areas": ["*"],                                    "grupo": "Utilidades"},

    # --- Skills internas: nao expor no dropdown ---
    # `areas: []` sinaliza ao auto-discovery que estas devem ser escondidas.
    # Nao remove a skill do workspace — so nao aparece neste catalogo.
    "sis-caixa":                  {"areas": []},
    "dpu-digital":                {"areas": []},
    "ocr-documentos":             {"areas": []},
    "exportar-documento":         {"areas": []},
    "sync_calendar":              {"areas": []},
}


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(texto: str) -> dict[str, str]:
    """Parser minimo de frontmatter YAML.

    Suporta `chave: valor` em uma linha e continuacoes indentadas (linhas
    iniciadas por espaco/tab sao concatenadas no valor da chave anterior).
    Nao suporta literais multi-linha (`|`, `>`) — proposital: skills do
    workspace usam o formato simples e essa funcao precisa zero dependencias.
    """
    m = _FRONTMATTER_RE.match(texto)
    if not m:
        return {}
    bloco = m.group(1)
    out: dict[str, str] = {}
    chave_atual: str | None = None
    for linha in bloco.splitlines():
        if not linha.strip():
            continue
        # Continuacao indentada: junta no valor da chave anterior
        if linha[0] in (" ", "\t") and chave_atual is not None:
            out[chave_atual] = (out[chave_atual] + " " + linha.strip()).strip()
            continue
        if ":" not in linha:
            continue
        chave, _, valor = linha.partition(":")
        chave_atual = chave.strip()
        out[chave_atual] = valor.strip()
    return out


def _label_padrao(slug: str) -> str:
    """Slug → label legivel. 'peticoes-iniciais' → 'Peticoes iniciais'."""
    return slug.replace("-", " ").replace("_", " ").capitalize()


def _diretorio_skills() -> Path:
    return OFICIO_GERAL / ".claude" / "skills"


def _signature_skills() -> tuple[float, int]:
    """Assinatura barata pra detectar mudancas estruturais (skill nova,
    removida, renomeada). Edicao interna de SKILL.md nao invalida — o TTL
    de 60s cobre."""
    base = _diretorio_skills()
    if not base.exists():
        return (0.0, 0)
    try:
        return (base.stat().st_mtime, sum(1 for _ in base.iterdir()))
    except Exception:
        return (0.0, 0)


_skills_cache: dict = {"key": None, "ts": 0.0, "result": None}
_skills_lock = Lock()
_SKILLS_TTL_SEG = 60.0


def _carregar_skills() -> list[dict]:
    base = _diretorio_skills()
    if not base.exists() or not base.is_dir():
        return []
    skills: list[dict] = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.exists():
            continue
        slug = sub.name
        try:
            texto = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = _parse_frontmatter(texto)
        descricao = (meta.get("description") or meta.get("Description") or "").strip()

        override = SKILL_OVERRIDES.get(slug, {})

        # `areas: []` no override esconde a skill do dropdown (skills internas).
        if "areas" in override and not override["areas"]:
            continue

        areas = override.get("areas", ["*"])
        skills.append({
            "slug": slug,
            "label": override.get("label") or _label_padrao(slug),
            "descricao": descricao or override.get("descricao", ""),
            "areas": list(areas),
            "grupo": override.get("grupo") or "Utilidades",
        })
    return skills


def listar_skills() -> list[dict]:
    """Catalogo atual lendo OFICIO_GERAL/.claude/skills/ (cache 60s + mtime).

    Substitui a antiga lista hardcoded `SKILLS = [...]`. Para invalidar
    manualmente (ex: depois de criar skill nova e querer ver na hora),
    chamar `invalidar_cache_skills()`.
    """
    sig = _signature_skills()
    now = time.monotonic()
    with _skills_lock:
        if (
            _skills_cache["result"] is not None
            and _skills_cache["key"] == sig
            and (now - _skills_cache["ts"]) < _SKILLS_TTL_SEG
        ):
            return _skills_cache["result"]
        result = _carregar_skills()
        _skills_cache["result"] = result
        _skills_cache["ts"] = now
        _skills_cache["key"] = sig
        return result


def invalidar_cache_skills() -> None:
    """Forca releitura no proximo `listar_skills()`."""
    with _skills_lock:
        _skills_cache["result"] = None
        _skills_cache["ts"] = 0.0
        _skills_cache["key"] = None


def _norm_area(area: str) -> str:
    """Normaliza area do metadata (pode vir 'Civel', 'civel', 'Cível')."""
    if not area:
        return ""
    nfkd = unicodedata.normalize("NFKD", area)
    semacento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return semacento.strip().title()


def skills_para_area(area: str | None) -> list[dict]:
    """Retorna catalogo com flag `destaque` marcando skills aplicaveis a area.

    Todas as skills sempre estao na lista (pra nao restringir escolha do
    defensor); as que casam com a area do PAJ vem com `destaque=True` pro
    frontend ordenar/estilizar.
    """
    area_norm = _norm_area(area or "")
    out: list[dict] = []
    for s in listar_skills():
        destaque = False
        if area_norm and ("*" in s["areas"] or area_norm in s["areas"]):
            destaque = True
        out.append({**s, "destaque": destaque})
    return out


def skill_valida(slug: str) -> bool:
    return any(s["slug"] == slug for s in listar_skills())


def skill_descricao(slug: str) -> str:
    for s in listar_skills():
        if s["slug"] == slug:
            return s["descricao"]
    return ""


def listar_grupos() -> list[str]:
    return list(GRUPOS)
