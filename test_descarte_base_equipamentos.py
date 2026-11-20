import unittest

import pandas as pd

from cadastros.equipamentos.migracao_equipamentos import (
    selecionar_descartes_cliente_estoque,
)
from config.config import CLIENTES_ESTOQUE


class DescarteBaseTests(unittest.TestCase):
    def test_discards_base_copy_only_when_non_base_exists(self):
        df = pd.DataFrame({
            "id_legado": [1, 2, 3, 4],
            "tombo_legado": [10, 10, 20, 20],
            "cliente_ultimo_movimento": [10468, 999, 10468, 10662],
        })
        descartar = selecionar_descartes_cliente_estoque(df, {10468, 10662})
        # tombo 10: descarta a copia da base (id 1)
        # tombo 20: todas sao base, nada e descartado
        self.assertEqual(descartar, {1})

    def test_all_base_keeps_everything(self):
        df = pd.DataFrame({
            "id_legado": [1, 2],
            "tombo_legado": [5, 5],
            "cliente_ultimo_movimento": [10468, 10468],
        })
        self.assertEqual(selecionar_descartes_cliente_estoque(df, {10468}), set())

    def test_unique_tombo_is_not_discarded(self):
        df = pd.DataFrame({
            "id_legado": [1],
            "tombo_legado": [7],
            "cliente_ultimo_movimento": [10468],
        })
        self.assertEqual(selecionar_descartes_cliente_estoque(df, {10468}), set())

    def test_tombo_normalization_groups_float_and_text(self):
        df = pd.DataFrame({
            "id_legado": [1, 2],
            "tombo_legado": [10, "10.0"],
            "cliente_ultimo_movimento": [10468, 999],
        })
        self.assertEqual(selecionar_descartes_cliente_estoque(df, {10468}), {1})

    def test_missing_client_column_degrades_safely(self):
        df = pd.DataFrame({"id_legado": [1, 2], "tombo_legado": [10, 10]})
        self.assertEqual(selecionar_descartes_cliente_estoque(df, {10468}), set())

    def test_novos_clientes_estoque_estao_na_lista(self):
        novos = {255, 1023, 1069, 1071, 1318, 1321, 1323, 1324}
        self.assertTrue(novos.issubset(CLIENTES_ESTOQUE))


if __name__ == "__main__":
    unittest.main()
