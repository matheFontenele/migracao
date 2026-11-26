"""Contrato de dados entre o app de extração e a migração de contratos.

A extração (humana + agentes) grava um JSON por contrato em
``docs/contratos_json``. A migração não lê planilhas: ela valida esses
arquivos e carrega o banco.

Este módulo concentra apenas o que é comum aos dois lados: enums, mapa de
organizações, normalização e leitura de tipos.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Resolvido a partir do repositório, não do diretório corrente.
RAIZ_PADRAO = Path(__file__).resolve().parents[2] / 'docs' / 'contratos_json'

# A pasta do JSON é a chave canônica da organização no lado da extração.
ORGANIZACOES = {
    'ALUCOM': {'cnpj': '01628251000188', 'organization_id': 1115},
    'MOREIA': {'cnpj': '03420933000126', 'organization_id': 1122},
    'IP': {'cnpj': '31810764000176', 'organization_id': 1311},
    'AS_SISTEMAS': {'cnpj': '63320667000191', 'organization_id': 1378},
}
ORGANIZACAO_POR_ID = {
    dados['organization_id']: alias for alias, dados in ORGANIZACOES.items()
}
ORGANIZACAO_POR_CNPJ = {
    dados['cnpj']: alias for alias, dados in ORGANIZACOES.items()
}
PASTAS = tuple(ORGANIZACOES)

TIPOS_CONTRATO = {
    'LICITACAO': 1,
    'PESSOA_JURIDICA': 2,
    'PESSOA_FISICA': 3,
}
STATUS_CONTRATO = {
    'AGUARDANDO_INICIO': 1,
    'EM_ANDAMENTO': 2,
    'ENCERRADO': 3,
}
TIPOS_ADITIVO = {
    'CADASTRO': 1,
    'ADITIVO_DE_PRAZO': 2,
    'ADITIVO_DE_QUANTIDADE': 3,
    'ADITIVO_DE_REAJUSTE': 4,
    'ADITIVO_DE_SUPRESSAO': 5,
    'ADITIVO_MODIFICACAO_DE_ITEM': 6,
    'APOSTILAMENTO': 7,
}
TIPOS_ADITIVO_POR_ID = {valor: chave for chave, valor in TIPOS_ADITIVO.items()}
TIPOS_CONTRATO_POR_ID = {valor: chave for chave, valor in TIPOS_CONTRATO.items()}
STATUS_CONTRATO_POR_ID = {valor: chave for chave, valor in STATUS_CONTRATO.items()}

SCHEMA_VERSION = 1


class ContratoJsonInvalido(Exception):
    """Um ou mais arquivos não respeitam o contrato de dados."""

    def __init__(self, erros):
        self.erros = list(erros)
        super().__init__(
            f'{len(self.erros)} problema(s) nos JSONs de contrato:\n'
            + '\n'.join(f'  - {erro}' for erro in self.erros)
        )


def normalizar(valor) -> str:
    """Chave estável para linhagem: sem acento, minúscula, não-alfanumérico
    vira hífen."""
    texto = unicodedata.normalize('NFKD', str(valor or ''))
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r'[^a-z0-9]+', '-', texto.casefold())
    return texto.strip('-')


def chave_alias(valor) -> str:
    """Mesma comparação usada pelos caches de clientes (caixa e acento não
    contam, pontuação vira espaço)."""
    texto = unicodedata.normalize('NFKD', str(valor or ''))
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r'[^A-Za-z0-9]+', ' ', texto)
    return ' '.join(texto.upper().split())


# De/para curado dos aliases que os JSONs usam mas que não existem como
# customer com o mesmo nome. O valor é o alias do customer que representa o
# órgão (nunca um id, que muda a cada recarga do cadastro) e a organização
# onde ele deve ser procurado. Vale para qualquer contratante; quando o mesmo
# alias aponta para clientes diferentes conforme o contratante, use
# ``DE_PARA_CLIENTES_POR_ORGANIZACAO``.
_DE_PARA_CLIENTES_BRUTO = [
    # ICÓ — a planilha traz nomes longos; os customers canônicos são os filhos
    # da raiz PREFEITURA MUNICIPAL DE ICÓ (org. 1122).
    ('MUNICÍPIO DE ICÓ', 'PREFEITURA MUNICIPAL DE ICÓ', 1122),
    ('MUNICÍPIO DE ICO', 'PREFEITURA MUNICIPAL DE ICÓ', 1122),
    ('CONTROLADORIA GERAL', 'CONTROLADORIA GERAL DO MUNICÍPIO DE ICÓ', 1122),
    ('COTRAN - CENTRO DE OPERAÇÕES DE TRÂNSITO',
     'CENTRO DE OPERACAO DE TRANSITO AUTARQUIA MUNICIPAL DE ICO  - CE', 1122),
    ('GABINETE DA PREFEITA  DO MUNICIPIO DE ICÓ/CE',
     'GABINETE DO PREFEITO DO MUNICIPIO DE ICO', 1122),
    ('PROCURADORIA GERAL  DO MUNICIPIO DE ICÓ/CE',
     'PROCURADORIA GERAL  DO MUNICIPIO DE ICÓ', 1122),
    ('SECRETARIA DE ADMINISTRAÇÃO E FINANÇAS  DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA DE ADMINISTRAÇÃO E FINANÇAS MUNICIPIO DE ICÓ', 1122),
    ('SECRETARIA DE AGRICULTURA E RECURSOS HÍDRICOS DO MUNICIPIO DE ICÓ/CE - SEDAGRH',
     'SECRETARIA DE DESENVOLVIMENTO DA AGRICULTURA E RECURSOS HÍDRICOS MUNICIPIO DE ICÓ',
     1122),
    ('SECRETARIA DE DESENV. ECONÔMICO, CULTURA E TURISMO DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA DE DESENVOLVIMENTO ECONÔMICO, CULTURA E TURISMO DO MUNICIPIO DE ICÓ',
     1122),
    ('SECRETARIA DE DESENVOLVIMENTO URBANO E INFRAESTRUTURA DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA DE DESENVOLVIMENTO URBANO E INFRAESTRUTURA MUNICIPIO DE ICÓ', 1122),
    ('SECRETARIA DE EDUCAÇÃO DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA MUNICIPAL DE EDUCACAO DO MUNICIPIO DE ICÓ', 1122),
    ('SECRETARIA DE SAÚDE DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA DE SAÚDE MUNICIPIO DE ICÓ', 1122),
    ('SECRETARIA DO ESPORTE E JUVENTUDE DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA DO ESPORTE E JUVENTUDE DO MUNICIPIO DE ICÓ', 1122),
    ('SECRETARIA DO TRABALHO E ASSISTÊNCIA SOCIAL DO MUNICIPIO DE ICÓ/CE',
     'SECRETARIA DO TRABALHO E ASSISTÊNCIA SOCIAL DO MUNICIPIO DE ICÓ', 1122),
    ('Superintendência Municipal de Defesa do Meio Ambiente - SUDEMA - DO MUNICIPIO DE ICÓ/CE',
     'SUPERINTENDÊNCIA MUNICIPAL DE DEFESA DO MEIO AMBIENTE DE ICÓ - SUDEMA', 1122),
    # ARACOIABA — aliases genéricos de secretaria; o contratante é sempre
    # PREFEITURA MUNICIPAL DE ARACOIABA nesses contratos.
    ('P.M. ARACOIABA – SEC. DE SAÚDE',
     'SECRETARIA DE SAÚDE DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE EDUCAÇÃO',
     'SECRETARIA DE EDUCAÇÃO DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE SAÚDE', 'SECRETARIA DE SAÚDE DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE SAÚDE -FMS',
     'SECRETARIA DE SAÚDE DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE ASSISTÊNCIA SOCIAL',
     'SECRETARIA DE ASSISTENCIA SOCIAL DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE PROTEÇÃO SOCIAL E CIDADANIA',
     'SECRETARIA DE ASSISTENCIA SOCIAL DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE PLANEJAMENTO E GESTÃO',
     'SECRETARIA DE PLANEJAMENTO E GESTÃO DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIADE DESENVOLVIMENTO RURAL',
     'SECRETARIA DE DESENVOLVIMENTO RURAL DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE DESENVOLVIMENTO RURAL',
     'SECRETARIA DE DESENVOLVIMENTO RURAL DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE FINANÇAS', 'SECRETARIA DE FINANÇAS DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE FINANÇAS, PLANEJAMENTO E GESTÃO',
     'SECRETARIA DE FINANÇAS, PLANEJAMENTO E GESTÃO DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE CULTURA', 'SECRETARIA DE CULTURA DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE CONTROLADORIA', 'CONTROLADORIA DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE DESENVOLVIMENTO ECONÔMICO, TRABALHO E TECNOLOGIA',
     'SECRETARIA DESENVOLVIMENTO ECONOMICO TRABALHO E TECNOLOGIA DO MUNICIPIO DE ARACOIABA',
     1122),
    ('INFRAESTRUTURA, URBANISMO E MEIO AMBIENTE',
     'SECRETARIA DE INFRAESTRUTURA DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE GABINETE, GOVERNO E SEGURANÇA',
     'GABINETE DO PREFEITO DO MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE POLITICAS PUBLICAS PARA AS MULHERES',
     'SECRETARIA DE POLITICAS PUBLICAS PARA MULHERES MUNICIPIO DE ARACOIABA', 1122),
    ('SECRETARIA DE DE GOVERNO E MOVIMENTOS SOCIAIS',
     'GABINETE DO PREFEITO DO MUNICIPIO DE ARACOIABA', 1122),
    # EUSÉBIO — customer raiz é PREFEITURA MUNICIPAL DO EUSEBIO.
    ('PREFEITURA MUNICIPAL DE EUSÉBIO', 'PREFEITURA MUNICIPAL DO EUSEBIO', 1122),
    ('SECRETARIA DE SEGURANÇA PUBLICA E CIDADANIA',
     'SECRETARIA DE SEGURANÇA PUBLICA DO MUNICIPIO DE EUSEBIO', 1122),
    ('SECRETARIA DE DESENVOLVIMENTO SOCIAL - SDS',
     'SECRETARIA DE DESENVOLVIMENTO SOCIAL  DO MUNICIPIO DE EUSEBIO', 1122),
    ('SECRETARIA DE OBRAS E SERVIÇOS PÚBLICOS',
     'SECRETARIA DE OBRAS E SERVIÇOS PÚBLICOS DO MUNICIPIO DO EUSEBIO', 1122),
    # Eusébio não tem filho de Cultura na organização 1122; o customer de
    # Cultura/Turismo do Eusébio está na organização 1115 (pendência registrada).
    ('SECRETARIA DE CULTURA E TURISMO',
     'P M DE EUSEBIO - SECRETARIA DE CULTURA E TURISMO', 1115),
    # ARACATI — destinatário da planilha; é filho da prefeitura de Aracati.
    ('PRFEITURA MUNICIPAL DE ARACATI - SECRETARIA DE SAÚDE 2 (2021) - TESTE',
     'SECRETARIA DE SAUDE DO MUNICIPIO DE ARACATI', 1115),
]

DE_PARA_CLIENTES = {
    chave_alias(alias): (alias_alvo, organization_id)
    for alias, alias_alvo, organization_id in _DE_PARA_CLIENTES_BRUTO
}

# Exceções por contratante: Moreia (1122) não tem filho de Gabinete em
# Maranguape; aponta para a própria raiz (o vínculo continua na org).
_DE_PARA_CLIENTES_POR_ORGANIZACAO_BRUTO = [
    (1115, 'GABINETE DO PREFEITO', 'GABINETE - MARANGUAPE', 1115),
    (1122, 'GABINETE DO PREFEITO', 'PREFEITURA MUNICIPAL DE MARANGUAPE', 1122),
]

DE_PARA_CLIENTES_POR_ORGANIZACAO = {
    (organization_id, chave_alias(alias)): (alias_alvo, org_alvo)
    for organization_id, alias, alias_alvo, org_alvo
    in _DE_PARA_CLIENTES_POR_ORGANIZACAO_BRUTO
}


def nome_arquivo(number, contract_id=None, duplicado=False) -> str:
    """Nome determinístico do arquivo de um contrato."""
    base = re.sub(r'[\\/:*?"<>|\s]+', '-', str(number or '').strip())
    base = re.sub(r'-{2,}', '-', base).strip('-.')
    if not base:
        base = 'SEM-NUMERO'
    if duplicado and contract_id is not None:
        base = f'{base}__id{contract_id}'
    return f'{base}.json'


def ler_data(valor, campo, erros, obrigatorio=False):
    if valor in (None, ''):
        if obrigatorio:
            erros.append(f'{campo}: data obrigatória ausente')
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        erros.append(f'{campo}: data inválida {valor!r} (esperado YYYY-MM-DD)')
        return None


def ler_inteiro(valor, campo, erros, minimo=None):
    if valor in (None, ''):
        erros.append(f'{campo}: inteiro obrigatório ausente')
        return None
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        erros.append(f'{campo}: inteiro inválido {valor!r}')
        return None
    if minimo is not None and numero < minimo:
        erros.append(f'{campo}: valor {numero} abaixo do mínimo {minimo}')
    return numero


def ler_decimal(valor, campo, erros, minimo=None):
    if valor in (None, ''):
        erros.append(f'{campo}: número obrigatório ausente')
        return None
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        erros.append(f'{campo}: número inválido {valor!r}')
        return None
    if minimo is not None and numero < Decimal(str(minimo)):
        erros.append(f'{campo}: valor {numero} abaixo do mínimo {minimo}')
    return numero


def ler_json(caminho: Path):
    with caminho.open(encoding='utf-8') as arquivo:
        return json.load(arquivo)


def escrever_json(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(dados, ensure_ascii=False, indent=2)
    with caminho.open('w', encoding='utf-8', newline='\n') as arquivo:
        arquivo.write(texto + '\n')
