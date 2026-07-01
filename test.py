def executar(self):
        print("\n" + "=" * 70)
        print("📦 MÓDULO: DEVOLUÇÃO (ESTOQUE E LOGÍSTICA)")
        print("=" * 70)

        # 1. Extração dos dados de devolução do legado (Frente Única)
        df_devolucoes = self._extrair_dados_devolucao()
        
        if df_devolucoes.empty:
            print("⚠️ Nenhum movimento de devolução encontrado.")
            return

        print(f"📖 Processando {len(df_devolucoes)} devoluções...")
        
        for _, row in tqdm(df_devolucoes.iterrows(), total=df_devolucoes.shape[0], desc="Processando DEVOLUÇÕES"):
            tombo = str(row['TOMBO']).strip()
            id_movimento_legado = int(row['MOVIMENTO_ID'])
            cliente_id_legado = int(row['ID_CLIENTE'])
            data_mov_legado = row['updated_at'] if pd.notna(row['updated_at']) else self.now

            # Cadastros de infraestrutura do cache em RAM
            equipment_id_ref = self.dados["dict_equip_ref_por_number"].get(tombo)
            recipient_id = self.dados["dict_cliente_adress"].get(cliente_id_legado)
            cliente_final_address = self.dados["dict_endereco_por_legacy_client"].get(cliente_id_legado)

            if not equipment_id_ref or not recipient_id:
                continue

            # Contratos de Fallback padrão para a Capa da OS
            contrato_id_res = self.dados["dict_primeiro_contrato_por_cliente"].get(recipient_id)
            item_id_res = self.dados["dict_primeiro_item_por_cliente"].get(recipient_id)
            usr_id = int(row['usuario_id']) if pd.notna(row['usuario_id']) and row['usuario_id'] != 0 else 1

            # ==================================================================
            # PASSO 1: CRIAÇÃO DO PROCESSO EXISTENTE (SERVICE_ORDER E MOVEMENTS)
            # ==================================================================
            id_mov, id_mov_item = self.registrar_movimento(
                id_final=id_movimento_legado,
                recipient_id=recipient_id,
                cliente_final_address_id=cliente_final_address,
                usuario_id=usr_id,
                mov_date=data_mov_legado,
                deleted_at_mov=row['deleted_at'] if pd.notna(row['deleted_at']) else None,
                contrato_id=contrato_id_res,
                contrato_item_id=item_id_res,
                equipment_id_ref=equipment_id_ref,
                tipo_movimento_id=2,  # 2 = ID de Devolução no banco novo
                operation_type='DEVOLUCAO',
                alias_item=None,
                alias_movimento=row['NOME_EQUIPAMENTO'],
                details_capa="Migração - Devolução",
                details_item="Item de Ordem de Devolução"
            )

            # ==================================================================
            # PASSO 2: NASCIMENTO DOS SHIPMENTS (REAPROVEITANDO OS IDS ACIMA)
            # ==================================================================
            shipment_id_atual = self.shipment_id_counter
            self.shipment_id_counter += 1

            # A) Tabela: shipments
            self.shipments.append({
                "id": shipment_id_atual,
                "status_id": 1,
                "created_at": data_mov_legado,
                "updated_at": data_mov_legado
            })

            # B) Tabela: shipment_movements (A amarra da capa)
            self.shipment_movements.append({
                "shipment_id": shipment_id_atual,
                "movement_id": id_mov # Reaproveitando o ID retornado pelo Pai
            })

            # C) Tabela: shipment_items (A amarra do item físico)
            self.shipment_items.append({
                "id": self.shipment_item_id_counter,
                "shipment_id": shipment_id_atual,
                "status_id": 1,
                "movement_item_id": id_mov_item, # Reaproveitando o ID do item retornado pelo Pai
                "volume_id": None,
                "details": f"Item de migração devolvido na data {data_mov_legado}",
                "address_id": 1378  # ID da Organização AS Sistemas
            })
            self.shipment_item_id_counter += 1

        # ==================================================================
        # PASSO 3: PERSISTÊNCIA EM LOTE NO BANCO (Mestre + Shipments)
        # ==================================================================
        # Salva as 4 tabelas base e atualiza o equipamento para DISPONÍVEL (1)
        self.salvar_banco(id_status_equipamento=1) 
        
        # Salva as 3 tabelas de logística exclusivas da devolução
        self._salvar_tabelas_logistica()