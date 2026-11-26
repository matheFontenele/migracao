"""Destino genérico "Caucaia - Reservas" para a reserva legada 4089.

O cliente legado ``P M DE CAUCAIA - RESERVA`` não tem setor/departamento, então
não possui customer/endereço próprio no refatorado. A reserva passa a ser
anexada ao departamento da prefeitura de Caucaia (2) com alias próprio.
"""
import unittest

import pandas as pd

from cadastros.clientes.migracao_cliente import (
    config_destino_reserva,
    hierarquia_destino,
    ids_reserva_com_endereco_proprio,
)
from config.config import (
    DEPARA_RESERVAS_BASE,
    RESERVAS_BASE_AVULSA,
    RESERVAS_DESTINO_PREFEITURA,
)


class DestinoReservaCaucaiaTests(unittest.TestCase):

    def test_config_do_destino_caucaia(self):
        config = config_destino_reserva(4089)
        self.assertIsNotNone(config)
        self.assertEqual(config["departamento_legado"], 2)
        self.assertEqual(config["alias"], "Caucaia - Reservas")
        self.assertEqual(config["prefeitura"], "PREFEITURA MUNICIPAL DE CAUCAIA")
        self.assertEqual(config["city"], "CAUCAIA")
        self.assertEqual(config["state"], "CE")

    def test_4089_nao_usa_mais_o_box_caucaia(self):
        # O alias "Box Caucaia" (base avulsa) não deve mais ser usado, nem
        # sobrar um DE/PARA de titular que não existe.
        self.assertNotIn(4089, RESERVAS_BASE_AVULSA)
        self.assertNotIn(4089, DEPARA_RESERVAS_BASE)
        self.assertIn(4089, RESERVAS_DESTINO_PREFEITURA)

    def test_ids_com_endereco_proprio_inclui_destino_prefeitura(self):
        ids = ids_reserva_com_endereco_proprio()
        self.assertIn(4089, ids)
        # Reservas de base avulsa continuam entrando.
        self.assertIn(3259, ids)

    def test_mascara_de_reserva_base_cobre_4089(self):
        df = pd.DataFrame({"id_clean": [4089, 3259, 987654]})
        ids = ids_reserva_com_endereco_proprio()
        self.assertEqual(
            df.loc[df["id_clean"].isin(ids), "id_clean"].tolist(),
            [4089, 3259],
        )

    def test_hierarquia_redireciona_reserva_para_prefeitura(self):
        # 4089 ignora a hierarquia vazia do legado e cai no departamento 2.
        self.assertEqual(hierarquia_destino(4089, None, None), (2, None))
        # Mesmo se vier um setor/prefeitura do legado, o destino manda.
        self.assertEqual(hierarquia_destino(4089, 300, 1234), (2, None))

    def test_hierarquia_preserva_demais_clientes(self):
        self.assertEqual(hierarquia_destino(3259, 77, None), (77, None))
        self.assertEqual(hierarquia_destino(123, 5, 9), (5, 9))

    def test_lookup_ignora_ids_desconhecidos(self):
        self.assertIsNone(config_destino_reserva(987654))
        self.assertIsNone(config_destino_reserva(None))
        self.assertIsNone(config_destino_reserva(float("nan")))
        self.assertIsNone(config_destino_reserva("abc"))


if __name__ == "__main__":
    unittest.main()
