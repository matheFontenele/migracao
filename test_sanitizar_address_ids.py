import unittest

from movimentos.migracao_movimentos import sanitizar_address_ids_invalidos


class SanitizarAddressIdsInvalidosTest(unittest.TestCase):
    def test_mantem_enderecos_existentes(self):
        itens = [{"address_id": 10}, {"address_id": 20}]

        invalidos = sanitizar_address_ids_invalidos(itens, {10, 20, 30})

        self.assertEqual(invalidos, set())
        self.assertEqual([item["address_id"] for item in itens], [10, 20])

    def test_remove_endereco_inexistente_e_reporta(self):
        itens = [{"address_id": 10}, {"address_id": 2477}]

        invalidos = sanitizar_address_ids_invalidos(itens, {10, 20})

        self.assertEqual(invalidos, {2477})
        self.assertEqual([item["address_id"] for item in itens], [10, None])

    def test_ignora_endereco_ausente(self):
        itens = [{"address_id": None}, {}]

        invalidos = sanitizar_address_ids_invalidos(itens, {10})

        self.assertEqual(invalidos, set())
        self.assertIsNone(itens[0]["address_id"])

    def test_suporta_campo_alternativo_dos_equipamentos(self):
        equipamentos = [{"e_id": 1, "addr_id": 10}, {"e_id": 2, "addr_id": 99}]

        invalidos = sanitizar_address_ids_invalidos(
            equipamentos, {10}, campo="addr_id"
        )

        self.assertEqual(invalidos, {99})
        self.assertEqual(equipamentos[1]["addr_id"], None)


if __name__ == "__main__":
    unittest.main()
