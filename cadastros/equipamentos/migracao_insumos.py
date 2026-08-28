import pandas as pd
import sqlalchemy as sa
import time
import sys
import re
import unicodedata
from datetime import datetime
from tqdm import tqdm
from sqlalchemy import text

from config.config import MAPPING_ALUCOM, MAPPING_AS, MAPPING_IP, MAPPING_MOREIA, MAPPING_SC, EQUIPAMENTOS_TIPOS

TODOS_ORGAOS_MAPEADOS = set().union(MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, MAPPING_SC)

class MigracaoInsumos:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        self.stats = {
            "grupos": 0, "types": 0, "products": 0, "locations": 0,
            "transacoes_entrada": 0, "transacoes_saida": 0, 
            "product_items_criados": 0, "transaction_items": 0,
            "itens_ignorados_equipamento": 0
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
        return 1115

    def _extrair_grupo_do_nome(self, produto, tipo):
        p = str(produto).strip().upper()
        t = str(tipo).strip().upper()
        g = p.replace(t, '').strip()
        if t.endswith('S'):
            g = g.replace(t[:-1], '').strip()
        g = re.sub(r'\s+', ' ', g)
        g = re.sub(r'^[-\s]+', '', g)
        return g if g else "GERAL"

    def _mapear_condicao(self, condicao):
        c = str(condicao).strip().upper()
        if 'NOVO' in c: return 1
        if 'REVISADO' in c: return 2
        if 'REMAN' in c: return 3
        return 1

    def _normalizar_string(self, texto):
        if pd.isna(texto): return ""
        s = str(texto).upper().strip()
        return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

    def _carregar_mapa_org_address(self):
        print("\n📍 Carregando addresses default (BASE) por organização...")
        with self.engine_new.begin() as conn:
            # 1) Address com alias BASE
            rows = conn.execute(text("""
                SELECT addressable_id AS org_id, MIN(id) AS address_id
                FROM addresses
                WHERE addressable_id IN (1115, 1311, 1122, 1378)
                  AND deleted_at IS NULL
                  AND UPPER(alias) LIKE '%BASE%'
                GROUP BY addressable_id
            """)).fetchall()
            mapa = {int(r[0]): int(r[1]) for r in rows}

            # 2) Fallback: org sem BASE pega o primeiro address
            rows_all = conn.execute(text("""
                SELECT addressable_id AS org_id, MIN(id) AS address_id
                FROM addresses
                WHERE addressable_id IN (1115, 1311, 1122, 1378)
                  AND deleted_at IS NULL
                GROUP BY addressable_id
            """)).fetchall()
            for org_id, addr_id in rows_all:
                mapa.setdefault(int(org_id), int(addr_id))

        print(f"   ✅ Mapa ORG→ADDRESS: {mapa}")
        return mapa

    # ==============================================================================
    # ETL: EXTRAÇÃO (100K+ MOVIMENTOS DO LEGADO)
    # ==============================================================================
    def _extrair_e_transformar(self):
        print("\n📖 Extraindo histórico do banco legado...")
        
        query_legado = """
            SELECT PA.PRO_ID AS ID
                 , PA.PRO_NOME AS PRODUTO
                 , GPA.GPA_NOME AS CONDICAO
                 , SPA.SGA_NOME AS TIPO
                 , PA.PRO_QUANT_MIN AS QUANTI_MININA
                 , PA.PRO_QUANT_MAX AS QUANTI_MAX
                 , IESA.IES_VALOR_UNITARIO AS PRECO
                 , ESA.ESA_ID AS LEGACY_TX_ID
                 , ESA.ESA_TIPO AS TIPO_MOVIMENTO
                 , IESA.IES_QUANTIDADE AS QUANTIDADE_MOVIMENTO
                 , O.ORG_ID AS LEGACY_ORG_ID
                 , O.ORG_NOME AS ORGAO
                 , IESA.CREATED_AT AS DATA_TRANSACAO
                 , DEP.DPA_ID AS ID_DEPARTAMENTO
                 , DEP.DPA_NOME AS DEPARTAMENTO
            FROM PRODUTOS_ALM PA
            LEFT JOIN SUBGRUPOS_PRODUTO_ALM SPA ON PA.SGA_ID = SPA.SGA_ID
            LEFT JOIN GRUPOS_PRODUTO_ALM GPA ON SPA.GPA_ID = GPA.GPA_ID
            INNER JOIN ITENS_ENTRADA_SAIDA_ALM IESA ON PA.PRO_ID = IESA.PRO_ID
            INNER JOIN ENTRADAS_SAIDAS_ALM ESA ON IESA.ESA_ID = ESA.ESA_ID
            LEFT JOIN ORGAOS O ON ESA.ORG_ID = O.ORG_ID
            LEFT JOIN DEPARTAMENTOS_ALM DEP ON ESA.DPA_ID = DEP.DPA_ID
            WHERE GPA.ORG_ID IN (1122, 1264, 1313, 1326, 1328, 1351, 1358, 1360, 1369)
              AND ESA.DELETED_AT IS NULL
              AND PA.PRO_ATIVO = 'S'
            ORDER BY IESA.CREATED_AT ASC;
        """
        
        with self.engine_legado.connect() as conn:
            df_movimentos = pd.read_sql(text(query_legado), conn)

        print(f"🔍 Foram extraídos {len(df_movimentos)} movimentos válidos.")

        lista_mestre = []
        for index, row in tqdm(df_movimentos.iterrows(), total=df_movimentos.shape[0], desc="Mapeando Dicionários"):
            nome_produto = str(row['PRODUTO']).strip() if pd.notna(row['PRODUTO']) else "INSUMO SEM NOME"
            tipo = str(row['TIPO']).strip() if pd.notna(row['TIPO']) else "OUTROS"
            tipo_upper = tipo.upper()

            if tipo_upper in EQUIPAMENTOS_TIPOS:
                self.stats["itens_ignorados_equipamento"] += 1
                continue
            
            grupo = self._extrair_grupo_do_nome(nome_produto, tipo)
            condicao_id = self._mapear_condicao(row['CONDICAO'])
            
            raw_dep = str(row['DEPARTAMENTO']).strip() if pd.notna(row['DEPARTAMENTO']) else ""
            departamento = raw_dep if raw_dep and raw_dep.upper() != 'NONE' else "ALMOXARIFADO"
            
            qtd = float(row['QUANTIDADE_MOVIMENTO'])
            tipo_mov = str(row['TIPO_MOVIMENTO']).strip().upper() 
            org_destino = self._descobrir_id_organizacao_destino(row['LEGACY_ORG_ID'])
            data_full = row['DATA_TRANSACAO'] if pd.notna(row['DATA_TRANSACAO']) else self.now

            try:
                min_q = float(row['QUANTI_MININA']) if pd.notna(row['QUANTI_MININA']) else 0.0
                max_q = float(row['QUANTI_MAX']) if pd.notna(row['QUANTI_MAX']) else 0.0
            except:
                min_q, max_q = 0.0, 0.0

            if min_q == 0: min_q = 1.0
            if max_q == 0 or max_q < min_q: max_q = min_q

            net_qty = qtd if tipo_mov == 'E' else -qtd

            lista_mestre.append({
                "id_legado": row['ID'],
                "legacy_tx_id": row['LEGACY_TX_ID'], 
                "nome_produto": nome_produto,
                "grupo": grupo,
                "tipo": tipo,
                "departamento": departamento,
                "min_quantity": min_q,
                "max_quantity": max_q,
                "preco_unitario": float(row['PRECO']) if pd.notna(row['PRECO']) else 0.0,
                "condition_id": condicao_id,
                "tipo_movimento": tipo_mov,
                "quantidade": qtd,
                "net_qty": net_qty,
                "org_destino": org_destino,
                "data_transacao": data_full,
                "data_curta": pd.to_datetime(data_full).strftime('%Y-%m-%d')
            })

        return pd.DataFrame(lista_mestre)

    # ==============================================================================
    # ETL: CARGA DE CADASTROS (Com Dicionário Anti-Colisão)
    # ==============================================================================
    def _carregar_dimensionais(self, df_master):
        print("\n🚀 Persistindo Cadastros Base (Grupos, Tipos, Produtos)...")
        
        # --- 1. GROUPS ---
        grupos_unicos = df_master['grupo'].dropna().unique()
        with self.engine_new.begin() as conn:
            db_groups = pd.read_sql("SELECT id, name FROM `groups`", conn)
            
        mapa_db_groups = {self._normalizar_string(row['name']): row['id'] for _, row in db_groups.iterrows()}
        
        grupos_para_inserir = []
        for g in grupos_unicos:
            norm_g = self._normalizar_string(g)
            if norm_g not in mapa_db_groups:
                grupos_para_inserir.append(g)
                mapa_db_groups[norm_g] = None 
                
        if grupos_para_inserir:
            df_groups = pd.DataFrame({"name": grupos_para_inserir, "created_at": self.now, "updated_at": self.now})
            with self.engine_new.begin() as conn:
                df_groups.to_sql('groups', con=conn, if_exists='append', index=False)
            self.stats["grupos"] += len(grupos_para_inserir)

        with self.engine_new.begin() as conn:
            db_groups = pd.read_sql("SELECT id as group_id, name FROM `groups`", conn)
        mapa_db_groups_final = {self._normalizar_string(row['name']): row['group_id'] for _, row in db_groups.iterrows()}
        df_master['group_id'] = df_master['grupo'].apply(lambda x: mapa_db_groups_final.get(self._normalizar_string(x))).astype("Int64")

        # --- 2. TYPES ---
        tipos_unicos = df_master['tipo'].dropna().unique()
        with self.engine_new.begin() as conn:
            db_types = pd.read_sql("SELECT id, name FROM `types`", conn)
            
        mapa_db_types = {self._normalizar_string(row['name']): row['id'] for _, row in db_types.iterrows()}
        
        tipos_para_inserir = []
        for t in tipos_unicos:
            norm_t = self._normalizar_string(t)
            if norm_t not in mapa_db_types:
                tipos_para_inserir.append(t)
                mapa_db_types[norm_t] = None
                
        if tipos_para_inserir:
            df_types = pd.DataFrame({"name": tipos_para_inserir, "is_kit": 0, "created_at": self.now, "updated_at": self.now})
            with self.engine_new.begin() as conn:
                df_types.to_sql('types', con=conn, if_exists='append', index=False)
            self.stats["types"] += len(tipos_para_inserir)

        with self.engine_new.begin() as conn:
            db_types = pd.read_sql("SELECT id as type_id, name FROM `types`", conn)
        mapa_db_types_final = {self._normalizar_string(row['name']): row['type_id'] for _, row in db_types.iterrows()}
        df_master['type_id'] = df_master['tipo'].apply(lambda x: mapa_db_types_final.get(self._normalizar_string(x))).astype("Int64")

        # --- 3. PRODUCTS ---
        df_produtos_unicos = df_master[['nome_produto', 'type_id', 'group_id', 'min_quantity', 'max_quantity']].drop_duplicates(subset=['nome_produto', 'type_id', 'group_id'])
        
        with self.engine_new.begin() as conn:
            db_products = pd.read_sql("SELECT id, name FROM products WHERE is_asset = 0", conn)
            
        mapa_db_products = {self._normalizar_string(row['name']): row['id'] for _, row in db_products.iterrows()}
        
        produtos_para_inserir = []
        for _, row in df_produtos_unicos.iterrows():
            nome = row['nome_produto']
            norm_p = self._normalizar_string(nome)
            if norm_p not in mapa_db_products:
                produtos_para_inserir.append({
                    'name': nome,
                    'brand_id': None,
                    'type_id': row['type_id'],
                    'group_id': row['group_id'],
                    'is_asset': 0,
                    'length': 0, 'width': 0, 'height': 0, 'weight': 0,
                    'min_quantity': row['min_quantity'],
                    'max_quantity': row['max_quantity'],
                    'created_at': self.now, 'updated_at': self.now
                })
                mapa_db_products[norm_p] = None
                
        if produtos_para_inserir:
            df_products_inserir = pd.DataFrame(produtos_para_inserir)
            with self.engine_new.begin() as conn:
                df_products_inserir.to_sql('products', con=conn, if_exists='append', index=False)
            self.stats["products"] += len(produtos_para_inserir)

        with self.engine_new.begin() as conn:
            db_products = pd.read_sql("SELECT id as product_id, name as nome_produto FROM products", conn)
        
        mapa_db_products_final = {self._normalizar_string(row['nome_produto']): row['product_id'] for _, row in db_products.iterrows()}
        df_master['product_id'] = df_master['nome_produto'].apply(lambda x: mapa_db_products_final.get(self._normalizar_string(x))).astype("Int64")

        return df_master

    # ==============================================================================
    # ETL: PROCESSAMENTO MASSIVO (EVENT SOURCING E SALDO FINAL)
    # ==============================================================================
    def _gerar_inventario(self, df_master):
        print("\n📊 Processando Linha do Tempo e Saldo Final...")
        
        with self.engine_new.begin() as conn:
            res = conn.execute(text("SELECT id FROM suppliers WHERE name = 'ALUCOM LTDA'")).fetchone()
            if not res:
                res = conn.execute(text("INSERT INTO suppliers (name, alias, cpf_cnpj, created_at, updated_at) VALUES ('ALUCOM LTDA', 'ALUCOM', '00000000000000', :now, :now)"), {"now": self.now})
                fornecedor_padrao_id = res.lastrowid
            else:
                fornecedor_padrao_id = res[0]

        # ORG -> ADDRESS DEFAULT (BASE)
        mapa_org_address = self._carregar_mapa_org_address()

        # --- CRIAÇÃO DE LOCATIONS ---
        print("   📍 Mapeando e criando Locations por Address (Almoxarifados/Departamentos)...")
        mapa_locations = {}           # chave: (address_id, departamento) -> location_id
        fallback_almox_por_addr = {}  # address_id -> location_id do ALMOXARIFADO

        df_master['departamento'] = df_master['departamento'].astype(str).str.strip().str.upper()
        pares_org_dep = df_master[['org_destino', 'departamento']].drop_duplicates()

        addr_ids = sorted(set(mapa_org_address.values()))
        ids_str = ",".join(str(i) for i in addr_ids)

        with self.engine_new.begin() as conn:
            # Locations existentes dos addresses das orgs mapeadas
            db_locations = conn.execute(text(
                f"SELECT id, address_id, area FROM locations "
                f"WHERE address_id IN ({ids_str}) AND deleted_at IS NULL"
            )).fetchall()
            for loc_id, addr_id, area in db_locations:
                mapa_locations[(int(addr_id), str(area).strip().upper())] = loc_id

            # Fallback ALMOXARIFADO por address
            for addr_id in addr_ids:
                existe = conn.execute(text(
                    "SELECT id FROM locations WHERE address_id = :a "
                    "AND UPPER(area) = 'ALMOXARIFADO' AND deleted_at IS NULL"
                ), {"a": addr_id}).fetchone()
                if existe:
                    fallback_almox_por_addr[addr_id] = existe[0]
                else:
                    res_loc = conn.execute(text("""
                        INSERT INTO locations (address_id, area, section, spot, details, created_at, updated_at)
                        VALUES (:addr, 'ALMOXARIFADO', 'ÚNICA', 'ÚNICO', 'Criado via Migração (Fallback)', :now, :now)
                    """), {"addr": addr_id, "now": self.now})
                    fallback_almox_por_addr[addr_id] = res_loc.lastrowid
                    self.stats["locations"] += 1

            # Departamentos que não existem em cada address
            for _, row in pares_org_dep.iterrows():
                addr_id = mapa_org_address.get(int(row['org_destino']))
                if not addr_id:
                    continue
                dep = row['departamento']
                if dep and (addr_id, dep) not in mapa_locations:
                    res_loc = conn.execute(text("""
                        INSERT INTO locations (address_id, area, section, spot, details, created_at, updated_at)
                        VALUES (:addr, :area, 'ÚNICA', 'ÚNICO', 'Criado via Migração', :now, :now)
                    """), {"addr": addr_id, "area": dep, "now": self.now})
                    mapa_locations[(addr_id, dep)] = res_loc.lastrowid
                    self.stats["locations"] += 1

        # 1. ORDENAÇÃO CRONOLÓGICA ABSOLUTA
        df_master['data_transacao'] = pd.to_datetime(df_master['data_transacao'])
        df_master = df_master.sort_values(by='data_transacao')

        # Dicionários em Memória
        livro_razao = {}
        ultimos_precos = {}

        print("   ⏳ Reconstruindo o histórico de transações...")
        
        # Agrupamento cronológico seguro
        grupos_transacoes = df_master.groupby(['legacy_tx_id', 'org_destino', 'tipo_movimento'], sort=False)
        transaction_items_batch = []

        with self.engine_new.begin() as conn:
            for (legacy_tx, org_id, tipo_mov), df_itens in tqdm(grupos_transacoes, desc="Gerando Transações"):
                buyer_id_int = int(org_id)
                addr_id = mapa_org_address.get(buyer_id_int, 1)
                data_tx = df_itens['data_transacao'].iloc[0].strftime('%Y-%m-%d %H:%M:%S')

                if tipo_mov == 'E':
                    tx_type = 1 
                    sid = fornecedor_padrao_id
                    bid = buyer_id_int
                    sender_id = None
                    detalhes = f'Entrada Histórica (Ref. Legado ESA_ID: {legacy_tx})'
                    self.stats["transacoes_entrada"] += 1
                else:
                    tx_type = 3 
                    sid = None
                    bid = None
                    sender_id = buyer_id_int 
                    detalhes = f'Saída Histórica (Ref. Legado ESA_ID: {legacy_tx})'
                    self.stats["transacoes_saida"] += 1

                result_tx = conn.execute(text("""
                    INSERT INTO transactions (
                        transaction_date, transaction_type_id, supplier_id, buyer_id, 
                        sender_id, receiver_id, customer_id,
                        doc_type_id, doc_date, purchase_date, created_by, 
                        details, amount_total, amount_discount, created_at, updated_at
                    ) 
                    VALUES (
                        :data_tx, :type, :sid, :bid, 
                        :sender_id, NULL, NULL,
                        3, :data_tx, :data_tx, 1, 
                        :det, 1, 0, :now, :now
                    )
                """), {
                    "data_tx": data_tx, "type": tx_type, "sid": sid, "bid": bid, 
                    "sender_id": sender_id, "det": detalhes, "now": self.now
                })
                tx_id = result_tx.lastrowid

                for _, row in df_itens.iterrows():
                    pid = int(row['product_id'])
                    cond = int(row['condition_id'])
                    qtd = float(row['quantidade'])
                    preco_unit = float(row['preco_unitario'])
                    data_item = row['data_transacao'].strftime('%Y-%m-%d %H:%M:%S')
                    dep_nome = str(row['departamento']).strip().upper()
                    loc_id = mapa_locations.get((addr_id, dep_nome), fallback_almox_por_addr.get(addr_id))


                    # 2. CÁLCULO INTERNO: ATUALIZA O ESTOQUE CONSIDERANDO A LOCATION
                    chave_estoque = (pid, buyer_id_int, cond, loc_id)
                    ultimos_precos[chave_estoque] = preco_unit
                    
                    if chave_estoque not in livro_razao:
                        livro_razao[chave_estoque] = 0.0

                    if tipo_mov == 'E':
                        livro_razao[chave_estoque] += qtd
                    else:
                        livro_razao[chave_estoque] -= qtd

                    transaction_items_batch.append({
                        "tid": tx_id, "pid": pid, "cond": cond,
                        "addr": addr_id,
                        "loc": loc_id,
                        "unit_cost": preco_unit,
                        "qty": qtd, "now": data_item
                    })
                    
            if transaction_items_batch:
                print(f"\n   💾 Despejando {len(transaction_items_batch)} itens de transação no banco...")
                for i in range(0, len(transaction_items_batch), 5000):
                    conn.execute(text("""
                        INSERT INTO transaction_items (
                            transaction_id, product_id, category_id, condition_id, warranty_date,
                            address_id, location_id, unit_cost, quantity,
                            created_at, updated_at, deleted_at
                        )
                        VALUES (
                            :tid, :pid, 1, :cond, :now,
                            :addr, :loc, :unit_cost, :qty,
                            :now, :now, NULL
                        )
                    """), transaction_items_batch[i:i+5000])

        # 3. GERA OS 'PRODUCT ITEMS' COM LOCATION_ID MANTENDO A DIVISÃO CORRETA
        print("\n   📦 Consolidando o Saldo Final Físico (Product Items)...")
        product_items_batch = []
        contador_codigo_unico = 9000000 
        
        for chave_estoque, saldo_final in livro_razao.items():
            if saldo_final > 0:
                pid, org_id, cond, loc_id = chave_estoque
                addr_id = mapa_org_address.get(org_id, 1) 
                avg_cost_final = ultimos_precos.get(chave_estoque, 0.0)
                
                product_items_batch.append({
                    "pid": pid, "code": contador_codigo_unico, "cond": cond,
                    "addr": addr_id, "org": org_id, "qty": saldo_final,
                    "avg_cost": avg_cost_final, "loc_id": loc_id, "now": self.now
                })
                contador_codigo_unico += 1
                
        if product_items_batch:
            print(f"   💾 Injetando {len(product_items_batch)} lotes de estoque real com Locations (product_items)...")
            with self.engine_new.begin() as conn:
                for i in range(0, len(product_items_batch), 5000):
                    conn.execute(text("""
                        INSERT INTO product_items (
                            product_id, code, category_id, condition_id, address_id, 
                            organization_id, location_id, average_cost, quantity, 
                            created_at, updated_at
                        ) 
                        VALUES (
                            :pid, :code, 1, :cond, :addr, 
                            :org, :loc_id, :avg_cost, :qty, 
                            :now, :now
                        )
                    """), product_items_batch[i:i+5000])
                    self.stats["product_items_criados"] += len(product_items_batch[i:i+5000])

    # ==============================================================================
    # ORQUESTRADOR
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 INICIANDO MIGRAÇÃO: INSUMOS")
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
            print(f"📍 Novas Locations:        {self.stats['locations']}")
            print(f"🛒 Produtos (Insumos):     {self.stats['products']}")
            print(f"🚫 Movimentos ignorados (Equipamentos): {self.stats['itens_ignorados_equipamento']}")
            print("-" * 50)
            print(f"📈 Transações de Entrada:  {self.stats['transacoes_entrada']}")
            print(f"📉 Transações de Saída:    {self.stats['transacoes_saida']}")
            print(f"🧩 Itens Movimentados:     {self.stats['transaction_items']}")
            print(f"📦 Saldo Final Consolidado:{self.stats['product_items_criados']}")
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