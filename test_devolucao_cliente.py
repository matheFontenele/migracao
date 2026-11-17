"""Testes do cliente por fase na reconstrução da devolução.

Caso real que originou o bug (equipamentos 147249/147250/147357/147358):
duas devoluções seguidas para organizações diferentes. A devolução usava o
cliente do movimento anterior (ORIG_CLIENTE_ID) em vez do cliente da própria
devolução, gravando a capa e o endereço do equipamento no cliente errado.
"""

import unittest

import pandas as pd

from movimentos.migracao_devolucao import cliente_legado_id, resolver_clientes

MAPA_CUSTOMER = {10473: 2730, 11342: 2349, 10490: 2726, 522: 2635}
MAPA_ADDRESS = {10473: 2393, 11342: 549, 10490: 2386, 522: 1786}


class ClienteLegadoIdTests(unittest.TestCase):
    def test_normaliza(self):
        self.assertEqual(cliente_legado_id(10473), 10473)
        self.assertEqual(cliente_legado_id(10473.0), 10473)
        self.assertEqual(cliente_legado_id("10473"), 10473)

    def test_invalidos(self):
        self.assertIsNone(cliente_legado_id(None))
        self.assertIsNone(cliente_legado_id(float("nan")))
        self.assertIsNone(cliente_legado_id(0))
        self.assertIsNone(cliente_legado_id(-1))
        self.assertIsNone(cliente_legado_id("abc"))


class ResolverClientesTests(unittest.TestCase):
    def test_duas_devolucoes_seguidas_usa_o_cliente_da_devolucao(self):
        """Penúltimo movimento é devolução de outro cliente (MTE).

        Legado: D 531019 (MTE 11342) -> D 532569 (Icó 10473).
        A capa 532569 deve ficar com Icó (2730/2393), não com MTE (2349/549).
        """
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            11342, 10473, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((dev_customer, dev_address), (2730, 2393))
        self.assertEqual((orig_customer, orig_address), (2349, 549))

    def test_penultimo_aluguel_de_outro_cliente(self):
        """Legado: A 36463 (Caucaia 522) -> D 533259 (Icó Saúde 10490)."""
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            522, 10490, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((dev_customer, dev_address), (2726, 2386))
        self.assertEqual((orig_customer, orig_address), (2635, 1786))

    def test_mesmo_cliente_nas_duas_fases(self):
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            10473, 10473, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((orig_customer, orig_address), (2730, 2393))
        self.assertEqual((dev_customer, dev_address), (2730, 2393))

    def test_sem_movimento_anterior_usa_cliente_da_devolucao(self):
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            None, 10490, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((dev_customer, dev_address), (2726, 2386))
        self.assertEqual((orig_customer, orig_address), (2726, 2386))

    def test_sem_de_para_orig_cai_para_o_cliente_da_devolucao(self):
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            99999, 10473, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((dev_customer, dev_address), (2730, 2393))
        self.assertEqual((orig_customer, orig_address), (2730, 2393))

    def test_sem_de_para_da_devolucao_cai_para_o_orig(self):
        """Sem de/para na devolução, usa o cliente do movimento anterior."""
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            522, 99999, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((dev_customer, dev_address), (2635, 1786))
        self.assertEqual((orig_customer, orig_address), (2635, 1786))

    def test_sem_de_para_nenhum_rejeita(self):
        self.assertEqual(
            resolver_clientes(99998, 99999, MAPA_CUSTOMER, MAPA_ADDRESS),
            ((None, None), (None, None)),
        )

    def test_dev_nulo_usa_orig(self):
        (orig_customer, orig_address), (dev_customer, dev_address) = resolver_clientes(
            522, None, MAPA_CUSTOMER, MAPA_ADDRESS
        )
        self.assertEqual((dev_customer, dev_address), (2635, 1786))
        self.assertEqual((orig_customer, orig_address), (2635, 1786))


if __name__ == "__main__":
    unittest.main()
