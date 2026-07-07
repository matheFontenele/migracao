import os
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from datetime import datetime

from config.config import CLIENTES_BLOQUEADOS, ORGANIZACOES_BLOQUEADAS, MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS
from sqlalchemy import text
from datetime import datetime

# Importa a função oficial de higienização do projeto
from utils.sanetizador import executar_truncate_tabelas, limpar_valor_inteiro, limpar_valor_numerico, normalizar_para_match

# ==============================================================================
# CONFIGURAÇÕES DE MAPEAMENTO E BLOQUEIO DE ORGANIZAÇÕES
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==============================================================================
# FUNÇÕES AUXILIARES GENÉRICAS (usadas por todos os tipos de movimento)
# ==============================================================================
def limpar_codigo(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    return s[:-2] if s.endswith(".0") else s

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

def resetar_saldo_contract_items(engine):
    print("🔄 Resetando available_quantity de contract_items para o valor original (quantity)...")
    with engine.begin() as conn:
        conn.execute(text("UPDATE contract_items SET available_quantity = quantity"))
    print("   ✅ Saldos reestabelecidos.")

# ==============================================================================
# CARGA DE DADOS COMPARTILHADOS
# ==============================================================================
def carregar_dados_compartilhados(engine_legado, engine_new):
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

        df_vinculos = pd.read_sql("SELECT customer_id, contract_id FROM contract_recipient_customers", conn)

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
            "SELECT id, number, name, current_organization_id, deleted_at FROM equipments", conn
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

        query_primeiro_item_por_contrato = text("""
            SELECT c.id AS contract_id, ci.id AS contract_item_id
            FROM contract_items ci
            JOIN event_additives ea ON ea.id = ci.event_additive_id
            JOIN contract_events ce ON ce.id = ea.event_id
            JOIN contracts c ON c.id = ce.contract_id
            ORDER BY c.id ASC, ci.id ASC
        """)
        df_primeiro_item_por_contrato = pd.read_sql(query_primeiro_item_por_contrato, conn)
        df_primeiro_item_por_contrato = df_primeiro_item_por_contrato.drop_duplicates(subset=['contract_id'], keep='first')

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
    df_equipamentos_ativos = df_equipamentos_refatorado[
        df_equipamentos_refatorado['deleted_at'].isna()
    ].sort_values('id')
    dict_equip_ref_por_number = {
        limpar_codigo(row['number']): int(row['id'])
        for _, row in df_equipamentos_ativos.iterrows()
    }
    ids_equipamentos_ref = set(df_equipamentos_refatorado['id'].astype(int))
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
    dict_primeiro_item_por_contrato = dict(zip(
        df_primeiro_item_por_contrato['contract_id'].astype(int),
        df_primeiro_item_por_contrato['contract_item_id'].astype(int)
    ))

    # Dicionário de contrato_item com chave composta (cliente, contrato, item, descrição)
    dict_contratos_vinculados = df_vinculos.groupby('customer_id')['contract_id'].apply(list).to_dict()

    dict_contrato_item_por_chave = {
        (
            int(row['cliente_id']),
            int(row['contract_id']),
            normalizar_para_match(row['alias_item_contract']),
            normalizar_para_match(row['description'])
        ): {
            'id': int(row['contract_item_id']),
            'contract_id': int(row['contract_id']),
            'available_quantity': limpar_valor_numerico(row['available_quantity']),
            'original_quantity': limpar_valor_numerico(row['available_quantity'])
        }
        for _, row in df_contratos_itens.iterrows()
        if pd.notna(row['alias_item_contract']) and pd.notna(row['cliente_id'])
    }

    dict_contrato_item_aluguel_por_chave = {}
    dict_contrato_aluguel_por_chave = {}

    for _, row in df_contratos_itens.iterrows():
        if pd.isna(row['legacy_client_id']) or pd.isna(row['alias_item_contract']):
            continue

        legacy_client_id = limpar_valor_inteiro(row['legacy_client_id'])
        contract_id = limpar_valor_inteiro(row['contract_id'])
        contract_item_id = limpar_valor_inteiro(row['contract_item_id'])
        if not legacy_client_id or not contract_id or not contract_item_id:
            continue

        info_item = {
            'id': contract_item_id,
            'contract_id': contract_id,
            'available_quantity': limpar_valor_numerico(row['available_quantity']),
            'original_quantity': limpar_valor_numerico(row['available_quantity'])
        }

        chave_contrato = (legacy_client_id, contract_id)
        dict_contrato_aluguel_por_chave.setdefault(chave_contrato, {
            'contract_id': contract_id,
            'first_contract_item_id': dict_primeiro_item_por_contrato.get(contract_id, contract_item_id)
            })

        chave_item = (
            legacy_client_id,
            contract_id,
            normalizar_para_match(row['alias_item_contract']),
            normalizar_para_match(row['description'])
        )
        dict_contrato_item_aluguel_por_chave.setdefault(chave_item, info_item)

    saldos_por_id = {}
    for dados in dict_contrato_item_por_chave.values():
        item_id = dados['id']
        if item_id not in saldos_por_id:
            saldos_por_id[item_id] = dados['available_quantity']
    for dados in dict_contrato_item_aluguel_por_chave.values():
        item_id = dados['id']
        if item_id not in saldos_por_id:
            saldos_por_id[item_id] = dados['available_quantity']

    print(f"   ✅ {len(dict_contrato_item_por_chave)} combinações (cliente, contrato, item, descrição) indexadas.")
    print(f"   ✅ {len(dict_contrato_item_aluguel_por_chave)} combinações de aluguel por legacy_customer_id indexadas.")

    return {
        "dict_tombo_por_equip_id": dict_tombo_por_equip_id,
        "dict_equip_ref_por_number": dict_equip_ref_por_number,
        "ids_equipamentos_ref": ids_equipamentos_ref,
        "dict_movimentos_legado": dict_movimentos_legado,
        "dict_cliente_adress": dict_cliente_adress,
        "dict_endereco_por_legacy_client": dict_endereco_por_legacy_client,
        "dict_primeiro_item_por_cliente": dict_primeiro_item_por_cliente,
        "dict_primeiro_contrato_por_cliente": dict_primeiro_contrato_por_cliente,
        "dict_primeiro_item_por_contrato": dict_primeiro_item_por_contrato,
        "dict_contratos_vinculados": dict_contratos_vinculados,
        "dict_contrato_item_por_chave": dict_contrato_item_por_chave,
        "dict_contrato_item_aluguel_por_chave": dict_contrato_item_aluguel_por_chave,
        "dict_contrato_aluguel_por_chave": dict_contrato_aluguel_por_chave,
        "dict_tipo_por_equipamento": dict_tipo_por_equipamento,
        "saldos_por_id": saldos_por_id,
        "df_movimento_item_legado": df_movimento_item_legado,
    }

# ==============================================================================
# CLASSE PAI: A ABSTRAÇÃO ABSOLUTA DE MOVIMENTOS
# ==============================================================================

class BaseMigracaoMovimento:
    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=1, limpar_ambiente = False):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.dados = dados_compartilhados
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.limpar_ambiente = limpar_ambiente

        with self.engine_new.connect() as conn:
            max_so = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM service_orders")).scalar()
            max_mov = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM movements")).scalar()
            max_so_item = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM service_order_items")).scalar()
            max_mov_item = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM movement_items")).scalar()
            max_extra = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM service_order_item_extra_equipments")).scalar()

            # Pegando os MAX IDs das tabelas de SHIPMENT (Transporte) 🎯
            max_ship = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM shipments")).scalar()
            max_ship_item = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM shipment_items")).scalar()

            # Busca usuários válidos para fallback global (evita quebra de Foreign Key)
            res_users = conn.execute(text("SELECT id FROM users")).fetchall()
            self.usuarios_validos = set(row[0] for row in res_users)
        
        # Contadores blindados
        self.so_capa_id_counter = max(max_so, max_mov) + 1
        self.so_item_id_counter = max_so_item + 1
        self.mov_item_id_counter = max_mov_item + 1
        self.extra_id_counter = max_extra + 1

        # Contadores de Carregamento de Transporte (Shipment)
        self.shipment_id_counter = max_ship + 1
        self.shipment_item_id_counter = max_ship_item + 1
        
        # Dicionário para gerenciar os IDs gerados em memória
        self.mapa_ids_capa = {}
        
        self.servicos_mestre = []
        self.service_itens_mestre = []
        self.movimentos_mestre = []
        self.movimento_itens_mestre = []
        self.itens_extras_mestre = []
        
        self.pedidos_pai_inseridos = set()
        self.equipamentos_alterados = []

        self.shipments_mestre = []
        self.shipment_movements_mestre = []
        self.shipment_items_mestre = []

    def limpar_tabelas_movimento(self):
        pass

    def buscar_ultimo_movimento_por_tombo(self, lista_tombos: list) -> dict:
        if not lista_tombos:
            return {}

        lista_tombos_sql = "(" + ", ".join(map(str, lista_tombos)) + ")"
        query = f"""
            SELECT am.id, am.data, am.tipo_id, amt.nome AS tipo_nome,
                   am.cliente_id, am.usuario_id, am.updated_at, am.deleted_at,
                   ae.id AS equipment_id, ae.numero AS tombo
            FROM aluguel_movimento am
            INNER JOIN aluguel_movimento_itens ami ON ami.movimento_id = am.id
            INNER JOIN aluguel_equipamentos ae ON ae.id = ami.equipamento_id
            INNER JOIN aluguel_tipos_movimento amt ON amt.id = am.tipo_id
            WHERE am.deleted_at IS NULL
              AND ae.deleted_at IS NULL
              AND ae.numero IN {lista_tombos_sql}
              AND am.id = (
                  SELECT am2.id FROM aluguel_movimento am2
                  INNER JOIN aluguel_movimento_itens ami2 ON ami2.movimento_id = am2.id
                  WHERE ami2.equipamento_id = ae.id AND am2.deleted_at IS NULL
                  ORDER BY am2.updated_at DESC, am2.data DESC LIMIT 1
              )
            ORDER BY ae.numero, am.updated_at DESC, am.data DESC, am.id DESC;
        """
        df_resultado = pd.read_sql(query, self.engine_legado)

        dict_res = {}
        for _, row in df_resultado.iterrows():
            tombo_chave = limpar_codigo(row['tombo'])
            if tombo_chave and tombo_chave != 'nan':
                dict_res.setdefault(tombo_chave, {
                    'movimento': row.to_dict(),
                    'equipment_id': int(row['equipment_id']),
                    'data_dt': pd.to_datetime(row['updated_at'] if pd.notna(row['updated_at']) else row['data'])
                })
        
        return dict_res

    def buscar_equipamentos_novo_por_tombo(self, lista_tombos: list) -> dict:

        if not lista_tombos:
            return {}

        tombos_formatados = [f"'{str(t).strip()}'" for t in lista_tombos]
        lista_tombos_sql = "(" + ", ".join(tombos_formatados) + ")"
        
        # O SEU SQL APLICADO AQUI
        query = f"""
            SELECT 
                eq.id, 
                eq.number, 
                eq.name, 
                eq.last_movement_item_customer_id, 
                eq.deleted_at 
            FROM equipments eq 
            WHERE eq.number IN {lista_tombos_sql} AND eq.deleted_at IS NULL
        """
        
        # Executa a query diretamente no banco NOVO
        df_resultado = pd.read_sql(text(query), self.engine_new)

        # Monta o dicionário de tradução ultra-rápida (O(1))
        dict_res = {}
        for _, row in df_resultado.iterrows():
            tombo_chave = limpar_codigo(row['number'])
            if tombo_chave:
                dict_res[tombo_chave] = int(row['id'])
                
        return dict_res

    def calcular_saldo(
        self, contrato_item_id, recipient_id, equipment_id_ref, mov_date,
        item_servico_id_atual, fallback_contract_item_id=None, forcar_extra=False
    ):
        return 0, None, contrato_item_id

    def registrar_movimento(
        self, id_final: int,
        recipient_id: int,
        cliente_final_address_id: int,
        usuario_id: int,
        mov_date: str,
        deleted_at_mov: str,
        contrato_id: int,
        organization_id: int,
        contrato_item_id: int,
        equipment_id_ref: int,
        tipo_movimento_id: int,
        status_shipment: int,
        operation_type: str,
        alias_item: str = None,
        alias_movimento: str = None,
        details_capa: str = "Migração Automática",
        details_item: str = None,
        fallback_contract_item_id: int = None,
        forcar_extra: bool = False,
        is_exchange: bool = False
    ):
        
        #Blindagem para usuarios inexistentes
        if hasattr(self, 'usuarios_validos') and usuario_id in self.usuarios_validos:
            usuario_seguro = usuario_id
        else:
            usuario_seguro = 1
        
        if id_final not in self.mapa_ids_capa:
            novo_id_capa = self.so_capa_id_counter
            self.so_capa_id_counter += 1
            self.mapa_ids_capa[id_final] = novo_id_capa
        
        # 1️⃣ CAPAS PAI (Service Order + Movement)
            self.servicos_mestre.append({
                "id": novo_id_capa,
                "status_id": 3,
                "movement_type_id": tipo_movimento_id,
                "contract_id": contrato_id,
                "user_id": usuario_seguro,
                "destination_order_id": None,
                "mode_transport_id": 1,
                "organization_id": organization_id,
                "recipient_customer_id": recipient_id,
                "deadline": mov_date,
                "details": details_capa,
                "created_at": mov_date,
                "updated_at": mov_date,
                "deleted_at": deleted_at_mov
            })
            self.movimentos_mestre.append({
                "id": novo_id_capa,
                "number": id_final,
                "movement_date": mov_date,
                "service_order_id": novo_id_capa,
                "recipient_customer_id": recipient_id,
                "migrate_customer_id": None,
                "organization_id": organization_id,
                "status_id": 3,
                "created_by": usuario_seguro,
                "details": details_capa,
                "created_at": mov_date,
                "updated_at": mov_date,
                "deleted_at": deleted_at_mov
            })
            self.pedidos_pai_inseridos.add(id_final)

        id_capa_atual = self.mapa_ids_capa[id_final]

        # 2️⃣ GATILHO DE SALDO E EXTRAS
        item_servico_id_atual = self.so_item_id_counter
        self.so_item_id_counter += 1
        
        if tipo_movimento_id == 7:
            is_extra_flag = False
            extra_id_atual = None
            contrato_item_id_resolvido = None
        else:
            # Só calcula o saldo se NÃO for avulso
            is_extra_flag, extra_id_atual, contrato_item_id_resolvido = self.calcular_saldo(
                contrato_item_id, recipient_id, equipment_id_ref, mov_date,
                item_servico_id_atual, fallback_contract_item_id, forcar_extra
            )

        # 3️⃣ SERVICE ORDER ITEM
        txt_detalhe_final = details_item if details_item else ("Item Extra (Saldo do Item de Contrato Esgotado)" if is_extra_flag else None)

        self.service_itens_mestre.append({
            "id": item_servico_id_atual,
            "status_id": 3,
            "service_order_id": id_capa_atual,
            "department_id": 2,
            "movement_type_id": tipo_movimento_id,
            "contract_item_id": contrato_item_id_resolvido,
            "alias": alias_item,
            "equipment_id": equipment_id_ref,
            "type_id": None,
            "product_id": None,
            "is_exchange": 1 if is_exchange else 0,
            "is_extra": is_extra_flag,
            "quantity_product": None,
            "fulfilled_quantity_product": 0,
            "quantity": 1, "details": txt_detalhe_final,
            "address_id": cliente_final_address_id,
            "location_id": None,
            "created_at": mov_date,
            "updated_at": mov_date,
            "deleted_at": deleted_at_mov
        })

        # 4️⃣ MOVEMENT ITEM
        item_mov_id_atual = self.mov_item_id_counter
        self.mov_item_id_counter += 1
        self.movimento_itens_mestre.append({
            "id": item_mov_id_atual,
            "movement_id": id_capa_atual,
            "movement_type_id": tipo_movimento_id,
            "service_order_item_id": item_servico_id_atual,
            "equipment_id": equipment_id_ref,
            "extra_id": extra_id_atual,
            "status_id": 3,
            "product_item_id": None,
            "alias": alias_movimento,
            "old_organization_id": None,
            "new_organization_id": organization_id,
            "operation_type": operation_type,
            "confirmed_at": None,
            "confirmed_by": None,
            "created_at": mov_date,
            "updated_at": mov_date,
            "deleted_at": deleted_at_mov
        })

        # 5️⃣ SHIPMENTS
        ship_id_atual = self.shipment_id_counter
        self.shipment_id_counter += 1
        
        self.shipments_mestre.append({
            "id": ship_id_atual,
            "status_id": status_shipment,
            "created_by": usuario_seguro,
            "created_at": mov_date,
            "updated_at": mov_date,
            "deleted_at": deleted_at_mov
        })
        
        self.shipment_items_mestre.append({
            "id": self.shipment_item_id_counter,
            "shipment_id": ship_id_atual,
            "status_id": status_shipment,
            "movement_item_id": item_mov_id_atual,
            "volume_id": None,
            "details": f"Guia de transporte (Operação: {operation_type})",
            "address_id": cliente_final_address_id # O destino da máquina
        })
        self.shipment_item_id_counter += 1

        self.shipment_movements_mestre.append({
            "shipment_id": ship_id_atual,
            "movement_id": id_capa_atual
        })

        self.equipamentos_alterados.append({int(equipment_id_ref): item_mov_id_atual})

        return id_capa_atual, item_servico_id_atual, item_mov_id_atual

   # ==========================================================================
    # 1. PERSISTÊNCIA DE NOVOS REGISTROS (INSERT EM MASSA)
    # ==========================================================================
    def salvar_movimentos_banco(self):
        """Salva apenas a estrutura de tabelas de movimentação e serviços."""
        print(f"\n🚀 Persistindo capas e itens de movimento no MySQL...")
        with self.engine_new.begin() as conn:
            if self.servicos_mestre: pd.DataFrame(self.servicos_mestre).to_sql("service_orders", con=conn, if_exists="append", index=False)

            if self.service_itens_mestre: pd.DataFrame(self.service_itens_mestre).to_sql("service_order_items", con=conn, if_exists="append", index=False)

            if self.movimentos_mestre: pd.DataFrame(self.movimentos_mestre).to_sql("movements", con=conn, if_exists="append", index=False)

            if self.movimento_itens_mestre: pd.DataFrame(self.movimento_itens_mestre).to_sql("movement_items", con=conn, if_exists="append", index=False)

            if self.itens_extras_mestre: pd.DataFrame(self.itens_extras_mestre).to_sql("service_order_item_extra_equipments", con=conn, if_exists="append", index=False)

            if self.shipments_mestre: pd.DataFrame(self.shipments_mestre).to_sql("shipments", con=conn, if_exists="append", index=False)

            if self.shipment_movements_mestre: pd.DataFrame(self.shipment_movements_mestre).to_sql("shipment_movements", con=conn, if_exists="append", index=False)

            if self.shipment_items_mestre: pd.DataFrame(self.shipment_items_mestre).to_sql("shipment_items", con=conn, if_exists="append", index=False)

        print(f"--- 🏁 Resumo {self.__class__.__name__} ---")
        print(f"📦 Capas Pais Criadas: {len(self.servicos_mestre)}")
        print(f"📦 Itens Criados: {len(self.service_itens_mestre)}")
        print(f"📦 Itens Extras Inseridos: {len(self.itens_extras_mestre)}")


    # ==========================================================================
    # 2. ATUALIZAÇÃO DO PARQUE FÍSICO (UPDATE EM MASSA)
    # ==========================================================================
    def atualizar_equipamentos_banco(self, id_status_equipamento: int, lista_dicionarios: list):

        if not lista_dicionarios:
            print("   ⚠️ Nenhum equipamento para atualizar. Pulando etapa de atualização.")
            return

        print(f"\n🚀 Atualizando status e cliente em {len(lista_dicionarios)} equipamentos...")
        with self.engine_new.begin() as conn:
            
            updates = []
            for dicionario in lista_dicionarios:
                for e_id, c_id in dicionario.items():
                    updates.append({
                        "e_id": int(e_id), 
                        "s_id": int(id_status_equipamento), 
                        "c_id": int(c_id)
                    })

            conn.execute(
                text("UPDATE equipments SET status_id = :s_id, last_movement_item_customer_id = :c_id WHERE id = :e_id"),
                updates
            )
        print("   ✅ Atualização de equipamentos concluída.")
