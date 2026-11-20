import unittest

import pandas as pd

from cadastros.equipamentos.migracao_equipamentos import MigracaoEquipamentos


class AcessorioParaDetailsTests(unittest.TestCase):
    """`aluguel_equipamentos.acessorio` -> `equipments.details`."""

    def test_valor_texto_e_preservado(self):
        self.assertEqual(
            MigracaoEquipamentos._normalizar_acessorio("ADI 62012"), "ADI 62012"
        )

    def test_espacos_sao_removidos(self):
        self.assertEqual(
            MigracaoEquipamentos._normalizar_acessorio("  6* GERAÇÃO  "),
            "6* GERAÇÃO",
        )

    def test_texto_vazio_vira_none(self):
        self.assertIsNone(MigracaoEquipamentos._normalizar_acessorio(""))
        self.assertIsNone(MigracaoEquipamentos._normalizar_acessorio("   "))

    def test_none_nan_e_pd_na_viram_none(self):
        self.assertIsNone(MigracaoEquipamentos._normalizar_acessorio(None))
        self.assertIsNone(MigracaoEquipamentos._normalizar_acessorio(float("nan")))
        self.assertIsNone(MigracaoEquipamentos._normalizar_acessorio(pd.NA))

    def test_coluna_so_de_none_no_dataframe_nao_chega_como_nan(self):
        """Regressão: pd.DataFrame(lista) transforma coluna só de None em NaN.

        O valor lido de volta precisa ser normalizado antes do INSERT, senão o
        pymysql recusa com "nan can not be used with MySQL".
        """
        df = pd.DataFrame([{'acessorio': None}, {'acessorio': None}])
        self.assertTrue(pd.isna(df.iloc[0]['acessorio']))

        valores = [MigracaoEquipamentos._normalizar_acessorio(v)
                   for v in df['acessorio']]
        self.assertEqual(valores, [None, None])

    def test_dataframe_misto_preserva_texto_e_limpa_vazios(self):
        df = pd.DataFrame([{'acessorio': 'ADI 62012'},
                           {'acessorio': None},
                           {'acessorio': ''}])

        valores = [MigracaoEquipamentos._normalizar_acessorio(v)
                   for v in df['acessorio']]
        self.assertEqual(valores, ['ADI 62012', None, None])


if __name__ == "__main__":
    unittest.main()
