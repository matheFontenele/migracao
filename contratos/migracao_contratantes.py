import sqlalchemy
from sqlalchemy import create_engine, text
from tqdm import tqdm

# CONFIGURAÇÕES DE CONEXÃO
DB_CONFIG = {
    "host": "localhost",
    "port": "3307",
    "db": "controle-interno",
    "user": "root",
    "pass": "root",
}

conn_string = (
    f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['pass']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['db']}"
)
engine = create_engine(conn_string)


def migrar_destinatarios_completo():
    print("🚀 Iniciando vínculo de destinatários (Contratante + Último Nível de Clientes)...")

    try:
        with engine.begin() as connection:
            # 1. Buscar contratos
            print("📑 Lendo contratos...")
            contratos = connection.execute(
                text("SELECT id, customer_id FROM contracts")
            ).fetchall()

            stats = {"contratos": 0, "vinculos_contratantes": 0, "vinculos_filhos": 0}

            # 2. Processar contrato a contrato
            for con in tqdm(contratos, desc="Sincronizando"):
                contract_id = con.id
                contratante_id = con.customer_id

                if not contratante_id:
                    continue

                # 3. Limpar vínculos antigos para evitar duplicidade
                connection.execute(
                    text("DELETE FROM contract_recipient_customers WHERE contract_id = :cid"),
                    {"cid": contract_id},
                )

                # 4. PASSO A: Vincular o próprio Contratante (Prefeitura)
                connection.execute(
                    text("""
                        INSERT INTO contract_recipient_customers (contract_id, customer_id)
                        VALUES (:contract_id, :customer_id)
                    """),
                    {"contract_id": contract_id, "customer_id": contratante_id},
                )
                stats["vinculos_contratantes"] += 1

                # 5. PASSO B: Vincular o ÚLTIMO NÍVEL de Clientes (Secretarias / Filhos)
                # Pega todos os customers cujo pai é o contratante atual
                res_filhos = connection.execute(
                    text("""
                        INSERT INTO contract_recipient_customers (contract_id, customer_id)
                        SELECT :contract_id, id
                        FROM customers
                        WHERE parent_id = :customer_id
                    """),
                    {"contract_id": contract_id, "customer_id": contratante_id},
                )
                
                stats["vinculos_filhos"] += res_filhos.rowcount
                stats["contratos"] += 1

        print("\n" + "=" * 50)
        print("✅ SINCRONIZAÇÃO CONCLUÍDA")
        print("=" * 50)
        print(f"📊 Contratos processados:        {stats['contratos']}")
        print(f"🔗 Contratantes vinculados:      {stats['vinculos_contratantes']}")
        print(f"🔗 Secretarias/Filhos vinculados: {stats['vinculos_filhos']}")
        print(f"📈 Total de vínculos gerados:    {stats['vinculos_contratantes'] + stats['vinculos_filhos']}")
        print("=" * 50)

    except Exception as e:
        print(f"\n❌ ERRO CRÍTICO: {e}")
        print("Rollback automático acionado.")


if __name__ == "__main__":
    migrar_destinatarios_completo()