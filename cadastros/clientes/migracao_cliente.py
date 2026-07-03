import re
import unicodedata
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from config.config import (
    CLIENTES_BLOQUEADOS,
    ORGANIZACOES_BLOQUEADAS,
    FALSOS_RESERVAS,
    BASES_AVULSOS, ENDERECOS_BASES
)
from utils.sanetizador import normalizar_para_match, executar_truncate_tabelas
from utils.mapeador import descobrir_id_organizacao

TABELAS_CLIENTES = [
    'addresses', 
    'customers'
]

#De/PARA de clientes e dicionarios
DEPARA_EXCECOES = {
        10824: 10711,
        4182: 4174,
        10450: 3915,
        10427: 10414,
        10853: 10806,
        10434: 3634,
        3362: 3332,
        4439: 2879,
        4467: 4465,
        4464: 4466,
        11920: 11919,
        10935: 10823,
}
MAPA_SAO_LUIS = {
        # === IPAM (Secretaria 1194) ===
        11791: (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        4467:  (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        4465:  (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        11932: (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        11933: (1194, "INSTITUTO DE PREVIDÊNCIA E ASSISTÊNCIA DO MUNICÍPIO - IPAM"),
        
        # === SEMUSC (Secretaria 1197) ===
        4464:  (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        4466:  (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        10502: (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        10513: (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        11924: (1197, "SECRETARIA MUNICIPAL DE SEGURANÇA COM CIDADANIA - SEMUSC"),
        
        # === SMTT (Secretaria 1303) ===
        10714: (1303, "SECRETARIA DE TRÂNSITO E TRANSPORTES - SMTT"),
        10591: (1303, "SECRETARIA DE TRÂNSITO E TRANSPORTES - SMTT"),
        11928: (1303, "SECRETARIA DE TRÂNSITO E TRANSPORTES - SMTT"),
        
        # === SEMFAZ / PM SÃO LUIS (Secretaria 1220) ===
        10400: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
        10401: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
        11919: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
        11920: (1220, "SECRETARIA DA FAZENDA - SEMFAZ"),
    }
SAO_LUIS_CONFIG = {
    "ID_PREF_ALVO": 287,
    "NOME_PREF_ALVO": "PREFEITURA MUNICIPAL DE SÃO LUÍS"
}
MAPA_REGIOS_PCPB = {
    "CAMPINA GRANDE": "CAMPINA GRANDE",
    "GUARABIRA": "GUARABIRA",
    "JOAO PESSOA": "JOÃO PESSOA",
    "CAPITAL": "JOÃO PESSOA",
    "MANGABEIRA": "JOÃO PESSOA",
    "PATOS": "PATOS",
    "SANTA RITA": "SANTA RITA",
    "ALHANDRA": "ALHANDRA",
    "CAAPORA": "CAAPORÃ",
    "MAMANGUAPE": "MAMANGUAPE",
    "CONDE": "CONDE",
    "PEDRAS DE FOGO": "PEDRAS DE FOGO",
    "PITIMBU": "PITIMBU",
    "PILAR": "PILAR",
    "PILOES": "PILÕES",
    "JERICO": "JERICÓ"
}
MAPA_IDS_SINTETICOS_PCPB = {
    "JOÃO PESSOA": 900001,
    "CAMPINA GRANDE": 900002,
    "GUARABIRA": 900003,
    "PATOS": 900004,
    "SANTA RITA": 900005,
    "ALHANDRA": 900006,
    "CAAPORÃ": 900007,
    "MAMANGUAPE": 900008,
    "CONDE": 900009,
    "PEDRAS DE FOGO": 900010,
    "PITIMBU": 900011,
    "PILAR": 900012,
    "PILÕES": 900013,
    "JERICÓ": 900014,
    "SEDE / OUTROS": 900015
}
MAPA_MINISTERIO_RELACOES = {
    "PREFEITURA": (375, 322),
    "SECRETARIA": (1424, 1347)
}

class MigracaoClientes:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.stats = {"pais": 0, "filhos": 0, "enderecos": 0, "mesclados": 0}


    # ==============================================================================
    # 1. EXTRAÇÃO
    # ==============================================================================

    def _extrair(self):
        print("📖 Extraindo dados do legado...")
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
        with self.engine_legado.connect() as conn:
            return pd.read_sql(text(query), conn)


     # ==============================================================================
    #==============================================================================
    # 2. APLICANDO REGRAS DE EXCEÇÃO E LIMPEZA DE DADOS
    # ==============================================================================
    def _unificar_sao_luis(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        mask_sl = df_modificado['id_clean'].isin(MAPA_SAO_LUIS.keys())
        
        for idx, row in df_modificado[mask_sl].iterrows():
            id_cliente = int(row['id_clean'])
            id_sec_alvo, nome_sec_alvo = MAPA_SAO_LUIS[id_cliente]
            
            # Remove o sufixo "2026" do nome do cliente final (Neto) e limpa espaços
            nome_cliente = str(row['CLIENTE'])
            nome_cliente_limpo = re.sub(r'\b2026\b', '', nome_cliente).strip()
            
            # Aplica a unificação na linha
            df_modificado.at[idx, 'ID_PREFEITURA'] = SAO_LUIS_CONFIG['ID_PREF_ALVO']
            df_modificado.at[idx, 'PREFEITURA'] = SAO_LUIS_CONFIG['NOME_PREF_ALVO']
            df_modificado.at[idx, 'ID_SECRETARIA'] = id_sec_alvo
            df_modificado.at[idx, 'SECRETARIA'] = nome_sec_alvo
            df_modificado.at[idx, 'CLIENTE'] = nome_cliente_limpo
        return df_modificado
    def _regionalizar_pcpb(self, df: pd.DataFrame) -> pd.DataFrame:
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
    def _unificar_ministerio_relacoes(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()

        pref_alvo, pref_origem = MAPA_MINISTERIO_RELACOES["PREFEITURA"]
        sec_alvo, sec_origem = MAPA_MINISTERIO_RELACOES["SECRETARIA"]

        id_pref_serie = pd.to_numeric(df_modificado['ID_PREFEITURA'], errors='coerce')
        id_sec_serie = pd.to_numeric(df_modificado['ID_SECRETARIA'], errors='coerce')
        
        mask_pref = id_pref_serie == pref_origem
        
        if mask_pref.any():
            nomes_pref_alvo = df_modificado.loc[id_pref_serie == pref_alvo, 'PREFEITURA']
            nome_pref_padrao = nomes_pref_alvo.iloc[0] if not nomes_pref_alvo.empty else "MINISTÉRIO DAS RELAÇÕES EXTERIORES"
            
            df_modificado.loc[mask_pref, 'ID_PREFEITURA'] = pref_alvo
            df_modificado.loc[mask_pref, 'PREFEITURA'] = nome_pref_padrao

        mask_sec = id_sec_serie == sec_origem  # Busca quem é 1347
        
        if mask_sec.any():
            nomes_sec_alvo = df_modificado.loc[id_sec_serie == sec_alvo, 'SECRETARIA']
            nome_sec_padrao = nomes_sec_alvo.iloc[0] if not nomes_sec_alvo.empty else "SECRETARIA MRE"
            
            df_modificado.loc[mask_sec, 'ID_SECRETARIA'] = sec_alvo
            df_modificado.loc[mask_sec, 'SECRETARIA'] = nome_sec_padrao
        
        return df_modificado



    def _limpar_cnpj(self, cnpj_raw):
        c = re.sub(r'\D', '', str(cnpj_raw))
        return c if c else '00000000000000'
    
    def _transformar(self, df):
        print("🧹 Iniciando limpeza e aplicação de regras de negócio...")
        df_clean = df.copy()
        
        # Limpeza básica
        colunas_texto = ['PREFEITURA', 'SECRETARIA', 'CLIENTE', 'CEP', 'ENDERECO', 'ESTADO', 'CIDADE', 'CNPJ']
        for col in colunas_texto:
            if col in df_clean.columns:
                df_clean[col] = df_clean[col].astype(str).str.strip().replace(['nan', 'None', ''], None)
        
        # Preenche os campos grandes normalmente
        df_clean[['ENDERECO', 'ESTADO', 'CIDADE', 'PHONE']] = df_clean[['ENDERECO', 'ESTADO', 'CIDADE', 'PHONE']].fillna('NÃO INFORMADO')
        
        # O CEP ganha um tratamento VIP: Se vazio, vira 'NULO'. E cortamos estritamente nos 8 primeiros caracteres para o banco não chorar.
        df_clean['CEP'] = df_clean['CEP'].fillna('NULO').astype(str).str.slice(0, 8)
        
        df_clean['ORG_DESTINO'] = df_clean['ORGANIZACAO'].apply(descobrir_id_organizacao)
        df_clean = df_clean[~df_clean['ORG_DESTINO'].isin(ORGANIZACOES_BLOQUEADAS)]
        df_clean['id_clean'] = pd.to_numeric(df_clean['ID_CLIENTE'], errors='coerce').fillna(0).astype(int)
        df_clean = df_clean[~df_clean['id_clean'].isin(CLIENTES_BLOQUEADOS)]

        # --- APLICA EXCEÇÕES (São Luís e PCPB) ---
        df_clean = self._regionalizar_pcpb(df_clean)
        df_clean = self._unificar_sao_luis(df_clean)
        df_clean = self._unificar_ministerio_relacoes(df_clean)

        # --- MERGE DE RESERVAS ---
        mask_reserva = df_clean['CLIENTE'].str.contains(r'\b(?:RESERVA|RESERVADO)\b', case=False, na=False) & ~df_clean['id_clean'].isin(FALSOS_RESERVAS)
        df_normais = df_clean[~mask_reserva].copy()
        df_reservas = df_clean[mask_reserva].copy()

        df_normais['nome_ajustado'] = df_normais['CLIENTE'].apply(normalizar_para_match)
        df_reservas['nome_ajustado'] = df_reservas['CLIENTE'].apply(normalizar_para_match)
        df_normais['escopo_pai'] = df_normais['ID_SECRETARIA'].fillna(df_normais['ID_PREFEITURA'])
        df_reservas['escopo_pai'] = df_reservas['ID_SECRETARIA'].fillna(df_reservas['ID_PREFEITURA'])

        df_reservas_lookup = df_reservas[['escopo_pai',
                                          'nome_ajustado',
                                          'id_clean',
                                          'CLIENTE']].rename(columns={'id_clean': 'reserved_customer_id', 'CLIENTE': 'nome_reserva_original'}).drop_duplicates(subset=['escopo_pai', 'nome_ajustado'])
        df_merged = pd.merge(df_normais, df_reservas_lookup, on=['escopo_pai', 'nome_ajustado'], how='left')

        for id_reserva, id_titular in DEPARA_EXCECOES.items():
            df_merged.loc[df_merged['id_clean'] == id_titular, 'reserved_customer_id'] = id_reserva

        reservas_pareadas = df_merged['reserved_customer_id'].dropna().unique()
        df_orfas = df_reservas[~df_reservas['id_clean'].isin(reservas_pareadas)].copy()
        df_orfas['reserved_customer_id'] = None

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

        return pd.concat([df_merged, df_orfas], ignore_index=True), df_clean

    # ==============================================================================
    # 3. CARGA (Bulk Inserts)
    # ==============================================================================
    def _carregar(self, df_enderecos_finais, df_bruto):
        print("🔄 Estruturando Dicionários de Pais e Filhos...")
        prefeituras, secretarias, destinos = {}, {}, {}

        for _, row in df_bruto.iterrows():
            if pd.notna(row['ID_PREFEITURA']):
                prefeituras[int(row['ID_PREFEITURA'])] = {"nome": row['PREFEITURA'], "row": row}
            if pd.notna(row['ID_SECRETARIA']):
                secretarias[int(row['ID_SECRETARIA'])] = {"nome": row['SECRETARIA'], "id_pai": int(row['ID_PREFEITURA']) if pd.notna(row['ID_PREFEITURA']) else None, "row": row}

        for _, row in df_enderecos_finais.iterrows():
            if pd.notna(row['ID_CLIENTE']):
                destinos[int(row['ID_CLIENTE'])] = {
                    "nome": row['CLIENTE'], "id_sec": int(row['ID_SECRETARIA']) if pd.notna(row['ID_SECRETARIA']) else None,
                    "id_pref": row['ID_PREFEITURA'], "legacy_id": int(row['ID_CLIENTE']),
                    "res_id": int(row['reserved_customer_id']) if pd.notna(row['reserved_customer_id']) else None, "row": row
                }

        # ==============================================================================
        # 🏢 CRIAÇÃO DE ENDEREÇOS DE BOX/ESTOQUES PARA CADA ORGANIZAÇÃO
        # ==============================================================================
        enderecos_batch = ENDERECOS_BASES.copy()
        
        organizacoes = [end for end in enderecos_batch if end.get("type") == "organization"]
        
        # 2. Criamos uma lista temporária para não quebrar a iteração
        novos_boxes = []

        for base in organizacoes:
            for box in BASES_AVULSOS.values():
                novos_boxes.append({
                    "type": "organization",
                    "id": base["id"],
                    "alias": box["alias"],
                    "zip": box["zip"],
                    "street": box["street"],
                    "num": box["number"],
                    "city": box["city"],
                    "state": box["state"],
                    "leg_id": None,
                    "res_id": None
                })
        
        enderecos_batch.extend(novos_boxes)

        print("🚀 Gravando dados no banco de dados...")
        with self.engine_new.begin() as conn:
            query_cust = text("INSERT INTO customers (alias, name, cpf_cnpj, organization_id, parent_id, created_at, updated_at) VALUES (:n, :n, :d, :o, :p, :now, :now)")
            
            # Pais
            dict_traducao_pais = {}
            for id_legado, info in tqdm(prefeituras.items(), desc="Inserindo Prefeituras"):
                res = conn.execute(query_cust, {"n": info["nome"], "d": self._limpar_cnpj(info["row"].get('CNPJ')), "o": descobrir_id_organizacao(info["row"]['ORGANIZACAO']), "p": None, "now": self.now})
                novo_id = res.lastrowid
                dict_traducao_pais[id_legado] = novo_id
                self.stats["pais"] += 1
                enderecos_batch.append({"type": "customer", "id": novo_id, "alias": f"BASE - {info['nome']}", "zip": "NULO", "street": "NÃO INFORMADO", "num": "S/N", "city": info["row"]['CIDADE'], "state": info["row"]['ESTADO'], "leg_id": None, "res_id": None})

            # Filhos
            dict_traducao_filhos = {}
            for id_legado, info in tqdm(secretarias.items(), desc="Inserindo Secretarias"):
                id_pai = dict_traducao_pais.get(info["id_pai"])
                if info["id_pai"] and not id_pai: continue
                res = conn.execute(query_cust, {"n": info["nome"], "d": self._limpar_cnpj(info["row"].get('CNPJ')), "o": descobrir_id_organizacao(info["row"]['ORGANIZACAO']), "p": id_pai, "now": self.now})
                novo_id = res.lastrowid
                dict_traducao_filhos[id_legado] = novo_id
                self.stats["filhos"] += 1
                enderecos_batch.append({"type": "customer", "id": novo_id, "alias": f"BASE - {info['nome']}", "zip": "NULO", "street": "NÃO INFORMADO", "num": "S/N", "city": info["row"]['CIDADE'], "state": info["row"]['ESTADO'], "leg_id": None, "res_id": None})

            # Netos (Preparação)
            for id_legado, info in tqdm(destinos.items(), desc="Preparando Endereços Finais"):
                target_id = dict_traducao_filhos.get(info["id_sec"]) or dict_traducao_pais.get(info["id_pref"])
                if target_id:
                    enderecos_batch.append({"type": "customer", "id": target_id, "alias": info["nome"], "zip": info["row"]['CEP'], "street": info["row"]['ENDERECO'], "num": "S/N", "city": info["row"]['CIDADE'], "state": info["row"]['ESTADO'], "leg_id": info["legacy_id"], "res_id": info["res_id"]})
                    self.stats["enderecos"] += 1

            # BULK INSERT MÁGICO (O fim do gargalo de rede)
            print(f"📦 Arremessando Lote de {len(enderecos_batch)} Endereços de uma só vez...")
            query_add = text("INSERT INTO addresses (addressable_type, addressable_id, alias, zip, street, number, city, state, country, legacy_customer_id, reserved_customer_id, created_at, updated_at) VALUES (:type, :id, :alias, :zip, :street, :num, :city, :state, 'Brasil', :leg_id, :res_id, :now, :now)")
            conn.execute(query_add, [{"type": a["type"], "id": a["id"], "alias": a["alias"], "zip": a["zip"], "street": a["street"], "num": a["num"], "city": a["city"], "state": a["state"], "leg_id": a["leg_id"], "res_id": a["res_id"], "now": self.now} for a in enderecos_batch])

        print("\n" + "="*50)
        print("📊 RELATÓRIO FINAL DA ESTRUTURA HIERÁRQUICA")
        print("="*50)
        print(f"✅ Prefeituras (Customers): {self.stats['pais']}")
        print(f"✅ Secretarias (Customers): {self.stats['filhos']}")
        print(f"💥 Reservas Mescladas:      {self.stats['mesclados']}")
        print(f"📈 Total Em Addresses:      {len(enderecos_batch)}")
        print("="*50)

    # ==============================================================================
    # ORQUESTRAÇÃO
    # ==============================================================================
    def executar(self):
        print("\n🚀 Iniciando Migração de Clientes...")
        df_bruto = self._extrair()
        df_finais, df_limpo = self._transformar(df_bruto)
        
        executar_truncate_tabelas(self.engine_new, TABELAS_CLIENTES)
        self._carregar(df_finais, df_limpo)
        print("✅ Migração de Clientes Finalizada.")