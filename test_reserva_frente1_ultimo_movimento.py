"""Regra de "último movimento" da Frente 1 de reservas.

A Frente 1 identifica equipamentos cuja última movimentação (pela data do
documento, desempate por id) é uma reserva. Um lançamento retroativo com id
maior não pode esconder essa reserva: é o caso do equipamento 26056, cuja
reserva (movimento 36446, 2020) foi cadastrada antes de um movimento de 2017
inserido depois no legado (id 37267).
"""
import unittest
from unittest.mock import patch

import pandas as pd

from movimentos import migracao_reserva


class _FakeConexao:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeEngine:
    def connect(self):
        return _FakeConexao()


class UltimoMovimentoFrente1Tests(unittest.TestCase):

    def setUp(self):
        # Evita rodar o __init__ pesado; só a extração é exercitada.
        self.reserva = migracao_reserva.MigracaoReserva.__new__(
            migracao_reserva.MigracaoReserva
        )
        self.reserva.engine_legado = _FakeEngine()

    def _capturar_sql(self, frente, ids=None):
        capturado = {}

        def fake_read_sql(sql, _conn):
            capturado['sql'] = str(sql)
            return pd.DataFrame()

        with patch.object(migracao_reserva.pd, 'read_sql', fake_read_sql):
            self.reserva._extrair_dados_reserva(frente, ids_clientes_reserva=ids)
        return capturado['sql']

    def test_frente1_ordena_pela_data_do_documento(self):
        sql = self._capturar_sql(1, ids=[4089])
        self.assertIn('ORDER BY m.data DESC, m.id DESC, mi.id DESC', sql)
        self.assertNotIn('MAX(m.id)', sql)
        self.assertIn('eq.situacao_id = 1', sql)
        self.assertIn('mov.tipo_id IN (1, 7)', sql)

    def test_frente2_mantem_selecao_por_id(self):
        sql = self._capturar_sql(2)
        self.assertIn('MAX(m.id)', sql)
        self.assertNotIn('ORDER BY m.data DESC', sql)

    def test_frente1_sem_ids_nao_consulta(self):
        self.assertTrue(
            self.reserva._extrair_dados_reserva(1, ids_clientes_reserva=[]).empty
        )


if __name__ == "__main__":
    unittest.main()
