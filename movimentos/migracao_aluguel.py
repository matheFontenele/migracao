import os
import glob
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from movimentos.migracao_movimentos import carregar_dados_compartilhados, resetar_saldo_contract_items
from utils.sanetizador import executar_truncate_tabelas, limpar_valor_inteiro, limpar_valor_numerico
from movimentos.migracao_movimentos import BaseMigracaoMovimento, normalizar_para_match

TABELAS = [
    "service_order_item_extra_equipments",
    "movement_items",
    "movements",
    "service_order_items",
    "service_orders"
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

    def calcular_saldo(
        self, contrato_item_id, recipient_id, equipment_id_ref, mov_date,
        item_servico_id_atual, fallback_contract_item_id=None, forcar_extra=False
    ):
        return 0, None, int(contrato_item_id) if pd.notna(contrato_item_id) else None
    
    def _atualizar_saldos_mysql(self):
        if getattr(self, 'consumir_saldos', None) is False:
            return

        if not self.saldos_modificados:
            print("\n💾 Nenhum saldo precisou ser atualizado no banco de dados.")
            return

        print(f"\n💾 Sincronizando {len(self.saldos_modificados)} saldos modificados com o MySQL...")
        
        atualizados = 0
        with self.engine_new.begin() as conn:
            for c_id in self.saldos_modificados:
                # Pega a quantidade nova calculada na RAM e protege contra número negativo
                qtd_final_banco = max(0, int(self.dados["saldos_por_id"][c_id]))
                
                # Executa o UPDATE direto e certeiro
                res = conn.execute(
                    text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), 
                    {"nova_qtd": qtd_final_banco, "id": c_id}
                )
                
                if res.rowcount > 0:
                    atualizados += 1

        print(f"  ✔️ {atualizados} itens de contrato atualizados com sucesso!")
    
    def executar(self):
        print("\n" + "-" * 70)
        print("📦 MÓDULO: ALUGUEL (Fonte: CSV)")
        print("-" * 70)

        self.limpar_tabelas_movimento()
        
        # ======================================================================
        # CARREGAMENTO DO DICIONÁRIO TYPES.CSV
        # ======================================================================
        caminho_types = "./docs/types.csv"
        if os.path.exists(caminho_types):
            print("📖 Carregando mapeamento de Tipos e Kits (types.csv)...")
            df_types = pd.read_csv(caminho_types, sep=",", encoding="utf-8")
            # Mapeia {id: is_kit}
            self.dict_is_kit = {int(row['id']): int(row['is_kit']) for _, row in df_types.iterrows() if pd.notna(row['id'])}
        else:
            print("⚠️ Arquivo types.csv não encontrado. Todos os equipamentos assumirão is_kit = 0.")
            self.dict_is_kit = {}

        # ======================================================================
        # CARREGAMENTO DOS ARQUIVOS PARQUET
        # ======================================================================
        pasta_parquets = "./docs/parquets"
        arquivos_parquet = glob.glob(os.path.join(pasta_parquets, "*.parquet"))

        if not arquivos_parquet:
            print("❌ Nenhum arquivo .parquet encontrado na pasta 'docs/'.")
            return

        print(f"📖 Lendo dados de {len(arquivos_parquet)} arquivos Parquet...")
        lista_dfs = []
        for arq in arquivos_parquet:
            df_temp = pd.read_parquet(arq)
            df_temp.columns = df_temp.columns.str.upper() 
            lista_dfs.append(df_temp)
        
        # Consolida tudo num único Mega DataFrame
        df_csv = pd.concat(lista_dfs, ignore_index=True)

        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        
        if 'ITEM_DO_CONTRATO' in df_csv.columns:
            df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
            df_csv = df_csv[df_csv['ITEM_DO_CONTRATO'].str.lower() != 'nan']
            
        df_csv['CONTRACT_ID'] = df_csv['CONTRACT_ID'].astype(str).str.replace('.0', '', regex=False)

        tombos = df_csv['TOMBO'].astype(int).unique().tolist()

        dict_ultimo_mov = self.buscar_ultimo_movimento_por_tombo(tombos)
        print(f"   ✅ {len(dict_ultimo_mov)} tombos indexados no legado.")

        dict_equipamentos_novo = self.buscar_equipamentos_novo_por_tombo(tombos)
        print(f"   ✅ {len(dict_equipamentos_novo)} equipamentos correspondentes encontrados no banco novo.")

        with self.engine_new.connect() as conn:
            # 1. Organização dos Contratos
            df_contracts = pd.read_sql("SELECT id, organization_id FROM contracts", conn)
            dict_contract_org = dict(zip(df_contracts['id'], df_contracts['organization_id']))
            
            # 2. Organização dos Clientes
            df_customers = pd.read_sql("SELECT id, organization_id FROM customers", conn)
            dict_customer_org = dict(zip(df_customers['id'], df_customers['organization_id']))
            
            # 3. Organização dos Equipamentos
            df_equip_orgs = pd.read_sql("SELECT id, current_organization_id FROM equipments", conn)
            dict_equip_org = dict(zip(df_equip_orgs['id'], df_equip_orgs['current_organization_id']))


        log_nao_match = []
        rejeitados = 0
        

        for _, row_csv in tqdm(df_csv.iterrows(), total=df_csv.shape[0], desc="Processando ALUGUEL"):

            contrato_item_equip = row_csv['TOMBO']
            if contrato_item_equip == 'nan' or not contrato_item_equip:
                continue
                
            tombo = str(contrato_item_equip).strip()
            
            ultimo_mov = dict_ultimo_mov.get(tombo)
            if ultimo_mov is None:
                rejeitados += 1
                continue

            row_mov = ultimo_mov['movimento']
            if row_mov['tipo_id'] not in {1, 5}: 
                rejeitados += 1
                continue

            cli_legado_id = int(row_mov['cliente_id'])
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            
            if not recipient_id: 
                rejeitados += 1
                continue

            # ==================================================================
            # VALIDAÇÃO DO EQUIPAMENTO (Consultando o dicionário do SQL Otimizado)
            # ==================================================================
            equipment_id_ref = dict_equipamentos_novo.get(tombo)
            if not equipment_id_ref:
                rejeitados += 1
                continue

            # ==================================================================
            # 1. CAPTURA E NORMALIZAÇÃO DOS DADOS DA PLANILHA
            # ==================================================================      
            raw_contract_id = row_csv.get('CONTRACT_ID')
            if pd.isna(raw_contract_id) or str(raw_contract_id).strip() in ['None', 'nan', '']:
                csv_contract_id = None
            else:
                try:
                    csv_contract_id = int(float(raw_contract_id))
                except (ValueError, TypeError):
                    csv_contract_id = None

            # Captura do ID do Item direto da planilha
            raw_item_id = row_csv.get('CONTRACT_ITEM_ID')
            if pd.isna(raw_item_id) or str(raw_item_id).strip() in ['None', 'nan', '']:
                csv_item_id = None
            else:
                try:
                    csv_item_id = int(float(raw_item_id))
                except (ValueError, TypeError):
                    csv_item_id = None
            
            contrato_id_res = None
            item_id_res = None
            teve_match_perfeito = False
            is_avulso = False
            motivo_divergencia = None
            
            contrato_id_res = None
            item_id_res = None
            teve_match_perfeito = False
            is_avulso = False

            # ==================================================================
            # 2. VALIDAÇÃO DO CONTRATO (A Chave de Ouro é o Parquet)
            # ==================================================================
            motivo_divergencia = None

            if csv_contract_id is not None:
                contrato_id_res = csv_contract_id
            else:
                is_avulso = True
                motivo_divergencia = "Planilha definiu equipamento sem contrato (None). Mantido como AVULSO."

            # ==================================================================
            # 3. MATCH DOS ITENS (Só executa se houver um contrato validado)
            # ==================================================================
            if contrato_id_res and not is_avulso:
                if csv_item_id is not None:
                    item_id_res = csv_item_id
                    teve_match_perfeito = True
                else:
                    # 🚨 FALLBACK BLINDADO (Caso falte a coluna CONTRACT_ITEM_ID no Parquet)
                    item_id_res = next(
                        (info['id'] for chave, info in self.dados["dict_contrato_item_por_chave"].items() if chave[1] == contrato_id_res),
                        None
                    )

                    if not item_id_res:
                         item_id_res = next(
                            (info['id'] for chave, info in self.dados["dict_contrato_item_aluguel_por_chave"].items() if chave[1] == contrato_id_res),
                            None
                        )

                    if not item_id_res:
                        item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)
                        
                    if not motivo_divergencia: 
                        motivo_divergencia = "ID do Item ausente no Parquet. Forçado para o 1º item do contrato."

            # ==================================================================
            # 4. GESTÃO DE SALDOS E EXCEDENTES (A Matemática Real)
            # ==================================================================
            is_excedente = False

            if contrato_id_res and item_id_res and not is_avulso:
                # Força tipo Inteiro para casar com as chaves do dicionário do Base
                item_id_res_int = int(item_id_res)
                
                saldo_atual = self.dados["saldos_por_id"].get(item_id_res_int, 0)
                type_id_atual = self.dados["dict_tipo_por_equipamento"].get(equipment_id_ref)
                is_kit_atual = self.dict_is_kit.get(type_id_atual, 0)
                
                if saldo_atual <= 0 and is_kit_atual == 0:
                    is_excedente = True
                    if not motivo_divergencia:
                        motivo_divergencia = "Saldo do item esgotado. Mantido no contrato como excedente."
                else:
                    # 📉 Abate o saldo da memória e RASTREIA a mudança!
                    self.dados["saldos_por_id"][item_id_res_int] = saldo_atual - 1
                    self.saldos_modificados.add(item_id_res_int)

            if motivo_divergencia:
                log_nao_match.append({
                    "TOMBO": tombo,
                    "EQUIPAMENTO_CSV": row_csv.get('EQUIPAMENTO_NOME', 'NÃO INFORMADO'),
                    "ID_CLIENTE_LEGADO": cli_legado_id,
                    "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRACT_ID_CSV": csv_contract_id if csv_contract_id else "VAZIO",
                    "CONTRATO_RESOLVIDO": contrato_id_res if contrato_id_res else "NENHUM (AVULSO)",
                    "ITEM_CSV": row_csv.get('ITEM_DO_CONTRATO', 'VAZIO'),
                    "DESC_ITEM_CSV": row_csv.get('DESCRICAO_ITEM', 'VAZIO'),
                    "ITEM_RESOLVIDO_ID": item_id_res if item_id_res else "NENHUM",
                    "STATUS_FINAL": "EXCEDENTE" if is_excedente else ("AVULSO" if is_avulso else "ALUGUEL (ITEM EXTRA)"),
                    "MOTIVO_EXATO": motivo_divergencia
                })

            usr_id = int(row_mov['usuario_id']) if pd.notna(row_mov['usuario_id']) and row_mov['usuario_id'] != 0 else 1
            dt_mov = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else self.now

            detalhes_item = None
            if is_avulso:
                detalhes_item = "Movimento Avulso (Cliente sem contrato ativo)"
            elif is_excedente:
                detalhes_item = "Equipamento Excedente (Contrato sem saldo, item is_kit=0)"
            elif not teve_match_perfeito:
                detalhes_item = "Item Extra Oficial (Fallback de Contrato/Item)"

            org_id_resolvida = None

            # 1. Tenta pegar a Org através do Contrato
            if contrato_id_res and pd.notna(dict_contract_org.get(contrato_id_res)):
                org_id_resolvida = dict_contract_org.get(contrato_id_res)

            # 2. Se falhou, tenta pegar a Org através do Cliente (recipient_id)
            if (not org_id_resolvida or pd.isna(org_id_resolvida)) and pd.notna(dict_customer_org.get(recipient_id)):
                org_id_resolvida = dict_customer_org.get(recipient_id)

            # 3. Se falhou, tenta pegar a Org através do Equipamento (equipment_id_ref)
            if (not org_id_resolvida or pd.isna(org_id_resolvida)) and pd.notna(dict_equip_org.get(equipment_id_ref)):
                org_id_resolvida = dict_equip_org.get(equipment_id_ref)

            # 4. Se absolutamente tudo falhou, cai no Fallback seguro 1115
            org_id_resolvida = int(org_id_resolvida) if org_id_resolvida and pd.notna(org_id_resolvida) else 1115

            self.registrar_movimento(
                id_final=int(row_mov['id']),
                recipient_id=recipient_id,
                cliente_final_address_id=self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id),
                usuario_id=usr_id,
                organization_id=org_id_resolvida,
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

                is_exchange=is_excedente,
                
                alias_item=str(row_csv.get('ITEM_DO_CONTRATO')).strip() if pd.notna(row_csv.get('ITEM_DO_CONTRATO')) else None,
                alias_movimento=row_csv.get('EQUIPAMENTO_NOME'),
                details_capa="Migração",
                details_item=detalhes_item
            )

        # ==================================================================
        # FINALIZAÇÃO E LOGS
        # ==================================================================        
        if log_nao_match:
            print(f"📝 {len(log_nao_match)} divergências registradas. Salvando log...")
            df_erros = pd.DataFrame(log_nao_match)
            df_erros.to_csv("log_divergencias_aluguel.csv", index=False, encoding="utf-8")
            print("   📄 Log salvo em 'log_divergencias_aluguel.csv'")

        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=self.equipamentos_alterados)

        self._atualizar_saldos_mysql()
        
# ==============================================================================
# WRAPPER (Ponte para a execução dinâmica do main.py no Modo Debug)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_aluguel import resetar_saldo_contract_items, carregar_dados_compartilhados

    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de ALUGUEL")
    print("="*70)

    # 1. Faxina pré-teste no banco novo
    print("\n🧹 Executando faxina e reset de saldos...")
    print("\n🔄 Ajustando regras de negócio pré-migração...")
    resetar_saldo_contract_items(eng_novo)

    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)

    app_teste = MigracaoAluguel(eng_novo, eng_legado, dados_ram, start_counter=1)
    app_teste.executar()
