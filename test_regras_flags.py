import unittest

from movimentos.migracao_movimentos import BaseMigracaoMovimento


class TestRegrasItemContratosComFlags(unittest.TestCase):
    """Classe forçada pelas flags do Parquet no cérebro compartilhado."""

    def setUp(self):
        self.obj = object.__new__(BaseMigracaoMovimento)
        self.obj.dados = {
            "dict_tipo_por_equipamento": {5: 9},
            "dict_primeiro_item_por_contrato": {10: 111},
            "dict_contrato_item_por_chave": {("a", 10): {"id": 112}},
            "dict_contrato_item_aluguel_por_chave": {("b", 10): {"id": 113}},
            "dict_primeiro_item_por_cliente": {7: 114},
            "saldos_por_id": {111: 5, 112: 2, 113: 1, 114: 1},
        }
        self.obj.saldos_modificados = set()

    def test_kit_flag_usa_primeiro_item_e_nao_abate(self):
        contrato, item, avulso, kit, excedente, match, motivo = self.obj.regras_item_contratos(
            10, 999, 5, 7, {}, classe_forcada="KIT"
        )
        self.assertEqual((contrato, item), (10, 111))
        self.assertTrue(kit)
        self.assertFalse(avulso or excedente or match)
        self.assertEqual(self.obj.dados["saldos_por_id"][111], 5)
        self.assertIn("IS_EXTRA_KIT", motivo)

    def test_kit_flag_sem_item_no_contrato_vira_excedente(self):
        self.obj.dados["dict_primeiro_item_por_contrato"] = {}
        _, item, _, kit, excedente, _, motivo = self.obj.regras_item_contratos(
            10, None, 5, 7, {}, classe_forcada="KIT"
        )
        self.assertIsNone(item)
        self.assertFalse(kit)
        self.assertTrue(excedente)
        self.assertIn("EXCEDENTE", motivo)

    def test_avulso_flag_ignora_contrato_e_item(self):
        contrato, item, avulso, kit, excedente, _, motivo = self.obj.regras_item_contratos(
            10, 999, 5, 7, {}, classe_forcada="AVULSO"
        )
        self.assertIsNone(contrato)
        self.assertIsNone(item)
        self.assertTrue(avulso)
        self.assertFalse(kit or excedente)
        self.assertIn("IS_AVULSO", motivo)

    def test_extra_fallback_consome_saldo_da_cascata(self):
        _, item, _, _, excedente, match, _ = self.obj.regras_item_contratos(
            10, None, 5, 7, {}, classe_forcada="EXTRA_FALLBACK"
        )
        self.assertEqual(item, 112)
        self.assertFalse(excedente)
        self.assertFalse(match)
        self.assertEqual(self.obj.dados["saldos_por_id"][112], 1)

    def test_extra_fallback_sem_saldo_vira_excedente(self):
        self.obj.dados["saldos_por_id"][112] = 0
        _, item, _, _, excedente, _, motivo = self.obj.regras_item_contratos(
            10, None, 5, 7, {}, classe_forcada="EXTRA_FALLBACK"
        )
        self.assertEqual(item, 112)
        self.assertTrue(excedente)
        self.assertIn("EXCEDENTE", motivo)

    def test_heuristica_types_csv_preservada_sem_flag(self):
        _, item, _, kit, excedente, _, _ = self.obj.regras_item_contratos(
            10, None, 5, 7, {9: 1}
        )
        self.assertEqual(item, 111)
        self.assertTrue(kit)
        self.assertFalse(excedente)

    def test_item_do_parquet_continua_normal(self):
        _, item, _, _, excedente, match, motivo = self.obj.regras_item_contratos(
            10, 112, 5, 7, {}
        )
        self.assertEqual(item, 112)
        self.assertTrue(match)
        self.assertFalse(excedente)
        self.assertIsNone(motivo)
        self.assertEqual(self.obj.dados["saldos_por_id"][112], 1)

    def test_substituicao_sem_fallback_mantem_item_nao_vinculado(self):
        _, item, _, _, excedente, _, motivo = self.obj.regras_item_contratos(
            10, None, 5, 7, {}, permitir_fallback_item=False
        )
        self.assertIsNone(item)
        self.assertFalse(excedente)
        self.assertIn("não vinculado", motivo)


if __name__ == "__main__":
    unittest.main()
