import sqlalchemy
from sqlalchemy import create_engine, text
from tqdm import tqdm
import sys

from cadastros.contratos.planilhas_contrato_2026 import (
    DESTINATARIOS_ESPECIFICOS_POR_NUMERO,
)
from utils.sanetizador import ultra_normalizar


TABELAS = [
    'contract_recipient_customers'
]

# Destinatários adicionais autorizados fora da relação contratante -> filhos
# diretos. A regra antiga do contrato 2025/2208.09 apontava para o customer
# 2462 (secretaria de Aquiraz). Depois da unificação das prefeituras 27 e 57
# essa secretaria deixa de existir como customer (os clientes viram endereços
# da raiz), então o vínculo não é mais aplicável. O dicionário fica vazio para
# manter o mecanismo para futuras exceções explícitas.
VINCULOS_DESTINATARIOS_ADICIONAIS = {}

class MigracaoContratantes:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        
        self.stats = {
            "contratos": 0, 
            "vinculos_contratantes": 0, 
            "vinculos_filhos": 0,
            "vinculos_adicionais": 0,
            "contratos_preservados": 0,
        }

    def _vincular_destinatarios(self, conn):
            print("📑 Lendo contratos ativos...")
            
            # Filtramos direto na query para evitar trazer contratos sem cliente
            contratos = conn.execute(
                text("""
                    SELECT id, customer_id, number, organization_id
                    FROM contracts
                    WHERE customer_id IS NOT NULL
                """)
            ).fetchall()

            # Os destinatários vêm do JSON de contrato. Só derivamos da
            # hierarquia os contratos que chegaram sem nenhum vínculo — é o
            # fallback para bases antigas, não a regra.
            com_vinculo = {
                linha[0] for linha in conn.execute(
                    text("SELECT DISTINCT contract_id FROM contract_recipient_customers")
                ).fetchall()
            }

            for con in tqdm(contratos, desc="Sincronizando Destinatários"):
                contract_id = con.id
                contratante_id = con.customer_id

                if contract_id in com_vinculo:
                    self.stats["contratos_preservados"] += 1
                    continue

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

                # 2b. Contratos das planilhas 2026 de Tauá: o destinatário é
                # apenas a secretaria indicada no arquivo. Nunca vincular todas
                # as secretarias filhas da prefeitura nesses casos.
                alias_especifico = DESTINATARIOS_ESPECIFICOS_POR_NUMERO.get(
                    (con.number or '').strip()
                )
                if alias_especifico:
                    filhos = conn.execute(
                        text("""
                            SELECT id, alias
                            FROM customers
                            WHERE parent_id = :customer_id
                              AND organization_id = :org
                              AND deleted_at IS NULL
                        """),
                        {
                            "customer_id": contratante_id,
                            "org": con.organization_id,
                        },
                    ).fetchall()
                    chave = ultra_normalizar(alias_especifico)
                    encontrados = [
                        f for f in filhos
                        if ultra_normalizar(f.alias) == chave
                    ]
                    if len(encontrados) != 1:
                        raise RuntimeError(
                            f"Contrato {con.number!r}: esperava exatamente um "
                            f"destinatário {alias_especifico!r} filho do "
                            f"contratante {contratante_id}; encontrados: "
                            f"{[f.id for f in encontrados]}"
                        )
                    conn.execute(
                        text("""
                            INSERT INTO contract_recipient_customers
                                (contract_id, customer_id)
                            VALUES (:contract_id, :customer_id)
                        """),
                        {
                            "contract_id": contract_id,
                            "customer_id": encontrados[0].id,
                        },
                    )
                    self.stats["vinculos_adicionais"] += 1
                    self.stats["contratos"] += 1
                    continue

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

                # 4. PASSO C: Destinatários adicionais explicitamente aprovados.
                # INSERT IGNORE mantém a operação idempotente caso o destinatário
                # também esteja entre o contratante ou seus filhos diretos.
                destinatarios_adicionais = VINCULOS_DESTINATARIOS_ADICIONAIS.get(
                    contract_id, ()
                )
                for destinatario_id in destinatarios_adicionais:
                    res_adicional = conn.execute(
                        text("""
                            INSERT IGNORE INTO contract_recipient_customers
                                (contract_id, customer_id)
                            SELECT :contract_id, id
                            FROM customers
                            WHERE id = :customer_id
                        """),
                        {
                            "contract_id": contract_id,
                            "customer_id": destinatario_id,
                        },
                    )
                    self.stats["vinculos_adicionais"] += res_adicional.rowcount

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
            print(f"🔒 Vínculos vindos do JSON:      {self.stats['contratos_preservados']}")
            print(f"🔗 Contratantes vinculados:      {self.stats['vinculos_contratantes']}")
            print(f"🔗 Secretarias/Filhos vinculados: {self.stats['vinculos_filhos']}")
            print(f"🔗 Vínculos adicionais:          {self.stats['vinculos_adicionais']}")
            print(
                "📈 Total de vínculos gerados:    "
                f"{self.stats['vinculos_contratantes'] + self.stats['vinculos_filhos'] + self.stats['vinculos_adicionais']}"
            )
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
