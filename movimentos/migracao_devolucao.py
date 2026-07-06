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
            max_ship = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM shipments")).scalar()
            max_ship_item = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM shipment_items")).scalar()

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
            
        self.shipment_id_counter = max_ship + 1
        self.shipment_item_id_counter = max_ship_item + 1
        
        # 2. Listas de Persistência Exclusivas
        self.shipments_mestre = []
        self.shipment_movements_mestre = []
        self.shipment_items_mestre = []

    def salvar_shipments_banco(self):
        if not self.shipments_mestre:
            return
            
        print(f"\n🚀 Persistindo guias de transporte (Shipments) no MySQL...")
        with self.engine_new.begin() as conn:
            pd.DataFrame(self.shipments_mestre).to_sql("shipments", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_movements_mestre).to_sql("shipment_movements", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_items_mestre).to_sql("shipment_items", con=conn, if_exists="append", index=False)
        print("   ✅ Shipments salvos com sucesso.")

    def _extrair_dados_devolucao(self):
        print("   📖 Extraindo histórico (Ordenação Cronológica via Pandas)...")
        
        # 1. Traz o histórico bruto, mas apenas dos equipamentos que estão devolvidos
        query = """
            SELECT
                eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
                mov.id AS MOVIMENTO_ID, ac.id AS CLIENTE_ID,
                COALESCE(NULLIF(mov.usuario_id, 0), 1) AS USR_ID,
                COALESCE(mov.updated_at, mov.data) AS DATA_MOV,
                mov.deleted_at AS DEL_MOV,
                mov.tipo_id AS TIPO_LEGADO,
                mov.data AS DATA_REAL_ORDENACAO
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens ami ON ami.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ami.movimento_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL
              AND mov.deleted_at IS NULL
              AND eq.situacao_id = 14
        """
        with self.engine_legado.connect() as conn:
            df = pd.read_sql(text(query), conn)

        if df.empty:
            return pd.DataFrame()

        print("   🧠 Ordenando a linha do tempo e validando as origens...")
        
        # 2. Ordena cronologicamente pela data real do fato lançado
        df.sort_values(by=['TOMBO', 'DATA_REAL_ORDENACAO', 'MOVIMENTO_ID'], ascending=[True, False, False], inplace=True)

        # 🎯 O PRESENTE: Pega o movimento absoluto MAIS RECENTE de todos (Que é o responsável pelo status 14)
        df_presente = df.groupby('TOMBO').first().reset_index()
        df_presente.rename(columns={
            'MOVIMENTO_ID': 'DEV_MOV_ID', 'CLIENTE_ID': 'DEV_CLIENTE_ID',
            'USR_ID': 'DEV_USR_ID', 'DATA_MOV': 'DEV_DATA', 'DEL_MOV': 'DEV_DEL'
        }, inplace=True)

        # 🎯 O PASSADO (A ORIGEM): Filtra SÓ por Aluguel(1) ou Reserva(7) no histórico, 
        # e então pega o MAIS RECENTE dentre eles (ignorando transferências intermediárias).
        df_historico_origens = df[df['TIPO_LEGADO'].isin([1, 7])]
        df_passado = df_historico_origens.groupby('TOMBO').first().reset_index()
        df_passado.rename(columns={
            'MOVIMENTO_ID': 'ORIG_MOV_ID', 'CLIENTE_ID': 'ORIG_CLIENTE_ID',
            'USR_ID': 'ORIG_USR_ID', 'DATA_MOV': 'ORIG_DATA', 'DEL_MOV': 'ORIG_DEL',
            'TIPO_LEGADO': 'ORIG_TIPO_LEGADO'
        }, inplace=True)

        # Mescla os dois momentos em uma única linha.
        # O 'inner' garante que só faremos a devolução se acharmos uma origem válida no histórico.
        df_final = pd.merge(
            df_presente, 
            df_passado[['TOMBO', 'ORIG_MOV_ID', 'ORIG_CLIENTE_ID', 'ORIG_USR_ID', 'ORIG_DATA', 'ORIG_DEL', 'ORIG_TIPO_LEGADO']], 
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
            org_id_destino = descobrir_id_organizacao_destino(cli_legado_id)
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
                cliente_final_address_id=cliente_final_address, # Vai para o endereço do cliente
                usuario_id=usr_origem,
                mov_date=dt_origem,
                deleted_at_mov=row['ORIG_DEL'] if pd.notna(row['ORIG_DEL']) else None,
                contrato_id=contrato_id_ativo,
                contrato_item_id=item_id_ativo,
                equipment_id_ref=equip_id_novo,
                tipo_movimento_id=tipo_mov_novo,
                operation_type=op_type,
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

            # Registra a Devolução e captura os IDs gerados
            id_capa_dev, id_serv_item_dev, id_mov_item_dev = self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id, 
                cliente_final_address_id=endereco_base_id, # Retorna para a base física
                usuario_id=usr_dev,
                mov_date=dt_dev,
                deleted_at_mov=row['DEV_DEL'] if pd.notna(row['DEV_DEL']) else None,
                contrato_id=None,
                contrato_item_id=None,
                equipment_id_ref=equip_id_novo,
                tipo_movimento_id=3, # 3 = ID de Devolução
                operation_type='DEVOLUCAO',
                organization_id=org_id_destino,
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa="Migração: Devolução",
                details_item="Retorno para a Base"
            )

            # =========================================================
            # 🚚 FASE 3: GERAR O SHIPMENT (GUIA DE TRANSPORTE DE VOLTA)
            # =========================================================
            # Blindagem local do usuário do Shipment para evitar quebras de FK externa
            usr_shipment = usr_dev if hasattr(self, 'usuarios_validos') and usr_dev in self.usuarios_validos else 1
            
            ship_id_atual = self.shipment_id_counter
            self.shipment_id_counter += 1
            
            self.shipments_mestre.append({
                "id": ship_id_atual,
                "status_id": 1,
                "created_by": usr_shipment,
                "created_at": dt_dev,
                "updated_at": dt_dev,
                "deleted_at": None
            })
            
            self.shipment_items_mestre.append({
                "id": self.shipment_item_id_counter,
                "shipment_id": ship_id_atual,
                "status_id": 1,
                "movement_item_id": id_mov_item_dev,
                "volume_id": None,
                "details": f"Devolução referente a movimento {id_mov_dev}",
                "address_id": endereco_base_id 
            })
            self.shipment_item_id_counter += 1

            self.shipment_movements_mestre.append({
                "shipment_id": ship_id_atual,
                "movement_id": id_capa_dev
            })

        # ==================================================================
        # FINALIZAÇÃO: SALVAR TUDO
        # ==================================================================
        if rejeitados > 0:
            print(f"\n⚠️ Equipamentos rejeitados (Não encontrados no banco novo): {rejeitados}")

        self.salvar_movimentos_banco()
        self.salvar_shipments_banco()
        
        # O Status 8 na tabela de equipments representa "Devolvido / Disponível na Base"
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