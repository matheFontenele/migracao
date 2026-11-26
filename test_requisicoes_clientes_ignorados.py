import unittest

import pandas as pd

from movimentos.migracao_requisicoes_2026 import (
    CLIENTES_IGNORADOS_REQUISICOES_2026,
    filtrar_clientes_ignorados,
)


def _planilha(clientes: list[str]) -> pd.DataFrame:
    return pd.DataFrame({
        "Cliente": clientes,
        "Equipamento | Insumos": ["NOTEBOOK"] * len(clientes),
        "Req": ["SEPARADO"] * len(clientes),
    })


class FiltrarClientesIgnoradosTests(unittest.TestCase):
    """Exclusão de cliente na migração de requisições 2026."""

    def test_exclui_somente_o_cliente_pedido(self):
        df = _planilha([
            "SECRETARIA DE SAUDE DE SÃO LUIS",
            "BASE SÃO LUÍS",
            "PM SÃO LUÍS - SEC. CRIANÇA E ASSISTÊNCIA SOCIAL - SEMCAS",
            "P M DE SAO LUIS - SEC MUNICIPAL DE SEGURANCA COM CIDADANIA - SEMUSC 2026",
            "P M SÃO LUIS - SECRETARIA DE TRÂNSITO E TRANSPORTES-SMTT 2026",
        ])
        auditoria: list[dict] = []

        restante = filtrar_clientes_ignorados(df, auditoria)

        self.assertEqual(len(restante), 4)
        self.assertNotIn("SECRETARIA DE SAUDE DE SÃO LUIS",
                         set(restante["Cliente"]))
        self.assertEqual(len(auditoria), 1)
        self.assertEqual(auditoria[0]["resultado"], "IGNORADA")
        self.assertEqual(auditoria[0]["linha"], 2)

    def test_comparacao_ignora_acento_e_caixa(self):
        df = _planilha([
            "Secretaria de Saúde de São Luís",
            "SECRETARIA DE SAUDE DE SAO LUIS",
            "  secretaria de saude de são luis  ",
            "SECRETARIA DE SAUDE - GRANJA",
        ])
        auditoria: list[dict] = []

        restante = filtrar_clientes_ignorados(df, auditoria)

        self.assertEqual(list(restante["Cliente"]),
                         ["SECRETARIA DE SAUDE - GRANJA"])
        self.assertEqual(len(auditoria), 3)

    def test_indice_original_preservado_na_auditoria(self):
        df = _planilha(["BASE SÃO LUÍS", "SECRETARIA DE SAUDE DE SÃO LUIS",
                        "BASE SÃO LUÍS"])
        df.index = [10, 11, 12]
        auditoria: list[dict] = []

        restante = filtrar_clientes_ignorados(df, auditoria)

        self.assertEqual(list(restante.index), [10, 12])
        self.assertEqual(auditoria[0]["linha"], 13)

    def test_sem_cliente_ignorado_nao_altera_dataframe(self):
        df = _planilha(["BASE SÃO LUÍS", "ASSEMBLEIA LEGISLATIVA"])
        auditoria: list[dict] = []

        restante = filtrar_clientes_ignorados(df, auditoria)

        self.assertIs(restante, df)
        self.assertEqual(auditoria, [])

    def test_constante_contem_apenas_o_cliente_pedido(self):
        self.assertEqual(CLIENTES_IGNORADOS_REQUISICOES_2026,
                         frozenset({"SECRETARIA DE SAUDE DE SÃO LUIS"}))


if __name__ == "__main__":
    unittest.main()
