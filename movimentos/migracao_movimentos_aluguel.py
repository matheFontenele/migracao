import os
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_IMPORTACAO = os.path.join(BASE_DIR, "docs", "equipeAS.csv")


def processar_aluguel(engine_new, dados_compartilhados):
    from migracao_movimentos import (
        limpar_codigo, normalizar_texto, buscar_ultimo_movimento_por_tombo, engine_legado
    )

    print("\n" + "-" * 70)
    print("📦 MÓDULO: ALUGUEL")
    print("-" * 70)

    # Desempacota os dados compartilhados
    dict_equip_ref_por_number       = dados_compartilhados["dict_equip_ref_por_number"]
    dict_cliente_adress             = dados_compartilhados["dict_cliente_adress"]
    dict_endereco_por_legacy_client = dados_compartilhados["dict_endereco_por_legacy_client"]
    dict_primeiro_item_por_cliente  = dados_compartilhados["dict_primeiro_item_por_cliente"]
    dict_primeiro_contrato_por_cliente = dados_compartilhados["dict_primeiro_contrato_por_cliente"]
    dict_contrato_item_por_chave    = dados_compartilhados["dict_contrato_item_por_chave"]
    dict_tipo_por_equipamento       = dados_compartilhados["dict_tipo_por_equipamento"]
    saldos_por_id                   = dados_compartilhados["saldos_por_id"]

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # --------------------------------------------------------------------
    # Carrega e limpa a planilha auxiliar específica de aluguel
    # --------------------------------------------------------------------
    print("📖 Carregando planilha auxiliar de aluguel...")
    df_planilha_auxiliar = pd.read_csv(
        ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False
    )
    df_planilha_auxiliar['TOMBO'] = pd.to_numeric(df_planilha_auxiliar['TOMBO'], errors='coerce')
    df_planilha_auxiliar['CLIENTE_ID'] = pd.to_numeric(df_planilha_auxiliar['CLIENTE_ID'], errors='coerce')
    df_planilha_auxiliar = df_planilha_auxiliar.dropna(subset=['TOMBO'])
    df_planilha_auxiliar['TOMBO'] = df_planilha_auxiliar['TOMBO'].astype(int).astype(str)
    df_planilha_auxiliar['CLIENTE_ID'] = df_planilha_auxiliar['CLIENTE_ID'].astype(int).astype(str)
    df_planilha_auxiliar['ITEM_DO_CONTRATO'] = (
        df_planilha_auxiliar['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    )
    df_planilha_auxiliar = df_planilha_auxiliar[
        df_planilha_auxiliar['ITEM_DO_CONTRATO'].str.lower() != 'nan'
    ]

    # --------------------------------------------------------------------
    # Busca o último movimento (tipo 1 - Alugado) só para os tombos desta planilha
    # --------------------------------------------------------------------
    tombos_unicos = df_planilha_auxiliar['TOMBO'].astype(int).unique().tolist()
    dict_ultimo_movimento_por_tombo = buscar_ultimo_movimento_por_tombo(tombos_unicos)
    print(f"   ✅ {len(dict_ultimo_movimento_por_tombo)} tombos indexados com último movimento.")

    # --------------------------------------------------------------------
    # Estruturas de saída
    # --------------------------------------------------------------------
    servicos_mestre = []
    service_itens_mestre = []
    movimentos_mestre = []
    movimento_itens_mestre = []
    service_order_item_extra_equipments = []
    pedidos_pai_inseridos = set()
    equipamentos_alterados = set()
    extra_id_counter = 1
    so_item_id_counter = 1

    # ======================================================================
    # LOOP PRINCIPAL
    # ======================================================================
    for index_csv, row_csv in tqdm(
        df_planilha_auxiliar.iterrows(), total=df_planilha_auxiliar.shape[0], desc="Processando ALUGUEL"
    ):
        contrato_item_equip = str(row_csv['TOMBO']).strip()
        if contrato_item_equip in ['nan', ''] or pd.isna(row_csv['TOMBO']):
            continue

        contrato_item = str(row_csv['ITEM_DO_CONTRATO']).strip()
        if contrato_item.lower() == 'nan':
            continue

        nome_contrato_csv = normalizar_texto(row_csv.get('CONTRATO'))
        descricao_item_csv = normalizar_texto(row_csv.get('DESCRICAO_ITEM'))
        cliente_legado_csv = row_csv['CLIENTE_ID']
        name_item_final = row_csv['EQUIPAMENTO_NOME']
        ultimo_mov_info = dict_ultimo_movimento_por_tombo.get(contrato_item_equip)

        if ultimo_mov_info is None:
            continue

        row_mov = ultimo_mov_info['movimento']

        # Filtro de tipos válidos para ALUGUEL (Alugado=1, Substituição Alugado=5)
        if row_mov['tipo_id'] not in {1, 5}:
            continue

        id_final = row_mov['id']
        cliente_id_legado = row_mov['cliente_id']
        recipient_id = dict_cliente_adress.get(int(cliente_id_legado)) if pd.notna(cliente_id_legado) else None

        if not recipient_id:
            continue

        usuario_id = row_mov['usuario_id']
        if pd.isna(usuario_id) or usuario_id == 0:
            usuario_id = 1
        else:
            usuario_id = int(usuario_id)

        mov_date = row_mov['updated_at'] if pd.notna(row_mov['updated_at']) else now
        deleted_at_mov = row_mov['deleted_at'] if pd.notna(row_mov.get('deleted_at')) else None
        equipment_id_ref = dict_equip_ref_por_number.get(contrato_item_equip)
        cliente_final = dict_endereco_por_legacy_client.get(int(cliente_legado_csv)) if pd.notna(cliente_legado_csv) else None

        # ------------------------------------------------------------------
        # Match do contrato (chave composta)
        # ------------------------------------------------------------------
        item_contrato_info = None
        contrato_id_final = None
        if contrato_item and recipient_id:
            chave_busca = (int(recipient_id), nome_contrato_csv, contrato_item.upper(), descricao_item_csv)
            item_contrato_info = dict_contrato_item_por_chave.get(chave_busca)

        if item_contrato_info:
            contrato_id_final = item_contrato_info['contract_id']
        else:
            contrato_id_final = dict_primeiro_contrato_por_cliente.get(int(recipient_id))

        # 1️⃣ Service Order + Movement (pai)
        if id_final not in pedidos_pai_inseridos:
            servicos_mestre.append({
                "id": id_final, "status_id": 3, "movement_type_id": 1, "contract_id": contrato_id_final,
                "user_id": usuario_id, "destination_order_id": None, "mode_transport_id": 1,
                "organization_id": 1378, "recipient_customer_id": recipient_id, "deadline": now,
                "details": "Migração", "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
            })
            movimentos_mestre.append({
                "id": id_final, "number": id_final, "movement_date": mov_date, "service_order_id": id_final,
                "recipient_customer_id": recipient_id, "migrate_customer_id": None, "organization_id": 1378,
                "status_id": 3, "created_by": usuario_id, "details": "Migração",
                "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
            })
            pedidos_pai_inseridos.add(id_final)

        # ------------------------------------------------------------------
        # Controle de saldo / item extra
        # ------------------------------------------------------------------
        extra_id_atual = None
        is_extra_flag = 0
        item_servico_id_atual = so_item_id_counter
        so_item_id_counter += 1
        contrato_item_id = None

        if item_contrato_info:
            contrato_item_id = item_contrato_info['id']
            if saldos_por_id[contrato_item_id] > 0:
                saldos_por_id[contrato_item_id] -= 1
            else:
                is_extra_flag = 1
                extra_id_atual = extra_id_counter
                service_order_item_extra_equipments.append({
                    "id": extra_id_atual, "service_order_item_id": item_servico_id_atual,
                    "contract_item_id": contrato_item_id,
                    "type_id": dict_tipo_por_equipamento.get(equipment_id_ref),
                    "quantity": 1, "removed_quantity": 0,
                    "created_at": mov_date, "updated_at": mov_date, "deleted_at": None
                })
                extra_id_counter += 1
        else:
            fallback_contract_item_id = dict_primeiro_item_por_cliente.get(int(recipient_id))
            if fallback_contract_item_id:
                is_extra_flag = 1
                contrato_item_id = int(fallback_contract_item_id)
                extra_id_atual = extra_id_counter
                service_order_item_extra_equipments.append({
                    "id": extra_id_atual, "service_order_item_id": item_servico_id_atual,
                    "contract_item_id": contrato_item_id,
                    "type_id": dict_tipo_por_equipamento.get(equipment_id_ref),
                    "quantity": 1, "removed_quantity": 0,
                    "created_at": mov_date, "updated_at": mov_date, "deleted_at": None
                })
                extra_id_counter += 1
            else:
                print(f"⚠️ [Aviso] Cliente ID {recipient_id} sem contratos ativos no sistema.")

        if equipment_id_ref:
            equipamentos_alterados.add(equipment_id_ref)

        # 2️⃣ Service Order Item
        service_itens_mestre.append({
            "id": item_servico_id_atual, "status_id": 3, "service_order_id": id_final,
            "department_id": 2, "movement_type_id": 1, "contract_item_id": contrato_item_id,
            "alias": None if contrato_item == '' else contrato_item,
            "equipment_id": equipment_id_ref, "type_id": None, "product_id": None,
            "is_exchange": 0, "is_extra": is_extra_flag, "quantity_product": None,
            "fulfilled_quantity_product": 0, "quantity": 1,
            "details": None if item_contrato_info else "Item Extra (Sem Match de Contrato)",
            "address_id": cliente_final, "location_id": None,
            "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
        })

        # 4️⃣ Movement Item
        movimento_itens_mestre.append({
            "movement_id": id_final, "movement_type_id": 1, "service_order_item_id": item_servico_id_atual,
            "equipment_id": equipment_id_ref, "extra_id": extra_id_atual, "status_id": 3,
            "product_item_id": None, "alias": name_item_final,
            "old_organization_id": None, "new_organization_id": None, "operation_type": 'ALUGUEL',
            "confirmed_at": None, "confirmed_by": None,
            "created_at": mov_date, "updated_at": mov_date, "deleted_at": deleted_at_mov
        })

    # ======================================================================
    # SALVAMENTO
    # ======================================================================
    print("\n🚀 Persistindo dados de ALUGUEL no banco...")
    with engine_new.connect() as conn:
        trans = conn.begin()
        try:
            if servicos_mestre:
                pd.DataFrame(servicos_mestre).to_sql("service_orders", con=conn, if_exists="append", index=False)
                print(f"  ✔️ {len(servicos_mestre)} Registros em 'service_orders'.")

            if service_itens_mestre:
                pd.DataFrame(service_itens_mestre).to_sql("service_order_items", con=conn, if_exists="append", index=False)
                print(f"  ✔️ {len(service_itens_mestre)} Registros em 'service_order_items'.")

            if movimentos_mestre:
                pd.DataFrame(movimentos_mestre).to_sql("movements", con=conn, if_exists="append", index=False)
                print(f"  ✔️ {len(movimentos_mestre)} Registros em 'movements'.")

            if movimento_itens_mestre:
                pd.DataFrame(movimento_itens_mestre).to_sql("movement_items", con=conn, if_exists="append", index=False)
                print(f"  ✔️ {len(movimento_itens_mestre)} Registros em 'movement_items'.")

            if service_order_item_extra_equipments:
                pd.DataFrame(service_order_item_extra_equipments).to_sql(
                    "service_order_item_extra_equipments", con=conn, if_exists="append", index=False
                )
                print(f"  ✔️ {len(service_order_item_extra_equipments)} Registros em 'service_order_item_extra_equipments'.")

            if equipamentos_alterados:
                lista_equip_ids = list(equipamentos_alterados)
                for i in range(0, len(lista_equip_ids), 500):
                    bloco = lista_equip_ids[i:i + 500]
                    conn.execute(text("UPDATE equipments SET status_id = 2 WHERE id IN :ids"), {"ids": tuple(bloco)})
                print(f"  ✔️ {len(lista_equip_ids)} Equipamentos atualizados para status ALUGADO.")

            # Atualização de saldo dos contract_items
            itens_contrato_modificados = []
            ids_ja_processados = set()
            for dados in dict_contrato_item_por_chave.values():
                item_id = dados['id']
                qtd_original = dados['original_quantity']
                qtd_atual = saldos_por_id[item_id]
                if qtd_atual != qtd_original and item_id not in ids_ja_processados:
                    itens_contrato_modificados.append({"id": item_id, "nova_qtd": qtd_atual})
                    ids_ja_processados.add(item_id)

            if itens_contrato_modificados:
                for item in itens_contrato_modificados:
                    conn.execute(
                        text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"),
                        {"nova_qtd": item['nova_qtd'], "id": item['id']}
                    )
                print(f"  ✔️ {len(itens_contrato_modificados)} contract_items atualizados.")

            trans.commit()
            print("🎉 ALUGUEL concluído com sucesso!")

        except Exception as e:
            trans.rollback()
            print(f"❌ Erro crítico no módulo ALUGUEL: {e}")
            raise e

    # ----------------------------------------------------------------------
    print("\n--- 🏁 Resumo ALUGUEL ---")
    print(f"📦 Serviços Criados: {len(servicos_mestre)}")
    print(f"📦 Movimentos Criados: {len(movimentos_mestre)}")
    print(f"📦 Itens de Serviços: {len(service_itens_mestre)}")
    print(f"📦 Itens de Movimentos: {len(movimento_itens_mestre)}")
    print(f"📦 Equipamentos Extras: {len(service_order_item_extra_equipments)}")