import pandas as pd
from sqlalchemy import text
from tqdm import tqdm
from datetime import datetime

# Herança direta do Motor Pai
from migracao_movimentos import BaseMigracaoMovimento, limpar_codigo

class MigracaoDevolucao(BaseMigracaoMovimento):
    """
    Classe especialista em Devoluções.
    Varre o histórico do legado, reconstrói a Ida (Aluguel/Reserva) 
    e consolida a Volta (Devolução) em formato de par cronológico.
    """

    def _extrair_pares_devolucao(self):
        """
        Analisa o histórico contido na RAM compartilhada para agrupar 
        o último movimento (Devolução) com o seu respectivo penúltimo (Aluguel/Reserva).
        """
        print("🔍 Analisando histórico de movimentos para formar os pares (Ida ➔ Volta)...")
        
        df_mi = self.dados["df_movimento_item_legado"]
        df_m = self.dados["df_movimentos_legado"]
        
        # Junta os itens com as capas de movimento e ordena (Equipamento + ID decrescente)
        df_full = pd.merge(df_mi, df_m, left_on='movimento_id', right_on='id')
        df_full = df_full.sort_values(by=['equipamento_id', 'movimento_id'], ascending=[True, False])

        # 1. Isola o ÚLTIMO movimento de cada máquina
        df_latest = df_full.drop_duplicates(subset=['equipamento_id'], keep='first')
        
        # Filtragem crucial: Só nos interessam equipamentos cujo ÚLTIMO estado seja Devolução (tipo_id = 2)
        df_devolvidos = df_latest[df_latest['tipo_id'] == 2].copy()

        # 2. Isola o PENÚLTIMO movimento de cada máquina (removendo o último do bolo)
        df_full_no_latest = df_full[~df_full['movimento_id'].isin(df_latest['movimento_id'])]
        df_penultimate = df_full_no_latest.drop_duplicates(subset=['equipamento_id'], keep='first')

        # 3. O Cruzamento Perfeito: Junta a Volta com a sua respectiva Ida
        df_pares = pd.merge(
            df_devolvidos, df_penultimate, 
            on='equipamento_id', 
            suffixes=('_dev', '_ida')
        )
        
        return df_pares

    # ==============================================================================
    # ENTRYPOINT DO MÓDULO DEVOLUÇÃO
    # ==============================================================================
    def executar(self):
        print("\n" + "-" * 70)
        print("📦 MÓDULO: DEVOLUÇÃO (Fonte: Histórico SQL)")
        print("-" * 70)

        # 1. Extrai os pares ordenados direto da memória RAM
        df_pares = self._extrair_pares_devolucao()
        
        if df_pares.empty:
            print("⚠️ Nenhuma devolução com histórico de ida encontrada para migrar.")
            return
            
        print(f"   ✅ {len(df_pares)} equipamentos prontos para reconstituição de ciclo completo.")

        # 2. Laço principal de processamento do Par
        for _, row in tqdm(df_pares.iterrows(), total=df_pares.shape[0], desc="Processando DEVOLUÇÕES"):
            
            equip_id_legado = row['equipamento_id']
            tombo = self.dados["dict_tombo_por_equip_id"].get(equip_id_legado)
            equipment_id_ref = self.dados["dict_equip_ref_por_number"].get(tombo)
            
            # Se o equipamento não existir no sistema refatorado, pula
            if not equipment_id_ref: 
                continue

            # Dados extraídos da IDA (Penúltimo Movimento)
            id_mov_ida = int(row['movimento_id_ida'])
            tipo_ida_legado = int(row['tipo_id_ida'])
            cliente_id_legado_ida = int(row['cliente_id_ida'])
            usuario_ida = int(row['usuario_id_ida']) if row['usuario_id_ida'] else 1
            data_ida = row['updated_at_ida'] if pd.notna(row['updated_at_ida']) else self.now

            # Dados extraídos da VOLTA (Último Movimento - Devolução)
            id_mov_dev = int(row['movimento_id_dev'])
            usuario_dev = int(row['usuario_id_dev']) if row['usuario_id_dev'] else 1
            data_dev = row['updated_at_dev'] if pd.notna(row['updated_at_dev']) else self.now

            # Localiza os endereçamentos do cliente baseado na época da Ida
            recipient_id = self.dados["dict_cliente_adress"].get(cliente_id_legado_ida)
            cliente_final = self.dados["dict_endereco_por_legacy_client"].get(cliente_id_legado_ida)
            
            if not recipient_id: 
                continue

            # Mapeia contratos de amarração básicos
            contrato_id = self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            contrato_item_id = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)

            # ==================================================================
            # FASE 1: RECONSTITUIÇÃO DA IDA (Aluguel ou Reserva Histórica)
            # ==================================================================
            # Regra de corte baseada no tipo do movimento penúltimo:
            if tipo_ida_legado == 7:
                tipo_movimento_ida_novo = 4  # Tipo 4 no refatorado = Reserva
                operation_type_ida = 'RESERVA_HISTORICA'
                detalhe_ida = "Histórico Migrado - Ida em Reserva"
            elif tipo_ida_legado in {1, 5}:
                tipo_movimento_ida_novo = 1  # Tipo 1 no refatorado = Aluguel
                operation_type_ida = 'ALUGUEL_HISTORICO'
                detalhe_ida = "Histórico Migrado - Ida em Aluguel"
            else:
                # Se for qualquer outro tipo estranho de movimento anterior, ignoramos o par por segurança
                continue

            # Invoca o motor Pai para registrar a Ida
            self.registrar_movimento(
                id_final=id_mov_ida,
                recipient_id=recipient_id,
                cliente_final=cliente_final,
                usuario_id=usuario_ida,
                mov_date=data_ida,
                deleted_at_mov=None,
                contrato_id=contrato_id,
                contrato_item_id=contrato_item_id,
                equipment_id_ref=equipment_id_ref,
                tipo_movimento_id=tipo_movimento_ida_novo,
                operation_type=operation_type_ida,
                nome_equipamento=None,
                alias_item=None,
                detalhes_item=detalhe_ida
            )

            # ==================================================================
            # FASE 2: CONSOLIDAÇÃO DA VOLTA (Devolução Real)
            # ==================================================================
            # Invoca o motor Pai para registrar a Volta (Tipo 3 = Devolução)
            self.registrar_movimento(
                id_final=id_mov_dev,
                recipient_id=recipient_id,
                cliente_final=cliente_final,
                usuario_id=usuario_dev,
                mov_date=data_dev,
                deleted_at_mov=None,
                contrato_id=contrato_id,
                contrato_item_id=contrato_item_id,
                equipment_id_ref=equipment_id_ref,
                tipo_movimento_id=3,
                operation_type='DEVOLUCAO',
                nome_equipamento=None,
                alias_item=None,
                detalhes_item="Migração Automática - Devolução Realizada"
            )

        # 3. Salva todo o bloco no banco novo (Status do equipamento 8 = Devolvido/Disponível em Estoque)
        self.salvar_banco(id_status_equipamento=8)