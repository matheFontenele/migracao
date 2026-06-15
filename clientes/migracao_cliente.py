import os
import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, text
from datetime import datetime
from tqdm import tqdm

# ==============================================================================
# LIMPEZA E TRATAMENTO DE DADOS
# ==============================================================================

def limpar_e_tratar_dados(df: pd.DataFrame) -> pd.DataFrame:
    print("🧹 Iniciando limpeza e tratamento dos dados do legado...")
    
    # Criamos uma cópia para evitar avisos de cópia do Pandas
    df_clean = df.copy()
    
    # 1. Lista de colunas de texto que precisam de limpeza de espaços (strip)
    colunas_texto = ['PREFEITURA', 'SECRETARIA', 'CLIENTE', 'CEP', 'ENDERECO', 'ESTADO', 'CIDADE', 'PHONE']
    
    for col in colunas_texto:
        if col in df_clean.columns:
            # Converte para string, remove espaços extras nas pontas
            df_clean[col] = df_clean[col].astype(str).str.strip() 
            
            # Substitui strings fantasmas geradas pelo banco/pandas por None real do Python
            df_clean[col] = df_clean[col].replace(['nan', 'None', '', 'NaN', '<NA>'], None)

    # 2. Aplicação de Fallbacks (Valores padrão para campos obrigatórios que estão nulos)
    df_clean['CEP'] = df_clean['CEP'].fillna('NULO')
    df_clean['ENDERECO'] = df_clean['ENDERECO'].fillna('NÃO INFORMADO')
    df_clean['ESTADO'] = df_clean['ESTADO'].fillna('NÃO INFORMADO')
    df_clean['CIDADE'] = df_clean['CIDADE'].fillna('NÃO INFORMADO')
    
    # Tratamento para telefone (caso seja nulo)
    df_clean['PHONE'] = df_clean['PHONE'].fillna('NÃO INFORMADO')

    print(f"✅ Tratamento concluído! {len(df_clean)} linhas prontas para processamento hierárquico.")
    return df_clean


# ==============================================================================
# CONFIGURAÇÕES DE MAPEAMENTO E BLOQUEIO DE ORGANIZAÇÕES
# ==============================================================================
# Grupos do legado (IDs de origem)
MAPPING_ALUCOM = {1327, 1329, 1353, 1363, 1365, 1367, 1370, 1373, 1376, 1377}
MAPPING_IP = {1346, 1349, 1350, 1364, 1368, 1371}
MAPPING_MOREIA = {1313, 1326, 1328, 1358, 1369}
MAPPING_AS = {1378}

# LISTA EM ABERTO: IDs de destino que devem ser DELETADOS/IGNORADOS na migração
ORGANIZACOES_BLOQUEADAS = {1123, 1366}


# Configurações de Conexão
DB_CONFIG_NEW = {
    "host": "localhost",
    "port": "3307",
    "db": "controle-interno",
    "user": "root",
    "pass": "root"
}

DB_CONFIG_LEGADO = {
    "host": "172.16.0.200",
    "port": "3310",
    "db": "aluguel_legado",
    "user": "root",
    "pass": "1234"
}

# Criação das Engines utilizando a estrutura exata solicitada
engine_new = create_engine(
    f"mysql+pymysql://{DB_CONFIG_NEW['user']}:{DB_CONFIG_NEW['pass']}@{DB_CONFIG_NEW['host']}:{DB_CONFIG_NEW['port']}/{DB_CONFIG_NEW['db']}"
)
engine_legado = create_engine(
    f"mysql+pymysql://{DB_CONFIG_LEGADO['user']}:{DB_CONFIG_LEGADO['pass']}@{DB_CONFIG_LEGADO['host']}:{DB_CONFIG_LEGADO['port']}/{DB_CONFIG_LEGADO['db']}"
)

print("Conexão com os bancos estabelecida.")


def descobrir_id_organizacao_destino(id_legado):
    """Mapeia o ID do legado para o ID correspondente no banco novo."""
    if pd.isna(id_legado):
        return 1115  # Padrão caso seja nulo
    
    id_legado_int = int(id_legado)
    if id_legado_int in MAPPING_ALUCOM:
        return 1115
    elif id_legado_int in MAPPING_IP:
        return 1311
    elif id_legado_int in MAPPING_MOREIA:
        return 1122
    elif id_legado_int in MAPPING_AS:
        return 1378
    
    return id_legado_int  # Retorna o próprio ID caso não esteja nos mapeamentos de grupo


def extrair_dados_legado(engine):
    query = """
    SELECT
        ald.id as ID_PREFEITURA,
        ald.nome as PREFEITURA,
        als.id as ID_SECRETARIA,
        als.nome as SECRETARIA,
        ac.id as ID_CLIENTE,
        ac.nome_razao_social as CLIENTE,
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


def limpar_tabela_destino(engine):
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        conn.execute(text("TRUNCATE TABLE addresses"))
        conn.execute(text("TRUNCATE TABLE customers"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def executar_pipeline_migracao():
    try:
        df_bruto = extrair_dados_legado(engine_legado)
    except Exception as e:
        print(f"❌ Erro crítico na extração de dados: {e}")
        return

    print(f"📊 Total de linhas brutas do legado: {len(df_bruto)}")
    
    # CHAMADA DO SCRIPT DE TRATAMENTO ISOLADO
    df = limpar_e_tratar_dados(df_bruto)
    
    # 🚫 APLICAÇÃO DO FILTRO DE ORGANIZAÇÕES BLOQUEADAS
    linhas_antes = len(df)
    
    # Filtra mantendo apenas as linhas onde a organização destino calculada NÃO está na lista de bloqueadas
    df = df[df['ORGANIZACAO'].apply(lambda x: descobrir_id_organizacao_destino(x) not in ORGANIZACOES_BLOQUEADAS)]
    
    linhas_depois = len(df)
    if linhas_antes != linhas_depois:
        print(f"🛑 [FILTRO] Removidas {linhas_antes - linhas_depois} linhas pertencentes às organizações bloqueadas: {ORGANIZACOES_BLOQUEADAS}")

    # 1. CONSTRUÇÃO DOS DICIONÁRIOS HIERÁRQUICOS
    prefeitura_dist = {}       
    secretaria_dit = {}     
    destino_dist = {}      

    for _, row in df.iterrows():
        if pd.notna(row['ID_PREFEITURA']):
            id_pref = int(row['ID_PREFEITURA'])
            if id_pref not in prefeitura_dist:
                prefeitura_dist[id_pref] = {"nome": row['PREFEITURA'], "novo_id": None, "row": row}

        if pd.notna(row['ID_SECRETARIA']):
            id_sec = int(row['ID_SECRETARIA'])
            if id_sec not in secretaria_dit:
                secretaria_dit[id_sec] = {
                    "nome": row['SECRETARIA'],
                    "id_prefeitura": int(row['ID_PREFEITURA']) if pd.notna(row['ID_PREFEITURA']) else None,
                    "novo_id": None,
                    "row": row
                }

        if pd.notna(row['ID_CLIENTE']):
            id_cli = int(row['ID_CLIENTE'])
            if id_cli not in destino_dist:
                destino_dist[id_cli] = {
                    "nome": row['CLIENTE'],
                    "id_secretaria": int(row['ID_SECRETARIA']) if pd.notna(row['ID_SECRETARIA']) else None,
                    "novo_id": None,
                    "row": row
                }

    print(f"📌 Mapeamento concluído: {len(prefeitura_dist)} Pais, {len(secretaria_dit)} Filhos, {len(destino_dist)} Netos estruturados.")

    try:
        limpar_tabela_destino(engine_new)
    except Exception as e:
        print(f"❌ Erro ao limpar base destino: {e}")
        return

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    stats = {"pais": 0, "filhos": 0, "netos_enderecos": 0}

    def inserir_estrutura_customer(conn, nome, parent_id, row_data):
        org_id = descobrir_id_organizacao_destino(row_data['ORGANIZACAO'])
        
        res = conn.execute(text("""
            INSERT INTO customers (alias, name, cpf_cnpj, phone, organization_id, parent_id, created_at, updated_at)
            VALUES (:nome, :nome, '00000000000000', NULL, :org_id, :parent_id, :now, :now)
        """), {"nome": nome, "org_id": org_id, "parent_id": parent_id, "now": now})
        
        return res.lastrowid

    print("🔄 Gravando dados no banco de dados...")
    with engine_new.begin() as conn_new:
        
        # ======================================================================
        # 🌟 NOVO PASSO: INSERÇÃO SEGURA DOS ENDEREÇOS OPERACIONAIS DAS ORGANIZAÇÕES
        # ======================================================================
        print("🏠 Inserindo os 4 endereços operacionais mestre na tabela 'addresses'...")
        dados_enderecos_bases = [
            {"addressable_id": 1115, "alias": "ALUCOM - BASE", "number": "40"},
            {"addressable_id": 1122, "alias": "MOREIA - BASE", "number": "50"},
            {"addressable_id": 1311, "alias": "IP - BASE", "number": "60"},
            {"addressable_id": 1378, "alias": "AS SISTEMAS - BASE", "number": "70"}
        ]
        
        for base in dados_enderecos_bases:
            conn_new.execute(text("""
                INSERT INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, created_at, updated_at)
                VALUES ('organization', :addressable_id, :alias, '60175205', 'RUA RIACHUELO PAPICU', :number, 'FORTALEZA', 'CE', 'Brazil', :now, :now)
            """), {
                "addressable_id": base["addressable_id"],
                "alias": base["alias"],
                "number": base["number"],
                "now": now
            })
            
        # 🔍 Checagem física de segurança e armazenamento estruturado dos IDs criados
        print("🔍 Checando e salvando referências de IDs gerados no banco:")
        result_confirmacao = conn_new.execute(text("""
            SELECT id, addressable_id, alias FROM addresses 
            WHERE addressable_type = 'organization' 
            AND addressable_id IN (1115, 1122, 1311, 1378)
        """))
        
        mapa_ids_bases = {}
        for row_end in result_confirmacao:
            # Garante compatibilidade de desestruturação da row independente da versão do SQLAlchemy
            id_banco, org_id, alias = row_end[0], row_end[1], row_end[2]
            mapa_ids_bases[org_id] = id_banco
            print(f"   -> [CONFIRMADO] {alias} (Org {org_id}) fixado com ID definitivo: {id_banco}")
            
        # ======================================================================

        # PASSO A: Inserir todos os PAIS (Prefeituras) -> Apenas Customer
        for id_pref, info in tqdm(prefeitura_dist.items(), desc="Inserindo Pais (Prefeituras)", unit="pref"):
            novo_id = inserir_estrutura_customer(conn_new, info["nome"], parent_id=None, row_data=info["row"])
            prefeitura_dist[id_pref]["novo_id"] = novo_id
            stats["pais"] += 1

        # PASSO B: Inserir todos os FILHOS (Secretarias) -> Apenas Customer
        for id_sec, info in tqdm(secretaria_dit.items(), desc="Inserindo Filhos (Secretarias)", unit="sec"):
            id_pai_legado = info["id_prefeitura"]
            novo_parent_id = prefeitura_dist[id_pai_legado]["novo_id"] if id_pai_legado in prefeitura_dist else None
            
            if id_pai_legado and not novo_parent_id:
                continue

            novo_id = inserir_estrutura_customer(conn_new, info["nome"], parent_id=novo_parent_id, row_data=info["row"])
            secretaria_dit[id_sec]["novo_id"] = novo_id
            stats["filhos"] += 1

        # PASSO C: Inserir os NETOS apenas como ENDEREÇOS vinculados ao sub-cliente (Filho ou Pai)
        for id_cli, info in tqdm(destino_dist.items(), desc="Inserindo Netos (Endereços)", unit="end"):
            id_filho_legado = info["id_secretaria"]
            
            target_customer_id = secretaria_dit[id_filho_legado]["novo_id"] if id_filho_legado in secretaria_dit else None
            
            if not target_customer_id:
                id_pai_legado = info["row"]["ID_PREFEITURA"]
                target_customer_id = prefeitura_dist[id_pai_legado]["novo_id"] if pd.notna(id_pai_legado) and int(id_pai_legado) in prefeitura_dist else None

            if target_customer_id:
                row_data = info["row"]
                
                conn_new.execute(text("""
                    INSERT INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, legacy_customer_id, created_at, updated_at)
                    VALUES ('customer', :id, :nome, :zip, :street, 'S/N', :city, :state, 'Brasil', :legacy_customer_id, :now, :now)
                """), {
                    "id": target_customer_id, 
                    "nome": info["nome"], 
                    "zip": row_data['CEP'], 
                    "street": row_data['ENDERECO'], 
                    "city": row_data['CIDADE'], 
                    "state": row_data['ESTADO'], 
                    "legacy_customer_id": int(id_cli),
                    "now": now
                })
                
                stats["netos_enderecos"] += 1

    print("\n" + "="*50)
    print("📊 RELATÓRIO FINAL DA ESTRUTURA HIERÁRQUICA")
    print("="*50)
    print(f"✅ Prefeituras estruturadas: {stats['pais']}")
    print(f"✅ Secretarias estruturadas: {stats['filhos']}")
    print(f"🏠 Endereços Finais salvos:    {stats['netos_enderecos']}")
    print("="*50)


if __name__ == "__main__":
    executar_pipeline_migracao()