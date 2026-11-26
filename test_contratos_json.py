"""Contrato de dados dos contratos em JSON (extração -> migração)."""
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import sqlalchemy as sa

from cadastros.contratos import contratos_json as cj
from cadastros.contratos import importar_contratos_json as importer


DDL = [
    """CREATE TABLE customers (
        id INTEGER PRIMARY KEY, alias TEXT NOT NULL, parent_id INTEGER NULL,
        organization_id INTEGER NULL, deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        number TEXT NOT NULL, contract_type_id INTEGER NOT NULL,
        contract_status_id INTEGER NOT NULL, organization_id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL, object TEXT NOT NULL,
        original_start_date DATE NULL, created_at TIMESTAMP NULL,
        updated_at TIMESTAMP NULL, deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE contract_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
        created_at TIMESTAMP NULL, updated_at TIMESTAMP NULL,
        deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE event_additives (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
        contract_event_type_id INTEGER NOT NULL, created_at TIMESTAMP NULL,
        updated_at TIMESTAMP NULL, deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE contract_infos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_additive_id INTEGER NULL,
        start_date DATE NOT NULL, duration INTEGER NOT NULL,
        max_duration INTEGER NOT NULL, end_date DATE NULL,
        max_end_date DATE NULL, total_amount INTEGER NOT NULL,
        services_total_amount NUMERIC NOT NULL DEFAULT 0,
        created_at TIMESTAMP NULL, updated_at TIMESTAMP NULL,
        deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE contract_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, root_item_id INTEGER NULL,
        event_additive_id INTEGER NULL, alias TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL,
        available_quantity INTEGER NOT NULL DEFAULT 0, price NUMERIC NOT NULL,
        created_at TIMESTAMP NULL, updated_at TIMESTAMP NULL,
        deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE contract_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, root_job_id INTEGER NULL,
        event_additive_id INTEGER NULL, alias TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL,
        price NUMERIC NOT NULL, created_at TIMESTAMP NULL,
        updated_at TIMESTAMP NULL, deleted_at TIMESTAMP NULL)""",
    """CREATE TABLE contract_recipient_customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL, UNIQUE (contract_id, customer_id))""",
]


def _documento(number='001/2025', itens_cadastro=None, itens_prazo=None,
               customer_alias='PREFEITURA TESTE', pendencias=None):
    return {
        'schema_version': 1,
        'organization': {'cnpj': cj.ORGANIZACOES['ALUCOM']['cnpj'],
                         'alias': 'ALUCOM'},
        'contract': {
            'status': 'OK',
            'stable_key': f'ALUCOM|{number}|id=1',
            'source_contract_id': 1,
            'number': number,
            'name': 'CONTRATO TESTE',
            'customer_alias': customer_alias,
            'recipient_aliases': [customer_alias],
            'contract_type': 'LICITACAO',
            'contract_status': 'EM_ANDAMENTO',
            'object': 'OBJETO',
            'original_start_date': '2025-01-01',
            'created_at': '2025-01-01T00:00:00',
            'source': [{'file': 'teste'}],
            'events': [
                {'sequence': 1, 'additives': [{
                    'type': 'CADASTRO',
                    'info': {'start_date': '2025-01-01', 'end_date': '2025-12-31',
                             'duration': 12, 'max_duration': 60,
                             'max_end_date': '2030-12-31',
                             'total_amount': 1200,
                             'services_total_amount': 0},
                    'items': itens_cadastro if itens_cadastro is not None else [
                        {'alias': 'IMPRESSORA A4', 'description': '',
                         'quantity': 1, 'price': 100, 'months': 12,
                         'occurrence': 0, 'lineage_key': 'impressora-a4#0'},
                    ],
                    'services': [],
                }]},
                {'sequence': 2, 'additives': [{
                    'type': 'ADITIVO_DE_PRAZO',
                    'info': {'start_date': '2025-01-01', 'end_date': '2026-12-31',
                             'duration': 24, 'max_duration': 60,
                             'max_end_date': '2030-12-31',
                             'total_amount': 2400,
                             'services_total_amount': 0},
                    'items': itens_prazo if itens_prazo is not None else [
                        {'alias': 'IMPRESSORA A4', 'description': '',
                         'quantity': 1, 'price': 100, 'months': 24,
                         'occurrence': 0, 'lineage_key': 'impressora-a4#0'},
                    ],
                    'services': [],
                }]},
            ],
            'pendencias': pendencias or [],
        },
    }


class ContratosJsonTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        for pasta in cj.PASTAS:
            (self.raiz / pasta).mkdir(parents=True, exist_ok=True)

        self.engine = sa.create_engine('sqlite://')
        with self.engine.begin() as conn:
            for ddl in DDL:
                conn.execute(sa.text(ddl))
            conn.execute(sa.text(
                "INSERT INTO customers (id, alias, parent_id, organization_id) "
                "VALUES (10, 'PREFEITURA TESTE', NULL, 1115)"
            ))
            conn.execute(sa.text(
                "INSERT INTO customers (id, alias, parent_id, organization_id) "
                "VALUES (11, 'SECRETARIA TESTE', 10, 1115)"
            ))

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def _gravar(self, documento, nome='001-2025.json', pasta='ALUCOM'):
        caminho = self.raiz / pasta / nome
        caminho.write_text(json.dumps(documento), encoding='utf-8')
        return caminho

    def test_importa_e_reconstroi_linhagem(self):
        self._gravar(_documento())

        stats = importer.importar(self.engine, self.raiz, verbose=False)

        self.assertEqual(stats['contratos'], 1)
        self.assertEqual(stats['itens'], 2)
        with self.engine.connect() as conn:
            linhas = conn.execute(sa.text(
                "SELECT id, root_item_id, quantity FROM contract_items ORDER BY id"
            )).all()
        self.assertEqual(len(linhas), 2)
        self.assertEqual(linhas[0].root_item_id, linhas[0].id)
        self.assertEqual(linhas[1].root_item_id, linhas[0].id)

    def test_total_fracionario_arredonda_half_up_com_aviso(self):
        # A coluna é INT e os JSONs da extração antiga trazem centavos.
        documento = _documento()
        documento['contract']['events'][0]['additives'][0]['info']['total_amount'] = 1200.6
        documento['contract']['events'][1]['additives'][0]['info']['total_amount'] = 2400
        self._gravar(documento)

        stats = importer.importar(self.engine, self.raiz, verbose=False)

        with self.engine.connect() as conn:
            totais = conn.execute(sa.text(
                'SELECT total_amount FROM contract_infos ORDER BY start_date'
            )).scalars().all()
        self.assertEqual(totais, [1201, 2400])
        self.assertEqual(
            sum('arredondado' in aviso for aviso in stats['avisos']), 1
        )

    def test_importa_servicos_com_linhagem(self):
        documento = _documento()
        documento['contract']['events'][0]['additives'][0]['services'] = [
            {'alias': 'SUPORTE', 'description': '', 'quantity': 1, 'price': 50,
             'months': 12, 'occurrence': 0, 'lineage_key': 'suporte#0'},
        ]
        documento['contract']['events'][1]['additives'][0]['services'] = [
            {'alias': 'SUPORTE', 'description': '', 'quantity': 1, 'price': 50,
             'months': 24, 'occurrence': 0, 'lineage_key': 'suporte#0'},
        ]
        self._gravar(documento)

        importer.importar(self.engine, self.raiz, verbose=False)

        with self.engine.connect() as conn:
            linhas = conn.execute(sa.text(
                "SELECT id, root_job_id FROM contract_jobs ORDER BY id"
            )).all()
        self.assertEqual(linhas[0].root_job_id, linhas[0].id)
        self.assertEqual(linhas[1].root_job_id, linhas[0].id)

    def test_cliente_inexistente_aborta_sem_truncar(self):
        self._gravar(_documento(customer_alias='CLIENTE QUE NAO EXISTE'))
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO contracts (name, number, contract_type_id, "
                "contract_status_id, organization_id, customer_id, object) "
                "VALUES ('ANTIGO', 'X', 1, 2, 1115, 10, 'x')"
            ))

        with self.assertRaises(cj.ContratoJsonInvalido):
            importer.importar(self.engine, self.raiz, verbose=False)

        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(sa.text('SELECT COUNT(*) FROM contracts')).scalar(), 1
            )

    def test_stable_key_repetida_aborta(self):
        self._gravar(_documento(), nome='a.json')
        self._gravar(_documento(), nome='b.json')

        with self.assertRaises(cj.ContratoJsonInvalido) as ctx:
            importer.importar(self.engine, self.raiz, verbose=False)
        self.assertIn('stable_key', str(ctx.exception))

    def test_pendentes_bloqueia_carga(self):
        self._gravar(_documento())
        (self.raiz / '_pendentes' / 'ALUCOM').mkdir(parents=True)
        (self.raiz / '_pendentes' / 'ALUCOM' / 'x.json').write_text('{}')

        with self.assertRaises(cj.ContratoJsonInvalido):
            importer.importar(self.engine, self.raiz, verbose=False)

    def test_alias_repetido_na_mesma_ocorrencia_e_aviso(self):
        itens = [
            {'alias': 'NOBREAK', 'description': '', 'quantity': 1, 'price': 10,
             'months': 12, 'occurrence': 0, 'lineage_key': 'nobreak#0'},
            {'alias': 'NOBREAK', 'description': '', 'quantity': 2, 'price': 10,
             'months': 12, 'occurrence': 0, 'lineage_key': 'nobreak#0'},
        ]
        self._gravar(_documento(itens_cadastro=itens, itens_prazo=[]))

        stats = importer.importar(self.engine, self.raiz, verbose=False)

        self.assertEqual(stats['itens'], 2)
        self.assertTrue(any('repetido' in aviso for aviso in stats['avisos']))

    def test_enum_invalido_aborta(self):
        documento = _documento()
        documento['contract']['contract_status'] = 'ATIVO'
        self._gravar(documento)

        with self.assertRaises(cj.ContratoJsonInvalido):
            importer.importar(self.engine, self.raiz, verbose=False)

    def test_nome_arquivo_desempata_numero_repetido(self):
        self.assertEqual(cj.nome_arquivo('2025/2208.16'), '2025-2208.16.json')
        self.assertEqual(
            cj.nome_arquivo('2025/2208.02', contract_id=217, duplicado=True),
            '2025-2208.02__id217.json',
        )
        self.assertEqual(cj.nome_arquivo('07-2024'), cj.nome_arquivo('07/2024'))

    def test_somente_validar_resolve_clientes_sem_truncar(self):
        self._gravar(_documento())
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO contracts (name, number, contract_type_id, "
                "contract_status_id, organization_id, customer_id, object) "
                "VALUES ('ANTIGO', 'X', 1, 2, 1115, 10, 'x')"
            ))

        stats = importer.importar(
            self.engine, self.raiz, verbose=False, somente_validar=True
        )

        self.assertEqual(stats['contratos'], 1)
        self.assertEqual(stats['resolucoes'], 2)
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(sa.text('SELECT COUNT(*) FROM contracts')).scalar(), 1
            )

    def test_cliente_do_contrato_e_a_raiz_do_destinatario(self):
        documento = _documento(customer_alias='SECRETARIA TESTE')
        self._gravar(documento)

        importer.importar(self.engine, self.raiz, verbose=False)

        with self.engine.connect() as conn:
            cliente = conn.execute(
                sa.text('SELECT customer_id FROM contracts')
            ).scalar()
        self.assertEqual(cliente, 10)


class SelecaoClienteTest(unittest.TestCase):
    """Resolução de alias de cliente contra customers.

    O mesmo alias pode existir em mais de uma organização; a escolha não
    pode depender da ordem de leitura da tabela.
    """

    def _mapa(self, *linhas):
        por_chave = defaultdict(list)
        por_id = {}
        for id_, alias, parent_id, org in linhas:
            linha = {
                'id': id_, 'alias': alias,
                'parent_id': parent_id, 'organization_id': org,
            }
            por_chave[cj.chave_alias(alias)].append(linha)
            por_id[id_] = linha
        return por_chave, por_id

    def _selecionar(self, mapa, alias, org_id=1115):
        erros, avisos = [], []
        linha, origem = importer._selecionar_cliente(
            mapa[0], alias, org_id, 'campo', erros, avisos
        )
        return linha, origem, erros, avisos

    def test_prefere_cliente_da_mesma_organizacao(self):
        mapa = self._mapa(
            (136, 'PREFEITURA DE MARANGUAPE', None, 1115),
            (137, 'PREFEITURA DE MARANGUAPE', None, 1122),
        )

        linha, origem, erros, avisos = self._selecionar(mapa, 'PREFEITURA DE MARANGUAPE')

        self.assertEqual((linha['id'], origem), (136, 'org'))
        self.assertEqual(erros, [])
        self.assertEqual(avisos, [])

    def test_mesmo_alias_em_outra_organizacao_nao_vaza(self):
        mapa = self._mapa(
            (136, 'PREFEITURA DE MARANGUAPE', None, 1115),
            (137, 'PREFEITURA DE MARANGUAPE', None, 1122),
        )

        linha, origem, _, avisos = self._selecionar(
            mapa, 'PREFEITURA DE MARANGUAPE', org_id=1122
        )

        self.assertEqual((linha['id'], origem), (137, 'org'))
        self.assertEqual(avisos, [])

    def test_aceita_cliente_unico_de_outra_organizacao(self):
        mapa = self._mapa(
            (2461, 'SECRETARIA DE SAUDE DE ARACATI', 96, 1115),
        )

        linha, origem, erros, _ = self._selecionar(
            mapa, 'SECRETARIA DE SAUDE DE ARACATI', org_id=1122
        )

        self.assertEqual((linha['id'], origem), (2461, 'outra_org'))
        self.assertEqual(erros, [])

    def test_ambiguo_entre_organizacoes_usa_menor_id_e_avisa(self):
        mapa = self._mapa(
            (429, 'CASPE SERVICOS', None, 1115),
            (428, 'CASPE SERVICOS', None, 1122),
        )

        linha, origem, erros, avisos = self._selecionar(
            mapa, 'CASPE SERVICOS', org_id=1311
        )

        self.assertEqual((linha['id'], origem), (428, 'outra_org_ambiguo'))
        self.assertEqual(erros, [])
        self.assertEqual(len(avisos), 1)
        self.assertIn('revisar de/para', avisos[0])

    def test_varios_na_mesma_organizacao_usa_menor_id_e_avisa(self):
        mapa = self._mapa(
            (278, 'ANTONIA ELIANE', None, 1115),
            (277, 'ANTONIA ELIANE', None, 1115),
        )

        linha, origem, erros, avisos = self._selecionar(mapa, 'ANTONIA ELIANE')

        self.assertEqual((linha['id'], origem), (277, 'org_ambiguo'))
        self.assertEqual(erros, [])
        self.assertEqual(len(avisos), 1)

    def test_de_para_geral_resolve_alias_inexistente(self):
        mapa = self._mapa(
            (2481, 'SECRETARIA DE SAÚDE DO MUNICIPIO DE ARACOIABA', 97, 1122)
        )

        linha, origem, erros, _ = self._selecionar(mapa, 'SECRETARIA DE SAÚDE')

        self.assertEqual((linha['id'], origem), (2481, 'de_para'))
        self.assertEqual(erros, [])

    def test_de_para_alvo_em_outra_organizacao_e_aceito_com_aviso(self):
        # O de/para aponta para a org. 1115; se o customer migrar de
        # organização o alias continua resolvendo (com aviso).
        mapa = self._mapa(
            (1441, 'P M DE EUSEBIO - SECRETARIA DE CULTURA E TURISMO', None, 1122)
        )

        linha, origem, erros, avisos = self._selecionar(
            mapa, 'SECRETARIA DE CULTURA E TURISMO'
        )

        self.assertEqual((linha['id'], origem), (1441, 'de_para'))
        self.assertEqual(erros, [])
        self.assertTrue(any('de outra organização' in a for a in avisos))

    def test_de_para_por_organizacao_tem_prioridade(self):
        mapa = self._mapa(
            (137, 'PREFEITURA MUNICIPAL DE MARANGUAPE', None, 1122),
            (2724, 'GABINETE - MARANGUAPE', 136, 1115),
        )

        linha_1115, origem_1115, _, _ = self._selecionar(
            mapa, 'GABINETE DO PREFEITO', org_id=1115
        )
        linha_1122, origem_1122, _, _ = self._selecionar(
            mapa, 'GABINETE DO PREFEITO', org_id=1122
        )

        self.assertEqual((linha_1115['id'], origem_1115), (2724, 'de_para_org'))
        self.assertEqual((linha_1122['id'], origem_1122), (137, 'de_para_org'))

    def test_de_para_para_customer_inexistente_e_erro(self):
        mapa = self._mapa((10, 'PREFEITURA TESTE', None, 1115))

        linha, origem, erros, _ = self._selecionar(mapa, 'SECRETARIA DE CULTURA')

        self.assertIsNone(linha)
        self.assertEqual(origem, 'de_para')
        self.assertTrue(any('inexistente ou excluído' in e for e in erros))

    def test_alias_sem_customer_e_erro(self):
        mapa = self._mapa((10, 'PREFEITURA TESTE', None, 1115))

        linha, origem, erros, _ = self._selecionar(mapa, 'CLIENTE NOVO')

        self.assertIsNone(linha)
        self.assertEqual(origem, 'sem_cliente')
        self.assertTrue(any('não existe em customers' in e for e in erros))

    def test_resolver_clientes_ignora_excluidos(self):
        engine = sa.create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(sa.text(DDL[0]))
            conn.execute(sa.text(
                "INSERT INTO customers (id, alias, organization_id, deleted_at) "
                "VALUES (10, 'ATIVO', 1115, NULL)"
            ))
            conn.execute(sa.text(
                "INSERT INTO customers (id, alias, organization_id, deleted_at) "
                "VALUES (11, 'EXCLUIDO', 1115, '2025-01-01')"
            ))
        with engine.connect() as conn:
            por_chave, por_id = importer._resolver_clientes(conn)
        engine.dispose()

        self.assertEqual(set(por_id), {10})
        self.assertIn(cj.chave_alias('ATIVO'), por_chave)
        self.assertNotIn(cj.chave_alias('EXCLUIDO'), por_chave)


if __name__ == '__main__':
    unittest.main()
