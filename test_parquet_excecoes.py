"""Exceções ao escudo do Parquet (equipamentos priorizados)."""

import unittest

import pandas as pd

from ajustes.ajustes_movimentos import AjustesMovimentos
from cadastros.equipamentos.migracao_equipamentos import selecionar_descartes_cliente_estoque
from config.parquet_excecoes import (
    IDS_LEGADO_PRIORIZADOS,
    equipamentos_priorizados,
    id_priorizado,
    motivo_prioridade,
    normalizar_tombo,
    remover_priorizados,
    tombos_priorizados,
)


class _ConexaoFake:
    def __init__(self, linhas):
        self.linhas = linhas

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return self.linhas

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _EngineFake:
    def __init__(self, linhas):
        self.linhas = linhas

    def connect(self):
        return _ConexaoFake(self.linhas)


class RegistroTests(unittest.TestCase):
    def test_id_priorizado(self):
        alvo = next(iter(IDS_LEGADO_PRIORIZADOS))
        self.assertTrue(id_priorizado(alvo))
        self.assertTrue(id_priorizado(str(alvo)))
        self.assertFalse(id_priorizado(999999))
        self.assertFalse(id_priorizado(None))
        self.assertFalse(id_priorizado("abc"))

    def test_motivo(self):
        alvo = next(iter(IDS_LEGADO_PRIORIZADOS))
        self.assertIn("Parquet", motivo_prioridade(alvo))
        self.assertIsNone(motivo_prioridade(999999))

    def test_normalizar_tombo(self):
        self.assertEqual(normalizar_tombo(710), "710")
        self.assertEqual(normalizar_tombo(710.0), "710")
        self.assertEqual(normalizar_tombo(" 710 "), "710")
        self.assertEqual(normalizar_tombo(None), "")


class TombosPriorizadosTests(unittest.TestCase):
    def test_le_tombo_do_legado(self):
        engine = _EngineFake([("47613",), (47613.0,), (None,)])
        self.assertEqual(tombos_priorizados(engine), {"47613"})

    def test_remover_priorizados(self):
        engine = _EngineFake([("47613",)])
        protegidos = {"47613", "111", "222"}
        self.assertEqual(
            remover_priorizados(protegidos, engine), {"111", "222"}
        )

    def test_sem_excecoes_mantem_conjunto(self):
        engine = _EngineFake([])
        self.assertEqual(remover_priorizados({"1", "2"}, engine), {"1", "2"})


class EscudoParquetTests(unittest.TestCase):
    """S1: o escudo usa EQUIPAMENTO_ID; o tombo é só fallback."""

    def _executar(self, df):
        import os
        import tempfile
        from unittest.mock import patch

        from ajustes import ajustes_movimentos as mod

        with tempfile.TemporaryDirectory() as pasta:
            df.to_parquet(os.path.join(pasta, "recorte.parquet"), index=False)
            app = object.__new__(AjustesMovimentos)
            app.engine_legado = _EngineFake([])
            with patch.object(mod, "PASTA_PARQUETS", pasta):
                return app._carregar_escudo_parquet()

    def test_protege_por_equipamento_id(self):
        equipamentos, tombos = self._executar(
            pd.DataFrame(
                {
                    "EQUIPAMENTO_ID": ["40022", "4778", None],
                    "TOMBO": [4778, 4778, 9999],
                }
            )
        )
        self.assertEqual(equipamentos, {40022, 4778})
        # Só o tombo da linha sem EQUIPAMENTO_ID vira fallback.
        self.assertEqual(tombos, {"9999"})

    def test_priorizado_ignora_escudo_por_id(self):
        alvo = next(iter(IDS_LEGADO_PRIORIZADOS))
        equipamentos, _ = self._executar(
            pd.DataFrame({"EQUIPAMENTO_ID": [str(alvo)], "TOMBO": [47613]})
        )
        self.assertNotIn(alvo, equipamentos)


class DescarteBaseTests(unittest.TestCase):
    def test_equipamento_priorizado_nunca_e_descartado(self):
        alvo = next(iter(IDS_LEGADO_PRIORIZADOS))
        df = pd.DataFrame(
            {
                "id_legado": [alvo, 2],
                "tombo_legado": ["47613", "47613"],
                "cliente_ultimo_movimento": [100, 200],
            }
        )
        descartes = selecionar_descartes_cliente_estoque(
            df, clientes_estoque={100}, protegidos={alvo}
        )
        self.assertEqual(descartes, set())

    def test_sem_protecao_descarta_base(self):
        df = pd.DataFrame(
            {
                "id_legado": [1, 2],
                "tombo_legado": ["47613", "47613"],
                "cliente_ultimo_movimento": [100, 200],
            }
        )
        descartes = selecionar_descartes_cliente_estoque(
            df, clientes_estoque={100}, protegidos=set()
        )
        self.assertEqual(descartes, {1})


class ElegiveisTests(unittest.TestCase):
    def test_tombo_repetido_sem_escudo_entra_nos_elegiveis(self):
        df = pd.DataFrame(
            {
                "id_legado": [1, 999999],
                "copias_tombo_refatorado": [3, 3],
                "corrigivel": [True, True],
                "classificacao": ["REINSERIR_MOVIMENTO", "REINSERIR_MOVIMENTO"],
                "motivo_excecao": ["", ""],
                "id_movimento_legado": [1, 2],
            }
        )
        app = object.__new__(AjustesMovimentos)

        elegiveis = app._selecionar_elegiveis(df)

        self.assertEqual(list(elegiveis["id_legado"]), [1, 999999])

    def test_protegido_pelo_parquet_fica_fora_dos_elegiveis(self):
        df = pd.DataFrame(
            {
                "id_legado": [1],
                "copias_tombo_refatorado": [3],
                "corrigivel": [False],
                "classificacao": ["CORRIGIR_EQUIPAMENTO"],
                "motivo_excecao": ["PARQUET"],
                "id_movimento_legado": [2],
            }
        )
        app = object.__new__(AjustesMovimentos)

        self.assertTrue(app._selecionar_elegiveis(df).empty)

    def test_priorizados_ignoram_escudo_por_id(self):
        alvo = next(iter(IDS_LEGADO_PRIORIZADOS))
        self.assertIn(alvo, equipamentos_priorizados())
        self.assertNotIn(999999, equipamentos_priorizados())


if __name__ == "__main__":
    unittest.main()
