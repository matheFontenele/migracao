import unittest

import pandas as pd

from ajustes.ajustes_movimentos import (
    AjustesMovimentos,
    _rotulo_copias,
    _tombo_key,
    identificar_reserva_mesclada,
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


class SelecaoIgnoraTomboDuplicadoTests(unittest.TestCase):
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

    def test_elegiveis_excluem_duplicados(self):
        obj = AjustesMovimentos.__new__(AjustesMovimentos)
        elegiveis = obj._selecionar_elegiveis(self.base())
        self.assertEqual(list(elegiveis["id_equipamento"]), [1])

    def test_forcar_status_nao_inclui_duplicado(self):
        df = self.base()
        df["motivo_excecao"] = ["", "CLIENTE_ESTOQUE", "", "CLIENTE_ESTOQUE"]
        obj = AjustesMovimentos.__new__(AjustesMovimentos)
        elegiveis = obj._selecionar_elegiveis(df, forcar_status_legado=True)
        self.assertEqual(list(elegiveis["id_equipamento"]), [1])

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


if __name__ == "__main__":
    unittest.main()
