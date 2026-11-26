import re
import unicodedata
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from config.config import (
    CLIENTES_BLOQUEADOS,
    CLIENTES_ESTOQUE,
    SETORES_IGNORADOS,
    ORGANIZACOES_BLOQUEADAS,
    FALSOS_RESERVAS,
    BASES_AVULSOS, ENDERECOS_BASES, RESERVAS_BASE_AVULSA,
    DEPARA_RESERVAS_BASE, RESERVAS_DESTINO_PREFEITURA,
    MESCLAGENS_CLIENTE,
)
from utils.sanetizador import normalizar_para_match, executar_truncate_tabelas
from utils.mapeador import descobrir_id_organizacao

TABELAS_CLIENTES = [
    'addresses', 
    'customers'
]

#De/PARA de clientes e dicionarios
DEPARA_EXCECOES = {
        10824: 10711,
        4182: 4174,
        10450: 3915,
        10427: 10414,
        10853: 10806,
        10434: 3634,
        3362: 3332,
        4439: 2879,
        4467: 4465,
        4464: 4466,
        11920: 11919,
        10935: 10823,
        10862: 10819,
        10866: 10865,
        # 3259:
        # 4089:
        # 4468:
        # 4504:
        # 4516:
        # 4544:
        # 10401:
        # 10482:
        # 10487:
        # 10513:
        # 10522:
        # 10556:
        # 10557:
        # 10558:
        # 10559:
        # 10560:
        # 10561:
        # 10562:
        # 10563:
        # 10590:
        # 10654:
        # 10671:
        # 10681:
        # 10696:
        # 10708:
        # 10714:
        # 10716:
        # 10722:
        # 10752:
        # 10754:
        # 10816:
        # 10822:
        # 10825:
        # 10827:
        # 10830:
        # 10835:
        # 10838:
        # 10843:
        # 10850:
        # 10854:
        # 10859:
        # 10872:
        # 10932:
        # 10947:
        # 10972:
        # 10973:
        # 11054:
        # 11059:
        # 11060:
        # 11061:
        # 11062:
        # 11063:
        # 11064:
        # 11065:
        # 11066:
        # 11067:
        # 11068:
        # 11069:
        # 11405:
        # 11421:

}
MAPA_SAO_LUIS = {
        # === IPAM (Secretaria 1194) ===
        11791: (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        4467:  (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        4465:  (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        11932: (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        11933: (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        
        # === SEMUSC (Secretaria 1197) ===
        4464:  (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        4466:  (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        10502: (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        10513: (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        11924: (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        
        # === SMTT (Secretaria 1303) ===
        10714: (1303, "SECRETARIA DE TRÂNSITO E TRANSPORTES - SMTT"),
        10591: (1303, "SECRETARIA DE TRÂNSITO E TRANSPORTES - SMTT"),
        11928: (1303, "SECRETARIA DE TRÂNSITO E TRANSPORTES - SMTT"),
        
        # === SEMFAZ / PM SÃO LUIS (Secretaria 1220) ===
        10400: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
        10401: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
        11919: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
        11920: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
    }
SAO_LUIS_CONFIG = {
    "ID_PREF_ALVO": 287,
    "NOME_PREF_ALVO": "PREFEITURA MUNICIPAL DE SÃO LUÍS"
}
# Prefeituras que aparecem repetidas no legado e devem virar um único cliente
# no refatorado. A mesclagem absorve as raízes, as secretarias e os clientes
# de destino; o nome do destino original permanece no alias do endereço.
# `organizacao_alvo` fixa a organização do cliente raiz (sem isso a última
# linha lida decidiria a organização, o que varia conforme a ordem do legado).
MESCLAGENS_PREFEITURA = {
    "CAUCAIA": {
        "ids": (2, 295, 300),
        "id_alvo": 2,
        "nome_alvo": "PREFEITURA MUNICIPAL DE CAUCAIA",
        "organizacao_alvo": 1115,
    },
    "BARROQUINHA": {
        "ids": (6, 105),
        "id_alvo": 6,
        "nome_alvo": "PREFEITURA MUNICIPAL DE BARROQUINHA",
        "organizacao_alvo": 1115,
    },
    # Duas raízes de Tauá no legado: 39 (org. 1115) e 77 (org. 1122).
    # Os contratos de Tauá estão na org. 1115, então ela é a dona do cadastro
    # unificado; os 23 destinos da org. 1122 passam a ser endereços dela.
    # A raiz 312 (IP/1311) fica fora de propósito: as planilhas 2026 da IP
    # (docs/contratos/IP/...TAUÁ...) exigem uma raiz própria na org. 1311.
    "TAUA": {
        "ids": (39, 77),
        "id_alvo": 39,
        "nome_alvo": "PREFEITURA MUNICIPAL DE TAUA",
        "organizacao_alvo": 1115,
    },
    # Aquiraz também aparece duplicada: 27 (ALUCOM/1115) e 57 (MOREIA/1122).
    # Mesma regra de Tauá: a raiz da ALUCOM concentra o cadastro e os 54
    # destinos da org. 1122 passam a ser endereços dela.
    "AQUIRAZ": {
        "ids": (27, 57),
        "id_alvo": 27,
        "nome_alvo": "PREFEITURA MUNICIPAL DE AQUIRAZ",
        "organizacao_alvo": 1115,
    },
    # ICÓ tem duas raízes com o mesmo órgão: 37 (ALUCOM/1115, grafia "ICO",
    # 8 setores/21 clientes) e 292 (MOREIA/1122, grafia "ICÓ", 16 setores/45
    # clientes). Ao contrário das anteriores, a dona do cadastro é a 292/1122:
    # é nela que estão os contratos de ICÓ (docs/contratos_json/MOREIA) e a
    # raiz que ``planilhas_moreia_ico.py`` procura.
    # As secretarias NÃO são achatadas: os contratos da MOREIA apontam para os
    # 16 setores da 292 e os da AS_SISTEMAS para os 8 setores da 37 (nomes
    # diferentes, sem colisão de alias). Achatar qualquer um dos lados
    # apagaria customers usados no de/para de contrato. Os setores da 37
    # passam a ser filhos da raiz 292 e seus clientes viram endereços deles.
    "ICO": {
        "ids": (37, 292),
        "id_alvo": 292,
        "nome_alvo": "PREFEITURA MUNICIPAL DE ICÓ",
        "organizacao_alvo": 1122,
        "preservar_hierarquia": True,
    },
}

# Clientes-raiz do legado (sem setor/departamento) que são apenas o cadastro
# repetido de uma prefeitura existente. A linha não vira um customer próprio:
# passa a ser um endereço da raiz, preservando o ``legacy_customer_id`` para
# movimentos e contratos. Chave: id do cliente no legado; valor: departamento
# que absorve o cadastro.
CLIENTES_RAIZ_EM_PREFEITURA = {
    903: 27,  # PREFEITURA MUNICIPAL DE AQUIRAZ -> prefeitura 27 (ALUCOM)
}
MAPA_REGIOS_PCPB = {
    "CAMPINA GRANDE": "CAMPINA GRANDE",
    "GUARABIRA": "GUARABIRA",
    "JOAO PESSOA": "JOÃO PESSOA",
    "CAPITAL": "JOÃO PESSOA",
    "MANGABEIRA": "JOÃO PESSOA",
    "PATOS": "PATOS",
    "SANTA RITA": "SANTA RITA",
    "ALHANDRA": "ALHANDRA",
    "CAAPORA": "CAAPORÃ",
    "MAMANGUAPE": "MAMANGUAPE",
    "CONDE": "CONDE",
    "PEDRAS DE FOGO": "PEDRAS DE FOGO",
    "PITIMBU": "PITIMBU",
    "PILAR": "PILAR",
    "PILOES": "PILÕES",
    "JERICO": "JERICÓ"
}
MAPA_IDS_SINTETICOS_PCPB = {
    "JOÃO PESSOA": 900001,
    "CAMPINA GRANDE": 900002,
    "GUARABIRA": 900003,
    "PATOS": 900004,
    "SANTA RITA": 900005,
    "ALHANDRA": 900006,
    "CAAPORÃ": 900007,
    "MAMANGUAPE": 900008,
    "CONDE": 900009,
    "PEDRAS DE FOGO": 900010,
    "PITIMBU": 900011,
    "PILAR": 900012,
    "PILÕES": 900013,
    "JERICÓ": 900014,
    "SEDE / OUTROS": 900015
}
MAPA_MINISTERIO_RELACOES = {
    "PREFEITURA": (375, 322),
    "SECRETARIA": (1424, 1347)
}
MAPA_MINISTERIO_TRANSPORTES = {
    "PREFEITURA": (337, 330)
}
DETRAN_CONFIG = {
    "ID_PAI_SINTETICO": 910000,
    "NOME_PAI": "DEPARTAMENTO ESTADUAL DE TRÂNSITO - DETRAN",
    "UNIDADES": {
        416: {
            "id_filho_sintetico": 910001,
            "nome_filho": "DETRAN/CE",
            "uf": "CE",
        },
        361: {
            "id_filho_sintetico": 910002,
            "nome_filho": "DETRAN/MA",
            "uf": "MA",
        },
    },
}
HOSPITAIS_PB_CONFIG = {
    10688: {"id_pai": 920001, "nome": "HULW - HOSPITAL UNIVERSITÁRIO LAURO WANDERLEY-JOÃO PESSOA-PB"},
    10822: {"id_pai": 920001, "nome": "HULW - HOSPITAL UNIVERSITÁRIO LAURO WANDERLEY-JOÃO PESSOA-PB"},
    10687: {"id_pai": 920002, "nome": "HUJB-HOSPITAL UNIVERSITÁRIO JULIO BANDEIRA-CAJAZEIRAS-PB"},
    10752: {"id_pai": 920002, "nome": "HUJB-HOSPITAL UNIVERSITÁRIO JULIO BANDEIRA-CAJAZEIRAS-PB"},
    10686: {"id_pai": 920003, "nome": "HUAC - HOSPITAL UNIVERSITÁRIO ALCIDES CARNEIRO-CAMPINA GRANDE-PB"}
}


def config_destino_reserva(id_legado):
    """Endereço genérico configurado para uma reserva legada (ou ``None``).

    As reservas listadas em ``RESERVAS_DESTINO_PREFEITURA`` não têm cliente
    titular no refatorado; o endereço delas é criado sob a prefeitura
    indicada, com alias próprio.
    """
    try:
        return RESERVAS_DESTINO_PREFEITURA.get(int(id_legado))
    except (TypeError, ValueError):
        return None


def ids_reserva_com_endereco_proprio():
    """IDs de reserva que recebem endereço próprio (base ou prefeitura)."""
    return set(RESERVAS_BASE_AVULSA) | set(RESERVAS_DESTINO_PREFEITURA)


def hierarquia_destino(id_legado, id_prefeitura, id_secretaria):
    """Hierarquia efetiva do endereço, aplicando o destino genérico de reserva.

    Uma reserva configurada em ``RESERVAS_DESTINO_PREFEITURA`` é anexada
    diretamente ao departamento indicado, ignorando secretaria/prefeitura do
    legado (o cliente de reserva não tem setor).
    """
    config = config_destino_reserva(id_legado)
    if config is None:
        return id_prefeitura, id_secretaria
    return config["departamento_legado"], None


class MigracaoClientes:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.stats = {"pais": 0, "filhos": 0, "enderecos": 0, "mesclados": 0}
        self.clientes_descartados = []
        self.arquivo_log_descartados = 'ajustes/clientes_descartados_migracao.csv'
        self.arquivo_duplicados = 'ajustes/clientes_duplicados_similaridade.csv'
        self.arquivo_duplicados_json = 'ajustes/clientes_duplicados_similaridade.json'
        self.arquivo_depara_duplicados = 'ajustes/clientes_de_para_duplicados.csv'

    def _registrar_clientes_descartados(self, df: pd.DataFrame, motivo: str):
        """Acumula linhas de clientes excluídos para auditoria, sem alterar o fluxo."""
        if df.empty:
            return

        colunas = [
            'ID_CLIENTE', 'CLIENTE', 'ID_PREFEITURA', 'PREFEITURA',
            'ID_SECRETARIA', 'SECRETARIA', 'ORGANIZACAO', 'ORG_DESTINO',
            'CNPJ', 'CIDADE', 'ESTADO', 'MOTIVO_DESCARTE',
        ]
        registro = df.copy()
        registro['MOTIVO_DESCARTE'] = motivo
        for coluna in colunas:
            if coluna not in registro.columns:
                registro[coluna] = None
        self.clientes_descartados.append(registro[colunas])

    def _salvar_log_clientes_descartados(self):
        import os

        os.makedirs(os.path.dirname(self.arquivo_log_descartados), exist_ok=True)
        if self.clientes_descartados:
            df_log = pd.concat(self.clientes_descartados, ignore_index=True)
            df_log = df_log.drop_duplicates(subset=['ID_CLIENTE', 'MOTIVO_DESCARTE'])
        else:
            df_log = pd.DataFrame(columns=[
                'ID_CLIENTE', 'CLIENTE', 'ID_PREFEITURA', 'PREFEITURA',
                'ID_SECRETARIA', 'SECRETARIA', 'ORGANIZACAO', 'ORG_DESTINO',
                'CNPJ', 'CIDADE', 'ESTADO', 'MOTIVO_DESCARTE',
            ])
        df_log.to_csv(self.arquivo_log_descartados, index=False, encoding='utf-8-sig')
        print(
            f"📄 Log de clientes descartados salvo em '{self.arquivo_log_descartados}' "
            f"({len(df_log)} registros)."
        )

    # ==============================================================================
    # RELATÓRIO DE CLIENTES DUPLICADOS POR SIMILARIDADE DE NOME
    # ==============================================================================
    # Similaridade estritamente acima de 95% sobre o nome normalizado.
    LIMIAR_DUPLICIDADE = 0.95
    COLUNAS_DUPLICADOS = [
        'similaridade_pct',
        'id_legado_a', 'cliente_legado_a', 'customer_id_a', 'address_id_a',
        'endereco_refatorado_a', 'id_prefeitura_a', 'prefeitura_a',
        'id_secretaria_a', 'secretaria_a',
        'id_legado_b', 'cliente_legado_b', 'customer_id_b', 'address_id_b',
        'endereco_refatorado_b', 'id_prefeitura_b', 'prefeitura_b',
        'id_secretaria_b', 'secretaria_b',
        'mesma_hierarquia',
    ]

    @staticmethod
    def _tokens_bloqueio(nome_normalizado: str):
        return [token for token in nome_normalizado.split() if len(token) >= 3]

    def _candidatos_por_bloco(self, nomes: list):
        """Gera pares candidatos via índice invertido por token e prefixo.

        Comparar todos contra todos seria quadrático numa base de milhares de
        clientes. Como nomes com mais de 95% de similaridade compartilham
        praticamente todos os tokens, bloqueamos por token. Tokens muito
        frequentes (ex.: DE, SAO, SECRETARIA) são ignorados para não formar
        blocos gigantes; nomes sem token útil caem num bloco pela própria chave.
        """
        total = len(nomes)
        if total < 2:
            return set()

        frequencia = {}
        tokens_por_nome = []
        for nome in nomes:
            tokens = set(self._tokens_bloqueio(nome))
            tokens_por_nome.append(tokens)
            for token in tokens:
                frequencia[token] = frequencia.get(token, 0) + 1

        limite_frequente = max(50, int(total * 0.05))
        indice = {}
        for idx, tokens in enumerate(tokens_por_nome):
            chaves = [t for t in tokens if frequencia[t] <= limite_frequente]
            if not chaves:
                # Sem token distintivo: agrupa por prefixo e faixa de tamanho,
                # que é o que ainda garante comparações plausíveis.
                chaves = [
                    '#' + nomes[idx][:12] + '|' + str(len(nomes[idx]) // 10)
                ]
            for chave in chaves:
                indice.setdefault(chave, []).append(idx)

        candidatos = set()
        for chave, indices in indice.items():
            if len(indices) < 2:
                continue
            if len(indices) > 3000:
                print(
                    f"   ⚠️ Bloco de similaridade '{chave}' com {len(indices)} "
                    "nomes ignorado para evitar explosão combinatória."
                )
                continue
            for pos_i in range(len(indices)):
                for pos_j in range(pos_i + 1, len(indices)):
                    candidatos.add((indices[pos_i], indices[pos_j]))
        return candidatos

    def _relacionar_duplicados_clientes(self, df_enderecos_finais, mapa_refatorado):
        """Compara nomes normalizados e grava CSV ligando legado x refatorado.

        Retorna o DataFrame do relatório (vazio se não houver pares acima do
        limiar). Não altera o resultado da migração: é somente auditoria.
        """
        from difflib import SequenceMatcher
        import os

        df = df_enderecos_finais.copy()
        colunas_base = [
            'ID_CLIENTE', 'CLIENTE', 'ID_PREFEITURA', 'PREFEITURA',
            'ID_SECRETARIA', 'SECRETARIA', 'reserved_customer_id',
        ]
        for coluna in colunas_base:
            if coluna not in df.columns:
                df[coluna] = None

        base = df[colunas_base].copy()
        base['id_legado'] = pd.to_numeric(base['ID_CLIENTE'], errors='coerce')
        base = base.dropna(subset=['id_legado'])
        base['id_legado'] = base['id_legado'].astype(int)
        base = base[base['id_legado'].isin(mapa_refatorado)]
        base = base.drop_duplicates(subset=['id_legado'], keep='first')
        base['nome_normalizado'] = base['CLIENTE'].apply(normalizar_para_match)
        base = base[base['nome_normalizado'] != ''].reset_index(drop=True)

        # Agrupa ocorrências por nome normalizado: nomes idênticos geram pares
        # de 100% e a comparação custosa roda só entre nomes distintos.
        grupos = {}
        ordem = []
        for idx, linha in base.iterrows():
            nome = linha['nome_normalizado']
            if nome not in grupos:
                grupos[nome] = []
                ordem.append(nome)
            grupos[nome].append(idx)

        candidatos = self._candidatos_por_bloco(ordem)
        max_pares = 500_000
        truncado = False
        registros = []

        def montar_registro(pos_a, pos_b, similaridade):
            linha_a = base.loc[pos_a]
            linha_b = base.loc[pos_b]
            destino_a = mapa_refatorado[int(linha_a['id_legado'])]
            destino_b = mapa_refatorado[int(linha_b['id_legado'])]
            return {
                'similaridade_pct': round(similaridade * 100, 2),
                'id_legado_a': int(linha_a['id_legado']),
                'cliente_legado_a': linha_a['CLIENTE'],
                'customer_id_a': destino_a.get('customer_id'),
                'address_id_a': destino_a.get('address_id'),
                'endereco_refatorado_a': destino_a.get('alias'),
                'id_prefeitura_a': linha_a['ID_PREFEITURA'],
                'prefeitura_a': linha_a['PREFEITURA'],
                'id_secretaria_a': linha_a['ID_SECRETARIA'],
                'secretaria_a': linha_a['SECRETARIA'],
                'id_legado_b': int(linha_b['id_legado']),
                'cliente_legado_b': linha_b['CLIENTE'],
                'customer_id_b': destino_b.get('customer_id'),
                'address_id_b': destino_b.get('address_id'),
                'endereco_refatorado_b': destino_b.get('alias'),
                'id_prefeitura_b': linha_b['ID_PREFEITURA'],
                'prefeitura_b': linha_b['PREFEITURA'],
                'id_secretaria_b': linha_b['ID_SECRETARIA'],
                'secretaria_b': linha_b['SECRETARIA'],
                'mesma_hierarquia': (
                    str(linha_a['ID_PREFEITURA']) == str(linha_b['ID_PREFEITURA'])
                    and str(linha_a['ID_SECRETARIA']) == str(linha_b['ID_SECRETARIA'])
                ),
            }

        # Nomes idênticos após normalização (ex.: sufixo RESERVA removido).
        for nome in ordem:
            if truncado:
                break
            indices = grupos[nome]
            for pos_i in range(len(indices)):
                for pos_j in range(pos_i + 1, len(indices)):
                    if len(registros) >= max_pares:
                        truncado = True
                        break
                    registros.append(
                        montar_registro(indices[pos_i], indices[pos_j], 1.0)
                    )
                if truncado:
                    break

        for pos_a, pos_b in candidatos:
            if truncado:
                break
            nome_a, nome_b = ordem[pos_a], ordem[pos_b]
            if nome_a == nome_b:
                continue
            menor, maior = sorted((len(nome_a), len(nome_b)))
            # Limite superior do ratio: se já é <= limiar, não vale comparar.
            if (2 * menor) / (menor + maior) <= self.LIMIAR_DUPLICIDADE:
                continue
            similaridade = SequenceMatcher(None, nome_a, nome_b).ratio()
            if similaridade <= self.LIMIAR_DUPLICIDADE:
                continue
            for id_a in grupos[nome_a]:
                for id_b in grupos[nome_b]:
                    if len(registros) >= max_pares:
                        truncado = True
                        break
                    registros.append(montar_registro(id_a, id_b, similaridade))
                if truncado:
                    break

        relatorio = pd.DataFrame(registros, columns=self.COLUNAS_DUPLICADOS)
        if not relatorio.empty:
            relatorio = relatorio.sort_values(
                'similaridade_pct', ascending=False
            ).reset_index(drop=True)
        if truncado:
            print(
                f"   ⚠️ Relatório truncado em {max_pares} pares; "
                "refine o limiar ou a base para o restante."
            )

        os.makedirs(os.path.dirname(self.arquivo_duplicados) or '.', exist_ok=True)
        relatorio.to_csv(self.arquivo_duplicados, index=False, encoding='utf-8-sig')
        print(
            f"🔗 Relatório de clientes duplicados (> {self.LIMIAR_DUPLICIDADE * 100:.0f}% "
            f"de similaridade) salvo em '{self.arquivo_duplicados}' "
            f"({len(relatorio)} pares)."
        )
        self._salvar_duplicados_json(relatorio)
        return relatorio

    # ------------------------------------------------------------------------------
    # JSON DE DUPLICADOS: um cliente principal + suas cópias
    # ------------------------------------------------------------------------------
    @staticmethod
    def _classificacao_ocorrencia(id_prefeitura, id_secretaria):
        """Classifica a ocorrência do legado pelo nível que ela representa."""
        if id_secretaria is not None and not pd.isna(id_secretaria):
            return 'cliente'
        if id_prefeitura is None or pd.isna(id_prefeitura):
            return 'prefeitura'
        try:
            # IDs negativos são raízes sintéticas criadas para clientes sem
            # hierarquia: no refatorado eles viram customer raiz (prefeitura).
            if int(id_prefeitura) < 0:
                return 'prefeitura'
        except (TypeError, ValueError):
            pass
        return 'secretaria'

    @staticmethod
    def _ids_ja_tratados_por_regras():
        """Clientes já resolvidos por DE/PARA e exceções da migração."""
        return (
            set(DEPARA_EXCECOES.keys())
            | set(DEPARA_EXCECOES.values())
            | set(MAPA_SAO_LUIS.keys())
            | set(HOSPITAIS_PB_CONFIG.keys())
        )

    @staticmethod
    def _valor_json(valor, inteiro=False):
        if valor is None or pd.isna(valor):
            return None
        if inteiro:
            try:
                return int(valor)
            except (TypeError, ValueError):
                return None
        return valor

    def _ocorrencia_do_relatorio(self, linha, lado):
        id_prefeitura = getattr(linha, f'id_prefeitura_{lado}')
        id_secretaria = getattr(linha, f'id_secretaria_{lado}')
        return {
            'id_legado': int(getattr(linha, f'id_legado_{lado}')),
            'cliente': self._valor_json(getattr(linha, f'cliente_legado_{lado}')),
            'classificacao': self._classificacao_ocorrencia(
                id_prefeitura, id_secretaria
            ),
            'customer_id': self._valor_json(
                getattr(linha, f'customer_id_{lado}'), inteiro=True
            ),
            'address_id': self._valor_json(
                getattr(linha, f'address_id_{lado}'), inteiro=True
            ),
            'endereco_refatorado': self._valor_json(
                getattr(linha, f'endereco_refatorado_{lado}')
            ),
            'prefeitura': self._valor_json(getattr(linha, f'prefeitura_{lado}')),
            'secretaria': self._valor_json(getattr(linha, f'secretaria_{lado}')),
        }

    def _agrupar_duplicados(self, relatorio):
        """Agrupa pares em componentes conexos (cliente principal + cópias)."""
        if relatorio.empty:
            return [], 0

        ids_ignorados = self._ids_ja_tratados_por_regras()
        linhas = [
            linha for linha in relatorio.itertuples(index=False)
            if int(linha.id_legado_a) not in ids_ignorados
            and int(linha.id_legado_b) not in ids_ignorados
        ]
        ignorados = len(relatorio) - len(linhas)
        if not linhas:
            return [], ignorados

        pai = {}

        def encontrar(item):
            while pai[item] != item:
                pai[item] = pai[pai[item]]
                item = pai[item]
            return item

        def unir(item_a, item_b):
            raiz_a, raiz_b = encontrar(item_a), encontrar(item_b)
            if raiz_a != raiz_b:
                pai[raiz_b] = raiz_a

        ocorrencias = {}
        for linha in linhas:
            for lado in ('a', 'b'):
                ocorrencia = self._ocorrencia_do_relatorio(linha, lado)
                ocorrencias[ocorrencia['id_legado']] = ocorrencia
        for item in ocorrencias:
            pai.setdefault(item, item)
        for linha in linhas:
            unir(int(linha.id_legado_a), int(linha.id_legado_b))

        grupos = {}
        for item in ocorrencias:
            grupos.setdefault(encontrar(item), []).append(item)

        similares_por_grupo = {}
        for linha in linhas:
            raiz = encontrar(int(linha.id_legado_a))
            similares_por_grupo.setdefault(raiz, []).append(
                float(linha.similaridade_pct)
            )

        resultado = []
        for raiz, ids in grupos.items():
            ids = sorted(ids)
            principal_id = ids[0]  # critério estável de principal
            ocorrencias_grupo = [ocorrencias[i] for i in ids]
            hierarquias = {
                (o['prefeitura'], o['secretaria']) for o in ocorrencias_grupo
            }
            similaridades = similares_por_grupo.get(raiz, [])
            resultado.append({
                'similaridade_minima_pct': min(similaridades),
                'similaridade_maxima_pct': max(similaridades),
                'mesma_hierarquia': len(hierarquias) == 1,
                'total_ocorrencias': len(ids),
                'principal': ocorrencias[principal_id],
                'copias': [
                    ocorrencias[i] for i in ids if i != principal_id
                ],
            })

        resultado.sort(
            key=lambda grupo: (
                -grupo['similaridade_maxima_pct'],
                grupo['principal']['id_legado'],
            )
        )
        return resultado, ignorados

    def _salvar_duplicados_json(self, relatorio):
        import json
        import os

        grupos, ignorados = self._agrupar_duplicados(relatorio)
        conteudo = {
            'limiar_similaridade_pct': self.LIMIAR_DUPLICIDADE * 100,
            'criterio': (
                'similaridade estrita de nome normalizado; '
                'principal = menor id_legado do grupo'
            ),
            'resumo': {
                'grupos': len(grupos),
                'ocorrencias': sum(g['total_ocorrencias'] for g in grupos),
                'pares_ignorados_por_regras': ignorados,
            },
            'grupos': grupos,
        }
        os.makedirs(os.path.dirname(self.arquivo_duplicados_json) or '.', exist_ok=True)
        with open(self.arquivo_duplicados_json, 'w', encoding='utf-8') as arquivo:
            json.dump(conteudo, arquivo, ensure_ascii=False, indent=2)
        print(
            f"🗂️ JSON de duplicados salvo em '{self.arquivo_duplicados_json}' "
            f"({len(grupos)} grupos)."
        )

    # ------------------------------------------------------------------------------
    # MERGE DAS CÓPIAS (mesmo customer e mesmo nível, sem reservas)
    # ------------------------------------------------------------------------------
    COLUNAS_DEPARA_DUPLICADOS = [
        'id_legado_copia', 'id_legado_principal', 'cliente_copia',
        'cliente_principal', 'classificacao', 'customer_id',
        'address_id_principal', 'similaridade_pct',
    ]

    @staticmethod
    def _parece_reserva(cliente, id_legado):
        if id_legado in FALSOS_RESERVAS:
            return False
        return bool(re.search(r'\b(?:RESERVA|RESERVADO)\b', str(cliente or ''), re.IGNORECASE))

    def _pares_mesclaveis(self, relatorio):
        """Pares que podem ser fundidos: mesmo customer e mesma classificação.

        O filtro é por par, não por grupo: um componente conexo pode ligar
        clientes de customers diferentes por similaridade de nome, mas as
        duplicatas reais dentro do mesmo customer continuam mescláveis.
        """
        if relatorio.empty:
            return relatorio

        manter = []
        for linha in relatorio.itertuples(index=False):
            customer_a = self._valor_json(linha.customer_id_a, inteiro=True)
            customer_b = self._valor_json(linha.customer_id_b, inteiro=True)
            if customer_a is None or customer_b is None or customer_a != customer_b:
                manter.append(False)
                continue
            classificacao_a = self._classificacao_ocorrencia(
                linha.id_prefeitura_a, linha.id_secretaria_a
            )
            classificacao_b = self._classificacao_ocorrencia(
                linha.id_prefeitura_b, linha.id_secretaria_b
            )
            manter.append(classificacao_a == classificacao_b)
        return relatorio[manter].reset_index(drop=True)

    def _aplicar_merge_duplicados(self, relatorio, df_enderecos_finais):
        """Mescla cópias do mesmo customer/nível e remove seus addresses.

        Só entram pares no mesmo ``customer_id`` e mesma classificação.
        Grupos com reservas ficam de fora para não quebrar o roteamento.
        """
        import os

        grupos, _ = self._agrupar_duplicados(self._pares_mesclaveis(relatorio))

        # IDs cujo address guarda roteamento de reserva e não pode ser apagado.
        # Inclui os clientes mapeados para endereço-base e os destinos genéricos
        # de prefeitura, que recebem reserved_customer_id = próprio id na carga.
        ids_com_reserva = {
            int(id_legado) for id_legado in ids_reserva_com_endereco_proprio()
        }
        if 'reserved_customer_id' in df_enderecos_finais.columns:
            for id_cliente, reserva in zip(
                df_enderecos_finais['ID_CLIENTE'],
                df_enderecos_finais['reserved_customer_id'],
            ):
                if pd.notna(reserva):
                    try:
                        ids_com_reserva.add(int(id_cliente))
                    except (TypeError, ValueError):
                        pass

        registros = []
        enderecos_remover = []
        grupos_preservados_reserva = 0
        for grupo in grupos:
            ocorrencias = [grupo['principal'], *grupo['copias']]
            if len({o['customer_id'] for o in ocorrencias}) != 1:
                continue
            if len({o['classificacao'] for o in ocorrencias}) != 1:
                continue
            if any(
                self._parece_reserva(o['cliente'], o['id_legado'])
                or o['id_legado'] in ids_com_reserva
                for o in ocorrencias
            ):
                grupos_preservados_reserva += 1
                continue

            principal = grupo['principal']
            if principal['address_id'] is None or principal['customer_id'] is None:
                continue

            for copia in grupo['copias']:
                if copia['address_id'] is None:
                    continue
                registros.append({
                    'id_legado_copia': copia['id_legado'],
                    'id_legado_principal': principal['id_legado'],
                    'cliente_copia': copia['cliente'],
                    'cliente_principal': principal['cliente'],
                    'classificacao': principal['classificacao'],
                    'customer_id': principal['customer_id'],
                    'address_id_principal': principal['address_id'],
                    'similaridade_pct': grupo['similaridade_maxima_pct'],
                })
                enderecos_remover.append(
                    (copia['address_id'], principal['customer_id'])
                )

        depara = pd.DataFrame(
            registros, columns=self.COLUNAS_DEPARA_DUPLICADOS
        )

        if enderecos_remover:
            with self.engine_new.begin() as conn:
                for address_id, customer_id in enderecos_remover:
                    conn.execute(
                        text("""
                            DELETE FROM addresses
                            WHERE id = :address_id
                              AND addressable_id = :customer_id
                        """),
                        {'address_id': address_id, 'customer_id': customer_id},
                    )

        os.makedirs(
            os.path.dirname(self.arquivo_depara_duplicados) or '.', exist_ok=True
        )
        depara.to_csv(
            self.arquivo_depara_duplicados, index=False, encoding='utf-8-sig'
        )

        print(
            f"🧬 Merge de duplicados: {len(depara)} cópia(s) mesclada(s); "
            f"{grupos_preservados_reserva} grupo(s) preservado(s) por reserva. "
            f"DE/PARA em '{self.arquivo_depara_duplicados}'."
        )
        return depara



    # ==============================================================================
    # 1. EXTRAÇÃO
    # ==============================================================================

    def _extrair(self):
        print("📖 Extraindo dados do legado...")
        # 👇 QUERY ATUALIZADA: Incluindo updated_at e created_at
        query = """
                SELECT
                    ald.id as ID_PREFEITURA,
                    ald.nome as PREFEITURA,
                    als.id as ID_SECRETARIA,
                    als.nome as SECRETARIA,
                    ac.id as ID_CLIENTE,
                    ac.nome_razao_social as CLIENTE,
                    ac.cpf_cnpj as CNPJ,
                    ac.orgao_id as ORGANIZACAO,
                    ac.cep as CEP,
                    ac.endereco as ENDERECO,
                    ac.estado as ESTADO,
                    ac.cidade as CIDADE,
                    ac.telefone as PHONE,
                    ac.updated_at as updated_at,
                    ac.created_at as created_at
                FROM aluguel_clientes ac
                LEFT JOIN aluguel_setor als ON als.id = ac.setor_id
                LEFT JOIN aluguel_departamento ald ON ald.id = als.departamento_id
                WHERE ac.deleted_at IS NULL
                AND als.deleted_at IS NULL
                AND ald.deleted_at IS NULL
                ORDER BY
                    CASE
                        WHEN ald.id IS NOT NULL AND als.id IS NOT NULL THEN 1
                        WHEN ac.id IS NOT NULL THEN 2
                        ELSE 3
                    END,
                    ald.nome,
                    als.nome,
                    ac.nome_razao_social;
                """
        with self.engine_legado.connect() as conn:
            return pd.read_sql(text(query), conn)


     # ==============================================================================
    # 2. APLICANDO REGRAS DE EXCEÇÃO E LIMPEZA DE DADOS
    # ==============================================================================
    def _achatar_secretarias_iguais_prefeitura(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        # Cria uma máscara para identificar onde Prefeitura e Secretaria são iguais
        mask_same_name = (
            df_modificado['PREFEITURA'].notna() & 
            df_modificado['SECRETARIA'].notna() & 
            (df_modificado['PREFEITURA'].astype(str).str.strip().str.upper() == 
             df_modificado['SECRETARIA'].astype(str).str.strip().str.upper())
        )
        
        if mask_same_name.any():
            # Achata a hierarquia definindo a Secretaria como nula
            df_modificado.loc[mask_same_name, 'ID_SECRETARIA'] = None
            df_modificado.loc[mask_same_name, 'SECRETARIA'] = None
            
        return df_modificado


    def _anexar_clientes_raiz_a_prefeitura(
        self, df: pd.DataFrame
    ) -> pd.DataFrame:
        """Anexa cadastros-raiz repetidos à prefeitura que os absorve.

        Alguns clientes do legado não têm setor/departamento, mas são o mesmo
        órgão já existente em ``MESCLAGENS_PREFEITURA`` (ex.: cliente 903 =
        PREFEITURA MUNICIPAL DE AQUIRAZ). Sem isso a promoção de hierarquia os
        transforma em uma raiz sintética e o cadastro fica duplicado. Aqui a
        linha ganha o departamento alvo e passa a ser tratada como destino
        (endereço) da raiz, preservando ``ID_CLIENTE`` como
        ``legacy_customer_id`` para movimentos e contratos.
        """
        df_modificado = df.copy()
        if not CLIENTES_RAIZ_EM_PREFEITURA:
            return df_modificado

        for id_legado, id_prefeitura in CLIENTES_RAIZ_EM_PREFEITURA.items():
            mask = (
                (df_modificado['id_clean'] == int(id_legado))
                & df_modificado['ID_PREFEITURA'].isna()
                & df_modificado['ID_SECRETARIA'].isna()
            )
            if not mask.any():
                continue
            df_modificado.loc[mask, 'ID_PREFEITURA'] = int(id_prefeitura)
            df_modificado.loc[mask, 'PREFEITURA'] = df_modificado.loc[
                mask, 'CLIENTE'
            ]
            print(
                f"   🏛️  cliente(s) raiz {id_legado} anexado(s) à prefeitura "
                f"{id_prefeitura}."
            )

        return df_modificado

    def _unificar_prefeituras(self, df: pd.DataFrame) -> pd.DataFrame:
        """Mescla prefeituras repetidas no legado em um único cliente.

        Cada entrada de ``MESCLAGENS_PREFEITURA`` lista departamentos que são
        o mesmo órgão com cadastro repetido. Todas as secretarias e clientes
        de destino vinculados a eles passam a pertencer ao cliente alvo. Por
        padrão a hierarquia de secretaria é achatada: o destino é anexado
        direto à prefeitura e o nome original do cliente é preservado no
        alias do endereço. Com ``preservar_hierarquia`` os setores de origem
        continuam existindo, apenas reparentados sob a prefeitura alvo (usado
        quando os contratos apontam para os setores dos dois lados).
        """
        df_modificado = df.copy()
        ids_prefeitura = pd.to_numeric(
            df_modificado['ID_PREFEITURA'], errors='coerce'
        )

        for rotulo, config in MESCLAGENS_PREFEITURA.items():
            mask = ids_prefeitura.isin(config['ids'])
            if not mask.any():
                continue
            df_modificado.loc[mask, 'ID_PREFEITURA'] = config['id_alvo']
            df_modificado.loc[mask, 'PREFEITURA'] = config['nome_alvo']
            if not config.get('preservar_hierarquia'):
                df_modificado.loc[mask, 'ID_SECRETARIA'] = None
                df_modificado.loc[mask, 'SECRETARIA'] = None
            organizacao_alvo = config.get('organizacao_alvo')
            if organizacao_alvo is not None:
                df_modificado.loc[mask, 'ORGANIZACAO'] = organizacao_alvo
            print(
                f"   🏛️  {int(mask.sum())} cliente(s) das prefeituras de "
                f"{rotulo} unificados em '{config['nome_alvo']}'."
            )

        return df_modificado

    def _unificar_clientes_repetidos(self, df: pd.DataFrame) -> pd.DataFrame:
        """Consolida clientes repetidos que não são prefeitura/secretaria.

        Cada grupo de ``MESCLAGENS_CLIENTE`` mantém apenas o cadastro
        principal (``id_alvo``). As cópias deixam de gerar customer e
        endereço; movimentos, contratos e delta as resolvem pelo de/para
        legado de ``utils.depara_clientes``. Reservas continuam fora daqui:
        elas seguem a regra de ``reserved_customer_id``.
        """
        df_modificado = df.copy()
        for rotulo, config in MESCLAGENS_CLIENTE.items():
            id_alvo = int(config['id_alvo'])
            ids_grupo = {int(id_legado) for id_legado in config['ids']}
            mask_alvo = df_modificado['id_clean'] == id_alvo
            if not mask_alvo.any():
                print(
                    f"   ⚠️ Mesclagem de clientes {rotulo} ignorada: cadastro "
                    f"principal {id_alvo} não está na extração."
                )
                continue

            # Um cadastro que é pai de outro cliente tem hierarquia própria:
            # removê-lo apagaria a raiz/secretaria dos filhos.
            ids_pais = set()
            for coluna in ('ID_PREFEITURA', 'ID_SECRETARIA'):
                valores = pd.to_numeric(
                    df_modificado[coluna], errors='coerce'
                ).dropna()
                ids_pais.update(valores.astype(int))

            copias = ids_grupo - {id_alvo}
            if copias & ids_pais:
                print(
                    f"   ⚠️ Mesclagem de clientes {rotulo}: mantidos "
                    f"{sorted(copias & ids_pais)} por serem pai de outro cliente."
                )
            copias -= ids_pais
            mask_copias = df_modificado['id_clean'].isin(copias)
            if not mask_copias.any():
                continue

            nome_alvo = config.get('nome_alvo')
            if nome_alvo:
                df_modificado.loc[mask_alvo, 'CLIENTE'] = nome_alvo

            total_copias = int(mask_copias.sum())
            df_modificado = df_modificado[~mask_copias].copy()
            print(
                f"   🔗 {total_copias} cadastro(s) de {rotulo} mesclado(s) ao "
                f"cliente {id_alvo}."
            )

        return df_modificado

    def _unificar_receita_federal(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()

        id_pref_serie = pd.to_numeric(df_modificado['ID_PREFEITURA'], errors='coerce')
        id_sec_serie = pd.to_numeric(df_modificado['ID_SECRETARIA'], errors='coerce')

        # 1️⃣ Unir Prefeitura 420 -> 297
        mask_pref_420 = id_pref_serie == 420
        if mask_pref_420.any():
            # Busca o nome original da Prefeitura 297 para padronizar
            nomes_pref_297 = df_modificado.loc[id_pref_serie == 297, 'PREFEITURA']
            nome_pref_padrao = nomes_pref_297.iloc[0] if not nomes_pref_297.empty else "SUPERINTENDENCIA REG DA RECEITA FEDERAL DO BRASIL SANTA CATARINA"
            
            df_modificado.loc[mask_pref_420, 'ID_PREFEITURA'] = 297
            df_modificado.loc[mask_pref_420, 'PREFEITURA'] = nome_pref_padrao

        # 2️⃣ Mesclar Secretarias 1650 e 1653 (Rio Grande do Sul)
        mask_sec_rs = id_sec_serie.isin([1650, 1653])
        if mask_sec_rs.any():
            df_modificado.loc[mask_sec_rs, 'ID_SECRETARIA'] = 1650
            df_modificado.loc[mask_sec_rs, 'SECRETARIA'] = "RECEITA FEDERAL DE RIO GRANDE DO SUL"

        # 3️⃣ Ajustar nome das demais secretarias que contém "SUP_REG_RECEITA"
        mask_sup_reg = df_modificado['SECRETARIA'].astype(str).str.contains('SUP_REG_RECEITA', na=False)
        if mask_sup_reg.any():
            df_modificado.loc[mask_sup_reg, 'SECRETARIA'] = df_modificado.loc[mask_sup_reg, 'SECRETARIA'].str.replace('SUP_REG_RECEITA', 'RECEITA FEDERAL', regex=False).str.strip()

        return df_modificado

    def _unificar_sao_luis(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        mask_sl = df_modificado['id_clean'].isin(MAPA_SAO_LUIS.keys())
        
        for idx, row in df_modificado[mask_sl].iterrows():
            id_cliente = int(row['id_clean'])
            id_sec_alvo, nome_sec_alvo = MAPA_SAO_LUIS[id_cliente]
            
            # Remove o sufixo "2026" do nome do cliente final (Neto) e limpa espaços
            nome_cliente = str(row['CLIENTE'])
            nome_cliente_limpo = re.sub(r'\b2026\b', '', nome_cliente).strip()
            
            # Aplica a unificação na linha
            df_modificado.at[idx, 'ID_PREFEITURA'] = SAO_LUIS_CONFIG['ID_PREF_ALVO']
            df_modificado.at[idx, 'PREFEITURA'] = SAO_LUIS_CONFIG['NOME_PREF_ALVO']
            df_modificado.at[idx, 'ID_SECRETARIA'] = id_sec_alvo
            df_modificado.at[idx, 'SECRETARIA'] = nome_sec_alvo
            df_modificado.at[idx, 'CLIENTE'] = nome_cliente_limpo

        # Unifica tambem as linhas de prefeitura/secretaria que nao estao
        # vinculadas aos IDs de cliente mapeados acima (variacoes de nome,
        # acento e sufixo 2026).
        def normalizar_texto(valor):
            texto = unicodedata.normalize('NFKD', str(valor or ''))
            return texto.encode('ascii', 'ignore').decode('utf-8').upper()

        mask_pref_sl = df_modificado['PREFEITURA'].apply(normalizar_texto).str.contains(
            'PREFEITURA MUNICIPAL DE SAO LUIS', regex=False
        )
        if mask_pref_sl.any():
            df_modificado.loc[mask_pref_sl, 'ID_PREFEITURA'] = SAO_LUIS_CONFIG['ID_PREF_ALVO']
            df_modificado.loc[mask_pref_sl, 'PREFEITURA'] = SAO_LUIS_CONFIG['NOME_PREF_ALVO']

        mask_sec_semusc = df_modificado['SECRETARIA'].apply(normalizar_texto).str.contains(
            'SEGURANCA COM CIDADANIA|SEMUSC', regex=True
        )
        if mask_sec_semusc.any():
            df_modificado.loc[mask_sec_semusc, 'ID_SECRETARIA'] = 1197
            df_modificado.loc[mask_sec_semusc, 'SECRETARIA'] = (
                "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"
            )
        return df_modificado
        
    def _regionalizar_pcpb(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        mask_pcpb = (df_modificado['ID_PREFEITURA'].astype(str).str.strip() == '320') | \
                    (df_modificado['CLIENTE'].str.contains('PCPB', na=False))
        
        for idx, row in df_modificado[mask_pcpb].iterrows():
            nome_cliente = str(row['CLIENTE']).upper()
            nome_cliente_limpo = unicodedata.normalize('NFD', nome_cliente).encode('ascii', 'ignore').decode('utf-8')
            
            regiao_encontrada = "SEDE / OUTROS"
            for termo_busca, regiao_oficial in MAPA_REGIOS_PCPB.items():
                if termo_busca in nome_cliente_limpo:
                    regiao_encontrada = regiao_oficial
                    break
                    
            id_sintetico = MAPA_IDS_SINTETICOS_PCPB[regiao_encontrada]
            
            df_modificado.at[idx, 'ID_SECRETARIA'] = id_sintetico
            df_modificado.at[idx, 'SECRETARIA'] = f"POLÍCIA CIVIL - {regiao_encontrada}"
        return df_modificado
        
    def _unificar_ministerio_relacoes(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()

        pref_alvo, pref_origem = MAPA_MINISTERIO_RELACOES["PREFEITURA"]
        sec_alvo, sec_origem = MAPA_MINISTERIO_RELACOES["SECRETARIA"]

        id_pref_serie = pd.to_numeric(df_modificado['ID_PREFEITURA'], errors='coerce')
        id_sec_serie = pd.to_numeric(df_modificado['ID_SECRETARIA'], errors='coerce')

        mask_ministerio = id_pref_serie.isin([pref_origem, pref_alvo]) | id_sec_serie.isin([sec_origem, sec_alvo])
        
        if mask_ministerio.any():
            nomes_pref_alvo = df_modificado.loc[id_pref_serie == pref_alvo, 'PREFEITURA']
            nome_pref_padrao = nomes_pref_alvo.iloc[0] if not nomes_pref_alvo.empty else "MINISTÉRIO DAS RELAÇÕES EXTERIORES"
            
            # ======================================================================
            # 1️⃣ UNIFICA A PREFEITURA (Redireciona tudo para o alvo)
            # ======================================================================
            df_modificado.loc[mask_ministerio, 'ID_PREFEITURA'] = pref_alvo
            df_modificado.loc[mask_ministerio, 'PREFEITURA'] = nome_pref_padrao

            # ======================================================================
            # 2️⃣ ACHATAMENTO DA HIERARQUIA (Elimina a Secretaria)
            # ======================================================================
            df_modificado.loc[mask_ministerio, 'ID_SECRETARIA'] = None
            df_modificado.loc[mask_ministerio, 'SECRETARIA'] = None
        
        return df_modificado
        
    def _unificar_ministerio_transportes(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()

        pref_alvo, pref_origem = MAPA_MINISTERIO_TRANSPORTES["PREFEITURA"]
        id_pref_serie = pd.to_numeric(df_modificado['ID_PREFEITURA'], errors='coerce')

        mask_transportes = id_pref_serie.isin([pref_origem, pref_alvo])
        
        if mask_transportes.any():
            # 1️⃣ UNIFICA A PREFEITURA (Redireciona RJ para DF)
            df_modificado.loc[mask_transportes, 'ID_PREFEITURA'] = pref_alvo
            df_modificado.loc[mask_transportes, 'PREFEITURA'] = "MINISTÉRIO DOS TRANSPORTES"

            # 2️⃣ ACHATAMENTO TOTAL DA HIERARQUIA 
            df_modificado.loc[mask_transportes, 'ID_SECRETARIA'] = None
            df_modificado.loc[mask_transportes, 'SECRETARIA'] = None
        
        return df_modificado
        
    def _reestruturar_detran(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()

        ids_prefeitura = pd.to_numeric(
            df_modificado["ID_PREFEITURA"],
            errors="coerce"
        )

        for id_prefeitura_legado, unidade in DETRAN_CONFIG["UNIDADES"].items():
            mask_detran = ids_prefeitura.eq(id_prefeitura_legado)

            if not mask_detran.any():
                continue

            # Cliente pai unico DETRAN.
            df_modificado.loc[
                mask_detran,
                ["ID_PREFEITURA", "PREFEITURA"]
            ] = [
                DETRAN_CONFIG["ID_PAI_SINTETICO"],
                DETRAN_CONFIG["NOME_PAI"],
            ]

            # Subcliente que recebera os addresses dos clientes atrelados no legado.
            df_modificado.loc[
                mask_detran,
                ["ID_SECRETARIA", "SECRETARIA"]
            ] = [
                unidade["id_filho_sintetico"],
                unidade["nome_filho"],
            ]

        return df_modificado
        
    def _padronizar_hospitais_pb(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        for id_legado, info in HOSPITAIS_PB_CONFIG.items():
            mask_hosp = df_modificado['id_clean'] == id_legado
            
            if mask_hosp.any():
                # 1. Promove ao status de Pai (Prefeitura Sintética)
                df_modificado.loc[mask_hosp, 'ID_PREFEITURA'] = info['id_pai']
                df_modificado.loc[mask_hosp, 'PREFEITURA'] = info['nome']
                
                # 2. Achata hierarquia (Remove secretaria antiga)
                df_modificado.loc[mask_hosp, 'ID_SECRETARIA'] = None
                df_modificado.loc[mask_hosp, 'SECRETARIA'] = None
                
                # 3. Renomeia o cliente final (Isso remove a palavra RESERVA, escapando do merge de reservas)
                df_modificado.loc[mask_hosp, 'CLIENTE'] = info['nome']
                
        return df_modificado
        
    # ==============================================================================
    # FUNÇÕES AUXILIARES DE SANETIZAÇÃO
    # ==============================================================================
    def _limpar_cnpj(self, cnpj_raw):
        c = re.sub(r'\D', '', str(cnpj_raw))
        return c if c else '00000000000000'
    
    def _transformar(self, df):
        print("🧹 Iniciando limpeza e aplicação de regras de negócio...")
        df_clean = df.copy()
        
        # Limpeza básica
        colunas_texto = ['PREFEITURA', 'SECRETARIA', 'CLIENTE', 'CEP', 'ENDERECO', 'ESTADO', 'CIDADE', 'CNPJ']
        for col in colunas_texto:
            if col in df_clean.columns:
                df_clean[col] = df_clean[col].astype(str).str.strip().replace(['nan', 'None', ''], None)
        
        # Preenche os campos grandes normalmente
        df_clean[['ENDERECO', 'ESTADO', 'CIDADE', 'PHONE']] = df_clean[['ENDERECO', 'ESTADO', 'CIDADE', 'PHONE']].fillna('NÃO INFORMADO')
        
        # O CEP ganha um tratamento VIP: Se vazio, vira 'NULO'. E cortamos estritamente nos 8 primeiros caracteres para o banco não chorar.
        df_clean['CEP'] = df_clean['CEP'].fillna('NULO').astype(str).str.slice(0, 8)
        
        df_clean['ORG_DESTINO'] = df_clean['ORGANIZACAO'].apply(descobrir_id_organizacao)
        mask_org_bloqueada = df_clean['ORG_DESTINO'].isin(ORGANIZACOES_BLOQUEADAS)
        self._registrar_clientes_descartados(
            df_clean.loc[mask_org_bloqueada], 'ORGANIZACAO_BLOQUEADA'
        )
        df_clean = df_clean[~mask_org_bloqueada]
        df_clean['id_clean'] = pd.to_numeric(df_clean['ID_CLIENTE'], errors='coerce').fillna(0).astype(int)
        # Clientes internos de estoque/base não existirão no refatorado. Seus
        # equipamentos alugados são liberados para locação na etapa de movimentos.
        mask_cliente_bloqueado = df_clean['id_clean'].isin(CLIENTES_BLOQUEADOS)
        mask_cliente_estoque = df_clean['id_clean'].isin(CLIENTES_ESTOQUE)
        self._registrar_clientes_descartados(
            df_clean.loc[mask_cliente_bloqueado], 'CLIENTE_BLOQUEADO'
        )
        self._registrar_clientes_descartados(
            df_clean.loc[mask_cliente_estoque], 'CLIENTE_INTERNO_ESTOQUE_BASE'
        )
        df_clean = df_clean[~(mask_cliente_bloqueado | mask_cliente_estoque)]

        # --- APLICA EXCEÇÕES ---
        # df_clean = self._unificar_receita_federal(df_clean)
        df_clean = self._regionalizar_pcpb(df_clean)
        df_clean = self._padronizar_hospitais_pb(df_clean)
        df_clean = self._unificar_sao_luis(df_clean)
        df_clean = self._anexar_clientes_raiz_a_prefeitura(df_clean)
        df_clean = self._unificar_prefeituras(df_clean)
        df_clean = self._unificar_ministerio_relacoes(df_clean)
        df_clean = self._unificar_ministerio_transportes(df_clean)
        df_clean = self._reestruturar_detran(df_clean)
        df_clean = self._achatar_secretarias_iguais_prefeitura(df_clean)
        df_clean = self._unificar_clientes_repetidos(df_clean)

        # Setores internos não devem virar customer (secretaria). Os clientes
        # desses setores são anexados diretamente ao departamento pai.
        if SETORES_IGNORADOS:
            mask_setor_ignorado = pd.to_numeric(
                df_clean['ID_SECRETARIA'], errors='coerce'
            ).isin(SETORES_IGNORADOS)
            if mask_setor_ignorado.any():
                df_clean.loc[mask_setor_ignorado, 'ID_SECRETARIA'] = None
                df_clean.loc[mask_setor_ignorado, 'SECRETARIA'] = None
                print(
                    f"   🧩 {int(mask_setor_ignorado.sum())} cliente(s) de setor(es) "
                    f"interno(s) foram anexados diretamente ao departamento pai."
                )

        # --- MERGE DE RESERVAS ---
        mask_reserva = df_clean['CLIENTE'].str.contains(r'\b(?:RESERVA|RESERVADO)\b', case=False, na=False) & ~df_clean['id_clean'].isin(FALSOS_RESERVAS)
        df_normais = df_clean[~mask_reserva].copy()
        df_reservas = df_clean[mask_reserva].copy()

        df_normais['nome_ajustado'] = df_normais['CLIENTE'].apply(normalizar_para_match)
        df_reservas['nome_ajustado'] = df_reservas['CLIENTE'].apply(normalizar_para_match)
        df_normais['escopo_pai'] = df_normais['ID_SECRETARIA'].fillna(df_normais['ID_PREFEITURA'])
        df_reservas['escopo_pai'] = df_reservas['ID_SECRETARIA'].fillna(df_reservas['ID_PREFEITURA'])

        df_reservas_lookup = df_reservas[['escopo_pai',
                                          'nome_ajustado',
                                          'id_clean',
                                          'CLIENTE']].rename(columns={'id_clean': 'reserved_customer_id', 'CLIENTE': 'nome_reserva_original'}).drop_duplicates(subset=['escopo_pai', 'nome_ajustado'])
        df_merged = pd.merge(df_normais, df_reservas_lookup, on=['escopo_pai', 'nome_ajustado'], how='left')

        for id_reserva, id_titular in DEPARA_EXCECOES.items():
            df_merged.loc[df_merged['id_clean'] == id_titular, 'reserved_customer_id'] = id_reserva

        # Reservas sem titular pareado automaticamente: usa o DE/PARA
        # explícito pelo nome do cliente, preservando o ID da reserva.
        for id_reserva, nome_titular in DEPARA_RESERVAS_BASE.items():
            nome_normalizado = normalizar_para_match(nome_titular)
            mask_titular = df_merged['nome_ajustado'] == nome_normalizado
            df_merged.loc[mask_titular, 'reserved_customer_id'] = id_reserva

        # Clientes normais sem hierarquia precisam de uma raiz para a carga
        # conseguir criar seu customer/address. Fazemos isso depois do pareamento
        # para que a alteração de escopo não interfira no match das reservas.
        mask_reserva_hierarquia = df_clean['CLIENTE'].str.contains(
            r'\b(?:RESERVA|RESERVADO)\b', case=False, na=False
        ) & ~df_clean['id_clean'].isin(FALSOS_RESERVAS)
        mask_sem_pai = (
            df_clean['ID_PREFEITURA'].isna()
            & df_clean['ID_SECRETARIA'].isna()
            & df_clean['ID_CLIENTE'].notna()
            & ~mask_reserva_hierarquia
        )
        if mask_sem_pai.any():
            ids_sem_pai = set(df_clean.loc[mask_sem_pai, 'id_clean'])
            for frame in (df_clean, df_merged):
                mask_promover = (
                    frame['id_clean'].isin(ids_sem_pai)
                    & frame['ID_PREFEITURA'].isna()
                    & frame['ID_SECRETARIA'].isna()
                )
                # IDs negativos são chaves sintéticas internas: evitam colisão
                # entre, por exemplo, o departamento legado 439 e o cliente 439.
                ids_raiz_sintetica = pd.to_numeric(
                    frame.loc[mask_promover, 'ID_CLIENTE'], errors='coerce'
                ).abs()
                frame.loc[mask_promover, 'ID_PREFEITURA'] = -ids_raiz_sintetica
                frame.loc[mask_promover, 'PREFEITURA'] = frame.loc[
                    mask_promover, 'CLIENTE'
                ]
            print(
                f"   🌱 {int(mask_sem_pai.sum())} cliente(s) sem hierarquia "
                "promovido(s) a raiz para migração."
            )

        reservas_pareadas = df_merged['reserved_customer_id'].dropna().unique()
        df_orfas = df_reservas[~df_reservas['id_clean'].isin(reservas_pareadas)].copy()
        df_orfas['reserved_customer_id'] = None
        # Reservas sem titular pareado continuam precisando de um endereço de
        # reserva para que o módulo de movimentos consiga roteá-las.
        mask_reserva_base = df_orfas['id_clean'].isin(
            ids_reserva_com_endereco_proprio()
        )
        df_orfas.loc[mask_reserva_base, 'reserved_customer_id'] = df_orfas.loc[
            mask_reserva_base, 'id_clean'
        ]

        print(
            f"\n🔎 Reservas sem match por nome/escopo ou DEPARA manual: "
            f"{len(df_orfas)}"
        )
        for _, row in df_orfas.iterrows():
            reserva_id = int(row['id_clean'])
            config_reserva = RESERVAS_DESTINO_PREFEITURA.get(reserva_id)
            base_reserva = RESERVAS_BASE_AVULSA.get(reserva_id)
            if config_reserva:
                resultado = (
                    "será anexada ao destino genérico "
                    f"'{config_reserva['alias']}'"
                )
            elif base_reserva:
                resultado = f"será direcionada para o endereço-base '{base_reserva}'"
            else:
                resultado = "sem endereço-base configurado; não foi possível atrelar"
            print(
                f"   ⚠️ ID reserva [{reserva_id}] '{row['CLIENTE']}' — "
                f"{resultado}."
            )
        print("=" * 75 + "\n")

        return pd.concat([df_merged, df_orfas], ignore_index=True), df_clean

    # ==============================================================================
    # PROMOÇÃO HIERÁRQUICA PARA PREEITURAS NULAS
    # ==============================================================================
    def _promover_hierarquia(self, df: pd.DataFrame) -> pd.DataFrame:
        """Sobe a secretaria ou o cliente de nível caso falte um pai estrutural"""
        df_mod = df.copy()
        print("   🌳 Remodelando hierarquia (Promoção de Secretarias e Clientes para Pais)...")

        # Geramos IDs formatados em string (Ex: P_10, S_10, C_10) para evitar que o Python confunda 
        # a Prefeitura 10 com o Cliente 10 na hora de montar o dicionário
        id_pref_orig = df_mod['ID_PREFEITURA']
        id_sec_orig = df_mod['ID_SECRETARIA']
        id_cli_orig = df_mod['ID_CLIENTE']

        df_mod['ID_PREF_STR'] = id_pref_orig.apply(lambda x: f"P_{int(x)}" if pd.notna(x) and str(x).strip() != '' else None)
        df_mod['ID_SEC_STR'] = id_sec_orig.apply(lambda x: f"S_{int(x)}" if pd.notna(x) and str(x).strip() != '' else None)
        df_mod['ID_CLI_STR'] = id_cli_orig.apply(lambda x: f"C_{int(x)}" if pd.notna(x) and str(x).strip() != '' else None)

        # 🎯 REGRA 1: Se a PREFEITURA é nula, a SECRETARIA é promovida a Prefeitura!
        mask_sem_pref = df_mod['ID_PREF_STR'].isna() & df_mod['ID_SEC_STR'].notna()
        df_mod.loc[mask_sem_pref, 'ID_PREF_STR'] = df_mod.loc[mask_sem_pref, 'ID_SEC_STR']
        df_mod.loc[mask_sem_pref, 'PREFEITURA'] = df_mod.loc[mask_sem_pref, 'SECRETARIA']
        df_mod.loc[mask_sem_pref, 'ID_SEC_STR'] = None
        df_mod.loc[mask_sem_pref, 'SECRETARIA'] = None

        # 🎯 REGRA 2: Se não sobrou nem PREFEITURA nem SECRETARIA, o CLIENTE assume a liderança!
        mask_tudo_nulo = df_mod['ID_PREF_STR'].isna() & df_mod['ID_SEC_STR'].isna()
        df_mod.loc[mask_tudo_nulo, 'ID_PREF_STR'] = df_mod.loc[mask_tudo_nulo, 'ID_CLI_STR']
        df_mod.loc[mask_tudo_nulo, 'PREFEITURA'] = df_mod.loc[mask_tudo_nulo, 'CLIENTE']

        # Repassa os IDs String blindados para as colunas oficiais
        df_mod['ID_PREFEITURA'] = df_mod['ID_PREF_STR']
        df_mod['ID_SECRETARIA'] = df_mod['ID_SEC_STR']

        df_mod = df_mod.drop(columns=['ID_PREF_STR', 'ID_SEC_STR', 'ID_CLI_STR'])
        return df_mod
        
    # ==============================================================================
    # 3. CARGA (Bulk Inserts)
    # ==============================================================================
    def _carregar(self, df_enderecos_finais, df_bruto):
        print("🔄 Estruturando Dicionários de Pais e Filhos...")
        prefeituras, secretarias, destinos = {}, {}, {}
        mapa_refatorado = {}

        for _, row in df_bruto.iterrows():
            if pd.notna(row['ID_PREFEITURA']):
                prefeituras[int(row['ID_PREFEITURA'])] = {"nome": row['PREFEITURA'], "row": row}

            if pd.notna(row['ID_SECRETARIA']):
                secretarias[int(row['ID_SECRETARIA'])] = {"nome": row['SECRETARIA'], "id_pai": int(row['ID_PREFEITURA']) if pd.notna(row['ID_PREFEITURA']) else None, "row": row}

        for _, row in df_enderecos_finais.iterrows():
            if pd.notna(row['ID_CLIENTE']):
                destinos[int(row['ID_CLIENTE'])] = {
                    "nome": row['CLIENTE'], "id_sec": int(row['ID_SECRETARIA']) if pd.notna(row['ID_SECRETARIA']) else None,
                    "id_pref": row['ID_PREFEITURA'], "legacy_id": int(row['ID_CLIENTE']),
                    "res_id": int(row['reserved_customer_id']) if pd.notna(row['reserved_customer_id']) else None, "row": row
                }

        # ==============================================================================
        # 🏢 CRIAÇÃO DE ENDEREÇOS DE BOX/ESTOQUES PARA CADA ORGANIZAÇÃO
        # ==============================================================================
        enderecos_batch = ENDERECOS_BASES.copy()
        
        enderecos_organizacoes = [
            endereco for endereco in enderecos_batch
            if endereco.get("type") == "organization"
        ]
        
        # 2. Criamos uma lista temporária para não quebrar a iteração
        novos_boxes = []

        for endereco_organizacao in enderecos_organizacoes:
            for box in BASES_AVULSOS.values():
                novos_boxes.append({
                    "type": "organization",
                    "id": endereco_organizacao["id"],
                    "alias": box["alias"],
                    "zip": box["zip"],
                    "street": box["street"],
                    "num": box["number"],
                    "city": box["city"],
                    "state": box["state"],
                    "leg_id": None,
                    "res_id": None,
                    "created_at": self.now,
                    "updated_at": self.now
                })
        
        enderecos_batch.extend(novos_boxes)

        print("🚀 Gravando dados no banco de dados...")
        with self.engine_new.begin() as conn:
            query_cust = text("""
                INSERT INTO customers (alias, name, cpf_cnpj, organization_id, parent_id, created_at, updated_at) 
                VALUES (:n, :n, :d, :o, :p, :created_at, :updated_at)
            """)
            
            # Pais (Prefeituras)
            dict_traducao_pais = {}
            for id_legado, info in tqdm(prefeituras.items(), desc="Inserindo Prefeituras"):
                c_at = info["row"]['created_at'] if pd.notna(info["row"].get('created_at')) else self.now
                u_at = info["row"]['updated_at'] if pd.notna(info["row"].get('updated_at')) else self.now
                
                res = conn.execute(query_cust, {
                    "n": info["nome"], "d": self._limpar_cnpj(info["row"].get('CNPJ')), 
                    "o": descobrir_id_organizacao(info["row"]['ORGANIZACAO']), "p": None, 
                    "created_at": c_at, "updated_at": u_at
                })
                novo_id = res.lastrowid
                dict_traducao_pais[id_legado] = novo_id
                self.stats["pais"] += 1

            # Filhos (Secretarias)
            dict_traducao_filhos = {}
            for id_legado, info in tqdm(secretarias.items(), desc="Inserindo Secretarias"):
                id_pai = dict_traducao_pais.get(info["id_pai"])
                if info["id_pai"] and not id_pai:
                    self._registrar_clientes_descartados(
                        pd.DataFrame([info['row']]), 'SECRETARIA_SEM_PREFEITURA_MIGRADA'
                    )
                    continue
                
                c_at = info["row"]['created_at'] if pd.notna(info["row"].get('created_at')) else self.now
                u_at = info["row"]['updated_at'] if pd.notna(info["row"].get('updated_at')) else self.now

                res = conn.execute(query_cust, {
                    "n": info["nome"], "d": self._limpar_cnpj(info["row"].get('CNPJ')), 
                    "o": descobrir_id_organizacao(info["row"]['ORGANIZACAO']), "p": id_pai, 
                    "created_at": c_at, "updated_at": u_at
                })
                novo_id = res.lastrowid
                dict_traducao_filhos[id_legado] = novo_id
                self.stats["filhos"] += 1

            # Netos (Endereços) - Preparação
            for id_legado, info in tqdm(destinos.items(), desc="Preparando Endereços Finais"):
                # Reservas com destino genérico de prefeitura ignoram a
                # hierarquia do legado (o cliente de reserva não tem setor) e
                # são anexadas ao departamento configurado.
                config_reserva = config_destino_reserva(id_legado)
                id_pref, id_sec = hierarquia_destino(
                    id_legado, info["id_pref"], info["id_sec"]
                )
                target_id = dict_traducao_filhos.get(id_sec) or dict_traducao_pais.get(id_pref)
                if target_id:
                    c_at = info["row"]['created_at'] if pd.notna(info["row"].get('created_at')) else self.now
                    u_at = info["row"]['updated_at'] if pd.notna(info["row"].get('updated_at')) else self.now

                    base_configurada = RESERVAS_BASE_AVULSA.get(info["res_id"]) or RESERVAS_BASE_AVULSA.get(id_legado)
                    dados_base_reserva = BASES_AVULSOS.get(base_configurada)
                    usa_endereco_base = dados_base_reserva is not None

                    # Por padrão, address representa o destino final do legado.
                    # Só as exceções explicitamente mapeadas usam endereço-base
                    # ou o destino genérico de reserva configurado.
                    dados_endereco = config_reserva or dados_base_reserva or {
                        "alias": info["nome"],
                        "zip": info["row"]['CEP'],
                        "street": info["row"]['ENDERECO'],
                        "number": "S/N",
                        "city": info["row"]['CIDADE'],
                        "state": info["row"]['ESTADO'],
                    }

                    enderecos_batch.append({
                        "type": "customer", "id": target_id,
                        "alias": dados_endereco["alias"],
                        "zip": dados_endereco["zip"],
                        "street": dados_endereco["street"],
                        "num": dados_endereco["number"],
                        "city": dados_endereco["city"],
                        "state": dados_endereco["state"],
                        "leg_id": info["legacy_id"],
                        "res_id": (
                            info["res_id"]
                            if info["res_id"] is not None
                            else (id_legado if usa_endereco_base else None)
                        ),
                        "created_at": c_at, "updated_at": u_at
                    })
                    self.stats["enderecos"] += 1
                    mapa_refatorado[int(id_legado)] = {
                        "customer_id": target_id,
                        "address_id": None,
                        "alias": dados_endereco["alias"],
                        "reserved_customer_id": (
                            info["res_id"]
                            if info["res_id"] is not None
                            else (id_legado if usa_endereco_base else None)
                        ),
                    }
                else:
                    self._registrar_clientes_descartados(
                        pd.DataFrame([info['row']]), 'CLIENTE_SEM_PAI_HIERARQUICO_MIGRADO'
                    )

            # BULK INSERT MÁGICO
            print(f"📦 Arremessando Lote de {len(enderecos_batch)} Endereços de uma só vez...")
            # 👇 Ajustado: query dos Addresses agora insere as datas variáveis
            query_add = text("""
                INSERT INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, legacy_customer_id, reserved_customer_id, created_at, updated_at) 
                VALUES (:type, :id, :alias, :zip, :street, :num, :city, :state, 'Brasil', :leg_id, :res_id, :created_at, :updated_at)
            """)
            
            conn.execute(query_add, [{
                "type": a["type"], "id": a["id"], "alias": a["alias"], 
                "zip": a.get("zip"), "street": a.get("street"), "num": a.get("num"), 
                "city": a.get("city"), "state": a.get("state"), 
                "leg_id": a.get("leg_id"), "res_id": a.get("res_id"),
                "created_at": a.get("created_at", self.now), 
                "updated_at": a.get("updated_at", self.now)
            } for a in enderecos_batch])

            # Recupera o id gerado de cada endereço para o relatório de duplicados.
            # legacy_customer_id guarda o vínculo com o cliente do legado.
            if mapa_refatorado:
                consulta_ids = conn.execute(text("""
                    SELECT id, legacy_customer_id
                    FROM addresses
                    WHERE legacy_customer_id IS NOT NULL
                """))
                for address_id, legacy_customer_id in consulta_ids:
                    destino = mapa_refatorado.get(int(legacy_customer_id))
                    if destino is not None:
                        destino["address_id"] = address_id

        try:
            relatorio = self._relacionar_duplicados_clientes(
                df_enderecos_finais, mapa_refatorado
            )
        except Exception as erro:
            # Auditoria não pode derrubar a migração após a carga.
            print(f"⚠️ Não foi possível gerar o relatório de duplicados: {erro}")
            relatorio = None
        if relatorio is not None:
            # O merge altera dados: falha aqui deve interromper a migração.
            self._aplicar_merge_duplicados(relatorio, df_enderecos_finais)

        print("\n" + "="*50)
        print("📊 RELATÓRIO FINAL DA ESTRUTURA HIERÁRQUICA")
        print("="*50)
        print(f"✅ Prefeituras (Customers): {self.stats['pais']}")
        print(f"✅ Secretarias (Customers): {self.stats['filhos']}")
        print(f"💥 Reservas Mescladas:      {self.stats['mesclados']}")
        print(f"📈 Total Em Addresses:      {len(enderecos_batch)}")
        print("="*50)

    # ==============================================================================
    # ORQUESTRAÇÃO
    # ==============================================================================
    def executar(self):
        print("\n🚀 Iniciando Migração de Clientes...")
        df_bruto = self._extrair()
        df_finais, df_limpo = self._transformar(df_bruto)
        try:
            executar_truncate_tabelas(self.engine_new, TABELAS_CLIENTES)
            self._carregar(df_finais, df_limpo)
        finally:
            self._salvar_log_clientes_descartados()
        print("✅ Migração de Clientes Finalizada.")
