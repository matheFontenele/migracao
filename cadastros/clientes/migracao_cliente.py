import re
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from config.config import (
    CLIENTES_BLOQUEADOS,
    ORGANIZACOES_BLOQUEADAS,
    FALSOS_RESERVAS,
    DEPARA_EXCECOES,
    MAPA_SAO_LUIS,
)
from utils.sanetizador import normalizar_para_match, executar_truncate_tabelas
from utils.mapeador import descobrir_id_organizacao

TABELAS_CLIENTES = [
    'addresses', 
    'customers'
]


class MigracaoClientes:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.stats = {"pais": 0, "filhos": 0, "enderecos": 0}

    #Inicio de extração
    def extrair_dados_legado(engine):
        query = """
        SELECT
            ald.id as ID_PREFEITURA,
            ald.nome as PREFEITURA,
            als.id as ID_SECRETARIA,
            als.nome as SECRETARIA,
            ac.id as ID_CLIENTE,
            ac.nome_razao_social as CLIENTE,
            ac.cpf_cnpj as CNPJ,
            ac.orgao_id as ORGANIZACAO,
            ac.cep as CEP,
            ac.endereco as ENDERECO,
            ac.estado as ESTADO,
            ac.cidade as CIDADE,
            ac.telefone as PHONE
        FROM aluguel_clientes ac
        LEFT JOIN aluguel_setor als ON als.id = ac.setor_id
        LEFT JOIN aluguel_departamento ald ON ald.id = als.departamento_id
        WHERE ac.deleted_at IS NULL
        AND als.deleted_at IS NULL
        AND ald.deleted_at IS NULL
        ORDER BY
            CASE
                WHEN ald.id IS NOT NULL AND als.id IS NOT NULL THEN 1
                WHEN ac.id IS NOT NULL THEN 2
                ELSE 3
            END,
            ald.nome,
            als.nome,
            ac.nome_razao_social;
        """
        with engine.connect() as conn:
            return pd.read_sql(text(query), conn)
        

    #Funções de padronização de São Luis e PCPB
    def unificar_sao_luis(df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        mask_sl = df_modificado['id_clean'].isin(MAPA_SAO_LUIS.keys())
        
        for idx, row in df_modificado[mask_sl].iterrows():
            id_cliente = int(row['id_clean'])
            id_sec_alvo, nome_sec_alvo = MAPA_SAO_LUIS[id_cliente]
            
            # Remove o sufixo "2026" do nome do cliente final (Neto) e limpa espaços
            nome_cliente = str(row['CLIENTE'])
            nome_cliente_limpo = re.sub(r'\b2026\b', '', nome_cliente).strip()
            
            # Aplica a unificação na linha
            df_modificado.at[idx, 'ID_PREFEITURA'] = SAO_LUIS_CONFIG['ID_PREF_ALVo']
            df_modificado.at[idx, 'PREFEITURA'] = SAO_LUIS_CONFIG['NOME_PREF_ALVO']
            df_modificado.at[idx, 'ID_SECRETARIA'] = id_sec_alvo
            df_modificado.at[idx, 'SECRETARIA'] = nome_sec_alvo
            df_modificado.at[idx, 'CLIENTE'] = nome_cliente_limpo
        return df_modificado
    def regionalizar_pcpb(df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        mask_pcpb = (df_modificado['ID_PREFEITURA'].astype(str).str.strip() == '320') | \
                    (df_modificado['CLIENTE'].str.contains('PCPB', na=False))
        
        for idx, row in df_modificado[mask_pcpb].iterrows():
            nome_cliente = str(row['CLIENTE']).upper()
            nome_cliente_limpo = unicodedata.normalize('NFD', nome_cliente).encode('ascii', 'ignore').decode('utf-8')
            
            regiao_encontrada = "SEDE / OUTROS"
            for termo_busca, regiao_oficial in MAPA_REGIOS_PCPB.items():
                if termo_busca in nome_cliente_limpo:
                    regiao_encontrada = regiao_oficial
                    break
                    
            id_sintetico = MAPA_IDS_SINTETICOS_PCPB[regiao_encontrada]
            
            df_modificado.at[idx, 'ID_SECRETARIA'] = id_sintetico
            df_modificado.at[idx, 'SECRETARIA'] = f"POLÍCIA CIVIL - {regiao_encontrada}"
        return df_modificado

    # ==============================================================================
    # INICIO DE MOTOR DE TRANSFORMAÇÃO
    # ==============================================================================
    def _transformar(self, df):
        print("🧹 Iniciando limpeza e aplicação de regras de negócio...")
        df_clean = df.copy()
        
        # Limpeza básica
        colunas_texto = ['PREFEITURA', 'SECRETARIA', 'CLIENTE', 'CEP', 'ENDERECO', 'ESTADO', 'CIDADE', 'CNPJ']
        for col in colunas_texto:
            if col in df_clean.columns:
                df_clean[col] = df_clean[col].astype(str).str.strip().replace(['nan', 'None', ''], None)
        
        df_clean['CEP'] = df_clean['CEP'].fillna('NULO')
        df_clean['ENDERECO'] = df_clean['ENDERECO'].fillna('NÃO INFORMADO')
        df_clean['ESTADO'] = df_clean['ESTADO'].fillna('NÃO INFORMADO')
        df_clean['CIDADE'] = df_clean['CIDADE'].fillna('NÃO INFORMADO')
        
        df_clean['ORG_DESTINO'] = df_clean['ORGANIZACAO'].apply(self._descobrir_organizacao)
        df_clean = df_clean[~df_clean['ORG_DESTINO'].isin(ORGANIZACOES_BLOQUEADAS)]
        df_clean['id_clean'] = pd.to_numeric(df_clean['ID_CLIENTE'], errors='coerce').fillna(0).astype(int)
        df_clean = df_clean[~df_clean['id_clean'].isin(CLIENTES_BLOQUEADOS)]

        # --- APLICA EXCEÇÕES (São Luís e PCPB) ---
        mask_sl = df_clean['id_clean'].isin(MAPA_SAO_LUIS.keys())
        for idx, row in df_clean[mask_sl].iterrows():
            id_sec_alvo, nome_sec_alvo = MAPA_SAO_LUIS[int(row['id_clean'])]
            df_clean.at[idx, 'ID_PREFEITURA'] = 287
            df_clean.at[idx, 'PREFEITURA'] = "PREFEITURA MUNICIPAL DE SÃO LUÍS"
            df_clean.at[idx, 'ID_SECRETARIA'] = id_sec_alvo
            df_clean.at[idx, 'SECRETARIA'] = nome_sec_alvo
            df_clean.at[idx, 'CLIENTE'] = re.sub(r'\b2026\b', '', str(row['CLIENTE'])).strip()

        mask_pcpb = (df_clean['ID_PREFEITURA'].astype(str).str.strip() == '320') | (df_clean['CLIENTE'].str.contains('PCPB', na=False))
        for idx, row in df_clean[mask_pcpb].iterrows():
            nome_limpo = unicodedata.normalize('NFD', str(row['CLIENTE']).upper()).encode('ascii', 'ignore').decode('utf-8')
            regiao = next((ro for tb, ro in MAPA_REGIOES_PCPB.items() if tb in nome_limpo), "SEDE / OUTROS")
            df_clean.at[idx, 'ID_SECRETARIA'] = MAPA_IDS_SINTETICOS_PCPB[regiao]
            df_clean.at[idx, 'SECRETARIA'] = f"POLÍCIA CIVIL - {regiao}"

        # --- MERGE DE RESERVAS ---
        mask_reserva = df_clean['CLIENTE'].str.contains(r'\b(?:RESERVA|RESERVADO)\b', case=False, na=False) & ~df_clean['id_clean'].isin(FALSOS_RESERVAS)
        df_normais = df_clean[~mask_reserva].copy()
        df_reservas = df_clean[mask_reserva].copy()

        df_normais['nome_ajustado'] = df_normais['CLIENTE'].apply(normalizar_para_match)
        df_reservas['nome_ajustado'] = df_reservas['CLIENTE'].apply(normalizar_para_match)
        df_normais['escopo_pai'] = df_normais['ID_SECRETARIA'].fillna(df_normais['ID_PREFEITURA'])
        df_reservas['escopo_pai'] = df_reservas['ID_SECRETARIA'].fillna(df_reservas['ID_PREFEITURA'])

        df_reservas_lookup = df_reservas[['escopo_pai', 'nome_ajustado', 'id_clean']].rename(columns={'id_clean': 'reserved_customer_id'}).drop_duplicates()
        df_merged = pd.merge(df_normais, df_reservas_lookup, on=['escopo_pai', 'nome_ajustado'], how='left')

        for id_reserva, id_titular in DEPARA_EXCECOES.items():
            df_merged.loc[df_merged['id_clean'] == id_titular, 'reserved_customer_id'] = id_reserva

        reservas_pareadas = df_merged['reserved_customer_id'].dropna().unique()
        df_orfas = df_reservas[~df_reservas['id_clean'].isin(reservas_pareadas)].copy()
        df_orfas['reserved_customer_id'] = None

        return pd.concat([df_merged, df_orfas], ignore_index=True), df_clean


def normalizar_para_match(nome: str) -> str:
    if not nome or pd.isna(nome): return ""
    s = unicodedata.normalize('NFD', str(nome))
    s = s.encode('ascii', 'ignore').decode('utf8').upper()
    s = re.sub(r'[\-\(\s]*\b(RESERVA|RESERVADO)\b[\)\s]*', '', s)
    s = re.sub(r'[^\w\s]', '', s)
    return re.sub(r'\s+', ' ', s).strip()

# LIMPEZA E TRATAMENTO DE DADOS
# ==============================================================================
def limpar_e_tratar_dados(df: pd.DataFrame) -> pd.DataFrame:
    print("🧹 Iniciando limpeza e tratamento dos dados do legado...")
    
    # Criamos uma cópia para evitar avisos de cópia do Pandas
    df_clean = df.copy()
    
    # 1. Lista de colunas de texto que precisam de limpeza de espaços (strip)
    colunas_texto = ['PREFEITURA', 'SECRETARIA', 'CLIENTE', 'CEP', 'ENDERECO', 'ESTADO', 'CIDADE', 'PHONE', 'CNPJ']
    
    for col in colunas_texto:
        if col in df_clean.columns:
            df_clean[col] = df_clean[col].astype(str).str.strip() 
            df_clean[col] = df_clean[col].replace(['nan', 'None', '', 'NaN', '<NA>'], None)

    # 2. Aplicação de Fallbacks (Valores padrão para campos obrigatórios que estão nulos)
    df_clean['CEP'] = df_clean['CEP'].fillna('NULO')
    df_clean['ENDERECO'] = df_clean['ENDERECO'].fillna('NÃO INFORMADO')
    df_clean['ESTADO'] = df_clean['ESTADO'].fillna('NÃO INFORMADO')
    df_clean['CIDADE'] = df_clean['CIDADE'].fillna('NÃO INFORMADO')
    df_clean['PHONE'] = df_clean['PHONE'].fillna('NÃO INFORMADO')

    print(f"✅ Tratamento concluído! {len(df_clean)} linhas prontas para processamento hierárquico.")
    return df_clean


  # Retorna o próprio ID caso não esteja nos mapeamentos de grupo


executar_truncate_tabelas(engine, TABELAS_CLIENTES)

def executar_pipeline_migracao(engine_new, engine_legado):
    try:
        df_bruto = extrair_dados_legado(engine_legado)
    except Exception as e:
        print(f"❌ Erro crítico na extração de dados: {e}")
        return

    print(f"📊 Total de linhas brutas do legado: {len(df_bruto)}")
    df = limpar_e_tratar_dados(df_bruto)
    
    linhas_antes = len(df)
    df = df[df['ORGANIZACAO'].apply(lambda x: descobrir_id_organizacao_destino(x) not in ORGANIZACOES_BLOQUEADAS)]
    if linhas_antes != len(df):
        print(f"🛑 [FILTRO] Removidas {linhas_antes - len(df)} linhas bloqueadas.")
        
    df['id_clean'] = pd.to_numeric(df['ID_CLIENTE'], errors='coerce').fillna(0).astype(int)

    # Exclusão de clientes vazios (tecnicos)
    linhas_antes_cli = len(df)
    df = df[~df['id_clean'].isin(CLIENTES_BLOQUEADOS)]
    if linhas_antes_cli != len(df):
        print(f"🛑 [FILTRO] Removidos {linhas_antes_cli - len(df)} clientes específicos bloqueados manualmente.")

    # INJEÇÃO DE EXCESSÕES
    df = regionalizar_pcpb(df)
    df = unificar_sao_luis(df)

    print("Construindo clientes...")
    # 1. Garante uma coluna de ID numérico limpa para o merge

    # 2. Máscara de corte
    mask_reserva = (
        df['CLIENTE'].str.contains(r'\b(?:RESERVA|RESERVADO)\b', case=False, na=False) & 
        ~df['id_clean'].isin(FALSOS_RESERVAS)
    )

    # 3. Separação nos dois DataFrames
    df_normais = df[~mask_reserva].copy()
    df_reservas = df[mask_reserva].copy()

    # 4. Aplica a limpeza que arranca a palavra RESERVA para gerar a chave de acoplamento
    df_normais['nome_ajustado'] = df_normais['CLIENTE'].apply(normalizar_para_match)
    df_reservas['nome_ajustado'] = df_reservas['CLIENTE'].apply(normalizar_para_match)

    # Escopo de busca (evita que o Almoxarifado da Saúde cole no Almoxarifado da Educação)
    df_normais['escopo_pai'] = df_normais['ID_SECRETARIA'].fillna(df_normais['ID_PREFEITURA'])
    df_reservas['escopo_pai'] = df_reservas['ID_SECRETARIA'].fillna(df_reservas['ID_PREFEITURA'])

    # 5. Prepara a tabela de lookup das reservas
    df_reservas_lookup = df_reservas[['escopo_pai', 'nome_ajustado', 'id_clean', 'CLIENTE']].rename(
        columns={'id_clean': 'reserved_customer_id', 'CLIENTE': 'nome_reserva_original'}
    )
    df_reservas_lookup = df_reservas_lookup.drop_duplicates(subset=['escopo_pai', 'nome_ajustado'])

    # 6. O MERGE CANÔNICO (Left Join)
    df_merged = pd.merge(df_normais, df_reservas_lookup, on=['escopo_pai', 'nome_ajustado'], how='left')

    for id_reserva, id_titular in DEPARA_EXCECOES.items():
        # Força a colagem do ID reserva diretamente no colo do titular utilizando a coluna id_clean
        df_merged.loc[df_merged['id_clean'] == id_titular, 'reserved_customer_id'] = id_reserva

    # 7. Resgate das Reservas Órfãs (As que não deram match com nenhum titular)
    reservas_pareadas = df_merged['reserved_customer_id'].dropna().unique()
    df_orfas = df_reservas[~df_reservas['id_clean'].isin(reservas_pareadas)].copy()
    df_orfas['reserved_customer_id'] = None

    # O DataFrame definitivo com todos os endereços que de fato nascerão no banco!
    df_enderecos_finais = pd.concat([df_merged, df_orfas], ignore_index=True)

    # --- 🖨️ PRINT TEMPORÁRIO DE AUDITORIA ---
    sucessos = df_merged[df_merged['reserved_customer_id'].notna()]
    print("\n" + "="*75)
    print("🕵️ RELATÓRIO DE AUDITORIA: MESCLAGEM DEFINITIVA (PANDAS MERGE)")
    print("="*75)
    print(f"✔️ SUCESSO ({len(sucessos)} reservas acopladas para dentro do titular):")
    for _, r in sucessos.head(15).iterrows():
        print(f"   🎯 TITULAR: [{r['id_clean']}] {r['CLIENTE'][:32].ljust(32)} <-- EMBUTIU: [{int(r['reserved_customer_id'])}] {r['nome_reserva_original']}")
    if len(sucessos) > 15: print(f"   ... (e mais {len(sucessos) - 15} ocultos)")

    print(f"\n❌ ÓRFÃS ({len(df_orfas)} viraram endereços avulsos):")
    for _, r in df_orfas.iterrows(): print(f"   ⚠️ ÓRFÃ: [{r['id_clean']}] '{r['CLIENTE']}'")
    print("="*75 + "\n")

    # CONSTRUÇÃO DOS DICIONÁRIOS
    prefeitura_dist = {}       
    secretaria_dit = {}     
    destino_dist = {}

    # ONDA 0: Prefeituras e Secretarias olham pro DF BRUTO COMPLETO (Garante que nenhum pai suma)
    for _, row in df.iterrows():
        if pd.notna(row['ID_PREFEITURA']):
            id_pref = int(row['ID_PREFEITURA'])
            if id_pref not in prefeitura_dist: prefeitura_dist[id_pref] = {"nome": row['PREFEITURA'], "novo_id": None, "row": row}

        if pd.notna(row['ID_SECRETARIA']):
            id_sec = int(row['ID_SECRETARIA'])
            if id_sec not in secretaria_dit: secretaria_dit[id_sec] = {"nome": row['SECRETARIA'], "id_prefeitura": int(row['ID_PREFEITURA']) if pd.notna(row['ID_PREFEITURA']) else None, "novo_id": None, "row": row}

    # ONDA 1: Endereços olham ESTRITAMENTE para o df_enderecos_finais
    for _, row in df_enderecos_finais.iterrows():
        if pd.notna(row['ID_CLIENTE']):
            id_cli = int(row['ID_CLIENTE'])
            
            destino_dist[id_cli] = {
                "nome": row['CLIENTE'],
                "id_secretaria": int(row['ID_SECRETARIA']) if pd.notna(row['ID_SECRETARIA']) else None,
                "id_prefeitura_fallback": row['ID_PREFEITURA'],
                "legacy_customer_id": id_cli,
                "reserved_customer_id": int(row['reserved_customer_id']) if pd.notna(row['reserved_customer_id']) else None,
                "row": row
            }

    print(f"📌 Mapeamento concluído: {len(prefeitura_dist)} Pais, {len(secretaria_dit)} Filhos, {len(destino_dist)} Endereços Reais.")

    try: limpar_tabela_destino(engine_new)
    except Exception as e: print(f"❌ Erro ao limpar base destino: {e}"); return

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    stats = {
        "pais": 0, 
        "filhos": 0, 
        "netos_enderecos": 0, 
        "netos_base_obrigatorios": 0, 
        "mesclados": len(sucessos)
    }

    def inserir_estrutura_customer(conn, nome, parent_id, row_data):
        org_id = descobrir_id_organizacao_destino(row_data['ORGANIZACAO'])
        cnpj_raw = str(row_data.get('CNPJ', ''))
        cnpj_clean = re.sub(r'\D', '', cnpj_raw)
        if not cnpj_clean:
            cnpj_clean = '00000000000000'

        res = conn.execute(text("""
            INSERT INTO customers (
                alias, name, cpf_cnpj, phone, organization_id, parent_id, created_at, updated_at
            ) VALUES (
                :nome, :nome, :cpf_cnpj, NULL, :org_id, :parent_id, :now, :now
            )
        """), {
            "nome": nome, 
            "cpf_cnpj": cnpj_clean,
            "org_id": org_id, 
            "parent_id": parent_id, 
            "now": now
        })
        return res.lastrowid

    print("🔄 Gravando dados no banco de dados...")
    with engine_new.begin() as conn_new:
        
        # 4 Endereços Base das Organizações
        dados_enderecos_bases = [
            {"addressable_id": 1115, "alias": "ALUCOM - BASE", "number": "40"},
            {"addressable_id": 1122, "alias": "MOREIA - BASE", "number": "50"},
            {"addressable_id": 1311, "alias": "IP - BASE", "number": "60"},
            {"addressable_id": 1378, "alias": "AS SISTEMAS - BASE", "number": "70"}
        ]
        for base in dados_enderecos_bases: 
            conn_new.execute(text("INSERT INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, created_at, updated_at) VALUES ('organization', :addressable_id, :alias, '60175205', 'RUA RIACHUELO PAPICU', :number, 'FORTALEZA', 'CE', 'Brazil', :now, :now)"), {"addressable_id": base["addressable_id"], "alias": base["alias"], "number": base["number"], "now": now})

        # PASSO A: Inserindo Pais (Prefeituras) + Seu Endereço Base Isento de ID Legado
        for id_pref, info in tqdm(prefeitura_dist.items(), desc="Inserindo Pais (Prefeituras)"):
            novo_id = inserir_estrutura_customer(conn_new, info["nome"], None, info["row"])
            prefeitura_dist[id_pref]["novo_id"] = novo_id
            stats["pais"] += 1

            conn_new.execute(text("""
                INSERT INTO addresses (
                    addressable_type, addressable_id, alias, zip, street, number, city, state, country, 
                    legacy_customer_id, reserved_customer_id, created_at, updated_at
                ) VALUES (
                    'customer', :id, :alias, 'NULO', 'NÃO INFORMADO', 'S/N', :city, :state, 'Brasil', 
                    NULL, NULL, :now, :now
                )
            """), {
                "id": novo_id,
                "alias": f"BASE - {info['nome']}",
                "city": info["row"]['CIDADE'] if pd.notna(info["row"]['CIDADE']) else 'NÃO INFORMADO',
                "state": info["row"]['ESTADO'] if pd.notna(info["row"]['ESTADO']) else 'NÃO INFORMADO',
                "now": now
            })
            stats["netos_base_obrigatorios"] += 1

        # PASSO B: Inserindo Filhos (Secretarias) + Seu Endereço Base Isento de ID Legado
        for id_sec, info in tqdm(secretaria_dit.items(), desc="Inserindo Filhos (Secretarias)"):
            id_pai = info["id_prefeitura"]
            novo_parent_id = prefeitura_dist[id_pai]["novo_id"] if id_pai in prefeitura_dist else None
            if id_pai and not novo_parent_id: continue
            
            novo_id = inserir_estrutura_customer(conn_new, info["nome"], novo_parent_id, info["row"])
            secretaria_dit[id_sec]["novo_id"] = novo_id
            stats["filhos"] += 1

            conn_new.execute(text("""
                INSERT INTO addresses (
                    addressable_type, addressable_id, alias, zip, street, number, city, state, country, 
                    legacy_customer_id, reserved_customer_id, created_at, updated_at
                ) VALUES (
                    'customer', :id, :alias, 'NULO', 'NÃO INFORMADO', 'S/N', :city, :state, 'Brasil', 
                    NULL, NULL, :now, :now
                )
            """), {
                "id": novo_id,
                "alias": f"BASE - {info['nome']}",
                "city": info["row"]['CIDADE'] if pd.notna(info["row"]['CIDADE']) else 'NÃO INFORMADO',
                "state": info["row"]['ESTADO'] if pd.notna(info["row"]['ESTADO']) else 'NÃO INFORMADO',
                "now": now
            })
            stats["netos_base_obrigatorios"] += 1

        # PASSO C: Inserção blindada dos Netos Reais do Legado
        for _, info in tqdm(destino_dist.items(), desc="Inserindo Endereços Finais"):
            id_filho = info["id_secretaria"]
            target_customer_id = secretaria_dit[id_filho]["novo_id"] if id_filho in secretaria_dit else None
            
            if not target_customer_id:
                id_pai = info["id_prefeitura_fallback"]
                target_customer_id = prefeitura_dist[id_pai]["novo_id"] if pd.notna(id_pai) and int(id_pai) in prefeitura_dist else None

            if target_customer_id:
                row_data = info["row"]
                conn_new.execute(text("""
                    INSERT INTO addresses (
                        addressable_type, addressable_id, alias, zip, street, number, city, state, country, 
                        legacy_customer_id, reserved_customer_id, created_at, updated_at
                    ) VALUES (
                        'customer', :id, :nome, :zip, :street, 'S/N', :city, :state, 'Brasil', 
                        :legacy_customer_id, :reserved_customer_id, :now, :now
                    )
                """), {
                    "id": target_customer_id, 
                    "nome": info["nome"], 
                    "zip": row_data['CEP'], 
                    "street": row_data['ENDERECO'], 
                    "city": row_data['CIDADE'], 
                    "state": row_data['ESTADO'], 
                    "legacy_customer_id": info["legacy_customer_id"],
                    "reserved_customer_id": info["reserved_customer_id"],
                    "now": now
                })
                stats["netos_enderecos"] += 1

    total_addresses = len(dados_enderecos_bases) + stats["netos_base_obrigatorios"] + stats["netos_enderecos"]
    print("\n" + "="*50)
    print("📊 RELATÓRIO FINAL DA ESTRUTURA HIERÁRQUICA")
    print("="*50)
    print(f"✅ Prefeituras (Customers):      {stats['pais']}")
    print(f"✅ Secretarias (Customers):      {stats['filhos']}")
    print(f"🏠 Endereços Base Mandatórios:  {stats['netos_base_obrigatorios']}")
    print(f"🏠 Endereços Reais (Netos):     {stats['netos_enderecos']}")
    print(f"💥 Reservas Mescladas:          {stats['mesclados']}")
    print(f"📈 Total de Linhas em Addresses: {total_addresses}")
    print("="*50)

if __name__ == "__main__":
    executar_pipeline_migracao()