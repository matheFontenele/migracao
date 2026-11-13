import unittest

import pandas as pd

from movimentos.migracao_movimentos import (
    selecionar_equipamento_por_tombo,
    selecionar_situacao_preferida_por_tombo,
)


class DuplicateEquipmentPolicyTests(unittest.TestCase):
    def test_non_supply_preferred_regardless_of_order(self):
        toner = {'id': 1, 'name': 'Toner', 'situacao': 7}
        machine = {'id': 9, 'name': 'Impressora', 'situacao': 2}
        self.assertEqual(selecionar_equipamento_por_tombo([toner, machine])['id'], 9)
        self.assertEqual(selecionar_equipamento_por_tombo([machine, toner])['id'], 9)

    def test_all_supply_selects_lowest_id_and_word_tokens_are_case_insensitive(self):
        candidates = [
            {'id': 5, 'name': 'Cartucho BROTHER', 'situacao': 7},
            {'id': 2, 'name': 'TONER BROTHER', 'situacao': 1},
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates)['id'], 2)

    def test_single_candidate_and_preferred_status(self):
        candidates = [
            {'id': 3, 'name': 'Impressora', 'situacao': 2},
            {'id': 4, 'name': 'toner', 'situacao': 7},
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates[:1])['id'], 3)
        self.assertEqual(selecionar_situacao_preferida_por_tombo(candidates), 2)

    def test_nome_equipamento_field_is_considered_for_supply(self):
        candidates = [
            {'id': 1, 'nome_equipamento': 'TONER CANON 445'},
            {'id': 2, 'nome_equipamento': 'IMPRESSORA CANON'},
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates)['id'], 2)

    def test_base_client_is_avoided_when_another_copy_exists(self):
        candidates = [
            {'id': 1, 'name': 'IMPRESSORA', 'cliente_id': 10468},  # base/estoque
            {'id': 2, 'name': 'IMPRESSORA', 'cliente_id': 999},
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates)['id'], 2)

    def test_all_base_copies_keep_deterministic_lowest_id(self):
        candidates = [
            {'id': 7, 'name': 'IMPRESSORA', 'cliente_id': 10468},
            {'id': 3, 'name': 'IMPRESSORA', 'cliente_id': 10662},
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates)['id'], 3)

    def test_new_database_base_customer_is_avoided(self):
        candidates = [
            {'id': 1, 'name': 'IMPRESSORA', 'last_movement_item_customer_id': 500},
            {'id': 2, 'name': 'IMPRESSORA', 'last_movement_item_customer_id': 900},
        ]
        self.assertEqual(
            selecionar_equipamento_por_tombo(
                candidates, clientes_estoque_ref={500}
            )['id'],
            2,
        )

    def test_pandas_series_candidates_are_supported(self):
        candidates = [
            pd.Series({'id': 1, 'nome_equipamento': 'TONER', 'cliente_id': 10468}),
            pd.Series({'id': 2, 'nome_equipamento': 'IMPRESSORA', 'cliente_id': 999}),
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates)['id'], 2)

    def test_base_preference_applies_after_supply_preference(self):
        candidates = [
            {'id': 1, 'name': 'TONER', 'cliente_id': 999},
            {'id': 2, 'name': 'IMPRESSORA', 'cliente_id': 10468},
            {'id': 3, 'name': 'IMPRESSORA', 'cliente_id': 999},
        ]
        self.assertEqual(selecionar_equipamento_por_tombo(candidates)['id'], 3)


if __name__ == '__main__':
    unittest.main()
