import os
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from utils.sanetizador import normalizar_para_match
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino, limpar_codigo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_IMPORTACAO = "./docs/EquipAS.csv"

class MigracaoSubstituicao(BaseMigracaoMovimento):

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=900000):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente=False)
        
        # 1. Contadores Inteligentes para Shipments (Para a devolução da máquina velha)
        with self.engine_new.connect() as conn:
            max_ship = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM shipments")).scalar()
            max_ship_item = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM shipment_items")).scalar()

            org_ids = [str(org['id']) for org in ENDERECOS_BASES]
            org_ids_sql = "(" + ", ".join(org_ids) + ")"
            query_enderecos = f"SELECT addressable_id, MIN(id) as address_id FROM addresses WHERE addressable_type = 'organization' AND addressable_id IN {org_ids_sql} GROUP BY addressable_id"
            result_ends = conn.execute(text(query_enderecos)).fetchall()
            self.dict_enderecos_base_org = {row.addressable_id: row.address_id for row in result_ends}
            
        self.shipment_id_counter = max_ship + 1
        self.shipment_item_id_counter = max_ship_item + 1
        self.shipments_mestre = []
        self.shipment_movements_mestre = []
        self.shipment_items_mestre = []

    def salvar_shipments_banco(self):
        if not self.shipments_mestre: return
        print(f"\n🚀 Persistindo guias de transporte (Shipments) de Retorno no MySQL...")
        with self.engine_new.begin() as conn:
            pd.DataFrame(self.shipments_mestre).to_sql("shipments", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_movements_mestre).to_sql("shipment_movements", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_items_mestre).to_sql("shipment_items", con=conn, if_exists="append", index=False)

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

    def _extrair_dados_substituicao(self, lista_mov_ids):
        if not lista_mov_ids: return pd.DataFrame()

        print("   📖 Extraindo o núcleo das Substituições no legado...")
        lista_sql = "(" + ", ".join(map(str, lista_mov_ids)) + ")"

        # 1. Puxa os dados da tabela aluguel_substituicao amarrando os dois equipamentos
        query_subst = f"""
            SELECT 
                mov_novo.id as MOV_NOVO_ID, eq_novo.numero as TOMBO_NOVO, eq_novo.nome as NOME_NOVO,
                COALESCE(mov_novo.updated_at, mov_novo.data) AS DATA_SUBST,
                COALESCE(NULLIF(mov_novo.usuario_id, 0), 1) AS USR_SUBST,
                mov_novo.deleted_at AS DEL_SUBST,
                
                mov_dev.id as MOV_DEV_ID, eq_antigo.numero as TOMBO_ANTIGO, eq_antigo.nome as NOME_ANTIGO,
                ac.id as CLIENTE_ID
            FROM aluguel_substituicao als
            INNER JOIN aluguel_movimento_itens ami_novo ON ami_novo.id = als.substituicao_aluguel_id
            INNER JOIN aluguel_movimento mov_novo ON mov_novo.id = ami_novo.movimento_id
            INNER JOIN aluguel_equipamentos eq_novo ON eq_novo.id = ami_novo.equipamento_id
            
            INNER JOIN aluguel_movimento_itens ami_dev ON ami_dev.id = als.substituicao_devolucao_id
            INNER JOIN aluguel_movimento mov_dev ON mov_dev.id = ami_dev.movimento_id
            INNER JOIN aluguel_equipamentos eq_antigo ON eq_antigo.id = ami_dev.equipamento_id
            
            LEFT JOIN aluguel_clientes ac ON ac.id = mov_novo.cliente_id
            WHERE mov_novo.id IN {lista_sql}
        """
        
        with self.engine_legado.connect() as conn:
            df_subst = pd.read_sql(text(query_subst), conn)

        if df_subst.empty: return df_subst

        # 2. Busca o histórico de Aluguel dos equipamentos antigos para recriarmos o passado
        tombos_antigos = df_subst['TOMBO_ANTIGO'].dropna().unique().tolist()
        tombos_antigos_sql = "(" + ", ".join([f"'{t}'" for t in tombos_antigos]) + ")"

        print("   🧠 Analisando o histórico original dos equipamentos substituídos...")
        query_hist = f"""
            SELECT
                eq.numero AS TOMBO_ANTIGO, mov.id AS ORIG_MOV_ID,
                COALESCE(NULLIF(mov.usuario_id, 0), 1) AS ORIG_USR_ID,
                COALESCE(mov.updated_at, mov.data) AS ORIG_DATA,
                mov.deleted_at AS ORIG_DEL, mov.tipo_id AS ORIG_TIPO_LEGADO,
                mov.data AS DATA_REAL_ORDENACAO
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens ami ON ami.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ami.movimento_id
            WHERE eq.numero IN {tombos_antigos_sql} AND mov.deleted_at IS NULL AND mov.tipo_id IN (1, 5, 7)
        """
        with self.engine_legado.connect() as conn:
            df_hist = pd.read_sql(text(query_hist), conn)

        # 3. Mágica do Pandas: Cruza a substituição com o último Aluguel ANTES dela acontecer
        df_hist.sort_values(by=['TOMBO_ANTIGO', 'DATA_REAL_ORDENACAO', 'ORIG_MOV_ID'], ascending=[True, False, False], inplace=True)
        df_merged = pd.merge(df_subst, df_hist, on='TOMBO_ANTIGO', how='left')
        
        # Filtra para pegar estritamente o movimento que aconteceu antes da devolução
        df_merged = df_merged[df_merged['ORIG_MOV_ID'] < df_merged['MOV_DEV_ID']]
        
        # Pega a primeira linha de cada substituição (A origem válida mais recente)
        df_final = df_merged.groupby('MOV_NOVO_ID').first().reset_index()
        return df_final

    def executar(self):
        print("\n" + "=" * 70)
        print("🔄 MÓDULO: SUBSTITUIÇÃO (A TRANSIÇÃO COMPLETA)")
        print("=" * 70)

        print("📖 Carregando planilha auxiliar de validação...")
        df_csv = pd.read_csv(ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)
        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
        
        tombos_csv = df_csv['TOMBO'].astype(int).unique().tolist()
        dict_ultimo_mov = self.buscar_ultimo_movimento_por_tombo(tombos_csv)
        
        # Filtra os Tombos da Planilha que O ÚLTIMO MOVIMENTO seja de Substituição (Tipo 6)
        lista_movs_subst = []
        dict_row_csv_by_mov = {}
        
        for _, row_csv in df_csv.iterrows():
            tombo = row_csv['TOMBO']
            mov = dict_ultimo_mov.get(tombo)
            if mov and mov['movimento']['tipo_id'] == 6:
                mov_id = mov['movimento']['id']
                lista_movs_subst.append(mov_id)
                dict_row_csv_by_mov[mov_id] = row_csv

        if not lista_movs_subst:
            print("⚠️ Nenhum equipamento do CSV se encontra no status final de Substituição.")
            return

        print(f"   🎯 {len(lista_movs_subst)} registros de Substituição identificados para processamento.")
        
        # Manda os IDs de substituição pro banco trazer os detalhes
        df_subst = self._extrair_dados_substituicao(lista_movs_subst)
        if df_subst.empty:
            return

        # Puxa os IDs de todos os equipamentos (Novos e Antigos) no banco novo
        tombos_todos = list(set(df_subst['TOMBO_NOVO'].tolist() + df_subst['TOMBO_ANTIGO'].tolist()))
        dict_equip_novo = self.buscar_equipamentos_novo_por_tombo(tombos_todos)
        
        rejeitados = 0

        for _, row in tqdm(df_subst.iterrows(), total=df_subst.shape[0], desc="Processando Ciclos"):
            
            mov_novo_id = row['MOV_NOVO_ID']
            row_csv = dict_row_csv_by_mov[mov_novo_id]
            
            eq_id_novo = dict_equip_novo.get(row['TOMBO_NOVO'])
            eq_id_antigo = dict_equip_novo.get(row['TOMBO_ANTIGO'])
            
            if not eq_id_novo or not eq_id_antigo:
                rejeitados += 1
                continue

            cli_legado_id = int(row['CLIENTE_ID']) if pd.notna(row['CLIENTE_ID']) else 0
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            cliente_final_address = self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id)
            
            if not recipient_id:
                rejeitados += 1
                continue

            org_id_destino = descobrir_id_organizacao_destino(cli_legado_id)
            endereco_base_id = self.dict_enderecos_base_org.get(org_id_destino, 1)

            # =========================================================
            # MATCH DO CONTRATO (Baseado no Planilha do Novo Equipamento)
            # =========================================================
            try: csv_contract_id = int(float(row_csv.get('CONTRACT_ID')))
            except: csv_contract_id = None

            desc_i = normalizar_para_match(row_csv.get('DESCRICAO_ITEM'))
            item_c = normalizar_para_match(row_csv.get('ITEM_DO_CONTRATO'))
            
            contrato_id_res = None
            item_id_res = None
            is_avulso = False

            contratos_validos = self.dados["dict_contratos_vinculados"].get(recipient_id, [])
            if csv_contract_id in contratos_validos: contrato_id_res = csv_contract_id
            elif contratos_validos: contrato_id_res = contratos_validos[0]
            else: is_avulso = True

            if contrato_id_res and not is_avulso:
                chave_rigida = (int(recipient_id), contrato_id_res, item_c, desc_i)
                match_info = self.dados["dict_contrato_item_por_chave"].get(chave_rigida)
                if not match_info: match_info = self.dados["dict_contrato_item_aluguel_por_chave"].get((cli_legado_id, contrato_id_res, item_c, desc_i))

                if match_info: item_id_res = match_info['id']
                else: item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)

            # O MESMO contrato rege o antigo e o novo!
            # =========================================================
            # 🕰️ FASE 1: O PASSADO (ALUGAMOS A MÁQUINA ANTIGA)
            # =========================================================
            if pd.notna(row.get('ORIG_MOV_ID')):
                self.registrar_movimento(
                    id_final=int(row['ORIG_MOV_ID']),
                    recipient_id=recipient_id, cliente_final_address_id=cliente_final_address,
                    usuario_id=int(row['ORIG_USR_ID']), organization_id=org_id_destino,
                    mov_date=row['ORIG_DATA'], deleted_at_mov=row['ORIG_DEL'] if pd.notna(row['ORIG_DEL']) else None,
                    contrato_id=contrato_id_res, contrato_item_id=item_id_res, equipment_id_ref=eq_id_antigo,
                    tipo_movimento_id=1, operation_type='ALUGUEL', is_exchange=False, # Não é troca, é aluguel limpo
                    alias_movimento=row['NOME_ANTIGO'], details_capa="Migração (Reconstrução): Aluguel Histórico", details_item="Alocado no Cliente (Histórico)"
                )

            # =========================================================
            # 📥 FASE 2: O PRESENTE (RECOLHEMOS A MÁQUINA ANTIGA)
            # =========================================================
            id_mov_dev = int(row['MOV_DEV_ID'])
            dt_subst = row['DATA_SUBST']
            usr_subst = int(row['USR_SUBST'])

            id_capa_dev, _, id_mov_item_dev = self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id, 
                cliente_final_address_id=endereco_base_id,
                usuario_id=usr_subst, 
                organization_id=org_id_destino,
                mov_date=dt_subst, 
                deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                contrato_id=contrato_id_res, 
                contrato_item_id=item_id_res, 
                equipment_id_ref=eq_id_antigo,
                tipo_movimento_id=2, # 🎯 CORRIGIDO: 2 = SUBSTITUIÇÃO
                operation_type='SUBSTITUICAO', # 🎯 CORRIGIDO
                is_exchange=True, # Flag de troca mantida!
                alias_movimento=row['NOME_ANTIGO'], 
                details_capa="Migração: Retorno por Substituição", 
                details_item="Equipamento Substituído (Retorno)"
            )

            # SHIPMENT DE RETORNO DA MÁQUINA VELHA
            ship_id = self.shipment_id_counter
            self.shipment_id_counter += 1
            self.shipments_mestre.append({"id": ship_id, "status_id": 1, "created_by": usr_subst, "created_at": dt_subst, "updated_at": dt_subst, "deleted_at": None})
            self.shipment_items_mestre.append({"id": self.shipment_item_id_counter, "shipment_id": ship_id, "status_id": 1, "movement_item_id": id_mov_item_dev, "volume_id": None, "details": "Retorno de Substituição", "address_id": endereco_base_id})
            self.shipment_item_id_counter += 1
            self.shipment_movements_mestre.append({"shipment_id": ship_id, "movement_id": id_capa_dev})

            # =========================================================
            # 📤 FASE 3: O PRESENTE (ENVIAMOS A MÁQUINA NOVA)
            # =========================================================
            self.registrar_movimento(
                id_final=mov_novo_id,
                recipient_id=recipient_id, 
                cliente_final_address_id=cliente_final_address, # Vai para o endereço do cliente!
                usuario_id=usr_subst, 
                organization_id=org_id_destino,
                mov_date=dt_subst, 
                deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                contrato_id=contrato_id_res, 
                contrato_item_id=item_id_res, 
                equipment_id_ref=eq_id_novo,
                tipo_movimento_id=2, # 🎯 CORRIGIDO: 2 = SUBSTITUIÇÃO
                operation_type='SUBSTITUICAO', # 🎯 CORRIGIDO
                is_exchange=True, # Flag de troca mantida!
                alias_movimento=row['NOME_NOVO'], 
                details_capa="Migração: Envio por Substituição", 
                details_item="Equipamento Novo (Substituto)"
            )

        print(f"\n⚠️ Registros rejeitados (Sem equipamento encontrado): {rejeitados}")
        self.salvar_movimentos_banco()
        self.salvar_shipments_banco()
        
        # Status 2 = Alugado (No Cliente) -> Máquinas Novas assumem este status. 
        # A máquina antiga foi gravada na devolucao, mas o dict vai ser sobreescrito aqui se quisermos.
        # Por segurança, atualizamos a máquina nova para "Alugado":
        eqs_novos_alterar = [{int(dict_equip_novo.get(tombo)): cli_legado_id} for tombo in df_subst['TOMBO_NOVO'].dropna()]
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