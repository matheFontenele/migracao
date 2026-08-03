import os
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from utils.sanetizador import executar_truncate_tabelas
from movimentos.migracao_movimentos import BaseMigracaoMovimento

TABELAS_MANUTENCAO = [
    'maintenance_items',
    'maintenances'
]

# ==============================================================================
# DICIONÁRIOS DE MAPEAMENTO (Conforme Documentação Visual)
# ==============================================================================

MAPA_MAINTENANCE_TYPE = {
    4: 1, 1: 1, 2: 1,
    17: 2,
    18: 3,
    19: 4,
    20: 5, 3: 5, 16: 5,
    21: 6
}

MAPA_TECHNICIAN_STATUS = {
    1: 1, 2: 2, 3: 3, 4: 4, 
    5: 5, 6: 6, 8: 7, 7: 8   
}

class MigracaoManutencao(BaseMigracaoMovimento):

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=1):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente=False)
        
        # Carrega apenas o que é exclusivo da manutenção
        with self.engine_new.connect() as conn:
            df_equipments = pd.read_sql("SELECT id, product_item_id FROM equipments", conn)
            self.dict_product_items = dict(zip(df_equipments['id'], df_equipments['product_item_id']))

    # ==============================================================================
    # HELPER DE VALIDAÇÃO DA ORGANIZAÇÃO (Regra de Negócio)
    # ==============================================================================
    def _descobrir_id_organizacao_destino(self, orgao_cliente, orgao_equip):
        # Define a prioridade: Org do Cliente > Org do Equipamento
        id_escolhido = orgao_cliente if pd.notna(orgao_cliente) else orgao_equip
        
        if pd.isna(id_escolhido): 
            return 1115
            
        try:
            id_legado_int = int(id_escolhido)
        except (ValueError, TypeError):
            return 1115

        from config.config import MAPPING_ALUCOM, MAPPING_AS, MAPPING_IP, MAPPING_MOREIA, MAPPING_SC
        
        if id_legado_int in MAPPING_ALUCOM: return 1115
        if id_legado_int in MAPPING_IP: return 1311 
        if id_legado_int in MAPPING_MOREIA: return 1122
        if id_legado_int in MAPPING_AS: return 1378 
        if id_legado_int in MAPPING_SC: return 1115  
        
        return 1115 # Fallback geral caso não encontre nas regras mapeadas

    # ==============================================================================
    # 1. EXTRAÇÃO (Query Atualizada)
    # ==============================================================================
    def _extrair(self):
        print("📖 Extraindo dados de manutenção do legado...")
        
        query = """
            SELECT
                alq.id AS id_equipamento,
                alq.numero AS tombo,
                alq.nome AS nome,
                alt.id AS tipo_id,
                alt.nome AS tipo,
                alq.situacao_id,
                asi.nome AS situacao,
                alma.id AS manutencao_id,
                alma.cliente_id AS cliente,
                cli.orgao_id AS orgao_cliente_id,
                alq.orgao_id AS orgao_id_equip,
                alma.usuario_id AS usuario,
                alma.situacao_anterior_id AS situacao_anterior_id,
                alma.manutencao_situacao_id AS technician_status,
                alma.motivo AS descricao_manutencao,
                almai.id AS item_manutencao_id,
                almai.observacao AS descricao_item_manutencao,
                almai.manutencao_servico_id AS maintenance_types,
                alma.created_at AS data_de_entrada,
                alma.updated_at,
                alma.deleted_at
            FROM aluguel_equipamentos alq
            INNER JOIN aluguel_manutencao_movimento alma ON alq.id = alma.equipamento_id
            LEFT JOIN aluguel_manutencao_movimento_itens almai ON alma.id = almai.manutencao_movimento_id
            INNER JOIN aluguel_situacao asi ON alq.situacao_id = asi.id
            INNER JOIN aluguel_tipos alt ON alq.tipo_id = alt.id
            LEFT JOIN aluguel_clientes cli ON alma.cliente_id = cli.id
            WHERE alma.deleted_at IS NULL AND asi.id = 3
        """
        with self.engine_legado.connect() as conn:
            return pd.read_sql(text(query), conn)

    # ==============================================================================
    # 2. TRANSFORMAÇÃO E CARGA
    # ==============================================================================
    def _transformar_e_carregar(self, df_bruto):
        print(f"🧹 Transformando e mapeando {len(df_bruto)} registros de manutenção...")
        
        maintenances_batch = []
        maintenance_items_batch = []
        equipamentos_para_atualizar = set()
        
        id_maintenance_counter = 1
        id_item_counter = 1
        manutencoes_processadas = set()

        for _, row in tqdm(df_bruto.iterrows(), total=df_bruto.shape[0], desc="Processando Manutenções"):
            legacy_id = row['manutencao_id']
            equip_id = row['id_equipamento']
            
            if equip_id not in self.dict_product_items:
                continue
                
            # Adiciona o equipamento na lista para atualizar o status no final
            equipamentos_para_atualizar.add(equip_id)

            tech_id = row['usuario']
            if pd.isna(tech_id) or tech_id not in self.usuarios_validos:
                tech_id = 1  
                
            dt_maintenance = row['data_de_entrada'] if pd.notna(row['data_de_entrada']) else self.now
            dt_created = dt_maintenance
            dt_updated = row['updated_at'] if pd.notna(row['updated_at']) else dt_maintenance
            dt_deleted = row['deleted_at'] if pd.notna(row['deleted_at']) else None
            
            motivo = str(row['descricao_manutencao']).strip() if pd.notna(row['descricao_manutencao']) else ""
            obs = str(row['descricao_item_manutencao']).strip() if pd.notna(row['descricao_item_manutencao']) else ""
            
            if obs and motivo and obs != motivo:
                details_text = f"Motivo: {motivo} | Obs: {obs}"
            elif obs:
                details_text = obs
            elif motivo:
                details_text = motivo
            else:
                details_text = "Migração Automática do Legado"

            # ------------------------------------------------------------------
            # APLICAÇÃO DA REGRA DE NEGÓCIO: ORGANIZAÇÃO (ORGAO_ID)
            # ------------------------------------------------------------------
            org_cliente = row['orgao_cliente_id']
            org_equip = row['orgao_id_equip']
            organization_id = self._descobrir_id_organizacao_destino(org_cliente, org_equip)

            # ------------------------------------------------------------------
            # MONTAGEM DA CAPA
            # ------------------------------------------------------------------
            if legacy_id not in manutencoes_processadas:
                maintenances_batch.append({
                    "id": id_maintenance_counter,
                    "maintenance_date": dt_maintenance,
                    "created_by": tech_id,
                    "equipment_id": equip_id,
                    "product_item_id": self.dict_product_items.get(equip_id),
                    "organization_id": organization_id,
                    "product_quantity": 1,
                    "is_closed": 0,  
                    "details": details_text,
                    "created_at": dt_created,
                    "updated_at": dt_updated,
                    "deleted_at": dt_deleted
                })
                manutencoes_processadas.add(legacy_id)
                current_maintenance_id = id_maintenance_counter
                id_maintenance_counter += 1
            else:
                current_maintenance_id = id_maintenance_counter - 1 

            # ------------------------------------------------------------------
            # MONTAGEM DO ITEM
            # ------------------------------------------------------------------
            leg_situacao = row['technician_status']
            leg_servico = row['maintenance_types']
            
            tech_status_id = MAPA_TECHNICIAN_STATUS.get(int(leg_situacao) if pd.notna(leg_situacao) else 0, 1)
            maint_type_id = MAPA_MAINTENANCE_TYPE.get(int(leg_servico) if pd.notna(leg_servico) else 0, 5)

            if tech_status_id in (1, 2, 3, 5):
                maint_status_id = 2
            else:
                maint_status_id = 1

            maintenance_items_batch.append({
                "id": id_item_counter,
                "maintenance_id": current_maintenance_id,
                "movement_id": None,
                "maintenance_type_id": maint_type_id,
                "maintenance_status_id": maint_status_id,
                "technician_status_id": tech_status_id,
                "technician_id": tech_id,
                "details": details_text,
                "created_at": dt_created,
                "updated_at": dt_updated,
                "deleted_at": dt_deleted
            })
            id_item_counter += 1

        # ------------------------------------------------------------------
        # PERSISTÊNCIA NO BANCO
        # ------------------------------------------------------------------
        print(f"\n🚀 Inserindo {len(maintenances_batch)} capas e {len(maintenance_items_batch)} itens no banco...")
        with self.engine_new.begin() as conn:
            if maintenances_batch:
                pd.DataFrame(maintenances_batch).to_sql('maintenances', con=conn, if_exists='append', index=False)
            if maintenance_items_batch:
                pd.DataFrame(maintenance_items_batch).to_sql('maintenance_items', con=conn, if_exists='append', index=False)
                
            if equipamentos_para_atualizar:
                print(f"🔧 Modificando o status_id para 6 em {len(equipamentos_para_atualizar)} equipamentos...")
                ids_sql = "(" + ", ".join(map(str, equipamentos_para_atualizar)) + ")"
                conn.execute(text(f"UPDATE equipments SET status_id = 6 WHERE id IN {ids_sql}"))

    # ==============================================================================
    # ORQUESTRAÇÃO
    # ==============================================================================
    def executar(self):
        print("\n" + "="*70)
        print("🔧 MÓDULO: MIGRAÇÃO DE MANUTENÇÕES")
        print("="*70)
        
        print("\n🧹 Limpando tabelas de manutenção no destino...")
        executar_truncate_tabelas(self.engine_new, TABELAS_MANUTENCAO)
        
        df_bruto = self._extrair()
        if not df_bruto.empty:
            self._transformar_e_carregar(df_bruto)
            print("\n✅ Migração de Manutenções Finalizada.")
        else:
            print("\n⚠️ Nenhum registro encontrado na origem com a situação 3 (em manutenção).")

# ==============================================================================
# WRAPPER (Ponte para o orquestrador/main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)
    app = MigracaoManutencao(eng_novo, eng_legado, dados_ram)
    app.executar()