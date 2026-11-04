#!/usr/bin/env python3
"""
Migração completa: dimensões + equipamentos (ordem de dependência)
Origem: controle-interno local (3307) → Destino: refatorado (ajustar URL)

Ordem:
  1. organizations
  2. addresses (organization)
  3. status
  4. suppliers
  5. brands
  6. types
  7. groups
  8. products
  9. product_items
  10. transactions
  11. transaction_items
  12. equipments
  13. equipment_history
"""

import os
import sys
import pandas as pd
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ============================================================
# CONFIG - AJUSTE AQUI
# ============================================================
# Origem (onde estão os dados migrados)
ORIGEM_URL = "mysql+pymysql://root:root@localhost:3307/controle-interno?charset=utf8mb4"

# Destino (refatorado - OUTRO servidor/ambiente)
DESTINO_URL = os.getenv("DESTINO_DATABASE_URL",
    "mysql+pymysql://root:root@DESTINO_HOST:3306/controle_interno?charset=utf8mb4")

# Tombos alvo (sua lista)
TOMBO_ALVO = [
    98492, 98493, 98494, 98495, 98496, 98497, 98498, 98499, 98500,
    106031, 98601, 106032, 106033, 98602, 106034, 106035, 106036,
    106037, 98603, 106038, 106039, 106040, 106041, 98604, 98605,
    98606, 98607, 98608, 106042, 106044, 106045, 106043, 106046,
    106047, 98609, 98610, 106048, 106049, 106050, 106051, 98611,
    98612, 98613, 106052, 106053, 105962, 105963, 105964, 105965,
    105966, 105967, 105970, 105971, 105972, 105968, 105969, 105973,
    105974, 105975, 102001, 105976, 105977, 105978, 102002, 105979,
    105980, 105981, 105984, 105985, 105982, 105983, 105987, 105986,
    105994, 105989, 105995, 105992, 105988, 105991, 105990, 105996,
    105993, 105998, 105997, 105999, 106000, 106001, 106002, 106004,
    106003, 106005, 106008, 106010, 106006, 106007, 106009, 106012,
    106015, 106016, 106013, 106014, 106011, 106017, 106018, 106022,
    106021, 106020, 106019, 106028, 106024, 106026, 106023, 106027,
    106025, 106030, 106029, 104370, 48214, 78868, 77245, 88171,
    91761, 91772, 96575, 91480, 75064, 85151, 96576, 57794,
    96578, 96581, 96577
]

TOMBO_SQL = ','.join(str(t) for t in TOMBO_ALVO)

DIR_EXPORT = "export_migracao"
os.makedirs(DIR_EXPORT, exist_ok=True)


# ============================================================
# HELPERS
# ============================================================
def get_engine(url):
    return create_engine(url, pool_pre_ping=True)


def exportar_tabela(engine, sql, nome, cols_pk=None):
    """Exporta tabela para CSV e retorna DataFrame."""
    print(f"  📤 Exportando {nome}...")
    with engine.connect() as c:
        df = pd.read_sql(text(sql), c)
    path = os.path.join(DIR_EXPORT, f"{nome}.csv")
    df.to_csv(path, index=False, encoding='utf-8-sig')
    print(f"     → {len(df)} linhas → {path}")
    return df


def importar_tabela(engine, df, tabela, cols_pk=None, chunk=500):
    """Importa DataFrame no destino (append, ignora duplicatas por PK)."""
    if df.empty:
        print(f"  ⏭️  {tabela}: vazio, pulando")
        return 0

    # Remove duplicatas por PK se informada
    if cols_pk:
        df = df.drop_duplicates(subset=cols_pk, keep='first')

    print(f"  📥 Importando {tabela} ({len(df)} linhas)...")
    with engine.begin() as c:
        df.to_sql(tabela, con=c, if_exists='append', index=False, chunksize=chunk)
    print(f"     ✅ {len(df)} linhas")
    return len(df)


def get_existing_pks(engine, tabela, pk_col):
    """Retorna set de PKs já existentes no destino."""
    with engine.connect() as c:
        return set(r[0] for r in c.execute(text(f"SELECT `{pk_col}` FROM `{tabela}`")).fetchall())


# ============================================================
# QUERIES DE EXPORTAÇÃO (filtradas pelos equipamentos alvo)
# ============================================================
QUERIES = {
    # 1. Organizations (todas referenciadas)
    "organizations": f"""
        SELECT id, alias, name, cpf_cnpj, phone, email, color, active,
               created_at, updated_at, deleted_at
        FROM organizations
        WHERE id IN (1115, 1122, 1311, 1378)
    """,

    # 2. Addresses (organization)
    "addresses": f"""
        SELECT id, addressable_type, addressable_id, alias, zip, street, number,
               details, district, city, state, country, contact_name,
               contact_phone, contact_email, legacy_customer_id, reserved_customer_id,
               created_at, updated_at, deleted_at
        FROM addresses
        WHERE addressable_type = 'organization'
          AND addressable_id IN (1115, 1122, 1311, 1378)
          AND deleted_at IS NULL
    """,

    # 3. Status (todos)
    "status": """
        SELECT id, name, created_at, updated_at, deleted_at
        FROM status
    """,

    # 4. Suppliers (referenciados nas transações dos equipamentos)
    "suppliers": f"""
        SELECT DISTINCT s.*
        FROM suppliers s
        INNER JOIN transactions tr ON tr.supplier_id = s.id
        INNER JOIN transaction_items ti ON ti.transaction_id = tr.id
        INNER JOIN equipments e ON e.transaction_item_id = ti.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 5. Brands (dos products dos equipamentos)
    "brands": f"""
        SELECT DISTINCT b.*
        FROM brands b
        INNER JOIN products p ON p.brand_id = b.id
        INNER JOIN product_items pi ON pi.product_id = p.id
        INNER JOIN equipments e ON e.product_item_id = pi.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 6. Types
    "types": f"""
        SELECT DISTINCT t.*
        FROM types t
        INNER JOIN products p ON p.type_id = t.id
        INNER JOIN product_items pi ON pi.product_id = p.id
        INNER JOIN equipments e ON e.product_item_id = pi.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 7. Groups
    "groups": f"""
        SELECT DISTINCT g.*
        FROM `groups` g
        INNER JOIN products p ON p.group_id = g.id
        INNER JOIN product_items pi ON pi.product_id = p.id
        INNER JOIN equipments e ON e.product_item_id = pi.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 8. Products
    "products": f"""
        SELECT DISTINCT p.*
        FROM products p
        INNER JOIN product_items pi ON pi.product_id = p.id
        INNER JOIN equipments e ON e.product_item_id = pi.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 9. Product Items
    "product_items": f"""
        SELECT DISTINCT pi.*
        FROM product_items pi
        INNER JOIN equipments e ON e.product_item_id = pi.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 10. Transactions (das transações dos equipamentos)
    "transactions": f"""
        SELECT DISTINCT tr.*
        FROM transactions tr
        INNER JOIN transaction_items ti ON ti.transaction_id = tr.id
        INNER JOIN equipments e ON e.transaction_item_id = ti.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 11. Transaction Items
    "transaction_items": f"""
        SELECT DISTINCT ti.*
        FROM transaction_items ti
        INNER JOIN equipments e ON e.transaction_item_id = ti.id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 12. Equipments (finais)
    "equipments": f"""
        SELECT e.*
        FROM equipments e
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,

    # 13. Equipment History
    "equipment_history": f"""
        SELECT eh.*
        FROM equipment_history eh
        INNER JOIN equipments e ON e.id = eh.equipment_id
        WHERE e.number IN ({TOMBO_SQL}) AND e.serial_required = 1
    """,
}

# Ordem de dependência
ORDEM = [
    "organizations", "addresses", "status", "suppliers",
    "brands", "types", "groups", "products",
    "product_items", "transactions", "transaction_items",
    "equipments", "equipment_history"
]

PKS = {
    "organizations": "id",
    "addresses": "id",
    "status": "id",
    "suppliers": "id",
    "brands": "id",
    "types": "id",
    "groups": "id",
    "products": "id",
    "product_items": "id",
    "transactions": "id",
    "transaction_items": "id",
    "equipments": "id",
    "equipment_history": "id",
}


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("🚀 MIGRAÇÃO DIMENSÕES + EQUIPAMENTOS (Caminho A)")
    print("=" * 60)
    print(f"Origem:  {ORIGEM_URL}")
    print(f"Destino: {DESTINO_URL}")
    print(f"Tombos alvo: {len(TOMBO_ALVO)}")
    print(f"Export dir: {DIR_EXPORT}")
    print()

    eng_origem = get_engine(ORIGEM_URL)
    eng_destino = get_engine(DESTINO_URL)

    # Testa conexões
    try:
        with eng_origem.connect() as c: c.execute(text("SELECT 1"))
        with eng_destino.connect() as c: c.execute(text("SELECT 1"))
        print("✅ Conexões OK")
    except Exception as e:
        print(f"❌ Erro de conexão: {e}")
        sys.exit(1)

    # Verifica PKs existentes no destino
    print("\n🔍 Verificando PKs já existentes no destino...")
    existing = {}
    for tabela in ORDEM:
        pk = PKS.get(tabela)
        if pk:
            existing[tabela] = get_existing_pks(eng_destino, tabela, pk)
            print(f"   {tabela}: {len(existing[tabela])} existentes")

    # EXPORT (origem)
    print("\n📦 EXPORTANDO da origem...")
    dfs = {}
    for tabela in ORDEM:
        sql = QUERIES[tabela]
        df = exportar_tabela(eng_origem, sql, tabela)
        dfs[tabela] = df

    # IMPORT (destino) - na ordem
    print("\n📦 IMPORTANDO no destino...")
    for tabela in ORDEM:
        df = dfs[tabela]
        pk = PKS.get(tabela)

        # Filtra PKs já existentes
        if pk and pk in df.columns and tabela in existing:
            antes = len(df)
            df = df[~df[pk].isin(existing[tabela])]
            if antes != len(df):
                print(f"   ⏭️  {tabela}: {antes - len(df)} já existem (pulados)")

        importar_tabela(eng_destino, df, tabela, cols_pk=pk)

    print("\n" + "=" * 60)
    print("✅ MIGRAÇÃO CONCLUÍDA")
    print("=" * 60)


if __name__ == "__main__":
    main()
