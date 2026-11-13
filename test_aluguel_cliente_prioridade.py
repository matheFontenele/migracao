import unittest

from config.config import CLIENTES_ESTOQUE
from movimentos.migracao_aluguel import escolher_cliente_aluguel


class ClienteAluguelPrioridadeTests(unittest.TestCase):
    def test_legado_tem_prioridade_sobre_parquet(self):
        # Caso 48135: legado SEMCAS (4539), parquet Caucaia (2656).
        cliente, origem = escolher_cliente_aluguel(4539, 2656)
        self.assertEqual((cliente, origem), (4539, "legado"))

    def test_parquet_e_fallback_quando_legado_nao_existe(self):
        cliente, origem = escolher_cliente_aluguel(None, 2656)
        self.assertEqual((cliente, origem), (2656, "parquet"))

    def test_cliente_de_estoque_no_legado_usa_parquet(self):
        cliente, origem = escolher_cliente_aluguel(10468, 2656)
        self.assertEqual((cliente, origem), (2656, "parquet"))

    def test_ambos_base_mantem_legado(self):
        cliente, origem = escolher_cliente_aluguel(10468, 10475)
        self.assertEqual((cliente, origem), (10468, "legado_base"))

    def test_sem_clientes_retorna_none(self):
        self.assertEqual(escolher_cliente_aluguel(None, None), (None, "nenhum"))

    def test_lista_de_estoque_e_respeitada(self):
        self.assertIn(10468, CLIENTES_ESTOQUE)
        cliente, _ = escolher_cliente_aluguel(10468, 999, {10468})
        self.assertEqual(cliente, 999)


if __name__ == "__main__":
    unittest.main()
