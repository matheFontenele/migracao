"""Carrega no banco refatorado os contratos descritos em JSON.

A migração não lê mais planilhas: o app de extração entrega um arquivo por
contrato em ``docs/contratos_json/<ORG>/`` e este módulo valida e carrega.

Ordem de trabalho:

1. valida TUDO antes de tocar no banco (um JSON quebrado não apaga dados);
2. resolve clientes por alias (nunca por id);
3. trunca as 7 tabelas de contrato;
4. insere na ordem determinística ``(organização, número, id de origem)``;
5. reconstrói ``root_item_id``/``root_job_id`` a partir de ``lineage_key``.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import text

from cadastros.contratos import contratos_json as cj

TABELAS_CONTRATOS = [
    'contracts',
    'contract_items',
    'contract_infos',
    'contract_jobs',
    'event_additives',
    'contract_events',
    'contract_recipient_customers',
]

STATUS_VALIDOS = {'OK', 'PENDENTE'}

RELATORIO_CLIENTES = (
    Path(__file__).resolve().parents[2] / 'ajustes' / 'contratos_de_para_clientes.csv'
)

COLUNAS_RELATORIO_CLIENTES = [
    'contrato_arquivo', 'contrato_numero', 'organization_id', 'campo',
    'alias', 'customer_id', 'customer_alias', 'customer_organization_id',
    'origem',
]


def _agora():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _total_inteiro(valor, rotulo, avisos):
    """Converte ``total_amount`` para INT como o MySQL faria (half-up).

    Os JSONs da extração antiga trazem centavos (ex.: 212838.6). Deixar o
    banco arredondar calado tornaria a carga dependente do dialeto; o valor
    é arredondado aqui e a diferença vira aviso.
    """
    numero = Decimal(str(valor))
    inteiro = int(numero.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if numero != inteiro:
        avisos.append(
            f'{rotulo}: total_amount {numero} arredondado para {inteiro} '
            '(coluna INT)'
        )
    return inteiro


def _truncar(engine):
    with engine.begin() as conn:
        if engine.dialect.name == 'mysql':
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 0'))
            for tabela in TABELAS_CONTRATOS:
                conn.execute(text(f'TRUNCATE TABLE `{tabela}`'))
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 1'))
        else:
            for tabela in TABELAS_CONTRATOS:
                conn.execute(text(f'DELETE FROM "{tabela}"'))


def _listar_arquivos(raiz: Path):
    arquivos, erros = [], []
    if not raiz.is_dir():
        return [], [f'{raiz}: diretório de contratos JSON não existe']

    for pasta in cj.PASTAS:
        diretorio = raiz / pasta
        if not diretorio.is_dir():
            erros.append(f'{pasta}/: pasta ausente em {raiz}')
            continue
        for caminho in sorted(diretorio.iterdir()):
            if caminho.is_dir():
                erros.append(f'{caminho}: diretório inesperado dentro da organização')
            elif caminho.suffix.lower() != '.json':
                erros.append(f'{caminho}: arquivo inesperado (só .json é aceito)')
            else:
                arquivos.append(caminho)
    return arquivos, erros


def _validar_info(info, prefixo, erros):
    if info is None:
        return
    if not isinstance(info, dict):
        erros.append(f'{prefixo}.info: deve ser objeto ou null')
        return
    cj.ler_data(info.get('start_date'), f'{prefixo}.info.start_date', erros, True)
    cj.ler_inteiro(info.get('duration'), f'{prefixo}.info.duration', erros, 0)
    cj.ler_inteiro(info.get('max_duration'), f'{prefixo}.info.max_duration', erros, 0)
    cj.ler_inteiro(info.get('total_amount'), f'{prefixo}.info.total_amount', erros, 0)
    cj.ler_data(info.get('end_date'), f'{prefixo}.info.end_date', erros)
    cj.ler_data(info.get('max_end_date'), f'{prefixo}.info.max_end_date', erros)
    if info.get('services_total_amount') is not None:
        cj.ler_decimal(
            info['services_total_amount'],
            f'{prefixo}.info.services_total_amount', erros, 0,
        )


def _validar_linhas(linhas, prefixo, erros, avisos):
    if not isinstance(linhas, list):
        erros.append(f'{prefixo}: deve ser lista')
        return
    vistos = set()
    for indice, linha in enumerate(linhas):
        campo = f'{prefixo}[{indice}]'
        if not isinstance(linha, dict):
            erros.append(f'{campo}: deve ser objeto')
            continue
        alias = str(linha.get('alias') or '').strip()
        if not alias:
            erros.append(f'{campo}.alias: obrigatório')
        elif len(alias) > 255:
            erros.append(f'{campo}.alias: {len(alias)} caracteres (máximo 255)')
        if not isinstance(linha.get('description', ''), str):
            erros.append(f'{campo}.description: deve ser texto')
        cj.ler_inteiro(linha.get('quantity'), f'{campo}.quantity', erros, 0)
        cj.ler_decimal(linha.get('price'), f'{campo}.price', erros, 0)
        if linha.get('months') is not None:
            cj.ler_inteiro(linha.get('months'), f'{campo}.months', erros, 0)
        ocorrencia = cj.ler_inteiro(
            linha.get('occurrence'), f'{campo}.occurrence', erros, 0
        )
        lineage = str(linha.get('lineage_key') or '').strip()
        if not lineage:
            erros.append(f'{campo}.lineage_key: obrigatório')
        chave = (cj.chave_alias(alias), ocorrencia)
        if chave in vistos:
            # Duas linhas do mesmo item no mesmo aditivo existem na base
            # (quantidade duplicada na fonte). Elas compartilham a linhagem.
            avisos.append(
                f'{campo}: alias {alias!r} repetido com occurrence '
                f'{ocorrencia} (mesma linhagem)'
            )
        vistos.add(chave)
        if linha.get('price') is not None and not linha.get('price'):
            avisos.append(f'{campo}: item sem preço ({alias!r})')


def _validar_documento(caminho: Path, pasta: str, documento, erros, avisos):
    rotulo = str(caminho)
    if not isinstance(documento, dict):
        erros.append(f'{rotulo}: conteúdo deve ser um objeto JSON')
        return None

    if documento.get('schema_version') != cj.SCHEMA_VERSION:
        erros.append(
            f'{rotulo}: schema_version {documento.get("schema_version")!r} '
            f'(esperado {cj.SCHEMA_VERSION})'
        )

    organizacao = documento.get('organization') or {}
    cnpj_esperado = cj.ORGANIZACOES[pasta]['cnpj']
    if str(organizacao.get('cnpj')) != cnpj_esperado:
        erros.append(
            f'{rotulo}: organization.cnpj {organizacao.get("cnpj")!r} não é o '
            f'da pasta {pasta} ({cnpj_esperado})'
        )

    contrato = documento.get('contract')
    if not isinstance(contrato, dict):
        erros.append(f'{rotulo}: contract ausente')
        return None

    status = contrato.get('status')
    if status not in STATUS_VALIDOS:
        erros.append(f'{rotulo}: contract.status {status!r} inválido')
    elif status != 'OK':
        erros.append(
            f'{rotulo}: contrato marcado como {status} não pode ficar na '
            f'pasta {pasta} (mova para _pendentes)'
        )
        return None

    for campo in ('stable_key', 'number', 'name', 'customer_alias',
                  'contract_type', 'contract_status', 'object',
                  'original_start_date'):
        if contrato.get(campo) in (None, ''):
            erros.append(f'{rotulo}: contract.{campo} obrigatório')

    nome = str(contrato.get('name') or '')
    if len(nome) > 255:
        erros.append(f'{rotulo}: contract.name com {len(nome)} caracteres')
    if contrato.get('contract_type') not in cj.TIPOS_CONTRATO:
        erros.append(
            f'{rotulo}: contract_type {contrato.get("contract_type")!r} inválido'
        )
    if contrato.get('contract_status') not in cj.STATUS_CONTRATO:
        erros.append(
            f'{rotulo}: contract_status {contrato.get("contract_status")!r} inválido'
        )

    cj.ler_data(
        contrato.get('original_start_date'),
        f'{rotulo}: original_start_date', erros, True,
    )
    if contrato.get('created_at') is not None:
        cj.ler_data(contrato.get('created_at'), f'{rotulo}: created_at', erros)

    destinatarios = contrato.get('recipient_aliases')
    if not isinstance(destinatarios, list) or not destinatarios:
        erros.append(f'{rotulo}: recipient_aliases deve ser lista não vazia')
    else:
        normalizados = [cj.chave_alias(alias) for alias in destinatarios]
        if len(normalizados) != len(set(normalizados)):
            avisos.append(f'{rotulo}: recipient_aliases com alias repetido')

    eventos = contrato.get('events')
    if not isinstance(eventos, list) or not eventos:
        erros.append(f'{rotulo}: events deve ser lista não vazia')
        return contrato

    sequencias = set()
    for indice, evento in enumerate(eventos):
        prefixo = f'{rotulo}: events[{indice}]'
        if not isinstance(evento, dict):
            erros.append(f'{prefixo}: deve ser objeto')
            continue
        sequencia = cj.ler_inteiro(evento.get('sequence'), f'{prefixo}.sequence',
                                   erros, 1)
        if sequencia in sequencias:
            erros.append(f'{prefixo}.sequence: {sequencia} repetida no contrato')
        sequencias.add(sequencia)

        aditivos = evento.get('additives')
        if not isinstance(aditivos, list) or not aditivos:
            erros.append(f'{prefixo}.additives: lista não vazia obrigatória')
            continue
        for posicao, aditivo in enumerate(aditivos):
            campo = f'{prefixo}.additives[{posicao}]'
            if not isinstance(aditivo, dict):
                erros.append(f'{campo}: deve ser objeto')
                continue
            if aditivo.get('type') not in cj.TIPOS_ADITIVO:
                erros.append(f'{campo}.type: {aditivo.get("type")!r} inválido')
            _validar_info(aditivo.get('info'), campo, erros)
            _validar_linhas(aditivo.get('items') or [], f'{campo}.items',
                            erros, avisos)
            _validar_linhas(aditivo.get('services') or [], f'{campo}.services',
                            erros, avisos)

            info = aditivo.get('info')
            itens = aditivo.get('items') or []
            if isinstance(info, dict) and info.get('total_amount') and itens:
                duracao = info.get('duration') or 0
                calculado = sum(
                    float(item.get('quantity') or 0)
                    * float(item.get('price') or 0)
                    * float(item.get('months') or duracao)
                    for item in itens
                    if isinstance(item, dict)
                )
                if abs(calculado - float(info['total_amount'])) > 0.01:
                    avisos.append(
                        f'{campo}: total declarado {info["total_amount"]} difere '
                        f'de Σ(qtd×preço×meses)={calculado:.2f}'
                    )

    return contrato


def carregar(raiz=cj.RAIZ_PADRAO, ignorar_pendentes=False):
    """Lê e valida todos os arquivos. Não toca no banco."""
    raiz = Path(raiz)
    erros = []
    avisos = []

    pendentes = sorted((raiz / '_pendentes').rglob('*.json')) \
        if (raiz / '_pendentes').is_dir() else []
    if pendentes and ignorar_pendentes:
        avisos.append(
            f'_pendentes/: {len(pendentes)} contrato(s) fora da carga '
            '(--ignorar-pendentes)'
        )
    elif pendentes:
        erros.append(
            f'_pendentes/ tem {len(pendentes)} arquivo(s): '
            + ', '.join(str(p.relative_to(raiz)) for p in pendentes[:5])
        )

    arquivos, erros_arquivos = _listar_arquivos(raiz)
    erros.extend(erros_arquivos)

    contratos = []
    chaves = {}
    for caminho in arquivos:
        pasta = caminho.parent.name
        try:
            documento = cj.ler_json(caminho)
        except Exception as exc:
            erros.append(f'{caminho}: JSON inválido ({exc})')
            continue

        contrato = _validar_documento(caminho, pasta, documento, erros, avisos)
        if contrato is None:
            continue

        chave = contrato.get('stable_key')
        if chave in chaves:
            erros.append(
                f'{caminho}: stable_key {chave!r} repetida '
                f'(já usada em {chaves[chave]})'
            )
        else:
            chaves[chave] = caminho

        contratos.append({
            'arquivo': caminho,
            'org_alias': pasta,
            'org_id': cj.ORGANIZACOES[pasta]['organization_id'],
            'dados': contrato,
        })

    if erros:
        raise cj.ContratoJsonInvalido(erros)

    return contratos, avisos


def _resolver_clientes(conn):
    """Indexa customers por alias e por id.

    O mesmo alias pode existir em mais de uma organização (as bases foram
    separadas por contratante), então o índice por alias guarda uma lista;
    a escolha acontece em ``_selecionar_cliente``, com preferência pela
    organização do contrato.
    """
    linhas = conn.execute(text("""
        SELECT id, alias, parent_id, organization_id
        FROM customers
        WHERE deleted_at IS NULL
    """)).mappings().all()
    por_chave = defaultdict(list)
    por_id = {}
    for linha in linhas:
        por_chave[cj.chave_alias(linha['alias'])].append(linha)
        por_id[linha['id']] = linha
    return por_chave, por_id


def _resolver_alias_alvo(por_chave, alias_alvo, org_alvo):
    """Localiza o customer alvo do de/para curado pelo alias.

    Prefere a organização configurada (o de/para é estável entre recargas;
    ids de customer não são). Se o alias não existir mais na organização
    configurada, aceita o customer de outra organização avisando, para não
    quebrar a carga por uma mudança de organização.
    """
    candidatos = por_chave.get(cj.chave_alias(alias_alvo)) or []
    mesma_org = [
        linha for linha in candidatos
        if linha['organization_id'] == org_alvo
    ]
    if len(mesma_org) == 1:
        return mesma_org[0], None
    if len(mesma_org) > 1:
        escolhido = min(mesma_org, key=lambda linha: linha['id'])
        return escolhido, (
            f'alias alvo {alias_alvo!r} tem {len(mesma_org)} customers na '
            f'organização {org_alvo}; usando o menor id ({escolhido["id"]})'
        )
    if len(candidatos) == 1:
        return candidatos[0], (
            f'alias alvo {alias_alvo!r} não existe na organização '
            f'{org_alvo}; usando o customer {candidatos[0]["id"]} de outra '
            'organização'
        )
    if candidatos:
        escolhido = min(candidatos, key=lambda linha: linha['id'])
        organizacoes = sorted({linha['organization_id'] for linha in candidatos})
        return escolhido, (
            f'alias alvo {alias_alvo!r} não existe na organização '
            f'{org_alvo}; existe em {organizacoes}. Usando o menor id '
            f'({escolhido["id"]}) — revisar de/para'
        )
    return None, None


def _selecionar_cliente(por_chave, alias, org_id, campo, erros, avisos):
    """Resolve um alias para a linha de customer e a origem da decisão.

    Ordem: de/para da organização, de/para geral, customer da mesma
    organização, customer único de outra organização. Alias que só existe
    em outra organização é aceito (há contratos da Moreia com clientes da
    Alucom), mas fica marcado no relatório.
    """
    chave = cj.chave_alias(alias)

    alvo = cj.DE_PARA_CLIENTES_POR_ORGANIZACAO.get((org_id, chave))
    origem = 'de_para_org'
    if alvo is None:
        alvo = cj.DE_PARA_CLIENTES.get(chave)
        origem = 'de_para'
    if alvo is not None:
        alias_alvo, org_alvo = alvo
        linha, aviso = _resolver_alias_alvo(por_chave, alias_alvo, org_alvo)
        if linha is None:
            erros.append(
                f'{campo}: de/para de {alias!r} aponta para o customer '
                f'{alias_alvo!r}, inexistente ou excluído'
            )
            return None, origem
        if aviso:
            avisos.append(f'{campo}: {aviso}')
        return linha, origem

    candidatos = por_chave.get(chave) or []
    if not candidatos:
        erros.append(
            f'{campo}: cliente {alias!r} não existe em customers e não há '
            'de/para para ele'
        )
        return None, 'sem_cliente'

    mesma_org = [linha for linha in candidatos if linha['organization_id'] == org_id]
    if len(mesma_org) == 1:
        return mesma_org[0], 'org'
    if len(mesma_org) > 1:
        escolhido = min(mesma_org, key=lambda linha: linha['id'])
        avisos.append(
            f'{campo}: alias {alias!r} tem {len(mesma_org)} customers na '
            f'organização {org_id}; usando o menor id ({escolhido["id"]})'
        )
        return escolhido, 'org_ambiguo'
    if len(candidatos) == 1:
        return candidatos[0], 'outra_org'

    escolhido = min(candidatos, key=lambda linha: linha['id'])
    organizacoes = sorted({linha['organization_id'] for linha in candidatos})
    avisos.append(
        f'{campo}: cliente {alias!r} não existe na organização {org_id}; '
        f'existe em {organizacoes}. Usando o menor id ({escolhido["id"]}) — '
        'revisar de/para'
    )
    return escolhido, 'outra_org_ambiguo'


def _escrever_relatorio_clientes(linhas):
    caminho = RELATORIO_CLIENTES
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open('w', encoding='utf-8', newline='') as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS_RELATORIO_CLIENTES)
        escritor.writeheader()
        escritor.writerows(linhas)
    return caminho


def importar(engine, raiz=cj.RAIZ_PADRAO, permitir_reducao=False,
             incluir_novos=False, ignorar_pendentes=False, verbose=True,
             somente_validar=False):
    raiz = Path(raiz)
    contratos, avisos = carregar(raiz, ignorar_pendentes=ignorar_pendentes)

    novos = sorted((raiz / '_novos').rglob('*.json')) \
        if (raiz / '_novos').is_dir() else []
    if novos and not incluir_novos:
        raise cj.ContratoJsonInvalido([
            f'_novos/ tem {len(novos)} contrato(s) aguardando aprovação. '
            'Use --incluir-novos depois de revisar.'
        ])
    for caminho in novos:
        pasta = caminho.parent.name
        if pasta not in cj.ORGANIZACOES:
            raise cj.ContratoJsonInvalido([f'{caminho}: organização desconhecida'])
        documento = cj.ler_json(caminho)
        erros = []
        contrato = _validar_documento(caminho, pasta, documento, erros, avisos)
        if erros:
            raise cj.ContratoJsonInvalido(erros)
        if contrato is not None:
            contratos.append({
                'arquivo': caminho,
                'org_alias': pasta,
                'org_id': cj.ORGANIZACOES[pasta]['organization_id'],
                'dados': contrato,
            })

    if not contratos:
        raise cj.ContratoJsonInvalido(['nenhum contrato válido encontrado'])

    erros = []
    with engine.connect() as conn:
        mapa_clientes, _ = _resolver_clientes(conn)
        existentes = conn.execute(
            text('SELECT COUNT(*) FROM contracts')
        ).scalar() or 0

    if existentes and len(contratos) < existentes and not permitir_reducao:
        raise cj.ContratoJsonInvalido([
            f'{len(contratos)} contrato(s) nos JSONs contra {existentes} no '
            'banco. Se a redução é intencional, use --permitir-reducao.'
        ])

    relatorio = []

    def registrar(registro, campo, alias, linha, origem, customer_id):
        relatorio.append({
            'contrato_arquivo': str(registro['arquivo']),
            'contrato_numero': registro['dados'].get('number') or '',
            'organization_id': registro['org_id'],
            'campo': campo,
            'alias': alias,
            'customer_id': customer_id if customer_id is not None else '',
            'customer_alias': linha['alias'] if linha is not None else '',
            'customer_organization_id': (
                linha['organization_id'] if linha is not None else ''
            ),
            'origem': origem,
        })

    for registro in contratos:
        contrato = registro['dados']
        rotulo = str(registro['arquivo'])
        linha, origem = _selecionar_cliente(
            mapa_clientes, contrato['customer_alias'],
            registro['org_id'], f'{rotulo}: customer_alias', erros, avisos,
        )
        registro['customer_id'] = (
            (linha['parent_id'] or linha['id']) if linha is not None else None
        )
        registrar(registro, 'customer_alias', contrato['customer_alias'],
                  linha, origem, registro['customer_id'])
        destinatarios = []
        for alias in contrato.get('recipient_aliases') or []:
            linha, origem = _selecionar_cliente(
                mapa_clientes, alias, registro['org_id'],
                f'{rotulo}: recipient_aliases', erros, avisos,
            )
            cliente_id = linha['id'] if linha is not None else None
            registrar(registro, 'recipient_aliases', alias, linha, origem,
                      cliente_id)
            if cliente_id and cliente_id not in destinatarios:
                destinatarios.append(cliente_id)
        registro['recipient_ids'] = destinatarios

    caminho_relatorio = None
    if not erros and verbose:
        caminho_relatorio = _escrever_relatorio_clientes(relatorio)

    if erros:
        raise cj.ContratoJsonInvalido(erros)

    if somente_validar:
        stats = {
            'contratos': len(contratos),
            'resolucoes': len(relatorio),
            'de_para': sum(1 for r in relatorio
                           if r['origem'].startswith('de_para')),
            'outra_org': sum(1 for r in relatorio
                             if r['origem'] == 'outra_org'),
            'relatorio': str(caminho_relatorio or ''),
        }
        if verbose:
            print('\n' + '=' * 80)
            print('🔎 PRÉ-VOO DE CLIENTES (nenhuma escrita no banco)')
            print('=' * 80)
            for chave, valor in stats.items():
                print(f'   {chave:<15} {valor}')
            if avisos:
                print(f'   ⚠️  avisos       {len(avisos)}')
                for aviso in avisos[:10]:
                    print(f'      - {aviso}')
            print('=' * 80)
        stats['avisos'] = avisos
        return stats

    contratos.sort(key=lambda r: (
        r['org_id'],
        str(r['dados'].get('number') or ''),
        r['dados'].get('source_contract_id') or 0,
        str(r['dados'].get('name') or ''),
    ))

    agora = _agora()
    stats = {
        'contratos': 0, 'eventos': 0, 'aditivos': 0, 'infos': 0,
        'itens': 0, 'servicos': 0, 'destinatarios': 0, 'linhagens': 0,
    }

    _truncar(engine)

    with engine.begin() as conn:
        for registro in contratos:
            contrato = registro['dados']
            criado = contrato.get('created_at') or contrato['original_start_date']
            criado = str(criado).replace('T', ' ')[:19]

            resultado = conn.execute(text("""
                INSERT INTO contracts
                    (name, number, contract_type_id, contract_status_id,
                     organization_id, customer_id, object,
                     original_start_date, created_at, updated_at)
                VALUES
                    (:name, :number, :tipo, :status, :org, :cliente, :objeto,
                     :inicio, :criado, :criado)
            """), {
                'name': contrato['name'],
                'number': contrato['number'],
                'tipo': cj.TIPOS_CONTRATO[contrato['contract_type']],
                'status': cj.STATUS_CONTRATO[contrato['contract_status']],
                'org': registro['org_id'],
                'cliente': registro['customer_id'],
                'objeto': contrato.get('object') or 'NÃO INFORMADO',
                'inicio': contrato['original_start_date'],
                'criado': criado,
            })
            contract_id = resultado.lastrowid
            stats['contratos'] += 1

            for destinatario in registro['recipient_ids']:
                if engine.dialect.name == 'mysql':
                    conn.execute(text("""
                        INSERT IGNORE INTO contract_recipient_customers
                            (contract_id, customer_id)
                        VALUES (:contrato, :cliente)
                    """), {'contrato': contract_id, 'cliente': destinatario})
                else:
                    conn.execute(text("""
                        INSERT OR IGNORE INTO contract_recipient_customers
                            (contract_id, customer_id)
                        VALUES (:contrato, :cliente)
                    """), {'contrato': contract_id, 'cliente': destinatario})
                stats['destinatarios'] += 1

            raizes_itens = {}
            raizes_jobs = {}

            eventos = sorted(
                contrato['events'], key=lambda e: e.get('sequence') or 0
            )
            for evento in eventos:
                resultado_evento = conn.execute(text("""
                    INSERT INTO contract_events
                        (contract_id, created_at, updated_at)
                    VALUES (:contrato, :criado, :criado)
                """), {'contrato': contract_id, 'criado': criado})
                event_id = resultado_evento.lastrowid
                stats['eventos'] += 1

                for aditivo in evento['additives']:
                    resultado_aditivo = conn.execute(text("""
                        INSERT INTO event_additives
                            (event_id, contract_event_type_id,
                             created_at, updated_at)
                        VALUES (:evento, :tipo, :criado, :criado)
                    """), {
                        'evento': event_id,
                        'tipo': cj.TIPOS_ADITIVO[aditivo['type']],
                        'criado': criado,
                    })
                    additive_id = resultado_aditivo.lastrowid
                    stats['aditivos'] += 1

                    info = aditivo.get('info')
                    if info:
                        conn.execute(text("""
                            INSERT INTO contract_infos
                                (event_additive_id, start_date, end_date,
                                 duration, max_duration, max_end_date,
                                 total_amount, services_total_amount,
                                 created_at, updated_at)
                            VALUES
                                (:aditivo, :inicio, :fim, :duracao,
                                 :duracao_max, :fim_max, :total, :total_serv,
                                 :criado, :criado)
                        """), {
                            'aditivo': additive_id,
                            'inicio': info['start_date'],
                            'fim': info.get('end_date'),
                            'duracao': info['duration'],
                            'duracao_max': info['max_duration'],
                            'fim_max': info.get('max_end_date'),
                            'total': _total_inteiro(
                                info['total_amount'],
                                f'{registro["arquivo"].name} evento '
                                f'{evento.get("sequence")}',
                                avisos,
                            ),
                            'total_serv': info.get('services_total_amount') or 0,
                            'criado': criado,
                        })
                        stats['infos'] += 1

                    for item in aditivo.get('items') or []:
                        raiz = raizes_itens.get(item['lineage_key'])
                        resultado_item = conn.execute(text("""
                            INSERT INTO contract_items
                                (root_item_id, event_additive_id, alias,
                                 description, quantity, available_quantity,
                                 price, created_at, updated_at)
                            VALUES
                                (:raiz, :aditivo, :alias, :descricao,
                                 :quantidade, :quantidade, :preco,
                                 :criado, :criado)
                        """), {
                            'raiz': raiz,
                            'aditivo': additive_id,
                            'alias': item['alias'],
                            'descricao': item.get('description') or '',
                            'quantidade': item['quantity'],
                            'preco': item['price'],
                            'criado': criado,
                        })
                        item_id = resultado_item.lastrowid
                        if raiz is None:
                            conn.execute(text(
                                'UPDATE contract_items SET root_item_id = :id '
                                'WHERE id = :id'
                            ), {'id': item_id})
                            raizes_itens[item['lineage_key']] = item_id
                            stats['linhagens'] += 1
                        stats['itens'] += 1

                    for servico in aditivo.get('services') or []:
                        raiz = raizes_jobs.get(servico['lineage_key'])
                        resultado_job = conn.execute(text("""
                            INSERT INTO contract_jobs
                                (root_job_id, event_additive_id, alias,
                                 description, quantity, price,
                                 created_at, updated_at)
                            VALUES
                                (:raiz, :aditivo, :alias, :descricao,
                                 :quantidade, :preco, :criado, :criado)
                        """), {
                            'raiz': raiz,
                            'aditivo': additive_id,
                            'alias': servico['alias'],
                            'descricao': servico.get('description') or '',
                            'quantidade': servico['quantity'],
                            'preco': servico['price'],
                            'criado': criado,
                        })
                        job_id = resultado_job.lastrowid
                        if raiz is None:
                            conn.execute(text(
                                'UPDATE contract_jobs SET root_job_id = :id '
                                'WHERE id = :id'
                            ), {'id': job_id})
                            raizes_jobs[servico['lineage_key']] = job_id
                            stats['linhagens'] += 1
                        stats['servicos'] += 1

    if verbose:
        print('\n' + '=' * 80)
        print('📊 CARGA DE CONTRATOS A PARTIR DE JSON')
        print('=' * 80)
        for chave, valor in stats.items():
            print(f'   {chave:<15} {valor}')
        if avisos:
            print(f'   ⚠️  avisos       {len(avisos)}')
            for aviso in avisos[:10]:
                print(f'      - {aviso}')
            if len(avisos) > 10:
                print(f'      ... e mais {len(avisos) - 10}')
        print('=' * 80)

    stats['avisos'] = avisos
    return stats


def executar(eng_novo, eng_legado=None, argumentos_cli=None):
    """Ponto de entrada da etapa ``main.py contratos``.

    Flags aceitas depois do nome do alvo:
        --incluir-novos       carrega também ``_novos/<ORG>/``
        --permitir-reducao    aceita menos contratos que o banco atual
        --ignorar-pendentes   carrega apesar dos arquivos em ``_pendentes/``
    """
    import argparse

    parser = argparse.ArgumentParser(prog='main.py contratos')
    parser.add_argument('--incluir-novos', action='store_true')
    parser.add_argument('--permitir-reducao', action='store_true')
    parser.add_argument('--ignorar-pendentes', action='store_true')
    parser.add_argument(
        '--somente-validar', action='store_true',
        help='resolve os clientes e grava o relatório, sem tocar nas tabelas',
    )
    args = parser.parse_args(argumentos_cli or [])

    importar(
        eng_novo,
        incluir_novos=args.incluir_novos,
        permitir_reducao=args.permitir_reducao,
        ignorar_pendentes=args.ignorar_pendentes,
        somente_validar=args.somente_validar,
    )
