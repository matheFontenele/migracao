"""
Ajustes finais de movimentos e status de equipamentos.

Objetivo
--------
Rodar DEPOIS de toda a migração (cadastros + movimentos) para:

1. Comparar o último movimento/status de cada equipamento entre o legado e o
   banco refatorado, usando as duas queries de diagnóstico.
2. Gerar um CSV com TODAS as divergências encontradas.
3. Opcionalmente (--aplicar), corrigir as divergências elegíveis reinserindo o
   último movimento do legado de forma INDIVIDUAL (um INSERT por vez), sem usar
   os inserts em massa (pandas.to_sql / executemany) dos scripts de migração.

Observação importante sobre o cruzamento
----------------------------------------
O tombo NÃO é único nos dois bancos (há vários equipamentos com o mesmo número).
Por isso as queries abaixo são as mesmas enviadas para diagnóstico, mas com a
adição de `alq.id` / `eqp.id` e alguns campos auxiliares. O cruzamento é feito
por ID (o ID do equipamento foi preservado na migração), não por tombo.

Uso
---
    python main.py ajustes_movimentos
    python main.py ajustes_movimentos --aplicar
    python main.py ajustes_movimentos --aplicar --limite 100
    python main.py ajustes_movimentos --aplicar --movimento 561308 51179
    python main.py ajustes_movimentos --aplicar --incluir-parquet
    python main.py ajustes_movimentos --aplicar --forcar-status-legado

Ou diretamente:

    python -m ajustes.ajustes_movimentos --aplicar
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime

import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from config.config import (
    CLIENTES_BASE_AJUSTE,
    CLIENTES_BLOQUEADOS,
    CLIENTES_ESTOQUE,
    EQUIPAMENTOS_REVISAO_MANUAL,
    FALSOS_RESERVAS,
)
from config.database import obter_engines
from config.parquet_excecoes import (
    equipamentos_priorizados,
    remover_priorizados,
)
from utils.depara_clientes import propagar_depara_duplicados


# ==============================================================================
# CAMINHOS E ARQUIVOS
# ==============================================================================
PASTA_AJUSTES = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PASTA_AJUSTES)
PASTA_PARQUETS = os.getenv("PARQUETS_DIR") or os.path.join(BASE_DIR, "docs", "parquets")

ARQ_DIVERGENCIAS = os.path.join(PASTA_AJUSTES, "divergencias_movimentos.csv")
ARQ_DIVERGENCIAS_POS = os.path.join(PASTA_AJUSTES, "divergencias_movimentos_pos_ajuste.csv")
ARQ_RESUMO = os.path.join(PASTA_AJUSTES, "resumo_divergencias_movimentos.csv")
ARQ_RESULTADO = os.path.join(PASTA_AJUSTES, "resultado_ajustes_movimentos.csv")
ARQ_DUPLICADOS_PENDENTES = os.path.join(PASTA_AJUSTES, "duplicados_pendentes_pos_migracao.csv")
ARQ_EXCECOES_ESTOQUE = os.path.join(PASTA_AJUSTES, "excecoes_estoque_esperadas.csv")


# ==============================================================================
# DE/PARA DE STATUS
# ==============================================================================
# situacao_id do legado -> status_id do banco refatorado
MAPA_SITUACAO_STATUS = {
    1: 2,    # ALUGADO                  -> ALUGADO
    2: 4,    # EMPRESTADO               -> EMPRESTADO
    3: 6,    # EM MANUTENÇÃO            -> EM MANUTENÇÃO
    4: 9,    # SEM CONSERTO             -> BAIXADO
    5: 5,    # USO DA ALUCOM            -> USO INTERNO
    6: 7,    # GARANTIA                 -> NA GARANTIA
    7: 1,    # LIBERADO P/ LOCACAO      -> DISPONÍVEL PARA LOCAÇÃO
    8: 9,    # GABINETE VAZIO           -> BAIXADO
    9: 6,    # COM DEFEITO              -> EM MANUTENÇÃO
    10: 9,   # BAIXADO                  -> BAIXADO
    11: 1,   # DISPONÍVEL P/ LOCAÇÃO    -> DISPONÍVEL PARA LOCAÇÃO
    12: 7,   # MANUTENÇÃO NA GARANTIA   -> NA GARANTIA
    13: 6,   # MANUTENÇÃO INTERNA       -> EM MANUTENÇÃO
    14: 8,   # EM DEVOLUÇÃO             -> EM DEVOLUÇÃO
    15: 3,   # RESERVADO                -> RESERVADO
    16: 5,   # USO INTERNO              -> USO INTERNO
}

NOMES_STATUS = {
    1: "DISPONÍVEL PARA LOCAÇÃO",
    2: "ALUGADO",
    3: "RESERVADO",
    4: "EMPRESTADO",
    5: "USO INTERNO",
    6: "EM MANUTENÇÃO",
    7: "NA GARANTIA",
    8: "EM DEVOLUÇÃO",
    9: "BAIXADO",
}

# tipo do legado -> movement_types.id / operation_type do banco novo
MAPA_TIPO_MOVIMENTO = {
    "A": (1, "ALUGUEL"),
    "D": (3, "DEVOLUÇÃO"),
    "SD": (2, "SUBSTITUIÇÃO"),
    "SA": (2, "SUBSTITUIÇÃO"),
    "TR": (7, "AVULSO"),
    "R": (4, "RESERVA"),
    "UI": (7, "AVULSO"),
}


def parear_equipamento_retorno(itens_sa, itens_sd, equipamento_entregue):
    """Equipamento devolvido que corresponde ao entregue num movimento SA.

    O legado vincula apenas as capas em ``aluguel_substituicao`` (SD =
    devolução, SA = envio do substituto). Os itens não têm vínculo explícito,
    mas o próprio sistema legado grava um item de cada lado por equipamento no
    mesmo laço, na mesma ordem — logo a posição na lista ordenada por ``id`` é
    o pareamento. Sem par (ou fora da faixa), devolve ``None``.
    """
    try:
        posicao = list(itens_sa).index(int(equipamento_entregue))
    except (TypeError, ValueError):
        return None
    itens_sd = list(itens_sd)
    if posicao >= len(itens_sd):
        return None
    return int(itens_sd[posicao])

# Classificações usadas no CSV
CLASSIFICACOES = {
    "OK": "Movimento/status já batem com o legado",
    "CORRIGIR_EQUIPAMENTO": "Último movimento confere; corrigir status/endereço do equipamento",
    "REINSERIR_MOVIMENTO": "Reinserir o último movimento do legado individualmente + atualizar equipamento",
    "SEM_MOVIMENTO_AMBOS": "Sem movimento nos dois bancos; corrigir apenas o status",
    "REVISAR_CLIENTE": "Mesmo movimento, mas o cliente do legado não tem de/para no refatorado",
    "NOVO_MAIS_RECENTE": "Revisão manual: o banco novo tem movimento mais recente que o legado",
    "LEGADO_SEM_MOVIMENTO_NOVO_TEM": "Revisão manual: legado sem movimento, novo com movimento",
}


# ==============================================================================
# QUERIES DE DIAGNÓSTICO
# ==============================================================================
# São as queries do pedido original. A única diferença é a inclusão de IDs e
# campos auxiliares para cruzamento/Correção. A query do legado NÃO filtra
# `mov.tipo IS NOT NULL`, para que equipamentos sem movimento também apareçam.
QUERY_LEGADO = """
SELECT
    alq.id AS id_legado,
    alq.numero AS tombo_legado,
    alq.nome AS equipamento_legado,
    alq.situacao_id AS situacao_id,
    sit.nome AS status_legado,
    alq.orgao_id AS orgao_legado,
    mov.id AS id_movimento_legado,
    mov.data AS data_legado,
    mov.tipo AS tipo_legado,
    mov.cliente_id AS id_cliente_legado,
    mov.usuario_id AS usuario_legado,
    cli.nome_razao_social AS cliente_legado
FROM aluguel_equipamentos alq
INNER JOIN aluguel_situacao sit
    ON sit.id = alq.situacao_id
LEFT JOIN aluguel_movimento_itens movi
    ON movi.id = (
        SELECT movi2.id
        FROM aluguel_movimento_itens movi2
        INNER JOIN aluguel_movimento mov2
            ON mov2.id = movi2.movimento_id
        WHERE movi2.equipamento_id = alq.id
          AND movi2.deleted_at IS NULL
          AND mov2.deleted_at IS NULL
        ORDER BY mov2.data DESC, mov2.id DESC, movi2.id DESC
        LIMIT 1
    )
LEFT JOIN aluguel_movimento mov
    ON mov.id = movi.movimento_id
LEFT JOIN aluguel_clientes cli
    ON cli.id = mov.cliente_id
WHERE alq.deleted_at IS NULL
ORDER BY alq.numero, alq.id
"""

QUERY_NOVO = """
SELECT
    eqp.id AS id_equipamento,
    eqp.number AS tombo_novo,
    eqp.name AS equipamento_novo,
    eqp.status_id AS status_id_novo,
    st.name AS status_novo,
    eqp.current_organization_id AS organization_id_novo,
    eqp.address_id AS address_id_novo,
    mov.id AS id_movimento_novo,
    mov.number AS numero_movimento_novo,
    movi.id AS id_mov_item_novo,
    mov.updated_at AS data_novo,
    mov_types.name AS tipo_novo,
    mov.recipient_customer_id AS id_customer_novo,
    cus.name AS cliente_novo,
    ad.alias AS destino_novo
FROM equipments eqp
LEFT JOIN status st
    ON eqp.status_id = st.id
LEFT JOIN movement_items movi
    ON movi.id = (
        SELECT movi2.id
        FROM movement_items movi2
        INNER JOIN movements mov2
            ON movi2.movement_id = mov2.id
        WHERE movi2.equipment_id = eqp.id
          AND movi2.deleted_at IS NULL
          AND mov2.deleted_at IS NULL
        ORDER BY mov2.updated_at DESC, mov2.id DESC, movi2.id DESC
        LIMIT 1
    )
LEFT JOIN movements mov
    ON movi.movement_id = mov.id
LEFT JOIN movement_types mov_types
    ON movi.movement_type_id = mov_types.id
LEFT JOIN customers cus
    ON mov.recipient_customer_id = cus.id
LEFT JOIN addresses ad
    ON eqp.address_id = ad.id
WHERE eqp.deleted_at IS NULL
ORDER BY eqp.number, eqp.id
"""

# endereço/legacy_customer_id -> customer_id e address_id (mesmo de/para usado
# pela migração de movimentos).
QUERY_MAPA_CLIENTES = """
SELECT
    legacy_customer_id,
    addressable_id AS id_customer,
    id AS address_id
FROM addresses
WHERE legacy_customer_id IS NOT NULL
  AND addressable_type = 'customer'
  AND deleted_at IS NULL
"""

QUERY_MAPA_RESERVAS = """
SELECT
    reserved_customer_id AS legacy_reserva_id,
    addressable_id AS id_customer,
    id AS address_id
FROM addresses
WHERE reserved_customer_id IS NOT NULL
  AND addressable_type = 'customer'
  AND deleted_at IS NULL
"""


# ==============================================================================
# HELPERS
# ==============================================================================
def _int_or_none(valor):
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _str_or_none(valor):
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    texto = str(valor).strip()
    return texto or None


def _tombo_key(valor):
    """Normaliza o tombo para agrupar copias no legado (710 e 710.0 = 710)."""
    numero = _int_or_none(valor)
    if numero is not None:
        return str(numero)
    return _str_or_none(valor)


def _rotulo_copias(qtd):
    """Rotulo de ocorrencia do tombo no legado."""
    qtd = _int_or_none(qtd) or 0
    if qtd <= 1:
        return "UNICO"
    if qtd == 2:
        return "DUPLICADO"
    if qtd == 3:
        return "TRIPLICADO"
    if qtd == 4:
        return "QUADRIPLICADO"
    return f"{qtd} COPIAS"


def separar_tombo_duplicado(df):
    """Separa as linhas com tombo repetido no banco migrado (diagnóstico).

    A duplicidade considerada é a refletida na migração (cópias do mesmo tombo
    entre equipamentos migrados). O conteúdo do legado fica apenas como
    diagnóstico, pois insumos podem repetir o tombo sem virar equipamento.

    O tombo repetido não bloqueia mais a correção automática (o cruzamento é
    por ID); esta separação serve ao relatório de duplicados que continuam
    pendentes por proteção do Parquet ou exceção de negócio.
    """
    coluna = (
        "copias_tombo_refatorado"
        if "copias_tombo_refatorado" in df.columns
        else "copias_tombo_legado"
    )
    if coluna not in df.columns:
        return df.copy(), df.iloc[0:0].copy()
    repetidos = df[coluna].fillna(0).astype(int) > 1
    return df[~repetidos].copy(), df[repetidos].copy()


def separar_divergencias(df):
    """Separa as divergencias acionaveis das excecoes esperadas de estoque.

    Equipamentos alugados no legado para clientes internos de estoque/base
    (``CLIENTES_ESTOQUE``) sao liberados para locacao pela propria migracao.
    Por isso ``ALUGADO`` x ``DISPONIVEL PARA LOCACAO`` nesses clientes e o
    comportamento esperado e nao deve entrar no relatorio de divergencias.

    As demais excecoes (``PARQUET``, ``CLIENTE_BLOQUEADO``,
    ``CLIENTE_SEM_MAPA`` etc.) continuam no relatorio: sao protegidas ou
    exigem revisao manual.
    """
    if df.empty:
        return df.copy(), df.copy()

    def coluna(nome, default=False):
        if nome in df.columns:
            return df[nome]
        return pd.Series(default, index=df.index)

    # Padrao esperado de liberacao: equipamento de cliente base/estoque.
    #   - status DISPONIVEL PARA LOCACAO: o bem esta na base, independente da
    #     situacao legada;
    #   - outro status, desde que alinhado ao legado (ex.: EM MANUTENCAO,
    #     EM DEVOLUCAO): o bem esta fisicamente na base e o cliente da base
    #     nao existe no refatorado, entao nao ha o que corrigir.
    esperada_estoque = (
        coluna("motivo_excecao", "").eq("CLIENTE_ESTOQUE")
        & (
            coluna("status_id_novo", None).apply(_int_or_none).eq(1)
            | (
                ~coluna("div_status", False).astype(bool)
                & ~coluna("status_id_novo", None).apply(_int_or_none).eq(2)
            )
        )
    )
    divergencia = (
        coluna("div_status")
        | coluna("div_cliente")
        | coluna("div_movimento")
        | coluna("cliente_sem_mapa")
        | coluna("classificacao", "").isin(
            ["NOVO_MAIS_RECENTE", "LEGADO_SEM_MOVIMENTO_NOVO_TEM"]
        )
    ) & ~coluna("reserva_mesclada")

    return (
        df[divergencia & ~esperada_estoque].copy(),
        df[divergencia & esperada_estoque].copy(),
    )


def identificar_reserva_mesclada(df, reservas_mapeadas=None):
    """Marca equipamentos alugados no legado para clientes de RESERVA.

    A migracao de clientes mescla esses clientes com o titular e transforma o
    aluguel em reserva. Logo, legado ALUGADO x refatorado RESERVADO e o
    comportamento esperado, nao uma divergencia. O equipamento tambem pode
    ficar DISPONIVEL PARA LOCACAO (1): a reserva e do titular e o bem esta na
    base, disponivel — por isso os dois status sao aceitos.

    A reserva pode ser identificada pelo nome (RESERVA/RESERVADO) ou pelo
    de/para registrado em ``addresses.reserved_customer_id``. Alem dos status
    DISPONIVEL (1) e RESERVADO (3), qualquer status alinhado ao legado (ex.:
    EM MANUTENCAO, EM DEVOLUCAO) tambem e excecao, pois o bem segue na
    base/reserva do titular. ALUGADO (2) continua divergencia: o bem nao esta
    na base, mesmo que legado e refatorado concordem.
    """
    if df.empty:
        return pd.Series(False, index=df.index)
    nomes = (
        df.get("cliente_legado", pd.Series("", index=df.index))
        .fillna("").astype(str)
        .str.contains(r"\b(?:RESERVA|RESERVADO)\b", case=False, regex=True)
    )
    ids = df.get("id_cliente_legado", pd.Series(None, index=df.index)).apply(_int_or_none)
    situacao = df.get("situacao_id", pd.Series(None, index=df.index)).apply(_int_or_none)
    status_novo = df.get("status_id_novo", pd.Series(None, index=df.index)).apply(_int_or_none)
    ids_validos = ~ids.isin(FALSOS_RESERVAS)

    por_nome = nomes & ids_validos
    por_mescla = pd.Series(False, index=df.index)
    if reservas_mapeadas:
        customer_novo = df.get(
            "id_customer_novo", pd.Series(None, index=df.index)
        ).apply(_int_or_none)
        customer_reserva = ids.map(reservas_mapeadas)
        por_mescla = (
            customer_reserva.notna()
            & customer_novo.notna()
            & (customer_reserva == customer_novo)
            & ids_validos
        )

    status_esperado = situacao.apply(
        lambda valor: MAPA_SITUACAO_STATUS.get(valor)
    )
    status_ok = status_novo.isin([1, 3]) | (
        (status_novo != 2)
        & status_esperado.notna()
        & (status_esperado == status_novo)
    )
    return (por_nome | por_mescla) & status_ok


# Clientes internos/base: CLIENTES_ESTOQUE (também usado pela migração) mais
# os identificados depois da migração, válidos apenas para esta regra.
CLIENTES_BASE = set(CLIENTES_ESTOQUE) | set(CLIENTES_BASE_AJUSTE)


def _equipamento_no_parquet(row, equipamentos_parquet):
    """True quando o equipamento da linha está no Parquet (por EQUIPAMENTO_ID).

    O ``EQUIPAMENTO_ID`` do Parquet é o id do equipamento no legado, preservado
    na migração. É a chave forte do escudo: o tombo se repete e já protegeu
    equipamentos que apenas compartilham o número com outro registro.
    """
    if not equipamentos_parquet:
        return False
    for chave in ("id_equipamento", "id_legado"):
        equipamento = _int_or_none(row.get(chave))
        if equipamento is not None and equipamento in equipamentos_parquet:
            return True
    return False


def motivo_excecao_linha(row, tombos_parquet, clientes_base=None, equipamentos_parquet=None):
    """Motivo de exceção de uma linha do diagnóstico, em ordem de prioridade.

    1. revisão manual registrada em ``EQUIPAMENTOS_REVISAO_MANUAL``;
    2. aluguel para cliente de RESERVA (mesclado com o titular);
    3. cliente de base/estoque com equipamento DISPONÍVEL PARA LOCAÇÃO
       (o bem está na base, status correto — não importa a situação legada)
       ou com outro status alinhado ao legado (ex.: EM MANUTENÇÃO);
    4. cliente bloqueado;
    5. equipamento presente no Parquet, identificado por ``EQUIPAMENTO_ID``;
       ``tombos_parquet`` só vale como fallback para os tombos que o Parquet
       não consegue vincular a um equipamento (sem id).
    """
    clientes_base = CLIENTES_BASE if clientes_base is None else set(clientes_base)
    id_legado = _int_or_none(row.get("id_legado"))
    if id_legado in EQUIPAMENTOS_REVISAO_MANUAL:
        return "REVISAO_MANUAL"

    cliente = _int_or_none(row.get("id_cliente_legado"))

    if bool(row.get("reserva_mesclada")):
        return "RESERVA_MESCLADA"

    if cliente in clientes_base and _int_or_none(row.get("status_id_novo")) == 1:
        return "CLIENTE_ESTOQUE"

    # Base/estoque com status diferente de DISPONÍVEL e alinhado ao legado:
    # o bem está fisicamente na base (manutenção/devolução) e o cliente da
    # base não existe no refatorado. Não há divergência a corrigir; só o
    # status importa aqui.
    if cliente in clientes_base:
        status_novo = _int_or_none(row.get("status_id_novo"))
        situacao = _int_or_none(row.get("situacao_id"))
        esperado = MAPA_SITUACAO_STATUS.get(situacao)
        # ALUGADO em cliente base é revisável: um bem de base não deveria
        # estar locado, mesmo que legado e refatorado concordem.
        if status_novo != 2 and esperado is not None and esperado == status_novo:
            return "CLIENTE_ESTOQUE"

    if cliente in CLIENTES_BLOQUEADOS:
        return "CLIENTE_BLOQUEADO"

    if _equipamento_no_parquet(row, equipamentos_parquet):
        return "PARQUET"

    tombo = _str_or_none(row.get("tombo_novo"))
    if tombo and tombo in tombos_parquet:
        return "PARQUET"

    classificacao = row.get("classificacao")
    if classificacao == "NOVO_MAIS_RECENTE":
        return "NOVO_MAIS_RECENTE"
    if classificacao == "LEGADO_SEM_MOVIMENTO_NOVO_TEM":
        return "LEGADO_SEM_MOVIMENTO"
    if (
        classificacao == "REINSERIR_MOVIMENTO"
        and _int_or_none(row.get("id_customer_esperado")) is None
    ):
        return "CLIENTE_SEM_MAPA"
    return ""


def _data_str(valor, default=None):
    if valor is None:
        return default
    try:
        if pd.isna(valor):
            return default
    except (TypeError, ValueError):
        pass
    data = pd.to_datetime(valor, errors="coerce")
    if pd.isna(data):
        return default
    return data.strftime("%Y-%m-%d %H:%M:%S")


def _data_date(valor, default=None):
    texto = _data_str(valor, default)
    return texto[:10] if texto else default


# ==============================================================================
# CLASSE PRINCIPAL
# ==============================================================================
class AjustesMovimentos:

    def __init__(self, engine_new, engine_legado, ignorar_parquet=True):
        self.engine_new = engine_new
        self.engine_legado = engine_legado
        self.ignorar_parquet = ignorar_parquet

        with self.engine_new.connect() as conn:
            self.usuarios_validos = {
                int(row[0]) for row in conn.execute(text("SELECT id FROM users"))
            }

        if ignorar_parquet:
            self.equipamentos_parquet, self.tombos_parquet = (
                self._carregar_escudo_parquet()
            )
        else:
            self.equipamentos_parquet, self.tombos_parquet = set(), set()
        # (numero_legado, movement_type_id) -> dados da capa criada/reutilizada
        self.cache_cabecalhos = {}
        # capas reaproveitadas cujo cabeçalho tem tipo diferente do legado
        self.capas_tipo_divergente = 0
        # movimento legado -> itens (ids de equipamento na ordem de inserção)
        self.cache_itens_legado = {}
        # (movimento SA, equipamento entregue) -> equipamento devolvido
        self.cache_retorno_substituicao = {}

    # --------------------------------------------------------------------------
    # PARQUET: equipamentos que o pipeline já tratou e não devem ser sobrescritos
    # --------------------------------------------------------------------------
    def _carregar_escudo_parquet(self):
        """Carrega o escudo do Parquet: equipamentos e tombos sem vínculo.

        A chave de proteção é o ``EQUIPAMENTO_ID`` do Parquet (o mesmo id do
        equipamento no legado, preservado na migração). O tombo só protege no
        fallback: registros do Parquet sem ``EQUIPAMENTO_ID``. Isso evita que
        um número repetido proteja equipamentos que apenas o compartilham.

        Retorna ``(equipamentos, tombos_sem_id)``, ambos sem os priorizados em
        ``config.parquet_excecoes`` (que seguem o legado).
        """
        arquivos = glob.glob(os.path.join(PASTA_PARQUETS, "*.parquet"))
        if not arquivos:
            print(f"⚠️  Nenhum Parquet em {PASTA_PARQUETS}; proteção de equipamentos desativada.")
            return set(), set()

        try:
            frames = []
            for arquivo in arquivos:
                df = pd.read_parquet(arquivo)
                df.columns = df.columns.str.upper()
                frames.append(df)

            df = pd.concat(frames, ignore_index=True)

            if "EQUIPAMENTO_ID" in df.columns:
                ids = pd.to_numeric(df["EQUIPAMENTO_ID"], errors="coerce")
            else:
                ids = pd.Series(float("nan"), index=df.index)
            equipamentos = {int(valor) for valor in ids.dropna().astype("int64")}

            # Fallback: só os tombos que o Parquet não consegue vincular a um
            # equipamento (linhas sem EQUIPAMENTO_ID).
            tombos_sem_id = set()
            if "TOMBO" in df.columns:
                tombos = pd.to_numeric(df["TOMBO"], errors="coerce")
                orfaos = tombos[ids.isna()].dropna()
                tombos_sem_id = {str(int(valor)) for valor in orfaos.astype("int64")}

            priorizados = equipamentos_priorizados()
            liberados_eq = equipamentos & priorizados
            equipamentos -= priorizados

            tombos_priorizados = remover_priorizados(tombos_sem_id, self.engine_legado)
            liberados_tombo = tombos_sem_id - tombos_priorizados
            tombos_sem_id = tombos_priorizados

            if liberados_eq or liberados_tombo:
                detalhes = []
                if liberados_eq:
                    detalhes.append("equipamento(s) " + ", ".join(str(i) for i in sorted(liberados_eq)))
                if liberados_tombo:
                    detalhes.append("tombo(s) " + ", ".join(sorted(liberados_tombo)))
                print(
                    "🔓 "
                    f"{len(liberados_eq) + len(liberados_tombo)} exceção(ões) priorizada(s) "
                    f"seguem o legado: {'; '.join(detalhes)}"
                )

            print(
                f"🛡️  {len(equipamentos):,} equipamentos do Parquet protegidos "
                "por EQUIPAMENTO_ID."
            )
            if tombos_sem_id:
                print(
                    f"   • {len(tombos_sem_id):,} tombo(s) sem EQUIPAMENTO_ID no Parquet "
                    "protegidos por fallback."
                )
            return equipamentos, tombos_sem_id
        except Exception as exc:
            print(f"⚠️  Não foi possível ler os Parquets ({exc}). Seguindo sem proteção.")
            return set(), set()

    # --------------------------------------------------------------------------
    # CARGA
    # --------------------------------------------------------------------------
    def _carregar_mapas_clientes(self, conn):
        df = pd.read_sql(text(QUERY_MAPA_CLIENTES), conn)
        if df.empty:
            return {}, {}, {}, {}
        mapa_customer = dict(zip(df["legacy_customer_id"].astype(int), df["id_customer"].astype(int)))
        mapa_address = dict(zip(df["legacy_customer_id"].astype(int), df["address_id"].astype(int)))
        # Cópias mescladas na migração de clientes não têm endereço próprio:
        # herdam o de/para do cliente principal (mesma regra da migração).
        # Sem isso, o diagnóstico acusa CLIENTE_SEM_MAPA para um cliente que a
        # migração de movimentos consegue resolver.
        propagar_depara_duplicados(mapa_customer)
        propagar_depara_duplicados(mapa_address)
        df_reservas = pd.read_sql(text(QUERY_MAPA_RESERVAS), conn)
        mapa_reservas = (
            dict(zip(df_reservas["legacy_reserva_id"].astype(int),
                     df_reservas["id_customer"].astype(int)))
            if not df_reservas.empty else {}
        )
        mapa_address_reservas = (
            dict(zip(df_reservas["legacy_reserva_id"].astype(int),
                     df_reservas["address_id"].astype(int)))
            if not df_reservas.empty else {}
        )
        return mapa_customer, mapa_address, mapa_reservas, mapa_address_reservas

    def comparar(self):
        print("\n" + "=" * 78)
        print("🔎 AJUSTES DE MOVIMENTOS — DIAGNÓSTICO LEGADO x REFATORADO")
        print("=" * 78)

        print("📖 Lendo último movimento/status no LEGADO...")
        with self.engine_legado.connect() as conn:
            df_legado = pd.read_sql(text(QUERY_LEGADO), conn)

        print("📖 Lendo último movimento/status no REFATORADO...")
        with self.engine_new.connect() as conn:
            df_novo = pd.read_sql(text(QUERY_NOVO), conn)
            mapa_customer, mapa_address, mapa_reservas, mapa_address_reservas = (
                self._carregar_mapas_clientes(conn)
            )

        print(
            f"   Legado: {len(df_legado):,} equipamentos | "
            f"Refatorado: {len(df_novo):,} equipamentos"
        )

        # ------------------------------------------------------------------
        # Cruzamento por ID (o tombo se repete nos dois bancos)
        # ------------------------------------------------------------------
        df = df_novo.merge(
            df_legado,
            left_on="id_equipamento",
            right_on="id_legado",
            how="left",
            suffixes=("", "_leg"),
        )

        # ------------------------------------------------------------------
        # Ocorrencia do tombo no legado (todas as copias ativas)
        # ------------------------------------------------------------------
        chave_legado = df_legado["tombo_legado"].apply(_tombo_key)
        contagem_tombo = chave_legado.value_counts()
        df["copias_tombo_legado"] = (
            df["tombo_legado"].apply(_tombo_key).map(contagem_tombo).fillna(0).astype(int)
        )
        df["status_tombo_legado"] = df["copias_tombo_legado"].apply(_rotulo_copias)

        # ------------------------------------------------------------------
        # Ocorrencia do tombo no banco refatorado (equipamentos migrados).
        # A duplicidade so e considerada quando aparece na migracao; insumos
        # que repetem o tombo no legado nao contam aqui.
        # ------------------------------------------------------------------
        chave_refatorado = df_novo["tombo_novo"].apply(_tombo_key)
        contagem_refatorado = chave_refatorado.value_counts()
        df["copias_tombo_refatorado"] = (
            df["tombo_novo"].apply(_tombo_key).map(contagem_refatorado).fillna(0).astype(int)
        )
        df["status_tombo_refatorado"] = df["copias_tombo_refatorado"].apply(_rotulo_copias)

        # ------------------------------------------------------------------
        # De/para de status e cliente
        # ------------------------------------------------------------------
        df["status_esperado_id"] = df["situacao_id"].apply(
            lambda v: MAPA_SITUACAO_STATUS.get(_int_or_none(v))
        )
        df["status_esperado"] = df["status_esperado_id"].apply(
            lambda v: NOMES_STATUS.get(_int_or_none(v), "")
        )

        # Aluguel para cliente de RESERVA no legado vira RESERVA no refatorado
        # por regra da migracao de clientes. Nao e divergencia.
        df["reserva_mesclada"] = identificar_reserva_mesclada(df, mapa_reservas)

        clientes_legado = df["id_cliente_legado"].apply(_int_or_none)
        df["id_customer_esperado"] = clientes_legado.map(mapa_customer)
        df["address_id_esperado"] = clientes_legado.map(mapa_address)
        df["cliente_esperado"] = df["cliente_legado"]

        # Cliente de RESERVA só existe no legado: o destino no refatorado é o
        # titular (addressable_id do endereço reservado). Sem isso, o
        # diagnóstico acusa CLIENTE_SEM_MAPA para uma reserva já resolvida.
        reserva_customer = clientes_legado.map(mapa_reservas)
        reserva_address = clientes_legado.map(mapa_address_reservas)
        sem_mapa = df["id_customer_esperado"].isna()
        df.loc[sem_mapa & reserva_customer.notna(), "id_customer_esperado"] = reserva_customer
        df.loc[sem_mapa & reserva_address.notna(), "address_id_esperado"] = reserva_address

        df["numero_movimento_novo_int"] = pd.to_numeric(
            df["numero_movimento_novo"], errors="coerce"
        )

        # ------------------------------------------------------------------
        # Flags de divergência
        # ------------------------------------------------------------------
        df["div_status"] = (
            df["status_esperado_id"].notna()
            & (df["status_esperado_id"] != df["status_id_novo"])
        )

        df["mesmo_movimento"] = (
            df["id_movimento_legado"].notna()
            & df["numero_movimento_novo_int"].notna()
            & (df["numero_movimento_novo_int"] == df["id_movimento_legado"])
        )

        df["div_movimento"] = (
            df["id_movimento_legado"].notna() & ~df["mesmo_movimento"]
        )

        df["div_cliente"] = (
            df["mesmo_movimento"]
            & df["id_customer_esperado"].notna()
            & (df["id_customer_esperado"] != df["id_customer_novo"])
        )

        df["cliente_sem_mapa"] = (
            df["mesmo_movimento"]
            & df["id_cliente_legado"].notna()
            & df["id_customer_esperado"].isna()
        )

        # Reservas mescladas sao regra esperada: nao contam como divergencia.
        if df["reserva_mesclada"].any():
            df.loc[
                df["reserva_mesclada"],
                ["div_status", "div_cliente", "div_movimento", "cliente_sem_mapa"],
            ] = False

        # ------------------------------------------------------------------
        # Classificação da linha
        # ------------------------------------------------------------------
        def classificar(row):
            mov_legado = _int_or_none(row["id_movimento_legado"])
            mov_novo = _int_or_none(row["id_movimento_novo"])

            if mov_legado is None:
                if mov_novo is None:
                    return "SEM_MOVIMENTO_AMBOS"
                return "LEGADO_SEM_MOVIMENTO_NOVO_TEM"

            if mov_novo is None:
                return "REINSERIR_MOVIMENTO"

            if _int_or_none(row["numero_movimento_novo_int"]) == mov_legado:
                if bool(row["cliente_sem_mapa"]):
                    return "REVISAR_CLIENTE"
                if bool(row["div_status"]) or bool(row["div_cliente"]):
                    return "CORRIGIR_EQUIPAMENTO"
                return "OK"

            data_legado = pd.to_datetime(row["data_legado"], errors="coerce")
            data_novo = pd.to_datetime(row["data_novo"], errors="coerce")
            if pd.notna(data_legado) and pd.notna(data_novo) and data_novo > data_legado:
                return "NOVO_MAIS_RECENTE"

            return "REINSERIR_MOVIMENTO"

        df["classificacao"] = df.apply(classificar, axis=1)

        # ------------------------------------------------------------------
        # Exceções de negócio
        # ------------------------------------------------------------------
        df["motivo_excecao"] = df.apply(
            lambda row: motivo_excecao_linha(
                row,
                self.tombos_parquet,
                equipamentos_parquet=self.equipamentos_parquet,
            ),
            axis=1,
        )

        # ------------------------------------------------------------------
        # Elegibilidade para correção
        # ------------------------------------------------------------------
        df["corrigivel"] = (
            df["classificacao"].isin(
                ["REINSERIR_MOVIMENTO", "CORRIGIR_EQUIPAMENTO", "SEM_MOVIMENTO_AMBOS"]
            )
            & (df["motivo_excecao"] == "")
            & (
                df["div_status"]
                | df["div_cliente"]
                | (df["classificacao"] == "REINSERIR_MOVIMENTO")
            )
        )

        # ------------------------------------------------------------------
        # Ação sugerida (texto para o CSV)
        # ------------------------------------------------------------------
        def acao_sugerida(row):
            if row["corrigivel"]:
                if row["classificacao"] == "REINSERIR_MOVIMENTO":
                    return "Reinserir último movimento individualmente e atualizar equipamento"
                if row["classificacao"] == "SEM_MOVIMENTO_AMBOS":
                    return "Atualizar somente o status do equipamento"
                if bool(row["div_cliente"]):
                    return (
                        "Atualizar cliente da capa do movimento "
                        "(movements + service_orders) e endereço do equipamento"
                    )
                return "Atualizar status/endereço do equipamento"
            if row["motivo_excecao"] == "REVISAO_MANUAL":
                motivo = EQUIPAMENTOS_REVISAO_MANUAL.get(
                    _int_or_none(row["id_legado"]), "decisão manual registrada"
                )
                return f"Revisão manual: {motivo}"
            if row["motivo_excecao"] == "RESERVA_MESCLADA":
                return "Exceção: aluguel de cliente reserva (mesclado com o titular)"
            if row["motivo_excecao"] == "NOVO_MAIS_RECENTE":
                return "Revisar manualmente: o refatorado tem movimento mais recente"
            if row["motivo_excecao"] == "LEGADO_SEM_MOVIMENTO":
                return "Revisar manualmente: legado sem movimento"
            if row["motivo_excecao"] == "CLIENTE_ESTOQUE":
                return "Exceção: cliente interno de estoque/base"
            if row["motivo_excecao"] == "CLIENTE_BLOQUEADO":
                return "Exceção: cliente bloqueado"
            if row["motivo_excecao"] == "PARQUET":
                return "Exceção: tombo protegido pelo Parquet"
            if row["motivo_excecao"] == "CLIENTE_SEM_MAPA":
                return "Revisar manualmente: cliente legado sem de/para no refatorado"
            return ""

        df["acao_sugerida"] = df.apply(acao_sugerida, axis=1)

        # ------------------------------------------------------------------
        # Recorte final
        # ------------------------------------------------------------------
        # Excecoes esperadas de estoque (cliente interno/base) ficam fora do
        # relatorio de divergencias, mas sao mantidas em um CSV separado para
        # auditoria. PARQUET e demais excecoes seguem no relatorio.
        df_div, df_excecoes_estoque = separar_divergencias(df)
        if not df_excecoes_estoque.empty:
            df_excecoes_estoque.to_csv(
                ARQ_EXCECOES_ESTOQUE, index=False, encoding="utf-8-sig"
            )
            print(
                f"📄 {len(df_excecoes_estoque):,} exceções esperadas de estoque "
                f"salvas em: {ARQ_EXCECOES_ESTOQUE}"
            )

        # ------------------------------------------------------------------
        # Ordenação e seleção das colunas do arquivo
        # ------------------------------------------------------------------
        colunas = [
            "id_legado",
            "id_equipamento",
            "tombo_novo",
            "equipamento_novo",
            "copias_tombo_refatorado",
            "status_tombo_refatorado",
            "copias_tombo_legado",
            "status_tombo_legado",
            "situacao_id",
            "status_legado",
            "status_novo",
            "status_esperado",
            "div_status",
            "id_cliente_legado",
            "cliente_legado",
            "id_customer_novo",
            "cliente_novo",
            "id_customer_esperado",
            "cliente_esperado",
            "div_cliente",
            "cliente_sem_mapa",
            "id_movimento_legado",
            "id_movimento_novo",
            "numero_movimento_novo",
            "tipo_legado",
            "tipo_novo",
            "data_legado",
            "data_novo",
            "div_movimento",
            "classificacao",
            "motivo_excecao",
            "corrigivel",
            "acao_sugerida",
        ]
        df_div = df_div[colunas].sort_values(
            ["classificacao", "tombo_novo", "id_equipamento"]
        )

        self._imprimir_resumo(df, df_div, df_excecoes_estoque)
        self._salvar_resumo(df, df_div, df_excecoes_estoque)
        return df, df_div

    # --------------------------------------------------------------------------
    # RESUMO / ARQUIVOS
    # --------------------------------------------------------------------------
    def _imprimir_resumo(self, df, df_div, df_excecoes_estoque=None):
        total = len(df)
        df_sem_estoque = (
            df[df["motivo_excecao"] != "CLIENTE_ESTOQUE"]
            if "motivo_excecao" in df.columns
            else df
        )
        print("\n" + "-" * 78)
        print(f"📦 Equipamentos analisados: {total:,}")
        print(f"⚠️  Linhas com alguma divergência: {len(df_div):,}")
        print(f"   • Status divergente:   {int(df_sem_estoque['div_status'].sum()):,}")
        print(f"   • Cliente divergente:  {int(df_sem_estoque['div_cliente'].sum()):,}")
        print(f"   • Movimento ausente/diferente: {int(df_sem_estoque['div_movimento'].sum()):,}")
        if df_excecoes_estoque is not None and not df_excecoes_estoque.empty:
            print(
                f"   • Exceções esperadas de estoque: {len(df_excecoes_estoque):,} "
                "(fora do relatório)"
            )
        com_copias = df_div[df_div["copias_tombo_refatorado"] > 1]
        if not com_copias.empty:
            elegiveis_copias = int(
                com_copias["corrigivel"].fillna(False).astype(bool).sum()
            )
            print(f"   • Tombo duplicado na migração: {len(com_copias):,}")
            print(
                f"       elegíveis p/ ajuste: {elegiveis_copias:,} | "
                f"bloqueados/exceção: {len(com_copias) - elegiveis_copias:,}"
            )
            for rotulo, qtd in com_copias["status_tombo_refatorado"].value_counts().items():
                print(f"       {rotulo:<18} {qtd:>7,}")
        com_copias_legado = df_div[df_div["copias_tombo_legado"] > 1]
        print(f"   • Tombo repetido no legado (diagnóstico):  {len(com_copias_legado):,}")
        print(f"   • Elegíveis p/ correção automática: {int(df['corrigivel'].sum()):,}")
        print("-" * 78)
        print("Divergências por classificação:")
        for chave, qtd in df_div["classificacao"].value_counts().items():
            print(f"   {chave:<32} {qtd:>7,}")
        if (df_div["motivo_excecao"] != "").any():
            print("Exceções / revisão manual:")
            for chave, qtd in (
                df_div.loc[df_div["motivo_excecao"] != "", "motivo_excecao"]
                .value_counts()
                .items()
            ):
                print(f"   {chave:<32} {qtd:>7,}")
        print("-" * 78)

    def _salvar_resumo(self, df, df_div, df_excecoes_estoque=None):
        linhas = []
        df_sem_estoque = (
            df[df["motivo_excecao"] != "CLIENTE_ESTOQUE"]
            if "motivo_excecao" in df.columns
            else df
        )

        linhas.append({"tipo_resumo": "TOTAL_ANALISADO", "chave": "", "quantidade": len(df)})
        linhas.append(
            {"tipo_resumo": "TOTAL_DIVERGENCIAS", "chave": "", "quantidade": len(df_div)}
        )
        linhas.append(
            {"tipo_resumo": "STATUS_DIVERGENTE", "chave": "", "quantidade": int(df_sem_estoque["div_status"].sum())}
        )
        linhas.append(
            {"tipo_resumo": "CLIENTE_DIVERGENTE", "chave": "", "quantidade": int(df_sem_estoque["div_cliente"].sum())}
        )
        linhas.append(
            {"tipo_resumo": "MOVIMENTO_DIVERGENTE", "chave": "", "quantidade": int(df_sem_estoque["div_movimento"].sum())}
        )
        if df_excecoes_estoque is not None:
            linhas.append(
                {
                    "tipo_resumo": "EXCECAO_ESTOQUE_ESPERADA",
                    "chave": "",
                    "quantidade": int(len(df_excecoes_estoque)),
                }
            )
        linhas.append(
            {"tipo_resumo": "CORRIGIVEIS", "chave": "", "quantidade": int(df["corrigivel"].sum())}
        )
        com_copias = df_div[df_div["copias_tombo_refatorado"] > 1]
        linhas.append(
            {"tipo_resumo": "TOMBOS_COM_COPIAS", "chave": "", "quantidade": len(com_copias)}
        )
        linhas.append(
            {
                "tipo_resumo": "TOMBOS_COM_COPIAS_ELEGIVEIS",
                "chave": "",
                "quantidade": int(
                    com_copias["corrigivel"].fillna(False).astype(bool).sum()
                ),
            }
        )
        for chave, qtd in com_copias["status_tombo_refatorado"].value_counts().items():
            linhas.append(
                {"tipo_resumo": "TOMBO_OCORRENCIA", "chave": chave, "quantidade": int(qtd)}
            )
        com_copias_legado = df_div[df_div["copias_tombo_legado"] > 1]
        linhas.append(
            {"tipo_resumo": "TOMBOS_COM_COPIAS_LEGADO", "chave": "", "quantidade": len(com_copias_legado)}
        )

        for chave, qtd in df_div["classificacao"].value_counts().items():
            linhas.append(
                {"tipo_resumo": "CLASSIFICACAO", "chave": chave, "quantidade": int(qtd)}
            )

        for chave, qtd in (
            df_div.loc[df_div["motivo_excecao"] != "", "motivo_excecao"].value_counts().items()
        ):
            linhas.append(
                {"tipo_resumo": "EXCECAO", "chave": chave, "quantidade": int(qtd)}
            )

        # Pares de status legado -> status atual (só quando há divergência)
        base_status = df_sem_estoque[df_sem_estoque["div_status"]].copy()
        if not base_status.empty:
            base_status["par"] = (
                base_status["status_legado"].fillna("(sem status)")
                + " -> "
                + base_status["status_novo"].fillna("(sem status)")
            )
            for chave, qtd in base_status["par"].value_counts().items():
                linhas.append(
                    {"tipo_resumo": "STATUS_PAR", "chave": chave, "quantidade": int(qtd)}
                )

        pd.DataFrame(linhas).to_csv(ARQ_RESUMO, index=False, encoding="utf-8-sig")

    def salvar_divergencias(self, df_div, caminho=ARQ_DIVERGENCIAS):
        df_div.to_csv(caminho, index=False, encoding="utf-8-sig")
        print(f"📄 Arquivo de divergências gerado: {caminho}")
        print(f"📄 Resumo gerado: {ARQ_RESUMO}")

    def salvar_duplicados_pendentes(self, df_div, caminho=ARQ_DUPLICADOS_PENDENTES):
        """Registra os equipamentos com tombo repetido que continuam sem correção.

        Com a correção liberada para tombo repetido, este arquivo deixa de ser
        a lista de "ignorados": guarda apenas os duplicados que seguem fora do
        ajuste automático (protegidos pelo Parquet ou exceção de negócio).
        """
        _, duplicados = separar_tombo_duplicado(df_div)
        pendentes = duplicados[~duplicados["corrigivel"].fillna(False).astype(bool)]
        if not pendentes.empty:
            pendentes = pendentes.assign(
                _tombo_ordem=pd.to_numeric(pendentes["tombo_novo"], errors="coerce")
            ).sort_values(
                ["status_tombo_refatorado", "_tombo_ordem", "id_equipamento"]
            ).drop(columns="_tombo_ordem")
        pendentes.to_csv(caminho, index=False, encoding="utf-8-sig")
        print(
            f"📄 {len(pendentes):,} equipamento(s) com tombo duplicado seguem sem "
            f"correção automática e foram salvos em: {caminho}"
        )
        return pendentes

    # --------------------------------------------------------------------------
    # CORREÇÃO INDIVIDUAL (SEM BULK INSERT)
    # --------------------------------------------------------------------------
    def _selecionar_elegiveis(self, df, forcar_status_legado=False):
        """Linhas corrigíveis. Com --forcar-status-legado também inclui as
        exceções de cliente interno/bloqueado (nunca Parquet ou movimento mais
        recente no refatorado).

        Tombo repetido na migração não bloqueia mais o ajuste: a duplicidade
        por si só não identifica o equipamento errado (o cruzamento é por ID).
        O que bloqueia é o escudo do Parquet, já refletido em
        ``motivo_excecao``.
        """
        mask = df["corrigivel"]
        if forcar_status_legado:
            forcaveis = (
                df["classificacao"].isin(
                    ["REINSERIR_MOVIMENTO", "CORRIGIR_EQUIPAMENTO", "SEM_MOVIMENTO_AMBOS"]
                )
                & df["motivo_excecao"].isin(["CLIENTE_ESTOQUE", "CLIENTE_BLOQUEADO"])
            )
            mask = mask | forcaveis
        return df[mask].copy()

    def aplicar(self, df, limite=None, forcar_status_legado=False, movimentos=None):
        elegiveis = self._selecionar_elegiveis(df, forcar_status_legado)
        if movimentos:
            elegiveis = elegiveis[
                pd.to_numeric(elegiveis["id_movimento_legado"], errors="coerce").isin(
                    [int(m) for m in movimentos]
                )
            ]
        # Agrupa os equipamentos do mesmo movimento para reaproveitar a capa
        # (um movimento do legado pode conter dezenas de equipamentos).
        elegiveis["_ordem_mov"] = pd.to_numeric(
            elegiveis["id_movimento_legado"], errors="coerce"
        )
        elegiveis = elegiveis.sort_values(
            ["_ordem_mov", "id_equipamento"], na_position="last"
        )
        if limite:
            elegiveis = elegiveis.head(int(limite))

        if elegiveis.empty:
            print("\n✅ Nenhuma divergência elegível para correção automática.")
            return pd.DataFrame()

        print("\n" + "=" * 78)
        print(f"🛠️  APLICANDO CORREÇÕES INDIVIDUAIS EM {len(elegiveis):,} EQUIPAMENTOS")
        print("   (um INSERT/UPDATE por equipamento; sem to_sql/executemany)")
        print("=" * 78)

        resultados = []
        for _, row in tqdm(
            elegiveis.iterrows(), total=len(elegiveis), desc="Corrigindo equipamentos"
        ):
            resultado = self._corrigir_uma_linha(row, forcar_status_legado)
            resultados.append(resultado)

        df_resultado = pd.DataFrame(resultados)
        df_resultado.to_csv(ARQ_RESULTADO, index=False, encoding="utf-8-sig")

        sucesso = int((df_resultado["erro"].fillna("") == "").sum())
        falha = len(df_resultado) - sucesso
        print("\n" + "-" * 78)
        print(f"✅ Corrigidos: {sucesso:,}")
        print(f"❌ Falhas:    {falha:,}")
        if getattr(self, "capas_tipo_divergente", 0):
            print(f"♻️ Capas reaproveitadas com tipo divergente do legado: "
                  f"{self.capas_tipo_divergente:,}")
        print(f"📄 Resultado detalhado: {ARQ_RESULTADO}")
        if falha:
            print("\nPrimeiras falhas:")
            for _, linha in df_resultado[df_resultado["erro"].fillna("") != ""].head(10).iterrows():
                print(f"   tombo {linha['tombo']} (eq {linha['id_equipamento']}): {linha['erro']}")
        print("-" * 78)
        return df_resultado

    def _corrigir_uma_linha(self, row, forcar_status_legado):
        id_equipamento = _int_or_none(row["id_equipamento"])
        tombo = _str_or_none(row["tombo_novo"])
        status_antes = _int_or_none(row["status_id_novo"])
        cliente_antes = _int_or_none(row["id_customer_novo"])
        mov_antes = _int_or_none(row["numero_movimento_novo"])
        resultado = {
            "id_equipamento": id_equipamento,
            "tombo": tombo,
            "equipamento": row["equipamento_novo"],
            "classificacao": row["classificacao"],
            "acao": "",
            "status_antes": status_antes,
            "status_depois": status_antes,
            "cliente_antes": cliente_antes,
            "cliente_depois": cliente_antes,
            "movimento_legado": _int_or_none(row["id_movimento_legado"]),
            "movimento_novo_antes": mov_antes,
            "movimento_novo_depois": mov_antes,
            "erro": "",
        }

        try:
            with self.engine_new.begin() as conn:
                mov_id, item_id, modo = self._garantir_movimento(conn, row)

                status_depois, cliente_depois = self._atualizar_equipamento(
                    conn, row, item_id, forcar_status_legado
                )

                self._gravar_historico(conn, row, mov_id, item_id, status_depois)

            resultado.update(
                {
                    "acao": modo,
                    "status_depois": status_depois,
                    "cliente_depois": cliente_depois,
                    "movimento_novo_depois": _int_or_none(row["id_movimento_legado"])
                    if mov_id
                    else mov_antes,
                }
            )
        except Exception as exc:
            resultado["erro"] = f"{type(exc).__name__}: {exc}"

        return resultado

    def _garantir_movimento(self, conn, row):
        """Garante que o último movimento do legado exista no refatorado.

        Retorna (movement_id, movement_item_id, modo).

        Um mesmo movimento do legado pode cobrir vários equipamentos. Para não
        duplicar capas, o primeiro equipamento cria o movimento e os seguintes
        entram como novos itens do mesmo movimento — sempre com INSERT
        individual (sem to_sql/executemany).
        """
        numero_legado = _int_or_none(row["id_movimento_legado"])
        if numero_legado is None:
            return None, _int_or_none(row["id_mov_item_novo"]), "SEM_MOVIMENTO_LEGADO"

        tipo_legado = (_str_or_none(row["tipo_legado"]) or "A").upper()
        movimento_tipo_id, _ = MAPA_TIPO_MOVIMENTO.get(tipo_legado, (1, "ALUGUEL"))

        item_existente = self._buscar_item_existente(conn, row, numero_legado)
        if item_existente:
            mov_id, item_id = item_existente
            if bool(row["div_cliente"]):
                self._atualizar_cliente_cabecalho(conn, row, mov_id)
            return mov_id, item_id, "MOVIMENTO_REUTILIZADO"

        cabecalho = self._obter_cabecalho(conn, numero_legado, movimento_tipo_id)
        if cabecalho is None:
            cabecalho = self._inserir_cabecalho(conn, row, numero_legado, movimento_tipo_id)
            modo = "MOVIMENTO_INSERIDO"
        else:
            modo = "MOVIMENTO_REUTILIZADO_ITENS"

        item_id = self._inserir_itens(conn, row, cabecalho, movimento_tipo_id)
        if bool(row["div_cliente"]):
            self._atualizar_cliente_cabecalho(conn, row, cabecalho["movement_id"])
        return cabecalho["movement_id"], item_id, modo

    def _buscar_item_existente(self, conn, row, numero_legado):
        """Procura um item já migrado deste equipamento no movimento do legado."""
        existente = conn.execute(
            text(
                """
                SELECT m.id, mi.id
                FROM movements m
                INNER JOIN movement_items mi
                    ON mi.movement_id = m.id
                   AND mi.equipment_id = :eid
                   AND mi.deleted_at IS NULL
                WHERE m.number = :numero
                  AND m.deleted_at IS NULL
                ORDER BY mi.id DESC
                LIMIT 1
                """
            ),
            {"numero": str(numero_legado), "eid": int(row["id_equipamento"])},
        ).fetchone()

        if not existente:
            return None
        return int(existente[0]), int(existente[1])

    def _obter_cabecalho(self, conn, numero_legado, movimento_tipo_id):
        """Retorna a capa (movements/service_order/shipment) já existente para o
        número do movimento do legado, ou None."""
        chave = (int(numero_legado), int(movimento_tipo_id))
        if chave in self.cache_cabecalhos:
            return self.cache_cabecalhos[chave]

        linha = conn.execute(
            text(
                """
                SELECT
                    m.id,
                    m.service_order_id,
                    (SELECT so.movement_type_id
                       FROM service_orders so
                      WHERE so.id = m.service_order_id) AS movement_type_id,
                    (SELECT sh.id
                       FROM shipment_movements sm
                       INNER JOIN shipments sh
                           ON sh.id = sm.shipment_id
                          AND sh.deleted_at IS NULL
                      WHERE sm.movement_id = m.id
                        AND sm.deleted_at IS NULL
                      ORDER BY sm.id
                      LIMIT 1) AS shipment_id
                FROM movements m
                WHERE m.number = :numero
                  AND m.deleted_at IS NULL
                ORDER BY m.id
                LIMIT 1
                """
            ),
            {"numero": str(numero_legado)},
        ).fetchone()

        if not linha:
            return None

        tipo_existente = _int_or_none(linha[2])
        if tipo_existente is not None and tipo_existente != int(movimento_tipo_id):
            # O mesmo movimento do legado costuma misturar operações (aluguel +
            # devolução) e a migração criou a capa com o tipo do primeiro item.
            # Em vez de abrir uma SEGUNDA capa com o mesmo `number`, reaproveita
            # a existente: o item entra com o tipo do legado e o cabeçalho
            # mantém o tipo que a migração definiu.
            self.capas_tipo_divergente += 1

        cabecalho = {
            "movement_id": int(linha[0]),
            "service_order_id": _int_or_none(linha[1]),
            "movement_type_id": int(movimento_tipo_id),
            "shipment_id": _int_or_none(linha[3]),
        }
        self.cache_cabecalhos[chave] = cabecalho
        return cabecalho

    def _atualizar_cliente_cabecalho(self, conn, row, mov_id):
        """Atualiza o cliente do movimento para o cliente do legado.

        O movimento do legado possui um único `cliente_id` para todos os seus
        itens, portanto o cabeçalho do refatorado também deve ter um único
        cliente — mesmo quando o movimento cobre vários equipamentos.

        A capa do refatorado é duplicada em `movements` e `service_orders`
        (a migração grava o mesmo cliente nos dois). Atualizar só `movements`
        deixaria o histórico de devolução apontando para o cliente errado.
        `updated_at` não é tocado de propósito: a comparação de recência do
        diagnóstico usa `movements.updated_at`.
        """
        cliente_esperado = _int_or_none(row["id_customer_esperado"])
        if cliente_esperado is None:
            return False

        atual = conn.execute(
            text("SELECT recipient_customer_id FROM movements WHERE id = :mid"),
            {"mid": mov_id},
        ).scalar()
        if _int_or_none(atual) == cliente_esperado:
            return False

        conn.execute(
            text(
                """
                UPDATE movements
                SET recipient_customer_id = :customer_id
                WHERE id = :mid
                """
            ),
            {"customer_id": cliente_esperado, "mid": mov_id},
        )
        conn.execute(
            text(
                """
                UPDATE service_orders so
                INNER JOIN movements m ON m.service_order_id = so.id
                SET so.recipient_customer_id = :customer_id
                WHERE m.id = :mid
                """
            ),
            {"customer_id": cliente_esperado, "mid": mov_id},
        )
        return True

    def _dados_comuns(self, row):
        """Campos compartilhados entre cabeçalho, itens e histórico."""
        org_id = _int_or_none(row["organization_id_novo"])
        address_id = _int_or_none(row["address_id_esperado"]) or _int_or_none(
            row["address_id_novo"]
        )
        usuario_legado = _int_or_none(row["usuario_legado"])
        user_id = usuario_legado if usuario_legado in self.usuarios_validos else 1
        data_mov = _data_str(row["data_legado"]) or datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        alias = _str_or_none(row["equipamento_novo"]) or _str_or_none(
            row["equipamento_legado"]
        )
        return {
            "org_id": org_id,
            "address_id": address_id,
            "user_id": user_id,
            "data_mov": data_mov,
            "alias": alias,
            "detalhe": "Ajuste de migração individual (ajustes_movimentos)",
        }

    def _inserir_cabecalho(self, conn, row, numero_legado, movimento_tipo_id):
        """Insere a capa do movimento (service_order + movement + shipment)."""
        customer_id = _int_or_none(row["id_customer_esperado"])
        if customer_id is None:
            raise ValueError(
                "cliente legado sem de/para no refatorado; movimento não pode ser inserido"
            )

        dados = self._dados_comuns(row)
        data_mov = dados["data_mov"]
        status_shipment = 2

        # 1) Service Order
        res = conn.execute(
            text(
                """
                INSERT INTO service_orders
                    (status_id, movement_type_id, contract_id, user_id,
                     destination_order_id, mode_transport_id, organization_id,
                     recipient_customer_id, deadline, details, situation,
                     changed_by, created_at, updated_at, deleted_at)
                VALUES
                    (3, :movimento_tipo_id, NULL, :user_id,
                     NULL, 1, :org_id,
                     :customer_id, :data_mov, :detalhe, 'approved',
                     :user_id, :data_mov, :data_mov, NULL)
                """
            ),
            {
                "movimento_tipo_id": movimento_tipo_id,
                "user_id": dados["user_id"],
                "org_id": dados["org_id"],
                "customer_id": customer_id,
                "data_mov": data_mov,
                "detalhe": dados["detalhe"],
            },
        )
        service_order_id = int(res.lastrowid)

        # 2) Movement
        res = conn.execute(
            text(
                """
                INSERT INTO movements
                    (number, movement_date, service_order_id,
                     recipient_customer_id, migrate_customer_id, organization_id,
                     status_id, created_by, details, maintenance_created_movement,
                     created_at, updated_at, deleted_at)
                VALUES
                    (:numero, :data_mov, :service_order_id,
                     :customer_id, NULL, :org_id,
                     3, :user_id, :detalhe, 0,
                     :data_mov, :data_mov, NULL)
                """
            ),
            {
                "numero": str(numero_legado),
                "data_mov": data_mov,
                "service_order_id": service_order_id,
                "customer_id": customer_id,
                "org_id": dados["org_id"],
                "user_id": dados["user_id"],
                "detalhe": dados["detalhe"],
            },
        )
        movement_id = int(res.lastrowid)

        # 3) Shipment
        res = conn.execute(
            text(
                """
                INSERT INTO shipments
                    (status_id, requires_invoice, created_by,
                     created_at, updated_at, deleted_at)
                VALUES
                    (:status_shipment, 0, :user_id,
                     :data_mov, :data_mov, NULL)
                """
            ),
            {
                "status_shipment": status_shipment,
                "user_id": dados["user_id"],
                "data_mov": data_mov,
            },
        )
        shipment_id = int(res.lastrowid)

        conn.execute(
            text(
                """
                INSERT INTO shipment_movements
                    (shipment_id, movement_id, created_at, updated_at, deleted_at)
                VALUES
                    (:shipment_id, :movement_id, :data_mov, :data_mov, NULL)
                """
            ),
            {
                "shipment_id": shipment_id,
                "movement_id": movement_id,
                "data_mov": data_mov,
            },
        )

        cabecalho = {
            "movement_id": movement_id,
            "service_order_id": service_order_id,
            "movement_type_id": int(movimento_tipo_id),
            "shipment_id": shipment_id,
        }
        self.cache_cabecalhos[(int(numero_legado), int(movimento_tipo_id))] = cabecalho
        return cabecalho

    def _itens_movimento_legado(self, id_movimento):
        """Ids dos equipamentos de um movimento legado, na ordem de inserção."""
        if not hasattr(self, "cache_itens_legado"):
            self.cache_itens_legado = {}
        chave = int(id_movimento)
        if chave not in self.cache_itens_legado:
            with self.engine_legado.connect() as conn:
                self.cache_itens_legado[chave] = [
                    int(linha[0])
                    for linha in conn.execute(
                        text(
                            """
                            SELECT equipamento_id
                            FROM aluguel_movimento_itens
                            WHERE movimento_id = :movimento_id
                              AND deleted_at IS NULL
                            ORDER BY id
                            """
                        ),
                        {"movimento_id": chave},
                    )
                ]
        return self.cache_itens_legado[chave]

    def _equipamento_retorno_substituicao(self, row):
        """Equipamento devolvido no par SD de um movimento SA do legado.

        No refatorado o item da OS de uma substituição guarda o equipamento a
        retornar, e o item do movimento guarda o equipamento entregue. Sem o
        par SD — ou quando o devolvido não existe/no refatorado — devolve
        ``None``, para a tela não exibir o próprio entregue como retorno.
        """
        if (self._tipo_legado(row)) != "SA":
            return None

        id_movimento = _int_or_none(row["id_movimento_legado"])
        entregue = _int_or_none(row["id_equipamento"])
        if id_movimento is None or entregue is None:
            return None

        if not hasattr(self, "cache_retorno_substituicao"):
            self.cache_retorno_substituicao = {}
        chave = (int(id_movimento), int(entregue))
        if chave in self.cache_retorno_substituicao:
            return self.cache_retorno_substituicao[chave]

        with self.engine_legado.connect() as conn:
            id_sd = conn.execute(
                text(
                    """
                    SELECT substituicao_aluguel_id
                    FROM aluguel_substituicao
                    WHERE substituicao_devolucao_id = :id_sa
                    ORDER BY id
                    LIMIT 1
                    """
                ),
                {"id_sa": id_movimento},
            ).scalar()

        devolvido = None
        if id_sd is not None:
            devolvido = parear_equipamento_retorno(
                self._itens_movimento_legado(id_movimento),
                self._itens_movimento_legado(id_sd),
                entregue,
            )
            if devolvido is not None:
                with self.engine_new.connect() as conn:
                    existe = conn.execute(
                        text(
                            """
                            SELECT 1
                            FROM equipments
                            WHERE id = :equipment_id
                              AND deleted_at IS NULL
                            """
                        ),
                        {"equipment_id": devolvido},
                    ).fetchone()
                if not existe:
                    devolvido = None

        self.cache_retorno_substituicao[chave] = devolvido
        return devolvido

    @staticmethod
    def _tipo_legado(row):
        return (_str_or_none(row["tipo_legado"]) or "A").upper()

    def _inserir_itens(self, conn, row, cabecalho, movimento_tipo_id):
        """Insere os itens individuais (service_order_item, movement_item,
        shipment_item) de um equipamento na capa informada.

        Retorna o id do movement_item criado.
        """
        id_equipamento = int(row["id_equipamento"])
        # Na substituição o item da OS carrega o equipamento a RETORNAR; o
        # item do movimento carrega o entregue. Para os demais tipos os dois
        # são o mesmo equipamento.
        equipment_id_os = (
            self._equipamento_retorno_substituicao(row)
            if self._tipo_legado(row) == "SA"
            else id_equipamento
        )
        dados = self._dados_comuns(row)
        data_mov = dados["data_mov"]
        status_shipment = 2
        _, operacao = MAPA_TIPO_MOVIMENTO.get(
            (_str_or_none(row["tipo_legado"]) or "A").upper(), (1, "ALUGUEL")
        )

        service_order_id = cabecalho["service_order_id"]
        if service_order_id is None:
            raise ValueError("movimento existente sem service_order vinculada")

        # 1) Service Order Item
        res = conn.execute(
            text(
                """
                INSERT INTO service_order_items
                    (status_id, service_order_id, department_id, movement_type_id,
                     contract_item_id, alias, equipment_id, type_id, product_id,
                     is_exchange, is_extra, quantity_product,
                     fulfilled_quantity_product, quantity, details, address_id,
                     location_id, created_at, updated_at, deleted_at)
                VALUES
                    (3, :service_order_id, 2, :movimento_tipo_id,
                     NULL, :alias, :equipment_id, NULL, NULL,
                     0, 0, NULL,
                     0, 1, :detalhe, :address_id,
                     NULL, :data_mov, :data_mov, NULL)
                """
            ),
            {
                "service_order_id": service_order_id,
                "movimento_tipo_id": movimento_tipo_id,
                "alias": dados["alias"],
                "equipment_id": equipment_id_os,
                "detalhe": dados["detalhe"],
                "address_id": dados["address_id"],
                "data_mov": data_mov,
            },
        )
        service_order_item_id = int(res.lastrowid)

        # 2) Movement Item
        res = conn.execute(
            text(
                """
                INSERT INTO movement_items
                    (movement_id, movement_type_id, service_order_item_id,
                     equipment_id, extra_id, status_id, product_item_id, alias,
                     old_organization_id, new_organization_id, operation_type,
                     confirmed_at, confirmed_by, created_at, updated_at, deleted_at)
                VALUES
                    (:movement_id, :movimento_tipo_id, :service_order_item_id,
                     :equipment_id, NULL, 3, NULL, :alias,
                     NULL, :org_id, :operacao,
                     NULL, NULL, :data_mov, :data_mov, NULL)
                """
            ),
            {
                "movement_id": cabecalho["movement_id"],
                "movimento_tipo_id": movimento_tipo_id,
                "service_order_item_id": service_order_item_id,
                "equipment_id": id_equipamento,
                "alias": dados["alias"],
                "org_id": dados["org_id"],
                "operacao": operacao,
                "data_mov": data_mov,
            },
        )
        movement_item_id = int(res.lastrowid)

        # 3) Shipment Item
        res = conn.execute(
            text(
                """
                INSERT INTO shipment_items
                    (shipment_id, status_id, movement_item_id, volume_id,
                     details, address_id, delivery_date, created_at, updated_at)
                VALUES
                    (:shipment_id, :status_shipment, :movement_item_id, NULL,
                     :detalhe, :address_id, :delivery_date, :data_mov, :data_mov)
                """
            ),
            {
                "shipment_id": cabecalho["shipment_id"],
                "status_shipment": status_shipment,
                "movement_item_id": movement_item_id,
                "detalhe": dados["detalhe"],
                "address_id": dados["address_id"],
                "delivery_date": data_mov[:10],
                "data_mov": data_mov,
            },
        )
        shipment_item_id = int(res.lastrowid)

        row["_shipment_item_id"] = shipment_item_id
        row["_service_order_item_id"] = service_order_item_id
        return movement_item_id

    def _atualizar_equipamento(self, conn, row, item_id, forcar_status_legado):
        id_equipamento = int(row["id_equipamento"])

        # Equipamento baixado/excluido no banco novo nao pode ter status,
        # endereco nem cliente reaplicados pelo ajuste de movimentos.
        excluido = conn.execute(
            text("SELECT deleted_at FROM equipments WHERE id = :equipment_id"),
            {"equipment_id": id_equipamento},
        ).scalar()
        if excluido is not None:
            return None, None

        status_esperado = _int_or_none(row["status_esperado_id"])
        status_atual = _int_or_none(row["status_id_novo"])

        # Por padrão, respeita as exceções de negócio (cliente de estoque etc.).
        # Com --forcar-status-legado, aplica o status do legado em qualquer caso.
        if forcar_status_legado or row["motivo_excecao"] == "":
            status_depois = status_esperado if status_esperado is not None else status_atual
        else:
            status_depois = status_atual

        address_id = _int_or_none(row["address_id_esperado"])
        if address_id is None:
            address_id = _int_or_none(row["address_id_novo"])

        conn.execute(
            text(
                """
                UPDATE equipments
                SET status_id = :status_id,
                    address_id = COALESCE(:address_id, address_id),
                    last_movement_item_customer_id = COALESCE(
                        :item_id, last_movement_item_customer_id
                    )
                WHERE id = :equipment_id
                  AND deleted_at IS NULL
                """
            ),
            {
                "status_id": status_depois,
                "address_id": address_id,
                "item_id": item_id,
                "equipment_id": id_equipamento,
            },
        )

        # Cliente efetivo depois da correção: o do movimento que ficou como
        # último (se houve inserção/reutilização) ou o cliente atual.
        cliente_depois = _int_or_none(row["id_customer_esperado"])
        if cliente_depois is None:
            cliente_depois = _int_or_none(row["id_customer_novo"])

        return status_depois, cliente_depois

    def _gravar_historico(self, conn, row, movement_id, item_id, status_depois):
        if status_depois is None:
            return

        ocorrido = _data_str(row["data_legado"]) or datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        usuario_legado = _int_or_none(row["usuario_legado"])
        user_id = usuario_legado if usuario_legado in self.usuarios_validos else 1

        existente = conn.execute(
            text(
                """
                SELECT 1
                FROM equipment_history
                WHERE equipment_id = :equipment_id
                  AND reason = 'ADJUST_MOVEMENT_INDIVIDUAL'
                  AND movement_item_id <=> :movement_item_id
                LIMIT 1
                """
            ),
            {
                "equipment_id": int(row["id_equipamento"]),
                "movement_item_id": item_id,
            },
        ).fetchone()
        if existente:
            return

        conn.execute(
            text(
                """
                INSERT INTO equipment_history
                    (equipment_id, status_id, occurred_at, movement_item_id,
                     service_order_item_id, contract_item_id, shipment_item_id,
                     is_conversion, reason, user_id)
                VALUES
                    (:equipment_id, :status_id, :ocorrido, :movement_item_id,
                     :service_order_item_id, NULL, :shipment_item_id,
                     0, 'ADJUST_MOVEMENT_INDIVIDUAL', :user_id)
                """
            ),
            {
                "equipment_id": int(row["id_equipamento"]),
                "status_id": status_depois,
                "ocorrido": ocorrido,
                "movement_item_id": item_id,
                "service_order_item_id": _int_or_none(row.get("_service_order_item_id")),
                "shipment_item_id": _int_or_none(row.get("_shipment_item_id")),
                "user_id": user_id,
            },
        )

    # --------------------------------------------------------------------------
    # ORQUESTRAÇÃO
    # --------------------------------------------------------------------------
    def executar(
        self, aplicar=False, limite=None, forcar_status_legado=False, movimentos=None
    ):
        df, df_div = self.comparar()
        self.salvar_divergencias(df_div, ARQ_DIVERGENCIAS)

        if not aplicar:
            print(
                "\nℹ️  Modo diagnóstico. Nenhuma alteração foi feita no banco.\n"
                "   Para corrigir as divergências elegíveis, rode com --aplicar."
            )
            return

        self.salvar_duplicados_pendentes(df_div)
        self.aplicar(
            df,
            limite=limite,
            forcar_status_legado=forcar_status_legado,
            movimentos=movimentos,
        )

        print("\n🔁 Revalidando divergências após a correção...")
        _, df_div_pos = self.comparar()
        self.salvar_divergencias(df_div_pos, ARQ_DIVERGENCIAS_POS)

        antes = len(df_div)
        depois = len(df_div_pos)
        print("\n" + "=" * 78)
        print("📊 COMPARATIVO ANTES x DEPOIS")
        print("=" * 78)
        print(f"   Divergências antes:  {antes:,}")
        print(f"   Divergências depois: {depois:,}")
        print(f"   Redução:             {antes - depois:,}")
        print("=" * 78)


# ==============================================================================
# ENTRYPOINTS
# ==============================================================================
def main(argv=None, engines=None):
    parser = argparse.ArgumentParser(
        description="Ajustes finais de movimentos/status (legado x refatorado)."
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="Aplica as correções elegíveis (um INSERT/UPDATE por equipamento).",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Limita a quantidade de equipamentos corrigidos (útil para teste).",
    )
    parser.add_argument(
        "--incluir-parquet",
        action="store_true",
        help="Não protege os equipamentos presentes em docs/parquets (EQUIPAMENTO_ID).",
    )
    parser.add_argument(
        "--forcar-status-legado",
        action="store_true",
        help="Aplica o status do legado mesmo em exceções (ex.: cliente de estoque).",
    )
    parser.add_argument(
        "--movimento",
        type=int,
        nargs="+",
        default=None,
        metavar="ID",
        help="Restringe a correção aos IDs de movimento do legado informados.",
    )
    args = parser.parse_args(argv)

    if engines is None:
        eng_novo, eng_legado = obter_engines()
    else:
        eng_novo, eng_legado = engines

    app = AjustesMovimentos(
        eng_novo,
        eng_legado,
        ignorar_parquet=not args.incluir_parquet,
    )
    app.executar(
        aplicar=args.aplicar,
        limite=args.limite,
        forcar_status_legado=args.forcar_status_legado,
        movimentos=args.movimento,
    )


def executar(eng_novo, eng_legado, argumentos_cli=None):
    """Wrapper usado pelo main.py."""
    main(argv=argumentos_cli or [], engines=(eng_novo, eng_legado))


if __name__ == "__main__":
    main()