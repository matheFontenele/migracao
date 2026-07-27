import os
import glob
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from movimentos.migracao_movimentos import carregar_dados_compartilhados, resetar_saldo_contract_items
from utils.sanetizador import executar_truncate_tabelas
from movimentos.migracao_movimentos import BaseMigracaoMovimento

TABELAS = [
    "service_order_item_extra_equipments", "movement_items", "movements", "service_order_items", "service_orders"
]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class MigracaoAluguel(BaseMigracaoMovimento):

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=1, limpar_ambiente=True):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente)
        self.saldos_modificados = set()

    def limpar_tabelas_movimento(self):
        if self.limpar_ambiente:
            print("\n🧹 [ALUGUEL] Iniciando faxina estrutural nas tabelas transacionais...")
            executar_truncate_tabelas(self.engine_new, TABELAS)  

    def calcular_saldo(self, *args, **kwargs):
        """Dummy fantasma: A matemática do saldo do Excedente ocorre na classe Pai!"""
        return 0, None, int(args[0]) if pd.notna(args[0]) else None
    
    def _atualizar_saldos_mysql(self):
        if getattr(self, 'consumir_saldos', None) is False or not self.saldos_modificados: return
        print(f"\n💾 Sincronizando {len(self.saldos_modificados)} saldos modificados com o MySQL...")
        atualizados = 0
        with self.engine_new.begin() as conn:
            for c_id in self.saldos_modificados:
                qtd_final_banco = max(0, int(self.dados["saldos_por_id"][c_id]))
                res = conn.execute(text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), {"nova_qtd": qtd_final_banco, "id": c_id})
                if res.rowcount > 0: atualizados += 1
        print(f"  ✔️ {atualizados} itens de contrato atualizados com sucesso!")
    
    def executar(self):
        print("\n" + "-" * 70)
        print("📦 MÓDULO: ALUGUEL (Fonte: CSV Parquet)")
        print("-" * 70)

        self.limpar_tabelas_movimento()
        
        caminho_types = "./docs/types.csv"
        if os.path.exists(caminho_types):
            print("📖 Carregando mapeamento de Tipos e Kits (types.csv)...")
            self.dict_is_kit = {int(row['id']): int(row['is_kit']) for _, row in pd.read_csv(caminho_types).iterrows() if pd.notna(row['id'])}
        else:
            print("⚠️ Arquivo types.csv não encontrado. Todos os equipamentos assumirão is_kit = 0.")
            self.dict_is_kit = {}

        arquivos_parquet = glob.glob(os.path.join("./docs/parquets", "*.parquet"))
        if not arquivos_parquet: return

        print(f"📖 Lendo dados de {len(arquivos_parquet)} arquivos Parquet...")
        df_csv = pd.concat([pd.read_parquet(arq).rename(columns=str.upper) for arq in arquivos_parquet], ignore_index=True)
        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        if 'ITEM_DO_CONTRATO' in df_csv.columns:
            df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
            df_csv = df_csv[df_csv['ITEM_DO_CONTRATO'].str.lower() != 'nan']
        df_csv['CONTRACT_ID'] = df_csv['CONTRACT_ID'].astype(str).str.replace('.0', '', regex=False)
        
        tombos = df_csv['TOMBO'].unique().tolist()
        dict_ultimo_mov = self.buscar_ultimo_movimento_por_tombo(tombos)
        dict_equipamentos_novo = self.buscar_equipamentos_novo_por_tombo(tombos)

        with self.engine_new.connect() as conn:
            dict_contract_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM contracts", conn).values.T))
            dict_customer_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM customers", conn).values.T))
            dict_equip_org = dict(zip(*pd.read_sql("SELECT id, current_organization_id FROM equipments", conn).values.T))

        log_nao_match = []
        rejeitados = 0
        
        for _, row_csv in tqdm(df_csv.iterrows(), total=df_csv.shape[0], desc="Processando ALUGUEL"):

            tombo = str(row_csv['TOMBO']).strip()
            ultimo_mov = dict_ultimo_mov.get(tombo)

            if not ultimo_mov or ultimo_mov['movimento']['tipo_id'] != 1: 
                rejeitados += 1
                continue

            row_mov = ultimo_mov['movimento']
            cli_legado_id = int(row_mov['cliente_id'])
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            equipment_id_ref = dict_equipamentos_novo.get(tombo)
            
            if not recipient_id or not equipment_id_ref: 
                rejeitados += 1
                continue

            # ==================================================================
            # 1.APLICA AS REGRAS USANDO O CÉREBRO DA CLASSE PAI
            # ==================================================================      
            raw_contract_id = row_csv.get('CONTRACT_ID')
            csv_contract_id = int(float(raw_contract_id)) if pd.notna(raw_contract_id) and str(raw_contract_id).strip() not in ['None', 'nan', ''] else None

            raw_item_id = row_csv.get('CONTRACT_ITEM_ID')
            csv_item_id = int(float(raw_item_id)) if pd.notna(raw_item_id) and str(raw_item_id).strip() not in ['None', 'nan', ''] else None
            
            (contrato_id_res, item_id_res, is_avulso, is_kit, is_excedente, teve_match_perfeito, motivo_divergencia) = self.regras_item_contratos(
                csv_contract_id, csv_item_id, equipment_id_ref, recipient_id, self.dict_is_kit, abater_saldo=True
            )

            # Logs de Auditoria
            if motivo_divergencia:
                status_final_log = "AVULSO (SEM CONTRATO)" if is_avulso else "KIT (IMUNE)" if is_kit else "EXCEDENTE (IS_EXCHANGE)" if is_excedente else "ALUGUEL NORMAL"
                log_nao_match.append({
                    "TOMBO": tombo, "EQUIPAMENTO_CSV": row_csv.get('EQUIPAMENTO_NOME', 'NÃO INFORMADO'),
                    "ID_CLIENTE_LEGADO": cli_legado_id, "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRACT_ID_CSV": csv_contract_id if csv_contract_id else "VAZIO", "CONTRATO_RESOLVIDO": contrato_id_res if contrato_id_res else "NENHUM (AVULSO)",
                    "ITEM_CSV": row_csv.get('ITEM_DO_CONTRATO', 'VAZIO'), "DESC_ITEM_CSV": row_csv.get('DESCRICAO_ITEM', 'VAZIO'),
                    "ITEM_RESOLVIDO_ID": item_id_res if item_id_res else "NENHUM", "STATUS_FINAL": status_final_log, "MOTIVO_EXATO": motivo_divergencia
                })

            usr_id = int(row_mov['usuario_id']) if pd.notna(row_mov['usuario_id']) and row_mov['usuario_id'] != 0 else 1
            dt_mov = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else self.now

            detalhes_item = "Movimento Avulso (Cliente sem contrato ativo)" if is_avulso else "Equipamento Kit (Imune a saldo, sem item vinculado)" if is_kit else "Equipamento Excedente (Contrato sem saldo)" if is_excedente else "Item Extra Oficial (Fallback de Contrato/Item)" if not teve_match_perfeito else None

            org_id_resolvida = dict_contract_org.get(contrato_id_res) or dict_customer_org.get(recipient_id) or dict_equip_org.get(equipment_id_ref) or 1115

            self.registrar_movimento(
                id_final=int(row_mov['id']),
                recipient_id=recipient_id,
                cliente_final_address_id=self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id),
                usuario_id=usr_id,
                organization_id=int(org_id_resolvida),
                mov_date=dt_mov,
                deleted_at_mov=row_mov['deleted_at'] if pd.notna(row_mov['deleted_at']) else None,
                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=equipment_id_ref,
                status_shipment=2,
                tipo_movimento_id=7 if is_avulso else 1,
                operation_type='AVULSO' if is_avulso else 'ALUGUEL',
                status_equipment_id=2,
                history_reason='SHIPPING_CONFIRMED_SEPARATE' if is_avulso else 'SHIPPING_CONFIRMED_RENT',
                forcar_extra=False,
                is_exchange=is_excedente,
                alias_item=str(row_csv.get('ITEM_DO_CONTRATO')).strip() if pd.notna(row_csv.get('ITEM_DO_CONTRATO')) else None,
                alias_movimento=row_csv.get('EQUIPAMENTO_NOME'),
                details_capa="Migração",
                details_item=detalhes_item
            )

        if log_nao_match:
            print(f"📝 {len(log_nao_match)} regras comerciais processadas. Salvando log...")
            pd.DataFrame(log_nao_match).to_csv("log_divergencias_aluguel.csv", index=False, encoding="utf-8")
            print("   📄 Log salvo em 'log_divergencias_aluguel.csv'")

        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=self.equipamentos_alterados)
        self._atualizar_saldos_mysql()
        
def executar(eng_novo, eng_legado):
    from movimentos.migracao_aluguel import resetar_saldo_contract_items, carregar_dados_compartilhados
    print("\n" + "="*70 + "\n🚀 MODO DEBUG: Disparando teste isolado de ALUGUEL\n" + "="*70)
    print("\n🧹 Executando faxina e reset de saldos...\n🔄 Ajustando regras de negócio pré-migração...")
    resetar_saldo_contract_items(eng_novo)
    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)
    MigracaoAluguel(eng_novo, eng_legado, dados_ram, start_counter=1).executar()