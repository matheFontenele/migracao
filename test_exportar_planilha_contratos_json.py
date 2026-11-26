"""Exportação de docs/Contratos.xlsx para um JSON por contrato.

Os testes usam a planilha real (é a fonte de identidade dos contratos) em
``dry_run``, sem escrever em ``docs/contratos_json``.
"""
import csv
import tempfile
import unittest
from pathlib import Path

from cadastros.contratos import contratos_json as cj
from cadastros.contratos import exportar_planilha_contratos_json as ex
from cadastros.contratos import importar_contratos_json as importer


class ExportarPlanilhaTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raiz = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _exportar(self, ids, raiz=None, **kwargs):
        return ex.exportar(
            ids, raiz=Path(raiz or self.raiz), dry_run=True, relatorio=None,
            **kwargs
        )

    def _documento(self, contrato_id):
        resultado = self._exportar([contrato_id])
        self.assertEqual(len(resultado['contratos']), 1)
        return resultado['contratos'][0]

    @staticmethod
    def _aditivos(documento):
        return [
            aditivo
            for evento in documento['contract']['events']
            for aditivo in evento['additives']
        ]

    def test_cegor_nao_puxa_eventos_de_mesmo_id_de_outro_contrato(self):
        # CEGOR só tem um ADITIVO DE PRAZO (evento 68). Os eventos 37/38 da
        # aba EVENTO pertencem a outras delegacias e não podem entrar aqui.
        montado = self._documento(38)
        documento = montado['documento']
        contrato = documento['contract']

        self.assertEqual(documento['organization']['alias'], 'ALUCOM')
        self.assertEqual(
            contrato['number'], 'Nº 26/2022 - SEGER – SUB-ROGADO CEGOR'
        )
        self.assertEqual(len(contrato['events']), 1)
        aditivo = contrato['events'][0]['additives'][0]
        self.assertEqual(aditivo['type'], 'ADITIVO_DE_PRAZO')
        self.assertEqual(len(aditivo['items']), 4)
        self.assertEqual(
            [item['alias'] for item in aditivo['items']],
            ['NOTEBOOK TIPO II - CORE I5', 'MICRO TIPO II - CORE I5',
             'IMPRESSORA TIPO VI - PLOTTER - COM FORNECIMENTO DE PAPEL',
             'TV TIPO III - 40 POLEGADAS'],
        )
        self.assertEqual(contrato['original_start_date'], '2025-09-20')
        self.assertTrue(
            any('sem evento CADASTRO' in p for p in montado['pendencias'])
        )

    def test_contrato_com_cadastro_unico(self):
        montado = self._documento(262)
        contrato = montado['documento']['contract']

        self.assertEqual(montado['documento']['organization']['alias'], 'IP')
        self.assertEqual(contrato['number'], '015/2025')
        self.assertEqual(len(contrato['events']), 1)
        aditivo = contrato['events'][0]['additives'][0]
        self.assertEqual(aditivo['type'], 'CADASTRO')
        self.assertEqual(len(aditivo['items']), 2)
        self.assertEqual(aditivo['info']['total_amount'], 39729)
        self.assertEqual(contrato['original_start_date'], '2025-10-23')
        self.assertTrue(
            any('total_amount arredondado de 39729.16' in p
                for p in montado['pendencias'])
        )

    def test_aditivos_de_prazo_clonam_itens_do_cadastro(self):
        montado = self._documento(192)
        contrato = montado['documento']['contract']

        self.assertEqual(len(contrato['events']), 5)
        aditivos = self._aditivos(montado['documento'])
        self.assertEqual(aditivos[0]['type'], 'CADASTRO')
        self.assertEqual(
            [aditivo['type'] for aditivo in aditivos[1:]],
            ['ADITIVO_DE_PRAZO'] * 4,
        )
        original = aditivos[0]['items'][0]
        self.assertEqual(original['months'], 12)
        for aditivo in aditivos[1:]:
            self.assertEqual(len(aditivo['items']), 1)
            clonado = aditivo['items'][0]
            self.assertEqual(clonado['alias'], original['alias'])
            self.assertEqual(clonado['lineage_key'], original['lineage_key'])
            self.assertEqual(clonado['months'], aditivo['info']['duration'])
        self.assertEqual(montado['pendencias'], [])
        self.assertEqual(contrato['original_start_date'], '2023-01-03')

    def test_override_pareia_eventos_por_ordem(self):
        # As duas cadeias de CADASTRO de Caucaia não casam pelo nome do
        # contrato; a decisão é a ordem dos eventos.
        self.assertEqual(ex.EVENTOS_POR_CONTRATO[149], [486, 487, 488, 489, 490])

        montado = self._documento(149)
        contrato = montado['documento']['contract']

        self.assertEqual(len(contrato['events']), 5)
        self.assertEqual(
            [evento['sequence'] for evento in contrato['events']],
            [1, 2, 3, 4, 5],
        )
        self.assertTrue(
            any('pareamento por ordem de evento' in p
                for p in montado['pendencias'])
        )

    def test_caucaia_usa_prefeitura_como_contratante(self):
        # A migração de clientes achata as secretarias de Caucaia em endereços
        # da prefeitura única; os quatro contratos apontam para a raiz.
        for contrato_id in (149, 150, 151, 152):
            with self.subTest(contrato_id=contrato_id):
                montado = self._documento(contrato_id)
                contrato = montado['documento']['contract']
                self.assertEqual(
                    contrato['customer_alias'], 'PREFEITURA MUNICIPAL DE CAUCAIA'
                )
                self.assertEqual(
                    contrato['recipient_aliases'],
                    ['PREFEITURA MUNICIPAL DE CAUCAIA'],
                )
                self.assertTrue(
                    any('cliente ajustado por de/para' in p
                        for p in montado['pendencias'])
                )

    def test_seab_e_sepa_resolvem_por_de_para(self):
        # 168: a planilha usa o nome por extenso da secretaria do Amapá.
        montado = self._documento(168)
        contrato = montado['documento']['contract']
        self.assertEqual(
            contrato['customer_alias'],
            'GOV. ESTAD. AMAPÁ - SEC. EXTRAOR. REPRESENTAÇÃO DO GOV. DO '
            'ESTADO DO AMAPÁ',
        )
        self.assertFalse(
            any('não existe em customers' in p for p in montado['pendencias'])
        )

        # 262: a planilha traz a SEPA; o customer é a coordenadoria de TI.
        montado = self._documento(262)
        contrato = montado['documento']['contract']
        self.assertEqual(
            contrato['customer_alias'],
            'CE - SECRETARIA DE PROTEÇÃO ANIMAL - SEPA/COORDENADORIA DE '
            'TECNOLOGIA DA INFORMAÇÃO',
        )
        self.assertEqual(
            contrato['recipient_aliases'], [contrato['customer_alias']]
        )
        self.assertFalse(
            any('não existe em customers' in p for p in montado['pendencias'])
        )

    def test_documentos_exportados_passam_na_validacao_do_loader(self):
        for contrato_id in (38, 262, 192):
            with self.subTest(contrato_id=contrato_id):
                montado = self._documento(contrato_id)
                erros, avisos = [], []
                contrato = importer._validar_documento(
                    montado['arquivo'], montado['org_alias'],
                    montado['documento'], erros, avisos,
                )
                self.assertEqual(erros, [])
                self.assertIsNotNone(contrato)

    def test_cliente_ausente_vai_para_pendentes_e_entra_no_relatorio(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            referencia = raiz / '_referencia'
            referencia.mkdir()
            (referencia / 'customers.csv').write_text(
                'id,alias\n1,OUTRO CLIENTE\n', encoding='utf-8'
            )
            relatorio = raiz / 'relatorio.csv'

            resultado = ex.exportar(
                [262], raiz=raiz, dry_run=False, relatorio=relatorio
            )

            montado = resultado['contratos'][0]
            self.assertEqual(montado['arquivo'].parent.parent.name, '_pendentes')
            self.assertTrue(montado['arquivo'].is_file())
            self.assertEqual(resultado['stats']['pendentes'], 1)

            with relatorio.open(encoding='utf-8') as arquivo:
                linhas = list(csv.DictReader(arquivo))
            self.assertEqual(len(linhas), 1)
            self.assertEqual(linhas[0]['contrato_id'], '262')
            self.assertEqual(linhas[0]['motivo'], 'cliente inexistente em customers')
            self.assertEqual(linhas[0]['arquivo'], 'IP/015-2025__id262.json')

    def test_arquivo_existente_exige_sobrescrever(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            resultado = ex.exportar(
                [262], raiz=raiz, dry_run=False, relatorio=None
            )
            caminho = resultado['contratos'][0]['arquivo']
            self.assertTrue(caminho.is_file())

            with self.assertRaises(FileExistsError):
                ex.exportar([262], raiz=raiz, dry_run=False, relatorio=None)


class AuxiliaresExportacaoTest(unittest.TestCase):

    def test_resolver_tipos_evento(self):
        self.assertEqual(ex._resolver_tipos_evento(None), [1])
        self.assertEqual(ex._resolver_tipos_evento('CADASTRO'), [1])
        self.assertEqual(ex._resolver_tipos_evento('APOSTILAMENTO'), [7])
        self.assertEqual(
            ex._resolver_tipos_evento('ADITIVO DE PRAZO E QUANTIDADE'), [2, 3]
        )

    def test_quantidade_inteira_e_problemas(self):
        self.assertEqual(ex._quantidade(None), (0, None))
        self.assertEqual(ex._quantidade(10), (10, None))
        self.assertEqual(ex._quantidade('10'), (10, None))

        inteiro, problema = ex._quantidade(2.4)
        self.assertEqual(inteiro, 2)
        self.assertIn('arredondada para 2', problema)

        self.assertEqual(ex._quantidade('abc'), (0, 'quantidade não numérica '
                                                   "'abc' convertida em 0"))

    def test_data_inicial_sem_cadastro_usa_aditivo_mais_antigo(self):
        aditivos = [
            {'tipo': 2, 'info': {'start_date': '2024-06-01'}},
            {'tipo': 2, 'info': {'start_date': '2024-01-01'}},
        ]
        pendencias = []

        self.assertEqual(
            ex._data_inicial(aditivos, 1, pendencias), '2024-01-01'
        )
        self.assertTrue(pendencias)

    def test_data_inicial_sem_info_e_erro(self):
        with self.assertRaises(ValueError):
            ex._data_inicial([{'tipo': 2, 'info': None}], 1, [])

    def test_clonar_sem_dados_copia_linhagem_e_duracao(self):
        aditivos = [
            {'tipo': 1, 'evento_id': 1, 'info': {'duration': 12},
             'itens': [{'alias': 'KIT', 'months': 12, 'lineage_key': 'kit#0'}],
             'servicos': [{'alias': 'SUPORTE', 'months': 12,
                           'lineage_key': 'suporte#0'}]},
            {'tipo': 2, 'evento_id': 2, 'info': {'duration': 6},
             'itens': [], 'servicos': []},
        ]
        pendencias = []

        ex._clonar_sem_dados(aditivos, pendencias)

        self.assertEqual(pendencias, [])
        self.assertEqual(aditivos[1]['itens'][0]['lineage_key'], 'kit#0')
        self.assertEqual(aditivos[1]['itens'][0]['months'], 6)
        self.assertEqual(aditivos[1]['servicos'][0]['lineage_key'], 'suporte#0')
        self.assertEqual(aditivos[1]['servicos'][0]['months'], 6)
        # O aditivo de origem não pode ser alterado.
        self.assertEqual(aditivos[0]['itens'][0]['months'], 12)

    def test_clonar_sem_origem_registra_pendencia(self):
        aditivos = [
            {'tipo': 2, 'evento_id': 7, 'info': {'duration': 6},
             'itens': [], 'servicos': []},
        ]
        pendencias = []

        ex._clonar_sem_dados(aditivos, pendencias)

        self.assertEqual(aditivos[0]['itens'], [])
        self.assertEqual(len(pendencias), 1)
        self.assertIn('sem aditivo anterior com itens', pendencias[0])

    def test_montar_linhas_numera_ocorrencias(self):
        import pandas as pd

        linhas = [
            pd.Series({'APELIDO': 'NOTEBOOK', 'DESCRICAO': '',
                       'QUANTIDADE': 1, 'VALOR_UNITARIO': 10}),
            pd.Series({'APELIDO': 'notebook', 'DESCRICAO': '',
                       'QUANTIDADE': 2, 'VALOR_UNITARIO': 10}),
        ]
        itens = ex._montar_linhas(linhas, {'duration': 12}, [], 'teste')

        self.assertEqual([item['occurrence'] for item in itens], [0, 1])
        self.assertEqual(
            [item['lineage_key'] for item in itens],
            [f'{cj.normalizar("NOTEBOOK")}#0', f'{cj.normalizar("NOTEBOOK")}#1'],
        )
        self.assertTrue(all(item['months'] == 12 for item in itens))


if __name__ == '__main__':
    unittest.main()
