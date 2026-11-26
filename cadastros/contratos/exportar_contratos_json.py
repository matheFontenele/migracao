"""Exporta os contratos do banco refatorado para o JSON de contrato.

Serve para dois usos:

1. *bootstrap*: captura o estado atual (267 contratos) antes de a extração
   externa assumir a responsabilidade, gerando o baseline que a migração
   passa a consumir;
2. *snapshot*: conferência periódica do que está no banco contra o que os
   arquivos dizem.

O JSON é sempre um arquivo por contrato, no mesmo schema que o app externo
preenche (ver ``contratos_json.py``).
"""
from __future__ import annotations

import csv
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from cadastros.contratos import contratos_json as cj


def _iso(valor):
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.isoformat(timespec='seconds')
    if isinstance(valor, date):
        return valor.isoformat()
    return valor


def _numero(valor):
    if isinstance(valor, Decimal):
        return float(valor)
    return valor


def _sha256(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open('rb') as arquivo:
        for bloco in iter(lambda: arquivo.read(65536), b''):
            digest.update(bloco)
    return digest.hexdigest()


def _ids_existentes(raiz: Path):
    """Preserva ``source_contract_id`` já publicado.

    O id do banco muda a cada recarga (é gerado pelo AUTO_INCREMENT). O id de
    origem é só correlação com o snapshot: uma vez atribuído, não pode mudar.
    """
    preservados = {}
    for pasta in cj.PASTAS:
        diretorio = raiz / pasta
        if not diretorio.is_dir():
            continue
        for arquivo in sorted(diretorio.glob('*.json')):
            try:
                dados = cj.ler_json(arquivo)
                contrato = dados['contract']
                chave = (dados['organization']['alias'],
                         contrato['number'], contrato['name'])
                if contrato.get('source_contract_id') is not None:
                    preservados[chave] = contrato['source_contract_id']
            except Exception:
                continue
    return preservados


def _carregar_banco(conn):
    contratos = conn.execute(text("""
        SELECT c.id, c.name, c.number, c.contract_type_id,
               c.contract_status_id, c.organization_id, c.customer_id,
               c.object, c.original_start_date, c.created_at,
               cu.alias AS customer_alias, cu.parent_id AS customer_parent_id
        FROM contracts c
        JOIN customers cu ON cu.id = c.customer_id
        ORDER BY c.organization_id, c.number, c.id
    """)).mappings().all()

    eventos = conn.execute(text("""
        SELECT id, contract_id, created_at
        FROM contract_events
        ORDER BY contract_id, created_at, id
    """)).mappings().all()

    aditivos = conn.execute(text("""
        SELECT id, event_id, contract_event_type_id
        FROM event_additives
        ORDER BY event_id, id
    """)).mappings().all()

    infos = {
        linha['event_additive_id']: linha
        for linha in conn.execute(text("""
            SELECT event_additive_id, start_date, end_date, duration,
                   max_duration, max_end_date, total_amount,
                   services_total_amount
            FROM contract_infos
            WHERE event_additive_id IS NOT NULL
            ORDER BY id
        """)).mappings().all()
    }

    itens = conn.execute(text("""
        SELECT id, root_item_id, event_additive_id, alias, description,
               quantity, price
        FROM contract_items
        WHERE deleted_at IS NULL
        ORDER BY id
    """)).mappings().all()

    jobs = conn.execute(text("""
        SELECT id, root_job_id, event_additive_id, alias, description,
               quantity, price
        FROM contract_jobs
        WHERE deleted_at IS NULL
        ORDER BY id
    """)).mappings().all()

    destinatarios = conn.execute(text("""
        SELECT crc.contract_id, cu.alias
        FROM contract_recipient_customers crc
        JOIN customers cu ON cu.id = crc.customer_id
        ORDER BY crc.contract_id, cu.alias
    """)).mappings().all()

    return contratos, eventos, aditivos, infos, itens, jobs, destinatarios


def _linhagem(linhas, campo_root):
    """Reconstrói ``lineage_key`` a partir de ``root_item_id``/``root_job_id``.

    A raiz é o primeiro registro da linhagem; a ocorrência desempata alias
    repetido dentro do mesmo aditivo.
    """
    ocorrencia = {}
    chave_por_id = {}
    for linha in linhas:
        chave = (linha['event_additive_id'],
                 cj.normalizar(linha['alias']))
        indice = ocorrencia.get(chave, 0)
        ocorrencia[chave] = indice + 1
        chave_por_id[linha['id']] = f'{chave[1]}#{indice}'

    resultado = {}
    for linha in linhas:
        raiz = linha[campo_root] or linha['id']
        resultado[linha['id']] = chave_por_id.get(raiz, chave_por_id[linha['id']])
    return resultado


def _csv(caminho: Path, cabecalho, linhas):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open('w', encoding='utf-8', newline='') as arquivo:
        escritor = csv.writer(arquivo, lineterminator='\n')
        escritor.writerow(cabecalho)
        escritor.writerows(linhas)


def exportar(engine, raiz=cj.RAIZ_PADRAO, verbose=True, limpar=False):
    raiz = Path(raiz)
    raiz.mkdir(parents=True, exist_ok=True)

    with engine.connect() as conn:
        (contratos, eventos, aditivos, infos,
         itens, jobs, destinatarios) = _carregar_banco(conn)
        clientes = conn.execute(text("""
            SELECT cu.id, cu.alias, cup.alias AS parent_alias,
                   o.cpf_cnpj AS organization_cnpj, ad.city, ad.state,
                   ad.legacy_customer_id
            FROM customers cu
            LEFT JOIN customers cup ON cup.id = cu.parent_id
            LEFT JOIN organizations o ON o.id = cu.organization_id
            LEFT JOIN addresses ad ON ad.id = (
                SELECT MIN(a.id) FROM addresses a
                WHERE a.addressable_type = 'customer'
                  AND a.addressable_id = cu.id
            )
            WHERE cu.deleted_at IS NULL
            ORDER BY cu.alias
        """)).mappings().all()

    # A referência é sempre a pasta canônica: exportar para um diretório
    # temporário não pode renumerar os contratos.
    preservados = _ids_existentes(cj.RAIZ_PADRAO)
    alias_por_id = {cliente['id']: cliente['alias'] for cliente in clientes}

    eventos_por_contrato = {}
    for evento in eventos:
        eventos_por_contrato.setdefault(evento['contract_id'], []).append(evento)

    aditivos_por_evento = {}
    for aditivo in aditivos:
        aditivos_por_evento.setdefault(aditivo['event_id'], []).append(aditivo)

    itens_por_aditivo = {}
    for item in itens:
        itens_por_aditivo.setdefault(item['event_additive_id'], []).append(item)

    jobs_por_aditivo = {}
    for job in jobs:
        jobs_por_aditivo.setdefault(job['event_additive_id'], []).append(job)

    destinatarios_por_contrato = {}
    for linha in destinatarios:
        lista = destinatarios_por_contrato.setdefault(linha['contract_id'], [])
        # Prefeituras mescladas podem ter dois customers com o mesmo alias;
        # o JSON referencia o alias, então a lista é deduplicada por ele.
        if cj.chave_alias(linha['alias']) not in {cj.chave_alias(a) for a in lista}:
            lista.append(linha['alias'])

    linhagem_itens = _linhagem(itens, 'root_item_id')
    linhagem_jobs = _linhagem(jobs, 'root_job_id')

    # Nome de arquivo repetido na mesma organização exige sufixo de id.
    contagem_numero = {}
    for contrato in contratos:
        chave = (contrato['organization_id'],
                 cj.nome_arquivo(contrato['number']))
        contagem_numero[chave] = contagem_numero.get(chave, 0) + 1

    arquivos = []
    snapshot_contratos = []
    itens_snapshot = []
    total = {'contracts': 0, 'events': 0, 'additives': 0, 'infos': 0,
             'items': 0, 'services': 0, 'recipients': 0}

    for contrato in contratos:
        org_alias = cj.ORGANIZACAO_POR_ID.get(contrato['organization_id'])
        if not org_alias:
            raise RuntimeError(
                f"Contrato {contrato['id']} em organização desconhecida "
                f"{contrato['organization_id']}"
            )

        cliente_alias = contrato['customer_alias']
        if contrato['customer_parent_id']:
            # O JSON sempre aponta para a raiz da hierarquia.
            cliente_alias = alias_por_id.get(
                contrato['customer_parent_id'], cliente_alias
            )

        chave_preservada = (org_alias, contrato['number'], contrato['name'])
        source_id = preservados.get(chave_preservada, contrato['id'])

        eventos_json = []
        for sequencia, evento in enumerate(eventos_por_contrato.get(contrato['id'], []), start=1):
            aditivos_json = []
            for aditivo in aditivos_por_evento.get(evento['id'], []):
                info = infos.get(aditivo['id'])
                info_json = None
                if info is not None:
                    info_json = {
                        'start_date': _iso(info['start_date']),
                        'end_date': _iso(info['end_date']),
                        'duration': info['duration'],
                        'max_duration': info['max_duration'],
                        'max_end_date': _iso(info['max_end_date']),
                        'total_amount': info['total_amount'],
                        'services_total_amount': _numero(info['services_total_amount']),
                    }

                itens_json = [
                    {
                        'alias': item['alias'],
                        'description': item['description'] or '',
                        'quantity': item['quantity'],
                        'price': _numero(item['price']),
                        'months': (info['duration'] if info is not None else None),
                        'occurrence': int(linhagem_itens[item['id']].rsplit('#', 1)[1]),
                        'lineage_key': linhagem_itens[item['id']],
                    }
                    for item in itens_por_aditivo.get(aditivo['id'], [])
                ]
                jobs_json = [
                    {
                        'alias': job['alias'],
                        'description': job['description'] or '',
                        'quantity': job['quantity'],
                        'price': _numero(job['price']),
                        'months': (info['duration'] if info is not None else None),
                        'occurrence': int(linhagem_jobs[job['id']].rsplit('#', 1)[1]),
                        'lineage_key': linhagem_jobs[job['id']],
                    }
                    for job in jobs_por_aditivo.get(aditivo['id'], [])
                ]

                aditivos_json.append({
                    'type': cj.TIPOS_ADITIVO_POR_ID[aditivo['contract_event_type_id']],
                    'info': info_json,
                    'items': itens_json,
                    'services': jobs_json,
                })
                total['additives'] += 1
                total['infos'] += 1 if info_json else 0
                total['items'] += len(itens_json)
                total['services'] += len(jobs_json)

            eventos_json.append({'sequence': sequencia, 'additives': aditivos_json})
            total['events'] += 1

        destinatarios_contrato = destinatarios_por_contrato.get(contrato['id'], [])

        documento = {
            'schema_version': cj.SCHEMA_VERSION,
            'organization': {
                'cnpj': cj.ORGANIZACOES[org_alias]['cnpj'],
                'alias': org_alias,
            },
            'contract': {
                'status': 'OK',
                'stable_key': f'{org_alias}|{contrato["number"]}|id={source_id}',
                'source_contract_id': source_id,
                'number': contrato['number'],
                'name': contrato['name'],
                'customer_alias': cliente_alias,
                'recipient_aliases': destinatarios_contrato,
                'contract_type': cj.TIPOS_CONTRATO_POR_ID[contrato['contract_type_id']],
                'contract_status': cj.STATUS_CONTRATO_POR_ID[contrato['contract_status_id']],
                'object': contrato['object'] or 'NÃO INFORMADO',
                'original_start_date': _iso(contrato['original_start_date']),
                'created_at': _iso(contrato['created_at']),
                'source': [{'file': 'bootstrap:controle-interno',
                            'detail': f'id_origem={source_id}'}],
                'events': eventos_json,
                'pendencias': [],
            },
        }

        duplicado = contagem_numero[
            (contrato['organization_id'], cj.nome_arquivo(contrato['number']))
        ] > 1
        nome = cj.nome_arquivo(
            contrato['number'],
            contract_id=source_id if source_id is not None else contrato['id'],
            duplicado=duplicado,
        )
        caminho = raiz / org_alias / nome
        cj.escrever_json(caminho, documento)

        arquivos.append({
            'path': f'{org_alias}/{nome}',
            'sha256': _sha256(caminho),
            'stable_key': documento['contract']['stable_key'],
            'status': 'OK',
        })
        snapshot_contratos.append([
            source_id, org_alias, contrato['number'], contrato['name'],
            cliente_alias, documento['contract']['contract_type'],
            documento['contract']['contract_status'],
            documento['contract']['original_start_date'],
        ])
        for evento in eventos_json:
            for aditivo in evento['additives']:
                for item in aditivo['items']:
                    itens_snapshot.append([
                        source_id, aditivo['type'], item['alias'],
                        item['occurrence'], item['quantity'], item['price'],
                    ])
        total['contracts'] += 1
        total['recipients'] += len(destinatarios_contrato)

    arquivos.sort(key=lambda item: item['path'])

    if limpar:
        # Um arquivo que sobrou de uma rodada anterior seria um contrato a
        # mais na carga (o importador aborta por stable_key repetida).
        esperados = {(raiz / item['path']).resolve() for item in arquivos}
        for pasta in cj.PASTAS:
            diretorio = raiz / pasta
            if not diretorio.is_dir():
                continue
            for caminho in diretorio.glob('*.json'):
                if caminho.resolve() not in esperados:
                    caminho.unlink()

    _csv(raiz / '_referencia' / 'organizations.csv',
         ['cnpj', 'alias', 'organization_id'],
         [[dados['cnpj'], alias, dados['organization_id']]
          for alias, dados in cj.ORGANIZACOES.items()])

    _csv(raiz / '_referencia' / 'customers.csv',
         ['alias', 'parent_alias', 'organization_cnpj', 'city', 'state',
          'legacy_customer_id'],
         [[c['alias'], c['parent_alias'], c['organization_cnpj'], c['city'],
           c['state'], c['legacy_customer_id']] for c in clientes])

    cj.escrever_json(raiz / '_referencia' / 'enums.json', {
        'contract_type': list(cj.TIPOS_CONTRATO),
        'contract_status': list(cj.STATUS_CONTRATO),
        'event_type': list(cj.TIPOS_ADITIVO),
    })

    _csv(raiz / '_referencia' / 'contracts_snapshot.csv',
         ['source_contract_id', 'org_alias', 'number', 'name',
          'customer_alias', 'type', 'status', 'original_start_date'],
         snapshot_contratos)

    _csv(raiz / '_referencia' / 'contract_items_snapshot.csv',
         ['source_contract_id', 'additive_type', 'alias', 'occurrence',
          'quantity', 'price'],
         itens_snapshot)

    decisoes = raiz / '_referencia' / 'decisoes_humanas.csv'
    if not decisoes.exists():
        _csv(decisoes, ['chave', 'decisao', 'valor', 'autor', 'data'], [])

    cj.escrever_json(raiz / 'manifest.json', {
        'schema_version': cj.SCHEMA_VERSION,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'sources': [{'path': 'bootstrap:controle-interno',
                     'detail': 'export do banco refatorado'}],
        'files': arquivos,
        'totals': total,
        'pendentes': 0,
        'novos': 0,
    })

    if verbose:
        print(f'📦 {total["contracts"]} contratos exportados em {raiz}')
        print(f'   eventos={total["events"]} aditivos={total["additives"]} '
              f'infos={total["infos"]} itens={total["items"]} '
              f'serviços={total["services"]} destinatários={total["recipients"]}')

    return total


def executar(eng_novo, eng_legado=None):
    exportar(eng_novo)
