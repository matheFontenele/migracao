# E. BULK INSERT EQUIPAMENTOS
            if lista_equipamentos_global:
                conn.execute(text("""
                    INSERT INTO equipments (id, product_item_id, transaction_item_id, number, name, serial_number, serial_required, current_organization_id, status_id, address_id, location_id, is_completed, created_at, updated_at, deleted_at) 
                    VALUES (:id, :product_item_id, :transaction_item_id, :number, :name, :serial_number, :serial_required, :current_organization_id, :status_id, :address_id, :location_id, :is_completed, :created_at, :updated_at, :deleted_at)
                """), lista_equipamentos_global)
                self.stats["equipments"] += len(lista_equipamentos_global)

                # ==============================================================================
                # F. BULK INSERT: HISTÓRICO DE ENTRADA DO EQUIPAMENTO (LOG)
                # ==============================================================================
                lista_historico = []
                for eq in lista_equipamentos_global:
                    lista_historico.append({
                        "equipment_id": eq["id"],
                        "status_id": 1,
                        "occurred_at": eq["created_at"], # Data de criação do equipamento
                        "movement_item_id": None,
                        "service_order_item_id": None,
                        "contract_item_id": None,
                        "shipment_item_id": None,
                        "is_conversion": None,
                        "reason": "TRANSACTION_ENTRANCE_EQUIPMENT",
                        "user_id": 1 # 🎯 Assumindo usuário do sistema/admin como 1
                    })

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
                """), lista_historico)
                
                self.stats["histories"] += len(lista_historico)