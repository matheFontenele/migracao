"""Aluguel em massa (avulso) para o cliente CO BRASIL.

Fonte: docs/co_brasil.csv (tombo + data_ultimo_movimento).
Cria, por data distinta do CSV:
  - 1 service_order AVULSO (status 3, sem contrato)
  - N service_order_items (equipment_id, status 3)
  - 1 movement (status 3) com N movement_items (status 3)
  - 1 shipment/carregamento (status 2) com N shipment_items
  - 1 volume com N volume_items
  - equipment_history e atualização do equipamento para ALUGADO (status 2)

Modo padrão: prévia, sem gravar. Use --apply para gravar.
"""

from __future__ import annotations

import argparse
import csv
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from config.database import obter_engines

DEFAULT_INPUT = Path("docs/co_brasil.csv")
DEFAULT_OUTPUT_DIR = Path("/tmp/opencode/aluguel-co-brasil")

CLIENTE_ID = 48
ADDRESS_ID = 314
ORGANIZATION_ID = 1115
USER_ID = 1
MOVEMENT_TYPE_ID = 7  # AVULSO
STATUS_DONE = 3
STATUS_SHIPMENT_DELIVERED = 2
STATUS_EQUIPMENT_RENTED = 2
DETAILS_PREFIX = "Migração aluguel avulso CO Brasil 2026"


def parse_datetime(value: str) -> datetime | None:
    value = " ".join(str(value or "").split())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _number(value: object) -> int | None:
    text_value = str(value or "").strip()
    return int(text_value) if text_value.isdigit() else None


def load_source(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    for index, row in enumerate(rows, start=2):
        row["_linha"] = index
        row["_tombo"] = _number(row.get("tombo"))
        row["_data"] = parse_datetime(row.get("data_ultimo_movimento", ""))
    return rows


def load_lookups(conn):
    equipments: dict[int, dict] = {}
    for row in conn.execute(text(
        "SELECT id, number, name, status_id, address_id "
        "FROM equipments WHERE number IS NOT NULL AND deleted_at IS NULL ORDER BY id"
    )).mappings():
        equipments.setdefault(int(row["number"]), dict(row))
    customer = conn.execute(text(
        "SELECT id, name, organization_id FROM customers "
        "WHERE id=:id AND deleted_at IS NULL"
    ), {"id": CLIENTE_ID}).mappings().first()
    address = conn.execute(text(
        "SELECT id, addressable_id, addressable_type FROM addresses "
        "WHERE id=:id AND deleted_at IS NULL"
    ), {"id": ADDRESS_ID}).mappings().first()
    return {
        "equipments": equipments,
        "customer": dict(customer) if customer else None,
        "address": dict(address) if address else None,
    }


def resolve_groups(rows: list[dict], lookups: dict):
    errors: list[str] = []
    if not lookups["customer"]:
        errors.append(f"Cliente {CLIENTE_ID} não encontrado/ativo")
    if not lookups["address"]:
        errors.append(f"Endereço {ADDRESS_ID} não encontrado/ativo")
    elif lookups["address"]["addressable_id"] != CLIENTE_ID:
        errors.append(
            f"Endereço {ADDRESS_ID} pertence a addressable_id="
            f"{lookups['address']['addressable_id']} ({lookups['address']['addressable_type']}), "
            f"não ao cliente {CLIENTE_ID}"
        )

    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    skipped: list[dict] = []
    for row in rows:
        tombo = row["_tombo"]
        when = row["_data"]
        problems = []
        if tombo is None:
            problems.append("tombo inválido")
        if when is None:
            problems.append("data_ultimo_movimento inválida")
        equipment = lookups["equipments"].get(tombo) if tombo is not None else None
        if tombo is not None and not equipment:
            problems.append("equipment.number não encontrado no refatorado")
        if equipment and equipment["status_id"] not in (1, 2):
            problems.append(
                f"equipamento está com status_id={equipment['status_id']} "
                "(esperado 1/2 para recriar aluguel histórico)"
            )
        if problems:
            skipped.append({"linha": row["_linha"], "tombo": tombo, "motivos": "; ".join(problems)})
            continue
        key = when.strftime("%Y-%m-%d %H:%M:%S")
        groups.setdefault(key, []).append({**row, "_equipment": equipment})
    return groups, skipped, errors


def marker_for(date_key: str, count: int) -> str:
    return f"{DETAILS_PREFIX} ({date_key}; {count} equipamentos)"


def print_preview(groups, skipped, errors, apply_hint=True):
    print("\nPRÉVIA DO ALUGUEL EM MASSA — CO BRASIL")
    print(
        f"Cliente: {CLIENTE_ID} | Endereço: {ADDRESS_ID} | "
        f"Organização: {ORGANIZATION_ID} | Tipo: AVULSO (7)"
    )
    print(f"Equipamentos válidos: {sum(len(items) for items in groups.values())}")
    print(f"Equipamentos ignorados: {len(skipped)}")
    print(f"Grupos de data: {len(groups)}")
    for date_key, items in groups.items():
        print(
            f"  {date_key}: service_order=1, movement=1, shipment=1, "
            f"itens={len(items)}, volume=1"
        )
    for error in errors:
        print(f"ERRO: {error}")
    for item in skipped:
        print(f"IGNORADO linha {item['linha']} tombo {item['tombo']}: {item['motivos']}")
    if not groups:
        print("NADA A GRAVAR: nenhum grupo válido.")
    if apply_hint:
        print("Prévia somente leitura. Use --apply para gravar.")
    else:
        print("Prévia consultiva; a gravação exige confirmação explícita do destino.")


def next_movement_number(conn) -> int:
    separator = conn.execute(text(
        "SELECT last_number FROM movement_number_sequences WHERE id=1 FOR UPDATE"
    )).first()
    if not separator:
        raise ValueError("Sequência movement_number_sequences id=1 não encontrada")
    max_number = conn.execute(text(
        "SELECT COALESCE(MAX(CAST(number AS UNSIGNED)), 0) FROM movements "
        "WHERE number REGEXP '^[0-9]+$'"
    )).scalar() or 0
    number = max(int(separator[0]), int(max_number)) + 1
    conn.execute(
        text("UPDATE movement_number_sequences SET last_number=:number WHERE id=1"),
        {"number": number},
    )
    return number


def insert_row(conn, table: str, values: dict) -> int:
    columns = ", ".join(f"`{column}`" for column in values)
    binds = ", ".join(f":{column}" for column in values)
    result = conn.execute(
        text(f"INSERT INTO `{table}` ({columns}) VALUES ({binds})"), values
    )
    return result.lastrowid


def apply_groups(engine, groups, lookups) -> dict[str, int]:
    totals = {"orders": 0, "items": 0, "movements": 0, "movement_items": 0,
              "shipments": 0, "shipment_items": 0, "volumes": 0, "volume_items": 0,
              "equipment_updates": 0, "duplicates": 0}
    organization_id = ORGANIZATION_ID
    address_id = int(lookups["address"]["id"])

    with engine.begin() as conn:
        max_volume = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM volumes")).scalar() or 0
        volume_counter = int(max_volume)
        for date_key, items in groups.items():
            marker = marker_for(date_key, len(items))
            existing = conn.execute(text(
                "SELECT id FROM service_orders WHERE details=:details AND deleted_at IS NULL LIMIT 1"
            ), {"details": marker}).first()
            if existing:
                print(f"BLOQUEADO {date_key}: service_order já criado ({marker})")
                totals["duplicates"] += 1
                continue

            stamp = date_key
            order_id = insert_row(conn, "service_orders", {
                "status_id": STATUS_DONE, "movement_type_id": MOVEMENT_TYPE_ID,
                "contract_id": None, "user_id": USER_ID, "mode_transport_id": 1,
                "requires_invoice": False, "organization_id": organization_id,
                "recipient_customer_id": CLIENTE_ID, "deadline": date_key[:10],
                "destination_order_id": None, "maintenance_created_order": False,
                "situation": "approved", "changed_by": USER_ID, "details": marker,
                "created_at": stamp, "updated_at": stamp, "deleted_at": None,
            })
            totals["orders"] += 1

            movement_number = next_movement_number(conn)
            movement_id = insert_row(conn, "movements", {
                "number": str(movement_number), "movement_date": stamp,
                "service_order_id": order_id, "recipient_customer_id": CLIENTE_ID,
                "migrate_customer_id": None, "organization_id": organization_id,
                "status_id": STATUS_DONE, "created_by": USER_ID, "details": marker,
                "maintenance_created_movement": False,
                "created_at": stamp, "updated_at": stamp, "deleted_at": None,
            })
            totals["movements"] += 1

            shipment_id = insert_row(conn, "shipments", {
                "status_id": STATUS_SHIPMENT_DELIVERED, "created_by": USER_ID,
                "created_at": stamp, "updated_at": stamp, "deleted_at": None,
            })
            totals["shipments"] += 1

            volume_counter += 1
            volume_id = volume_counter
            insert_row(conn, "volumes", {
                "id": volume_id, "movement_id": movement_id,
                "name": f"CO BRASIL {date_key[:10]}",
                "code": f"COBRASIL-{date_key[:10]}", "shipped": 0,
                "created_at": stamp, "updated_at": stamp,
            })
            totals["volumes"] += 1
            insert_row(conn, "shipment_movements", {
                "shipment_id": shipment_id, "movement_id": movement_id,
            })

            for row in items:
                equipment = row["_equipment"]
                service_item_id = insert_row(conn, "service_order_items", {
                    "status_id": STATUS_DONE, "service_order_id": order_id,
                    "department_id": 2, "movement_type_id": MOVEMENT_TYPE_ID,
                    "contract_item_id": None, "alias": equipment["name"],
                    "equipment_id": equipment["id"], "type_id": None, "product_id": None,
                    "is_exchange": False, "is_extra": False,
                    "quantity_product": None, "fulfilled_quantity_product": 0,
                    "quantity": 1, "fulfilled_quantity": 0,
                    "details": "Aluguel avulso CO Brasil 2026",
                    "address_id": address_id, "location_id": None,
                    "created_at": stamp, "updated_at": stamp, "deleted_at": None,
                    "exchange_approval_status": None, "exchange_approved_by": None,
                    "exchange_approved_at": None, "stock_status": "in_stock",
                })
                totals["items"] += 1
                movement_item_id = insert_row(conn, "movement_items", {
                    "movement_id": movement_id, "movement_type_id": MOVEMENT_TYPE_ID,
                    "service_order_item_id": service_item_id,
                    "equipment_id": equipment["id"], "extra_id": None,
                    "status_id": STATUS_DONE, "product_item_id": None,
                    "alias": equipment["name"], "old_organization_id": None,
                    "new_organization_id": organization_id, "operation_type": "AVULSO",
                    "confirmed_at": None, "confirmed_by": None,
                    "created_at": stamp, "updated_at": stamp, "deleted_at": None,
                })
                totals["movement_items"] += 1

                shipment_item_id = insert_row(conn, "shipment_items", {
                    "shipment_id": shipment_id, "status_id": STATUS_SHIPMENT_DELIVERED,
                    "movement_item_id": movement_item_id, "volume_id": None,
                    "details": "Guia de transporte (Operação: AVULSO)",
                    "address_id": address_id, "delivery_date": date_key[:10],
                    "created_at": stamp, "updated_at": stamp,
                })
                totals["shipment_items"] += 1

                insert_row(conn, "volume_items", {
                    "volume_id": volume_id, "movement_item_id": movement_item_id,
                    "quantity": 1, "created_at": stamp, "updated_at": stamp,
                })
                totals["volume_items"] += 1

                insert_row(conn, "equipment_history", {
                    "equipment_id": equipment["id"], "status_id": STATUS_EQUIPMENT_RENTED,
                    "occurred_at": stamp, "movement_item_id": movement_item_id,
                    "service_order_item_id": service_item_id, "contract_item_id": None,
                    "shipment_item_id": shipment_item_id, "is_conversion": 0,
                    "reason": "SHIPPING_CONFIRMED_SEPARATE", "user_id": USER_ID,
                })

                conn.execute(text(
                    "UPDATE equipments SET status_id=:status, "
                    "last_movement_item_customer_id=:movement_item, address_id=:address "
                    "WHERE id=:equipment"
                ), {
                    "status": STATUS_EQUIPMENT_RENTED,
                    "movement_item": movement_item_id,
                    "address": address_id,
                    "equipment": equipment["id"],
                })
                totals["equipment_updates"] += 1

            print(f"CRIADO {date_key}: service_order={order_id}, movement={movement_id} "
                  f"(protocolo {movement_number}), shipment={shipment_id}, itens={len(items)}")
    return totals


def write_preview_csv(path: Path, groups, skipped) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["data", "linha_csv", "tombo", "equipment_id", "equipamento",
                         "status_atual", "cliente_id", "address_id"])
        for date_key, items in groups.items():
            for row in items:
                equipment = row["_equipment"]
                writer.writerow([date_key, row["_linha"], row["_tombo"], equipment["id"],
                                 equipment["name"], equipment["status_id"],
                                 CLIENTE_ID, ADDRESS_ID])
        for item in skipped:
            writer.writerow(["", item["linha"], item["tombo"], "", item["motivos"],
                             "", "", ""])


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Aluguel avulso em massa CO BRASIL; grava somente com --apply."
    )
    parser.add_argument("--arquivo", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--saida-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true", help="Grava os grupos válidos")
    args = parser.parse_args(argv)

    rows = load_source(args.arquivo)
    engine, _ = obter_engines()
    with engine.connect() as conn:
        lookups = load_lookups(conn)
    groups, skipped, errors = resolve_groups(rows, lookups)
    print_preview(groups, skipped, errors)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    preview_path = args.saida_dir / f"preview_aluguel_co_brasil_{timestamp}.csv"
    write_preview_csv(preview_path, groups, skipped)
    print(f"Prévia: {preview_path}")

    if errors or skipped:
        print("Nada será gravado enquanto houver erros/ignorados. Revise a prévia.")
        return
    if not args.apply:
        return
    if not groups:
        return
    totals = apply_groups(engine, groups, lookups)
    print("APLICADO: " + "; ".join(f"{key}={value}" for key, value in totals.items()))


if __name__ == "__main__":
    main()
