"""DE/PARA de clientes duplicados mesclados na migração.

O relatório de similaridade gera ``ajustes/clientes_de_para_duplicados.csv``
com os pares cópia -> principal. Este módulo centraliza a leitura desse
arquivo (e das regras explícitas) para que clientes, contratos, movimentos e
a importação de delta resolvam o ID legado da cópia para o principal.
"""

import csv
import os

from config.config import (
    DEPARA_CLIENTES_HIERARQUIA_DELETADA,
    DEPARA_CLIENTES_SEM_ENDERECO,
    MESCLAGENS_CLIENTE,
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


def depara_mesclagens_cliente():
    """Pares cópia -> principal dos clientes unificados por exceção.

    Só os grupos de ``MESCLAGENS_CLIENTE`` entram aqui: reservas seguem a
    regra de ``reserved_customer_id`` e não são remapeadas.
    """
    depara = {}
    for config in MESCLAGENS_CLIENTE.values():
        principal = int(config['id_alvo'])
        for id_legado in config['ids']:
            id_legado = int(id_legado)
            if id_legado != principal:
                depara[id_legado] = principal
    return depara


def carregar_depara_duplicados(caminho=None):
    """Lê somente o CSV gerado pelo merge de duplicados."""
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


def carregar_depara_clientes(caminho=None):
    """De/para completo: regras explícitas + CSV de duplicados mesclados."""
    depara = dict(DEPARA_CLIENTES_HIERARQUIA_DELETADA)
    depara.update(DEPARA_CLIENTES_SEM_ENDERECO)
    depara.update(depara_mesclagens_cliente())
    depara.update(carregar_depara_duplicados(caminho))
    return depara


def propagar_depara_duplicados(mapa, caminho=None):
    """Faz o ID da cópia apontar para o mesmo valor do principal.

    Aplica as regras explícitas (hierarquia excluída, cliente sem endereço e
    ``MESCLAGENS_CLIENTE``) e o CSV de duplicados mesclados por similaridade.
    O CSV tem precedência caso o mesmo cliente apareça nos dois lugares.
    """
    for copia, principal in carregar_depara_clientes(caminho).items():
        if copia not in mapa and principal in mapa:
            mapa[copia] = mapa[principal]
    return mapa
