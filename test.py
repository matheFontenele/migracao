def _processar_contratos(self, conn, df_ex_contract):
        print("\n🔄 Processando Contratos (UPSERT)...")
        for idx, row in df_ex_contract.iterrows():
            if pd.isna(row['CONTRATANTE']) or pd.isna(row['APELIDO_CONTRATO']): 
                self.stats['contratos_ignorados'] += 1
                continue
            
            # ==============================================================================
            # 1️⃣ CAPTURA DO ID DA PLANILHA
            # ==============================================================================
            id_contrato_excel = limpar_valor_inteiro(row.get('ID_CONTRATO'))
            
            # Se a linha não tiver um ID válido, nós pulamos para não quebrar o banco
            if id_contrato_excel == 0:
                print(f"   ⚠️ Contrato ignorado: A linha não possui um 'ID_CONTRATO' válido.")
                self.stats['contratos_ignorados'] += 1
                continue
            # ==============================================================================

            cust_id = self._get_hierarchical_customer(row['CONTRATANTE'])
            if not cust_id:
                self.stats['contratos_ignorados'] += 1
                continue

            nome_contrato = str(row['APELIDO_CONTRATO']).strip().upper()
            numero_contrato = str(row['NUMERO_CONTRATO']).strip() if pd.notna(row['NUMERO_CONTRATO']) else "SEM_NUMERO"
            org_id = MAP_ORGANIZACAO.get(row['CONTRATADO'], 1115)
            chave_contrato = f"{nome_contrato}|{numero_contrato}|{org_id}"

            contract_info = self.contracts_cache.get(chave_contrato)
            if not contract_info and numero_contrato != "SEM_NUMERO":
                contract_info = self.contracts_by_number.get(numero_contrato)

            dados_contrato = {
                'id': id_contrato_excel, # 👈 O ID forçado entra aqui no dicionário de dados
                'name': nome_contrato, 
                'number': numero_contrato,
                'contract_type_id': MAP_TIPO.get(ultra_normalizar(row['TIPO_CONTRATO']), 1),
                'contract_status_id': MAP_STATUS.get(ultra_normalizar(row['STATUS_CONTRATO']), 2),
                'organization_id': org_id, 
                'customer_id': int(cust_id),
                'object': str(row['OBJETO_DO_CONTRATO'])[:500] if not pd.isna(row['OBJETO_DO_CONTRATO']) else "NÃO INFORMADO",
                'updated_at': self.now
            }

            if contract_info:
                contract_id = contract_info['id']
                conn.execute(text("""
                    UPDATE contracts 
                    SET name = :name, contract_type_id = :contract_type_id, contract_status_id = :contract_status_id,
                        customer_id = :customer_id, object = :object, updated_at = :updated_at
                    WHERE id = :id AND number = :number
                """), {**dados_contrato, 'id': contract_id, 'number': numero_contrato})
                self.stats['contratos_atualizados'] += 1
            else:
                dados_contrato['created_at'] = self.now
                
                # ==============================================================================
                # 2️⃣ INJEÇÃO DO ID NO INSERT
                # Adicionamos a coluna 'id' no comando SQL para forçar a criação com o número da planilha
                # ==============================================================================
                conn.execute(text("""
                    INSERT INTO contracts (id, name, number, contract_type_id, contract_status_id, organization_id, customer_id, object, created_at, updated_at)
                    VALUES (:id, :name, :number, :contract_type_id, :contract_status_id, :organization_id, :customer_id, :object, :created_at, :updated_at)
                """), dados_contrato)
                
                # 3️⃣ A variável que armazena o id criado não usa mais `res.lastrowid`, usa o valor do Excel
                contract_id = id_contrato_excel 
                
                novo_cache = {'id': contract_id, 'customer_id': cust_id}
                self.contracts_cache[chave_contrato] = novo_cache
                if numero_contrato != "SEM_NUMERO":
                    self.contracts_by_number[numero_contrato] = novo_cache
                self.stats['contratos_criados'] += 1

            conn.execute(text("INSERT IGNORE INTO contract_recipient_customers (contract_id, customer_id) VALUES (:c_id, :cust_id)"), 
                         {'c_id': int(contract_id), 'cust_id': int(cust_id)})

            # Esse mapa agora guarda exatamente o ID que veio da planilha para repassar aos eventos e aditivos!
            self.contract_id_map[nome_contrato] = contract_id