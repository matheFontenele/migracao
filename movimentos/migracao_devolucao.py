import os
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino, limpar_codigo

class MigracaoDevolucao(BaseMigracaoMovimento):
    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=800000):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter)
        self.consumir_saldos = False # 🛡️ Proteção Absoluta: Devolução não consome saldos

        # 1. Contadores Inteligentes para Shipments
        with self.engine_new.connect() as conn:

            # 🎯 Extrai todos os IDs de Organização do config.py
            org_ids = [str(org['id']) for org in ENDERECOS_BASES]
            org_ids_sql = "(" + ", ".join(org_ids) + ")"
            
            # Busca os Endereços reais (PK) de TODAS as bases listadas
            query_enderecos = f"""
                SELECT addressable_id, MIN(id) as address_id 
                FROM addresses 
                WHERE addressable_type = 'organization' 
                AND addressable_id IN {org_ids_sql}
                GROUP BY addressable_id
            """
            result_ends = conn.execute(text(query_enderecos)).fetchall()
            
            # Dicionário em memória: {ID_DA_ORG: ID_DO_ENDERECO}
            self.dict_enderecos_base_org = {row.addressable_id: row.address_id for row in result_ends}
            
    def calcular_saldo(self, *args, **kwargs):
        """
        POLIMORFISMO: Devolução ignora totalmente as regras comerciais de saldo ou excedente
        """
        return 0, None, int(args[0]) if pd.notna(args[0]) else None

    def salvar_shipments_banco(self):
        if not self.shipments_mestre:
            return
            
        print(f"\n🚀 Persistindo guias de transporte no MySQL...")
        with self.engine_new.begin() as conn:
            pd.DataFrame(self.shipments_mestre).to_sql("shipments", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_movements_mestre).to_sql("shipment_movements", con=conn, if_exists="append", index=False)
            pd.DataFrame(self.shipment_items_mestre).to_sql("shipment_items", con=conn, if_exists="append", index=False)
        print("   ✅ Shipments salvos com sucesso.")

    def _extrair_dados_devolucao(self):
        print("   📖 Extraindo Histórico Completo (Passado e Presente) em Query Única...")
        
        # ==================================================================
        # 1. QUERY MESTRA: Cruzamento de histórico usando CTE e RN 1 e RN 2
        # ==================================================================
        query = """
            WITH HistoricoMovimentos AS (
                SELECT
                    movi.equipamento_id,
                    mov.id AS movimento_id,
                    mov.data AS data_movimento,
                    mov.updated_at,
                    mov.deleted_at,
                    mov.cliente_id,
                    ac.orgao_id,
                    mov.usuario_id,
                    mov.tipo_id AS tipo_mov_id,
                    -- Numera os movimentos de cada equipamento, do mais recente (1) pro mais antigo (N)
                    ROW_NUMBER() OVER(PARTITION BY movi.equipamento_id ORDER BY COALESCE(mov.data, '1900-01-01') DESC, mov.id DESC) as rn
                FROM aluguel_movimento mov
                INNER JOIN aluguel_movimento_itens movi ON mov.id = movi.movimento_id
                LEFT JOIN aluguel_clientes ac ON mov.cliente_id = ac.id
                WHERE mov.deleted_at IS NULL
                  AND movi.deleted_at IS NULL
            )
            SELECT
                eq.id AS equipamento_id,
                eq.numero AS TOMBO,
                eq.nome AS NOME_EQUIPAMENTO,
                
                -- 👇 FASE 2: DADOS DO PRESENTE (A DEVOLUÇÃO / rn = 1)
                dev.movimento_id AS DEV_MOV_ID,
                COALESCE(dev.updated_at, dev.data_movimento) AS DEV_DATA,
                dev.deleted_at AS DEV_DEL,
                dev.cliente_id AS DEV_CLIENTE_ID,
                COALESCE(NULLIF(dev.usuario_id, 0), 1) AS DEV_USR_ID,

                -- 👇 FASE 1: DADOS DO PASSADO (O ALUGUEL ORIGINAL / rn = 2)
                alu.movimento_id AS ORIG_MOV_ID,
                COALESCE(alu.updated_at, alu.data_movimento) AS ORIG_DATA,
                alu.deleted_at AS ORIG_DEL,
                alu.cliente_id AS ORIG_CLIENTE_ID,
                alu.orgao_id AS ORIG_ORGAO_ID,
                COALESCE(NULLIF(alu.usuario_id, 0), 1) AS ORIG_USR_ID,
                alu.tipo_mov_id AS ORIG_TIPO_LEGADO
            FROM aluguel_equipamentos eq
            -- Cruzamento 1: Pega o último movimento absoluto (A Devolução)
            INNER JOIN HistoricoMovimentos dev
                ON eq.id = dev.equipamento_id
                AND dev.rn = 1
            -- Cruzamento 2: Pega o movimento imediatamente anterior (A Saída/Aluguel)
            LEFT JOIN HistoricoMovimentos alu
                ON eq.id = alu.equipamento_id
                AND alu.rn = 2
            WHERE eq.deleted_at IS NULL
              AND eq.situacao_id = 14
              AND dev.tipo_mov_id IN (2);
        """
        
        with self.engine_legado.connect() as conn:
            df_final = pd.read_sql(text(query), conn)

        return df_final

    def executar(self):
        print("\n" + "=" * 70)
        print("📦 MÓDULO: DEVOLUÇÃO (RECONSTRUÇÃO BIFÁSICA SIMPLIFICADA)")
        print("=" * 70)

        # 1. Extrai o DataFrame estruturado
        df_devolucoes = self._extrair_dados_devolucao()
        if df_devolucoes.empty:
            print("⚠️ Nenhuma Devolução válida com status 14 encontrada.")
            return

        # 2. Busca os IDs do banco novo baseados nos tombos extraídos
        tombos = df_devolucoes['TOMBO'].dropna().unique().tolist()
        dict_equip_novo = self.buscar_equipamentos_novo_por_tombo(tombos)
        print(f"   ✅ {len(dict_equip_novo)} equipamentos validados no banco novo.")

        rejeitados = 0

        # 3. Iteração Principal
        for _, row in tqdm(df_devolucoes.iterrows(), total=df_devolucoes.shape[0], desc="Processando Devoluções"):
            tombo = limpar_codigo(row['TOMBO'])
            equip_id_novo = dict_equip_novo.get(tombo)
            
            if not equip_id_novo:
                rejeitados += 1
                continue

            # =========================================================
            # 🎯 RESOLVENDO O CLIENTE E A ORGANIZAÇÃO (Roteamento)
            # =========================================================
            # Prioriza o cliente da transação original (Passado) para amarrar os contratos corretos
            if pd.notna(row.get('ORIG_CLIENTE_ID')):
                cli_legado_id = int(row['ORIG_CLIENTE_ID'])
            else:
                cli_legado_id = int(row['DEV_CLIENTE_ID']) if pd.notna(row.get('DEV_CLIENTE_ID')) else 0
                
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            cliente_final_address = self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id)
            
            if not recipient_id:
                rejeitados += 1
                continue

            # =========================================================
            # ROTEAMENTO INTELIGENTE FISICO (Qual base vai receber o frete?)
            # =========================================================
            orgao_id_legado = row['ORIG_ORGAO_ID'] if pd.notna(row.get('ORIG_ORGAO_ID')) else None
            org_id_destino = descobrir_id_organizacao_destino(orgao_id_legado)
            endereco_base_id = self.dict_enderecos_base_org.get(org_id_destino)
            
            # Fallback de segurança (Se falhar, vai para a Base Principal - 1115)
            if not endereco_base_id:
                endereco_base_id = self.dict_enderecos_base_org.get(1115, 1) 
                org_id_destino = 1115

            # ==================================================================
            # 🎯 REGRA DE DEVOLUÇÃO (Sem Parquets, usa o que tá ativo pro cliente)
            # ==================================================================
            contrato_id_ativo = self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            item_id_ativo = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)

            # =========================================================
            # 🕰️ FASE 1: RECONSTRUIR O PASSADO (FORÇANDO COMO ALUGUEL)
            # =========================================================
            # Executa apenas se o banco legado encontrou um movimento anterior
            if pd.notna(row.get('ORIG_MOV_ID')):
                id_mov_origem = int(row['ORIG_MOV_ID'])
                dt_origem = row['ORIG_DATA']
                usr_origem = int(row['ORIG_USR_ID'])

                self.registrar_movimento(
                    id_final=id_mov_origem,
                    recipient_id=recipient_id,
                    cliente_final_address_id=cliente_final_address,
                    usuario_id=usr_origem,
                    mov_date=dt_origem,
                    deleted_at_mov=row['ORIG_DEL'] if pd.notna(row['ORIG_DEL']) else None,

                    contrato_id=contrato_id_ativo,
                    contrato_item_id=item_id_ativo,
                    equipment_id_ref=equip_id_novo,
                    type_id_ref=None,
                    product_id_ref=None,
                    
                    status_shipment=2,
                    tipo_movimento_id=1,  # 👈 Força ser 1 (Aluguel)
                    operation_type='ALUGUEL', # 👈 Força ser Aluguel
                    status_equipment_id=2, 
                    history_reason='SHIPPING_CONFIRMED_RENT',
                    
                    is_exchange=False,
                    forcar_extra=False,
                    
                    organization_id=org_id_destino,
                    alias_movimento=row['NOME_EQUIPAMENTO'],
                    details_capa="Migração (Reconstrução): Aluguel Histórico",
                    details_item="Alocado no Cliente (Histórico Anterior à Devolução)"
                )

            # =========================================================
            # 📦 FASE 2: REGISTRAR A DEVOLUÇÃO (O PRESENTE)
            # =========================================================
            id_mov_dev = int(row['DEV_MOV_ID'])
            dt_dev = row['DEV_DATA']
            usr_dev = int(row['DEV_USR_ID'])

            self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id, 
                cliente_final_address_id=endereco_base_id,
                usuario_id=usr_dev,
                mov_date=dt_dev,
                deleted_at_mov=row['DEV_DEL'] if pd.notna(row['DEV_DEL']) else None,
                
                contrato_id=None,
                contrato_item_id=None,
                equipment_id_ref=equip_id_novo,
                type_id_ref=None,
                product_id_ref=None,
                
                status_equipment_id=8,
                history_reason='RECEIPT_CONFIRMED_RETURN',
                
                status_shipment=1, 
                tipo_movimento_id=3,
                operation_type='DEVOLUCAO',
                
                is_exchange=False,
                forcar_extra=False,
                
                organization_id=org_id_destino,
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa="Migração: Devolução",
                details_item="Retorno para a Base"
            )

        # ==================================================================
        # FINALIZAÇÃO: SALVAR TUDO
        # ==================================================================
        if rejeitados > 0:
            print(f"\n⚠️ Equipamentos rejeitados (Não encontrados no banco novo ou sem cliente): {rejeitados}")

        self.salvar_movimentos_banco()
        # Coloca a máquina como Inativa/Manutenção no parque, conforme o 14 original
        self.atualizar_equipamentos_banco(id_status_equipamento=8, lista_dicionarios=self.equipamentos_alterados)

# ==============================================================================
# WRAPPER 
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados

    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de DEVOLUÇÃO")
    print("="*70)

    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)

    app_teste = MigracaoDevolucao(eng_novo, eng_legado, dados_ram, start_counter=1)
    app_teste.executar()