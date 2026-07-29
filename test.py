import os
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from datetime import datetime

from config.config import CLIENTES_BLOQUEADOS, ORGANIZACOES_BLOQUEADAS, MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS
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
        
        query_saldos = text("""
            SELECT
                coi.id AS contract_item_id,
                coi.alias AS contract_item_alias,
                con.id AS contract_id,
                con.name AS contract_name,
                coi.quantity,
                coi.available_quantity
            FROM contract_items coi
            INNER JOIN event_additives ev ON coi.event_additive_id = ev.id
            INNER JOIN contract_events cov ON ev.event_id = cov.id
            INNER JOIN contracts con ON cov.contract_id = con.id
        """)
        df_saldos_contract_items = pd.read_sql(query_saldos, conn)

        df_equipamentos_refatorado = pd.read_sql(
            "SELECT id, number, name, current_organization_id, deleted_at FROM equipments", conn
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

        query_refs_equipamentos = text("""
            SELECT
                eqp.id AS equipment_id,
                prod.id AS product_id,
                types.id AS type_id
            FROM equipments eqp
            INNER JOIN product_items proditem ON eqp.product_item_id = proditem.id
            INNER JOIN products prod ON proditem.product_id = prod.id
            INNER JOIN types ON prod.type_id = types.id
        """)
        result_refs = conn.execute(query_refs_equipamentos).fetchall()
        dict_refs_por_equipamento = {
            int(row.equipment_id): {
                'type_id': int(row.type_id), 
                'product_id': int(row.product_id)
            }
            for row in result_refs
        }

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

    saldos_por_id = {
        int(row['contract_item_id']): limpar_valor_numerico(row['available_quantity'])
        for _, row in df_saldos_contract_items.iterrows()
        if pd.notna(row['contract_item_id'])
    }

    # Mantém o dicionário legacy apenas para a lógica de 'is_kit_override' do Aluguel
    dict_tipo_por_equipamento = {
        k: v['type_id'] for k, v in dict_refs_por_equipamento.items()
    }

    print(f"   ✅ {len(dict_contrato_item_por_chave)} combinações (cliente, contrato, item, descrição) indexadas.")
    print(f"   ✅ {len(dict_refs_por_equipamento)} mapeamentos de type/product indexados para os equipamentos.")

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
        "dict_refs_por_equipamento": dict_refs_por_equipamento,
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
        self.equipment_histories_mestre = []

    def limpar_tabelas_movimento(self):
        pass

    # ==============================================================================
    # 🎯 NOVA FUNÇÃO OTIMIZADA: CTE COM ROW_NUMBER()
    # ==============================================================================
    def buscar_ultimo_movimento_cte(self, lista_tombos: list, tipos_permitidos: tuple = (1,), situacoes_permitidas: tuple = (1, 15)) -> dict:
        """
        Busca o último movimento de um equipamento, filtrando por tipos específicos (ex: 1 para Aluguel, 5 para Substituição)
        e restrito apenas aos tombos que vieram do Parquet. Usa CTE (WITH) com ROW_NUMBER() para máxima performance.
        """
        if not lista_tombos: 
            return {}

        # Formatação segura para o SQL (Injeta as aspas e separa por vírgula)
        lista_tombos_sql = "(" + ", ".join([f"'{str(t).strip()}'" for t in lista_tombos]) + ")"
        tipos_sql = "(" + ", ".join(map(str, tipos_permitidos)) + ")"
        situacoes_sql = "(" + ", ".join(map(str, situacoes_permitidas)) + ")"

        query = f"""
            WITH MovimentosOrdenados AS (
                SELECT
                    alq.id AS equipment_id,
                    alq.numero AS tombo,
                    alq.situacao_id,
                    mov.id,
                    mov.data,
                    mov.updated_at,
                    mov.deleted_at,
                    mov.tipo_id,
                    mov.cliente_id,
                    mov.usuario_id,
                    ROW_NUMBER() OVER(PARTITION BY alq.id ORDER BY mov.data DESC, mov.id DESC) AS ordem
                FROM aluguel_equipamentos alq
                INNER JOIN aluguel_movimento_itens movi ON alq.id = movi.equipamento_id
                INNER JOIN aluguel_movimento mov ON movi.movimento_id = mov.id
                WHERE mov.deleted_at IS NULL 
                  AND alq.deleted_at IS NULL 
                  AND alq.situacao_id IN {situacoes_sql}
                  AND mov.tipo_id IN {tipos_sql}
                  AND alq.numero IN {lista_tombos_sql}
            )
            SELECT * FROM MovimentosOrdenados WHERE ordem = 1;
        """
        
        # Executa no banco legado
        df_resultado = pd.read_sql(text(query), self.engine_legado)
        df_resultado['tombo_clean'] = df_resultado['tombo'].apply(limpar_codigo)

        dict_res = {}
        for _, row in df_resultado.iterrows():
            tombo_chave = row['tombo_clean']
            if tombo_chave:
                dict_res[tombo_chave] = {
                    'movimento': row.to_dict(),
                    'equipment_id': int(row['equipment_id']),
                    'data_dt': pd.to_datetime(row['data']) # A data real dita a regra
                }
        return dict_res

    def buscar_equipamentos_novo_por_tombo(self, lista_tombos: list) -> dict:

        if not lista_tombos:
            return {}

        tombos_formatados = [f"'{str(t).strip()}'" for t in lista_tombos]
        lista_tombos_sql = "(" + ", ".join(tombos_formatados) + ")"
        
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
        
        df_resultado = pd.read_sql(text(query), self.engine_new)

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

    def regras_item_contratos(
            self, csv_contract_id, csv_item_id, equipment_id_ref, recipient_id, dict_is_kit, abater_saldo=True
        ):
            """
            Avalia Avulso, Kit e Excedente, e abate o saldo se necessário.
            Usado pelos scripts filhos (Aluguel, Substituição) para evitar duplicação de regras lógicas.
            """
            contrato_id_res = None
            item_id_res = None
            is_avulso = False
            is_excedente = False
            is_kit = False
            teve_match_perfeito = False
            motivo_divergencia = None
    
            # 1. REGRA DO AVULSO
            if csv_contract_id is None:
                is_avulso = True
                motivo_divergencia = "Equipamento sem contrato no Parquet. Mantido como AVULSO."
            else:
                contrato_id_res = csv_contract_id
                
                type_id_atual = self.dados["dict_tipo_por_equipamento"].get(equipment_id_ref)
                eh_tipo_kit = dict_is_kit.get(type_id_atual, 0) == 1
                
                # 2. REGRA DO KIT
                if eh_tipo_kit and csv_item_id is None:
                    is_kit = True
                    motivo_divergencia = "Equipamento é KIT e não foi atrelado a um item no Parquet. Regra de KIT aplicada (Imune)."
                
                # 3. REGRA NORMAL OU EXCEDENTE
                else:
                    item_id_res = csv_item_id
                    if item_id_res is not None:
                        teve_match_perfeito = True
                    else:
                        # Fallback em Cascata
                        item_id_res = next((info['id'] for chave, info in self.dados["dict_contrato_item_por_chave"].items() if chave[1] == contrato_id_res), None)
                        if not item_id_res:
                            item_id_res = next((info['id'] for chave, info in self.dados["dict_contrato_item_aluguel_por_chave"].items() if chave[1] == contrato_id_res), None)
                        if not item_id_res:
                            item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)
                            
                        if not motivo_divergencia:
                            motivo_divergencia = "ID do Item ausente no Parquet. Fallback aplicado para o primeiro item disponível."
    
                    if item_id_res is not None:
                        item_id_res_int = int(item_id_res)
                        saldo_atual = self.dados["saldos_por_id"].get(item_id_res_int, 0)
                        
                        # Ativador da Regra do Excedente
                        if saldo_atual <= 0:
                            is_excedente = True
                            if not motivo_divergencia:
                                motivo_divergencia = "Item de contrato atrelado está com saldo zerado/negativo. Regra do EXCEDENTE aplicada."
                        else:
                            # Abate o Saldo Globalmente
                            if abater_saldo:
                                self.dados["saldos_por_id"][item_id_res_int] = saldo_atual - 1
                                if hasattr(self, 'saldos_modificados'):
                                    self.saldos_modificados.add(item_id_res_int)
                    else:
                        is_excedente = True
                        motivo_divergencia = "Contrato sem itens vinculáveis no banco. Forçado para EXCEDENTE."
    
            return contrato_id_res, item_id_res, is_avulso, is_kit, is_excedente, teve_match_perfeito, motivo_divergencia

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
        status_equipment_id: int,
        history_reason: str,
        type_id_ref: int = None,
        product_id_ref: int = None,
        alias_item: str = None,
        alias_movimento: str = None,
        details_capa: str = "Processo de Migração",
        details_item: str = None,
        fallback_contract_item_id: int = None,
        forcar_extra: bool = False,
        is_exchange: bool = False,
        consumir_saldo: bool = True,
        is_kit_override: bool = None,
        type_id_override: int = None,
        forcar_atualizacao_parque: bool = False
    ):
        
        # Blindagem para usuarios inexistentes
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
                "situation": "APPROVED",
                "changed_by": usuario_seguro,
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
        
        if tipo_movimento_id == 7 or not consumir_saldo:
            is_extra_flag = False
            extra_id_atual = None
            contrato_item_id_resolvido = (
                int(contrato_item_id)
                if contrato_item_id is not None and pd.notna(contrato_item_id)
                else None
            )
        else:
            if is_kit_override is None and type_id_override is None:
                is_extra_flag, extra_id_atual, contrato_item_id_resolvido = self.calcular_saldo(
                    contrato_item_id, recipient_id, equipment_id_ref, mov_date,
                    item_servico_id_atual, fallback_contract_item_id, forcar_extra
                )
            else:
                is_extra_flag, extra_id_atual, contrato_item_id_resolvido = self.calcular_saldo(
                    contrato_item_id, recipient_id, equipment_id_ref, mov_date,
                    item_servico_id_atual, fallback_contract_item_id, forcar_extra,
                    is_kit_override=is_kit_override,
                    type_id_override=type_id_override
                )

        refs_equip = self.dados.get("dict_refs_por_equipamento", {}).get(int(equipment_id_ref), {})
        type_id_resolvido = type_id_ref if type_id_ref is not None else refs_equip.get('type_id')
        product_id_resolvido = product_id_ref if product_id_ref is not None else refs_equip.get('product_id')


        # 3️⃣ SERVICE ORDER ITEM
        txt_detalhe_final = details_item if details_item else ("Item Extra (Saldo do Item de Contrato Esgotado)" if is_extra_flag else None)

        equip_id_mov_item = None if operation_type == 'ALUGUEL' else equipment_id_ref
        type_mov_item = None if operation_type == 'ALUGUEL' else type_id_resolvido
        prod_id_item = None if operation_type == 'ALUGUEL' else product_id_resolvido

        self.service_itens_mestre.append({
            "id": item_servico_id_atual,
            "status_id": 3,
            "service_order_id": id_capa_atual,
            "department_id": 2,
            "movement_type_id": tipo_movimento_id,
            "contract_item_id": contrato_item_id_resolvido,
            "alias": alias_item,
            "equipment_id": equip_id_mov_item,
            "type_id": type_mov_item,
            "product_id": prod_id_item,
            "is_exchange": is_exchange,
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
        
        shipment_item_id_atual = self.shipment_item_id_counter
        self.shipment_items_mestre.append({
            "id": self.shipment_item_id_counter,
            "shipment_id": ship_id_atual,
            "status_id": status_shipment,
            "movement_item_id": item_mov_id_atual,
            "volume_id": None,
            "details": f"Guia de transporte (Operação: {operation_type})",
            "address_id": cliente_final_address_id,
            'created_at': mov_date,
            'updated_at': mov_date
        })
        self.shipment_item_id_counter += 1

        self.shipment_movements_mestre.append({
            "shipment_id": ship_id_atual,
            "movement_id": id_capa_atual
        })

        # ==============================================================================
        # 🛡️ ESCUDO CRONOLÓGICO (Proteção do Parque Físico)
        # ==============================================================================
        ultimo_mov_conhecido = self.dados.get("dict_ultimo_mov_equip", {}).get(int(equipment_id_ref))
        if forcar_atualizacao_parque or ultimo_mov_conhecido is None or int(id_final) >= int(ultimo_mov_conhecido):
            self.equipamentos_alterados.append({
                "e_id": int(equipment_id_ref),
                "mov_item_id": int(item_mov_id_atual),
                "addr_id": int(cliente_final_address_id) if cliente_final_address_id else None
            })

        # ==============================================================================
        # 6️⃣ HISTÓRICO DO EQUIPAMENTO
        # ==============================================================================
        self.equipment_histories_mestre.append({
            "equipment_id": equipment_id_ref, 
            "status_id": status_equipment_id, 
            "occurred_at": mov_date, 
            "movement_item_id": item_mov_id_atual,
            "service_order_item_id": item_servico_id_atual, 
            "contract_item_id": contrato_item_id_resolvido, 
            "shipment_item_id": shipment_item_id_atual,
            "is_conversion": 0, 
            "reason": history_reason, 
            "user_id": usuario_seguro
        })

        return id_capa_atual, item_servico_id_atual, item_mov_id_atual


    # ==========================================================================
    # 1. PERSISTÊNCIA DE NOVOS REGISTROS (INSERT EM MASSA)
    # ==========================================================================
    def salvar_movimentos_banco(self):
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
            if self.equipment_histories_mestre: pd.DataFrame(self.equipment_histories_mestre).to_sql("equipment_history", con=conn, if_exists="append", index=False)

        print(f"--- 🏁 Resumo {self.__class__.__name__} ---")
        print(f"📦 Capas Pais Criadas: {len(self.servicos_mestre)}")
        print(f"📦 Itens Criados: {len(self.service_itens_mestre)}")
        print(f"📦 Itens Extras Inseridos: {len(self.itens_extras_mestre)}")
        print(f"📦 Histórico do Equipamento Criado: {len(self.equipment_histories_mestre)}")

    # ==========================================================================
    # 2. ATUALIZAÇÃO DO PARQUE FÍSICO (UPDATE EM MASSA)
    # ==========================================================================
    def atualizar_equipamentos_banco(self, id_status_equipamento: int, lista_dicionarios: list):
            if not lista_dicionarios:
                return
    
            print(f"\n🚀 Atualizando status, LAST_MOVEMENT_ITEM e ENDEREÇO em {len(lista_dicionarios)} equipamentos...")
            with self.engine_new.begin() as conn:
                updates = []
                for item in lista_dicionarios:
                    if isinstance(item, dict) and "e_id" in item:
                        updates.append({
                            "e_id": item["e_id"], 
                            "s_id": int(id_status_equipamento), 
                            "mov_item_id": item.get("mov_item_id"),
                            "addr_id": item.get("addr_id")
                        })
                    else:
                        for e_id, mov_item_id in item.items():
                            updates.append({
                                "e_id": int(e_id), 
                                "s_id": int(id_status_equipamento), 
                                "mov_item_id": int(mov_item_id) if pd.notna(mov_item_id) else None,
                                "addr_id": None
                            })
    
                conn.execute(
                    text("""
                        UPDATE equipments 
                        SET status_id = :s_id, 
                            last_movement_item_customer_id = :mov_item_id, 
                            address_id = COALESCE(:addr_id, address_id) 
                        WHERE id = :e_id
                    """),
                    updates
                )
            print("   ✅ Atualização de equipamentos concluída.")