import unittest

from cadastros.equipamentos.migracao_insumos import MigracaoInsumos
from config.config import (
    LOCATION_ESTOQUE_ALUCOM,
    LOCATION_ESTOQUE_EQUIPAMENTOS,
    LOCATION_ESTOQUE_GERAL,
)


class LocationsSemSectionSpotTests(unittest.TestCase):
    def test_config_nao_define_section_spot(self):
        for spec in (
            LOCATION_ESTOQUE_ALUCOM,
            LOCATION_ESTOQUE_GERAL,
            LOCATION_ESTOQUE_EQUIPAMENTOS,
        ):
            self.assertIsNone(spec["section"])
            self.assertIsNone(spec["spot"])

    def test_resolver_location_insumos_retorna_none(self):
        obj = MigracaoInsumos.__new__(MigracaoInsumos)
        _, _, section, spot, _ = obj._resolver_location(1115, 1360, True, {1115: 1})
        self.assertIsNone(section)
        self.assertIsNone(spot)

    def test_resolver_location_geral_retorna_none(self):
        obj = MigracaoInsumos.__new__(MigracaoInsumos)
        _, _, section, spot, _ = obj._resolver_location(1115, None, False, {1115: 1})
        self.assertIsNone(section)
        self.assertIsNone(spot)


if __name__ == "__main__":
    unittest.main()
