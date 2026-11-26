import os
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from utils.sanetizador import executar_truncate_tabelas
from movimentos.migracao_movimentos import BaseMigracaoMovimento, limpar_codigo

TABELAS_MANUTENCAO = [
    'maintenance_items',
    'maintenances'
]

# ==============================================================================
# DICIONÁRIOS DE MAPEAMENTO (Conforme Documentação Visual)
# ==============================================================================

# Serviço legado -> maintenance_types do refatorado
#   1 REVISÃO_MANUTENÇÃO_OTRS  -> 8 REVISÃO_MANUTENÇÃO_OTRS
#   2 MANUTENÇÃO_REVISÃO_OTRS  -> 8 REVISÃO_MANUTENÇÃO_OTRS
#   3 UPGRADE                  -> 7 UPGRADE/DOWNGRADE
#   4 MONTAGEM                 -> 1 MONTAGEM
#   5 OTRS                     -> 6 OTRS
#  16 MANUTENÇÃO EXTERNA       -> 5 MANUTENÇÃO
#  17 BENCHMARK                -> 2 BENCHMARK
#  18 VISTORIA                 -> 3 VISTORIA
#  19 DESMONTAGEM              -> 4 DESMONTAGEM
#  20 MANUTENÇÃO               -> 5 MANUTENÇÃO
#  21 OTRS                     -> 6 OTRS
MAPA_MAINTENANCE_TYPE = {
    1: 8,
    2: 8,
    3: 7,
    4: 1,
    5: 6,
    16: 5,
    17: 2,
    18: 3,
    19: 4,
    20: 5,
    21: 6,
}

MAPA_TECHNICIAN_STATUS = {
    1: 1, 2: 2, 3: 3, 4: 4, 
    5: 5, 6: 6, 8: 7, 7: 8   
}

# Situações do legado cujo item é considerado concluído (regra histórica do módulo)
SITUACOES_TECNICAS_CONCLUIDAS = (1, 2, 3, 5)

class MigracaoManutencao(BaseMigracaoMovimento):

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=1):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente=False)
        
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
    # 1. EXTRAÇÃO (Histórico completo, independente da situação atual)
    # ==============================================================================
    def _extrair(self):
        print("📖 Extraindo histórico completo de manutenções do legado...")

        # Cabeçalhos: uma linha por aluguel_manutencao_movimento (sem join com itens,
        # para não multiplicar linhas). O equipamento é resolvido depois pelo id legado,
        # que é preservado em equipments.id.
        query_capas = """
            SELECT
                alma.id AS manutencao_id,
                alma.equipamento_id AS equipamento_id,
                eq.numero AS tombo,
                alma.cliente_id AS cliente,
                cli.orgao_id AS orgao_cliente_id,
                eq.orgao_id AS orgao_id_equip,
                eq.situacao_id AS situacao_equipamento,
                alma.usuario_id AS usuario,
                alma.manutencao_situacao_id AS technician_status,
                alma.motivo AS descricao_manutencao,
                alma.status AS status_legado,
                alma.created_at AS data_de_entrada,
                alma.updated_at,
                alma.deleted_at
            FROM aluguel_manutencao_movimento alma
            LEFT JOIN aluguel_equipamentos eq ON eq.id = alma.equipamento_id
            LEFT JOIN aluguel_clientes cli ON cli.id = alma.cliente_id
            WHERE alma.deleted_at IS NULL
            ORDER BY alma.created_at, alma.id
        """

        # Itens: uma linha por aluguel_manutencao_movimento_itens (sem duplicação).
        query_itens = """
            SELECT
                almai.id AS item_manutencao_id,
                almai.manutencao_movimento_id AS manutencao_id,
                almai.usuario_id AS usuario,
                almai.observacao AS descricao_item_manutencao,
                almai.manutencao_situacao_id AS technician_status,
                almai.manutencao_servico_id AS maintenance_types,
                almai.created_at,
                almai.updated_at,
                almai.deleted_at
            FROM aluguel_manutencao_movimento_itens almai
            WHERE almai.deleted_at IS NULL
            ORDER BY almai.manutencao_movimento_id, almai.id
        """

        with self.engine_legado.connect() as conn:
            df_capas = pd.read_sql(text(query_capas), conn)
            df_itens = pd.read_sql(text(query_itens), conn)

        with self.engine_new.connect() as conn:
            equipamentos_destino = {
                int(row[0]) for row in conn.execute(text("SELECT id FROM equipments"))
            }

        return df_capas, df_itens, equipamentos_destino

    # ==============================================================================
    # 2. TRANSFORMAÇÃO E CARGA
    # ==============================================================================
    def _resolver_equipamento(self, equipamento_id, tombo, equipamentos_destino):
        """Resolve o equipamento do destino.

        O id de aluguel_equipamentos é preservado em equipments.id, então o caminho
        principal é o vínculo direto. O tombo é apenas fallback (pode ser ambíguo).
        """
        if pd.notna(equipamento_id):
            try:
                equip_id = int(equipamento_id)
            except (TypeError, ValueError):
                equip_id = None
            if equip_id is not None and equip_id in equipamentos_destino:
                return equip_id

        tombo_limpo = limpar_codigo(tombo) if tombo is not None else None
        if not tombo_limpo:
            return None

        equip_id = self.dados['dict_equip_ref_por_number'].get(tombo_limpo)
        if equip_id is None or pd.isna(equip_id):
            return None
        equip_id = int(equip_id)
        return equip_id if equip_id in equipamentos_destino else None

    def _texto(self, *valores):
        for valor in valores:
            if pd.notna(valor):
                texto = str(valor).strip()
                if texto:
                    return texto
        return "Migração Automática do Legado"

    def _montar_batches(self, df_capas, df_itens, equipamentos_destino):
        """Monta as capas e itens em memória (sem tocar no banco)."""
        print(f"🧹 Transformando {len(df_capas)} manutenções e {len(df_itens)} itens...")

        maintenances_batch = []
        maintenance_items_batch = []
        equipamentos_para_atualizar = set()
        manutencoes_sem_equipamento = 0
        itens_sem_manutencao = 0

        id_maintenance_counter = 1
        id_item_counter = 1
        mapa_capas = {}

        # ------------------------------------------------------------------
        # CAPAS
        # ------------------------------------------------------------------
        for _, row in tqdm(df_capas.iterrows(), total=df_capas.shape[0], desc="Processando Manutenções"):
            legacy_id = row['manutencao_id']
            equip_id = self._resolver_equipamento(
                row['equipamento_id'], row.get('tombo'), equipamentos_destino
            )

            # Só cria a manutenção se o equipamento correspondente existir no destino.
            if equip_id is None:
                manutencoes_sem_equipamento += 1
                continue

            # O status do equipamento só é alterado para os que estão em manutenção hoje.
            if pd.notna(row['situacao_equipamento']) and int(row['situacao_equipamento']) == 3:
                equipamentos_para_atualizar.add(equip_id)

            tech_id = row['usuario']
            if pd.isna(tech_id) or int(tech_id) not in self.usuarios_validos:
                tech_id = 1
            tech_id = int(tech_id)

            dt_maintenance = row['data_de_entrada'] if pd.notna(row['data_de_entrada']) else self.now
            dt_created = dt_maintenance
            dt_updated = row['updated_at'] if pd.notna(row['updated_at']) else dt_maintenance
            dt_deleted = row['deleted_at'] if pd.notna(row['deleted_at']) else None

            motivo = row['descricao_manutencao']
            details_text = self._texto(motivo)

            # ------------------------------------------------------------------
            # APLICAÇÃO DA REGRA DE NEGÓCIO: ORGANIZAÇÃO (ORGAO_ID)
            # ------------------------------------------------------------------
            organization_id = self._descobrir_id_organizacao_destino(
                row['orgao_cliente_id'], row['orgao_id_equip']
            )

            # status legado: 1 = aberta/atual, 0 = fechada/histórica
            is_closed = 0 if pd.notna(row['status_legado']) and int(row['status_legado']) == 1 else 1

            maintenances_batch.append({
                "id": id_maintenance_counter,
                "maintenance_date": pd.Timestamp(dt_maintenance).date(),
                "created_by": tech_id,
                "equipment_id": equip_id,
                # Manutenção de equipamento não implica consumo de insumo.
                "product_item_id": None,
                "organization_id": organization_id,
                "product_quantity": None,
                "is_closed": is_closed,
                "details": details_text,
                "created_at": dt_created,
                "updated_at": dt_updated,
                "deleted_at": dt_deleted
            })
            mapa_capas[legacy_id] = id_maintenance_counter
            id_maintenance_counter += 1

        # ------------------------------------------------------------------
        # ITENS
        # ------------------------------------------------------------------
        for _, row in tqdm(df_itens.iterrows(), total=df_itens.shape[0], desc="Processando Itens"):
            legacy_mov_id = row['manutencao_id']
            maintenance_id = mapa_capas.get(legacy_mov_id)
            if maintenance_id is None:
                itens_sem_manutencao += 1
                continue

            tech_id = row['usuario']
            if pd.isna(tech_id) or int(tech_id) not in self.usuarios_validos:
                # fallback: usuário do cabeçalho
                tech_id = maintenances_batch[maintenance_id - 1]['created_by']
            tech_id = int(tech_id)

            leg_situacao = row['technician_status']
            leg_servico = row['maintenance_types']

            tech_status_id = MAPA_TECHNICIAN_STATUS.get(
                int(leg_situacao) if pd.notna(leg_situacao) else 0, 1
            )
            maint_type_id = MAPA_MAINTENANCE_TYPE.get(
                int(leg_servico) if pd.notna(leg_servico) else 0, 5
            )

            capa = maintenances_batch[maintenance_id - 1]
            if capa['is_closed']:
                # Manutenção fechada/histórica sempre concluída.
                maint_status_id = 2
            elif tech_status_id in SITUACOES_TECNICAS_CONCLUIDAS:
                maint_status_id = 2
            else:
                maint_status_id = 1

            dt_created = row['created_at'] if pd.notna(row['created_at']) else capa['created_at']
            dt_updated = row['updated_at'] if pd.notna(row['updated_at']) else dt_created
            dt_deleted = row['deleted_at'] if pd.notna(row['deleted_at']) else None

            maintenance_items_batch.append({
                "id": id_item_counter,
                "maintenance_id": maintenance_id,
                "movement_id": None,
                "maintenance_type_id": maint_type_id,
                "maintenance_status_id": maint_status_id,
                "technician_status_id": tech_status_id,
                "technician_id": tech_id,
                "details": self._texto(row['descricao_item_manutencao'],
                                       capa['details']),
                "created_at": dt_created,
                "updated_at": dt_updated,
                "deleted_at": dt_deleted
            })
            id_item_counter += 1

        return (
            maintenances_batch,
            maintenance_items_batch,
            equipamentos_para_atualizar,
            manutencoes_sem_equipamento,
            itens_sem_manutencao,
        )

    def _transformar_e_carregar(self, df_capas, df_itens, equipamentos_destino):
        (
            maintenances_batch,
            maintenance_items_batch,
            equipamentos_para_atualizar,
            manutencoes_sem_equipamento,
            itens_sem_manutencao,
        ) = self._montar_batches(df_capas, df_itens, equipamentos_destino)

        # ------------------------------------------------------------------
        # PERSISTÊNCIA NO BANCO
        # ------------------------------------------------------------------
        print(f"\n🚀 Inserindo {len(maintenances_batch)} capas e {len(maintenance_items_batch)} itens no banco...")
        if manutencoes_sem_equipamento:
            print(
                f"⚠️ {manutencoes_sem_equipamento} manutenções não migradas "
                "porque o equipamento não existe no destino."
            )
        if itens_sem_manutencao:
            print(
                f"⚠️ {itens_sem_manutencao} itens ignorados porque a manutenção "
                "correspondente não foi migrada."
            )
        with self.engine_new.begin() as conn:
            if maintenances_batch:
                pd.DataFrame(maintenances_batch).to_sql('maintenances', con=conn, if_exists='append', index=False)
            if maintenance_items_batch:
                pd.DataFrame(maintenance_items_batch).to_sql('maintenance_items', con=conn, if_exists='append', index=False)

            if equipamentos_para_atualizar:
                print(f"🔧 Modificando o status_id para 6 em {len(equipamentos_para_atualizar)} equipamentos...")
                ids_sql = "(" + ", ".join(map(str, sorted(equipamentos_para_atualizar))) + ")"
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
        
        df_capas, df_itens, equipamentos_destino = self._extrair()
        if df_capas.empty:
            print("\n⚠️ Nenhum registro de manutenção encontrado na origem.")
            return

        self._transformar_e_carregar(df_capas, df_itens, equipamentos_destino)
        print("\n✅ Migração de Manutenções Finalizada.")

# ==============================================================================
# WRAPPER (Ponte para o orquestrador/main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)
    app = MigracaoManutencao(eng_novo, eng_legado, dados_ram)
    app.executar()
