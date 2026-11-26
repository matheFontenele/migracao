"""Contrato e item de substituição resolvidos por endereço/similaridade.

Casos reais (2026-11-24): a requisição da SO 7209 (Aquiraz) foi atrelada ao
contrato 105 (Camocim) porque o Parquet descrevia o equipamento novo para
outro cliente. O contrato correto (240 - Aquiraz/Saúde) está escrito no
endereço de entrega; o item correto (2669) é o multifuncional jato de tinta.
"""

import unittest

import pandas as pd

from movimentos.migracao_substituicao import (
    construir_indice_contratos,
    resolver_contrato_item_substituicao,
    resolver_contrato_por_endereco,
    resolver_item_por_similaridade,
)


ITENS_AQUIRAZ_SAUDE = [
    {"id": 2668, "alias": "IMPRESSORA LASER MONOCROMÁTICA", "description": ""},
    {"id": 2669, "alias": "IMPRESSORA MULTIFUNCIONAL JATO DE TINTA COLORIDA", "description": ""},
    {"id": 2670, "alias": "IMPRESSORA MULTIFUNCIONAL LASER COLORIDA A3", "description": ""},
    {"id": 2671, "alias": "IMPRESSORA MULTIFUNCIONAL LASER MONOCROMÁTICA", "description": ""},
    {"id": 2672, "alias": "SCANNER DE MESA 30 PPM", "description": ""},
    {"id": 2673, "alias": "MICROCOMPUTADOR TIPO II", "description": ""},
    {"id": 2674, "alias": "ACCESS POINT LONG RANGE 2.4GHZ E 5GHZ", "description": ""},
]


class IndiceContratosTests(unittest.TestCase):
    def test_ignora_numeros_curtos_e_indexa_por_digitos(self):
        df = pd.DataFrame(
            [
                {"id": 240, "number": "2025/2208.12"},
                {"id": 105, "number": "2022.04.05.002-10"},
                {"id": 9, "number": "12/24"},
            ]
        )
        indice = construir_indice_contratos(df)
        self.assertIn("2025220812", indice)
        self.assertEqual(indice["2025220812"], {240})
        self.assertNotIn("1224", indice)


class ContratoPorEnderecoTests(unittest.TestCase):
    def setUp(self):
        self.indice = construir_indice_contratos(
            pd.DataFrame(
                [
                    {"id": 240, "number": "2025/2208.12"},
                    {"id": 105, "number": "2022.04.05.002-10"},
                ]
            )
        )

    def test_numero_do_endereco_resolve_o_contrato(self):
        alias = "PM AQUIRA/SEC SAUDE - UBS TELHA-20252208.12"
        self.assertEqual(resolver_contrato_por_endereco(alias, self.indice), 240)

    def test_endereco_sem_numero_de_contrato_nao_resolve(self):
        self.assertIsNone(resolver_contrato_por_endereco("SECRETARIA DE SAUDE", self.indice))

    def test_numero_ambiguo_nao_vira_vinculo(self):
        indice = {"2025220812": {240}, "2025": {240, 241}}
        alias = "CONTRATO 2025/2208.12"
        self.assertIsNone(resolver_contrato_por_endereco(alias, indice))

    def test_numero_parcial_nao_casa_por_engano(self):
        alias = "PM AQUIRA/SEC SAUDE - UBS TELHA-20252208.13"
        self.assertIsNone(resolver_contrato_por_endereco(alias, self.indice))


class ItemPorSimilaridadeTests(unittest.TestCase):
    def test_multifuncional_jato_de_tinta_casa_com_item_certo(self):
        item = resolver_item_por_similaridade(
            "MULTIFUNCIONAL JATO DE TINTA CANON G3110 NS KMVF59110",
            ITENS_AQUIRAZ_SAUDE,
        )
        self.assertEqual(item, 2669)

    def test_equipamento_sem_parecido_nao_vincula(self):
        item = resolver_item_por_similaridade(
            "NOBREAK 1500VA SENOIDAL", ITENS_AQUIRAZ_SAUDE
        )
        self.assertIsNone(item)

    def test_empate_nao_vincula(self):
        itens = [
            {"id": 1, "alias": "IMPRESSORA LASER", "description": ""},
            {"id": 2, "alias": "IMPRESSORA LASER", "description": ""},
        ]
        self.assertIsNone(resolver_item_por_similaridade("IMPRESSORA LASER", itens))

    def test_lista_vazia_nao_vincula(self):
        self.assertIsNone(resolver_item_por_similaridade("IMPRESSORA LASER", []))


class ResolucaoCompletaTests(unittest.TestCase):
    def setUp(self):
        self.indice = construir_indice_contratos(
            pd.DataFrame(
                [
                    {"id": 240, "number": "2025/2208.12"},
                    {"id": 105, "number": "2022.04.05.002-10"},
                ]
            )
        )
        self.itens = {
            240: ITENS_AQUIRAZ_SAUDE,
            105: [{"id": 3821, "alias": "NOTEBOOK TIPO II", "description": "NOTEBOOK CORE I5"}],
        }
        self.contratos_por_cliente = {95: {240}, 108: {105}, 2537: {105}}

    def test_contrato_do_endereco_resolve_item_por_similaridade(self):
        contrato, item, origem = resolver_contrato_item_substituicao(
            "PM AQUIRA/SEC SAUDE - UBS TELHA-20252208.12",
            "MULTIFUNCIONAL JATO DE TINTA CANON G3110 NS KMVF59110",
            self.indice,
            self.itens,
            customer_id=95,
            contratos_por_cliente=self.contratos_por_cliente,
        )
        self.assertEqual((contrato, item, origem), (240, 2669, "ENDERECO"))

    def test_parquet_de_outro_cliente_nao_vira_contrato(self):
        contrato, item, origem = resolver_contrato_item_substituicao(
            None,
            "MULTIFUNCIONAL JATO DE TINTA CANON G3110 NS KMVF59110",
            self.indice,
            self.itens,
            csv_contract_id=105,
            csv_item_id=3821,
            cliente_parquet_ok=False,
            customer_id=95,
            contratos_por_cliente=self.contratos_por_cliente,
        )
        self.assertEqual((contrato, item, origem), (None, None, "SEM_CONTRATO"))

    def test_parquet_do_mesmo_cliente_e_aceito(self):
        contrato, item, origem = resolver_contrato_item_substituicao(
            None,
            "MULTIFUNCIONAL JATO DE TINTA CANON G3110 NS KMVF59110",
            self.indice,
            self.itens,
            csv_contract_id=105,
            csv_item_id=3821,
            cliente_parquet_ok=True,
            customer_id=2537,
            contratos_por_cliente=self.contratos_por_cliente,
        )
        self.assertEqual((contrato, item, origem), (105, 3821, "PARQUET"))

    def test_contrato_de_outro_cliente_e_descartado(self):
        # Endereço com número do contrato 105, mas o cliente é 95 (Aquiraz).
        contrato, item, origem = resolver_contrato_item_substituicao(
            "ENDERECO 2022.04.05.002-10",
            "NOTEBOOK TIPO II",
            self.indice,
            self.itens,
            customer_id=95,
            contratos_por_cliente=self.contratos_por_cliente,
        )
        self.assertEqual((contrato, item, origem), (None, None, "CONTRATO_DE_OUTRO_CLIENTE"))

    def test_item_do_parquet_de_outro_contrato_e_substituido_por_similaridade(self):
        contrato, item, origem = resolver_contrato_item_substituicao(
            "PM AQUIRA/SEC SAUDE - UBS TELHA-20252208.12",
            "MULTIFUNCIONAL JATO DE TINTA CANON G3110 NS KMVF59110",
            self.indice,
            self.itens,
            csv_contract_id=240,
            csv_item_id=3821,  # item do contrato 105
            customer_id=95,
            contratos_por_cliente=self.contratos_por_cliente,
        )
        self.assertEqual((contrato, item, origem), (240, 2669, "ENDERECO"))


if __name__ == "__main__":
    unittest.main()
