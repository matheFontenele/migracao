import pandas as pd
import sqlalchemy as sa
import time
from datetime import datetime
from tqdm import tqdm
from sqlalchemy import text

ARQUIVO_IMPORTACAO = "planilha_equipamentos.csv"

MAPPING_ALUCOM = {1327, 1329, 1353, 1363, 1365, 1367, 1370, 1373, 1376, 1377}
MAPPING_IP = {1346, 1349, 1350, 1364, 1368, 1371}
MAPPING_MOREIA = {1313, 1326, 1328, 1358, 1369}
MAPPING_AS = {1378}

DB_LEGADO = {
    "host": "172.16.0.200",
    "port": "3310",
    "db":   "aluguel_legado",
    "user": "root",
    "pass": "1234"
}
DB_NOVO = {
    "host": "localhost",
    "port": "3307",
    "db":   "controle-interno",
    "user": "root",
    "pass": "root"
}

engine_legado = sa.create_engine(f"mysql+pymysql://{DB_LEGADO['user']}:{DB_LEGADO['pass']}@{DB_LEGADO['host']}:{DB_LEGADO['port']}/{DB_LEGADO['db']}")
engine_novo   = sa.create_engine(f"mysql+pymysql://{DB_NOVO['user']}:{DB_NOVO['pass']}@{DB_NOVO['host']}:{DB_NOVO['port']}/{DB_NOVO['db']}")

# ==============================================================================
# IMPORTAÇÃO DE DADOS DA PLANILHA E LEGADO
# ==============================================================================
print("📖 Carregando dados da planilha e do banco legado...")
df_equipamentos = pd.read_csv(ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)

with engine_legado.connect() as conn:
    df_equipamentos_legado = pd.read_sql("SELECT id, orgao_id, situacao_id, created_at, updated_at, deleted_at FROM aluguel_equipamentos", conn)

mapa_equipamento_orgao = dict(zip(df_equipamentos_legado['id'], df_equipamentos_legado['orgao_id']))
mapa_equipamento_situacao = dict(zip(df_equipamentos_legado['id'], df_equipamentos_legado['situacao_id']))
now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
mapa_datas_legado = {
    row['id']: {
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'deleted_at': row['deleted_at']
    }
    for _, row in df_equipamentos_legado.iterrows()
}

lista_fornecedores_legado = df_equipamentos['MARCA_AJUSTADA'].dropna().unique()

# ==============================================================================
# PROCESSAMENTO DOS DADOS
# ==============================================================================
def descobrir_id_organizacao_destino(id_legado):
    if pd.isna(id_legado):
        return 1115
    id_legado_int = int(id_legado)
    if id_legado_int in MAPPING_ALUCOM:  return 1115
    elif id_legado_int in MAPPING_IP:    return 1311
    elif id_legado_int in MAPPING_MOREIA: return 1122
    elif id_legado_int in MAPPING_AS:    return 1378
    return id_legado_int

lista_mestre = []
for index, row in tqdm(df_equipamentos.iterrows(), total=df_equipamentos.shape[0], desc="Refatorando dados"):
    id_equipamento_legado = row.get("ID_LEGADO")
    
    id_orgao_legado = mapa_equipamento_orgao.get(id_equipamento_legado, None)
    id_situacao_legado = mapa_equipamento_situacao.get(id_equipamento_legado, None)
    org_destino = descobrir_id_organizacao_destino(id_orgao_legado)

    if org_destino not in {1115, 1122, 1311, 1378}:
        org_destino = 1115

    #  EXTRAÇÃO DAS DATAS DO LEGADO
    datas_legado = mapa_datas_legado.get(id_equipamento_legado, {}) 

    created_at_destino = datas_legado.get('created_at') if datas_legado.get('created_at') else now
    updated_at_destino = datas_legado.get('updated_at') if datas_legado.get('updated_at') else now

    # CONDICIONAL PARA DELETED_AT
    if id_situacao_legado == 10 or pd.notna(datas_legado.get('deleted_at')):
        deleted_at_destino = datas_legado.get('deleted_at') if pd.notna(datas_legado.get('deleted_at')) else now
    else:
        deleted_at_destino = None    

    # CONDICIONAL PARA STATUS DO EQUIPAMENTO
    if id_situacao_legado == 10:
        status_id_destino = 9
    else:
        status_id_destino = 1  

    lista_mestre.append({
        "id_legado":     id_equipamento_legado,
        "TOMBO":         row.get("TOMBO"),
        "NOME_AJUSTADO": row.get("NOME_AJUSTADO"),
        "NUMERO_SERIE":  row.get("NUMERO_SERIE"),
        "codigo_item":   row.get("codigo_item"),
        "valor":         float(row["valor"]) if pd.notna(row.get("valor")) else 0.0,
        "grupo":         row.get("GRUPOS") if pd.notna(row.get("GRUPOS")) else None,
        "group_id":      None,
        "marca":         row.get("MARCA_AJUSTADA") if pd.notna(row.get("MARCA_AJUSTADA")) else None,
        "brand_id":      None,
        "tipo":          row.get("TIPO_AJUSTADO")  if pd.notna(row.get("TIPO_AJUSTADO"))  else None,
        "type_id":       None,
        "status_id":     status_id_destino,
        "org_destino":   org_destino,
        "created_at":    created_at_destino,
        "updated_at":    updated_at_destino,
        "deleted_at":    deleted_at_destino,
        "transaction_id": None
    })

df_master = pd.DataFrame(lista_mestre).reset_index(drop=True)

# ==============================================================================
# LIMPEZA DOS DADOS
# ==============================================================================
def limpar_tabelas_equipamentos(engine):
        with engine.begin() as conn:
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
            for tabela in ['equipments', 'transaction_items', 'product_items',
                           'products', 'types', 'brands', 'groups', 'transactions']:
                conn.execute(sa.text(f"TRUNCATE TABLE `{tabela}`"))
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))

limpar_tabelas_equipamentos(engine_novo)
print("✅ Tabelas limpas.")

# ==============================================================================
# FUNÇÕES AUXILIARES
# ==============================================================================
def nula(val):
    """Retorna None estruturado se o valor for nulo/NaN do Pandas"""
    return None if pd.isna(val) else val

# ==============================================================================
# INSERÇÃO DOS ENDEREÇOS PADRÕES DE ESTOQUES
# ==============================================================================
print("\n📦 Criando estoques padrões...")
dados_enderecos_bases = [
    {"addressable_id": 1115, "alias": "ALUCOM - BASE", "number": "40"},
    {"addressable_id": 1122, "alias": "MOREIA - BASE", "number": "50"},
    {"addressable_id": 1311, "alias": "IP - BASE", "number": "60"},
    {"addressable_id": 1378, "alias": "AS SISTEMAS - BASE", "number": "70"}
]

with engine_novo.begin() as conn:
    for base in dados_enderecos_bases:
            conn.execute(sa.text("""
                INSERT INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, created_at, updated_at)
                VALUES ('organization', :addressable_id, :alias, '60175205', 'RUA RIACHUELO PAPICU', :number, 'FORTALEZA', 'CE', 'Brazil', :now, :now)
            """), {
                "addressable_id": base["addressable_id"],
                "alias": base["alias"],
                "number": base["number"],
                "now": now
            })

# ← Recupera os IDs gerados logo após o insert, na mesma sequência
df_enderecos = pd.read_sql("""
    SELECT id AS address_id, addressable_id 
    FROM addresses 
    WHERE addressable_type = 'organization' 
    AND addressable_id IN (1115, 1122, 1311, 1378)
    ORDER BY id ASC
""", engine_novo)
mapa_enderecos = dict(zip(df_enderecos['addressable_id'], df_enderecos['address_id']))
id_fallback = int(df_enderecos['address_id'].iloc[0]) if not df_enderecos.empty else 1

print("✅ Estoques criados e mapa de endereços montado:")
for org_id, addr_id in mapa_enderecos.items():
    print(f"   -> Org {org_id} → address_id: {addr_id}")

# ==============================================================================
# INSERÇÃO DOS DADOS
# ==============================================================================
print("\n🚀 Iniciando a persistência dos dados no banco novo...")
try:

    #=====================================================================
    # PASSO 1: Groups
    #=====================================================================
    print("💾 Inserindo groups...")
    grupos_unicos = df_master['grupo'].dropna().unique()
    if len(grupos_unicos) > 0:
        with engine_novo.begin() as conn:
            df_groups_inserir = pd.DataFrame({
                "name": grupos_unicos,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None
            })
            df_groups_inserir.to_sql('groups', con=conn, if_exists='append', index=False)

    db_groups = pd.read_sql("SELECT id as group_id, name FROM `groups`", engine_novo)
    df_master['group_id'] = df_master['grupo'].map(dict(zip(db_groups['name'], db_groups['group_id']))).astype("Int64")
    print(f"   ✅ {len(grupos_unicos)} groups inseridos.")

    #=====================================================================
    # PASSO 2: Brands
    #=====================================================================
    print("💾 Inserindo brands...")
    marcas_unicas = df_master['marca'].dropna().unique()
    if len(marcas_unicas) > 0:
        with engine_novo.begin() as conn:
            df_brands_inserir = pd.DataFrame({
                "name": marcas_unicas,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None 
            })
            df_brands_inserir.to_sql('brands', con=conn, if_exists='append', index=False)
            
    db_brands = pd.read_sql("SELECT id as brand_id, name FROM `brands`", engine_novo)
    df_master['brand_id'] = df_master['marca'].map(dict(zip(db_brands['name'], db_brands['brand_id']))).astype("Int64")
    print(f"   ✅ {len(marcas_unicas)} brands inseridos.")

    #=====================================================================
    # PASSO 3: Types
    #=====================================================================
    print("💾 Inserindo types...")
    tipos_unicos = df_master['tipo'].dropna().unique()
    if len(tipos_unicos) > 0:
        with engine_novo.begin() as conn:
            df_types_inserir = pd.DataFrame({
                "name": tipos_unicos,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None 
            })
            df_types_inserir.to_sql('types', con=conn, if_exists='append', index=False)
            
    db_types = pd.read_sql("SELECT id as type_id, name FROM `types`", engine_novo)
    df_master['type_id'] = df_master['tipo'].map(dict(zip(db_types['name'], db_types['type_id']))).astype("Int64")
    print(f"   ✅ {len(tipos_unicos)} types inseridos.")

    #=====================================================================
    # PASSO 4: Products
    #=====================================================================
    print("💾 Inserindo products...")
    df_produtos_unicos = df_master[['marca', 'tipo', 'grupo', 'brand_id', 'type_id', 'group_id']].drop_duplicates()

    def gerar_nome_produto(row):
        partes = [
            str(row['tipo'])  if pd.notna(row['tipo'])  else "",
            str(row['grupo']) if pd.notna(row['grupo']) else "",
            str(row['marca']) if pd.notna(row['marca']) else ""
        ]
        nome = " ".join([p for p in partes if p]).strip()
        return nome if nome else "PRODUTO SEM ESPECIFICAÇÃO"

    df_produtos_unicos['name'] = df_produtos_unicos.apply(gerar_nome_produto, axis=1)

    df_products_inserir = pd.DataFrame({
        "name":       df_produtos_unicos['name'],
        "brand_id":   df_produtos_unicos['brand_id'],
        "type_id":    df_produtos_unicos['type_id'],
        "group_id":   df_produtos_unicos['group_id'],
        "is_asset":   1,
        "length":     0,
        "width":      0,
        "height":     0,
        "weight":     0,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None 
    })

    if not df_products_inserir.empty:
        with engine_novo.begin() as conn:
            df_products_inserir.to_sql('products', con=conn, if_exists='append', index=False)
            
    print(f"   ✅ {len(df_products_inserir)} products únicos inseridos.")

    # ← Inclui 'name' no read_sql para carregar junto com o merge
    db_products = pd.read_sql("SELECT id as product_id, name as product_name, brand_id, type_id, group_id FROM products", engine_novo)
    db_products['brand_id'] = db_products['brand_id'].astype("Int64")
    db_products['type_id']  = db_products['type_id'].astype("Int64")
    db_products['group_id'] = db_products['group_id'].astype("Int64")

    # ← product_name entra no df_master automaticamente pelo merge
    df_master = df_master.merge(
        db_products[['product_id', 'product_name', 'brand_id', 'type_id', 'group_id']],
        on=['brand_id', 'type_id', 'group_id'],
        how='left'
    )
    df_master['product_id']   = df_master['product_id'].astype("Int64")
    df_master['product_name'] = df_master['product_name'].fillna("PRODUTO SEM ESPECIFICAÇÃO")

    #=====================================================================
    # PASSO 5: Cadastro de Fornecedores (Suppliers) remanescentes
    #=====================================================================
    print("💾 Verificando e inserindo suppliers...")

    # 1. Garante que o Fornecedor Genérico existe no banco
    with engine_novo.begin() as conn:
        res = conn.execute(sa.text("SELECT id FROM suppliers WHERE name = 'FORNECEDOR NÃO IDENTIFICADO'"))
        row = res.fetchone()
        if row:
            id_generico = row[0]
        else:
            res = conn.execute(sa.text("""
                INSERT INTO suppliers (name, alias, cpf_cnpj, phone, email, created_at, updated_at)
                VALUES ('FORNECEDOR NÃO IDENTIFICADO', 'GENERICO', '00000000000000', '0000000000', 'nao@informado.com', :now, :now)
            """), {"now": now})
            id_generico = res.lastrowid

    with engine_novo.begin() as conn:
        df_fornecedores_existentes = pd.read_sql("SELECT id, name FROM suppliers", conn)

    fornecedores_para_inserir = []
    for marca in lista_fornecedores_legado:
        marca_str = str(marca).strip()
        marca_lower = marca_str.lower()
        existe = df_fornecedores_existentes['name'].str.lower().str.contains(marca_lower, regex=False).any()
        if not existe:
            fornecedores_para_inserir.append(marca_str)
            
    if len(fornecedores_para_inserir) > 0:
        base_id_unico = int(time.time())
        cpfs_unicos = [str(base_id_unico + i) for i in range(len(fornecedores_para_inserir))]

        with engine_novo.begin() as conn:
            df_suppliers_inserir = pd.DataFrame({
                "name":       fornecedores_para_inserir,
                "alias":      fornecedores_para_inserir,
                "cpf_cnpj":   cpfs_unicos,
                "phone":      "00000000000",
                "email":      'migracao@exemplo.com',
                "created_at": now,
                "updated_at": now,
                "deleted_at": None 
            })
            df_suppliers_inserir.to_sql('suppliers', con=conn, if_exists='append', index=False)
        print(f"   ✅ {len(fornecedores_para_inserir)} novos fornecedores cadastrados.")
    else:
        print("   ✅ Nenhum fornecedor novo precisou ser criado (todos já existiam).")

    with engine_novo.begin() as conn:
        df_todos_fornecedores = pd.read_sql("SELECT id, name FROM suppliers", conn)
        
    mapa_marca_supplier_id = {}
    for marca in marcas_unicas:
        marca_str = str(marca).strip()
        marca_lower = marca_str.lower()
        match = df_todos_fornecedores[df_todos_fornecedores['name'].str.lower().str.contains(marca_lower, regex=False)]
        if not match.empty:
            mapa_marca_supplier_id[marca_str] = match.iloc[0]['id']

    df_master['supplier_id'] = df_master['marca'].map(mapa_marca_supplier_id)
    
    qtd_faltantes = df_master['supplier_id'].isna().sum()
    df_master['supplier_id'] = df_master['supplier_id'].fillna(id_generico).astype("Int64")
    
    print(f"   ✅ Mapeamento concluído. {qtd_faltantes} itens atribuídos ao fornecedor genérico (ID: {id_generico}).")

    # ==========================================================================
    # PASSO 6 : Transactions, Transaction Items e Equipamentos
    # ==========================================================================
    print("📊 Gerando agrupamento composto por Fornecedor e Órgão de Destino...")
    df_validos = df_master[df_master['supplier_id'].notna() & df_master['org_destino'].notna()]
    contagem_grupos = df_validos.groupby(['supplier_id', 'org_destino']).size().to_dict()

    #====================================================
    #DEBUUG DE TESTE
    total_antes = len(df_master)
    total_validos = len(df_validos)
    print(f"DEBUG: Total original: {total_antes} | Total válidos: {total_validos} | Excluídos: {total_antes - total_validos}")

    # Opcional: ver o que está sem fornecedor
    sem_fornecedor = df_master[df_master['supplier_id'].isna()]
    print(f"DEBUG: Itens sem fornecedor mapeado: {len(sem_fornecedor)}")

    print(f"   ℹ️ Encontradas {len(contagem_grupos)} combinações únicas de Fornecedor + Órgão.")
    print("\n💾 Iniciando a persistência unificada (Transações -> Itens -> Equipamentos)...")
    #==========================================================================================
    
    contador_tx = 0
    contador_itens = 0
    lista_equipamentos_global = []
    contador_codigo_unico = 1000000

    with engine_novo.begin() as conn:
        for (s_id, org_id), qtd in contagem_grupos.items():
            supplier_id_int = int(s_id)
            buyer_id_int = int(org_id)
            df_grupo = df_validos[(df_validos['supplier_id'] == s_id) & (df_validos['org_destino'] == org_id)]

            # A. Criamos a Transação Mãe para o grupo
            stmt_tx = text("""
                INSERT INTO transactions (
                    transaction_date, transaction_type_id, supplier_id, buyer_id, 
                    address_id, receiver_id, customer_id, doc_type_id, doc_number, 
                    doc_date, purchase_date, created_by, details, amount_total, 
                    amount_discount, created_at, updated_at, deleted_at
                ) VALUES (
                    :transaction_date, :transaction_type_id, :supplier_id, :buyer_id, 
                    :address_id, :receiver_id, :customer_id, :doc_type_id, :doc_number, 
                    :doc_date, :purchase_date, :created_by, :details, :amount_total, 
                    :amount_discount, :created_at, :updated_at, :deleted_at
                )
            """)
            
            result_tx = conn.execute(stmt_tx, {
                "transaction_date":    now,
                "transaction_type_id": 1,
                "supplier_id":         supplier_id_int,
                "buyer_id":            buyer_id_int,
                "address_id":          None,
                "receiver_id":         None, 
                "customer_id":         None,
                "doc_type_id":         3,
                "doc_number":          None,
                "doc_date":            now,
                "purchase_date":       now,
                "created_by":          1,
                "details":             "Migração",
                "amount_total":        1,
                "amount_discount":     0,
                "created_at":          now,
                "updated_at":          now,
                "deleted_at":          None
            })
            
            tx_id_gerado = result_tx.lastrowid
            contador_tx += 1

            # B. Processamos os Itens da Transação e preparamos os Equipamentos
            stmt_item = text("""
                INSERT INTO transaction_items (
                    transaction_id, product_id, equipment_id, category_id, condition_id, 
                    warranty_date, address_id, location_id, unit_cost, quantity, 
                    created_at, updated_at, deleted_at
                ) VALUES (
                    :transaction_id, :product_id, :equipment_id, :category_id, :condition_id, 
                    :warranty_date, :address_id, :location_id, :unit_cost, :quantity, 
                    :created_at, :updated_at, :deleted_at
                )
            """)

            stmt_product_item = text("""
                INSERT INTO product_items (
                    product_id, code, category_id, condition_id,
                    address_id, location_id, organization_id,
                    average_cost, quantity, created_at, updated_at, deleted_at
                ) VALUES (
                    :product_id, :code, :category_id, :condition_id,
                    :address_id, :location_id, :organization_id,
                    :average_cost, :quantity, :created_at, :updated_at, :deleted_at
                )
            """)

            for _, linha_equip in df_grupo.iterrows():

                codigo_numerico = contador_codigo_unico
                contador_codigo_unico += 1

                # Tratamento de segurança: se não houver código na planilha, gera um provisório baseado no Tombo ou Timestamp
                codigo_item_val = linha_equip['codigo_item']
                if pd.isna(codigo_item_val) or not str(codigo_item_val).strip():
                    tombo_val = linha_equip['TOMBO']
                    if pd.notna(tombo_val) and str(tombo_val).strip():
                        codigo_item_val = f"TMB-{tombo_val}"
                    else:
                        # Fallback extremo para não dar erro de NOT NULL
                        codigo_item_val = f"MIG-{int(time.time() * 1000)}"

                # 1. Insere product_item e captura ID
                result_pi = conn.execute(stmt_product_item, {
                    "product_id":      int(linha_equip['product_id']) if pd.notna(linha_equip['product_id']) else None,
                    "code":            codigo_numerico,
                    "category_id":     1,
                    "condition_id":    1,
                    "address_id":      mapa_enderecos.get(buyer_id_int, id_fallback),
                    "location_id":     None,
                    "organization_id": buyer_id_int,
                    "average_cost":    1,
                    "quantity":        1,
                    "created_at":      now,
                    "updated_at":      now,
                    "deleted_at":      None
                })
                product_item_id_gerado = result_pi.lastrowid

                # 2. Insere transaction_item (igual ao que já tem)
                result_item = conn.execute(stmt_item, {
                    "transaction_id": int(tx_id_gerado),
                    "product_id":     int(linha_equip['product_id']) if pd.notna(linha_equip['product_id']) else None,
                    "equipment_id":   None,
                    "category_id":    1,
                    "condition_id":   1,
                    "warranty_date":  now,
                    "address_id":     mapa_enderecos.get(buyer_id_int, id_fallback),
                    "location_id":    None,
                    "unit_cost":      0,
                    "quantity":       1,
                    "created_at":     now,
                    "updated_at":     now,
                    "deleted_at":     nula(linha_equip['deleted_at'])
                })
                tx_item_id_gerado = result_item.lastrowid
                contador_itens += 1

                # 3. Monta equipamento com product_item_id real
                lista_equipamentos_global.append({
                    "id":                             int(linha_equip['id_legado']),
                    "product_item_id":                product_item_id_gerado,
                    "transaction_item_id":            int(tx_item_id_gerado),
                    "number":                         linha_equip['TOMBO'],
                    "name":                           linha_equip['product_name'],
                    "serial_number":                  nula(linha_equip['NUMERO_SERIE']),
                    "serial_required":                0,
                    "current_organization_id":        buyer_id_int,
                    "status_id":                      linha_equip['status_id'],
                    "address_id":                     mapa_enderecos.get(buyer_id_int, id_fallback),
                    "location_id":                    None,
                    "is_completed":                   1,
                    "completed_by":                   None,
                    "movement_date":                  None,
                    "last_movement_item_customer_id": None,
                    "created_at":                     linha_equip['created_at'],
                    "updated_at":                     linha_equip['updated_at'],
                    "deleted_at":                     nula(linha_equip['deleted_at'])
                })

        # C. BULK INSERT: Grava todos os equipamentos de uma só vez
        if lista_equipamentos_global:
            print(f"💾 Efetuando inserção em lote de {len(lista_equipamentos_global)} equipamentos...")
            stmt_equip = text("""
                INSERT INTO equipments (
                    id, product_item_id, transaction_item_id, number, name, serial_number, 
                    serial_required, current_organization_id, status_id, address_id, 
                    location_id, is_completed, completed_by, movement_date, 
                    last_movement_item_customer_id, created_at, updated_at, deleted_at
                ) VALUES (
                    :id, :product_item_id, :transaction_item_id, :number, :name, :serial_number, 
                    :serial_required, :current_organization_id, :status_id, :address_id, 
                    :location_id, :is_completed, :completed_by, :movement_date, 
                    :last_movement_item_customer_id, :created_at, :updated_at, :deleted_at
                )
            """)
            conn.execute(stmt_equip, lista_equipamentos_global)

    print(f"\n   ✅ [SUCESSO] {contador_tx} transações mãe criadas.")
    print(f"   ✅ [SUCESSO] {contador_itens} itens de transação vinculados.")
    print(f"   ✅ [SUCESSO] {len(lista_equipamentos_global)} equipamentos físicos persistidos com sucesso!")

except Exception as e:
    print(f"\n❌ ERRO CRÍTICO: {e}")
    raise e