"""Classificação das linhas do Parquet a partir das flags do gerador.

O gerador grava ``IS_EXTRA_KIT`` e ``IS_AVULSO`` como booleanos anuláveis:

* ``True``  → o usuário marcou "Sim";
* ``False`` → marcou "Não" (ou o app limpou ao preencher o item);
* ``None``  → o usuário nunca classificou (ou o arquivo é antigo e não tem a
  coluna).

A precedência é sempre flag explícita > heurística (``types.csv``/fallback).
Um mesmo Parquet pode misturar Normal, Kit e Extra: a classificação é por
linha, nunca pelo nome do arquivo.
"""
from __future__ import annotations

import pandas as pd

AVULSO = "AVULSO"
KIT = "KIT"
EXTRA_FALLBACK = "EXTRA_FALLBACK"
REJEITAR = "REJEITAR"

FLAGS = ("IS_EXTRA_KIT", "IS_AVULSO")

_VALORES_VERDADEIROS = {"true", "sim", "yes", "1", "t"}
_VALORES_FALSOS = {"false", "nao", "não", "no", "0", "f"}
_VALORES_AUSENTES = {"", "none", "nan", "null", "<na>"}


def flag_booleana(valor):
    """Normaliza uma flag anulável para ``True``/``False``/``None``.

    Tolera ``NaN``, ``pd.NA``, ``numpy.bool_`` e texto ("Sim"/"Não").
    """
    if valor is None:
        return None
    if isinstance(valor, bool):
        return valor
    try:
        if valor != valor:  # NaN / pd.NA
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(valor, str):
        texto = valor.strip().lower()
        if texto in _VALORES_VERDADEIROS:
            return True
        if texto in _VALORES_FALSOS:
            return False
        return None
    try:
        return bool(valor)
    except (TypeError, ValueError):
        return None


def _ausente(valor):
    if valor is None:
        return True
    if isinstance(valor, str) and valor.strip().lower() in _VALORES_AUSENTES:
        return True
    try:
        if valor != valor:  # NaN / pd.NA
            return True
    except (TypeError, ValueError):
        return True
    return False


def classe_por_flags(is_extra_kit, is_avulso, contract_id, contract_item_id):
    """Decide a classe comercial de uma linha do Parquet.

    Retorna ``(classe, motivo, origem)`` ou ``None`` para seguir a heurística
    legada (``types.csv`` + fallback). ``classe`` é um de ``AVULSO``,
    ``KIT``, ``EXTRA_FALLBACK`` ou ``REJEITAR``.
    """
    kit = flag_booleana(is_extra_kit)
    avulso = flag_booleana(is_avulso)
    tem_contrato = not _ausente(contract_id)
    tem_item = not _ausente(contract_item_id)

    # Só duas combinações são rejeitadas: as que não têm interpretação
    # defensável (o app permite marcar, mas a esteira não publica o arquivo).
    if kit is True and avulso is True:
        return REJEITAR, "CONFLITO KIT+AVULSO", "FLAG"

    if avulso is True:
        motivo = "AVULSO (FLAG IS_AVULSO)"
        if tem_contrato or tem_item:
            motivo += "; CONTRATO/ITEM IGNORADOS"
        return AVULSO, motivo, "FLAG_AVULSO"

    if kit is True:
        if not tem_contrato:
            return REJEITAR, "CONFLITO KIT SEM CONTRATO", "FLAG"
        motivo = "KIT (FLAG IS_EXTRA_KIT)"
        if tem_item:
            motivo += "; ITEM IGNORADO"
        return KIT, motivo, "FLAG_KIT"

    if not tem_contrato:
        if avulso is False:
            return AVULSO, "AVULSO INFERIDO (flag IS_AVULSO=Não)", "SEM_CONTRATO"
        return AVULSO, "AVULSO (SEM CONTRATO)", "SEM_CONTRATO"

    if tem_item:
        return None

    if kit is False:
        return EXTRA_FALLBACK, "USUARIO MARCOU NAO E KIT", "FALLBACK"

    return None


def flags_ausentes(df):
    """Colunas de flag ausentes no DataFrame (compatibilidade com arquivo antigo)."""
    return [coluna for coluna in FLAGS if coluna not in df.columns]


def serie_booleana(serie):
    """Coluna de flags como ``boolean`` anulável, tolerando texto ("Sim"/"Não")."""
    if serie.dtype == "boolean":
        return serie
    try:
        return serie.astype("boolean")
    except (TypeError, ValueError):
        return serie.map(flag_booleana).astype("boolean")
