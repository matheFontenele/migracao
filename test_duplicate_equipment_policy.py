import unittest

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


if __name__ == '__main__':
    unittest.main()
