import tempfile
import unittest
from pathlib import Path

from movimentos.migracao_requisicoes_2026 import (
    IndiceSimilaridade,
    Referencias,
    carregar_mapa_insumos,
    carregar_produtos_pecas_adi,
    classificar_item,
    departamento_do_item,
    numero,
    numero_pat,
    numeros_substituicao,
    palavra_chave_tipo,
    proximos_numeros,
    texto_substituicao,
)


class NumeroTests(unittest.TestCase):
    """Leitura de PAT e quantidades da planilha de requisições."""

    def test_pat_terminado_em_zero_nao_perde_digito(self):
        # Regressão: `rstrip(".0")` removia todos os zeros finais (106050 -> 10605).
        self.assertEqual(numero("106050"), 106050)
        self.assertEqual(numero("91480"), 91480)
        self.assertEqual(numero("80400"), 80400)
        self.assertEqual(numero("100"), 100)
        self.assertEqual(numero("0"), 0)

    def test_pat_com_decimal_textual_e_lido_inteiro(self):
        self.assertEqual(numero("106050.0"), 106050)
        self.assertEqual(numero("106050,0"), 106050)

    def test_volume_nao_e_pat(self):
        self.assertIsNone(numero("19963-1/1"))
        self.assertIsNone(numero("PAT:79823"))

    def test_vazio_e_none(self):
        self.assertIsNone(numero(""))
        self.assertIsNone(numero("   "))
        self.assertIsNone(numero(None))


class NumeroPatTests(unittest.TestCase):
    """PAT com sufixo de cópia: `19687-1/3` continua sendo o tombo 19687."""

    def test_copias_do_tombo(self):
        self.assertEqual(numero_pat("19687-1/3"), 19687)
        self.assertEqual(numero_pat("19963-1/1"), 19963)
        self.assertEqual(numero_pat("19963/3"), 19963)
        self.assertEqual(numero_pat("19687 - 1/3"), 19687)
        self.assertEqual(numero_pat(" 19687 "), 19687)

    def test_pat_numerico_normal(self):
        self.assertEqual(numero_pat("106050"), 106050)
        self.assertEqual(numero_pat("106050.0"), 106050)

    def test_sem_numero_inicial(self):
        self.assertIsNone(numero_pat("PAT:79823"))
        self.assertIsNone(numero_pat("REVISADO"))
        self.assertIsNone(numero_pat(""))
        self.assertIsNone(numero_pat(None))

    def test_numero_continua_estrito(self):
        # `numero()` é usado em quantidade/produto: não pode virar parser de PAT.
        self.assertIsNone(numero("19687-1/3"))


class SubstituicaoTests(unittest.TestCase):
    def test_numero_unico(self):
        self.assertEqual(numeros_substituicao("508"), (508,))

    def test_lista_numerica(self):
        self.assertEqual(numeros_substituicao("11, 22, 33"), (11, 22, 33))

    def test_texto_nao_e_substituicao(self):
        self.assertEqual(numeros_substituicao("REVISADO"), ())
        self.assertEqual(numeros_substituicao("AA061209"), ())
        self.assertEqual(numeros_substituicao("NOVO"), ())


class TextoSubstituicaoTests(unittest.TestCase):
    def test_texto_livre_e_preservado(self):
        self.assertEqual(texto_substituicao("REVISADO"), "REVISADO")
        self.assertEqual(texto_substituicao(" Reserva "), "Reserva")
        self.assertEqual(texto_substituicao("AA061209"), "AA061209")

    def test_novo_e_vazio_nao_geram_texto(self):
        self.assertEqual(texto_substituicao("NOVO"), "")
        self.assertEqual(texto_substituicao("novo"), "")
        self.assertEqual(texto_substituicao(""), "")
        self.assertEqual(texto_substituicao(None), "")

    def test_numero_nao_e_texto(self):
        self.assertEqual(texto_substituicao("508"), "")
        self.assertEqual(texto_substituicao("11, 22, 33"), "")


class DepartamentoTests(unittest.TestCase):
    """Departamento do item em service_order_departments."""

    def test_equipamento_vai_para_expedicao(self):
        self.assertEqual(departamento_do_item("EQUIPAMENTO", False), "EXPEDIÇÃO")
        self.assertEqual(departamento_do_item("EQUIPAMENTO", True), "EXPEDIÇÃO")

    def test_insumo_pecas_adi_vai_para_elisangela(self):
        self.assertEqual(departamento_do_item("INSUMO", True), "ELISANGELA")

    def test_insumo_comum_vai_para_almoxarifado(self):
        self.assertEqual(departamento_do_item("INSUMO", False), "ALMOXARIFADO")

    def test_indefinido_cai_no_almoxarifado(self):
        self.assertEqual(departamento_do_item("INDEFINIDO", False), "ALMOXARIFADO")
        self.assertEqual(departamento_do_item(None, False), "ALMOXARIFADO")

    def test_tipo_minusculo_e_aceito(self):
        self.assertEqual(departamento_do_item("equipamento", False), "EXPEDIÇÃO")


class _ResultadoFalso:
    def __init__(self, valores):
        self._valores = valores

    def scalars(self):
        return iter(self._valores)

    def mappings(self):
        return iter(self._valores)


class _ConexaoFalsa:
    def __init__(self, valores):
        self._valores = valores

    def execute(self, *_args, **_kwargs):
        return _ResultadoFalso(self._valores)


class ProdutosPecasAdiTests(unittest.TestCase):
    """Leitura da coluna única via `.scalars()` (não via Row)."""

    def test_le_product_id_como_escalar(self):
        self.assertEqual(
            carregar_produtos_pecas_adi(_ConexaoFalsa([10, 20, 20])), {10, 20})

    def test_sem_produtos(self):
        self.assertEqual(carregar_produtos_pecas_adi(_ConexaoFalsa([])), set())


class ProximosNumerosTests(unittest.TestCase):
    """Protocolo dos movimentos gravados em SQL puro (number NULL)."""

    def test_base_zero(self):
        self.assertEqual(proximos_numeros(0, 3), [1, 2, 3])

    def test_base_alta_continua_acima_do_maximo(self):
        self.assertEqual(proximos_numeros(562_997, 2), [562_998, 562_999])

    def test_lista_vazia(self):
        self.assertEqual(proximos_numeros(100, 0), [])
        self.assertEqual(proximos_numeros(None, None), [])

    def test_quantidade_nunca_repete_numero(self):
        numeros = proximos_numeros(10, 5)
        self.assertEqual(len(numeros), len(set(numeros)))
        self.assertTrue(all(n > 10 for n in numeros))


def _referencias(produtos=(), mapa=None, produtos_por_id=None):
    produtos = [dict(p) for p in produtos]
    return Referencias(
        clientes=None,
        produtos=IndiceSimilaridade(produtos),
        produtos_insumo=IndiceSimilaridade(
            [p for p in produtos if not int(p["is_asset"])]),
        tipos=IndiceSimilaridade([]),
        usuarios={},
        organizacoes=set(),
        transportes=set(),
        produtos_por_id=produtos_por_id or {
            int(p["id"]): p["name"] for p in produtos},
        mapa_insumos=mapa or {},
    )


class ClassificacaoItemTests(unittest.TestCase):
    """Separação EQUIPAMENTOS x INSUMOS: `product_id` é o critério do sistema."""

    def test_insumo_do_catalogo_nao_leva_type_id(self):
        refs = _referencias([{
            "id": 3409, "name": "TONER KYOCERA TK-1175", "type_id": 7,
            "is_asset": 0}])
        achado = classificar_item("TONER KYOCERA TK-1175", refs, {})
        self.assertEqual(achado.tipo, "INSUMO")
        self.assertEqual(achado.product_id, 3409)
        # Regressão: `type_id` em insumo escondia o item da lista de INSUMOS.
        self.assertIsNone(achado.type_id)

    def test_equipamento_do_catalogo_nao_leva_product_id(self):
        refs = _referencias([{
            "id": 10, "name": "MONITOR LED 24", "type_id": 3, "is_asset": 1}])
        achado = classificar_item("MONITOR LED 24", refs, {})
        self.assertEqual(achado.tipo, "EQUIPAMENTO")
        self.assertIsNone(achado.product_id)
        self.assertEqual(achado.type_id, 3)

    def test_mapa_de_insumos_tem_precedencia_sobre_o_catalogo(self):
        # O catálogo diz equipamento; o de/para curado diz que é insumo.
        refs = _referencias(
            [{"id": 10, "name": "NOTEBOOK QUALQUER", "type_id": 3,
              "is_asset": 1}],
            mapa={"NOTEBOOK QUALQUER": 3409},
            produtos_por_id={3409: "TONER KYOCERA TK-1175"})
        achado = classificar_item("NOTEBOOK QUALQUER", refs, {})
        self.assertEqual(achado.tipo, "INSUMO")
        self.assertEqual(achado.product_id, 3409)
        self.assertIsNone(achado.type_id)
        self.assertEqual(achado.metodo, "mapa insumos")

    def test_mapa_de_insumos_ignora_acento_e_caixa(self):
        refs = _referencias(mapa={"TONER TK-1175": 3409},
                            produtos_por_id={3409: "TONER"})
        achado = classificar_item("toner  tk-1175", refs, {})
        self.assertEqual(achado.product_id, 3409)

    def test_tiny_e_equipamento_mesmo_com_ssd_na_descricao(self):
        # "SSD" é insumo, mas vem depois de "TINY" na descrição.
        self.assertEqual(
            palavra_chave_tipo("TINY I5 10ºG 16GB SSD 512GB"), "EQUIPAMENTO")


class _ConexaoMapaFalsa:
    def __init__(self, ids, nomes=None):
        self._ids = ids
        self._nomes = nomes or {}

    def execute(self, *_args, **_kwargs):
        return _ResultadoFalso([
            {"id": i, "name": self._nomes.get(i, ""), "is_asset": 0}
            for i in self._ids
        ])


class MapaInsumosTests(unittest.TestCase):
    """De/para curado: arquivo, validação e produto inexistente."""

    def _arquivo(self, conteudo):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        caminho = Path(pasta.name) / "mapa.csv"
        caminho.write_text(conteudo, encoding="utf-8")
        return caminho

    def test_le_alias_e_produto(self):
        arquivo = self._arquivo(
            "alias,produto_id\n\"FONTE ATX\",2687\nSSD 240GB,3299\n")
        mapa = carregar_mapa_insumos(
            _ConexaoMapaFalsa([2687, 3299]), arquivo)
        self.assertEqual(mapa, {"FONTE ATX": 2687, "SSD 240GB": 3299})

    def test_nome_manda_quando_o_id_do_csv_esta_defasado(self):
        # O CSV guarda o ID antigo (2687); o catálogo atual tem outro ID.
        arquivo = self._arquivo(
            "alias,produto_id,produto\n"
            "\"FONTE ATX\",2687,FONTE ATX (200W NOVO)\n")
        mapa = carregar_mapa_insumos(
            _ConexaoMapaFalsa([9001], {9001: "FONTE ATX  ( 200W NOVO)"}),
            arquivo)
        self.assertEqual(mapa, {"FONTE ATX": 9001})

    def test_nome_ambiguo_usa_o_id_quando_ele_esta_entre_os_candidatos(self):
        arquivo = self._arquivo(
            "alias,produto_id,produto\nTK 1162,3977,TONER KYOCERA TK1162 OG\n")
        mapa = carregar_mapa_insumos(
            _ConexaoMapaFalsa([3423, 3977],
                              {3423: "TONER KYOCERA TK1162 OG",
                               3977: "TONER KYOCERA TK 1162 OG"}), arquivo)
        self.assertEqual(mapa, {"TK 1162": 3977})

    def test_produto_que_nao_e_insumo_e_rejeitado(self):
        # Produto 10 existe, mas não está no catálogo de insumos (`is_asset=1`).
        arquivo = self._arquivo("alias,produto_id\nMONITOR,10\n")
        with self.assertRaises(RuntimeError):
            carregar_mapa_insumos(_ConexaoMapaFalsa([]), arquivo)

    def test_alias_repetido_com_produtos_diferentes_e_rejeitado(self):
        arquivo = self._arquivo(
            "alias,produto_id\nFONTE ATX,2687\nfonte  atx,2349\n")
        with self.assertRaises(RuntimeError):
            carregar_mapa_insumos(_ConexaoMapaFalsa([2687, 2349]), arquivo)

    def test_arquivo_ausente_devolve_mapa_vazio(self):
        self.assertEqual(
            carregar_mapa_insumos(_ConexaoMapaFalsa([]),
                                  Path("/nao/existe/mapa.csv")), {})


if __name__ == "__main__":
    unittest.main()
