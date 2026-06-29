import pandas as pd

from config.config import MAPPING_ALUCOM, MAPPING_IP, MAPPING_MOREIA, MAPPING_AS

def descobrir_id_organizacao(id_legado, default=1115):
    """Lógica centralizada de mapeamento de IDs de organização."""
    if pd.isna(id_legado): 
        return default
    
    id_int = int(id_legado)
    
    # Dicionário de mapeamento invertido para busca rápida
    # (Ou você pode manter os IFs se a lista for pequena)
    if id_int in MAPPING_ALUCOM: return 1115
    if id_int in MAPPING_IP:     return 1311
    if id_int in MAPPING_MOREIA: return 1122
    if id_int in MAPPING_AS:     return 1378
    
    return id_int