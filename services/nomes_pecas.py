"""Traducao dos codigos e-Proc pra nomes legiveis em portugues.

Os arquivos baixados pelo dpuscript tem nomes como:
    2018-08-14_ev1_ACOR16.pdf
    2026-03-28_ev4_DESPADEC1.pdf

A parte apos `ev{N}_` e o CODIGO do documento no e-Proc (ex: ACOR, DESPADEC).
Este modulo traduz esses codigos pra nomes humanos e agrupa por categoria.
"""

from __future__ import annotations

import re

# Mapeamento: codigo -> (nome_legivel, categoria)
NOMES: dict[str, tuple[str, str]] = {
    # Decisoes e despachos
    "DEC": ("Decisao", "decisoes"),
    "DECTNU": ("Decisao TNU", "decisoes"),
    "DECADMPU": ("Decisao de Admissibilidade (PU)", "decisoes"),
    "DESPACHO": ("Despacho", "decisoes"),
    "DESPADEC": ("Despacho/Decisao", "decisoes"),
    "SENT": ("Sentenca", "decisoes"),
    "ACOR": ("Acordao", "decisoes"),
    "ACORTR": ("Acordao do TRF", "decisoes"),

    # Recursos e impugnacoes
    "EMBDECL": ("Embargos de Declaracao", "recursos"),
    "CONTRAZ": ("Contrarrazoes", "recursos"),
    "REC": ("Recurso", "recursos"),
    "RECEXTRA": ("Recurso Extraordinario", "recursos"),
    "AGRAVO": ("Agravo", "recursos"),
    "AGRAVOINOMLEG": ("Agravo Inominado Legal", "recursos"),
    "AGRRETID": ("Agravo Retido", "recursos"),

    # Pedidos de uniformizacao e peticoes iniciais
    "INIC": ("Peticao Inicial", "peticoes"),
    "PET": ("Peticao", "peticoes"),
    "PU": ("Pedido de Uniformizacao", "peticoes"),
    "PEDUNIFNAC": ("Pedido de Uniformizacao Nacional", "peticoes"),
    "MEMORIAIS": ("Memoriais", "peticoes"),

    # Contestacao
    "CONT": ("Contestacao", "contestacao"),

    # Documentos probatorios
    "LAUDO": ("Laudo Pericial", "provas"),
    "PARECER": ("Parecer (MPF)", "provas"),
    "CERT": ("Certidao", "provas"),
    "CERTOBT": ("Certidao de Obito", "provas"),
    "DECL": ("Declaracao", "provas"),
    "ATA": ("Ata", "provas"),

    # Documentos pessoais / cadastrais
    "PROC": ("Procuracao", "pessoais"),
    "SUBS": ("Substabelecimento", "pessoais"),
    "PROCSUB": ("Procuracao/Substabelecimento", "pessoais"),
    "RG": ("RG", "pessoais"),
    "END": ("Comprovante de Endereco", "pessoais"),
    "ESTATUTO": ("Estatuto", "pessoais"),
    "CONTR": ("Contrato", "pessoais"),

    # Tramite e administrativo
    "OFIC": ("Oficio", "tramite"),
    "MEMORANDO": ("Memorando", "tramite"),
    "EMAIL": ("Email", "tramite"),
    "COMP": ("Documento Complementar", "tramite"),
    "ANEXO": ("Anexo", "tramite"),
}

CATEGORIAS_ORDEM = ["decisoes", "recursos", "peticoes", "contestacao", "provas", "pessoais", "tramite", "outros"]

CATEGORIAS_LABEL = {
    "decisoes": "Decisoes e Despachos",
    "recursos": "Recursos e Impugnacoes",
    "peticoes": "Peticoes",
    "contestacao": "Contestacao",
    "provas": "Provas",
    "pessoais": "Documentos Pessoais",
    "tramite": "Tramite",
    "outros": "Outros",
}

CATEGORIAS_COR = {
    "decisoes": "error",       # vermelho — mais importante
    "recursos": "warning",     # laranja
    "peticoes": "info",        # azul
    "contestacao": "accent",   # roxo
    "provas": "success",       # verde
    "pessoais": "ghost",       # cinza
    "tramite": "neutral",
    "outros": "neutral",
}


# Regex pra extrair: YYYY-MM-DD_ev{N}_CODIGO{N_OPCIONAL}.{ext}
_RE_PECA = re.compile(r"^(\d{4}-\d{2}-\d{2})_ev(\d+)_([A-Z]+)(\d*)(?:\.|_)")


def parse_nome_peca(filename: str) -> dict:
    """Extrai metadados do filename de uma peca baixada pelo dpuscript.

    Input: "2018-08-14_ev1_ACOR16.pdf"
    Output: {
        "data": "2018-08-14",
        "evento": 1,
        "codigo": "ACOR",
        "sequencial": 16,
        "nome_legivel": "Acordao",
        "categoria": "decisoes",
        "categoria_label": "Decisoes e Despachos",
        "categoria_cor": "error",
    }
    """
    m = _RE_PECA.match(filename)
    if not m:
        return {
            "data": "",
            "evento": 0,
            "codigo": "?",
            "sequencial": 0,
            "nome_legivel": filename,
            "categoria": "outros",
            "categoria_label": "Outros",
            "categoria_cor": "neutral",
        }

    data = m.group(1)
    evento = int(m.group(2))
    codigo = m.group(3)
    seq_str = m.group(4)
    sequencial = int(seq_str) if seq_str else 0

    nome_legivel, categoria = NOMES.get(codigo, (codigo.title(), "outros"))

    return {
        "data": data,
        "evento": evento,
        "codigo": codigo,
        "sequencial": sequencial,
        "nome_legivel": nome_legivel,
        "categoria": categoria,
        "categoria_label": CATEGORIAS_LABEL.get(categoria, "Outros"),
        "categoria_cor": CATEGORIAS_COR.get(categoria, "neutral"),
    }
