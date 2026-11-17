# modules/config.py

# =========================================================================
# DE-PARA DE ORGANIZAÇÕES (DOMÍNIO DO NEGÓCIO)
# =========================================================================
MAPPING_ALUCOM = {1115, 1327, 1329, 1363, 1365, 1366, 1367, 1370, 1353, 1373, 1377}
MAPPING_IP = {1311, 1346, 1349, 1350, 1364, 1368, 1371}
MAPPING_MOREIA = {1122, 1326, 1328, 1358, 1369}
MAPPING_AS = {1378}
MAPPING_SC = {1379}

# =========================================================================
# REGRAS DE EXCEÇÃO E FILTROS
# =========================================================================
ORGANIZACOES_BLOQUEADAS = {1123, 1351, 1366}  # 1351 = TONER ADI (não migrar seus clientes)
CLIENTES_BLOQUEADOS = {2131, 2707, 10672, 10651}
# Clientes cujo nome contém "RESERVA" mas são centros de preparação de
# oficiais da reserva (não são cópias de titular/reserva).
FALSOS_RESERVAS = {10482, 10487}

# Clientes internos usados no legado como estoque/base da própria ALUCOM.
# Eles não existirão no banco refatorado e os equipamentos alugados para eles
# devem voltar como DISPONÍVEL PARA LOCAÇÃO (status_id = 1).
# Ajuste esta lista sempre que um novo cliente-estoque for identificado.
CLIENTES_ESTOQUE = {
    255,    # ALUCOM - JOÃO PESSOA
    1023,   # IGOR GABRIEL SOUSA DA MAIA - TECNICO BRASILIA
    1069,   # IP LOCACOES
    1071,   # IP LOCACOES
    1318,   # MOREIA LOCACOES
    1321,   # MOREIA TECNOLOGIA - MANUTENÇÃO DE EQUIPAMENTOS
    1323,   # MOREIA TECNOLOGIA LOCACAO
    1324,   # MOREIA TECNOLOGIA MANUTENCAO
    3192,   # ALUCOM SÃO PAULO - 2018
    4029,   # MOREIA TECNOLOGIA - LOCACOES
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

# Clientes ativos no legado cuja hierarquia (setor/departamento) foi excluída.
# A migração de clientes descarta o setor/departamento deletado, mas o cliente
# continua válido e deve resolver para o cliente canônico ativo da mesma
# unidade organizacional. Sem este de/para, movimentos e contratos do cliente
# antigo ficam sem destino (CLIENTE_SEM_MAPA) — ex.: 35917 e 36914.
# Formato: id_legado_copia -> id_legado_principal.
DEPARA_CLIENTES_HIERARQUIA_DELETADA = {
    3607: 1215,  # P M DE CASCAVEL / SECRETARIA DE SAÚDE -> CASCAVEL - SECRETARIA DE SAÚDE
    3664: 3518,  # P M DE CASCAVEL - SAUDE/POLICLINICA -> P.M DE CASCAVEL - SECRETARIA DE SAÚDE - POLICLINICA
}

# Clientes cujo cadastro ficou SEM ENDEREÇO no refatorado porque o setor/
# departamento de origem não foi migrado (hierarquia excluída, limpeza de
# duplicados ou entrada repetida no legado). São o mesmo órgão de outro id
# legado que possui endereço migrado. Sem o de/para o movimento fica
# CLIENTE_SEM_MAPA e o equipamento não recebe histórico.
#
# Só entram pares verificados: a cópia não tem endereço próprio e o principal
# tem endereço ativo apontando para o customer da mesma secretaria. Entradas
# ambíguas (base/estoque, demonstração, "a localizar", sem endereço de
# destino) ficam de fora e continuam reportadas como exceção.
# Formato: id_legado_copia -> id_legado_principal.
DEPARA_CLIENTES_SEM_ENDERECO = {
    # Caucaia
    772: 11436,    # SEC. DE TURISMO E CULTURA E LAZER -> SECRETARIA DE CULTURA
    2851: 11436,   # SEC. DE TURISMO E CULTURA 2 (cópia) -> SECRETARIA DE CULTURA
    2858: 1636,    # ESPORTE E JUVENTUDE -> SECRETARIA DE ESPORTE E JUVENTUDE
    2906: 1099,    # P M DE CAUCAIA - IPM -> P M DE CAUCAIA / IPM
    3036: 1498,    # AÇÃO SOCIAL - CREAS SEDE -> AÇÃO SOCIAL - SEDE
    11099: 426,    # DESENVOLVIMENTO SOCIAL -> P M DE CAUCAIA - DESENVOLVIMENTO SOCIAL
    # 4439 (SAÚDE SEDE RESERVA SMS) é resolvido por migracao_reserva.
    # Cascavel
    3748: 2181,    # SECRETARIA DE OBRAS -> P.M DE CASCAVEL - SECRETARIA DE OBRAS
    3724: 3414,    # EDUCAÇÃO / PROTOCOLO -> EDUCAÇÃO PROTOCOLO
    3731: 3418,    # EDUCAÇÃO / LABORATÓRIO NAEC -> EDUCAÇÃO LABORATÓRIO NAEC
    3732: 1273,    # EDUCAÇÃO / PRESTAÇÃO DE CONTAS -> EDUCAÇÃO - PRESTAÇÃO DE CONTAS
    # Crato
    10388: 11698,  # SECRETARIA DE URBANISMO E MEIO AMBIENTE -> PM DE CRATO / SEC. DE URBANISMO
    2821: 2297,    # SECRETARIA DE ASSISTÊNCIA SOCIAL -> P. M. DE CRATO - SEC. DE ASSISTÊNCIA SOCIAL
    # Camocim
    3149: 2574,    # P M CAMOCIM / SECRETARIA DE SAÚDE (duplicado) -> idem com endereço
    2557: 2553,    # EDUCAÇÃO - PAIC -> EDUCAÇÃO - ADM (mesmo endereço da secretaria)
    # Aracoiaba
    3614: 4214,    # PM ARACOIABA - MEIO AMBIENTE -> idem com endereço
    # 3581 (SECRETARIA DE GOVERNO, órgão 1122) não tem customer/endereço no
    # refatorado; o par com o Gabinete (3604, mesmo órgão) é o único destino
    # ativo com endereço. Usado pelos equipamentos 35948/35950 (mov. 116967).
    3581: 3604,    # P M DE ARACOIABA/SECRETARIA DE GOVERNO -> GABINETE DO PREFEITO
    # Acarape / Aracati / Acopiara / Ocara / Barroquinha
    3843: 4114,    # ACARAPE - GABINETE DO PREFEITO -> idem com endereço
    3618: 4213,    # ACARAPE - CAMARA -> P M DE ACARAPE - CAMARA
    3606: 4122,    # ARACATI - SECRETARIA DE ADMINISTRAÇÃO -> idem com endereço
    3353: 3348,    # ARACATI - AÇÃO SOCIAL -> P. M. DE ARACATI - AÇÃO SOCIAL
    3873: 2813,    # ACOPIARA - TRABALHO E DESENVOLVIMENTO SOCIAL -> SECRETARIA DO TRABALHO
    4264: 3642,    # OCARA - SECRETARIA DE AGRICULTURA -> idem com endereço
    3017: 675,     # BARROQUINHA - SECRETARIA DE SAÚDE -> P.M DE BARROQUINHA - SAÚDE
    # Aquiraz / Eusébio / Iguatu
    # Reservas (10434, 4439, 4544) NÃO entram aqui: são resolvidas por
    # migracao_reserva via addresses.reserved_customer_id.
    1845: 3621,    # AQUIRAZ - SEC. DE EDUCAÇÃO -> P M AQUIRAZ / SECRETARIA DE EDUCAÇÃO
    4203: 3619,    # AQUIRAZ - GABINETE DO PREFEITO -> P M AQUIRAZ / GABINETE DO PREFEITO
    3823: 4428,    # EUSÉBIO - SECRETARIA DE CULTURA E TURISMO -> idem com endereço
    3966: 3792,    # IGUATU - TRÂNSITO E MOBILIDADE URBANA -> idem com endereço
    # Nacionais
    10504: 3624,   # SINDICATO DAS EMPRESAS DE TRANSPORTES (ETUFOR) -> idem sem sufixo
    10482: 10487,  # CENTRO DE PREP DE OFICIAIS DA RESERVA (SP) -> idem com endereço
    # MTE: 11252 (SIA - SETOR DE IND. E ABASTECIMENTO, deletado no legado em
    # 2025-04-08) não foi migrado. 11329 aponta para o customer 2347
    # (MTE - BRASÍLIA, SIA TRECHO 3) — mesmo complexo, com endereço.
    # Usado por 146947 (contrato 27) e 146980 (SA 530131 do ciclo 7105).
    11252: 11329,  # MTE - SIA -> MTE - BRASÍLIA (SIA)
    4027: 1840,    # HOSP. MUNICIPAL ABELARDO GADELHA - SAAD -> hospital
}

# Clientes que atuam como base/estoque apenas para a regra de status do ajuste.
# Diferente de CLIENTES_ESTOQUE (que também afeta a migração), esta lista
# corrige somente a classificação do relatório de divergências: equipamento com
# status DISPONÍVEL PARA LOCAÇÃO nesses clientes está correto.
CLIENTES_BASE_AJUSTE = {
    3163,   # MOREIA TECNOLOGIA - MANUTENÇÃO DE EQUIPAMENTOS
    4082,   # TONINHO PIRES FERREIRA (base Alucom)
    4090,   # AS SISTEMAS
}

# Equipamentos com decisão manual registrada: não entram na correção
# automática. A chave é o id do equipamento no LEGADO.
EQUIPAMENTOS_REVISAO_MANUAL = {
    118769: (
        "Duplicado em sistema: uma cópia está alugada e a outra em manutenção; "
        "o status EM MANUTENÇÃO desta cópia está correto."
    ),
}

# Setores internos que NÃO devem virar customer (secretaria). Seus clientes
# são anexados diretamente ao departamento pai.
# 1187 = ALUCOM SEDE RIACHUELO (departamento 283 = ADI)
# 1715 = Secretaria da Prefeitura 439 (cliente 12196 vai direto para a prefeitura)
SETORES_IGNORADOS = {1187, 1715}

# =========================================================================
# ESTOQUES CONSOLIDADOS DO BANCO REFATORADO
# =========================================================================
# Regra dos insumos:
#   - Produto da lista "Peças ADI" (GPA.ORG_ID = 1360 com saldo > 0 na query
#     legada de montagem) -> location PEÇAS ADI.
#   - Qualquer outro insumo -> location GERAL.
# Regra dos equipamentos (is_asset = 1):
#   - Todos -> location EQUIPAMENTOS no endereço-base da organização.
ORGS_ESTOQUE_ALUCOM = {1360}

LOCATION_ESTOQUE_ALUCOM = {
    "address_id": 1,
    "area": "Peças ADI",
    "section": None,
    "spot": None,
    "details": "Migração",
}

LOCATION_ESTOQUE_GERAL = {
    "address_id": 1,
    "area": "Geral",
    "section": None,
    "spot": None,
    "details": "Migração",
}

LOCATION_ESTOQUE_EQUIPAMENTOS = {
    "area": "Equipamentos",
    "section": None,
    "spot": None,
    "details": "Migração",
}


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
