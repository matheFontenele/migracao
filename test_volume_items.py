"""Itens de volume da migração precisam carregar o equipamento/insumo.

Diagnóstico de 2026-11-28: 82.046 de 82.220 `volume_items` foram gravados
apenas com `movement_item_id`. A tela de detalhes lê `equipment`/`product_item`
do item do volume (por isso aparecia "undefined - undefined") e a conferência
por scanner (`VolumeService::validateScannedItems`) filtra por
`volume_items.equipment_id`.
"""

import unittest

from movimentos.migracao_movimentos import montar_item_volume


class MontarItemVolumeTests(unittest.TestCase):
    AGORA = "2026-11-28 10:00:00"

    def test_copia_equipamento_do_item_de_movimento(self):
        item = montar_item_volume(
            29858, {"equipment_id": 57351, "product_item_id": None}, 799486, self.AGORA
        )
        self.assertEqual(item["equipment_id"], 57351)
        self.assertIsNone(item["product_item_id"])
        self.assertEqual(item["movement_item_id"], 799486)
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["volume_id"], 29858)

    def test_insumo_copia_product_item(self):
        item = montar_item_volume(
            7, {"equipment_id": None, "product_item_id": 1234}, 99, self.AGORA
        )
        self.assertIsNone(item["equipment_id"])
        self.assertEqual(item["product_item_id"], 1234)

    def test_item_sem_origem_nao_inventa_vinculo(self):
        item = montar_item_volume(7, {}, 99, self.AGORA)
        self.assertIsNone(item["equipment_id"])
        self.assertIsNone(item["product_item_id"])
        self.assertEqual(item["movement_item_id"], 99)

    def test_nan_nao_vira_id(self):
        item = montar_item_volume(
            7, {"equipment_id": float("nan"), "product_item_id": float("nan")}, 99, self.AGORA
        )
        self.assertIsNone(item["equipment_id"])
        self.assertIsNone(item["product_item_id"])


if __name__ == "__main__":
    unittest.main()
