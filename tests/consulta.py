import pandas as pd
from sqlalchemy import create_engine

# --- CONFIGURAÇÃO ---
# Substitua pelas suas credenciais de conexão
DB_URL = {
    "host": "172.16.0.200", "port": "3310", "db": "aluguel_legado", "user": "root", "pass": "1234"
}

engine = create_engine(
    f"mysql+pymysql://{DB_URL['user']}:{DB_URL['pass']}@{DB_URL['host']}:{DB_URL['port']}/{DB_URL['db']}"
)


def extrair_historico_movimentos():
    print("📖 Extraindo histórico completo do banco...")
    
    query = """
    SELECT
        am.id AS movimento_id,
        am.data,
        am.tipo_id,
        amt.nome AS tipo_nome,
        am.cliente_id,
        am.usuario_id,
        ae.numero AS tombo
    FROM aluguel_movimento am
    INNER JOIN aluguel_movimento_itens ami ON ami.movimento_id = am.id
    INNER JOIN aluguel_equipamentos ae ON ae.id = ami.equipamento_id
    INNER JOIN aluguel_tipos_movimento amt ON amt.id = am.tipo_id
    WHERE am.deleted_at IS NULL
    ORDER BY ae.numero, am.data ASC, am.id ASC
    """
    
    df = pd.read_sql(query, engine)
    
    # --- O "PULO DO GATO" NO PANDAS (Equivalente ao LAG do SQL 8.0) ---
    # Ordenamos por tombo e data/id para garantir a sequência cronológica
    df = df.sort_values(['tombo', 'data', 'movimento_id'])
    
    # Criamos as colunas que olham para o registro anterior do MESMO tombo
    df['tipo_id_anterior'] = df.groupby('tombo')['tipo_id'].shift(1)
    df['id_movimento_anterior'] = df.groupby('tombo')['movimento_id'].shift(1)
    
    # Filtramos apenas as devoluções (tipo_id = 2)
    devolucoes = df[df['tipo_id'] == 7].copy()
    
    return detalhar_resultado(devolucoes)

def detalhar_resultado(df_devolucoes):    
    # Exibe colunas essenciais para auditoria
    colunas_exibicao = [
        'tombo', 
        'movimento_id', 'tipo_id', 
        'id_movimento_anterior', 'tipo_id_anterior'
    ]
    return df_devolucoes[colunas_exibicao]

# --- EXECUÇÃO ---
if __name__ == "__main__":
    resultado = extrair_historico_movimentos()
    
    # Exibe no console
    print("\n--- RESUMO DE DEVOLUÇÕES (COM ALUGUEL PAI) ---")
    print(resultado.to_string(index=False))
    

    resultado.to_excel("auditoria_devolucoes.xlsx", index=False)

if __name__ == "__main__":
    extrair_historico_movimentos()