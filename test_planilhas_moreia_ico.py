import os
import tempfile
import unittest

import openpyxl

from cadastros.contratos.planilhas_moreia_ico import (
    ORGANIZACAO_MOREIA,
    SECRETARIAS,
    carregar_planilhas_moreia,
    ler_contrato_moreia,
    localizar_arquivos_moreia,
)
from cadastros.contratos.planilhas_contrato_2026 import (
    PlanilhaContratoInvalida,
    PASTA_PADRAO,
)

A9_PADRAO = (
    'CONTRATO Nº: 11/2022-01 - SRP - VIG: 01/01/23 A 31/12/23 + 1º ADT DE '
    'PRAZO - VIG: 01/01/24 A 31/12/24 + 2º ADT DE PRAZO - VIG: 01/01/25 A '
    '31/12/25 - TOTAL 60 MESES'
)


def criar_ficha(
    caminho,
    *,
    numero='11/2022-01',
    a9=A9_PADRAO,
    itens=None,
    total_global='R$ 2.400,00',
    total_mensal='R$ 200,00',
    dotacao_colunas=5,
    abas_extras=None,
):
    """Gera uma ficha no layout ICÓ com dotação configurável."""
    if itens is None:
        itens = [
            ('1', 'MICROCOMPUTADOR TIPO I - ESPECIFICAÇÕES MÍNIMAS: Desktop',
             2, 100.0, 12),
            ('0', 'ITEM ZERADO - NÃO DEVE ENTRAR', 0, 50.0, 12),
        ]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'TESTE-NOV26'
    ws['A2'] = 'MUNICÍPIO DE ICÓ'
    ws['A4'] = 'CONTRATANTE: SECRETARIA DE EDUCAÇÃO DO MUNICIPIO DE ICÓ/CE'
    ws['A7'] = 'OBJETO: REGISTRO DE PREÇOS PARA LOCAÇÃO DE EQUIPAMENTOS.'
    ws['A9'] = a9

    ws['A11'] = 'ITEM'
    ws['B11'] = 'ESPECIFICAÇÃO EQUIPAMENTO'
    ws['A12'] = 'ITEM'
    ws['B12'] = 'ESPECIFICAÇÃO EQUIPAMENTO'
    for indice in range(dotacao_colunas):
        ws.cell(row=13, column=5 + indice).value = f'DOT{indice}'
    base = 5 + dotacao_colunas
    ws.cell(row=12, column=base).value = 'TOTAL'
    ws.cell(row=12, column=base + 1).value = 'R$ UNIT'
    ws.cell(row=12, column=base + 2).value = 'MÊS'
    ws.cell(row=12, column=base + 3).value = 'R$ GLOBAL/ ANUAL'

    linha = 14
    ws.cell(row=linha, column=1).value = 'LOTE I'
    linha += 1
    for _, descricao, quantidade, preco, meses in itens:
        ws.cell(row=linha, column=1).value = len(descricao)  # qualquer valor
        ws.cell(row=linha, column=2).value = descricao
        ws.cell(row=linha, column=base).value = quantidade
        ws.cell(row=linha, column=base + 1).value = preco
        ws.cell(row=linha, column=base + 2).value = meses
        linha += 1

    ws.cell(row=linha, column=1).value = 'R$ VALOR GLOBAL/ANUAL: LOTE I + LOTE II'
    ws.cell(row=linha, column=base + 2).value = total_global
    linha += 1
    ws.cell(row=linha, column=1).value = 'R$ VALOR MENSAL: LOTE I + LOTE II'
    ws.cell(row=linha, column=base + 2).value = total_mensal

    for nome_aba, texto in (abas_extras or {}).items():
        extra = wb.create_sheet(nome_aba)
        extra['A9'] = texto

    wb.save(caminho)
    return caminho


def criar_pasta_completa(pasta):
    """Cria uma ficha válida para cada número mapeado em <pasta>/MOREIA."""
    pasta = os.path.join(pasta, 'MOREIA')
    os.makedirs(pasta, exist_ok=True)
    for numero, config in SECRETARIAS.items():
        criar_ficha(
            os.path.join(pasta, f'PLANILHA - {config["arquivo_contem"]}.xlsx'),
            numero=numero,
            a9=A9_PADRAO.replace('11/2022-01', numero),
        )
    return pasta


class TestLeituraFichaIco(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def caminho(self, nome='PLANILHA - EDUCAÇÃO.xlsx'):
        return os.path.join(self.pasta, nome)

    def test_le_contrato_com_dotacao_larga(self):
        caminho = criar_ficha(self.caminho(), dotacao_colunas=7)
        contrato = ler_contrato_moreia(caminho)

        self.assertEqual(contrato.numero, '11/2022-01')
        self.assertEqual(contrato.organizacao_id, ORGANIZACAO_MOREIA)
        self.assertEqual(contrato.destinatario_alias,
                         SECRETARIAS['11/2022-01']['destinatario_alias'])
        self.assertEqual(contrato.duracao_meses, 36)
        self.assertEqual(contrato.max_duracao_meses, 60)
        self.assertEqual(contrato.max_fim.isoformat(), '2027-12-31')
        self.assertEqual(contrato.inicio.isoformat(), '2023-01-01')
        self.assertEqual(contrato.fim.isoformat(), '2025-12-31')

    def test_ignora_item_com_quantidade_zero(self):
        caminho = criar_ficha(self.caminho())
        contrato = ler_contrato_moreia(caminho)

        self.assertEqual(len(contrato.itens), 1)
        item = contrato.itens[0]
        self.assertEqual(item.nome, 'MICROCOMPUTADOR TIPO I')
        self.assertEqual(item.quantidade, 2)
        self.assertEqual(item.preco, 100.0)
        self.assertEqual(item.meses, 12)
        self.assertEqual(item.numero_tr, 1)
        self.assertAlmostEqual(contrato.total_calculado, 2400.0)

    def test_erro_quando_total_nao_fecha(self):
        caminho = criar_ficha(self.caminho(), total_global='R$ 9.999,00')

        with self.assertRaises(PlanilhaContratoInvalida) as ctx:
            ler_contrato_moreia(caminho)
        self.assertIn('total global não fecha', str(ctx.exception))

    def test_erro_para_numero_fora_do_mapa(self):
        caminho = criar_ficha(
            self.caminho(),
            numero='11/2022-07',
            a9=A9_PADRAO.replace('11/2022-01', '11/2022-07'),
        )

        with self.assertRaises(PlanilhaContratoInvalida) as ctx:
            ler_contrato_moreia(caminho)
        self.assertIn('não está no mapa', str(ctx.exception))

    def test_erro_quando_arquivo_nao_corresponde_ao_numero(self):
        caminho = criar_ficha(self.caminho('PLANILHA - SAÚDE.xlsx'))

        with self.assertRaises(PlanilhaContratoInvalida) as ctx:
            ler_contrato_moreia(caminho)
        self.assertIn('não corresponde', str(ctx.exception))

    def test_erro_quando_abas_apontam_numeros_diferentes(self):
        caminho = criar_ficha(
            self.caminho(),
            abas_extras={
                'OUTRA ABA': A9_PADRAO.replace('11/2022-01', '11/2022-02'),
            },
        )

        with self.assertRaises(PlanilhaContratoInvalida) as ctx:
            ler_contrato_moreia(caminho)
        self.assertIn('números de contrato diferentes', str(ctx.exception))

    def test_escolhe_aba_com_vigencia_mais_recente(self):
        caminho = criar_ficha(
            self.caminho(),
            abas_extras={
                'ABA ANTIGA': (
                    'CONTRATO Nº: 11/2022-01 - VIG: 01/01/23 A 31/12/23'
                ),
            },
        )
        contrato = ler_contrato_moreia(caminho)

        self.assertEqual(contrato.fim.isoformat(), '2025-12-31')


class TestCarregamentoPastaIco(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = criar_pasta_completa(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_carrega_as_quatorze_fichas(self):
        contratos = carregar_planilhas_moreia(self.tmp.name)

        self.assertEqual(len(contratos), 14)
        self.assertEqual({c.numero for c in contratos}, set(SECRETARIAS))
        self.assertNotIn('11/2022-07', {c.numero for c in contratos})
        self.assertEqual(
            {c.organizacao_id for c in contratos}, {ORGANIZACAO_MOREIA}
        )

    def test_erro_quando_falta_ficha(self):
        os.remove(os.path.join(
            self.pasta,
            f'PLANILHA - {SECRETARIAS["11/2022-14"]["arquivo_contem"]}.xlsx',
        ))

        with self.assertRaises(PlanilhaContratoInvalida) as ctx:
            carregar_planilhas_moreia(self.tmp.name)
        self.assertIn('Faltam fichas', str(ctx.exception))

    def test_erro_quando_contrato_repete(self):
        criar_ficha(
            os.path.join(self.pasta, 'PLANILHA - EDUCAÇÃO EXTRA.xlsx'),
            numero='11/2022-01',
        )

        with self.assertRaises(PlanilhaContratoInvalida) as ctx:
            carregar_planilhas_moreia(self.tmp.name)
        self.assertIn('repetidos', str(ctx.exception))


class TestPastaRealIco(unittest.TestCase):
    def test_pasta_real_tem_quatorze_arquivos(self):
        pasta = os.path.join(PASTA_PADRAO, 'MOREIA')
        if not os.path.isdir(pasta) or not any(
            nome.lower().endswith('.xlsx') for nome in os.listdir(pasta)
        ):
            self.skipTest('docs/contratos/MOREIA sem planilhas reais')
        arquivos = localizar_arquivos_moreia()
        self.assertEqual(len(arquivos), 14)


if __name__ == '__main__':
    unittest.main()
