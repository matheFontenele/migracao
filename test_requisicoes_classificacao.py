import unittest

from movimentos.migracao_requisicoes_2026 import (
    IndiceSimilaridade, Referencias, classificar_item, palavra_chave_tipo,
)


class PalavraChaveTipoTests(unittest.TestCase):
    """Equipamento x insumo quando o catálogo não resolve o item."""

    def test_equipamento_com_ssd_na_especificacao(self):
        self.assertEqual(
            palavra_chave_tipo("NOTEBOOK I7 8GB SSD 240GB"), "EQUIPAMENTO")
        self.assertEqual(
            palavra_chave_tipo("SERVIDOR XEON E5, 32GB RAM, SSD 512GB, HD 3 TB"),
            "EQUIPAMENTO")

    def test_peca_antes_do_equipamento(self):
        self.assertEqual(palavra_chave_tipo("MEMORIA PARA NOTEBOOK"), "INSUMO")
        self.assertEqual(palavra_chave_tipo("CABO PARA NOTEBOOK"), "INSUMO")
        self.assertEqual(palavra_chave_tipo("TONER PARA IMPRESSORA"), "INSUMO")

    def test_insumo_puro(self):
        self.assertEqual(palavra_chave_tipo("SSD 240GB"), "INSUMO")
        self.assertEqual(palavra_chave_tipo("TONER TK-1175"), "INSUMO")
        self.assertEqual(palavra_chave_tipo("FONTE ATX"), "INSUMO")

    def test_equipamento_puro(self):
        self.assertEqual(
            palavra_chave_tipo("PONTO DE ACESSO WIFI COORPORATIVO"), "EQUIPAMENTO")
        self.assertEqual(palavra_chave_tipo("MULTIFUNCIONAL JATO DE TINTA"),
                         "EQUIPAMENTO")

    def test_sem_palavra_conhecida(self):
        self.assertIsNone(palavra_chave_tipo("AÇUCAR"))
        self.assertIsNone(palavra_chave_tipo(""))


def _referencias(produtos: list[dict]) -> Referencias:
    return Referencias(
        clientes=None,
        produtos=IndiceSimilaridade(produtos),
        produtos_insumo=IndiceSimilaridade(
            [p for p in produtos if not int(p["is_asset"])]),
        tipos=IndiceSimilaridade([]),
        usuarios={},
        organizacoes=set(),
        transportes=set(),
    )


class ClassificarItemTests(unittest.TestCase):
    """O catálogo de insumos repete descrições de equipamento."""

    def test_notebook_cadastrado_como_insumo_vira_equipamento(self):
        refs = _referencias([
            {"id": 3989, "name": "NOTEBOOK I3 8GB SSD 120GB",
             "type_id": 302, "is_asset": 0},
        ])
        achado = classificar_item("NOTEBOOK I7 8GB SSD 240GB", refs, {})
        self.assertEqual(achado.tipo, "EQUIPAMENTO")
        self.assertIsNone(achado.product_id)
        self.assertEqual(achado.type_id, 302)

    def test_toner_cadastrado_como_insumo_continua_insumo(self):
        refs = _referencias([
            {"id": 10, "name": "TONER TK-1175", "type_id": 5, "is_asset": 0},
        ])
        achado = classificar_item("TONER TK-1175", refs, {})
        self.assertEqual(achado.tipo, "INSUMO")
        self.assertEqual(achado.product_id, 10)

    def test_equipamento_com_serie_no_nome(self):
        refs = _referencias([
            {"id": 11, "name": "MICRO I5 8GB SSD 240GB",
             "type_id": 239, "is_asset": 0},
        ])
        achado = classificar_item("MICRO I5 8GB SSD 240GB NS 04233011402101000070",
                                  refs, {})
        self.assertEqual(achado.tipo, "EQUIPAMENTO")
        self.assertIsNone(achado.product_id)


if __name__ == "__main__":
    unittest.main()
