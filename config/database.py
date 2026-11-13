# modules/database.py
import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine


def obter_engines():
    # Ambos os bancos agora vivem no MESMO servidor local (Dump-refactor)
    HOST = os.getenv("DB_HOST", "localhost")
    PORT = os.getenv("DB_PORT", "3307")
    USER = os.getenv("DB_USER", "root")
    PASS = os.getenv("DB_PASS", "root")

    config_new = {
        "host": HOST, "port": PORT,
        "db": "controle-interno",      # banco novo refatorado (MySQL 8)
        "user": USER, "pass": PASS,
    }

    config_legado = {
        "host": HOST, "port": PORT,
        "db": "aluguel_legado",        # snapshot local do legado
        "user": USER, "pass": PASS,
    }

    url_new = (
        f"mysql+pymysql://{config_new['user']}:{config_new['pass']}"
        f"@{config_new['host']}:{config_new['port']}/{config_new['db']}"
        f"?charset=utf8mb4"
    )
    engine_new = create_engine(url_new, pool_pre_ping=True)

    url_legado = (
        f"mysql+pymysql://{config_legado['user']}:{config_legado['pass']}"
        f"@{config_legado['host']}:{config_legado['port']}/{config_legado['db']}"
        f"?charset=utf8mb4"
    )
    engine_legado = create_engine(url_legado, pool_pre_ping=True)

    return engine_new, engine_legado


def obter_engine_producao():
    """Cria conexão explícita com o refatorado de produção; não usa fallback local."""
    required = ("PROD_DB_HOST", "PROD_DB_PORT", "PROD_DB_USER", "PROD_DB_PASS")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Perfil de produção incompleto; configure: " + ", ".join(missing)
        )

    database = os.getenv("PROD_DB_NAME", "controle-interno")
    if database != "controle-interno":
        raise ValueError("PROD_DB_NAME deve ser 'controle-interno'; conexão com legado bloqueada.")

    user = quote_plus(os.environ["PROD_DB_USER"])
    password = quote_plus(os.environ["PROD_DB_PASS"])
    host = os.environ["PROD_DB_HOST"]
    port = os.environ["PROD_DB_PORT"]
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True)


def obter_engine_local_refatorado():
    """Conecta ao refatorado local usando perfil isolado, sem fallback."""
    required = ("LOCAL_DB_HOST", "LOCAL_DB_PORT", "LOCAL_DB_USER", "LOCAL_DB_PASS")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Perfil local-refatorado incompleto; configure: " + ", ".join(missing)
        )

    user = quote_plus(os.environ["LOCAL_DB_USER"])
    password = quote_plus(os.environ["LOCAL_DB_PASS"])
    host = os.environ["LOCAL_DB_HOST"]
    port = os.environ["LOCAL_DB_PORT"]
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/controle-interno?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True)


# Sanity check rápido: python -m modules.database
if __name__ == "__main__":
    from sqlalchemy import text

    eng_new, eng_legado = obter_engines()

    with eng_new.connect() as c:
        print("✅ NEW     :", c.execute(text("SELECT DATABASE(), VERSION()")).fetchone())

    with eng_legado.connect() as c:
        print("✅ LEGADO  :", c.execute(text("SELECT DATABASE(), VERSION()")).fetchone())
        tabelas = c.execute(text("SHOW TABLES")).fetchall()
        print(f"   tabelas no legado: {len(tabelas)}")
        # A query que estava quebrando no servidor remoto:
        n = c.execute(text("SELECT COUNT(*) FROM aluguel_setor")).scalar()
        print(f"   aluguel_setor legível: {n} linhas")