"""Exceções ao escudo do Parquet.

O Parquet é a fonte do aluguel vigente e, por padrão, os tombos presentes nele
são intocáveis nos módulos de migração e de ajuste: o aluguel é gerado a partir
do Parquet e o status/endereço desses equipamentos não é sobrescrito pelo
legado.

Este registro existe para os casos pontuais em que o dado do Parquet está
errado ou desatualizado para um equipamento e o **legado deve prevalecer**.

A chave é o id do equipamento no LEGADO (``aluguel_equipamentos.id``). O tombo
é resolvido no legado em tempo de execução, para não guardar aqui um número que
pode mudar (o tombo se repete no legado).
"""

from sqlalchemy import text

IDS_LEGADO_PRIORIZADOS = {
    44764: (
        "Tombo 47613: o Parquet aponta aluguel AVULSO de ELEICOES 2024 (2022), "
        "enquanto o legado registra devolução em 2026-04-24 para "
        "P M ICÓ / SECRETARIA DE ADMINISTRAÇÃO E FINANÇAS / TRIBUTOS. "
        "O legado prevalece."
    ),
}


def normalizar_tombo(valor):
    """Normaliza tombo para comparação (710 e 710.0 -> '710')."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    if texto.endswith(".0"):
        texto = texto[:-2]
    return texto


def ids_priorizados():
    return set(IDS_LEGADO_PRIORIZADOS)


def equipamentos_priorizados():
    """IDs de equipamento do legado que ignoram o escudo do Parquet.

    A coluna ``EQUIPAMENTO_ID`` do Parquet é o id do equipamento no legado, ou
    seja, a mesma chave de ``IDS_LEGADO_PRIORIZADOS``. Usar o id evita que a
    exceção seja estendida a outros equipamentos que repetem o tombo.
    """
    return set(IDS_LEGADO_PRIORIZADOS)


def id_priorizado(id_legado):
    """True quando o id do legado está na lista de exceções."""
    if id_legado is None:
        return False
    try:
        return int(id_legado) in IDS_LEGADO_PRIORIZADOS
    except (TypeError, ValueError):
        return False


def motivo_prioridade(id_legado):
    if not id_priorizado(id_legado):
        return None
    return IDS_LEGADO_PRIORIZADOS[int(id_legado)]


def tombos_priorizados(engine_legado):
    """Tombos (``aluguel_equipamentos.numero``) dos equipamentos priorizados.

    Lido do legado para manter uma única fonte de verdade: se o número mudar,
    o escudo do Parquet continua sendo ignorado para o equipamento certo.
    """
    ids = sorted(IDS_LEGADO_PRIORIZADOS)
    if not ids:
        return set()

    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": valor for i, valor in enumerate(ids)}
    with engine_legado.connect() as conn:
        linhas = conn.execute(
            text(
                "SELECT numero FROM aluguel_equipamentos "
                f"WHERE id IN ({placeholders})"
            ),
            params,
        ).fetchall()

    return {normalizar_tombo(linha[0]) for linha in linhas if normalizar_tombo(linha[0])}


def remover_priorizados(tombos, engine_legado):
    """Remove do conjunto de tombos protegidos os que estão priorizados."""
    if not tombos:
        return set(tombos or ())
    excecoes = tombos_priorizados(engine_legado)
    if not excecoes:
        return {normalizar_tombo(t) for t in tombos}
    return {normalizar_tombo(t) for t in tombos if normalizar_tombo(t) not in excecoes}
