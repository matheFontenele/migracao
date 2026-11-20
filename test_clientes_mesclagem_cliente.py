import unittest

import pandas as pd

from cadastros.clientes.migracao_cliente import MigracaoClientes
from utils.depara_clientes import (
    depara_mesclagens_cliente,
    propagar_depara_duplicados,
)


class UnificarClientesRepetidosTests(unittest.TestCase):
    """Exceção de clientes repetidos que não são prefeitura/secretaria (IZZI)."""

    def setUp(self):
        self.obj = MigracaoClientes(engine_new=None, engine_legado=None)

    @staticmethod
    def _df(linhas):
        base = pd.DataFrame(linhas)
        base['id_clean'] = base['ID_CLIENTE'].astype(int)
        return base

    def test_mantem_apenas_o_principal_e_renomeia(self):
        df = self._df([
            {'ID_CLIENTE': 3576, 'CLIENTE': 'IZZI ... LTDA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            {'ID_CLIENTE': 11455, 'CLIENTE': 'IZZI ... LTDA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            {'ID_CLIENTE': 2840, 'CLIENTE': 'IZZI ... TELEATENDIMENTOS',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
        ])

        resultado = self.obj._unificar_clientes_repetidos(df)

        self.assertEqual(resultado['ID_CLIENTE'].tolist(), [3576])
        self.assertEqual(
            resultado.iloc[0]['CLIENTE'],
            'IZZI SOLUÇÕES EM COBRANÇAS E TELEATENDIMENTO LTDA',
        )

    def test_reserva_nao_entra_na_mesclagem(self):
        df = self._df([
            {'ID_CLIENTE': 3576, 'CLIENTE': 'IZZI ... LTDA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            {'ID_CLIENTE': 10522, 'CLIENTE': 'IZZI ... LTDA - RESERVA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
        ])

        resultado = self.obj._unificar_clientes_repetidos(df)

        self.assertEqual(sorted(resultado['ID_CLIENTE'].tolist()), [3576, 10522])
        self.assertNotIn(10522, depara_mesclagens_cliente())

    def test_sem_principal_nao_remove_copias(self):
        df = self._df([
            {'ID_CLIENTE': 11455, 'CLIENTE': 'IZZI ... LTDA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            {'ID_CLIENTE': 2840, 'CLIENTE': 'IZZI ... TELEATENDIMENTOS',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
        ])

        resultado = self.obj._unificar_clientes_repetidos(df)

        self.assertEqual(sorted(resultado['ID_CLIENTE'].tolist()), [2840, 11455])

    def test_nao_remove_copia_que_e_pai_de_outro_cliente(self):
        df = self._df([
            {'ID_CLIENTE': 3576, 'CLIENTE': 'IZZI ... LTDA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            {'ID_CLIENTE': 11455, 'CLIENTE': 'IZZI ... LTDA',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            {'ID_CLIENTE': 2840, 'CLIENTE': 'IZZI ... TELEATENDIMENTOS',
             'ID_PREFEITURA': None, 'ID_SECRETARIA': None},
            # 2840 é secretaria de um cliente: não pode ser removido.
            {'ID_CLIENTE': 9999, 'CLIENTE': 'FILHO',
             'ID_PREFEITURA': 1, 'ID_SECRETARIA': 2840},
        ])

        resultado = self.obj._unificar_clientes_repetidos(df)

        self.assertIn(2840, resultado['ID_CLIENTE'].tolist())
        self.assertNotIn(11455, resultado['ID_CLIENTE'].tolist())
        self.assertIn(9999, resultado['ID_CLIENTE'].tolist())


class DeparaMesclagemClienteTests(unittest.TestCase):
    def test_depara_aponta_copias_para_o_principal(self):
        depara = depara_mesclagens_cliente()
        self.assertEqual(depara[2840], 3576)
        self.assertEqual(depara[11455], 3576)
        self.assertNotIn(3576, depara)

    def test_propaga_copias_para_o_endereco_do_principal(self):
        mapa = {3576: 4000}
        propagar_depara_duplicados(mapa, '/caminho/inexistente.csv')
        self.assertEqual(mapa[11455], 4000)
        self.assertEqual(mapa[2840], 4000)


if __name__ == '__main__':
    unittest.main()
