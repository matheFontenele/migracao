import json
import os
import unittest

from cadastros.clientes.endereco_uf import (
    ARQUIVO_MUNICIPIOS,
    SEM_UF,
    UFS_VALIDAS,
    normalizar_uf,
    uf_do_municipio,
)


class UfDoMunicipioTests(unittest.TestCase):
    """Derivação de UF a partir do município (base do IBGE)."""

    def test_base_carregada(self):
        self.assertTrue(os.path.exists(ARQUIVO_MUNICIPIOS))
        with open(ARQUIVO_MUNICIPIOS, encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
        self.assertGreater(len(dados), 5000)
        # valores são sempre listas de UFs válidas
        for ufs in list(dados.values())[:200]:
            self.assertTrue(set(ufs) <= UFS_VALIDAS)

    def test_ufs_validas_sao_os_27_estados(self):
        self.assertEqual(len(UFS_VALIDAS), 27)
        self.assertIn("CE", UFS_VALIDAS)
        self.assertIn("DF", UFS_VALIDAS)

    def test_resolve_municipio_unico(self):
        self.assertEqual(uf_do_municipio("FORTALEZA"), "CE")
        self.assertEqual(uf_do_municipio("CAUCAIA"), "CE")
        self.assertEqual(uf_do_municipio("ICÓ"), "CE")
        self.assertEqual(uf_do_municipio("São Luís"), "MA")

    def test_ignora_caixa_e_acento(self):
        self.assertEqual(uf_do_municipio("fortaleza"), "CE")
        self.assertEqual(uf_do_municipio("SÃO LUIS"), "MA")
        self.assertEqual(uf_do_municipio("  porto   alegre  "), "RS")

    def test_homonimo_nao_resolve(self):
        # Cascavel existe no CE e no PR; Iguaçu/Independência idem.
        self.assertIsNone(uf_do_municipio("CASCAVEL"))
        self.assertIsNone(uf_do_municipio("IGUATU"))
        self.assertIsNone(uf_do_municipio("INDEPENDÊNCIA"))

    def test_distrito_e_grafia_errada_nao_resolvem(self):
        self.assertIsNone(uf_do_municipio("PECÉM"))
        self.assertIsNone(uf_do_municipio("FORTAELZA"))

    def test_vazio_e_none(self):
        self.assertIsNone(uf_do_municipio(None))
        self.assertIsNone(uf_do_municipio(""))
        self.assertIsNone(uf_do_municipio("   "))


class NormalizarUfTests(unittest.TestCase):
    """Normalização do valor gravado em addresses.state."""

    def test_uf_valida_permanece(self):
        self.assertEqual(normalizar_uf("CE", "FORTALEZA"), "CE")
        self.assertEqual(normalizar_uf("ce", None), "CE")
        self.assertEqual(normalizar_uf("SP", "CASCAVEL"), "SP")

    def test_placeholder_deriva_do_municipio(self):
        self.assertEqual(normalizar_uf("0", "CAUCAIA"), "CE")
        self.assertEqual(normalizar_uf("", "São Luís"), "MA")
        self.assertEqual(normalizar_uf(SEM_UF, "FORTALEZA"), "CE")

    def test_sem_resolucao_vira_nao_informado(self):
        self.assertEqual(normalizar_uf("0", "CASCAVEL"), SEM_UF)
        self.assertEqual(normalizar_uf("0", "PECÉM"), SEM_UF)
        self.assertEqual(normalizar_uf(None, None), SEM_UF)
        self.assertEqual(normalizar_uf("NÃO INFORMADO", ""), SEM_UF)

    def test_valor_invalido_mas_resolvivel(self):
        # '0E' não é UF; o município ainda resolve.
        self.assertEqual(normalizar_uf("0E", "FORTALEZA"), "CE")


if __name__ == "__main__":
    unittest.main()
