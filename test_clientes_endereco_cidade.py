import unittest

from cadastros.clientes.endereco_cidade import (
    municipio_da_prefeitura,
    resolver_cidade,
)


class MunicipioDaPrefeituraTests(unittest.TestCase):
    """Extração do município a partir do nome da prefeitura."""

    def test_prefixos_comuns(self):
        self.assertEqual(
            municipio_da_prefeitura("PREFEITURA MUNICIPAL DE ICÓ"), "ICÓ"
        )
        self.assertEqual(
            municipio_da_prefeitura("PREFEITURA MUNICIPAL DO CRATO"), "CRATO"
        )
        self.assertEqual(
            municipio_da_prefeitura("MUNICÍPIO DE ICÓ"), "ICÓ"
        )
        self.assertEqual(
            municipio_da_prefeitura("PREFEITURA DE ARACATI"), "ARACATI"
        )

    def test_sufixos_de_uf_ano_e_observacao(self):
        self.assertEqual(
            municipio_da_prefeitura("PREFEITURA MUNICIPAL DE ICÓ/CE"), "ICÓ"
        )
        self.assertEqual(
            municipio_da_prefeitura("PREFEITURA MUNICIPAL DE QUIXADÁ 2026"),
            "QUIXADÁ",
        )

    def test_homonimo_nao_resolve(self):
        # Cascavel existe no CE e no PR; Arac... idem CE/PE.
        self.assertIsNone(municipio_da_prefeitura("PREFEITURA MUNICIPAL DE CASCAVEL"))
        self.assertIsNone(municipio_da_prefeitura("PREFEITURA MUNICIPAL DE ARACOIABA"))

    def test_nome_sem_municipio_unico_nao_resolve(self):
        self.assertIsNone(
            municipio_da_prefeitura("MINISTÉRIO DO TRABALHO E EMPREGO - MTE")
        )
        self.assertIsNone(
            municipio_da_prefeitura(
                "PREFEITURA M DE INDEPENDENCIA - CONTRATO ENCERRADO"
            )
        )
        self.assertIsNone(municipio_da_prefeitura(None))
        self.assertIsNone(municipio_da_prefeitura(""))


class ResolverCidadeTests(unittest.TestCase):
    """Cidade a gravar no endereço migrado."""

    def test_placeholder_deriva_da_prefeitura(self):
        self.assertEqual(
            resolver_cidade("NÃO INFORMADO", "PREFEITURA MUNICIPAL DE ICÓ"), "ICÓ"
        )
        self.assertEqual(
            resolver_cidade("", "PREFEITURA MUNICIPAL DE ARACATI"), "ARACATI"
        )
        self.assertEqual(
            resolver_cidade("0", "PREFEITURA MUNICIPAL DO CRATO"), "CRATO"
        )

    def test_cidade_ja_preenchida_nao_muda(self):
        self.assertIsNone(resolver_cidade("FORTALEZA", "PREFEITURA MUNICIPAL DE ICÓ"))
        self.assertIsNone(resolver_cidade("Redenção", "PREFEITURA MUNICIPAL DE ICÓ"))

    def test_sem_resolucao_mantem_placeholder(self):
        self.assertIsNone(resolver_cidade("NÃO INFORMADO", "PREFEITURA MUNICIPAL DE CASCAVEL"))
        self.assertIsNone(resolver_cidade("NÃO INFORMADO", None))


if __name__ == "__main__":
    unittest.main()
