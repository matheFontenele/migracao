import os
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

# ==============================================================================
# EXTRAÇÕES SQL
# ==============================================================================

def extrair_frente_1_clientes_reservados(engine_legado):
    print("📖 Extraindo Frente 1: Clientes Reservados (Tipos 1 e 7)...")
    query = """
        SELECT
            eq.id AS ID_EQUIPAMENTO, eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
            ac.id AS ID_CLIENTE, ac.nome_razao_social AS CLIENTE,
            mov.id as MOVIMENTO_ID, mov.tipo_id AS TIPO_MOVIMENTO, mov.usuario_id,
            mov.data AS DATA_ALUGUEL, mov.created_at, mov.updated_at, mov.deleted_at
        FROM aluguel_equipamentos eq
        INNER JOIN (
            SELECT mi.equipamento_id, MAX(m.id) as ultimo_movimento_id
            FROM aluguel_movimento_itens mi
            INNER JOIN aluguel_movimento m ON m.id = mi.movimento_id
            WHERE m.deleted_at IS NULL
            GROUP BY mi.equipamento_id
        ) ult_mov ON ult_mov.equipamento_id = eq.id
        INNER JOIN aluguel_movimento mov ON mov.id = ult_mov.ultimo_movimento_id
        LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
        WHERE eq.deleted_at IS NULL AND ac.deleted_at IS NULL AND eq.situacao_id IN (1, 15)
          AND ac.nome_razao_social LIKE '%RESERV%' AND ac.id != 10487
          AND mov.tipo_id IN (1, 7)
    """
    with engine_legado.connect() as conn:
        return pd.read_sql(text(query), conn)

def extrair_frente_2_movimentos_gerais(engine_legado):
    """
    FRENTE 2: Clientes normais (Sem Like '%RESERV%' ou Falso Positivo).
    Puxa APENAS movimentos do tipo 7. O Match no Python será pelo legacy_customer_id.
    """
    print("📖 Extraindo Frente 2: Movimentos Gerais de Reserva (Apenas Tipo 7)...")
    query = """
        SELECT
            eq.id AS ID_EQUIPAMENTO, eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
            ac.id AS ID_CLIENTE, ac.nome_razao_social AS CLIENTE,
            mov.id as MOVIMENTO_ID, mov.tipo_id AS TIPO_MOVIMENTO, mov.usuario_id,
            mov.data AS DATA_ALUGUEL, mov.created_at, mov.updated_at, mov.deleted_at
        FROM aluguel_equipamentos eq
        INNER JOIN (
            SELECT mi.equipamento_id, MAX(m.id) as ultimo_movimento_id
            FROM aluguel_movimento_itens mi
            INNER JOIN aluguel_movimento m ON m.id = mi.movimento_id
            WHERE m.deleted_at IS NULL
            GROUP BY mi.equipamento_id
        ) ult_mov ON ult_mov.equipamento_id = eq.id
        INNER JOIN aluguel_movimento mov ON mov.id = ult_mov.ultimo_movimento_id
        LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
        WHERE eq.deleted_at IS NULL AND ac.deleted_at IS NULL AND eq.situacao_id IN (1, 15)
          AND (ac.nome_razao_social NOT LIKE '%RESERV%' OR ac.id = 10487)
          AND mov.tipo_id = 7
    """
    with engine_legado.connect() as conn:
        return pd.read_sql(text(query), conn)

# ==============================================================================
# PROCESSAMENTO PRINCIPAL
# ==============================================================================

def processar_reservas(engine_new, engine_legado, dados_compartilhados):
    print("\n" + "=" * 70)
    print("📦 MÓDULO: ALOCAÇÃO DE RESERVAS (ESTOQUE E CLIENTES)")
    print("=" * 70)

    # Desempacota dicionários tradicionais do legacy_customer_id
    dict_equip_ref_por_number          = dados_compartilhados["dict_equip_ref_por_number"]
    dict_cliente_adress                = dados_compartilhados["dict_cliente_adress"]
    dict_endereco_por_legacy_client    = dados_compartilhados["dict_endereco_por_legacy_client"]
    dict_primeiro_item_por_cliente     = dados_compartilhados["dict_primeiro_item_por_cliente"]
    dict_primeiro_contrato_por_cliente = dados_compartilhados["dict_primeiro_contrato_por_cliente"]

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 1. Busca os Dicionários da Frente 1 (Match pelo reserved_customer_id) direto no Banco Novo
    dict_recipient_por_reserved = {}
    dict_endereco_por_reserved = {}
    with engine_new.connect() as conn:
        print("🔍 Mapeando dicionários de reserved_customer_id...")
        res = conn.execute(text("""
            SELECT addressable_id, id, reserved_customer_id 
            FROM addresses 
            WHERE addressable_type = 'customer' AND reserved_customer_id IS NOT NULL
        """))
        for r in res.mappings():
            res_id = int(r['reserved_customer_id'])
            dict_recipient_por_reserved[res_id] = int(r['addressable_id'])
            dict_endereco_por_reserved[res_id] = int(r['id'])

    # 2. Carrega as duas extrações
    df_frente1 = extrair_frente_1_clientes_reservados(engine_legado)
    df_frente2 = extrair_frente_2_movimentos_gerais(engine_legado)

    if df_frente1.empty and df_frente2.empty:
        print("Nenhum movimento de reserva encontrado nas duas frentes.")
        return

    # 3. Estruturas de saída
    servicos_mestre = []
    service_itens_mestre = []
    movimentos_mestre = []
    movimento_itens_mestre = []
    
    pedidos_pai_inseridos = set()
    equipamentos_alterados = set()
    so_item_id_counter = 500000 
    MOVEMENT_TYPE_RESERVA = 4

    # ----------------------------------------------------------------------
    # FUNÇÃO INTERNA DE PROCESSAMENTO DA LINHA (Serve para ambas as frentes)
    # ----------------------------------------------------------------------
    def processar_linha(row, frente):
        nonlocal so_item_id_counter
        
        id_final = int(row['MOVIMENTO_ID'])
        tombo = str(row['TOMBO']).strip()
        cliente_id_legado = int(row['ID_CLIENTE'])
        name_item_final = row['NOME_EQUIPAMENTO']
        
        equipment_id_ref = dict_equip_ref_por_number.get(tombo)
        if not equipment_id_ref: return

        if frente == 1:
            recipient_id = dict_recipient_por_reserved.get(cliente_id_legado)
            cliente_final = dict_endereco_por_reserved.get(cliente_id_legado)
        else:
            recipient_id = dict_cliente_adress.get(cliente_id_legado)
            cliente_final = dict_endereco_por_legacy_client.get(cliente_id_legado)

        if not recipient_id: return

        usuario_id = int(row['usuario_id']) if pd.notna(row['usuario_id']) and row['usuario_id'] != 0 else 1
        mov_date = row['updated_at'] if pd.notna(row['updated_at']) else now
        deleted_at_mov = row['deleted_at'] if pd.notna(row['deleted_at']) else None

        contrato_id_final = dict_primeiro_contrato_por_cliente.get(recipient_id)
        contrato_item_id = dict_primeiro_item_por_cliente.get(recipient_id)

        if id_final not in pedidos_pai_inseridos:
            servicos_mestre.append({
                "id": id_final,
                 "status_id": 3,
                 "movement_type_id": MOVEMENT_TYPE_RESERVA, 
                "contract_id": contrato_id_final,
                "user_id": usuario_id,
                "destination_order_id": None, 
                "mode_transport_id": 1,
                "organization_id": 1378,
                "recipient_customer_id": recipient_id, 
                "deadline": now,
                "details": f"Migração - Reserva (Frente {frente})", 
                "created_at": mov_date,
                "updated_at": mov_date,
                "deleted_at": deleted_at_mov
            })
            movimentos_mestre.append({
                "id": id_final,
                "number": id_final,
                "movement_date": mov_date,
                "service_order_id": id_final,
                "recipient_customer_id": recipient_id,
                "migrate_customer_id": None,
                "organization_id": 1378,
                "status_id": 3,
                "created_by": usuario_id,
                "details": f"Migração - Reserva (Frente {frente})",
                "created_at": mov_date,
                "updated_at": mov_date,
                "deleted_at": deleted_at_mov
            })
            pedidos_pai_inseridos.add(id_final)

        item_servico_id_atual = so_item_id_counter
        so_item_id_counter += 1

        service_itens_mestre.append({
            "id": item_servico_id_atual, "status_id": 3, "service_order_id": id_final,
            "department_id": 2, "movement_type_id": MOVEMENT_TYPE_RESERVA, "contract_item_id": contrato_item_id,
            "alias": None, "equipment_id": equipment_id_ref, "type_id": None, "product_id": None,
            "is_exchange": 0, "is_extra": 0, "quantity_product": None,
            "fulfilled_quantity_product": 0, "quantity": 1,
            "details": f"Alocação de Reserva (Frente {frente})",
            "address_id": cliente_final, "location_id": None,
            "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
        })

        movimento_itens_mestre.append({
            "movement_id": id_final, "movement_type_id": MOVEMENT_TYPE_RESERVA, "service_order_item_id": item_servico_id_atual,
            "equipment_id": equipment_id_ref, "extra_id": None, "status_id": 3,
            "product_item_id": None, "alias": name_item_final,
            "old_organization_id": None, "new_organization_id": None, "operation_type": 'RESERVA',
            "confirmed_at": None, "confirmed_by": None,
            "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
        })

        equipamentos_alterados.add(equipment_id_ref)

    # 4. Executa os loops
    for _, row in tqdm(df_frente1.iterrows(), total=df_frente1.shape[0], desc="Executando FRENTE 1"):
        processar_linha(row, frente=1)
        
    for _, row in tqdm(df_frente2.iterrows(), total=df_frente2.shape[0], desc="Executando FRENTE 2"):
        processar_linha(row, frente=2)

    # ======================================================================
    # SALVAMENTO NO BANCO NOVO
    # ======================================================================
    print("\n🚀 Persistindo dados de RESERVA no banco...")
    with engine_new.connect() as conn:
        trans = conn.begin()
        try:
            if servicos_mestre:
                pd.DataFrame(servicos_mestre).to_sql("service_orders", con=conn, if_exists="append", index=False)
            if service_itens_mestre:
                pd.DataFrame(service_itens_mestre).to_sql("service_order_items", con=conn, if_exists="append", index=False)
            if movimentos_mestre:
                pd.DataFrame(movimentos_mestre).to_sql("movements", con=conn, if_exists="append", index=False)
            if movimento_itens_mestre:
                pd.DataFrame(movimento_itens_mestre).to_sql("movement_items", con=conn, if_exists="append", index=False)

            if equipamentos_alterados:
                lista_equip_ids = list(equipamentos_alterados)
                for i in range(0, len(lista_equip_ids), 500):
                    bloco = lista_equip_ids[i:i + 500]
                    conn.execute(text("UPDATE equipments SET status_id = 3 WHERE id IN :ids"), {"ids": tuple(bloco)})

            trans.commit()
            print("🎉 MÓDULO RESERVA concluído com sucesso!")

        except Exception as e:
            trans.rollback()
            print(f"❌ Erro crítico no módulo RESERVA: {e}")
            raise e

    print("\n--- 🏁 Resumo RESERVAS ---")
    print(f"📦 Extração Frente 1 (Clientes Reserva): {len(df_frente1)}")
    print(f"📦 Extração Frente 2 (Movimentos Tipo 7): {len(df_frente2)}")
    print(f"📦 Serviços/Movimentos Pais Criados: {len(servicos_mestre)}")
    print(f"📦 Itens Criados: {len(service_itens_mestre)}")