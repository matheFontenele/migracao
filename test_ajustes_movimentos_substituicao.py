"""Movimento SA reinserido pelo ajuste: equipamento de retorno.

O refatorado guarda os dois lados da substituição em campos diferentes:
``movement_items.equipment_id`` é o equipamento ENTREGUE e
``service_order_items.equipment_id`` é o equipamento a RETORNAR. O ajuste
individual reinseria um movimento SA (envio do substituto) e precisa buscar o
par SD no legado — senão a tela mostra o próprio equipamento como retorno
(caso do movimento 558652).
"""

import unittest

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from ajustes.ajustes_movimentos import (
    AjustesMovimentos,
    parear_equipamento_retorno,
)


class PareamentoTests(unittest.TestCase):
    def test_pareia_pela_posicao_de_insercao(self):
        # O legado grava um item SD e um SA por equipamento, na mesma ordem.
        self.assertEqual(
            parear_equipamento_retorno([96361, 1234], [76903, 5555], 1234),
            5555,
        )

    def test_entregue_fora_do_movimento(self):
        self.assertIsNone(parear_equipamento_retorno([96361], [76903], 777))

    def test_devolucao_com_menos_itens_que_a_entrega(self):
        self.assertIsNone(parear_equipamento_retorno([96361, 1234], [76903], 1234))

    def test_lista_vazia(self):
        self.assertIsNone(parear_equipamento_retorno([], [], 96361))


class EquipamentoRetornoTest(unittest.TestCase):
    ENTREGUE = 96361
    DEVOLVIDO = 76903

    def setUp(self):
        comum = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        self.engine_legado = create_engine("sqlite://", **comum)
        self.engine_new = create_engine("sqlite://", **comum)

        with self.engine_legado.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE aluguel_substituicao (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    substituicao_aluguel_id INTEGER NOT NULL,
                    substituicao_devolucao_id INTEGER NOT NULL
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE aluguel_movimento_itens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    movimento_id INTEGER NOT NULL,
                    equipamento_id INTEGER NOT NULL,
                    deleted_at TEXT
                )
                """
            ))

        with self.engine_new.begin() as conn:
            conn.execute(text(
                "CREATE TABLE equipments (id INTEGER PRIMARY KEY, deleted_at TEXT)"
            ))
            conn.execute(text(
                """
                CREATE TABLE service_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status_id INTEGER, service_order_id INTEGER,
                    department_id INTEGER, movement_type_id INTEGER,
                    contract_item_id INTEGER, alias TEXT, equipment_id INTEGER,
                    type_id INTEGER, product_id INTEGER, is_exchange INTEGER,
                    is_extra INTEGER, quantity_product INTEGER,
                    fulfilled_quantity_product INTEGER, quantity INTEGER,
                    details TEXT, address_id INTEGER, location_id INTEGER,
                    created_at TEXT, updated_at TEXT, deleted_at TEXT
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE movement_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    movement_id INTEGER, movement_type_id INTEGER,
                    service_order_item_id INTEGER, equipment_id INTEGER,
                    extra_id INTEGER, status_id INTEGER, product_item_id INTEGER,
                    alias TEXT, old_organization_id INTEGER,
                    new_organization_id INTEGER, operation_type TEXT,
                    confirmed_at TEXT, confirmed_by INTEGER,
                    created_at TEXT, updated_at TEXT, deleted_at TEXT
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE shipment_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_id INTEGER, status_id INTEGER,
                    movement_item_id INTEGER, volume_id INTEGER,
                    details TEXT, address_id INTEGER, delivery_date TEXT,
                    created_at TEXT, updated_at TEXT
                )
                """
            ))

    # ------------------------------------------------------------------
    # Cenários de dados
    # ------------------------------------------------------------------
    def cadastrar_par(self, itens_sd, itens_sa):
        with self.engine_legado.begin() as conn:
            for equipamento in itens_sd:
                conn.execute(
                    text(
                        "INSERT INTO aluguel_movimento_itens "
                        "(movimento_id, equipamento_id) VALUES (100, :eq)"
                    ),
                    {"eq": equipamento},
                )
            for equipamento in itens_sa:
                conn.execute(
                    text(
                        "INSERT INTO aluguel_movimento_itens "
                        "(movimento_id, equipamento_id) VALUES (200, :eq)"
                    ),
                    {"eq": equipamento},
                )
            conn.execute(text(
                "INSERT INTO aluguel_substituicao "
                "(substituicao_aluguel_id, substituicao_devolucao_id) VALUES (100, 200)"
            ))

    def cadastrar_equipamento(self, equipamento_id, deleted_at=None):
        with self.engine_new.begin() as conn:
            conn.execute(
                text("INSERT INTO equipments (id, deleted_at) VALUES (:id, :del)"),
                {"id": equipamento_id, "del": deleted_at},
            )

    def montar_ajuste(self):
        obj = AjustesMovimentos.__new__(AjustesMovimentos)
        obj.engine_new = self.engine_new
        obj.engine_legado = self.engine_legado
        obj.cache_itens_legado = {}
        obj.cache_retorno_substituicao = {}
        obj.usuarios_validos = {1}
        obj.ignorar_parquet = True
        return obj

    def linha(self, tipo_legado="SA", id_movimento_legado=200):
        return {
            "id_equipamento": self.ENTREGUE,
            "tipo_legado": tipo_legado,
            "id_movimento_legado": id_movimento_legado,
            "organization_id_novo": 1115,
            "address_id_esperado": 10,
            "address_id_novo": 10,
            "usuario_legado": 1,
            "data_legado": "2026-08-11 09:33:16",
            "equipamento_novo": "MULTIFUNCIONAL JATO DE TINTA A3",
            "equipamento_legado": "MULTIFUNCIONAL JATO DE TINTA A3",
        }

    def inserir_itens(self, obj, row, tipo_movimento=2):
        with self.engine_new.begin() as conn:
            return obj._inserir_itens(
                conn,
                row,
                {
                    "movement_id": 500,
                    "service_order_id": 600,
                    "movement_type_id": tipo_movimento,
                    "shipment_id": 700,
                },
                tipo_movimento,
            )

    # ------------------------------------------------------------------
    # Testes
    # ------------------------------------------------------------------
    def test_entrega_guarda_o_devolvido_no_item_da_os(self):
        self.cadastrar_par([self.DEVOLVIDO], [self.ENTREGUE])
        self.cadastrar_equipamento(self.ENTREGUE)
        self.cadastrar_equipamento(self.DEVOLVIDO)

        self.inserir_itens(self.montar_ajuste(), self.linha())

        with self.engine_new.connect() as conn:
            equipamento_os = conn.execute(
                text("SELECT equipment_id FROM service_order_items")
            ).scalar()
            equipamento_movimento, operacao = conn.execute(
                text("SELECT equipment_id, operation_type FROM movement_items")
            ).one()
        self.assertEqual(equipamento_os, self.DEVOLVIDO)
        self.assertEqual(equipamento_movimento, self.ENTREGUE)
        self.assertEqual(operacao, "SUBSTITUIÇÃO")

    def test_entrega_sem_par_nao_repete_o_proprio_equipamento(self):
        self.cadastrar_equipamento(self.ENTREGUE)

        self.inserir_itens(self.montar_ajuste(), self.linha())

        with self.engine_new.connect() as conn:
            equipamento_os = conn.execute(
                text("SELECT equipment_id FROM service_order_items")
            ).scalar()
        self.assertIsNone(equipamento_os)

    def test_devolvido_excluido_no_refatorado_nao_e_vinculado(self):
        self.cadastrar_par([self.DEVOLVIDO], [self.ENTREGUE])
        self.cadastrar_equipamento(self.ENTREGUE)
        self.cadastrar_equipamento(self.DEVOLVIDO, deleted_at="2026-11-01 00:00:00")

        self.inserir_itens(self.montar_ajuste(), self.linha())

        with self.engine_new.connect() as conn:
            equipamento_os = conn.execute(
                text("SELECT equipment_id FROM service_order_items")
            ).scalar()
        self.assertIsNone(equipamento_os)

    def test_devolucao_e_aluguel_mantem_o_equipamento(self):
        self.cadastrar_equipamento(self.ENTREGUE)

        for tipo in ("A", "D", "SD"):
            with self.subTest(tipo=tipo):
                with self.engine_new.begin() as conn:
                    conn.execute(text("DELETE FROM service_order_items"))
                    conn.execute(text("DELETE FROM movement_items"))
                    conn.execute(text("DELETE FROM shipment_items"))
                self.inserir_itens(
                    self.montar_ajuste(), self.linha(tipo_legado=tipo)
                )
                with self.engine_new.connect() as conn:
                    equipamento_os = conn.execute(
                        text("SELECT equipment_id FROM service_order_items")
                    ).scalar()
                self.assertEqual(equipamento_os, self.ENTREGUE)

    def test_par_com_varios_equipamentos_casa_pela_posicao(self):
        self.cadastrar_par([self.DEVOLVIDO, 5555], [1111, self.ENTREGUE])
        for equipamento in (1111, self.ENTREGUE, self.DEVOLVIDO, 5555):
            self.cadastrar_equipamento(equipamento)

        self.inserir_itens(self.montar_ajuste(), self.linha())

        with self.engine_new.connect() as conn:
            equipamento_os = conn.execute(
                text("SELECT equipment_id FROM service_order_items")
            ).scalar()
        self.assertEqual(equipamento_os, 5555)


class ReconhecerSubstituicaoUnificadaTests(unittest.TestCase):
    """Ajuste não deve reinserir a devolução dentro da capa unificada.

    Na substituição unificada o equipamento antigo vai no item da requisição e
    o substituto no item do movimento. Comparar só por
    ``movement_items.equipment_id`` fazia o antigo parecer sem movimento e o
    ajuste reinseria a devolução como item solto na mesma capa (3.550 itens
    duplicados na recarga de 2026-11-28).
    """

    SD = 561564
    SA = 561565
    ANTIGO = 57351
    NOVO = 54020

    def _df(self, linhas):
        return pd.DataFrame(linhas)

    def _capas(self):
        return pd.DataFrame(
            [
                {
                    "id_movimento": 299276,
                    "numero": str(self.SD),
                    "data_mov": "2026-11-28 16:24:00",
                    "id_customer": 2453,
                    "cliente": "SECRETARIA DE EDUCACAO",
                    "tipo": "SUBSTITUIÇÃO",
                    "id_mov_item": 704037,
                    "id_equipamento_entregue": self.NOVO,
                    "id_equipamento_retorno": self.ANTIGO,
                }
            ]
        )

    def _pares(self):
        return pd.DataFrame([{"id_sd": self.SD, "id_sa": self.SA}])

    def test_devolucao_aponta_para_a_capa_pelo_id_sd(self):
        df = self._df(
            [
                {
                    "id_equipamento": self.ANTIGO,
                    "id_movimento_legado": self.SD,
                    "numero_movimento_novo": None,
                    "numero_movimento_novo_int": float("nan"),
                    "movimento_equivalente_int": float("nan"),
                    "substituicao_retorno": False,
                }
            ]
        )
        resultado = AjustesMovimentos._reconhecer_substituicao_unificada(
            df, self._capas(), self._pares()
        )
        self.assertEqual(resultado.loc[0, "movimento_equivalente_int"], self.SD)
        self.assertTrue(bool(resultado.loc[0, "substituicao_retorno"]))
        self.assertEqual(resultado.loc[0, "id_movimento_novo"], 299276)

    def test_envio_aponta_para_a_capa_pelo_id_sa(self):
        df = self._df(
            [
                {
                    "id_equipamento": self.NOVO,
                    "id_movimento_legado": self.SA,
                    "numero_movimento_novo": str(self.SD),
                    "numero_movimento_novo_int": self.SD,
                    "movimento_equivalente_int": self.SD,
                    "substituicao_retorno": False,
                }
            ]
        )
        resultado = AjustesMovimentos._reconhecer_substituicao_unificada(
            df, self._capas(), self._pares()
        )
        self.assertEqual(resultado.loc[0, "movimento_equivalente_int"], self.SA)
        self.assertFalse(bool(resultado.loc[0, "substituicao_retorno"]))

    def test_movimento_divergente_nao_e_reescrito(self):
        df = self._df(
            [
                {
                    "id_equipamento": self.ANTIGO,
                    "id_movimento_legado": 999999,
                    "numero_movimento_novo": "48390",
                    "numero_movimento_novo_int": 48390,
                    "movimento_equivalente_int": 48390,
                    "substituicao_retorno": False,
                }
            ]
        )
        resultado = AjustesMovimentos._reconhecer_substituicao_unificada(
            df, self._capas(), self._pares()
        )
        self.assertEqual(resultado.loc[0, "movimento_equivalente_int"], 48390)
        self.assertFalse(bool(resultado.loc[0, "substituicao_retorno"]))

    def test_sem_capas_nao_altera_nada(self):
        df = self._df(
            [
                {
                    "id_equipamento": self.ANTIGO,
                    "id_movimento_legado": self.SD,
                    "numero_movimento_novo": None,
                    "numero_movimento_novo_int": float("nan"),
                    "movimento_equivalente_int": float("nan"),
                    "substituicao_retorno": False,
                }
            ]
        )
        vazio = pd.DataFrame(columns=self._capas().columns)
        resultado = AjustesMovimentos._reconhecer_substituicao_unificada(
            df, vazio, self._pares()
        )
        self.assertTrue(pd.isna(resultado.loc[0, "movimento_equivalente_int"]))
        self.assertFalse(bool(resultado.loc[0, "substituicao_retorno"]))


if __name__ == "__main__":
    unittest.main()
