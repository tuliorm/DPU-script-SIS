"""Calculo deterministico de prazos processuais.

Regras (decididas pelo usuario):

Civel/Administrativo:
  - Dias uteis (CPC art. 219).
  - Exclui o dia da intimacao, inclui o dia final.
  - Termo final em fim de semana/feriado prorroga pro proximo dia util.
  - Suspensao 20/12 a 20/01 (recesso forense): dias dentro nao contam.
  - Prazo em dobro pra DPU (art. 186 CPC, art. 44, I, LC 80/94) por padrao.

JEF (Juizado Especial Federal):
  - Dias uteis e regras gerais do CPC, MAS sem prazo em dobro pra DPU.
  - Recesso forense suspende.

Penal:
  - Dias corridos (sabado, domingo e feriado contam).
  - Exclui o dia da intimacao. Se inicio cair em fim de semana/feriado,
    comeca no proximo dia util.
  - Termo final em fim de semana/feriado prorroga.
  - SEM dobra pra DPU em prazo penal.
  - Recesso forense NAO suspende prazo penal.

Ciencia ficta de 10 dias (PJe/eproc): NAO tratada nesta versao. O termo
inicial e' sempre `data_mov` (data da movimentacao no SISDPU). O usuario
pediu pra nao presumir leitura automatica.

Sem dependencia externa: feriados nacionais + Carnaval/Pascoa/Corpus Christi
calculados a partir do algoritmo de Gauss para a data da Pascoa.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from enum import Enum
from functools import lru_cache


class Rito(str, Enum):
    CIVEL = "civel"
    JEF = "jef"
    PENAL = "penal"
    ADMINISTRATIVO = "administrativo"


# Recesso forense (CPC art. 220): 20/12 a 20/01, inclusivos.
RECESSO_INICIO_MES_DIA = (12, 20)
RECESSO_FIM_MES_DIA = (1, 20)


def _domingo_pascoa(ano: int) -> dt.date:
    """Algoritmo de Gauss/Meeus pra data da Pascoa no calendario gregoriano."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    mes = (h + L - 7 * m + 114) // 31
    dia = ((h + L - 7 * m + 114) % 31) + 1
    return dt.date(ano, mes, dia)


@lru_cache(maxsize=64)
def feriados_nacionais(ano: int) -> frozenset[dt.date]:
    """Feriados federais brasileiros + Carnaval/Sexta-feira Santa/Corpus Christi.

    Nao inclui feriados estaduais/municipais. Pra fins de prazo processual
    federal isso e' suficiente; o sistema judiciario federal segue feriados
    nacionais. Se aparecerem casos com feriado estadual relevante, expandir
    a fonte aqui (ex: aniversarios de capital).
    """
    fixos = {
        dt.date(ano, 1, 1),    # Confraternizacao
        dt.date(ano, 4, 21),   # Tiradentes
        dt.date(ano, 5, 1),    # Trabalho
        dt.date(ano, 9, 7),    # Independencia
        dt.date(ano, 10, 12),  # N. Sra. Aparecida
        dt.date(ano, 11, 2),   # Finados
        dt.date(ano, 11, 15),  # Proclamacao da Republica
        dt.date(ano, 11, 20),  # Consciencia Negra (federal a partir de 2024)
        dt.date(ano, 12, 25),  # Natal
    }
    pascoa = _domingo_pascoa(ano)
    moveis = {
        pascoa - dt.timedelta(days=48),  # Segunda de Carnaval
        pascoa - dt.timedelta(days=47),  # Terca de Carnaval
        pascoa - dt.timedelta(days=2),   # Sexta-feira Santa
        pascoa + dt.timedelta(days=60),  # Corpus Christi
    }
    return frozenset(fixos | moveis)


def eh_recesso_forense(d: dt.date) -> bool:
    """True se a data esta entre 20/12 e 20/01 (inclusivos)."""
    if (d.month, d.day) >= RECESSO_INICIO_MES_DIA:
        return True
    if (d.month, d.day) <= RECESSO_FIM_MES_DIA:
        return True
    return False


def eh_dia_util(d: dt.date) -> bool:
    """Util pra prazo civel: nao e' sab/dom, nao e' feriado, nao e' recesso."""
    if d.weekday() >= 5:  # 5=sab, 6=dom
        return False
    if d in feriados_nacionais(d.year):
        return False
    if eh_recesso_forense(d):
        return False
    return True


def eh_dia_util_penal(d: dt.date) -> bool:
    """Pra prorrogacao em prazo penal: ignora recesso forense."""
    if d.weekday() >= 5:
        return False
    if d in feriados_nacionais(d.year):
        return False
    return True


def proximo_dia_util(d: dt.date, *, penal: bool = False) -> dt.date:
    """Avanca ate cair num dia util. Se ja for util, retorna a propria data."""
    check = eh_dia_util_penal if penal else eh_dia_util
    while not check(d):
        d = d + dt.timedelta(days=1)
    return d


def _calcular_civel(data_mov: dt.date, dias: int) -> dt.date:
    """Conta `dias` dias uteis a partir do dia seguinte a data_mov."""
    cur = data_mov + dt.timedelta(days=1)
    restantes = dias
    while restantes > 0:
        if eh_dia_util(cur):
            restantes -= 1
            if restantes == 0:
                return cur
        cur = cur + dt.timedelta(days=1)
    return cur


def _calcular_penal(data_mov: dt.date, dias: int) -> dt.date:
    """Penal: dias corridos. Inicio prorroga se cair em fds/feriado.
    Termo final tambem prorroga se cair em fds/feriado."""
    inicio = proximo_dia_util(data_mov + dt.timedelta(days=1), penal=True)
    final = inicio + dt.timedelta(days=dias - 1)
    return proximo_dia_util(final, penal=True)


def calcular_data_alvo(
    data_mov: dt.date,
    dias: int,
    rito: Rito,
    em_dobro: bool = True,
) -> dt.date:
    """Calcula data-alvo do prazo aplicando as regras do rito.

    `em_dobro=True` aplica a dobra da DPU (art. 186 CPC). Ignorado pra
    rito penal (sem dobra) e pra JEF (sem dobra explicita).
    """
    if dias <= 0:
        return data_mov

    if rito == Rito.PENAL:
        return _calcular_penal(data_mov, dias)

    dias_efetivos = dias
    if em_dobro and rito in (Rito.CIVEL, Rito.ADMINISTRATIVO):
        dias_efetivos = dias * 2
    # JEF: sem dobra, mesmo se em_dobro=True
    final = _calcular_civel(data_mov, dias_efetivos)
    # Garante que termo final cai em dia util (a contagem ja faz isso, mas
    # cobre o caso degenerado dias=0)
    return proximo_dia_util(final)


def dias_restantes(data_alvo: dt.date, hoje: dt.date | None = None) -> int:
    """Diferenca em dias corridos entre hoje e data_alvo. Negativo = vencido."""
    hoje = hoje or dt.date.today()
    return (data_alvo - hoje).days


def _norm(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def inferir_rito(area_paj: str = "", descricao_mov: str = "", foro: str = "") -> Rito:
    """Heuristica conservadora pra inferir rito.

    Ordem:
    1. JEF se foro/descricao mencionar 'juizado especial federal' ou 'JEF'.
    2. Penal se area=='criminal' ou descricao tem palavras-chave penais.
    3. Administrativo se area=='administrativo'.
    4. Civel default.
    """
    area = _norm(area_paj)
    desc = _norm(descricao_mov)
    foro_n = _norm(foro)

    if "juizado especial federal" in desc or "juizado especial federal" in foro_n:
        return Rito.JEF
    if " jef " in f" {desc} " or " jef " in f" {foro_n} ":
        return Rito.JEF

    if area in ("criminal", "penal"):
        return Rito.PENAL
    if any(t in desc for t in ("denuncia", "reu", "alegacoes finais", "habeas corpus", "cautelar penal")):
        return Rito.PENAL

    if area == "administrativo":
        return Rito.ADMINISTRATIVO

    return Rito.CIVEL
