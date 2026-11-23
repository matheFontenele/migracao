"""Importa as planilhas de controle de contrato 2026 da Prefeitura de Tauá (IP).

As planilhas de ``docs/contratos`` não têm uma linha por contrato: cada arquivo
é uma ficha mensal de um único contrato, com cabeçalho em texto corrido
(A2-A6), itens do Lote I a partir da linha 11 e itens extras/autorizados nas
colunas AA-AE. Este módulo conhece esse layout e converte cada ficha para as
tabelas de contrato do sistema refatorado.

Regras aplicadas (ver plano de importação):

* ``Número`` <- A6; ``Cláusula/Objeto`` <- A4; ``Contratante`` <- A3.
* Vigência, duração e datas <- A6.
* Itens <- A (nº do TR), B (descrição), D (quantidade), F (preço unitário
  mensal) e G (meses).
* ``Destinatário`` <- nome do arquivo/título da previsão, resolvido para a
  secretaria filha da Prefeitura de Tauá na organização IP SERVIÇOS (1311).
* ``Tipo`` = Licitação (1) e ``Status`` = Em andamento (2), equivalentes a
  "Locação"/"Ativo" nas opções reais do sistema.
* Itens extras (AA-AE) não viram itens do contrato (preço 0 e sem previsão na
  tela); são exportados para relatório de auditoria.

Os ids de cliente nunca são fixados no código: a migração de clientes pode
deslocá-los entre execuções. A resolução usa alias + organização + cliente
pai, com erro explícito quando não encontra ou encontra mais de um.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import openpyxl
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from utils.sanetizador import ultra_normalizar

ORGANIZACAO_IP = 1311
CONTRATANTE_ALIAS = 'PREFEITURA MUNICIPAL DE TAÚA'
CNPJ_ESPERADO = '07849532000147'
TIPO_LICITACAO = 1
STATUS_EM_ANDAMENTO = 2
TIPO_EVENTO_CADASTRO = 1

# A pasta de primeiro nível em docs/contratos identifica a organização dona
# dos contratos. Novas organizações devem ser adicionadas aqui antes de
# colocar planilhas na pasta correspondente.
ORGANIZACOES_POR_PASTA = {
    'ALUCOM': 1115,
    'MOREIA': 1122,
    'IP': 1311,
    'AS': 1378,
    'AS SISTEMAS': 1378,
}

_ORGANIZACOES_POR_PASTA_NORMALIZADO = {
    ultra_normalizar(pasta): organizacao
    for pasta, organizacao in ORGANIZACOES_POR_PASTA.items()
}

PASTA_PADRAO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'docs', 'contratos',
)
PASTA_AJUSTES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'ajustes',
)

# Cada arquivo é vinculado a uma secretaria filha da Prefeitura de Tauá na
# organização IP SERVIÇOS. A referência é o alias do customer, nunca o id.
PLANILHAS_2026 = (
    {
        'pasta': 'IP',
        'arquivo_contem': 'CONTROLADRIA, OUVIDORIA, TRANSPARENCIA',
        'destinatario_alias': (
            'DEPARTAMENTO DE CONTROLADORIA E OUVIDORIA DO MUNICIPIO DO TAUA'
        ),
        'titulo_chave': (
            'CONTROLADORIA, OUVIDORIA, TRANSPARENCIA E INTEGRIDADE PUBLICA'
        ),
    },
    {
        'pasta': 'IP',
        'arquivo_contem': 'DESENVOLVIMENTO RURAL E RECURSOS HIDRICOS',
        'destinatario_alias': (
            'SECRETARIA DE DESENVOLVIMENTO RURAL E RECURSOS HÍDRICOS '
            'DA PREFEITURA DE TAUA'
        ),
        'titulo_chave': 'DESENVOLVIMENTO RURAL E RECURSOS HIDRICOS',
    },
    {
        'pasta': 'IP',
        'arquivo_contem': 'GABINETE DA PREFEITA',
        'destinatario_alias': 'GABINETE DO PREFEITO DO MUNICIPIO DE TAUA',
        'titulo_chave': 'GABINETE DA PREFEITA',
    },
    {
        'pasta': 'IP',
        'arquivo_contem': 'SUPERINTENDENCIA DO MEIO AMBIENTE',
        'destinatario_alias': (
            'SUPERINTENDENCIA DO MEIO AMBIENTE DA PREFEITURA DE TAUA'
        ),
        'titulo_chave': 'MEIO AMBIENTE',
    },
)


# Destinatário específico de cada contrato, por número. Usado também pela
# sincronização de contratantes, que não deve conceder acesso a todas as
# secretarias filhas da Prefeitura de Tauá.
DESTINATARIOS_ESPECIFICOS_POR_NUMERO = {
    '20.08.002/2026': (
        'DEPARTAMENTO DE CONTROLADORIA E OUVIDORIA DO MUNICIPIO DO TAUA'
    ),
    '14.09.001/2026-SEDERHI': (
        'SECRETARIA DE DESENVOLVIMENTO RURAL E RECURSOS HÍDRICOS '
        'DA PREFEITURA DE TAUA'
    ),
    '20.08.001/2026-GABP': 'GABINETE DO PREFEITO DO MUNICIPIO DE TAUA',
    '21.08.001/2026': (
        'SUPERINTENDENCIA DO MEIO AMBIENTE DA PREFEITURA DE TAUA'
    ),
}


class PlanilhaContratoInvalida(RuntimeError):
    """A planilha não segue o layout esperado ou os dados não fecham."""


@dataclass(frozen=True)
class ItemPlanilha:
    numero_tr: int
    nome: str
    descricao: str
    quantidade: int
    preco: float
    meses: int


@dataclass(frozen=True)
class ExtraPlanilha:
    nome: str
    quantidade: int
    preco: float
    numero_tr: int = 0


@dataclass
class ContratoPlanilha:
    arquivo: str
    numero: str
    contratante: str
    cnpj: str | None
    objeto: str
    inicio: date
    fim: date
    duracao_meses: int
    itens: list[ItemPlanilha] = field(default_factory=list)
    extras: list[ExtraPlanilha] = field(default_factory=list)
    total_planilha: float = 0.0
    total_mensal_planilha: float = 0.0
    titulo_previsao: str = ''
    destinatario_alias: str = ''
    organizacao_id: int | None = None
    contratante_alias: str = CONTRATANTE_ALIAS
    contratante_contem: str = 'TAUA'
    contratante_id: int | None = None
    destinatario_id: int | None = None
    existente_id: int | None = None
    situacao: str = ''

    @property
    def contrato_nome(self):
        nome = f'LOCAÇÃO DE EQUIPAMENTOS DE INFORMÁTICA - {self.destinatario_alias}'
        return nome[:255]

    @property
    def total_calculado(self):
        return sum(i.quantidade * i.preco * i.meses for i in self.itens)

    @property
    def total_mensal_calculado(self):
        return sum(i.quantidade * i.preco for i in self.itens)

    @property
    def cnpj_divergente(self):
        return bool(self.cnpj and self.cnpj != CNPJ_ESPERADO)


def _limpar(valor):
    if valor is None:
        return ''
    return re.sub(r'\s+', ' ', str(valor)).strip()


def _numero(valor):
    if valor is None or valor == '':
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip().replace('R$', '').replace(' ', '')
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    return float(texto)


def _data(texto, origem):
    try:
        return datetime.strptime(texto, '%d/%m/%Y').date()
    except ValueError as exc:
        raise PlanilhaContratoInvalida(
            f'{origem}: data inválida "{texto}" (esperado DD/MM/AAAA)'
        ) from exc


def _meses_entre(inicio, fim):
    rd = relativedelta(fim, inicio)
    meses = rd.years * 12 + rd.months
    if rd.days:
        meses += 1
    return meses


def _organizacao_da_pasta(nome_pasta):
    chave = ultra_normalizar(nome_pasta)
    organizacao = _ORGANIZACOES_POR_PASTA_NORMALIZADO.get(chave)
    if organizacao is None:
        raise PlanilhaContratoInvalida(
            f'Pasta de organização desconhecida em docs/contratos: '
            f'{nome_pasta!r}. Pastas mapeadas: '
            f'{sorted(ORGANIZACOES_POR_PASTA)}'
        )
    return organizacao


def _localizar_arquivos(pasta=PASTA_PADRAO):
    """Casa cada configuração com um .xlsx dentro da pasta da organização.

    A estrutura esperada é ``docs/contratos/<ORGANIZACAO>/<arquivo>.xlsx``.
    A pasta de primeiro nível define a organização do contrato; arquivos
    soltos na raiz são ignorados para não misturar organizações.
    """
    arquivos = sorted(
        glob.glob(os.path.join(pasta, '**', '*.xlsx'), recursive=True)
    )
    if not arquivos:
        raise PlanilhaContratoInvalida(f'Nenhum .xlsx encontrado em {pasta!r}')

    pares = []
    usados = set()
    for config in PLANILHAS_2026:
        pasta_config = config.get('pasta')
        if not pasta_config:
            raise PlanilhaContratoInvalida(
                f'Configuração {config["arquivo_contem"]!r} sem pasta de '
                'organização definida.'
            )
        alvo_pasta = ultra_normalizar(pasta_config)
        alvo = ultra_normalizar(config['arquivo_contem'])
        encontrados = []
        for caminho in arquivos:
            relativo = os.path.relpath(caminho, pasta)
            partes = relativo.split(os.sep)
            if len(partes) < 2:
                continue
            if ultra_normalizar(partes[0]) != alvo_pasta:
                continue
            if alvo in ultra_normalizar(os.path.basename(caminho)):
                encontrados.append(caminho)
        if len(encontrados) != 1:
            raise PlanilhaContratoInvalida(
                f'Esperava exatamente um arquivo contendo '
                f'{config["arquivo_contem"]!r} em {pasta_config!r}; '
                f'encontrados: {[os.path.basename(p) for p in encontrados]}'
            )
        caminho = encontrados[0]
        if caminho in usados:
            raise PlanilhaContratoInvalida(
                f'Arquivo {os.path.basename(caminho)!r} casou com mais de '
                'uma configuração de planilha.'
            )
        usados.add(caminho)
        organizacao_id = _organizacao_da_pasta(
            os.path.relpath(caminho, pasta).split(os.sep)[0]
        )
        pares.append((config, caminho, organizacao_id))

    ignorados = [
        os.path.relpath(caminho, pasta)
        for caminho in arquivos
        if caminho not in usados
    ]
    if ignorados:
        print(f'⚠️ Planilhas ignoradas (sem configuração): {ignorados}')
    return pares


def ler_planilha(caminho, config, organizacao_id=None):
    """Lê uma ficha de controle e devolve um ``ContratoPlanilha`` validado."""
    nome_arquivo = os.path.basename(caminho)
    try:
        wb = openpyxl.load_workbook(caminho, data_only=True)
    except Exception as exc:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: não foi possível abrir o arquivo ({exc})'
        ) from exc

    ws = wb.worksheets[0]
    contratante_txt = _limpar(ws['A3'].value)
    objeto_txt = _limpar(ws['A4'].value)
    fornecedor_txt = _limpar(ws['A5'].value)
    linha6 = _limpar(ws['A6'].value)

    if not contratante_txt or not objeto_txt or not linha6:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: cabeçalho incompleto (A3/A4/A6 vazios).'
        )

    match_numero = re.search(
        r'CONTRATO\s*N[º°Oo]\s*([^\s,]+)', linha6, flags=re.IGNORECASE
    )
    match_vigencia = re.search(
        r'\((\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})\)', linha6
    )
    if not match_numero or not match_vigencia:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: não encontrei número/vigência em A6: {linha6!r}'
        )

    numero = match_numero.group(1).strip()
    inicio = _data(match_vigencia.group(1), nome_arquivo)
    fim = _data(match_vigencia.group(2), nome_arquivo)
    duracao = _meses_entre(inicio, fim)
    if duracao <= 0 or fim <= inicio:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: vigência inválida ({inicio} a {fim}).'
        )

    objeto = re.sub(r'^OBJETO:\s*', '', objeto_txt, flags=re.IGNORECASE)
    contratante = re.sub(
        r'^CONTRATANTE:\s*', '', contratante_txt, flags=re.IGNORECASE
    )
    match_cnpj = re.search(
        r'CNPJ/MF\s+sob\s+o\s+n[oº°]?\s*([\d./-]+)',
        contratante_txt, flags=re.IGNORECASE,
    )
    cnpj = re.sub(r'\D', '', match_cnpj.group(1)) if match_cnpj else None

    itens: list[ItemPlanilha] = []
    extras: list[ExtraPlanilha] = []
    total_planilha = None
    total_mensal = None

    for linha in range(11, ws.max_row + 1):
        coluna_a = _limpar(ws.cell(linha, 1).value)
        coluna_a_norm = ultra_normalizar(coluna_a)

        if coluna_a_norm.startswith('VALOR GLOBAL LOTE'):
            total_planilha = _numero(ws.cell(linha, 7).value)
            continue
        if coluna_a_norm.startswith('VALOR MENSAL'):
            total_mensal = _numero(ws.cell(linha, 7).value)
            continue
        if not re.fullmatch(r'\d+', coluna_a):
            continue

        descricao = _limpar(ws.cell(linha, 2).value)
        if not descricao:
            continue

        nome = re.split(r'\s*-\s+|\s+-\s*', descricao, maxsplit=1)[0].strip()
        quantidade = int(_numero(ws.cell(linha, 4).value))
        preco = _numero(ws.cell(linha, 6).value)
        meses = int(_numero(ws.cell(linha, 7).value) or duracao)

        if quantidade <= 0:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: item {coluna_a} sem quantidade positiva.'
            )
        if preco < 0:
            raise PlanilhaContratoInvalida(
                f'{nome_arquivo}: item {coluna_a} com preço negativo.'
            )

        itens.append(ItemPlanilha(
            numero_tr=int(coluna_a),
            nome=nome,
            descricao=descricao,
            quantidade=quantidade,
            preco=preco,
            meses=meses,
        ))

        # Colunas AA (27), AB (28) e AD (30): itens extras/autorizados.
        extra_nome = _limpar(ws.cell(linha, 27).value)
        if extra_nome:
            extras.append(ExtraPlanilha(
                nome=extra_nome,
                quantidade=int(_numero(ws.cell(linha, 28).value)),
                preco=_numero(ws.cell(linha, 30).value),
                numero_tr=int(coluna_a),
            ))

    if not itens:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: nenhum item encontrado a partir da linha 11.'
        )
    if total_planilha is None:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: não encontrei a linha "VALOR GLOBAL LOTE I".'
        )

    titulo_previsao = ''
    for linha in range(ws.max_row, 0, -1):
        valor = _limpar(ws.cell(linha, 1).value)
        if 'PREVIS' in ultra_normalizar(valor):
            titulo_previsao = valor
            break
    if not titulo_previsao:
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: não encontrei o título "PREVISÃO".'
        )

    chave_titulo = ultra_normalizar(config['titulo_chave'])
    if chave_titulo not in ultra_normalizar(titulo_previsao):
        raise PlanilhaContratoInvalida(
            f'{nome_arquivo}: o título da previsão não corresponde ao '
            f'destinatário configurado.\n  título: {titulo_previsao!r}\n'
            f'  esperado conter: {config["titulo_chave"]!r}'
        )

    contrato = ContratoPlanilha(
        arquivo=nome_arquivo,
        numero=numero,
        contratante=contratante,
        cnpj=cnpj,
        objeto=objeto,
        inicio=inicio,
        fim=fim,
        duracao_meses=duracao,
        itens=itens,
        extras=extras,
        total_planilha=float(total_planilha),
        total_mensal_planilha=float(total_mensal or 0),
        titulo_previsao=titulo_previsao,
        destinatario_alias=config['destinatario_alias'],
        organizacao_id=organizacao_id,
        contratante_alias=config.get('contratante_alias', CONTRATANTE_ALIAS),
        contratante_contem=config.get('contratante_contem', 'TAUA'),
    )

    _validar_totais(contrato)
    if fornecedor_txt and 'IP SERVI' not in ultra_normalizar(fornecedor_txt):
        print(
            f'⚠️ {nome_arquivo}: fornecedor inesperado em A5: '
            f'{fornecedor_txt!r}'
        )
    return contrato


def _validar_totais(contrato: ContratoPlanilha):
    total_calculado = contrato.total_calculado
    mensal_calculado = contrato.total_mensal_calculado

    if abs(total_calculado - contrato.total_planilha) > 0.05:
        raise PlanilhaContratoInvalida(
            f'{contrato.arquivo}: total global não fecha. '
            f'Planilha={contrato.total_planilha:.2f}, '
            f'calculado={total_calculado:.2f}'
        )
    if contrato.total_mensal_planilha and (
        abs(mensal_calculado - contrato.total_mensal_planilha) > 0.05
    ):
        raise PlanilhaContratoInvalida(
            f'{contrato.arquivo}: total mensal não fecha. '
            f'Planilha={contrato.total_mensal_planilha:.2f}, '
            f'calculado={mensal_calculado:.2f}'
        )

    nomes = [item.nome.upper() for item in contrato.itens]
    repetidos = sorted({nome for nome in nomes if nomes.count(nome) > 1})
    if repetidos:
        raise PlanilhaContratoInvalida(
            f'{contrato.arquivo}: itens com nome repetido {repetidos}; '
            'o upsert por alias ficaria ambíguo.'
        )


def carregar_planilhas(pasta=PASTA_PADRAO):
    """Lê e valida todas as planilhas configuradas, sem tocar no banco."""
    contratos = [
        ler_planilha(caminho, config, organizacao_id)
        for config, caminho, organizacao_id in _localizar_arquivos(pasta)
    ]

    chaves = [(contrato.organizacao_id, contrato.numero) for contrato in contratos]
    duplicados = sorted({chave for chave in chaves if chaves.count(chave) > 1})
    if duplicados:
        raise PlanilhaContratoInvalida(
            f'Contratos repetidos (organização, número) nas planilhas: '
            f'{duplicados}'
        )

    print(f'\n📄 Planilhas de contrato 2026 carregadas de {pasta!r}:')
    for contrato in contratos:
        print(
            f'   • [org {contrato.organizacao_id}] {contrato.numero} | '
            f'{contrato.destinatario_alias} | '
            f'{len(contrato.itens)} item(ns) | '
            f'{contrato.duracao_meses} meses | '
            f'R$ {contrato.total_planilha:,.2f}'
        )
        if contrato.cnpj_divergente:
            print(
                f'     ⚠️ CNPJ da planilha {contrato.cnpj} diverge do esperado '
                f'{CNPJ_ESPERADO}; o vínculo usa o destinatário configurado.'
            )
    return contratos


def _indice_clientes_por_organizacao(conn, organizacoes):
    """Carrega, por organização, os clientes indexados por alias normalizado."""
    organizacoes = sorted({org for org in organizacoes})
    if not organizacoes:
        return {}

    placeholders = ', '.join(f':org{i}' for i in range(len(organizacoes)))
    params = {f'org{i}': org for i, org in enumerate(organizacoes)}
    linhas = conn.execute(text(f"""
        SELECT cu.id, cu.alias, cu.parent_id, cu.organization_id,
               p.alias AS parent_alias
        FROM customers cu
        LEFT JOIN customers p ON p.id = cu.parent_id
        WHERE cu.organization_id IN ({placeholders})
          AND cu.deleted_at IS NULL
    """), params).mappings().all()

    indices: dict[int, dict[str, list]] = {}
    for linha in linhas:
        indice = indices.setdefault(int(linha['organization_id']), {})
        indice.setdefault(ultra_normalizar(linha['alias']), []).append(linha)
    return indices


def resolver_clientes(conn, contratos):
    """Resolve contratante/destinatário por alias, por organização da pasta."""
    sem_org = [c.arquivo for c in contratos if c.organizacao_id is None]
    if sem_org:
        raise PlanilhaContratoInvalida(
            'Contratos sem organização definida (verifique a pasta de '
            f'origem em docs/contratos): {sem_org}'
        )

    indices = _indice_clientes_por_organizacao(
        conn, [c.organizacao_id for c in contratos]
    )

    raizes: dict[tuple[int, str], dict] = {}

    for contrato in contratos:
        org = int(contrato.organizacao_id)
        indice = indices.get(org, {})
        if not indice:
            raise PlanilhaContratoInvalida(
                f'{contrato.arquivo}: organização {org} não possui clientes '
                'cadastrados; não é possível resolver contratante.'
            )

        termo = ultra_normalizar(contrato.contratante_contem)
        if termo and termo not in ultra_normalizar(contrato.contratante):
            raise PlanilhaContratoInvalida(
                f'{contrato.arquivo}: contratante em A3 não contém '
                f'{contrato.contratante_contem!r}: {contrato.contratante!r}'
            )

        chave_contratante = ultra_normalizar(contrato.contratante_alias)
        chave_raiz = (org, chave_contratante)
        contratante = raizes.get(chave_raiz)
        if contratante is None:
            candidatos = [
                linha for linha in indice.get(chave_contratante, [])
                if linha['parent_id'] is None
            ]
            if len(candidatos) != 1:
                raise PlanilhaContratoInvalida(
                    f'Esperava exatamente uma raiz {contrato.contratante_alias!r} '
                    f'na organização {org}; encontrados: '
                    f'{[dict(c) for c in candidatos]}'
                )
            contratante = candidatos[0]
            raizes[chave_raiz] = contratante

        chave_destino = ultra_normalizar(contrato.destinatario_alias)
        candidatos = [
            linha for linha in indice.get(chave_destino, [])
            if linha['parent_id'] == contratante['id']
        ]
        if len(candidatos) != 1:
            raise PlanilhaContratoInvalida(
                f'{contrato.arquivo}: esperava exatamente uma secretaria '
                f'{contrato.destinatario_alias!r} filha de '
                f'{contrato.contratante_alias!r} na organização {org}; '
                f'encontradas: {[dict(c) for c in candidatos]}'
            )

        contrato.contratante_id = int(contratante['id'])
        contrato.destinatario_id = int(candidatos[0]['id'])

        existentes = conn.execute(text("""
            SELECT id FROM contracts
            WHERE organization_id = :org AND number = :numero
        """), {
            'org': org,
            'numero': contrato.numero,
        }).fetchall()
        if len(existentes) > 1:
            raise PlanilhaContratoInvalida(
                f'{contrato.arquivo}: há {len(existentes)} contratos com o '
                f'número {contrato.numero!r} na organização '
                f'{org}; não é possível decidir qual atualizar.'
            )
        contrato.existente_id = int(existentes[0][0]) if existentes else None

    return contratos


def _situacao_contrato(contrato: ContratoPlanilha):
    if contrato.situacao in ('criar', 'atualizar'):
        return contrato.situacao
    return 'atualizar' if contrato.existente_id else 'criar'


def gerar_relatorio(contratos, pasta=PASTA_AJUSTES):
    """Grava o relatório do que será criado/atualizado e os extras ignorados."""
    import csv

    os.makedirs(pasta, exist_ok=True)
    caminho_contratos = os.path.join(pasta, 'contratos_planilhas_2026.csv')
    caminho_extras = os.path.join(pasta, 'contratos_planilhas_2026_extras.csv')

    with open(caminho_contratos, 'w', newline='', encoding='utf-8-sig') as fh:
        campos = [
            'arquivo', 'organizacao_id', 'numero', 'contrato_nome',
            'contratante', 'cnpj_planilha', 'cnpj_divergente', 'destinatario',
            'contratante_id', 'destinatario_id', 'data_inicio', 'data_fim',
            'duracao_meses', 'qtd_itens', 'total_mensal', 'total_global',
            'qtd_extras', 'situacao', 'contrato_id',
        ]
        escritor = csv.DictWriter(fh, fieldnames=campos)
        escritor.writeheader()
        for contrato in contratos:
            escritor.writerow({
                'arquivo': contrato.arquivo,
                'organizacao_id': contrato.organizacao_id or '',
                'numero': contrato.numero,
                'contrato_nome': contrato.contrato_nome,
                'contratante': contrato.contratante,
                'cnpj_planilha': contrato.cnpj or '',
                'cnpj_divergente': 'S' if contrato.cnpj_divergente else 'N',
                'destinatario': contrato.destinatario_alias,
                'contratante_id': contrato.contratante_id or '',
                'destinatario_id': contrato.destinatario_id or '',
                'data_inicio': contrato.inicio.isoformat(),
                'data_fim': contrato.fim.isoformat(),
                'duracao_meses': contrato.duracao_meses,
                'qtd_itens': len(contrato.itens),
                'total_mensal': f'{contrato.total_mensal_calculado:.2f}',
                'total_global': f'{contrato.total_calculado:.2f}',
                'qtd_extras': len(contrato.extras),
                'situacao': _situacao_contrato(contrato),
                'contrato_id': contrato.existente_id or '',
            })

    with open(caminho_extras, 'w', newline='', encoding='utf-8-sig') as fh:
        campos = [
            'numero', 'destinatario', 'arquivo',
            'numero_item_tr', 'nome', 'quantidade', 'preco',
        ]
        escritor = csv.DictWriter(fh, fieldnames=campos)
        escritor.writeheader()
        for contrato in contratos:
            for extra in contrato.extras:
                escritor.writerow({
                    'numero': contrato.numero,
                    'destinatario': contrato.destinatario_alias,
                    'arquivo': contrato.arquivo,
                    'numero_item_tr': extra.numero_tr,
                    'nome': extra.nome,
                    'quantidade': extra.quantidade,
                    'preco': f'{extra.preco:.2f}',
                })

    print(f'\n📊 Relatório de contratos: {caminho_contratos}')
    print(f'📊 Relatório de extras não importados: {caminho_extras}')
    return caminho_contratos, caminho_extras


def _obter_ou_criar_evento_cadastro(conn, contract_id, now, stats):
    evento = conn.execute(text("""
        SELECT id FROM contract_events
        WHERE contract_id = :contract_id AND deleted_at IS NULL
        ORDER BY id LIMIT 1
    """), {'contract_id': contract_id}).fetchone()
    if evento:
        event_id = int(evento[0])
    else:
        resultado = conn.execute(text("""
            INSERT INTO contract_events (contract_id, created_at, updated_at)
            VALUES (:contract_id, :now, :now)
        """), {'contract_id': contract_id, 'now': now})
        event_id = int(resultado.lastrowid)
        stats['eventos_criados'] += 1

    aditivo = conn.execute(text("""
        SELECT id FROM event_additives
        WHERE event_id = :event_id
          AND contract_event_type_id = :tipo
          AND deleted_at IS NULL
        ORDER BY id LIMIT 1
    """), {
        'event_id': event_id,
        'tipo': TIPO_EVENTO_CADASTRO,
    }).fetchone()
    if aditivo:
        return event_id, int(aditivo[0])

    resultado = conn.execute(text("""
        INSERT INTO event_additives
            (event_id, contract_event_type_id, created_at, updated_at)
        VALUES (:event_id, :tipo, :now, :now)
    """), {
        'event_id': event_id,
        'tipo': TIPO_EVENTO_CADASTRO,
        'now': now,
    })
    stats['aditivos_criados'] += 1
    return event_id, int(resultado.lastrowid)


def _salvar_info(conn, additive_id, contrato, now, stats):
    existente = conn.execute(text("""
        SELECT id FROM contract_infos
        WHERE event_additive_id = :additive_id AND deleted_at IS NULL
        ORDER BY id LIMIT 1
    """), {'additive_id': additive_id}).fetchone()

    dados = {
        'additive_id': additive_id,
        'start_date': contrato.inicio,
        'end_date': contrato.fim,
        'max_end_date': contrato.fim,
        'duration': contrato.duracao_meses,
        'max_duration': contrato.duracao_meses,
        'total_amount': int(round(contrato.total_calculado)),
        'now': now,
    }

    if existente:
        conn.execute(text("""
            UPDATE contract_infos
            SET start_date = :start_date, end_date = :end_date,
                max_end_date = :max_end_date, duration = :duration,
                max_duration = :max_duration, total_amount = :total_amount,
                updated_at = :now
            WHERE id = :id
        """), {**dados, 'id': int(existente[0])})
        stats['infos_atualizadas'] += 1
    else:
        conn.execute(text("""
            INSERT INTO contract_infos
                (event_additive_id, start_date, end_date, max_end_date,
                 duration, max_duration, total_amount, created_at, updated_at)
            VALUES
                (:additive_id, :start_date, :end_date, :max_end_date,
                 :duration, :max_duration, :total_amount, :now, :now)
        """), dados)
        stats['infos_criadas'] += 1


def _salvar_itens(conn, additive_id, contrato, now, stats):
    # Um mesmo nome pode aparecer mais de uma vez (faixas de preço distintas):
    # a chave é nome + ocorrência da linha, nunca o nome isolado.
    ocorrencias = {}
    for item in contrato.itens:
        chave = str(item.nome).strip().casefold()
        ocorrencia = ocorrencias.get(chave, 0)
        ocorrencias[chave] = ocorrencia + 1

        existente = conn.execute(text("""
            SELECT id FROM contract_items
            WHERE event_additive_id = :additive_id
              AND alias = :alias
              AND deleted_at IS NULL
            ORDER BY id LIMIT 1 OFFSET :ocorrencia
        """), {
            'additive_id': additive_id,
            'alias': item.nome,
            'ocorrencia': ocorrencia,
        }).fetchone()

        dados = {
            'additive_id': additive_id,
            'alias': item.nome,
            'description': item.descricao,
            'quantity': item.quantidade,
            'available_quantity': item.quantidade,
            'price': item.preco,
            'now': now,
        }

        if existente:
            conn.execute(text("""
                UPDATE contract_items
                SET description = :description, quantity = :quantity,
                    available_quantity = :available_quantity,
                    price = :price, updated_at = :now
                WHERE id = :id
            """), {**dados, 'id': int(existente[0])})
            stats['itens_atualizados'] += 1
        else:
            resultado = conn.execute(text("""
                INSERT INTO contract_items
                    (event_additive_id, alias, description, quantity,
                     available_quantity, price, created_at, updated_at)
                VALUES
                    (:additive_id, :alias, :description, :quantity,
                     :available_quantity, :price, :now, :now)
            """), dados)
            item_id = int(resultado.lastrowid)
            conn.execute(text("""
                UPDATE contract_items SET root_item_id = :item_id
                WHERE id = :item_id
            """), {'item_id': item_id})
            stats['itens_criados'] += 1


def _vincular_destinatarios(conn, contract_id, contrato):
    for customer_id in (contrato.contratante_id, contrato.destinatario_id):
        conn.execute(text("""
            INSERT IGNORE INTO contract_recipient_customers
                (contract_id, customer_id, created_at, updated_at)
            VALUES (:contract_id, :customer_id, NULL, NULL)
        """), {
            'contract_id': contract_id,
            'customer_id': int(customer_id),
        })


def importar_contratos(conn, contratos, now):
    """Grava os contratos das planilhas na conexão/transação recebida."""
    from collections import defaultdict

    stats = defaultdict(int)

    for contrato in contratos:
        if contrato.contratante_id is None or contrato.destinatario_id is None:
            raise RuntimeError(
                f'{contrato.arquivo}: clientes não resolvidos; chame '
                'resolver_clientes() antes de importar.'
            )

        existente = conn.execute(text("""
            SELECT id FROM contracts
            WHERE organization_id = :org AND number = :numero
            ORDER BY id LIMIT 1
        """), {
            'org': contrato.organizacao_id,
            'numero': contrato.numero,
        }).fetchone()

        dados = {
            'name': contrato.contrato_nome,
            'number': contrato.numero,
            'contract_type_id': TIPO_LICITACAO,
            'contract_status_id': STATUS_EM_ANDAMENTO,
            'organization_id': contrato.organizacao_id,
            'customer_id': contrato.contratante_id,
            'object': contrato.objeto,
            'original_start_date': contrato.inicio,
            'now': now,
        }

        if existente:
            contract_id = int(existente[0])
            contrato.situacao = 'atualizar'
            conn.execute(text("""
                UPDATE contracts
                SET name = :name, number = :number,
                    contract_type_id = :contract_type_id,
                    contract_status_id = :contract_status_id,
                    organization_id = :organization_id,
                    customer_id = :customer_id, object = :object,
                    original_start_date = :original_start_date,
                    updated_at = :now
                WHERE id = :id
            """), {**dados, 'id': contract_id})
            stats['contratos_atualizados'] += 1
        else:
            resultado = conn.execute(text("""
                INSERT INTO contracts
                    (name, number, contract_type_id, contract_status_id,
                     organization_id, customer_id, object,
                     original_start_date, created_at, updated_at)
                VALUES
                    (:name, :number, :contract_type_id, :contract_status_id,
                     :organization_id, :customer_id, :object,
                     :original_start_date, :now, :now)
            """), dados)
            contract_id = int(resultado.lastrowid)
            contrato.situacao = 'criar'
            stats['contratos_criados'] += 1

        contrato.existente_id = contract_id

        _, additive_id = _obter_ou_criar_evento_cadastro(
            conn, contract_id, now, stats
        )
        _salvar_info(conn, additive_id, contrato, now, stats)
        _salvar_itens(conn, additive_id, contrato, now, stats)
        _vincular_destinatarios(conn, contract_id, contrato)

        print(
            f'   ✓ {contrato.numero}: contrato {contract_id}, '
            f'{len(contrato.itens)} item(ns), destinatário '
            f'{contrato.destinatario_alias} ({contrato.destinatario_id})'
        )

    stats['extras_ignorados'] = sum(len(c.extras) for c in contratos)
    return dict(stats)
