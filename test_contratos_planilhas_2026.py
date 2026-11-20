import os
import tempfile
import unittest
from datetime import date

import openpyxl

from cadastros.contratos.planilhas_contrato_2026 import (
    DESTINATARIOS_ESPECIFICOS_POR_NUMERO,
    ItemPlanilha,
    PlanilhaContratoInvalida,
    _localizar_arquivos,
    _organizacao_da_pasta,
    _validar_totais,
    carregar_planilhas,
    ler_planilha,
)


CONFIG = {
    'arquivo_contem': 'TESTE',
    'destinatario_alias': 'SECRETARIA TESTE',
    'titulo_chave': 'SECRETARIA TESTE',
}


def criar_planilha(caminho, *, total='1200,00', itens=None, titulo=None):
    """Gera uma ficha mínima no mesmo layout das planilhas reais."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws['A2'] = 'PREFEITURA MUNICIPAL DE TAUÁ'
    ws['A3'] = (
        'CONTRATANTE: O Município de Tauá, inscrito no CNPJ/MF sob o no '
        '07.849.532/0001-47.'
    )
    ws['A4'] = 'OBJETO: Locação de equipamentos de informática.'
    ws['A5'] = 'FORNECEDOR: IP SERVIÇOS'
    ws['A6'] = (
        'CONTRATO Nº 99.99.999/2026 - VIGÊNCIA (27/08/2026 - 27/08/2027).'
    )
    ws['A11'] = 'Nº'
    itens = itens if itens is not None else [
        (1, 'MICRO TIPO I - descrição', 1, 100.0, 12),
    ]
    for deslocamento, (numero, descricao, qtd, preco, meses) in enumerate(itens):
        linha = 12 + deslocamento
        ws.cell(linha, 1, numero)
        ws.cell(linha, 2, descricao)
        ws.cell(linha, 4, qtd)
        ws.cell(linha, 6, preco)
        ws.cell(linha, 7, meses)
    ws['A30'] = 'VALOR GLOBAL LOTE I'
    ws['G30'] = total
    ws['A31'] = titulo or 'PREVISÃO SECRETARIA TESTE'
    wb.save(caminho)
    return caminho


class PlanilhasReaisTests(unittest.TestCase):
    """Confere o conteúdo conhecido das 4 planilhas de docs/contratos."""

    @classmethod
    def setUpClass(cls):
        cls.contratos = carregar_planilhas()
        cls.por_numero = {c.numero: c for c in cls.contratos}

    def test_quatro_contratos_com_numeros_esperados(self):
        self.assertEqual(len(self.contratos), 4)
        self.assertEqual(
            set(self.por_numero),
            {
                '20.08.002/2026',
                '14.09.001/2026-SEDERHI',
                '20.08.001/2026-GABP',
                '21.08.001/2026',
            },
        )

    def test_quantidade_de_itens_e_totais(self):
        esperado = {
            '20.08.002/2026': (4, 16992.48),
            '14.09.001/2026-SEDERHI': (3, 23222.64),
            '20.08.001/2026-GABP': (9, 58374.24),
            '21.08.001/2026': (4, 18075.00),
        }
        for numero, (qtd, total) in esperado.items():
            contrato = self.por_numero[numero]
            self.assertEqual(len(contrato.itens), qtd, numero)
            self.assertAlmostEqual(contrato.total_calculado, total, places=2)

    def test_vigencia_de_doze_meses(self):
        for contrato in self.contratos:
            self.assertEqual(contrato.inicio, date(2026, 8, 27))
            self.assertEqual(contrato.fim, date(2027, 8, 27))
            self.assertEqual(contrato.duracao_meses, 12)

    def test_extras_ficam_fora_dos_itens(self):
        self.assertEqual(sum(len(c.extras) for c in self.contratos), 7)
        for contrato in self.contratos:
            for extra in contrato.extras:
                self.assertNotIn(
                    extra.nome.upper(),
                    [item.nome.upper() for item in contrato.itens],
                )

    def test_nomes_dos_contratos_sao_unicos(self):
        nomes = [contrato.contrato_nome for contrato in self.contratos]
        self.assertEqual(len(nomes), len(set(nomes)))

    def test_organizacao_inferida_da_pasta(self):
        for contrato in self.contratos:
            self.assertEqual(contrato.organizacao_id, 1311)
            self.assertEqual(contrato.contratante_alias, 'PREFEITURA MUNICIPAL DE TAÚA')

    def test_mapa_de_destinatarios_casa_com_as_planilhas(self):
        self.assertEqual(
            set(DESTINATARIOS_ESPECIFICOS_POR_NUMERO), set(self.por_numero)
        )
        for numero, contrato in self.por_numero.items():
            self.assertEqual(
                DESTINATARIOS_ESPECIFICOS_POR_NUMERO[numero],
                contrato.destinatario_alias,
            )

    def test_cnpj_divergente_apenas_sinaliza(self):
        self.assertFalse(self.por_numero['14.09.001/2026-SEDERHI'].cnpj_divergente)
        self.assertTrue(self.por_numero['20.08.002/2026'].cnpj_divergente)


class EstruturaPorOrganizacaoTests(unittest.TestCase):
    """Valida a inferência de organização pela pasta e a busca recursiva."""

    def test_pastas_mapeadas(self):
        self.assertEqual(_organizacao_da_pasta('IP'), 1311)
        self.assertEqual(_organizacao_da_pasta('ALUCOM'), 1115)
        self.assertEqual(_organizacao_da_pasta('MOREIA'), 1122)
        self.assertEqual(_organizacao_da_pasta('AS'), 1378)
        self.assertEqual(_organizacao_da_pasta('AS SISTEMAS'), 1378)

    def test_pasta_desconhecida_aborta(self):
        with self.assertRaises(PlanilhaContratoInvalida):
            _organizacao_da_pasta('ORGANIZACAO NOVA')

    def test_localizacao_recursiva_por_subpasta(self):
        origem = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'docs', 'contratos', 'IP'
        )
        with tempfile.TemporaryDirectory() as tmp:
            pasta_ip = os.path.join(tmp, 'IP')
            os.makedirs(pasta_ip)
            for nome in os.listdir(origem):
                if nome.lower().endswith('.xlsx'):
                    destino = os.path.join(pasta_ip, nome)
                    with open(os.path.join(origem, nome), 'rb') as src, \
                            open(destino, 'wb') as dst:
                        dst.write(src.read())

            pares = _localizar_arquivos(tmp)
            self.assertEqual(len(pares), 4)
            for _config, caminho, organizacao_id in pares:
                self.assertEqual(organizacao_id, 1311)
                self.assertEqual(
                    os.path.basename(os.path.dirname(caminho)), 'IP'
                )

    def test_arquivo_na_raiz_e_ignorado(self):
        with tempfile.TemporaryDirectory() as tmp:
            criar_planilha(os.path.join(tmp, 'PLANILHA TESTE.xlsx'))
            with self.assertRaises(PlanilhaContratoInvalida):
                _localizar_arquivos(tmp)


class PlanilhaSinteticaTests(unittest.TestCase):
    """Valida as guardas do parser com fichas geradas em diretório temporário."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _arquivo(self, nome='PLANILHA TESTE.xlsx', **kwargs):
        return criar_planilha(
            os.path.join(self.tmp.name, nome), **kwargs
        )

    def test_leitura_valida(self):
        contrato = ler_planilha(self._arquivo(), CONFIG)
        self.assertEqual(contrato.numero, '99.99.999/2026')
        self.assertEqual(contrato.duracao_meses, 12)
        self.assertEqual(len(contrato.itens), 1)
        self.assertAlmostEqual(contrato.total_calculado, 1200.0)

    def test_total_divergente_aborta(self):
        with self.assertRaises(PlanilhaContratoInvalida):
            ler_planilha(self._arquivo(total='999,00'), CONFIG)

    def test_titulo_diferente_aborta(self):
        with self.assertRaises(PlanilhaContratoInvalida):
            ler_planilha(self._arquivo(titulo='PREVISÃO OUTRA COISA'), CONFIG)

    def test_item_duplicado_aborta(self):
        itens = [
            (1, 'MICRO - a', 1, 100.0, 12),
            (2, 'MICRO - b', 1, 100.0, 12),
        ]
        with self.assertRaises(PlanilhaContratoInvalida):
            ler_planilha(self._arquivo(itens=itens, total='2400,00'), CONFIG)

    def test_validar_totais_sem_itens(self):
        from cadastros.contratos.planilhas_contrato_2026 import ContratoPlanilha
        contrato = ContratoPlanilha(
            arquivo='x.xlsx', numero='1', contratante='TAUA',
            cnpj=None, objeto='o', inicio=date(2026, 1, 1),
            fim=date(2026, 12, 31), duracao_meses=12,
            itens=[ItemPlanilha(1, 'A', 'A', 1, 100.0, 12)],
            total_planilha=1200.0,
        )
        _validar_totais(contrato)  # não levanta

        contrato.total_planilha = 1.0
        with self.assertRaises(PlanilhaContratoInvalida):
            _validar_totais(contrato)


if __name__ == '__main__':
    unittest.main()
