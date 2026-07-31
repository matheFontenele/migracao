import pandas as pd
import sqlalchemy as sa
import os
import time
import sys
import difflib
from datetime import datetime
from tqdm import tqdm
from sqlalchemy import text

from config.config import MAPPING_ALUCOM, MAPPING_AS, MAPPING_IP, MAPPING_MOREIA, MAPPING_SC
from utils.sanetizador import executar_truncate_tabelas

TODOS_ORGAOS_MAPEADOS = set().union(MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, MAPPING_SC)

TABELAS = [
    'equipments',
    'equipment_history',
    'suppliers', 
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
        self.ARQUIVO_TYPES = "docs/types.csv"
        self.ARQUIVO_SUPPLIERS = "docs/suppliers.csv"
        
        # Variáveis de Estado (Memória)
        self.mapa_enderecos = {}
        self.id_fallback = 1
        self.id_generico = None
        self.lista_fornecedores_legado = []
        
        # Estatísticas
        self.stats = {
            "grupos": 0, "brands": 0, "types": 0, "products": 0,
            "suppliers": 0, "transactions": 0, "transaction_items": 0, "product_items_criados": 0, "equipments": 0, "history": 0, "tipos_inferidos": 0
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
        if id_legado_int in MAPPING_SC: return 1115  
        return id_legado_int

    def _inferir_tipo_por_similaridade(self, nome_equipamento, tipos_validos):
            if pd.isna(nome_equipamento) or not str(nome_equipamento).strip():
                return None
            nome_str = str(nome_equipamento).strip().upper()
            for tipo in tipos_validos:
                if tipo in nome_str:
                    return tipo
            matches = difflib.get_close_matches(nome_str, tipos_validos, n=1, cutoff=0.6)
            if matches:
                return matches[0]
            return None
            
    # ==============================================================================
    # ETL: EXTRAÇÃO E TRANSFORMAÇÃO (PANDAS + NOVA CTE)
    # ==============================================================================
    def _extrair_e_transformar(self):
        print("\n📖 Carregando dados da planilha e do banco legado (via CTE Otimizada)...")
        df_planilha = pd.read_csv(self.ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)

        df_planilha['ID_LEGADO'] = pd.to_numeric(df_planilha['ID_LEGADO'], errors='coerce')
        mapa_planilha = {
            int(row['ID_LEGADO']): row 
            for _, row in df_planilha.dropna(subset=['ID_LEGADO']).iterrows()
        }

        query_legado = """
            WITH UltimoMovimento AS (
                SELECT 
                    equipamento_id,
                    novo_orgao_id,
                    ROW_NUMBER() OVER(PARTITION BY equipamento_id ORDER BY id DESC) AS ordem
                FROM aluguel_movimento_itens
                WHERE deleted_at IS NULL
            )
            SELECT 
                alq.id AS id_legado,
                alq.numero AS tombo_legado,
                alq.nome AS nome_legado,
                tipe.nome AS tipo_legado,
                og.ORG_ID AS orgao_original,
                org.ORG_ID AS orgao_atual,
                alq.situacao_id,
                alq.created_at,
                alq.updated_at,
                alq.deleted_at
            FROM aluguel_equipamentos alq
            LEFT JOIN aluguel_tipos tipe ON alq.tipo_id = tipe.id
            INNER JOIN ORGAOS og ON alq.orgao_id = og.ORG_ID
            LEFT JOIN UltimoMovimento ult_movi ON alq.id = ult_movi.equipamento_id AND ult_movi.ordem = 1
            LEFT JOIN ORGAOS org ON ult_movi.novo_orgao_id = org.ORG_ID
            WHERE alq.deleted_at IS NULL;
        """
        
        with self.engine_legado.connect() as conn:
            df_equipamentos_legado = pd.read_sql(text(query_legado), conn)

        print(f"\n🔍 Total retornado pelo SQL: {len(df_equipamentos_legado)}")


        self.lista_fornecedores_legado = df_planilha['MARCA_AJUSTADA'].dropna().unique()
        tipos_existentes = [str(t).strip().upper() for t in df_planilha['TIPO_AJUSTADO'].dropna().unique() if str(t).strip()]
        tipos_existentes.sort(key=len, reverse=True)

        lista_mestre = []

        # CONTADORES DE DESCARTE
        descartados_tombo_zero = 0
        descartados_org_nao_mapeada = 0
        orgaos_nao_mapeados_amostra = set()
        
        for index, dados_db in tqdm(df_equipamentos_legado.iterrows(), total=df_equipamentos_legado.shape[0], desc="Refatorando dados"):

            tombo_atual = dados_db['tombo_legado']
            
            # Pula tombos 0, 0.0 etc.
            if pd.notna(tombo_atual) and str(tombo_atual).strip() in ['0', '0.0', '0.00']:
                descartados_tombo_zero += 1
                continue

            id_equipamento_legado = dados_db['id_legado']
            
            # Regra de Órgão: Usa o Órgão Atual. Se for Nulo, usa o Original.
            orgao_bruto = dados_db['orgao_atual'] if pd.notna(dados_db['orgao_atual']) else dados_db['orgao_original']
            
            # Se a máquina pertence a um órgão que não estamos migrando, pula
            if orgao_bruto not in TODOS_ORGAOS_MAPEADOS:
                descartados_org_nao_mapeada += 1
                if len(orgaos_nao_mapeados_amostra) < 20:
                    orgaos_nao_mapeados_amostra.add(orgao_bruto)
                continue
                
            org_destino = self._descobrir_id_organizacao_destino(orgao_bruto)
            id_situacao_legado = dados_db['situacao_id']

            created_at_destino = dados_db['created_at'] if pd.notna(dados_db['created_at']) else self.now
            updated_at_destino = dados_db['updated_at'] if pd.notna(dados_db['updated_at']) else self.now
            deleted_at_destino = dados_db['deleted_at'] if pd.notna(dados_db['deleted_at']) else (self.now if id_situacao_legado == 10 else None)
            
            status_id_destino = 9 if id_situacao_legado == 10 else 1 

            row_plan = mapa_planilha.get(id_equipamento_legado, {})
            
            # TRATAMENTO DO TIPO
            tipo_original = row_plan.get("TIPO_AJUSTADO")
            if pd.isna(tipo_original) or str(tipo_original).strip() == "":
                tipo_bd = dados_db['tipo_legado']
                if pd.notna(tipo_bd) and str(tipo_bd).strip() != "":
                    tipo_final = str(tipo_bd).strip().upper()
                else:
                    nome_base = row_plan.get("NOME_AJUSTADO") if pd.notna(row_plan.get("NOME_AJUSTADO")) else dados_db['nome_legado']
                    tipo_final = self._inferir_tipo_por_similaridade(nome_base, tipos_existentes)
                    self.stats["tipos_inferidos"] += 1
            else:
                tipo_final = str(tipo_original).strip().upper() 

            # TRATAMENTO DOS DEMAIS DADOS
            nome_final = row_plan.get("NOME_AJUSTADO")
            if pd.isna(nome_final): nome_final = dados_db['nome_legado']
            if pd.isna(nome_final): nome_final = "SEM NOME REGISTRADO"
            
            marca_final = row_plan.get("MARCA_AJUSTADA")
            if pd.isna(marca_final): marca_final = "FORNECEDOR NÃO IDENTIFICADO"
            
            grupo_final = row_plan.get("GRUPOS")
            if pd.isna(grupo_final): grupo_final = "GERAL"

            lista_mestre.append({
                "id_legado":     id_equipamento_legado,
                "TOMBO":         tombo_atual,
                "NOME_AJUSTADO": nome_final,
                "NUMERO_SERIE":  row_plan.get("NUMERO_SERIE") if pd.notna(row_plan.get("NUMERO_SERIE")) else None,
                "codigo_item":   row_plan.get("codigo_item") if pd.notna(row_plan.get("codigo_item")) else None,
                "valor":         float(row_plan.get("valor")) if pd.notna(row_plan.get("valor")) else 0.0,
                "grupo":         grupo_final,
                "group_id":      None,
                "marca":         marca_final,
                "brand_id":      None,
                "tipo":          tipo_final if tipo_final else "OUTROS",
                "type_id":       None,
                "status_id":     status_id_destino,
                "org_destino":   org_destino,
                "created_at":    created_at_destino,
                "updated_at":    updated_at_destino,
                "deleted_at":    deleted_at_destino,
                "transaction_id": None
            })

        print(f"\n📊 RESUMO DE DESCARTES NO LOOP:")
        print(f"   Total original (SQL):              {len(df_equipamentos_legado)}")
        print(f"   ❌ Descartados (tombo zero):  {descartados_tombo_zero}")
        print(f"   ❌ Descartados (órgão não mapeado): {descartados_org_nao_mapeada}")
        print(f"   ✅ Dados limpos: {len(lista_mestre)}")
        print(f"   🔍 Amostra de órgãos não mapeados: {orgaos_nao_mapeados_amostra}")

        return pd.DataFrame(lista_mestre).reset_index(drop=True)

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

        # 3. TYPES COM VERIFICAÇÃO DE IS_KIT
        print("💾 Inserindo types e mapeando is_kit...")
        tipos_unicos = df_master['tipo'].dropna().unique()
        if len(tipos_unicos) > 0:
            mapa_is_kit = {}
            if os.path.exists(self.ARQUIVO_TYPES):
                df_csv_types = pd.read_csv(self.ARQUIVO_TYPES)
                for _, row_type in df_csv_types.iterrows():
                    if pd.notna(row_type.get('name')):
                        nome_tipo = str(row_type['name']).strip().upper()
                        mapa_is_kit[nome_tipo] = int(row_type.get('is_kit', 0))
            else:
                print(f"   ⚠️ Arquivo {self.ARQUIVO_TYPES} não encontrado. Todos os tipos assumirão is_kit = 0.")

            valores_is_kit = [mapa_is_kit.get(str(t).upper(), 0) for t in tipos_unicos]

            with self.engine_new.begin() as conn:
                df_types = pd.DataFrame({
                    "name": tipos_unicos,
                    "is_kit": valores_is_kit,
                    "created_at": self.now, 
                    "updated_at": self.now
                })
                df_types.to_sql('types', con=conn, if_exists='append', index=False)
                
        db_types = pd.read_sql("SELECT id as type_id, name FROM `types`", self.engine_new)
        df_master['type_id'] = df_master['tipo'].map(dict(zip(db_types['name'], db_types['type_id']))).astype("Int64")
        self.stats["types"] = len(tipos_unicos)

        # 4. PRODUCTS (Agrupamento único)
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
            "min_quantity": 1, 
            "max_quantity": 1,
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
    # ETL: FORNECEDORES (SUPPLIERS VIA CSV)
    # ==============================================================================
    def _tratar_fornecedores(self, df_master):
        print("\n💾 Processando Fornecedores (Suppliers)...")

        # 1. Carregar CSV e Inserir no Banco
        if os.path.exists(self.ARQUIVO_SUPPLIERS):
            df_csv_suppliers = pd.read_csv(self.ARQUIVO_SUPPLIERS, sep=",", encoding="utf-8", on_bad_lines="skip")
            df_csv_suppliers.columns = df_csv_suppliers.columns.str.lower()
            
            # Detecta qual é a coluna do nome
            col_name = 'name' if 'name' in df_csv_suppliers.columns else ('nome' if 'nome' in df_csv_suppliers.columns else None)
            
            if col_name:
                df_suppliers_inserir = pd.DataFrame()
                df_suppliers_inserir['name'] = df_csv_suppliers[col_name].dropna().astype(str).str.strip()
                df_suppliers_inserir['alias'] = df_csv_suppliers.get('alias', df_suppliers_inserir['name'])
                cnpj_raw = df_csv_suppliers.get('cpf_cnpj', df_csv_suppliers.get('cnpj', '00000000000000'))
                df_suppliers_inserir['cpf_cnpj'] = (
                    cnpj_raw.astype(str)
                    .str.replace(r'\D', '', regex=True)
                    .str.slice(0, 14)
                    .replace('', '00000000000000')
                )
                df_suppliers_inserir['phone'] = df_csv_suppliers.get('phone', df_csv_suppliers.get('telefone', '00000000000')).fillna('00000000000')
                df_suppliers_inserir['email'] = df_csv_suppliers.get('email', 'nao@informado.com').fillna('nao@informado.com')
                df_suppliers_inserir['created_at'] = self.now
                df_suppliers_inserir['updated_at'] = self.now
                
                with self.engine_new.begin() as conn:
                    df_suppliers_inserir.to_sql('suppliers', con=conn, if_exists='append', index=False)
                self.stats["suppliers"] = len(df_suppliers_inserir)
                print(f"   ✅ {len(df_suppliers_inserir)} fornecedores importados do CSV.")
            else:
                print(f"   ⚠️ Coluna de nome (name/nome) não encontrada em {self.ARQUIVO_SUPPLIERS}.")
        else:
            print(f"   ⚠️ Arquivo {self.ARQUIVO_SUPPLIERS} não encontrado no projeto.")

        # 2. Garantir que o Fallback 'ALUCOM LTDA' Exista
        with self.engine_new.begin() as conn:
            res = conn.execute(text("SELECT id FROM suppliers WHERE name = 'ALUCOM LTDA'")).fetchone()
            if res:
                self.id_generico = res[0]
            else:
                res = conn.execute(text("""
                    INSERT INTO suppliers (name, alias, cpf_cnpj, phone, email, created_at, updated_at)
                    VALUES ('ALUCOM LTDA', 'ALUCOM', '00000000000000', '0000000000', 'nao@informado.com', :now, :now)
                """), {"now": self.now})
                self.id_generico = res.lastrowid
                print("   ✅ Fornecedor fallback 'ALUCOM LTDA' não existia, criado automaticamente.")

        # 3. Mapear os Fornecedores com Base no Nome
        with self.engine_new.begin() as conn:
            df_todos_fornecedores = pd.read_sql("SELECT id, name FROM suppliers", conn)
            
        mapa_nome_id = {str(row['name']).strip().lower(): row['id'] for _, row in df_todos_fornecedores.iterrows()}

        def buscar_fornecedor(marca):
            if pd.isna(marca) or not str(marca).strip():
                return self.id_generico
                
            marca_str = str(marca).strip().lower()
            
            # Match exato
            if marca_str in mapa_nome_id:
                return mapa_nome_id[marca_str]
                
            # Match parcial (procura substring no banco ou vice-versa)
            for db_name, db_id in mapa_nome_id.items():
                if marca_str in db_name or db_name in marca_str:
                    return db_id
                    
            return self.id_generico

        df_master['supplier_id'] = df_master['marca'].apply(buscar_fornecedor).astype("Int64")
        
        # Estatística rápida de quantos foram para o fallback
        qtd_fallback = sum(df_master['supplier_id'] == self.id_generico)
        print(f"   ✅ Relacionamento concluído. {qtd_fallback} equipamentos usarão o fornecedor ALUCOM LTDA.")

        return df_master

    # ==============================================================================
    # ETL: INVENTÁRIO (AGRUPADO POR MATCH PARA PRODUCT_ITEMS)
    # ==============================================================================
    def _gerar_inventario(self, df_master):
        print("📊 Gerando agrupamento de Inventário (Fornecedor + Órgão + Produto)...")
        
        df_validos = df_master[df_master['org_destino'].notna()]
        grupos_transacao = df_validos.groupby(['supplier_id', 'org_destino'], dropna=False)

        lista_equipamentos_global = []
        lista_historico_global = []
        contador_codigo_unico = 1000000

        with self.engine_new.begin() as conn:
            for (s_id, org_id), df_transacao in grupos_transacao:
                supplier_id_int = int(s_id) if pd.notna(s_id) else None
                buyer_id_int = int(org_id)

                datas_lote = pd.to_datetime(df_transacao['created_at'], errors='coerce').dropna()
                data_transacao = datas_lote.min().strftime('%Y-%m-%d %H:%M:%S') if not datas_lote.empty else self.now

                # A. Transação Mãe (O "Caminhão" que chegou no órgão com a data do equipamento mais antigo)
                result_tx = conn.execute(text("""
                    INSERT INTO transactions (transaction_date, transaction_type_id, supplier_id, buyer_id, 
                    doc_type_id, doc_date, purchase_date, created_by, details, amount_total, amount_discount, created_at, updated_at) 
                    VALUES (:data_tx, 1, :sid, :bid, 3, :data_tx, :data_tx, 1, 'Migração Inicial (Lote)', 1, 0, :now, :now)
                """), {
                    "data_tx": data_transacao, 
                    "now": self.now, 
                    "sid": supplier_id_int, 
                    "bid": buyer_id_int
                })
                tx_id_gerado = result_tx.lastrowid
                self.stats["transactions"] += 1

                grupos_produtos = df_transacao.groupby('product_id', dropna=False)

                for p_id, df_equipamentos_identicos in grupos_produtos:
                    
                    p_id_val = int(p_id) if pd.notna(p_id) else None
                    qtd_repeticoes = len(df_equipamentos_identicos)
                    addr_id = self.mapa_enderecos.get(buyer_id_int, self.id_fallback)

                    result_pi = conn.execute(text("""
                        INSERT INTO product_items (product_id, code, category_id, condition_id, address_id, organization_id, average_cost, quantity, created_at, updated_at) 
                        VALUES (:pid, :code, 1, 1, :addr, :org, 1, :qty, :now, :now)
                    """), {
                        "pid": p_id_val, "code": contador_codigo_unico, "addr": addr_id, 
                        "org": buyer_id_int, "qty": qtd_repeticoes, "now": self.now
                    })
                    pi_id_gerado = result_pi.lastrowid
                    contador_codigo_unico += 1
                    self.stats["product_items_criados"] += 1

                    result_item = conn.execute(text("""
                        INSERT INTO transaction_items (transaction_id, product_id, category_id, condition_id, warranty_date, address_id, unit_cost, quantity, created_at, updated_at, deleted_at) 
                        VALUES (:tid, :pid, 1, 1, :now, :addr, 0, :qty, :now, :now, NULL)
                    """), {
                        "tid": tx_id_gerado, "pid": p_id_val, "now": self.now, 
                        "addr": addr_id, "qty": qtd_repeticoes
                    })
                    ti_id_gerado = result_item.lastrowid
                    self.stats["transaction_items"] += 1

                    for _, linha_equip in df_equipamentos_identicos.iterrows():
                        eq_id = int(linha_equip['id_legado'])
                        eq_created_at = linha_equip['created_at']

                        lista_equipamentos_global.append({
                            "id": eq_id,
                            "product_item_id": pi_id_gerado,
                            "transaction_item_id": ti_id_gerado,
                            "number": linha_equip['TOMBO'],
                            "name": linha_equip['NOME_AJUSTADO'],
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

                        lista_historico_global.append({
                            "equipment_id": eq_id,
                            "status_id": 1,
                            "occurred_at": eq_created_at,
                            "movement_item_id": None,
                            "service_order_item_id": None,
                            "contract_item_id": None,
                            "shipment_item_id": None,
                            "is_conversion": 0,
                            "reason": "TRANSACTION_ENTRANCE_EQUIPMENT",
                            "user_id": 1 
                        })

            if lista_equipamentos_global:
                print(f"💾 Inserindo {len(lista_equipamentos_global)} máquinas físicas no MySQL...")
                conn.execute(text("""
                    INSERT INTO equipments (id, product_item_id, transaction_item_id, number, name, serial_number, serial_required, current_organization_id, status_id, address_id, location_id, is_completed, created_at, updated_at, deleted_at) 
                    VALUES (:id, :product_item_id, :transaction_item_id, :number, :name, :serial_number, :serial_required, :current_organization_id, :status_id, :address_id, :location_id, :is_completed, :created_at, :updated_at, :deleted_at)
                """), lista_equipamentos_global)
                self.stats["equipments"] += len(lista_equipamentos_global)

            if lista_historico_global:
                print(f"💾 Inserindo {len(lista_historico_global)} logs de histórico no MySQL...")
                conn.execute(text("""
                    INSERT INTO equipment_history (
                        equipment_id, status_id, occurred_at, movement_item_id, 
                        service_order_item_id, contract_item_id, shipment_item_id, 
                        is_conversion, reason, user_id
                    ) VALUES (
                        :equipment_id, :status_id, :occurred_at, :movement_item_id, 
                        :service_order_item_id, :contract_item_id, :shipment_item_id, 
                        :is_conversion, :reason, :user_id
                    )
                """), lista_historico_global)
                self.stats["history"] += len(lista_historico_global)

    # ==============================================================================
    # ORQUESTRADOR PRINCIPAL DA CLASSE
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 INICIANDO MIGRAÇÃO: EQUIPAMENTOS E INVENTÁRIO (COM AGRUPAMENTO)")
        print("=" * 80)
        
        try:
            executar_truncate_tabelas(self.engine_new, TABELAS)

            df_master = self._extrair_e_transformar()
            df_master = self._carregar_tabelas_dimensionais(df_master)
            df_master = self._tratar_fornecedores(df_master)
            self._gerar_inventario(df_master)

            print("\n" + "=" * 50)
            print("📊 RELATÓRIO FINAL DE EQUIPAMENTOS")
            print("=" * 50)
            print(f"📦 Grupos criados:         {self.stats['grupos']}")
            print(f"🏷️  Marcas criadas:         {self.stats['brands']}")
            print(f"📁 Tipos criados:          {self.stats['types']}")
            print(f"🤖 Tipos Inferidos:        {self.stats['tipos_inferidos']}")
            print(f"🛒 Produtos cadastrados:   {self.stats['products']}")
            print(f"🏭 Fornecedores (CSV):     {self.stats['suppliers']}")
            print("-" * 50)
            print(f"📑 Transações-Mãe:         {self.stats['transactions']}")
            print(f"📦 Blocos de Estoque (PI): {self.stats['product_items_criados']}")
            print(f"🧩 Itens de Transação:     {self.stats['transaction_items']}")
            print(f"💻 Equipamentos salvos:    {self.stats['equipments']}")
            print(f"📜 Logs de Entrada (TX):   {self.stats['history']}")
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