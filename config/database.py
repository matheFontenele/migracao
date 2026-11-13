# modules/database.py
import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine


def _isolated_profile(prefix):
    required = (f"{prefix}_DB_HOST", f"{prefix}_DB_PORT", f"{prefix}_DB_USER", f"{prefix}_DB_PASS")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("Perfil incompleto; configure: " + ", ".join(missing))
    user = quote_plus(os.environ[f"{prefix}_DB_USER"])
    password = quote_plus(os.environ[f"{prefix}_DB_PASS"])
    host = os.environ[f"{prefix}_DB_HOST"]
    port = os.environ[f"{prefix}_DB_PORT"]
    database = os.getenv(f"{prefix}_DB_NAME", "controle-interno")
    if database != "controle-interno":
        raise ValueError(f"{prefix}_DB_NAME deve ser controle-interno")
    return create_engine(
        f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4",
        pool_pre_ping=True,
    )


def obter_engine_producao():
    """Cria engine de PROD somente com credenciais PROD explícitas."""
    return _isolated_profile("PROD")


def obter_engine_local_refatorado():
    """Cria engine LOCAL com credenciais LOCAL explícitas."""
    return _isolated_profile("LOCAL")


def obter_engines():
    # Ambos os bancos agora vivem no MESMO servidor local (Dump-refactor)
    HOST = os.getenv("DB_HOST", "localhost")
    PORT = os.getenv("DB_PORT", "3307")
    USER = os.getenv("DB_USER", "root")
    PASS = os.getenv("DB_PASS", "root")

    config_new = {
        "host": HOST, "port": PORT,
        "db": os.getenv("DB_NAME_NEW", "controle-interno"),      # banco novo refatorado (MySQL 8)
        "user": USER, "pass": PASS,
    }

    config_legado = {
        "host": HOST, "port": PORT,
        "db": os.getenv("DB_NAME_LEGADO", "aluguel_legado"),        # snapshot local do legado
        "user": USER, "pass": PASS,
    }

    url_new = (
        f"mysql+pymysql://{quote_plus(config_new['user'])}:{quote_plus(config_new['pass'])}"
        f"@{config_new['host']}:{config_new['port']}/{config_new['db']}"
        f"?charset=utf8mb4"
    )
    engine_new = create_engine(url_new, pool_pre_ping=True)

    url_legado = (
        f"mysql+pymysql://{quote_plus(config_legado['user'])}:{quote_plus(config_legado['pass'])}"
        f"@{config_legado['host']}:{config_legado['port']}/{config_legado['db']}"
        f"?charset=utf8mb4"
    )
    engine_legado = create_engine(url_legado, pool_pre_ping=True)

    return engine_new, engine_legado


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
