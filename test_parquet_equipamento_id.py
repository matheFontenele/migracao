"""Resolução tombo -> equipamento usando o EQUIPAMENTO_ID do Parquet.

Caso real: tombo 47613 existe em duas máquinas no refatorado (44764 e 45174).
A busca por tombo escolhe a menor cópia (44764) e amarrava o aluguel do Parquet
(COMITÊ ICÓ 2024) à máquina errada. Com o id explícito, cada linha do Parquet
vai para a sua máquina.
"""

import unittest

from movimentos.migracao_aluguel import escolher_cliente_aluguel, resolver_equipamento_do_parquet

# tombo -> equipamento escolhido pela regra antiga (menor id)
POR_TOMBO = {"47613": 44764, "111": 10}
# id -> number do refatorado
POR_ID = {44764: "47613", 45174: "47613", 10: "111"}


class ResolverEquipamentoTests(unittest.TestCase):
    def test_prefere_id_explicito_do_parquet(self):
        escolhido = resolver_equipamento_do_parquet(45174, "47613", POR_TOMBO, POR_ID)
        self.assertEqual(escolhido, 45174)

    def test_mantem_busca_por_tombo_quando_id_ausente(self):
        self.assertEqual(
            resolver_equipamento_do_parquet(None, "47613", POR_TOMBO, POR_ID), 44764
        )
        self.assertEqual(
            resolver_equipamento_do_parquet(float("nan"), "111", POR_TOMBO, POR_ID), 10
        )

    def test_id_inexistente_no_refatorado_cai_para_tombo(self):
        self.assertEqual(
            resolver_equipamento_do_parquet(999999, "47613", POR_TOMBO, POR_ID), 44764
        )

    def test_id_com_tombo_divergente_cai_para_tombo(self):
        # parquet diz id 45174 mas tombo 111 (número do equipamento 10): usa tombo
        self.assertEqual(
            resolver_equipamento_do_parquet(45174, "111", POR_TOMBO, POR_ID), 10
        )

    def test_sem_mapa_de_ids(self):
        self.assertEqual(
            resolver_equipamento_do_parquet(45174, "47613", POR_TOMBO, {}), 44764
        )


class PrioridadeClienteTests(unittest.TestCase):
    def test_legado_prevalece_sobre_parquet(self):
        self.assertEqual(escolher_cliente_aluguel(10447, 3975), (10447, "legado"))
        self.assertEqual(escolher_cliente_aluguel(3975, 3975), (3975, "legado"))


if __name__ == "__main__":
    unittest.main()
