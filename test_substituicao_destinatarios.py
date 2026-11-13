import unittest

from movimentos.migracao_substituicao import MigracaoSubstituicao


class SelecionarDestinatariosTest(unittest.TestCase):
    def test_historico_usa_cliente_original_e_presente_cliente_substituicao(self):
        atual, passado = MigracaoSubstituicao.selecionar_destinatarios(
            2659, 48, {2659: 2659, 48: 48}, {2659: 12659, 48: 1048}
        )

        self.assertEqual(atual, (2659, 12659))
        self.assertEqual(passado, (48, 1048))

    def test_historico_sem_cliente_mapeado_reutiliza_cliente_atual(self):
        atual, passado = MigracaoSubstituicao.selecionar_destinatarios(
            2659, 9999, {2659: 2659}, {2659: 12659}
        )

        self.assertEqual(atual, (2659, 12659))
        self.assertEqual(passado, atual)


if __name__ == "__main__":
    unittest.main()
