import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime
import re
import unicodedata

# 1. Configuração da Conexão (Docker Localhost:3307)
engine = create_engine("mysql+pymysql://root:root@localhost:3307/controle-interno")

# Dicionários de Mapeamento (Seus Enums)
# ENUMS CONTRACT
MAP_TIPO = {'LICITAÇÃO': 1, 'PESSOA JURÍDICA': 2, 'PESSOA FÍSICA': 3}
MAP_STATUS = {'AGUARDANDO INÍCIO': 1, 'EM ANDAMENTO': 2, 'ENCERRADO': 3}
MAP_ORGANIZACAO = {'ALUCOM': 1115, 'MOREIA': 1122, 'IP': 1311, 'AS SISTEMAS': 1378}

# ENUMS EVENT
MAP_EVENT_TYPES = {
    'CADASTRO': 1,
    'ADITIVO DE PRAZO': 2,
    'ADITIVO DE QUANTIDADE': 3,
    'ADITIVO DE REAJUSTE': 4,
    'ADITIVO DE SUPRESSÃO': 5
}

def ultra_normalizar(texto):
    """Normalização profunda para ignorar acentos, símbolos e padronizar termos."""
    if pd.isna(texto): return ""
    texto = str(texto).upper()
    
    # 1. Limpeza de caracteres especiais e indicadores ordinais
    substituicoes = {
        'º': ' ', '°': ' ', 'ª': ' ', '§': ' ', 
        '(': ' ', ')': ' ', '/': ' ', '-': ' ', '.': ' ', ',': ' '
    }
    for char, rep in substituicoes.items():
        texto = texto.replace(char, rep)
    
    # 2. Expansões de abreviações críticas
    texto = re.sub(r'\bPREF\b', 'PREFEITURA', texto)
    
    # 3. Remove acentos (Normalização NFD)
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    
    # 4. Mantém apenas letras de A-Z e números de 0-9
    texto = re.sub(r'[^A-Z0-9 ]', '', texto)
    
    return ' '.join(texto.split())

def validar_conflito_estrito(tokens_alvo, tokens_banco):
    """TRAVA DE SEGURANÇA MÁXIMA: Impede confusão entre números, siglas e estados."""
    # 1. Trava de Números (Ex: 8a vs 10a Região)
    num_alvo = {t for t in tokens_alvo if t.isdigit()}
    num_banco = {t for t in tokens_banco if t.isdigit()}
    if num_alvo and num_banco and num_alvo != num_banco:
        return False

    # 2. Trava de Siglas Federais (IFCE vs IFRN)
    siglas = {'IFCE', 'IFRN', 'IFPB', 'UFC', 'UFSC', 'TRE', 'TRT'}
    for s in siglas:
        if s in tokens_alvo and s not in tokens_banco: return False
        if s in tokens_banco and s not in tokens_alvo: return False
            
    return True

def match_por_tokens(nome_planilha_norm, customer_cache):
    """Busca inteligente baseada em tokens com pesos para casos específicos (SEGER, Canindé)."""
    stopwords = {'DE', 'DA', 'DO', 'DOS', 'DAS', 'E', 'EM', 'NA', 'NO', 'PARA', 'COM', 'POR', 'O', 'A', 'MUNICIPAL', 'ESTADO', 'MUNICIPIO'}
    
    tokens_alvo = set(nome_planilha_norm.split())
    tokens_alvo_limpos = tokens_alvo - stopwords
    
    if not tokens_alvo_limpos: return None

    melhores_candidatos = []

    for chave_banco, dados in customer_cache.items():
        tokens_banco = set(chave_banco.split())
        tokens_banco_limpos = tokens_banco - stopwords
        
        if not validar_conflito_estrito(tokens_alvo, tokens_banco):
            continue

        interseccao = tokens_alvo_limpos.intersection(tokens_banco_limpos)
        len_inter = len(interseccao)
        
        # --- CASO SEGER / GESTAO REGIONAL ---
        if 'REGIONAL' in tokens_alvo and ('GESTAO' in tokens_alvo or 'SEGER' in tokens_alvo):
            if 'REGIONAL' in tokens_banco and 'GESTAO' in tokens_banco:
                return dados

        # --- CASO CANINDÉ AGRICULTURA ---
        if 'CANINDE' in tokens_alvo and 'AGRICULTURA' in tokens_alvo:
            if 'CANINDE' in tokens_banco and 'AGRICULTURA' in tokens_banco:
                return dados

        # Cálculo de Scores de similaridade
        ratio_alvo = len_inter / len(tokens_alvo_limpos) if len(tokens_alvo_limpos) > 0 else 0
        ratio_banco = len_inter / len(tokens_banco_limpos) if len(tokens_banco_limpos) > 0 else 0
        
        if ratio_alvo >= 0.70 or ratio_banco >= 0.70:
            melhores_candidatos.append((max(ratio_alvo, ratio_banco), dados))

    if melhores_candidatos:
        melhores_candidatos.sort(key=lambda x: x[0], reverse=True)
        return melhores_candidatos[0][1]

    return None

def get_hierarchical_customer(nome_planilha, customer_cache):
    """Coordena a busca: primeiro exata, depois por tokens inteligente."""
    nome_alvo = ultra_normalizar(nome_planilha)
    if not nome_alvo: return None
    
    # 1. TENTATIVA EXATA
    if nome_alvo in customer_cache:
        d = customer_cache[nome_alvo]
        print(f"✅ Match (Exato): '{nome_planilha}' -> '{d['debug']}'")
        return d['parent_id'] if d['parent_id'] else d['id']

    # 2. BUSCA POR TOKENS (Com travas de segurança)
    match_tokens = match_por_tokens(nome_alvo, customer_cache)
    if match_tokens:
        d = match_tokens
        print(f"✅ Match (Tokens): '{nome_planilha}' -> '{d['debug']}'")
        return d['parent_id'] if d['parent_id'] else d['id']

    print(f"❌ Cliente não localizado: {nome_planilha}")
    return None

def limpar_valor_numerico(valor):
    """Limpa strings monetárias e converte para float."""
    if pd.isna(valor) or valor == '': return 0.0
    if isinstance(valor, (int, float)): return float(valor)
    texto = str(valor).replace('R$', '').replace('.', '').replace(',', '.').replace(' ', '').strip()
    try: return float(texto)
    except: return 0.0

def migrar_completo():
    caminho_planilha = 'Contratos.xlsx'
    agora = datetime.now()
    additive_lookup = {}
    contract_name_map = {} # Mapeia apelido da planilha para o nome gerado no banco

    try:
        with engine.begin() as conn:
            # --- LIMPEZA (RESET) ---
            print("🧹 Limpando tabelas para novo teste...")
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))
            
            tabelas = ['contract_infos', 'contract_jobs', 'contract_items', 'event_additives', 'contract_events', 'contracts']
            for table in tabelas:
                conn.execute(text(f"DELETE FROM {table};"))
                conn.execute(text(f"ALTER TABLE {table} AUTO_INCREMENT = 1;"))
            
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
            print("✅ Tabelas limpas e IDs resetados.")

            # --- PREPARAÇÃO DE CACHE DE CLIENTES ---
            print("🔍 Mapeando Clientes e Estrutura Hierárquica...")
            query = text("""
                SELECT c.id, c.name, c.alias, c.parent_id, a.city, a.alias as addr_alias 
                FROM customers c 
                LEFT JOIN addresses a ON a.addressable_id = c.id AND a.addressable_type = 'customer'
            """)
            res_cust = conn.execute(query).fetchall()
            
            customer_cache = {}
            for r in res_cust:
                info = {'id': r[0], 'parent_id': r[3], 'debug': r[1]}
                # Cadastra variações (Nome, Alias, Nome+Cidade) para aumentar chance de match
                combos = [r[1], r[2]]
                if r[1] and r[4]: combos.append(f"{r[1]} {r[4]}")
                if r[1] and r[5]: combos.append(f"{r[1]} {r[5]}")
                for txt in combos:
                    norm = ultra_normalizar(txt)
                    if norm: customer_cache[norm] = info

            # --- LEITURA DA PLANILHA ---
            print("📖 Lendo abas da planilha...")
            df_ex_contract = pd.read_excel(caminho_planilha, sheet_name='CONTRATOS')
            df_ex_events = pd.read_excel(caminho_planilha, sheet_name='EVENTO')
            df_ex_itens = pd.read_excel(caminho_planilha, sheet_name='ITENS')
            df_ex_jobs = pd.read_excel(caminho_planilha, sheet_name='SERVICOS')
            df_ex_infos = pd.read_excel(caminho_planilha, sheet_name='INFORMAÇÕES')

            # --- CONTRACTS ---
            print("🔄 Migrando Contratos...")
            contracts_batch = []
            for _, row in df_ex_contract.iterrows():
                if pd.isna(row['CONTRATANTE']): continue
                
                cust_id = get_hierarchical_customer(row['CONTRATANTE'], customer_cache)
                
                if cust_id:
                    # USANDO O APELIDO JÁ PRONTO DA PLANILHA COMO NOME DO CONTRATO
                    nome_contrato = str(row['APELIDO_CONTRATO']).strip().upper()
                    
                    contracts_batch.append({
                        'name': nome_contrato,
                        'number': str(row['NUMERO_CONTRATO']).strip(),
                        'contract_type_id': MAP_TIPO.get(ultra_normalizar(row['TIPO_CONTRATO']), 1),
                        'contract_status_id': MAP_STATUS.get(ultra_normalizar(row['STATUS_CONTRATO']), 2),
                        'organization_id': MAP_ORGANIZACAO.get(ultra_normalizar(row['CONTRATADO']), 1115),
                        'customer_id': int(cust_id),
                        'object': str(row['OBJETO_DO_CONTRATO'])[:500] if not pd.isna(row['OBJETO_DO_CONTRATO']) else "NÃO INFORMADO",
                        'created_at': agora,
                        'updated_at': agora
                    })
            
            if contracts_batch:
                pd.DataFrame(contracts_batch).to_sql('contracts', con=conn, if_exists='append', index=False)
                print(f"✅ {len(contracts_batch)} Contratos inseridos.")

            # --- CONTRACT_EVENTS & EVENT_ADDITIVES ---
            print("📅 Gerando Eventos e Aditivos...")
            df_db_contracts = pd.read_sql('SELECT id, name FROM contracts', con=conn)
            # Mapeia o contrato da planilha para o nome exato gerado no banco
            df_ex_events['NOME_BUSCA'] = df_ex_events['CONTRATO'].str.strip().str.upper()
            df_ev_merge = pd.merge(df_ex_events, df_db_contracts, left_on='NOME_BUSCA', right_on='name')

            for _, row in df_ev_merge.iterrows():
                tipo_planilha = ultra_normalizar(row['TIPO'])
                
                # Insere o Evento pai
                conn.execute(text("INSERT INTO contract_events (contract_id, created_at, updated_at) VALUES (:c_id, :now, :now)"), 
                             {"c_id": row['id'], "now": agora})
                last_event_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

                # Lógica para múltiplos aditivos no mesmo evento (ex: Reajuste + Prazo)
                if "REAJUSTE" in tipo_planilha and "PRAZO" in tipo_planilha:
                    for t_id in [2, 4]:
                        conn.execute(text("INSERT INTO event_additives (event_id, contract_event_type_id, created_at, updated_at) VALUES (:e_id, :t_id, :now, :now)"),
                                     {"e_id": last_event_id, "t_id": t_id, "now": agora})
                        if t_id == 4: # Itens costumam vincular ao Reajuste/Valor
                            additive_lookup[row['ID']] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
                else:
                    t_id = MAP_EVENT_TYPES.get(tipo_planilha, 1)
                    conn.execute(text("INSERT INTO event_additives (event_id, contract_event_type_id, created_at, updated_at) VALUES (:e_id, :t_id, :now, :now)"),
                                 {"e_id": last_event_id, "t_id": t_id, "now": agora})
                    additive_lookup[row['ID']] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

            # --- CONTRACT_ITEMS ---
            print("📦 Vinculando Itens...")
            itens_data = []
            for _, row in df_ex_itens.iterrows():
                aid = additive_lookup.get(row['EVENTO'])
                if aid:
                    itens_data.append({
                        'event_additive_id': aid,
                        'alias': str(row['APELIDO'])[:100],
                        'description': str(row['DESCRICAO'])[:500],
                        'quantity': limpar_valor_numerico(row['QUANTIDADE']),
                        'available_quantity': limpar_valor_numerico(row['QUANTIDADE']),
                        'price': limpar_valor_numerico(row['VALOR_UNITARIO']),
                        'created_at': agora, 'updated_at': agora
                    })
            if itens_data:
                pd.DataFrame(itens_data).to_sql('contract_items', con=conn, if_exists='append', index=False)

            # --- CONTRACT_JOBS ---
            print("🛠️ Vinculando Serviços...")
            jobs_data = []
            for _, row in df_ex_jobs.iterrows():
                aid = additive_lookup.get(row['EVENTO'])
                if aid:
                    jobs_data.append({
                        'event_additive_id': aid,
                        'alias': str(row['APELIDO'])[:100],
                        'description': str(row['DESCRICAO'])[:500],
                        'quantity': limpar_valor_numerico(row['QUANTIDADE']),
                        'price': limpar_valor_numerico(row['VALOR_UNITARIO']),
                        'created_at': agora, 'updated_at': agora
                    })
            if jobs_data:
                pd.DataFrame(jobs_data).to_sql('contract_jobs', con=conn, if_exists='append', index=False)

            # --- CONTRACT_INFOS ---
            print("📅 Vinculando Vigências e Valores...")
            infos_data = []
            for _, row in df_ex_infos.iterrows():
                aid = additive_lookup.get(row['EVENTO'])
                if aid:
                    infos_data.append({
                        'event_additive_id': aid,
                        'start_date': row['DATA_INICIAL'],
                        'end_date': row['DATA_FINAL'],
                        'max_end_date': row['DATA_FINAL_MAXIMA'],
                        'duration': int(row['DURAÇÃO (MESES)']) if not pd.isna(row['DURAÇÃO (MESES)']) else 0,
                        'max_duration': int(row['DURAÇÃO_MAXIMA (MESES)']) if not pd.isna(row['DURAÇÃO_MAXIMA (MESES)']) else 60,
                        'total_amount': limpar_valor_numerico(row['VALOR TOTAL']),
                        'created_at': agora, 'updated_at': agora
                    })
            if infos_data:
                pd.DataFrame(infos_data).to_sql('contract_infos', con=conn, if_exists='append', index=False)

            print("🚀 Migração concluída com sucesso!")

    except Exception as e:
        print(f"❌ Erro crítico: {e}")

if __name__ == "__main__":
    migrar_completo()