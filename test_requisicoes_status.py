import unittest
from datetime import datetime
from types import SimpleNamespace

from movimentos.migracao_requisicoes_2026 import status_do_cabecalho

DATA = datetime(2026, 11, 27)


def _linha(alias, req="SEPARADO", tipo="EQUIPAMENTO", pat_id=None,
           separado_em=DATA, separador_id=1):
    motivo = ""
    if separado_em is None:
        motivo = "Data Separação ausente"
    elif pat_id is None:
        motivo = "PAT ausente ou não encontrado"
    elif separador_id is None:
        motivo = "separador sem usuário: vazio"
    return SimpleNamespace(
        req=req, alias=alias, classificacao=SimpleNamespace(tipo=tipo),
        pat_id=pat_id, separado_em=separado_em, separador_id=separador_id,
        motivo_movimento=motivo)


def _item(alias, quantidade, tipo="EQUIPAMENTO"):
    return SimpleNamespace(alias=alias, quantidade=quantidade,
                           classificacao=SimpleNamespace(tipo=tipo))


class StatusCabecalhoTests(unittest.TestCase):
    """Status do cabeçalho pelo atendimento real (movimentos)."""

    def test_sem_entrega_e_nao_atendida(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", separado_em=None)],
            [_item("MULTIFUNCIONAL", 1)])
        self.assertEqual(cab, (1, "approved"))

    def test_insumo_nao_conta_como_entregue(self):
        cab = status_do_cabecalho(
            [_linha("TONER", tipo="INSUMO", pat_id=99)],
            [_item("TONER", 3, tipo="INSUMO")])
        self.assertEqual(cab, (1, "approved"))

    def test_entrega_completa(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", pat_id=10)],
            [_item("MULTIFUNCIONAL", 1)])
        self.assertEqual(cab, (3, "approved"))

    def test_dois_tombos_cobrem_quantidade_dois(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", pat_id=10),
             _linha("MULTIFUNCIONAL", pat_id=11)],
            [_item("MULTIFUNCIONAL", 2)])
        self.assertEqual(cab, (3, "approved"))

    def test_quantidade_incompleta_e_parcial(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", pat_id=10)],
            [_item("MULTIFUNCIONAL", 2)])
        self.assertEqual(cab, (2, "approved"))

    def test_equipamento_entregue_com_insumo_pendente_e_parcial(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", pat_id=10),
             _linha("TONER", tipo="INSUMO", pat_id=99)],
            [_item("MULTIFUNCIONAL", 1), _item("TONER", 4, tipo="INSUMO")])
        self.assertEqual(cab, (2, "approved"))

    def test_separador_sem_usuario_nao_entrega(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", pat_id=10, separador_id=None)],
            [_item("MULTIFUNCIONAL", 1)])
        self.assertEqual(cab, (1, "approved"))

    def test_cancelada(self):
        cab = status_do_cabecalho(
            [_linha("MULTIFUNCIONAL", req="CANCELADA", pat_id=10)],
            [_item("MULTIFUNCIONAL", 1)])
        self.assertEqual(cab, (4, "cancelled"))


if __name__ == "__main__":
    unittest.main()
