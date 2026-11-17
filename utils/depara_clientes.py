"""DE/PARA de clientes duplicados mesclados na migração.

O relatório de similaridade gera ``ajustes/clientes_de_para_duplicados.csv``
com os pares cópia -> principal. Este módulo centraliza a leitura desse
arquivo para que clientes, contratos e movimentos resolvam o ID legado da
cópia para o endereço do principal.
"""

import csv
import os

from config.config import (
    DEPARA_CLIENTES_HIERARQUIA_DELETADA,
    DEPARA_CLIENTES_SEM_ENDERECO,
)

CAMINHO_DEPARA_DUPLICADOS = os.path.join(
    'ajustes', 'clientes_de_para_duplicados.csv'
)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolver_caminho(caminho):
    """Aceita caminho absoluto ou relativo à raiz do projeto."""
    if os.path.isabs(caminho) or os.path.exists(caminho):
        return caminho
    alternativo = os.path.join(BASE_DIR, caminho)
    return alternativo if os.path.exists(alternativo) else caminho


def carregar_depara_duplicados(caminho=None):
    caminho = _resolver_caminho(caminho or CAMINHO_DEPARA_DUPLICADOS)
    if not os.path.exists(caminho):
        return {}

    depara = {}
    with open(caminho, encoding='utf-8-sig', newline='') as arquivo:
        for linha in csv.DictReader(arquivo):
            try:
                copia = int(linha['id_legado_copia'])
                principal = int(linha['id_legado_principal'])
            except (KeyError, TypeError, ValueError):
                continue
            if copia != principal:
                depara[copia] = principal
    return depara


def propagar_depara_duplicados(mapa, caminho=None):
    """Faz o ID da cópia apontar para o mesmo valor do principal.

    Além das cópias mescladas por similaridade (CSV), aplica o de/para
    explícito de clientes ativos cuja hierarquia legada foi excluída. O CSV
    tem precedência caso o mesmo cliente apareça nos dois lugares.
    """
    depara = dict(DEPARA_CLIENTES_HIERARQUIA_DELETADA)
    depara.update(DEPARA_CLIENTES_SEM_ENDERECO)
    depara.update(carregar_depara_duplicados(caminho))
    for copia, principal in depara.items():
        if copia not in mapa and principal in mapa:
            mapa[copia] = mapa[principal]
    return mapa
