import sys
import pandas as pd
from sqlalchemy import text
from datetime import datetime

# ==============================================================================
# IMPORTAÇÕES (Verifique se todos esses MAPS existem no seu config.config)
# ==============================================================================
from config.config import (
    CLIENTES_BLOQUEADOS, ORGANIZACOES_BLOQUEADAS, MAPPING_ALUCOM, 
    MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, FALSOS_RESERVAS,
    ABBREVIATIONS, MAP_ORGANIZACAO, MAP_TIPO, MAP_STATUS, MAP_EVENT_TYPES
)

from utils.sanetizador import executar_truncate_tabelas, limpar_valor_inteiro, limpar_valor_numerico, ultra_normalizar

TABELAS_CONTRATOS = [
    'contract_recipient_customers',
    'contract_events', 
    'event_additives', 
    'contract_infos',
    'contract_items', 
    'contract_jobs', 
    'contracts'
]

class MigracaoContratos:
    """
    Pipeline ETL Orientado a Objetos para Migração de Contratos e Aditivos.
    Lê de planilhas Excel e realiza UPSERTs com clonagem de histórico no MySQL.
    """

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.agora = datetime.now()
        self.caminho_planilha = './docs/Contratos.xlsx'
        
        self.stats = {
            'contratos_criados': 0, 'contratos_atualizados': 0, 'contratos_ignorados': 0,
            'eventos_criados': 0, 'eventos_ignorados': 0, 'aditivos_criados': 0,
            'itens_criados': 0, 'itens_atualizados': 0, 'jobs_criados': 0, 'jobs_atualizados': 0,
            'infos_criadas': 0, 'infos_atualizadas': 0, 'erros': 0
        }

        # Caches em Memória
        self.customer_cache = {}
        self.contracts_cache = {}
        self.contracts_by_number = {}
        self.events_cache = {}
        self.additives_cache = {}
        self.contract_id_map = {}
        self.additive_lookup = {}
        self.contract_event_counters = {}
        self.ultimo_aditivo_por_contrato = {}

    # ==============================================================================
    # MOTORES DE MATCH E BUSCA (HELPER METHODS)
    # ==============================================================================
    def _validar_conflito_estrito(self, tokens_alvo, tokens_banco):
        num_alvo = {t for t in tokens_alvo if t.isdigit()}
        num_banco = {t for t in tokens_banco if t.isdigit()}
        if num_alvo and num_banco and num_alvo != num_banco:
            return False
        siglas = {'IFCE', 'IFRN', 'IFPB', 'UFC', 'UFSC', 'TRE', 'TRT'}
        for s in siglas:
            if s in tokens_alvo and s not in tokens_banco: return False
            if s in tokens_banco and s not in tokens_alvo: return False
        return True

    def _match_por_tokens(self, nome_planilha_norm):
        stopwords = {'DE', 'DA', 'DO', 'DOS', 'DAS', 'E', 'EM', 'NA', 'NO', 'PARA', 'COM', 'POR', 'O', 'A', 'MUNICIPAL', 'ESTADO', 'MUNICIPIO'}
        tokens_alvo = set(nome_planilha_norm.split())
        tokens_alvo_limpos = tokens_alvo - stopwords
        if not tokens_alvo_limpos: return None

        melhores_candidatos = []
        for chave_banco, dados in self.customer_cache.items():
            tokens_banco = set(chave_banco.split())
            tokens_banco_limpos = tokens_banco - stopwords
            
            if not self._validar_conflito_estrito(tokens_alvo, tokens_banco):
                continue
                
            interseccao = tokens_alvo_limpos.intersection(tokens_banco_limpos)
            len_inter = len(interseccao)

            if 'REGIONAL' in tokens_alvo and ('GESTAO' in tokens_alvo or 'SEGER' in tokens_alvo):
                if 'REGIONAL' in tokens_banco and 'GESTAO' in tokens_banco: return dados
            if 'CANINDE' in tokens_alvo and 'AGRICULTURA' in tokens_alvo:
                if 'CANINDE' in tokens_banco and 'AGRICULTURA' in tokens_banco: return dados

            ratio_alvo = len_inter / len(tokens_alvo_limpos) if len(tokens_alvo_limpos) > 0 else 0
            ratio_banco = len_inter / len(tokens_banco_limpos) if len(tokens_banco_limpos) > 0 else 0
            
            if ratio_alvo >= 0.70 or ratio_banco >= 0.70:
                melhores_candidatos.append((max(ratio_alvo, ratio_banco), dados))

        if melhores_candidatos:
            melhores_candidatos.sort(key=lambda x: x[0], reverse=True)
            return melhores_candidatos[0][1]
        return None

    def _get_hierarchical_customer(self, nome_planilha):
        nome_alvo = ultra_normalizar(nome_planilha)
        if not nome_alvo: 
            print(f"❌ Cliente não localizado: {nome_planilha} (vazio)")
            return None

        if nome_alvo in self.customer_cache:
            d = self.customer_cache[nome_alvo]
            print(f"   ✅ Match (Exato): '{nome_planilha}' -> '{d['debug']}'")
            return d['parent_id'] if d['parent_id'] else d['id']

        match_tokens = self._match_por_tokens(nome_alvo)
        if match_tokens:
            d = match_tokens
            print(f"   ✅ Match (Tokens): '{nome_planilha}' -> '{d['debug']}'")
            return d['parent_id'] if d['parent_id'] else d['id']

        print(f"   ❌ Cliente não localizado: {nome_planilha}")
        return None

    # ==============================================================================
    # CARREGAMENTO DE CACHES DO BANCO DE DADOS
    # ==============================================================================
    def _construir_caches(self, conn):
        print("\n🔍 Carregando dados existentes do banco para memória (Caches)...")
        
        # 1. Cache de Contratos
        res_contracts = conn.execute(text("SELECT id, name, number, organization_id, customer_id FROM contracts")).fetchall()
        for r in res_contracts:
            chave = f"{r[1]}|{r[2]}|{r[3]}"
            dados_cache = {"id": r[0], "customer_id": r[4]}
            self.contracts_cache[chave] = dados_cache
            if r[2] and r[2] != "SEM_NUMERO":
                self.contracts_by_number[r[2]] = dados_cache
        print(f"   📋 {len(self.contracts_cache)} contratos em cache")

        # 2. Cache de Eventos
        res_events = conn.execute(text("SELECT id, contract_id FROM contract_events")).fetchall()
        for r in res_events:
            if r[1] not in self.events_cache:
                self.events_cache[r[1]] = []
            self.events_cache[r[1]].append(r[0])
        print(f"   📅 {len(res_events)} eventos em cache")

        # 3. Cache de Aditivos
        res_additives = conn.execute(text("SELECT id, event_id, contract_event_type_id FROM event_additives")).fetchall()
        for r in res_additives:
            self.additives_cache[f"{r[1]}|{r[2]}"] = r[0]
        print(f"   📝 {len(self.additives_cache)} aditivos em cache")

        # 4. Cache de Clientes (Matches Complexos)
        res_cust = conn.execute(text("""
            SELECT c.id, c.name, c.alias, c.parent_id, a.city, a.alias as addr_alias 
            FROM customers c 
            LEFT JOIN addresses a ON a.addressable_id = c.id AND a.addressable_type = 'customer'
        """)).fetchall()
        
        for r in res_cust:
            info = {'id': r[0], 'parent_id': r[3], 'debug': r[1]}
            combos = [r[1], r[2]]
            if r[1] and r[4]: combos.append(f"{r[1]} {r[4]}")
            if r[1] and r[5]: combos.append(f"{r[1]} {r[5]}")
            for txt in combos:
                norm = ultra_normalizar(txt)
                if norm: self.customer_cache[norm] = info

        print("   🔄 Expandindo cache com abreviações conhecidas...")
        for abbr, full_name in ABBREVIATIONS.items():
            norm_abbr = ultra_normalizar(abbr)
            norm_full = ultra_normalizar(full_name)
            if norm_full in self.customer_cache:
                self.customer_cache[norm_abbr] = self.customer_cache[norm_full]

    # ==============================================================================
    # PROCESSAMENTO DE ENTIDADES (UPSERT)
    # ==============================================================================
    def _processar_contratos(self, conn, df_ex_contract):
        print("\n🔄 Processando Contratos (UPSERT)...")
        for idx, row in df_ex_contract.iterrows():
            if pd.isna(row['CONTRATANTE']) or pd.isna(row['APELIDO_CONTRATO']): 
                self.stats['contratos_ignorados'] += 1
                continue
            
            cust_id = self._get_hierarchical_customer(row['CONTRATANTE'])
            if not cust_id:
                self.stats['contratos_ignorados'] += 1
                continue

            nome_contrato = str(row['APELIDO_CONTRATO']).strip().upper()
            numero_contrato = str(row['NUMERO_CONTRATO']).strip() if pd.notna(row['NUMERO_CONTRATO']) else "SEM_NUMERO"
            org_id = MAP_ORGANIZACAO.get(row['CONTRATADO'], 1115)
            chave_contrato = f"{nome_contrato}|{numero_contrato}|{org_id}"

            contract_info = self.contracts_cache.get(chave_contrato)
            if not contract_info and numero_contrato != "SEM_NUMERO":
                contract_info = self.contracts_by_number.get(numero_contrato)

            dados_contrato = {
                'name': nome_contrato, 'number': numero_contrato,
                'contract_type_id': MAP_TIPO.get(ultra_normalizar(row['TIPO_CONTRATO']), 1),
                'contract_status_id': MAP_STATUS.get(ultra_normalizar(row['STATUS_CONTRATO']), 2),
                'organization_id': org_id, 'customer_id': int(cust_id),
                'object': str(row['OBJETO_DO_CONTRATO'])[:500] if not pd.isna(row['OBJETO_DO_CONTRATO']) else "NÃO INFORMADO",
                'updated_at': self.agora
            }

            if contract_info:
                contract_id = contract_info['id']
                conn.execute(text("""
                    UPDATE contracts 
                    SET contract_type_id = :contract_type_id, contract_status_id = :contract_status_id,
                        customer_id = :customer_id, object = :object, updated_at = :updated_at
                    WHERE id = :id AND number = :number
                """), {**dados_contrato, 'id': contract_id, 'number': numero_contrato})
                self.stats['contratos_atualizados'] += 1
            else:
                dados_contrato['created_at'] = self.agora
                res = conn.execute(text("""
                    INSERT INTO contracts (name, number, contract_type_id, contract_status_id, organization_id, customer_id, object, created_at, updated_at)
                    VALUES (:name, :number, :contract_type_id, :contract_status_id, :organization_id, :customer_id, :object, :created_at, :updated_at)
                """), dados_contrato)
                
                contract_id = res.lastrowid
                novo_cache = {'id': contract_id, 'customer_id': cust_id}
                self.contracts_cache[chave_contrato] = novo_cache
                if numero_contrato != "SEM_NUMERO":
                    self.contracts_by_number[numero_contrato] = novo_cache
                self.stats['contratos_criados'] += 1

            conn.execute(text("INSERT IGNORE INTO contract_recipient_customers (contract_id, customer_id) VALUES (:c_id, :cust_id)"), 
                         {'c_id': int(contract_id), 'cust_id': int(cust_id)})

            self.contract_id_map[nome_contrato] = contract_id

    def _processar_eventos(self, conn, df_ex_events):
        print("\n📅 Processando Eventos e Aditivos (UPSERT + CLONAGEM)...")
        for idx, row in df_ex_events.iterrows():
            if pd.isna(row['CONTRATO']) or pd.isna(row['ID']):
                self.stats['eventos_ignorados'] += 1
                continue
            
            nome_contrato = str(row['CONTRATO']).strip().upper()
            
            if nome_contrato not in self.contract_id_map:
                found = False
                for chave, dados in self.contracts_cache.items():
                    if chave.startswith(nome_contrato + "|"):
                        self.contract_id_map[nome_contrato] = dados['id']
                        found = True
                        break
                if not found:
                    self.stats['eventos_ignorados'] += 1
                    continue

            contract_id = self.contract_id_map[nome_contrato]
            tipo_planilha = ultra_normalizar(row['TIPO']) if pd.notna(row['TIPO']) else 'CADASTRO'
            id_evento_planilha = row['ID']

            if contract_id not in self.contract_event_counters:
                self.contract_event_counters[contract_id] = 0
            
            idx_evento_atual = self.contract_event_counters[contract_id]

            if contract_id in self.events_cache and idx_evento_atual < len(self.events_cache[contract_id]):
                event_id = self.events_cache[contract_id][idx_evento_atual]
            else:
                res = conn.execute(text("INSERT INTO contract_events (contract_id, created_at, updated_at) VALUES (:c_id, :now, :now)"), 
                                   {"c_id": contract_id, "now": self.agora})
                event_id = res.lastrowid
                if contract_id not in self.events_cache:
                    self.events_cache[contract_id] = []
                self.events_cache[contract_id].append(event_id)
                self.stats['eventos_criados'] += 1

            self.contract_event_counters[contract_id] += 1

            tipos_aditivos = [2, 4] if "REAJUSTE" in tipo_planilha and "PRAZO" in tipo_planilha else [MAP_EVENT_TYPES.get(tipo_planilha, 1)]

            for t_id in tipos_aditivos:
                chave_aditivo = f"{event_id}|{t_id}"
                
                if chave_aditivo in self.additives_cache:
                    additive_id = self.additives_cache[chave_aditivo]
                else:
                    res = conn.execute(text("INSERT INTO event_additives (event_id, contract_event_type_id, created_at, updated_at) VALUES (:e_id, :t_id, :now, :now)"), 
                                       {"e_id": event_id, "t_id": t_id, "now": self.agora})
                    additive_id = res.lastrowid
                    self.additives_cache[chave_aditivo] = additive_id
                    self.stats['aditivos_criados'] += 1
                    
                    antigo_additive_id = self.ultimo_aditivo_por_contrato.get(contract_id)
                    if antigo_additive_id:
                        print(f"      📋 Clonando dados do aditivo ({antigo_additive_id}) para o novo ({additive_id})...")
                        conn.execute(text("INSERT INTO contract_infos (event_additive_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, created_at, updated_at) SELECT :novo_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, :now, :now FROM contract_infos WHERE event_additive_id = :antigo_id"), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": self.agora})
                        conn.execute(text("INSERT INTO contract_items (event_additive_id, alias, description, quantity, available_quantity, price, created_at, updated_at) SELECT :novo_id, alias, description, quantity, available_quantity, price, :now, :now FROM contract_items WHERE event_additive_id = :antigo_id"), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": self.agora})
                        conn.execute(text("INSERT INTO contract_jobs (event_additive_id, alias, description, quantity, price, created_at, updated_at) SELECT :novo_id, alias, description, quantity, price, :now, :now FROM contract_jobs WHERE event_additive_id = :antigo_id"), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": self.agora})

                self.ultimo_aditivo_por_contrato[contract_id] = additive_id
                if id_evento_planilha not in self.additive_lookup or t_id in [1, 3, 4, 5]:
                    self.additive_lookup[id_evento_planilha] = additive_id

    def _processar_itens(self, conn, df_ex_itens):
        print("\n📦 Processando Itens (UPSERT)...")
        for _, row in df_ex_itens.iterrows():
            if pd.isna(row['EVENTO']): continue
            aid = self.additive_lookup.get(row['EVENTO'])
            if not aid: continue

            exists = conn.execute(text("SELECT id FROM contract_items WHERE event_additive_id = :aid AND alias = :alias"), 
                                  {"aid": aid, "alias": str(row['APELIDO'])[:100]}).fetchone()

            dados_item = {
                'alias': str(row['APELIDO'])[:100],
                'description': str(row['DESCRICAO'])[:500] if pd.notna(row['DESCRICAO']) else '',
                'quantity': limpar_valor_numerico(row['QUANTIDADE']),
                'available_quantity': limpar_valor_numerico(row['QUANTIDADE']),
                'price': limpar_valor_numerico(row['VALOR_UNITARIO']),
                'updated_at': self.agora
            }

            if exists:
                conn.execute(text("UPDATE contract_items SET description = :description, quantity = :quantity, available_quantity = :available_quantity, price = :price, updated_at = :updated_at WHERE id = :id"), {**dados_item, 'id': exists[0]})
                self.stats['itens_atualizados'] += 1
            else:
                dados_item['event_additive_id'] = aid
                dados_item['created_at'] = self.agora
                conn.execute(text("INSERT INTO contract_items (event_additive_id, alias, description, quantity, available_quantity, price, created_at, updated_at) VALUES (:event_additive_id, :alias, :description, :quantity, :available_quantity, :price, :created_at, :updated_at)"), dados_item)
                self.stats['itens_criados'] += 1

    def _processar_servicos(self, conn, df_ex_jobs):
        print("\n🛠️ Processando Serviços (UPSERT)...")
        for _, row in df_ex_jobs.iterrows():
            if pd.isna(row['EVENTO']): continue
            aid = self.additive_lookup.get(row['EVENTO'])
            if not aid: continue

            exists = conn.execute(text("SELECT id FROM contract_jobs WHERE event_additive_id = :aid AND alias = :alias"), {"aid": aid, "alias": str(row['APELIDO'])[:100]}).fetchone()

            dados_job = {
                'alias': str(row['APELIDO'])[:100],
                'description': str(row['DESCRICAO'])[:500] if pd.notna(row['DESCRICAO']) else '',
                'quantity': limpar_valor_numerico(row['QUANTIDADE']),
                'price': limpar_valor_numerico(row['VALOR_UNITARIO']),
                'updated_at': self.agora
            }

            if exists:
                conn.execute(text("UPDATE contract_jobs SET description = :description, quantity = :quantity, price = :price, updated_at = :updated_at WHERE id = :id"), {**dados_job, 'id': exists[0]})
                self.stats['jobs_atualizados'] += 1
            else:
                dados_job['event_additive_id'] = aid
                dados_job['created_at'] = self.agora
                conn.execute(text("INSERT INTO contract_jobs (event_additive_id, alias, description, quantity, price, created_at, updated_at) VALUES (:event_additive_id, :alias, :description, :quantity, :price, :created_at, :updated_at)"), dados_job)
                self.stats['jobs_criados'] += 1

    def _processar_infos(self, conn, df_ex_infos):
        print("\n📅 Processando Vigências e Valores (UPSERT)...")
        for _, row in df_ex_infos.iterrows():
            if pd.isna(row['EVENTO']): continue
            aid = self.additive_lookup.get(row['EVENTO'])
            if not aid: continue

            exists = conn.execute(text("SELECT id FROM contract_infos WHERE event_additive_id = :aid"), {"aid": aid}).fetchone()

            dados_info = {
                'start_date': row['DATA_INICIAL'] if pd.notna(row['DATA_INICIAL']) else None,
                'end_date': row['DATA_FINAL'] if pd.notna(row['DATA_FINAL']) else None,
                'max_end_date': row['DATA_FINAL_MAXIMA'] if pd.notna(row['DATA_FINAL_MAXIMA']) else None,
                'duration': limpar_valor_inteiro(row['DURAÇÃO (MESES)']),
                'max_duration': limpar_valor_inteiro(row['DURAÇÃO_MAXIMA (MESES)']) or 60,
                'total_amount': limpar_valor_numerico(row['VALOR TOTAL']),
                'updated_at': self.agora
            }

            if exists:
                conn.execute(text("UPDATE contract_infos SET start_date = :start_date, end_date = :end_date, max_end_date = :max_end_date, duration = :duration, max_duration = :max_duration, total_amount = :total_amount, updated_at = :updated_at WHERE id = :id"), {**dados_info, 'id': exists[0]})
                self.stats['infos_atualizadas'] += 1
            else:
                dados_info['event_additive_id'] = aid
                dados_info['created_at'] = self.agora
                conn.execute(text("INSERT INTO contract_infos (event_additive_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, created_at, updated_at) VALUES (:event_additive_id, :start_date, :end_date, :max_end_date, :duration, :max_duration, :total_amount, :created_at, :updated_at)"), dados_info)
                self.stats['infos_criadas'] += 1

    # ==============================================================================
    # ORQUESTRADOR PRINCIPAL DA CLASSE
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 MODO UPSERT - HISTÓRICO CONSOLIDADO (SNAPSTHOTS)")
        print("=" * 80)
        
        try:
            # 🧹 CASO VOCÊ QUEIRA LIMPAR O BANCO E INICIAR DO ZERO, DESCOMENTE A LINHA ABAIXO:
            executar_truncate_tabelas(self.engine_new, TABELAS_CONTRATOS)

            print(f"\n📖 Lendo abas da planilha {self.caminho_planilha}...")
            df_ex_contract = pd.read_excel(self.caminho_planilha, sheet_name='CONTRATOS')
            df_ex_events = pd.read_excel(self.caminho_planilha, sheet_name='EVENTO')
            df_ex_itens = pd.read_excel(self.caminho_planilha, sheet_name='ITENS')
            df_ex_jobs = pd.read_excel(self.caminho_planilha, sheet_name='SERVICOS')
            df_ex_infos = pd.read_excel(self.caminho_planilha, sheet_name='INFORMAÇÕES')

            # TUDO AQUI RODA DENTRO DE UMA ÚNICA TRANSAÇÃO SEGURA!
            with self.engine_new.begin() as conn:
                self._construir_caches(conn)
                self._processar_contratos(conn, df_ex_contract)
                self._processar_eventos(conn, df_ex_events)
                self._processar_itens(conn, df_ex_itens)
                self._processar_servicos(conn, df_ex_jobs)
                self._processar_infos(conn, df_ex_infos)

            print("\n" + "=" * 80)
            print("📊 RELATÓRIO DE MIGRAÇÃO (MODO UPSERT + SNAPSHOTS)")
            print("=" * 80)
            print(f"{'CONTRATOS:':<20} ✅ Criados: {self.stats['contratos_criados']:<5} 🔄 Atualizados: {self.stats['contratos_atualizados']:<5} ⚠️ Ignorados: {self.stats['contratos_ignorados']}")
            print(f"{'EVENTOS:':<20} ✅ Criados: {self.stats['eventos_criados']:<5} ⚠️ Ignorados: {self.stats['eventos_ignorados']}")
            print(f"{'ADITIVOS:':<20} ✅ Criados: {self.stats['aditivos_criados']}")
            print(f"{'ITENS:':<20} ✅ Criados: {self.stats['itens_criados']:<5} 🔄 Atualizados: {self.stats['itens_atualizados']}")
            print(f"{'SERVIÇOS:':<20} ✅ Criados: {self.stats['jobs_criados']:<5} 🔄 Atualizados: {self.stats['jobs_atualizados']}")
            print(f"{'INFORMAÇÕES:':<20} ✅ Criadas: {self.stats['infos_criadas']:<5} 🔄 Atualizadas: {self.stats['infos_atualizadas']}")
            print("=" * 80)
            print("🚀 Migração concluída com sucesso!")

        except Exception as e:
            print(f"❌ Erro crítico: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

# ==============================================================================
# WRAPPER (Ponte para o main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    migrador = MigracaoContratos(eng_novo, eng_legado)
    migrador.executar()