import unittest

import pandas as pd

from movimentos.classificacao_flags import (
    AVULSO, EXTRA_FALLBACK, KIT, REJEITAR, classe_por_flags, flag_booleana,
    serie_booleana,
)


class TestClassePorFlags(unittest.TestCase):
    def test_flags_ausentes_com_item_seguem_heuristica(self):
        self.assertIsNone(classe_por_flags(None, None, 10, 100))
        self.assertIsNone(classe_por_flags(pd.NA, pd.NA, 10, 100))

    def test_flags_false_com_item_seguem_heuristica(self):
        self.assertIsNone(classe_por_flags(False, False, 10, 100))

    def test_kit_com_contrato(self):
        classe, motivo, origem = classe_por_flags(True, False, 10, None)
        self.assertEqual((classe, origem), (KIT, "FLAG_KIT"))
        self.assertIn("IS_EXTRA_KIT", motivo)

    def test_kit_ignora_item_do_parquet(self):
        classe, motivo, _ = classe_por_flags(True, None, 10, 100)
        self.assertEqual(classe, KIT)
        self.assertIn("ITEM IGNORADO", motivo)

    def test_kit_sem_contrato_rejeita(self):
        classe, motivo, _ = classe_por_flags(True, False, None, None)
        self.assertEqual(classe, REJEITAR)
        self.assertIn("KIT SEM CONTRATO", motivo)

    def test_kit_com_avulso_rejeita(self):
        classe, motivo, _ = classe_por_flags(True, True, 10, 100)
        self.assertEqual(classe, REJEITAR)
        self.assertIn("KIT+AVULSO", motivo)

    def test_avulso_flag_ignora_contrato_e_item(self):
        classe, motivo, origem = classe_por_flags(None, True, 10, 100)
        self.assertEqual((classe, origem), (AVULSO, "FLAG_AVULSO"))
        self.assertIn("IGNORADOS", motivo)

    def test_avulso_flag_sem_contrato(self):
        classe, _, origem = classe_por_flags(None, True, None, None)
        self.assertEqual((classe, origem), (AVULSO, "FLAG_AVULSO"))

    def test_sem_contrato_sem_flags_continua_avulso(self):
        classe, _, origem = classe_por_flags(None, None, None, None)
        self.assertEqual((classe, origem), (AVULSO, "SEM_CONTRATO"))

    def test_avulso_inferido_quando_flag_false(self):
        classe, motivo, origem = classe_por_flags(False, False, None, None)
        self.assertEqual((classe, origem), (AVULSO, "SEM_CONTRATO"))
        self.assertIn("INFERIDO", motivo)

    def test_extra_fallback_quando_usuario_marcou_nao_e_kit(self):
        classe, _, origem = classe_por_flags(False, False, 10, None)
        self.assertEqual((classe, origem), (EXTRA_FALLBACK, "FALLBACK"))

    def test_flag_booleana_tolera_texto_e_nulos(self):
        self.assertIs(flag_booleana("Sim"), True)
        self.assertIs(flag_booleana("NÃO"), False)
        self.assertIsNone(flag_booleana(""))
        self.assertIsNone(flag_booleana(float("nan")))
        self.assertIsNone(flag_booleana(pd.NA))
        self.assertIs(flag_booleana(1), True)
        self.assertIs(flag_booleana(0), False)

    def test_serie_booleana_tolera_texto_e_preserva_nulo(self):
        convertida = serie_booleana(pd.Series(["Sim", "Não", None], dtype=object))
        self.assertEqual(str(convertida.dtype), "boolean")
        self.assertEqual(convertida.iloc[:2].tolist(), [True, False])
        self.assertTrue(bool(convertida.isna().iloc[2]))


if __name__ == "__main__":
    unittest.main()
