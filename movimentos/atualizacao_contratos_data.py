import os
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm
from datetime import datetime

class AtualizacaoContratosData:
    def __init__(self, engine_new):
        self.engine_new = engine_new
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def executar(self):
        print("\n" + "=" * 70)
        print("⏳ MÓDULO: ALINHAMENTO CRONOLÓGICO DE CONTRATOS E ADITIVOS")
        print("=" * 70)

        print("🧠 Carregando mapas estruturais do banco novo para a RAM...")
        
        # 1️⃣ Busca a data do primeiro movimento real de cada contrato
        query_base_movimentos = """
            SELECT so.contract_id, MIN(soi.created_at) as primeira_data
            FROM service_order_items soi
            INNER JOIN service_orders so ON so.id = soi.service_order_id
            WHERE so.contract_id IS NOT NULL AND so.deleted_at IS NULL
            GROUP BY so.contract_id
        """
        
        # 2️⃣ Busca a estrutura sequencial de eventos/aditivos de cada contrato
        query_eventos = """
            SELECT id as event_id, contract_id, created_at
            FROM contract_events
            WHERE deleted_at IS NULL
            ORDER BY contract_id, created_at ASC, id ASC
        """

        # 3️⃣ Busca o vínculo direto de itens de contrato com seus respectivos contratos
        query_itens = """
            SELECT ci.id as item_id, ce.contract_id
            FROM contract_items ci
            INNER JOIN event_additives ea ON ea.id = ci.event_additive_id
            INNER JOIN contract_events ce ON ce.id = ea.event_id
            WHERE ci.deleted_at IS NULL
        """

        with self.engine_new.connect() as conn:
            df_base = pd.read_sql(text(query_base_movimentos), conn)
            df_events = pd.read_sql(text(query_eventos), conn)
            df_items = pd.read_sql(text(query_itens), conn)

        if df_base.empty:
            print("⚠️ Nenhum movimento de contrato encontrado para reajustar datas.")
            return

        print("⚡ Calculando as linhas do tempo e retrocessos de dias...")
        
        # Converte para datetime para podermos subtrair e somar dias matematicamente
        df_base['primeira_data_dt'] = pd.to_datetime(df_base['primeira_data'])
        df_base['data_base_dt'] = df_base['primeira_data_dt'] - pd.Timedelta(days=1)
        
        # Dicionário rápido de busca: { contract_id: data_base_datetime }
        mapa_contrato_data_base = dict(zip(df_base['contract_id'], df_base['data_base_dt']))

        # Listas que vão acumular os pacotes de Bulk Updates
        updates_contracts = []
        updates_items = []
        updates_events = []
        updates_additives = []

        # ======================================================================
        # 🎯 PROCESSAMENTO DOS CONTRATOS E ITENS (DATA BASE: DIA -1)
        # ======================================================================
        for contract_id, data_base_dt in mapa_contrato_data_base.items():
            data_base_str = data_base_dt.strftime('%Y-%m-%d %H:%M:%S')
            
            # Acumula o update da capa do Contrato
            updates_contracts.append({
                "contract_id": int(contract_id),
                "dt": data_base_str
            })

        # Vincula a data base reduzida aos itens do contrato
        df_items_filtrados = df_items[df_items['contract_id'].isin(mapa_contrato_data_base.keys())]
        for _, row_item in df_items_filtrados.iterrows():
            c_id = row_item['contract_id']
            dt_base_item = mapa_contrato_data_base[c_id].strftime('%Y-%m-%d %H:%M:%S')
            
            updates_items.append({
                "item_id": int(row_item['item_id']),
                "dt": dt_base_item
            })

        # ======================================================================
        # 🎯 PROCESSAMENTO DOS EVENTOS E ADITIVOS (PROGRESSÃO CRONOLÓGICA)
        # ======================================================================
        # Agrupa os eventos por contrato para aplicar a escada de dias (+1, +2...)
        df_events_filtrados = df_events[df_events['contract_id'].isin(mapa_contrato_data_base.keys())]
        
        for contract_id, group in tqdm(df_events_filtrados.groupby('contract_id'), desc="Ordenando Aditivos"):
            data_base_dt = mapa_contrato_data_base[contract_id]
            
            # Como o df_events original já veio ordenado por data e ID do banco,
            # o enumerate vai garantir a ordem exata de entrada de cada aditivo
            for i, (_, row_event) in enumerate(group.iterrows()):
                data_progressiva_dt = data_base_dt + pd.Timedelta(days=i)
                data_progressiva_str = data_progressiva_dt.strftime('%Y-%m-%d %H:%M:%S')
                ev_id = int(row_event['event_id'])

                updates_events.append({
                    "event_id": ev_id,
                    "dt": data_progressiva_str
                })
                
                updates_additives.append({
                    "event_id": ev_id,
                    "dt": data_progressiva_str
                })

        # ======================================================================
        # 🚀 EXECUÇÃO DAS ATUALIZAÇÕES EM MASSA (BULK)
        # ======================================================================
        print(f"\n🚀 Disparando Bulk Updates para as 4 tabelas estruturais...")
        
        with self.engine_new.begin() as conn:
            if updates_contracts:
                conn.execute(
                    text("UPDATE contracts SET created_at = :dt, updated_at = :dt WHERE id = :contract_id"),
                    updates_contracts
                )
                print(f"   ✅ Tabela `contracts` alinhada ({len(updates_contracts)} registros).")

            if updates_items:
                conn.execute(
                    text("UPDATE contract_items SET created_at = :dt, updated_at = :dt WHERE id = :item_id"),
                    updates_items
                )
                print(f"   ✅ Tabela `contract_items` alinhada ({len(updates_items)} registros).")

            if updates_events:
                conn.execute(
                    text("UPDATE contract_events SET created_at = :dt, updated_at = :dt WHERE id = :event_id"),
                    updates_events
                )
                print(f"   ✅ Tabela `contract_events` alinhada ({len(updates_events)} registros).")

            if updates_additives:
                conn.execute(
                    text("UPDATE event_additives SET created_at = :dt, updated_at = :dt WHERE event_id = :event_id"),
                    updates_additives
                )
                print(f"   ✅ Tabela `event_additives` alinhada ({len(updates_additives)} registros).")

        print("🏆 Alinhamento cronológico finalizado com 100% de sucesso!")

# Wrapper oficial de execução externa
def executar(eng_novo, eng_legado):
    app = AtualizacaoContratosData(eng_novo)
    app.executar()