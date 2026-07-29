import os
import glob
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino, limpar_codigo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_PARQUETS = "./docs/parquets"
ARQUIVO_TYPES = "./docs/types.csv"

class MigracaoSubstituicao(BaseMigracaoMovimento):

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
        print("   📖 Extraindo O PRESENTE (Pareamento exato por TIPO de Equipamento)...")

        query_presente = """
            WITH devolucoes AS (
                SELECT
                    sub.id AS substituicao_id,
                    mov.id AS movimento_id,
                    mov.data AS data_mov,
                    mov.cliente_id,
                    mov.usuario_id,
                    mov.deleted_at,
                    eq.numero AS tombo,
                    eq.nome,
                    eq.tipo_id,
                    ROW_NUMBER() OVER(PARTITION BY sub.id, eq.tipo_id ORDER BY eq.numero) as par_index
                FROM aluguel_substituicao sub
                INNER JOIN aluguel_movimento mov ON sub.substituicao_devolucao_id = mov.id
                INNER JOIN aluguel_movimento_itens movi ON mov.id = movi.movimento_id
                INNER JOIN aluguel_equipamentos eq ON movi.equipamento_id = eq.id
                WHERE mov.deleted_at IS NULL AND eq.deleted_at IS NULL
            ),
            alugueis AS (
                SELECT
                    sub.id AS substituicao_id,
                    mov.id AS movimento_id,
                    mov.data AS data_mov,
                    mov.cliente_id,
                    mov.usuario_id,
                    mov.deleted_at,
                    eq.numero AS tombo,
                    eq.nome,
                    eq.tipo_id,
                    ROW_NUMBER() OVER(PARTITION BY sub.id, eq.tipo_id ORDER BY eq.numero) as par_index
                FROM aluguel_substituicao sub
                INNER JOIN aluguel_movimento mov ON sub.substituicao_aluguel_id = mov.id
                INNER JOIN aluguel_movimento_itens movi ON mov.id = movi.movimento_id
                INNER JOIN aluguel_equipamentos eq ON movi.equipamento_id = eq.id
                WHERE mov.deleted_at IS NULL AND eq.deleted_at IS NULL
            )
            SELECT
                d.substituicao_id,
                d.cliente_id AS CLIENTE_ID,
                COALESCE(NULLIF(d.usuario_id, 0), 1) AS USR_SUBST,
                COALESCE(a.data_mov, d.data_mov) AS DATA_SUBST,
                d.deleted_at AS DEL_SUBST,

                d.movimento_id AS MOV_DEV_ID,
                d.tombo AS TOMBO_ANTIGO,
                d.nome AS NOME_ANTIGO,
                d.tipo_id AS TIPO_ANTIGO,

                a.movimento_id AS MOV_NOVO_ID,
                a.tombo AS TOMBO_NOVO,
                a.nome AS NOME_NOVO,
                a.tipo_id AS TIPO_NOVO
            FROM devolucoes d
            INNER JOIN alugueis a 
                ON d.substituicao_id = a.substituicao_id 
                AND d.tipo_id = a.tipo_id 
                AND d.par_index = a.par_index
        """
        with self.engine_legado.connect() as conn:
            df_presente = pd.read_sql(text(query_presente), conn)

        if df_presente.empty: return pd.DataFrame()

        tombos_antigos = df_presente['TOMBO_ANTIGO'].dropna().unique().tolist()
        tombos_sql = "(" + ", ".join([f"'{t}'" for t in tombos_antigos]) + ")"

        print(f"   📖 Extraindo O PASSADO (Histórico de Origens para {len(tombos_antigos)} equipamentos antigos)...")
        query_passado = f"""
            SELECT eq.numero AS TOMBO_ANTIGO, mov.id AS ORIG_MOV_ID, ac.id AS ORIG_CLIENTE_ID, COALESCE(NULLIF(mov.usuario_id, 0), 1) AS ORIG_USR_ID, COALESCE(mov.updated_at, mov.data) AS ORIG_DATA, mov.deleted_at AS ORIG_DEL, mov.tipo_id AS ORIG_TIPO_LEGADO, mov.data AS DATA_REAL_ORDENACAO
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens ami ON ami.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ami.movimento_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL AND mov.deleted_at IS NULL AND eq.numero IN {tombos_sql} AND mov.tipo_id IN (1, 7)
        """
        with self.engine_legado.connect() as conn:
            df_historico_origens = pd.read_sql(text(query_passado), conn)

        df_historico_origens.sort_values(by=['TOMBO_ANTIGO', 'DATA_REAL_ORDENACAO', 'ORIG_MOV_ID'], ascending=[True, False, False], inplace=True)
        df_passado = df_historico_origens.groupby('TOMBO_ANTIGO').first().reset_index()

        return pd.merge(df_presente, df_passado[['TOMBO_ANTIGO', 'ORIG_MOV_ID', 'ORIG_CLIENTE_ID', 'ORIG_USR_ID', 'ORIG_DATA', 'ORIG_DEL', 'ORIG_TIPO_LEGADO']], on='TOMBO_ANTIGO', how='inner')

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
        log_nao_match = []
        eqs_antigos_alterar = []
        eqs_novos_alterar = []

        for _, row in tqdm(df_subst.iterrows(), total=df_subst.shape[0], desc="Processando Ciclos"):
            
            tombo_novo = limpar_codigo(row['TOMBO_NOVO'])
            tombo_antigo = limpar_codigo(row['TOMBO_ANTIGO'])
            
            if tombo_novo not in dict_ultimo_mov_subst:
                rejeitados += 1
                continue
                
            mov_novo_id = int(row['MOV_NOVO_ID'])
            eq_id_novo = dict_equip_novo.get(tombo_novo)
            eq_id_antigo = dict_equip_novo.get(tombo_antigo)
            
            if not eq_id_novo or not eq_id_antigo:
                rejeitados += 1
                continue

            row_parquet = dict_row_parquet_by_tombo.get(tombo_novo)
            cli_leg_parquet = row_parquet.get("CLIENTE_ID") if row_parquet is not None else None
            
            if pd.notna(cli_leg_parquet) and str(cli_leg_parquet).strip() != '':
                cli_legado_id = int(float(cli_leg_parquet))
            elif pd.notna(row.get('ORIG_CLIENTE_ID')):
                cli_legado_id = int(row['ORIG_CLIENTE_ID'])
            else:
                cli_legado_id = int(row['CLIENTE_ID']) if pd.notna(row['CLIENTE_ID']) else 0

            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            cliente_final_address = self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id)
            
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
                csv_contract_id, csv_item_id, eq_id_novo, recipient_id, self.dict_is_kit, 
                abater_saldo=True
            )

            if motivo_divergencia:
                status_final_log = "AVULSO" if is_avulso else "KIT (IMUNE)" if is_kit else "EXCEDENTE" if is_excedente else "NORMAL"
                log_nao_match.append({
                    "TOMBO": tombo_novo, "ID_CLIENTE_LEGADO": cli_legado_id, "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRACT_ID_CSV": csv_contract_id, "CONTRATO_RESOLVIDO": contrato_id_res,
                    "ITEM_RESOLVIDO_ID": item_id_res, "STATUS_FINAL": status_final_log, "MOTIVO_EXATO": motivo_divergencia
                })

            org_id_cascata = dict_contract_org.get(contrato_id_res) or dict_customer_org.get(recipient_id) or dict_equip_org.get(eq_id_novo) or 1115

            # =========================================================
            # 🕰️ FASE 1: O PASSADO (ALUGAMOS A MÁQUINA ANTIGA)
            # =========================================================
            if pd.notna(row.get('ORIG_MOV_ID')):
                id_legado_origem = int(row['ORIG_MOV_ID'])
                self.registrar_movimento(
                    id_final=int(row['ORIG_MOV_ID']),
                    recipient_id=recipient_id,
                    cliente_final_address_id=cliente_final_address,
                    usuario_id=int(row['ORIG_USR_ID']),
                    organization_id=int(org_id_cascata),
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

            # =========================================================
            # 📥 FASE 2: O PRESENTE (RECOLHEMOS A MÁQUINA ANTIGA)
            # =========================================================
            id_mov_dev = int(row['MOV_DEV_ID'])
            _, _, id_mov_item_dev = self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id,
                cliente_final_address_id=endereco_base_id,
                usuario_id=int(row['USR_SUBST']),
                organization_id=int(org_id_cascata),
                mov_date=row['DATA_SUBST'],
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
            eqs_antigos_alterar.append({int(eq_id_antigo): int(id_mov_item_dev)})

            # =========================================================
            # 📤 FASE 3: O PRESENTE (ENVIAMOS A MÁQUINA NOVA)
            # =========================================================
            _, _, id_mov_item_novo = self.registrar_movimento(
                id_final=mov_novo_id,
                recipient_id=recipient_id,
                cliente_final_address_id=cliente_final_address,
                usuario_id=int(row['USR_SUBST']),
                organization_id=int(org_id_cascata),
                mov_date=row['DATA_SUBST'],
                deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=eq_id_novo, 
                status_shipment=2, 
                tipo_movimento_id=1, 
                operation_type='ALUGUEL',
                status_equipment_id=2,
                history_reason='SHIPPING_CONFIRMED_RENT',
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

        if log_nao_match:
            pd.DataFrame(log_nao_match).to_csv("log_divergencias_substituicao.csv", index=False)

        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=8, lista_dicionarios=eqs_antigos_alterar)
        self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=eqs_novos_alterar)
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