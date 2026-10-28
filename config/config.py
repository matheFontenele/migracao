# modules/config.py

# =========================================================================
# DE-PARA DE ORGANIZAÇÕES (DOMÍNIO DO NEGÓCIO)
# =========================================================================
MAPPING_ALUCOM = {1115, 1327, 1329, 1363, 1365, 1366, 1367, 1370,1353, 1373, 1377}
MAPPING_IP = {1311, 1346, 1349, 1350, 1364, 1368, 1371}
MAPPING_MOREIA = {1122, 1326, 1328, 1358, 1369}
MAPPING_AS = {1378}
MAPPING_SC = {1379}

# =========================================================================
# REGRAS DE EXCEÇÃO E FILTROS
# =========================================================================
ORGANIZACOES_BLOQUEADAS = {1123, 1366}
CLIENTES_BLOQUEADOS = {2131, 2707, 10672, 10651}
FALSOS_RESERVAS = {10487}

# Clientes internos usados no legado como estoque/base da própria ALUCOM.
# Eles não existirão no banco refatorado e os equipamentos alugados para eles
# devem voltar como DISPONÍVEL PARA LOCAÇÃO (status_id = 1).
# Ajuste esta lista sempre que um novo cliente-estoque for identificado.
CLIENTES_ESTOQUE = {
    3192,   # ALUCOM SÃO PAULO - 2018
    3622,   # ALUCOM LOCACOES
    3967,   # ALUCOM LOCACOES
    10428,  # ALUCOM LTDA
    10431,  # ALUCOM LTDA
    10432,  # ALUCOM LTDA
    10468,  # ALUCOM ESTOQUE
    10475,  # ALUCOM SAO LUIS
    10484,  # SAO LUIS ALUCOM LOCACOES
    10662,  # ALUCOM - BASE SANTOS DUMONT
    10663,  # ALUCOM - BASE SANTOS DUMONT
    10672,  # BASE ALUCOM - SÃO LUIS (já bloqueado)
    10847,  # ALUCOM - BASE ARACATI
    10856,  # ALUCOM EDUCATION
    10857,  # ALUCOM DISTRIBUIÇÃO
    11001,  # DEPOSITO FLORIANOPOLIS - ALUCOM
    11070,  # ALUCOM - BASE JOÃO PESSOA
    11460,  # BASE ALUCOM - FLORIANOPOLIS
    11651,  # BASE ALUCOM - SÃO LUIS
}

# Setores internos que NÃO devem virar customer (secretaria). Seus clientes
# são anexados diretamente ao departamento pai.
# 1187 = ALUCOM SEDE RIACHUELO (departamento 283 = ADI)
# 1715 = Secretaria da Prefeitura 439 (cliente 12196 vai direto para a prefeitura)
SETORES_IGNORADOS = {1187, 1715}

# =========================================================================
# EXCEÇÃO DE TIPOS PARA INSUMOS (MIGRAÇÃO DE EQUIPAMENTOS)
# =========================================================================
EQUIPAMENTOS_TIPOS = {'MULTIFUNCIONAL', 'IMPRESSORA', 'SCANNER', 'MONITOR', 'NOBREAK', 'ESTABILIZADOR', 'SWITCH', 'FONE DE OUVIDO', 'CAIXA DE SOM', 'NOTEBOOK', 'IMPRESSORAS REVISADAS', 'IMPRESSORA'}


# =========================================================================
# ENDEREÇOS BASES (ORGANIZAÇÕES)
# =========================================================================
ENDERECOS_BASES = [
            {"type": "organization", "id": 1115, "alias": "ALUCOM - BASE", "zip": "60175205", "street": "RUA RIACHUELO PAPICU", "num": "40", "city": "FORTALEZA", "state": "CE", "leg_id": None, "res_id": None},
            {"type": "organization", "id": 1122, "alias": "MOREIA - BASE", "zip": "60175205", "street": "RUA RIACHUELO PAPICU", "num": "50", "city": "FORTALEZA", "state": "CE", "leg_id": None, "res_id": None},
            {"type": "organization", "id": 1311, "alias": "IP - BASE", "zip": "60175205", "street": "RUA RIACHUELO PAPICU", "num": "60", "city": "FORTALEZA", "state": "CE", "leg_id": None, "res_id": None},
            {"type": "organization", "id": 1378, "alias": "AS SISTEMAS - BASE", "zip": "60175205", "street": "RUA RIACHUELO PAPICU", "num": "70", "city": "FORTALEZA", "state": "CE", "leg_id": None, "res_id": None}
        ]

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
    },
    "Estoque Caucaia": {
        "alias": "Box Caucaia",
        "zip": "13579-246",
        "street": "Rua Exemplo 3",
        "number": "654",
        "city": "Caucaia",
        "state": "CE",
        "country": "Brasil"
    },
    "Estoque Aracati": {
        "alias": "Box Aracati",
        "zip": "24680-135",
        "street": "Avenida Exemplo 3",
        "number": "987",
        "city": "Aracati",
        "state": "CE",
        "country": "Brasil"
    }
}

# Reservas legadas sem cliente titular correspondente: criaremos o endereço de
# reserva no pai hierárquico migrado, usando os dados da base indicada. A
# organização efetiva da reserva continua sendo resolvida pelo contrato no
# módulo de movimentos.
RESERVAS_BASE_AVULSA = {
    10835: "Estoque Paraíba",
    11405: "Estoque Paraíba",
    10722: "Estoque Paraíba",
    3259: "Estoque Aracati",
    4468: "Box São Luis",
    10859: "Estoque Paraíba",
    4089: "Estoque Caucaia",
}

# DE/PARA das reservas avulsas para os clientes titulares que receberão os
# equipamentos. O endereço do cliente usará a base configurada acima.
DEPARA_RESERVAS_BASE = {
    10835: "POLÍCIA CIVIL - GUARABIRA",
    11405: "POLÍCIA CIVIL - CAMPINA GRANDE",
    10722: "POLÍCIA CIVIL - JOÃO PESSOA",
    3259: "PREFEITURA MUNICIPAL DE ARACATI",
    4468: "PM SÃO LUÍS",
    10859: "SECRETARIA DE ESTADO DA SAÚDE DA PARAÍBA - HEMOCENTRO",
    4089: "P M DE CAUCAIA - RESERVA",
}

