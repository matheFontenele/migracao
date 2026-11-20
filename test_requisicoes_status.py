import unittest
from types import SimpleNamespace

from movimentos.migracao_requisicoes_2026 import (
    STATUS_REQ,
    status_do_cabecalho,
)


def _linhas(*reqs):
    return [SimpleNamespace(req=req) for req in reqs]


class StatusCabecalhoTests(unittest.TestCase):
    """Status/situacao do cabecalho por valor da coluna `Req`."""

    def test_nao_atendida_entra_aprovada(self):
        # Regra de avulso do destino deixava a requisicao pendente de aprovacao.
        for req in ("SOLICITADO ENVIO", "SEM ESTOQUE", ""):
            with self.subTest(req=req):
                self.assertEqual(status_do_cabecalho(_linhas(req)), (1, "approved"))

    def test_mistura_de_nao_atendidas_entra_aprovada(self):
        self.assertEqual(
            status_do_cabecalho(_linhas("SEM ESTOQUE", "")), (1, "approved"))

    def test_parcial(self):
        self.assertEqual(status_do_cabecalho(_linhas("PARCIAL")), (2, "approved"))

    def test_atendida(self):
        self.assertEqual(status_do_cabecalho(_linhas("ATENDIDA")), (3, "approved"))
        self.assertEqual(status_do_cabecalho(_linhas("ENVIADO")), (3, "approved"))
        self.assertEqual(status_do_cabecalho(_linhas("SEPARADO")), (3, "approved"))

    def test_cancelada(self):
        self.assertEqual(status_do_cabecalho(_linhas("CANCELADA")), (4, "cancelled"))

    def test_mapa_por_req(self):
        self.assertEqual(STATUS_REQ["SOLICITADO ENVIO"], (1, "approved"))
        self.assertEqual(STATUS_REQ["SEM ESTOQUE"], (1, "approved"))
        self.assertEqual(STATUS_REQ[""], (1, "approved"))
        self.assertEqual(STATUS_REQ["CANCELADA"], (4, "cancelled"))


if __name__ == "__main__":
    unittest.main()
