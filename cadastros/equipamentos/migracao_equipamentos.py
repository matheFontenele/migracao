import pandas as pd
import sqlalchemy as sa
import time
import sys
from datetime import datetime
from tqdm import tqdm
from sqlalchemy import text

from config.config import MAPPING_ALUCOM, MAPPING_AS, MAPPING_IP, MAPPING_MOREIA
from utils.sanetizador import executar_truncate_tabelas

TABELAS = [
    'equipments', 
    'transaction_items', 
    'product_items',
    'products', 
    'types', 
    'brands', 
    'groups', 
    'transactions'
]

class MigracaoEquipamentos:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.ARQUIVO_IMPORTACAO = "docs/planilha_equipamentos.csv"
        
        # Variáveis de Estado (Memória)
        self.mapa_enderecos = {}
        self.id_fallback = 1
        self.id_generico = None
        self.lista_fornecedores_legado = []
        
        # Estatísticas
        self.stats = {
            "grupos": 0, "brands": 0, "types": 0, "products": 0,
            "suppliers": 0, "transactions": 0, "transaction_items": 0, "equipments": 0
        }

    # ==============================================================================
    # HELPERS
    # ==============================================================================
    def _nula(self, val):
        return None if pd.isna(val) else val
    
    def _descobrir_id_organizacao_destino(self, id_legado):
        if pd.isna(id_legado): return 1115
        id_legado_int = int(id_legado)
        if id_legado_int in MAPPING_ALUCOM: return 1115
        if id_legado_int in MAPPING_IP: return 1311
        if id_legado_int in MAPPING_MOREIA: return 1122
        if id_legado_int in MAPPING_AS: return 1378
        return id_legado_int
    
    # ==============================================================================
    # ETL: EXTRAÇÃO E TRANSFORMAÇÃO (PANDAS)
    # ==============================================================================
    def _extrair_e_transformar(self):
        print("\n📖 Carregando dados da planilha e do banco legado...")
        df_equipamentos = pd.read_csv(self.ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)

        with self.engine_legado.connect() as conn:
            df_equipamentos_legado = pd.read_sql("SELECT id, orgao_id, situacao_id, created_at, updated_at, deleted_at FROM aluguel_equipamentos", conn)

        mapa_equipamento_orgao = dict(zip(df_equipamentos_legado['id'], df_equipamentos_legado['orgao_id']))
        mapa_equipamento_situacao = dict(zip(df_equipamentos_legado['id'], df_equipamentos_legado['situacao_id']))
        
        mapa_datas_legado = {
            row['id']: {
                'created_at': row['created_at'], 'updated_at': row['updated_at'], 'deleted_at': row['deleted_at']
            } for _, row in df_equipamentos_legado.iterrows()
        }

        self.lista_fornecedores_legado = df_equipamentos['MARCA_AJUSTADA'].dropna().unique()

        lista_mestre = []
        for index, row in tqdm(df_equipamentos.iterrows(), total=df_equipamentos.shape[0], desc="Refatorando dados"):
            id_equipamento_legado = row.get("ID_LEGADO")
            id_orgao_legado = mapa_equipamento_orgao.get(id_equipamento_legado, None)
            id_situacao_legado = mapa_equipamento_situacao.get(id_equipamento_legado, None)
            org_destino = self._descobrir_id_organizacao_destino(id_orgao_legado)

            if org_destino not in {1115, 1122, 1311, 1378}: org_destino = 1115

            datas_legado = mapa_datas_legado.get(id_equipamento_legado, {}) 
            created_at_destino = datas_legado.get('created_at') if datas_legado.get('created_at') else self.now
            updated_at_destino = datas_legado.get('updated_at') if datas_legado.get('updated_at') else self.now

            if id_situacao_legado == 10 or pd.notna(datas_legado.get('deleted_at')):
                deleted_at_destino = datas_legado.get('deleted_at') if pd.notna(datas_legado.get('deleted_at')) else self.now
            else:
                deleted_at_destino = None    

            status_id_destino = 9 if id_situacao_legado == 10 else 1  

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

        return pd.DataFrame(lista_mestre).reset_index(drop=True)

    # ==============================================================================
    # ETL: CARGA DE DEPENDÊNCIAS BASE (ENDEREÇOS)
    # ==============================================================================
    def _criar_estoques_padroes(self):
        print("\n📦 Criando estoques padrões...")
        dados_enderecos_bases = [
            {"addressable_id": 1115, "alias": "ALUCOM - BASE", "number": "40"},
            {"addressable_id": 1122, "alias": "MOREIA - BASE", "number": "50"},
            {"addressable_id": 1311, "alias": "IP - BASE", "number": "60"},
            {"addressable_id": 1378, "alias": "AS SISTEMAS - BASE", "number": "70"}
        ]

        with self.engine_new.begin() as conn:
            for base in dados_enderecos_bases:
                conn.execute(text("""
                    INSERT IGNORE INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, created_at, updated_at)
                    VALUES ('organization', :addressable_id, :alias, '60175205', 'RUA RIACHUELO PAPICU', :number, 'FORTALEZA', 'CE', 'Brazil', :now, :now)
                """), {**base, "now": self.now})

        df_enderecos = pd.read_sql("""
            SELECT id AS address_id, addressable_id 
            FROM addresses WHERE addressable_type = 'organization' 
            AND addressable_id IN (1115, 1122, 1311, 1378) ORDER BY id ASC
        """, self.engine_new)
        
        self.mapa_enderecos = dict(zip(df_enderecos['addressable_id'], df_enderecos['address_id']))
        self.id_fallback = int(df_enderecos['address_id'].iloc[0]) if not df_enderecos.empty else 1

        print("   ✅ Estoques criados e mapa de endereços montado:")
        for org_id, addr_id in self.mapa_enderecos.items():
            print(f"      -> Org {org_id} → address_id: {addr_id}")

    # ==============================================================================
    # ETL: CARGA DE CADASTROS BÁSICOS E PRODUTOS
    # ==============================================================================
    def _carregar_tabelas_dimensionais(self, df_master):
        print("\n🚀 Iniciando a persistência de tabelas dimensionais...")
        
        # 1. GROUPS
        print("💾 Inserindo groups...")
        grupos_unicos = df_master['grupo'].dropna().unique()
        if len(grupos_unicos) > 0:
            with self.engine_new.begin() as conn:
                df_groups = pd.DataFrame({"name": grupos_unicos, "created_at": self.now, "updated_at": self.now})
                df_groups.to_sql('groups', con=conn, if_exists='append', index=False)
        db_groups = pd.read_sql("SELECT id as group_id, name FROM `groups`", self.engine_new)
        df_master['group_id'] = df_master['grupo'].map(dict(zip(db_groups['name'], db_groups['group_id']))).astype("Int64")
        self.stats["grupos"] = len(grupos_unicos)

        # 2. BRANDS
        print("💾 Inserindo brands...")
        marcas_unicas = df_master['marca'].dropna().unique()
        if len(marcas_unicas) > 0:
            with self.engine_new.begin() as conn:
                df_brands = pd.DataFrame({"name": marcas_unicas, "created_at": self.now, "updated_at": self.now})
                df_brands.to_sql('brands', con=conn, if_exists='append', index=False)
        db_brands = pd.read_sql("SELECT id as brand_id, name FROM `brands`", self.engine_new)
        df_master['brand_id'] = df_master['marca'].map(dict(zip(db_brands['name'], db_brands['brand_id']))).astype("Int64")
        self.stats["brands"] = len(marcas_unicas)

        # 3. TYPES
        print("💾 Inserindo types...")
        tipos_unicos = df_master['tipo'].dropna().unique()
        if len(tipos_unicos) > 0:
            with self.engine_new.begin() as conn:
                df_types = pd.DataFrame({"name": tipos_unicos, "created_at": self.now, "updated_at": self.now})
                df_types.to_sql('types', con=conn, if_exists='append', index=False)
        db_types = pd.read_sql("SELECT id as type_id, name FROM `types`", self.engine_new)
        df_master['type_id'] = df_master['tipo'].map(dict(zip(db_types['name'], db_types['type_id']))).astype("Int64")
        self.stats["types"] = len(tipos_unicos)

        # 4. PRODUCTS
        print("💾 Inserindo products...")
        df_produtos_unicos = df_master[['marca', 'tipo', 'grupo', 'brand_id', 'type_id', 'group_id']].drop_duplicates()

        def gerar_nome_produto(row):
            partes = [str(row['tipo']) if pd.notna(row['tipo']) else "",
                      str(row['grupo']) if pd.notna(row['grupo']) else "",
                      str(row['marca']) if pd.notna(row['marca']) else ""]
            nome = " ".join([p for p in partes if p]).strip()
            return nome if nome else "PRODUTO SEM ESPECIFICAÇÃO"

        df_produtos_unicos['name'] = df_produtos_unicos.apply(gerar_nome_produto, axis=1)

        df_products_inserir = pd.DataFrame({
            "name": df_produtos_unicos['name'], "brand_id": df_produtos_unicos['brand_id'],
            "type_id": df_produtos_unicos['type_id'], "group_id": df_produtos_unicos['group_id'],
            "is_asset": 1, "length": 0, "width": 0, "height": 0, "weight": 0,
            "created_at": self.now, "updated_at": self.now
        })

        if not df_products_inserir.empty:
            with self.engine_new.begin() as conn:
                df_products_inserir.to_sql('products', con=conn, if_exists='append', index=False)
        self.stats["products"] = len(df_products_inserir)

        # Merge de IDs de volta no DF
        db_products = pd.read_sql("SELECT id as product_id, name as product_name, brand_id, type_id, group_id FROM products", self.engine_new)
        db_products['brand_id'] = db_products['brand_id'].astype("Int64")
        db_products['type_id']  = db_products['type_id'].astype("Int64")
        db_products['group_id'] = db_products['group_id'].astype("Int64")

        df_master = df_master.merge(
            db_products[['product_id', 'product_name', 'brand_id', 'type_id', 'group_id']],
            on=['brand_id', 'type_id', 'group_id'], how='left'
        )
        df_master['product_id'] = df_master['product_id'].astype("Int64")
        df_master['product_name'] = df_master['product_name'].fillna("PRODUTO SEM ESPECIFICAÇÃO")

        return df_master

    # ==============================================================================
    # ETL: FORNECEDORES (SUPPLIERS)
    # ==============================================================================
    def _tratar_fornecedores(self, df_master):
        print("💾 Verificando e inserindo suppliers...")

        with self.engine_new.begin() as conn:
            res = conn.execute(text("SELECT id FROM suppliers WHERE name = 'FORNECEDOR NÃO IDENTIFICADO'"))
            row = res.fetchone()
            if row:
                self.id_generico = row[0]
            else:
                res = conn.execute(text("""
                    INSERT INTO suppliers (name, alias, cpf_cnpj, phone, email, created_at, updated_at)
                    VALUES ('FORNECEDOR NÃO IDENTIFICADO', 'GENERICO', '00000000000000', '0000000000', 'nao@informado.com', :now, :now)
                """), {"now": self.now})
                self.id_generico = res.lastrowid

        with self.engine_new.begin() as conn:
            df_fornecedores_existentes = pd.read_sql("SELECT id, name FROM suppliers", conn)

        fornecedores_para_inserir = []
        for marca in self.lista_fornecedores_legado:
            marca_str = str(marca).strip()
            existe = df_fornecedores_existentes['name'].str.lower().str.contains(marca_str.lower(), regex=False).any()
            if not existe:
                fornecedores_para_inserir.append(marca_str)
                
        if fornecedores_para_inserir:
            base_id_unico = int(time.time())
            cpfs_unicos = [str(base_id_unico + i) for i in range(len(fornecedores_para_inserir))]

            with self.engine_new.begin() as conn:
                df_suppliers_inserir = pd.DataFrame({
                    "name": fornecedores_para_inserir, "alias": fornecedores_para_inserir,
                    "cpf_cnpj": cpfs_unicos, "phone": "00000000000", "email": 'migracao@exemplo.com',
                    "created_at": self.now, "updated_at": self.now
                })
                df_suppliers_inserir.to_sql('suppliers', con=conn, if_exists='append', index=False)
            self.stats["suppliers"] = len(fornecedores_para_inserir)

        # Mapeamento final
        with self.engine_new.begin() as conn:
            df_todos_fornecedores = pd.read_sql("SELECT id, name FROM suppliers", conn)
            
        mapa_marca_supplier_id = {}
        for marca in df_master['marca'].dropna().unique():
            marca_str = str(marca).strip()
            match = df_todos_fornecedores[df_todos_fornecedores['name'].str.lower().str.contains(marca_str.lower(), regex=False)]
            if not match.empty:
                mapa_marca_supplier_id[marca_str] = match.iloc[0]['id']

        df_master['supplier_id'] = df_master['marca'].map(mapa_marca_supplier_id)
        df_master['supplier_id'] = df_master['supplier_id'].fillna(self.id_generico).astype("Int64")

        return df_master

    # ==============================================================================
    # ETL: INVENTÁRIO (TRANSACTIONS E EQUIPMENTS)
    # ==============================================================================
    def _gerar_inventario(self, df_master):
        print("📊 Gerando agrupamento de Inventário (Fornecedor + Órgão de Destino)...")
        df_validos = df_master[df_master['supplier_id'].notna() & df_master['org_destino'].notna()]
        contagem_grupos = df_validos.groupby(['supplier_id', 'org_destino']).size().to_dict()

        lista_equipamentos_global = []
        contador_codigo_unico = 1000000

        with self.engine_new.begin() as conn:
            for (s_id, org_id), qtd in contagem_grupos.items():
                supplier_id_int = int(s_id)
                buyer_id_int = int(org_id)
                df_grupo = df_validos[(df_validos['supplier_id'] == s_id) & (df_validos['org_destino'] == org_id)]

                # A. Transação Mãe
                result_tx = conn.execute(text("""
                    INSERT INTO transactions (transaction_date, transaction_type_id, supplier_id, buyer_id, 
                    doc_type_id, doc_date, purchase_date, created_by, details, amount_total, amount_discount, created_at, updated_at) 
                    VALUES (:now, 1, :sid, :bid, 3, :now, :now, 1, 'Migração', 1, 0, :now, :now)
                """), {"now": self.now, "sid": supplier_id_int, "bid": buyer_id_int})
                tx_id_gerado = result_tx.lastrowid
                self.stats["transactions"] += 1

                for _, linha_equip in df_grupo.iterrows():
                    codigo_item_val = f"MIG-{int(time.time() * 1000)}" if pd.isna(linha_equip['codigo_item']) else linha_equip['codigo_item']
                    addr_id = self.mapa_enderecos.get(buyer_id_int, self.id_fallback)

                    # B. Product Item
                    result_pi = conn.execute(text("""
                        INSERT INTO product_items (product_id, code, category_id, condition_id, address_id, organization_id, average_cost, quantity, created_at, updated_at) 
                        VALUES (:pid, :code, 1, 1, :addr, :org, 1, 1, :now, :now)
                    """), {"pid": self._nula(linha_equip['product_id']), "code": contador_codigo_unico, "addr": addr_id, "org": buyer_id_int, "now": self.now})
                    contador_codigo_unico += 1

                    # C. Transaction Item
                    result_item = conn.execute(text("""
                        INSERT INTO transaction_items (transaction_id, product_id, category_id, condition_id, warranty_date, address_id, unit_cost, quantity, created_at, updated_at, deleted_at) 
                        VALUES (:tid, :pid, 1, 1, :now, :addr, 0, 1, :now, :now, :del)
                    """), {"tid": tx_id_gerado, "pid": self._nula(linha_equip['product_id']), "now": self.now, "addr": addr_id, "del": self._nula(linha_equip['deleted_at'])})
                    
                    self.stats["transaction_items"] += 1

                    # D. Equipamento
                    lista_equipamentos_global.append({
                        "id": int(linha_equip['id_legado']),
                        "product_item_id": result_pi.lastrowid,
                        "transaction_item_id": result_item.lastrowid,
                        "number": linha_equip['TOMBO'],
                        "name": linha_equip['product_name'],
                        "serial_number": self._nula(linha_equip['NUMERO_SERIE']),
                        "serial_required": 0,
                        "current_organization_id": buyer_id_int,
                        "status_id": linha_equip['status_id'],
                        "address_id": addr_id,
                        "location_id": None,
                        "is_completed": 1,
                        "created_at": linha_equip['created_at'],
                        "updated_at": linha_equip['updated_at'],
                        "deleted_at": self._nula(linha_equip['deleted_at'])
                    })

            # E. BULK INSERT EQUIPAMENTOS
            if lista_equipamentos_global:
                conn.execute(text("""
                    INSERT INTO equipments (id, product_item_id, transaction_item_id, number, name, serial_number, serial_required, current_organization_id, status_id, address_id, location_id, is_completed, created_at, updated_at, deleted_at) 
                    VALUES (:id, :product_item_id, :transaction_item_id, :number, :name, :serial_number, :serial_required, :current_organization_id, :status_id, :address_id, :location_id, :is_completed, :created_at, :updated_at, :deleted_at)
                """), lista_equipamentos_global)
                self.stats["equipments"] = len(lista_equipamentos_global)

    # ==============================================================================
    # ORQUESTRADOR PRINCIPAL DA CLASSE
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 INICIANDO MIGRAÇÃO: EQUIPAMENTOS E INVENTÁRIO")
        print("=" * 80)
        
        try:
            # 🧹 Limpeza garantida caso o módulo seja rodado de forma isolada!
            executar_truncate_tabelas(self.engine_new, TABELAS)

            # Pipeline
            df_master = self._extrair_e_transformar()
            self._criar_estoques_padroes()
            df_master = self._carregar_tabelas_dimensionais(df_master)
            df_master = self._tratar_fornecedores(df_master)
            self._gerar_inventario(df_master)

            print("\n" + "=" * 50)
            print("📊 RELATÓRIO FINAL DE EQUIPAMENTOS")
            print("=" * 50)
            print(f"📦 Grupos criados:       {self.stats['grupos']}")
            print(f"🏷️  Marcas criadas:       {self.stats['brands']}")
            print(f"📁 Tipos criados:        {self.stats['types']}")
            print(f"🛒 Produtos cadastrados: {self.stats['products']}")
            print(f"🏭 Novos fornecedores:   {self.stats['suppliers']}")
            print(f"📑 Transações-Mãe:       {self.stats['transactions']}")
            print(f"🧩 Itens de Transação:   {self.stats['transaction_items']}")
            print(f"💻 Equipamentos salvos:  {self.stats['equipments']}")
            print("=" * 50)

        except Exception as e:
            print(f"\n❌ ERRO CRÍTICO NO PIPELINE DE EQUIPAMENTOS: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

# ==============================================================================
# WRAPPER (Ponte para o main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    migrador = MigracaoEquipamentos(eng_novo, eng_legado)
    migrador.executar()