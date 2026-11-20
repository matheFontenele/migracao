"""Testes do de/para explícito das requisições 2026.

Os destinos de ``DEPARA_REQUISICOES_CLIENTES`` não podem depender dos ids do
customer no refatorado: a migração de clientes (mesclagens de prefeituras,
duplicados etc.) desloca esses ids entre execuções. Por isso a resolução usa
o id do legado ou o nome exato do customer + organização + alias do endereço.
"""

import unittest

from utils.clientes_requisicoes import (
    IndiceClientes,
    OverrideRequisicaoInvalido,
    normalizar,
)


def _registro(legacy_id, nome, customer_id, customer_name,
              address_id, alias, city=""):
    return {
        "legacy_id": legacy_id,
        "legacy_name": nome,
        "city": city,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "address_id": address_id,
        "address_alias": alias,
        "legacy_principal": legacy_id,
    }


REGISTROS = [
    _registro(100, "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI", 500,
              "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI", 900,
              "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI", "FORTALEZA"),
    _registro(200, "P M DE CAUCAIA / SECRETARIA DE ADMINISTRACAO", 700,
              "PREFEITURA MUNICIPAL DE CAUCAIA", 910,
              "P M DE CAUCAIA / SECRETARIA DE ADMINISTRACAO"),
    _registro(201, "P M DE CAUCAIA / ACAO SOCIAL", 700,
              "PREFEITURA MUNICIPAL DE CAUCAIA", 911,
              "P M DE CAUCAIA / ACAO SOCIAL - SEDE"),
    _registro(300, "RAFAEL ONORIO - ROTA ZONA NORTE", 800,
              "RAFAEL ONORIO - ROTA ZONA NORTE", 920,
              "RAFAEL ONORIO - ROTA ZONA NORTE"),
    _registro(301, "RAFAEL ONORIO - ROTA ZONA NORTE", 801,
              "RAFAEL ONORIO - ROTA ZONA NORTE", 921,
              "RAFAEL ONORIO - ROTA ZONA NORTE"),
]

CATALOGO = {
    500: {"customer_name": "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI",
          "organization_id": 1115, "address_id": 900,
          "address_alias": "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI"},
    700: {"customer_name": "PREFEITURA MUNICIPAL DE CAUCAIA",
          "organization_id": 1115, "address_id": 910,
          "address_alias": "P M DE CAUCAIA / SECRETARIA DE ADMINISTRACAO"},
    800: {"customer_name": "RAFAEL ONORIO - ROTA ZONA NORTE",
          "organization_id": 1122, "address_id": 920,
          "address_alias": "RAFAEL ONORIO - ROTA ZONA NORTE"},
    801: {"customer_name": "RAFAEL ONORIO - ROTA ZONA NORTE",
          "organization_id": 1115, "address_id": 921,
          "address_alias": "RAFAEL ONORIO - ROTA ZONA NORTE"},
    990: {"customer_name": "PREFEITURA MUNICIPAL DE TIANGUA",
          "organization_id": 1115, "address_id": None, "address_alias": ""},
}

ENDERECOS = {
    500: [{"id": 900, "alias": "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI"}],
    700: [
        {"id": 910, "alias": "P M DE CAUCAIA / SECRETARIA DE ADMINISTRACAO"},
        {"id": 911, "alias": "P M DE CAUCAIA / ACAO SOCIAL - SEDE"},
    ],
    800: [{"id": 920, "alias": "RAFAEL ONORIO - ROTA ZONA NORTE"}],
    801: [{"id": 921, "alias": "RAFAEL ONORIO - ROTA ZONA NORTE"}],
    990: [],
}

ALIASES = {
    900: "UNIVERSIDADE FEDERAL DO CEARA - CAMPUS PICI",
    910: "P M DE CAUCAIA / SECRETARIA DE ADMINISTRACAO",
    911: "P M DE CAUCAIA / ACAO SOCIAL - SEDE",
    920: "RAFAEL ONORIO - ROTA ZONA NORTE",
    921: "RAFAEL ONORIO - ROTA ZONA NORTE",
}


def _indice(overrides):
    return IndiceClientes(
        REGISTROS, overrides=overrides, catalogo=CATALOGO,
        alias_endereco=ALIASES, enderecos_por_customer=ENDERECOS,
    )


class ResolucaoOverrideTest(unittest.TestCase):
    def test_legado_resolve_customer_e_endereco_sem_id_do_refatorado(self):
        indice = _indice({"UFC - CAMPUS PICI": {"legado": 100}})

        cliente = indice.overrides_resolvidos[normalizar("UFC - CAMPUS PICI")]

        self.assertEqual(cliente.customer_id, 500)
        self.assertEqual(cliente.address_id, 900)
        self.assertEqual(cliente.metodo, "explicito")

    def test_legado_inexistente_aborta_com_mensagem_explicita(self):
        with self.assertRaisesRegex(OverrideRequisicaoInvalido,
                                    r"aluguel_clientes\.id=999"):
            _indice({"BASE X": {"legado": 999}})

    def test_nome_com_org_desempata_homonimos(self):
        indice = _indice({
            "ROTA - SR. ONORIO": {
                "cliente": "RAFAEL ONORIO - ROTA ZONA NORTE", "org": 1115,
            },
        })

        cliente = indice.overrides_resolvidos[normalizar("ROTA - SR. ONORIO")]

        self.assertEqual((cliente.customer_id, cliente.address_id), (801, 921))

    def test_nome_sem_org_ambiguo_aborta(self):
        with self.assertRaisesRegex(OverrideRequisicaoInvalido, "ambíguo"):
            _indice({"ROTA - SR. ONORIO": {
                "cliente": "RAFAEL ONORIO - ROTA ZONA NORTE"}})

    def test_endereco_escolhe_alias_especifico(self):
        indice = _indice({
            "PM CAUCAIA - ASSISTÊNCIA SOCIAL": {
                "cliente": "PREFEITURA MUNICIPAL DE CAUCAIA",
                "endereco": "P M DE CAUCAIA / AÇÃO SOCIAL - SEDE",
            },
        })

        cliente = indice.overrides_resolvidos[
            normalizar("PM CAUCAIA - ASSISTÊNCIA SOCIAL")]

        self.assertEqual(cliente.customer_id, 700)
        self.assertEqual(cliente.address_id, 911)

    def test_endereco_inexistente_aborta_com_enderecos_disponiveis(self):
        with self.assertRaisesRegex(OverrideRequisicaoInvalido,
                                    "SECRETARIA DE SAÚDE"):
            _indice({"PM CAUCAIA - SAÚDE": {
                "cliente": "PREFEITURA MUNICIPAL DE CAUCAIA",
                "endereco": "SECRETARIA DE SAÚDE",
            }})

    def test_sem_endereco(self):
        indice = _indice({
            "PM DE TIANGUÁ": {
                "cliente": "PREFEITURA MUNICIPAL DE TIANGUA",
                "sem_endereco": True,
            },
        })

        cliente = indice.overrides_resolvidos[normalizar("PM DE TIANGUÁ")]

        self.assertEqual(cliente.customer_id, 990)
        self.assertIsNone(cliente.address_id)

    def test_id_do_customer_inexistente_aborta_cedo(self):
        with self.assertRaisesRegex(OverrideRequisicaoInvalido,
                                    "customer 9999 não existe"):
            _indice({"BASE Y": {"customer_id": 9999}})

    def test_ausente_do_de_para_nao_e_override(self):
        indice = _indice({"BASE X": {"legado": 100}})

        self.assertIsNone(indice._buscar_override(normalizar("OUTRO"), ""))


if __name__ == "__main__":
    unittest.main()
