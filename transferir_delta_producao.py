#!/usr/bin/env python3
"""Copy a bounded production delta into local, preserving primary/foreign keys.

Default is read-only preview. Apply needs --apply and typing APPLY. No truncate.
"""
import argparse
import csv
from datetime import date
import re
import unicodedata


# Queries deliberately parameterize values; table/column identifiers are constants.
def _text(sql):
    from sqlalchemy import text
    return text(sql)


def _expanding(sql, name):
    from sqlalchemy import bindparam, text
    return text(sql).bindparams(bindparam(name, expanding=True))


def _window(col, start, end):
    return f"`{col}` >= :start AND `{col}` < :end"


def normalize_key(value):
    """Normalize natural identifiers without treating numeric IDs as keys."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def classify_candidates(source, candidates, method="natural_key"):
    """Return unique-only mapping status; conflicts and missing keys stay pending."""
    candidates = list(candidates)
    if not source:
        return {"target_id": "", "method": method, "confidence": "none", "status": "pending", "conflict": "missing_source_key"}
    if len(candidates) == 1:
        return {"target_id": candidates[0], "method": method, "confidence": "high", "status": "candidate_review", "conflict": ""}
    if len(candidates) > 1:
        return {"target_id": "|".join(map(str, candidates)), "method": method, "confidence": "ambiguous", "status": "conflict", "conflict": "multiple_candidates"}
    return {"target_id": "", "method": method, "confidence": "none", "status": "pending", "conflict": "no_candidate"}


def candidate_for(row, target_rows, key_fields, corroborate=()):
    """Find candidates by normalized natural key, then enforce available evidence."""
    normalized = normalize_key(row.get(key_fields[0]))
    if not normalized:
        return classify_candidates("", [], "+".join(key_fields))
    matched = []
    for target in target_rows:
        if normalize_key(target.get(key_fields[0])) != normalized:
            continue
        if any(row.get(field) is not None and target.get(field) is not None and str(row[field]) != str(target[field]) for field in corroborate):
            continue
        matched.append(target.get("id"))
    return classify_candidates(normalized, matched, "+".join(key_fields + list(corroborate)))


def write_crosswalk_csv(rows, path):
    fields = ("source_table", "source_id", "candidate_target_table", "candidate_target_id", "match_method", "confidence", "source_parent_evidence", "target_parent_evidence", "status", "conflict")
    with open(path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_crosswalk(source_rows, target_rows):
    """Audit natural-key candidates; never uses numeric PKs as identity."""
    result = []
    src_equipment = source_rows.get("equipments", [])
    dst_equipment = target_rows.get("equipments", [])
    equipment_map = {}
    for row in src_equipment:
        number = row.get("number", row.get("tombo"))
        target = [t for t in dst_equipment if normalize_key(t.get("number", t.get("tombo"))) == normalize_key(number)] if normalize_key(number) else []
        # Serial only corroborates; it cannot rescue an absent/nonmatching number.
        status = classify_candidates(normalize_key(number), [t.get("id") for t in target if not row.get("serial_number") or not t.get("serial_number") or normalize_key(row.get("serial_number")) == normalize_key(t.get("serial_number"))], "number+serial_corroboration")
        equipment_map[row.get("id")] = status.get("target_id") if status["status"] == "candidate_review" else None
        result.append(_crosswalk_row("equipments", row, "equipments", status, "number/tombo; serial corroboration"))
    accepted = {"equipments": equipment_map}
    for table in ("service_orders", "movements", "shipments", "service_order_items", "movement_items", "shipment_items", "shipment_movements", "equipment_history", "maintenances", "maintenance_items"):
        for row in source_rows.get(table, []):
            parent_field = parent = mapped_parent = None
            if table == "movements":
                key_fields, corroborate = ("number",), ("movement_date", "organization_id", "recipient_customer_id")
            elif table == "service_orders":
                key_fields = ("created_at", "movement_type_id", "recipient_customer_id", "organization_id", "user_id")
                corroborate = ()
            else:
                parent_field = next((field for field in ("service_order_id", "movement_id", "shipment_id", "maintenance_id") if row.get(field) is not None), None)
                parent = row.get(parent_field) if parent_field else None
                mapped_parent = accepted.get({"service_order_id":"service_orders", "movement_id":"movements", "shipment_id":"shipments", "maintenance_id":"maintenances"}.get(parent_field, ""), {}).get(parent)
                # Child candidates are considered only if the source parent has
                # a unique accepted candidate; equality of raw IDs is forbidden.
                if not parent or not mapped_parent:
                    status = classify_candidates("", [], "parent+natural_attributes")
                    result.append(_crosswalk_row(table, row, table, status, "parent unmapped", parent_field, parent, ""))
                    continue
                key_fields = ("alias", "quantity", "movement_type_id", "equipment_id") if table == "service_order_items" else ("equipment_id", "operation_type", "movement_type_id") if table == "movement_items" else ("delivery_date", "address_id", "status_id") if table == "shipment_items" else ("status_id", "created_by", "created_at") if table == "shipments" else ("equipment_id", "occurred_at", "status_id") if table == "equipment_history" else ("equipment_id", "maintenance_type_id", "maintenance_status_id", "details") if table == "maintenance_items" else ("equipment_id", "created_at", "updated_at", "maintenance_date") if table == "maintenances" else ("status_id",)
                corroborate = ()
            pool = target_rows.get(table, [])
            if table == "service_orders":
                candidates = [t for t in pool if all(row.get(k) is None or t.get(k) is None or str(row[k]) == str(t[k]) for k in key_fields)]
                # Composite requires at least two populated fields; all present
                # identity attributes must agree and exactly one candidate.
                populated = [k for k in key_fields if row.get(k) is not None]
                candidates = [t for t in candidates if len(populated) >= 2 and all(t.get(k) is not None and str(t[k]) == str(row[k]) for k in populated)]
                status = classify_candidates("|".join(str(row.get(k)) for k in populated), [t.get("id") for t in candidates], "composite:" + "+".join(key_fields))
            elif table == "movements":
                status = candidate_for(row, pool, ["number"], corroborate)
            else:
                candidates = [t for t in pool if all(row.get(k) is not None and t.get(k) is not None and normalize_key(row[k]) == normalize_key(t[k]) for k in key_fields if k in row and k in t)]
                # Restrict by the mapped parent relationship where the child has one.
                if parent_field:
                    target_parent_field = parent_field
                    candidates = [t for t in candidates if t.get(target_parent_field) == mapped_parent]
                status = classify_candidates("|".join(str(row.get(k)) for k in key_fields), [t.get("id") for t in candidates], "parent+" + "+".join(key_fields))
            if status["status"] == "candidate_review":
                accepted.setdefault(table, {})[row.get("id")] = status["target_id"]
            result.append(_crosswalk_row(table, row, table, status, "+".join(key_fields + corroborate), parent_field if table not in ("service_orders", "movements") else "", parent, mapped_parent if parent_field else ""))
    return result


def _crosswalk_row(table, row, target_table, match, evidence, parent_field="", parent_id="", target_parent_id=""):
    return {"source_table": table, "source_id": row.get("id", ""), "candidate_target_table": target_table,
            "candidate_target_id": match["target_id"], "match_method": match["method"], "confidence": match["confidence"],
            "source_parent_evidence": f"{parent_field}={parent_id}; {evidence}", "target_parent_evidence": f"mapped_parent={target_parent_id}" if target_parent_id else "unresolved",
            "status": match["status"], "conflict": match["conflict"]}


def collect(source, start, end):
    """Build graph IDs from production, keeping every selection parent-linked."""
    p = {"start": start, "end": end}
    result = {}
    # Service orders are selected directly and through time-windowed movements.
    movement_where = f"({_window('created_at', start, end)} OR {_window('movement_date', start, end)})"
    result['movements'] = _ids(source, f"SELECT id FROM movements WHERE {movement_where}", p)
    # Shipment scope includes all three date predicates, items linked to selected
    # shipments, and shipments reached via qualifying shipment items.
    ship_items = _ids(source, f"SELECT id FROM shipment_items WHERE {_window('created_at',start,end)} OR {_window('delivery_date',start,end)}", p)
    ship_ids = _ids(source, f"SELECT id FROM shipments WHERE {_window('created_at',start,end)} OR id IN (SELECT shipment_id FROM shipment_items WHERE {_window('created_at',start,end)} OR {_window('delivery_date',start,end)})", p)
    result['shipments'] = ship_ids
    result['shipment_items'] = sorted(set(ship_items) | set(_ids(source, "SELECT id FROM shipment_items WHERE shipment_id IN :ids", {"ids": ship_ids}, expanding=True)))
    result['shipment_movements'] = _ids(source, "SELECT id FROM shipment_movements WHERE shipment_id IN :ids", {"ids": ship_ids}, expanding=True)

    # Shipment-derived movement IDs are part of the movement CTE scope.
    if result['shipment_items']:
        linked_movements = _ids(source, "SELECT DISTINCT mi.movement_id FROM shipment_items si JOIN movement_items mi ON mi.id = si.movement_item_id WHERE si.id IN :ids AND mi.movement_id IS NOT NULL", {"ids": result['shipment_items']}, expanding=True)
        result['movements'] = sorted(set(result['movements']) | set(linked_movements))
    # Rebuild order scope after all directly selected and shipment-linked movements.
    qualifying_order_ids = []
    if result['movements']:
        qualifying_order_ids = _ids(source, "SELECT DISTINCT service_order_id FROM movements WHERE id IN :ids AND service_order_id IS NOT NULL", {"ids": result['movements']}, expanding=True)
    created_orders = _ids(source, f"SELECT id FROM service_orders WHERE {_window('created_at',start,end)}", p)
    result['service_orders'] = sorted(set(created_orders) | set(qualifying_order_ids))
    if result['service_orders']:
        more = _ids(source, "SELECT id FROM movements WHERE service_order_id IN :ids", {"ids": result['service_orders']}, expanding=True)
        result['movements'] = sorted(set(result['movements']) | set(more))
    result['service_order_items'] = _ids(source, "SELECT id FROM service_order_items WHERE service_order_id IN :ids", {"ids": result['service_orders']}, expanding=True)
    result['movement_items'] = _ids(source, "SELECT id FROM movement_items WHERE movement_id IN :ids", {"ids": result['movements']}, expanding=True)
    result['service_order_item_extra_equipments'] = _ids(source, "SELECT id FROM service_order_item_extra_equipments WHERE service_order_item_id IN :ids", {"ids": result['service_order_items']}, expanding=True)

    # History is selected by occurred_at and by each scoped graph edge. Equipment
    # histories for selected equipment are included below after equipment IDs.
    history_links = []
    params = dict(p)
    for idx, (column, ids) in enumerate((('movement_item_id', result['movement_items']), ('service_order_item_id', result['service_order_items']), ('shipment_item_id', result['shipment_items']))):
        if ids:
            history_links.append(f"`{column}` IN :hist{idx}")
            params[f"hist{idx}"] = ids
    history_where = _window('occurred_at', start, end)
    if history_links:
        history_where += " AND (" + " OR ".join(history_links) + ")"
        result['equipment_history'] = _ids(source, "SELECT id FROM equipment_history WHERE " + history_where, params, expanding_keys=[f"hist{i}" for i in range(3) if f"hist{i}" in params])
    else:
        result['equipment_history'] = []

    maint_where = " OR ".join(_window(c, start, end) for c in ('created_at','updated_at','maintenance_date'))
    item_where = " OR ".join(_window(c, start, end) for c in ('created_at','updated_at'))
    result['maintenances'] = _ids(source, f"SELECT id FROM maintenances WHERE ({maint_where}) OR id IN (SELECT maintenance_id FROM maintenance_items WHERE {item_where})", p)
    result['maintenance_items'] = _ids(source, "SELECT id FROM maintenance_items WHERE maintenance_id IN :ids", {"ids": result['maintenances']}, expanding=True)
    result['maintenance_items'] = sorted(set(result['maintenance_items']) | set(_ids(source, f"SELECT id FROM maintenance_items WHERE {item_where}", p)))
    if result['maintenance_items']:
        linked = _ids(source, "SELECT movement_id FROM maintenance_items WHERE id IN :ids AND movement_id IS NOT NULL", {"ids": result['maintenance_items']}, expanding=True)
        result['movements'] = sorted(set(result['movements']) | set(linked))
        # Reclose the graph: maintenance-linked movements may imply orders,
        # order children, further movements, and shipment parents/children.
        linked_orders = _ids(source, "SELECT DISTINCT service_order_id FROM movements WHERE id IN :ids AND service_order_id IS NOT NULL", {"ids": result['movements']}, expanding=True) if result['movements'] else []
        result['service_orders'] = sorted(set(result['service_orders']) | set(linked_orders))
        if result['service_orders']:
            result['movements'] = sorted(set(result['movements']) | set(_ids(source, "SELECT id FROM movements WHERE service_order_id IN :ids", {"ids": result['service_orders']}, expanding=True)))
        result['service_order_items'] = sorted(set(result['service_order_items']) | set(_ids(source, "SELECT id FROM service_order_items WHERE service_order_id IN :ids", {"ids": result['service_orders']}, expanding=True)))
        result['movement_items'] = sorted(set(result['movement_items']) | set(_ids(source, "SELECT id FROM movement_items WHERE movement_id IN :ids", {"ids": result['movements']}, expanding=True)))
        if result['movement_items']:
            add_ship_items = _ids(source, "SELECT id FROM shipment_items WHERE movement_item_id IN :ids", {"ids": result['movement_items']}, expanding=True)
            result['shipment_items'] = sorted(set(result['shipment_items']) | set(add_ship_items))
            if add_ship_items:
                add_shipments = _ids(source, "SELECT DISTINCT shipment_id FROM shipment_items WHERE id IN :ids", {"ids": add_ship_items}, expanding=True)
                result['shipments'] = sorted(set(result['shipments']) | set(add_shipments))
                result['shipment_movements'] = sorted(set(result['shipment_movements']) | set(_ids(source, "SELECT id FROM shipment_movements WHERE shipment_id IN :ids", {"ids": add_shipments}, expanding=True)))

    equipment_ids = set()
    for table in ('movement_items','service_order_items','equipment_history'):
        if result[table]:
            equipment_ids.update(_ids(source, f"SELECT DISTINCT equipment_id FROM `{table}` WHERE id IN :ids AND equipment_id IS NOT NULL", {"ids": result[table]}, expanding=True))
    if result['shipment_items']:
        equipment_ids.update(_ids(source, "SELECT DISTINCT mi.equipment_id FROM shipment_items si JOIN movement_items mi ON mi.id = si.movement_item_id WHERE si.id IN :ids AND mi.equipment_id IS NOT NULL", {"ids": result['shipment_items']}, expanding=True))
    if result['maintenances']:
        equipment_ids.update(_ids(source, "SELECT DISTINCT equipment_id FROM maintenances WHERE id IN :ids AND equipment_id IS NOT NULL", {"ids": result['maintenances']}, expanding=True))
    if result['maintenance_items']:
        equipment_ids.update(_ids(source, "SELECT DISTINCT e.equipment_id FROM maintenance_items mi JOIN maintenances e ON e.id = mi.maintenance_id WHERE mi.id IN :ids AND e.equipment_id IS NOT NULL", {"ids": result['maintenance_items']}, expanding=True))
    if result['maintenances']:
        equipment_ids.update(_ids(source, "SELECT DISTINCT equipment_id FROM maintenances WHERE id IN :ids AND equipment_id IS NOT NULL", {"ids": result['maintenances']}, expanding=True))
    result['equipment_ids'] = sorted(equipment_ids)
    if equipment_ids:
        history = _ids(source, f"SELECT id FROM equipment_history WHERE {_window('occurred_at',start,end)} AND equipment_id IN :equipment_ids", {**p, "equipment_ids": sorted(equipment_ids)}, expanding_keys=['equipment_ids'])
        result['equipment_history'] = sorted(set(result['equipment_history']) | set(history))
        # History rows can themselves reference additional in-scope equipment.
        if result['equipment_history']:
            equipment_ids.update(_ids(source, "SELECT DISTINCT equipment_id FROM equipment_history WHERE id IN :ids AND equipment_id IS NOT NULL", {"ids": result['equipment_history']}, expanding=True))
    return result


def _ids(conn, sql, params=None, expanding=False, expanding_keys=None):
    from sqlalchemy import bindparam, text
    stmt = text(sql)
    names = (["ids"] if expanding else []) + (expanding_keys or [])
    if names:
        stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in names))
    return list(conn.execute(stmt, params or {}).scalars())


TABLE_ORDER = ('service_orders','service_order_items','movements','movement_items','service_order_item_extra_equipments','shipments','shipment_movements','shipment_items','maintenances','maintenance_items','equipment_history')
EQUIPMENT_COLUMNS = ('status_id','current_status_id','updated_at','current_location_id','address_id')


def preview_delta(source, start, end):
    selected = collect(source, start, end)
    print(f"Intervalo: [{start}, {end})")
    for table in TABLE_ORDER:
        print(f"{table}: {len(selected.get(table, []))}")
    print(f"equipments a atualizar: {len(selected['equipment_ids'])}")
    print("PRÉVIA: nenhuma linha gravada; revisar o CSV de correspondências.")
    return selected


def preview_crosswalk(source, target, selected, output):
    """Fetch only selected source rows plus target natural-key candidate fields."""
    from sqlalchemy import inspect
    inspector = inspect(source)
    source_rows, target_rows = {}, {}
    tables = ('equipments',) + TABLE_ORDER
    for table in tables:
        ids = selected.get('equipment_ids', []) if table == 'equipments' else selected.get(table, [])
        src_cols = [c['name'] for c in inspector.get_columns(table)]
        # Candidate attributes are schema-dependent; don't assume optional columns.
        key_cols = {'id','number','tombo','serial_number','movement_date','created_at','updated_at','organization_id','recipient_customer_id','customer_id','user_id','movement_type_id','deadline','details','status_id','alias','quantity','equipment_id','operation_type','shipment_id','movement_item_id','delivery_date','address_id','service_order_id','maintenance_id','maintenance_type_id','maintenance_status_id','occurred_at','movement_id','service_order_item_id','shipment_item_id','created_by'}
        cols = [c for c in src_cols if c in key_cols]
        if ids:
            stmt = _expanding(f"SELECT {', '.join('`'+c+'`' for c in cols)} FROM `{table}` WHERE id IN :ids", 'ids')
            source_rows[table] = [dict(r) for r in source.execute(stmt, {'ids': ids}).mappings()]
        else:
            source_rows[table] = []
        target_cols = [c['name'] for c in inspect(target).get_columns(table)]
        use_cols = [c for c in target_cols if c in key_cols]
        target_rows[table] = [dict(r) for r in target.execute(_text(f"SELECT {', '.join('`'+c+'`' for c in use_cols)} FROM `{table}`")).mappings()]
    rows = build_crosswalk(source_rows, target_rows)
    write_crosswalk_csv(rows, output)
    print(f"Crosswalk audit: {output} ({len(rows)} candidatos; nenhum write)")
    return rows


def main(argv=None, source_engine=None, target_engine=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2026-11-04')
    parser.add_argument('--end', default='2026-11-13', help='limite exclusivo')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--crosswalk-csv', default='delta_crosswalk_review.csv')
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start >= end:
        parser.error('--start deve anteceder --end')
    if args.apply:
        parser.error(
            '--apply está temporariamente bloqueado até existir de/para auditado '
            'entre IDs de produção e destino local.'
        )
    if source_engine is None or target_engine is None:
        from config import database
        prod = getattr(database, 'obter_engine_producao', None)
        local = getattr(database, 'obter_engine_local_refatorado', None)
        if prod is None or local is None:
            raise RuntimeError('config/database.py precisa definir obter_engine_producao e obter_engine_local_refatorado (PROD_DB_* / LOCAL_DB_*).')
        source_engine = source_engine or prod()
        target_engine = target_engine or local()
    with source_engine.connect() as src:
        selected = preview_delta(src, start, end)
        with target_engine.connect() as target:
            preview_crosswalk(src, target, selected, args.crosswalk_csv)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
