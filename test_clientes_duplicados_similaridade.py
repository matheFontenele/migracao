import json
import os
import tempfile
import unittest

import pandas as pd

from cadastros.clientes.migracao_cliente import MigracaoClientes
from utils.depara_clientes import (
    carregar_depara_duplicados,
    propagar_depara_duplicados,
)


def _base():
    return pd.DataFrame({
        'ID_CLIENTE': [1, 2, 3, 4, 5, 6],
        'CLIENTE': [
            'SECRETARIA MUNICIPAL DE SAUDE DE SAO LUIS',
            'SECRETARIA MUNICIPAL DE SAUDE DE SAO LUIZ',
            'SECRETARIA MUNICIPAL DE EDUCACAO DE SAO LUIS',
            'P M DE SAO LUIS SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA SEMUSC',
            'P M DE SAO LUIS SECRETARIA MUNICIPAL DE SEGURANCA COM CIDADANIA SEMUSC RESERVA',
            'SECRETARIA MUNICIPAL DE SAUDE DE SAO LUIS',
        ],
        'ID_PREFEITURA': [287, 287, 287, 287, 287, 500],
        'PREFEITURA': ['SAO LUIS'] * 5 + ['OUTRA'],
        'ID_SECRETARIA': [1249, 1249, 1249, 1197, 1197, 600],
        'SECRETARIA': ['SAUDE', 'SAUDE', 'SAUDE', 'SEMUSC', 'SEMUSC', 'OUTRA'],
        'reserved_customer_id': [None, None, None, None, 4, None],
    })


def _mapa():
    return {
        1: {'customer_id': 100, 'address_id': 1000, 'alias': 'SAUDE'},
        2: {'customer_id': 100, 'address_id': 1001, 'alias': 'SAUDE LUIZ'},
        3: {'customer_id': 100, 'address_id': 1002, 'alias': 'EDUCACAO'},
        4: {'customer_id': 101, 'address_id': 1003, 'alias': 'SEMUSC'},
        5: {'customer_id': 101, 'address_id': 1004, 'alias': 'SEMUSC'},
    }


class DuplicadosSimilaridadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.obj = MigracaoClientes.__new__(MigracaoClientes)
        self.obj.arquivo_duplicados = os.path.join(self.tmp.name, 'duplicados.csv')
        self.obj.arquivo_duplicados_json = os.path.join(self.tmp.name, 'duplicados.json')
        self.obj.arquivo_depara_duplicados = os.path.join(self.tmp.name, 'depara.csv')

    def test_pares_acima_de_95_por_cento(self):
        relatorio = self.obj._relacionar_duplicados_clientes(_base(), _mapa())

        pares = {frozenset((row.id_legado_a, row.id_legado_b)) for row in relatorio.itertuples()}
        self.assertIn(frozenset((1, 2)), pares)
        # "RESERVA" é removida na normalização: vira 100%.
        self.assertIn(frozenset((4, 5)), pares)
        self.assertNotIn(frozenset((1, 3)), pares)

    def test_similaridade_estritamente_maior_que_95(self):
        relatorio = self.obj._relacionar_duplicados_clientes(_base(), _mapa())
        self.assertTrue((relatorio['similaridade_pct'] > 95).all())

        linha = relatorio[
            relatorio['id_legado_a'].isin([1, 2])
            & relatorio['id_legado_b'].isin([1, 2])
        ].iloc[0]
        self.assertAlmostEqual(linha['similaridade_pct'], 97.56, places=2)

    def test_relaciona_legado_com_refatorado(self):
        relatorio = self.obj._relacionar_duplicados_clientes(_base(), _mapa())
        linha = relatorio[
            relatorio['id_legado_a'].isin([1, 2])
            & relatorio['id_legado_b'].isin([1, 2])
        ].iloc[0]

        self.assertEqual(linha['customer_id_a'], 100)
        self.assertEqual(linha['customer_id_b'], 100)
        self.assertIn(linha['address_id_a'], (1000, 1001))
        self.assertIn(linha['address_id_b'], (1000, 1001))
        self.assertTrue(linha['mesma_hierarquia'])

    def test_cliente_ausente_no_refatorado_nao_entra(self):
        relatorio = self.obj._relacionar_duplicados_clientes(_base(), _mapa())
        self.assertFalse(relatorio['id_legado_a'].isin([6]).any())
        self.assertFalse(relatorio['id_legado_b'].isin([6]).any())

    def test_csv_gerado_com_colunas_esperadas(self):
        self.obj._relacionar_duplicados_clientes(_base(), _mapa())

        self.assertTrue(os.path.exists(self.obj.arquivo_duplicados))
        lido = pd.read_csv(self.obj.arquivo_duplicados, encoding='utf-8-sig')
        self.assertEqual(list(lido.columns), MigracaoClientes.COLUNAS_DUPLICADOS)
        self.assertGreater(len(lido), 0)

    def test_base_vazia_gera_csv_vazio(self):
        relatorio = self.obj._relacionar_duplicados_clientes(
            _base().iloc[0:0], {}
        )
        self.assertTrue(relatorio.empty)
        self.assertTrue(os.path.exists(self.obj.arquivo_duplicados))


def _base_niveis():
    return pd.DataFrame({
        'ID_CLIENTE': [10, 11, 12, 13],
        'CLIENTE': [
            'UNIDADE DE SAUDE DO CENTRO',
            'UNIDADE DE SAUDE DO CENTRO',
            'PREFEITURA MUNICIPAL DE SAO LUIS',
            'PREFEITURA MUNICIPAL DE SAO LUIZ',
        ],
        'ID_PREFEITURA': [287, 287, None, None],
        'PREFEITURA': ['SAO LUIS', 'SAO LUIS', None, None],
        'ID_SECRETARIA': [None, None, None, None],
        'SECRETARIA': [None, None, None, None],
        'reserved_customer_id': [None, None, None, None],
    })


def _mapa_niveis():
    return {
        10: {'customer_id': 200, 'address_id': 2000, 'alias': 'CENTRO'},
        11: {'customer_id': 200, 'address_id': 2001, 'alias': 'CENTRO'},
        12: {'customer_id': 201, 'address_id': 2002, 'alias': 'PREFEITURA'},
        13: {'customer_id': 201, 'address_id': 2003, 'alias': 'PREFEITURA'},
    }


class JsonDuplicadosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.obj = MigracaoClientes.__new__(MigracaoClientes)
        self.obj.arquivo_duplicados = os.path.join(self.tmp.name, 'duplicados.csv')
        self.obj.arquivo_duplicados_json = os.path.join(self.tmp.name, 'duplicados.json')
        self.obj.arquivo_depara_duplicados = os.path.join(self.tmp.name, 'depara.csv')

    def _ler_json(self):
        with open(self.obj.arquivo_duplicados_json, encoding='utf-8') as arquivo:
            return json.load(arquivo)

    def test_json_tem_principal_e_copias(self):
        self.obj._relacionar_duplicados_clientes(_base_niveis(), _mapa_niveis())
        conteudo = self._ler_json()

        self.assertEqual(conteudo['resumo']['grupos'], 2)
        self.assertEqual(conteudo['resumo']['ocorrencias'], 4)

        grupo = next(
            g for g in conteudo['grupos']
            if g['principal']['id_legado'] == 10
        )
        self.assertEqual(grupo['principal']['cliente'], 'UNIDADE DE SAUDE DO CENTRO')
        self.assertEqual(len(grupo['copias']), 1)
        self.assertEqual(grupo['copias'][0]['id_legado'], 11)
        self.assertTrue(grupo['mesma_hierarquia'])
        self.assertEqual(grupo['similaridade_maxima_pct'], 100.0)

    def test_json_classifica_cliente_secretaria_e_prefeitura(self):
        self.obj._relacionar_duplicados_clientes(_base_niveis(), _mapa_niveis())
        conteudo = self._ler_json()
        grupos = {g['principal']['id_legado']: g for g in conteudo['grupos']}

        self.assertEqual(grupos[10]['principal']['classificacao'], 'secretaria')
        self.assertEqual(grupos[10]['copias'][0]['classificacao'], 'secretaria')
        self.assertEqual(grupos[12]['principal']['classificacao'], 'prefeitura')
        self.assertEqual(grupos[12]['copias'][0]['classificacao'], 'prefeitura')

    def test_classificacao_por_nivel_da_hierarquia(self):
        self.assertEqual(
            MigracaoClientes._classificacao_ocorrencia(287, 1249), 'cliente'
        )
        self.assertEqual(
            MigracaoClientes._classificacao_ocorrencia(287, None), 'secretaria'
        )
        self.assertEqual(
            MigracaoClientes._classificacao_ocorrencia(None, None), 'prefeitura'
        )
        # Raiz sintética de cliente sem hierarquia.
        self.assertEqual(
            MigracaoClientes._classificacao_ocorrencia(-10468, None), 'prefeitura'
        )

    def test_json_ignora_ids_de_para_e_excecoes(self):
        df = pd.DataFrame({
            'ID_CLIENTE': [10824, 10711, 501, 502],
            'CLIENTE': [
                'CLIENTE ESPECIAL ALFA',
                'CLIENTE ESPECIAL ALFA',
                'OUTRO CLIENTE BETA',
                'OUTRO CLIENTE BETA',
            ],
            'ID_PREFEITURA': [287, 287, 287, 287],
            'PREFEITURA': ['SAO LUIS'] * 4,
            'ID_SECRETARIA': [1249, 1249, 1249, 1249],
            'SECRETARIA': ['SAUDE'] * 4,
            'reserved_customer_id': [None] * 4,
        })
        mapa = {
            10824: {'customer_id': 1, 'address_id': 1, 'alias': 'A'},
            10711: {'customer_id': 1, 'address_id': 2, 'alias': 'A'},
            501: {'customer_id': 1, 'address_id': 3, 'alias': 'B'},
            502: {'customer_id': 1, 'address_id': 4, 'alias': 'B'},
        }

        relatorio = self.obj._relacionar_duplicados_clientes(df, mapa)
        conteudo = self._ler_json()

        # O CSV continua sendo a visão completa (2 pares, 1 por nome).
        self.assertEqual(len(relatorio), 2)
        self.assertEqual(conteudo['resumo']['pares_ignorados_por_regras'], 1)
        self.assertEqual(conteudo['resumo']['grupos'], 1)

        ids = [
            ocorrencia['id_legado']
            for grupo in conteudo['grupos']
            for ocorrencia in [grupo['principal'], *grupo['copias']]
        ]
        self.assertNotIn(10824, ids)
        self.assertNotIn(10711, ids)
        self.assertEqual(sorted(ids), [501, 502])

    def test_json_vazio_quando_nao_ha_duplicados(self):
        self.obj._relacionar_duplicados_clientes(_base().iloc[0:0], {})
        conteudo = self._ler_json()

        self.assertEqual(conteudo['resumo']['grupos'], 0)
        self.assertEqual(conteudo['grupos'], [])


class _FakeConexao:
    def __init__(self):
        self.execucoes = []

    def execute(self, query, params=None):
        self.execucoes.append((str(query), dict(params or {})))


class _FakeEngine:
    def __init__(self):
        self.conexao = _FakeConexao()

    def begin(self):
        conexao = self.conexao

        class _Contexto:
            def __enter__(self):
                return conexao

            def __exit__(self, *args):
                return False

        return _Contexto()


def _base_merge():
    return pd.DataFrame({
        'ID_CLIENTE': [1, 2, 3, 4, 5, 6],
        'CLIENTE': [
            'UNIDADE DE SAUDE CENTRO',
            'UNIDADE DE SAUDE CENTRO',
            'PREFEITURA MUNICIPAL DE SAO LUIS',
            'PREFEITURA MUNICIPAL DE SAO LUIZ',
            'ALMOXARIFADO REGIONAL NORTE',
            'ALMOXARIFADO REGIONAL NORTE',
        ],
        'ID_PREFEITURA': [287, 287, 300, 301, 287, 287],
        'PREFEITURA': ['SAO LUIS', 'SAO LUIS', 'P1', 'P2', 'SAO LUIS', 'SAO LUIS'],
        'ID_SECRETARIA': [1249, 1249, None, None, 1250, 1250],
        'SECRETARIA': ['SAUDE', 'SAUDE', None, None, 'OBRAS', 'OBRAS'],
        'reserved_customer_id': [None, None, None, None, 999, None],
    })


def _mapa_merge():
    return {
        1: {'customer_id': 100, 'address_id': 1000, 'alias': 'CENTRO'},
        2: {'customer_id': 100, 'address_id': 1001, 'alias': 'CENTRO'},
        3: {'customer_id': 200, 'address_id': 2000, 'alias': 'P1'},
        4: {'customer_id': 201, 'address_id': 2001, 'alias': 'P2'},
        5: {'customer_id': 100, 'address_id': 1002, 'alias': 'ALMOX'},
        6: {'customer_id': 100, 'address_id': 1003, 'alias': 'ALMOX'},
    }


class MergeDuplicadosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.obj = MigracaoClientes.__new__(MigracaoClientes)
        self.obj.arquivo_duplicados = os.path.join(self.tmp.name, 'duplicados.csv')
        self.obj.arquivo_duplicados_json = os.path.join(self.tmp.name, 'duplicados.json')
        self.obj.arquivo_depara_duplicados = os.path.join(self.tmp.name, 'depara.csv')
        self.obj.engine_new = _FakeEngine()

    def test_mescla_apenas_mesmo_customer_e_nivel(self):
        relatorio = self.obj._relacionar_duplicados_clientes(
            _base_merge(), _mapa_merge()
        )
        depara = self.obj._aplicar_merge_duplicados(
            relatorio, _base_merge()
        )

        self.assertEqual(len(depara), 1)
        linha = depara.iloc[0]
        self.assertEqual(linha['id_legado_copia'], 2)
        self.assertEqual(linha['id_legado_principal'], 1)
        self.assertEqual(linha['customer_id'], 100)
        self.assertEqual(linha['address_id_principal'], 1000)

    def test_remove_address_da_copia(self):
        relatorio = self.obj._relacionar_duplicados_clientes(
            _base_merge(), _mapa_merge()
        )
        self.obj._aplicar_merge_duplicados(relatorio, _base_merge())

        execucoes = self.obj.engine_new.conexao.execucoes
        self.assertEqual(len(execucoes), 1)
        consulta, parametros = execucoes[0]
        self.assertIn('DELETE FROM addresses', consulta)
        self.assertEqual(parametros, {'address_id': 1001, 'customer_id': 100})

    def test_preserva_grupo_com_reserva(self):
        relatorio = self.obj._relacionar_duplicados_clientes(
            _base_merge(), _mapa_merge()
        )
        self.obj._aplicar_merge_duplicados(relatorio, _base_merge())

        ids_copia = set(self._ler_depara())
        self.assertNotIn(6, ids_copia)
        self.assertNotIn(5, ids_copia)

    def test_preserva_cliente_com_endereco_base_sem_reserva_no_nome(self):
        # 3259 usa endereço-base (RESERVAS_BASE_AVULSA) e não tem RESERVA no
        # nome; apagar seu address quebraria o roteamento de reservas.
        df = pd.DataFrame({
            'ID_CLIENTE': [3259, 9001],
            'CLIENTE': [
                'PREFEITURA MUNICIPAL DE ARACATI',
                'PREFEITURA MUNICIPAL DE ARACATI',
            ],
            'ID_PREFEITURA': [400, 400],
            'PREFEITURA': ['ARACATI', 'ARACATI'],
            'ID_SECRETARIA': [None, None],
            'SECRETARIA': [None, None],
            'reserved_customer_id': [None, None],
        })
        mapa = {
            3259: {'customer_id': 300, 'address_id': 3000, 'alias': 'BASE'},
            9001: {'customer_id': 300, 'address_id': 3001, 'alias': 'BASE'},
        }
        relatorio = self.obj._relacionar_duplicados_clientes(df, mapa)
        depara = self.obj._aplicar_merge_duplicados(relatorio, df)

        self.assertTrue(depara.empty)
        self.assertEqual(self.obj.engine_new.conexao.execucoes, [])

    def test_csv_de_para_vazio_quando_nada_e_mesclavel(self):
        relatorio = self.obj._relacionar_duplicados_clientes(
            _base().iloc[0:0], {}
        )
        depara = self.obj._aplicar_merge_duplicados(
            relatorio, _base().iloc[0:0]
        )

        self.assertTrue(depara.empty)
        self.assertTrue(os.path.exists(self.obj.arquivo_depara_duplicados))
        self.assertEqual(self._ler_depara(), {})

    def _ler_depara(self):
        return carregar_depara_duplicados(self.obj.arquivo_depara_duplicados)

    def test_mescla_par_valido_quando_grupo_liga_outro_customer(self):
        # Nomes idênticos geram pares (1,2), (1,3) e (2,3). Como 3 é de outro
        # customer, o filtro não pode descartar o grupo inteiro: (1,2) segue
        # mesclável.
        df = pd.DataFrame({
            'ID_CLIENTE': [1, 2, 3],
            'CLIENTE': ['UNIDADE DE SAUDE CENTRO'] * 3,
            'ID_PREFEITURA': [287, 287, 287],
            'PREFEITURA': ['SAO LUIS'] * 3,
            'ID_SECRETARIA': [1249, 1249, 1249],
            'SECRETARIA': ['SAUDE'] * 3,
            'reserved_customer_id': [None, None, None],
        })
        mapa = {
            1: {'customer_id': 100, 'address_id': 1000, 'alias': 'CENTRO'},
            2: {'customer_id': 100, 'address_id': 1001, 'alias': 'CENTRO'},
            3: {'customer_id': 200, 'address_id': 2000, 'alias': 'CENTRO'},
        }
        relatorio = self.obj._relacionar_duplicados_clientes(df, mapa)
        self.assertEqual(len(relatorio), 3)

        depara = self.obj._aplicar_merge_duplicados(relatorio, df)
        self.assertEqual(len(depara), 1)
        self.assertEqual(depara.iloc[0]['id_legado_copia'], 2)
        self.assertEqual(depara.iloc[0]['id_legado_principal'], 1)

    def test_propaga_depara_para_mapas_de_lookup(self):
        relatorio = self.obj._relacionar_duplicados_clientes(
            _base_merge(), _mapa_merge()
        )
        self.obj._aplicar_merge_duplicados(relatorio, _base_merge())

        mapa = {1: 'address_principal'}
        propagar_depara_duplicados(mapa, self.obj.arquivo_depara_duplicados)
        self.assertEqual(mapa[2], 'address_principal')

    def test_propaga_depara_de_clientes_sem_endereco(self):
        """De/para explícito resolve clientes cujo endereço não foi criado.

        Diagnóstico de 2026-11-14: 10388 (CRATO/URBANISMO) e 10504 (ETUFOR)
        apareciam como CLIENTE_SEM_MAPA mesmo sendo o mesmo órgão de outro id
        legado com endereço migrado.
        """
        mapa = {11698: 2992, 3624: 177}
        propagar_depara_duplicados(mapa, '/caminho/inexistente.csv')
        self.assertEqual(mapa[10388], 2992)
        self.assertEqual(mapa[10504], 177)

    def test_propaga_depara_aracoiaba_e_mte(self):
        """3581 (Aracóiba/Governo) e 11252 (MTE/SIA) não têm customer.

        Diagnóstico de 2026-11-14: 35948/35950 (mov. 116967, cliente 3581) e
        146947/146980 (cliente 11252) eram rejeitados por recipient_id nulo.
        Os pares apontam para clientes ativos com endereço no mesmo órgão.
        """
        mapa = {3604: 2512, 11329: 2347}
        propagar_depara_duplicados(mapa, '/caminho/inexistente.csv')
        self.assertEqual(mapa[3581], 2512)
        self.assertEqual(mapa[11252], 2347)

    def test_depara_sem_endereco_nao_sobrescreve_endereco_proprio(self):
        mapa = {10388: 9999, 11698: 2992}
        propagar_depara_duplicados(mapa, '/caminho/inexistente.csv')
        self.assertEqual(mapa[10388], 9999)


if __name__ == '__main__':
    unittest.main()

