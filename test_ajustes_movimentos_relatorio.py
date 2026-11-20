import unittest

import pandas as pd

from ajustes.ajustes_movimentos import (
    AjustesMovimentos,
    _rotulo_copias,
    _tombo_key,
    identificar_reserva_mesclada,
    separar_divergencias,
    separar_tombo_duplicado,
)


class TomboKeyTests(unittest.TestCase):
    def test_numeric_and_float_are_same_key(self):
        self.assertEqual(_tombo_key(710), "710")
        self.assertEqual(_tombo_key(710.0), "710")
        self.assertEqual(_tombo_key(" 710 "), "710")

    def test_missing_tombo(self):
        self.assertIsNone(_tombo_key(None))
        self.assertIsNone(_tombo_key(float("nan")))


class RotuloCopiasTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(_rotulo_copias(1), "UNICO")
        self.assertEqual(_rotulo_copias(2), "DUPLICADO")
        self.assertEqual(_rotulo_copias(3), "TRIPLICADO")
        self.assertEqual(_rotulo_copias(4), "QUADRIPLICADO")
        self.assertEqual(_rotulo_copias(7), "7 COPIAS")

    def test_missing_count_is_unico(self):
        self.assertEqual(_rotulo_copias(None), "UNICO")


class ContagemNoLegadoTests(unittest.TestCase):
    def test_counts_copies_per_tombo(self):
        legado = pd.DataFrame({"tombo_legado": [1, 1, 2, 3, 3, 3, None]})
        contagem = legado["tombo_legado"].apply(_tombo_key).value_counts()
        self.assertEqual(contagem["1"], 2)
        self.assertEqual(contagem["2"], 1)
        self.assertEqual(contagem["3"], 3)


class SelecaoTomboDuplicadoTests(unittest.TestCase):
    def base(self):
        return pd.DataFrame({
            "id_equipamento": [1, 2, 3, 4],
            "copias_tombo_legado": [1, 2, 1, 3],
            "copias_tombo_refatorado": [1, 2, 1, 3],
            "corrigivel": [True, True, False, True],
            "classificacao": [
                "REINSERIR_MOVIMENTO", "REINSERIR_MOVIMENTO",
                "CORRIGIR_EQUIPAMENTO", "CORRIGIR_EQUIPAMENTO",
            ],
            "motivo_excecao": ["", "", "", ""],
        })

    def test_separa_repetidos(self):
        ajustavel, duplicados = separar_tombo_duplicado(self.base())
        self.assertEqual(list(ajustavel["id_equipamento"]), [1, 3])
        self.assertEqual(list(duplicados["id_equipamento"]), [2, 4])

    def test_elegiveis_incluem_duplicados_nao_protegidos(self):
        # S2: tombo repetido não bloqueia; quem bloqueia é o escudo do Parquet.
        obj = AjustesMovimentos.__new__(AjustesMovimentos)
        elegiveis = obj._selecionar_elegiveis(self.base())
        self.assertEqual(list(elegiveis["id_equipamento"]), [1, 2, 4])

    def test_forcar_status_inclui_excecoes_e_mantem_duplicados(self):
        df = self.base()
        df["motivo_excecao"] = ["", "CLIENTE_ESTOQUE", "", "CLIENTE_ESTOQUE"]
        obj = AjustesMovimentos.__new__(AjustesMovimentos)
        elegiveis = obj._selecionar_elegiveis(df, forcar_status_legado=True)
        self.assertEqual(list(elegiveis["id_equipamento"]), [1, 2, 4])

    def test_legado_repetido_mas_unico_na_migracao_e_ajustavel(self):
        # Caso 14355: monitor + toner no legado, mas so o monitor migrou.
        df = self.base()
        df["copias_tombo_legado"] = [2, 2, 1, 1]
        df["copias_tombo_refatorado"] = [1, 1, 1, 1]
        ajustavel, duplicados = separar_tombo_duplicado(df)
        self.assertEqual(len(duplicados), 0)
        self.assertEqual(len(ajustavel), 4)
        obj = AjustesMovimentos.__new__(AjustesMovimentos)
        elegiveis = obj._selecionar_elegiveis(df)
        self.assertEqual(list(elegiveis["id_equipamento"]), [1, 2, 4])


class ReservaMescladaTests(unittest.TestCase):
    def test_reserva_alugada_no_legado_vira_reservado_no_refatorado(self):
        df = pd.DataFrame({
            "cliente_legado": ["P. M. DE ARACATI - TITTO LUZ DEL SOL TI - RESERVA 2018"],
            "id_cliente_legado": [3259],
            "situacao_id": [1],
            "status_id_novo": [3],
        })
        self.assertTrue(bool(identificar_reserva_mesclada(df).iloc[0]))

    def test_nao_marca_quando_status_refatorado_nao_e_reservado(self):
        df = pd.DataFrame({
            "cliente_legado": ["CLIENTE - RESERVA"],
            "id_cliente_legado": [10],
            "situacao_id": [1],
            "status_id_novo": [2],
        })
        self.assertFalse(bool(identificar_reserva_mesclada(df).iloc[0]))

    def test_nao_marca_cliente_sem_reserva_no_nome(self):
        df = pd.DataFrame({
            "cliente_legado": ["SECRETARIA DE EDUCACAO"],
            "id_cliente_legado": [10],
            "situacao_id": [1],
            "status_id_novo": [3],
        })
        self.assertFalse(bool(identificar_reserva_mesclada(df).iloc[0]))

    def test_falsos_reservas_sao_ignorados(self):
        df = pd.DataFrame({
            "cliente_legado": ["CLIENTE - RESERVA"],
            "id_cliente_legado": [10487],
            "situacao_id": [1],
            "status_id_novo": [3],
        })
        self.assertFalse(bool(identificar_reserva_mesclada(df).iloc[0]))

    def test_reserva_mapeada_por_reserved_customer_id(self):
        # Caso 10853: nome sem "RESERVA", mas addresses.reserved_customer_id
        # aponta a reserva para o cliente refatorado 2383.
        df = pd.DataFrame({
            "cliente_legado": ["CELEBRE CONTABILIDADE E ASSESSORIA S/S LTDA"],
            "id_cliente_legado": [10853],
            "situacao_id": [1],
            "status_id_novo": [3],
            "id_customer_novo": [2383],
        })
        self.assertTrue(bool(identificar_reserva_mesclada(df, {10853: 2383}).iloc[0]))

    def test_reserva_mapeada_para_outro_cliente_nao_marca(self):
        df = pd.DataFrame({
            "cliente_legado": ["CELEBRE CONTABILIDADE E ASSESSORIA S/S LTDA"],
            "id_cliente_legado": [10853],
            "situacao_id": [1],
            "status_id_novo": [3],
            "id_customer_novo": [9999],
        })
        self.assertFalse(bool(identificar_reserva_mesclada(df, {10853: 2383}).iloc[0]))


class SeparacaoDivergenciasEstoqueTests(unittest.TestCase):
    def base(self):
        return pd.DataFrame({
            "id_equipamento": [1, 2, 3, 4, 5, 6],
            "situacao_id": [1, 1, 1, 1, 1, 1],
            "status_id_novo": [1, 2, 2, 2, 2, 2],
            "div_status": [True, False, True, False, False, False],
            "div_cliente": [False, False, False, False, False, False],
            "div_movimento": [False, True, False, True, False, False],
            "cliente_sem_mapa": [False, False, False, False, False, False],
            "classificacao": [
                "REINSERIR_MOVIMENTO", "REINSERIR_MOVIMENTO",
                "CORRIGIR_EQUIPAMENTO", "REINSERIR_MOVIMENTO", "OK",
                "REINSERIR_MOVIMENTO",
            ],
            "motivo_excecao": [
                "CLIENTE_ESTOQUE", "CLIENTE_ESTOQUE", "PARQUET", "", "",
                "",
            ],
            "reserva_mesclada": [False, False, False, False, False, True],
        })

    def test_excecao_de_estoque_sai_do_relatorio(self):
        divergencias, estoque = separar_divergencias(self.base())
        self.assertEqual(list(estoque["id_equipamento"]), [1])
        self.assertEqual(list(divergencias["id_equipamento"]), [2, 3, 4])

    def test_estoque_ainda_alugado_permanece_para_revisao(self):
        divergencias, estoque = separar_divergencias(self.base())
        self.assertIn(2, list(divergencias["id_equipamento"]))
        self.assertNotIn(2, list(estoque["id_equipamento"]))

    def test_parquet_permanece_no_relatorio(self):
        divergencias, _ = separar_divergencias(self.base())
        self.assertIn(3, list(divergencias["id_equipamento"]))

    def test_reserva_mesclada_nao_entra(self):
        divergencias, estoque = separar_divergencias(self.base())
        self.assertNotIn(6, list(divergencias["id_equipamento"]))
        self.assertNotIn(6, list(estoque["id_equipamento"]))

    def test_sem_divergencia_nao_entra(self):
        divergencias, estoque = separar_divergencias(self.base())
        self.assertNotIn(5, list(divergencias["id_equipamento"]))
        self.assertNotIn(5, list(estoque["id_equipamento"]))

    def test_dataframe_vazio(self):
        vazio = self.base().iloc[0:0]
        divergencias, estoque = separar_divergencias(vazio)
        self.assertTrue(divergencias.empty)
        self.assertTrue(estoque.empty)


class _ResultadoFake:
    def __init__(self, valor):
        self._valor = valor

    def scalar(self):
        return self._valor


class _ConexaoFake:
    """Registra as chamadas; devolve o cliente atual no SELECT."""

    def __init__(self, cliente_atual):
        self.cliente_atual = cliente_atual
        self.chamadas = []

    def execute(self, comando, params=None):
        self.chamadas.append((str(comando), dict(params or {})))
        return _ResultadoFake(self.cliente_atual)


class AtualizarClienteCabecalhoTests(unittest.TestCase):
    """A capa é duplicada em movements e service_orders: as duas precisam mudar."""

    def _ajustes(self):
        return object.__new__(AjustesMovimentos)

    def _row(self, customer):
        return pd.Series({"id_customer_esperado": customer})

    def test_atualiza_movements_e_service_orders(self):
        conn = _ConexaoFake(cliente_atual=2349)

        alterou = self._ajustes()._atualizar_cliente_cabecalho(conn, self._row(2730), 26051)

        self.assertTrue(alterou)
        sqls = [sql for sql, _ in conn.chamadas]
        self.assertIn("UPDATE movements", sqls[1])
        self.assertIn("UPDATE service_orders", sqls[2])
        self.assertEqual(conn.chamadas[1][1]["customer_id"], 2730)
        self.assertEqual(conn.chamadas[2][1]["customer_id"], 2730)
        # `updated_at` não pode ser tocado: a recência do diagnóstico depende dele.
        self.assertNotIn("updated_at", sqls[1] + sqls[2])

    def test_nao_faz_nada_quando_cliente_ja_confere(self):
        conn = _ConexaoFake(cliente_atual=2730)

        alterou = self._ajustes()._atualizar_cliente_cabecalho(conn, self._row(2730), 26051)

        self.assertFalse(alterou)
        self.assertEqual(len(conn.chamadas), 1)

    def test_sem_cliente_esperado_nao_altera(self):
        conn = _ConexaoFake(cliente_atual=2349)

        alterou = self._ajustes()._atualizar_cliente_cabecalho(conn, self._row(None), 26051)

        self.assertFalse(alterou)
        self.assertEqual(conn.chamadas, [])


class AtualizarEquipamentoExcluidoTests(unittest.TestCase):
    """Baixado (status 9 + deleted_at) não pode voltar a status operacional."""

    def _ajustes(self):
        return object.__new__(AjustesMovimentos)

    def _row(self):
        return {"id_equipamento": 53709, "status_esperado_id": 1,
                "status_id_novo": 9, "address_id_esperado": 3,
                "address_id_novo": None, "id_customer_esperado": 16321,
                "id_customer_novo": None, "motivo_excecao": ""}

    def test_nao_reaplica_status_em_equipamento_excluido(self):
        conn = _ConexaoFake(cliente_atual="2026-11-04 12:05:53")

        status, cliente = self._ajustes()._atualizar_equipamento(
            conn, self._row(), 999, False)

        self.assertIsNone(status)
        self.assertIsNone(cliente)
        self.assertEqual(len(conn.chamadas), 1)
        self.assertIn("SELECT deleted_at", conn.chamadas[0][0])

    def test_atualiza_equipamento_ativo_com_guarda_de_deleted_at(self):
        conn = _ConexaoFake(cliente_atual=None)

        status, cliente = self._ajustes()._atualizar_equipamento(
            conn, self._row(), 999, False)

        self.assertEqual(status, 1)
        self.assertEqual(cliente, 16321)
        self.assertEqual(len(conn.chamadas), 2)
        self.assertIn("deleted_at IS NULL", conn.chamadas[1][0])


if __name__ == "__main__":
    unittest.main()
