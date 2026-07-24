import sys
import re
import sqlalchemy as sa
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime

from utils.sanetizador import executar_truncate_tabelas, limpar_valor_inteiro, limpar_valor_numerico, ultra_normalizar
from utils.mapeador import descobrir_id_organizacao

# Lista de tabelas a serem utilizadas
TABELAS_CONTRATOS = [
    'contracts', 
    'contract_items', 
    'contract_infos',
    'contract_jobs', 
    'event_additives', 
    'contract_events', 
    'contract_recipient_customers'
]

# DICIONARIOS DE/PARA
ABBREVIATIONS = {
    'MT': 'MINISTÉRIO DOS TRANSPORTES - DF',
    'ESPCEX': 'ESCOLA PREPARATÓRIA DE CADETES DO EXÉRCITO',
    'FUNASA': 'FUNDAÇÃO NACIONAL DE SAÚDE',
    '20º RCB': '20º REGIMENTO DE CAVALARIA BLINDADO - CAMPO GRANDE',
    '20 RCB': '20º REGIMENTO DE CAVALARIA BLINDADO - CAMPO GRANDE',
    'CRN1': 'CONSELHO REGIONAL DE NUTRIÇÃO 1ª REGIÃO (CRN-1)',
    'CLA': 'COMANDO DA AERONÁUTICA - CENTRO DE LANÇAMENTO DE ALCÂNTARA - BASE SÃO LUIS',
    'CREF': 'CONSELHO REGIONAL DE EDUCAÇÃO FISICA DA 3º REGIÃO - SC',
    'COREN DF': 'CONSELHO REGIONAL DE ENFERMAGEM DO DISTRITO FEDERAL',
    'CINDACTA I': 'MINISTÉRIO DA DEFESA - CINDACTA I - GRUPAMENTO DE APOIO - DF',
    'CISNORDESTE SC': 'CONSÓRCIO INTERFEDERATIVO DE SAÚDE DO NORDESTE DE SANTA CATARINA',
    'CISNORDESTE': 'CONSÓRCIO INTERFEDERATIVO DE SAÚDE DO NORDESTE DE SANTA CATARINA',
    'ALECE': 'ASSEMBLEIA LEGISLATIVA DO ESTADO DO CEARÁ',
    'CMFOR': 'COMANDO DA MARINHA EM FORTALEZA',
    'EAMCE': 'ESCOLA DE APRENDIZES-MARINHEIROS DO CEARÁ',
    'UFC': 'UNIVERSIDADE FEDERAL DO CEARÁ',
    'UFSC': 'UNIVERSIDADE FEDERAL DE SANTA CATARINA - UFSC',
    'IFCE': 'INSTITUTO FEDERAL DE EDUCAÇÃO E TECNOLOGIA - IFCE',
    '10A REGIAO MILITAR': 'COMANDO DA 10ª REGIÃO MILITAR - FORTALEZA',
    '10ª REGIAO MILITAR': 'COMANDO DA 10ª REGIÃO MILITAR - FORTALEZA',
    'COMANDO DA 10A REGIAO MILITAR': 'COMANDO DA 10ª REGIÃO MILITAR - FORTALEZA',
    'COMANDO DA 10ª REGIAO MILITAR': 'COMANDO DA 10ª REGIÃO MILITAR - FORTALEZA',
    'ANA KALINCA': 'ANA KALINCA',
    'COMERCIAL DE MEDICAMENTOS CAVALCANTE LTDA - FARMÁCIA PREMIUM': 'COMERCIAL DE MEDICAMENTOS CAVALCANTE LTDA - FARMÁCIA PREMIUM',
    'H F DA ROCHA COMERCIO SERVIÇOS': 'HF DA ROCHA COMÉRCIO E SERVIÇOS DE INFORMÁTICA',
    'SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA−SEMUSC': 'PREFEITURA MUNICIPAL DE SAO LUIS',
    'MARANHÃO PARCEIRIAS - MAPA': 'MARANHÃO PARCERIAS S.A - SÃO LUÍS',
    'CONSELHO FEDERAL DE FARMÁCIA': 'CONSELHO FEDERAL DE FARMCIA - DF',
    'TRIBUNAL REGIONAL ELEITORAL O RIO DE JANEIRO': 'TRIBUNAL REGIONAL ELEITORAL DO RIO DE JANEIRO - TRE - RIO DE JANEIRO',
    'JOANA D ARC CLAUDIO BRASIL DOD': 'JOANA DARC CLAUDIO BRASIL DODO',
    'ACG CONSTRUÇÕES E CONSERVAÇÃO AMBIENTAL LTDA': 'ACG CONSTRUÇÕES E CONSERVAÇÃO AMBIENTAL LTDA',
    'GOVERNO MUNICIPAL DE URUOCA - FUNDO MUNICIPAL DE SAÚDE': 'SEC. MUNICIPAL DA SAÚDE - URUOCA',
    'GOVERNO MUNICIPAL DE URUOCA - FUNDO MUNICIPAL DE EDUCAÇÃO': 'SEC. MUNICIPAL DA EDUCAÇÃO - FUNDEB - URUOCA',
    'GOVERNO MUNICIPAL DE URUOCA - FUNDO MUNICIPAL DE ASSISTENCIA SOCIAL E CIDADANIA': 'SEC. DESENVOLVIMENTO SOCIAL, TRABALHO, EMPREENDORISMO E RENDA - URUOCA',
    'ESCRITORIO DE REPRESENTAÇÃO DO MINISTÉRIO DAS RELAÇÕES EXTERIORES': 'MINISTÉRIO DE RELAÇÕES EXTERIORES - SP',
}
MAP_EVENT_TYPES = {
    'CADASTRO': 1,
    'ADITIVO DE PRAZO': 2,
    'ADITIVO DE QUANTIDADE': 3,
    'ADITIVO DE REAJUSTE': 4,
    'ADITIVO DE SUPRESSAO': 5,
    'ADITIVO MODIFICACAO DE ITEM': 6,
    'APOSTILAMENTO': 7
}
TERMOS_EVENT_TYPES = [
    ('PRAZO', 2),
    ('QUANTIDADE', 3),
    ('REAJUSTE', 4),
    ('SUPRESSAO', 5),
    ('MODIFICACAO DE ITEM', 6),
    ('APOSTILAMENTO', 7),
]
TIPOS_ADITIVOS_COM_DADOS = {1, 3, 4, 5, 6}
MAP_STATUS = {'AGUARDANDO INICIO': 1, 'EM ANDAMENTO': 2, 'ENCERRADO': 3}
MAP_TIPO = {'LICITACAO': 1, 'PESSOA JURIDICA': 2, 'PESSOA FISICA': 3}
MAP_ORGANIZACAO = {'ALUCOM': 1115, 'MOREIA': 1122, 'IP': 1311, 'AS SISTEMAS': 1378}

class MigracaoContratos:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.caminho_planilha = './docs/Contratos.xlsx'

        self.stats = {
            'contratos_criados': 0, 'contratos_atualizados': 0, 'contratos_ignorados': 0,
            'eventos_criados': 0, 'eventos_ignorados': 0, 'aditivos_criados': 0,
            'itens_criados': 0, 'itens_atualizados': 0, 'jobs_criados': 0, 'jobs_atualizados': 0,
            'infos_criadas': 0, 'infos_atualizadas': 0, 'erros': 0
        }

        # Caches em Memória
        self.dict_legacy_to_customer = {}
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
    # CARREGAMENTO DE CACHES DO BANCO DE DADOS
    # ==============================================================================
    def _construir_caches(self, conn):
        print("\n🔍 Carregando dados existentes do banco para memória (Caches)...")
        
        # 1. Cache de Contratos
        res_contracts = conn.execute(text("SELECT id, name, number, organization_id, customer_id FROM contracts")).fetchall()
        for r in res_contracts:
            nome_contrato_banco = str(r[1]).strip().upper()
            chave = f"{nome_contrato_banco}|{r[4]}" 
            chave_simples = f"{nome_contrato_banco}"
            
            dados_cache = {"id": r[0], "customer_id": r[4]}
            self.contracts_cache[chave] = dados_cache
            self.contracts_cache[chave_simples] = dados_cache
            
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
        print("   🔍 Mapeando Hierarquia de Clientes e IDs Legados...")
        query_clientes = text("""
            SELECT 
                cup.id AS id_pai, cu.id AS id_cliente, cu.alias AS cliente_nome,
                cu.name AS razao_social, ad.legacy_customer_id AS id_legado,
                ad.alias AS endereco_nome, ad.city AS cidade
            FROM customers cu
            LEFT JOIN customers cup ON cu.parent_id = cup.id
            LEFT JOIN addresses ad ON ad.addressable_id = cu.id AND ad.addressable_type = 'customer'
        """)
        res_cust = conn.execute(query_clientes).fetchall()
        
        for r in res_cust:
            id_pai, id_cliente, cliente_nome, razao_social, id_legado, endereco_nome, cidade = r
            info = {'id': id_cliente, 'parent_id': id_pai, 'debug': cliente_nome}
            
            if id_legado:
                self.dict_legacy_to_customer[int(id_legado)] = info

            combos = [cliente_nome, razao_social, endereco_nome]
            if cliente_nome and cidade: combos.append(f"{cliente_nome} {cidade}")
            if endereco_nome and cidade: combos.append(f"{endereco_nome} {cidade}")
            
            for txt in combos:
                if txt:
                    norm = ultra_normalizar(txt)
                    if norm: self.customer_cache[norm] = info
                    
        print(f"   👥 {len(self.dict_legacy_to_customer)} IDs legados atrelados e {len(self.customer_cache)} variações de nomes em cache")

    # ==============================================================================
    # MOTORES DE MATCH E BUSCA (HELPER METHODS)
    # ==============================================================================
    @staticmethod
    def _normalizar_token_ordinal(token):
        match = re.fullmatch(r'(\d+)[AO]', token)
        if match:
            return match.group(1)
        return token

    def _validar_conflito_estrito(self, tokens_alvo, tokens_banco):
        # 1. Validação de Números (Ex: 10ª Região vs 11ª Região)
        num_alvo = {t for t in tokens_alvo if t.isdigit()}
        num_banco = {t for t in tokens_banco if t.isdigit()}
        if num_alvo and num_banco and num_alvo != num_banco:
            return False
            
        # 2. 🎯 TRAVA DE NATUREZA: Impede cross-match entre tipos diferentes de entidades
        # Uma Universidade NUNCA pode dar match com uma Superintendência (ex: UFSC x Receita Federal)
        naturezas = {'UNIVERSIDADE', 'SUPERINTENDENCIA', 'CONSELHO', 'TRIBUNAL', 'COMANDO', 'PREFEITURA'}
        nat_alvo = naturezas.intersection(tokens_alvo)
        nat_banco = naturezas.intersection(tokens_banco)
        
        # Se os dois possuem alguma palavra de natureza, eles PRECISAM concordar na natureza
        if nat_alvo and nat_banco and not nat_alvo.intersection(nat_banco):
            return False

        # 3. Validação de Siglas (Permite flexibilidade se um dos lados não tiver a sigla)
        siglas = {'IFCE', 'IFRN', 'IFPB', 'UFC', 'UFSC', 'TRE', 'TRT'}
        siglas_alvo = siglas.intersection(tokens_alvo)
        siglas_banco = siglas.intersection(tokens_banco)
        
        # Só proíbe se os dois lados tem uma sigla, e elas são conflitantes (Ex: UFC x UFSC)
        if siglas_alvo and siglas_banco and not siglas_alvo.intersection(siglas_banco): 
            return False
            
        return True

    def _match_por_tokens(self, nome_planilha_norm):
        stopwords = {'DE', 'DA', 'DO', 'DOS', 'DAS', 'E', 'EM', 'NA', 'NO', 'PARA', 'COM', 'POR', 'O', 'A', 'MUNICIPAL', 'ESTADO', 'MUNICIPIO'}
        tokens_alvo = {self._normalizar_token_ordinal(t) for t in nome_planilha_norm.split()}
        tokens_alvo_limpos = tokens_alvo - stopwords
        if not tokens_alvo_limpos: return None

        melhores_candidatos = []
        for chave_banco, dados in self.customer_cache.items():
            tokens_banco = {self._normalizar_token_ordinal(t) for t in chave_banco.split()}
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
        nome_bruto = str(nome_planilha).strip().upper()
        
        if nome_bruto in ABBREVIATIONS:
            nome_bruto = ABBREVIATIONS[nome_bruto]
            
        nome_alvo = ultra_normalizar(nome_bruto)
        if not nome_alvo: return None

        if nome_alvo in self.customer_cache:
            return self.customer_cache[nome_alvo]
        
        return self._match_por_tokens(nome_alvo)

    # ==============================================================================
    # PROCESSAMENTO DE ENTIDADES (UPSERT)
    # ==============================================================================
    def _resolver_tipos_evento(self, tipo_planilha):
        tipo_normalizado = ultra_normalizar(tipo_planilha)

        if not tipo_normalizado: return [MAP_EVENT_TYPES['CADASTRO']]
        if tipo_normalizado in MAP_EVENT_TYPES: return [MAP_EVENT_TYPES[tipo_normalizado]]

        tipos = [event_type_id for termo, event_type_id in TERMOS_EVENT_TYPES if termo in tipo_normalizado]
        if tipos: return tipos

        print(f"   ⚠️ Tipo de evento não mapeado: '{tipo_planilha}'. Usando CADASTRO.")
        return [MAP_EVENT_TYPES['CADASTRO']]

    def _processar_contratos(self, conn, df_ex_contract):
        print("\n🔄 Processando Contratos (UPSERT)...")
        for idx, row in df_ex_contract.iterrows():
            if pd.isna(row['CONTRATANTE']) or pd.isna(row['APELIDO_CONTRATO']): 
                self.stats['contratos_ignorados'] += 1
                continue

            id_contrato_excel = limpar_valor_inteiro(row.get('ID'))
            if id_contrato_excel == 0:
                self.stats['contratos_ignorados'] += 1
                continue

            cust_info = None

            # 1. Tenta pelo ID Legado
            for col_id in ['CLIENTE_ID', 'ID_CLIENTE', 'LEGACY_CUSTOMER_ID', 'ID_LEGADO']:
                if col_id in row and pd.notna(row[col_id]):
                    legacy_id = limpar_valor_inteiro(row[col_id])
                    if legacy_id in self.dict_legacy_to_customer:
                        cust_info = self.dict_legacy_to_customer[legacy_id]
                        break
            
            # 2. Tenta pelos nomes
            if not cust_info:
                cust_info = self._get_hierarchical_customer(row['CONTRATANTE'])

            if not cust_info:
                self.stats['contratos_ignorados'] += 1
                continue

            # Resolve hierarquia
            cust_id = cust_info['parent_id'] if cust_info['parent_id'] else cust_info['id']
            
            # ==================================================================
            # GRAVAÇÃO DO CONTRATO
            # ==================================================================
            nome_contrato = str(row['APELIDO_CONTRATO']).strip().upper()
            numero_original = str(row['NUMERO_CONTRATO']).strip() if pd.notna(row['NUMERO_CONTRATO']) else "SEM_NUMERO"
            numero_contrato = numero_original if numero_original != "SEM_NUMERO" else f"SEM_NUMERO ({nome_contrato})"[:255]

            org_id = MAP_ORGANIZACAO.get(row['CONTRATADO'], 1115)
            
            chave_contrato = f"{nome_contrato}|{cust_id}"
            chave_simples = f"{nome_contrato}"

            contract_info = self.contracts_cache.get(chave_contrato) or self.contracts_cache.get(chave_simples)

            dados_contrato = {
                "id": id_contrato_excel,
                'name': nome_contrato,
                'number': numero_contrato,
                'contract_type_id': MAP_TIPO.get(ultra_normalizar(row['TIPO_CONTRATO']), 1),
                'contract_status_id': MAP_STATUS.get(ultra_normalizar(row['STATUS_CONTRATO']), 2),
                'organization_id': org_id,
                'customer_id': int(cust_id),
                'object': str(row['OBJETO_DO_CONTRATO'])[:500] if not pd.isna(row['OBJETO_DO_CONTRATO']) else "NÃO INFORMADO",
                'updated_at': self.now
            }

            if contract_info:
                contract_id = contract_info['id']
                conn.execute(text("""
                    UPDATE contracts 
                    SET contract_type_id = :contract_type_id, contract_status_id = :contract_status_id,
                        customer_id = :customer_id, object = :object, updated_at = :updated_at, number = :number
                    WHERE id = :id
                """), {**dados_contrato, 'id': contract_id})
                self.stats['contratos_atualizados'] += 1
            else:
                dados_contrato['created_at'] = self.now
                res = conn.execute(text("""
                    INSERT INTO contracts (id, name, number, contract_type_id, contract_status_id, organization_id, customer_id, object, created_at, updated_at)
                    VALUES (:id, :name, :number, :contract_type_id, :contract_status_id, :organization_id, :customer_id, :object, :created_at, :updated_at)
                """), dados_contrato)
                
                contract_id = id_contrato_excel
                
                novo_cache = {'id': contract_id, 'customer_id': cust_id}
                self.contracts_cache[chave_contrato] = novo_cache
                self.contracts_cache[chave_simples] = novo_cache
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
            contract_id = self.contract_id_map.get(nome_contrato)
            
            if not contract_id:
                cache_info = self.contracts_cache.get(nome_contrato)
                if cache_info:
                    contract_id = cache_info['id']
                    self.contract_id_map[nome_contrato] = contract_id
            
            if not contract_id:
                self.stats['eventos_ignorados'] += 1
                continue

            id_evento_planilha = row['ID']

            if contract_id not in self.contract_event_counters: self.contract_event_counters[contract_id] = 0
            idx_evento_atual = self.contract_event_counters[contract_id]

            if contract_id in self.events_cache and idx_evento_atual < len(self.events_cache[contract_id]):
                event_id = self.events_cache[contract_id][idx_evento_atual]
            else:
                res = conn.execute(text("INSERT INTO contract_events (contract_id, created_at, updated_at) VALUES (:c_id, :now, :now)"), 
                                   {"c_id": contract_id, "now": self.now})
                event_id = res.lastrowid
                if contract_id not in self.events_cache: self.events_cache[contract_id] = []
                self.events_cache[contract_id].append(event_id)
                self.stats['eventos_criados'] += 1

            self.contract_event_counters[contract_id] += 1
            tipos_aditivos = self._resolver_tipos_evento(row['TIPO'])

            for t_id in tipos_aditivos:
                chave_aditivo = f"{event_id}|{t_id}"
                
                if chave_aditivo in self.additives_cache:
                    additive_id = self.additives_cache[chave_aditivo]
                else:
                    res = conn.execute(text("INSERT INTO event_additives (event_id, contract_event_type_id, created_at, updated_at) VALUES (:e_id, :t_id, :now, :now)"), 
                                       {"e_id": event_id, "t_id": t_id, "now": self.now})
                    additive_id = res.lastrowid
                    self.additives_cache[chave_aditivo] = additive_id
                    self.stats['aditivos_criados'] += 1
                    
                    antigo_additive_id = self.ultimo_aditivo_por_contrato.get(contract_id)
                    if antigo_additive_id:
                        conn.execute(text("INSERT INTO contract_infos (event_additive_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, created_at, updated_at) SELECT :novo_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, :now, :now FROM contract_infos WHERE event_additive_id = :antigo_id"), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": self.now})
                        conn.execute(text("INSERT INTO contract_items (event_additive_id, alias, description, quantity, available_quantity, price, created_at, updated_at) SELECT :novo_id, alias, description, quantity, available_quantity, price, :now, :now FROM contract_items WHERE event_additive_id = :antigo_id"), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": self.now})
                        conn.execute(text("INSERT INTO contract_jobs (event_additive_id, alias, description, quantity, price, created_at, updated_at) SELECT :novo_id, alias, description, quantity, price, :now, :now FROM contract_jobs WHERE event_additive_id = :antigo_id"), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": self.now})

                self.ultimo_aditivo_por_contrato[contract_id] = additive_id
                if id_evento_planilha not in self.additive_lookup or t_id in TIPOS_ADITIVOS_COM_DADOS:
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
                'updated_at': self.now
            }

            if exists:
                conn.execute(text("UPDATE contract_items SET description = :description, quantity = :quantity, available_quantity = :available_quantity, price = :price, updated_at = :updated_at WHERE id = :id"), {**dados_item, 'id': exists[0]})
                self.stats['itens_atualizados'] += 1
            else:
                dados_item['event_additive_id'] = aid
                dados_item['created_at'] = self.now
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
                'updated_at': self.now
            }

            if exists:
                conn.execute(text("UPDATE contract_jobs SET description = :description, quantity = :quantity, price = :price, updated_at = :updated_at WHERE id = :id"), {**dados_job, 'id': exists[0]})
                self.stats['jobs_atualizados'] += 1
            else:
                dados_job['event_additive_id'] = aid
                dados_job['created_at'] = self.now
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
                'updated_at': self.now
            }

            if exists:
                conn.execute(text("UPDATE contract_infos SET start_date = :start_date, end_date = :end_date, max_end_date = :max_end_date, duration = :duration, max_duration = :max_duration, total_amount = :total_amount, updated_at = :updated_at WHERE id = :id"), {**dados_info, 'id': exists[0]})
                self.stats['infos_atualizadas'] += 1
            else:
                dados_info['event_additive_id'] = aid
                dados_info['created_at'] = self.now
                conn.execute(text("INSERT INTO contract_infos (event_additive_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, created_at, updated_at) VALUES (:event_additive_id, :start_date, :end_date, :max_end_date, :duration, :max_duration, :total_amount, :created_at, :updated_at)"), dados_info)
                self.stats['infos_criadas'] += 1

    # ==============================================================================
    # ORQUESTRADOR PRINCIPAL DA CLASSE
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 MODO UPSERT - HISTÓRICO CONSOLIDADO (SNAPSHOTS)")
        print("=" * 80)
        
        try:
            executar_truncate_tabelas(self.engine_new, TABELAS_CONTRATOS)

            print(f"\n📖 Lendo abas da planilha {self.caminho_planilha}...")
            df_ex_contract = pd.read_excel(self.caminho_planilha, sheet_name='CONTRATOS')
            df_ex_events = pd.read_excel(self.caminho_planilha, sheet_name='EVENTO')
            df_ex_itens = pd.read_excel(self.caminho_planilha, sheet_name='ITENS')
            df_ex_jobs = pd.read_excel(self.caminho_planilha, sheet_name='SERVICOS')
            df_ex_infos = pd.read_excel(self.caminho_planilha, sheet_name='INFORMAÇÕES')

            with self.engine_new.begin() as conn:
                self._construir_caches(conn)
                self._processar_contratos(conn, df_ex_contract)
                self._processar_eventos(conn, df_ex_events)
                self._processar_itens(conn, df_ex_itens)
                self._processar_servicos(conn, df_ex_jobs)
                self._processar_infos(conn, df_ex_infos)

            print("\n" + "=" * 80)
            print("📊 RELATÓRIO DE MIGRAÇÃO")
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
# WRAPPER
# ==============================================================================
def executar(eng_novo, eng_legado):
    migrador = MigracaoContratos(eng_novo, eng_legado)
    migrador.executar()