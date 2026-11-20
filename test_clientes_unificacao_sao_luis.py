import unittest

import pandas as pd

from cadastros.clientes.migracao_cliente import MigracaoClientes
from cadastros.clientes.migracao_cliente import SAO_LUIS_CONFIG


class UnificacaoSaoLuisTests(unittest.TestCase):
    def test_semusc_variantes_viram_um_unico_destino(self):
        df = pd.DataFrame({
            "id_clean": [4464, 4466, 10502, 10513, 11924, 9999],
            "CLIENTE": [
                "P M DE SAO LUIS - SECRETARIA MUNICI DE SEGURANCA C/ CIDADANIA - SEMUSC RESERVA",
                "P M DE SAO LUIS - SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC",
                "P M DE SAO LUIS - SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC",
                "P M DE SAO LUIS - SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC RESERVA",
                "P M DE SAO LUIS - SEC MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC 2026",
                "OUTRO CLIENTE",
            ],
            "ID_PREFEITURA": [287, 290, 290, 411, 411, 500],
            "PREFEITURA": [
                "PREFEITURA MUNICIPAL DE SAO LUIS",
                "PREFEITURA MUNICIPAL DE SÃO LUÍS",
                "PREFEITURA MUNICIPAL DE SÃO LUIS - MA 2026",
                "PREFEITURA MUNICIPAL DE SÃO LUIS 2026",
                "PREFEITURA MUNICIPAL DE SÃO LUIS",
                "PREFEITURA MUNICIPAL DE OUTRA",
            ],
            "ID_SECRETARIA": [1197, 1249, 1249, 1573, 1573, 600],
            "SECRETARIA": [
                "SECRETARIA MUNICIAPAL DE SEGURANCA COM CIDADANIA - SEMUSC",
                "SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC",
                "SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC",
                "SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC",
                "SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC 2026",
                "OUTRA SECRETARIA",
            ],
        })

        obj = MigracaoClientes.__new__(MigracaoClientes)
        resultado = obj._unificar_sao_luis(df)

        semusc = resultado.iloc[:5]
        self.assertTrue((semusc["ID_PREFEITURA"] == SAO_LUIS_CONFIG["ID_PREF_ALVO"]).all())
        self.assertTrue((semusc["PREFEITURA"] == SAO_LUIS_CONFIG["NOME_PREF_ALVO"]).all())
        self.assertTrue((semusc["ID_SECRETARIA"] == 1197).all())
        self.assertTrue((semusc["SECRETARIA"] == "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC").all())

        outro = resultado.iloc[5]
        self.assertEqual(outro["ID_PREFEITURA"], 500)
        self.assertEqual(outro["ID_SECRETARIA"], 600)


if __name__ == "__main__":
    unittest.main()
