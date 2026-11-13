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
    CLIENTES_BLOQUEADOS,
    CLIENTES_ESTOQUE,
    FALSOS_RESERVAS,
)
from config.database import obter_engines


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
    addressable_id AS id_customer
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
    """Separa linhas com tombo repetido no banco migrado das ajustaveis.

    A duplicidade considerada e a refletida na migracao (copias do mesmo
    tombo entre equipamentos migrados). O conteudo do legado fica apenas
    como diagnostico, pois insumos podem repetir o tombo sem virar equipamento.
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


def identificar_reserva_mesclada(df, reservas_mapeadas=None):
    """Marca equipamentos alugados no legado para clientes de RESERVA.

    A migracao de clientes mescla esses clientes com o titular e transforma o
    aluguel em reserva. Logo, legado ALUGADO x refatorado RESERVADO e o
    comportamento esperado, nao uma divergencia.

    A reserva pode ser identificada pelo nome (RESERVA/RESERVADO) ou pelo
    de/para registrado em addresses.reserved_customer_id.
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
    return (por_nome | por_mescla) & (situacao == 1) & (status_novo == 3)


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

        self.tombos_parquet = self._carregar_tombos_parquet() if ignorar_parquet else set()
        # (numero_legado, movement_type_id) -> dados da capa criada/reutilizada
        self.cache_cabecalhos = {}

    # --------------------------------------------------------------------------
    # PARQUET: tombos que o pipeline já tratou e não devem ser sobrescritos
    # --------------------------------------------------------------------------
    def _carregar_tombos_parquet(self):
        arquivos = glob.glob(os.path.join(PASTA_PARQUETS, "*.parquet"))
        if not arquivos:
            print(f"⚠️  Nenhum Parquet em {PASTA_PARQUETS}; proteção de tombos desativada.")
            return set()

        try:
            frames = []
            for arquivo in arquivos:
                df = pd.read_parquet(arquivo)
                df.columns = df.columns.str.upper()
                frames.append(df)

            df = pd.concat(frames, ignore_index=True)
            if "TOMBO" not in df.columns:
                return set()

            df["TOMBO"] = pd.to_numeric(df["TOMBO"], errors="coerce")
            df = df.dropna(subset=["TOMBO"])
            tombos = {
                str(int(valor)) for valor in df["TOMBO"].astype("int64").tolist()
            }
            print(f"🛡️  {len(tombos)} tombos do Parquet serão protegidos.")
            return tombos
        except Exception as exc:
            print(f"⚠️  Não foi possível ler os Parquets ({exc}). Seguindo sem proteção.")
            return set()

    # --------------------------------------------------------------------------
    # CARGA
    # --------------------------------------------------------------------------
    def _carregar_mapas_clientes(self, conn):
        df = pd.read_sql(text(QUERY_MAPA_CLIENTES), conn)
        if df.empty:
            return {}, {}, {}
        mapa_customer = dict(zip(df["legacy_customer_id"].astype(int), df["id_customer"].astype(int)))
        mapa_address = dict(zip(df["legacy_customer_id"].astype(int), df["address_id"].astype(int)))
        df_reservas = pd.read_sql(text(QUERY_MAPA_RESERVAS), conn)
        mapa_reservas = (
            dict(zip(df_reservas["legacy_reserva_id"].astype(int),
                     df_reservas["id_customer"].astype(int)))
            if not df_reservas.empty else {}
        )
        return mapa_customer, mapa_address, mapa_reservas

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
            mapa_customer, mapa_address, mapa_reservas = self._carregar_mapas_clientes(conn)

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
        def motivo_excecao(row):
            cliente = _int_or_none(row["id_cliente_legado"])
            situacao = _int_or_none(row["situacao_id"])

            # Aluguel para cliente de RESERVA: a migracao mescla o cliente com
            # o titular e o equipamento passa a RESERVADO. Regra esperada.
            if bool(row.get("reserva_mesclada")):
                return "RESERVA_MESCLADA"

            # Equipamentos alugados para clientes internos de estoque/base são
            # liberados para locação (status 1) pela própria migração.
            if cliente in CLIENTES_ESTOQUE and situacao == 1:
                return "CLIENTE_ESTOQUE"

            if cliente in CLIENTES_BLOQUEADOS:
                return "CLIENTE_BLOQUEADO"

            tombo = _str_or_none(row["tombo_novo"])
            if tombo and tombo in self.tombos_parquet:
                return "PARQUET"

            if row["classificacao"] == "NOVO_MAIS_RECENTE":
                return "NOVO_MAIS_RECENTE"

            if row["classificacao"] == "LEGADO_SEM_MOVIMENTO_NOVO_TEM":
                return "LEGADO_SEM_MOVIMENTO"

            if (
                row["classificacao"] == "REINSERIR_MOVIMENTO"
                and _int_or_none(row["id_customer_esperado"]) is None
            ):
                return "CLIENTE_SEM_MAPA"

            return ""

        df["motivo_excecao"] = df.apply(motivo_excecao, axis=1)

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
                return "Atualizar status/endereço do equipamento"
            if row["motivo_excecao"] == "NOVO_MAIS_RECENTE":
                return "Revisar manualmente: o refatorado tem movimento mais recente"
            if row["motivo_excecao"] == "LEGADO_SEM_MOVIMENTO":
                return "Revisar manualmente: legado sem movimento"
            if row["motivo_excecao"] == "CLIENTE_ESTOQUE":
                return "Exceção: cliente interno de estoque (status 1)"
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
        mask_divergencia = (
            df["div_status"]
            | df["div_cliente"]
            | df["div_movimento"]
            | df["cliente_sem_mapa"]
            | df["classificacao"].isin(
                ["NOVO_MAIS_RECENTE", "LEGADO_SEM_MOVIMENTO_NOVO_TEM"]
            )
        ) & ~df["reserva_mesclada"]
        df_div = df[mask_divergencia].copy()

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

        self._imprimir_resumo(df, df_div)
        self._salvar_resumo(df, df_div)
        return df, df_div

    # --------------------------------------------------------------------------
    # RESUMO / ARQUIVOS
    # --------------------------------------------------------------------------
    def _imprimir_resumo(self, df, df_div):
        total = len(df)
        print("\n" + "-" * 78)
        print(f"📦 Equipamentos analisados: {total:,}")
        print(f"⚠️  Linhas com alguma divergência: {len(df_div):,}")
        print(f"   • Status divergente:   {int(df['div_status'].sum()):,}")
        print(f"   • Cliente divergente:  {int(df['div_cliente'].sum()):,}")
        print(f"   • Movimento ausente/diferente: {int(df['div_movimento'].sum()):,}")
        com_copias = df_div[df_div["copias_tombo_refatorado"] > 1]
        print(f"   • Tombo duplicado na migração (ignorados no ajuste): {len(com_copias):,}")
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

    def _salvar_resumo(self, df, df_div):
        linhas = []

        linhas.append({"tipo_resumo": "TOTAL_ANALISADO", "chave": "", "quantidade": len(df)})
        linhas.append(
            {"tipo_resumo": "TOTAL_DIVERGENCIAS", "chave": "", "quantidade": len(df_div)}
        )
        linhas.append(
            {"tipo_resumo": "STATUS_DIVERGENTE", "chave": "", "quantidade": int(df["div_status"].sum())}
        )
        linhas.append(
            {"tipo_resumo": "CLIENTE_DIVERGENTE", "chave": "", "quantidade": int(df["div_cliente"].sum())}
        )
        linhas.append(
            {"tipo_resumo": "MOVIMENTO_DIVERGENTE", "chave": "", "quantidade": int(df["div_movimento"].sum())}
        )
        linhas.append(
            {"tipo_resumo": "CORRIGIVEIS", "chave": "", "quantidade": int(df["corrigivel"].sum())}
        )
        com_copias = df_div[df_div["copias_tombo_refatorado"] > 1]
        linhas.append(
            {"tipo_resumo": "TOMBOS_COM_COPIAS", "chave": "", "quantidade": len(com_copias)}
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
        base_status = df[df["div_status"]].copy()
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
        """Registra os equipamentos com tombo repetido que o ajuste ignorou."""
        _, duplicados = separar_tombo_duplicado(df_div)
        if not duplicados.empty:
            duplicados = duplicados.assign(
                _tombo_ordem=pd.to_numeric(duplicados["tombo_novo"], errors="coerce")
            ).sort_values(
                ["status_tombo_refatorado", "_tombo_ordem", "id_equipamento"]
            ).drop(columns="_tombo_ordem")
        duplicados.to_csv(caminho, index=False, encoding="utf-8-sig")
        print(
            f"📄 {len(duplicados):,} equipamentos com tombo duplicado na migração "
            f"foram ignorados e salvos em: {caminho}"
        )
        return duplicados

    # --------------------------------------------------------------------------
    # CORREÇÃO INDIVIDUAL (SEM BULK INSERT)
    # --------------------------------------------------------------------------
    def _selecionar_elegiveis(self, df, forcar_status_legado=False):
        """Linhas corrigíveis. Com --forcar-status-legado também inclui as
        exceções de cliente interno/bloqueado (nunca Parquet ou movimento mais
        recente no refatorado). Tombos repetidos no legado são sempre
        ignorados: ficam pendentes de revisão manual."""
        df_ajustavel, _ = separar_tombo_duplicado(df)
        mask = df_ajustavel["corrigivel"]
        if forcar_status_legado:
            forcaveis = (
                df_ajustavel["classificacao"].isin(
                    ["REINSERIR_MOVIMENTO", "CORRIGIR_EQUIPAMENTO", "SEM_MOVIMENTO_AMBOS"]
                )
                & df_ajustavel["motivo_excecao"].isin(["CLIENTE_ESTOQUE", "CLIENTE_BLOQUEADO"])
            )
            mask = mask | forcaveis
        return df_ajustavel[mask].copy()

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
            # Número reaproveitado para outro tipo de operação: não misturar.
            return None

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

    def _inserir_itens(self, conn, row, cabecalho, movimento_tipo_id):
        """Insere os itens individuais (service_order_item, movement_item,
        shipment_item) de um equipamento na capa informada.

        Retorna o id do movement_item criado.
        """
        id_equipamento = int(row["id_equipamento"])
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
                "equipment_id": id_equipamento,
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
        help="Não protege os tombos presentes em docs/parquets.",
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