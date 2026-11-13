import unittest

from movimentos.migracao_substituicao import deve_reconstruir_aluguel_historico


class HistoricalRentalDeduplicationTests(unittest.TestCase):
    def test_same_source_item_is_skipped(self):
        self.assertFalse(deve_reconstruir_aluguel_historico(101, {101}))

    def test_other_item_in_same_movement_is_retained(self):
        migrated_item_ids = {101}
        # Movement ID is deliberately identical; only source item identity matters.
        self.assertTrue(deve_reconstruir_aluguel_historico(102, migrated_item_ids))

    def test_duplicate_history_decision_does_not_block_current_phases(self):
        phases = ["historical", "devolution", "replacement", "status"]
        execute_historical = deve_reconstruir_aluguel_historico(101, {101})
        executed = (["historical"] if execute_historical else []) + phases[1:]
        self.assertEqual(executed, ["devolution", "replacement", "status"])


if __name__ == "__main__":
    unittest.main()
