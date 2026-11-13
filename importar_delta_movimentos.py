#!/usr/bin/env python3
"""Planejador (dry-run) do delta de producao para o banco local migrado.

Regras centrais:
- A PK de producao nunca e reutilizada localmente; ela e apenas chave de origem.
- Linhas com criacao dentro da janela sao candidatas a insercao. Linhas apenas
  atualizadas na janela ficam fora da v1 (somente insercao).
- Toda FK e resolvida por um mapa explicito; FK obrigatoria sem mapa bloqueia.
- Nenhuma escrita e feita neste modulo. Apenas SELECT/information_schema.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

VERSION = "2.0.0"
NEW = "NEW"

DATE_FIELDS = ("created_at", "updated_at", "occurred_at", "movement_date", "transaction_date", "maintenance_date")
CREATION_FIELD = {
    "equipment_history": "occurred_at",
    "movements": "created_at",
    "service_orders": "created_at",
    "work_orders": "created_at",
}

DIMENSIONS = [
    "customers", "organizations", "users", "status", "movement_types", "movement_status",
    "service_order_status", "shipment_status", "maintenance_types", "maintenance_status",
    "technician_status", "document_types", "transaction_types", "categories", "conditions",
    "suppliers", "products", "brands", "groups", "types",
]

# Ordem de processamento: dimensoes -> estoque -> transacoes -> malha de movimentos.
PHASES = [
    "brands", "groups", "types", "products", "product_items", "equipments",
    "transactions", "transaction_items", "service_orders", "service_order_items",
    "movements", "movement_items", "shipments", "shipment_items", "volumes",
    "volume_items", "equipment_history", "maintenances", "maintenance_items",
    "return_confirmations", "return_confirmations_items", "romaneios",
]

# Tabelas cujo nome e chave natural (quando nao houver id local).
NAME_TABLES = {"brands", "groups", "types"}
PRODUCT_TABLE = "products"


def normalize(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def normalize_record(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def as_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def creation_date(table, row):
    field = CREATION_FIELD.get(table, "created_at")
    return as_date(row.get(field)) or as_date(row.get("created_at")) or as_date(row.get("occurred_at"))


def in_window(value, start, end):
    return value is not None and start <= value <= end


def normalize_number(value):
    if value is None:
        return None
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return normalize(value) or None


def equipment_choice(source, candidates):
    """Return (local_row or None, status, confidence, reason)."""
    number = normalize_number(source.get("number"))
    if number is None:
        # Equipamentos sem tombo existem no legado/producao; sao novos e nao
        # podem ser deduplicados por numero. O numero e nullable no destino.
        return None, "new", "low", "equipment without number; new row"
    same = [r for r in candidates if normalize_number(r.get("number")) == number]
    serial = normalize(source.get("serial_number")) if source.get("serial_number") not in (None, "") else ""
    if serial:
        exact = [r for r in same if normalize(r.get("serial_number")) == serial]
        if len(exact) == 1:
            return exact[0], "matched", "high", "number and serial"
        if len(same) > 1:
            return None, "blocked", "none", "duplicate number without matching serial"
    if len(same) == 1:
        if serial and same[0].get("serial_number") and normalize(same[0]["serial_number"]) != serial:
            return None, "blocked", "none", "serial conflict"
        return same[0], "candidate_review", "low", "unique number, serial not corroborated"
    if len(same) > 1:
        return None, "blocked", "none", "ambiguous equipment number"
    return None, "new", "high", "no local equipment for number"


class Planner:
    def __init__(self, lookups, fks, nullables):
        self.lookups = lookups          # table -> list[dict]
        self.fks = fks                  # table -> {column: referenced_table}
        self.nullables = nullables      # table -> set(column)
        self.map = {}                   # (table, source_id) -> local_id | NEW | None(blocked)
        self.status = {}                # (table, source_id) -> status
        self.evidence = {}              # (table, source_id) -> evidence
        self.rows = {}                  # table -> source rows (candidates)

    # ---------------------------------------------------------------- helpers
    def dim_exists(self, table, source_id):
        for row in self.lookups.get(table, []):
            if row.get("id") == source_id and not row.get("deleted_at"):
                return row["id"]
        return None

    def local_by_id(self, table, source_id):
        for row in self.lookups.get(table, []):
            if row.get("id") == source_id and not row.get("deleted_at"):
                return row
        return None

    def name_match(self, table, name):
        target = normalize(name)
        if not target:
            return None
        hits = [r for r in self.lookups.get(table, [])
                if not r.get("deleted_at") and normalize(r.get("name")) == target]
        return hits[0]["id"] if len(hits) == 1 else None

    def register(self, table, source_id, value, status, evidence=""):
        self.map[(table, source_id)] = value
        self.status[(table, source_id)] = status
        self.evidence[(table, source_id)] = evidence

    def resolve_fk(self, table, column, value):
        """Resolve one FK. Returns (local_value_or_NEW_or_None, missing: bool)."""
        if value is None:
            return None, False
        target = self.fks.get(table, {}).get(column)
        if not target:
            return value, False
        key = (target, value)
        if key in self.map:
            mapped = self.map[key]
            if mapped is None:
                return None, True
            return mapped, False
        if target in DIMENSIONS:
            local = self.dim_exists(target, value)
            if local is not None:
                return local, False
            return None, True
        # Entidades derivadas do mesmo legado preservam os IDs locais. So usar
        # esta equivalencia quando o registro existe e nao veio no delta.
        local = self.local_by_id(target, value)
        if local is not None:
            self.register(target, value, local["id"], "matched", "local id exists")
            return local["id"], False
        return None, True

    def apply_fks(self, table, row, address=None, location=None):
        """Return (resolved dict, list of blocking columns, list of nulled columns)."""
        resolved, blocking, nulled = {}, [], []
        for column, value in row.items():
            if column == "id" or not column.endswith("_id"):
                continue
            if column == "address_id" and address is not None:
                resolved[column] = address
                continue
            if column == "location_id" and location is not None:
                resolved[column] = location
                continue
            mapped, missing = self.resolve_fk(table, column, value)
            if not missing:
                resolved[column] = mapped
                continue
            if column in self.nullables.get(table, set()):
                resolved[column] = None
                nulled.append(column)
            else:
                blocking.append(column)
        return resolved, blocking, nulled

    def customer_address(self, customer_id):
        hits = [r for r in self.lookups.get("addresses", [])
                if r.get("addressable_type") == "customer"
                and r.get("addressable_id") == customer_id
                and not r.get("deleted_at")]
        linked = [r for r in hits if r.get("legacy_customer_id") is not None]
        if len(linked) == 1:
            return linked[0]["id"], "customer legacy address"
        if len(hits) == 1:
            return hits[0]["id"], "unique customer address"
        return None, "customer has multiple addresses"

    def location_match(self, address_id, area=None):
        if address_id is None:
            return None
        hits = [r for r in self.lookups.get("locations", [])
                if r.get("address_id") == address_id and not r.get("deleted_at")
                and (area is None or normalize(r.get("area")) == normalize(area))]
        return hits[0]["id"] if len(hits) == 1 else None

    # ---------------------------------------------------------------- phases
    def plan_dimension_names(self, table):
        for row in self.rows.get(table, []):
            source_id = row.get("id")
            local = self.name_match(table, row.get("name"))
            if local is not None:
                self.register(table, source_id, local, "matched", "name")
            else:
                self.register(table, source_id, NEW, "new", "new dimension by name")

    def plan_products(self):
        for row in self.rows.get(PRODUCT_TABLE, []):
            source_id = row.get("id")
            local = self.name_match(PRODUCT_TABLE, row.get("name"))
            if local is None:
                local = self.dim_exists(PRODUCT_TABLE, source_id)
            if local is not None:
                self.register(PRODUCT_TABLE, source_id, local, "matched", "name or id")
            else:
                self.register(PRODUCT_TABLE, source_id, NEW, "new", "new product")

    def plan_product_items(self):
        for row in self.rows.get("product_items", []):
            source_id = row.get("id")
            product, _ = self.resolve_fk("product_items", "product_id", row.get("product_id"))
            row_values = dict(row)
            row_values["product_id"] = product
            address, _ = self.resolve_fk("product_items", "address_id", row.get("address_id"))
            location, _ = self.resolve_fk("product_items", "location_id", row.get("location_id"))
            if address is None:
                address = self.location_match(None) and None
            combination = {
                "product_id": product,
                "category_id": self.resolve_fk("product_items", "category_id", row.get("category_id"))[0],
                "condition_id": self.resolve_fk("product_items", "condition_id", row.get("condition_id"))[0],
                "address_id": address,
                "location_id": location,
                "organization_id": self.resolve_fk("product_items", "organization_id", row.get("organization_id"))[0],
            }
            code = row.get("code")
            code_hits = [r for r in self.lookups.get("product_items", [])
                         if code is not None and r.get("code") == code and not r.get("deleted_at")]
            if len(code_hits) > 1:
                self.register("product_items", source_id, None, "blocked", "duplicate product_item code")
                continue
            if len(code_hits) == 1:
                self.register("product_items", source_id, code_hits[0]["id"], "matched", "code")
                continue
            hits = [r for r in self.lookups.get("product_items", [])
                    if not r.get("deleted_at") and all(
                        (r.get(k) is None and v is None) or r.get(k) == v
                        for k, v in combination.items())]
            if len(hits) == 1:
                self.register("product_items", source_id, hits[0]["id"], "matched", "unique combination")
            elif len(hits) > 1:
                self.register("product_items", source_id, None, "blocked", "ambiguous combination")
            else:
                self.register("product_items", source_id, NEW, "new", "new product item")

    def plan_equipments(self):
        candidates = self.lookups.get("equipments", [])
        for row in self.rows.get("equipments", []):
            source_id = row.get("id")
            local, status, confidence, reason = equipment_choice(row, candidates)
            if status == "matched" or status == "candidate_review":
                self.register("equipments", source_id, local["id"], status, reason)
            elif status == "new":
                self.register("equipments", source_id, NEW, "new", reason)
            else:
                self.register("equipments", source_id, None, "blocked", reason)

    def plan_new_parents(self, table):
        for row in self.rows.get(table, []):
            source_id = row.get("id")
            self.register(table, source_id, NEW, "new", "new row; production PK not reused")

    def plan_fk_rows(self, table, customer_column=None, address_column="address_id"):
        for row in self.rows.get(table, []):
            source_id = row.get("id")
            customer = None
            if customer_column and row.get(customer_column) is not None:
                customer, missing = self.resolve_fk(table, customer_column, row.get(customer_column))
                if missing:
                    self.register(table, source_id, None, "blocked", f"{customer_column} unresolved")
                    continue
            address, _ = self.resolve_fk(table, address_column, row.get(address_column)) if address_column in row else (None, False)
            if address is None and customer is not None and address_column in row and row.get(address_column) is not None:
                address, why = self.customer_address(customer)
                if address is None:
                    self.register(table, source_id, None, "blocked", why)
                    continue
            location = self.location_match(address) if "location_id" in row else None
            _, blocking, nulled = self.apply_fks(table, row, address=address, location=location)
            if blocking:
                self.register(table, source_id, None, "blocked", "unmapped NOT NULL: " + ",".join(blocking))
            else:
                reason = "all FKs mapped"
                if nulled:
                    reason += "; nulled: " + ",".join(nulled)
                self.register(table, source_id, NEW, "new", reason)

    def plan_children(self, table, parent_table, parent_column):
        for row in self.rows.get(table, []):
            source_id = row.get("id")
            parent_id = row.get(parent_column)
            parent = self.map.get((parent_table, parent_id)) if parent_id is not None else None
            if parent is None:
                self.register(table, source_id, None, "blocked", f"parent {parent_table}:{parent_id} outside delta or blocked")
                continue
            customer = None
            for column in ("recipient_customer_id",):
                if column in row and row.get(column) is not None:
                    customer, missing = self.resolve_fk(table, column, row.get(column))
                    if missing:
                        self.register(table, source_id, None, "blocked", f"{column} unresolved")
                        break
            else:
                address = None
                if "address_id" in row and row.get("address_id") is not None:
                    address, _ = self.resolve_fk(table, "address_id", row.get("address_id"))
                location = self.location_match(address) if "location_id" in row else None
                _, blocking, nulled = self.apply_fks(table, row, address=address, location=location)
                if blocking:
                    self.register(table, source_id, None, "blocked", "unmapped NOT NULL: " + ",".join(blocking))
                else:
                    reason = "parent " + str(parent)
                    if nulled:
                        reason += "; nulled: " + ",".join(nulled)
                    self.register(table, source_id, NEW, "new", reason)

    # ---------------------------------------------------------------- driver
    def run(self, rows):
        self.rows = rows
        for table in ("brands", "groups", "types"):
            self.plan_dimension_names(table)
        self.plan_products()
        self.plan_product_items()
        self.plan_equipments()
        for table in ("transactions", "service_orders", "movements", "shipments", "volumes", "maintenances", "return_confirmations", "romaneios"):
            self.plan_new_parents(table)
        self.plan_fk_rows("transaction_items")
        self.plan_children("service_order_items", "service_orders", "service_order_id")
        self.plan_children("movement_items", "movements", "movement_id")
        self.plan_children("shipment_items", "shipments", "shipment_id")
        self.plan_fk_rows("volume_items")
        self.plan_fk_rows("equipment_history")
        self.plan_children("maintenance_items", "maintenances", "maintenance_id")
        self.plan_fk_rows("return_confirmations_items")


def load_delta(path, start, end):
    raw = Path(path).read_bytes()
    entries = json.loads(raw)
    rows = defaultdict(list)
    updated_only = Counter()
    excluded = Counter()
    audit = Counter()
    for item in entries:
        table = item.get("tabela", "unknown")
        row = normalize_record(item.get("registro_json"))
        if table == "audit_logs":
            audit[str(row.get("action", "")).lower()] += 1
            continue
        if table == "report_exports":
            excluded[table] += 1
            continue
        created = creation_date(table, row)
        if created is None:
            rows[table].append(row)  # sem data: mantem para revisao
        elif in_window(created, start, end):
            rows[table].append(row)
        elif in_window(as_date(row.get("updated_at")), start, end):
            updated_only[table] += 1
        else:
            excluded[table] += 1
    return raw, rows, updated_only, excluded, audit


def load_lookups(engine, tables):
    from sqlalchemy import text
    lookups, fks, nullables = {}, {}, {}
    with engine.connect() as conn:
        database = conn.execute(text("SELECT DATABASE()")).scalar()
        existing = {r[0] for r in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"))}
        for table in tables:
            if table not in existing:
                continue
            columns = {r[0]: r[1] for r in conn.execute(text(
                "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": table})}
            needed = [c for c in ("id", "deleted_at", "name", "number", "serial_number", "code",
                                  "addressable_type", "addressable_id", "legacy_customer_id",
                                  "product_id", "category_id", "condition_id", "address_id",
                                  "location_id", "organization_id", "area", "customer_id")
                      if c in columns]
            if "id" not in needed:
                continue
            rows = conn.execute(text(
                "SELECT " + ", ".join(f"`{c}`" for c in needed) + f" FROM `{table}`")).all()
            lookups[table] = [dict(zip(needed, r)) for r in rows]
            nullables[table] = {c for c, v in columns.items() if v == "YES"}
            fks[table] = {r[0]: r[1] for r in conn.execute(text(
                "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND REFERENCED_TABLE_NAME IS NOT NULL"),
                {"t": table})}
        # Tabelas referenciadas apenas para comprovar existencia do ID.
        referenced = set().union(*(set(v.values()) for v in fks.values())) if fks else set()
        for table in sorted(referenced - set(lookups)):
            if table not in existing:
                continue
            columns = {r[0] for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": table})}
            wanted = [c for c in ("id", "deleted_at", "name") if c in columns]
            if "id" not in wanted:
                continue
            rows = conn.execute(text(
                "SELECT " + ", ".join(f"`{c}`" for c in wanted) + f" FROM `{table}`")).all()
            lookups[table] = [dict(zip(wanted, r)) for r in rows]
    return database, lookups, fks, nullables


def write_csv(path, rows, fields):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Fase 2: aplicacao transacional com IDs novos (escopo controlado)
# ---------------------------------------------------------------------------
SCOPE_ORDER = [
    "brands", "groups", "types", "products", "product_items",
    "transactions", "transaction_items", "equipments",
    "service_orders", "service_order_items", "movements", "movement_items",
    "shipments", "volumes", "shipment_items", "volume_items", "equipment_history",
    "maintenances", "maintenance_items", "return_confirmations",
    "return_confirmations_items", "romaneios",
]


def select_scope(rows, escopo):
    """Retorna as linhas do grafo fechado do escopo pedido."""
    if escopo == "completo":
        return {table: rows.get(table, []) for table in SCOPE_ORDER}

    movements = rows.get("movements", [])
    movement_ids = {r.get("id") for r in movements}
    service_order_ids = {r.get("service_order_id") for r in movements if r.get("service_order_id")}
    service_orders = [r for r in rows.get("service_orders", []) if r.get("id") in service_order_ids]
    service_order_items = [r for r in rows.get("service_order_items", [])
                           if r.get("service_order_id") in service_order_ids]
    movement_items = [r for r in rows.get("movement_items", [])
                      if r.get("movement_id") in movement_ids]
    movement_item_ids = {r.get("id") for r in movement_items}

    shipment_items_all = rows.get("shipment_items", [])
    shipment_ids = {r.get("shipment_id") for r in shipment_items_all
                    if r.get("movement_item_id") in movement_item_ids}
    shipments = [r for r in rows.get("shipments", []) if r.get("id") in shipment_ids]
    shipment_items = [r for r in shipment_items_all if r.get("shipment_id") in shipment_ids]

    volume_ids = {r.get("volume_id") for r in shipment_items if r.get("volume_id")}
    volume_ids |= {r.get("volume_id") for r in rows.get("volume_items", [])
                   if r.get("movement_item_id") in movement_item_ids and r.get("volume_id")}
    volumes = [r for r in rows.get("volumes", []) if r.get("id") in volume_ids]
    volume_items = [r for r in rows.get("volume_items", []) if r.get("volume_id") in volume_ids]

    equipment_ids = set()
    for group in (movement_items, service_order_items, shipment_items, volume_items):
        equipment_ids |= {r.get("equipment_id") for r in group if r.get("equipment_id")}
    equipment_history = [r for r in rows.get("equipment_history", [])
                         if r.get("equipment_id") in equipment_ids
                         or r.get("movement_item_id") in movement_item_ids
                         or not r.get("movement_item_id")]
    equipment_ids |= {r.get("equipment_id") for r in equipment_history if r.get("equipment_id")}
    equipments = [r for r in rows.get("equipments", []) if r.get("id") in equipment_ids]

    product_item_ids = set()
    for group in (movement_items, volume_items, equipments):
        product_item_ids |= {r.get("product_item_id") for r in group if r.get("product_item_id")}
    product_items = [r for r in rows.get("product_items", []) if r.get("id") in product_item_ids]

    transaction_item_ids = {r.get("transaction_item_id") for r in equipments if r.get("transaction_item_id")}
    transaction_items = [r for r in rows.get("transaction_items", [])
                         if r.get("id") in transaction_item_ids]
    transaction_ids = {r.get("transaction_id") for r in transaction_items if r.get("transaction_id")}
    transactions = [r for r in rows.get("transactions", []) if r.get("id") in transaction_ids]

    product_ids = {r.get("product_id") for r in product_items if r.get("product_id")}
    product_ids |= {r.get("product_id") for r in service_order_items if r.get("product_id")}
    product_ids |= {r.get("product_id") for r in transaction_items if r.get("product_id")}
    products = [r for r in rows.get("products", []) if r.get("id") in product_ids]
    brand_ids = {r.get("brand_id") for r in products if r.get("brand_id")}
    group_ids = {r.get("group_id") for r in products if r.get("group_id")}
    type_ids = {r.get("type_id") for r in products if r.get("type_id")}
    return {
        "brands": [r for r in rows.get("brands", []) if r.get("id") in brand_ids],
        "groups": [r for r in rows.get("groups", []) if r.get("id") in group_ids],
        "types": [r for r in rows.get("types", []) if r.get("id") in type_ids],
        "products": products,
        "product_items": product_items,
        "transactions": transactions,
        "transaction_items": transaction_items,
        "equipments": equipments,
        "service_orders": service_orders,
        "service_order_items": service_order_items,
        "movements": movements,
        "movement_items": movement_items,
        "shipments": shipments,
        "shipment_items": shipment_items,
        "volumes": volumes,
        "volume_items": volume_items,
        "equipment_history": equipment_history,
    }


def resolve_values(planner, inserted, table, row):
    """Monta os valores de INSERT trocando FKs pelos IDs locais ja criados."""
    values = {}
    for column, value in row.items():
        if column == "id":
            continue
        if isinstance(value, (dict, list)):
            values[column] = json.dumps(value, ensure_ascii=False)
            continue
        if table == "transaction_items" and column == "equipment_id":
            # Elo circular com equipments: preenchido depois dos equipamentos.
            values[column] = None
            continue
        target = planner.fks.get(table, {}).get(column)
        if target is None:
            values[column] = value
            continue
        if value is None:
            values[column] = None
            continue
        mapped = planner.map.get((target, value))
        if mapped is None and (target, value) not in planner.map:
            if target in DIMENSIONS:
                mapped = planner.dim_exists(target, value)
                if mapped is not None:
                    planner.register(target, value, mapped, "matched", "dimension id")
            else:
                local = planner.local_by_id(target, value)
                if local is not None:
                    mapped = local["id"]
                    planner.register(target, value, mapped, "matched", "local id exists")
        if mapped is None and target == "addresses":
            customer_source = row.get("recipient_customer_id") or row.get("customer_id")
            customer_local = planner.map.get(("customers", customer_source)) if customer_source else None
            if customer_local is None and customer_source is not None:
                customer_local = planner.dim_exists("customers", customer_source)
            if customer_local is not None:
                mapped, _ = planner.customer_address(customer_local)
        if mapped is None and target == "locations":
            address_source = row.get("address_id") or row.get("migrate_customer_id")
            address_local = planner.map.get(("addresses", address_source)) if address_source else None
            if address_local is not None:
                mapped = planner.location_match(address_local)
        if mapped == NEW:
            local = inserted.get((target, value))
            if local is None:
                if column in planner.nullables.get(table, set()):
                    values[column] = None
                    continue
                raise RuntimeError(f"{table}.{column}: dependencia {target}:{value} ainda nao inserida")
            values[column] = local
            continue
        if mapped is None:
            if column in planner.nullables.get(table, set()):
                values[column] = None
                continue
            raise RuntimeError(f"{table}.{column}: FK {target}:{value} nao mapeada")
        values[column] = mapped
    return values


def prune_scope(scope, planner):
    """Remove do grafo equipamentos cujo product_item de origem nao existe."""
    blocked = set()
    for equipment in scope.get("equipments", []):
        source = equipment.get("product_item_id")
        if not source:
            continue
        if (("product_items", source) not in planner.map
                and planner.local_by_id("product_items", source) is None):
            blocked.add(equipment.get("id"))
    if not blocked:
        return scope, blocked
    scope["equipments"] = [r for r in scope.get("equipments", []) if r.get("id") not in blocked]
    scope["equipment_history"] = [r for r in scope.get("equipment_history", [])
                                  if r.get("equipment_id") not in blocked]
    for table in ("movement_items", "volume_items", "shipment_items", "service_order_items"):
        scope[table] = [r for r in scope.get(table, []) if r.get("equipment_id") not in blocked]
    return scope, blocked


def apply_scope(engine, planner, scope, digest, ledger_path):
    from sqlalchemy import text

    ledger = {}
    path = Path(ledger_path)
    if path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            for line in csv.DictReader(stream):
                if line.get("digest") == digest:
                    if not line.get("local_id"):
                        continue
                    ledger[(line["table"], str(line["source_id"]))] = int(line["local_id"])

    inserted = {}
    actions = []
    with engine.begin() as conn:
        for table in SCOPE_ORDER:
            for row in scope.get(table, []):
                source_id = row.get("id")
                key = (table, str(source_id))
                if key in ledger:
                    inserted[(table, source_id)] = ledger[key]
                    planner.map[(table, source_id)] = ledger[key]
                    actions.append({"table": table, "source_id": source_id,
                                    "local_id": ledger[key], "action": "skip_ledger"})
                    continue
                planned = planner.map.get((table, source_id))
                if planned is None:
                    actions.append({"table": table, "source_id": source_id,
                                    "local_id": "", "action": "blocked"})
                    continue
                if planned != NEW:
                    inserted[(table, source_id)] = planned
                    actions.append({"table": table, "source_id": source_id,
                                    "local_id": planned, "action": "matched"})
                    continue
                values = resolve_values(planner, inserted, table, row)
                columns = list(values)
                binds = {f"v{i}": values[c] for i, c in enumerate(columns)}
                sql = ("INSERT INTO `" + table + "` (" + ", ".join(f"`{c}`" for c in columns)
                       + ") VALUES (" + ", ".join(f":v{i}" for i in range(len(columns))) + ")")
                result = conn.execute(text(sql), binds)
                local_id = result.lastrowid
                inserted[(table, source_id)] = local_id
                planner.map[(table, source_id)] = local_id
                actions.append({"table": table, "source_id": source_id,
                                "local_id": local_id, "action": "insert"})
        # Elo circular shipment_items <-> volume_items: preencher volume_id.
        for row in scope.get("volume_items", []):
            shipment_item_source = row.get("shipment_item_id")
            volume_source = row.get("volume_id")
            if not shipment_item_source or not volume_source:
                continue
            shipment_item = inserted.get(("shipment_items", shipment_item_source))
            volume = inserted.get(("volumes", volume_source))
            if shipment_item and volume:
                conn.execute(text("UPDATE `shipment_items` SET `volume_id`=:v WHERE `id`=:i"),
                             {"v": volume, "i": shipment_item})
        # Elo circular transaction_items <-> equipments: preencher equipment_id.
        for row in scope.get("transaction_items", []):
            equipment_source = row.get("equipment_id")
            transaction_item = inserted.get(("transaction_items", row.get("id")))
            if not equipment_source or not transaction_item:
                continue
            equipment = inserted.get(("equipments", equipment_source))
            if equipment is None:
                equipment = planner.map.get(("equipments", equipment_source))
            if equipment and equipment != NEW:
                conn.execute(text("UPDATE `transaction_items` SET `equipment_id`=:e WHERE `id`=:i"),
                             {"e": equipment, "i": transaction_item})

    write_csv(Path(ledger_path), [
        {"digest": digest, "table": a["table"], "source_id": a["source_id"],
         "local_id": a["local_id"], "action": a["action"]}
        for a in actions
    ], ["digest", "table", "source_id", "local_id", "action"])
    return actions



def main(argv=None):
    parser = argparse.ArgumentParser(description="Planejador dry-run do delta de producao")
    parser.add_argument("--json", default="docs/atualizacoes.json")
    parser.add_argument("--inicio", default="2026-11-04")
    parser.add_argument("--fim", default="2026-11-12")
    parser.add_argument("--out-dir", default="ajustes")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--escopo", choices=("movimentos", "completo"), default="movimentos")
    parser.add_argument("--ledger", default="ajustes/delta_ledger.csv")
    args = parser.parse_args(argv)

    start, end = date.fromisoformat(args.inicio), date.fromisoformat(args.fim)
    raw, rows, updated_only, excluded, audit = load_delta(args.json, start, end)

    from config.database import obter_engines
    engine, _ = obter_engines()
    tables = set(rows) | set(DIMENSIONS) | {
        "addresses", "locations", "contracts", "contract_items", "equipments",
        "product_items", "products",
    }
    database, lookups, fks, nullables = load_lookups(engine, tables)

    planner = Planner(lookups, fks, nullables)
    planner.run(rows)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    crosswalk, pending, plans = [], [], []
    for table in sorted(rows):
        for row in rows[table]:
            source_id = row.get("id")
            status = planner.status.get((table, source_id), "blocked")
            target = planner.map.get((table, source_id))
            evidence = planner.evidence.get((table, source_id), "not planned")
            crosswalk.append({
                "source_table": table, "source_id": source_id, "target_table": table,
                "target_id": "" if target in (None, NEW) else target,
                "method": "new" if target == NEW else ("mapped" if target else "blocked"),
                "confidence": "high" if status in ("matched", "new") else "low",
                "status": status, "evidence": evidence, "conflict": "" if status != "blocked" else evidence,
            })
            if status == "blocked":
                pending.append({"table": table, "source_id": source_id,
                                "reason": evidence, "evidence": json.dumps(row, ensure_ascii=False, default=str)})
    for table in sorted(rows):
        counts = Counter(r["status"] for r in crosswalk if r["source_table"] == table)
        for action in ("matched", "candidate_review", "new", "blocked"):
            if counts[action]:
                plans.append({"table": table, "action": action, "count": counts[action], "detail": "dry-run"})
        print(f"{table}: source={len(rows[table])} matched={counts['matched']} "
              f"review={counts['candidate_review']} new={counts['new']} blocked={counts['blocked']}")

    write_csv(out / "delta_crosswalk.csv", crosswalk,
              ["source_table", "source_id", "target_table", "target_id", "method",
               "confidence", "status", "evidence", "conflict"])
    write_csv(out / "delta_plano.csv", plans, ["table", "action", "count", "detail"])
    write_csv(out / "delta_pendencias.csv", pending, ["table", "source_id", "reason", "evidence"])
    summary = {
        "source_digest_sha256": hashlib.sha256(raw).hexdigest(),
        "window": {"inicio": args.inicio, "fim": args.fim},
        "database": database,
        "importer_version": VERSION,
        "audit_actions": dict(audit),
        "updated_only_out_of_scope": dict(updated_only),
        "excluded_outside_window": dict(excluded),
        "per_table": {
            table: {"source": len(rows[table]),
                    **dict(Counter(r["status"] for r in crosswalk if r["source_table"] == table))}
            for table in sorted(rows)
        },
    }
    (out / "delta_resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.apply:
        from collections import Counter as _Counter
        scope = select_scope(rows, args.escopo)
        scope, pruned = prune_scope(scope, planner)
        if pruned:
            print(f"  Equipamentos fora do apply (product_item ausente): {sorted(pruned)}")
        print("\nAplicando escopo:", args.escopo)
        for table in SCOPE_ORDER:
            print(f"  {table}: {len(scope.get(table, []))} linhas no grafo")
        actions = apply_scope(engine, planner, scope,
                              hashlib.sha256(raw).hexdigest(), args.ledger)
        counts = _Counter(a["action"] for a in actions)
        print("Resultado:", dict(counts))
        write_csv(out / "delta_aplicado.csv", actions,
                  ["table", "source_id", "local_id", "action"])
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
