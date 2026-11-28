#!/usr/bin/env python3
"""Fase 0 — extracao dos candidatos e dos sinais de decisao (somente leitura).

Le `controle-interno` e grava `out/candidatos.csv` (1 linha por copia) e
`out/revisao_extracao.md`. Nao escreve no banco.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import bindparam, text

try:
    from . import comum
except ImportError:  # execucao direta do script
    import comum


def _sql_in(comando: str):
    return text(comando).bindparams(bindparam("ids", expanding=True))


def _ids(linhas) -> list[int]:
    return [int(linha["id"]) for linha in linhas]


def _carregar_movimentos(conn, ids: list[int]) -> tuple[dict, dict]:
    """{id: {qtd, cliente, operacao}} e {id: ultimo_movimento}."""
    if not ids:
        return {}, {}

    agregado = {
        int(linha[0]): {
            "qtd": int(linha[1]),
            "ultimo": linha[2],
        }
        for linha in conn.execute(
            _sql_in(
                """
                SELECT mi.equipment_id, COUNT(*), MAX(m.movement_date)
                FROM movement_items mi
                JOIN movements m ON m.id = mi.movement_id AND m.deleted_at IS NULL
                WHERE mi.deleted_at IS NULL AND mi.equipment_id IN :ids
                GROUP BY mi.equipment_id
                """
            ),
            {"ids": tuple(ids)},
        ).all()
    }

    ultimo = {
        int(linha[0]): {
            "cliente": linha[2],
            "operacao": linha[3],
        }
        for linha in conn.execute(
            _sql_in(
                """
                SELECT equipment_id, movement_id, recipient_customer_id, operation_type
                FROM (
                    SELECT mi.equipment_id, mi.movement_id, m.recipient_customer_id,
                           mi.operation_type,
                           ROW_NUMBER() OVER (
                               PARTITION BY mi.equipment_id
                               ORDER BY m.movement_date DESC, mi.id DESC
                           ) AS rn
                    FROM movement_items mi
                    JOIN movements m ON m.id = mi.movement_id AND m.deleted_at IS NULL
                    WHERE mi.deleted_at IS NULL AND mi.equipment_id IN :ids
                ) x
                WHERE rn = 1
                """
            ),
            {"ids": tuple(ids)},
        ).all()
    }
    return agregado, ultimo


def _carregar_refs(conn, ids: list[int]) -> tuple[dict, dict]:
    """Conta refs ativas por equipamento e as linhas de entrada do historico."""
    refs: dict[int, dict[str, int]] = {
        i: {f"refs_{tabela}": 0 for tabela, _, _ in comum.TABELAS_REF}
        for i in ids
    }

    for tabela, coluna, tem_deleted in comum.TABELAS_REF:
        filtro = " AND deleted_at IS NULL" if tem_deleted else ""
        linhas = conn.execute(
            _sql_in(
                f"SELECT {coluna}, COUNT(*) FROM {tabela} "
                f"WHERE {coluna} IN :ids{filtro} GROUP BY {coluna}"
            ),
            {"ids": tuple(ids)},
        ).all()
        for equipamento_id, total in linhas:
            chave = int(equipamento_id)
            if chave in refs:
                refs[chave][f"refs_{tabela}"] = int(total)

    # related_equipments: sem id/deleted_at e com unique (new, old).
    relacionados = conn.execute(
        _sql_in(
            "SELECT new_equipment_id, old_equipment_id, "
            "       (SELECT COUNT(*) FROM related_equipments r2 "
            "         WHERE r2.new_equipment_id = r.new_equipment_id "
            "           AND r2.old_equipment_id = r.old_equipment_id) "
            "FROM related_equipments r "
            "WHERE new_equipment_id IN :ids OR old_equipment_id IN :ids"
        ),
        {"ids": tuple(ids)},
    ).all()
    for novo, antigo, _ in relacionados:
        for chave in {int(novo), int(antigo)}:
            if chave in refs:
                refs[chave]["refs_related_equipments"] += 1

    entradas = conn.execute(
        _sql_in(
            "SELECT equipment_id, COUNT(*) FROM equipment_history "
            "WHERE equipment_id IN :ids AND deleted_at IS NULL AND reason = :reason "
            "GROUP BY equipment_id"
        ),
        {"ids": tuple(ids), "reason": comum.REASON_ENTRADA},
    ).all()
    return refs, {int(e): int(t) for e, t in entradas}


def _carregar_os_aberta(conn, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return {
        int(linha)
        for linha in conn.execute(
            _sql_in(
                """
                SELECT DISTINCT soi.equipment_id
                FROM service_order_items soi
                JOIN service_orders so ON so.id = soi.service_order_id
                                      AND so.deleted_at IS NULL
                WHERE soi.deleted_at IS NULL
                  AND so.status_id IN (1, 2)
                  AND soi.equipment_id IN :ids
                """
            ),
            {"ids": tuple(ids)},
        ).scalars()
    }


def _carregar_manutencao_aberta(conn, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return {
        int(linha)
        for linha in conn.execute(
            _sql_in(
                "SELECT DISTINCT equipment_id FROM maintenances "
                "WHERE deleted_at IS NULL AND is_closed = 0 AND equipment_id IN :ids"
            ),
            {"ids": tuple(ids)},
        ).scalars()
    }


def _carregar_assinaturas_movimento(conn, ids: list[int]) -> list[tuple]:
    if not ids:
        return []
    return [
        (int(eq), int(mov), str(op or ""))
        for eq, mov, op in conn.execute(
            _sql_in(
                "SELECT equipment_id, movement_id, operation_type FROM movement_items "
                "WHERE deleted_at IS NULL AND movement_id IS NOT NULL "
                "AND equipment_id IN :ids"
            ),
            {"ids": tuple(ids)},
        ).all()
    ]


def _chave_grupo(linha: dict, escopo: str) -> tuple:
    tombo = comum.normalizar_int(linha["number"])
    if escopo == comum.ESCOPO_GLOBAL:
        return (tombo,)
    return (tombo, comum.normalizar_int(linha["current_organization_id"]))


def extrair(engine, escopo: str = comum.ESCOPO_ORGAO,
            pasta_saida: Path = comum.OUT, log=print) -> dict:
    if escopo == comum.ESCOPO_GLOBAL:
        particao = "eq.number"
    else:
        particao = "eq.number, eq.current_organization_id"

    with engine.connect() as conn:
        candidatos = conn.execute(
            text(
                f"""
                SELECT t.id, t.number, t.name, t.status_id, t.current_organization_id,
                       t.product_item_id, t.transaction_item_id, t.deleted_at,
                       t.created_at, t.serial_number, t.reserved_id, t.address_id,
                       t.location_id, t.repeticoes
                FROM (
                    SELECT eq.id, eq.number, eq.name, eq.status_id,
                           eq.current_organization_id, eq.product_item_id,
                           eq.transaction_item_id, eq.deleted_at, eq.created_at,
                           eq.serial_number, eq.reserved_id, eq.address_id,
                           eq.location_id,
                           COUNT(*) OVER (PARTITION BY {particao}) AS repeticoes
                    FROM equipments eq
                    WHERE eq.deleted_at IS NULL
                      AND eq.status_id <> 9
                      AND eq.number IS NOT NULL
                      AND UPPER(eq.name) NOT LIKE '%TONER%'
                      AND UPPER(eq.name) NOT LIKE '%CARTUCHO%'
                ) t
                WHERE t.repeticoes > 1
                ORDER BY t.current_organization_id, t.number, t.id
                """
            )
        ).mappings().all()
        candidatos = [dict(linha) for linha in candidatos]
        ids = _ids(candidatos)
        log(f"Candidatos ({escopo}): {len(candidatos)} linhas, {len(ids)} ids")

        movimentos, ultimo = _carregar_movimentos(conn, ids)
        refs, entradas = _carregar_refs(conn, ids)
        os_aberta = _carregar_os_aberta(conn, ids)
        manutencao_aberta = _carregar_manutencao_aberta(conn, ids)
        assinaturas = _carregar_assinaturas_movimento(conn, ids)

    priorizados = comum.carregar_priorizados()
    indice_parquet = comum.carregar_indice_parquet(log=log)

    # Sinais de grupo.
    grupos: dict[tuple, list[dict]] = {}
    for linha in candidatos:
        linha["_grupo"] = _chave_grupo(linha, escopo)
        grupos.setdefault(linha["_grupo"], []).append(linha)

    colisao_por_equipamento: dict[int, bool] = {}
    for chave, linhas in grupos.items():
        if len(linhas) < 2:
            continue
        vistos: dict[tuple, set[int]] = {}
        ids_grupo = {int(l["id"]) for l in linhas}
        for equipamento_id, movimento_id, operacao in assinaturas:
            if equipamento_id not in ids_grupo:
                continue
            vistos.setdefault((movimento_id, operacao), set()).add(equipamento_id)
        colidiu = any(len(conjunto) >= 2 for conjunto in vistos.values())
        for equipamento_id in ids_grupo:
            colisao_por_equipamento[equipamento_id] = colidiu

    for chave, linhas in grupos.items():
        total = len(linhas)
        nomes = {str(l["name"]).strip() for l in linhas}
        status = {comum.normalizar_int(l["status_id"]) for l in linhas}
        pis = {comum.normalizar_int(l["product_item_id"]) for l in linhas}
        ids_parquet = {int(l["id"]) for l in linhas if int(l["id"]) in indice_parquet}
        reservado = any(comum.normalizar_int(l["reserved_id"]) for l in linhas)
        tem_parquet = {int(l["id"]): int(l["id"]) in indice_parquet for l in linhas}

        for linha in linhas:
            equipamento_id = int(linha["id"])
            linha["escopo"] = escopo
            linha["status_nome"] = comum.STATUS_NOMES.get(
                comum.normalizar_int(linha["status_id"]), "")
            linha["deleted_at"] = linha["deleted_at"] or ""
            linha["created_at"] = linha["created_at"] or ""
            linha["cliente_ultimo"] = ultimo.get(equipamento_id, {}).get("cliente", "") or ""
            linha["operacao_ultima"] = ultimo.get(equipamento_id, {}).get("operacao", "") or ""
            linha["qtd_movimentos"] = movimentos.get(equipamento_id, {}).get("qtd", 0)
            linha["ultimo_movimento"] = comum.formatar_data(
                movimentos.get(equipamento_id, {}).get("ultimo"))
            linha.update(refs.get(equipamento_id, {}))
            linha["refs_equipment_history_entrada"] = entradas.get(equipamento_id, 0)
            linha["refs_total_repointar"] = (
                sum(valor for campo, valor in refs.get(equipamento_id, {}).items()
                    if campo != "refs_equipment_history")
                + max(linha["refs_equipment_history"]
                      - linha["refs_equipment_history_entrada"], 0)
            )
            linha["tem_parquet"] = "S" if tem_parquet[equipamento_id] else "N"
            linha["arquivo_parquet"] = "|".join(indice_parquet.get(equipamento_id, []))
            linha["priorizado"] = "S" if equipamento_id in priorizados else "N"
            linha["grupo_repeticoes"] = total
            linha["pi_divergente_no_grupo"] = "S" if len(pis) > 1 else "N"
            linha["name_divergente_no_grupo"] = "S" if len(nomes) > 1 else "N"
            linha["status_divergente_no_grupo"] = "S" if len(status) > 1 else "N"
            linha["tem_algum_alugado"] = "S" if 2 in status else "N"
            linha["qtd_ids_parquet_no_grupo"] = len(ids_parquet)
            linha["colisao_mesmo_movimento"] = (
                "S" if colisao_por_equipamento.get(equipamento_id) else "N")
            linha["em_os_aberta"] = "S" if equipamento_id in os_aberta else "N"
            linha["em_manutencao_aberta"] = (
                "S" if equipamento_id in manutencao_aberta else "N")
            linha["reservado_no_grupo"] = "S" if reservado else "N"

    comum.escrever_csv(pasta_saida / "candidatos.csv",
                       comum.CABECALHO_CANDIDATOS, candidatos)

    stats = _resumo(candidatos, grupos, escopo)
    _escrever_revisao(pasta_saida / "revisao_extracao.md", stats, grupos)
    return stats


def _resumo(candidatos, grupos, escopo) -> dict:
    def grupos_com(campo) -> int:
        # Sinais por equipamento (OS/manutencao) exigem `any`: um grupo conta
        # uma vez mesmo que so uma das copias tenha o sinal.
        return sum(
            1 for linhas in grupos.values()
            if any(comum.sim(l.get(campo)) for l in linhas))

    stats = {
        "escopo": escopo,
        "linhas": len(candidatos),
        "grupos": len(grupos),
        "excedentes": len(candidatos) - len(grupos),
        "grupos_name_divergente": grupos_com("name_divergente_no_grupo"),
        "grupos_pi_divergente": sum(
            1 for linhas in grupos.values()
            if any(comum.sim(l.get("pi_divergente_no_grupo")) for l in linhas)),
        "grupos_status_divergente": grupos_com("status_divergente_no_grupo"),
        "grupos_com_alugado": grupos_com("tem_algum_alugado"),
        "grupos_parquet": sum(
            1 for linhas in grupos.values()
            if any(comum.sim(l.get("tem_parquet")) for l in linhas)),
        "grupos_parquet_duplo": sum(
            1 for linhas in grupos.values()
            if (comum.normalizar_int(linhas[0].get("qtd_ids_parquet_no_grupo")) or 0) >= 2),
        "grupos_colisao": grupos_com("colisao_mesmo_movimento"),
        "grupos_manutencao_aberta": grupos_com("em_manutencao_aberta"),
        "grupos_os_aberta": grupos_com("em_os_aberta"),
        "ids_parquet": sum(1 for l in candidatos if comum.sim(l.get("tem_parquet"))),
    }
    return stats


def _escrever_revisao(caminho: Path, stats: dict, grupos: dict) -> None:
    linhas = [
        "# Diagnostico das duplicatas de equipamentos",
        "",
        f"Gerado em {comum.agora_iso()} (somente leitura).",
        "",
        "## Contagens",
        "",
        "| Metrica | Valor |",
        "|---|---:|",
        f"| Escopo | {stats['escopo']} |",
        f"| Linhas (copias) | {stats['linhas']} |",
        f"| Grupos | {stats['grupos']} |",
        f"| Excedentes (linhas - grupos) | {stats['excedentes']} |",
        f"| Grupos com `name` divergente | {stats['grupos_name_divergente']} |",
        f"| Grupos com `product_item_id` divergente | {stats['grupos_pi_divergente']} |",
        f"| Grupos com `status_id` divergente | {stats['grupos_status_divergente']} |",
        f"| Grupos com copia ALUGADA | {stats['grupos_com_alugado']} |",
        f"| Grupos citados no Parquet | {stats['grupos_parquet']} |",
        f"| Grupos com 2+ ids no Parquet | {stats['grupos_parquet_duplo']} |",
        f"| Grupos com colisao de movimento | {stats['grupos_colisao']} |",
        f"| Grupos com manutencao aberta | {stats['grupos_manutencao_aberta']} |",
        f"| Grupos com item em OS aberta | {stats['grupos_os_aberta']} |",
        "",
        "## Grupos por organizacao",
        "",
        "| orgao_id | grupos | linhas |",
        "|---:|---:|---:|",
    ]
    por_orgao: dict = {}
    for linhas_grupo in grupos.values():
        orgao = comum.normalizar_int(linhas_grupo[0].get("current_organization_id"))
        registro = por_orgao.setdefault(orgao, {"grupos": 0, "linhas": 0})
        registro["grupos"] += 1
        registro["linhas"] += len(linhas_grupo)
    for orgao in sorted(por_orgao, key=lambda v: (-por_orgao[v]["grupos"], v or 0)):
        registro = por_orgao[orgao]
        linhas.append(f"| {orgao} | {registro['grupos']} | {registro['linhas']} |")

    travados = []
    for chave in sorted(grupos, key=lambda c: (c[1] if len(c) > 1 else 0, c[0] or 0)):
        linhas_grupo = grupos[chave]
        motivos = comum.avaliar_travas(linhas_grupo)
        if motivos:
            travados.append((chave, linhas_grupo, motivos))

    linhas += [
        "",
        f"## Grupos que a Fase 1 deve travar ({len(travados)})",
        "",
        "| orgao_id | tombo | ids | motivos |",
        "|---:|---:|---|---|",
    ]
    for chave, linhas_grupo, motivos in travados:
        tombo = chave[0]
        orgao = chave[1] if len(chave) > 1 else ""
        ids = ", ".join(str(l["id"]) for l in linhas_grupo)
        linhas.append(f"| {orgao} | {tombo} | {ids} | {', '.join(motivos)} |")

    linhas += [
        "",
        "## Sinais detalhados",
        "",
        "O arquivo `candidatos.csv` traz, por copia, todos os sinais usados na",
        "decisao (movimentos, refs por tabela, Parquet, manutencao/OS abertas,",
        "colisao de movimento e divergencias dentro do grupo).",
        "",
    ]
    Path(caminho).write_text("\n".join(linhas) + "\n", encoding="utf-8")
