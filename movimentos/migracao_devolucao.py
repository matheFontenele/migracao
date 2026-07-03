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
        print("   📖 Extraindo movimentos de Devolução do legado...")
        query = """
            SELECT
                eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
                ac.id AS ID_CLIENTE, mov.id as MOVIMENTO_ID,
                mov.usuario_id, mov.updated_at, mov.deleted_at
            FROM aluguel_equipamentos eq
            INNER JOIN (
                SELECT mi.equipamento_id, MAX(m.id) as ultimo_movimento_id
                FROM aluguel_movimento_itens mi
                INNER JOIN aluguel_movimento m ON m.id = mi.movimento_id
                WHERE m.deleted_at IS NULL
                GROUP BY mi.equipamento_id
            ) ult_mov ON ult_mov.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ult_mov.ultimo_movimento_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL
              AND ac.deleted_at IS NULL
              AND eq.situacao_id = 14
        """
        with self.engine_legado.connect() as conn:
            return pd.read_sql(text(query), conn)

    def executar(self):
        print("\n" + "=" * 70)
        print("📦 MÓDULO: DEVOLUÇÃO (RECONSTRUÇÃO HISTÓRICA)")
        print("=" * 70)

        # 1. Extrai o DataFrame completo do Legado usando o seu SQL!
        df_devolucoes = self._extrair_dados_devolucao()
        if df_devolucoes.empty:
            print("⚠️ Nenhuma Devolução encontrada no banco legado.")
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
            # 🎯 REGRA REAPROVEITADA DO ALUGUEL: RECIPIENT_ID DO CLIENTE
            # =========================================================
            cli_legado_id = int(row['ID_CLIENTE']) if pd.notna(row['ID_CLIENTE']) else 0
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            
            if not recipient_id:
                rejeitados += 1
                continue

            # =========================================================
            # ROTEAMENTO INTELIGENTE (Qual base vai receber?)
            # =========================================================
            cli_legado_id = int(row['ID_CLIENTE']) if pd.notna(row['ID_CLIENTE']) else 0
            org_id_destino = descobrir_id_organizacao_destino(cli_legado_id)
            
            endereco_base_id = self.dict_enderecos_base_org.get(org_id_destino)
            
            # Fallback de segurança (Se falhar, vai para AS Sistemas - 1378)
            if not endereco_base_id:
                endereco_base_id = self.dict_enderecos_base_org.get(1115, 1) 
                org_id_destino = 1115

            # =========================================================
            # REGISTRAR A DEVOLUÇÃO (O PRESENTE)
            # =========================================================
            usr_id = int(row['usuario_id']) if pd.notna(row['usuario_id']) and row['usuario_id'] != 0 else 1
            dt_mov = row['updated_at'] if pd.notna(row['updated_at']) else self.now
            id_mov_legado = int(row['MOVIMENTO_ID'])

            # Chama a função e captura os IDs gerados em memória
            id_capa_dev, id_serv_item_dev, id_mov_item_dev = self.registrar_movimento(
                id_final=id_mov_legado,
                recipient_id=recipient_id, # 👈 Amarração resolvida no Cliente!
                cliente_final_address_id=endereco_base_id, # 👈 Endereço Físico na Base!
                usuario_id=usr_id,
                mov_date=dt_mov,
                deleted_at_mov=row['deleted_at'] if pd.notna(row['deleted_at']) else None,
                contrato_id=None,
                contrato_item_id=None,
                equipment_id_ref=equip_id_novo,
                tipo_movimento_id=3, # 3 = ID de Devolução
                operation_type='DEVOLUCAO',
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa="Migração: Devolução",
                details_item="Retorno para a Base"
            )

            # =========================================================
            # GERAR O SHIPMENT (GUIA DE TRANSPORTE)
            # =========================================================
            ship_id_atual = self.shipment_id_counter
            self.shipment_id_counter += 1
            
            self.shipments_mestre.append({
                "id": ship_id_atual,
                "status_id": 1,
                "created_by": 1,
                "created_at": dt_mov,
                "updated_at": dt_mov,
                "deleted_at": None
            })
            
            self.shipment_items_mestre.append({
                "id": self.shipment_item_id_counter,
                "shipment_id": ship_id_atual,
                "status_id": 1,
                "movement_item_id": id_mov_item_dev,
                "volume_id": None,
                "details": f"Devolução referente a movimento {id_mov_legado}",
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
        
        # O Status 1 na tabela de equipments representa "Em Estoque / Base"
        self.atualizar_equipamentos_banco(id_status_equipamento=8, lista_dicionarios=self.equipamentos_alterados)
# ==============================================================================
# WRAPPER (Ponte para a execução dinâmica do main.py no Modo Debug)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados

    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de DEVOLUÇÃO")
    print("="*70)

    # 1. Faxina pré-teste no banco novo
    print("\n🧹 Executando faxina e reset de saldos...")

    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)

    app_teste = MigracaoDevolucao(eng_novo, eng_legado, dados_ram, start_counter=1)
    app_teste.executar()