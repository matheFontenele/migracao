"""Aplica um delta JSON da produção ao banco local, com preflight transacional.

O padrão é dry-run. Registros de audit_logs servem para validar e interpretar
eventos, mas não são copiados, pois seus IDs são locais e frequentemente colidem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict, deque
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from config.database import obter_engines


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "docs" / "atualizacoes.json"
LOG_DIR = BASE_DIR / "ajustes" / "logs_atualizacoes_producao"
MODEL_TABLES = {
    "contract_item": "contract_items",
    "event_additive": "event_additives",
    "contract_event": "contract_events",
    "contract": "contracts",
    "maintenance_item": "maintenance_items",
    "service_order": "service_orders",
    "movement": "movements",
    "service_order_item": "service_order_items",
    "movement_item": "movement_items",
    "equipment": "equipments",
    "volume": "volumes",
    "volume_item": "volume_items",
}


def qi(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def norm(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, str):
        # Datas ISO puras também são parseáveis como números/datetimes em
        # algumas rotas de normalização; preserva a equivalência com DATE SQL.
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        try:
            return Decimal(value).normalize()
        except InvalidOperation:
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                tzinfo=None
            ).isoformat(sep=" ")
        except ValueError:
            return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return value


def db_value(value, column_type: str):
    if isinstance(value, str) and column_type in {"datetime", "timestamp"}:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return value
    if isinstance(value, str) and column_type == "date":
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def load_delta(path: Path):
    with path.open(encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("A raiz do JSON deve ser uma lista.")
    rows, audits = defaultdict(list), []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not item.get("tabela"):
            raise ValueError(f"Registro {index}: campo 'tabela' ausente.")
        payload = item.get("registro_json")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError(f"Registro {index}: registro_json inválido.")
        if item["tabela"] == "audit_logs":
            audits.append(payload)
        else:
            rows[item["tabela"]].append(payload)
    return rows, audits


def schema_metadata(conn, schema: str, tables: set[str]):
    result = {}
    foreign_keys = []
    for table in sorted(tables):
        exists = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema=:schema AND table_name=:table AND table_type='BASE TABLE'
        """), {"schema": schema, "table": table}).scalar_one()
        if not exists:
            raise ValueError(f"Tabela do dump ausente no destino: {table}")
        cols = conn.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema=:schema AND table_name=:table
        """), {"schema": schema, "table": table}).all()
        pks = [r[0] for r in conn.execute(text("""
            SELECT column_name FROM information_schema.key_column_usage
            WHERE table_schema=:schema AND table_name=:table AND constraint_name='PRIMARY'
            ORDER BY ordinal_position
        """), {"schema": schema, "table": table})]
        if len(pks) != 1:
            raise ValueError(f"{table}: a sincronização requer PK simples; encontrada {pks}")
        result[table] = {"columns": dict(cols), "pk": pks[0]}
        foreign_keys.extend(conn.execute(text("""
            SELECT column_name, referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema=:schema AND table_name=:table
              AND referenced_table_name IS NOT NULL
        """), {"schema": schema, "table": table}).all())
    return result, foreign_keys


def audit_index(audits):
    index = defaultdict(list)
    actions = Counter()
    unmapped = []
    for event in audits:
        action = str(event.get("action", "")).lower()
        actions[action] += 1
        model = str(event.get("model_type", "")).split("\\")[-1]
        table = MODEL_TABLES.get(model)
        model_id = event.get("model_id")
        if not table or model_id is None:
            unmapped.append(event)
            continue
        index[(table, str(model_id))].append(event)
    return index, actions, unmapped


def fetch_existing(conn, table: str, pk: str, value):
    return conn.execute(text(
        f"SELECT * FROM {qi(table)} WHERE {qi(pk)}=:pk LIMIT 1"
    ), {"pk": value}).mappings().first()


def changed_audit_fields(event: dict, table_columns: set[str]):
    before, after = event.get("before"), event.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    return {
        field for field in table_columns & before.keys() & after.keys()
        if norm(before[field]) != norm(after[field])
    }


def make_plan(conn, schema: str, rows_by_table, audits):
    tables = set(rows_by_table)
    metadata, foreign_keys = schema_metadata(conn, schema, tables)
    audit_map, audit_actions, unmapped_audits = audit_index(audits)
    plan, conflicts, unchanged = [], [], 0
    source_ids = defaultdict(set)

    for table, rows in rows_by_table.items():
        info = metadata[table]
        pk = info["pk"]
        columns = set(info["columns"])
        for source in rows:
            unknown = set(source) - columns
            if unknown:
                conflicts.append({"table": table, "id": source.get(pk),
                                  "reason": f"Colunas ausentes no destino: {sorted(unknown)}"})
                continue
            if source.get(pk) is None:
                conflicts.append({"table": table, "id": None, "reason": "PK ausente"})
                continue
            source_id = source[pk]
            key = str(source_id)
            if key in source_ids[table]:
                conflicts.append({"table": table, "id": source_id, "reason": "PK repetida no dump"})
                continue
            source_ids[table].add(key)
            existing = fetch_existing(conn, table, pk, source_id)
            events = audit_map.get((table, key), [])
            update_events = [e for e in events if str(e.get("action", "")).lower() == "update"]
            delete_events = [e for e in events if str(e.get("action", "")).lower() == "delete"]
            create_events = [e for e in events if str(e.get("action", "")).lower() == "create"]

            if existing is None:
                plan.append({"table": table, "source": source, "kind": "insert",
                             "pk": pk, "id": source_id, "fields": list(source),
                             "column_types": info["columns"]})
                continue

            differing = [
                field for field, value in source.items()
                if norm(existing.get(field)) != norm(value)
            ]
            if not differing:
                unchanged += 1
                continue

            if create_events:
                conflicts.append({"table": table, "id": source_id,
                                  "reason": "Evento create conflita com PK já ocupada no destino"})
                continue

            # Só atualiza campos comprovadamente alterados no audit log e apenas
            # se o estado local ainda corresponde ao before-image nos demais.
            matching_updates = []
            for event in update_events + delete_events:
                before = event.get("before")
                if not isinstance(before, dict):
                    continue
                changed = changed_audit_fields(event, columns)
                if changed is None:
                    continue
                before_diffs = [
                    field for field, value in before.items()
                    if field in columns and field in existing
                    and norm(existing[field]) != norm(value)
                ]
                if not set(before_diffs) - changed:
                    matching_updates.append((event, changed))

            if not matching_updates:
                conflicts.append({"table": table, "id": source_id,
                                  "reason": "ID existe com dados divergentes e sem before-image compatível",
                                  "differing_fields": ";".join(differing)})
                continue

            # Consolida updates do mesmo registro e usa o estado final do dump.
            fields = set().union(*(changed for _, changed in matching_updates))
            fields &= set(source)
            values = {field: source[field] for field in fields}
            if not values:
                conflicts.append({"table": table, "id": source_id,
                                  "reason": "Audit não contém campos aplicáveis"})
                continue
            plan.append({"table": table, "source": values, "kind": "update",
                         "pk": pk, "id": source_id, "fields": sorted(values),
                         "column_types": info["columns"]})

    # Qualquer hard delete sem tombstone no dump exige regra explícita; não
    # convertemos audit logs em DELETEs implicitamente.
    for (table, key), events in audit_map.items():
        if table not in metadata:
            continue
        if not any(str(e.get("action", "")).lower() == "delete" for e in events):
            continue
        if key in source_ids[table]:
            continue
        event = next(e for e in events if str(e.get("action", "")).lower() == "delete")
        if fetch_existing(conn, table, metadata[table]["pk"], event.get("model_id")):
            conflicts.append({"table": table, "id": event.get("model_id"),
                              "reason": "Delete físico no audit sem tombstone na exportação"})

    # Valida FKs dos inserts contra destino e registros incluídos no próprio delta.
    inserts = {(item["table"], str(item["id"])) for item in plan if item["kind"] == "insert"}
    for item in plan:
        if item["kind"] != "insert":
            continue
        source = item["source"]
        table_fks = conn.execute(text("""
            SELECT column_name, referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema=:schema AND table_name=:table
              AND referenced_table_name IS NOT NULL
        """), {"schema": schema, "table": item["table"]}).all()
        for fk_column, parent_table, parent_column in table_fks:
            value = source.get(fk_column)
            if value is None:
                continue
            parent_exists = conn.execute(text(
                f"SELECT 1 FROM {qi(parent_table)} WHERE {qi(parent_column)}=:v LIMIT 1"
            ), {"v": value}).first()
            parent_in_delta = (
                parent_column == metadata.get(parent_table, {}).get("pk")
                and (parent_table, str(value)) in inserts
            )
            if not parent_exists and not parent_in_delta:
                conflicts.append({"table": item["table"], "id": item["id"],
                                  "reason": f"FK {fk_column} aponta para {parent_table}.{parent_column}={value} ausente"})

    # Detecta colisões em índices UNIQUE antes de tentar executar INSERT.
    for table in sorted({item["table"] for item in plan if item["kind"] == "insert"}):
        unique_indexes = defaultdict(list)
        for row in conn.execute(text("""
            SELECT index_name, column_name, seq_in_index
            FROM information_schema.statistics
            WHERE table_schema=:schema AND table_name=:table
              AND non_unique=0 AND index_name <> 'PRIMARY'
            ORDER BY index_name, seq_in_index
        """), {"schema": schema, "table": table}):
            unique_indexes[row[0]].append(row[1])
        seen_unique = set()
        for item in (p for p in plan if p["table"] == table and p["kind"] == "insert"):
            source = item["source"]
            for index_name, fields in unique_indexes.items():
                values = tuple(source.get(field) for field in fields)
                # MySQL permits multiple NULL values in a UNIQUE index.
                if any(value is None for value in values):
                    continue
                unique_key = (index_name, tuple(norm(value) for value in values))
                if unique_key in seen_unique:
                    conflicts.append({"table": table, "id": item["id"],
                                      "reason": f"Índice UNIQUE {index_name} repetido no delta"})
                    continue
                seen_unique.add(unique_key)
                predicates = " AND ".join(f"{qi(field)}=:u{i}" for i, field in enumerate(fields))
                params = {f"u{i}": value for i, value in enumerate(values)}
                found = conn.execute(text(
                    f"SELECT 1 FROM {qi(table)} WHERE {predicates} LIMIT 1"
                ), params).first()
                if found:
                    conflicts.append({"table": table, "id": item["id"],
                                      "reason": f"Índice UNIQUE {index_name} já ocupado no destino"})

    return plan, conflicts, unchanged, audit_actions, len(unmapped_audits)


def dependency_order(conn, schema: str, tables: set[str], plan=None):
    edges = defaultdict(set)
    indegree = {table: 0 for table in tables}
    inserted = defaultdict(set)
    if plan is not None:
        for item in plan:
            if item["kind"] == "insert":
                inserted[item["table"]].add(str(item["id"]))
    for child in tables:
        foreign_keys = conn.execute(text("""
            SELECT column_name, referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema=:schema AND table_name=:child
              AND referenced_table_name IS NOT NULL
        """), {"schema": schema, "child": child}).all()
        child_rows = [item["source"] for item in (plan or [])
                      if item["table"] == child and item["kind"] == "insert"]
        for fk_column, parent, referenced_column in foreign_keys:
            if parent not in tables:
                continue
            # Autorrelacionamentos (p.ex. contract_items.root_item_id) são
            # ordenados por registro, não no grafo entre tabelas.
            if parent == child:
                continue
            # Uma dependência de tabela só exige ordenação se algum registro
            # novo referencia um registro pai que também será inserido.
            if plan is not None and not any(
                row.get(fk_column) is not None
                and str(row.get(fk_column)) in inserted[parent]
                for row in child_rows
            ):
                continue
            if child not in edges[parent]:
                edges[parent].add(child)
                indegree[child] += 1
    queue = deque(sorted(table for table, degree in indegree.items() if degree == 0))
    ordered = []
    while queue:
        parent = queue.popleft()
        ordered.append(parent)
        for child in sorted(edges[parent]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(tables):
        raise ValueError("Há ciclo de chaves estrangeiras entre tabelas do delta; aplicação abortada.")
    return ordered


def apply_plan(conn, schema: str, plan):
    tables = {item["table"] for item in plan if item["kind"] == "insert"}
    ordered_tables = dependency_order(conn, schema, tables, plan) if tables else []
    rank = {table: index for index, table in enumerate(ordered_tables)}
    plan = sorted(plan, key=lambda item: (0 if item["kind"] == "insert" else 1,
                                          rank.get(item["table"], 999), item["table"], str(item["id"])))
    # Ordena registros dentro de tabelas com FK para si próprias.
    for table in tables:
        self_fks = conn.execute(text("""
            SELECT column_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema=:schema AND table_name=:table
              AND referenced_table_name=:table
              AND referenced_table_name IS NOT NULL
        """), {"schema": schema, "table": table}).all()
        for fk_column, referenced_column in self_fks:
            pending = [item for item in plan if item["table"] == table and item["kind"] == "insert"]
            pending_ids = {str(item["source"].get(referenced_column)) for item in pending}
            ordered_self = []
            while pending:
                ready = [item for item in pending
                         if item["source"].get(fk_column) is None
                         or str(item["source"].get(fk_column))
                         == str(item["source"].get(referenced_column))
                         or str(item["source"].get(fk_column)) not in pending_ids]
                if not ready:
                    raise ValueError(f"Ciclo de autorreferência em {table}.{fk_column}.")
                for item in ready:
                    pending.remove(item)
                    ordered_self.append(item)
                    pending_ids.discard(str(item["source"].get(referenced_column)))
            self_ids = {id(item) for item in ordered_self}
            positions = [i for i, item in enumerate(plan)
                         if item["table"] == table and item["kind"] == "insert"]
            for position, item in zip(positions, ordered_self):
                plan[position] = item
    actions = []
    for item in plan:
        table, pk, source = item["table"], item["pk"], item["source"]
        values = {
            field: db_value(value, item["column_types"].get(field, ""))
            for field, value in source.items()
        }
        if item["kind"] == "insert":
            columns = list(values)
            binds = {f"v{i}": values[column] for i, column in enumerate(columns)}
            column_sql = ", ".join(qi(column) for column in columns)
            value_sql = ", ".join(f":v{i}" for i in range(len(columns)))
            conn.execute(text(
                f"INSERT INTO {qi(table)} ({column_sql}) VALUES ({value_sql})"
            ), binds)
        else:
            fields = list(values)
            binds = {f"v{i}": values[field] for i, field in enumerate(fields)}
            binds["pk"] = item["id"]
            set_sql = ", ".join(f"{qi(field)}=:v{i}" for i, field in enumerate(fields))
            conn.execute(text(
                f"UPDATE {qi(table)} SET {set_sql} WHERE {qi(pk)}=:pk"
            ), binds)
        actions.append({"tabela": table, "id": item["id"], "acao": item["kind"],
                        "campos": ";".join(item["fields"])})
    return actions


def main(argv=None, engine_new=None):
    parser = argparse.ArgumentParser(description="Sincroniza o delta JSON da produção para o banco local.")
    parser.add_argument("--arquivo", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--apply", action="store_true", help="Aplica as alterações após preflight sem conflitos.")
    args = parser.parse_args(argv)
    print("\n" + "=" * 72)
    print("🔄 SINCRONIZAÇÃO DE ATUALIZAÇÕES DA PRODUÇÃO")
    print("=" * 72)
    print(f"📥 Lendo delta: {args.arquivo}")
    rows_by_table, audits = load_delta(args.arquivo)
    total = sum(len(rows) for rows in rows_by_table.values()) + len(audits)
    print(f"   Registros do delta: {total:,} em {len(rows_by_table)} tabelas de dados")
    print(f"   audit_logs carregados para validação: {len(audits):,} (não serão copiados)")
    print(f"   Ações no audit: {dict(Counter(str(x.get('action')) for x in audits))}")

    if engine_new is None:
        engine_new, _ = obter_engines()

    if args.apply:
        with engine_new.begin() as conn:
            schema = conn.execute(text("SELECT DATABASE()")).scalar_one()
            plan, conflicts, unchanged, audit_actions, unmapped = make_plan(
                conn, schema, rows_by_table, audits
            )
            skippable = [
                conflict for conflict in conflicts
                if conflict["reason"].startswith(
                    "ID existe com dados divergentes e sem before-image compatível"
                )
            ]
            blocking = [conflict for conflict in conflicts if conflict not in skippable]
            if blocking:
                print(f"❌ Preflight encontrou {len(blocking)} bloqueio(s); transação abortada.")
                for conflict in blocking[:30]:
                    print(f"   - {conflict['table']} id={conflict.get('id')}: {conflict['reason']}")
                raise RuntimeError("Nada foi gravado: resolva os conflitos e rode novamente.")
            if skippable:
                print(f"⚠️ {len(skippable)} registro(s) divergente(s) serão ignorados; restante seguirá.")
                for conflict in skippable:
                    print(f"   - Ignorado {conflict['table']} id={conflict.get('id')}: dados locais divergentes")
            print(f"🏗️ Aplicando {len(plan)} operação(ões) no banco {schema}...")
            actions = apply_plan(conn, schema, plan)
            actions.extend({
                "tabela": conflict["table"],
                "id": conflict.get("id"),
                "acao": "ignorado_divergente",
                "campos": conflict.get("differing_fields", ""),
            } for conflict in skippable)
            # Os logs são salvos somente depois do commit, fora deste bloco.
            result = (schema, actions, unchanged, audit_actions, unmapped, len(skippable))
        schema, actions, unchanged, audit_actions, unmapped, skipped = result
        digest = hashlib.sha256(args.arquivo.read_bytes()).hexdigest()[:12]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"delta_aplicado_{digest}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        pd.DataFrame(actions, columns=["tabela", "id", "acao", "campos"]).to_csv(
            log_path, index=False, encoding="utf-8-sig"
        )
        print("✅ Transação confirmada.")
        applied_count = sum(action["acao"] in {"insert", "update"} for action in actions)
        print(f"   Inseridos/atualizados: {applied_count:,} | Sem alteração: {unchanged:,} | Ignorados por divergência: {skipped:,}")
        print(f"📄 Log da aplicação: {log_path}")
        print("ℹ️ audit_logs da produção foram usados para validar eventos, mas não copiados.")
        return

    with engine_new.connect() as conn:
        schema = conn.execute(text("SELECT DATABASE()")).scalar_one()
        plan, conflicts, unchanged, audit_actions, unmapped = make_plan(
            conn, schema, rows_by_table, audits
        )
        if not conflicts:
            insert_tables = {item["table"] for item in plan if item["kind"] == "insert"}
            dependency_order(conn, schema, insert_tables, plan)
    by_action = Counter(item["kind"] for item in plan)
    print(f"\n📊 PREVIEW — banco destino: {schema}")
    print(f"   Inserções candidatas: {by_action['insert']:,}")
    print(f"   Atualizações validadas por before-image: {by_action['update']:,}")
    print(f"   Registros já iguais: {unchanged:,}")
    print(f"   Conflitos/bloqueios: {len(conflicts):,}")
    if conflicts:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        conflict_path = LOG_DIR.parent / f"conflitos_atualizacoes_producao_{datetime.now():%Y%m%d_%H%M%S}.csv"
        pd.DataFrame(conflicts).to_csv(
            conflict_path, index=False, encoding="utf-8-sig"
        )
        print(f"   📄 Relatório completo de conflitos: {conflict_path}")
        for conflict in conflicts[:30]:
            print(f"   - {conflict['table']} id={conflict.get('id')}: {conflict['reason']}")
    print("   Nenhuma gravação realizada. Use --apply somente após revisar o preview.")


if __name__ == "__main__":
    main()
