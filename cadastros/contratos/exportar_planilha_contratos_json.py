"""Exporta ``docs/Contratos.xlsx`` para o JSON de contrato.

A planilha é a fonte de identidade/conteúdo dos contratos antigos; o app de
extração é a fonte dos novos. Este módulo faz o caminho contrário do loader:
lê a planilha e grava um arquivo por contrato no schema de
``contratos_json.py``, para que a carga tenha uma origem única (JSON).

Regras portadas do antigo importador direto (``migracao_contratos_HEAD.py``):

* eventos ligam ao contrato **pelo nome** (``APELIDO_CONTRATO``), nunca pelo
  id numérico da aba EVENTO;
* um evento pode gerar vários aditivos (ex.: ``ADITIVO DE REAJUSTE E PRAZO``);
* itens/serviços/informações ficam no aditivo de um tipo com dados;
* PRAZO/APOSTILAMENTO sem linhas próprias herdam itens/serviços do último
  aditivo anterior com itens e a informação do aditivo anterior;
* ``total_amount`` é inteiro (coluna INT): arredonda e registra a diferença;
* contrato sem evento CADASTRO usa a data inicial do aditivo mais antigo.

Uso::

    python -m cadastros.contratos.exportar_planilha_contratos_json \
        --ids 38,156,262 --destino docs/contratos_json

Contratos cujo ``customer_alias`` não existe em
``docs/contratos_json/_referencia/customers.csv`` vão para ``_pendentes/`` e
entram no relatório ``ajustes/contratos_pendentes_carga.csv``.
"""
from __future__ import annotations

import argparse
import csv
import math
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from cadastros.contratos import contratos_json as cj
from utils.sanetizador import limpar_valor_inteiro, limpar_valor_numerico, ultra_normalizar

RAIZ_REPO = Path(__file__).resolve().parents[2]
PLANILHA_PADRAO = RAIZ_REPO / 'docs' / 'Contratos.xlsx'
RELATORIO_PADRAO = RAIZ_REPO / 'ajustes' / 'contratos_pendentes_carga.csv'

# --- mapas da planilha (idênticos ao HEAD) ---------------------------------
MAP_EVENT_TYPES = {
    'CADASTRO': 1,
    'ADITIVO DE PRAZO': 2,
    'ADITIVO DE QUANTIDADE': 3,
    'ADITIVO DE REAJUSTE': 4,
    'ADITIVO DE SUPRESSAO': 5,
    'ADITIVO MODIFICACAO DE ITEM': 6,
    'APOSTILAMENTO': 7,
}
TERMOS_EVENT_TYPES = [
    ('PRAZO', 2),
    ('QUANTIDADE', 3),
    ('REAJUSTE', 4),
    ('SUPRESSAO', 5),
    ('MODIFICACAO DE ITEM', 6),
    ('APOSTILAMENTO', 7),
]
TIPOS_ADITIVOS_COM_DADOS = {1, 3, 4, 5, 6}
TIPOS_ADITIVOS_SEM_DADOS = {2, 7}

TIPOS_CONTRATO_PLANILHA = {
    'LICITACAO': 'LICITACAO',
    'PESSOA JURIDICA': 'PESSOA_JURIDICA',
    'PESSOA FISICA': 'PESSOA_FISICA',
}
STATUS_CONTRATO_PLANILHA = {
    'AGUARDANDO INICIO': 'AGUARDANDO_INICIO',
    'EM ANDAMENTO': 'EM_ANDAMENTO',
    'ENCERRADO': 'ENCERRADO',
}
ORGANIZACAO_PLANILHA = {
    'ALUCOM': 'ALUCOM',
    'MOREIA': 'MOREIA',
    'IP SERVICOS': 'IP',
    'AS SISTEMAS': 'AS_SISTEMAS',
}

# Contratos cujo cliente só pode ser resolvido por de/para/creation futura.
# Chave = id na planilha; valor = alias exato em ``customers``.
CLIENTE_POR_CONTRATO: dict[int, str] = {
    # CAUCAIA — a planilha traz o nome composto prefeitura+secretaria, mas a
    # migração de clientes achata as secretarias de Caucaia em endereços da
    # prefeitura única (customers 76). O contratante é a própria raiz.
    149: 'PREFEITURA MUNICIPAL DE CAUCAIA',
    150: 'PREFEITURA MUNICIPAL DE CAUCAIA',
    151: 'PREFEITURA MUNICIPAL DE CAUCAIA',
    152: 'PREFEITURA MUNICIPAL DE CAUCAIA',
    # SEAB — a planilha escreve o nome por extenso; o customer canônico é o
    # nome abreviado herdado do legado 11071.
    168: 'GOV. ESTAD. AMAPÁ - SEC. EXTRAOR. REPRESENTAÇÃO DO GOV. DO ESTADO DO AMAPÁ',
    # SEPA — a planilha traz a secretaria; o customer migrado é a
    # coordenadoria de TI dela. Existe duplicado em AS SISTEMAS (436); o
    # contrato é da IP (1311), então o alvo é o 437.
    262: 'CE - SECRETARIA DE PROTEÇÃO ANIMAL - SEPA/COORDENADORIA DE TECNOLOGIA DA INFORMAÇÃO',
}

# Contratos cujos eventos não casam pelo nome do contrato (duas cadeias de
# CADASTRO para o mesmo contratante). O pareamento foi decidido pela ordem
# dos eventos e fica registrado nas pendências.
EVENTOS_POR_CONTRATO: dict[int, list[int]] = {
    149: [486, 487, 488, 489, 490],
    150: [491, 492, 493, 494, 495],
    151: [496, 497, 498, 499],
    152: [500, 501, 502, 503, 504],
}

COLUNAS_RELATORIO = [
    'contrato_id', 'organizacao', 'numero', 'apelido', 'contratante',
    'motivo', 'pendencias', 'eventos', 'aditivos', 'itens', 'servicos',
    'valor_total', 'arquivo',
]


# --- conversões ------------------------------------------------------------
def _texto(valor) -> str:
    if valor is None:
        return ''
    try:
        if pd.isna(valor):
            return ''
    except (TypeError, ValueError):
        pass
    return str(valor)


def _limpo(valor) -> str:
    return ' '.join(_texto(valor).split())


def _data(valor):
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()[:10]
    if not texto:
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _iso(valor):
    return valor.isoformat() if valor else None


def _arredondar(valor) -> int:
    return int(Decimal(str(valor)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _preco(valor) -> float:
    numero = limpar_valor_numerico(valor)
    return float(Decimal(str(numero)).quantize(Decimal('0.0001')))


def _quantidade(valor):
    """Devolve ``(inteiro, problema)``.

    ``contract_items.quantity``/``contract_jobs.quantity`` são INT: frações e
    valores não numéricos precisam de registro para não sumirem calados.
    """
    if valor is None:
        return 0, None
    try:
        if pd.isna(valor):
            return 0, None
    except (TypeError, ValueError):
        pass
    if isinstance(valor, (int, float)):
        numero = float(valor)
        bruto = numero
    else:
        texto = str(valor).replace('R$', '').replace('.', '').replace(',', '.')
        texto = texto.replace(' ', '').strip()
        try:
            numero = float(texto)
            bruto = numero
        except ValueError:
            return 0, f'quantidade não numérica {valor!r} convertida em 0'
    inteiro = _arredondar(numero)
    if abs(bruto - inteiro) > 1e-9:
        return inteiro, f'quantidade {bruto:g} arredondada para {inteiro} (coluna INT)'
    return inteiro, None


def _resolver_tipos_evento(tipo_planilha) -> list[int]:
    tipo = ultra_normalizar(tipo_planilha)
    if not tipo:
        return [1]
    if tipo in MAP_EVENT_TYPES:
        return [MAP_EVENT_TYPES[tipo]]
    tipos = [tipo_id for termo, tipo_id in TERMOS_EVENT_TYPES if termo in tipo]
    return tipos or [1]


# --- planilha --------------------------------------------------------------
def carregar_planilha(caminho=PLANILHA_PADRAO):
    abas = pd.read_excel(
        caminho,
        sheet_name=['CONTRATOS', 'EVENTO', 'INFORMAÇÕES', 'ITENS', 'SERVICOS'],
    )
    for aba in abas.values():
        aba.columns = [str(coluna).strip() for coluna in aba.columns]
    return abas


def _linhas_por_evento(aba):
    resultado = {}
    for _, linha in aba.iterrows():
        evento = linha.get('EVENTO')
        if pd.isna(evento):
            continue
        resultado.setdefault(int(evento), []).append(linha)
    return resultado


def _aliases_validos(raiz: Path):
    caminho = Path(raiz) / '_referencia' / 'customers.csv'
    if not caminho.is_file():
        return None
    with caminho.open(encoding='utf-8') as arquivo:
        return {cj.chave_alias(linha['alias']) for linha in csv.DictReader(arquivo)}


# --- montagem --------------------------------------------------------------
def _info_da_linha(linha, pendencias):
    inicio = _data(linha.get('DATA_INICIAL'))
    if inicio is None:
        pendencias.append('linha de INFORMAÇÕES sem DATA_INICIAL; info ignorada')
        return None
    duracao = int(limpar_valor_inteiro(linha.get('DURAÇÃO (MESES)')))
    duracao_max = int(limpar_valor_inteiro(linha.get('DURAÇÃO_MAXIMA (MESES)'))) or 60
    total_bruto = limpar_valor_numerico(linha.get('VALOR TOTAL'))
    total = _arredondar(total_bruto)
    if abs(float(total_bruto) - total) > 0.005:
        pendencias.append(
            f'total_amount arredondado de {float(total_bruto):.2f} para {total} '
            '(coluna INT)'
        )
    return {
        'start_date': inicio.isoformat(),
        'end_date': _iso(_data(linha.get('DATA_FINAL'))),
        'duration': duracao,
        'max_duration': duracao_max,
        'max_end_date': _iso(_data(linha.get('DATA_FINAL_MAXIMA'))),
        'total_amount': total,
        'services_total_amount': 0.0,
    }


def _montar_linhas(linhas, info, pendencias, rotulo):
    itens = []
    ocorrencias = {}
    for linha in linhas:
        alias = _texto(linha.get('APELIDO')).strip()[:100]
        if not alias:
            pendencias.append(f'{rotulo}: linha sem APELIDO ignorada')
            continue
        chave = cj.normalizar(alias)
        ocorrencia = ocorrencias.get(chave, 0)
        ocorrencias[chave] = ocorrencia + 1
        quantidade, problema = _quantidade(linha.get('QUANTIDADE'))
        if problema:
            pendencias.append(f'{rotulo} {alias!r}: {problema}')
        itens.append({
            'alias': alias,
            'description': _texto(linha.get('DESCRICAO'))[:500],
            'quantity': quantidade,
            'price': _preco(linha.get('VALOR_UNITARIO')),
            'months': info['duration'] if info else None,
            'occurrence': ocorrencia,
            'lineage_key': f'{chave}#{ocorrencia}',
        })
    return itens


def _montar_eventos(contrato, eventos_contrato, infos, itens, jobs, pendencias):
    """Constrói a lista ``events`` do contrato e devolve os aditivos achatados.

    ``eventos_contrato`` é a lista de linhas da aba EVENTO já na ordem de
    carga (nome do contrato ou override). Duas linhas com o mesmo id (caso
    HEMOCENTRO PB) contam como dois eventos; itens/infos ficam no último
    aditivo com dados, como no HEAD.
    """
    aditivos = []          # dicts internos, na ordem de criação
    eventos_json = []

    # O alvo de itens/infos por id de evento é o último evento daquele id que
    # tenha um tipo com dados (ou o último, se nenhum tiver).
    alvo_por_id = {}
    for posicao, linha in enumerate(eventos_contrato):
        evento_id = int(linha['ID'])
        tipos = _resolver_tipos_evento(linha.get('TIPO'))
        if tipos and any(t in TIPOS_ADITIVOS_COM_DADOS for t in tipos):
            alvo_por_id[evento_id] = posicao
    for posicao, linha in enumerate(eventos_contrato):
        alvo_por_id.setdefault(int(linha['ID']), posicao)

    for posicao, linha in enumerate(eventos_contrato):
        evento_id = int(linha['ID'])
        tipos = _resolver_tipos_evento(linha.get('TIPO'))
        tipo_dados = next((t for t in tipos if t in TIPOS_ADITIVOS_COM_DADOS), None)
        info_linhas = infos.get(evento_id) or []
        info_base = (
            _info_da_linha(info_linhas[0], pendencias)
            if info_linhas and posicao == alvo_por_id.get(evento_id) else None
        )
        linhas_itens = itens.get(evento_id) or []
        linhas_jobs = jobs.get(evento_id) or []
        if posicao != alvo_por_id.get(evento_id):
            linhas_itens, linhas_jobs = [], []

        alvo_dados = tipo_dados or tipos[0]
        aditivos_evento = []
        for tipo in tipos:
            recebe_dados = tipo == alvo_dados
            # O HEAD atribui a informação ao aditivo do evento que recebe os
            # dados (``additive_lookup``). Os demais aditivos do mesmo evento
            # ficam sem ``contract_infos``; a cópia "do aditivo anterior" que
            # ele faz na criação é vazia numa carga limpa, porque as
            # informações só são gravadas ao final.
            info = info_base if recebe_dados else None
            aditivo = {
                'tipo': tipo,
                'info': info,
                'itens': [],
                'servicos': [],
                'evento_id': evento_id,
            }
            if recebe_dados:
                aditivo['itens'] = _montar_linhas(
                    linhas_itens, info, pendencias, f'evento {evento_id}/itens'
                )
                aditivo['servicos'] = _montar_linhas(
                    linhas_jobs, info, pendencias, f'evento {evento_id}/serviços'
                )
            aditivos.append(aditivo)
            aditivos_evento.append(aditivo)

        eventos_json.append({
            'sequence': len(eventos_json) + 1,
            'additives': [
                {
                    'type': cj.TIPOS_ADITIVO_POR_ID[aditivo['tipo']],
                    'info': aditivo['info'],
                    'items': aditivo['itens'],
                    'services': aditivo['servicos'],
                }
                for aditivo in aditivos_evento
            ],
        })

    _clonar_sem_dados(aditivos, pendencias)
    return eventos_json, aditivos


def _clonar_sem_dados(aditivos, pendencias):
    """PRAZO/APOSTILAMENTO sem itens herdam do último aditivo anterior com itens.

    Equivalente a ``_clonar_aditivos_sem_dados`` do HEAD: percorre os aditivos
    na ordem de criação (``event_additives.id``), ignora os que já têm item e
    copia itens e serviços do aditivo anterior mais próximo que tenha item.
    A linhagem é copiada literalmente (o gene do item original), como faz o
    ``COALESCE(root_item_id, id)``.
    """
    for indice, aditivo in enumerate(aditivos):
        if aditivo['tipo'] not in TIPOS_ADITIVOS_SEM_DADOS or aditivo['itens']:
            continue
        origem = next(
            (anterior for anterior in reversed(aditivos[:indice]) if anterior['itens']),
            None,
        )
        if origem is None:
            pendencias.append(
                f"evento {aditivo['evento_id']}: aditivo "
                f"{cj.TIPOS_ADITIVO_POR_ID[aditivo['tipo']]} sem aditivo "
                'anterior com itens para clonar'
            )
            continue
        meses = (aditivo['info'] or {}).get('duration')
        aditivo['itens'][:] = [
            {**item, 'months': meses if meses is not None else item['months']}
            for item in origem['itens']
        ]
        aditivo['servicos'][:] = [
            {**job, 'months': meses if meses is not None else job['months']}
            for job in origem['servicos']
        ]


def _data_inicial(aditivos, contrato_id, pendencias):
    for aditivo in aditivos:
        if aditivo['tipo'] == 1 and aditivo['info']:
            return aditivo['info']['start_date']
    datas = [a['info']['start_date'] for a in aditivos if a['info']]
    if datas:
        pendencias.append(
            'sem evento CADASTRO; original_start_date veio do aditivo mais antigo'
        )
        return min(datas)
    raise ValueError(
        f'contrato {contrato_id}: nenhuma DATA_INICIAL nos eventos; '
        'original_start_date é obrigatório'
    )


def montar_documento(contrato, eventos_contrato, infos, itens, jobs,
                     contagem_numero, aliases_validos=None):
    contrato_id = int(contrato['ID'])
    org_alias = ORGANIZACAO_PLANILHA.get(
        ultra_normalizar(contrato.get('CONTRATADO'))
    )
    if not org_alias:
        raise ValueError(
            f"contrato {contrato_id}: CONTRATADO "
            f"{contrato.get('CONTRATADO')!r} sem organização conhecida"
        )

    pendencias = []
    nome = _limpo(contrato.get('APELIDO_CONTRATO')).upper()
    if not nome:
        raise ValueError(f'contrato {contrato_id}: APELIDO_CONTRATO vazio')
    numero = _limpo(contrato.get('NUMERO_CONTRATO'))
    if not numero:
        numero = f'SEM_NUMERO ({nome})'[:255]
    contratante = _limpo(contrato.get('CONTRATANTE'))
    if not contratante:
        raise ValueError(f'contrato {contrato_id}: CONTRATANTE vazio')
    cliente_alias = CLIENTE_POR_CONTRATO.get(contrato_id, contratante)
    if cliente_alias != contratante:
        pendencias.append(
            f'cliente ajustado por de/para: {contratante!r} -> {cliente_alias!r}'
        )

    if contrato_id in EVENTOS_POR_CONTRATO:
        pendencias.append(
            'pareamento por ordem de evento (duas cadeias CADASTRO sem '
            'vínculo com o número do contrato)'
        )

    eventos_json, aditivos = _montar_eventos(
        contrato, eventos_contrato, infos, itens, jobs, pendencias
    )
    if not eventos_json:
        raise ValueError(f'contrato {contrato_id}: nenhum evento casado por nome')

    objeto = _texto(contrato.get('OBJETO_DO_CONTRATO'))
    objeto = objeto[:500] if objeto else 'NÃO INFORMADO'
    tipo = TIPOS_CONTRATO_PLANILHA.get(
        ultra_normalizar(contrato.get('TIPO_CONTRATO')), 'LICITACAO'
    )
    status = STATUS_CONTRATO_PLANILHA.get(
        ultra_normalizar(contrato.get('STATUS_CONTRATO')), 'EM_ANDAMENTO'
    )

    if aliases_validos is not None and cj.chave_alias(cliente_alias) not in aliases_validos:
        pendencias.append(
            f'cliente {cliente_alias!r} não existe em customers '
            '(criar customer ou definir de/para)'
        )

    # A mesma pendência pode nascer em vários aditivos (ex.: arredondamento do
    # mesmo valor herdado); o relatório lista cada motivo uma vez.
    pendencias = list(dict.fromkeys(pendencias))

    documento = {
        'schema_version': cj.SCHEMA_VERSION,
        'organization': {
            'cnpj': cj.ORGANIZACOES[org_alias]['cnpj'],
            'alias': org_alias,
        },
        'contract': {
            'status': 'OK',
            'stable_key': f'{org_alias}|{numero}|id={contrato_id}',
            'source_contract_id': contrato_id,
            'number': numero,
            'name': nome,
            'customer_alias': cliente_alias,
            'recipient_aliases': [cliente_alias],
            'contract_type': tipo,
            'contract_status': status,
            'object': objeto,
            'original_start_date': _data_inicial(
                aditivos, contrato_id, pendencias
            ),
            'created_at': None,
            'source': [{
                'file': 'docs/Contratos.xlsx',
                'detail': f'ID={contrato_id}',
            }],
            'events': eventos_json,
            'pendencias': pendencias,
        },
    }

    chave = (org_alias, cj.nome_arquivo(numero))
    nome_arquivo = cj.nome_arquivo(
        numero,
        contract_id=contrato_id,
        duplicado=contagem_numero.get(chave, 0) > 1,
    )
    return {
        'documento': documento,
        'org_alias': org_alias,
        'nome_arquivo': nome_arquivo,
        'pendencias': pendencias,
        'contrato_id': contrato_id,
    }


# --- exportação ------------------------------------------------------------
def exportar(ids, raiz=cj.RAIZ_PADRAO, planilha=PLANILHA_PADRAO,
             sobrescrever=False, dry_run=False, relatorio=RELATORIO_PADRAO):
    """Gera o JSON dos contratos pedidos. Não toca no banco."""
    abas = carregar_planilha(planilha)
    contratos = abas['CONTRATOS']
    contratos = contratos[contratos['ID'].notna()]
    if contratos['APELIDO_CONTRATO'].isna().any():
        contratos = contratos[contratos['APELIDO_CONTRATO'].notna()]

    if ids is None:
        ids = [int(valor) for valor in contratos['ID']]
    ids = [int(valor) for valor in ids]

    eventos = abas['EVENTO']
    eventos = eventos[eventos['ID'].notna() & eventos['CONTRATO'].notna()]
    eventos_por_nome = {}
    eventos_por_id = {}
    for posicao, linha in eventos.iterrows():
        eventos_por_id[int(linha['ID'])] = linha
        chave = ultra_normalizar(linha.get('CONTRATO'))
        if chave:
            eventos_por_nome.setdefault(chave, []).append(linha)

    infos = _linhas_por_evento(abas['INFORMAÇÕES'])
    itens = _linhas_por_evento(abas['ITENS'])
    jobs = _linhas_por_evento(abas['SERVICOS'])

    contagem_numero = {}
    for _, contrato in contratos.iterrows():
        org = ORGANIZACAO_PLANILHA.get(ultra_normalizar(contrato.get('CONTRATADO')))
        if not org:
            continue
        numero = _limpo(contrato.get('NUMERO_CONTRATO')) or (
            f"SEM_NUMERO ({_limpo(contrato.get('APELIDO_CONTRATO')).upper()})"[:255]
        )
        chave = (org, cj.nome_arquivo(numero))
        contagem_numero[chave] = contagem_numero.get(chave, 0) + 1

    raiz = Path(raiz)
    aliases = _aliases_validos(raiz)

    resultado = []
    stats = {'contratos': 0, 'pendentes': 0, 'eventos': 0, 'aditivos': 0,
             'infos': 0, 'itens': 0, 'servicos': 0}
    for contrato_id in ids:
        linha = contratos[contratos['ID'] == contrato_id]
        if linha.empty:
            raise ValueError(f'contrato {contrato_id} não existe na planilha')
        contrato = linha.iloc[0]

        if contrato_id in EVENTOS_POR_CONTRATO:
            eventos_contrato = []
            for evento_id in EVENTOS_POR_CONTRATO[contrato_id]:
                if evento_id not in eventos_por_id:
                    raise ValueError(
                        f'contrato {contrato_id}: evento {evento_id} do '
                        'override não existe'
                    )
                eventos_contrato.append(eventos_por_id[evento_id])
        else:
            chave = ultra_normalizar(contrato.get('APELIDO_CONTRATO'))
            eventos_contrato = eventos_por_nome.get(chave, [])

        montado = montar_documento(
            contrato, eventos_contrato, infos, itens, jobs,
            contagem_numero, aliases,
        )
        documento = montado['documento']
        pendente = any(
            'não existe em customers' in item
            for item in montado['pendencias']
        )
        pasta = '_pendentes' if pendente else ''
        caminho = raiz / pasta / montado['org_alias'] / montado['nome_arquivo'] \
            if pasta else raiz / montado['org_alias'] / montado['nome_arquivo']
        montado['arquivo'] = caminho

        if not dry_run:
            if caminho.exists() and not sobrescrever:
                raise FileExistsError(
                    f'{caminho} já existe; use --sobrescrever para substituir'
                )
            cj.escrever_json(caminho, documento)

        stats['contratos'] += 1
        stats['pendentes'] += 1 if pendente else 0
        stats['eventos'] += len(documento['contract']['events'])
        for evento in documento['contract']['events']:
            for aditivo in evento['additives']:
                stats['aditivos'] += 1
                stats['infos'] += 1 if aditivo['info'] else 0
                stats['itens'] += len(aditivo['items'])
                stats['servicos'] += len(aditivo['services'])
        resultado.append(montado)

    if not dry_run and relatorio:
        escrever_relatorio(resultado, relatorio)
    return {'contratos': resultado, 'stats': stats}


def escrever_relatorio(resultado, caminho=RELATORIO_PADRAO):
    """Grava o CSV dos contratos que ficaram em ``_pendentes``."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open('w', encoding='utf-8', newline='') as arquivo:
        escritor = csv.writer(arquivo, lineterminator='\n')
        escritor.writerow(COLUNAS_RELATORIO)
        for montado in resultado:
            if not any('não existe em customers' in p for p in montado['pendencias']):
                continue
            documento = montado['documento']
            contrato = documento['contract']
            eventos = contrato['events']
            aditivos = [a for e in eventos for a in e['additives']]
            escritor.writerow([
                montado['contrato_id'],
                montado['org_alias'],
                contrato['number'],
                contrato['name'],
                contrato['customer_alias'],
                'cliente inexistente em customers',
                ' | '.join(montado['pendencias']),
                len(eventos),
                len(aditivos),
                sum(len(a['items']) for a in aditivos),
                sum(len(a['services']) for a in aditivos),
                contrato['events'][0]['additives'][0]['info']['total_amount']
                if aditivos and aditivos[0]['info'] else '',
                f'{montado["org_alias"]}/{montado["nome_arquivo"]}',
            ])


def executar(ids=None, raiz=cj.RAIZ_PADRAO, planilha=PLANILHA_PADRAO,
             sobrescrever=False, dry_run=False, relatorio=RELATORIO_PADRAO):
    resultado = exportar(
        ids, raiz=raiz, planilha=planilha, sobrescrever=sobrescrever,
        dry_run=dry_run, relatorio=relatorio,
    )
    stats = resultado['stats']
    print('📦 Contratos exportados: ' + str(stats))
    for montado in resultado['contratos']:
        print(f"   {montado['contrato_id']:>4} -> {montado['arquivo']}")
    return resultado


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Exporta docs/Contratos.xlsx para o JSON de contrato'
    )
    parser.add_argument('--ids', default='',
                        help='ids separados por vírgula (padrão: todos)')
    parser.add_argument('--raiz', default=str(cj.RAIZ_PADRAO))
    parser.add_argument('--planilha', default=str(PLANILHA_PADRAO))
    parser.add_argument('--sobrescrever', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--relatorio', default=str(RELATORIO_PADRAO))
    args = parser.parse_args(argv)

    ids = [int(valor) for valor in args.ids.split(',') if valor.strip()] or None
    executar(
        ids,
        raiz=Path(args.raiz),
        planilha=Path(args.planilha),
        sobrescrever=args.sobrescrever,
        dry_run=args.dry_run,
        relatorio=args.relatorio,
    )


if __name__ == '__main__':
    main()
