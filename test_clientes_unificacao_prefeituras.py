import unittest

import pandas as pd

from cadastros.clientes.migracao_cliente import (
    MESCLAGENS_PREFEITURA,
    MigracaoClientes,
)


class UnificarPrefeiturasTests(unittest.TestCase):
    """Prefeituras repetidas no legado viram uma única raiz no refatorado."""

    def setUp(self):
        self.obj = MigracaoClientes(engine_new=None, engine_legado=None)

    @staticmethod
    def _df(linhas):
        return pd.DataFrame(linhas)

    def test_taua_39_e_77_viram_uma_raiz(self):
        df = self._df([
            {'ID_PREFEITURA': 39, 'PREFEITURA': 'PREFEITURA MUNICIPAL DE TAUA',
             'ID_SECRETARIA': 342, 'SECRETARIA': 'SEC. DE FINANCAS - TAUA',
             'ORGANIZACAO': 1115},
            {'ID_PREFEITURA': 77, 'PREFEITURA': 'PREFEITURA MUNICIPAL DE TAUA',
             'ID_SECRETARIA': 556, 'SECRETARIA': 'SECRETARIA DE EDUCACAO DE TAUA',
             'ORGANIZACAO': 1122},
        ])

        resultado = self.obj._unificar_prefeituras(df)

        self.assertEqual(resultado['ID_PREFEITURA'].tolist(), [39, 39])
        self.assertEqual(
            resultado['PREFEITURA'].tolist(),
            ['PREFEITURA MUNICIPAL DE TAUA', 'PREFEITURA MUNICIPAL DE TAUA'],
        )
        self.assertTrue(resultado['ID_SECRETARIA'].isna().all())
        self.assertTrue(resultado['SECRETARIA'].isna().all())
        self.assertEqual(resultado['ORGANIZACAO'].tolist(), [1115, 1115])

    def test_taua_nao_afeta_outras_prefeituras(self):
        df = self._df([
            {'ID_PREFEITURA': 39, 'PREFEITURA': 'PREFEITURA MUNICIPAL DE TAUA',
             'ID_SECRETARIA': 342, 'SECRETARIA': 'SEC. DE FINANCAS - TAUA',
             'ORGANIZACAO': 1115},
            {'ID_PREFEITURA': 312, 'PREFEITURA': 'PREFEITURA MUNICIPAL DE TAUÁ',
             'ID_SECRETARIA': 1701, 'SECRETARIA': 'GABINETE DO PREFEITO',
             'ORGANIZACAO': 1311},
        ])

        resultado = self.obj._unificar_prefeituras(df)

        self.assertEqual(resultado['ID_PREFEITURA'].tolist(), [39, 312])
        self.assertEqual(resultado.iloc[1]['ID_SECRETARIA'], 1701)
        self.assertEqual(resultado.iloc[1]['ORGANIZACAO'], 1311)

    def test_configuracao_taua(self):
        config = MESCLAGENS_PREFEITURA['TAUA']
        self.assertEqual(config['ids'], (39, 77))
        self.assertEqual(config['id_alvo'], 39)
        self.assertEqual(config['organizacao_alvo'], 1115)

    def test_configuracao_organizacao_explicita(self):
        for rotulo in ('CAUCAIA', 'BARROQUINHA', 'TAUA'):
            with self.subTest(rotulo=rotulo):
                self.assertEqual(
                    MESCLAGENS_PREFEITURA[rotulo]['organizacao_alvo'], 1115)


if __name__ == '__main__':
    unittest.main()
