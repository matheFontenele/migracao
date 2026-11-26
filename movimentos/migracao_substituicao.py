import os
import glob
import re
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from config.parquet_excecoes import normalizar_tombo, tombos_priorizados
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino, limpar_codigo
from movimentos.classificacao_flags import KIT, classe_por_flags, serie_booleana
from utils.sanetizador import normalizar_para_match


STOPWORDS_CONTRATO = {
    "das", "dos", "com", "para", "por", "sem", "uma", "tipo", "nova", "novo",
    "canon", "epson", "brother", "ricoh", "samsung", "lenovo", "dell", "hp",
    "ns", "sn", "serie", "modelo",
}


def normalizar_numero_contrato(valor):
    """Somente dígitos: o número do contrato aparece no endereço sem máscara."""
    return re.sub(r"\D", "", str(valor or ""))


def tokens_contrato(valor):
    texto = normalizar_para_match(valor).lower()
    return {
        token for token in re.findall(r"[a-z0-9]+", texto)
        if len(token) > 2 and token not in STOPWORDS_CONTRATO
    }


def construir_indice_contratos(df_contratos):
    """Número (somente dígitos) -> ids de contrato, para busca no endereço."""
    indice = {}
    for _, row in df_contratos.iterrows():
        numero = normalizar_numero_contrato(row["number"])
        if len(numero) < 6:
            continue
        indice.setdefault(numero, set()).add(int(row["id"]))
    return indice


def resolver_contrato_por_endereco(alias_endereco, indice_contratos_por_numero):
    """Resolve o contrato pelo número embutido na descrição do endereço.

    O legado não guarda contrato por movimento; o endereço de entrega,
    porém, costuma trazer o número do contrato (ex.: ``...-20252208.12``).
    Só compara com sequências numéricas do endereço (evita casar com um
    dígito solto) e só aceita resultado único: número ambíguo não vira vínculo.
    """
    tokens = re.findall(r"\d[\d./-]{4,}\d", str(alias_endereco or ""))
    if not tokens:
        return None
    numeros_endereco = {normalizar_numero_contrato(token) for token in tokens}
    encontrados = {
        contrato_id
        for numero, ids in indice_contratos_por_numero.items()
        for sequencia in numeros_endereco
        if numero in sequencia
        for contrato_id in ids
    }
    return next(iter(encontrados)) if len(encontrados) == 1 else None


def resolver_item_por_similaridade(descricao_equipamento, itens_do_contrato, minimo=0.3):
    """Escolhe o item do contrato mais parecido com o equipamento.

    Jaccard sobre tokens normalizados, exigindo melhor score isolado. Sem
    isso o item cairia no "primeiro item do contrato", que costuma ser
    completamente outro produto.
    """
    tokens_equip = tokens_contrato(descricao_equipamento)
    if not tokens_equip or not itens_do_contrato:
        return None
    pontuados = []
    for item in itens_do_contrato:
        tokens_item = tokens_contrato(
            f"{item.get('alias') or ''} {item.get('description') or ''}"
        )
        uniao = tokens_equip | tokens_item
        if not uniao:
            continue
        pontuados.append((len(tokens_equip & tokens_item) / len(uniao), int(item["id"])))
    if not pontuados:
        return None
    pontuados.sort(reverse=True)
    melhor_score, melhor_id = pontuados[0]
    if melhor_score < minimo:
        return None
    if len(pontuados) > 1 and pontuados[1][0] == melhor_score:
        return None
    return melhor_id


def resolver_contrato_item_substituicao(
    alias_endereco, descricao_equipamento, indice_contratos_por_numero,
    itens_por_contrato, csv_contract_id=None, csv_item_id=None,
    cliente_parquet_ok=False, customer_id=None, contratos_por_cliente=None,
):
    """Contrato e item de uma fase da substituição.

    Ordem de confiança: número do contrato no endereço de entrega > Parquet
    do MESMO cliente do movimento > sem contrato (avulso). O contrato só é
    aceito se estiver vinculado ao cliente do movimento (dono ou
    destinatário) — é isso que impede repetir o vínculo cruzado do bug.
    O item só é aceito se pertencer ao contrato; caso contrário, é resolvido
    por similaridade com o equipamento — nunca pelo "primeiro item".
    """
    contrato = resolver_contrato_por_endereco(alias_endereco, indice_contratos_por_numero)
    if contrato is not None:
        origem = "ENDERECO"
    elif cliente_parquet_ok and csv_contract_id is not None:
        contrato, origem = csv_contract_id, "PARQUET"
    else:
        contrato, origem = None, "SEM_CONTRATO"

    if contrato is not None and contratos_por_cliente is not None:
        permitidos = contratos_por_cliente.get(int(customer_id), set()) if customer_id else set()
        if int(contrato) not in permitidos:
            contrato, origem = None, "CONTRATO_DE_OUTRO_CLIENTE"

    item = None
    if contrato is not None:
        itens_do_contrato = itens_por_contrato.get(contrato, [])
        if csv_item_id is not None and any(
            item_ref["id"] == csv_item_id for item_ref in itens_do_contrato
        ):
            item = csv_item_id
        else:
            item = resolver_item_por_similaridade(
                descricao_equipamento, itens_do_contrato
            )
    return contrato, item, origem


def deve_reconstruir_aluguel_historico(source_item_id, migrated_item_ids):
    """Deduplicate by exact legacy movement-item identity, not movement ID."""
    return source_item_id not in migrated_item_ids


def _int_ou_none(valor):
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def lados_ciclo_substituicao(eq_antigo, eq_novo, ids_equipamentos_ref):
    """Resolve os dois lados do ciclo de substituição contra o refatorado.

    O equipamento antigo pode ter sido excluído no legado (sucata/troca
    definitiva): ele não existe no refatorado. Nesse caso não há devolução
    nem aluguel histórico a gravar, mas o envio do substituto continua sendo
    válido e não pode ser perdido.
    """
    ids = set(ids_equipamentos_ref or ())
    antigo = eq_antigo if eq_antigo in ids else None
    novo = eq_novo if eq_novo in ids else None
    return antigo, novo


def destino_status_equipamento_antigo(situacao):
    """Status final (refatorado) do equipamento devolvido na substituição.

    - 14 (EM DEVOLUÇÃO) -> 8 (EM DEVOLUÇÃO no refatorado);
    - 1 (ALUGADO) -> None: continua alugado; a fase de aluguel já gravou o
      movimento ativo, então o status não deve ser sobrescrito;
    - demais situações -> 1 (DISPONÍVEL PARA LOCAÇÃO).
    """
    if situacao == 14:
        return 8
    if situacao == 1:
        return None
    return 1


def deve_processar_envio(tombo_novo, tombos_com_substituicao, equipamento_novo_id):
    """Indica se o envio da máquina nova deve ser migrado.

    Só migramos o envio quando o tombo novo é o aluguel vigente do Parquet
    (tem movimento de substituição registrado). A devolução da máquina antiga
    NÃO depende disso: ela é registrada mesmo quando o substituto está em
    manutenção ou fora do Parquet.
    """
    return bool(equipamento_novo_id) and tombo_novo in set(
        tombos_com_substituicao or ()
    )


def montar_presente_substituicao(df_devolucao, df_entrega):
    """Combina os dois lados do ciclo, preservando cada item uma única vez.

    Os DataFrames vêm pareados por ``(substituicao_id, seq)``. O merge é
    externo porque um lado pode ter mais itens que o outro (composição de kit
    diferente, item excluído, equipamento em manutenção): o item sem par não
    pode ser descartado, só a fase do lado ausente é pulada no loop.
    """
    if df_devolucao.empty and df_entrega.empty:
        return pd.DataFrame()

    df = pd.merge(
        df_devolucao,
        df_entrega,
        on=['substituicao_id', 'seq'],
        how='outer',
        suffixes=('_DEV', '_ENT'),
    )

    presente = pd.DataFrame({
        'substituicao_id': df['substituicao_id'],
        'seq': df['seq'],
        'CLIENTE_ID': df['CLIENTE_ID_DEV'].combine_first(df['CLIENTE_ID_ENT']),
        'ORGAO_DEV': df['ORGAO_ID_DEV'],
        'USR_SUBST': df['USUARIO_ID_DEV'].combine_first(df['USUARIO_ID_ENT']),
        'DATA_DEVOLUCAO': df['DATA_MOV_DEV'],
        'DATA_ALUGUEL': df['DATA_MOV_ENT'],
        'ORGAO_ALUGUEL': df['ORGAO_ID_ENT'],
        'DATA_SUBST': df['DATA_MOV_ENT'].combine_first(df['DATA_MOV_DEV']),
        'DEL_SUBST': df['DELETED_AT_DEV'],
        'MOV_DEV_ID': df['MOVIMENTO_ID_DEV'],
        'EQUIPAMENTO_ID_ANTIGO': df['EQUIPAMENTO_ID_DEV'],
        'TOMBO_ANTIGO': df['TOMBO_DEV'],
        'NOME_ANTIGO': df['NOME_DEV'],
        'TIPO_ANTIGO': df['TIPO_ID_DEV'],
        'MOV_NOVO_ID': df['MOVIMENTO_ID_ENT'],
        'EQUIPAMENTO_ID_NOVO': df['EQUIPAMENTO_ID_ENT'],
        'TOMBO_NOVO': df['TOMBO_ENT'],
        'NOME_NOVO': df['NOME_ENT'],
        'TIPO_NOVO': df['TIPO_ID_ENT'],
    })
    return presente.sort_values(['substituicao_id', 'seq']).reset_index(drop=True)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_PARQUETS = "./docs/parquets"
ARQUIVO_TYPES = "./docs/types.csv"


def parametros_envio_substituicao(is_avulso):
    """Tipo/operação/motivo do envio do substituto (FASE 3).

    Equipamento sem contrato válido entra como AVULSO (tipo 7), igual à
    migração de aluguel; com contrato, segue como ALUGUEL (tipo 1).
    """
    if is_avulso:
        return 7, 'AVULSO', 'SHIPPING_CONFIRMED_SEPARATE'
    return 1, 'ALUGUEL', 'SHIPPING_CONFIRMED_RENT'

class MigracaoSubstituicao(BaseMigracaoMovimento):

    @staticmethod
    def selecionar_destinatarios(cliente_substituicao_id, cliente_origem_id, dict_cliente_adress, dict_endereco_por_legacy_client):
        """Return current and historical customer/address pairs.

        O histórico só reaproveita o cliente da substituição quando o legado
        não informa a origem (aluguel avulso, sem cliente). Se a origem existe
        mas não tem de/para no refatorado, o par fica vazio: atribuir o aluguel
        histórico ao cliente da substituição geraria um movimento com dono
        errado — melhor não criar o histórico do que criar um vínculo falso.
        """
        def resolver(cliente_id):
            if cliente_id is None or pd.isna(cliente_id):
                return None, None
            cliente_id = int(cliente_id)
            return (
                dict_cliente_adress.get(cliente_id),
                dict_endereco_por_legacy_client.get(cliente_id),
            )

        atual = resolver(cliente_substituicao_id)
        origem_informada = cliente_origem_id is not None and not pd.isna(cliente_origem_id)
        passado = resolver(cliente_origem_id) if origem_informada else atual
        return atual, passado

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=900000):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente=False)
        self.saldos_modificados = set()
        
        with self.engine_new.connect() as conn:
            org_ids = [str(org['id']) for org in ENDERECOS_BASES]
            org_ids_sql = "(" + ", ".join(org_ids) + ")"
            query_enderecos = f"SELECT addressable_id, MIN(id) as address_id FROM addresses WHERE addressable_type = 'organization' AND addressable_id IN {org_ids_sql} GROUP BY addressable_id"
            result_ends = conn.execute(text(query_enderecos)).fetchall()
            self.dict_enderecos_base_org = {row.addressable_id: row.address_id for row in result_ends}

    def calcular_saldo(self, *args, **kwargs):
        """Dummy fantasma: A matemática do saldo ocorre na classe pai!"""
        return 0, None, int(args[0]) if pd.notna(args[0]) else None

    def _atualizar_saldos_mysql(self):
        if getattr(self, 'consumir_saldos', None) is False or not self.saldos_modificados:
            return

        print(f"\n💾 Sincronizando {len(self.saldos_modificados)} saldos modificados (Substituição) com o MySQL...")
        atualizados = 0
        with self.engine_new.begin() as conn:
            for c_id in self.saldos_modificados:
                qtd_final_banco = max(0, int(self.dados["saldos_por_id"][c_id]))
                res = conn.execute(
                    text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), 
                    {"nova_qtd": qtd_final_banco, "id": c_id}
                )
                if res.rowcount > 0:
                    atualizados += 1
        print(f"  ✔️ {atualizados} itens de contrato atualizados com sucesso!")

    def _carregar_itens_parquet(self):
        arquivos_parquet = sorted(glob.glob(os.path.join(PASTA_PARQUETS, "*.parquet")))
        if not arquivos_parquet: return pd.DataFrame()

        dataframes = [pd.read_parquet(arquivo) for arquivo in arquivos_parquet]
        df_itens = pd.concat(dataframes, ignore_index=True).drop_duplicates().copy()
        df_itens.columns = df_itens.columns.str.upper()

        df_itens["TOMBO"] = pd.to_numeric(df_itens["TOMBO"], errors="coerce")
        df_itens = df_itens.dropna(subset=["TOMBO"])
        df_itens["TOMBO"] = df_itens["TOMBO"].astype(int).astype(str)
        for coluna_flag in ("IS_EXTRA_KIT", "IS_AVULSO"):
            if coluna_flag in df_itens.columns:
                df_itens[coluna_flag] = serie_booleana(df_itens[coluna_flag])
        return df_itens

    def _extrair_dados_substituicao(self):
        print("   📖 Extraindo O PRESENTE (Pareamento por posição no ciclo)...")

        # O legado NÃO guarda o vínculo item a item da substituição:
        # aluguel_substituicao relaciona apenas as duas capas de movimento.
        # Cada lado é lido por inteiro e os itens são pareados pela posição
        # (SEQ). Assim cada item aparece uma única vez (sem produto
        # cartesiano) e um item sem par no outro lado não é descartado.
        #
        # No lado devolvido os equipamentos ainda existentes vêm primeiro,
        # para que a posição não seja gasta com sucata (que não tem fase a
        # registrar). A ordenação por tipo e número mantém o pareamento
        # alinhado quando a composição dos kits é a mesma.
        seq_devolucao = """
            (
                SELECT COUNT(*) + 1
                FROM aluguel_substituicao sub2
                INNER JOIN aluguel_movimento mov2 ON sub2.substituicao_aluguel_id = mov2.id
                INNER JOIN aluguel_movimento_itens movi2 ON mov2.id = movi2.movimento_id
                INNER JOIN aluguel_equipamentos eq2 ON movi2.equipamento_id = eq2.id
                WHERE mov2.deleted_at IS NULL AND movi2.deleted_at IS NULL
                AND sub2.id = sub.id
                AND (
                    (eq2.deleted_at IS NULL) > (eq.deleted_at IS NULL)
                    OR ((eq2.deleted_at IS NULL) = (eq.deleted_at IS NULL) AND (
                        eq2.tipo_id < eq.tipo_id
                        OR (eq2.tipo_id = eq.tipo_id AND eq2.numero < eq.numero)
                        OR (eq2.tipo_id = eq.tipo_id AND eq2.numero = eq.numero AND eq2.id < eq.id)
                    ))
                )
            )
        """

        seq_entrega = """
            (
                SELECT COUNT(*) + 1
                FROM aluguel_substituicao sub2
                INNER JOIN aluguel_movimento mov2 ON sub2.substituicao_devolucao_id = mov2.id
                INNER JOIN aluguel_movimento_itens movi2 ON mov2.id = movi2.movimento_id
                INNER JOIN aluguel_equipamentos eq2 ON movi2.equipamento_id = eq2.id
                WHERE mov2.deleted_at IS NULL AND movi2.deleted_at IS NULL
                AND eq2.deleted_at IS NULL
                AND sub2.id = sub.id
                AND (
                    eq2.tipo_id < eq.tipo_id
                    OR (eq2.tipo_id = eq.tipo_id AND eq2.numero < eq.numero)
                    OR (eq2.tipo_id = eq.tipo_id AND eq2.numero = eq.numero AND eq2.id < eq.id)
                )
            )
        """

        colunas_lado = """
                    sub.id AS substituicao_id,
                    mov.cliente_id AS CLIENTE_ID,
                    movi.novo_orgao_id AS ORGAO_ID,
                    COALESCE(NULLIF(mov.usuario_id, 0), 1) AS USUARIO_ID,
                    mov.data AS DATA_MOV,
                    mov.deleted_at AS DELETED_AT,
                    mov.id AS MOVIMENTO_ID,
                    eq.id AS EQUIPAMENTO_ID,
                    eq.numero AS TOMBO,
                    eq.nome AS NOME,
                    eq.tipo_id AS TIPO_ID
        """

        # Lado devolvido (substituicao_aluguel_id): o equipamento pode estar
        # excluído no legado (sucata/troca definitiva). A linha não pode ser
        # descartada; as fases do antigo só rodam se ele existir no
        # refatorado, mas a devolução de outros itens do ciclo continua.
        query_devolucao = f"""
            SELECT
                {seq_devolucao} AS seq,
                {colunas_lado}
            FROM aluguel_substituicao sub
            INNER JOIN aluguel_movimento mov ON sub.substituicao_aluguel_id = mov.id
            INNER JOIN aluguel_movimento_itens movi ON mov.id = movi.movimento_id
            INNER JOIN aluguel_equipamentos eq ON movi.equipamento_id = eq.id
            WHERE mov.deleted_at IS NULL AND movi.deleted_at IS NULL
        """

        # Lado entregue (substituicao_devolucao_id): só equipamentos ativos
        # no legado foram migrados para o refatorado.
        query_entrega = f"""
            SELECT
                {seq_entrega} AS seq,
                {colunas_lado}
            FROM aluguel_substituicao sub
            INNER JOIN aluguel_movimento mov ON sub.substituicao_devolucao_id = mov.id
            INNER JOIN aluguel_movimento_itens movi ON mov.id = movi.movimento_id
            INNER JOIN aluguel_equipamentos eq ON movi.equipamento_id = eq.id
            WHERE mov.deleted_at IS NULL AND movi.deleted_at IS NULL
                AND eq.deleted_at IS NULL
        """

        with self.engine_legado.connect() as conn:
            df_devolucao = pd.read_sql(text(query_devolucao), conn)
            df_entrega = pd.read_sql(text(query_entrega), conn)

        df_presente = montar_presente_substituicao(df_devolucao, df_entrega)
        if df_presente.empty:
            return pd.DataFrame()

        # O histórico é resolvido pelo ID do equipamento legado (preservado na
        # migração), nunca pelo tombo: o mesmo número pode identificar várias
        # cópias e o aluguel de uma não pertence à outra.
        ids_antigos = (
            df_presente['EQUIPAMENTO_ID_ANTIGO'].dropna().astype(int).unique().tolist()
        )
        ids_sql = "(" + ", ".join(str(i) for i in ids_antigos) + ")"

        print(f"   📖 Extraindo O PASSADO (Histórico de Origens para {len(ids_antigos)} equipamentos antigos)...")
        query_passado = f"""
            SELECT ami.equipamento_id AS EQUIPAMENTO_ID_ANTIGO, ami.id AS ORIG_MOV_ITEM_ID, mov.id AS ORIG_MOV_ID, ac.id AS ORIG_CLIENTE_ID, ami.novo_orgao_id AS ORIG_ORGAO_ID, COALESCE(NULLIF(mov.usuario_id, 0), 1) AS ORIG_USR_ID, COALESCE(mov.data, mov.updated_at) AS ORIG_DATA, mov.deleted_at AS ORIG_DEL, mov.tipo_id AS ORIG_TIPO_LEGADO, mov.data AS DATA_REAL_ORDENACAO
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens ami ON ami.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ami.movimento_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL AND mov.deleted_at IS NULL AND eq.id IN {ids_sql} AND mov.tipo_id IN (1, 7)
        """
        with self.engine_legado.connect() as conn:
            df_historico_origens = pd.read_sql(text(query_passado), conn)

        df_historico_origens.sort_values(by=['EQUIPAMENTO_ID_ANTIGO', 'DATA_REAL_ORDENACAO', 'ORIG_MOV_ID'], ascending=[True, False, False], inplace=True)

        # O aluguel histórico precisa ser o aluguel VIGENTE na data da
        # substituição. Pegar o último aluguel do equipamento sem limite de
        # data trazia aluguéis posteriores — de outro cliente, inclusive —
        # para dentro de um ciclo antigo.
        colunas_origem = [
            'ORIG_MOV_ITEM_ID', 'ORIG_MOV_ID', 'ORIG_CLIENTE_ID', 'ORIG_USR_ID',
            'ORIG_DATA', 'ORIG_DEL', 'ORIG_TIPO_LEGADO', 'ORIG_ORGAO_ID',
            'DATA_REAL_ORDENACAO',
        ]
        df_candidatos = pd.merge(
            df_presente,
            df_historico_origens[['EQUIPAMENTO_ID_ANTIGO'] + colunas_origem],
            on='EQUIPAMENTO_ID_ANTIGO',
            how='left',
        )
        limite = pd.to_datetime(df_candidatos['DATA_DEVOLUCAO'], errors='coerce')
        data_origem = pd.to_datetime(df_candidatos['ORIG_DATA'], errors='coerce')
        df_candidatos = df_candidatos[data_origem.isna() | (data_origem <= limite)]
        df_candidatos.sort_values(
            by=['substituicao_id', 'seq', 'DATA_REAL_ORDENACAO', 'ORIG_MOV_ID'],
            ascending=[True, True, False, False],
            inplace=True,
        )
        df_candidatos = df_candidatos.drop_duplicates(
            subset=['substituicao_id', 'seq'], keep='first'
        )

        # A fase de aluguel histórico é opcional: devoluções de equipamentos
        # sem aluguel de origem (tipo 1/7) no legado continuam válidas. Por
        # isso o ciclo é remontado a partir de df_presente — um ciclo sem
        # origem válida fica com ORIG_* nulos em vez de desaparecer.
        df_completo = pd.merge(
            df_presente,
            df_candidatos[['substituicao_id', 'seq'] + colunas_origem],
            on=['substituicao_id', 'seq'],
            how='left',
        )

        return df_completo.reset_index(drop=True)

    def executar(self):
        print("\n" + "=" * 70)
        print("🔄 MÓDULO: SUBSTITUIÇÃO (A TRANSIÇÃO COMPLETA)")
        print("=" * 70)

        # 1. Preparação de Dicionários Iniciais
        if os.path.exists(ARQUIVO_TYPES):
            self.dict_is_kit = {int(row['id']): int(row['is_kit']) for _, row in pd.read_csv(ARQUIVO_TYPES).iterrows() if pd.notna(row['id'])}
        else:
            self.dict_is_kit = {}

        df_parquet = self._carregar_itens_parquet()
        dict_row_parquet_by_tombo = {str(row["TOMBO"]): row for _, row in df_parquet.iterrows()} if not df_parquet.empty else {}
        
        # 🔓 Equipamentos priorizados não usam dados do Parquet: a linha é
        # descartada e a substituição é montada apenas com o legado.
        excecoes_parquet = tombos_priorizados(self.engine_legado)
        if excecoes_parquet:
            print(
                f"🔓 Linhas do Parquet ignoradas por priorização do legado: "
                f"{', '.join(sorted(excecoes_parquet))}"
            )
        
        tombos_parquet = list(dict_row_parquet_by_tombo.keys())
        dict_ultimo_mov_subst = self.buscar_ultimo_movimento_cte(tombos_parquet, tipos_permitidos=(5,), situacoes_permitidas=(1, 15))

        with self.engine_new.connect() as conn:
            dict_contract_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM contracts", conn).values.T))
            dict_customer_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM customers", conn).values.T))
            dict_equip_org = dict(zip(*pd.read_sql("SELECT id, current_organization_id FROM equipments", conn).values.T))

            # O contrato do movimento não existe no legado. A referência
            # confiável é o número do contrato escrito no endereço de entrega;
            # o Parquet só serve quando é do MESMO cliente do movimento.
            df_contratos_ref = pd.read_sql(
                "SELECT id, number FROM contracts WHERE deleted_at IS NULL", conn
            )
            df_itens_ref = pd.read_sql(
                """
                SELECT ci.id, ci.alias, ci.description, ce.contract_id
                FROM contract_items ci
                INNER JOIN event_additives ea ON ea.id = ci.event_additive_id
                INNER JOIN contract_events ce ON ce.id = ea.event_id
                WHERE ci.deleted_at IS NULL
                """,
                conn,
            )
            df_alias_enderecos = pd.read_sql(
                "SELECT id, alias FROM addresses WHERE deleted_at IS NULL", conn
            )
            df_contratos_clientes = pd.read_sql(
                """
                SELECT id AS contract_id, customer_id FROM contracts WHERE deleted_at IS NULL
                UNION
                SELECT contract_id, customer_id FROM contract_recipient_customers
                """,
                conn,
            )

        indice_contratos_por_numero = construir_indice_contratos(df_contratos_ref)
        contratos_por_cliente = {}
        for _, vinculo in df_contratos_clientes.iterrows():
            contratos_por_cliente.setdefault(int(vinculo["customer_id"]), set()).add(
                int(vinculo["contract_id"])
            )
        itens_por_contrato = {}
        for _, item_ref in df_itens_ref.iterrows():
            itens_por_contrato.setdefault(int(item_ref["contract_id"]), []).append(
                {
                    "id": int(item_ref["id"]),
                    "alias": item_ref["alias"],
                    "description": item_ref["description"],
                }
            )
        dict_alias_endereco = dict(zip(df_alias_enderecos["id"], df_alias_enderecos["alias"]))
            
        df_subst = self._extrair_dados_substituicao()
        if df_subst.empty:
            print("⚠️ Nenhuma Substituição válida encontrada no legado.")
            return

        tombos_todos = list(set(df_subst['TOMBO_NOVO'].tolist() + df_subst['TOMBO_ANTIGO'].tolist()))
        dict_equip_novo = self.buscar_equipamentos_novo_por_tombo(tombos_todos)
        
        rejeitados = 0
        historicos_sem_destino = 0
        log_nao_match = []
        log_contrato_ajustado = []
        eqs_antigos_alterar = []
        eqs_novos_alterar = []
        map_eqs_antigos = {} 
        eqs_antigos_alterar_8 = []
        eqs_antigos_alterar_1 = []

        for _, row in tqdm(df_subst.iterrows(), total=df_subst.shape[0], desc="Processando Ciclos"):

            tombo_novo = limpar_codigo(row.get('TOMBO_NOVO')) if pd.notna(row.get('TOMBO_NOVO')) else ""
            tombo_antigo = limpar_codigo(row['TOMBO_ANTIGO'])

            # O id do equipamento no legado é preservado na migração. Usar o
            # id do item do movimento evita escolher a cópia errada quando o
            # mesmo tombo aparece em mais de um equipamento.
            eq_id_antigo = _int_ou_none(row.get('EQUIPAMENTO_ID_ANTIGO'))
            if eq_id_antigo is None:
                eq_id_antigo = dict_equip_novo.get(tombo_antigo)

            eq_id_novo = _int_ou_none(row.get('EQUIPAMENTO_ID_NOVO'))
            if eq_id_novo is None:
                eq_id_novo = dict_equip_novo.get(tombo_novo)

            eq_id_antigo, eq_id_novo = lados_ciclo_substituicao(
                eq_id_antigo, eq_id_novo, self.dados["ids_equipamentos_ref"]
            )

            # O envio da máquina nova só é migrado quando o tombo novo é o
            # aluguel vigente do Parquet. A devolução da máquina antiga é
            # independente: ela precisa ser registrada mesmo que o substituto
            # não seja migrável (parquet desatualizado, equipamento em
            # manutenção, etc.). O inverso também vale: se o equipamento
            # antigo foi excluído no legado, o envio do substituto ainda
            # precisa ser migrado.
            processar_envio = deve_processar_envio(
                tombo_novo, dict_ultimo_mov_subst, eq_id_novo
            )
            mov_novo_id = _int_ou_none(row.get('MOV_NOVO_ID'))

            if eq_id_antigo is None and not processar_envio:
                rejeitados += 1
                continue

            row_parquet = dict_row_parquet_by_tombo.get(tombo_novo)
            tombo_parquet = tombo_novo
            if row_parquet is None:
                row_parquet = dict_row_parquet_by_tombo.get(tombo_antigo)
                tombo_parquet = tombo_antigo
            if row_parquet is not None and normalizar_tombo(tombo_parquet) in excecoes_parquet:
                row_parquet = None

            # O cliente da substituição pertence ao próprio registro legado
            # (d.cliente_id). O parquet é indexado somente pelo tombo e pode
            # conter mais de um equipamento/cliente com o mesmo número; não
            # deve sobrescrever a identidade explícita da substituição.
            if pd.notna(row.get('CLIENTE_ID')):
                cli_legado_id = int(row['CLIENTE_ID'])
            elif pd.notna(row.get('ORIG_CLIENTE_ID')):
                cli_legado_id = int(row['ORIG_CLIENTE_ID'])
            else:
                cli_leg_parquet = row_parquet.get("CLIENTE_ID") if row_parquet is not None else None
                cli_legado_id = int(float(cli_leg_parquet)) if pd.notna(cli_leg_parquet) and str(cli_leg_parquet).strip() else 0

            (recipient_id, cliente_final_address), (recipient_historico_id, endereco_historico) = self.selecionar_destinatarios(
                cli_legado_id,
                row.get('ORIG_CLIENTE_ID'),
                self.dados["dict_cliente_adress"],
                self.dados["dict_endereco_por_legacy_client"],
            )
            
            if not recipient_id:
                rejeitados += 1
                continue

            org_id_cliente_legado = descobrir_id_organizacao_destino(cli_legado_id)
            endereco_base_id = self.dict_enderecos_base_org.get(org_id_cliente_legado, self.dict_enderecos_base_org.get(1115, 1))
            raw_contract_id = row_parquet.get('CONTRACT_ID') if row_parquet is not None else None
            csv_contract_id = int(float(raw_contract_id)) if pd.notna(raw_contract_id) and str(raw_contract_id).strip() not in ['None', 'nan', ''] else None

            raw_item_id = row_parquet.get('CONTRACT_ITEM_ID') if row_parquet is not None else None
            csv_item_id = int(float(raw_item_id)) if pd.notna(raw_item_id) and str(raw_item_id).strip() not in ['None', 'nan', ''] else None

            # O Parquet é indexado por tombo e pode descrever o equipamento
            # para outro cliente/contrato. Só o usamos quando o cliente da
            # linha é o mesmo do movimento; caso contrário, o contrato é
            # descartado (avulso) ou recuperado pelo endereço de entrega.
            cliente_parquet_ok = False
            if row_parquet is not None:
                cli_parquet = row_parquet.get('CLIENTE_ID')
                if pd.notna(cli_parquet) and str(cli_parquet).strip() not in ['', 'None', 'nan']:
                    cliente_parquet_ok = int(float(cli_parquet)) == cli_legado_id

            contrato_resolvido, item_resolvido, origem_contrato = resolver_contrato_item_substituicao(
                dict_alias_endereco.get(cliente_final_address),
                row.get('NOME_ANTIGO') if pd.notna(row.get('NOME_ANTIGO')) else row.get('NOME_NOVO'),
                indice_contratos_por_numero,
                itens_por_contrato,
                csv_contract_id=csv_contract_id,
                csv_item_id=csv_item_id,
                cliente_parquet_ok=cliente_parquet_ok,
                customer_id=recipient_id,
                contratos_por_cliente=contratos_por_cliente,
            )

            # Flags do Parquet para o tombo desta fase. A flag IS_AVULSO não
            # altera a substituição: o vínculo do ciclo vem do registro legado.
            # Já o kit declarado continua atrelando o primeiro item do contrato,
            # mesmo com permitir_fallback_item=False. Conflitos de flag já
            # bloquearam o arquivo em geracao_parquets.py; aqui só o kit é
            # aplicado, e apenas quando há contrato resolvido para a fase.
            decisao = None
            if row_parquet is not None:
                decisao = classe_por_flags(
                    row_parquet.get("IS_EXTRA_KIT"),
                    row_parquet.get("IS_AVULSO"),
                    contrato_resolvido,
                    item_resolvido,
                )
            classe_forcada = (
                KIT if decisao is not None and decisao[0] == KIT and contrato_resolvido is not None
                else None
            )

            (contrato_id_res, item_id_res, is_avulso, is_kit, is_excedente, teve_match_perfeito, motivo_divergencia) = self.regras_item_contratos(
                contrato_resolvido, item_resolvido, eq_id_novo or eq_id_antigo, recipient_id, self.dict_is_kit,
                abater_saldo=processar_envio, permitir_fallback_item=False,
                classe_forcada=classe_forcada,
            )

            if contrato_id_res != csv_contract_id:
                log_contrato_ajustado.append({
                    "SUBSTITUICAO_ID": row['substituicao_id'], "TOMBO": tombo_novo,
                    "ID_CLIENTE_LEGADO": cli_legado_id, "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRATO_PARQUET": csv_contract_id, "CONTRATO_APLICADO": contrato_id_res,
                    "ORIGEM": origem_contrato, "ITEM_APLICADO": item_id_res,
                })

            if contrato_id_res is not None and item_id_res is None and not motivo_divergencia:
                motivo_divergencia = "Contrato sem item compatível; item não vinculado."

            if motivo_divergencia:
                status_final_log = "AVULSO" if is_avulso else "KIT (IMUNE)" if is_kit else "EXCEDENTE" if is_excedente else "NORMAL"
                log_nao_match.append({
                    "TOMBO": tombo_novo, "ID_CLIENTE_LEGADO": cli_legado_id, "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRACT_ID_CSV": csv_contract_id, "CONTRATO_RESOLVIDO": contrato_id_res,
                    "ITEM_RESOLVIDO_ID": item_id_res, "STATUS_FINAL": status_final_log, "MOTIVO_EXATO": motivo_divergencia
                })

            org_id_cascata = dict_contract_org.get(contrato_id_res) or dict_customer_org.get(recipient_id) or dict_equip_org.get(eq_id_novo or eq_id_antigo) or 1115
            org_aluguel = descobrir_id_organizacao_destino(row['ORGAO_ALUGUEL']) if pd.notna(row.get('ORGAO_ALUGUEL')) else org_id_cascata
            org_devolucao = descobrir_id_organizacao_destino(row['ORGAO_DEV']) if pd.notna(row.get('ORGAO_DEV')) else org_id_cascata

            # =========================================================
            # 🕰️ FASE 1: O PASSADO (ALUGAMOS A MÁQUINA ANTIGA)
            # =========================================================
            contrato_hist = contrato_id_res
            item_hist = item_id_res
            avulso_hist = is_avulso
            if recipient_historico_id and recipient_historico_id != recipient_id:
                # O aluguel histórico pertence ao cliente de ORIGEM, que pode
                # ser outro cliente. O contrato do cliente da substituição não
                # vale para ele: resolve pelo endereço de origem.
                contrato_hist, item_hist, _ = resolver_contrato_item_substituicao(
                    dict_alias_endereco.get(endereco_historico),
                    row.get('NOME_ANTIGO') if pd.notna(row.get('NOME_ANTIGO')) else row.get('NOME_NOVO'),
                    indice_contratos_por_numero,
                    itens_por_contrato,
                    customer_id=recipient_historico_id,
                    contratos_por_cliente=contratos_por_cliente,
                )
                avulso_hist = contrato_hist is None

            org_origem = (
                descobrir_id_organizacao_destino(row['ORIG_ORGAO_ID'])
                if pd.notna(row.get('ORIG_ORGAO_ID'))
                else dict_contract_org.get(contrato_hist)
                or dict_customer_org.get(recipient_historico_id)
                or org_id_cascata
            )

            if eq_id_antigo is not None and pd.notna(row.get('ORIG_MOV_ID')):
                if not recipient_historico_id:
                    # Origem existe no legado, mas o cliente não tem de/para.
                    # Não criamos o aluguel histórico para não atribuí-lo ao
                    # cliente errado; o diagnóstico sinaliza o caso.
                    historicos_sem_destino += 1
                else:
                    source_item_id = int(row['ORIG_MOV_ITEM_ID'])
                    migrated_item_ids = self.dados.setdefault("migrated_legacy_movement_item_ids", set())
                    if deve_reconstruir_aluguel_historico(source_item_id, migrated_item_ids):
                        id_legado_origem = int(row['ORIG_MOV_ID'])
                        self.registrar_movimento(
                        id_final=int(row['ORIG_MOV_ID']),
                        recipient_id=recipient_historico_id,
                        cliente_final_address_id=endereco_historico,
                        usuario_id=int(row['ORIG_USR_ID']),
                        organization_id=int(org_origem),
                        mov_date=row['ORIG_DATA'],
                        deleted_at_mov=row['ORIG_DEL'] if pd.notna(row['ORIG_DEL']) else None,
                        contrato_id=contrato_hist,
                        contrato_item_id=item_hist,
                        equipment_id_ref=eq_id_antigo,
                        status_shipment=2, 
                        tipo_movimento_id=7 if avulso_hist else 1,
                        operation_type='ALUGUEL',
                        status_equipment_id=2,
                        history_reason='SHIPPING_CONFIRMED_SEPARATE',
                        is_exchange=False,
                        consumir_saldo=False, 
                        alias_movimento=row['NOME_ANTIGO'],
                        details_capa=f"Aluguel Passado - Origem da Substituição: {id_legado_origem}",
                        details_item=f"Aluguel Passado - Origem da Substituição: {id_legado_origem}"
                        )
                        migrated_item_ids.add(source_item_id)

            # =========================================================
            # 📥 FASE 2: O PRESENTE (RECOLHEMOS A MÁQUINA ANTIGA)
            # =========================================================
            # Só existe devolução quando o equipamento antigo foi migrado.
            # Se ele foi excluído no legado, a fase é pulada e o envio do
            # substituto (fase 3) segue normalmente.
            if eq_id_antigo is not None:
                id_mov_dev = int(row['MOV_DEV_ID'])
                _, _, id_mov_item_dev = self.registrar_movimento(
                    id_final=id_mov_dev,
                    recipient_id=recipient_id,
                    cliente_final_address_id=cliente_final_address,
                    usuario_id=int(row['USR_SUBST']),
                    organization_id=int(org_devolucao),
                    mov_date=row['DATA_DEVOLUCAO'],
                    deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                    contrato_id=contrato_id_res,
                    contrato_item_id=item_id_res,
                    equipment_id_ref=eq_id_antigo,
                    status_shipment=2,
                    tipo_movimento_id=2,
                    operation_type='DEVOLUCAO',
                    status_equipment_id=8,
                    history_reason='SHIPPING_CONFIRMED_DEVOLUTION',
                    is_exchange=False,
                    consumir_saldo=False,
                    alias_movimento=row['NOME_ANTIGO'],
                    details_capa=f"Devolução da Substituição Legado: {row['substituicao_id']}",
                    details_item=f"Devolução da Substituição Legado: {row['substituicao_id']}"
                )
                map_eqs_antigos[int(eq_id_antigo)] = int(id_mov_item_dev)

            # =========================================================
            # 📤 FASE 3: O PRESENTE (ENVIAMOS A MÁQUINA NOVA)
            # =========================================================
            if processar_envio:
                tipo_envio, operacao_envio, motivo_envio = parametros_envio_substituicao(is_avulso)
                _, _, id_mov_item_novo = self.registrar_movimento(
                    id_final=mov_novo_id,
                    recipient_id=recipient_id,
                    cliente_final_address_id=cliente_final_address,
                    usuario_id=int(row['USR_SUBST']),
                    organization_id=int(org_aluguel),
                    mov_date=row['DATA_SUBST'],
                    deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                    contrato_id=contrato_id_res,
                    contrato_item_id=item_id_res,
                    equipment_id_ref=eq_id_novo, 
                    status_shipment=2, 
                    tipo_movimento_id=tipo_envio,
                    operation_type=operacao_envio,
                    status_equipment_id=2,
                    history_reason=motivo_envio,
                    is_exchange=is_excedente,
                    consumir_saldo=False, 
                    alias_movimento=row['NOME_NOVO'],
                    details_capa=f"Envio da Substituição Legado: {row['substituicao_id']}",
                    details_item=f"Envio da Substituição Legado: {row['substituicao_id']}",
                    forcar_atualizacao_parque=True 
                )
                eqs_novos_alterar.append({int(eq_id_novo): int(id_mov_item_novo)})

        if rejeitados > 0:
            print(f"\n⚠️ Registros rejeitados (Sem equipamento, rejeitado pela CTE ou faltante no Parquet): {rejeitados}")
        if historicos_sem_destino > 0:
            print(
                f"\n⚠️ {historicos_sem_destino} aluguel(éis) histórico(s) ignorado(s): "
                "o cliente de origem não tem de/para no refatorado."
            )

        if log_nao_match:
            pd.DataFrame(log_nao_match).to_csv("log_divergencias_substituicao.csv", index=False)

        if log_contrato_ajustado:
            pd.DataFrame(log_contrato_ajustado).to_csv("log_contratos_substituicao.csv", index=False)
            print(
                f"\n📄 {len(log_contrato_ajustado)} vínculo(s) de contrato ajustado(s) "
                f"em substituições (log_contratos_substituicao.csv)."
            )

        #Validação de situação de equipamentos antigos para atualizar status
        if map_eqs_antigos:
            print(f"\n🔍 Verificando situação final de {len(map_eqs_antigos)} equipamentos antigos no legado...")
            ids_in = "(" + ", ".join(str(int(i)) for i in map_eqs_antigos.keys()) + ")"

            # Consulta por ID (não por tombo): o tombo se repete no legado e a
            # situação de uma cópia não vale para as outras.
            query_situacao = f"""
                SELECT
                    alq.id AS id,
                    alq.numero AS tombo,
                    alq.nome AS name,
                    alq.situacao_id AS situacao
                FROM aluguel_equipamentos alq
                WHERE alq.id IN {ids_in}
            """

            with self.engine_legado.connect() as conn:
                df_situacoes = pd.read_sql(text(query_situacao), conn)

            for _, row_sit in df_situacoes.iterrows():
                eq_id = int(row_sit['id'])
                dict_update = map_eqs_antigos.get(eq_id)
                if not dict_update:
                    continue
                status_destino = destino_status_equipamento_antigo(int(row_sit['situacao']))
                if status_destino == 8:
                    eqs_antigos_alterar_8.append({eq_id: dict_update})
                elif status_destino == 1:
                    eqs_antigos_alterar_1.append({eq_id: dict_update})
                # status_destino None: o legado ainda considera o equipamento
                # alugado; a fase ALUGUEL já gravou o movimento ativo, status e
                # cliente corretos. Não sobrescrever com a devolução.

        # 👇 1. Salva primeiro todos os movimentos pendentes na transação
        self.salvar_movimentos_banco()
        
        # 👇 2. Atualiza os equipamentos velhos que continuam quebrados (14 vai pra 8)
        if eqs_antigos_alterar_8:
            self.atualizar_equipamentos_banco(id_status_equipamento=8, lista_dicionarios=eqs_antigos_alterar_8)
            
        # 👇 3. Atualiza os equipamentos velhos que já foram salvos/consertados (Qualquer outra vai pra 1)
        if eqs_antigos_alterar_1:
            self.atualizar_equipamentos_banco(id_status_equipamento=1, lista_dicionarios=eqs_antigos_alterar_1)
            
        # 👇 4. Atualiza os equipamentos novos que foram alugados nesta substituição (vai pra 2)
        if eqs_novos_alterar:
            self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=eqs_novos_alterar)
            
        # 👇 5. Atualiza os saldos de contrato impactados
        self._atualizar_saldos_mysql()

# ==============================================================================
# WRAPPER 
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados
    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de SUBSTITUIÇÃO")
    print("="*70)
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)
    app_teste = MigracaoSubstituicao(eng_novo, eng_legado, dados_ram, start_counter=900000)
    app_teste.executar()
