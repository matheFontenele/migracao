import os
import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, text
from datetime import datetime
from tqdm import tqdm

# ==============================================================================
# CONFIGURAÇÕES DE MAPEAMENTO E BLOQUEIO DE ORGANIZAÇÕES
# ==============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_IMPORTACAO = os.path.join(BASE_DIR, "docs", "equipeAS.csv")

MAPPING_ALUCOM = {1327, 1329, 1353, 1363, 1365, 1367, 1370, 1373, 1376, 1377}
MAPPING_IP = {1346, 1349, 1350, 1364, 1368, 1371}
MAPPING_MOREIA = {1313, 1326, 1328, 1358, 1369}
MAPPING_AS = {1378}
ORGANIZACOES_BLOQUEADAS = {1123, 1366}

DB_CONFIG_NEW = {
    "host": "localhost", "port": "3307", "db": "controle-interno", "user": "root", "pass": "root"
}
DB_CONFIG_LEGADO = {
    "host": "172.16.0.200", "port": "3310", "db": "aluguel_legado", "user": "root", "pass": "1234"
}

engine_new = create_engine(
    f"mysql+pymysql://{DB_CONFIG_NEW['user']}:{DB_CONFIG_NEW['pass']}@{DB_CONFIG_NEW['host']}:{DB_CONFIG_NEW['port']}/{DB_CONFIG_NEW['db']}"
)
engine_legado = create_engine(
    f"mysql+pymysql://{DB_CONFIG_LEGADO['user']}:{DB_CONFIG_LEGADO['pass']}@{DB_CONFIG_LEGADO['host']}:{DB_CONFIG_LEGADO['port']}/{DB_CONFIG_LEGADO['db']}"
)

# ==============================================================================
# LIMPEZA DE TABELAS
# ==============================================================================

def limpar_tabelas_refatoradas(engine):
    print("🧹 Iniciando a limpeza das tabelas no banco refatorado...")
    
    tabelas_para_limpar = [
        "service_order_item_extra_equipments",
        "movement_items",       
        "movements",            
        "service_order_items",  
        "service_orders"        
    ]
    
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))
            for tabela in tabelas_para_limpar:
                conn.execute(text(f"TRUNCATE TABLE {tabela};"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
            trans.commit()
        except Exception as e:
            trans.rollback()
            print(f"❌ Erro crítico ao limpar o banco refatorado: {e}")
            raise e

limpar_tabelas_refatoradas(engine_new)

# ==============================================================================
# IMPORTAÇÃO DOS DADOS
# ==============================================================================
print("📖 Carregando dados do arquivo auxiliar e dos bancos de dados...")

df_planilha_auxiliar = pd.read_csv(ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)

with engine_legado.connect() as conn:
    df_equipamentos_legado = pd.read_sql("SELECT id, numero, situacao_id, updated_at FROM aluguel_equipamentos", conn)
    df_clientes_legado = pd.read_sql("SELECT id FROM aluguel_clientes", conn)
    df_movimentos_legado = pd.read_sql("""
        SELECT id, data, tipo_id, cliente_id, usuario_id, updated_at, deleted_at 
        FROM aluguel_movimento 
        WHERE deleted_at IS NULL
    """, conn)
    df_movimento_item_legado = pd.read_sql("SELECT id, movimento_id, equipamento_id FROM aluguel_movimento_itens", conn)

with engine_new.connect() as conn:
    df_equipamentos_refatorado = pd.read_sql("SELECT id, number, name, current_organization_id FROM equipments", conn)
    df_contratos_refatorado = pd.read_sql("SELECT id, name, organization_id, customer_id FROM contracts", conn)
    df_contratos_itens = pd.read_sql("SELECT id, event_additive_id, alias, description, quantity, available_quantity FROM contract_items", conn)
    df_enderecos_valido = pd.read_sql("SELECT id, addressable_id, legacy_customer_id FROM addresses WHERE legacy_customer_id IS NOT NULL", conn)
    
    query_tipo_equipamentos = text("""
        SELECT 
            e.id AS equipment_id, 
            p.type_id 
        FROM equipments e
        JOIN product_items pi ON e.product_item_id = pi.id
        JOIN products p ON pi.product_id = p.id
        WHERE p.type_id IS NOT NULL
    """)

    result_tipos = conn.execute(query_tipo_equipamentos).fetchall()
    dict_tipo_por_equipamento = {row.equipment_id: row.type_id for row in result_tipos}

now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ==============================================================================
# INDEXAÇÃO DE BUSCA RÁPIDA (O(1)) E ESTRUTURAS DE CONTROLE
# ==============================================================================
print("⚡ Indexando dicionários e mapeando histórico de movimentos...")

dict_tombo_por_equip_id = {
    row['id']: str(row['numero']).strip().replace('.0', '') 
    for _, row in df_equipamentos_legado.iterrows() 
    if pd.notna(row['numero'])
}

dict_equip_ref_por_number = {str(row['number']): row['id'] for _, row in df_equipamentos_refatorado.iterrows()}
dict_movimentos_legado = {row['id']: row for _, row in df_movimentos_legado.iterrows()}
dict_cliente_adress = dict(zip(df_enderecos_valido['legacy_customer_id'].astype(int), df_enderecos_valido['addressable_id'].astype(int)))
dict_endereco_por_legacy_client = dict(zip(df_enderecos_valido['legacy_customer_id'].astype(int), df_enderecos_valido['id'].astype(int)))
dict_contrato_item_por_alias = {
    str(row['alias']): {
        'id': row['id'], 
        'available_quantity': int(row['available_quantity']), # Saldo que será decrementado
        'original_quantity': int(row['available_quantity'])    # Para validar se houve mudança
    } for _, row in df_contratos_itens.iterrows()
}

equipamentos_alterados = set()
extra_id_counter = 1
so_item_id_counter = 1 

dict_ultimo_movimento_por_tombo = {}

for _, row_item in df_movimento_item_legado.iterrows():
    mov_id = row_item['movimento_id']
    equip_id = row_item['equipamento_id']

    tombo_chave = dict_tombo_por_equip_id.get(equip_id)
    if not tombo_chave or tombo_chave == 'nan':
        continue
    
    mov_reg = dict_movimentos_legado.get(mov_id)
    if mov_reg is not None:
        data_mov = mov_reg['updated_at'] if pd.notna(mov_reg['updated_at']) else mov_reg['data']
        data_mov_dt = pd.to_datetime(data_mov)
        
        if tombo_chave not in dict_ultimo_movimento_por_tombo:
            dict_ultimo_movimento_por_tombo[tombo_chave] = {'movimento': mov_reg, 'data_dt': data_mov_dt}
        else:
            if data_mov_dt > dict_ultimo_movimento_por_tombo[tombo_chave]['data_dt']:
                dict_ultimo_movimento_por_tombo[tombo_chave] = {'movimento': mov_reg, 'data_dt': data_mov_dt}

# ==============================================================================
# PROCESSAMENTO PRINCIPAL (DRIVEN BY PLANILHA AUXILIAR)
# ==============================================================================
def real_tipo_movimento(tipo_id):
    mapeamento = {1: 1, 2: 3, 3: 2, 4: None, 5: 2, 6: None, 7: 4, 8: None}
    return mapeamento.get(tipo_id)

servicos_mestre = []
service_itens_mestre = []
movimentos_mestre = []
movimento_itens_mestre = []
service_order_item_extra_equipments = []

pedidos_pai_inseridos = set()

df_planilha_auxiliar['TOMBO'] = df_planilha_auxiliar['TOMBO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)

for index_csv, row_csv in tqdm(df_planilha_auxiliar.iterrows(), total=df_planilha_auxiliar.shape[0], desc="Processando linhas da planilha"):
    
    contrato_item_equip = row_csv['TOMBO']
    if contrato_item_equip == 'nan' or not contrato_item_equip:
        continue
        
    contrato_item = str(row_csv['ITEM_DO_CONTRATO']).strip()
    item_contrato_info = dict_contrato_item_por_alias.get(contrato_item)
    cliente_legado_csv  = row_csv['CLIENTE_ID']
    name_item_final     = row_csv['EQUIPAMENTO_NOME']
    
    ultimo_mov_info = dict_ultimo_movimento_por_tombo.get(contrato_item_equip)
    
    if ultimo_mov_info is not None:
        row_mov = ultimo_mov_info['movimento']
        
        if row_mov['tipo_id'] == 1:
            
            id_final           = row_mov['id']
            cliente_id_legado  = row_mov['cliente_id']
            
            if pd.notna(cliente_id_legado):
                recipient_id = dict_cliente_adress.get(int(cliente_id_legado))
            else:
                recipient_id = None
            
            if not recipient_id:
                print(f"\n⚠️ Ignorado: Mov. {id_final} (Tombo {contrato_item_equip}). Cliente {cliente_id_legado} não mapeado in 'addresses'.")
                continue
                
            usuario_id     = row_mov['usuario_id']
            tipo_id_legado = row_mov['tipo_id']
            mov_date       = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else now
            deleted_at_mov = row_mov['deleted_at'] if 'deleted_at' in row_mov and pd.notna(row_mov['deleted_at']) else None
            status_id_destino = real_tipo_movimento(tipo_id_legado) or 7
            
            equipment_id_ref = dict_equip_ref_por_number.get(contrato_item_equip)
            
            if pd.notna(cliente_legado_csv):
                cliente_final = dict_endereco_por_legacy_client.get(int(cliente_legado_csv))
            else:
                cliente_final = None
            
            item_contrato_info = dict_contrato_item_por_alias.get(contrato_item)
            if item_contrato_info:
                contrato_item_id = item_contrato_info['id']
                qtd_disponivel = item_contrato_info['available_quantity']
            else:
                contrato_item_id = None
                qtd_disponivel = 0

            # 1️⃣ Tabela: Serviços Mestre (Garante a existência do ID pai)
            if id_final not in pedidos_pai_inseridos:
                servicos_mestre.append({
                    "id":                           id_final,
                    "status_id":                    1,
                    "movement_type_id":             None,
                    "contract_id":                  None,
                    "user_id":                      usuario_id,
                    "destination_order_id":         None,
                    "mode_transport_id":            1,      
                    "organization_id":              1378,  
                    "recipient_customer_id":        recipient_id,
                    "deadline":                     now,
                    "details":                      "Migração",
                    "created_at":                   mov_date,
                    "updated_at":                   mov_date,
                    "deleted_at":                   deleted_at_mov
                })
                
                movimentos_mestre.append({
                    "id":                           id_final,
                    "number":                       id_final,
                    "movement_date":                mov_date,
                    "service_order_id":             id_final,
                    "recipient_customer_id":        recipient_id,
                    "migrate_customer_id":          None,
                    "organization_id":              1378,
                    "status_id":                    status_id_destino,
                    "created_by":                   usuario_id,
                    "details":                      "Migração",
                    "created_at":                   mov_date,
                    "updated_at":                   mov_date,
                    "deleted_at":                   deleted_at_mov
                })
                
                pedidos_pai_inseridos.add(id_final)
            
            if id_final not in pedidos_pai_inseridos:
                print(f"⚠️ Ignorado item do Tombo {contrato_item_equip}: O ID Pai {id_final} não foi gerado.")
                continue

            item_contrato_info = dict_contrato_item_por_alias.get(contrato_item)
            
            extra_id_atual = None
            item_servico_id_atual = so_item_id_counter
            so_item_id_counter += 1

            if item_contrato_info:
                item_contrato_info['available_quantity'] -= 1
                
                if item_contrato_info['available_quantity'] < 0:
                    extra_id_atual = extra_id_counter
                    service_order_item_extra_equipments.append({
                        "id":                     extra_id_atual,
                        "service_order_item_id":  item_servico_id_atual, 
                        "contract_item_id":       item_contrato_info['id'],
                        "type_id":                dict_tipo_por_equipamento.get(equipment_id_ref),
                        "quantity":               1,
                        "removed_quantity":       0,
                        "created_at":             mov_date,
                        "updated_at":             mov_date,
                        "deleted_at":             None
                    })
                    extra_id_counter += 1
                else:
                    item_contrato_info["available_quantity"] = item_contrato_info["available_quantity"] - 1

            if equipment_id_ref:
                equipamentos_alterados.add(equipment_id_ref)
            
            # 2️⃣ Tabela: Itens do Serviço Mestre
            service_itens_mestre.append({
                "id":                           item_servico_id_atual,
                "status_id":                    3,
                "service_order_id":             id_final,
                "department_id":                2,
                "movement_type_id":             1,
                "contract_item_id":             contrato_item_id,
                "alias":                        contrato_item,
                "equipment_id":                 equipment_id_ref,
                "type_id":                      None,
                "product_id":                   None,
                "is_exchange":                  0,
                "is_extra":                     1 if extra_id_atual else 0,
                "quantity_product":             None,
                "fulfilled_quantity_product":   0,
                "quantity":                     1,
                "details":                      None,
                "address_id":                   cliente_final,
                "location_id":                  None,
                "created_at":                   mov_date,
                "updated_at":                   mov_date,
                "deleted_at":                   deleted_at_mov
            })
            
            # 4️⃣ Tabela: Itens do Movimento Mestre
            movimento_itens_mestre.append({
                "movement_id":                  id_final,
                "movement_type_id":             1,
                "service_order_item_id":        item_servico_id_atual,
                "equipment_id":                 equipment_id_ref,
                "extra_id":                     extra_id_atual,
                "status_id":                    3,
                "product_item_id":              None,
                "alias":                        name_item_final,
                "old_organization_id":          None,
                "new_organization_id":          None,
                "operation_type":               'ALUGUEL',
                "confirmed_at":                 None,
                "confirmed_by":                 None,
                "created_at":                   mov_date,
                "updated_at":                   mov_date,
                "deleted_at":                   deleted_at_mov
            })

# ==============================================================================
#  SALVAMENTO E ATUALIZAÇÕES EM LOTE
# ==============================================================================
print("\n🚀 Enviando novos registros e aplicando atualizações de estado no banco...")

with engine_new.connect() as conn:
    trans = conn.begin()
    try:
        if servicos_mestre:
            pd.DataFrame(servicos_mestre).to_sql("service_orders", con=conn, if_exists="append", index=False)
            print(f"  ✔️ {len(servicos_mestre)} Registros inseridos em 'service_orders'.")
            
        if service_itens_mestre:
            pd.DataFrame(service_itens_mestre).to_sql("service_order_items", con=conn, if_exists="append", index=False)
            print(f"  ✔️ {len(service_itens_mestre)} Registros inseridos em 'service_order_items'.")
            
        if movimentos_mestre:
            pd.DataFrame(movimentos_mestre).to_sql("movements", con=conn, if_exists="append", index=False)
            print(f"  ✔️ {len(movimentos_mestre)} Registros inseridos em 'movements'.")
            
        if movimento_itens_mestre:
            pd.DataFrame(movimento_itens_mestre).to_sql("movement_items", con=conn, if_exists="append", index=False)
            print(f"  ✔️ {len(movimento_itens_mestre)} Registros inseridos em 'movement_items'.")

        if service_order_item_extra_equipments:
            pd.DataFrame(service_order_item_extra_equipments).to_sql("service_order_item_extra_equipments", con=conn, if_exists="append", index=False)
            print(f"  ✔️ {len(service_order_item_extra_equipments)} Registros inseridos em 'service_order_item_extra_equipments'.")

        # --- ATUALIZAÇÕES DE ESTADO (UPDATES) ---
        if equipamentos_alterados:
            lista_equip_ids = list(equipamentos_alterados)
            for i in range(0, len(lista_equip_ids), 500):
                bloco = lista_equip_ids[i:i+500]
                conn.execute(
                    text("UPDATE equipments SET status_id = 2 WHERE id IN :ids"),
                    {"ids": bloco}
                )
            print(f"  ✔️ {len(lista_equip_ids)} Equipamentos atualizados para status ALUGADO.")

        itens_contrato_modificados = []
        for alias, dados in dict_contrato_item_por_alias.items():
            if dados['available_quantity'] != dados['original_quantity']:
                itens_contrato_modificados.append({
                    "id": dados['id'], 
                    "nova_qtd": dados['available_quantity']
                })
        
        if itens_contrato_modificados:
            # Garante que o update rode item a item ou em lote de forma limpa
            for item in itens_contrato_modificados:
                conn.execute(
                    text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"),
                    item
                )
            print(f"  ✔️ {len(itens_contrato_modificados)} Itens de contrato atualizados.")

        trans.commit()
        print("🎉 Processo completo concluído com sucesso!")
        
    except Exception as e:
        trans.rollback()
        print(f"❌ Erro crítico ao atualizar os estados no banco novo: {e}")
        raise e

# ==============================================================================
# EXIBIÇÃO DE RESULTADOS
# ==============================================================================
print("\n--- 🏁 Resumo dos Dados Mestre Gerados (Filtrados) ---")
print(f"📦 Serviços Criados: {len(servicos_mestre)}")
print(f"📦 Movimentos Criados: {len(movimentos_mestre)}")
print(f"📦 Itens de Serviços: {len(service_itens_mestre)}")
print(f"📦 Itens de Movimentos: {len(movimento_itens_mestre)}")
print(f"📦 Equipamentos Extras Identificados: {len(service_order_item_extra_equipments)}")