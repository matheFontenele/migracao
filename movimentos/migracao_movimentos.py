import os
import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, text
from datetime import datetime

# ==============================================================================
# CONFIGURAÇÕES DE MAPEAMENTO E BLOQUEIO DE ORGANIZAÇÕES
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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

# ==============================================================================
# ENGINES (compartilhados entre todos os módulos de movimento)
# ==============================================================================
engine_new = create_engine(
    f"mysql+pymysql://{DB_CONFIG_NEW['user']}:{DB_CONFIG_NEW['pass']}@{DB_CONFIG_NEW['host']}:{DB_CONFIG_NEW['port']}/{DB_CONFIG_NEW['db']}"
)
engine_legado = create_engine(
    f"mysql+pymysql://{DB_CONFIG_LEGADO['user']}:{DB_CONFIG_LEGADO['pass']}@{DB_CONFIG_LEGADO['host']}:{DB_CONFIG_LEGADO['port']}/{DB_CONFIG_LEGADO['db']}"
)

# ==============================================================================
# FUNÇÕES AUXILIARES GENÉRICAS (usadas por todos os tipos de movimento)
# ==============================================================================
def limpar_codigo(val):
    """Remove .0 de strings numéricas e trata nulos."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    return s[:-2] if s.endswith(".0") else s


def normalizar_texto(val):
    """Padroniza string para comparação: remove espaços extras, upper, trata nulos."""
    if pd.isna(val):
        return ""
    return str(val).strip().upper()


def descobrir_id_organizacao_destino(id_legado):
    """Mapeia o ID do legado para o ID correspondente no banco novo."""
    if pd.isna(id_legado):
        return 1115
    id_legado_int = int(id_legado)
    if id_legado_int in MAPPING_ALUCOM:
        return 1115
    elif id_legado_int in MAPPING_IP:
        return 1311
    elif id_legado_int in MAPPING_MOREIA:
        return 1122
    elif id_legado_int in MAPPING_AS:
        return 1378
    return id_legado_int


def limpar_tabelas_refatoradas(engine):
    """Limpa as tabelas de movimentos antes de cada execução."""
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
            print("✅ Tabelas limpas com sucesso.")
        except Exception as e:
            trans.rollback()
            print(f"❌ Erro crítico ao limpar o banco refatorado: {e}")
            raise e


def resetar_saldo_contract_items(engine):
    """Reseta available_quantity para o valor original antes de cada execução de teste."""
    print("🔄 Resetando available_quantity de contract_items para o valor original...")
    with engine.begin() as conn:
        conn.execute(text("UPDATE contract_items SET available_quantity = quantity"))
    print("✅ Saldos de contract_items resetados.")


# ==============================================================================
# DADOS COMPARTILHADOS (carregados uma vez, usados por todos os módulos)
# ==============================================================================
def carregar_dados_compartilhados():
    print("📖 Carregando dados compartilhados do legado e do banco novo...")

    with engine_legado.connect() as conn:
        df_equipamentos_legado = pd.read_sql(
            "SELECT id, numero, situacao_id, updated_at FROM aluguel_equipamentos", conn
        )
        df_clientes_legado = pd.read_sql("SELECT id FROM aluguel_clientes", conn)
        df_movimentos_legado = pd.read_sql("""
            SELECT id, data, tipo_id, cliente_id, usuario_id, updated_at, deleted_at 
            FROM aluguel_movimento 
            WHERE deleted_at IS NULL
        """, conn)
        df_movimentos_legado['cliente_id'] = df_movimentos_legado['cliente_id'].fillna(0).astype(int)
        df_movimentos_legado['usuario_id'] = df_movimentos_legado['usuario_id'].fillna(0).astype(int)
        df_movimento_item_legado = pd.read_sql(
            "SELECT id, movimento_id, equipamento_id FROM aluguel_movimento_itens", conn
        )

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

        df_equipamentos_refatorado = pd.read_sql(
            "SELECT id, number, name, current_organization_id FROM equipments", conn
        )
        df_contratos_refatorado = pd.read_sql(
            "SELECT id, name, organization_id, customer_id FROM contracts", conn
        )
        df_enderecos_valido = pd.read_sql(
            "SELECT id, addressable_id, legacy_customer_id FROM addresses WHERE legacy_customer_id IS NOT NULL", conn
        )

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

        query_tipo_equipamentos = text("""
            SELECT e.id AS equipment_id, p.type_id 
            FROM equipments e
            JOIN product_items pi ON e.product_item_id = pi.id
            JOIN products p ON pi.product_id = p.id
            WHERE p.type_id IS NOT NULL
        """)
        result_tipos = conn.execute(query_tipo_equipamentos).fetchall()
        dict_tipo_por_equipamento = {row.equipment_id: row.type_id for row in result_tipos}

    # ----------------------------------------------------------------------
    # Construção dos dicionários de lookup O(1)
    # ----------------------------------------------------------------------
    dict_tombo_por_equip_id = {
        row['id']: limpar_codigo(row['numero'])
        for _, row in df_equipamentos_legado.iterrows()
        if pd.notna(row['numero'])
    }
    dict_equip_ref_por_number = {
        limpar_codigo(row['number']): row['id'] for _, row in df_equipamentos_refatorado.iterrows()
    }
    dict_movimentos_legado = {row['id']: row for _, row in df_movimentos_legado.iterrows()}
    dict_cliente_adress = dict(zip(
        df_enderecos_valido['legacy_customer_id'].astype(int),
        df_enderecos_valido['addressable_id'].astype(int)
    ))
    dict_endereco_por_legacy_client = dict(zip(
        df_enderecos_valido['legacy_customer_id'].astype(int),
        df_enderecos_valido['id'].astype(int)
    ))
    dict_primeiro_item_por_cliente = dict(zip(
        df_primeiro_item['customer_id'].astype(int),
        df_primeiro_item['contract_item_id'].astype(int)
    ))
    dict_primeiro_contrato_por_cliente = dict(zip(
        df_primeiro_item['customer_id'].astype(int),
        df_primeiro_item['contract_id'].astype(int)
    ))

    # Dicionário de contrato_item com chave composta (cliente, contrato, item, descrição)
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
            'original_quantity': int(row['available_quantity']) if pd.notna(row['available_quantity']) else 0
        }
        for _, row in df_contratos_itens.iterrows()
        if pd.notna(row['alias_item_contract']) and pd.notna(row['cliente_id'])
    }

    saldos_por_id = {}
    for dados in dict_contrato_item_por_chave.values():
        item_id = dados['id']
        if item_id not in saldos_por_id:
            saldos_por_id[item_id] = dados['available_quantity']

    print(f"   ✅ {len(dict_contrato_item_por_chave)} combinações (cliente, contrato, item, descrição) indexadas.")

    return {
        "dict_tombo_por_equip_id": dict_tombo_por_equip_id,
        "dict_equip_ref_por_number": dict_equip_ref_por_number,
        "dict_movimentos_legado": dict_movimentos_legado,
        "dict_cliente_adress": dict_cliente_adress,
        "dict_endereco_por_legacy_client": dict_endereco_por_legacy_client,
        "dict_primeiro_item_por_cliente": dict_primeiro_item_por_cliente,
        "dict_primeiro_contrato_por_cliente": dict_primeiro_contrato_por_cliente,
        "dict_contrato_item_por_chave": dict_contrato_item_por_chave,
        "dict_tipo_por_equipamento": dict_tipo_por_equipamento,
        "saldos_por_id": saldos_por_id,
        "df_movimento_item_legado": df_movimento_item_legado,
    }


def buscar_ultimo_movimento_por_tombo(lista_tombos):
    """
    Recebe uma lista de tombos e retorna um dict com o último movimento
    registrado no legado para cada um deles.
    """
    lista_tombos_sql = "(" + ", ".join(map(str, lista_tombos)) + ")"
    query = f"""
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
    df_resultado = pd.read_sql(query, engine_legado)

    dict_ultimo_movimento = {}
    for _, row in df_resultado.iterrows():
        tombo_chave = limpar_codigo(row['tombo'])
        if tombo_chave and tombo_chave != 'nan':
            dict_ultimo_movimento[tombo_chave] = {
                'movimento': row.to_dict(),
                'data_dt': pd.to_datetime(row['updated_at'] if pd.notna(row['updated_at']) else row['data'])
            }
    return dict_ultimo_movimento


# ==============================================================================
# ORQUESTRADOR PRINCIPAL
# ==============================================================================
def main():
    print("=" * 70)
    print("🚀 INICIANDO PIPELINE DE MIGRAÇÃO DE MOVIMENTOS")
    print("=" * 70)

    # Reset e limpeza antes de qualquer migração
    resetar_saldo_contract_items(engine_new)
    limpar_tabelas_refatoradas(engine_new)

    # Carrega dados compartilhados uma única vez
    dados_compartilhados = carregar_dados_compartilhados()

    # ----------------------------------------------------------------------
    # Importa e executa cada módulo de movimento (lazy import para evitar
    # acoplamento circular e permitir rodar só o que for necessário)
    # ----------------------------------------------------------------------
    from migracao_aluguel import processar_aluguel
    processar_aluguel(engine_new, dados_compartilhados)

    # Futuro: descomente conforme os módulos forem implementados
    # from migracao_devolucao import processar_devolucao
    # processar_devolucao(engine_new, dados_compartilhados)

    # from migracao_substituicao import processar_substituicao
    # processar_substituicao(engine_new, dados_compartilhados)

    # from migracao_reserva import processar_reserva
    # processar_reserva(engine_new, dados_compartilhados)

    print("\n" + "=" * 70)
    print("🎉 PIPELINE DE MIGRAÇÃO CONCLUÍDO")
    print("=" * 70)


if __name__ == "__main__":
    main()