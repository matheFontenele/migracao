# modules/database.py
import os
from sqlalchemy import create_engine

def obter_engines():
    
    HOST_NOVO = os.getenv("DB_HOST_NEW", "localhost")

    config_new = {
        "host": HOST_NOVO, "port": "3307", "db": "controle-interno",
        "user": "root", "pass": "root"
    }

    config_legado = {
        "host": HOST_NOVO, "port": "3307", "db": "aluguel_legado",
        "user": "root", "pass": "root"
    }

    # pool_pre_ping=True testa se o MySQL não "dormiu" antes de disparar a query
    url_new = f"mysql+pymysql://{config_new['user']}:{config_new['pass']}@{config_new['host']}:{config_new['port']}/{config_new['db']}"
    engine_new = create_engine(url_new, pool_pre_ping=True)

    url_legado = f"mysql+pymysql://{config_legado['user']}:{config_legado['pass']}@{config_legado['host']}:{config_legado['port']}/{config_legado['db']}"
    engine_legado = create_engine(url_legado, pool_pre_ping=True)

    return engine_new, engine_legado