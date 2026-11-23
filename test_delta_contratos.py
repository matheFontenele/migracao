"""Testes da traducao de contract_items/contracts no importador do delta."""

import unittest
from decimal import Decimal

from importar_delta_movimentos import (
    contract_item_indexes,
    match_contract_item,
    price_key,
    quantity_key,
)


def item(id_, contrato, alias, aditivo, tipo, ini, fim, price, quantity):
    return {
        "id": id_, "contrato": contrato, "alias": alias, "aditivo": aditivo,
        "tipo": tipo, "ini": ini, "fim": fim,
        "price": Decimal(str(price)), "quantity": Decimal(str(quantity)),
    }


class ChavesTest(unittest.TestCase):
    def test_price_and_quantity_keys_normalize_decimals(self):
        self.assertEqual(price_key(Decimal("125.0000")), "125.00")
        self.assertEqual(price_key(125), "125.00")
        self.assertEqual(quantity_key(Decimal("4.0000")), "4.0000")

    def test_alias_normalization_ignores_accents_and_punctuation(self):
        local = [item(1, 93, "Impressora  Multifuncional – JATO de Tinta", 229, 1,
                      "2026-01-01", "2026-12-31", 286.34, 17)]
        indexes = contract_item_indexes(local)
        source = item(976, 93, "IMPRESSORA MULTIFUNCIONAL - JATO DE TINTA", 229, 1,
                      "2026-01-01", "2026-12-31", 286.34, 17)
        self.assertEqual(match_contract_item(source, 93, indexes), 1)

    def test_matches_by_additive_when_dates_differ(self):
        local = [item(10, 24, "IMPRESSORA POLICROMATICA", 48, 1,
                      "2025-01-01", "2025-12-31", 168, 2)]
        indexes = contract_item_indexes(local)
        source = item(70, 24, "IMPRESSORA POLICROMATICA", 48, 1,
                      "2026-01-01", "2026-12-31", 999, 9)
        self.assertEqual(match_contract_item(source, 24, indexes), 10)

    def test_falls_back_to_price_and_quantity(self):
        local = [item(71, 24, "IMPRESSORA POLICROMATICA", 48, 1,
                      "2025-01-01", "2025-12-31", 168, 2)]
        indexes = contract_item_indexes(local)
        source = item(70, 24, "IMPRESSORA POLICROMATICA", 99, 3,
                      "2026-01-01", "2026-12-31", 168, 2)
        self.assertEqual(match_contract_item(source, 24, indexes), 71)

    def test_ambiguous_match_is_not_guessed(self):
        local = [
            item(1, 93, "NOTEBOOK TIPO I", 228, 1, "2025-01-01", "2025-12-31", 534.71, 20),
            item(2, 93, "NOTEBOOK TIPO I", 229, 1, "2026-01-01", "2026-12-31", 534.71, 20),
            item(3, 93, "NOTEBOOK TIPO I", 230, 1, "2026-06-01", "2026-12-31", 534.71, 20),
        ]
        indexes = contract_item_indexes(local)
        source = item(970, 93, "NOTEBOOK TIPO I", 999, 9,
                      "2027-01-01", "2027-12-31", 534.71, 20)
        self.assertIsNone(match_contract_item(source, 93, indexes))

    def test_unknown_contract_cannot_resolve(self):
        local = [item(1, 24, "IMPRESSORA", 48, 1, "2025-01-01", "2025-12-31", 168, 2)]
        indexes = contract_item_indexes(local)
        source = item(70, 999, "IMPRESSORA", 48, 1, "2025-01-01", "2025-12-31", 168, 2)
        self.assertIsNone(match_contract_item(source, None, indexes))

    def test_item_of_another_contract_is_not_used(self):
        local = [item(1, 43, "MICROCOMPUTADOR i5", 60, 1, "2025-01-01", "2025-12-31",
                      377, 10)]
        indexes = contract_item_indexes(local)
        source = item(2905, 39, "MICROCOMPUTADOR i5", 60, 1, "2025-01-01",
                      "2025-12-31", 377, 10)
        self.assertIsNone(match_contract_item(source, 39, indexes))


if __name__ == "__main__":
    unittest.main()
