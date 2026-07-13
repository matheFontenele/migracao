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

    def limpar_tabelas_movimento(self):
        if self.limpar_ambiente:
            print("\n🧹 [ALUGUEL] Iniciando faxina estrutural nas tabelas transacionais...")
            executar_truncate_tabelas(self.engine_new, TABELAS)  

    def calcular_saldo(
        self, contrato_item_id, recipient_id, equipment_id_ref, mov_date,
        item_servico_id_atual, fallback_contract_item_id=None, forcar_extra=False
    ):
        # 1. Modo Fantasma:
        if getattr(self, 'consumir_saldos', None) is False:
            return 0, None, int(contrato_item_id) if pd.notna(contrato_item_id) else None

        if pd.isna(contrato_item_id) or not contrato_item_id:
            return 0, None, None

        contrato_item_id = int(contrato_item_id)
        saldos = self.dados["saldos_por_id"]
        dict_tipo = self.dados["dict_tipo_por_equipamento"]
        saldo_atual = saldos.get(contrato_item_id, 0)
        
        type_id = dict_tipo.get(equipment_id_ref)
        is_kit = self.dict_is_kit.get(type_id, 0)

        # Regra caso não seja KIT e o saldo esteja zerado = Excedente
        if saldo_atual <= 0 and is_kit == 0:
            return 0, None, contrato_item_id
        
        # 🎯 SE FOR KIT OU TIVER SALDO: Aplica a lógica clássica de desconto na memória
        saldos[contrato_item_id] = saldo_atual - 1

        # 4. A REGRA DO EXTRA: Só joga para a tabela de Extras se o saldo ficou negativo e estiver dentro da regra de kits
        if saldos[contrato_item_id] < 0 and is_kit == 1:
            extra_id = self.extra_id_counter
            self.extra_id_counter += 1

            self.itens_extras_mestre.append({
                "id": extra_id, 
                "service_order_item_id": item_servico_id_atual,
                "contract_item_id": contrato_item_id,
                "type_id": dict_tipo.get(equipment_id_ref),
                "quantity": 1, 
                "removed_quantity": 0, 
                "created_at": mov_date, 
                "updated_at": mov_date, 
                "deleted_at": None
            })
            return 1, extra_id, contrato_item_id 

        # 5. Sucesso absoluto (Match feito e saldo positivo)
        return 0, None, contrato_item_id
    
    def _atualizar_saldos_mysql(self):
        # 1. MODO FANTASMA: Se for chamado pela Devolução no futuro, não altera o banco!
        if getattr(self, 'consumir_saldos', None) is False:
            return

        saldos = self.dados["saldos_por_id"]
        modificados = []

        # 🎯 MUDANÇA ABSOLUTA AQUI: Iteramos direto sobre TODOS os saldos da memória!
        # Não importa se o item deu match por texto, fallback ou milagre, se o saldo alterou, nós salvamos!
        for c_id, qtd_atual in saldos.items():
            qtd_final_banco = max(0, qtd_atual) # Impede saldo negativo no MySQL
            modificados.append({"id": c_id, "nova_qtd": qtd_final_banco})

        if modificados:
            with self.engine_new.begin() as conn:
                # Dispara o bulk update massivo
                conn.execute(text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), modificados)
            print(f"  ✔️ {len(modificados)} saldos de contrato sincronizados massivamente com o MySQL.")
    
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
            lista_dfs.append(pd.read_parquet(arq))
        
        df_csv = pd.concat(lista_dfs, ignore_index=True)

        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
        df_csv = df_csv[df_csv['ITEM_DO_CONTRATO'].str.lower() != 'nan']
        df_csv['CONTRACT_ID'] = df_csv['CONTRACT_ID'].astype(str).str.replace('.0', '', regex=False)

        tombos = df_csv['TOMBO'].astype(int).unique().tolist()

        dict_ultimo_mov = self.buscar_ultimo_movimento_por_tombo(tombos)
        print(f"   ✅ {len(dict_ultimo_mov)} tombos indexados no legado.")

        dict_equipamentos_novo = self.buscar_equipamentos_novo_por_tombo(tombos)
        print(f"   ✅ {len(dict_equipamentos_novo)} equipamentos correspondentes encontrados no banco novo.")

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
            # VALIDAÇÃO DO EQUIPAMENTO
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

            desc_i = normalizar_para_match(row_csv.get('DESCRICAO_ITEM'))
            item_c = normalizar_para_match(row_csv.get('ITEM_DO_CONTRATO'))
            
            contrato_id_res = None
            item_id_res = None
            teve_match_perfeito = False
            is_avulso = False
            motivo_divergencia = None

            # ==================================================================
            # 2. VALIDAÇÃO DO CONTRATO (A Chave de Ouro é o Parquet)
            # ==================================================================
            if csv_contract_id is not None:
                contrato_id_res = csv_contract_id
            else:
                is_avulso = True
                motivo_divergencia = "Planilha definiu equipamento sem contrato (None). Mantido como AVULSO."

            # ==================================================================
            # 3. MATCH DOS ITENS
            # ==================================================================
            if contrato_id_res and not is_avulso:
                chave_rigida = (int(recipient_id), contrato_id_res, item_c, desc_i)
                match_info = self.dados["dict_contrato_item_por_chave"].get(chave_rigida)

                if not match_info:
                    chave_legado = (cli_legado_id, contrato_id_res, item_c, desc_i)
                    match_info = self.dados["dict_contrato_item_aluguel_por_chave"].get(chave_legado)

                if match_info:
                    item_id_res = match_info['id']
                    teve_match_perfeito = True
                else:
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
                        motivo_divergencia = "Contrato OK, mas descrição/item não casou. Forçado para o 1º item do contrato."

            # ==================================================================
            # 4. GESTÃO DE SALDOS E EXCEDENTES
            # ==================================================================
            is_excedente = False
            
            if contrato_id_res and item_id_res and not is_avulso:
                saldo_atual = self.dados["saldos_por_id"].get(item_id_res, 0)
                
                if saldo_atual <= 0:
                    # Se não tem mais saldo, chuta para Avulso/Excedente
                    is_avulso = True
                    is_excedente = True
                    if not motivo_divergencia:
                        motivo_divergencia = "Saldo do item esgotado. Transformado em Excedente/Avulso."
                
                # 🚫 Removemos o decremento manual aqui, pois a classe Pai (`registrar_movimento` -> `calcular_saldo`) já abate da memória de forma segura.

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

            self.registrar_movimento(
                id_final=int(row_mov['id']),
                recipient_id=recipient_id,
                cliente_final_address_id=self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id),
                usuario_id=usr_id,
                organization_id=1378,
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
        print(f"\n⚠️ Registros rejeitados (Sem movimento ou sem endereço no novo banco): {rejeitados}")
        
        if log_nao_match:
            print(f"📝 {len(log_nao_match)} divergências registradas. Salvando log...")
            df_erros = pd.DataFrame(log_nao_match)
            df_erros.to_csv("log_divergencias_aluguel.csv", index=False, encoding="utf-8")
            print("   📄 Log salvo em 'log_divergencias_aluguel.csv'")

        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=self.equipamentos_alterados)

        # Atualiza todos os 1598 itens de uma vez no banco
        self._atualizar_saldos_mysql()
        
# ==============================================================================
# WRAPPER (Ponte para a execução dinâmica do main.py no Modo Debug)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_aluguel import resetar_saldo_contract_items, carregar_dados_compartilhados

    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de ALUGUEL")
    print("="*70)

    print("\n🧹 Executando faxina e reset de saldos...")
    print("\n🔄 Ajustando regras de negócio pré-migração...")
    resetar_saldo_contract_items(eng_novo)

    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)

    app_teste = MigracaoAluguel(eng_novo, eng_legado, dados_ram, start_counter=1)
    app_teste.executar()