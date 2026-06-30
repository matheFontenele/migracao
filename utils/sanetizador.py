# database_utils.py
import re
import unicodedata
import pandas as pd
from sqlalchemy import text

def limpar_cnpj(cnpj_raw):
        c = re.sub(r'\D', '', str(cnpj_raw))
        return c if c else '00000000000000'

def normalizar_para_match(nome: str) -> str:
    if not nome or str(nome).lower() == 'nan': return ""
    s = unicodedata.normalize('NFD', str(nome))
    s = s.encode('ascii', 'ignore').decode('utf8').upper()
    s = re.sub(r'[\-\(\s]*\b(RESERVA|RESERVADO)\b[\)\s]*', '', s)
    s = re.sub(r'[^\w\s]', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def executar_truncate_tabelas(engine, lista_tabelas: list):
    """Executa faxina estrutural desativando chaves estrangeiras temporariamente."""
    if not lista_tabelas:
        return
    print(f"🧹 Iniciando a limpeza de {len(lista_tabelas)} tabelas no banco novo...")
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for tabela in lista_tabelas:
            conn.execute(text(f"TRUNCATE TABLE `{tabela}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    print("✅ Tabelas limpas com sucesso.")

def limpar_valor_inteiro(valor):
    if pd.isna(valor) or str(valor).strip() in ('', '-', '–', '—'):
        return 0
    try:
        return int(float(valor))
    except (ValueError, TypeError):
        return 0

def limpar_valor_numerico(valor):
    if pd.isna(valor) or valor == '': 
        return 0.0
    if isinstance(valor, (int, float)): 
        return float(valor)
    texto = str(valor).replace('R$', '').replace('.', '').replace(',', '.').replace(' ', '').strip()
    try: 
        return float(texto)
    except: 
        return 0.0

def ultra_normalizar(texto):
    if pd.isna(texto): return ""
    texto = str(texto).upper()
    substituicoes = {'º': ' ', '°': ' ', 'ª': ' ', '§': ' ', '(': ' ', ')': ' ', '/': ' ', '-': ' ', '.': ' ', ',': ' '}
    for char, rep in substituicoes.items():
        texto = texto.replace(char, rep)
    texto = re.sub(r'\bPREF\b', 'PREFEITURA', texto)
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    texto = re.sub(r'[^A-Z0-9 ]', '', texto)
    return ' '.join(texto.split())