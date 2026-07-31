import pandas as pd
import sqlalchemy as sa
import time
import sys
import re
from datetime import datetime
from tqdm import tqdm
from sqlalchemy import text

from config.config import MAPPING_ALUCOM, MAPPING_AS, MAPPING_IP, MAPPING_MOREIA, MAPPING_SC

TODOS_ORGAOS_MAPEADOS = set().union(MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, MAPPING_SC)

class MigracaoInsumos:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Estatísticas
        self.stats = {
            "grupos": 0, "types": 0, "products": 0,
            "transactions": 0, "product_items_criados": 0, "transaction_items": 0
        }

    # ==============================================================================
    # HELPERS
    # ==============================================================================
    def _descobrir_id_organizacao_destino(self, id_legado):
        if pd.isna(id_legado): return 1115
        id_legado_int = int(id_legado)
        if id_legado_int in MAPPING_ALUCOM: return 1115
        if id_legado_int in MAPPING_IP: return 1311 
        if id_legado_int in MAPPING_MOREIA: return 1122
        if id_legado_int in MAPPING_AS: return 1378 
        if id_legado_int in MAPPING_SC: return 1115  
        return 1115 # Fallback geral para ALUCOM

    def _extrair_grupo_do_nome(self, produto, tipo):
        p = str(produto).strip().upper()
        t = str(tipo).strip().upper()
        
        # Tenta remover o tipo exato (ex: FONTES)
        g = p.replace(t, '').strip()
        
        # Se o tipo terminava em S, tenta remover o singular também (ex: FONTE)
        if t.endswith('S'):
            t_singular = t[:-1]
            g = g.replace(t_singular, '').strip()
            
        # Limpa espaços duplos ou hifens soltos
        g = re.sub(r'\s+', ' ', g)
        g = re.sub(r'^[-\s]+', '', g)
        
        return g if g else "GERAL"

    def _mapear_condicao(self, condicao):
        c = str(condicao).strip().upper()
        if 'NOVO' in c: return 1
        if 'REVISADO' in c: return 2
        if 'REMAN' in c: return 3 # Pega Remanufaturado ou Remanofaturado
        return 1 # Fallback default

    # ==============================================================================
    # ETL: EXTRAÇÃO (BANCO LEGADO)
    # ==============================================================================
    def _extrair_e_transformar(self):
        print("\n📖 Extraindo saldo de Insumos do banco legado...")
        
        # 🎯 QUERY ATUALIZADA COM MIN(IESA.CREATED_AT) PARA A DATA DA TRANSAÇÃO
        query_legado = """
            SELECT PA.PRO_ID AS ID
                , PA.PRO_NOME AS PRODUTO
                , GPA.GPA_NOME AS CONDICAO
                , SPA.SGA_NOME AS TIPO
                , PA.PRO_QUANT_MIN AS QUANTI_MININA
                , PA.PRO_QUANT_MAX AS QUANTI_MAX
                , COALESCE(SUM(CASE ESA.ESA_TIPO WHEN 'E' THEN (IESA.IES_QUANTIDADE * COALESCE(PA.PRO_QUANT_UNS, 1)) WHEN 'S' THEN -IESA.IES_QUANTIDADE END), 0) AS ESTOQUE_ATUAL
                , MAX(O.ORG_ID) AS LEGACY_ORG_ID
                , MAX(O.ORG_NOME) AS ORGAO
                , MIN(IESA.CREATED_AT) AS DATA_CRIACAO
            FROM PRODUTOS_ALM PA
            LEFT JOIN SUBGRUPOS_PRODUTO_ALM SPA ON PA.SGA_ID = SPA.SGA_ID
            LEFT JOIN GRUPOS_PRODUTO_ALM GPA ON SPA.GPA_ID = GPA.GPA_ID
            LEFT JOIN ITENS_ENTRADA_SAIDA_ALM IESA ON PA.PRO_ID = IESA.PRO_ID
            LEFT JOIN ENTRADAS_SAIDAS_ALM ESA ON IESA.ESA_ID = ESA.ESA_ID
            LEFT JOIN ORGAOS O ON ESA.ORG_ID = O.ORG_ID
            WHERE GPA.ORG_ID IN (1122,1264,1313,1326,1328,1351,1358,1360,1369)
            AND ESA.DELETED_AT IS NULL
            AND PA.PRO_ATIVO = 'S'
            GROUP BY PA.PRO_ID, PA.PRO_NOME, GPA.GPA_NOME, SPA.SGA_NOME, PA.PRO_QUANT_MIN, PA.PRO_QUANT_MAX
            HAVING ESTOQUE_ATUAL > 0;
        """
        
        with self.engine_legado.connect() as conn:
            df_insumos = pd.read_sql(text(query_legado), conn)

        print(f"🔍 Foram encontrados {len(df_insumos)} lotes de insumos com saldo positivo.")

        lista_mestre = []
        for index, row in tqdm(df_insumos.iterrows(), total=df_insumos.shape[0], desc="Mapeando Dicionários"):
            
            nome_produto = str(row['PRODUTO']).strip() if pd.notna(row['PRODUTO']) else "INSUMO SEM NOME"
            tipo = str(row['TIPO']).strip() if pd.notna(row['TIPO']) else "OUTROS"
            
            grupo = self._extrair_grupo_do_nome(nome_produto, tipo)
            condicao_id = self._mapear_condicao(row['CONDICAO'])
            
            estoque = float(row['ESTOQUE_ATUAL'])
            org_id_legado = row['LEGACY_ORG_ID']
            org_destino = self._descobrir_id_organizacao_destino(org_id_legado)

            data_criacao = row['DATA_CRIACAO'] if pd.notna(row['DATA_CRIACAO']) else self.now

            # 🎯 REGRA DE NEGÓCIO: Quantidade Mínima e Máxima
            try:
                min_q = float(row['QUANTI_MININA']) if pd.notna(row['QUANTI_MININA']) else 0.0
                max_q = float(row['QUANTI_MAX']) if pd.notna(row['QUANTI_MAX']) else 0.0
            except:
                min_q = 0.0
                max_q = 0.0

            if min_q == 0:
                min_q = 1.0

            if max_q == 0 or max_q < min_q:
                max_q = min_q

            lista_mestre.append({
                "id_legado": row['ID'],
                "nome_produto": nome_produto,
                "grupo": grupo,
                "tipo": tipo,
                "min_quantity": min_q,
                "max_quantity": max_q,
                "condition_id": condicao_id,
                "estoque_atual": estoque,
                "org_destino": org_destino,
                "data_criacao": data_criacao
            })

        return pd.DataFrame(lista_mestre)

    # ==============================================================================
    # ETL: CARGA DE CADASTROS (GRUPOS, TIPOS, PRODUTOS)
    # ==============================================================================
    def _carregar_dimensionais(self, df_master):
        print("\n🚀 Persistindo Grupos, Tipos e Produtos de Insumos (is_asset = 0)...")
        
        # 1. GROUPS 
        grupos_unicos = df_master['grupo'].unique()
        with self.engine_new.begin() as conn:
            db_groups_existentes = pd.read_sql("SELECT name FROM `groups`", conn)['name'].tolist()
            grupos_para_inserir = [g for g in grupos_unicos if g not in db_groups_existentes]
            
            if grupos_para_inserir:
                df_groups = pd.DataFrame({"name": grupos_para_inserir, "created_at": self.now, "updated_at": self.now})
                df_groups.to_sql('groups', con=conn, if_exists='append', index=False)
                self.stats["grupos"] += len(grupos_para_inserir)

            db_groups = pd.read_sql("SELECT id as group_id, name FROM `groups`", conn)
        df_master['group_id'] = df_master['grupo'].map(dict(zip(db_groups['name'], db_groups['group_id']))).astype("Int64")

        # 2. TYPES
        tipos_unicos = df_master['tipo'].unique()
        with self.engine_new.begin() as conn:
            db_types_existentes = pd.read_sql("SELECT name FROM `types`", conn)['name'].tolist()
            tipos_para_inserir = [t for t in tipos_unicos if t not in db_types_existentes]
            
            if tipos_para_inserir:
                df_types = pd.DataFrame({"name": tipos_para_inserir, "is_kit": 0, "created_at": self.now, "updated_at": self.now})
                df_types.to_sql('types', con=conn, if_exists='append', index=False)
                self.stats["types"] += len(tipos_para_inserir)

            db_types = pd.read_sql("SELECT id as type_id, name FROM `types`", conn)
        df_master['type_id'] = df_master['tipo'].map(dict(zip(db_types['name'], db_types['type_id']))).astype("Int64")

        # 3. PRODUCTS 
        df_produtos_unicos = df_master[['nome_produto', 'type_id', 'group_id', 'min_quantity', 'max_quantity']].drop_duplicates(subset=['nome_produto', 'type_id', 'group_id'])
        
        with self.engine_new.begin() as conn:
            db_products_existentes = pd.read_sql("SELECT name FROM products WHERE is_asset = 0", conn)['name'].tolist()
            
            df_products_inserir = df_produtos_unicos[~df_produtos_unicos['nome_produto'].isin(db_products_existentes)].copy()
            
            if not df_products_inserir.empty:
                df_products_inserir = df_products_inserir.rename(columns={'nome_produto': 'name'})
                df_products_inserir['brand_id'] = None  
                df_products_inserir['is_asset'] = 0     
                df_products_inserir['length'] = 0
                df_products_inserir['width'] = 0
                df_products_inserir['height'] = 0
                df_products_inserir['weight'] = 0
                df_products_inserir['created_at'] = self.now
                df_products_inserir['updated_at'] = self.now
                
                df_products_inserir.to_sql('products', con=conn, if_exists='append', index=False)
                self.stats["products"] += len(df_products_inserir)

            db_products = pd.read_sql("SELECT id as product_id, name as nome_produto FROM products", conn)
        
        df_master = df_master.merge(db_products, on='nome_produto', how='left')
        df_master['product_id'] = df_master['product_id'].astype("Int64")

        return df_master

    # ==============================================================================
    # ETL: INVENTÁRIO (OPERAÇÃO MASSIVA EM LOTE)
    # ==============================================================================
    def _gerar_inventario(self, df_master):
        print("\n📊 Gerando estoque massivo de insumos (Bulk Insert)...")
        
        with self.engine_new.begin() as conn:
            res = conn.execute(text("SELECT id FROM suppliers WHERE name = 'ALUCOM LTDA'")).fetchone()
            if res:
                fornecedor_id = res[0]
            else:
                res = conn.execute(text("INSERT INTO suppliers (name, alias, cpf_cnpj, created_at, updated_at) VALUES ('ALUCOM LTDA', 'ALUCOM', '00000000000000', :now, :now)"), {"now": self.now})
                fornecedor_id = res.lastrowid
        
        grupos_orgaos = df_master.groupby('org_destino')
        contador_codigo_unico = 9000000 

        product_items_batch = []
        transaction_items_batch = []

        with self.engine_new.begin() as conn:
            for org_id, df_itens in grupos_orgaos:
                buyer_id_int = int(org_id)
                
                # Identifica a data mais antiga do lote para usar na Transação
                datas_lote = pd.to_datetime(df_itens['data_criacao'], errors='coerce').dropna()
                data_transacao = datas_lote.min().strftime('%Y-%m-%d %H:%M:%S') if not datas_lote.empty else self.now

                # A. Cria a Transação 
                result_tx = conn.execute(text("""
                    INSERT INTO transactions (transaction_date, transaction_type_id, supplier_id, buyer_id, 
                    doc_type_id, doc_date, purchase_date, created_by, details, amount_total, amount_discount, created_at, updated_at) 
                    VALUES (:data_tx, 1, :sid, :bid, 3, :data_tx, :data_tx, 1, 'Migração Inicial Insumos', 1, 0, :now, :now)
                """), {"data_tx": data_transacao, "now": self.now, "sid": fornecedor_id, "bid": buyer_id_int})
                tx_id = result_tx.lastrowid
                self.stats["transactions"] += 1

                # B. Alimenta os dicionários em memória
                for _, row in df_itens.iterrows():
                    p_id_val = int(row['product_id'])
                    qtd = float(row['estoque_atual'])
                    condicao_id = int(row['condition_id'])
                    data_item = pd.to_datetime(row['data_criacao']).strftime('%Y-%m-%d %H:%M:%S') if pd.notna(row['data_criacao']) else self.now
                    addr_id = 1 

                    product_items_batch.append({
                        "pid": p_id_val, "code": contador_codigo_unico, "cond": condicao_id, 
                        "addr": addr_id, "org": buyer_id_int, "qty": qtd, "now": data_item
                    })
                    
                    transaction_items_batch.append({
                        "tid": tx_id, "pid": p_id_val, "cond": condicao_id, 
                        "addr": addr_id, "qty": qtd, "now": data_item
                    })
                    
                    contador_codigo_unico += 1

            # C. BULK INSERT MASSIVO 
            if product_items_batch:
                chunk_size = 5000
                print(f"   💾 Realizando dump massivo de {len(product_items_batch)} Product Items...")
                
                for i in range(0, len(product_items_batch), chunk_size):
                    conn.execute(text("""
                        INSERT INTO product_items (product_id, code, category_id, condition_id, address_id, organization_id, average_cost, quantity, created_at, updated_at) 
                        VALUES (:pid, :code, 1, :cond, :addr, :org, 1, :qty, :now, :now)
                    """), product_items_batch[i:i+chunk_size])
                    self.stats["product_items_criados"] += len(product_items_batch[i:i+chunk_size])

            if transaction_items_batch:
                print(f"   💾 Realizando dump massivo de {len(transaction_items_batch)} Transaction Items...")
                
                for i in range(0, len(transaction_items_batch), chunk_size):
                    conn.execute(text("""
                        INSERT INTO transaction_items (transaction_id, product_id, category_id, condition_id, warranty_date, address_id, unit_cost, quantity, created_at, updated_at, deleted_at) 
                        VALUES (:tid, :pid, 1, :cond, :now, :addr, 0, :qty, :now, :now, NULL)
                    """), transaction_items_batch[i:i+chunk_size])
                    self.stats["transaction_items"] += len(transaction_items_batch[i:i+chunk_size])

    # ==============================================================================
    # ORQUESTRADOR
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 INICIANDO MIGRAÇÃO: INSUMOS (BULK INSERT MASSIVO)")
        print("=" * 80)
        
        try:
            df_master = self._extrair_e_transformar()
            df_master = self._carregar_dimensionais(df_master)
            self._gerar_inventario(df_master)

            print("\n" + "=" * 50)
            print("📊 RELATÓRIO FINAL DE INSUMOS")
            print("=" * 50)
            print(f"📦 Novos Grupos:           {self.stats['grupos']}")
            print(f"📁 Novos Tipos:            {self.stats['types']}")
            print(f"🛒 Produtos (Insumos):     {self.stats['products']}")
            print("-" * 50)
            print(f"📑 Transações Geradas:     {self.stats['transactions']}")
            print(f"📦 Blocos de Estoque (PI): {self.stats['product_items_criados']}")
            print(f"🧩 Entradas de Estoque:    {self.stats['transaction_items']}")
            print("=" * 50)
            print("✅ Migração de Insumos Concluída com Sucesso!")

        except Exception as e:
            print(f"\n❌ ERRO CRÍTICO NO PIPELINE DE INSUMOS: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

def executar(eng_novo, eng_legado):
    migrador = MigracaoInsumos(eng_novo, eng_legado)
    migrador.executar()