import os
import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from migracao_movimentos import BaseMigracaoMovimento, limpar_codigo, normalizar_texto

class MigracaoAluguel(BaseMigracaoMovimento):
    """
    Filho 1: Especialista em Aluguel via CSV.
    Herda toda a engine de persistência e sobrescreve apenas o controle de Saldo.
    """

    def _buscar_ultimo_movimento_por_tombo(self, lista_tombos):
        """Query isolada: Busca no legado apenas o último movimento dos tombos do CSV."""
        lista_tombos_sql = "(" + ", ".join(map(str, lista_tombos)) + ")"
        query = f"""
            SELECT am.id, am.data, am.tipo_id, am.cliente_id, am.usuario_id, 
                   am.updated_at, am.deleted_at, ae.numero AS tombo
            FROM aluguel_movimento am
            INNER JOIN aluguel_movimento_itens ami ON ami.movimento_id = am.id
            INNER JOIN aluguel_equipamentos ae ON ae.id = ami.equipamento_id
            WHERE am.deleted_at IS NULL AND ae.numero IN {lista_tombos_sql}
              AND am.id = (
                  SELECT am2.id FROM aluguel_movimento am2
                  INNER JOIN aluguel_movimento_itens ami2 ON ami2.movimento_id = am2.id
                  WHERE ami2.equipamento_id = ae.id AND am2.deleted_at IS NULL
                  ORDER BY am2.updated_at DESC, am2.data DESC LIMIT 1
              )
        """
        df_res = pd.read_sql(text(query), self.engine_legado)
        
        dict_hist = {}
        for _, row in df_res.iterrows():
            t = limpar_codigo(row['tombo'])
            if t and t != 'nan': dict_hist[t] = row.to_dict()
        return dict_hist


    # ==========================================================================
    # 🌟 O OVERRIDE POLIMÓRFICO: A regra de dedução de saldo do Aluguel
    # ==========================================================================
    def calcular_saldo(self, contrato_item_id, recipient_id, equipment_id_ref, mov_date, item_servico_id_atual):
        saldos = self.dados["saldos_por_id"]
        dict_tipo = self.dados["dict_tipo_por_equipamento"]

        # Cenário A: Deu match perfeito no contrato E ainda tem saldo
        if contrato_item_id and saldos.get(contrato_item_id, 0) > 0:
            saldos[contrato_item_id] -= 1
            return 0, None, contrato_item_id  # (is_extra=0, extra_id=None, item_id)

        # Cenário B: Acabou o saldo OU não deu match (Vira Item Extra!)
        item_vinculo = contrato_item_id if contrato_item_id else self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)
        
        extra_id = self.extra_id_counter
        self.extra_id_counter += 1

        self.itens_extras_mestre.append({
            "id": extra_id, "service_order_item_id": item_servico_id_atual,
            "contract_item_id": item_vinculo, "type_id": dict_tipo.get(equipment_id_ref),
            "quantity": 1, "removed_quantity": 0, "created_at": mov_date, 
            "updated_at": mov_date, "deleted_at": None
        })

        return 1, extra_id, item_vinculo  # (is_extra=1, extra_id=999, fallback_id)


    def _atualizar_saldos_mysql(self):
        """Dispara o UPDATE físico na tabela contract_items ao final do processo."""
        saldos = self.dados["saldos_por_id"]
        modificados = []
        vistos = set()

        for info in self.dados["dict_contrato_item_por_chave"].values():
            c_id = info['id']
            qtd_orig = info['original_quantity']
            qtd_atual = saldos.get(c_id, qtd_orig)

            if qtd_atual != qtd_orig and c_id not in vistos:
                modificados.append({"id": c_id, "nova_qtd": qtd_atual})
                vistos.add(c_id)

        if modificados:
            with self.engine_new.begin() as conn:
                for item in modificados:
                    conn.execute(text("UPDATE contract_items SET available_quantity = :nova_qtd WHERE id = :id"), item)
            print(f"  ✔️ {len(modificados)} saldos de contrato atualizados no MySQL.")


    # ==========================================================================
    # ENTRYPOINT DO ALUGUEL
    # ==========================================================================
    def executar(self):
        print("\n" + "-" * 70)
        print("📦 MÓDULO: ALUGUEL (Fonte: CSV)")
        print("-" * 70)

        caminho_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "equipeAS.csv")
        
        print("📖 Carregando planilha auxiliar de aluguel...")
        df_csv = pd.read_csv(caminho_csv, sep=",", encoding="utf-8", on_bad_lines="skip", low_memory=False)
        df_csv['TOMBO'] = pd.to_numeric(df_csv['TOMBO'], errors='coerce')
        df_csv = df_csv.dropna(subset=['TOMBO'])
        df_csv['TOMBO'] = df_csv['TOMBO'].astype(int).astype(str)
        df_csv['CLIENTE_ID'] = df_csv['CLIENTE_ID'].astype(str).str.replace('.0', '', regex=False)
        df_csv['ITEM_DO_CONTRATO'] = df_csv['ITEM_DO_CONTRATO'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
        df_csv = df_csv[df_csv['ITEM_DO_CONTRATO'].str.lower() != 'nan']

        tombos = df_csv['TOMBO'].astype(int).unique().tolist()
        dict_ultimo_mov = self._buscar_ultimo_movimento_por_tombo(tombos)
        print(f"   ✅ {len(dict_ultimo_mov)} tombos indexados no legado.")

        for _, row_csv in tqdm(df_csv.iterrows(), total=df_csv.shape[0], desc="Processando ALUGUEL"):
            tombo = str(row_csv['TOMBO']).strip()
            contrato_item_str = str(row_csv['ITEM_DO_CONTRATO']).strip()
            
            ultimo_mov = dict_ultimo_mov.get(tombo)
            if not ultimo_mov: continue
            if ultimo_mov['tipo_id'] not in {1, 5}: continue

            cli_legado_id = int(ultimo_mov['cliente_id'])
            recipient_id = self.dados["dict_cliente_adress"].get(cli_legado_id)
            if not recipient_id: continue

            # Match de chaves compostas
            nome_c = normalizar_texto(row_csv.get('CONTRATO'))
            desc_i = normalizar_texto(row_csv.get('DESCRICAO_ITEM'))
            chave_busca = (int(recipient_id), nome_c, contrato_item_str.upper(), desc_i)
            match_contrato = self.dados["dict_contrato_item_por_chave"].get(chave_busca)

            contrato_id_res = match_contrato['contract_id'] if match_contrato else self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            item_id_res = match_contrato['id'] if match_contrato else None

            usr_id = int(ultimo_mov['usuario_id']) if pd.notna(ultimo_mov['usuario_id']) and ultimo_mov['usuario_id'] != 0 else 1
            dt_mov = ultimo_mov['updated_at'] if pd.notna(ultimo_mov['updated_at']) else self.now

            # 🌟 DELEGA A CONSTRUÇÃO DO PAYLOAD PARA O MOTOR PAI
            self.registrar_movimento(
                id_final=int(ultimo_mov['id']),
                recipient_id=recipient_id,
                cliente_final_address_id=self.dados["dict_endereco_por_legacy_client"].get(cli_legado_id),
                usuario_id=usr_id,
                mov_date=dt_mov,
                deleted_at_mov=ultimo_mov['deleted_at'] if pd.notna(ultimo_mov['deleted_at']) else None,
                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=self.dados["dict_equip_ref_por_number"].get(tombo),
                tipo_movimento_id=1,
                operation_type='ALUGUEL',
                alias_item=contrato_item_str if contrato_item_str != '' else None,
                alias_movimento=row_csv['EQUIPAMENTO_NOME'],
                details_capa="Migração",
                details_item=None if match_contrato else "Item Extra (Sem Match de Contrato)"
            )

        # Chama a persistência genérica herdada do Pai crava o status 2 = Alugado
        self.salvar_banco(id_status_equipamento=2)
        self._atualizar_saldos_mysql()