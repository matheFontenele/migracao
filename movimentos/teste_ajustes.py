import os
import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, text
from datetime import datetime
from tqdm import tqdm

# ==============================================================================
# CONFIGURAÇÕES DE CONEXÃO
# ==============================================================================
ARQUIVO_IMPORTACAO = "equipeAS.csv"

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
# FUNÇÃO DE LIMPEZA EXPANDIDA
# ==============================================================================
def limpar_tabelas_refatoradas(engine):
    print("🧹 Iniciando a limpeza das tabelas no banco refatorado...")
    tabelas_para_limpar = [
        "service_order_item_extra_equipments", # Nova tabela de extras inclusa na limpeza
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
                print(f"  ✔️ Tabela '{tabela}' zerada.")
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
            trans.commit()
            print("✨ Banco refatorado limpo com sucesso!\n")
        except Exception as e:
            trans.rollback()
            print(f"❌ Erro ao limpar tabelas: {e}")
            raise e

# Executa a limpeza inicial
limpar_tabelas_refatoradas(engine_new)

# ==============================================================================
# IMPORTAÇÃO DOS DADOS
# ==============================================================================
print("📖 Carregando dados...")
df_planilha_auxiliar = pd.read_csv(ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)

with engine_legado.connect() as conn:
    df_movimentos_legado = pd.read_sql("SELECT id, data, tipo_id, cliente_id, usuario_id, updated_at, deleted_at FROM aluguel_movimento WHERE deleted_at IS NULL", conn)
    df_movimento_item_legado = pd.read_sql("SELECT id, movimento_id, equipamento_id FROM aluguel_movimentos_itens", conn)

with engine_new.connect() as conn:
    df_equipamentos_refatorado = pd.read_sql("SELECT id, number, name FROM equipments", conn)
    df_contratos_itens = pd.read_sql("SELECT id, alias, available_quantity FROM contract_items", conn)
    df_enderecos_clientes_refatorado = pd.read_sql("SELECT id, legacy_customer_id FROM addresses", conn)

now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ==============================================================================
# INDEXAÇÃO EM MEMÓRIA O(1) E ESTRUTURAS DE CONTROLE
# ==============================================================================
print("⚡ Indexando dicionários e preparando tracking de estoque...")

dict_equip_ref_por_number = {str(row['number']): row['id'] for _, row in df_equipamentos_refatorado.iterrows()}
dict_endereco_por_legacy_client = {row['legacy_customer_id']: row['id'] for _, row in df_enderecos_clientes_refatorado.iterrows()}
dict_movimentos_legado = {row['id']: row for _, row in df_movimentos_legado.iterrows()}

# 💡 Novo mapa de itens de contrato contendo ID e a quantidade disponível para podermos decrementar em memória
dict_contrato_item_por_alias = {
    str(row['alias']): {
        'id': row['id'], 
        'available_quantity': int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0,
        'original_quantity': int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0
    } for _, row in df_contratos_itens.iterrows()
}

# Estruturas para rastrear alterações que vão pro banco depois do loop
equipamentos_alterados = set() # Guarda IDs dos equipamentos que mudarão para status_id = 2
extra_id_counter = 1           # Simulador do ID Auto-Increment da tabela de extras

# Mapeia o último movimento de cada equipamento
dict_ultimo_movimento_por_equip = {}
for _, row_item in df_movimento_item_legado.iterrows():
    mov_id, equip_id = row_item['movimento_id'], row_item['equipamento_id']
    mov_reg = dict_movimentos_legado.get(mov_id)
    if mov_reg is not None:
        data_mov = mov_reg['updated_at'] if pd.notna(mov_reg['updated_at']) else mov_reg['data']
        data_mov_dt = pd.to_datetime(data_mov)
        if equip_id not in dict_ultimo_movimento_por_equip or data_mov_dt > dict_ultimo_movimento_por_equip[equip_id]['data_dt']:
            dict_ultimo_movimento_por_equip[equip_id] = {'movimento': mov_reg, 'data_dt': data_mov_dt}

# ==============================================================================
# LOOP PRINCIPAL
# ==============================================================================
servicos_mestre = []
service_itens_mestre = []
movimentos_mestre = []
movimento_itens_mestre = []
service_order_item_extra_equipments = [] # Nova lista mestre

for index_csv, row_csv in tqdm(df_planilha_auxiliar.iterrows(), total=df_planilha_auxiliar.shape[0], desc="Processando linhas da planilha"):
    
    legacy_equip_id     = row_csv['EQUIPAMENTO_ID'] 
    contrato_item_equip = str(row_csv['TOMBO'])
    contrato_item       = str(row_csv['ITEM_DO_CONTRATO'])
    cliente_legado_csv  = row_csv['CLIENTE_ID']
    name_item_final     = row_csv['EQUIPAMENTO_NOME']
    
    ultimo_mov_info = dict_ultimo_movimento_por_equip.get(legacy_equip_id)
    
    if ultimo_mov_info is not None:
        row_mov = ultimo_mov_info['movimento']
        
        if row_mov['tipo_id'] == 1:
            id_final       = row_mov['id']
            recipient_id   = row_mov['cliente_id']
            usuario_id     = row_mov['usuario_id']
            mov_date       = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else now
            deleted_at_mov = row_mov['deleted_at'] if 'deleted_at' in row_mov and pd.notna(row_mov['deleted_at']) else None
            
            equipment_id_ref = dict_equip_ref_por_number.get(contrato_item_equip)
            cliente_final    = dict_endereco_por_legacy_client.get(cliente_legado_csv)
            
            # Mapeia informações dinâmicas do item do contrato
            contrato_item_id = None
            extra_id_atual = None
            
            item_contrato_info = dict_contrato_item_por_alias.get(contrato_item)
            if item_contrato_info is not None:
                contrato_item_id = item_contrato_info['id']
                qtd_disponivel = item_contrato_info['available_quantity']
                
                # 🎯 REGRA DO EXTRA: Se a quantidade disponível atual na memória for 0
                if qtd_disponivel == 0:
                    extra_id_atual = extra_id_counter
                    service_order_item_extra_equipments.append({
                        "id":                     extra_id_atual,
                        "service_order_item_id":  id_final, # ID do Service Order Atual
                        "contract_item_id":       contrato_item_id,
                        "quantity":               1,
                        "removed_quantity":       0,
                        "created_at":             mov_date,
                        "updated_at":             mov_date,
                        "deleted_at":             None
                    })
                    extra_id_counter += 1
                
                # Decrementa o saldo do contrato na memória (independente se foi para o negativo ou não)
                item_contrato_info['available_quantity'] -= 1

            # Rastreia o equipamento coletado para alterar o status_id para 2 posteriormente
            if equipment_id_ref:
                equipamentos_alterados.add(equipment_id_ref)
            
            # 1️⃣ Serviços Mestre
            servicos_mestre.append({
                "id": id_final, "status_id": 1, "movement_type_id": None, "contract_id": None,
                "user_id": usuario_id, "destination_order_id": None, "mode_transport_id": 1,      
                "organization_id": 1378, "recipient_customer_id": recipient_id, "maintenance_created_movement": 0,
                "deadline": now, "details": "Migração ativa", "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
            })
            
            # 2️⃣ Itens do Serviço Mestre
            service_itens_mestre.append({
                "status_id": 3, "service_order_id": id_final, "department_id": 2, "movement_type_id": 1,
                "contract_item_id": contrato_item_id, "alias": contrato_item, "equipment_id": equipment_id_ref,
                "type_id": None, "product_id": None, "is_exchange": 0, "is_extra": 1 if extra_id_atual else 0,
                "quantity_product": None, "fulfilled_quantity_product": 0, "quantity": 1, "details": None,
                "address_id": cliente_final, "location_id": None, "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
            })
            
            # 3️⃣ Movimentos Mestre
            movimentos_mestre.append({
                "id": id_final, "number": id_final, "movement_date": mov_date, "service_order_id": id_final,
                "recipient_customer_id": recipient_id, "migrate_customer_id": None, "organization_id": 1378,
                "status_id": 1, "created_by": usuario_id, "details": None, "maintenance_created_movement": 0,
                "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
            })
            
            # 4️⃣ Itens do Movimento Mestre
            movimento_itens_mestre.append({
                "moviment_id":                  id_final,
                "moviment_type_id":             1,
                "service_order_item_id":        None,
                "equipment_id":                 equipment_id_ref,
                "extra_id":                     extra_id_atual, # 🎯 Recebe o ID gerado ou None
                "status_id":                    3,
                "product_item_id":              None,
                "alias":                        name_item_final,
                "old_organization_id":          None,
                "new_organization_id":          None,
                "operation_type":               'ALUGUEL',
                "confirmed_at":                 None, "confirmed_by":                 None,
                "created_at":                   mov_date, "updated_at":                   mov_date, "deleted_at":                   deleted_at_mov
            })

# ==============================================================================
# 🔥 SALVAMENTO E ATUALIZAÇÕES EM LOTE (BULK UPDATE) NO BANCO REFATORADO
# ==============================================================================
print("\n🚀 Enviando novos registros e aplicando atualizações de estado no banco...")

with engine_new.connect() as conn:
    trans = conn.begin()
    try:
        # 1. Executa os Updates em Lote para a tabela de EQUIPMENTS (status_id = 2)
        if equipamentos_alterados:
            lista_equip_ids = list(equipamentos_alterados)
            # Dividindo em blocos de 500 para evitar estourar o limite de parâmetros do banco
            for i in range(0, len(lista_equip_ids), 500):
                bloco = lista_equip_ids[i:i+500]
                conn.execute(
                    text("UPDATE equipments SET status_id = 2 WHERE id IN :ids"),
                    {"ids": bloco}
                )
            print(f"  ✔️ {len(lista_equip_ids)} Equipamentos atualizados para status_id = 2.")

        # 2. Executa os Updates em Lote para CONTRACT_ITEMS (available_quantity atualizado)
        itens_contrato_modificados = [
            {"id": dados['id'], "nova_qtd": dados['available_quantity']}
            for dados in dict_contrato_item_por_alias.values()
            if dados['available_quantity'] != dados['original_quantity']
        ]
        
        if itens_contrato_modificados:
            # Comando otimizado executado em lote nativo via drivers executemany
            conn.execute(
                text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"),
                itens_contrato_modificados
            )
            print(f"  ✔️ {len(itens_contrato_modificados)} Itens de contrato tiveram seus saldos atualizados.")

        trans.commit()
        print("🎉 Processo completo concluído com integridade de dados e alta performance!")
        
    except Exception as e:
        trans.rollback()
        print(f"❌ Erro crítico ao atualizar os estados no banco novo: {e}")
        raise e

print(f"\n📋 Total de Extras gerados: {len(service_order_item_extra_equipments)}")