import os
import glob
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from config.parquet_excecoes import normalizar_tombo, tombos_priorizados
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino, limpar_codigo


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
        df_passado = df_historico_origens.groupby('EQUIPAMENTO_ID_ANTIGO').first().reset_index()

        # A fase de aluguel histórico é opcional: devoluções de equipamentos
        # sem aluguel de origem (tipo 1/7) no legado continuam válidas.
        return pd.merge(df_presente, df_passado[['EQUIPAMENTO_ID_ANTIGO', 'ORIG_MOV_ITEM_ID', 'ORIG_MOV_ID', 'ORIG_CLIENTE_ID', 'ORIG_USR_ID', 'ORIG_DATA', 'ORIG_DEL', 'ORIG_TIPO_LEGADO', 'ORIG_ORGAO_ID']], on='EQUIPAMENTO_ID_ANTIGO', how='left')

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
            
        df_subst = self._extrair_dados_substituicao()
        if df_subst.empty:
            print("⚠️ Nenhuma Substituição válida encontrada no legado.")
            return

        tombos_todos = list(set(df_subst['TOMBO_NOVO'].tolist() + df_subst['TOMBO_ANTIGO'].tolist()))
        dict_equip_novo = self.buscar_equipamentos_novo_por_tombo(tombos_todos)
        
        rejeitados = 0
        historicos_sem_destino = 0
        log_nao_match = []
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

            (contrato_id_res, item_id_res, is_avulso, is_kit, is_excedente, teve_match_perfeito, motivo_divergencia) = self.regras_item_contratos(
                csv_contract_id, csv_item_id, eq_id_novo or eq_id_antigo, recipient_id, self.dict_is_kit,
                abater_saldo=processar_envio
            )

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
            org_origem = descobrir_id_organizacao_destino(row['ORIG_ORGAO_ID']) if pd.notna(row.get('ORIG_ORGAO_ID')) else org_id_cascata

            # =========================================================
            # 🕰️ FASE 1: O PASSADO (ALUGAMOS A MÁQUINA ANTIGA)
            # =========================================================
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
                        contrato_id=contrato_id_res,
                        contrato_item_id=item_id_res,
                        equipment_id_ref=eq_id_antigo,
                        status_shipment=2, 
                        tipo_movimento_id=7 if is_avulso else 1,
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
