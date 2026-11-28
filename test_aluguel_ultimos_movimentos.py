"""Último movimento de aluguel: tipo 5 (SA) e recência por data.

Diagnóstico de 2026-11-14:

* Família 1 (8 equipamentos): ``liberar_equipamentos_clientes_estoque`` usava
  ``MAX(mi.id)`` como "último movimento". Os IDs de
  ``aluguel_movimento_itens`` não são cronológicos (lotes recriam itens com
  IDs novos para movimentos antigos), então um aluguel antigo para cliente de
  estoque sobrescrevia o aluguel avulso vigente do equipamento.
* Família 2 (22 equipamentos em 21 capas SA): as capas tipo 5 recriadas em
  2025-10-17 não têm vínculo em ``aluguel_substituicao`` e eram descartadas
  pelo filtro ``tipo_id IN (1, 2)``.
"""

import unittest
from unittest import mock

import pandas as pd

from movimentos.migracao_aluguel import MigracaoAluguel
from movimentos.migracao_movimentos import liberar_equipamentos_clientes_estoque
from movimentos.migracao_substituicao import parametros_substituicao_unificada


class BuscarUltimosMovimentosAluguelTests(unittest.TestCase):
    def _sql_executado(self):
        aluguel = MigracaoAluguel.__new__(MigracaoAluguel)
        aluguel.engine_legado = mock.MagicMock()
        aluguel.engine_legado.connect.return_value.__enter__.return_value = object()

        capturado = {}

        def fake_read_sql(query, conn):
            capturado['sql'] = str(query)
            return pd.DataFrame(columns=['tombo'])

        with mock.patch(
            'movimentos.migracao_aluguel.pd.read_sql', side_effect=fake_read_sql
        ):
            aluguel._buscar_ultimos_movimentos_aluguel(['1234'])

        return capturado['sql']

    def test_aceita_capa_sa_como_aluguel_vigente(self):
        sql = self._sql_executado()
        self.assertIn('mov.tipo_id IN (1, 2, 5)', sql)

    def test_exclui_capas_sa_de_ciclo_de_substituicao(self):
        # A capa SA que é lado entregue de um ciclo é migrada pela
        # substituição; aceitá-la aqui duplicaria movements.number.
        sql = self._sql_executado()
        self.assertIn('FROM aluguel_substituicao sub', sql)
        self.assertIn('sub.substituicao_devolucao_id = mov.id', sql)

    def test_ultimo_movimento_por_data_e_id(self):
        sql = self._sql_executado()
        self.assertIn('ORDER BY mov_x.data DESC, mov_x.id DESC', sql)
        self.assertIn('movi_x.deleted_at IS NULL', sql)


class _ResultadoFake:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _ConexaoFake:
    def __init__(self, ids_legado, equipamentos_novo):
        self.ids_legado = ids_legado
        self.equipamentos_novo = equipamentos_novo
        self.sqls = []

    def execute(self, comando, params=None):
        sql = str(comando)
        self.sqls.append(sql)
        if 'aluguel_equipamentos' in sql:
            return _ResultadoFake([(i,) for i in self.ids_legado])
        if 'current_organization_id' in sql:
            return _ResultadoFake(self.equipamentos_novo)
        if 'FROM addresses' in sql:
            return _ResultadoFake([(530,)])
        return _ResultadoFake()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _EngineFake:
    def __init__(self, conexao):
        self.conexao = conexao

    def connect(self):
        return self.conexao

    def begin(self):
        return self.conexao


class LiberarEquipamentosEstoqueTests(unittest.TestCase):
    def test_ultimo_movimento_usa_data_e_nao_max_item_id(self):
        legacy = _ConexaoFake(ids_legado=[], equipamentos_novo=[])
        liberar_equipamentos_clientes_estoque(
            _EngineFake(legacy), _EngineFake(legacy)
        )

        query_legado = next(
            sql for sql in legacy.sqls if 'aluguel_equipamentos' in sql
        )
        # MAX(mi.id) elegia movimentos antigos: os IDs de item do legado não
        # são cronológicos (lote de 2025-10-17 recriou itens).
        sem_comentarios = '\n'.join(
            linha for linha in query_legado.splitlines()
            if not linha.strip().startswith('--')
        )
        self.assertNotIn('MAX(', sem_comentarios)
        self.assertIn('ORDER BY m2.data DESC, m2.id DESC', query_legado)
        self.assertIn('mi2.deleted_at IS NULL', query_legado)

    def test_sem_equipamento_no_legado_nao_altera_novo(self):
        legacy = _ConexaoFake(ids_legado=[], equipamentos_novo=[])
        novo = _ConexaoFake(ids_legado=[], equipamentos_novo=[])

        total = liberar_equipamentos_clientes_estoque(
            _EngineFake(novo), _EngineFake(legacy)
        )

        self.assertEqual(total, 0)
        self.assertEqual(novo.sqls, [])

    def test_libera_equipamento_da_base(self):
        legacy = _ConexaoFake(ids_legado=[4029], equipamentos_novo=[(4029, 1115)])
        novo = _ConexaoFake(ids_legado=[], equipamentos_novo=[(4029, 1115)])

        total = liberar_equipamentos_clientes_estoque(
            _EngineFake(novo), _EngineFake(legacy)
        )

        self.assertEqual(total, 1)
        self.assertTrue(any('UPDATE equipments' in sql for sql in novo.sqls))


class ParametrosSubstituicaoUnificadaTests(unittest.TestCase):
    def test_movimento_unico_e_substituicao(self):
        # O legado separa SD/SA, mas o refatorado registra um único movimento
        # de SUBSTITUIÇÃO (tipo 2) com o substituto no item do movimento.
        self.assertEqual(
            parametros_substituicao_unificada(),
            (2, 'SUBSTITUIÇÃO', 'SHIPPING_CONFIRMED_REPLACEMENT_RENTED'),
        )

    def test_nao_confunde_com_aluguel_ou_avulso(self):
        tipo, operacao, motivo = parametros_substituicao_unificada()
        self.assertNotIn(tipo, (1, 7))
        self.assertNotEqual(operacao, 'ALUGUEL')
        self.assertNotEqual(operacao, 'AVULSO')


if __name__ == '__main__':
    unittest.main()
