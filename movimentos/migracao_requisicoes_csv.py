"""Prévia e importação controlada de requisições do CSV legado.

Por padrão o script é somente leitura. A gravação requer --apply e --departamento.
"""
from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from collections import OrderedDict
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = BASE_DIR / "docs" / "requisicao.csv"
DEFAULT_OUTPUT_DIR = BASE_DIR / "ajustes"

REQUIRED_COLUMNS = {
    "Solicitado Por", "Data", "Previsão", "Envio", "Cliente", "Etiquetas",
    "Qtd Sol", "Equipamento | Insumos", "Substituição ou Novo", "PAT",
    "Data Separação", "Separado por", "Req",
}

IGNORED_REQ_VALUES = {
    "enviado", "atendido", "atendida", "cancelado", "cancelada",
    "nao autorizado", "nao autorizada",
}

ORGANIZATION_LABELS = {
    "alucom": "Alucom",
    "moreia": "Moreia",
    "ip/zaploc": "IP",
    "ip": "IP",
    "as sistemas": "AS Sistemas",
    "adi": "Alucom",
}


def normalize(value: object) -> str:
    text = " ".join(str(value or "").split()).casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def parse_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def request_status_id(rows: list[dict[str, str]]) -> int:
    """Map Req to status: Parcial=2, Separado=3, otherwise 1."""
    req_values = {normalize(row.get("Req")) for row in rows}
    if "parcial" in req_values:
        return 2
    if "separado" in req_values:
        return 3
    return 1


def classify_request_type(value: str) -> tuple[str, str]:
    """Classifica somente casos explicitamente definidos; demais ficam pendentes."""
    normalized = normalize(value)
    if normalized in {"novo", "revisado", "revisada", "compativel", "original", "reserva",
                      "substituicao", "monofaturado"}:
        return "AVULSO", ""
    if normalized and all(part.strip().isdigit() for part in value.split(",")):
        return "SUBSTITUICAO", ""
    return "", "Tipo sem regra confirmada: revisar Substituição ou Novo"


def unique_values(rows: list[dict[str, str]], column: str) -> list[str]:
    values = OrderedDict()
    for row in rows:
        value = " ".join((row.get(column) or "").split())
        if value:
            values.setdefault(normalize(value), value)
    return list(values.values())


def compact_row_numbers(rows: list[dict[str, str]]) -> str:
    numbers = sorted({int(row["_csv_row"]) for row in rows})
    if not numbers:
        return ""
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ";".join(ranges)


def same_request_run(previous_key, key) -> bool:
    if previous_key is None or key is None:
        return False
    if key[0] != previous_key[0] or key[2] != previous_key[2]:
        return False
    if key[1] == previous_key[1]:
        return True
    # Allow a small typo within the same contiguous request, but surface the
    # variants in the report so the grouping can be checked by the operator.
    return SequenceMatcher(None, key[1], previous_key[1]).ratio() >= 0.94


def aggregate_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: OrderedDict[tuple[str, str], list[dict[str, str]]] = OrderedDict()
    for row in rows:
        item_key = (
            normalize(row.get("Equipamento | Insumos")),
            normalize(row.get("Substituição ou Novo")),
        )
        groups.setdefault(item_key, []).append(row)

    result = []
    for (item_name, replacement), item_rows in groups.items():
        quantities = []
        for row in item_rows:
            raw = (row.get("Qtd Sol") or "").strip()
            if raw:
                try:
                    quantities.append(int(raw))
                except ValueError:
                    quantities.append(raw)

        distinct_quantities = list(dict.fromkeys(quantities))
        pats = unique_values(item_rows, "PAT")
        warnings = []
        if not item_name:
            warnings.append("Modelo/equipamento vazio")
        if len(distinct_quantities) > 1:
            warnings.append("Mais de uma Qtd Sol preenchida para o mesmo item")
        if not distinct_quantities:
            # Linhas sem Qtd Sol não geram service_order_item.
            continue
        if len(unique_values(item_rows, "Data Separação")) > 1:
            warnings.append("Mais de uma data de separação para o mesmo item")
        if len(unique_values(item_rows, "Separado por")) > 1:
            warnings.append("Mais de um responsável pela separação para o mesmo item")
        asset_values = unique_values(item_rows, "asset") or unique_values(item_rows, "Asset")
        if len(asset_values) > 1:
            warnings.append("Valores divergentes em asset para o mesmo item")

        result.append({
            "equipamento_insumo": item_rows[0].get("Equipamento | Insumos", "").strip(),
            "product_csv": item_rows[0].get("Product", item_rows[0].get("product", "")).strip(),
            "substituicao_ou_novo": item_rows[0].get("Substituição ou Novo", "").strip(),
            "tipo_requisicao": classify_request_type(item_rows[0].get("Substituição ou Novo", ""))[0],
            "alerta_tipo": classify_request_type(item_rows[0].get("Substituição ou Novo", ""))[1],
            "quantidade_solicitada": distinct_quantities[0] if distinct_quantities else "",
            "quantidades_encontradas": "; ".join(map(str, distinct_quantities)),
            "linhas_csv": compact_row_numbers(item_rows),
            "quantidade_linhas_csv": len(item_rows),
            "pats": "; ".join(pats),
            "data_separacao": "; ".join(unique_values(item_rows, "Data Separação")),
            "separado_por": "; ".join(unique_values(item_rows, "Separado por")),
            "asset": "; ".join(unique_values(item_rows, "asset") or unique_values(item_rows, "Asset")),
            "estados_req": "; ".join(unique_values(item_rows, "Req")),
            "alertas_item": " | ".join(warnings),
        })
    return result


def build_preview(input_path: Path):
    with input_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headers = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(f"Colunas obrigatórias ausentes no CSV: {sorted(missing)}")
        source_rows = []
        for csv_row, row in enumerate(reader, start=2):
            row["_csv_row"] = str(csv_row)
            source_rows.append(row)

    # Segmentamos por blocos consecutivos no CSV. Isso mantém juntas as linhas
    # de uma requisição, mas não funde duas requisições distantes que por acaso
    # tenham a mesma Data + Cliente + Solicitado Por.
    request_groups: list[tuple[tuple[str, str, str], list[dict[str, str]]]] = []
    ignored_rows = []
    previous_key = None
    for row in source_rows:
        order_date = parse_date(row.get("Data", ""))
        client_key = normalize(row.get("Cliente"))
        requester_key = normalize(row.get("Solicitado Por"))
        if not order_date or not client_key or not requester_key:
            key = (f"__linha_{row['_csv_row']}", "", "")
        else:
            key = (order_date, client_key, requester_key)
        if not request_groups or not same_request_run(previous_key, key):
            request_groups.append((key, []))
        request_groups[-1][1].append(row)
        previous_key = key

    active_request_groups = []
    for key, rows in request_groups:
        active_rows = []
        for row in rows:
            status = normalize(row.get("Req"))
            if status in IGNORED_REQ_VALUES:
                ignored_rows.append({
                    "linha_csv": row["_csv_row"],
                    "req": (row.get("Req") or "").strip(),
                    "cliente": (row.get("Cliente") or "").strip(),
                    "solicitado_por": (row.get("Solicitado Por") or "").strip(),
                    "data": (row.get("Data") or "").strip(),
                    "equipamento_insumo": (row.get("Equipamento | Insumos") or "").strip(),
                    "motivo": "Estado Req configurado para ignorar",
                })
            else:
                active_rows.append(row)
        if active_rows:
            active_request_groups.append((key, active_rows))

    preview_rows = []
    for request_number, (key, rows) in enumerate(active_request_groups, start=1):
        header_warnings = []
        if key[0].startswith("__linha_"):
            header_warnings.append("Data, Cliente ou Solicitado Por ausente/inválido; linha isolada")
        for col in ("Previsão", "Envio", "Etiquetas"):
            if len(unique_values(rows, col)) > 1:
                header_warnings.append(f"Valores divergentes em {col}")
        if len(unique_values(rows, "Cliente")) > 1:
            header_warnings.append("Variação no nome do cliente agrupada por similaridade; revisar")
        for id_column in ("id_cliente", "Id_cliente", "ID_cliente", "Address_id", "address_id"):
            if len(unique_values(rows, id_column)) > 1:
                header_warnings.append(f"Valores divergentes em {id_column}")

        organization_values = unique_values(rows, "Etiquetas")
        organization_names = list(dict.fromkeys(
            ORGANIZATION_LABELS.get(normalize(value), "") for value in organization_values
        ))
        if len(organization_values) != 1 or not organization_names or not organization_names[0]:
            header_warnings.append("Organização ausente, divergente ou sem mapeamento explícito")

        items = aggregate_rows(rows)
        req_id = f"REQ-{request_number:04d}"
        for item in items:
            preview_rows.append({
                "req_candidata": req_id,
                "data": parse_date(rows[0].get("Data", "")) or rows[0].get("Data", "").strip(),
                "cliente_csv": rows[0].get("Cliente", "").strip(),
                "clientes_variantes": " | ".join(unique_values(rows, "Cliente")),
                "solicitado_por_csv": rows[0].get("Solicitado Por", "").strip(),
                "previsao": "; ".join(unique_values(rows, "Previsão")),
                "envio": "; ".join(unique_values(rows, "Envio")),
                "etiquetas": "; ".join(unique_values(rows, "Etiquetas")),
                "organizacao_sugerida": organization_names[0] if len(organization_names) == 1 else "",
                "linhas_requisicao": compact_row_numbers(rows),
                "quantidade_linhas_requisicao": len(rows),
                "quantidade_itens_logicos": len(items),
                **item,
                "alertas_requisicao": " | ".join(header_warnings),
            })

    return source_rows, preview_rows, ignored_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@lru_cache(maxsize=20000)
def _tokens(value: str) -> frozenset[str]:
    normalized = re.sub(r"\bjr\b", "junior", normalize(value))
    return frozenset(re.findall(r"[a-z0-9]+", normalized))


def _best_match(value: str, candidates: list[dict], fields: tuple[str, ...], threshold=0.72):
    """Compara tokens e só retorna candidato único com margem suficiente."""
    needle = normalize(value)
    if not needle:
        return None, "valor vazio"

    needle_tokens = _tokens(needle)
    ranked = []
    for candidate in candidates:
        labels = {normalize(candidate.get(field, "")) for field in fields}
        labels.discard("")
        scores = []
        for label in labels:
            label_tokens = _tokens(label)
            overlap = len(needle_tokens & label_tokens)
            precision = overlap / len(label_tokens) if label_tokens else 0
            recall = overlap / len(needle_tokens) if needle_tokens else 0
            token_f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0
            scores.append(token_f1)
            if needle == label:
                scores[-1] = 1.0
            elif needle_tokens and needle_tokens <= label_tokens:
                scores[-1] = max(scores[-1], 0.90)
        score = max(scores, default=0)
        ranked.append((score, candidate))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    if not ranked or ranked[0][0] < threshold:
        return None, "nenhuma correspondência suficientemente similar"
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.06:
        return None, "correspondência ambígua"
    return ranked[0][1], ""


def _state_code(value: str) -> str:
    states = {
        "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
        "bahia": "BA", "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES",
        "goias": "GO", "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS",
        "minas gerais": "MG", "para": "PA", "paraiba": "PB", "parana": "PR",
        "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
        "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO",
        "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP",
        "sergipe": "SE", "tocantins": "TO",
    }
    normalized = normalize(value)
    return states.get(normalized, normalized.upper())


def _resolve_group(group_rows, item_rows, lookups):
    """Resolve referências de uma requisição sem efetuar gravações."""
    warnings = []
    notices = []
    first = group_rows[0]
    for item in item_rows:
        header_alerts = [alert.strip() for alert in item["alertas_requisicao"].split("|")
                         if alert.strip() and alert.strip() != "Valores divergentes em Previsão"]
        if header_alerts:
            warnings.append(f"Requisição {item['req_candidata']}: {' | '.join(header_alerts)}")
            break
    client_id_raw = first.get("id_cliente") or first.get("Id_cliente") or first.get("ID_cliente")
    client = None
    if client_id_raw and str(client_id_raw).strip().isdigit():
        client = next((row for row in lookups["customers"] if row["id"] == int(client_id_raw)), None)
        if not client:
            warnings.append(f"id_cliente inválido: {client_id_raw}")
    else:
        client, reason = _best_match(first.get("Cliente", ""), lookups["customers"], ("name", "alias"))
        if not client:
            warnings.append(f"Cliente: {reason} ({first.get('Cliente', '')})")

    requester_id, reason = _mapped_user_id(first.get("Solicitado Por", ""), lookups, "requisição")
    if not requester_id:
        warnings.append(reason)
    elif reason:
        notices.append(reason)

    org_ids = {"alucom": 1115, "moreia": 1122, "ip/zaploc": 1311, "ip": 1311,
               "as sistemas": 1378, "adi": 1115}
    organization_id = org_ids.get(normalize(first.get("Etiquetas", "")))
    organization = next((row for row in lookups["organizations"] if row["id"] == organization_id), None)
    if not organization:
        warnings.append(f"Organização sem mapeamento confirmado: {first.get('Etiquetas', '')}")

    address = None
    if client:
        city = normalize(first.get("Cidade", ""))
        state = _state_code(first.get("Estado", ""))
        if not city or not state:
            warnings.append("Endereço: cidade e estado são obrigatórios para resolução")
        address_id_raw = (first.get("Address_id") or first.get("address_id")
                          or first.get("ADDRESS_ID"))
        addresses = [a for a in lookups["addresses"] if a["addressable_id"] == client["id"]
                     and a["addressable_type"] == "customer"]
        if address_id_raw and str(address_id_raw).strip().isdigit():
            matches = [a for a in addresses if a["id"] == int(address_id_raw)]
        else:
            matches = [a for a in addresses if normalize(a.get("city", "")) == city
                       and _state_code(a.get("state", "")) == state]
        if len(matches) == 1:
            address = matches[0]
        else:
            warnings.append("Endereço: não encontrado de forma única para cliente + cidade/estado")

    try:
        request_date = parse_date(first.get("Data", ""))
        deadline = parse_date(first.get("Previsão", "")) or request_date
        if not request_date or not deadline:
            raise ValueError
        datetime.strptime(request_date, "%Y-%m-%d")
        datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        request_date = deadline = ""
        warnings.append("Data ou Previsão ausente/inválida")

    resolved_items = []
    item_errors = []
    for item in item_rows:
        if item["alertas_item"] or item["alerta_tipo"]:
            item_errors.append(f"Item {item['linhas_csv']}: {item['alertas_item']} {item['alerta_tipo']}")
            continue
        try:
            quantity = int(item["quantidade_solicitada"])
            if quantity < 1:
                raise ValueError
        except (TypeError, ValueError):
            item_errors.append(f"Item {item['linhas_csv']}: quantidade inválida")
            continue

        item_type = item["tipo_requisicao"]
        product = None
        product_error = ""
        replacement = (item.get("substituicao_ou_novo") or "").strip()
        if replacement.isdigit():
            product = lookups["product_by_equipment_number"].get(int(replacement))
        if product is None:
            pat_candidates = []
            for pat in (part.strip() for part in re.split(r"[;,]", item.get("pats", ""))):
                if pat.isdigit():
                    candidate = lookups["product_by_equipment_number"].get(int(pat))
                    if candidate:
                        pat_candidates.append(candidate)
            distinct_products = {candidate["id"]: candidate for candidate in pat_candidates}
            if len(distinct_products) == 1:
                product = next(iter(distinct_products.values()))
            elif len(distinct_products) > 1:
                product_error = f"PAT aponta para products diferentes: {sorted(distinct_products)}"
        if product is None and not product_error:
            product_text = item.get("product_csv", "")
            if product_text:
                if product_text.strip().isdigit():
                    product_id = int(product_text.strip())
                    product = next((row for row in lookups["products"] if row["id"] == product_id), None)
                    if product is None:
                        product_error = f"Product contém ID inexistente em products: {product_id}"
                else:
                    product, product_error = _best_match(product_text, lookups["products"], ("name",))
                if product is None and not product_error:
                    product_error = "sem correspondência em products.name"
        if product is None:
            reason = product_error or "sem número em Substituição ou Novo/PAT e sem coluna Product correspondível"
            item_errors.append(f"Item {item['linhas_csv']}: product_id não resolvido ({reason})")
            continue
        service_order_equipment_id = None
        if item_type == "SUBSTITUICAO":
            replacement_number = replacement.strip()
            if replacement_number.isdigit():
                matches = [equipment for equipment in lookups["equipments"]
                           if str(equipment.get("number")) == replacement_number]
                if matches:
                    # The request item represents the equipment being replaced;
                    # movement_items independently resolve their equipment from PAT.
                    service_order_equipment_id = matches[0]["id"]
            if service_order_equipment_id is None:
                notices.append(
                    f"Item {item['linhas_csv']}: equipment_id da service_order_item ficará NULL "
                    "(número em Substituição ou Novo sem equipment.number correspondente)"
                )
        asset = normalize(item.get("asset", "")) == "sim"
        resolved_items.append({"preview": item, "quantity": quantity, "product": product,
                               "kind": item_type, "department_id": 2 if asset else 1,
                               "service_order_equipment_id": service_order_equipment_id})

    request_types = {item["kind"] for item in resolved_items}
    if len(request_types) > 1:
        warnings.append("Tipos de requisição divergentes entre itens com product_id resolvido")

    transport_value = normalize(first.get("Envio", ""))
    transport_map = {"rota": 1, "retirada": 2, "retirado": 2, "transportadora": 3}
    transport_id = transport_map.get(transport_value, 1)

    return {
        "warnings": warnings, "notices": notices, "item_errors": item_errors,
        "client": client, "requester_id": requester_id,
        "organization": organization, "address": address, "items": resolved_items,
        "request_date": request_date, "deadline": request_date,
        "transport_id": transport_id,
    }


def _load_lookups(conn):
    from sqlalchemy import text

    queries = {
        "customers": "SELECT id, name, alias FROM customers WHERE deleted_at IS NULL",
        "users": "SELECT id, name FROM users",
        "organizations": "SELECT id, name, alias FROM organizations WHERE deleted_at IS NULL",
        "addresses": "SELECT id, addressable_type, addressable_id, city, state FROM addresses WHERE deleted_at IS NULL",
        "equipments": "SELECT id, number, serial_number FROM equipments WHERE number IS NOT NULL ORDER BY id",
        "products": "SELECT id, name FROM products WHERE deleted_at IS NULL",
        "product_by_equipment_number": "SELECT eqp.number, prod.id, prod.name, eqp.id AS equipment_id "
            "FROM products prod INNER JOIN product_items prodi ON prod.id=prodi.product_id "
            "INNER JOIN equipments eqp ON prodi.id=eqp.product_item_id "
            "WHERE eqp.number IS NOT NULL ORDER BY eqp.id",
    }
    result = {key: [dict(row._mapping) for row in conn.execute(text(sql))]
              for key, sql in queries.items() if key != "product_by_equipment_number"}
    result["product_by_equipment_number"] = {}
    for row in conn.execute(text(queries["product_by_equipment_number"])):
        values = dict(row._mapping)
        result["product_by_equipment_number"].setdefault(int(values["number"]), values)
    return result


REQUESTER_IDS = {
    "victor": 9573, "beatriz": 9582, "santiago": 9576, "manuel": 1,
    "atila": 9412, "davi": 9591, "imna": 9470,
}
SEPARATOR_IDS = {"logistica": 9362}


def _mapped_user_id(value: str, lookups, purpose: str):
    key = normalize(value).split(" ")[0] if normalize(value) else ""
    if purpose == "requisição":
        user_id = REQUESTER_IDS.get(key)
        if user_id and any(user["id"] == user_id for user in lookups["users"]):
            return user_id, ""
        if any(user["id"] == 1 for user in lookups["users"]):
            return 1, f"Fallback Admin (id 1) para solicitante {value!r}"
        return None, f"Fallback Admin (id 1) indisponível para solicitante {value!r}"
    separator_id = SEPARATOR_IDS.get(normalize(value))
    if separator_id and any(user["id"] == separator_id for user in lookups["users"]):
        return separator_id, ""
    user, reason = _best_match(value, lookups["users"], ("name",))
    if user:
        return user["id"], ""
    if any(row["id"] == 1 for row in lookups["users"]):
        return 1, f"Fallback Admin (id 1) para {purpose} {value!r}: {reason}"
    return None, f"Fallback Admin (id 1) indisponível para {purpose} {value!r}: {reason}"


def _insert_row(conn, table: str, values: dict):
    from sqlalchemy import text

    columns = ", ".join(f"`{column}`" for column in values)
    binds = ", ".join(f":{column}" for column in values)
    result = conn.execute(text(f"INSERT INTO `{table}` ({columns}) VALUES ({binds})"), values)
    return result.lastrowid


def _group_import_rows(source_rows, preview_rows):
    grouped_preview: OrderedDict[str, list[dict]] = OrderedDict()
    for item in preview_rows:
        grouped_preview.setdefault(str(item["req_candidata"]), []).append(item)
    raw_groups: dict[str, list[dict]] = {}
    for candidate, rows in grouped_preview.items():
        line_numbers = {int(n) for item in rows for span in str(item["linhas_requisicao"]).split(";")
                        for n in (span.split("-") if "-" in span else [span])}
        raw_groups[candidate] = [row for row in source_rows if int(row["_csv_row"]) in line_numbers
                                 and normalize(row.get("Req")) not in IGNORED_REQ_VALUES]
    return grouped_preview, raw_groups


def validate_import(engine, source_rows, preview_rows) -> dict[str, int]:
    """Executa os mesmos matches da aplicação, em conexão somente leitura."""
    from sqlalchemy import text

    grouped_preview, raw_groups = _group_import_rows(source_rows, preview_rows)
    totals = {"orders_ready": 0, "orders_blocked": 0, "items_ready": 0,
              "items_skipped": 0, "movements_ready": 0, "movement_items_ready": 0,
              "movement_items_blocked": 0, "duplicates": 0,
              "service_orders_status_1": 0, "service_orders_status_2": 0,
              "service_orders_status_3": 0}
    with engine.connect() as conn:
        lookups = _load_lookups(conn)
        for candidate, items in grouped_preview.items():
            resolved = _resolve_group(raw_groups[candidate], items, lookups)
            for notice in resolved["notices"]:
                print(f"AVISO {candidate}: {notice}")
            for error in resolved["item_errors"]:
                print(f"ITEM IGNORADO {candidate}: {error}")
            totals["items_ready"] += len(resolved["items"])
            totals["items_skipped"] += len(resolved["item_errors"])
            if resolved["warnings"] or not resolved["items"]:
                print(f"BLOQUEADA {candidate}: " + " | ".join(resolved["warnings"] or ["nenhum item resolvido"]))
                totals["orders_blocked"] += 1
                continue
            details = f"Migração de requisição 2026 ({candidate}; linhas {compact_row_numbers(raw_groups[candidate])})"
            if conn.execute(text("SELECT id FROM service_orders WHERE details=:details AND deleted_at IS NULL LIMIT 1"),
                            {"details": details}).first():
                print(f"BLOQUEADA {candidate}: service_order desta origem já foi criada")
                totals["orders_blocked"] += 1
                totals["duplicates"] += 1
                continue
            types = {item["kind"] for item in resolved["items"]}
            if len(types) != 1:
                print(f"BLOQUEADA {candidate}: itens AVULSO e SUBSTITUIÇÃO misturados; revisar regra da capa")
                totals["orders_blocked"] += 1
                continue
            totals["orders_ready"] += 1
            status_id = request_status_id(raw_groups[candidate])
            totals[f"service_orders_status_{status_id}"] += 1
            movement_groups = set()
            for item in resolved["items"]:
                preview = item["preview"]
                raw_separation_date = preview.get("data_separacao", "")
                separation_date = parse_date(raw_separation_date)
                if raw_separation_date and not separation_date:
                    print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: Data Separação inválida")
                    totals["movement_items_blocked"] += 1
                    continue
                if not separation_date:
                    continue
                created_by, user_error = _mapped_user_id(preview.get("separado_por", ""), lookups, "movimento")
                if created_by and user_error:
                    print(f"AVISO {candidate} item {preview['linhas_csv']}: {user_error}")
                pats = [part.strip() for part in re.split(r"[;,]", preview.get("pats", "")) if part.strip()]
                matched = []
                if not created_by or not pats:
                    print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: "
                          f"{user_error or 'PAT vazio'}")
                    totals["movement_items_blocked"] += 1
                    continue
                for pat in pats:
                    equipment = next((row for row in lookups["equipments"] if str(row.get("number")) == pat), None)
                    if equipment:
                        matched.append(equipment)
                    else:
                        print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: "
                              f"PAT {pat} sem equipment.number correspondente")
                        matched = []
                        break
                if matched:
                    movement_groups.add((separation_date, created_by))
                    totals["movement_items_ready"] += len(matched)
            totals["movements_ready"] += len(movement_groups)
    print("VALIDAÇÃO (sem gravação): " + "; ".join(f"{key}={value}" for key, value in totals.items()))
    return totals


def apply_import(engine, source_rows, preview_rows) -> tuple[int, int, int]:
    """Cria pedidos e, quando houver separação, movimentos; grupo atômico."""
    from sqlalchemy import text

    grouped_preview, raw_groups = _group_import_rows(source_rows, preview_rows)

    created_orders = 0
    created_movements = 0
    skipped_items = 0
    with engine.begin() as conn:
        lookups = _load_lookups(conn)

        for candidate, items in grouped_preview.items():
            raw = raw_groups[candidate]
            resolved = _resolve_group(raw, items, lookups)
            for notice in resolved["notices"]:
                print(f"AVISO {candidate}: {notice}")
            for item_error in resolved["item_errors"]:
                print(f"ITEM IGNORADO {candidate}: {item_error}")
            skipped_items += len(resolved["item_errors"])
            if resolved["warnings"]:
                print(f"BLOQUEADA {candidate}: " + " | ".join(resolved["warnings"]))
                continue
            if not resolved["items"]:
                print(f"BLOQUEADA {candidate}: nenhum item com product_id resolvido")
                continue
            movement_types = {7 if item["kind"] == "AVULSO" else 2 for item in resolved["items"]}
            if len(movement_types) != 1:
                print(f"BLOQUEADA {candidate}: itens AVULSO e SUBSTITUIÇÃO misturados; revisar regra da capa")
                continue
            created_at = f"{resolved['request_date']} 00:00:00"
            movement_type_id = movement_types.pop()
            order_details = f"Migração de requisição 2026 ({candidate}; linhas {compact_row_numbers(raw)})"
            if conn.execute(text(
                "SELECT id FROM service_orders WHERE details=:details AND deleted_at IS NULL LIMIT 1"),
                {"details": order_details}).first():
                print(f"BLOQUEADA {candidate}: service_order desta origem já foi criado")
                continue
            service_order_id = _insert_row(conn, "service_orders", {
                "status_id": request_status_id(raw), "movement_type_id": movement_type_id,
                "contract_id": None,
                "user_id": resolved["requester_id"],
                "mode_transport_id": resolved["transport_id"], "requires_invoice": False,
                "organization_id": resolved["organization"]["id"],
                "recipient_customer_id": resolved["client"]["id"], "deadline": resolved["deadline"],
                "destination_order_id": None, "maintenance_created_order": False,
                "situation": "approved", "changed_by": resolved["requester_id"],
                "details": order_details,
                "created_at": created_at, "updated_at": created_at,
            })
            created_orders += 1
            service_item_ids = {}
            for item in resolved["items"]:
                preview = item["preview"]
                service_item_id = _insert_row(conn, "service_order_items", {
                    "status_id": request_status_id(raw), "service_order_id": service_order_id,
                    "department_id": item["department_id"],
                    "movement_type_id": 7 if item["kind"] == "AVULSO" else 2,
                    "contract_item_id": None, "alias": item["product"]["name"],
                    "equipment_id": item["service_order_equipment_id"],
                    "type_id": None, "product_id": None,
                    "is_exchange": False, "is_extra": False,
                    "quantity_product": None, "fulfilled_quantity_product": 0,
                    "quantity": item["quantity"], "fulfilled_quantity": 0,
                    "details": "Migração de requisição 2026",
                    "address_id": resolved["address"]["id"], "location_id": None,
                    "created_at": created_at, "updated_at": created_at, "deleted_at": None,
                    "exchange_approval_status": None, "exchange_approved_by": None,
                    "exchange_approved_at": None, "stock_status": "in_stock",
                })
                service_item_ids[preview["linhas_csv"]] = service_item_id

            # Cada data de separação gera uma capa de movimento; sem data não
            # há movimento. O sequenciador é travado na transação para evitar
            # colisão com outra gravação do sistema.
            movements_before = created_movements
            movement_groups = OrderedDict()
            for item in resolved["items"]:
                preview = item["preview"]
                raw_sep_date = preview.get("data_separacao", "").strip()
                if not raw_sep_date:
                    continue
                sep_date = parse_date(raw_sep_date)
                if not sep_date:
                    print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: Data Separação inválida")
                    continue
                created_by, user_error = _mapped_user_id(preview.get("separado_por", ""), lookups, "movimento")
                if created_by and user_error:
                    print(f"AVISO {candidate} item {preview['linhas_csv']}: {user_error}")
                if not created_by:
                    print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: {user_error}")
                    continue
                movement_groups.setdefault((sep_date, created_by), []).append(item)

            for (sep_date, created_by), separated_items in movement_groups.items():
                ready_items = []
                for item in separated_items:
                    preview = item["preview"]
                    pat_values = [part.strip() for part in re.split(r"[;,]", preview.get("pats", ""))
                                  if part.strip()]
                    equipment_matches = []
                    if not pat_values:
                        print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: PAT vazio")
                        continue
                    for pat in pat_values:
                        matches = [equipment for equipment in lookups["equipments"]
                                   if str(equipment.get("number")) == pat]
                        if not matches:
                            print(f"MOVIMENTO BLOQUEADO {candidate} item {preview['linhas_csv']}: "
                                  f"PAT {pat} sem equipment.number correspondente")
                            equipment_matches = []
                            break
                        # Se houver equipamentos com o mesmo number, usa o primeiro
                        # da lista ordenada por id, conforme regra informada.
                        equipment_matches.append(matches[0])
                    if equipment_matches:
                        ready_items.append((item, equipment_matches))
                if not ready_items:
                    continue
                separator = conn.execute(text(
                    "SELECT last_number FROM movement_number_sequences WHERE id=1 FOR UPDATE")).first()
                if not separator:
                    raise ValueError("Sequência movement_number_sequences id=1 não encontrada")
                max_number = conn.execute(text(
                    "SELECT COALESCE(MAX(CAST(number AS UNSIGNED)), 0) FROM movements "
                    "WHERE number REGEXP '^[0-9]+$'")).scalar() or 0
                next_number = max(int(separator[0]), int(max_number)) + 1
                conn.execute(text("UPDATE movement_number_sequences SET last_number=:number WHERE id=1"),
                             {"number": next_number})
                movement_date = f"{sep_date} 00:00:00"
                movement_id = _insert_row(conn, "movements", {
                    "number": str(next_number), "movement_date": movement_date,
                    "service_order_id": service_order_id,
                    "recipient_customer_id": resolved["client"]["id"], "migrate_customer_id": None,
                    "organization_id": resolved["organization"]["id"], "status_id": 1,
                    "created_by": created_by, "details": "Migração requisição 2026",
                    "maintenance_created_movement": False, "created_at": movement_date,
                    "updated_at": movement_date, "deleted_at": None,
                })
                for item, equipment_matches in ready_items:
                    preview = item["preview"]
                    for equipment in equipment_matches:
                        _insert_row(conn, "movement_items", {
                            "movement_id": movement_id,
                            "movement_type_id": 7 if item["kind"] == "AVULSO" else 2,
                            "service_order_item_id": service_item_ids[preview["linhas_csv"]],
                            "equipment_id": equipment["id"], "extra_id": None, "status_id": 1,
                            "product_item_id": None, "alias": item["product"]["name"],
                            "old_organization_id": None,
                            "new_organization_id": resolved["organization"]["id"],
                            "operation_type": "SUBSTITUIÇÃO" if item["kind"] == "SUBSTITUICAO" else "AVULSO",
                            "confirmed_at": None, "confirmed_by": None,
                            "created_at": movement_date, "updated_at": movement_date, "deleted_at": None,
                        })
                created_movements += 1
            print(f"CRIADA {candidate}: service_order={service_order_id}, "
                  f"itens={len(resolved['items'])}; movimentos gerados={created_movements - movements_before}")
    return created_orders, created_movements, skipped_items


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Gera prévia revisável; gravação somente com --apply explícito."
    )
    parser.add_argument("--arquivo", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--saida-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validar-banco", action="store_true",
                      help="Testa os vínculos no banco novo, sem gravar")
    mode.add_argument("--apply", action="store_true", help="Grava grupos resolvidos no banco novo")
    args = parser.parse_args(argv)

    source_rows, preview_rows, ignored_rows = build_preview(args.arquivo)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    preview_path = args.saida_dir / f"preview_requisicoes_{timestamp}.csv"
    ignored_path = args.saida_dir / f"requisicoes_ignoradas_{timestamp}.csv"
    write_csv(preview_path, preview_rows)
    write_csv(ignored_path, ignored_rows)

    candidate_orders = len({row["req_candidata"] for row in preview_rows})
    candidate_items = len(preview_rows)
    warnings = sum(bool(row["alertas_item"] or row["alertas_requisicao"] or row["alerta_tipo"]) for row in preview_rows)
    empty_quantity_rows = sum(not (row.get("Qtd Sol") or "").strip() for row in source_rows)
    print("\nPRÉVIA DA MIGRAÇÃO DE REQUISIÇÕES")
    print(f"Linhas lidas: {len(source_rows):,}")
    print(f"Linhas ignoradas pelo estado Req: {len(ignored_rows):,}")
    print(f"Linhas sem Qtd Sol ignoradas para itens: {empty_quantity_rows:,}")
    print(f"Requisições candidatas (Data + Cliente + Solicitado Por): {candidate_orders:,}")
    print(f"Itens lógicos candidatos: {candidate_items:,}")
    print(f"Linhas com alertas na prévia: {warnings:,}")
    print(f"Prévia: {preview_path}")
    print(f"Ignoradas: {ignored_path}")
    if args.validar_banco or args.apply:
        from config.database import obter_engines
        engine_new, _ = obter_engines()
    if args.validar_banco:
        validate_import(engine_new, source_rows, preview_rows)
    elif args.apply:
        orders, movements, skipped_items = apply_import(engine_new, source_rows, preview_rows)
        print(f"Service orders criadas: {orders}; movements criados: {movements}; "
              f"itens ignorados por falha de resolução: {skipped_items}")
    else:
        print("Modo somente leitura; use --validar-banco para testar os vínculos ou --apply para gravar.")


if __name__ == "__main__":
    main()
