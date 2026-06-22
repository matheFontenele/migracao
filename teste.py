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
    df_equipamentos_legado = pd.read_sql("SELECT id, numero, situacao_id, updated_at FROM aluguel_equipamentos WHERE deleted_at IS NULL", conn)
    df_clientes_legado = pd.read_sql("SELECT id FROM aluguel_clientes", conn)
    df_movimentos_legado = pd.read_sql("""
        SELECT id, data, tipo_id, cliente_id, usuario_id, updated_at, deleted_at 
        FROM aluguel_movimento 
        WHERE deleted_at IS NULL
    """, conn)
    df_movimento_item_legado = pd.read_sql("SELECT id, movimento_id, equipamento_id FROM aluguel_movimento_itens WHERE deleted_at IS NULL", conn)

with engine_new.connect() as conn:
    df_equipamentos_refatorado = pd.read_sql("SELECT id, number, name, current_organization_id FROM equipments", conn)
    df_contratos_refatorado = pd.read_sql("SELECT id, name, organization_id, customer_id FROM contracts", conn)
    df_contratos_itens = pd.read_sql("SELECT id, event_additive_id, alias, description, quantity, available_quantity FROM contract_items", conn)
    df_enderecos_valido = pd.read_sql("SELECT id, addressable_id, legacy_customer_id FROM addresses WHERE legacy_customer_id IS NOT NULL", conn)

    query_primeiro_item = text("""
        SELECT c.customer_id, ci.id AS contract_item_id
        FROM contract_items ci
        JOIN event_additives ea ON ea.id = ci.event_additive_id
        JOIN contract_events ce ON ce.id = ea.event_id
        JOIN contracts c ON c.id = ce.contract_id
        ORDER BY ci.id ASC
    """)
    df_primeiro_item = pd.read_sql(query_primeiro_item, conn)
    df_primeiro_item = df_primeiro_item.drop_duplicates(subset=['customer_id'], keep='first')
    dict_primeiro_item_por_cliente = dict(zip(df_primeiro_item['customer_id'].astype(int), df_primeiro_item['contract_item_id'].astype(int)))
    
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
    str(row['alias']).strip(): {   # ← .strip() remove espaços invisíveis
        'id': row['id'], 
        'available_quantity': int(row['available_quantity']),
        'original_quantity':  int(row['available_quantity'])
    } 
    for _, row in df_contratos_itens.iterrows() 
    if pd.notna(row['alias'])
}

equipamentos_alterados = set()
extra_id_counter = 1
so_item_id_counter = 1 
dict_ultimo_movimento_por_tombo = {}

for _, row_item in df_movimento_item_legado.iterrows():
    mov_id = row_item['movimento_id']
    equip_id = row_item['equipamento_id']
    tombo_chave = dict_tombo_por_equip_id.get(equip_id)
    if not tombo_chave or tombo_chave == 'nan': continue
    
    mov_reg = dict_movimentos_legado.get(mov_id)
    if mov_reg is not None:
        data_mov = mov_reg['updated_at'] if pd.notna(mov_reg['updated_at']) else mov_reg['data']
        data_mov_dt = pd.to_datetime(data_mov)
        
        if tombo_chave not in dict_ultimo_movimento_por_tombo or data_mov_dt > dict_ultimo_movimento_por_tombo[tombo_chave]['data_dt']:
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

#Limpeza da planilha auxiliar
df_planilha_auxiliar['TOMBO'] = pd.to_numeric(df_planilha_auxiliar['TOMBO'], errors='coerce')
df_planilha_auxiliar = df_planilha_auxiliar.dropna(subset=['TOMBO'])
df_planilha_auxiliar['TOMBO'] = df_planilha_auxiliar['TOMBO'].astype(int).astype(str)
df_planilha_auxiliar['ITEM_DO_CONTRATO'] = df_planilha_auxiliar['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
df_planilha_auxiliar = df_planilha_auxiliar[df_planilha_auxiliar['ITEM_DO_CONTRATO'].str.lower() != 'nan']

for index_csv, row_csv in tqdm(df_planilha_auxiliar.iterrows(), total=df_planilha_auxiliar.shape[0], desc="Processando linhas da planilha"):
    
    contrato_item_equip = str(row_csv['TOMBO']).strip()

    if contrato_item_equip in ['nan', ''] or pd.isna(row_csv['TOMBO']): continue
        
    contrato_item = str(row_csv['ITEM_DO_CONTRATO']).strip()

    #if contrato_item.lower() == 'nan': contrato_item = ''
        
    cliente_legado_csv  = row_csv['CLIENTE_ID']
    name_item_final     = row_csv['EQUIPAMENTO_NOME']
    ultimo_mov_info     = dict_ultimo_movimento_por_tombo.get(contrato_item_equip)
    
    if ultimo_mov_info is None: continue
    row_mov = ultimo_mov_info['movimento']
    
    if row_mov['tipo_id'] != 1: continue
        
    id_final = row_mov['id']
    cliente_id_legado = row_mov['cliente_id']
    recipient_id = dict_cliente_adress.get(int(cliente_id_legado)) if pd.notna(cliente_id_legado) else None
    
    if not recipient_id: continue
        
    usuario_id     = row_mov['usuario_id']
    tipo_id_legado = row_mov['tipo_id']
    mov_date       = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else now
    deleted_at_mov = row_mov['deleted_at'] if 'deleted_at' in row_mov and pd.notna(row_mov['deleted_at']) else None
    status_id_destino = real_tipo_movimento(tipo_id_legado) or 7
    equipment_id_ref = dict_equip_ref_por_number.get(contrato_item_equip)
    cliente_final = dict_endereco_por_legacy_client.get(int(cliente_legado_csv)) if pd.notna(cliente_legado_csv) else None
