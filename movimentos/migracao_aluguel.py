import os
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
ARQUIVO_IMPORTACAO = os.path.join(BASE_DIR, "docs", "equipeAS.csv")

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
        # 1. Modo Fantasma: (Proteção para quando a Devolução reaproveitar esta classe)
        if getattr(self, 'consumir_saldos', None) is False:
            return 0, None, int(contrato_item_id) if pd.notna(contrato_item_id) else None

        if pd.isna(contrato_item_id) or not contrato_item_id:
            return 0, None, None

        # 3. SE DEU MATCH: Aplica a lógica clássica de desconto de saldo
        contrato_item_id = int(contrato_item_id)
        saldos = self.dados["saldos_por_id"]
        dict_tipo = self.dados["dict_tipo_por_equipamento"]
        
        # Diminui o saldo da memória (Igual ao -= 1 do seu código antigo)
        saldos[contrato_item_id] = saldos.get(contrato_item_id, 0) - 1

        # 4. A REGRA DO EXTRA: Só joga para a tabela de Extras se o saldo ficou negativo
        if saldos[contrato_item_id] < 0:
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
        vistos = set()

        for info in self.dados["dict_contrato_item_por_chave"].values():
            c_id = info['id']
            qtd_orig = info['original_quantity']
            qtd_atual = saldos.get(c_id, qtd_orig)

            qtd_final_banco = max(0, qtd_atual)

            if qtd_final_banco != qtd_orig and c_id not in vistos:
                modificados.append({"id": c_id, "nova_qtd": qtd_final_banco})
                vistos.add(c_id)

        if modificados:
            with self.engine_new.begin() as conn:
                for item in modificados:
                    conn.execute(text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), item)
            print(f"  ✔️ {len(modificados)} saldos de contrato atualizados no MySQL (Valores negativos travados em 0).")

    def executar(self):
        print("\n" + "-" * 70)
        print("📦 MÓDULO: ALUGUEL (Fonte: CSV)")
        print("-" * 70)

        self.limpar_tabelas_movimento()
        caminho_csv = "./docs/EquipAS.csv"
        
        print("📖 Carregando planilha auxiliar de aluguel...")
        df_csv = pd.read_csv(caminho_csv, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)
        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
        df_csv = df_csv[df_csv['ITEM_DO_CONTRATO'].str.lower() != 'nan']
        df_csv['CONTRACT_ID'] = df_csv['CONTRACT_ID'].astype(str).str.replace('.0', '', regex=False)

        tombos = df_csv['TOMBO'].astype(int).unique().tolist()

        # 1. Busca os movimentos no banco Legado
        dict_ultimo_mov = self.buscar_ultimo_movimento_por_tombo(tombos)
        print(f"   ✅ {len(dict_ultimo_mov)} tombos indexados no legado.")

        # 2. Busca os IDs dos equipamentos no banco Novo (Usando o SQL Otimizado!)
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
                print(f"\n⚠️ Ignorado: Mov. {row_mov['id']} (Tombo {tombo}). Cliente {cli_legado_id} não mapeado na tabela 'addresses'.")
                rejeitados += 1
                continue

            # ==================================================================
            # VALIDAÇÃO DO EQUIPAMENTO (Consultando o dicionário do SQL Otimizado)
            # ==================================================================
            equipment_id_ref = dict_equipamentos_novo.get(tombo)
            if not equipment_id_ref:
                print(f"\n⚠️ Ignorado: Tombo {tombo} não encontrado na tabela equipments do banco novo.")
                rejeitados += 1
                continue

            # ==================================================================
            # 1. CAPTURA E NORMALIZAÇÃO DOS DADOS DA PLANILHA
            # ==================================================================      
            try:
                csv_contract_id = int(float(row_csv.get('CONTRACT_ID')))
            except (ValueError, TypeError):
                csv_contract_id = None

            desc_i = normalizar_para_match(row_csv.get('DESCRICAO_ITEM'))
            item_c = normalizar_para_match(row_csv.get('ITEM_DO_CONTRATO'))
            
            contrato_id_res = None
            item_id_res = None
            teve_match_perfeito = False
            is_avulso = False

            # ==================================================================
            # 2. VALIDAÇÃO DO CONTRATO (A Chave de Ouro)
            # ==================================================================
            contratos_validos_banco = self.dados["dict_contratos_vinculados"].get(recipient_id, [])
            motivo_divergencia = None

            if csv_contract_id in contratos_validos_banco:
                contrato_id_res = csv_contract_id
            elif contratos_validos_banco:
                contrato_id_res = contratos_validos_banco[0]
                motivo_divergencia = f"Contrato da planilha ({csv_contract_id}) não pertence ao cliente. Forçado para o real: {contrato_id_res}"
            else:
                contrato_id_res = None
                is_avulso = True
                motivo_divergencia = "Cliente não possui nenhum contrato ativo no banco. Forçado para AVULSO."

            # ==================================================================
            # 3. MATCH DOS ITENS (Só executa se houver um contrato validado)
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
                    fallback_contrato = self.dados["dict_contrato_aluguel_por_chave"].get((cli_legado_id, contrato_id_res))
                    if fallback_contrato:
                        item_id_res = fallback_contrato['first_contract_item_id']
                    else:
                        item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)
                        
                    if not motivo_divergencia: 
                        motivo_divergencia = "Contrato OK, mas descrição/item da planilha não casou. Forçado para Item Extra/Fallback."

            # ==================================================================
            # GRAVAÇÃO DO LOG RICO DE DIVERGÊNCIAS
            # ==================================================================
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
                    "STATUS_FINAL": "AVULSO" if is_avulso else "ALUGUEL (ITEM EXTRA)",
                    "MOTIVO_EXATO": motivo_divergencia
                })

            # ==================================================================
            # 4. REGISTRO DO MOVIMENTO (Limpo e sem erros de sintaxe)
            # ==================================================================
            usr_id = int(row_mov['usuario_id']) if pd.notna(row_mov['usuario_id']) and row_mov['usuario_id'] != 0 else 1
            dt_mov = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else self.now

            detalhes_item = None
            if is_avulso:
                detalhes_item = "Movimento Avulso (Cliente sem contrato ativo)"
            elif not teve_match_perfeito:
                detalhes_item = "Item Extra (Fallback de Contrato/Item)"

            self.registrar_movimento(
                id_final=int(row_mov['id']),
                recipient_id=recipient_id,
                cliente_final_address_id=self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id),
                usuario_id=usr_id,
                mov_date=dt_mov,
                deleted_at_mov=row_mov['deleted_at'] if pd.notna(row_mov['deleted_at']) else None,
                
                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=equipment_id_ref,
                
                tipo_movimento_id=7 if is_avulso else 1,
                
                operation_type='AVULSO' if is_avulso else 'ALUGUEL',
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
        
        self._atualizar_saldos_mysql()
# ==============================================================================
# WRAPPER (Ponte para a execução dinâmica do main.py no Modo Debug)
# ==============================================================================
def executar(eng_novo, eng_legado):

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