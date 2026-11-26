"""Importa as fichas de contrato ICÓ/MOREIA (ATA 11/2022) de docs/contratos.

As planilhas de ``docs/contratos/MOREIA`` não seguem o layout das fichas de
Tauá (IP): cada arquivo é um contrato da ATA de Registro de Preços 11/2022 do
Município de Icó, com uma aba por mês (JAN 2023 ... NOV 2026) e os itens do
Lote I/II a partir da linha 13. A dotação orçamentária ocupa de 0 a 12 colunas
entre os arquivos, portanto as colunas de quantidade, preço e meses são
localizadas pelo rótulo ``R$ GLOBAL/ ANUAL`` da linha 12 (o total fica 3
colunas à esquerda, o preço 2 e o mês 1).

Regras aplicadas:

* ``Número`` <- A9 da aba com a vigência mais recente (abas "ATA REG PREÇOS" e
  "SALDOS" não têm contrato e são ignoradas).
* ``Contratante`` <- A2 (MUNICÍPIO DE ICÓ), resolvido para a raiz
  "PREFEITURA MUNICIPAL DE ICÓ" da organização MOREIA (1122).
* ``Destinatário`` <- mapa número -> secretaria (aliases reais da org 1122).
* Vigência contratada: menor e maior data citadas em A9. Duração máxima:
  ``TOTAL <n> MESES`` de A9, com ``max_end_date`` = início + n meses - 1 dia.
* Itens: B (especificação), quantidade/preço/meses nas colunas localizadas;
  itens com quantidade <= 0 são ignorados (linhas de saldo/zeradas).
* Totais global e mensal são conferidos contra as linhas
  ``R$ VALOR GLOBAL/ANUAL`` e ``R$ VALOR MENSAL`` quando existirem.
"""

from __future__ import annotations

import glob
import os
import re
from calendar import monthrange
from datetime import date, datetime, timedelta

import openpyxl

from cadastros.contratos.planilhas_contrato_2026 import (
    ContratoPlanilha,
    ItemPlanilha,
    PASTA_PADRAO,
    PlanilhaContratoInvalida,
    _limpar,
    _validar_totais,
)
from utils.sanetizador import ultra_normalizar

ORGANIZACAO_MOREIA = 1122
CONTRATANTE_ALIAS = 'PREFEITURA MUNICIPAL DE ICÓ'
CONTRATANTE_CONTEM = 'ICÓ'
PASTA_MOREIA = 'MOREIA'
MESES_PADRAO_ITEM = 12
DURACAO_MAXIMA_PADRAO = 48

NUMERO_RE = re.compile(
    r'CONTRATO\s*N[º°Oo]\s*:?\s*([0-9]{2}/[0-9]{4}-[0-9]{2})',
    flags=re.IGNORECASE,
)
DATA_RE = re.compile(r'(\d{2})/(\d{2})/(\d{2,4})')
TOTAL_MESES_RE = re.compile(r'TOTAL\s+(\d+)\s+MESES', flags=re.IGNORECASE)

# Número do contrato -> arquivo e secretaria destinatária. Os aliases são os
# nomes reais das secretarias filhas da "PREFEITURA MUNICIPAL DE ICÓ" na
# organização 1122; a resolução é por alias normalizado, nunca por id.
SECRETARIAS = {
    '11/2022-01': {
        'arquivo_contem': 'EDUCAÇÃO',
        'destinatario_alias': (
            'SECRETARIA MUNICIPAL DE EDUCACAO DO MUNICIPIO DE ICÓ'
        ),
    },
    '11/2022-02': {
        'arquivo_contem': 'ADM E FINANÇAS',
        'destinatario_alias': (
            'SECRETARIA DE ADMINISTRAÇÃO E FINANÇAS MUNICIPIO DE ICÓ'
        ),
    },
    '11/2022-03': {
        'arquivo_contem': 'AGRICULTURA',
        'destinatario_alias': (
            'SECRETARIA DE DESENVOLVIMENTO DA AGRICULTURA E RECURSOS '
            'HÍDRICOS MUNICIPIO DE ICÓ'
        ),
    },
    '11/2022-04': {
        'arquivo_contem': 'CONTROLADORIA',
        'destinatario_alias': 'CONTROLADORIA GERAL DO MUNICÍPIO DE ICÓ',
    },
    '11/2022-05': {
        'arquivo_contem': 'PROCURADORIA',
        'destinatario_alias': 'PROCURADORIA GERAL  DO MUNICIPIO DE ICÓ',
    },
    '11/2022-06': {
        'arquivo_contem': 'GABINETE DA PREFEITA',
        'destinatario_alias': 'GABINETE DO PREFEITO DO MUNICIPIO DE ICO',
    },
    '11/2022-08': {
        'arquivo_contem': 'CULTURA E TUR',
        'destinatario_alias': (
            'SECRETARIA DE DESENVOLVIMENTO ECONÔMICO, CULTURA E TURISMO DO '
            'MUNICIPIO DE ICÓ'
        ),
    },
    '11/2022-09': {
        'arquivo_contem': 'ESPORTE',
        'destinatario_alias': (
            'SECRETARIA DO ESPORTE E JUVENTUDE DO MUNICIPIO DE ICÓ'
        ),
    },
    '11/2022-10': {
        'arquivo_contem': 'SEG PÚBLICA',
        'destinatario_alias': (
            'SECRETARIA DE SEGURANÇA PÚBLICA E CIDADANIA DO MUNICIPIO DE '
            'ICÓ/CE'
        ),
    },
    '11/2022-11': {
        'arquivo_contem': 'COTRAN',
        'destinatario_alias': (
            'CENTRO DE OPERACAO DE TRANSITO AUTARQUIA MUNICIPAL DE ICO  - CE'
        ),
    },
    '11/2022-12': {
        'arquivo_contem': 'MEIO AMBIENTE (SUDEMA)',
        'destinatario_alias': (
            'SUPERINTENDÊNCIA MUNICIPAL DE DEFESA DO MEIO AMBIENTE DE ICÓ - '
            'SUDEMA'
        ),
    },
    '11/2022-13': {
        'arquivo_contem': 'INFRAESTRUTURA',
        'destinatario_alias': (
            'SECRETARIA DE DESENVOLVIMENTO URBANO E INFRAESTRUTURA '
            'MUNICIPIO DE ICÓ'
        ),
    },
    '11/2022-14': {
        'arquivo_contem': 'SAÚDE',
        'destinatario_alias': 'SECRETARIA DE SAÚDE MUNICIPIO DE ICÓ',
    },
    '11/2022-15': {
        'arquivo_contem': 'ASSIST. SOCIAL',
        'destinatario_alias': (
            'SECRETARIA DO TRABALHO E ASSISTÊNCIA SOCIAL DO MUNICIPIO DE ICÓ'
        ),
    },
}


def _num_opt(valor):
    """Converte um valor de célula em float; devolve None quando não numérico."""
    if valor is None or valor == '':
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip().replace('R$', '').replace(' ', '')
    if not texto:
        return None
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    try:
        return float(texto)
    except ValueError:
        return None


def _converter_data(dia, mes, ano):
    texto = f'{dia}/{mes}/{ano}'
    formato = '%d/%m/%Y' if len(ano) == 4 else '%d/%m/%y'
    return datetime.strptime(texto, formato).date()


def _datas_do_cabecalho(texto):
    """Todas as datas dd/mm/aa(aa) citadas no cabeçalho A9."""
    datas = []
    for dia, mes, ano in DATA_RE.findall(texto):
        try:
            datas.append(_converter_data(dia, mes, ano))
        except ValueError:
            continue
    return datas


def _somar_meses(inicio, meses):
    """Último dia do prazo: início + meses - 1 dia."""
    total = inicio.month - 1 + int(meses)
    ano = inicio.year + total // 12
    mes = total % 12 + 1
    ultimo_dia = monthrange(ano, mes)[1]
    fim = date(ano, mes, min(inicio.day, ultimo_dia))
    return fim - timedelta(days=1)


def _pasta_moreia(pasta=PASTA_PADRAO):
    alvo = ultra_normalizar(PASTA_MOREIA)
    for entrada in sorted(os.listdir(pasta)):
        caminho = os.path.join(pasta, entrada)
        if os.path.isdir(caminho) and ultra_normalizar(entrada) == alvo:
            return caminho
    raise PlanilhaContratoInvalida(
        f'Pasta {PASTA_MOREIA!r} não encontrada em {pasta!r}.'
    )


def localizar_arquivos_moreia(pasta=PASTA_PADRAO):
    """Lista os .xlsx de docs/contratos/MOREIA (um contrato por arquivo)."""
    pasta_moreia = _pasta_moreia(pasta)
    arquivos = sorted(glob.glob(os.path.join(pasta_moreia, '*.xlsx')))
    if not arquivos:
        raise PlanilhaContratoInvalida(
            f'Nenhum .xlsx encontrado em {pasta_moreia!r}'
        )
    return arquivos


def _ler_a9(ws):
    for linha in ws.iter_rows(min_row=9, max_row=9, max_col=1, values_only=True):
        return str(linha[0] or '')
    return ''


def _selecionar_aba(wb, nome_arquivo):
    """Escolhe a aba do contrato com a vigência mais recente."""
    candidatas = []
    for indice, nome in enumerate(wb.sheetnames):
        if 'ATA REG' in ultra_normalizar(nome):
            continue
        texto = _ler_a9(wb[nome])
        match = NUMERO_RE.search(texto)
        if not match:
            continue
        datas = _datas_do_cabecalho(texto)
        if not datas:
            continue
        candidatas.append((max(datas), indice, nome, match.group(1), texto))

    if not candidatas:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: nenhuma aba com "CONTRATO Nº .../....-XX" em A9.'
        )

    numeros = {candidata[3] for candidata in candidatas}
    if len(numeros) > 1:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: abas apontam para números de contrato '
            f'diferentes: {sorted(numeros)}'
        )

    fim, _indice, nome, numero, texto = max(
        candidatas, key=lambda candidata: (candidata[0], candidata[1])
    )
    return nome, numero, texto


def _localizar_colunas(header, nome_arquivo):
    glob_col = None
    for indice, valor in enumerate(header):
        rotulo = ultra_normalizar(valor)
        if rotulo.startswith('R GLOBAL') and 'ANUAL' in rotulo:
            glob_col = indice
            break
    if glob_col is None or glob_col < 3:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: não encontrei o rótulo "R$ GLOBAL/ ANUAL" na '
            'linha 12.'
        )

    colunas = {
        'total': glob_col - 3,
        'preco': glob_col - 2,
        'meses': glob_col - 1,
    }
    esperados = {'total': 'TOTAL', 'preco': 'UNIT', 'meses': 'MES'}
    for chave, trecho in esperados.items():
        rotulo = ultra_normalizar(header[colunas[chave]])
        if trecho not in rotulo:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: coluna {chave!r} inesperada antes de '
                f'"R$ GLOBAL/ ANUAL" (rótulo {rotulo!r}).'
            )
    return colunas


def _nome_item(descricao):
    nome = re.split(r'\s*-\s+|\s+-\s*', descricao, maxsplit=1)[0].strip()
    return (nome or descricao).strip()[:255]


def ler_contrato_moreia(caminho):
    """Lê uma ficha ICÓ/MOREIA e devolve um ``ContratoPlanilha`` validado."""
    nome_arquivo = os.path.basename(caminho)
    try:
        wb = openpyxl.load_workbook(caminho, data_only=True)
    except Exception as exc:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: não foi possível abrir o arquivo ({exc})'
        ) from exc

    try:
        aba, numero, texto_a9 = _selecionar_aba(wb, nome_arquivo)
        ws = wb[aba]

        config = SECRETARIAS.get(numero)
        if config is None:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: contrato {numero!r} não está no mapa de '
                f'secretarias. Conhecidos: {sorted(SECRETARIAS)}'
            )
        chave_arquivo = ultra_normalizar(config['arquivo_contem'])
        if chave_arquivo not in ultra_normalizar(nome_arquivo):
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: o número {numero!r} pertence a '
                f'{config["arquivo_contem"]!r}, mas o nome do arquivo não '
                'corresponde.'
            )

        datas = _datas_do_cabecalho(texto_a9)
        inicio, fim = min(datas), max(datas)
        if fim <= inicio:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: vigência inválida ({inicio} a {fim}).'
            )
        duracao = (fim.year - inicio.year) * 12 + (fim.month - inicio.month) + 1

        match_meses = TOTAL_MESES_RE.search(texto_a9)
        max_duracao = (
            int(match_meses.group(1)) if match_meses else DURACAO_MAXIMA_PADRAO
        )
        if max_duracao < duracao:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: duração máxima ({max_duracao}) menor que a '
                f'vigência contratada ({duracao}).'
            )
        max_fim = _somar_meses(inicio, max_duracao)

        contratante = _limpar(ws['A2'].value)
        objeto_txt = _limpar(ws['A7'].value)
        if not contratante or not objeto_txt:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: cabeçalho incompleto (A2/A7 vazios).'
            )
        objeto = re.sub(r'^OBJETO:\s*', '', objeto_txt, flags=re.IGNORECASE)

        header = [celula.value for celula in ws[12]]
        colunas = _localizar_colunas(header, nome_arquivo)

        itens: list[ItemPlanilha] = []
        total_planilha = None
        total_mensal = None

        for linha in ws.iter_rows(
            min_row=13, max_row=ws.max_row, max_col=ws.max_column,
            values_only=True,
        ):
            valores = list(linha)
            if not valores:
                continue
            rotulo_a = ultra_normalizar(valores[0])
            valor_mes = (
                _num_opt(valores[colunas['meses']])
                if len(valores) > colunas['meses'] else None
            )
            if rotulo_a.startswith('R VALOR GLOBAL'):
                if valor_mes is not None:
                    total_planilha = valor_mes
                continue
            if rotulo_a.startswith('R VALOR MENSAL'):
                if valor_mes is not None:
                    total_mensal = valor_mes
                continue

            descricao = _limpar(valores[1]) if len(valores) > 1 else ''
            if not descricao:
                continue
            quantidade = (
                _num_opt(valores[colunas['total']])
                if len(valores) > colunas['total'] else None
            )
            if quantidade is None or quantidade <= 0:
                continue

            preco = (
                _num_opt(valores[colunas['preco']])
                if len(valores) > colunas['preco'] else None
            )
            meses = (
                _num_opt(valores[colunas['meses']])
                if len(valores) > colunas['meses'] else None
            )
            itens.append(ItemPlanilha(
                numero_tr=len(itens) + 1,
                nome=_nome_item(descricao),
                descricao=descricao,
                quantidade=int(round(quantidade)),
                preco=float(preco or 0.0),
                meses=int(round(meses or MESES_PADRAO_ITEM)),
            ))

        if not itens:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: nenhum item com quantidade positiva a '
                'partir da linha 13.'
            )

        contrato = ContratoPlanilha(
            arquivo=nome_arquivo,
            numero=numero,
            contratante=contratante,
            cnpj=None,
            objeto=objeto,
            inicio=inicio,
            fim=fim,
            duracao_meses=duracao,
            itens=itens,
            extras=[],
            total_planilha=(
                float(total_planilha)
                if total_planilha is not None
                else sum(i.quantidade * i.preco * i.meses for i in itens)
            ),
            total_mensal_planilha=float(total_mensal or 0),
            titulo_previsao=_limpar(ws['A4'].value),
            destinatario_alias=config['destinatario_alias'],
            organizacao_id=ORGANIZACAO_MOREIA,
            contratante_alias=CONTRATANTE_ALIAS,
            contratante_contem=CONTRATANTE_CONTEM,
            max_fim=max_fim,
            max_duracao_meses=max_duracao,
        )
        _validar_totais(contrato)
        return contrato
    finally:
        wb.close()


def carregar_planilhas_moreia(pasta=PASTA_PADRAO):
    """Lê e valida as 14 fichas ICÓ/MOREIA, sem tocar no banco."""
    contratos = [
        ler_contrato_moreia(caminho)
        for caminho in localizar_arquivos_moreia(pasta)
    ]

    numeros = [contrato.numero for contrato in contratos]
    repetidos = sorted({numero for numero in numeros if numeros.count(numero) > 1})
    if repetidos:
        raise PlanilhaContratoInvalida(
            f'Contratos ICÓ/MOREIA repetidos: {repetidos}'
        )
    faltando = sorted(set(SECRETARIAS) - set(numeros))
    if faltando:
        raise PlanilhaContratoInvalida(
            f'Faltam fichas para os contratos ICÓ/MOREIA: {faltando}'
        )

    print(f'\n📄 Planilhas ICÓ/MOREIA carregadas de {pasta!r}:')
    for contrato in contratos:
        print(
            f'   • [org {contrato.organizacao_id}] {contrato.numero} | '
            f'{contrato.destinatario_alias} | '
            f'{len(contrato.itens)} item(ns) | '
            f'{contrato.duracao_meses} meses '
            f'(máx. {contrato.max_duracao_meses}) | '
            f'R$ {contrato.total_planilha:,.2f}'
        )
    return contratos
