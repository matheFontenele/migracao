import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from config.config import FALSOS_RESERVAS
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino
from movimentos.migracao_movimentos import carregar_dados_compartilhados, resetar_saldo_contract_items

class MigracaoReserva(BaseMigracaoMovimento):
    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=500000):

        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter)
        self.consumir_saldos = False

    def calcular_saldo(self, *args, **kwargs):
        """
        POLIMORFISMO: Substitui a regra matemática. 
        Reservas não consomem saldo e não geram itens extras.
        """
        return 0, None, int(args[0]) if pd.notna(args[0]) else None

    def _carregar_mapa_reservas_refatorado(self):
        """reserved_customer_id legado -> (addressable_id, addresses.id)."""
        with self.engine_new.connect() as conn:
            df = pd.read_sql(text("""
                SELECT id AS address_id, addressable_id, reserved_customer_id
                FROM addresses
                WHERE addressable_type = 'customer'
                  AND reserved_customer_id IS NOT NULL
            """), conn)

        mapa = {}
        duplicados = {}
        for _, row in df.iterrows():
            reserva_id = int(row['reserved_customer_id'])
            rota = (int(row['addressable_id']), int(row['address_id']))
            if reserva_id in mapa and mapa[reserva_id] != rota:
                duplicados.setdefault(reserva_id, [mapa[reserva_id]]).append(rota)
                continue
            mapa[reserva_id] = rota

        if duplicados:
            print("⚠️ IDs de reserva vinculados a mais de um address; usando o primeiro:")
            for reserva_id, rotas in duplicados.items():
                print(f"   - reserved_customer_id={reserva_id}: {rotas}")

        print(f"🗺️ Endereços de reserva carregados: {len(mapa)} IDs legados")
        return mapa

    def _extrair_dados_reserva(self, frente, ids_clientes_reserva=None):
        print(f"   📖 Extraindo Frente {frente} de Reservas...")

        ids_sql = ""
        if frente == 1:
            ids_clientes_reserva = sorted({int(value) for value in (ids_clientes_reserva or [])})
            if not ids_clientes_reserva:
                print("   ℹ️ Nenhum reserved_customer_id cadastrado em addresses.")
                return pd.DataFrame(columns=[
                    'TOMBO', 'NOME_EQUIPAMENTO', 'ID_CLIENTE', 'CLIENTE',
                    'orgao_id', 'MOVIMENTO_ID', 'usuario_id', 'updated_at', 'deleted_at',
                ])
            ids_sql = f"AND ac.id IN ({', '.join(map(str, ids_clientes_reserva))})"

        if frente == 1:
            filtro_where = "mov.tipo_id IN (1, 7)"
            filtro_situacao = "eq.situacao_id = 1"
        else:
            filtro_where = "mov.tipo_id = 7"
            filtro_situacao = "eq.situacao_id IN (1, 15)"

        query = f"""
            SELECT
                eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
                ac.id AS ID_CLIENTE, ac.nome_razao_social AS CLIENTE,
                ac.orgao_id, mov.id as MOVIMENTO_ID,
                mov.usuario_id, mov.updated_at, mov.deleted_at
            FROM aluguel_equipamentos eq
            INNER JOIN (
                    SELECT mi.equipamento_id, MAX(m.id) as ultimo_movimento_id
                    FROM aluguel_movimento_itens mi
                    INNER JOIN aluguel_movimento m ON m.id = mi.movimento_id
                    WHERE m.deleted_at IS NULL
                      AND mi.deleted_at IS NULL
                GROUP BY mi.equipamento_id
            ) ult_mov ON ult_mov.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ult_mov.ultimo_movimento_id
            LEFT JOIN aluguel_clientes ac ON ac.id = mov.cliente_id
            WHERE eq.deleted_at IS NULL 
              AND ac.deleted_at IS NULL 
              AND {filtro_situacao}
              {ids_sql}
              AND {filtro_where}
        """
        
        with self.engine_legado.connect() as conn:
            return pd.read_sql(text(query), conn)

    def executar(self):
        print("\n" + "=" * 70)
        print("📦 MÓDULO: ALOCAÇÃO DE RESERVAS (ESTOQUE E CLIENTES)")
        print("=" * 70)

        # 1. Cada reserved_customer_id do destino informa qual address recebe
        # a reserva. Não recalculamos pareamentos por nome neste módulo.
        with self.engine_new.connect() as conn:
            dict_contract_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM contracts", conn).values.T))
            dict_customer_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM customers", conn).values.T))
            dict_equip_org = dict(zip(*pd.read_sql("SELECT id, current_organization_id FROM equipments", conn).values.T))

        mapa_address_por_reserva = self._carregar_mapa_reservas_refatorado()

        # 2. Executa as extrações. A Frente 1 é filtrada pelos IDs de reserva
        # mapeados, inclusive exceções manuais cujo nome não contém RESERVA.
        df_frente1 = self._extrair_dados_reserva(
            frente=1,
            ids_clientes_reserva=mapa_address_por_reserva.keys(),
        )
        df_frente2 = self._extrair_dados_reserva(frente=2)

        ids_reserva = set(mapa_address_por_reserva)
        df_frente1 = df_frente1[
            pd.to_numeric(df_frente1['ID_CLIENTE'], errors='coerce').isin(ids_reserva)
        ].copy()
        nomes_reserva = df_frente2['CLIENTE'].fillna('').str.contains(
            r'\b(?:RESERVA|RESERVADO)\b', case=False, regex=True
        )
        ids_cliente_frente2 = pd.to_numeric(df_frente2['ID_CLIENTE'], errors='coerce')
        df_frente2 = df_frente2[
            (~nomes_reserva | ids_cliente_frente2.isin(FALSOS_RESERVAS))
            & ~ids_cliente_frente2.isin(ids_reserva)
        ].copy()

        if df_frente1.empty and df_frente2.empty:
            print("⚠️ Nenhum movimento de reserva encontrado nas duas frentes.")
            return

        rejeitados = 0
        rejeitados_sem_equipamento = 0
        rejeitados_sem_destino = 0

        # 3. Lógica central de processamento linha a linha
        def processar_linha(row, frente):
            nonlocal rejeitados, rejeitados_sem_equipamento, rejeitados_sem_destino
            id_final = int(row['MOVIMENTO_ID'])
            tombo = str(row['TOMBO']).strip()
            cliente_id_legado = int(row['ID_CLIENTE'])
            orgao_id_legado = row['orgao_id']
            
            equipment_id_ref = self.dados["dict_equip_ref_por_number"].get(tombo)
            if not equipment_id_ref:
                rejeitados += 1
                rejeitados_sem_equipamento += 1
                return

            # Roteamento baseado na Frente
            if frente == 1:
                rota_refatorada = mapa_address_por_reserva.get(cliente_id_legado)
                recipient_id = rota_refatorada[0] if rota_refatorada else None
                cliente_final = rota_refatorada[1] if rota_refatorada else None
            else:
                recipient_id = self.dados["dict_cliente_adress"].get(cliente_id_legado)
                cliente_final = self.dados["dict_endereco_por_legacy_client"].get(cliente_id_legado)

            if not recipient_id or not cliente_final:
                rejeitados += 1
                rejeitados_sem_destino += 1
                return
            
            usr_id = int(row['usuario_id']) if pd.notna(row['usuario_id']) and row['usuario_id'] != 0 else 1
            mov_date = row['updated_at'] if pd.notna(row['updated_at']) else self.now
            
            # Fallback Padrão: Puxa o primeiro contrato/item disponível do cliente
            contrato_id_res = self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)

            # Roteamento Cascata de Organização
            org_id_cascata = dict_contract_org.get(contrato_id_res) or dict_customer_org.get(recipient_id) or dict_equip_org.get(equipment_id_ref)
            org_id_destino = int(org_id_cascata) if org_id_cascata and pd.notna(org_id_cascata) else descobrir_id_organizacao_destino(orgao_id_legado)

            # Envia para a fábrica da Classe Pai criar os registros nas 4 tabelas mestre
            self.registrar_movimento(
                id_final=id_final,
                recipient_id=recipient_id,
                cliente_final_address_id=cliente_final,
                usuario_id=usr_id,
                mov_date=mov_date,
                deleted_at_mov=row['deleted_at'] if pd.notna(row['deleted_at']) else None,

                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=equipment_id_ref,
                
                status_shipment=2,
                tipo_movimento_id=4,

                status_equipment_id=3, 
                history_reason='SHIPPING_CONFIRMED_RESERVED',
                
                organization_id=org_id_destino,
                operation_type='RESERVA',
                
                forcar_extra=False, 
                is_exchange=False,
                
                alias_item=None,
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa=f"Migração - Reserva (Frente {frente})",
                details_item=f"Alocação de Reserva (Frente {frente})"
            )

        # 4. Iteração sobre os DataFrames extraídos
        for _, row in tqdm(df_frente1.iterrows(), total=df_frente1.shape[0], desc="Processando FRENTE 1"):
            processar_linha(row, frente=1)
            
        for _, row in tqdm(df_frente2.iterrows(), total=df_frente2.shape[0], desc="Processando FRENTE 2"):
            processar_linha(row, frente=2)

        print(f"\n⚠️ Registros rejeitados (Sem equipamento ou sem endereço válido): {rejeitados}")
        print(f"   - Sem equipamento correspondente: {rejeitados_sem_equipamento}")
        print(f"   - Sem titular/endereço refatorado: {rejeitados_sem_destino}")

        # 5. Salva em lote no banco (Status 3 = Reservado)
        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=3, lista_dicionarios=self.equipamentos_alterados)
        
# ==============================================================================
# WRAPPER (A porta de entrada do orquestrador ou terminal)
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_reserva import resetar_saldo_contract_items, carregar_dados_compartilhados

    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de RESERVA")
    print("="*70)


    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)

    app_teste = MigracaoReserva(eng_novo, eng_legado, dados_ram, start_counter=500000)
    app_teste.executar()
