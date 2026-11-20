#!/usr/bin/env python3
"""Remove duplicidades entre as requisições 2026 e a migração principal.

Dois grupos, ambos por *soft delete* (coluna `deleted_at`), sem tocar em
`number` nem em qualquer outro dado do movimento:

1. Itens de "Ajuste de migração individual" que espelham, para o mesmo
   equipamento + cliente + operação, um item de movimento criado pela
   importação das requisições 2026 na janela de 0 a +1 dia (data de
   separação -> data de entrega). O ajuste nasceu para cobrir um movimento
   que o legado tinha e o refatorado não; a requisição depois recriou a
   mesma entrega, então o ajuste é o clone.
2. Resíduos de execuções anteriores da importação de requisições
   (movimentos "Migração requisição 2026" e service orders
   "Migração de requisição 2026 (...)").

Um movimento só é removido por inteiro quando todos os seus itens foram
classificados; caso contrário apenas os itens duplicados são desativados.

Uso:
    .venv/bin/python ajustes/limpar_duplicidades_requisicoes.py [--apply]

Sem `--apply` é dry-run: mostra o plano e grava o CSV de auditoria.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import bindparam, text

PASTA = Path(__file__).resolve().parent
sys.path.insert(0, str(PASTA.parent))
MARCADOR_REQ = "Migração REQUISIÇÃO 2026.csv"
MARCADOR_AJUSTE = "Ajuste de migração individual"
RESIDUO_MOVIMENTO = "Migração requisição 2026"
RESIDUO_ORDEM = "Migração de requisição 2026 ("

# Tabelas que apontam para movements / movement_items / service_order_items.
DEPENDENTES_MOVIMENTO = (
    ("romaneios", "movement_id"),
    ("shipment_document_movements", "movement_id"),
    ("volumes", "movement_id"),
)
DEPENDENTES_ITEM = (
    ("service_order_accessories", "service_order_item_id"),
    ("service_order_item_extra_equipments", "service_order_item_id"),
)


def _sql(comando: str):
    """`text()` com `IN :ids` preparado para receber uma tupla."""
    return text(comando).bindparams(bindparam("ids", expanding=True))


def _csv_saida() -> Path:
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PASTA / f"limpeza_duplicidades_{marca}.csv"


def _dependentes(conn, tabelas, coluna: str, ids: list[int]) -> dict[str, int]:
    achados = {}
    if not ids:
        return achados
    for tabela, campo in tabelas:
        n = int(
            conn.execute(
                _sql(f"SELECT COUNT(*) FROM {tabela} WHERE {campo} IN :ids"),
                {"ids": tuple(ids)},
            ).scalar_one()
        )
        if n:
            achados[f"{tabela}.{campo}"] = n
    return achados


def _itens_ajuste_duplicados(conn) -> list[dict]:
    """Itens de ajuste que espelham um item da requisição (0..+1 dia)."""
    sql = text(
        """
        SELECT ami.id                 AS item_ajuste,
               am.id                  AS mov_ajuste,
               am.number              AS num_ajuste,
               DATE(am.movement_date) AS data_ajuste,
               am.recipient_customer_id AS cliente,
               ami.equipment_id       AS equipamento,
               ami.operation_type     AS operacao,
               rm.id                  AS mov_req,
               rm.number              AS num_req,
               DATE(rm.movement_date) AS data_req
        FROM movements am
        JOIN movement_items ami ON ami.movement_id = am.id
                               AND ami.deleted_at IS NULL
        JOIN movement_items rmi ON rmi.equipment_id = ami.equipment_id
                               AND rmi.operation_type = ami.operation_type
                               AND rmi.deleted_at IS NULL
        JOIN movements rm ON rm.id = rmi.movement_id
                         AND rm.deleted_at IS NULL
        WHERE am.details LIKE :marcador_ajuste
          AND am.deleted_at IS NULL
          AND rm.details LIKE :marcador_req
          AND rm.recipient_customer_id = am.recipient_customer_id
          AND ABS(DATEDIFF(rm.movement_date, am.movement_date)) <= 1
        """
    )
    linhas = conn.execute(
        sql,
        {
            "marcador_ajuste": f"{MARCADOR_AJUSTE}%",
            "marcador_req": f"%{MARCADOR_REQ}%",
        },
    ).mappings()
    # Um item de ajuste pode casar com mais de um movimento da requisição;
    # guarda o primeiro par para a auditoria.
    vistos: dict[int, dict] = {}
    for linha in linhas:
        vistos.setdefault(int(linha["item_ajuste"]), dict(linha))
    return list(vistos.values())


def _movimentos_residuo(conn) -> list[int]:
    linhas = conn.execute(
        text(
            "SELECT id FROM movements "
            "WHERE BINARY details LIKE :marcador AND deleted_at IS NULL"
        ),
        {"marcador": f"{RESIDUO_MOVIMENTO}%"},
    ).scalars()
    return sorted(int(i) for i in linhas)


def _ordens_residuo(conn) -> list[int]:
    linhas = conn.execute(
        text(
            "SELECT id FROM service_orders "
            "WHERE BINARY details LIKE :marcador AND deleted_at IS NULL"
        ),
        {"marcador": f"{RESIDUO_ORDEM}%"},
    ).scalars()
    return sorted(int(i) for i in linhas)


def _plano_ajustes(conn, linhas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa movimentos removidos por inteiro dos removidos em parte."""
    por_movimento: dict[int, list[dict]] = {}
    for linha in linhas:
        por_movimento.setdefault(int(linha["mov_ajuste"]), []).append(linha)

    completos, parciais = [], []
    for mov_id, itens in por_movimento.items():
        total = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM movement_items "
                    "WHERE movement_id = :mid AND deleted_at IS NULL"
                ),
                {"mid": mov_id},
            ).scalar_one()
        )
        registro = {
            "mov_ajuste": mov_id,
            "number": itens[0]["num_ajuste"],
            "cliente": itens[0]["cliente"],
            "data": itens[0]["data_ajuste"],
            "itens": itens,
            "itens_no_movimento": total,
        }
        (completos if len(itens) >= total else parciais).append(registro)
    completos.sort(key=lambda r: r["mov_ajuste"])
    parciais.sort(key=lambda r: r["mov_ajuste"])
    return completos, parciais


def _shipments_do_grupo(conn, movimento_ids: list[int]) -> tuple[list[int], list[int]]:
    """Romaneios ligados aos movimentos, exigindo que sejam 1:1.

    Cada ajuste removido tem um romaneio com um único movimento (o próprio
    ajuste). Se aparecer romaneio com outros movimentos ou com documento,
    a remoção para: apagar o movimento deixaria o romaneio inconsistente, e
    reapontar a ligação impediria uma futura reexecução da etapa 8.
    """
    if not movimento_ids:
        return [], []
    linhas = conn.execute(
        _sql(
            "SELECT id, shipment_id, movement_id FROM shipment_movements "
            "WHERE movement_id IN :ids AND deleted_at IS NULL"
        ),
        {"ids": tuple(movimento_ids)},
    ).mappings().all()
    if not linhas:
        return [], []
    removidos = set(movimento_ids)
    shipment_ids = sorted({int(l["shipment_id"]) for l in linhas})
    irmaos = conn.execute(
        _sql(
            "SELECT id, shipment_id, movement_id FROM shipment_movements "
            "WHERE shipment_id IN :ids AND deleted_at IS NULL"
        ),
        {"ids": tuple(shipment_ids)},
    ).mappings().all()
    fora = [r for r in irmaos if int(r["movement_id"]) not in removidos]
    if fora:
        raise RuntimeError(
            "romaneio compartilhado com movimento preservado: "
            + ", ".join(f"shipment {r['shipment_id']} -> movement {r['movement_id']}"
                        for r in fora[:5])
        )
    docs = int(
        conn.execute(
            _sql("SELECT COUNT(*) FROM shipment_documents WHERE shipment_id IN :ids"),
            {"ids": tuple(shipment_ids)},
        ).scalar_one()
    )
    if docs:
        raise RuntimeError(f"{docs} documento(s) de romaneio nos shipments afetados")
    return shipment_ids, sorted(int(l["id"]) for l in linhas)


def _aplicar_ajustes(conn, completos, parciais) -> dict[str, int]:
    stats = {"movimentos": 0, "itens": 0, "itens_ordem": 0,
             "shipments": 0, "shipment_movimentos": 0}
    movimento_ids = [r["mov_ajuste"] for r in completos]
    item_ids = [int(i["item_ajuste"]) for r in completos + parciais
                for i in r["itens"]]

    shipment_ids, shipment_mov_ids = _shipments_do_grupo(conn, movimento_ids)
    if shipment_mov_ids:
        stats["shipment_movimentos"] = conn.execute(
            _sql(
                "UPDATE shipment_movements SET deleted_at = NOW() "
                "WHERE id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(shipment_mov_ids)},
        ).rowcount
    if shipment_ids:
        stats["shipments"] = conn.execute(
            _sql(
                "UPDATE shipments SET deleted_at = NOW() "
                "WHERE id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(shipment_ids)},
        ).rowcount

    if movimento_ids:
        ordens = [
            int(i) for i in conn.execute(
                _sql(
                    "SELECT service_order_id FROM movements "
                    "WHERE id IN :ids AND service_order_id IS NOT NULL"
                ),
                {"ids": tuple(movimento_ids)},
            ).scalars()
        ]
        if ordens:
            stats["itens_ordem"] = conn.execute(
                _sql(
                    "UPDATE service_order_items SET deleted_at = NOW() "
                    "WHERE service_order_id IN :ids AND deleted_at IS NULL"
                ),
                {"ids": tuple(ordens)},
            ).rowcount
            conn.execute(
                _sql(
                    "UPDATE service_orders SET deleted_at = NOW() "
                    "WHERE id IN :ids AND deleted_at IS NULL"
                ),
                {"ids": tuple(ordens)},
            )

    so_itens: list[int] = []
    if item_ids:
        so_itens = [
            int(i) for i in conn.execute(
                _sql(
                    "SELECT DISTINCT service_order_item_id FROM movement_items "
                    "WHERE id IN :ids AND service_order_item_id IS NOT NULL"
                ),
                {"ids": tuple(item_ids)},
            ).scalars()
        ]
        stats["itens"] = conn.execute(
            _sql(
                "UPDATE movement_items SET deleted_at = NOW() "
                "WHERE id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(item_ids)},
        ).rowcount

    if so_itens:
        stats["itens_ordem"] += conn.execute(
            _sql(
                "UPDATE service_order_items soi SET soi.deleted_at = NOW() "
                "WHERE soi.id IN :ids AND soi.deleted_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM movement_items mi "
                "                WHERE mi.service_order_item_id = soi.id "
                "                  AND mi.deleted_at IS NULL)"
            ),
            {"ids": tuple(so_itens)},
        ).rowcount

    if movimento_ids:
        stats["movimentos"] = conn.execute(
            _sql(
                "UPDATE movements SET deleted_at = NOW() "
                "WHERE id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(movimento_ids)},
        ).rowcount
    return stats


def _aplicar_residuos(conn, movimentos: list[int], ordens: list[int]) -> dict[str, int]:
    stats = {"movimentos": 0, "itens": 0, "ordens": 0, "itens_ordem": 0}
    if movimentos:
        stats["itens"] = conn.execute(
            _sql(
                "UPDATE movement_items SET deleted_at = NOW() "
                "WHERE movement_id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(movimentos)},
        ).rowcount
        stats["movimentos"] = conn.execute(
            _sql(
                "UPDATE movements SET deleted_at = NOW() "
                "WHERE id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(movimentos)},
        ).rowcount
    if ordens:
        stats["itens_ordem"] = conn.execute(
            _sql(
                "UPDATE service_order_items SET deleted_at = NOW() "
                "WHERE service_order_id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(ordens)},
        ).rowcount
        stats["ordens"] = conn.execute(
            _sql(
                "UPDATE service_orders SET deleted_at = NOW() "
                "WHERE id IN :ids AND deleted_at IS NULL"
            ),
            {"ids": tuple(ordens)},
        ).rowcount
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Grava deleted_at. Sem esta flag só mostra o plano.")
    args = parser.parse_args()

    from config.database import obter_engines

    engine, _ = obter_engines()
    destino = _csv_saida()

    with engine.begin() as conn:
        linhas = _itens_ajuste_duplicados(conn)
        completos, parciais = _plano_ajustes(conn, linhas)
        residuos_mov = _movimentos_residuo(conn)
        residuos_so = _ordens_residuo(conn)

        print("🧹 Limpeza de duplicidades das requisições 2026")
        print(f"   itens de ajuste duplicados : {len(linhas)}")
        print(f"   ajustes removidos inteiros : {len(completos)}")
        print(f"   ajustes removidos em parte : {len(parciais)}")
        print(f"   movimentos resíduo         : {residuos_mov}")
        print(f"   ordens resíduo             : {residuos_so}")

        with destino.open("w", newline="", encoding="utf-8-sig") as arquivo:
            escritor = csv.writer(arquivo, delimiter=";")
            escritor.writerow([
                "grupo", "acao", "movimento", "number", "cliente", "data",
                "item", "equipamento", "movimento_requisicao",
                "number_requisicao", "data_requisicao",
            ])
            for grupo, registros in (("AJUSTE_INTEIRO", completos),
                                     ("AJUSTE_PARCIAL", parciais)):
                for registro in registros:
                    for item in registro["itens"]:
                        escritor.writerow([
                            grupo, "DELETAR_ITEM", registro["mov_ajuste"],
                            registro["number"], registro["cliente"],
                            registro["data"], item["item_ajuste"],
                            item["equipamento"], item["mov_req"],
                            item["num_req"], item["data_req"],
                        ])
            for mov_id in residuos_mov:
                escritor.writerow(["RESIDUO", "DELETAR_MOVIMENTO", mov_id,
                                   "", "", "", "", "", "", "", ""])

        # Bloqueio: nada é apagado se algum registro tiver dependentes.
        mov_ids = [r["mov_ajuste"] for r in completos] + residuos_mov
        dep = _dependentes(conn, DEPENDENTES_MOVIMENTO, "movement_id", mov_ids)
        item_ids = [int(i["item_ajuste"]) for r in completos + parciais
                    for i in r["itens"]]
        dep.update(
            _dependentes(conn, DEPENDENTES_ITEM, "service_order_item_id", item_ids)
        )
        if dep:
            print("\n⛔ Dependências encontradas; nada foi alterado:")
            for chave, valor in dep.items():
                print(f"   {chave}: {valor}")
            return 1

        try:
            shipment_ids, shipment_mov_ids = _shipments_do_grupo(
                conn, [r["mov_ajuste"] for r in completos])
        except RuntimeError as erro:
            print(f"\n⛔ {erro}")
            return 1
        if shipment_ids:
            print(f"   romaneios 1:1 a desativar   : {len(shipment_ids)}")

        if parciais:
            print("\n⚠️ Ajustes com remoção parcial (itens preservados):")
            for registro in parciais:
                print(f"   mov {registro['mov_ajuste']} nº {registro['number']}: "
                      f"{len(registro['itens'])} de "
                      f"{registro['itens_no_movimento']} itens")

        if not args.apply:
            print(f"\n🔒 Dry-run: nada foi gravado. Plano em {destino}")
            return 0

        stats = _aplicar_ajustes(conn, completos, parciais)
        stats_res = _aplicar_residuos(conn, residuos_mov, residuos_so)

    print("\n🏁 Aplicado (soft delete):")
    print(f"   ajustes: {stats['movimentos']} movimentos, {stats['itens']} itens, "
          f"{stats['itens_ordem']} itens de ordem, "
          f"{stats['shipments']} romaneios")
    print(f"   resíduos: {stats_res['movimentos']} movimentos, "
          f"{stats_res['itens']} itens, {stats_res['ordens']} ordens "
          f"({stats_res['itens_ordem']} itens)")
    print(f"📄 Auditoria: {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
