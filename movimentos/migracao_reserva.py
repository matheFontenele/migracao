import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from movimentos.migracao_movimentos import BaseMigracaoMovimento

from movimentos.migracao_movimentos import carregar_dados_compartilhados, resetar_saldo_contract_items

class MigracaoReserva(BaseMigracaoMovimento):
    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=500000):

        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter)
        self.consumir_saldos = False  # 🛡️ Proteção: Reserva não subtrai saldo de contrato

    def calcular_saldo(
        self, contrato_item_id, recipient_id, equipment_id_ref, mov_date,
        item_servico_id_atual, fallback_contract_item_id=None, forcar_extra=False
    ):
        """
        POLIMORFISMO: Substitui a regra do Aluguel. 
        Reservas não consomem saldo e não geram itens extras na tabela.
        """
        return 0, None, int(contrato_item_id) if pd.notna(contrato_item_id) else None

    def _extrair_dados_reserva(self, frente):
        print(f"   📖 Extraindo Frente {frente} de Reservas...")
        
        # O %% escapa o % no SQLAlchemy para não bugar a query
        if frente == 1:
            filtro_where = "ac.nome_razao_social LIKE '%%RESERV%%' AND ac.id != 10487 AND mov.tipo_id IN (1, 7)"
        else:
            filtro_where = "(ac.nome_razao_social NOT LIKE '%%RESERV%%' OR ac.id = 10487) AND mov.tipo_id = 7"

        query = f"""
            SELECT
                eq.numero AS TOMBO, eq.nome AS NOME_EQUIPAMENTO,
                ac.id AS ID_CLIENTE, mov.id as MOVIMENTO_ID, 
                mov.usuario_id, mov.updated_at, mov.deleted_at
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
            WHERE eq.deleted_at IS NULL 
              AND ac.deleted_at IS NULL 
              AND eq.situacao_id IN (1, 15)
              AND {filtro_where}
        """
        
        with self.engine_legado.connect() as conn:
            return pd.read_sql(text(query), conn)

    def executar(self):
        print("\n" + "=" * 70)
        print("📦 MÓDULO: ALOCAÇÃO DE RESERVAS (ESTOQUE E CLIENTES)")
        print("=" * 70)

        # 1. Carrega o mapeamento específico para a Frente 1 (clientes reservados)
        dict_recipient_por_reserved = {}
        dict_endereco_por_reserved = {}
        with self.engine_new.connect() as conn:
            res = conn.execute(text("""
                SELECT addressable_id, id, reserved_customer_id 
                FROM addresses 
                WHERE addressable_type = 'customer' AND reserved_customer_id IS NOT NULL
            """))
            for r in res.mappings():
                res_id = int(r['reserved_customer_id'])
                dict_recipient_por_reserved[res_id] = int(r['addressable_id'])
                dict_endereco_por_reserved[res_id] = int(r['id'])

        # 2. Executa as Extrações (Frente 1 e Frente 2)
        df_frente1 = self._extrair_dados_reserva(frente=1)
        df_frente2 = self._extrair_dados_reserva(frente=2)

        if df_frente1.empty and df_frente2.empty:
            print("⚠️ Nenhum movimento de reserva encontrado nas duas frentes.")
            return

        rejeitados = 0

        # 3. Lógica central de processamento linha a linha
        def processar_linha(row, frente):
            nonlocal rejeitados
            id_final = int(row['MOVIMENTO_ID'])
            tombo = str(row['TOMBO']).strip()
            cliente_id_legado = int(row['ID_CLIENTE'])
            
            equipment_id_ref = self.dados["dict_equip_ref_por_number"].get(tombo)
            if not equipment_id_ref:
                rejeitados += 1
                return

            # Roteamento baseado na Frente
            if frente == 1:
                recipient_id = dict_recipient_por_reserved.get(cliente_id_legado)
                cliente_final = dict_endereco_por_reserved.get(cliente_id_legado)
            else:
                recipient_id = self.dados["dict_cliente_adress"].get(cliente_id_legado)
                cliente_final = self.dados["dict_endereco_por_legacy_client"].get(cliente_id_legado)

            if not recipient_id: 
                rejeitados += 1
                return

            usr_id = int(row['usuario_id']) if pd.notna(row['usuario_id']) and row['usuario_id'] != 0 else 1
            mov_date = row['updated_at'] if pd.notna(row['updated_at']) else self.now

            # Fallback Padrão da Reserva: Puxa o primeiro contrato/item disponível do cliente
            contrato_id_res = self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)

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
                tipo_movimento_id=4, # 4 = ID de Reserva no banco novo
                operation_type='RESERVA',
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

        print(f"\n⚠️ Registros rejeitados (Sem equipamento ou sem endereço): {rejeitados}")

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