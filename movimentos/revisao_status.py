import os
import glob
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

# ==============================================================================
# DICIONÁRIO DE AGRUPAMENTO (Foco Exclusivo em Reserva e Devolução)
# ==============================================================================
AGRUPAMENTO_STATUS = {
    # 1: [7, 8, 11],       # DISPONÍVEL PARA LOCAÇÃO (Ignorado)
    # 2: [1],              # ALUGADO (Ignorado para não alugar nada)
    3: [15],               # 🎯 RESERVADO
    # 4: [2],              # EMPRESTADO (Ignorado)
    # 5: [5, 16],          # USO INTERNO (Ignorado)
    # 6: [3, 9, 12, 13],   # EM MANUTENÇÃO (Ignorado)
    # 7: [6],              # NA GARANTIA (Ignorado)
    8: [14],               # 🎯 EM DEVOLUÇÃO
    # 9: [4, 10]           # BAIXADO (Ignorado)
}

MAPA_STATUS_LEGADO_PARA_NOVO = {
    status_legado: status_novo 
    for status_novo, lista_legados in AGRUPAMENTO_STATUS.items() 
    for status_legado in lista_legados
}

PASTA_PARQUETS = "./docs/parquets"

def limpar_codigo(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    return s[:-2] if s.endswith(".0") else s

class RevisaoStatusEquipamentos:
    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado

    def _carregar_tombos_parquet(self):
        """Carrega todos os tombos das planilhas Parquet para criar o escudo de proteção"""
        print("📖 Lendo arquivos Parquet para criar a lista de intocáveis (Verdade Absoluta)...")
        arquivos_parquet = glob.glob(os.path.join(PASTA_PARQUETS, "*.parquet"))
        
        if not arquivos_parquet:
            print("   ⚠️ Nenhum arquivo Parquet encontrado. A proteção do Parquet será ignorada.")
            return set()

        lista_dfs = []
        for arq in arquivos_parquet:
            df_temp = pd.read_parquet(arq)
            df_temp.columns = df_temp.columns.str.upper() 
            lista_dfs.append(df_temp)
        
        df_csv = pd.concat(lista_dfs, ignore_index=True)
        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        
        tombos_limpos = set(df_csv['TOMBO'].astype(int).astype(str).apply(limpar_codigo))
        print(f"   🛡️ {len(tombos_limpos)} tombos exclusivos do Parquet protegidos contra alterações.")
        
        return tombos_limpos

    def executar(self):
        print("\n" + "=" * 70)
        print("🔍 MÓDULO: CONCILIAÇÃO FINAL DE STATUS (RESERVAS E DEVOLUÇÕES)")
        print("=" * 70)

        tombos_intocaveis = self._carregar_tombos_parquet()

        # 1. Extração do Novo Banco (Com Join Dinâmico)
        print("📖 Lendo status atual no Banco NOVO...")
        query_novo = """
            SELECT equip.id,
                   equip.number,
                   equip.status_id AS status_atual,
                   status.name AS status_name
            FROM equipments AS equip
            INNER JOIN status ON equip.status_id = status.id
            WHERE equip.deleted_at IS NULL;
        """
        with self.engine_new.connect() as conn:
            df_novo = pd.read_sql(text(query_novo), conn)
            # Dicionário dinâmico para pegar o nome do status "Corrigido"
            df_status_ref = pd.read_sql("SELECT id, name FROM status", conn)
            dict_nome_status_novo = dict(zip(df_status_ref['id'], df_status_ref['name']))

        # 2. Extração do Legado (O SQL FILTRA O TIPO 3 NA FONTE!)
        print("📖 Lendo situação original no Banco LEGADO (Blindando as substituições pendentes)...")
        query_legado = """
            SELECT alq.id,
                   alq.numero,
                   alq.situacao_id,
                   alqdsitu.nome AS situacao_nome
            FROM aluguel_equipamentos alq
            INNER JOIN aluguel_situacao alqdsitu ON alq.situacao_id = alqdsitu.id
            
            -- Subquery para pegar apenas o movimento ABSOLUTO mais recente
            LEFT JOIN (
                SELECT mi.equipamento_id, MAX(m.id) as max_mov_id
                FROM aluguel_movimento_itens mi
                INNER JOIN aluguel_movimento m ON m.id = mi.movimento_id
                WHERE m.deleted_at IS NULL
                GROUP BY mi.equipamento_id
            ) ult_mov ON ult_mov.equipamento_id = alq.id
            
            -- Join para descobrir de qual tipo era esse último movimento
            LEFT JOIN aluguel_movimento mov ON mov.id = ult_mov.max_mov_id
            
            WHERE alq.deleted_at IS NULL
              AND (mov.tipo_id NOT IN (3) OR mov.tipo_id IS NULL)
        """
        with self.engine_legado.connect() as conn:
            df_legado = pd.read_sql(text(query_legado), conn)

        # 3. Limpeza das chaves de cruzamento
        print("🧠 Cruzando os dados (Exigindo Match Perfeito: ID + Tombo)...")
        df_novo['tombo_clean'] = df_novo['number'].apply(limpar_codigo)
        df_legado['tombo_clean'] = df_legado['numero'].apply(limpar_codigo)

        df_novo = df_novo[df_novo['tombo_clean'] != ""]
        df_legado = df_legado[df_legado['tombo_clean'] != ""]

        df_novo['id'] = df_novo['id'].astype(int)
        df_legado['id'] = df_legado['id'].astype(int)

        # Merge Omitirá automaticamente tudo que foi barrado pelo "NOT IN (3)" do Legado
        df_merge = pd.merge(
            df_novo, 
            df_legado[['id', 'tombo_clean', 'situacao_id', 'situacao_nome']], 
            on=['id', 'tombo_clean'], 
            how='inner'
        )

        # 4. Aplicação do De/Para e Filtro das Divergências
        divergencias = []
        ignorados_parquet = 0

        for _, row in tqdm(df_merge.iterrows(), total=df_merge.shape[0], desc="Analisando Equipamentos"):
            tombo = row['tombo_clean']
            
            # 🛡️ REGRA 1: Se estiver no Parquet, é intocável (o Aluguel já cuidou disso).
            if tombo in tombos_intocaveis:
                ignorados_parquet += 1
                continue

            sit_legado = row['situacao_id']
            if pd.isna(sit_legado):
                continue
                
            sit_legado = int(sit_legado)

            status_atual = int(row['status_atual']) if pd.notna(row['status_atual']) else None
            
            # 🛡️ REGRA 2: Só retorna valor se a situação estiver mapeada em AGRUPAMENTO_STATUS
            status_esperado = MAPA_STATUS_LEGADO_PARA_NOVO.get(sit_legado)

            if status_esperado is not None and status_esperado != status_atual:
                
                nome_leg = row['situacao_nome']
                nome_err = row['status_name']
                nome_cor = dict_nome_status_novo.get(status_esperado, "DESCONHECIDO")

                divergencias.append({
                    "e_id": int(row['id']),
                    "tombo": row['number'],
                    "id_legado": sit_legado,
                    "status_legado": nome_leg,
                    "id_banco_novo_errado": status_atual,
                    "status_banco_novo_errado": nome_err,
                    "id_corrigido": status_esperado,
                    "status_corrigido": nome_cor
                })

        # 5. Atualização em Massa no Banco Novo
        total_divergencias = len(divergencias)
        
        print("\n" + "=" * 50)
        print("📊 RELATÓRIO DA VARREDURA (RESERVAS E DEVOLUÇÕES)")
        print("=" * 50)
        print(f"📦 Total Verificado no Cruzamento Exato: {df_merge.shape[0]}")
        print(f"🛡️  Ignorados (Protegidos pelo Parquet): {ignorados_parquet}")
        print(f"🛠️  Total de Correções Aplicadas:        {total_divergencias}")
        print("=" * 50)
        
        if total_divergencias == 0:
            print("\n✅ As Reservas e Devoluções do banco novo estão perfeitamente sincronizadas com o legado.")
            return

        print(f"\n⚠️ Executando correção de {total_divergencias} divergências no MySQL...")

        updates = [{"e_id": item["e_id"], "s_id": item["id_corrigido"]} for item in divergencias]

        with self.engine_new.begin() as conn:
            conn.execute(
                text("UPDATE equipments SET status_id = :s_id WHERE id = :e_id"),
                updates
            )

        df_log = pd.DataFrame(divergencias)
        df_log.to_csv("docs/log_status_corrigidos.csv", index=False)
        print("📄 Log detalhado salvo em 'docs/log_status_corrigidos.csv'")


# ==============================================================================
# WRAPPER (Ponte para o main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    app_revisao = RevisaoStatusEquipamentos(eng_novo, eng_legado)
    app_revisao.executar()