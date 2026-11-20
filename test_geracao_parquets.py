import tempfile
import unittest
from pathlib import Path

import pandas as pd

from movimentos.geracao_parquets import _publicar, _validar_arquivos


def quadro(equipamento=1, contrato=10, item=None, incluir_item=True):
    dados = {"EQUIPAMENTO_ID": [equipamento], "TOMBO": ["T-1"],
             "CLIENTE_ID": [2], "CONTRACT_ID": [contrato], "ORGAO_ID": [3]}
    if incluir_item:
        dados["CONTRACT_ITEM_ID"] = [item]
    return pd.DataFrame(dados)


class TestGeracaoParquets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origem = self.root / "origem"
        self.origem.mkdir()

    def gravar(self, nome, df):
        df.to_parquet(self.origem / nome, index=False)

    def test_sem_arquivos(self):
        with self.assertRaisesRegex(ValueError, "Nenhum arquivo"):
            _validar_arquivos(self.origem, {10}, set())

    def test_coluna_obrigatoria_ausente(self):
        self.gravar("a.parquet", quadro().drop(columns="TOMBO"))
        with self.assertRaisesRegex(ValueError, "colunas obrigatórias"):
            _validar_arquivos(self.origem, {10}, set())

    def test_equipamento_duplicado_entre_arquivos(self):
        self.gravar("a.parquet", quadro())
        self.gravar("b.parquet", quadro())
        with self.assertRaisesRegex(ValueError, "EQUIPAMENTO_ID duplicado"):
            _validar_arquivos(self.origem, {10}, {None})

    def test_contrato_inexistente(self):
        self.gravar("a.parquet", quadro())
        with self.assertRaisesRegex(ValueError, "CONTRACT_ID inexistente"):
            _validar_arquivos(self.origem, set(), {None})

    def test_arquivos_validos(self):
        self.gravar("a.parquet", quadro())
        stats = _validar_arquivos(self.origem, {10}, {None})
        self.assertEqual(stats["linhas"], 1)
        self.assertEqual(stats["equipamentos"], 1)
        self.assertEqual(len(stats["contratos"]), 1)

    def test_item_contrato_nulo_aceito(self):
        self.gravar("a.parquet", quadro(item=None))
        self.assertEqual(_validar_arquivos(self.origem, {10}, set())["linhas"], 1)

    def test_publica_e_preserva_snapshot_anterior(self):
        self.gravar("novo.parquet", quadro())
        destino = self.root / "destino"
        destino.mkdir()
        (destino / "antigo.parquet").write_bytes(b"snapshot")
        publicados = _publicar(self.origem, destino)
        self.assertEqual([p.name for p in publicados], ["novo.parquet"])
        self.assertEqual([p.name for p in destino.glob("*.parquet")], ["novo.parquet"])
        arquivos_anteriores = list((destino / "_anterior").glob("*/*.parquet"))
        self.assertEqual(len(arquivos_anteriores), 1)
        self.assertEqual(arquivos_anteriores[0].read_bytes(), b"snapshot")
        self.assertFalse(list(destino.glob("_staging_*")))


if __name__ == "__main__":
    unittest.main()
