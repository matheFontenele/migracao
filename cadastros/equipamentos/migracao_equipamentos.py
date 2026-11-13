import pandas as pd
import sqlalchemy as sa
import os
import time
import sys
import difflib
import re
from datetime import datetime
from tqdm import tqdm
from sqlalchemy import text

from config.config import MAPPING_ALUCOM, MAPPING_AS, MAPPING_IP, MAPPING_MOREIA, MAPPING_SC, LOCATION_ESTOQUE_EQUIPAMENTOS, CLIENTES_ESTOQUE
from utils.sanetizador import executar_truncate_tabelas

TODOS_ORGAOS_MAPEADOS = set().union(MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS, MAPPING_SC)


def _id_int(valor):
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _tombo_key_equip(valor):
    if pd.isna(valor):
        return None
    texto = str(valor).strip()
    if texto.endswith(".0"):
        texto = texto[:-2]
    return texto or None


def selecionar_descartes_cliente_estoque(df, clientes_estoque=None):
    """IDs de equipamentos de base que devem ser descartados quando o tombo
    possui ao menos uma copia fora da base/estoque.

    Se todas as copias do tombo forem de base, nada e descartado (evita
    eliminar o equipamento por completo).
    """
    clientes_estoque = CLIENTES_ESTOQUE if clientes_estoque is None else set(clientes_estoque)
    if df.empty or "cliente_ultimo_movimento" not in df.columns:
        return set()
    trabalho = df[["id_legado", "tombo_legado", "cliente_ultimo_movimento"]].copy()
    trabalho["_tombo_key"] = trabalho["tombo_legado"].apply(_tombo_key_equip)
    descartar = set()
    for _, grupo in trabalho.dropna(subset=["_tombo_key"]).groupby("_tombo_key"):
        if len(grupo) <= 1:
            continue
        na_base = grupo["cliente_ultimo_movimento"].apply(
            lambda valor: _id_int(valor) in clientes_estoque
        )
        if na_base.any() and (~na_base).any():
            descartar |= set(grupo.loc[na_base, "id_legado"].tolist())
    return descartar


def _product_item_combination_key(product_id, address_id, location_id, organization_id):
    """Return the columns that define a unique product-item stock combination."""
    return (product_id, 1, 1, address_id, location_id, organization_id)


TABELAS = [
    'equipments',
    'equipment_history',
    'suppliers', 
    'transaction_items', 
    'product_items',
    'products', 
    'types', 
    'brands', 
    'groups', 
    'transactions'
]

class MigracaoEquipamentos:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.ARQUIVO_IMPORTACAO = "docs/planilha_equipamentos.csv"
        self.ARQUIVO_TYPES = "docs/types.csv"
        self.ARQUIVO_SUPPLIERS = "docs/suppliers.csv"
        
        # Variáveis de Estado (Memória)
        self.mapa_enderecos = {}
        self.mapa_locations = {}
        self.id_fallback = 1
        self.id_generico = None
        self.lista_fornecedores_legado = []
        
        # Estatísticas
        self.stats = {
            "grupos": 0, "brands": 0, "types": 0, "products": 0,
            "suppliers": 0, "transactions": 0, "transaction_items": 0, "product_items_criados": 0, "equipments": 0, "history": 0, "tipos_inferidos": 0
        }

    # ==============================================================================
    # HELPERS
    # ==============================================================================
    def _nula(self, val):
        return None if pd.isna(val) else val

    def _carregar_enderecos_e_locations(self):
        """Carrega o address BASE de cada organização e garante a location padrão."""
        print("\n📍 Carregando addresses BASE e locations por organização...")

        with self.engine_new.begin() as conn:
            rows = conn.execute(text("""
                SELECT addressable_id AS org_id, MIN(id) AS address_id
                FROM addresses
                WHERE addressable_id IN (1115, 1311, 1122, 1378)
                  AND deleted_at IS NULL
                  AND UPPER(alias) LIKE '%BASE%'
                GROUP BY addressable_id
            """)).fetchall()
            mapa = {int(r[0]): int(r[1]) for r in rows}

            rows_all = conn.execute(text("""
                SELECT addressable_id AS org_id, MIN(id) AS address_id
                FROM addresses
                WHERE addressable_id IN (1115, 1311, 1122, 1378)
                  AND deleted_at IS NULL
                GROUP BY addressable_id
            """)).fetchall()
            for org_id, addr_id in rows_all:
                mapa.setdefault(int(org_id), int(addr_id))

            self.mapa_enderecos = mapa

            for org_id, addr_id in mapa.items():
                # Equipamentos ficam em location própria no endereço-base da organização
                spec = LOCATION_ESTOQUE_EQUIPAMENTOS
                area = str(spec["area"])
                section = str(spec.get("section") or "Unica")
                spot = str(spec.get("spot") or "Unico")
                details = str(spec.get("details") or "Migração")
                location_addr_id = int(spec.get("address_id") or addr_id)

                existente = conn.execute(text(
                    "SELECT id FROM locations WHERE address_id = :a "
                    "AND UPPER(area) = :area AND deleted_at IS NULL"
                ), {"a": location_addr_id, "area": area.upper()}).fetchone()

                if existente:
                    loc_id = existente[0]
                else:
                    res_loc = conn.execute(text("""
                        INSERT INTO locations (address_id, area, section, spot, details, created_at, updated_at)
                        VALUES (:addr, :area, :section, :spot, :details, :now, :now)
                    """), {
                        "addr": location_addr_id, "area": area, "section": section,
                        "spot": spot, "details": details, "now": self.now,
                    })
                    loc_id = res_loc.lastrowid

                self.mapa_locations[org_id] = loc_id

        print(f"   ✅ Mapa ORG→ADDRESS: {self.mapa_enderecos}")
        print(f"   ✅ Mapa ORG→LOCATION: {self.mapa_locations}")
    
    def _descobrir_id_organizacao_destino(self, id_legado):
        if pd.isna(id_legado): return 1115
        id_legado_int = int(id_legado)
        if id_legado_int in MAPPING_ALUCOM: return 1115
        if id_legado_int in MAPPING_IP: return 1311 
        if id_legado_int in MAPPING_MOREIA: return 1122
        if id_legado_int in MAPPING_AS: return 1378 
        if id_legado_int in MAPPING_SC: return 1115  
        return id_legado_int

    def _inferir_tipo_por_similaridade(self, nome_equipamento, tipos_validos):
            if pd.isna(nome_equipamento) or not str(nome_equipamento).strip():
                return None
            nome_str = str(nome_equipamento).strip().upper()
            for tipo in tipos_validos:
                if tipo in nome_str:
                    return tipo
            matches = difflib.get_close_matches(nome_str, tipos_validos, n=1, cutoff=0.6)
            if matches:
                return matches[0]
            return None

    def _tipo_grupo_legado_especial(self, tipo_legado):
        """Normalize selected legacy equipment categories and extract their spec group."""
        if pd.isna(tipo_legado):
            return None
        texto = str(tipo_legado).strip().upper()
        if re.search(r'\bESTABILIZADOR(?:ES)?\b', texto):
            spec = re.search(r'\b\d+(?:[.,]\d+)?\s*VA\b', texto)
            return 'ESTABILIZADOR', spec.group(0).replace(' ', '') if spec else None
        if re.search(r'\bMONITOR(?:ES)?\b', texto):
            spec = re.search(r'\b\d+(?:[.,]\d+)?\s*(?:"|”|\bPOL(?:EGADAS?)?\b)', texto)
            group = spec.group(0).strip() if spec else None
            if group:
                group = re.sub(r'\s+', '', group).replace('”', '"')
                if not group.endswith('"'):
                    group = re.sub(r'(?:POL(?:EGADAS?)?)$', '"', group)
            return 'MONITOR', group
        if re.search(r'\bTRANSFORMADOR(?:ES)?\b', texto):
            spec = re.search(r'\b\d+(?:[.,]\d+)?\s*VA\b', texto)
            return 'TRANSFORMADOR', spec.group(0).replace(' ', '') if spec else None
        return None

    def _tipo_grupo_nome(self, nome):
        """Infer special category/specification from an equipment name."""
        return self._tipo_grupo_legado_especial(nome)

    def _salvar_relatorio_tombos_repetidos(self, df_master):
        colunas = ['ID', 'TOMBO', 'EQUIPAMENTO', 'ORG_ID']
        caminho_ajustes = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'ajustes',
        )
        os.makedirs(caminho_ajustes, exist_ok=True)
        caminho_relatorio = os.path.join(caminho_ajustes, 'relatorio_duplicatas_tombos.csv')

        if df_master.empty or 'TOMBO' not in df_master.columns:
            duplicados = pd.DataFrame(columns=colunas)
        else:
            tombo_normalizado = df_master['TOMBO'].astype('string').str.strip()
            mask_duplicados = tombo_normalizado.notna() & tombo_normalizado.ne('') & tombo_normalizado.duplicated(keep=False)
            duplicados = df_master.loc[mask_duplicados, ['id_legado', 'TOMBO', 'NOME_AJUSTADO', 'org_destino']].copy()
            duplicados.columns = colunas
            duplicados = duplicados.sort_values(['TOMBO', 'ID'], kind='stable')

        duplicados.to_csv(caminho_relatorio, index=False, encoding='utf-8-sig')
        print(f"📄 Relatório de equipamentos com tombo repetido salvo em '{caminho_relatorio}' ({len(duplicados)} registros).")

    def _salvar_relatorio_descartes_base(self, registros):
        caminho_ajustes = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'ajustes',
        )
        os.makedirs(caminho_ajustes, exist_ok=True)
        caminho = os.path.join(caminho_ajustes, 'equipamentos_descartados_base.csv')
        colunas = ['id_legado', 'tombo', 'nome', 'cliente_ultimo_movimento']
        relatorio = pd.DataFrame(registros, columns=colunas)
        relatorio.to_csv(caminho, index=False, encoding='utf-8-sig')
        print(f"📄 Copias de base ignoradas salvas em '{caminho}' ({len(relatorio)} registros).")

    def _salvar_relatorio_fallbacks(self, registros):
        caminho_ajustes = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'ajustes')
        os.makedirs(caminho_ajustes, exist_ok=True)
        caminho_relatorio = os.path.join(caminho_ajustes, 'fallback_equipamentos.csv')
        colunas = ['id_legado', 'tombo', 'nome', 'tipo', 'org_destino', 'grupo', 'marca', 'grupo_fallback', 'marca_fallback', 'situacao_id_legado', 'fornecedor_fallback', 'tipo_legado', 'tombo_legado']
        relatorio = pd.DataFrame(registros, columns=colunas)
        if not relatorio.empty:
            relatorio = relatorio.sort_values(['tombo', 'id_legado'], kind='stable')
        relatorio.to_csv(caminho_relatorio, index=False, encoding='utf-8-sig')
        print(f"📄 Relatório de fallbacks salvo em '{caminho_relatorio}' ({len(relatorio)} registros).")

    def _salvar_relatorio_fornecedores_fallback(self, registros):
        caminho = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'ajustes')
        os.makedirs(caminho, exist_ok=True)
        pd.DataFrame(registros, columns=['id_legado', 'tombo', 'nome', 'tipo', 'org_destino', 'grupo', 'marca']).to_csv(
            os.path.join(caminho, 'fallback_fornecedor_alcom.csv'), index=False, encoding='utf-8-sig')
            
    # ==============================================================================
    # ETL: EXTRAÇÃO E TRANSFORMAÇÃO (PANDAS + NOVA CTE)
    # ==============================================================================
    def _extrair_e_transformar(self):
        print("\n📖 Carregando dados da planilha e do banco legado (via CTE Otimizada)...")
        df_planilha = pd.read_csv(self.ARQUIVO_IMPORTACAO, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)

        df_planilha['ID_LEGADO'] = pd.to_numeric(df_planilha['ID_LEGADO'], errors='coerce')
        mapa_planilha = {
            int(row['ID_LEGADO']): row 
            for _, row in df_planilha.dropna(subset=['ID_LEGADO']).iterrows()
        }

        query_legado = """
            SELECT 
                alq.id AS id_legado,
                alq.numero AS tombo_legado,
                alq.nome AS nome_legado,
                tipe.nome AS tipo_legado,
                og.ORG_ID AS orgao_original,
                org.ORG_ID AS orgao_atual,
                alq.situacao_id,
                alq.created_at,
                alq.updated_at,
                alq.deleted_at,
                alq.data_aquisicao,
                alq.data_garantia,
                ult_movi.cliente_id AS cliente_ultimo_movimento
            FROM aluguel_equipamentos alq
            LEFT JOIN aluguel_tipos tipe ON alq.tipo_id = tipe.id
            INNER JOIN ORGAOS og ON alq.orgao_id = og.ORG_ID
            LEFT JOIN (
                SELECT mi.equipamento_id, mi.novo_orgao_id, mov.cliente_id
                FROM aluguel_movimento_itens mi
                INNER JOIN (
                    SELECT equipamento_id, MAX(id) AS max_id
                    FROM aluguel_movimento_itens
                    WHERE deleted_at IS NULL
                    GROUP BY equipamento_id
                ) mx ON mx.max_id = mi.id
                LEFT JOIN aluguel_movimento mov ON mov.id = mi.movimento_id
                WHERE mi.deleted_at IS NULL
            ) ult_movi ON alq.id = ult_movi.equipamento_id
            LEFT JOIN ORGAOS org ON ult_movi.novo_orgao_id = org.ORG_ID
            WHERE alq.deleted_at IS NULL;
        """
        
        with self.engine_legado.connect() as conn:
            df_equipamentos_legado = pd.read_sql(text(query_legado), conn)

        print(f"\n🔍 Total retornado pelo SQL: {len(df_equipamentos_legado)}")

        # Tombos com mais de uma copia: descarta as copias ligadas a bases
        # (CLIENTES_ESTOQUE) quando existe ao menos uma copia fora da base.
        orgao_para_migrar = df_equipamentos_legado['orgao_atual'].fillna(
            df_equipamentos_legado['orgao_original']
        )
        ids_descartar_base = selecionar_descartes_cliente_estoque(
            df_equipamentos_legado[orgao_para_migrar.isin(TODOS_ORGAOS_MAPEADOS)],
            CLIENTES_ESTOQUE,
        )
        if ids_descartar_base:
            print(f"♻️  {len(ids_descartar_base)} equipamentos de base serão ignorados por tombo repetido.")

        self.lista_fornecedores_legado = df_planilha['MARCA_AJUSTADA'].dropna().unique()
        tipos_existentes = [str(t).strip().upper() for t in df_planilha['TIPO_AJUSTADO'].dropna().unique() if str(t).strip()]
        tipos_existentes.sort(key=len, reverse=True)

        lista_mestre = []
        registros_fallback = []

        # CONTADORES DE DESCARTE
        descartados_tombo_zero = 0
        descartados_org_nao_mapeada = 0
        descartados_tombo_base = 0
        registros_descartados_base = []
        orgaos_nao_mapeados_amostra = set()
        
        for index, dados_db in tqdm(df_equipamentos_legado.iterrows(), total=df_equipamentos_legado.shape[0], desc="Refatorando dados"):

            tombo_atual = dados_db['tombo_legado']
            
            # Pula tombos 0, 0.0 etc.
            if pd.notna(tombo_atual) and str(tombo_atual).strip() in ['0', '0.0', '0.00']:
                descartados_tombo_zero += 1
                continue

            id_equipamento_legado = dados_db['id_legado']

            if id_equipamento_legado in ids_descartar_base:
                descartados_tombo_base += 1
                registros_descartados_base.append({
                    'id_legado': id_equipamento_legado,
                    'tombo': tombo_atual,
                    'nome': dados_db['nome_legado'],
                    'cliente_ultimo_movimento': dados_db['cliente_ultimo_movimento'],
                })
                continue
            
            # Regra de Órgão: Usa o Órgão Atual. Se for Nulo, usa o Original.
            orgao_bruto = dados_db['orgao_atual'] if pd.notna(dados_db['orgao_atual']) else dados_db['orgao_original']
            
            # Se a máquina pertence a um órgão que não estamos migrando, pula
            if orgao_bruto not in TODOS_ORGAOS_MAPEADOS:
                descartados_org_nao_mapeada += 1
                if len(orgaos_nao_mapeados_amostra) < 20:
                    orgaos_nao_mapeados_amostra.add(orgao_bruto)
                continue
                
            org_destino = self._descobrir_id_organizacao_destino(orgao_bruto)
            id_situacao_legado = dados_db['situacao_id']

            created_at_destino = dados_db['created_at'] if pd.notna(dados_db['created_at']) else self.now
            updated_at_destino = dados_db['updated_at'] if pd.notna(dados_db['updated_at']) else self.now
            deleted_at_destino = dados_db['deleted_at'] if pd.notna(dados_db['deleted_at']) else (self.now if id_situacao_legado == 10 else None)
            
            status_id_destino = 9 if id_situacao_legado == 10 else 1 

            row_plan = mapa_planilha.get(id_equipamento_legado, {})
            
            # TRATAMENTO DO TIPO
            tipo_original = row_plan.get("TIPO_AJUSTADO")
            categoria_legado = self._tipo_grupo_legado_especial(dados_db.get('tipo_legado'))
            nome_categoria = self._tipo_grupo_nome(row_plan.get('NOME_AJUSTADO') if pd.notna(row_plan.get('NOME_AJUSTADO')) else dados_db.get('nome_legado'))
            if categoria_legado is None:
                categoria_legado = nome_categoria
            elif nome_categoria and categoria_legado[0] == nome_categoria[0] and not categoria_legado[1]:
                categoria_legado = nome_categoria
            nome_multi_grupo = None
            nome_aux = row_plan.get('NOME_AJUSTADO')
            marca_aux = row_plan.get('MARCA_AJUSTADA')
            if pd.notna(nome_aux) and pd.notna(marca_aux) and str(marca_aux).strip():
                nome_upper = str(nome_aux).strip().upper()
                marca_upper = str(marca_aux).strip().upper()
                multi_match = re.match(r'^MULTIFUNCIONAL\s+(.+)$', nome_upper)
                if multi_match:
                    resto = multi_match.group(1).strip()
                    if resto.endswith(marca_upper):
                        frase = resto[:-len(marca_upper)].strip()
                        frase = re.sub(r'\s+', ' ', frase)
                        if frase:
                            nome_multi_grupo = frase
            if nome_multi_grupo:
                tipo_final = 'MULTIFUNCIONAL'
            elif categoria_legado:
                tipo_final = categoria_legado[0]
            elif pd.isna(tipo_original) or str(tipo_original).strip() == "":
                tipo_bd = dados_db['tipo_legado']
                if pd.notna(tipo_bd) and str(tipo_bd).strip() != "":
                    tipo_final = str(tipo_bd).strip().upper()
                else:
                    nome_base = row_plan.get("NOME_AJUSTADO") if pd.notna(row_plan.get("NOME_AJUSTADO")) else dados_db['nome_legado']
                    tipo_final = self._inferir_tipo_por_similaridade(nome_base, tipos_existentes)
                    self.stats["tipos_inferidos"] += 1
            else:
                tipo_final = str(tipo_original).strip().upper() 

            # TRATAMENTO DOS DEMAIS DADOS
            nome_final = row_plan.get("NOME_AJUSTADO")
            if pd.isna(nome_final): nome_final = dados_db['nome_legado']
            if pd.isna(nome_final): nome_final = "SEM NOME REGISTRADO"
            
            marca_final = row_plan.get("MARCA_AJUSTADA")
            marca_fallback = pd.isna(marca_final) or not str(marca_final).strip()
            if marca_fallback and categoria_legado and categoria_legado[0] == 'TRANSFORMADOR':
                nome_marca = str(nome_final).upper()
                nome_marca = re.sub(r'\bTRANSFORMADOR(?:ES)?\b', '', nome_marca)
                nome_marca = re.sub(r'\b\d+(?:[.,]\d+)?\s*VA\b', '', nome_marca)
                inferida = re.sub(r'[^A-Z0-9 ]', ' ', nome_marca).strip()
                if inferida:
                    marca_final = inferida
                    marca_fallback = False
            if marca_fallback: marca_final = "ALUCOM"
            
            grupo_final = row_plan.get("GRUPOS")
            grupo_fallback = pd.isna(grupo_final) or not str(grupo_final).strip()
            if nome_multi_grupo:
                grupo_final = nome_multi_grupo
                grupo_fallback = False
            elif categoria_legado and categoria_legado[1]:
                grupo_final = categoria_legado[1]
                grupo_fallback = False
            elif grupo_fallback:
                grupo_final = "GERAL"

            if marca_fallback or grupo_fallback:
                registros_fallback.append({'id_legado': id_equipamento_legado, 'tombo': tombo_atual,
                    'nome': nome_final, 'tipo': tipo_final, 'org_destino': org_destino,
                    'grupo': grupo_final, 'marca': marca_final, 'grupo_fallback': int(grupo_fallback),
                    'marca_fallback': int(marca_fallback), 'situacao_id_legado': id_situacao_legado,
                    'fornecedor_fallback': '',
                    'tipo_legado': dados_db.get('tipo_legado'), 'tombo_legado': tombo_atual})

            lista_mestre.append({
                "id_legado":     id_equipamento_legado,
                "TOMBO":         tombo_atual,
                "NOME_AJUSTADO": nome_final,
                "NUMERO_SERIE":  row_plan.get("NUMERO_SERIE") if pd.notna(row_plan.get("NUMERO_SERIE")) else None,
                "codigo_item":   row_plan.get("codigo_item") if pd.notna(row_plan.get("codigo_item")) else None,
                "valor":         float(row_plan.get("valor")) if pd.notna(row_plan.get("valor")) else 0.0,
                "grupo":         grupo_final,
                "group_id":      None,
                "marca":         marca_final,
                "brand_id":      None,
                "tipo":          tipo_final if tipo_final else "OUTROS",
                "type_id":       None,
                "status_id":     status_id_destino,
                "org_destino":   org_destino,
                "created_at":    created_at_destino,
                "updated_at":    updated_at_destino,
                "deleted_at":    deleted_at_destino,
                "data_aquisicao": self._nula(dados_db['data_aquisicao']),
                "data_garantia":  self._nula(dados_db['data_garantia']),
                "transaction_id": None
            })

        print(f"\n📊 RESUMO DE DESCARTES NO LOOP:")
        print(f"   Total original (SQL):              {len(df_equipamentos_legado)}")
        print(f"   ❌ Descartados (tombo zero):  {descartados_tombo_zero}")
        print(f"   ❌ Descartados (órgão não mapeado): {descartados_org_nao_mapeada}")
        print(f"   ❌ Descartados (base com tombo repetido): {descartados_tombo_base}")
        print(f"   ✅ Dados limpos: {len(lista_mestre)}")
        print(f"   🔍 Amostra de órgãos não mapeados: {orgaos_nao_mapeados_amostra}")
        self._salvar_relatorio_fallbacks(registros_fallback)
        self._salvar_relatorio_descartes_base(registros_descartados_base)

        return pd.DataFrame(lista_mestre).reset_index(drop=True)

    # ==============================================================================
    # ETL: CARGA DE CADASTROS BÁSICOS E PRODUTOS
    # ==============================================================================
    def _carregar_tabelas_dimensionais(self, df_master):
        print("\n🚀 Iniciando a persistência de tabelas dimensionais...")
        
        # 1. GROUPS
        print("💾 Inserindo groups...")
        grupos_unicos = df_master['grupo'].dropna().unique()
        if len(grupos_unicos) > 0:
            with self.engine_new.begin() as conn:
                df_groups = pd.DataFrame({"name": grupos_unicos, "created_at": self.now, "updated_at": self.now})
                df_groups.to_sql('groups', con=conn, if_exists='append', index=False)
        db_groups = pd.read_sql("SELECT id as group_id, name FROM `groups`", self.engine_new)
        df_master['group_id'] = df_master['grupo'].map(dict(zip(db_groups['name'], db_groups['group_id']))).astype("Int64")
        self.stats["grupos"] = len(grupos_unicos)

        # 2. BRANDS
        print("💾 Inserindo brands...")
        marcas_unicas = df_master['marca'].dropna().unique()
        if len(marcas_unicas) > 0:
            with self.engine_new.begin() as conn:
                df_brands = pd.DataFrame({"name": marcas_unicas, "created_at": self.now, "updated_at": self.now})
                df_brands.to_sql('brands', con=conn, if_exists='append', index=False)
        db_brands = pd.read_sql("SELECT id as brand_id, name FROM `brands`", self.engine_new)
        df_master['brand_id'] = df_master['marca'].map(dict(zip(db_brands['name'], db_brands['brand_id']))).astype("Int64")
        self.stats["brands"] = len(marcas_unicas)

        # 3. TYPES COM VERIFICAÇÃO DE IS_KIT
        print("💾 Inserindo types e mapeando is_kit...")
        tipos_unicos = df_master['tipo'].dropna().unique()
        if len(tipos_unicos) > 0:
            mapa_is_kit = {}
            if os.path.exists(self.ARQUIVO_TYPES):
                df_csv_types = pd.read_csv(self.ARQUIVO_TYPES)
                for _, row_type in df_csv_types.iterrows():
                    if pd.notna(row_type.get('name')):
                        nome_tipo = str(row_type['name']).strip().upper()
                        mapa_is_kit[nome_tipo] = int(row_type.get('is_kit', 0))
            else:
                print(f"   ⚠️ Arquivo {self.ARQUIVO_TYPES} não encontrado. Todos os tipos assumirão is_kit = 0.")

            valores_is_kit = [mapa_is_kit.get(str(t).upper(), 0) for t in tipos_unicos]

            with self.engine_new.begin() as conn:
                df_types = pd.DataFrame({
                    "name": tipos_unicos,
                    "is_kit": valores_is_kit,
                    "created_at": self.now, 
                    "updated_at": self.now
                })
                df_types.to_sql('types', con=conn, if_exists='append', index=False)
                
        db_types = pd.read_sql("SELECT id as type_id, name FROM `types`", self.engine_new)
        df_master['type_id'] = df_master['tipo'].map(dict(zip(db_types['name'], db_types['type_id']))).astype("Int64")
        self.stats["types"] = len(tipos_unicos)

        # 4. PRODUCTS (Agrupamento único)
        print("💾 Inserindo products...")
        df_produtos_unicos = df_master[['marca', 'tipo', 'grupo', 'brand_id', 'type_id', 'group_id']].drop_duplicates()

        def gerar_nome_produto(row):
            partes = [str(row['tipo']) if pd.notna(row['tipo']) else "",
                      str(row['grupo']) if pd.notna(row['grupo']) else "",
                      str(row['marca']) if pd.notna(row['marca']) else ""]
            nome = " ".join([p for p in partes if p]).strip()
            return nome if nome else "PRODUTO SEM ESPECIFICAÇÃO"

        df_produtos_unicos['name'] = df_produtos_unicos.apply(gerar_nome_produto, axis=1)

        df_products_inserir = pd.DataFrame({
            "name": df_produtos_unicos['name'], "brand_id": df_produtos_unicos['brand_id'],
            "type_id": df_produtos_unicos['type_id'], "group_id": df_produtos_unicos['group_id'],
            "is_asset": 1, "length": 0, "width": 0, "height": 0, "weight": 0,
            "min_quantity": 1, 
            "max_quantity": 2,
            "created_at": self.now, "updated_at": self.now
        })

        if not df_products_inserir.empty:
            with self.engine_new.begin() as conn:
                df_products_inserir.to_sql('products', con=conn, if_exists='append', index=False)
        self.stats["products"] = len(df_products_inserir)

        # Merge de IDs de volta no DF
        db_products = pd.read_sql("SELECT id as product_id, name as product_name, brand_id, type_id, group_id FROM products", self.engine_new)
        db_products['brand_id'] = db_products['brand_id'].astype("Int64")
        db_products['type_id']  = db_products['type_id'].astype("Int64")
        db_products['group_id'] = db_products['group_id'].astype("Int64")

        df_master = df_master.merge(
            db_products[['product_id', 'product_name', 'brand_id', 'type_id', 'group_id']],
            on=['brand_id', 'type_id', 'group_id'], how='left'
        )
        df_master['product_id'] = df_master['product_id'].astype("Int64")
        df_master['product_name'] = df_master['product_name'].fillna("PRODUTO SEM ESPECIFICAÇÃO")

        return df_master

    # ==============================================================================
    # ETL: FORNECEDORES (SUPPLIERS VIA CSV)
    # ==============================================================================
    def _tratar_fornecedores(self, df_master):
        print("\n💾 Processando Fornecedores (Suppliers)...")

        # 1. Carregar CSV e Inserir no Banco
        if os.path.exists(self.ARQUIVO_SUPPLIERS):
            df_csv_suppliers = pd.read_csv(self.ARQUIVO_SUPPLIERS, sep=",", encoding="utf-8", on_bad_lines="skip")
            df_csv_suppliers.columns = df_csv_suppliers.columns.str.lower()
            
            # Detecta qual é a coluna do nome
            col_name = 'name' if 'name' in df_csv_suppliers.columns else ('nome' if 'nome' in df_csv_suppliers.columns else None)
            
            if col_name:
                df_suppliers_inserir = pd.DataFrame()
                df_suppliers_inserir['name'] = df_csv_suppliers[col_name].dropna().astype(str).str.strip()
                df_suppliers_inserir['alias'] = df_csv_suppliers.get('alias', df_suppliers_inserir['name'])
                cnpj_raw = df_csv_suppliers.get('cpf_cnpj', df_csv_suppliers.get('cnpj', '00000000000000'))
                df_suppliers_inserir['cpf_cnpj'] = (
                    cnpj_raw.astype(str)
                    .str.replace(r'\D', '', regex=True)
                    .str.slice(0, 14)
                    .replace('', '00000000000000')
                )
                df_suppliers_inserir['phone'] = df_csv_suppliers.get('phone', df_csv_suppliers.get('telefone', '00000000000')).fillna('00000000000')
                df_suppliers_inserir['email'] = df_csv_suppliers.get('email', 'nao@informado.com').fillna('nao@informado.com')
                df_suppliers_inserir['created_at'] = self.now
                df_suppliers_inserir['updated_at'] = self.now
                
                with self.engine_new.begin() as conn:
                    df_suppliers_inserir.to_sql('suppliers', con=conn, if_exists='append', index=False)
                self.stats["suppliers"] = len(df_suppliers_inserir)
                print(f"   ✅ {len(df_suppliers_inserir)} fornecedores importados do CSV.")
            else:
                print(f"   ⚠️ Coluna de nome (name/nome) não encontrada em {self.ARQUIVO_SUPPLIERS}.")
        else:
            print(f"   ⚠️ Arquivo {self.ARQUIVO_SUPPLIERS} não encontrado no projeto.")

        # 3. Mapear os Fornecedores com Base no Nome
        with self.engine_new.begin() as conn:
            df_todos_fornecedores = pd.read_sql("SELECT id, name FROM suppliers", conn)
            
        mapa_nome_id = {str(row['name']).strip().lower(): row['id'] for _, row in df_todos_fornecedores.iterrows()}
        mapa_alias_id = {}
        if os.path.exists(self.ARQUIVO_SUPPLIERS):
            aliases = pd.read_csv(self.ARQUIVO_SUPPLIERS, sep=",", encoding="utf-8", on_bad_lines="skip")
            aliases.columns = aliases.columns.str.lower()
            nome_col = 'name' if 'name' in aliases.columns else ('nome' if 'nome' in aliases.columns else None)
            if nome_col and 'alias' in aliases.columns:
                for _, supplier in aliases.iterrows():
                    if pd.notna(supplier.get('alias')) and pd.notna(supplier.get(nome_col)):
                        supplier_id = mapa_nome_id.get(str(supplier[nome_col]).strip().lower())
                        if supplier_id is not None:
                            mapa_alias_id[str(supplier['alias']).strip().lower()] = supplier_id
        alucom_id = mapa_nome_id.get('alucom') or mapa_nome_id.get('alucom ltda')
        if alucom_id is None:
            alucom_id = next((sid for alias, sid in mapa_alias_id.items() if alias in ('alucom', 'alucom ltda')), None)
        if alucom_id is None:
            raise ValueError("Fornecedor ALUCOM não encontrado em suppliers ou docs/suppliers.csv")

        falhas_fornecedor = {}
        def buscar_fornecedor(marca):
            if pd.isna(marca) or not str(marca).strip():
                falhas_fornecedor[str(marca)] = True
                return alucom_id
                
            marca_str = str(marca).strip().lower()
            
            # Match exato
            if marca_str in mapa_nome_id:
                falhas_fornecedor[marca_str] = False
                return mapa_nome_id[marca_str]
            if marca_str in mapa_alias_id:
                falhas_fornecedor[marca_str] = False
                return mapa_alias_id[marca_str]
                
            # Match parcial (procura substring no banco ou vice-versa)
            for db_name, db_id in mapa_nome_id.items():
                if marca_str in db_name or db_name in marca_str:
                    falhas_fornecedor[marca_str] = False
                    return db_id
            falhas_fornecedor[marca_str] = True
            return alucom_id

        df_master['supplier_id'] = df_master['marca'].apply(buscar_fornecedor).astype("Int64")
        df_master['supplier_fallback'] = df_master['marca'].apply(
            lambda marca: falhas_fornecedor.get(str(marca).strip().lower(), False)
            if pd.notna(marca) else falhas_fornecedor.get(str(marca), False)
        )
        registros = []
        for _, row in df_master[df_master['supplier_fallback']].iterrows():
                registros.append({'id_legado': row.get('id_legado'), 'tombo': row.get('TOMBO'),
                    'nome': row.get('NOME_AJUSTADO'), 'tipo': row.get('tipo'), 'org_destino': row.get('org_destino'),
                    'grupo': row.get('grupo'), 'marca': row.get('marca')})
        self._salvar_relatorio_fornecedores_fallback(registros)
        
        # Estatística rápida de quantos foram para o fallback
        qtd_fallback = int(df_master['supplier_fallback'].sum())
        print(f"   ✅ Relacionamento concluído. {qtd_fallback} equipamentos usarão o fornecedor ALUCOM.")

        return df_master

    # ==============================================================================
    # ETL: INVENTÁRIO (AGRUPADO POR MATCH PARA PRODUCT_ITEMS)
    # ==============================================================================
    def _gerar_inventario(self, df_master):
        print("📊 Gerando agrupamento de Inventário (Fornecedor + Órgão + Produto)...")
        
        df_validos = df_master[df_master['org_destino'].notna()]
        grupos_transacao = df_validos.groupby(['supplier_id', 'org_destino', 'created_at'], dropna=False)
        total_grupos = grupos_transacao.ngroups

        lista_equipamentos_global = []
        lista_historico_global = []
        contador_codigo_unico = 1000000
        product_items_existentes = {}

        with self.engine_new.begin() as conn:
            for (s_id, org_id, created_at_chave), df_transacao in tqdm(
                grupos_transacao,
                total=total_grupos,
                desc="Gerando inventário",
                unit="lote",
                dynamic_ncols=True,
            ):
                supplier_id_int = int(s_id) if pd.notna(s_id) else None
                buyer_id_int = int(org_id)
                addr_id = self.mapa_enderecos.get(buyer_id_int, self.id_fallback)
                loc_id = self.mapa_locations.get(buyer_id_int)

                # DATA DE COMPRA: data_aquisicao do legado; fallback = menor created_at do lote
                data_compra_dt = pd.to_datetime(created_at_chave, errors='coerce')
                if pd.isna(data_compra_dt):
                    datas_lote = pd.to_datetime(df_transacao['created_at'], errors='coerce').dropna()
                    data_compra_dt = datas_lote.min() if not datas_lote.empty else pd.to_datetime(self.now)
                data_transacao = data_compra_dt.strftime('%Y-%m-%d %H:%M:%S')

                # A. Transação Mãe (purchase_date = data de compra real)
                result_tx = conn.execute(text("""
                    INSERT INTO transactions (transaction_date, transaction_type_id, supplier_id, buyer_id, 
                    doc_type_id, doc_date, purchase_date, created_by, details, amount_total, amount_discount, created_at, updated_at) 
                    VALUES (:data_tx, 1, :sid, :bid, 3, :data_tx, :data_tx, 1, 'Migração Inicial (Lote)', 1, 0, :now, :now)
                """), {
                    "data_tx": data_transacao, 
                    "now": self.now, 
                    "sid": supplier_id_int, 
                    "bid": buyer_id_int
                })
                tx_id_gerado = result_tx.lastrowid
                self.stats["transactions"] += 1

                grupos_produtos = df_transacao.groupby('product_id', dropna=False)

                # UM ÚNICO loop por produto (SEM duplicação)
                for p_id, df_equipamentos_identicos in grupos_produtos:
                    p_id_val = int(p_id) if pd.notna(p_id) else None
                    qtd_repeticoes = len(df_equipamentos_identicos)

                    # GARANTIA POR PRODUTO: data_garantia REAL do legado; fallback = compra + 1 ANO
                    garantias_lote = pd.to_datetime(df_equipamentos_identicos['data_garantia'], errors='coerce').dropna()
                    if not garantias_lote.empty:
                        data_garantia_dt = garantias_lote.min()
                    else:
                        data_garantia_dt = data_compra_dt + pd.DateOffset(years=1)
                    data_garantia = data_garantia_dt.strftime('%Y-%m-%d %H:%M:%S')

                    # B. Bloco de Estoque (product_items)
                    pi_key = _product_item_combination_key(p_id_val, addr_id, loc_id, buyer_id_int)
                    pi_id_gerado = product_items_existentes.get(pi_key)
                    if pi_id_gerado is None:
                        existing_pi = conn.execute(text("""
                            SELECT id, quantity FROM product_items
                            WHERE product_id = :pid AND category_id = 1 AND condition_id = 1
                              AND address_id = :addr AND location_id = :loc AND organization_id = :org
                            LIMIT 1
                        """), {"pid": p_id_val, "addr": addr_id, "loc": loc_id, "org": buyer_id_int}).fetchone()
                        if existing_pi:
                            pi_id_gerado = existing_pi[0]
                            conn.execute(text("UPDATE product_items SET quantity = quantity + :qty, updated_at = :now WHERE id = :id"),
                                         {"qty": qtd_repeticoes, "now": self.now, "id": pi_id_gerado})
                        else:
                            result_pi = conn.execute(text("""
                                INSERT INTO product_items (product_id, code, category_id, condition_id, address_id, location_id, organization_id, average_cost, quantity, created_at, updated_at)
                                VALUES (:pid, :code, 1, 1, :addr, :loc, :org, 1, :qty, :now, :now)
                            """), {
                                "pid": p_id_val, "code": contador_codigo_unico, "addr": addr_id, "loc": loc_id,
                                "org": buyer_id_int, "qty": qtd_repeticoes, "now": self.now
                            })
                            pi_id_gerado = result_pi.lastrowid
                            contador_codigo_unico += 1
                            self.stats["product_items_criados"] += 1
                        product_items_existentes[pi_key] = pi_id_gerado
                    else:
                        conn.execute(text("UPDATE product_items SET quantity = quantity + :qty, updated_at = :now WHERE id = :id"),
                                     {"qty": qtd_repeticoes, "now": self.now, "id": pi_id_gerado})

                    # C. Item de Transação (transaction_items com garantia real)
                    result_item = conn.execute(text("""
                        INSERT INTO transaction_items (transaction_id, product_id, category_id, condition_id, warranty_date, address_id, location_id, unit_cost, quantity, created_at, updated_at, deleted_at)
                        VALUES (:tid, :pid, 1, 1, :warranty_date, :addr, :loc, 0, :qty, :now, :now, NULL)
                    """), {
                        "tid": tx_id_gerado, 
                        "pid": p_id_val, 
                        "warranty_date": data_garantia,
                        "now": self.now, 
                        "addr": addr_id, 
                        "loc": loc_id,
                        "qty": qtd_repeticoes
                    })
                    ti_id_gerado = result_item.lastrowid
                    self.stats["transaction_items"] += 1

                    # D. Equipamentos físicos + histórico
                    for _, linha_equip in df_equipamentos_identicos.iterrows():
                        eq_id = int(linha_equip['id_legado'])
                        eq_created_at = linha_equip['created_at']

                        lista_equipamentos_global.append({
                            "id": eq_id,
                            "product_item_id": pi_id_gerado,
                            "transaction_item_id": ti_id_gerado,
                            "number": linha_equip['TOMBO'],
                            "name": linha_equip['NOME_AJUSTADO'],
                            "serial_number": self._nula(linha_equip['NUMERO_SERIE']),
                            "serial_required": 0,
                            "current_organization_id": buyer_id_int,
                            "status_id": linha_equip['status_id'],
                            "address_id": addr_id,
                            "location_id": loc_id,
                            "is_completed": 1,
                            "created_at": linha_equip['created_at'],
                            "updated_at": linha_equip['updated_at'],
                            "deleted_at": self._nula(linha_equip['deleted_at'])
                        })

                        lista_historico_global.append({
                            "equipment_id": eq_id,
                            "status_id": 1,
                            "occurred_at": eq_created_at,
                            "movement_item_id": None,
                            "service_order_item_id": None,
                            "contract_item_id": None,
                            "shipment_item_id": None,
                            "is_conversion": 0,
                            "reason": "TRANSACTION_ENTRANCE_EQUIPMENT",
                            "user_id": 1 
                        })

            # Inserção em lote (DENTRO do with, FORA dos loops - uma vez só)
            if lista_equipamentos_global:
                print(f"💾 Inserindo {len(lista_equipamentos_global)} máquinas físicas no MySQL...")
                conn.execute(text("""
                    INSERT INTO equipments (id, product_item_id, transaction_item_id, number, name, serial_number, serial_required, current_organization_id, status_id, address_id, location_id, is_completed, created_at, updated_at, deleted_at) 
                    VALUES (:id, :product_item_id, :transaction_item_id, :number, :name, :serial_number, :serial_required, :current_organization_id, :status_id, :address_id, :location_id, :is_completed, :created_at, :updated_at, :deleted_at)
                """), lista_equipamentos_global)
                self.stats["equipments"] += len(lista_equipamentos_global)

            if lista_historico_global:
                print(f"💾 Inserindo {len(lista_historico_global)} logs de histórico no MySQL...")
                conn.execute(text("""
                    INSERT INTO equipment_history (
                        equipment_id, status_id, occurred_at, movement_item_id, 
                        service_order_item_id, contract_item_id, shipment_item_id, 
                        is_conversion, reason, user_id
                    ) VALUES (
                        :equipment_id, :status_id, :occurred_at, :movement_item_id, 
                        :service_order_item_id, :contract_item_id, :shipment_item_id, 
                        :is_conversion, :reason, :user_id
                    )
                """), lista_historico_global)
                self.stats["history"] += len(lista_historico_global)

    # ==============================================================================
    # ORQUESTRADOR PRINCIPAL DA CLASSE
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 INICIANDO MIGRAÇÃO: EQUIPAMENTOS E INVENTÁRIO (COM AGRUPAMENTO)")
        print("=" * 80)
        
        try:
            executar_truncate_tabelas(self.engine_new, TABELAS)

            df_master = self._extrair_e_transformar()
            self._salvar_relatorio_tombos_repetidos(df_master)
            self._carregar_enderecos_e_locations()
            df_master = self._carregar_tabelas_dimensionais(df_master)
            df_master = self._tratar_fornecedores(df_master)
            self._gerar_inventario(df_master)

            print("\n" + "=" * 50)
            print("📊 RELATÓRIO FINAL DE EQUIPAMENTOS")
            print("=" * 50)
            print(f"📦 Grupos criados:         {self.stats['grupos']}")
            print(f"🏷️  Marcas criadas:         {self.stats['brands']}")
            print(f"📁 Tipos criados:          {self.stats['types']}")
            print(f"🤖 Tipos Inferidos:        {self.stats['tipos_inferidos']}")
            print(f"🛒 Produtos cadastrados:   {self.stats['products']}")
            print(f"🏭 Fornecedores (CSV):     {self.stats['suppliers']}")
            print("-" * 50)
            print(f"📑 Transações-Mãe:         {self.stats['transactions']}")
            print(f"📦 Blocos de Estoque (PI): {self.stats['product_items_criados']}")
            print(f"🧩 Itens de Transação:     {self.stats['transaction_items']}")
            print(f"💻 Equipamentos salvos:    {self.stats['equipments']}")
            print(f"📜 Logs de Entrada (TX):   {self.stats['history']}")
            print("=" * 50)

        except Exception as e:
            print(f"\n❌ ERRO CRÍTICO NO PIPELINE DE EQUIPAMENTOS: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

# ==============================================================================
# WRAPPER (Ponte para o main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    migrador = MigracaoEquipamentos(eng_novo, eng_legado)
    migrador.executar()
