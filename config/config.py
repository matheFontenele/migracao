# modules/config.py

# =========================================================================
# DE-PARA DE ORGANIZAÇÕES (DOMÍNIO DO NEGÓCIO)
# =========================================================================
MAPPING_ALUCOM = {1115, 1327, 1329, 1363, 1365, 1366, 1367, 1370,1353, 1373,1376, 1377}
MAPPING_IP = {1311, 1346, 1349, 1350, 1364, 1368, 1371}
MAPPING_MOREIA = {1122, 1326, 1328, 1358, 1369}
MAPPING_AS = {1378}
MAPPING_SC = {1379}

# =========================================================================
# REGRAS DE EXCEÇÃO E FILTROS
# =========================================================================
ORGANIZACOES_BLOQUEADAS = {1123, 1366}
CLIENTES_BLOQUEADOS = {2131, 2707}
FALSOS_RESERVAS = {10487}


# =========================================================================
# ENDEREÇOS BASES MESCLADAS
# =========================================================================
BASES_AVULSOS = {
    "Box São Luis": {
        "alias": "Box São Luis",
        "zip": "12345-678",
        "street": "Rua Exemplo",
        "number": "123",
        "city": "São Luis",
        "state": "MA",
        "country": "Brasil"
    },
    "Estoque Santa Catarina": {
        "alias": "Box Santa Catarina",
        "zip": "98765-432",
        "street": "Avenida Exemplo",
        "number": "456",
        "city": "Florianópolis",
        "state": "SC",
        "country": "Brasil"
    },
    "Estoque Paraíba": {
        "alias": "Box Paraíba",
        "zip": "54321-987",
        "street": "Rua Exemplo 2",
        "number": "789",
        "city": "João Pessoa",
        "state": "PB",
        "country": "Brasil"
    },
    "Box Brasilia": {
        "alias": "Box Brasilia",
        "zip": "67890-123",
        "street": "Avenida Exemplo 2",
        "number": "321",
        "city": "Distrito Federal",
        "state": "DF",
        "country": "Brasil"
    }
}



