#!/usr/bin/env python3
"""
Migração direta do CSV controle_interno_aluguel_equipamentos.csv
para o banco refatorado (controle-interno).

Ordem: suppliers → brands → types → groups → products → product_items
       → transactions → transaction_items → equipments → equipment_history
"""

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm
import os
import sys

# Adiciona o path do projeto para importar config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.database import obter_engines
from config.config import ENDERECOS_BASES, MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, MAPPING_SC


# ============================================================
# CONFIGURAÇÕES
# ============================================================
ARQUIVO_CSV = "controle_interno_aluguel_equipamentos.csv"
CHUNK_SIZE = 1000
NOW = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# Mapeamento legado -> novo status
MAPEAMENTO_STATUS = {
    1: 2,   # ALUGADO -> ALUGADO
    2: 4,   # EMPRESTADO -> EMPRESTADO
    3: 6,   # EM MANUTENÇÃO -> EM MANUTENÇÃO
    4: 9,   # SEM CONSERTO -> BAIXADO (aprox)
    5: 5,   # USO DA ALUCOM -> USO INTERNO
    6: 7,   # GARANTIA -> NA GARANTIA
    7: 1,   # LIBERADO P/ LOCACAO -> DISPONÍVEL PARA LOCAÇÃO
    8: 1,   # GABINETE VAZIO -> DISPONÍVEL PARA LOCAÇÃO
    9: 9,   # COM DEFEITO -> BAIXADO
    10: 9,  # BAIXADO -> BAIXADO
    11: 1,  # DISPONÍVEL P/ LOCAÇÃO -> DISPONÍVEL PARA LOCAÇÃO
    12: 6,  # MANUTENÇÃO NA GARANTIA -> EM MANUTENÇÃO
    13: 6,  # MANUTENÇÃO INTERNA -> EM MANUTENÇÃO
    14: 8,  # EM DEVOLUÇÃO -> EM DEVOLUÇÃO
    15: 3,  # RESERVADO -> RESERVADO
    16: 5,  # USO INTERNO -> USO INTERNO
}
STATUS_DEFAULT = 1  # DISPONÍVEL PARA LOCAÇÃO

# Mapeamento organização legado -> novo
TODOS_ORGAOS_MAPEADOS = set().union(
    MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, MAPPING_SC
)

def descobrir_org_destino(id_legado):
    if pd.isna(id_legado): return 1115
    i = int(id_legado)
    if i in MAPPING_ALUCOM: return 1115
    if i in MAPPING_IP: return 1311
    if i in MAPPING_MOREIA: return 1122
    if i in MAPPING_AS: return 1378
    if i in MAPPING_SC: return 1115
    return i


# ============================================================
# HELPERS
# ============================================================
def nula(val):
    return None if pd.isna(val) else val

def extrair_marca(nome):
    """Extrai marca do nome do equipamento (primeira palavra conhecida)."""
    if pd.isna(nome): return "DESCONHECIDA"
    partes = str(nome).strip().split()
    # Marcas conhecidas no seu CSV
    marcas_conhecidas = {'COLETEK', 'TCN', 'BEM', 'LIGADO', 'NS', 'DELL', 'HP', 'LENOVO',
                         'SAMSUNG', 'LG', 'EPSON', 'CANON', 'BROTHER', 'XEROX', 'RICOH',
                         'KYOCERA', 'OKI', 'LEXMARK', 'ZEBRA', 'HONEYWELL', 'DATALOGIC',
                         'MOTOROLA', 'SYMBOL', 'INTERMEC', 'TECNOMAQ', 'ARTEC', 'ELGIN',
                         'BEMATECH', 'GERTEC', 'INGENICO', 'VERIFONE', 'PAX', 'SUNMI'}
    for p in partes:
        if p.upper() in marcas_conhecidas:
            return p.upper()
    # Fallback: primeira palavra com mais de 3 chars
    for p in partes:
        if len(p) > 3:
            return p.upper()
    return partes[0].upper() if partes else "DESCONHECIDA"

def extrair_tipo(nome):
    """Extrai tipo/categoria do nome."""
    if pd.isna(nome): return "OUTROS"
    n = str(nome).upper()
    if 'ESTABILIZADOR' in n: return 'ESTABILIZADOR'
    if 'MONITOR' in n: return 'MONITOR'
    if 'NOTEBOOK' in n or 'LAPTOP' in n: return 'NOTEBOOK'
    if 'DESKTOP' in n or 'CPU' in n or 'COMPUTADOR' in n: return 'DESKTOP'
    if 'IMPRESSORA' in n or 'PRINTER' in n: return 'IMPRESSORA'
    if 'NOBREAK' in n or 'UPS' in n: return 'NOBREAK'
    if 'SWITCH' in n: return 'SWITCH'
    if 'ROTEADOR' in n or 'ROUTER' in n: return 'ROTEADOR'
    if 'ACESS POINT' in n or 'ACCESS POINT' in n or 'AP ' in n: return 'ACCESS POINT'
    if 'TELEFONE' in n or 'RAMAL' in n: return 'TELEFONE'
    if 'HEADSET' in n or 'FONE' in n: return 'HEADSET'
    if 'PROJETOR' in n: return 'PROJETOR'
    if 'TABLET' in n: return 'TABLET'
    if 'CELULAR' in n or 'SMARTPHONE' in n: return 'CELULAR'
    if 'SERVIDOR' in n: return 'SERVIDOR'
    if 'STORAGE' in n or 'HD ' in n or 'SSD' in n: return 'ARMAZENAMENTO'
    # Pega primeira palavra significativa
    for p in n.split():
        if len(p) > 4 and p not in {'COLETEK', 'BEM', 'LIGADO', 'NS', 'VA', 'W', 'KVA'}:
            return p
    return "OUTROS"

def extrair_grupo(tipo):
    """Grupo geral baseado no tipo."""
    grupos = {
        'ESTABILIZADOR': 'ENERGIA',
        'NOBREAK': 'ENERGIA',
        'MONITOR': 'VIDEO',
        'NOTEBOOK': 'COMPUTADOR',
        'DESKTOP': 'COMPUTADOR',
        'IMPRESSORA': 'IMPRESSAO',
        'SWITCH': 'REDE',
        'ROTEADOR': 'REDE',
        'ACCESS POINT': 'REDE',
        'TELEFONE': 'TELEFONIA',
        'HEADSET': 'TELEFONIA',
        'PROJETOR': 'VIDEO',
        'TABLET': 'MOVEL',
        'CELULAR': 'MOVEL',
        'SERVIDOR': 'SERVIDOR',
        'ARMAZENAMENTO': 'ARMAZENAMENTO',
    }
    return grupos.get(tipo, 'GERAL')


# ============================================================
# CLASSE PRINCIPAL
# ============================================================
class MigracaoEquipamentosCSV:

    def __init__(self):
        self.engine_new, self.engine_legado = obter_engines()
        self.stats = {
            "suppliers": 0, "brands": 0, "types": 0, "groups": 0,
            "products": 0, "product_items": 0, "transactions": 0,
            "transaction_items": 0, "equipments": 0, "equipment_history": 0,
            "pulados_org": 0, "pulados_tombo": 0, "erros": 0
        }
        self.id_fallback_endereco = 1
        # Carregar endereços-base reais do banco (criados pela migração de clientes)
        with self.engine_new.connect() as conn:
            rows = conn.execute(text("""
                SELECT addressable_id, id FROM addresses
                WHERE addressable_type = 'organization' AND deleted_at IS NULL
            """)).fetchall()
            self.mapa_enderecos_base = {int(r[0]): int(r[1]) for r in rows}
        print(f"   📍 Endereços-base carregados: {self.mapa_enderecos_base}")

    def executar(self):
        print("=" * 70)
        print("🚀 MIGRAÇÃO EQUIPAMENTOS - controle_interno_aluguel_equipamentos.csv")
        print("=" * 70)

        # 1. Carregar CSV
        df = self._carregar_csv()
        if df.empty:
            print("❌ CSV vazio ou erro na leitura.")
            return

        # 2. Filtrar e preparar
        df = self._preparar_dados(df)
        if df.empty:
            print("❌ Nenhum equipamento válido após filtros.")
            return

        print(f"\n📦 {len(df)} equipamentos para migrar")

        # 3. Dimensões
        self._criar_dimensoes(df)

        # 4. Products + Product Items
        df = self._criar_produtos_e_itens(df)

        # 5. Transactions + Transaction Items + Equipments + History
        self._criar_transacoes_equipamentos_historico(df)

        # 6. Resumo
        self._imprimir_resumo()

    def _carregar_csv(self):
        print(f"\n📖 Lendo {ARQUIVO_CSV}...")
        try:
            df = pd.read_csv(ARQUIVO_CSV, sep=",", encoding="utf-8",
                           on_bad_lines="skip", low_memory=False)
            print(f"   Linhas lidas: {len(df)}")
            print(f"   Colunas: {list(df.columns)}")
            return df
        except Exception as e:
            print(f"❌ Erro ao ler CSV: {e}")
            return pd.DataFrame()

    def _preparar_dados(self, df):
        print("\n🔧 Preparando dados...")

        # Renomear colunas para facilitar
        df = df.rename(columns={
            'id': 'id_legado',
            'numero': 'tombo',
            'numero_antigo': 'tombo_antigo',
            'nome': 'nome',
            'descricao': 'descricao',
            'empresa_aquisicao': 'empresa_aquisicao',
            'nota_fiscal': 'nota_fiscal',
            'uso_interno': 'uso_interno',
            'valor_real': 'valor_real',
            'valor_dolar': 'valor_dolar',
            'valor_aluguel': 'valor_aluguel',
            'valor_alugado': 'valor_alugado',
            'data_aquisicao': 'data_aquisicao',
            'data_locacao': 'data_locacao',
            'data_garantia': 'data_garantia',
            'tipo_id': 'tipo_id_legado',
            'situacao_id': 'situacao_id_legado',
            'fornecedor_id': 'fornecedor_id_legado',
            'orgao_id': 'orgao_id_legado',
        })

        # Filtros
        original_len = len(df)

        # Remover tombos 0/NaN
        df['tombo'] = pd.to_numeric(df['tombo'], errors='coerce')
        df = df[df['tombo'].notna() & (df['tombo'] > 0)]
        self.stats['pulados_tombo'] = original_len - len(df)

        # Filtrar orgãos mapeados
        df['org_destino'] = df['orgao_id_legado'].apply(descobrir_org_destino)
        df = df[df['orgao_id_legado'].isin(TODOS_ORGAOS_MAPEADOS)]
        self.stats['pulados_org'] = original_len - self.stats['pulados_tombo'] - len(df)

        # Status
        df['status_id'] = df['situacao_id_legado'].map(MAPEAMENTO_STATUS).fillna(STATUS_DEFAULT).astype(int)

        # Datas
        for col in ['data_aquisicao', 'data_locacao', 'data_garantia', 'created_at', 'updated_at', 'deleted_at']:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors='coerce')

        # Created/updated at para as tabelas novas
        df['created_at_novo'] = df['created_at'].fillna(NOW)
        df['updated_at_novo'] = df['updated_at'].fillna(NOW)
        df['deleted_at_novo'] = df.apply(
            lambda r: r['deleted_at'] if pd.notna(r['deleted_at']) else (NOW if r['situacao_id_legado'] == 10 else None),
            axis=1
        )

        # Marca, Tipo, Grupo (inferidos do nome/descricao)
        df['marca'] = df['nome'].apply(extrair_marca)
        df['tipo'] = df['nome'].apply(extrair_tipo)
        df['grupo'] = df['tipo'].apply(extrair_grupo)

        # Serial number: extrair da descricao se parecer serial
        def extrair_serial(desc):
            if pd.isna(desc): return None
            s = str(desc).strip()
            # Padrão: NS XXXXXXXXXXXXXXXX ou similar
            import re
            m = re.search(r'(?:NS|SN|SERIAL|SÉRIE)\s*[:]?\s*([A-Z0-9]{10,})', s, re.IGNORECASE)
            if m: return m.group(1)
            # Fallback: alfanumérico longo
            m = re.search(r'([A-Z0-9]{15,})', s)
            if m: return m.group(1)
            return None

        df['serial_number'] = df['descricao'].apply(extrair_serial)

        # Valor de aquisição
        df['valor_aquisicao'] = pd.to_numeric(df['valor_real'], errors='coerce').fillna(0)

        print(f"   ✅ {len(df)} equipamentos válidos")
        print(f"   ⏭️  Pulados (tombo inválido): {self.stats['pulados_tombo']}")
        print(f"   ⏭️  Pulados (órgão não mapeado): {self.stats['pulados_org']}")
        return df.reset_index(drop=True)

    def _criar_dimensoes(self, df):
        print("\n📐 Criando tabelas dimensionais...")

        with self.engine_new.begin() as conn:
            # 1. SUPPLIERS (fornecedor_id_legado -> suppliers)
            print("   💾 Suppliers...")
            fornecedores_unicos = df['fornecedor_id_legado'].dropna().unique()
            if len(fornecedores_unicos) > 0:
                for f in fornecedores_unicos:
                    fid = int(f)
                    cnpj_dummy = f"0000000{fid:07d}"  # 14 dígitos
                    conn.execute(text("""
                        INSERT INTO suppliers (alias, name, cpf_cnpj, phone, email, created_at, updated_at)
                        VALUES (:alias, :name, :cnpj, :phone, :email, :now, :now)
                        ON DUPLICATE KEY UPDATE updated_at = :now
                    """), {
                        "alias": f'FORN_{fid}', "name": f'Fornecedor {fid}',
                        "cnpj": cnpj_dummy, "phone": '0000000000',
                        "email": 'nao@informado.com', "now": NOW
                    })
            self.stats['suppliers'] = len(fornecedores_unicos)

            # Fallback MIGRACAO
            res = conn.execute(text("SELECT id FROM suppliers WHERE name = 'MIGRACAO'")).fetchone()
            if res:
                self.id_forn_migracao = res[0]
            else:
                res = conn.execute(text("""
                    INSERT INTO suppliers (name, alias, cpf_cnpj, phone, email, created_at, updated_at)
                    VALUES ('MIGRACAO', 'MIGRACAO', '00000000000001', '0000000000', 'nao@informado.com', :now, :now)
                    ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
                """), {"now": NOW})
                self.id_forn_migracao = res.lastrowid
                self.stats['suppliers'] += 1

            # 2. BRANDS
            print("   💾 Brands...")
            marcas_unicas = df['marca'].dropna().unique()
            if len(marcas_unicas) > 0:
                for m in marcas_unicas:
                    nome = str(m).strip()
                    conn.execute(text("""
                        INSERT INTO brands (name, created_at, updated_at)
                        VALUES (:name, :now, :now)
                        ON DUPLICATE KEY UPDATE updated_at = :now
                    """), {"name": nome, "now": NOW})
            self.stats['brands'] = len(marcas_unicas)

            # 3. TYPES
            print("   💾 Types...")
            tipos_unicos = df['tipo'].dropna().unique()
            if len(tipos_unicos) > 0:
                for t in tipos_unicos:
                    nome = str(t).strip()
                    conn.execute(text("""
                        INSERT INTO types (name, is_kit, created_at, updated_at)
                        VALUES (:name, 0, :now, :now)
                        ON DUPLICATE KEY UPDATE updated_at = :now
                    """), {"name": nome, "now": NOW})
            self.stats['types'] = len(tipos_unicos)

            # 4. GROUPS
            print("   💾 Groups...")
            grupos_unicos = df['grupo'].dropna().unique()
            if len(grupos_unicos) > 0:
                for g in grupos_unicos:
                    nome = str(g).strip()
                    conn.execute(text("""
                        INSERT INTO `groups` (name, created_at, updated_at)
                        VALUES (:name, :now, :now)
                        ON DUPLICATE KEY UPDATE updated_at = :now
                    """), {"name": nome, "now": NOW})
            self.stats['groups'] = len(grupos_unicos)

        # Carregar IDs de volta
        with self.engine_new.connect() as conn:
            self.map_brand = dict(conn.execute(text("SELECT name, id FROM brands")).fetchall())
            self.map_type = dict(conn.execute(text("SELECT name, id FROM types")).fetchall())
            self.map_group = dict(conn.execute(text("SELECT name, id FROM `groups`")).fetchall())
            self.map_supplier = dict(conn.execute(text("SELECT alias, id FROM suppliers")).fetchall())

            # Fallback: alias -> id para fornecedor_id_legado
            self.map_forn_legado = {}
            for f in fornecedores_unicos:
                alias = f'FORN_{int(f)}'
                self.map_forn_legado[int(f)] = self.map_supplier.get(alias, self.id_forn_migracao)

        # Mapear no dataframe
        df['brand_id'] = df['marca'].map(self.map_brand).astype("Int64")
        df['type_id'] = df['tipo'].map(self.map_type).astype("Int64")
        df['group_id'] = df['grupo'].map(self.map_group).astype("Int64")
        df['supplier_id'] = df['fornecedor_id_legado'].map(self.map_forn_legado).astype("Int64")

    def _criar_produtos_e_itens(self, df):
        print("\n📦 Criando Products e Product Items...")

        # Produtos únicos por (brand, type, group)
        df_prod = df[['brand_id', 'type_id', 'group_id']].drop_duplicates().reset_index(drop=True)

        # Nome do produto = tipo + grupo + marca
        def nome_produto(row):
            marca = df[df['brand_id']==row['brand_id']]['marca'].iloc[0] if row['brand_id'] in df['brand_id'].values else ''
            tipo = df[df['type_id']==row['type_id']]['tipo'].iloc[0] if row['type_id'] in df['type_id'].values else ''
            grupo = df[df['group_id']==row['group_id']]['grupo'].iloc[0] if row['group_id'] in df['group_id'].values else ''
            return f"{tipo} {grupo} {marca}".strip() or "PRODUTO SEM ESPECIFICAÇÃO"

        df_prod['name'] = df_prod.apply(nome_produto, axis=1)

        with self.engine_new.begin() as conn:
            # Inserir products
            df_prod_ins = pd.DataFrame({
                'name': df_prod['name'],
                'brand_id': df_prod['brand_id'],
                'type_id': df_prod['type_id'],
                'group_id': df_prod['group_id'],
                'is_asset': 1, 'length': 0, 'width': 0, 'height': 0, 'weight': 0,
                'min_quantity': 1, 'max_quantity': 2,
                'created_at': NOW, 'updated_at': NOW
            })
            if not df_prod_ins.empty:
                df_prod_ins.to_sql('products', con=conn, if_exists='append', index=False)
            self.stats['products'] = len(df_prod_ins)

            # Recuperar product_ids
            db_products = pd.read_sql("""
                SELECT id as product_id, name, brand_id, type_id, group_id
                FROM products
            """, conn)

        # Merge product_id de volta
        df = df.merge(
            db_products[['product_id', 'brand_id', 'type_id', 'group_id']],
            on=['brand_id', 'type_id', 'group_id'], how='left'
        )
        df['product_id'] = df['product_id'].astype("Int64")

        # Product Items: um por equipamento (quantity=1)
        print("   💾 Product Items...")
        with self.engine_new.begin() as conn:
            for idx, row in tqdm(df.iterrows(), total=len(df), desc="Product Items"):
                addr_id = self.mapa_enderecos_base.get(int(row['org_destino']), self.id_fallback_endereco)
                code = f"PI-{row['id_legado']}"

                result = conn.execute(text("""
                    INSERT INTO product_items (product_id, code, category_id, condition_id,
                        address_id, organization_id, average_cost, quantity, created_at, updated_at)
                    VALUES (:pid, :code, 1, 1, :addr, :org, :cost, 1, :now, :now)
                """), {
                    "pid": int(row['product_id']), "code": code, "addr": addr_id,
                    "org": int(row['org_destino']), "cost": float(row['valor_aquisicao']), "now": NOW
                })
                df.at[idx, 'product_item_id'] = result.lastrowid
                self.stats['product_items'] += 1

        return df

    def _criar_transacoes_equipamentos_historico(self, df):
        print("\n💰 Criando Transactions, Transaction Items, Equipments e History...")

        with self.engine_new.begin() as conn:
            # Agrupar por (fornecedor, org, data_aquisicao) para criar transações
            df['data_compra'] = df['data_aquisicao'].fillna(df['created_at_novo'])

            grupos_tx = df.groupby(['supplier_id', 'org_destino', 'data_compra'], dropna=False)

            for (s_id, org_id, data_compra), df_tx in tqdm(grupos_tx, desc="Transações"):
                supplier_id = int(s_id) if pd.notna(s_id) else self.id_forn_migracao
                buyer_id = int(org_id)
                addr_id = self.mapa_enderecos_base.get(buyer_id, self.id_fallback_endereco)
                data_tx = pd.to_datetime(data_compra).strftime('%Y-%m-%d %H:%M:%S') if pd.notna(data_compra) else NOW

                # A. Transação (compra)
                result_tx = conn.execute(text("""
                    INSERT INTO transactions (transaction_date, transaction_type_id, supplier_id, buyer_id,
                        doc_type_id, doc_date, purchase_date, created_by, details, amount_total, amount_discount,
                        created_at, updated_at)
                    VALUES (:dt, 1, :sid, :bid, 3, :dt, :dt, 1, 'Migração Inicial', 1, 0, :now, :now)
                """), {"dt": data_tx, "sid": supplier_id, "bid": buyer_id, "now": NOW})
                tx_id = result_tx.lastrowid
                self.stats['transactions'] += 1

                # B. Por product_item (já temos product_item_id por equipamento)
                for _, eq in df_tx.iterrows():
                    pi_id = int(eq['product_item_id'])

                    # Garantia
                    data_gar = eq['data_garantia']
                    if pd.notna(data_gar):
                        data_garantia = pd.to_datetime(data_gar).strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        data_garantia = (pd.to_datetime(data_tx) + pd.DateOffset(years=1)).strftime('%Y-%m-%d %H:%M:%S')

                    # Transaction Item
                    result_ti = conn.execute(text("""
                        INSERT INTO transaction_items (transaction_id, product_id, equipment_id, category_id,
                            condition_id, warranty_date, address_id, location_id, unit_cost, quantity, created_at, updated_at)
                        VALUES (:tx_id, :pid, NULL, 1, 1, :warranty, :addr, NULL, :cost, 1, :now, :now)
                    """), {
                        "tx_id": tx_id, "pid": int(eq['product_id']), "warranty": data_garantia,
                        "addr": addr_id, "cost": float(eq['valor_aquisicao']), "now": NOW
                    })
                    ti_id = result_ti.lastrowid
                    self.stats['transaction_items'] += 1

                    # C. Equipment
                    serial = eq['serial_number'] if pd.notna(eq['serial_number']) else None
                    status_id = int(eq['status_id'])
                    created_eq = pd.to_datetime(eq['created_at_novo']).strftime('%Y-%m-%d %H:%M:%S')
                    updated_eq = pd.to_datetime(eq['updated_at_novo']).strftime('%Y-%m-%d %H:%M:%S')
                    deleted_eq = pd.to_datetime(eq['deleted_at_novo']).strftime('%Y-%m-%d %H:%M:%S') if pd.notna(eq['deleted_at_novo']) else None

                    result_eq = conn.execute(text("""
                        INSERT INTO equipments (product_item_id, transaction_item_id, number, name, serial_number,
                            current_organization_id, status_id, address_id, is_completed, movement_date,
                            created_at, updated_at, deleted_at)
                        VALUES (:pi_id, :ti_id, :num, :name, :serial, :org, :status, :addr, 0, :mov_date, :created, :updated, :deleted)
                    """), {
                        "pi_id": pi_id, "ti_id": ti_id, "num": int(eq['tombo']),
                        "name": str(eq['nome'])[:255], "serial": serial,
                        "org": buyer_id, "status": status_id, "addr": addr_id,
                        "mov_date": data_tx, "created": created_eq, "updated": updated_eq, "deleted": deleted_eq
                    })
                    eq_id = result_eq.lastrowid
                    self.stats['equipments'] += 1

                    # Atualizar transaction_item.equipment_id
                    conn.execute(text("UPDATE transaction_items SET equipment_id = :eid WHERE id = :ti_id"),
                               {"eid": eq_id, "ti_id": ti_id})

                    # D. Equipment History (status inicial)
                    conn.execute(text("""
                        INSERT INTO equipment_history (equipment_id, status_id, occurred_at, is_conversion, reason, user_id, created_at, updated_at)
                        VALUES (:eq_id, :status, :occurred, 0, 'MIGRACAO_LEGADO', 1, :now, :now)
                    """), {
                        "eq_id": eq_id, "status": status_id, "occurred": data_tx, "now": NOW
                    })
                    self.stats['equipment_history'] += 1

    def _imprimir_resumo(self):
        print("\n" + "=" * 70)
        print("📊 RESUMO DA MIGRAÇÃO")
        print("=" * 70)
        for k, v in self.stats.items():
            print(f"   {k:25s}: {v}")
        print("=" * 70)


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    if not os.path.exists(ARQUIVO_CSV):
        print(f"❌ Arquivo não encontrado: {ARQUIVO_CSV}")
        print(f"   Coloque o arquivo na raiz do projeto: {os.getcwd()}")
        sys.exit(1)

    migracao = MigracaoEquipamentosCSV()
    migracao.executar()
