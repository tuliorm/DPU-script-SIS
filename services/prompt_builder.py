"""Gera PROMPT_MAX.md dinamicamente por PAJ.

Concatena:
- cabecalho estruturado (identificacao, prazo, processo)
- ultimas movimentacoes
- lista de pecas anteriores do mesmo assistido em `Pecas Feitas/`
- texto completo do SISDPU
"""

from __future__ import annotations

import json
from pathlib import Path

from config import PAJS_DIR


MAX_MOVIMENTACOES_RESUMO = 8


def _ler_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def gerar_prompt_max(paj_norm: str) -> Path | None:
    """Monta PROMPT_MAX.md dentro da pasta do PAJ e retorna o Path."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return None

    metadata = _ler_json(pasta / "metadata.json") or {}
    sisdpu_path = pasta / "sisdpu.txt"
    sisdpu_texto = (
        sisdpu_path.read_text(encoding="utf-8", errors="replace")
        if sisdpu_path.exists()
        else ""
    )

    # Import tardio pra evitar ciclo com paj_service
    from services.paj_service import listar_pecas_assistido

    pecas_antes = listar_pecas_assistido(metadata.get("assistido_caixa", ""))

    partes: list[str] = []
    partes.append(f"# PAJ {metadata.get('paj', paj_norm)}")
    partes.append("")
    partes.append("## Identificacao")
    partes.append(f"- **Assistido:** {metadata.get('assistido_caixa') or '—'}")
    partes.append(f"- **Pretensao:** {metadata.get('pretensao') or '—'}")
    partes.append(f"- **Oficio responsavel:** {metadata.get('oficio_caixa') or '—'}")
    proc = metadata.get("processo_judicial") or ""
    foro_det = metadata.get("foro_detalhado") or ""
    if proc:
        partes.append(f"- **Processo judicial:** {proc}" + (f" ({foro_det})" if foro_det else ""))
    else:
        partes.append("- **Processo judicial:** (nao cadastrado)")
    partes.append(f"- **Status:** {metadata.get('detalhes_sisdpu', {}).get('status_paj') or 'Ativo'}")

    prazos = metadata.get("prazos_abertos") or []
    if prazos:
        partes.append("")
        partes.append("## Prazos abertos")
        for p in prazos:
            descr = p.get("descricao") or p.get("parte") or "prazo"
            partes.append(f"- **{p.get('data_final', '?')}** ({p.get('dias', '?')} dias) — {descr}")

    # Movimentacoes recentes (cronologico reverso)
    det = metadata.get("detalhes_sisdpu", {}) or {}
    movs = det.get("movimentacoes") or []
    movs_ord = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0), reverse=True)
    truncou_alguma = False
    if movs_ord:
        partes.append("")
        partes.append(f"## Ultimas movimentacoes (top {min(MAX_MOVIMENTACOES_RESUMO, len(movs_ord))})")
        for mov in movs_ord[:MAX_MOVIMENTACOES_RESUMO]:
            data = mov.get("data_original") or mov.get("data") or "?"
            descr = (mov.get("descricao") or "").strip()
            if len(descr) > 400:
                descr = descr[:400].rstrip() + "..."
                truncou_alguma = True
            partes.append(f"- **[{data}]** {descr}")
        # Aviso explicito sobre truncamento + onde ler o texto integral. So aparece
        # quando alguma descricao foi cortada — caso contrario, o resumo ja basta.
        if truncou_alguma:
            partes.append("")
            partes.append(
                "> Algumas descricoes acima foram truncadas em 400 caracteres. O texto "
                "integral consta na secao \"Texto completo do SISDPU\" abaixo, e "
                "tambem em `sisdpu.txt` nesta mesma pasta caso prefira ler isolado."
            )

    if pecas_antes:
        partes.append("")
        partes.append(f"## Pecas anteriores do mesmo assistido ({len(pecas_antes)})")
        partes.append("Arquivos em `Pecas Feitas/` com nome similar ao assistido:")
        for p in pecas_antes[:20]:
            partes.append(f"- `{p['nome']}`")

    partes.append("")
    partes.append("---")
    partes.append("")
    partes.append("## Texto completo do SISDPU")
    partes.append("")
    partes.append(sisdpu_texto.strip() or "(sem texto de SISDPU)")
    partes.append("")
    partes.append("---")
    partes.append("")
    partes.append(
        "Siga o `CLAUDE.md` do workspace. Ao final, proponha o proximo passo "
        "(despacho SISDPU, peticao, recurso, manifestacao) e produza o TEXTO "
        "pronto da peca/despacho na pasta do PAJ."
    )

    prompt_path = pasta / "PROMPT_MAX.md"
    prompt_path.write_text("\n".join(partes), encoding="utf-8")
    return prompt_path
