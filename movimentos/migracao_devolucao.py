from movimentos.migracao_aluguel import MigracaoAluguel
from movimentos.migracao_reserva import MigracaoReserva
from movimentos.migracao_movimentos import BaseMigracaoMovimento

class MigracaoDevolucao(BaseMigracaoMovimento):
    def __init__(self, engine_new, engine_legado, dados_compartilhados, start_counter=800000):
        super().__init__(engine_new, engine_legado, dados_compartilhados, start_counter)
        
        # Novas tabelas exclusivas da Devolução
        self.shipments = []
        self.shipment_movements = []
        self.shipment_items = []
        
        # Contadores de IDs
        self.shipment_id_counter = 1
        self.shipment_item_id_counter = 1

    def reconstruir_passado(self, tipo_movimento_anterior, dados_linha):
        """
        O Poder do Modo Fantasma: Invoca o Aluguel ou Reserva 
        sem limpar o banco e sem descontar saldo!
        """
        if tipo_movimento_anterior == 'ALUGUEL':
            fantasma = MigracaoAluguel(
                self.engine_new, self.engine_legado, self.dados, consumir_saldos=False
            )
            # Injeta os dados específicos daquele movimento passado e manda executar
            
        elif tipo_movimento_anterior == 'RESERVA':
            fantasma = MigracaoReserva(
                self.engine_new, self.engine_legado, self.dados
            )
            # Injeta os dados...

    def executar(self):
        # 1. Loop nas devoluções do legado (Frente única)
        # ...
        
        # 2. Roteia a reconstrução do passado
        # self.reconstruir_passado(...)
        
        # 3. Registra a Devolução atual
        # self.registrar_movimento(operation_type='DEVOLUCAO', ...)
        
        # 4. Alimenta as 3 tabelas de Shipments
        shipment_id = self.shipment_id_counter
        self.shipment_id_counter += 1
        
        self.shipments.append({
            "id": shipment_id,
            "status_id": 1,
            "created_by": usuario_id_do_movimento, # ID do usuário, não a data
            "updated_by": usuario_id_do_movimento,
            "created_at": data_do_movimento,       # A data entra aqui!
            "updated_at": data_do_movimento
        })
        
        self.shipment_movements.append({
            # id auto-increment no banco
            "shipment_id": shipment_id,
            "movement_id": id_do_movimento_devolucao 
        })
        
        self.shipment_items.append({
            "id": self.shipment_item_id_counter,
            "shipment_id": shipment_id,
            "status_id": 1,
            "movement_item_id": id_do_item_do_movimento, # ⚠️ Ponto de atenção aqui
            "volume_id": None,
            "details": f"Item de migração devolvido na data {data_do_movimento_formatada}",
            "address_id": 1378 # ID da Organização AS
        })
        self.shipment_item_id_counter += 1

        # 5. Salvar as 4 tabelas mestre + 3 tabelas de shipment no banco