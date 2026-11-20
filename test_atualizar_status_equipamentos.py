import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text

from atualizar_status_equipamentos import carregar_alteracoes, executar, planejar


def equipamento(id_, numero, status, criado="2025-01-01 10:00:00",
                atualizado="2026-11-06 10:00:00", serie="S1", deleted_at=None):
    return {
        "id": id_, "number": numero, "serial_number": serie, "status_id": status,
        "created_at": criado, "updated_at": atualizado, "deleted_at": deleted_at,
    }


class TestAtualizarStatusEquipamentos(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def gravar_json(self, equipamentos):
        caminho = self.root / "delta.json"
        caminho.write_text(json.dumps(
            [{"tabela": "equipments", "registro_json": e} for e in equipamentos]
        ), encoding="utf-8")
        return caminho

    def test_filtra_janela_e_criacao(self):
        caminho = self.gravar_json([
            equipamento(1, "100", 2),                                       # entra
            equipamento(2, "200", 2, criado="2026-11-05 08:00:00"),         # criado na janela
            equipamento(3, "300", 2, atualizado="2026-11-20 08:00:00"),     # atualizado fora
        ])
        alteracoes = carregar_alteracoes(
            caminho, date(2026, 11, 4), date(2026, 11, 12))
        self.assertEqual([a["id"] for a in alteracoes], [1])

    def test_planeja_update_por_id_e_numero(self):
        updates, pendencias = planejar(
            [equipamento(1, "100", 6)],
            [{"id": 1, "number": "100", "serial_number": "S1", "status_id": 1}],
            {1, 2, 6},
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["local_id"], 1)
        self.assertEqual(updates[0]["status_anterior"], 1)
        self.assertEqual(updates[0]["status_novo"], 6)
        self.assertEqual(pendencias, [])

    def test_status_igual_nao_gera_update(self):
        updates, pendencias = planejar(
            [equipamento(1, "100", 2)],
            [{"id": 1, "number": "100", "serial_number": "S1", "status_id": 2}],
            {2},
        )
        self.assertEqual((updates, pendencias), ([], []))

    def test_id_divergente_casa_por_numero_e_serie(self):
        updates, pendencias = planejar(
            [equipamento(999, "100", 11, serie="SERIE-X")],
            [{"id": 1, "number": "100", "serial_number": "SERIE-X", "status_id": 1}],
            {1, 11},
        )
        self.assertEqual(updates[0]["local_id"], 1)
        self.assertEqual(updates[0]["metodo"], "numero")

    def test_numero_ambiguo_fica_pendente(self):
        updates, pendencias = planejar(
            [equipamento(999, "100", 6, serie="OUTRA")],
            [{"id": 1, "number": "100", "serial_number": "S1", "status_id": 1},
             {"id": 2, "number": "100", "serial_number": "S2", "status_id": 1}],
            {1, 6},
        )
        self.assertEqual(updates, [])
        self.assertIn("duplicado", pendencias[0]["motivo"])

    def test_status_inexistente_fica_pendente(self):
        updates, pendencias = planejar(
            [equipamento(1, "100", 99)],
            [{"id": 1, "number": "100", "serial_number": "S1", "status_id": 1}],
            {1, 2},
        )
        self.assertEqual(updates, [])
        self.assertIn("inexistente", pendencias[0]["motivo"])

    def test_equipamento_excluido_localmente_fica_pendente(self):
        updates, pendencias = planejar(
            [equipamento(1, "100", 6)],
            [{"id": 1, "number": "100", "serial_number": "S1", "status_id": 9,
              "deleted_at": "2026-11-04 12:05:53"}],
            {6, 9},
        )
        self.assertEqual(updates, [])
        self.assertIn("excluido no banco local", pendencias[0]["motivo"])

    def test_excluido_na_origem_fica_pendente(self):
        updates, pendencias = planejar(
            [equipamento(1, "100", 6, deleted_at="2026-11-07 10:00:00")],
            [{"id": 1, "number": "100", "serial_number": "S1", "status_id": 1}],
            {1, 6},
        )
        self.assertEqual(updates, [])
        self.assertIn("excluido", pendencias[0]["motivo"])

    def test_executar_dry_run_nao_grava(self):
        engine, caminho = self._banco_sqlite()
        resultado = executar(engine, caminho_json=caminho,
                             saida=self.root / "relatorio.csv", apply=False)
        self.assertEqual(resultado["atualizados"], 1)
        self.assertEqual(self._status(engine), 1)
        self.assertTrue((self.root / "relatorio.csv").exists())

    def test_executar_apply_grava_status(self):
        engine, caminho = self._banco_sqlite()
        resultado = executar(engine, caminho_json=caminho,
                             saida=self.root / "relatorio.csv", apply=True)
        self.assertEqual(resultado["atualizados"], 1)
        self.assertEqual(self._status(engine), 6)

    def _status(self, engine):
        with engine.connect() as conn:
            return conn.execute(text(
                "SELECT status_id FROM equipments WHERE id = 1")).scalar()

    def _banco_sqlite(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE equipments (id INTEGER PRIMARY KEY, number TEXT, "
                "serial_number TEXT, status_id INTEGER, deleted_at TEXT)"))
            conn.execute(text("CREATE TABLE status (id INTEGER PRIMARY KEY)"))
            conn.execute(text(
                "INSERT INTO equipments VALUES (1, '100', 'S1', 1, NULL)"))
            conn.execute(text("INSERT INTO status VALUES (1), (6)"))
        caminho = self.gravar_json([equipamento(1, "100", 6)])
        return engine, caminho


if __name__ == "__main__":
    unittest.main()
