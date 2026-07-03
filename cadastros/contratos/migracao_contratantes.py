import sqlalchemy
from sqlalchemy import create_engine, text
from tqdm import tqdm
import sys


TABELAS = [
    'contract_recipient_customers'
]

class MigracaoContratantes:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        
        self.stats = {
            "contratos": 0, 
            "vinculos_contratantes": 0, 
            "vinculos_filhos": 0
        }

    def _vincular_destinatarios(self, conn):
            print("📑 Lendo contratos ativos...")
            
            # Filtramos direto na query para evitar trazer contratos sem cliente
            contratos = conn.execute(
                text("SELECT id, customer_id FROM contracts WHERE customer_id IS NOT NULL")
            ).fetchall()

            for con in tqdm(contratos, desc="Sincronizando Destinatários"):
                contract_id = con.id
                contratante_id = con.customer_id

                # 1. Limpar vínculos antigos (Garante Idempotência caso rode com --no-truncate)
                conn.execute(
                    text("DELETE FROM contract_recipient_customers WHERE contract_id = :cid"),
                    {"cid": contract_id},
                )

                # 2. PASSO A: Vincular o próprio Contratante (Prefeitura)
                conn.execute(
                    text("""
                        INSERT INTO contract_recipient_customers (contract_id, customer_id)
                        VALUES (:contract_id, :customer_id)
                    """),
                    {"contract_id": contract_id, "customer_id": contratante_id},
                )
                self.stats["vinculos_contratantes"] += 1

                # 3. PASSO B: Vincular o ÚLTIMO NÍVEL de Clientes (Secretarias / Filhos)
                res_filhos = conn.execute(
                    text("""
                        INSERT INTO contract_recipient_customers (contract_id, customer_id)
                        SELECT :contract_id, id
                        FROM customers
                        WHERE parent_id = :customer_id
                    """),
                    {"contract_id": contract_id, "customer_id": contratante_id},
                )
                
                self.stats["vinculos_filhos"] += res_filhos.rowcount
                self.stats["contratos"] += 1

    # ==============================================================================
    # ORQUESTRADOR PRINCIPAL DA CLASSE
    # ==============================================================================
    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 MODO DE VÍNCULO: CONTRATANTES E DESTINATÁRIOS")
        print("=" * 80)
        
        try:
            # Transação única: Se houver erro, nada é salvo no banco (Rollback automático)
            with self.engine_new.begin() as conn:
                self._vincular_destinatarios(conn)

            print("\n" + "=" * 50)
            print("✅ SINCRONIZAÇÃO CONCLUÍDA")
            print("=" * 50)
            print(f"📊 Contratos processados:        {self.stats['contratos']}")
            print(f"🔗 Contratantes vinculados:      {self.stats['vinculos_contratantes']}")
            print(f"🔗 Secretarias/Filhos vinculados: {self.stats['vinculos_filhos']}")
            print(f"📈 Total de vínculos gerados:    {self.stats['vinculos_contratantes'] + self.stats['vinculos_filhos']}")
            print("=" * 50)

        except Exception as e:
            print(f"\n❌ ERRO CRÍTICO: {e}")
            print("Rollback automático acionado.")
            import traceback
            traceback.print_exc()
            sys.exit(1)

# ==============================================================================
# WRAPPER (Ponte para o main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    migrador = MigracaoContratantes(eng_novo, eng_legado)
    migrador.executar()