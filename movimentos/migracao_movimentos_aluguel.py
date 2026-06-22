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

df_planilha_auxiliar['TOMBO'] = pd.to_numeric(df_planilha_auxiliar['TOMBO'], errors='coerce')
tombos_unicos = df_planilha_auxiliar['TOMBO'].dropna().astype(int).unique().tolist()
lista_tombos_sql = "(" + ", ".join(map(str, tombos_unicos)) + ")"

with engine_legado.connect() as conn:
    query_movimentos_legado = f"""
    SELECT
        am.id,
        am.data,
        am.tipo_id,
        amt.nome AS tipo_nome,
        am.cliente_id,
        am.usuario_id,
        am.updated_at,
        am.deleted_at,
        ae.numero AS tombo
    FROM aluguel_movimento am
    INNER JOIN aluguel_movimento_itens ami ON ami.movimento_id = am.id
    INNER JOIN aluguel_equipamentos ae ON ae.id = ami.equipamento_id
    INNER JOIN aluguel_tipos_movimento amt ON amt.id = am.tipo_id
    WHERE am.deleted_at IS NULL
      AND ae.numero IN {lista_tombos_sql}
      AND am.id = (
          SELECT am2.id
          FROM aluguel_movimento am2
          INNER JOIN aluguel_movimento_itens ami2 ON ami2.movimento_id = am2.id
          WHERE ami2.equipamento_id = ae.id
            AND am2.deleted_at IS NULL
          ORDER BY am2.updated_at DESC, am2.data DESC
          LIMIT 1
      )
    ORDER BY ae.numero;
"""
    df_ultimo_movimento_por_tombo_sql = pd.read_sql(query_movimentos_legado, engine_legado)
    df_equipamentos_legado = pd.read_sql("SELECT id, numero, situacao_id, updated_at FROM aluguel_equipamentos", conn)
    df_clientes_legado = pd.read_sql("SELECT id FROM aluguel_clientes", conn)
    df_movimentos_legado = pd.read_sql("""
        SELECT id, data, tipo_id, cliente_id, usuario_id, updated_at, deleted_at 
        FROM aluguel_movimento 
        WHERE deleted_at IS NULL
    """, conn)
    # Padronização de ids para retirar o .0
    df_movimentos_legado['cliente_id'] = df_movimentos_legado['cliente_id'].fillna(0).astype(int)
    df_movimentos_legado['usuario_id'] = df_movimentos_legado['usuario_id'].fillna(0).astype(int)
    df_movimento_item_legado = pd.read_sql("SELECT id, movimento_id, equipamento_id FROM aluguel_movimento_itens", conn)


with engine_new.connect() as conn:
    query_contratos_itens = text("""
    SELECT
        crc.customer_id AS cliente_id,
        co.name AS contract_name,
        co.customer_id,
        ad.legacy_customer_id AS legacy_client_id,
        ad.alias AS customer_name,
        co.id AS contract_id,
        ci.id AS contract_item_id,
        ci.alias AS alias_item_contract,
        ci.description,
        ci.quantity,
        ci.available_quantity
    FROM contract_items ci
    INNER JOIN event_additives ev ON ev.id = ci.event_additive_id
    INNER JOIN contract_events ce ON ce.id = ev.event_id
    INNER JOIN contracts co ON co.id = ce.contract_id
    INNER JOIN contract_recipient_customers crc ON crc.contract_id = co.id
    INNER JOIN addresses ad ON ad.addressable_id = crc.customer_id AND ad.addressable_type = 'customer'
    ORDER BY ci.created_at DESC;
    """)
    df_contratos_itens = pd.read_sql(query_contratos_itens, conn)

    df_equipamentos_refatorado = pd.read_sql("SELECT id, number, name, current_organization_id FROM equipments", conn)
    df_contratos_refatorado = pd.read_sql("SELECT id, name, organization_id, customer_id FROM contracts", conn)
    df_enderecos_valido = pd.read_sql("SELECT id, addressable_id, legacy_customer_id FROM addresses WHERE legacy_customer_id IS NOT NULL", conn)

    query_primeiro_item = text("""
        SELECT c.customer_id, c.id AS contract_id, ci.id AS contract_item_id
        FROM contract_items ci
        JOIN event_additives ea ON ea.id = ci.event_additive_id
        JOIN contract_events ce ON ce.id = ea.event_id
        JOIN contracts c ON c.id = ce.contract_id
        ORDER BY ci.id ASC
    """)
    df_primeiro_item = pd.read_sql(query_primeiro_item, conn)
    df_primeiro_item = df_primeiro_item.drop_duplicates(subset=['customer_id'], keep='first')
    dict_primeiro_item_por_cliente = dict(zip(df_primeiro_item['customer_id'].astype(int), df_primeiro_item['contract_item_id'].astype(int)))
    dict_primeiro_item_por_cliente = dict(zip(df_primeiro_item['customer_id'].astype(int), df_primeiro_item['contract_item_id'].astype(int)))
    dict_primeiro_contrato_por_cliente = dict(zip(df_primeiro_item['customer_id'].astype(int), df_primeiro_item['contract_id'].astype(int)))
    
    query_tipo_equipamentos = text("""
        SELECT e.id AS equipment_id, p.type_id 
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

def limpar_codigo(val):
    if pd.isna(val): return ""
    s = str(val).strip()
    return s[:-2] if s.endswith(".0") else s

dict_tombo_por_equip_id = {
    row['id']: limpar_codigo(row['numero'])
    for _, row in df_equipamentos_legado.iterrows() 
    if pd.notna(row['numero'])
}
dict_equip_ref_por_number = {limpar_codigo(row['number']): row['id'] for _, row in df_equipamentos_refatorado.iterrows()}
dict_movimentos_legado = {row['id']: row for _, row in df_movimentos_legado.iterrows()}
dict_cliente_adress = dict(zip(df_enderecos_valido['legacy_customer_id'].astype(int), df_enderecos_valido['addressable_id'].astype(int)))
dict_endereco_por_legacy_client = dict(zip(df_enderecos_valido['legacy_customer_id'].astype(int), df_enderecos_valido['id'].astype(int)))

# --- DICIONÁRIO DE CONTROLE DE SALDOS (REGRA 1) ---
dict_contrato_item_por_alias = {
    (int(row['cliente_id']), str(row['alias_item_contract']).strip().upper()): {
        'id': int(row['contract_item_id']), 
        'available_quantity': int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0,
        'original_quantity':  int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0
    } 
    for _, row in df_contratos_itens.iterrows() 
    if pd.notna(row['alias_item_contract']) and pd.notna(row['cliente_id'])
}

saldos_por_id = {}
for dados in dict_contrato_item_por_alias.values():
    item_id = dados['id']
    # Garante que cada ID do banco tenha apenas um saldo rastreado
    if item_id not in saldos_por_id:
        saldos_por_id[item_id] = dados['available_quantity']

equipamentos_alterados = set()
extra_id_counter = 1
so_item_id_counter = 1 
dict_ultimo_movimento_por_tombo = {}
for _, row in df_ultimo_movimento_por_tombo_sql.iterrows():
    tombo_chave = limpar_codigo(row['tombo'])
    if tombo_chave and tombo_chave != 'nan':
        dict_ultimo_movimento_por_tombo[tombo_chave] = {
            'movimento': row.to_dict(),
            'data_dt': pd.to_datetime(row['updated_at'] if pd.notna(row['updated_at']) else row['data'])
        }

print(f"✅ {len(dict_ultimo_movimento_por_tombo)} tombos indexados com último movimento (via SQL).")

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

#Limpeza da planilha auxiliar
# 1. Tenta converter a coluna TOMBO para número. O que for texto vazio, lixo ou NaN vira 'NaN' numérico real.
df_planilha_auxiliar['TOMBO'] = pd.to_numeric(df_planilha_auxiliar['TOMBO'], errors='coerce')
df_planilha_auxiliar['CLIENTE_ID'] = pd.to_numeric(df_planilha_auxiliar['CLIENTE_ID'], errors='coerce')
# 2. Agora excluímos qualquer linha onde o TOMBO não seja um número válido (isso mata as linhas vazias e sujeiras)
df_planilha_auxiliar = df_planilha_auxiliar.dropna(subset=['TOMBO'])
# 3. Converte o TOMBO para inteiro (isso remove o .0 automaticamente) e depois para string, deixando o ID limpinho
df_planilha_auxiliar['TOMBO'] = df_planilha_auxiliar['TOMBO'].astype(int).astype(str)
df_planilha_auxiliar['CLIENTE_ID'] = df_planilha_auxiliar['CLIENTE_ID'].astype(int).astype(str)
# 4. Agora trata o ITEM_DO_CONTRATO (como já limpamos o lixo, não deve sobrar 'nan' fantasma, mas garantimos)
df_planilha_auxiliar['ITEM_DO_CONTRATO'] = df_planilha_auxiliar['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
# 5. Filtro extra de segurança: tira qualquer linha onde o item do contrato tenha virado a string 'nan'
df_planilha_auxiliar = df_planilha_auxiliar[df_planilha_auxiliar['ITEM_DO_CONTRATO'].str.lower() != 'nan']

# ==============================================================================
# DICIONÁRIO DE CONTROLE DE SALDOS — CHAVE COMPOSTA (cliente, contrato, item, descrição)
# ==============================================================================
def normalizar_texto(val):
    """Padroniza string para comparação: remove espaços extras, upper, trata nulos."""
    if pd.isna(val):
        return ""
    return str(val).strip().upper()

dict_contrato_item_por_chave = {
    (
        int(row['cliente_id']),
        normalizar_texto(row['contract_name']),
        normalizar_texto(row['alias_item_contract']),
        normalizar_texto(row['description'])
    ): {
        'id': int(row['contract_item_id']),
        'contract_id': int(row['contract_id']),
        'available_quantity': int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0,
        'original_quantity':  int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0
    }
    for _, row in df_contratos_itens.iterrows()
    if pd.notna(row['alias_item_contract']) and pd.notna(row['cliente_id'])
}

saldos_por_id = {}
for dados in dict_contrato_item_por_chave.values():
    item_id = dados['id']
    if item_id not in saldos_por_id:
        saldos_por_id[item_id] = dados['available_quantity']

print(f"✅ {len(dict_contrato_item_por_chave)} combinações únicas de (cliente, contrato, item, descrição) indexadas.")


#DEBUUG DIAGNOSTICO===============================================================
# ← DIAGNÓSTICO: compara strings da planilha vs banco para o cliente 868 (Outsourcing P&B)
print("\n🔍 Comparação de strings para debug:")
amostra_banco = [k for k in dict_contrato_item_por_chave.keys() if k[0] == 868]
print(f"   Chaves no banco para cliente 868:")
for k in amostra_banco[:5]:
    print(f"      {k}")

amostra_planilha = df_planilha_auxiliar[df_planilha_auxiliar['CLIENTE_ID'] == '868']
if not amostra_planilha.empty:
    linha = amostra_planilha.iloc[0]
    print(f"\n   Exemplo da planilha (cliente 868):")
    print(f"      CONTRATO:         '{linha.get('CONTRATO')}'")
    print(f"      ITEM_DO_CONTRATO: '{linha.get('ITEM_DO_CONTRATO')}'")
    print(f"      DESCRICAO_ITEM:   '{linha.get('DESCRICAO_ITEM')}'")

#===================================================================================
#LOOP PRINCIPAL
#===================================================================================
for index_csv, row_csv in tqdm(df_planilha_auxiliar.iterrows(), total=df_planilha_auxiliar.shape[0], desc="Processando linhas da planilha"):
    
    contrato_item_equip = str(row_csv['TOMBO']).strip()
    if contrato_item_equip in ['nan', ''] or pd.isna(row_csv['TOMBO']): 
        continue
        
    contrato_item = str(row_csv['ITEM_DO_CONTRATO']).strip()
    
    # Pula só os que sabemos que são esperados (sem contrato - ex: TRANSFORMADOR)
    if contrato_item.lower() == 'nan':
        continue


    nome_contrato_csv    = normalizar_texto(row_csv.get('CONTRATO'))
    descricao_item_csv   = normalizar_texto(row_csv.get('DESCRICAO_ITEM'))
    cliente_legado_csv  = row_csv['CLIENTE_ID']
    name_item_final     = row_csv['EQUIPAMENTO_NOME']
    ultimo_mov_info      = dict_ultimo_movimento_por_tombo.get(contrato_item_equip)
    
    if ultimo_mov_info is None:
        print(f"   ⚠️ SEM MOVIMENTO: tombo={contrato_item_equip}, item='{contrato_item}'")
        continue
        
    row_mov = ultimo_mov_info['movimento']
        
    id_final = row_mov['id']
    cliente_id_legado = row_mov['cliente_id']
    recipient_id = dict_cliente_adress.get(int(cliente_id_legado)) if pd.notna(cliente_id_legado) else None
    
    if not recipient_id:
        print(f"   ⚠️ SEM RECIPIENT: tombo={contrato_item_equip}, cliente_id_legado={cliente_id_legado}")
        continue
        
    usuario_id     = row_mov['usuario_id']
    # ← Tratamento de fallback para usuario_id nulo
    if pd.isna(usuario_id) or usuario_id == 0:
        usuario_id = 1  # ID superadmin
    else:
        usuario_id = int(usuario_id)

    tipo_id_legado = row_mov['tipo_id']
    mov_date       = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else now
    deleted_at_mov = row_mov['deleted_at'] if 'deleted_at' in row_mov and pd.notna(row_mov['deleted_at']) else None
    status_id_destino = real_tipo_movimento(tipo_id_legado) or 7
    equipment_id_ref = dict_equip_ref_por_number.get(contrato_item_equip)
    cliente_final = dict_endereco_por_legacy_client.get(int(cliente_legado_csv)) if pd.notna(cliente_legado_csv) else None
    
    #Captura do contrato ID e do item de contrato para alimentar serviço pai
    item_contrato_info = None
    contrato_id_final = None
    
    if contrato_item and recipient_id:
        chave_busca = (
            int(recipient_id),
            nome_contrato_csv,
            contrato_item.upper(),
            descricao_item_csv
        )
        item_contrato_info = dict_contrato_item_por_chave.get(chave_busca)

    if item_contrato_info:
        contrato_id_final = item_contrato_info['contract_id']
    else:
        # Fallback: Se não encontrou o item específico, pega o ID do primeiro contrato ativo do cliente
        contrato_id_final = dict_primeiro_contrato_por_cliente.get(int(recipient_id))

    # 1️⃣ Tabela: Serviços Mestre (service_orders) com os IDs resolvidos


    # 1️⃣ Tabela: Serviços Mestre 
    if id_final not in pedidos_pai_inseridos:
        servicos_mestre.append({
            "id": id_final,
            "status_id": 3,
            "movement_type_id": 1,
            "contract_id": contrato_id_final,
            "user_id": usuario_id,
            "destination_order_id": None,
            "mode_transport_id": 1,      
            "organization_id": 1378,
            "recipient_customer_id": recipient_id,
            "deadline": now,
            "details": "Migração",
            "created_at": mov_date,
            "updated_at": mov_date,
            "deleted_at": deleted_at_mov
        })
        movimentos_mestre.append({
            "id": id_final,
            "number": id_final,
            "movement_date": mov_date,
            "service_order_id": id_final,
            "recipient_customer_id": recipient_id,
            "migrate_customer_id": None,
            "organization_id": 1378,
            "status_id": 3, "created_by": usuario_id,
            "details": "Migração",
            "created_at": mov_date,
            "updated_at": mov_date,
            "deleted_at": deleted_at_mov
        })
        pedidos_pai_inseridos.add(id_final)

    # ==========================================================================
    # ⚡ FLUXO UNIFICADO DE MATCH DE CONTRATOS E CONTROLE DE SALDO (REGRAS 1 E 2)
    # ==========================================================================

    item_contrato_info = None
    if contrato_item and recipient_id:
        chave_busca = (
            int(recipient_id),
            nome_contrato_csv,
            contrato_item.upper(),
            descricao_item_csv
        )
        item_contrato_info = dict_contrato_item_por_chave.get(chave_busca)
    
    # ← DIAGNÓSTICO: mostra a chave exata buscada
    print(f"   🔑 Chave buscada: {chave_busca if contrato_item and recipient_id else 'N/A'}")

    extra_id_atual = None
    is_extra_flag = 0
    item_servico_id_atual = so_item_id_counter
    so_item_id_counter += 1
    contrato_item_id = None

    # 2. Lógica principal de abatimento (agora com a indentação correta)
    if item_contrato_info:
        contrato_item_id = item_contrato_info['id']
        
        if saldos_por_id[contrato_item_id] > 0:
            saldos_por_id[contrato_item_id] -= 1
        else:
            is_extra_flag = 1
            extra_id_atual = extra_id_counter
            service_order_item_extra_equipments.append({
                "id":                     extra_id_atual,
                "service_order_item_id":  item_servico_id_atual, 
                "contract_item_id":       contrato_item_id, # Mantém o original, apenas cria o extra
                "type_id":                dict_tipo_por_equipamento.get(equipment_id_ref),
                "quantity":               1,
                "removed_quantity":       0,
                "created_at":             mov_date,
                "updated_at":             mov_date,
                "deleted_at":             None
            })
            extra_id_counter += 1
    else:
        # 🚨 Match Falhou: Não encontrou o item no contrato DESTE cliente. Força como EXTRA.
        
        # ASSUMIR O PRIMEIRO ITEM DE CONTRATO DO CLIENTE VIA CACHE EM MEMÓRIA
        fallback_contract_item_id = dict_primeiro_item_por_cliente.get(int(recipient_id))
        
        if fallback_contract_item_id:
            is_extra_flag = 1
            contrato_item_id = int(fallback_contract_item_id) # Atualiza a variável para replicar nas outras tabelas
            extra_id_atual = extra_id_counter
            
            service_order_item_extra_equipments.append({
                "id":                     extra_id_atual,
                "service_order_item_id":  item_servico_id_atual, 
                "contract_item_id":       contrato_item_id,
                "type_id":                dict_tipo_por_equipamento.get(equipment_id_ref),
                "quantity":               1,
                "removed_quantity":       0,
                "created_at":             mov_date,
                "updated_at":             mov_date,
                "deleted_at":             None
            })
            extra_id_counter += 1
        else:
            is_extra_flag = 0
            extra_id_atual = None
            contrato_item_id = None
            print(f"⚠️ [Aviso] Cliente ID {recipient_id} sem contratos ativos no sistema.")

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
        "alias":                        None if contrato_item == '' else contrato_item, 
        "equipment_id":                 equipment_id_ref,
        "type_id":                      None,
        "product_id":                   None,
        "is_exchange":                  0,
        "is_extra":                     is_extra_flag,
        "quantity_product":             None,
        "fulfilled_quantity_product":   0,
        "quantity":                     1,
        "details":                      None if item_contrato_info else "Item Extra (Sem Match de Contrato)",
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

        # --- ATUALIZAÇÕES DE ESTADO (UPDATES OTIMIZADOS) ---
        if equipamentos_alterados:
            lista_equip_ids = list(equipamentos_alterados)
            for i in range(0, len(lista_equip_ids), 500):
                bloco = lista_equip_ids[i:i+500]
                conn.execute(
                    text("UPDATE equipments SET status_id = 2 WHERE id IN :ids"),
                    {"ids": tuple(bloco)}
                )
            print(f"  ✔️ {len(lista_equip_ids)} Equipamentos atualizados para status ALUGADO.")

        # ==============================================================================
        # ATUALIZAÇÃO DO SALDO FINAL (UNIFICADA E SEM DUPLICATAS)
        # ==============================================================================
        itens_contrato_modificados = []
        ids_ja_processados = set()
        
        # Iteramos sobre a estrutura original, mas consultamos o 'saldos_por_id' (a fonte da verdade)
        for dados in dict_contrato_item_por_chave.values():
            item_id = dados['id']
            qtd_original = dados['original_quantity']
            qtd_atual = saldos_por_id[item_id]
            
            if qtd_atual != qtd_original and item_id not in ids_ja_processados:
                itens_contrato_modificados.append({
                    "id": item_id, 
                    "nova_qtd": qtd_atual
                })
                ids_ja_processados.add(item_id)
        
        if itens_contrato_modificados:
            for item in itens_contrato_modificados:
                conn.execute(
                    text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"),
                    {"nova_qtd": item['nova_qtd'], "id": item['id']}
                )

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