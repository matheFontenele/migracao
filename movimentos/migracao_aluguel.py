import os
import glob
import pandas as pd
from sqlalchemy import text
from datetime import datetime
from tqdm import tqdm

from movimentos.migracao_movimentos import carregar_dados_compartilhados, resetar_saldo_contract_items
from utils.sanetizador import executar_truncate_tabelas, normalizar_para_match
from movimentos.migracao_movimentos import BaseMigracaoMovimento

TABELAS = [
    "service_order_item_extra_equipments", "movement_items", "movements", "service_order_items", "service_orders"
]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class MigracaoAluguel(BaseMigracaoMovimento):

    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=1, limpar_ambiente=True):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter, limpar_ambiente)
        self.saldos_modificados = set()

    def limpar_tabelas_movimento(self):
        if self.limpar_ambiente:
            print("\n🧹 [ALUGUEL] Iniciando faxina estrutural nas tabelas transacionais...")
            executar_truncate_tabelas(self.engine_new, TABELAS)  

    def calcular_saldo(self, *args, **kwargs):
        """Dummy fantasma: A matemática do saldo do Excedente ocorre na classe Pai!"""
        return 0, None, int(args[0]) if pd.notna(args[0]) else None
    
    def _atualizar_saldos_mysql(self):
        if getattr(self, 'consumir_saldos', None) is False or not self.saldos_modificados: return
        print(f"\n💾 Sincronizando {len(self.saldos_modificados)} saldos modificados com o MySQL...")
        atualizados = 0
        with self.engine_new.begin() as conn:
            for c_id in self.saldos_modificados:
                qtd_final_banco = max(0, int(self.dados["saldos_por_id"][c_id]))
                res = conn.execute(text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), {"nova_qtd": qtd_final_banco, "id": c_id})
                if res.rowcount > 0: atualizados += 1
        print(f"  ✔️ {atualizados} itens de contrato atualizados com sucesso!")

    def _salvar_relatorio_clientes_sem_contrato(self, registros):
        colunas = [
            'ID_CLIENTE_LEGADO', 'CLIENTE', 'ID_CUSTOMER_REFATORADO',
            'MOTIVO', 'CONTRACT_ID_PARQUET', 'CONTRATO_PARQUET',
            'QTD_EQUIPAMENTOS_PULADOS', 'TOMBOS',
        ]
        linhas = []
        for info in registros.values():
            linhas.append({
                'ID_CLIENTE_LEGADO': info.get('ID_CLIENTE_LEGADO'),
                'CLIENTE': info.get('CLIENTE'),
                'ID_CUSTOMER_REFATORADO': info.get('ID_CUSTOMER_REFATORADO'),
                'MOTIVO': info.get('MOTIVO'),
                'CONTRACT_ID_PARQUET': ', '.join(sorted(info['CONTRACT_ID_PARQUET'])),
                'CONTRATO_PARQUET': ' | '.join(sorted(info['CONTRATO_PARQUET'])),
                'QTD_EQUIPAMENTOS_PULADOS': len(info['TOMBOS']),
                'TOMBOS': ', '.join(sorted(info['TOMBOS'])),
            })

        df_relatorio = pd.DataFrame(linhas, columns=colunas)
        caminho = 'docs/clientes_sem_contrato.xlsx'
        df_relatorio.to_excel(caminho, index=False)
        print(
            f"📄 Relatório de clientes/movimentos sem contrato salvo em "
            f"'{caminho}' ({len(df_relatorio)} registros agrupados)."
        )

    def _resolver_item_parquet(self, item_id_csv, alias_item, description_item, legacy_client_id, contract_id):
        """Valida o ID do Parquet e, se necessário, resolve o item pelo alias."""
        if item_id_csv is not None:
            item_id = int(item_id_csv)
            item_info = self.dados.get('dict_item_por_id', {}).get(item_id)
            contract_id_do_item = item_info.get('contract_id') if item_info else None
            if contract_id_do_item == contract_id:
                return item_id, False

        alias_normalizado = normalizar_para_match(alias_item)
        if not alias_normalizado or contract_id is None:
            return None, item_id_csv is not None

        descricao_normalizada = normalizar_para_match(description_item)
        candidatos_exatos = [
            item_id
            for item_id, info in self.dados.get('dict_item_por_id', {}).items()
            if info['contract_id'] == int(contract_id)
            and normalizar_para_match(info.get('alias')) == alias_normalizado
            and normalizar_para_match(info.get('description')) == descricao_normalizada
        ]
        if len(candidatos_exatos) == 1:
            item_resolvido = candidatos_exatos[0]
            return item_resolvido, item_resolvido != item_id_csv

        # A consulta compartilhada vem ordenada pelos itens mais recentes;
        # setdefault mantém o primeiro item quando o alias se repete em aditivos.
        if not hasattr(self, '_indice_item_por_contrato_alias'):
            indice_cliente = {}
            indice_contrato = {}
            for chave, info in self.dados.get('dict_contrato_item_aluguel_por_chave', {}).items():
                legacy_id_item, contrato_id_item, alias_norm, _ = chave
                indice_cliente.setdefault(
                    (int(legacy_id_item), int(contrato_id_item), alias_norm),
                    int(info['id']),
                )
                indice_contrato.setdefault(
                    (int(contrato_id_item), alias_norm),
                    int(info['id']),
                )
            self._indice_item_por_contrato_alias = (indice_cliente, indice_contrato)

        indice_cliente, indice_contrato = self._indice_item_por_contrato_alias
        item_resolvido = indice_cliente.get(
            (int(legacy_client_id), int(contract_id), alias_normalizado)
        ) or indice_contrato.get((int(contract_id), alias_normalizado))
        return item_resolvido, item_resolvido is not None and item_resolvido != item_id_csv
    
    # ==============================================================================
    # MÉTODO CUSTOMIZADO: BUSCA DE MOVIMENTOS COM FILTRO DE TOMBOS (PARQUET)
    # ==============================================================================
    def _buscar_ultimos_movimentos_aluguel(self, tombos_list):
        if not tombos_list: return {}
        
        print(f"🔍 Buscando últimos movimentos no legado cruzando com {len(tombos_list)} tombos do Parquet...")
        
        # Formata a lista para '1234', '5678', etc. para o clause IN do SQL
        tombos_formatados = ",".join(f"'{str(t).strip()}'" for t in tombos_list)
        
        query = f"""
            SELECT
                eq.id AS equipamento_id,
                eq.numero AS tombo,
                eq.nome AS nome_equipamento,
                eq.tipo_id AS eq_tipo_id,

                mov.id AS id,
                mov.data AS data_movimento,
                mov.updated_at,
                mov.deleted_at,
                mov.cliente_id,
                mov.usuario_id,
                mov.tipo_id AS tipo_id,
                mov.tipo AS tipo_movimento
            FROM aluguel_equipamentos eq
            INNER JOIN aluguel_movimento_itens movi ON movi.equipamento_id = eq.id
            INNER JOIN aluguel_movimento mov       ON mov.id = movi.movimento_id
            WHERE mov.deleted_at IS NULL
            AND movi.deleted_at IS NULL
            AND eq.deleted_at IS NULL
            AND eq.situacao_id = 1
            AND mov.tipo_id IN (1, 2)
            AND eq.numero IN ({tombos_formatados})
            AND mov.id = (
                    -- 🆕 substitui o ROW_NUMBER() ... rn = 1:
                    -- último movimento do equipamento (por data, desempate por id)
                    SELECT mov_x.id
                    FROM aluguel_movimento_itens movi_x
                    INNER JOIN aluguel_movimento mov_x ON mov_x.id = movi_x.movimento_id
                    WHERE movi_x.equipamento_id = eq.id
                    AND mov_x.deleted_at IS NULL
                    AND movi_x.deleted_at IS NULL
                    ORDER BY mov_x.data DESC, mov_x.id DESC
                    LIMIT 1
            )
        """
        
        with self.engine_legado.connect() as conn:
            df_movs = pd.read_sql(text(query), conn)
            
        dict_retorno = {}
        for _, row in df_movs.iterrows():
            dict_retorno[str(row['tombo']).strip()] = {
                'movimento': row.to_dict()
            }
        return dict_retorno

    # ==============================================================================
    # ORQUESTRAÇÃO
    # ==============================================================================
    def executar(self):
        print("\n" + "-" * 70)
        print("📦 MÓDULO: ALUGUEL (Fonte: CSV Parquet)")
        print("-" * 70)

        self.limpar_tabelas_movimento()
        
        caminho_types = "./docs/types.csv"
        if os.path.exists(caminho_types):
            print("📖 Carregando mapeamento de Tipos e Kits (types.csv)...")
            self.dict_is_kit = {int(row['id']): int(row['is_kit']) for _, row in pd.read_csv(caminho_types).iterrows() if pd.notna(row['id'])}
        else:
            print("⚠️ Arquivo types.csv não encontrado. Todos os equipamentos assumirão is_kit = 0.")
            self.dict_is_kit = {}

        arquivos_parquet = glob.glob(os.path.join("./docs/parquets", "*.parquet"))
        if not arquivos_parquet: return

        print(f"📖 Lendo dados de {len(arquivos_parquet)} arquivos Parquet...")
        df_csv = pd.concat([pd.read_parquet(arq).rename(columns=str.upper) for arq in arquivos_parquet], ignore_index=True)
        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        if 'ITEM_DO_CONTRATO' in df_csv.columns:
            df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
            df_csv = df_csv[df_csv['ITEM_DO_CONTRATO'].str.lower() != 'nan']
        df_csv['CONTRACT_ID'] = df_csv['CONTRACT_ID'].astype(str).str.replace('.0', '', regex=False)
        
        tombos = df_csv['TOMBO'].unique().tolist()
        
        # 👇 INJEÇÃO DO NOVO MÉTODO SQL AQUI
        dict_ultimo_mov = self._buscar_ultimos_movimentos_aluguel(tombos)
        dict_equipamentos_novo = self.buscar_equipamentos_novo_por_tombo(tombos)

        with self.engine_new.connect() as conn:
            dict_contract_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM contracts", conn).values.T))
            dict_customer_org = dict(zip(*pd.read_sql("SELECT id, organization_id FROM customers", conn).values.T))
            dict_equip_org = dict(zip(*pd.read_sql("SELECT id, current_organization_id FROM equipments", conn).values.T))

        log_nao_match = []
        rejeitados = 0
        clientes_sem_contrato = {}

        # Vínculos reais disponíveis no banco refatorado. Isso evita inserir
        # contract_id vindo do Parquet quando esse ID não existe no destino.
        with self.engine_new.connect() as conn:
            df_contracts = pd.read_sql(
                "SELECT id AS contract_id, customer_id FROM contracts", conn
            )
            df_contract_recipients = pd.read_sql(
                "SELECT contract_id, customer_id FROM contract_recipient_customers", conn
            )
        contratos_validos = set(df_contracts['contract_id'].astype(int))
        df_vinculos_contrato_cliente = pd.concat(
            [df_contracts, df_contract_recipients], ignore_index=True
        ).dropna(subset=['contract_id', 'customer_id'])
        contratos_por_cliente = (
            df_vinculos_contrato_cliente.groupby('customer_id')['contract_id']
            .apply(lambda values: set(values.astype(int)))
            .to_dict()
        )

        def registrar_cliente_sem_contrato(row, cli_legado_id, recipient_id, motivo):
            chave = (cli_legado_id, motivo)
            info = clientes_sem_contrato.setdefault(chave, {
                'ID_CLIENTE_LEGADO': cli_legado_id,
                'CLIENTE': row_csv_name(row, cli_legado_id),
                'ID_CUSTOMER_REFATORADO': recipient_id,
                'MOTIVO': motivo,
                'CONTRACT_ID_PARQUET': set(),
                'CONTRATO_PARQUET': set(),
                'TOMBOS': set(),
            })
            raw_contract_id = row.get('CONTRACT_ID')
            contract_id_num = pd.to_numeric(raw_contract_id, errors='coerce')
            if pd.notna(contract_id_num):
                info['CONTRACT_ID_PARQUET'].add(str(int(contract_id_num)))
            contract_name = row.get('CONTRATO')
            if pd.notna(contract_name) and str(contract_name).strip():
                info['CONTRATO_PARQUET'].add(str(contract_name).strip())
            info['TOMBOS'].add(str(row.get('TOMBO', '')).strip())

        def row_csv_name(row, cli_legado_id):
            nome = row.get('CLIENTE_NOME')
            return str(nome).strip() if pd.notna(nome) else f'ID legado {cli_legado_id}'
        
        for _, row_csv in tqdm(df_csv.iterrows(), total=df_csv.shape[0], desc="Processando ALUGUEL"):

            tombo = str(row_csv['TOMBO']).strip()
            ultimo_mov = dict_ultimo_mov.get(tombo)

            # O SQL já garante que se veio algo, é tipo 1 ou 2, com situação 1, e não deletado!
            if not ultimo_mov: 
                rejeitados += 1
                continue

            row_mov = ultimo_mov['movimento']
            
            cli_leg_parquet = row_csv.get('CLIENTE_ID')
            if pd.notna(cli_leg_parquet) and str(cli_leg_parquet).lower() not in ['', 'nan', 'none']:
                cli_legado_id = int(float(cli_leg_parquet))
            else:
                cli_legado_id = int(row_mov['cliente_id'])
            
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            equipment_id_ref = dict_equipamentos_novo.get(tombo)
            
            if not recipient_id or not equipment_id_ref: 
                rejeitados += 1
                continue

            contratos_cliente = contratos_por_cliente.get(int(recipient_id), set())
            if not contratos_cliente:
                registrar_cliente_sem_contrato(
                    row_csv, cli_legado_id, recipient_id, 'CLIENTE_SEM_CONTRATO_NO_REFATORADO'
                )
                rejeitados += 1
                continue

            # ==================================================================
            # 1. APLICA AS REGRAS USANDO O CÉREBRO DA CLASSE PAI
            # ==================================================================      
            raw_contract_id = row_csv.get('CONTRACT_ID')
            csv_contract_id = int(float(raw_contract_id)) if pd.notna(raw_contract_id) and str(raw_contract_id).strip() not in ['None', 'nan', ''] else None

            if csv_contract_id is not None and csv_contract_id not in contratos_validos:
                registrar_cliente_sem_contrato(
                    row_csv, cli_legado_id, recipient_id,
                    'CONTRACT_ID_DO_PARQUET_AUSENTE_NO_REFATORADO',
                )
                rejeitados += 1
                continue

            raw_item_id = row_csv.get('CONTRACT_ITEM_ID')
            csv_item_id = int(float(raw_item_id)) if pd.notna(raw_item_id) and str(raw_item_id).strip() not in ['None', 'nan', ''] else None
            item_id_original = csv_item_id
            csv_item_id, item_remapeado = self._resolver_item_parquet(
                csv_item_id,
                row_csv.get('ITEM_DO_CONTRATO'),
                row_csv.get('DESCRICAO_ITEM'),
                cli_legado_id,
                csv_contract_id,
            )
            
            (contrato_id_res, item_id_res, is_avulso, is_kit, is_excedente, teve_match_perfeito, motivo_divergencia) = self.regras_item_contratos(
                csv_contract_id, csv_item_id, equipment_id_ref, recipient_id, self.dict_is_kit, abater_saldo=True
            )

            # Logs de Auditoria
            if motivo_divergencia:
                status_final_log = "AVULSO (SEM CONTRATO)" if is_avulso else "KIT (IMUNE)" if is_kit else "EXCEDENTE (IS_EXCHANGE)" if is_excedente else "ALUGUEL NORMAL"
                log_nao_match.append({
                    "TOMBO": tombo, "EQUIPAMENTO_CSV": row_csv.get('EQUIPAMENTO_NOME', 'NÃO INFORMADO'),
                    "ID_CLIENTE_LEGADO": cli_legado_id, "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRACT_ID_CSV": csv_contract_id if csv_contract_id else "VAZIO", "CONTRATO_RESOLVIDO": contrato_id_res if contrato_id_res else "NENHUM (AVULSO)",
                    "ITEM_CSV": row_csv.get('ITEM_DO_CONTRATO', 'VAZIO'), "DESC_ITEM_CSV": row_csv.get('DESCRICAO_ITEM', 'VAZIO'),
                    "ITEM_RESOLVIDO_ID": item_id_res if item_id_res else "NENHUM", "STATUS_FINAL": status_final_log, "MOTIVO_EXATO": motivo_divergencia
                })
            elif item_remapeado:
                log_nao_match.append({
                    "TOMBO": tombo,
                    "EQUIPAMENTO_CSV": row_csv.get('EQUIPAMENTO_NOME', 'NÃO INFORMADO'),
                    "ID_CLIENTE_LEGADO": cli_legado_id,
                    "ID_CLIENTE_NOVO": recipient_id,
                    "CONTRACT_ID_CSV": csv_contract_id,
                    "CONTRATO_RESOLVIDO": contrato_id_res,
                    "ITEM_CSV": row_csv.get('ITEM_DO_CONTRATO', 'VAZIO'),
                    "ITEM_ID_CSV_ORIGINAL": item_id_original,
                    "ITEM_RESOLVIDO_ID": item_id_res,
                    "STATUS_FINAL": "ITEM REMAPEADO POR ALIAS",
                    "MOTIVO_EXATO": "ID do item do Parquet não existe no contrato atual; remapeado pelo alias do item.",
                })

            usr_id = int(row_mov['usuario_id']) if pd.notna(row_mov['usuario_id']) and row_mov['usuario_id'] != 0 else 1
            dt_mov = row_mov['data_movimento'] if pd.notna(row_mov['data_movimento']) else self.now

            detalhes_item = "Movimento Avulso (Cliente sem contrato ativo)" if is_avulso else "Equipamento Kit (Imune a saldo, sem item vinculado)" if is_kit else "Equipamento Excedente (Contrato sem saldo)" if is_excedente else "Item Extra Oficial (Fallback de Contrato/Item)" if not teve_match_perfeito else None

            org_id_resolvida = dict_contract_org.get(contrato_id_res) or dict_customer_org.get(recipient_id) or dict_equip_org.get(equipment_id_ref) or 1115
            id_legado_origem = int(row_mov['id'])

            self.registrar_movimento(
                id_final=id_legado_origem,
                recipient_id=recipient_id,
                cliente_final_address_id=self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id),
                usuario_id=usr_id,
                organization_id=int(org_id_resolvida),
                mov_date=dt_mov,
                deleted_at_mov=row_mov['deleted_at'] if pd.notna(row_mov['deleted_at']) else None,
                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=equipment_id_ref,
                status_shipment=2,
                tipo_movimento_id=7 if is_avulso else 1,
                operation_type='AVULSO' if is_avulso else 'ALUGUEL',
                status_equipment_id=2,
                history_reason='SHIPPING_CONFIRMED_SEPARATE' if is_avulso else 'SHIPPING_CONFIRMED_RENT',
                forcar_extra=False,
                is_exchange=is_excedente,
                alias_item=str(row_csv.get('ITEM_DO_CONTRATO')).strip() if pd.notna(row_csv.get('ITEM_DO_CONTRATO')) else None,
                alias_movimento=row_csv.get('EQUIPAMENTO_NOME'),
                details_capa=f"Gerado por migração: registro legado {id_legado_origem}",
                details_item=detalhes_item,
                forcar_atualizacao_parque=True
            )

        if log_nao_match:
            print(f"📝 {len(log_nao_match)} regras comerciais processadas. Salvando log...")
            pd.DataFrame(log_nao_match).to_csv("log_divergencias_aluguel.csv", index=False, encoding="utf-8")
            print("   📄 Log salvo em 'log_divergencias_aluguel.csv'")

        self._salvar_relatorio_clientes_sem_contrato(clientes_sem_contrato)
            
        print(f"   ⏩ Ignorados (Sua última ação real foi Substituição, não Aluguel original): {rejeitados}")

        self.salvar_movimentos_banco()
        self.atualizar_equipamentos_banco(id_status_equipamento=2, lista_dicionarios=self.equipamentos_alterados)
        self._atualizar_saldos_mysql()
        
def executar(eng_novo, eng_legado):
    from movimentos.migracao_aluguel import resetar_saldo_contract_items, carregar_dados_compartilhados
    print("\n" + "="*70 + "\n🚀 MODO DEBUG: Disparando teste isolado de ALUGUEL\n" + "="*70)
    print("\n🧹 Executando faxina e reset de saldos...\n🔄 Ajustando regras de negócio pré-migração...")
    resetar_saldo_contract_items(eng_novo)
    print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
    dados_ram = carregar_dados_compartilhados(eng_legado, eng_novo)
    MigracaoAluguel(eng_novo, eng_legado, dados_ram, start_counter=1).executar()
