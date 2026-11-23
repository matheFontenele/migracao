"""Chaves de UPSERT dos itens de contrato e herança dos aditivos sem dados.

Cobre as duas falhas encontradas no contrato 17/2024 (Resende):
1. duas linhas com o mesmo APELIDO no mesmo evento se sobrescreviam;
2. aditivo de prazo era clonado antes da carga dos itens e ficava vazio.
"""
import unittest
from collections import Counter
from types import SimpleNamespace

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text

from cadastros.contratos.migracao_contratos import MigracaoContratos
from cadastros.contratos.planilhas_contrato_2026 import ItemPlanilha, _salvar_itens


DDL = [
    """
    CREATE TABLE contract_events (
        id INTEGER PRIMARY KEY,
        contract_id INTEGER NOT NULL,
        deleted_at TIMESTAMP NULL
    )
    """,
    """
    CREATE TABLE event_additives (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        contract_event_type_id INTEGER NOT NULL,
        deleted_at TIMESTAMP NULL
    )
    """,
    """
    CREATE TABLE contract_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_additive_id INTEGER NULL,
        alias VARCHAR(255) NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL DEFAULT 0,
        available_quantity INTEGER NOT NULL DEFAULT 0,
        price NUMERIC NOT NULL DEFAULT 0,
        root_item_id INTEGER NULL,
        created_at TIMESTAMP NULL,
        updated_at TIMESTAMP NULL,
        deleted_at TIMESTAMP NULL
    )
    """,
    """
    CREATE TABLE contract_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_additive_id INTEGER NULL,
        alias VARCHAR(255) NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL DEFAULT 0,
        price NUMERIC NOT NULL DEFAULT 0,
        created_at TIMESTAMP NULL,
        updated_at TIMESTAMP NULL,
        deleted_at TIMESTAMP NULL
    )
    """,
]

ALIAS = 'IMPRESSORA MONOCROMÁTICO LASER –  PÁGINAS A4'


def criar_banco():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        for ddl in DDL:
            conn.execute(text(ddl))
    return engine


def criar_migrador(engine):
    migrador = MigracaoContratos(engine, None)
    migrador.now = '2026-11-23 10:00:00'
    return migrador


def itens(conn, additive_id):
    return conn.execute(text("""
        SELECT id, alias, quantity, price, root_item_id
        FROM contract_items
        WHERE event_additive_id = :aid AND deleted_at IS NULL
        ORDER BY id
    """), {'aid': additive_id}).fetchall()


class ProcessarItensTest(unittest.TestCase):
    def setUp(self):
        self.engine = criar_banco()
        self.migrador = criar_migrador(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO contract_events (id, contract_id) VALUES (80, 48)"
            ))
            conn.execute(text(
                "INSERT INTO event_additives (id, event_id, contract_event_type_id)"
                " VALUES (99, 80, 1)"
            ))
        self.migrador.additive_lookup = {85: 99}
        self.migrador.additive_to_contract = {99: 48}

    def dataframe(self, linhas):
        return pd.DataFrame(linhas, columns=[
            'EVENTO', 'APELIDO', 'DESCRICAO', 'QUANTIDADE', 'VALOR_UNITARIO',
        ])

    def test_apelido_repetido_persiste_as_duas_linhas(self):
        df = self.dataframe([
            (85, ALIAS, 'BROTHER MFC-L6912DW', 61, 275.40),
            (85, ALIAS, 'BROTHER MFC-L6912DW', 27, 183.60),
            (85, 'IMPRESSORA COLORIDA', 'KYOCERA MA3500cix', 2, 1324.12),
        ])

        with self.engine.begin() as conn:
            self.migrador._processar_itens(conn, df)

        with self.engine.connect() as conn:
            linhas = itens(conn, 99)

        self.assertEqual(len(linhas), 3)
        self.assertEqual([(l.quantity, float(l.price)) for l in linhas], [
            (61, 275.40),
            (27, 183.60),
            (2, 1324.12),
        ])
        # Cada linha é raiz da própria linhagem (não herda a raiz da anterior).
        self.assertEqual([l.root_item_id for l in linhas], [l.id for l in linhas])

    def test_apelido_com_espaco_no_fim_nao_sobrescreve(self):
        # MySQL compara VARCHAR ignorando espaços à direita: sem normalizar a
        # chave, 'NOBREAK ' e 'NOBREAK' viram um UPSERT só.
        df = self.dataframe([
            (85, 'NOBREAK ', 'NOBREAK 1,5KVA', 1, 199.46),
            (85, 'NOBREAK', 'NOBREAK 1,5KVA', 1, 63.80),
        ])

        with self.engine.begin() as conn:
            self.migrador._processar_itens(conn, df)

        with self.engine.connect() as conn:
            linhas = itens(conn, 99)

        self.assertEqual(len(linhas), 2)
        self.assertEqual([float(l.price) for l in linhas], [199.46, 63.80])
        self.assertEqual([l.alias for l in linhas], ['NOBREAK', 'NOBREAK'])

    def test_reprocessar_atualiza_sem_duplicar(self):
        df = self.dataframe([
            (85, ALIAS, 'BROTHER MFC-L6912DW', 61, 275.40),
            (85, ALIAS, 'BROTHER MFC-L6912DW', 27, 183.60),
        ])

        with self.engine.begin() as conn:
            self.migrador._processar_itens(conn, df)
        df.at[0, 'QUANTIDADE'] = 62
        with self.engine.begin() as conn:
            self.migrador._processar_itens(conn, df)

        with self.engine.connect() as conn:
            linhas = itens(conn, 99)

        self.assertEqual(len(linhas), 2)
        self.assertEqual([l.quantity for l in linhas], [62, 27])


class ProcessarServicosTest(unittest.TestCase):
    def setUp(self):
        self.engine = criar_banco()
        self.migrador = criar_migrador(self.engine)
        self.migrador.additive_lookup = {8: 99}

    def test_linhas_repetidas_de_servico_persistem(self):
        df = pd.DataFrame([
            (8, 'OUTSOURCING A4', 'PÁGINAS A4', 90000, 0.05),
            (8, 'OUTSOURCING A4 ', 'PÁGINAS A4', 120000, 0.07),
        ], columns=['EVENTO', 'APELIDO', 'DESCRICAO', 'QUANTIDADE', 'VALOR_UNITARIO'])

        with self.engine.begin() as conn:
            self.migrador._processar_servicos(conn, df)

        with self.engine.connect() as conn:
            linhas = conn.execute(text("""
                SELECT alias, quantity, price FROM contract_jobs
                WHERE event_additive_id = 99 ORDER BY id
            """)).fetchall()

        self.assertEqual(len(linhas), 2)
        self.assertEqual([(l.quantity, float(l.price)) for l in linhas],
                         [(90000, 0.05), (120000, 0.07)])


class ClonarAditivosSemDadosTest(unittest.TestCase):
    def setUp(self):
        self.engine = criar_banco()
        self.migrador = criar_migrador(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO contract_events (id, contract_id)"
                " VALUES (80, 48), (89, 48)"
            ))
            conn.execute(text(
                "INSERT INTO event_additives (id, event_id, contract_event_type_id)"
                " VALUES (99, 80, 1), (110, 89, 2)"
            ))
            conn.execute(text("""
                INSERT INTO contract_items
                    (event_additive_id, alias, description, quantity,
                     available_quantity, price, root_item_id)
                VALUES
                    (99, :a, 'MONO', 61, 0, 275.40, NULL),
                    (99, :b, 'COLOR', 2, 0, 1324.12, NULL)
            """), {'a': ALIAS, 'b': 'IMPRESSORA COLORIDA'})
            conn.execute(text(
                "UPDATE contract_items SET root_item_id = id"
            ))
            conn.execute(text("""
                INSERT INTO contract_jobs
                    (event_additive_id, alias, description, quantity, price)
                VALUES (99, 'CÓPIAS EXCEDENTES', 'EXCEDENTE', 90000, 0.05)
            """))

    def test_aditivo_de_prazo_herda_itens_e_servicos(self):
        with self.engine.begin() as conn:
            self.migrador._clonar_aditivos_sem_dados(conn)

        with self.engine.connect() as conn:
            origem = itens(conn, 99)
            clonados = itens(conn, 110)
            jobs = conn.execute(text("""
                SELECT alias, quantity, price FROM contract_jobs
                WHERE event_additive_id = 110
            """)).fetchall()

        self.assertEqual(len(clonados), 2)
        self.assertEqual([l.root_item_id for l in clonados],
                         [l.id for l in origem])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].alias, 'CÓPIAS EXCEDENTES')
        self.assertEqual(self.migrador.stats['aditivos_clonados'], 1)
        self.assertEqual(self.migrador.stats['itens_clonados'], 2)

    def test_aditivo_com_itens_proprios_nao_e_clonado(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO contract_items
                    (event_additive_id, alias, description, quantity,
                     available_quantity, price)
                VALUES (110, 'ITEM PROPRIO', 'PROPRIO', 1, 1, 10)
            """))
            self.migrador._clonar_aditivos_sem_dados(conn)

        with self.engine.connect() as conn:
            clonados = itens(conn, 110)

        self.assertEqual(len(clonados), 1)
        self.assertEqual(clonados[0].alias, 'ITEM PROPRIO')
        self.assertEqual(self.migrador.stats['aditivos_clonados'], 0)


class SalvarItensPlanilhaTest(unittest.TestCase):
    """Mesmo defeito de chave no importador das planilhas 2026."""

    def test_nome_repetido_persiste_as_duas_linhas(self):
        engine = criar_banco()
        contrato = SimpleNamespace(itens=[
            ItemPlanilha(1, ALIAS, 'MONO', 61, 275.40, 12),
            ItemPlanilha(1, ALIAS, 'MONO', 27, 183.60, 12),
        ])
        stats = Counter()

        with engine.begin() as conn:
            _salvar_itens(conn, 99, contrato, '2026-11-23 10:00:00', stats)
            _salvar_itens(conn, 99, contrato, '2026-11-23 11:00:00', stats)

        with engine.connect() as conn:
            linhas = itens(conn, 99)

        self.assertEqual(len(linhas), 2)
        self.assertEqual([l.quantity for l in linhas], [61, 27])
        self.assertEqual(stats['itens_criados'], 2)


if __name__ == '__main__':
    unittest.main()
