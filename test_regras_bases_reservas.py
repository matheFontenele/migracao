"""Regras de base/estoque e reserva no diagnóstico de movimentos.

Casos reais (relatório de 2026-11-14):

* equipamentos alugados no legado para clientes que são base da Alucom
  (3163 MOREIA MANUTENÇÃO, 4082 TONINHO PIRES, 4090 AS SISTEMAS) ficam
  DISPONÍVEL PARA LOCAÇÃO no refatorado — status correto, não divergência;
* equipamentos de clientes "RESERVA" (mesclados com o titular) podem ficar
  DISPONÍVEL PARA LOCAÇÃO, não só RESERVADO;
* equipamento com decisão manual (118769, cópia duplicada em manutenção)
  não pode entrar na correção automática.
"""

import unittest

import pandas as pd

from ajustes.ajustes_movimentos import (
    CLIENTES_BASE,
    identificar_reserva_mesclada,
    motivo_excecao_linha,
    separar_divergencias,
)
from config.config import CLIENTES_BASE_AJUSTE, EQUIPAMENTOS_REVISAO_MANUAL


def _linha(**kwargs):
    base = {
        "id_legado": 1,
        "id_cliente_legado": None,
        "reserva_mesclada": False,
        "status_id_novo": 1,
        "situacao_id": 1,
        "tombo_novo": "999",
        "classificacao": "CORRIGIR_EQUIPAMENTO",
        "id_customer_esperado": 10,
    }
    base.update(kwargs)
    return base


class ClientesBaseTests(unittest.TestCase):
    def test_base_ajuste_entra_no_conjunto(self):
        self.assertTrue({3163, 4082, 4090} <= set(CLIENTES_BASE_AJUSTE))
        self.assertTrue({3163, 4082, 4090} <= CLIENTES_BASE)

    def test_base_com_status_disponivel_e_excecao(self):
        for cliente in (3163, 4082, 4090):
            self.assertEqual(
                motivo_excecao_linha(
                    _linha(id_cliente_legado=cliente, status_id_novo=1), set()
                ),
                "CLIENTE_ESTOQUE",
            )

    def test_base_em_devolucao_disponivel_e_excecao(self):
        # 129006: ALUCOM - BASE SANTOS DUMONT, legado EM DEVOLUÇÃO
        self.assertEqual(
            motivo_excecao_linha(
                _linha(id_cliente_legado=10662, status_id_novo=1), set()
            ),
            "CLIENTE_ESTOQUE",
        )

    def test_base_com_status_alugado_nao_e_excecao(self):
        self.assertEqual(
            motivo_excecao_linha(
                _linha(id_cliente_legado=3163, status_id_novo=2, tombo_novo="1"),
                set(),
            ),
            "",
        )

    def test_base_em_manutencao_alinhado_ao_legado_e_excecao(self):
        # 11651/10662: base com equipamento em manutenção; status 6 = esperado.
        self.assertEqual(
            motivo_excecao_linha(
                _linha(
                    id_cliente_legado=10662,
                    situacao_id=3,
                    status_id_novo=6,
                    tombo_novo="1",
                ),
                set(),
            ),
            "CLIENTE_ESTOQUE",
        )

    def test_base_com_status_divergente_fica_para_revisao(self):
        # Base em devolução (esperado 8), mas refatorado em manutenção (6).
        self.assertEqual(
            motivo_excecao_linha(
                _linha(
                    id_cliente_legado=10662,
                    situacao_id=14,
                    status_id_novo=6,
                    tombo_novo="1",
                ),
                set(),
            ),
            "",
        )


class ReservaTests(unittest.TestCase):
    def _df(self, **kwargs):
        base = {
            "cliente_legado": ["P M DE CAUCAIA - RESERVA"],
            "id_cliente_legado": [4089],
            "situacao_id": [1],
            "status_id_novo": [1],
            "id_customer_novo": [None],
        }
        base.update(kwargs)
        return pd.DataFrame(base)

    def test_disponivel_conta_como_reserva(self):
        self.assertTrue(identificar_reserva_mesclada(self._df()).iloc[0])

    def test_reservado_continua_valendo(self):
        self.assertTrue(
            identificar_reserva_mesclada(self._df(status_id_novo=[3])).iloc[0]
        )

    def test_alugado_nao_e_reserva(self):
        self.assertFalse(
            identificar_reserva_mesclada(self._df(status_id_novo=[2])).iloc[0]
        )

    def test_falso_reserva_ignorado(self):
        self.assertFalse(
            identificar_reserva_mesclada(
                self._df(cliente_legado=["FALSO RESERVA"], id_cliente_legado=[10487])
            ).iloc[0]
        )

    def test_centro_de_preparacao_de_reserva_ignorado(self):
        # 10482/10487: "CENTRO DE PREP DE OFICIAIS DA RESERVA E COLEGIO
        # MILITAR" nao e uma copia de reserva, apesar do nome.
        self.assertFalse(
            identificar_reserva_mesclada(
                self._df(
                    cliente_legado=[
                        "CENTRO DE PREP DE OFICIAIS DA RESERVA E COLEGIO MILITAR"
                    ],
                    id_cliente_legado=[10482],
                    situacao_id=[7],
                )
            ).iloc[0]
        )

    def test_reserva_por_de_para(self):
        df = self._df(
            cliente_legado=["X"], id_cliente_legado=[10434], id_customer_novo=[2483]
        )
        self.assertTrue(identificar_reserva_mesclada(df, {10434: 2483}).iloc[0])

    def test_reserva_com_status_alinhado_ao_legado(self):
        # 4089/4544: equipamento em manutencao no legado e no refatorado.
        self.assertTrue(
            identificar_reserva_mesclada(
                self._df(situacao_id=[3], status_id_novo=[6])
            ).iloc[0]
        )

    def test_reserva_em_devolucao_alinhada(self):
        # 4089/4544: equipamento em devolucao no legado e no refatorado.
        self.assertTrue(
            identificar_reserva_mesclada(
                self._df(situacao_id=[14], status_id_novo=[8])
            ).iloc[0]
        )

    def test_reserva_com_status_divergente_nao_marca(self):
        # Legado LIBERADO (esperado 1), refatorado EM MANUTENCAO (6): revisar.
        self.assertFalse(
            identificar_reserva_mesclada(
                self._df(situacao_id=[7], status_id_novo=[6])
            ).iloc[0]
        )


class RevisaoManualTests(unittest.TestCase):
    def test_equipamento_manual_tem_motivo_proprio(self):
        alvo = next(iter(EQUIPAMENTOS_REVISAO_MANUAL))
        self.assertEqual(
            motivo_excecao_linha(
                _linha(id_legado=alvo, tombo_novo="45745"), set()
            ),
            "REVISAO_MANUAL",
        )

    def test_manual_vence_parquet(self):
        alvo = next(iter(EQUIPAMENTOS_REVISAO_MANUAL))
        self.assertEqual(
            motivo_excecao_linha(
                _linha(id_legado=alvo, tombo_novo="45745"), {"45745"}
            ),
            "REVISAO_MANUAL",
        )


class ParquetTests(unittest.TestCase):
    def test_tombo_protegido(self):
        self.assertEqual(
            motivo_excecao_linha(_linha(tombo_novo="47613"), {"47613"}), "PARQUET"
        )

    def test_equipamento_protegido_por_id(self):
        # EQUIPAMENTO_ID do Parquet é o id do equipamento no legado.
        self.assertEqual(
            motivo_excecao_linha(
                _linha(id_legado=40022, id_equipamento=40022, tombo_novo="4778"),
                set(),
                equipamentos_parquet={40022},
            ),
            "PARQUET",
        )

    def test_tombo_compartilhado_nao_protege_por_id(self):
        # Equipamento 40022 (tombo 4778) NÃO está no Parquet; o tombo pertence
        # ao equipamento 4778. A proteção por id não deve alcançar 40022.
        self.assertEqual(
            motivo_excecao_linha(
                _linha(id_legado=40022, id_equipamento=40022, tombo_novo="4778"),
                set(),
                equipamentos_parquet={4778},
            ),
            "",
        )

    def test_tombo_e_fallback_quando_parquet_nao_tem_id(self):
        tombo = _linha(id_legado=40022, id_equipamento=40022, tombo_novo="4778")
        self.assertEqual(
            motivo_excecao_linha(
                tombo, {"4778"}, equipamentos_parquet=set()
            ),
            "PARQUET",
        )

    def test_cliente_sem_mapa(self):
        self.assertEqual(
            motivo_excecao_linha(
                _linha(
                    classificacao="REINSERIR_MOVIMENTO", id_customer_esperado=None
                ),
                set(),
            ),
            "CLIENTE_SEM_MAPA",
        )


class PrecedenciaParquetTests(unittest.TestCase):
    def test_base_vence_parquet(self):
        self.assertEqual(
            motivo_excecao_linha(
                _linha(
                    id_cliente_legado=3163, status_id_novo=1, tombo_novo="47613"
                ),
                {"47613"},
            ),
            "CLIENTE_ESTOQUE",
        )

    def test_reserva_vence_parquet(self):
        self.assertEqual(
            motivo_excecao_linha(
                _linha(reserva_mesclada=True, tombo_novo="47613"), {"47613"}
            ),
            "RESERVA_MESCLADA",
        )

    def test_parquet_sem_excecao_continua_protegido(self):
        self.assertEqual(
            motivo_excecao_linha(_linha(tombo_novo="47613"), {"47613"}),
            "PARQUET",
        )


class SeparacaoTests(unittest.TestCase):
    def _df(self, **kwargs):
        base = {
            "motivo_excecao": ["CLIENTE_ESTOQUE"],
            "situacao_id": [14],
            "status_id_novo": [1],
            "div_status": [True],
            "div_cliente": [False],
            "div_movimento": [False],
            "cliente_sem_mapa": [False],
            "classificacao": ["CORRIGIR_EQUIPAMENTO"],
            "reserva_mesclada": [False],
        }
        base.update(kwargs)
        return pd.DataFrame(base)

    def test_estoque_em_devolucao_sai_do_relatorio(self):
        div, esperadas = separar_divergencias(self._df())
        self.assertTrue(div.empty)
        self.assertEqual(len(esperadas), 1)

    def test_estoque_com_status_errado_fica_no_relatorio(self):
        div, esperadas = separar_divergencias(self._df(status_id_novo=[8]))
        self.assertEqual(len(div), 1)
        self.assertTrue(esperadas.empty)

    def test_estoque_em_manutencao_alinhado_sai_do_relatorio(self):
        div, esperadas = separar_divergencias(
            self._df(status_id_novo=[6], div_status=[False], div_movimento=[True])
        )
        self.assertTrue(div.empty)
        self.assertEqual(len(esperadas), 1)


if __name__ == "__main__":
    unittest.main()
