"""Devoluções de substituição: status do equipamento antigo e de/para de clientes.

Casos reais (diagnóstico de 2026-11-14): equipamentos que entraram em devolução
por substituição (129006, 129821, 160183, 158117, 149824, 140931, 148023)
ficaram DISPONÍVEL PARA LOCAÇÃO no refatorado porque o ciclo de substituição
não era extraído (tipo do equipamento muda na troca ou o antigo não tem aluguel
de origem).

Além da extração, a resolução do equipamento passou a usar o ID do item do
movimento (preservado na migração), e não o tombo — que se repete no legado
(ex.: 49726 e 69504 têm mais de uma cópia).
"""

import unittest
from unittest import mock

import pandas as pd

from ajustes.ajustes_movimentos import AjustesMovimentos
from movimentos.migracao_substituicao import (
    _int_ou_none,
    deve_processar_envio,
    destino_status_equipamento_antigo,
    lados_ciclo_substituicao,
    montar_presente_substituicao,
)


class DestinoStatusEquipamentoAntigoTests(unittest.TestCase):
    def test_em_devolucao_vira_status_8(self):
        self.assertEqual(destino_status_equipamento_antigo(14), 8)

    def test_alugado_nao_sobrescreve(self):
        # A fase ALUGUEL já gravou o movimento ativo do equipamento.
        self.assertIsNone(destino_status_equipamento_antigo(1))

    def test_demais_situacoes_liberam_para_locacao(self):
        for situacao in (7, 11, 3, 9):
            self.assertEqual(destino_status_equipamento_antigo(situacao), 1)


class IntOuNoneTests(unittest.TestCase):
    def test_valores(self):
        self.assertEqual(_int_ou_none("129821"), 129821)
        self.assertEqual(_int_ou_none(129821.0), 129821)
        self.assertIsNone(_int_ou_none(None))
        self.assertIsNone(_int_ou_none(float("nan")))
        self.assertIsNone(_int_ou_none(""))


class ProcessarEnvioTests(unittest.TestCase):
    def test_envia_quando_tombo_novo_tem_substituicao(self):
        self.assertTrue(deve_processar_envio("84248", {"84248"}, 147708))

    def test_nao_envia_substituto_fora_do_parquet(self):
        # 129821 -> 84249 (em devolução, fora do Parquet): a devolução do
        # antigo continua sendo migrada, mas o envio do novo não.
        self.assertFalse(deve_processar_envio("84249", {"84248"}, 147709))

    def test_nao_envia_sem_equipamento_novo(self):
        self.assertFalse(deve_processar_envio("84248", {"84248"}, None))


class LadosCicloSubstituicaoTests(unittest.TestCase):
    """O substituto não pode ser perdido quando o antigo foi excluído.

    Diagnóstico de 2026-11-14: 1.306 equipamentos alugados (último movimento
    SA, situacao 1, no Parquet) não tinham nenhum movimento no refatorado
    porque o equipamento devolvido no ciclo (SD) estava excluído no legado.
    A extração descartava o ciclo inteiro pelo INNER JOIN em d.
    """

    def test_antigo_excluido_preserva_envio_do_novo(self):
        # 13618 é o substituto alugado; 32402 (antigo) não existe no novo.
        antigo, novo = lados_ciclo_substituicao(32402, 13618, {13618})
        self.assertIsNone(antigo)
        self.assertEqual(novo, 13618)
        self.assertTrue(deve_processar_envio("13618", {"13618": {}}, novo))

    def test_ambos_migraveis_mantem_ciclo_completo(self):
        self.assertEqual(lados_ciclo_substituicao(1, 2, {1, 2}), (1, 2))

    def test_novo_ausente_mantem_apenas_devolucao(self):
        antigo, novo = lados_ciclo_substituicao(1, 2, {1})
        self.assertEqual(antigo, 1)
        self.assertIsNone(novo)
        self.assertFalse(deve_processar_envio("2", {"2": {}}, novo))


class MontarPresenteSubstituicaoTests(unittest.TestCase):
    """Pareamento por tipo (posição como reserva) não pode duplicar itens.

    Diagnóstico de 2026-11-14: o pareamento por tipo/quantidade gerava produto
    cartesiano quando o movimento tinha itens excluídos (sub 4454: 3×3 = 9
    linhas para um ciclo de 1 item válido por lado) e descartava itens sem par
    (sub 130: 3 itens entregues para 6 devolvidos).
    """

    COLUNAS = [
        'substituicao_id', 'seq', 'CLIENTE_ID', 'ORGAO_ID', 'USUARIO_ID',
        'DATA_MOV', 'DELETED_AT', 'MOVIMENTO_ID', 'EQUIPAMENTO_ID', 'TOMBO',
        'NOME', 'TIPO_ID',
    ]

    def _lado(self, linhas):
        return pd.DataFrame(linhas, columns=self.COLUNAS)

    def test_item_sem_par_do_outro_lado_e_preservado(self):
        devolucao = self._lado([
            [1, 1, 10, 1, 1, '2025-01-01', None, 501, 900, '900', 'A', 1],
            [1, 2, 10, 1, 1, '2025-01-01', None, 501, 901, '901', 'B', 1],
        ])
        entrega = self._lado([
            [1, 1, 10, 2, 1, '2025-01-02', None, 502, 800, '800', 'C', 2],
        ])
        presente = montar_presente_substituicao(devolucao, entrega)
        self.assertEqual(len(presente), 2)
        self.assertEqual(presente['EQUIPAMENTO_ID_ANTIGO'].tolist(), [900, 901])
        self.assertEqual(presente['EQUIPAMENTO_ID_NOVO'].notna().tolist(), [True, False])
        # Nenhum equipamento repetido no mesmo ciclo.
        self.assertEqual(presente['EQUIPAMENTO_ID_ANTIGO'].nunique(), 2)

    def test_entrega_sem_devolucao_nao_e_descartada(self):
        devolucao = self._lado([
            [2, 1, 11, 1, 1, '2025-01-01', None, 503, 910, '910', 'A', 1],
        ])
        entrega = self._lado([
            [2, 1, 11, 2, 1, '2025-01-02', None, 504, 810, '810', 'B', 2],
            [2, 2, 11, 2, 1, '2025-01-02', None, 504, 811, '811', 'C', 2],
        ])
        presente = montar_presente_substituicao(devolucao, entrega)
        self.assertEqual(len(presente), 2)
        self.assertEqual(presente['EQUIPAMENTO_ID_NOVO'].tolist(), [810, 811])
        self.assertEqual(presente['EQUIPAMENTO_ID_ANTIGO'].notna().tolist(), [True, False])

    def test_um_para_um_nao_duplica(self):
        devolucao = self._lado([
            [3, 1, 12, 1, 1, '2025-01-01', None, 505, 920, '920', 'A', 1],
        ])
        entrega = self._lado([
            [3, 1, 12, 2, 1, '2025-01-02', None, 506, 820, '820', 'B', 2],
        ])
        presente = montar_presente_substituicao(devolucao, entrega)
        self.assertEqual(len(presente), 1)
        self.assertEqual(presente.loc[0, 'EQUIPAMENTO_ID_ANTIGO'], 920)
        self.assertEqual(presente.loc[0, 'EQUIPAMENTO_ID_NOVO'], 820)

    def test_lados_vazios(self):
        vazio = self._lado([])
        self.assertTrue(montar_presente_substituicao(vazio, vazio).empty)

    def test_mesmo_tipo_tem_prioridade_sobre_a_posicao(self):
        # Devolve nobreak e monitor; recebe monitor e nobreak na mesma ordem
        # de tipo_id do outro lado. Casar por posição trocaria as categorias
        # (caso real do ciclo 14634, seq 12-15).
        devolucao = self._lado([
            [7, 1, 10, 1, 1, '2025-01-01', None, 510, 900, '900', 'nobreak', 124],
            [7, 2, 10, 1, 1, '2025-01-01', None, 510, 901, '901', 'monitor', 1252],
        ])
        entrega = self._lado([
            [7, 1, 10, 2, 1, '2025-01-02', None, 511, 800, '800', 'monitor', 1252],
            [7, 2, 10, 2, 1, '2025-01-02', None, 511, 801, '801', 'nobreak', 124],
        ])
        presente = montar_presente_substituicao(devolucao, entrega)
        self.assertEqual(
            presente[['EQUIPAMENTO_ID_ANTIGO', 'EQUIPAMENTO_ID_NOVO']].values.tolist(),
            [[900, 801], [901, 800]],
        )

    def test_tipo_diferente_dos_dois_lados_cai_na_posicao(self):
        # Troca real de categoria: nenhum par do mesmo tipo, mantém a ordem.
        devolucao = self._lado([
            [8, 1, 10, 1, 1, '2025-01-01', None, 512, 910, '910', 'nobreak', 124],
        ])
        entrega = self._lado([
            [8, 1, 10, 2, 1, '2025-01-02', None, 513, 810, '810', 'monitor', 1252],
        ])
        presente = montar_presente_substituicao(devolucao, entrega)
        self.assertEqual(presente.loc[0, 'EQUIPAMENTO_ID_ANTIGO'], 910)
        self.assertEqual(presente.loc[0, 'EQUIPAMENTO_ID_NOVO'], 810)

    def test_tipo_repetido_mantem_ordem_de_insercao(self):
        devolucao = self._lado([
            [9, 1, 10, 1, 1, '2025-01-01', None, 514, 920, '920', 'a', 124],
            [9, 2, 10, 1, 1, '2025-01-01', None, 514, 921, '921', 'b', 124],
        ])
        entrega = self._lado([
            [9, 1, 10, 2, 1, '2025-01-02', None, 515, 820, '820', 'c', 124],
            [9, 2, 10, 2, 1, '2025-01-02', None, 515, 821, '821', 'd', 124],
        ])
        presente = montar_presente_substituicao(devolucao, entrega)
        self.assertEqual(
            presente[['EQUIPAMENTO_ID_ANTIGO', 'EQUIPAMENTO_ID_NOVO']].values.tolist(),
            [[920, 820], [921, 821]],
        )


class MapaClientesDiagnosticoTests(unittest.TestCase):
    """O diagnóstico deve usar o mesmo de/para de duplicados da migração."""

    def test_copia_herda_customer_e_address_do_principal(self):
        mapa_clientes = pd.DataFrame(
            {
                "legacy_customer_id": [3624],
                "id_customer": [177],
                "address_id": [2929],
            }
        )
        reservas = pd.DataFrame(
            {"legacy_reserva_id": [], "id_customer": [], "address_id": []}
        )

        def read_sql_fake(query, conn):
            texto = str(query)
            if "reserved_customer_id" in texto:
                return reservas
            return mapa_clientes

        app = AjustesMovimentos.__new__(AjustesMovimentos)
        with mock.patch(
            "ajustes.ajustes_movimentos.pd.read_sql", side_effect=read_sql_fake
        ), mock.patch(
            "utils.depara_clientes.carregar_depara_duplicados",
            return_value={10504: 3624},
        ):
            mapa_customer, mapa_address, _, _ = app._carregar_mapas_clientes(conn=None)

        # 10504 é cópia de 3624 e passa a resolver para o mesmo destino.
        self.assertEqual(mapa_customer[10504], 177)
        self.assertEqual(mapa_address[10504], 2929)
        self.assertEqual(mapa_customer[3624], 177)

    def test_cliente_com_hierarquia_deletada_herda_principal(self):
        """3607/3664 têm setor/departamento excluídos no legado.

        O de/para explícito deve apontá-los para o cliente canônico ativo
        (1215/3518), sem depender do CSV de duplicados.
        """
        mapa_clientes = pd.DataFrame(
            {
                "legacy_customer_id": [1215, 3518],
                "id_customer": [2631, 2631],
                "address_id": [1718, 1781],
            }
        )
        reservas = pd.DataFrame(
            {"legacy_reserva_id": [], "id_customer": [], "address_id": []}
        )

        def read_sql_fake(query, conn):
            texto = str(query)
            if "reserved_customer_id" in texto:
                return reservas
            return mapa_clientes

        app = AjustesMovimentos.__new__(AjustesMovimentos)
        with mock.patch(
            "ajustes.ajustes_movimentos.pd.read_sql", side_effect=read_sql_fake
        ), mock.patch(
            "utils.depara_clientes.carregar_depara_duplicados",
            return_value={},
        ):
            mapa_customer, mapa_address, _, _ = app._carregar_mapas_clientes(conn=None)

        self.assertEqual(mapa_customer[3607], 2631)
        self.assertEqual(mapa_address[3607], 1718)
        self.assertEqual(mapa_customer[3664], 2631)
        self.assertEqual(mapa_address[3664], 1781)

    def test_reserva_resolve_para_titular_e_endereco(self):
        mapa_clientes = pd.DataFrame(
            {
                "legacy_customer_id": [4539],
                "id_customer": [2927],
                "address_id": [1806],
            }
        )
        reservas = pd.DataFrame(
            {
                "legacy_reserva_id": [4544],
                "id_customer": [2927],
                "address_id": [2830],
            }
        )

        def read_sql_fake(query, conn):
            texto = str(query)
            if "reserved_customer_id" in texto:
                return reservas
            return mapa_clientes

        app = AjustesMovimentos.__new__(AjustesMovimentos)
        with mock.patch(
            "ajustes.ajustes_movimentos.pd.read_sql", side_effect=read_sql_fake
        ), mock.patch(
            "utils.depara_clientes.carregar_depara_duplicados",
            return_value={},
        ):
            mapa_customer, mapa_address, mapa_reservas, mapa_addr_reservas = (
                app._carregar_mapas_clientes(conn=None)
            )

        self.assertEqual(mapa_reservas[4544], 2927)
        self.assertEqual(mapa_addr_reservas[4544], 2830)
        self.assertNotIn(4544, mapa_customer)


if __name__ == "__main__":
    unittest.main()
