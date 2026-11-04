#!/usr/bin/env python3
"""
Exporta equipamentos DISPONÍVEL PARA LOCAÇÃO (status_id=1) da lista de tombos
e opcionalmente importa no banco destino.

Uso:
    # Só exportar CSV
    python exportar_equipamentos_disponiveis.py --export

    # Exportar E importar no banco destino (precisa configurar DESTINO_DATABASE_URL)
    python exportar_equipamentos_disponiveis.py --export --import

    # Só importar CSV já existente
    python exportar_equipamentos_disponiveis.py --import --csv equipamentos_disponiveis_para_locacao.csv
"""

import argparse
import os
import sys
import pandas as pd
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ============================================================
# CONFIGURAÇÃO - AJUSTE AQUI
# ============================================================
# Banco ORIGEM (onde estão os equipamentos migrados = banco refatorado local)
ORIGEM_DATABASE_URL = "mysql+pymysql://root:root@localhost:3307/controle-interno?charset=utf8mb4"

# Banco DESTINO (refatorado - outro ambiente/staging/produção)
# Ex: mysql+pymysql://user:senha@host:port/db
DESTINO_DATABASE_URL = os.getenv("DESTINO_DATABASE_URL", "mysql+pymysql://root:root@localhost:3306/controle_interno?charset=utf8mb4")

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

CSV_SAIDA = "equipamentos_disponiveis_para_locacao.csv"


# ============================================================
# FUNÇÕES
# ============================================================
def get_engine(url: str):
    return create_engine(url, pool_pre_ping=True)


def exportar(engine_origem) -> pd.DataFrame:
    """Busca equipamentos status=1, deduplica por tombo, retorna DataFrame."""
    ph = ','.join(str(t) for t in TOMBO_ALVO)

    sql = f"""
        WITH ranked AS (
            SELECT e.*,
                   ROW_NUMBER() OVER (PARTITION BY e.number ORDER BY e.updated_at DESC, e.id DESC) as rn
            FROM equipments e
            WHERE e.number IN ({ph}) AND e.status_id = 1
        )
        SELECT
            e.id as equipment_id,
            e.number as tombo,
            e.name as equipamento_nome,
            e.serial_number as numero_serie,
            e.serial_required as serial_obrigatorio,
            e.status_id,
            e.current_organization_id as organizacao_id,
            e.address_id as endereco_id,
            e.product_item_id,
            e.transaction_item_id,
            e.movement_date,
            e.created_at as equip_created_at,
            e.updated_at as equip_updated_at,
            e.deleted_at as equip_deleted_at,
            e.is_completed as equip_completed,
            e.last_movement_item_customer_id as equip_ultimo_cliente_movimento,
            s.name as status_nome,
            o.name as organizacao_nome,
            o.alias as organizacao_alias,
            o.cpf_cnpj as organizacao_cnpj,
            pi.code as product_item_codigo,
            p.name as product_nome,
            b.name as brand_nome,
            t.name as type_nome,
            g.name as group_nome
        FROM ranked e
        JOIN status s ON s.id = e.status_id
        JOIN organizations o ON o.id = e.current_organization_id
        JOIN product_items pi ON pi.id = e.product_item_id
        JOIN products p ON p.id = pi.product_id
        LEFT JOIN brands b ON b.id = p.brand_id
        LEFT JOIN types t ON t.id = p.type_id
        LEFT JOIN `groups` g ON g.id = p.group_id
        WHERE e.rn = 1
        ORDER BY e.number
    """

    with engine_origem.connect() as conn:
        df = pd.read_sql(text(sql), conn)

    print(f"📊 Encontrados: {len(df)} equipamentos (status=1, deduplicados)")
    return df


def salvar_csv(df: pd.DataFrame, caminho: str):
    df.to_csv(caminho, index=False, encoding='utf-8-sig')
    print(f"✅ CSV salvo: {caminho} ({len(df)} linhas)")


def importar(engine_destino, csv_path: str):
    """Importa CSV no banco destino, ignorando tombos já existentes."""
    if not os.path.exists(csv_path):
        print(f"❌ Arquivo não encontrado: {csv_path}")
        return

    df = pd.read_csv(csv_path)
    print(f"📥 Lendo CSV: {len(df)} linhas")

    # Verifica tombos já existentes no destino
    with engine_destino.connect() as conn:
        existentes = set()
        if df['tombo'].notna().any():
            tombos_csv = tuple(int(t) for t in df['tombo'].dropna().unique())
            if len(tombos_csv) == 1:
                q = f"SELECT number FROM equipments WHERE number = {tombos_csv[0]}"
            else:
                q = f"SELECT number FROM equipments WHERE number IN {tombos_csv}"
            result = conn.execute(text(q))
            existentes = {r[0] for r in result}

    print(f"🔍 Tombos já no destino: {len(existentes)}")

    # Filtra só os novos
    df_novos = df[~df['tombo'].isin(existentes)].copy()
    print(f"➕ Novos para inserir: {len(df_novos)}")

    if df_novos.empty:
        print("✅ Nada a importar (todos já existem)")
        return

    # Prepara colunas para equipments
    cols_map = {
        'product_item_id': 'product_item_id',
        'transaction_item_id': 'transaction_item_id',
        'tombo': 'number',
        'equipamento_nome': 'name',
        'numero_serie': 'serial_number',
        'serial_obrigatorio': 'serial_required',
        'organizacao_id': 'current_organization_id',
        'status_id': 'status_id',
        'endereco_id': 'address_id',
        'equip_completed': 'is_completed',
        'equip_movement_date': 'movement_date',
        'equip_created_at': 'created_at',
        'equip_updated_at': 'updated_at',
        'equip_deleted_at': 'deleted_at',
    }

    df_insert = df_novos[list(cols_map.keys())].rename(columns=cols_map)
    df_insert['is_completed'] = df_insert['is_completed'].fillna(0).astype(int)

    # Insere em lote
    with engine_destino.begin() as conn:
        df_insert.to_sql('equipments', con=conn, if_exists='append', index=False, chunksize=500)

    print(f"✅ {len(df_insert)} equipamentos importados no destino")


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Exporta/Importa equipamentos DISPONÍVEL PARA LOCAÇÃO")
    parser.add_argument('--export', action='store_true', help='Exportar CSV do banco origem')
    parser.add_argument('--import', dest='do_import', action='store_true', help='Importar CSV no banco destino')
    parser.add_argument('--csv', default=CSV_SAIDA, help=f'Arquivo CSV (padrão: {CSV_SAIDA})')
    parser.add_argument('--origem-url', default=ORIGEM_DATABASE_URL, help='URL banco origem')
    parser.add_argument('--destino-url', default=DESTINO_DATABASE_URL, help='URL banco destino')

    args = parser.parse_args()

    if not args.export and not args.do_import:
        parser.print_help()
        sys.exit(1)

    # Export
    if args.export:
        print(f"🔌 Conectando no banco origem: {args.origem_url}")
        eng_origem = get_engine(args.origem_url)
        df = exportar(eng_origem)
        salvar_csv(df, args.csv)

    # Import
    if args.do_import:
        print(f"🔌 Conectando no banco destino: {args.destino_url}")
        eng_destino = get_engine(args.destino_url)
        importar(eng_destino, args.csv)


if __name__ == "__main__":
    main()
