import sys
import sqlalchemy as sa
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime
import re
import unicodedata

# 1. Configuração da Conexão (Docker Localhost:3307)
engine = create_engine("mysql+pymysql://root:root@localhost:3307/controle-interno")

# Dicionários de Mapeamento (Seus Enums)
MAP_TIPO = {'LICITACAO': 1, 'PESSOA JURIDICA': 2, 'PESSOA FISICA': 3}
MAP_STATUS = {'AGUARDANDO INICIO': 1, 'EM ANDAMENTO': 2, 'ENCERRADO': 3}
MAP_ORGANIZACAO = {'ALUCOM': 1115, 'MOREIA': 1122, 'IP SERVIÇOS': 1311, 'AS SISTEMAS': 1378}

# ENUMS EVENT
MAP_EVENT_TYPES = {
    'CADASTRO': 1,
    'ADITIVO DE PRAZO': 2,
    'ADITIVO DE QUANTIDADE': 3,
    'ADITIVO DE REAJUSTE': 4,
    'ADITIVO DE SUPRESSAO': 5,
    'ADITIVO MODIFICAÇÃO DE ITEM': 6,
    'APOSTILAMENTO': 7
}

# === DICIONÁRIO DE ABREVIAÇÕES CONHECIDAS ===
ABBREVIATIONS = {
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
    'IFCE': 'INSTITUTO FEDERAL DE EDUCAÇÃO E TECNOLOGIA - IFCE',
    '10A REGIAO MILITAR': '10ª REGIÃO MILITAR',
    '10ª REGIAO MILITAR': '10ª REGIÃO MILITAR',
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
    'ESCRITORIO DE REPRESENTAÇÃO DO MINISTÉRIO DAS RELAÇÕES EXTERIORES': 'MINISTÉRIO DE RELAÇÕES EXTERIORES - SP'
}
# Limpeza de tabelas refatorado
def limpar_tabelas_equipamentos(engine):
        with engine.begin() as conn:
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
            for tabela in ['contracts', 'contract_items', 'contract_infos',
                           'contract_jobs', 'event_additives', 'contract_events', 'contract_recipient_customers']:
                conn.execute(sa.text(f"TRUNCATE TABLE `{tabela}`"))
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))

limpar_tabelas_equipamentos(engine)
print("✅ Tabelas limpas.")



#===================================================
# BLOCO DE LIMPEZA E TRATAMENTO DE DADOS (PANDAS)
#===================================================
def limpar_valor_inteiro(valor):
    if pd.isna(valor) or str(valor).strip() in ('', '-', '–', '—'):
        return 0
    try:
        return int(float(valor))
    except (ValueError, TypeError):
        return 0

def ultra_normalizar(texto):
    """Normalização profunda para ignorar acentos, símbolos e padronizar termos."""
    if pd.isna(texto): return ""
    texto = str(texto).upper()
    substituicoes = {'º': ' ', '°': ' ', 'ª': ' ', '§': ' ', '(': ' ', ')': ' ', '/': ' ', '-': ' ', '.': ' ', ',': ' '}
    for char, rep in substituicoes.items():
        texto = texto.replace(char, rep)
    texto = re.sub(r'\bPREF\b', 'PREFEITURA', texto)
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    texto = re.sub(r'[^A-Z0-9 ]', '', texto)
    return ' '.join(texto.split())

def validar_conflito_estrito(tokens_alvo, tokens_banco):
    num_alvo = {t for t in tokens_alvo if t.isdigit()}
    num_banco = {t for t in tokens_banco if t.isdigit()}
    if num_alvo and num_banco and num_alvo != num_banco:
        return False
    siglas = {'IFCE', 'IFRN', 'IFPB', 'UFC', 'UFSC', 'TRE', 'TRT'}
    for s in siglas:
        if s in tokens_alvo and s not in tokens_banco: return False
        if s in tokens_banco and s not in tokens_alvo: return False
    return True

def match_por_tokens(nome_planilha_norm, customer_cache):
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

        if 'REGIONAL' in tokens_alvo and ('GESTAO' in tokens_alvo or 'SEGER' in tokens_alvo):
            if 'REGIONAL' in tokens_banco and 'GESTAO' in tokens_banco:
                return dados
        if 'CANINDE' in tokens_alvo and 'AGRICULTURA' in tokens_alvo:
            if 'CANINDE' in tokens_banco and 'AGRICULTURA' in tokens_banco:
                return dados

        ratio_alvo = len_inter / len(tokens_alvo_limpos) if len(tokens_alvo_limpos) > 0 else 0
        ratio_banco = len_inter / len(tokens_banco_limpos) if len(tokens_banco_limpos) > 0 else 0
        if ratio_alvo >= 0.70 or ratio_banco >= 0.70:
            melhores_candidatos.append((max(ratio_alvo, ratio_banco), dados))

    if melhores_candidatos:
        melhores_candidatos.sort(key=lambda x: x[0], reverse=True)
        return melhores_candidatos[0][1]
    return None

def get_hierarchical_customer(nome_planilha, customer_cache):
    nome_alvo = ultra_normalizar(nome_planilha)
    if not nome_alvo: 
        print(f"❌ Cliente não localizado: {nome_planilha} (vazio)")
        return None

    if nome_alvo in customer_cache:
        d = customer_cache[nome_alvo]
        print(f"✅ Match (Exato): '{nome_planilha}' -> '{d['debug']}'")
        return d['parent_id'] if d['parent_id'] else d['id']

    match_tokens = match_por_tokens(nome_alvo, customer_cache)
    if match_tokens:
        d = match_tokens
        print(f"✅ Match (Tokens): '{nome_planilha}' -> '{d['debug']}'")
        return d['parent_id'] if d['parent_id'] else d['id']

    print(f"❌ Cliente não localizado: {nome_planilha}")
    return None

def limpar_valor_numerico(valor):
    if pd.isna(valor) or valor == '': return 0.0
    if isinstance(valor, (int, float)): return float(valor)
    texto = str(valor).replace('R$', '').replace('.', '').replace(',', '.').replace(' ', '').strip()
    try: return float(texto)
    except: return 0.0


def migrar_completo_upsert():
    """
    Versão UPSERT com suporte a Snapshots Históricos completando herança de aditivos anteriores.
    """
    caminho_planilha = './docs/Contratos.xlsx'
    agora = datetime.now()
    
    stats = {
        'contratos_criados': 0,
        'contratos_atualizados': 0,
        'contratos_ignorados': 0,
        'eventos_criados': 0,
        'eventos_ignorados': 0,
        'aditivos_criados': 0,
        'itens_criados': 0,
        'itens_atualizados': 0,
        'jobs_criados': 0,
        'jobs_atualizados': 0,
        'infos_criadas': 0,
        'infos_atualizadas': 0,
        'erros': 0
    }

    try:
        with engine.begin() as conn:
            print("=" * 80)
            print("🚀 MODO UPSERT - HISTÓRICO CONSOLIDADO (SNAPSHOTS)")
            print("=" * 80)
            
            print("\n🔍 Carregando dados existentes do banco...")
            contracts_cache = {}
            contracts_by_number = {}
            
            res_contracts = conn.execute(text("""
                SELECT id, name, number, organization_id, customer_id 
                FROM contracts
            """)).fetchall()
            
            for r in res_contracts:
                chave = f"{r[1]}|{r[2]}|{r[3]}"
                dados_cache = {"id": r[0], "customer_id": r[4]}
                contracts_cache[chave] = dados_cache
                if r[2] and r[2] != "SEM_NUMERO":
                    contracts_by_number[r[2]] = dados_cache
                    
            print(f"   📋 {len(contracts_cache)} contratos em cache")

            events_cache = {}
            res_events = conn.execute(text("""
                SELECT id, contract_id FROM contract_events
            """)).fetchall()
            for r in res_events:
                if r[1] not in events_cache:
                    events_cache[r[1]] = []
                events_cache[r[1]].append(r[0])
            print(f"   📅 {len(res_events)} eventos em cache")

            additives_cache = {}
            res_additives = conn.execute(text("""
                SELECT id, event_id, contract_event_type_id FROM event_additives
            """)).fetchall()
            for r in res_additives:
                chave = f"{r[1]}|{r[2]}"
                additives_cache[chave] = r[0]
            print(f"   📝 {len(additives_cache)} aditivos em cache")

            print("\n🔍 Carregando clientes do banco...")
            query = text("""
                SELECT c.id, c.name, c.alias, c.parent_id, a.city, a.alias as addr_alias 
                FROM customers c 
                LEFT JOIN addresses a ON a.addressable_id = c.id AND a.addressable_type = 'customer'
            """)
            res_cust = conn.execute(query).fetchall()
            
            customer_cache = {}
            for r in res_cust:
                info = {'id': r[0], 'parent_id': r[3], 'debug': r[1]}
                combos = [r[1], r[2]]
                if r[1] and r[4]: combos.append(f"{r[1]} {r[4]}")
                if r[1] and r[5]: combos.append(f"{r[1]} {r[5]}")
                for txt in combos:
                    norm = ultra_normalizar(txt)
                    if norm: customer_cache[norm] = info

            print("🔄 Expandindo cache com abreviações conhecidas...")
            for abbr, full_name in ABBREVIATIONS.items():
                norm_abbr = ultra_normalizar(abbr)
                norm_full = ultra_normalizar(full_name)
                if norm_full in customer_cache:
                    customer_cache[norm_abbr] = customer_cache[norm_full]

            print("\n📖 Lendo abas da planilha...")
            df_ex_contract = pd.read_excel(caminho_planilha, sheet_name='CONTRATOS')
            df_ex_events = pd.read_excel(caminho_planilha, sheet_name='EVENTO')
            df_ex_itens = pd.read_excel(caminho_planilha, sheet_name='ITENS')
            df_ex_jobs = pd.read_excel(caminho_planilha, sheet_name='SERVICOS')
            df_ex_infos = pd.read_excel(caminho_planilha, sheet_name='INFORMAÇÕES')

            # ====================================================================
            # MIGRAÇÃO DE CONTRATOS (COM VÍNCULO DE DESTINATÁRIO AUTOMÁTICO)
            # ====================================================================
            print("\n🔄 Processando Contratos (UPSERT)...")
            contract_id_map = {}
            
            for idx, row in df_ex_contract.iterrows():
                if pd.isna(row['CONTRATANTE']) or pd.isna(row['APELIDO_CONTRATO']): 
                    stats['contratos_ignorados'] += 1
                    continue
                
                cust_id = get_hierarchical_customer(row['CONTRATANTE'], customer_cache)
                if not cust_id:
                    print(f"   ⚠️ Linha {idx}: Cliente '{row['CONTRATANTE']}' não encontrado - IGNORADO")
                    stats['contratos_ignorados'] += 1
                    continue

                nome_contrato = str(row['APELIDO_CONTRATO']).strip().upper()
                numero_contrato = str(row['NUMERO_CONTRATO']).strip() if pd.notna(row['NUMERO_CONTRATO']) else "SEM_NUMERO"
                org_id = MAP_ORGANIZACAO.get(row['CONTRATADO'], 1115)
                
                chave_contrato = f"{nome_contrato}|{numero_contrato}|{org_id}"

                contract_info = contracts_cache.get(chave_contrato)
                if not contract_info and numero_contrato != "SEM_NUMERO":
                    contract_info = contracts_by_number.get(numero_contrato)

                dados_contrato = {
                    'name': nome_contrato,
                    'number': numero_contrato,
                    'contract_type_id': MAP_TIPO.get(ultra_normalizar(row['TIPO_CONTRATO']), 1),
                    'contract_status_id': MAP_STATUS.get(ultra_normalizar(row['STATUS_CONTRATO']), 2),
                    'organization_id': org_id,
                    'customer_id': int(cust_id),
                    'object': str(row['OBJETO_DO_CONTRATO'])[:500] if not pd.isna(row['OBJETO_DO_CONTRATO']) else "NÃO INFORMADO",
                    'updated_at': agora
                }

                if contract_info:
                    contract_id = contract_info['id']
                    conn.execute(text("""
                        UPDATE contracts 
                        SET contract_type_id = :contract_type_id,
                            contract_status_id = :contract_status_id,
                            customer_id = :customer_id,
                            object = :object,
                            updated_at = :updated_at
                        WHERE id = :id AND number = :number
                    """), {**dados_contrato, 'id': contract_id, 'number': numero_contrato})
                    stats['contratos_atualizados'] += 1
                    print(f"   🔄 Atualizado: {nome_contrato} (Número: {numero_contrato})")
                else:
                    dados_contrato['created_at'] = agora
                    res = conn.execute(text("""
                        INSERT INTO contracts 
                        (name, number, contract_type_id, contract_status_id, 
                         organization_id, customer_id, object, created_at, updated_at)
                        VALUES (:name, :number, :contract_type_id, :contract_status_id,
                                :organization_id, :customer_id, :object, :created_at, :updated_at)
                    """), dados_contrato)
                    
                    # No MySQL puro com pymysql, o res.lastrowid captura o ID gerado perfeitamente!
                    contract_id = res.lastrowid
                    
                    novo_cache = {'id': contract_id, 'customer_id': cust_id}
                    contracts_cache[chave_contrato] = novo_cache
                    if numero_contrato != "SEM_NUMERO":
                        contracts_by_number[numero_contrato] = novo_cache
                        
                    stats['contratos_criados'] += 1
                    print(f"   ✅ Criado: {nome_contrato}")

                # -----------------------------------------------------------------
                #  Insere o mesmo cliente como Destinatário do Contrato
                # -----------------------------------------------------------------
                conn.execute(text("""
                    INSERT IGNORE INTO contract_recipient_customers (contract_id, customer_id)
                    VALUES (:contract_id, :customer_id)
                """), {
                    'contract_id': int(contract_id),
                    'customer_id': int(cust_id)
                })
                # -----------------------------------------------------------------

                contract_id_map[nome_contrato] = contract_id

            # ====================================================================
            # CONTRACT_EVENTS & EVENT_ADDITIVES (COM CLONAGEM DE HISTÓRICO)
            # ====================================================================
            print("\n📅 Processando Eventos e Aditivos (UPSERT + CLONAGEM)...")
            additive_lookup = {}  # Mapeia ID_EVENTO_PLANILHA → additive_id
            ultimo_aditivo_por_contrato = {}  # Cache temporário: { contract_id: ultimo_additive_id }
            
            contract_event_counters = {} 
            
            for idx, row in df_ex_events.iterrows():
                if pd.isna(row['CONTRATO']) or pd.isna(row['ID']):
                    stats['eventos_ignorados'] += 1
                    continue
                
                nome_contrato = str(row['CONTRATO']).strip().upper()
                
                if nome_contrato not in contract_id_map:
                    found = False
                    for chave, dados in contracts_cache.items():
                        if chave.startswith(nome_contrato + "|"):
                            contract_id_map[nome_contrato] = dados['id']
                            found = True
                            break
                    
                    if not found:
                        print(f"   ⚠️ Linha {idx}: Contrato '{nome_contrato}' não encontrado - EVENTO IGNORADO")
                        stats['eventos_ignorados'] += 1
                        continue

                contract_id = contract_id_map[nome_contrato]
                tipo_planilha = ultra_normalizar(row['TIPO']) if pd.notna(row['TIPO']) else 'CADASTRO'
                id_evento_planilha = row['ID']

                # Inicializa o contador do contrato se for a primeira vez vendo ele na planilha
                if contract_id not in contract_event_counters:
                    contract_event_counters[contract_id] = 0
                
                idx_evento_atual = contract_event_counters[contract_id]

                # 🛠️ CORREÇÃO AQUI: Verifica se já existe um evento registrado para esta "posição" do contrato
                if contract_id in events_cache and idx_evento_atual < len(events_cache[contract_id]):
                    event_id = events_cache[contract_id][idx_evento_atual]
                else:
                    # Se a planilha tem mais eventos do que o banco já tinha mapeado, cria um novo contract_event
                    res = conn.execute(text("""
                        INSERT INTO contract_events (contract_id, created_at, updated_at) 
                        VALUES (:c_id, :now, :now)
                    """), {"c_id": contract_id, "now": agora})
                    event_id = res.lastrowid
                    if contract_id not in events_cache:
                        events_cache[contract_id] = []
                    events_cache[contract_id].append(event_id)
                    stats['eventos_criados'] += 1

                # Incrementa o contador para a próxima linha deste mesmo contrato
                contract_event_counters[contract_id] += 1

                tipos_aditivos = []
                if "REAJUSTE" in tipo_planilha and "PRAZO" in tipo_planilha:
                    tipos_aditivos = [2, 4]
                else:
                    tipos_aditivos = [MAP_EVENT_TYPES.get(tipo_planilha, 1)]

                for t_id in tipos_aditivos:
                    chave_aditivo = f"{event_id}|{t_id}"
                    
                    if chave_aditivo in additives_cache:
                        # Se já existe no banco, apenas pegamos o ID
                        additive_id = additives_cache[chave_aditivo]
                    else:
                        # Se é um aditivo totalmente novo, inserimos e aplicamos a herança (Cópia)
                        res = conn.execute(text("""
                            INSERT INTO event_additives 
                            (event_id, contract_event_type_id, created_at, updated_at) 
                            VALUES (:e_id, :t_id, :now, :now)
                        """), {"e_id": event_id, "t_id": t_id, "now": agora})
                        additive_id = res.lastrowid
                        additives_cache[chave_aditivo] = additive_id
                        stats['aditivos_criados'] += 1
                        
                        # ✨ REGRA DE NEGÓCIO: Clonar o estado anterior do contrato (Efeito Cópia)
                        antigo_additive_id = ultimo_aditivo_por_contrato.get(contract_id)
                        if antigo_additive_id:
                            print(f"      📋 [Histórico] Clonando dados do aditivo anterior ({antigo_additive_id}) para o novo aditivo ({additive_id})...")
                            
                            # 1. Clona as Informações Gerais de Vigência
                            conn.execute(text("""
                                INSERT INTO contract_infos (event_additive_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, created_at, updated_at)
                                SELECT :novo_id, start_date, end_date, max_end_date, duration, max_duration, total_amount, :now, :now
                                FROM contract_infos WHERE event_additive_id = :antigo_id
                            """), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": agora})
                            
                            # 2. Clona os Itens Existentes
                            conn.execute(text("""
                                INSERT INTO contract_items (event_additive_id, alias, description, quantity, available_quantity, price, created_at, updated_at)
                                SELECT :novo_id, alias, description, quantity, available_quantity, price, :now, :now
                                FROM contract_items WHERE event_additive_id = :antigo_id
                            """), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": agora})
                            
                            # 3. Clona os Serviços Existentes
                            conn.execute(text("""
                                INSERT INTO contract_jobs (event_additive_id, alias, description, quantity, price, created_at, updated_at)
                                SELECT :novo_id, alias, description, quantity, price, :now, :now
                                FROM contract_jobs WHERE event_additive_id = :antigo_id
                            """), {"novo_id": additive_id, "antigo_id": antigo_additive_id, "now": agora})

                    # Atualiza o ponteiro da linha do tempo deste contrato
                    ultimo_aditivo_por_contrato[contract_id] = additive_id
                    
                    # SOLUÇÃO COMPLETA: Mapeia o ID da planilha diretamente para o aditivo correto gerado
                    if id_evento_planilha not in additive_lookup or t_id in [1, 3, 4, 5]:
                        additive_lookup[id_evento_planilha] = additive_id

            # ====================================================================
            # CONTRACT_ITEMS (UPSERT)
            # ====================================================================
            print("\n📦 Processando Itens (UPSERT)...")
            for _, row in df_ex_itens.iterrows():
                if pd.isna(row['EVENTO']):
                    continue
                    
                aid = additive_lookup.get(row['EVENTO'])
                if not aid:
                    continue

                exists = conn.execute(text("""
                    SELECT id FROM contract_items 
                    WHERE event_additive_id = :aid AND alias = :alias
                """), {"aid": aid, "alias": str(row['APELIDO'])[:100]}).fetchone()

                dados_item = {
                    'alias': str(row['APELIDO'])[:100],
                    'description': str(row['DESCRICAO'])[:500] if pd.notna(row['DESCRICAO']) else '',
                    'quantity': limpar_valor_numerico(row['QUANTIDADE']),
                    'available_quantity': limpar_valor_numerico(row['QUANTIDADE']),
                    'price': limpar_valor_numerico(row['VALOR_UNITARIO']),
                    'updated_at': agora
                }

                if exists:
                    conn.execute(text("""
                        UPDATE contract_items 
                        SET description = :description,
                            quantity = :quantity,
                            available_quantity = :available_quantity,
                            price = :price,
                            updated_at = :updated_at
                        WHERE id = :id
                    """), {**dados_item, 'id': exists[0]})
                    stats['itens_atualizados'] += 1
                else:
                    dados_item['event_additive_id'] = aid
                    dados_item['created_at'] = agora
                    conn.execute(text("""
                        INSERT INTO contract_items 
                        (event_additive_id, alias, description, quantity, 
                         available_quantity, price, created_at, updated_at)
                        VALUES (:event_additive_id, :alias, :description, :quantity,
                                :available_quantity, :price, :created_at, :updated_at)
                    """), dados_item)
                    stats['itens_criados'] += 1

            # ====================================================================
            # CONTRACT_JOBS (UPSERT)
            # ====================================================================
            print("\n🛠️ Processando Serviços (UPSERT)...")
            for _, row in df_ex_jobs.iterrows():
                if pd.isna(row['EVENTO']):
                    continue
                    
                aid = additive_lookup.get(row['EVENTO'])
                if not aid:
                    continue

                exists = conn.execute(text("""
                    SELECT id FROM contract_jobs 
                    WHERE event_additive_id = :aid AND alias = :alias
                """), {"aid": aid, "alias": str(row['APELIDO'])[:100]}).fetchone()

                dados_job = {
                    'alias': str(row['APELIDO'])[:100],
                    'description': str(row['DESCRICAO'])[:500] if pd.notna(row['DESCRICAO']) else '',
                    'quantity': limpar_valor_numerico(row['QUANTIDADE']),
                    'price': limpar_valor_numerico(row['VALOR_UNITARIO']),
                    'updated_at': agora
                }

                if exists:
                    conn.execute(text("""
                        UPDATE contract_jobs 
                        SET description = :description,
                            quantity = :quantity,
                            price = :price,
                            updated_at = :updated_at
                        WHERE id = :id
                    """), {**dados_job, 'id': exists[0]})
                    stats['jobs_atualizados'] += 1
                else:
                    dados_job['event_additive_id'] = aid
                    dados_job['created_at'] = agora
                    conn.execute(text("""
                        INSERT INTO contract_jobs 
                        (event_additive_id, alias, description, quantity, 
                         price, created_at, updated_at)
                        VALUES (:event_additive_id, :alias, :description, :quantity,
                                :price, :created_at, :updated_at)
                    """), dados_job)
                    stats['jobs_criados'] += 1

            # ====================================================================
            # CONTRACT_INFOS (UPSERT)
            # ====================================================================
            print("\n📅 Processando Vigências e Valores (UPSERT)...")
            for _, row in df_ex_infos.iterrows():
                if pd.isna(row['EVENTO']):
                    continue
                    
                aid = additive_lookup.get(row['EVENTO'])
                if not aid:
                    continue

                exists = conn.execute(text("""
                    SELECT id FROM contract_infos 
                    WHERE event_additive_id = :aid
                """), {"aid": aid}).fetchone()

                dados_info = {
                    'start_date': row['DATA_INICIAL'] if pd.notna(row['DATA_INICIAL']) else None,
                    'end_date': row['DATA_FINAL'] if pd.notna(row['DATA_FINAL']) else None,
                    'max_end_date': row['DATA_FINAL_MAXIMA'] if pd.notna(row['DATA_FINAL_MAXIMA']) else None,
                    'duration': limpar_valor_inteiro(row['DURAÇÃO (MESES)']),
                    'max_duration': limpar_valor_inteiro(row['DURAÇÃO_MAXIMA (MESES)']) or 60,
                    'total_amount': limpar_valor_numerico(row['VALOR TOTAL']),
                    'updated_at': agora
                }

                if exists:
                    conn.execute(text("""
                        UPDATE contract_infos 
                        SET start_date = :start_date,
                            end_date = :end_date,
                            max_end_date = :max_end_date,
                            duration = :duration,
                            max_duration = :max_duration,
                            total_amount = :total_amount,
                            updated_at = :updated_at
                        WHERE id = :id
                    """), {**dados_info, 'id': exists[0]})
                    stats['infos_atualizadas'] += 1
                else:
                    dados_info['event_additive_id'] = aid
                    dados_info['created_at'] = agora
                    conn.execute(text("""
                        INSERT INTO contract_infos 
                        (event_additive_id, start_date, end_date, max_end_date, 
                         duration, max_duration, total_amount, created_at, updated_at)
                        VALUES (:event_additive_id, :start_date, :end_date, :max_end_date,
                                :duration, :max_duration, :total_amount, :created_at, :updated_at)
                    """), dados_info)
                    stats['infos_criadas'] += 1

            # ====================================================================
            # RELATÓRIO FINAL
            # ====================================================================
            print("\n" + "=" * 80)
            print("📊 RELATÓRIO DE MIGRAÇÃO (MODO UPSERT + SNAPSHOTS)")
            print("=" * 80)
            print(f"{'CONTRATOS:':<30} ✅ Criados: {stats['contratos_criados']:<5} 🔄 Atualizados: {stats['contratos_atualizados']:<5} ⚠️ Ignorados: {stats['contratos_ignorados']}")
            print(f"{'EVENTOS:':<30} ✅ Criados: {stats['eventos_criados']:<5} ⚠️ Ignorados: {stats['eventos_ignorados']}")
            print(f"{'ADITIVOS:':<30} ✅ Criados: {stats['aditivos_criados']}")
            print(f"{'ITENS:':<30} ✅ Criados: {stats['itens_criados']:<5} 🔄 Atualizados: {stats['itens_atualizados']}")
            print(f"{'SERVIÇOS:':<30} ✅ Criados: {stats['jobs_criados']:<5} 🔄 Atualizados: {stats['jobs_atualizados']}")
            print(f"{'INFORMAÇÕES:':<30} ✅ Criadas: {stats['infos_criadas']:<5} 🔄 Atualizadas: {stats['infos_atualizadas']}")
            print(f"{'ERROS:':<30} ❌ {stats['erros']}")
            print("=" * 80)
            print("🚀 Migração concluída com sucesso!")

    except Exception as e:
        print(f"❌ Erro crítico: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    migrar_completo_upsert()