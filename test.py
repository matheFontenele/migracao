import os
import glob
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from config.config import ENDERECOS_BASES
from utils.sanetizador import normalizar_para_match
from movimentos.migracao_movimentos import BaseMigracaoMovimento, descobrir_id_organizacao_destino, limpar_codigo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_PARQUETS = "./docs/parquets"
ARQUIVO_TYPES = "./docs/types.csv"

class MigracaoSubstituicao(BaseMigracaoMovimento):

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=900000):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente=False)
        self.tipos_por_nome = []
        
        with self.engine_new.connect() as conn:
            org_ids = [str(org['id']) for org in ENDERECOS_BASES]
            org_ids_sql = "(" + ", ".join(org_ids) + ")"
            query_enderecos = f"SELECT addressable_id, MIN(id) as address_id FROM addresses WHERE addressable_type = 'organization' AND addressable_id IN {org_ids_sql} GROUP BY addressable_id"
            result_ends = conn.execute(text(query_enderecos)).fetchall()
            self.dict_enderecos_base_org = {row.addressable_id: row.address_id for row in result_ends}

    def _carregar_tipos_por_nome(self):
        if not os.path.exists(ARQUIVO_TYPES):
            raise FileNotFoundError(f"Arquivo obrigatório não encontrado: {ARQUIVO_TYPES}")

        df_types = pd.read_csv(ARQUIVO_TYPES, sep=",", encoding="utf-8")
        colunas_obrigatorias = {"id", "name", "is_kit"}
        colunas_ausentes = colunas_obrigatorias - set(df_types.columns)
        if colunas_ausentes:
            raise ValueError(
                f"Colunas ausentes em types.csv: {sorted(colunas_ausentes)}"
            )

        tipos = []
        for _, row in df_types.iterrows():
            if pd.isna(row["name"]):
                continue
            nome_normalizado = normalizar_para_match(row["name"])
            if not nome_normalizado or pd.isna(row["id"]) or pd.isna(row["is_kit"]):
                continue
            tipos.append({
                "id": int(row["id"]),
                "name": str(row["name"]).strip(),
                "nome_normalizado": nome_normalizado,
                "is_kit": int(row["is_kit"]),
            })

        self.tipos_por_nome = sorted(
            tipos,
            key=lambda tipo: len(tipo["nome_normalizado"]),
            reverse=True,
        )

    def _resolver_tipo_por_nome(self, nome_equipamento):
        if nome_equipamento is None or pd.isna(nome_equipamento):
            return None
        nome_normalizado = normalizar_para_match(nome_equipamento)
        if not nome_normalizado:
            return None

        # Primeiro tenta o nome completo ou o prefixo mais específico.
        for tipo in self.tipos_por_nome:
            nome_tipo = tipo["nome_normalizado"]
            if nome_normalizado == nome_tipo or nome_normalizado.startswith(f"{nome_tipo} "):
                return tipo

        # Alguns nomes trazem um prefixo adicional, como "IMPRESSORA ...".
        for tipo in self.tipos_por_nome:
            if tipo["nome_normalizado"] in nome_normalizado:
                return tipo

        # Aceita abreviação somente quando os melhores candidatos concordam no is_kit.
        tokens_equipamento = nome_normalizado.split()
        candidatos = []
        maior_prefixo = 0
        for tipo in self.tipos_por_nome:
            tokens_tipo = tipo["nome_normalizado"].split()
            tamanho_prefixo = 0
            for token_equipamento, token_tipo in zip(tokens_equipamento, tokens_tipo):
                if token_equipamento != token_tipo:
                    break
                tamanho_prefixo += 1

            if tamanho_prefixo < 2:
                continue
            if tamanho_prefixo > maior_prefixo:
                maior_prefixo = tamanho_prefixo
                candidatos = [tipo]
            elif tamanho_prefixo == maior_prefixo:
                candidatos.append(tipo)

        if candidatos and len({tipo["is_kit"] for tipo in candidatos}) == 1:
            return candidatos[0]
        return None

    def _carregar_itens_parquet(self):
        arquivos_parquet = sorted(glob.glob(os.path.join(PASTA_PARQUETS, "*.parquet")))
        if not arquivos_parquet:
            raise FileNotFoundError(
                f"Nenhum arquivo .parquet encontrado em {PASTA_PARQUETS}"
            )

        dataframes = [pd.read_parquet(arquivo) for arquivo in arquivos_parquet]
        df_itens = pd.concat(dataframes, ignore_index=True).drop_duplicates().copy()
        
        # Padroniza as colunas para UPPERCASE
        df_itens.columns = df_itens.columns.str.upper()

        colunas_obrigatorias = {
            "TOMBO", "CLIENTE_ID", "CONTRACT_ID",
            "EQUIPAMENTO_NOME",
        }
        colunas_ausentes = colunas_obrigatorias - set(df_itens.columns)
        if colunas_ausentes:
            raise ValueError(
                f"Colunas ausentes nos parquets: {sorted(colunas_ausentes)}"
            )

        df_itens["TOMBO"] = pd.to_numeric(df_itens["TOMBO"], errors="coerce")
        df_itens = df_itens.dropna(subset=["TOMBO"])
        df_itens["TOMBO"] = df_itens["TOMBO"].astype(int).astype(str)
        
        df_itens["COMPLETUDE_CONTRATO"] = df_itens[
            ["CONTRACT_ID"]
        ].notna().sum(axis=1)
        
        return df_itens

    def _buscar_ultimo_movimento_por_tombo(self, tombos_parquet):
        """Busca o último movimento de cada equipamento no legado"""
        if not tombos_parquet:
            return pd.DataFrame()

        tombos_sql = "(" + ", ".join(map(str, sorted(set(tombos_parquet)))) + ")"
        
        query = f"""
            SELECT
                alq.id AS EQUIPAMENTO_LEGADO_ID,
                alq.numero AS TOMBO,
                alq.situacao_id AS SITUACAO_ID,
                alqs.nome AS SITUACAO_NOME,
                ami.id AS ULTIMO_MOV_ITEM_ID,
                alm.id AS MOV_NOVO_ID,
                alm.tipo_id AS TIPO_ID,
                alm.cliente_id AS CLIENTE_ID,
                alm.data AS DATA_MOVIMENTO,
                alm.updated_at AS UPDATED_AT_MOVIMENTO
            FROM aluguel_equipamentos alq
            INNER JOIN aluguel_situacao alqs ON alq.situacao_id = alqs.id
            INNER JOIN (
                SELECT equipamento_id, MAX(id) AS ultimo_mov_item_id
                FROM aluguel_movimento_itens
                GROUP BY equipamento_id
            ) ult_mov ON alq.id = ult_mov.equipamento_id
            INNER JOIN aluguel_movimento_itens ami ON ult_mov.ultimo_mov_item_id = ami.id
            INNER JOIN aluguel_movimento alm ON ami.movimento_id = alm.id
            WHERE alq.numero IN {tombos_sql}
        """
        
        print(f"   🔍 Buscando último movimento de {len(tombos_parquet)} equipamentos no legado...")
        with self.engine_legado.connect() as conn:
            df_resultado = pd.read_sql(text(query), conn)
        
        print(f"   📊 Encontrados {len(df_resultado)} equipamentos com movimentos")
        return df_resultado

    def _extrair_dados_substituicao(self, df_movimentos_subst):
        """Extrai os detalhes completos das substituições identificadas"""
        if df_movimentos_subst.empty:
            return pd.DataFrame()

        print("   📖 Extraindo detalhes das substituições...")
        
        mov_ids = df_movimentos_subst['MOV_NOVO_ID'].tolist()
        lista_sql = "(" + ", ".join(map(str, mov_ids)) + ")"
        
        # Query mais simples: pega os movimentos de substituição e seus itens
        query_subst = f"""
            SELECT 
                mov_novo.id as MOV_NOVO_ID,
                mov_novo.cliente_id as CLIENTE_ID,
                COALESCE(mov_novo.updated_at, mov_novo.data) AS DATA_SUBST,
                COALESCE(NULLIF(mov_novo.usuario_id, 0), 1) AS USR_SUBST,
                mov_novo.deleted_at AS DEL_SUBST,
                
                ami_novo.id as ITEM_NOVO_ID,
                eq_novo.id as EQ_NOVO_ID,
                eq_novo.numero as TOMBO_NOVO,
                eq_novo.nome as NOME_NOVO
            FROM aluguel_movimento mov_novo
            INNER JOIN aluguel_movimento_itens ami_novo ON ami_novo.movimento_id = mov_novo.id
            INNER JOIN aluguel_equipamentos eq_novo ON eq_novo.id = ami_novo.equipamento_id
            WHERE mov_novo.id IN {lista_sql}
              AND mov_novo.tipo_id = 5
        """
        
        with self.engine_legado.connect() as conn:
            df_subst = pd.read_sql(text(query_subst), conn)
        
        if df_subst.empty:
            print("   ⚠️ Nenhum movimento de substituição encontrado")
            return df_subst

        print(f"   ✅ {len(df_subst)} movimentos de substituição encontrados")
        
        # Agora busca o equipamento ANTERIOR (o que foi devolvido)
        tombos_novos = df_subst['TOMBO_NOVO'].tolist()
        tombos_novos_sql = "(" + ", ".join([f"'{t}'" for t in tombos_novos]) + ")"
        
        # Query para pegar o histórico completo de cada equipamento novo
        query_historico = f"""
            SELECT 
                eq.id as EQ_ID,
                eq.numero as TOMBO,
                ami.id as ITEM_ID,
                mov.id as MOV_ID,
                mov.tipo_id as TIPO_ID,
                mov.cliente_id as CLIENTE_ID,
                COALESCE(mov.updated_at, mov.data) as DATA_MOV,
                mov.deleted_at as DEL_MOV
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens ami ON ami.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov ON mov.id = ami.movimento_id
            WHERE eq.numero IN {tombos_novos_sql}
              AND mov.deleted_at IS NULL
            ORDER BY eq.numero, mov.data DESC, mov.id DESC
        """
        
        with self.engine_legado.connect() as conn:
            df_historico = pd.read_sql(text(query_historico), conn)
        
        resultados = []
        
        for _, row_subst in df_subst.iterrows():
            mov_novo_id = row_subst['MOV_NOVO_ID']
            cliente_id = row_subst['CLIENTE_ID']
            data_subst = row_subst['DATA_SUBST']
            
            # Busca movimentos de devolução do mesmo cliente antes da substituição
            query_devolucao = f"""
                SELECT 
                    mov.id as MOV_DEV_ID,
                    ami.id as ITEM_DEV_ID,
                    eq.id as EQ_ANTIGO_ID,
                    eq.numero as TOMBO_ANTIGO,
                    eq.nome as NOME_ANTIGO,
                    mov.data as DATA_DEV,
                    mov.updated_at as UPDATED_DEV
                FROM aluguel_movimento mov
                INNER JOIN aluguel_movimento_itens ami ON ami.movimento_id = mov.id
                INNER JOIN aluguel_equipamentos eq ON eq.id = ami.equipamento_id
                WHERE mov.cliente_id = {cliente_id}
                  AND mov.tipo_id = 7
                  AND mov.deleted_at IS NULL
                  AND mov.data <= '{data_subst}'
                ORDER BY mov.data DESC, mov.id DESC
                LIMIT 1
            """
            
            with self.engine_legado.connect() as conn:
                df_dev = pd.read_sql(text(query_devolucao), conn)
            
            if not df_dev.empty:
                row_dev = df_dev.iloc[0]
                
                # Busca o movimento de aluguel ORIGINAL do equipamento antigo
                query_aluguel_original = f"""
                    SELECT 
                        mov.id as ORIG_MOV_ID,
                        COALESCE(NULLIF(mov.usuario_id, 0), 1) AS ORIG_USR_ID,
                        COALESCE(mov.updated_at, mov.data) AS ORIG_DATA,
                        mov.deleted_at AS ORIG_DEL
                    FROM aluguel_movimento mov
                    INNER JOIN aluguel_movimento_itens ami ON ami.movimento_id = mov.id
                    WHERE ami.equipamento_id = {row_dev['EQ_ANTIGO_ID']}
                      AND mov.tipo_id = 1
                      AND mov.deleted_at IS NULL
                      AND mov.cliente_id = {cliente_id}
                    ORDER BY mov.data ASC
                    LIMIT 1
                """
                
                with self.engine_legado.connect() as conn:
                    df_orig = pd.read_sql(text(query_aluguel_original), conn)
                
                resultado = {
                    'MOV_NOVO_ID': mov_novo_id,
                    'TOMBO_NOVO': row_subst['TOMBO_NOVO'],
                    'NOME_NOVO': row_subst['NOME_NOVO'],
                    'DATA_SUBST': data_subst,
                    'USR_SUBST': row_subst['USR_SUBST'],
                    'DEL_SUBST': row_subst['DEL_SUBST'],
                    'CLIENTE_ID': cliente_id,
                    
                    'MOV_DEV_ID': row_dev['MOV_DEV_ID'],
                    'TOMBO_ANTIGO': row_dev['TOMBO_ANTIGO'],
                    'NOME_ANTIGO': row_dev['NOME_ANTIGO'],
                    
                    'ORIG_MOV_ID': df_orig.iloc[0]['ORIG_MOV_ID'] if not df_orig.empty else None,
                    'ORIG_USR_ID': df_orig.iloc[0]['ORIG_USR_ID'] if not df_orig.empty else None,
                    'ORIG_DATA': df_orig.iloc[0]['ORIG_DATA'] if not df_orig.empty else None,
                    'ORIG_DEL': df_orig.iloc[0]['ORIG_DEL'] if not df_orig.empty else None,
                }
                resultados.append(resultado)
        
        df_final = pd.DataFrame(resultados)
        print(f"   🎯 {len(df_final)} substituições completas processadas")
        return df_final

    def calcular_saldo(
        self, contrato_item_id, recipient_id, equipment_id_ref, mov_date,
        item_servico_id_atual, fallback_contract_item_id=None, forcar_extra=False,
        is_kit_override=None, type_id_override=None
    ):
        if getattr(self, 'consumir_saldos', None) is False:
            return 0, None, int(contrato_item_id) if pd.notna(contrato_item_id) else None

        if pd.isna(contrato_item_id) or not contrato_item_id:
            return 0, None, None

        contrato_item_id = int(contrato_item_id)
        saldos = self.dados["saldos_por_id"]
        saldo_atual = saldos.get(contrato_item_id, 0)
        if is_kit_override is None:
            raise ValueError(
                f"Não foi possível determinar is_kit pelo nome do equipamento {equipment_id_ref}."
            )
        type_id = int(type_id_override) if type_id_override is not None else None
        is_kit = int(is_kit_override)

        # 🎯 REGRA EXCEDENTE: Não abate saldo se zerou e não é kit
        if saldo_atual <= 0 and is_kit == 0:
            return 0, None, contrato_item_id
        
        saldos[contrato_item_id] = saldo_atual - 1

        if saldos[contrato_item_id] < 0 and is_kit == 1:
            extra_id = self.extra_id_counter
            self.extra_id_counter += 1
            self.itens_extras_mestre.append({
                "id": extra_id, "service_order_item_id": item_servico_id_atual,
                "contract_item_id": contrato_item_id, "type_id": type_id,
                "quantity": 1, "removed_quantity": 0, "created_at": mov_date,
                "updated_at": mov_date, "deleted_at": None
            })
            return 1, extra_id, contrato_item_id 

        return 0, None, contrato_item_id
    
    def _atualizar_saldos_mysql(self):
        
        # 1. MODO FANTASMA: Se for chamado pela Devolução no futuro, não altera o banco!
        if getattr(self, 'consumir_saldos', None) is False:
            return

        saldos = self.dados["saldos_por_id"]
        modificados = []
        vistos = set()

        for info in self.dados["dict_contrato_item_por_chave"].values():
            c_id = info['id']
            qtd_orig = info['original_quantity']
            qtd_atual = saldos.get(c_id, qtd_orig)

            qtd_final_banco = max(0, qtd_atual)

            if qtd_final_banco != qtd_orig and c_id not in vistos:
                modificados.append({"id": c_id, "nova_qtd": qtd_final_banco})
                vistos.add(c_id)

        if modificados:
            with self.engine_new.begin() as conn:
                for item in modificados:
                    conn.execute(text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), item)
            print(f"  ✔️ {len(modificados)} saldos de contrato atualizados no MySQL (Valores negativos travados em 0).")

    def executar(self):
        print("\n" + "=" * 70)
        print("🔄 MÓDULO: SUBSTITUIÇÃO (A TRANSIÇÃO COMPLETA)")
        print("=" * 70)

        print("📖 Carregando itens dos Parquets e tipos por nome...")
        self._carregar_tipos_por_nome()
        df_parquet = self._carregar_itens_parquet()
        
        tombos_parquet = df_parquet["TOMBO"].astype(int).unique().tolist()
        
        # 🎯 NOVA ABORDAGEM: Buscar último movimento de cada tombo
        df_ultimos_mov = self._buscar_ultimo_movimento_por_tombo(tombos_parquet)
        
        if df_ultimos_mov.empty:
            print("⚠️ Nenhum movimento encontrado para os tombos do Parquet")
            return
        
        # Filtrar apenas os que são substituição (tipo_id = 5)
        df_subst_sql = df_ultimos_mov[df_ultimos_mov['TIPO_ID'] == 5].copy()
        
        if df_subst_sql.empty:
            print("⚠️ Nenhum movimento de substituição (tipo_id=5) encontrado")
            return
        
        print(f"   🎯 {len(df_subst_sql)} movimentos de substituição identificados")
        
        dict_subst_sql_by_tombo = {
            limpar_codigo(row["TOMBO"]): row
            for _, row in df_subst_sql.iterrows()
        }
        
        # 🎯 COMPARAÇÃO PURA: Se o Tombo existe no Parquet e no SQL, ele é validado!
        dict_row_parquet_by_tombo = {}
        
        for _, row_parquet in df_parquet.iterrows():
            tombo = str(row_parquet["TOMBO"]).strip()
            subst_sql = dict_subst_sql_by_tombo.get(tombo)

            if subst_sql is None:
                continue

            row_atual = dict_row_parquet_by_tombo.get(tombo)

            if (
                row_atual is None
                or row_parquet["COMPLETUDE_CONTRATO"] > row_atual["COMPLETUDE_CONTRATO"]
            ):
                dict_row_parquet_by_tombo[tombo] = row_parquet

        lista_tombos_subst = sorted(int(tombo) for tombo in dict_row_parquet_by_tombo)

        if not lista_tombos_subst:
            print("⚠️ Nenhum tombo dos Parquets apareceu na listagem de substituições.")
            return

        print(f"   🎯 {len(lista_tombos_subst)} tombos em Substituição identificados para processamento.")
        
        # Manda os movimentos de substituição para extrair os detalhes completos
        df_subst = self._extrair_dados_substituicao(df_subst_sql)
        if df_subst.empty:
            return

        # Puxa os IDs de todos os equipamentos (Novos e Antigos) no banco novo
        tombos_todos = list(set(df_subst['TOMBO_NOVO'].tolist() + df_subst['TOMBO_ANTIGO'].tolist()))
        dict_equip_novo = self.buscar_equipamentos_novo_por_tombo(tombos_todos)
        
        rejeitados = 0
        rejeitados_tipo = 0
        eqs_antigos_alterar = []
        eqs_novos_alterar = []

        for _, row in tqdm(df_subst.iterrows(), total=df_subst.shape[0], desc="Processando Ciclos"):
            
            mov_novo_id = int(row['MOV_NOVO_ID'])
            
            tombo_novo = limpar_codigo(row['TOMBO_NOVO'])
            tombo_antigo = limpar_codigo(row['TOMBO_ANTIGO'])
            eq_id_novo = dict_equip_novo.get(tombo_novo)
            eq_id_antigo = dict_equip_novo.get(tombo_antigo)
            row_parquet = dict_row_parquet_by_tombo.get(tombo_novo)
            
            if row_parquet is None or not eq_id_novo or not eq_id_antigo:
                rejeitados += 1
                continue

            nome_parquet = row_parquet.get("EQUIPAMENTO_NOME")
            nome_novo = (
                nome_parquet
                if pd.notna(nome_parquet) and str(nome_parquet).strip()
                else row["NOME_NOVO"]
            )
            tipo_novo = self._resolver_tipo_por_nome(nome_novo)
            if tipo_novo is None:
                print(
                    f"\n⚠️ Substituição {mov_novo_id} ignorada: tipo não identificado "
                    f"pelo nome '{nome_novo}'."
                )
                rejeitados_tipo += 1
                continue

            # 🎯 Pega o cliente_id de destino (prioriza a planilha Parquet)
            cli_leg_parquet = row_parquet.get("CLIENTE_ID")
            if pd.notna(cli_leg_parquet) and str(cli_leg_parquet).strip() != '':
                cli_legado_id = int(float(cli_leg_parquet))
            else:
                cli_legado_id = int(row['CLIENTE_ID']) if pd.notna(row['CLIENTE_ID']) else 0

            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            cliente_final_address = self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id)
            
            if not recipient_id:
                rejeitados += 1
                continue

            org_id_destino = descobrir_id_organizacao_destino(cli_legado_id)
            endereco_base_id = self.dict_enderecos_base_org.get(org_id_destino, 1)

            # =========================================================
            # MATCH DO CONTRATO (A Chave de Ouro é o Parquet)
            # =========================================================
            try:
                parquet_contract_id = int(float(row_parquet.get('CONTRACT_ID')))
            except (TypeError, ValueError):
                parquet_contract_id = None

            try:
                parquet_contract_item_id = int(float(row_parquet.get('CONTRACT_ITEM_ID')))
            except (TypeError, ValueError):
                parquet_contract_item_id = None

            contrato_id_res = parquet_contract_id
            item_id_res = None
            is_avulso = False

            if contrato_id_res is not None:
                if parquet_contract_item_id is not None:
                    item_id_res = parquet_contract_item_id
                else:
                    item_id_res = next(
                        (info['id'] for chave, info in self.dados["dict_contrato_item_por_chave"].items() if chave[1] == contrato_id_res),
                        None
                    )
            else:
                is_avulso = True

            # =========================================================
            # 🕰️ FASE 1: O PASSADO (ALUGAMOS A MÁQUINA ANTIGA)
            # =========================================================
            if pd.notna(row.get('ORIG_MOV_ID')):
                self.registrar_movimento(
                    id_final=int(row['ORIG_MOV_ID']),
                    recipient_id=recipient_id, cliente_final_address_id=cliente_final_address,
                    usuario_id=int(row['ORIG_USR_ID']), organization_id=org_id_destino,
                    mov_date=row['ORIG_DATA'],
                    deleted_at_mov=row['ORIG_DEL'] if pd.notna(row['ORIG_DEL']) else None,

                    contrato_id=contrato_id_res,
                    contrato_item_id=item_id_res,
                    equipment_id_ref=eq_id_antigo,
                    
                    status_shipment=2,
                    tipo_movimento_id=7 if is_avulso else 1,
                    operation_type='AVULSO' if is_avulso else 'ALUGUEL',

                    status_equipment_id=2, 
                    history_reason='SHIPPING_CONFIRMED_SEPARATE',
                    
                    is_exchange=False,
                    consumir_saldo=False,
                    alias_movimento=row['NOME_ANTIGO'],
                    details_capa="Migração (Reconstrução): Aluguel Histórico", details_item="Alocado no Cliente (Histórico)"
                )

            # =========================================================
            # 📥 FASE 2: O PRESENTE (RECOLHEMOS A MÁQUINA ANTIGA)
            # =========================================================
            id_mov_dev = int(row['MOV_DEV_ID'])
            dt_subst = row['DATA_SUBST']
            usr_subst = int(row['USR_SUBST'])

            _, _, id_mov_item_dev = self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id, 
                cliente_final_address_id=endereco_base_id,
                usuario_id=usr_subst, 
                organization_id=org_id_destino,
                mov_date=dt_subst, 
                deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                
                contrato_id=contrato_id_res, 
                contrato_item_id=item_id_res, 
                equipment_id_ref=eq_id_antigo, status_shipment=1,
                
                tipo_movimento_id=2,
                operation_type='SUBSTITUICAO',
                
                status_equipment_id=8, 
                history_reason='SHIPPING_CONFIRMED_DEVOLUTION',
                
                is_exchange=False,
                consumir_saldo=False,
                alias_movimento=row['NOME_ANTIGO'], 
                details_capa="Migração: Retorno por Substituição", 
                details_item="Equipamento Substituído (Retorno)"
            )

            eqs_antigos_alterar.append({int(eq_id_antigo): int(id_mov_item_dev)})

            # =========================================================
            # 📤 FASE 3: O PRESENTE (ENVIAMOS A MÁQUINA NOVA)
            # =========================================================
            _, _, id_mov_item_novo = self.registrar_movimento(
                id_final=mov_novo_id,
                recipient_id=recipient_id, 
                cliente_final_address_id=cliente_final_address,
                usuario_id=usr_subst, 
                organization_id=org_id_destino,
                mov_date=dt_subst, 
                deleted_at_mov=row['DEL_SUBST'] if pd.notna(row['DEL_SUBST']) else None,
                
                contrato_id=contrato_id_res, 
                contrato_item_id=item_id_res, 
                equipment_id_ref=eq_id_novo, status_shipment=1,
                
                tipo_movimento_id=2,
                operation_type='SUBSTITUICAO',
                
                status_equipment_id=2, 
                history_reason='SHIPPING_CONFIRMED_SEPARATE',

                is_exchange=False,
                consumir_saldo=False,
                is_kit_override=tipo_novo["is_kit"],
                type_id_override=tipo_novo["id"],
                alias_movimento=row['NOME_NOVO'], 
                details_capa="Migração: Envio por Substituição", 
                details_item="Equipamento Novo (Substituto)"
            )
            eqs_novos_alterar.append({int(eq_id_novo): int(id_mov_item_novo)})

        print(f"\n⚠️ Registros rejeitados (Sem equipamento encontrado): {rejeitados}")
        print(f"⚠️ Registros rejeitados (Tipo não identificado pelo nome): {rejeitados_tipo}")

        self.salvar_movimentos_banco()

        self.atualizar_equipamentos_banco(id_status_equipamento=8, lista_dicionarios=eqs_antigos_alterar)
        self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=eqs_novos_alterar)

        self._atualizar_saldos_mysql()

# ==============================================================================
# WRAPPER 
# ==============================================================================
def executar(eng_novo, eng_legado):
    from movimentos.migracao_movimentos import carregar_dados_compartilhados
    print("\n" + "="*70)
    print("🚀 MODO DEBUG: Disparando teste isolado de SUBSTITUIÇÃO")
    print("="*70)
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)
    app_teste = MigracaoSubstituicao(eng_novo, eng_legado, dados_ram, start_counter=900000)
    app_teste.executar()