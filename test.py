import os
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from movimentos.migracao_movimentos import BaseMigracaoMovimento, carregar_dados_compartilhados, descobrir_id_organizacao_destino, limpar_codigo

class MigracaoDevolucao(BaseMigracaoMovimento):
    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=800000):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter)
        
        # 1. Contadores Inteligentes para Shipments
        with self.engine_new.connect() as conn:

            # 🎯 Extrai todos os IDs de Organização do config.py
            org_ids = [str(org['id']) for org in ENDERECOS_BASES]
            org_ids_sql = "(" + ", ".join(org_ids) + ")"
            
            # Busca os Endereços reais (PK) de TODAS as bases listadas
            query_enderecos = f"""
                SELECT addressable_id, MIN(id) as address_id 
                FROM addresses 
                WHERE addressable_type = 'organization' 
                AND addressable_id IN {org_ids_sql}
                GROUP BY addressable_id
            """
            result_ends = conn.execute(text(query_enderecos)).fetchall()
            
            # Dicionário em memória: {ID_DA_ORG: ID_DO_ENDERECO}
            self.dict_enderecos_base_org = {row.addressable_id: row.address_id for row in result_ends}
            

    def salvar_shipments_banco(self):
        if not self.shipments_mestre:
            return
            
        print(f"\n🚀 Persistindo guias de transporte no MySQL...")
        with self.engine_new.begin() as conn:
            pd.DataFrame(self.shipments_mestre).to_sql("shipments", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_movements_mestre).to_sql("shipment_movements", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_items_mestre).to_sql("shipment_items", con=conn, if_exists="append", index=False)
        print("   ✅ Shipments salvos com sucesso.")

    def _extrair_dados_devolucao(self):
        print("   📖 Extraindo O PRESENTE (Devoluções puras e deduplicadas)...")
        
        # ==================================================================
        # 1. QUERY OTIMIZADA: Pega apenas o movimento absoluto final de devolução
        # ==================================================================
        query_presente = """
            SELECT
                eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
                mov.id AS DEV_MOV_ID, ac.id AS DEV_CLIENTE_ID,
                ac.orgao_id as ORGAO_ID,
                COALESCE(NULLIF(mov.usuario_id, 0), 1) AS DEV_USR_ID,
                COALESCE(mov.updated_at, mov.data) AS DEV_DATA,
                mov.deleted_at AS DEV_DEL
            FROM aluguel_equipamentos eq
            INNER JOIN (
                SELECT mi.equipamento_id, MAX(m.id) as ultimo_mov_id
                FROM aluguel_movimento_itens mi
                INNER JOIN aluguel_movimento m ON m.id = mi.movimento_id
                WHERE m.deleted_at IS NULL
                GROUP BY mi.equipamento_id
            ) ult_mov ON ult_mov.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ult_mov.ultimo_mov_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL
              AND eq.situacao_id = 14
              AND mov.tipo_id = 2 -- 🎯 EXIGE QUE SEJA DEVOLUÇÃO PURA
        """
        with self.engine_legado.connect() as conn:
            df_presente = pd.read_sql(text(query_presente), conn)

        if df_presente.empty:
            return pd.DataFrame()
            
        tombos_presente = df_presente['TOMBO'].dropna().unique().tolist()
        tombos_sql = "(" + ", ".join([f"'{t}'" for t in tombos_presente]) + ")"

        print(f"   📖 Extraindo O PASSADO (Histórico de Origens para {len(tombos_presente)} equipamentos)...")

        # ==================================================================
        # 2. O PASSADO: Busca o histórico de Aluguel/Reserva só para esses equipamentos
        # ==================================================================
        query_passado = f"""
            SELECT
                eq.numero AS TOMBO,
                mov.id AS ORIG_MOV_ID, ac.id AS ORIG_CLIENTE_ID,
                ac.orgao_id as ORIG_ORGAO_ID,
                COALESCE(NULLIF(mov.usuario_id, 0), 1) AS ORIG_USR_ID,
                COALESCE(mov.updated_at, mov.data) AS ORIG_DATA,
                mov.deleted_at AS ORIG_DEL,
                mov.tipo_id AS ORIG_TIPO_LEGADO,
                mov.data AS DATA_REAL_ORDENACAO
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens ami ON ami.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ami.movimento_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL
              AND mov.deleted_at IS NULL
              AND eq.numero IN {tombos_sql}
              AND mov.tipo_id IN (1, 7)
        """
        with self.engine_legado.connect() as conn:
            df_historico_origens = pd.read_sql(text(query_passado), conn)

        print("   🧠 Cruzando as linhas temporais...")
        
        # Ordena cronologicamente e pega a origem válida mais recente de cada máquina
        df_historico_origens.sort_values(by=['TOMBO', 'DATA_REAL_ORDENACAO', 'ORIG_MOV_ID'], ascending=[True, False, False], inplace=True)
        df_passado = df_historico_origens.groupby('TOMBO').first().reset_index()

        # ==================================================================
        # 3. MERGE: Junta o Passado e o Presente na mesma linha
        # ==================================================================
        df_final = pd.merge(
            df_presente, 
            df_passado[['TOMBO', 'ORIG_MOV_ID', 'ORIG_CLIENTE_ID', 'ORIG_ORGAO_ID', 'ORIG_USR_ID', 'ORIG_DATA', 'ORIG_DEL', 'ORIG_TIPO_LEGADO']], 
            on='TOMBO', 
            how='inner' 
        )

        return df_final

    def executar(self):
        print("\n" + "=" * 70)
        print("📦 MÓDULO: DEVOLUÇÃO (RECONSTRUÇÃO BIFÁSICA)")
        print("=" * 70)

        # 1. Extrai o DataFrame estruturado com Presente e Passado na mesma linha
        df_devolucoes = self._extrair_dados_devolucao()
        if df_devolucoes.empty:
            print("⚠️ Nenhuma Devolução válida com histórico de aluguel/reserva encontrada.")
            return

        # 2. Busca os IDs do banco novo baseados nos tombos extraídos
        tombos = df_devolucoes['TOMBO'].dropna().unique().tolist()
        dict_equip_novo = self.buscar_equipamentos_novo_por_tombo(tombos)
        print(f"   ✅ {len(dict_equip_novo)} equipamentos validados no banco novo.")

        rejeitados = 0

        # 3. Iteração Principal
        for _, row in tqdm(df_devolucoes.iterrows(), total=df_devolucoes.shape[0], desc="Processando Devoluções"):
            tombo = limpar_codigo(row['TOMBO'])
            equip_id_novo = dict_equip_novo.get(tombo)
            
            if not equip_id_novo:
                rejeitados += 1
                continue

            # =========================================================
            # 🎯 RESOLVENDO O CLIENTE E A ORGANIZAÇÃO (Roteamento)
            # =========================================================
            # Prioriza o cliente da transação original (Passado) para amarrar os contratos corretos
            if pd.notna(row.get('ORIG_CLIENTE_ID')):
                cli_legado_id = int(row['ORIG_CLIENTE_ID'])
            else:
                cli_legado_id = int(row['DEV_CLIENTE_ID']) if pd.notna(row.get('DEV_CLIENTE_ID')) else 0
                
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            cliente_final_address = self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id)
            
            if not recipient_id:
                rejeitados += 1
                continue

            # =========================================================
            # ROTEAMENTO INTELIGENTE FISICO (Qual base vai receber o frete?)
            # =========================================================
            orgao_id_legado = row['ORIG_ORGAO_ID'] if pd.notna(row.get('ORIG_ORGAO_ID')) else row['ORGAO_ID']
            org_id_destino = descobrir_id_organizacao_destino(orgao_id_legado)
            endereco_base_id = self.dict_enderecos_base_org.get(org_id_destino)
            
            # Fallback de segurança (Se falhar, vai para a Base Principal - 1115)
            if not endereco_base_id:
                endereco_base_id = self.dict_enderecos_base_org.get(1115, 1) 
                org_id_destino = 1115

            # Carrega contratos de Fallback para a Fase 1 e Fase 2
            contrato_id_ativo = self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            item_id_ativo = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)

            # =========================================================
            # 🕰️ FASE 1: RECONSTRUIR O PASSADO (ALUGUEL / RESERVA)
            # =========================================================
            id_mov_origem = int(row['ORIG_MOV_ID'])
            tipo_legado_origem = int(row['ORIG_TIPO_LEGADO'])
            dt_origem = row['ORIG_DATA']
            usr_origem = int(row['ORIG_USR_ID'])
            
            if tipo_legado_origem == 1:
                tipo_mov_novo = 1
                op_type = 'ALUGUEL'
            else:
                tipo_mov_novo = 4
                op_type = 'RESERVA'

            self.registrar_movimento(
                id_final=id_mov_origem,
                recipient_id=recipient_id,
                cliente_final_address_id=cliente_final_address,
                usuario_id=usr_origem,
                mov_date=dt_origem,
                deleted_at_mov=row['ORIG_DEL'] if pd.notna(row['ORIG_DEL']) else None,

                contrato_id=contrato_id_ativo,
                contrato_item_id=item_id_ativo,
                equipment_id_ref=equip_id_novo,
                type_id_ref=None,     # 🎯 PREVENÇÃO DE BUG DA CLASSE PAI
                product_id_ref=None,  # 🎯 PREVENÇÃO DE BUG DA CLASSE PAI
                
                status_shipment=2,
                tipo_movimento_id=tipo_mov_novo,
                operation_type=op_type,

                status_equipment_id=2, 
                history_reason='SHIPPING_CONFIRMED_SEPARATE',
                
                organization_id=org_id_destino,
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa=f"Migração (Reconstrução): {op_type} Histórico",
                details_item="Alocado no Cliente (Histórico)"
            )

            # =========================================================
            # 📦 FASE 2: REGISTRAR A DEVOLUÇÃO (O PRESENTE)
            # =========================================================
            id_mov_dev = int(row['DEV_MOV_ID'])
            dt_dev = row['DEV_DATA']
            usr_dev = int(row['DEV_USR_ID'])

            self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id, 
                cliente_final_address_id=endereco_base_id,
                usuario_id=usr_dev,
                mov_date=dt_dev,
                deleted_at_mov=row['DEV_DEL'] if pd.notna(row['DEV_DEL']) else None,
                
                contrato_id=None,
                contrato_item_id=None,
                equipment_id_ref=equip_id_novo,
                type_id_ref=None,     # 🎯 PREVENÇÃO DE BUG DA CLASSE PAI
                product_id_ref=None,  # 🎯 PREVENÇÃO DE BUG DA CLASSE PAI
                
                status_equipment_id=8,
                history_reason='RECEIPT_CONFIRMED_RETURN',
                
                status_shipment=1, 
                tipo_movimento_id=3,
                operation_type='DEVOLUCAO',
                
                organization_id=org_id_destino,
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa="Migração: Devolução",
                details_item="Retorno para a Base"
            )

        # ==================================================================
        # FINALIZAÇÃO: SALVAR TUDO
        # ==================================================================
        if rejeitados > 0:
            print(f"\n⚠️ Equipamentos rejeitados (Não encontrados no banco novo): {rejeitados}")

        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=8, lista_dicionarios=self.equipamentos_alterados)

# ==============================================================================
# WRAPPER (Ponte para a execução dinâmica do main.py no Modo Debug)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados

    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de DEVOLUÇÃO")
    print("="*70)

    print("\n🧹 Executando faxina e reset de saldos...")

    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)

    app_teste = MigracaoDevolucao(eng_novo, eng_legado, dados_ram, start_counter=1)
    app_teste.executar()