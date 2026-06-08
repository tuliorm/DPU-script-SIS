"""Triagem inteligente do "Elaborar todos" (modo automático).

Roteia a skill por PAJ e, para PAJs **com processo judicial**, roda um pipeline
de 2 fases com effort condicional:

- **SEM processo** → `firac-triagem` (triagem pré-processual), 1 fase, effort padrão.
- **COM processo** → `analisar-processo`:
  - **Fase 1 (classificar):** a skill classifica (TRIVIAL / HARD CASE / URGENTE) e
    recomenda a peça (effort padrão = xhigh). Ao concluir, `chat_service` chama
    `encadear_fase2`.
  - **Fase 2 (elaborar):** elabora a peça recomendada — `ELABORACAO_EFFORT_HARD`
    (max) se HARD CASE, senão o padrão. HARD usa sessão nova (garante o effort);
    não-HARD retoma a Fase 1 via `--resume` (mesmo effort, reaproveita contexto).

Tudo best-effort e idempotente o suficiente (a triagem do `start_lote` já pulou
PAJs com peça/sync incompleta/em andamento).
"""

from __future__ import annotations

import re

from config import (
    ELABORACAO_EFFORT,
    ELABORACAO_EFFORT_HARD,
    PAJS_DIR,
    SKILL_COM_PROCESSO,
    SKILL_SEM_PROCESSO,
)

# Captura o VALOR do campo de classificação ("CLASSIFICAÇÃO| …" do bloco-resumo,
# ou "**Tipo:** …" da seção) preenchido pela skill analisar-processo. Ignora a
# linha-template (que lista os 3 rótulos entre colchetes/pipes) e menções nos
# critérios — só decide por um campo de classificação efetivamente preenchido.
_RE_CAMPO_CLASS = re.compile(
    r"(?:classifica[çc][ãa]o\s*\|?|tipo)\s*[:|]\s*\*{0,2}\s*([^\n]+)",
    re.IGNORECASE,
)
_RE_HARD = re.compile(r"hard\s*case", re.IGNORECASE)
_RE_NAO_HARD = re.compile(r"trivial|urgente", re.IGNORECASE)


def classificar(texto: str | None) -> str:
    """Lê a classificação do output da Fase 1: retorna 'hard' ou 'normal'.

    Default conservador 'normal' (trivial/urgente) se não identificar com
    segurança — assim só sobe pra 'max' quando a análise diz HARD CASE de fato.
    """
    if not texto:
        return "normal"
    for m in _RE_CAMPO_CLASS.finditer(texto):
        valor = m.group(1)
        # Pula o template não preenchido: "[TRIVIAL | HARD CASE | URGENTE]".
        if "[" in valor and "|" in valor:
            continue
        if _RE_HARD.search(valor):
            return "hard"
        if _RE_NAO_HARD.search(valor):
            return "normal"
    return "normal"


def _analise_path(paj_norm: str):
    return PAJS_DIR / paj_norm / "analise.md"


def _ler_analise(paj_norm: str) -> str:
    f = _analise_path(paj_norm)
    if f.exists():
        try:
            return f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _instrucao_fase1(paj_norm: str) -> str:
    """Instrução da Fase 1: APENAS classificar/recomendar (não elaborar). Enviada
    pelo start() anexada ao PROMPT_MAX. Evita que a analisar-processo já redija a
    peça (o que tornaria a Fase 2 redundante)."""
    analise = PAJS_DIR / paj_norm / "analise.md"
    return (
        "Use a skill `/analisar-processo` do workspace Ofício Geral para "
        "DIAGNOSTICAR este PAJ (processo judicial em andamento): extraia os dados, "
        "calcule o prazo (com dobro da DPU), CLASSIFIQUE o caso (TRIVIAL / HARD "
        "CASE / URGENTE, pelos critérios da skill) e recomende a peça cabível.\n\n"
        f"Salve a análise em `{analise}` (sobrescreva se existir), incluindo o "
        "bloco-resumo com o campo `CLASSIFICAÇÃO| ...` no formato da skill.\n\n"
        "**NÃO elabore a peça agora — apenas analise, classifique e recomende.** "
        "A elaboração é a etapa seguinte (automática), com o esforço calibrado "
        "pela classificação. Não pergunte nada; conclua a análise."
    )


def _instrucao_fase2(paj_norm: str, hard: bool, resume: bool) -> str:
    """Instrução da Fase 2 (elaboração pós-análise). Não reenvia o PROMPT_MAX:
    referencia os arquivos do PAJ (o Claude os lê) ou o contexto já retomado."""
    pasta = PAJS_DIR / paj_norm
    analise = _analise_path(paj_norm)
    if resume:
        intro = "Você acabou de analisar e classificar este PAJ (resumo acima nesta conversa)."
    else:
        intro = (
            f"A análise/triagem deste PAJ já foi concluída e está salva em "
            f"`{analise}`. Leia-a (e `{pasta / 'PROMPT_MAX.md'}` para o contexto e "
            f"o inventário de anexos)."
        )
    nivel = (
        "Este é um **HARD CASE** — dedique esforço máximo: aprofunde as teses, "
        "antecipe contra-argumentos e fundamente com rigor.\n\n"
        if hard
        else ""
    )
    return (
        f"{intro}\n\n{nivel}"
        "Agora **ELABORE** a peça/despacho recomendado na análise. Não pergunte, "
        "não reanalise do zero — produza o TEXTO final, pronto pra protocolar/"
        "expedir, seguindo o `CLAUDE.md` do workspace (terminologia DPU, estilo, "
        "endereçamento, assinatura).\n\n"
        f"Salve o(s) arquivo(s) gerado(s) na pasta do PAJ (`{pasta}`) — ex.: "
        "`peticao.txt`, `recurso.txt`, `despacho.txt`, `manifestacao.txt`. Ao "
        "final, apresente um RESUMO ESTRUTURADO (produto, justificativa, texto, "
        "arquivos gerados, pontos-chave)."
    )


def plano_paj(item: dict) -> dict:
    """Retorna o plano de roteamento de UM PAJ, SEM disparar (para dry-run/UI).

    {skill, fluxo: '1-fase'|'2-fases', fase1_effort}.
    """
    from services.paj_service import tem_processo

    if tem_processo(item):
        return {"skill": SKILL_COM_PROCESSO, "fluxo": "2-fases", "fase1_effort": ELABORACAO_EFFORT}
    return {"skill": SKILL_SEM_PROCESSO, "fluxo": "1-fase", "fase1_effort": ELABORACAO_EFFORT}


def disparar_paj(paj_norm: str, item: dict) -> dict:
    """Roteia e dispara UM PAJ no modo automático. Retorna o status + metadados
    de roteamento (pro relatório do lote)."""
    from services.chat_service import start_or_queue
    from services.paj_service import tem_processo

    if tem_processo(item):
        # Fase 1: SÓ classificar (analisar-processo via instrução custom, sem
        # elaborar). Ao concluir, o chat_service chama encadear_fase2.
        res = start_or_queue(
            paj_norm,
            skill_slug=SKILL_COM_PROCESSO,
            effort_override=ELABORACAO_EFFORT,
            encadear_elaboracao=True,
            instrucao_inicial=_instrucao_fase1(paj_norm),
        )
        return {**res, "fluxo": "2-fases", "fase": "classificar", "skill": SKILL_COM_PROCESSO}

    res = start_or_queue(
        paj_norm,
        skill_slug=SKILL_SEM_PROCESSO,
        effort_override=ELABORACAO_EFFORT,
    )
    return {**res, "fluxo": "1-fase", "fase": "triagem", "skill": SKILL_SEM_PROCESSO}


def encadear_fase2(paj_norm: str, summary_fase1: str, session_id_fase1: str) -> None:
    """Chamado por chat_service ao concluir a Fase 1 (analisar-processo).

    Classifica pelo output da Fase 1 (fallback: analise.md) e dispara a Fase 2:
    - HARD CASE → effort `ELABORACAO_EFFORT_HARD` (max), SESSÃO NOVA lendo
      analise.md (garante o effort; sessão nova respeita --effort).
    - não-HARD → effort padrão, RETOMA a Fase 1 via --resume (mesmo effort,
      reaproveita o contexto). Sem session_id → cai em sessão nova.
    """
    from services.chat_service import start_or_queue

    texto = summary_fase1 or _ler_analise(paj_norm)
    hard = classificar(texto) == "hard"
    effort = ELABORACAO_EFFORT_HARD if hard else ELABORACAO_EFFORT

    if hard or not session_id_fase1:
        instrucao = _instrucao_fase2(paj_norm, hard=hard, resume=False)
        start_or_queue(
            paj_norm,
            skill_slug=None,
            effort_override=effort,
            instrucao_elaboracao=instrucao,
        )
    else:
        instrucao = _instrucao_fase2(paj_norm, hard=False, resume=True)
        start_or_queue(
            paj_norm,
            skill_slug=None,
            effort_override=effort,
            resume_session_id=session_id_fase1,
            instrucao_elaboracao=instrucao,
        )
