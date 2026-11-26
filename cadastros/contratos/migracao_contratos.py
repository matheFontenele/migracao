"""Etapa de contratos da esteira.

A responsabilidade de extrair/tratar contratos saiu da migração. O que este
módulo faz agora é delegar ao carregador de JSON:

    docs/contratos_json/<ORG>/<numero>.json  ->  importar_contratos_json

Os parsers de planilha continuam no repositório apenas como referência de
regra de negócio (``planilhas_contrato_2026.py``, ``planilhas_moreia_ico.py``),
mas não são chamados por nenhuma etapa da esteira.
"""
from cadastros.contratos import importar_contratos_json

# Mantido para quem importava a lista daqui.
TABELAS_CONTRATOS = importar_contratos_json.TABELAS_CONTRATOS


def executar(eng_novo, eng_legado=None, argumentos_cli=None):
    importar_contratos_json.executar(eng_novo, eng_legado, argumentos_cli)
