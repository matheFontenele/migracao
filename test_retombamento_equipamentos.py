import unittest

from cadastros.equipamentos.migracao_equipamentos import (
    montar_details,
    textos_retombamento,
)


class MontarDetailsTests(unittest.TestCase):
    def test_sem_acessorio_nem_notas(self):
        self.assertIsNone(montar_details(None, []))

    def test_acessorio_apenas(self):
        self.assertEqual(montar_details("8* GERAÇÃO", []), "8* GERAÇÃO")

    def test_acessorio_mais_retombamento(self):
        self.assertEqual(
            montar_details("6* GERAÇÃO", ["Retombado do tombo antigo 7129"]),
            "6* GERAÇÃO | Retombado do tombo antigo 7129",
        )


class TextosRetombamentoTests(unittest.TestCase):
    def test_novo_recebe_o_tombo_antigo(self):
        self.assertEqual(textos_retombamento(7129, None),
                         ["Retombado do tombo antigo 7129"])

    def test_baixado_recebe_o_tombo_novo(self):
        self.assertEqual(textos_retombamento(None, [98778]),
                         ["Retombado para o tombo 98778"])

    def test_cadeia_recebe_os_dois_lados(self):
        self.assertEqual(
            textos_retombamento(7129, [98778]),
            ["Retombado do tombo antigo 7129", "Retombado para o tombo 98778"],
        )

    def test_float_do_pandas_vira_inteiro(self):
        self.assertEqual(textos_retombamento(7129.0, [98778.0]),
                         ["Retombado do tombo antigo 7129",
                          "Retombado para o tombo 98778"])

    def test_sem_dados_nao_gera_nota(self):
        self.assertEqual(textos_retombamento(None, []), [])


if __name__ == "__main__":
    unittest.main()
