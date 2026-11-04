"""Migra requisições de docs/REQUISIÇÃO 2026.csv.

Dry-run por padrão. Use --execute somente depois de revisar o CSV de auditoria.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text


BASE_DIR = Path(__file__).resolve().parents[1]
ARQUIVO_FONTE = BASE_DIR / "docs" / "REQUISIÇÃO 2026.csv"
ARQUIVO_AUDITORIA = BASE_DIR / "ajustes" / "auditoria_requisicoes_2026.csv"
DIR_LOGS = BASE_DIR / "ajustes" / "logs_requisicoes_2026"
MARCADOR_MIGRACAO = "Migração REQUISIÇÃO 2026.csv"

USUARIOS = {
    "VICTOR": "victoralves",
    "BEATRIZ": "BEATRIZCARDOSO",
    "DAVI": "DAVISOUZA",
    "EMANUEL": "EMANUEL",
    "IMNA": "imna",
    "SANTIAGO": "SANTIAGO",
    "ATILA": "atilacosta",
}
ORGANIZACOES = {
    "ALUCOM": 1115,
    "MOREIA": 1122,
    "IP": 1311,
    "AS SISTEMAS": 1378,
}
TRANSPORTES = {"ROTA": 1, "RETIRADA": 2, "TRANSPORTADORA": 3}
TIPOS_MOVIMENTO = {"ALUGUEL": 1, "SUBSTITUICAO": 2, "AVULSO": 7}
STATUS_REQ = {
    "PARCIAL": (2, "pending"),
    "SEPARADO": (1, "pending"),
    "SOLICITADO ENVIO": (1, "pending"),
    "ENVIADO": (3, "approved"),
    "ATENDIDA": (3, "approved"),
}
IGNORAR_REQ = {"CANCELADA", "SEM ESTOQUE", ""}


def normalizar(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip().upper()


def data_planilha(value: object) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)


def numero(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = float(raw.replace(",", "."))
        return int(parsed) if parsed > 0 and parsed.is_integer() else None
    except ValueError:
        return None


def selecionar_organizacao(etiqueta: str) -> int | None:
    tag = normalizar(etiqueta)
    # Etiquetas podem conter mais de uma marca (ex.: IP/Zaploc); prioridade
    # explícita evita escolher por substring curta antes da marca completa.
    for key in ("AS SISTEMAS", "MOREIA", "ALUCOM", "IP"):
        if key in tag:
            return ORGANIZACOES[key]
    return None


def carregar_referencias(conn):
    customers = [dict(r) for r in conn.execute(text(
        "SELECT id, name, alias FROM customers WHERE deleted_at IS NULL"
    )).mappings()]
    users = {normalizar(r.username): int(r.id) for r in conn.execute(text(
        "SELECT id, username FROM users WHERE deleted_at IS NULL"
    ))}
    orgs = {int(r.id) for r in conn.execute(text(
        "SELECT id FROM organizations WHERE deleted_at IS NULL"
    ))}
    transports = {int(r.id) for r in conn.execute(text(
        "SELECT id FROM service_order_mode_transports WHERE deleted_at IS NULL"
    ))}
    return customers, users, orgs, transports


def match_customer(name: str, customers: list[dict]) -> tuple[dict | None, float, list[dict]]:
    target = normalizar(name)
    generic = {"CLIENTE", "PREFEITURA", "MUNICIPAL", "SECRETARIA", "ESTADO", "INSTITUTO", "DE", "DA", "DO", "DOS", "DAS"}
    target_tokens = {token for token in target.split() if len(token) > 3 and token not in generic}
    candidates_pool = []
    if target_tokens:
        candidates_pool = [customer for customer in customers
                           if any(target_tokens & {token for token in normalizar(value).split()
                                                   if len(token) > 3 and token not in generic}
                                  for value in (customer.get("name"), customer.get("alias")) if value)]
    if len(candidates_pool) < 2:
        candidates_pool = customers
    scored = []
    for customer in candidates_pool:
        names = {normalizar(customer.get("name")), normalizar(customer.get("alias"))}
        score = max((difflib.SequenceMatcher(None, target, candidate).ratio()
                     for candidate in names if candidate), default=0)
        scored.append((score, customer))
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0] if scored else (0, None)
    # Limite conservador e diferença mínima para evitar matches ambíguos.
    runner_up = scored[1][0] if len(scored) > 1 else 0
    matches = [row for score, row in scored[:5] if score >= 0.45]
    if best[0] < 0.72 or best[0] - runner_up < 0.04:
        return None, best[0], matches
    return best[1], best[0], matches


def preparar(engine, arquivo: Path = ARQUIVO_FONTE) -> tuple[list[dict], list[dict]]:
    df = pd.read_csv(arquivo, encoding="utf-8-sig", dtype=str).fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    required = ["Solicitado Por", "Data", "Previsão", "Envio", "Cliente", "Etiquetas",
                "Qtd Sol", "Equipamento | Insumos", "Substituição ou Novo", "PAT",
                "Qtd Entregue", "Data Separação", "Separado por", "Req"]
    missing = set(required) - set(df.columns)
    if missing:
        raise ValueError(f"Colunas ausentes no CSV: {', '.join(sorted(missing))}")

    with engine.connect() as conn:
        customers, user_ids, org_ids, transports = carregar_referencias(conn)
        equipment_numbers = [int(float(v)) for v in df["PAT"] if str(v).strip().isdigit()]
        substitution_numbers = [int(float(v)) for v in df["Substituição ou Novo"] if str(v).strip().isdigit()]
        all_numbers = sorted(set(equipment_numbers + substitution_numbers))
        equipment = {}
        for start in range(0, len(all_numbers), 500):
            block = all_numbers[start:start + 500]
            if not block:
                continue
            marks = ",".join(f":n{i}" for i in range(len(block)))
            params = {f"n{i}": number for i, number in enumerate(block)}
            rows = conn.execute(text(f"""
                SELECT id, number, status_id, name, current_organization_id
                FROM equipments WHERE number IN ({marks}) AND deleted_at IS NULL
            """), params)
            for row in rows:
                equipment.setdefault(int(row.number), []).append(dict(
                    id=int(row.id), number=int(row.number), status_id=int(row.status_id),
                    name=row.name, organization_id=row.current_organization_id,
                ))

    audit, prepared = [], []
    customer_match_cache = {}
    for index, row in df.iterrows():
        line = index + 2
        req = normalizar(row["Req"])
        reason = ""
        if req in IGNORAR_REQ:
            reason = f"Req ignorada: {req or 'vazio'}"
        elif req not in STATUS_REQ:
            reason = f"Status Req não mapeado: {req}"

        tag_org = selecionar_organizacao(row["Etiquetas"])
        org_id = tag_org
        if not reason and not org_id:
            reason = f"Etiqueta sem organização reconhecida: {row['Etiquetas']}"
        elif org_id and org_id not in org_ids:
            reason = f"Organização {org_id} não existe no destino"

        requester = USUARIOS.get(normalizar(row["Solicitado Por"]))
        user_id = user_ids.get(normalizar(requester)) if requester else None
        if not reason and not user_id:
            reason = f"Solicitante sem usuário correspondente: {row['Solicitado Por']}"
        request_date = data_planilha(row["Data"])
        if not reason and not request_date:
            reason = "Data da requisição vazia ou inválida"

        customer_key = normalizar(row["Cliente"])
        if customer_key not in customer_match_cache:
            customer_match_cache[customer_key] = match_customer(row["Cliente"], customers)
        customer, score, candidates = customer_match_cache[customer_key]
        if not reason and not customer:
            reason = f"Cliente sem match confiável (similaridade {score:.2f})"

        mode = TRANSPORTES.get(normalizar(row["Envio"]))
        if not reason and (not mode or mode not in transports):
            reason = f"Transporte não mapeado/disponível: {row['Envio']}"

        pat = numero(row["PAT"])
        pat_matches = equipment.get(pat, []) if pat else []
        pat_equipment = next((e for e in pat_matches if e["status_id"] == 1), None)
        if not reason and not pat_equipment:
            reason = f"PAT ausente/ambíguo ou equipamento não está status_id=1: {row['PAT']}"

        subst_text = str(row["Substituição ou Novo"]).strip()
        normalized_subst = normalizar(subst_text)
        old_equipment = None
        if normalized_subst != "NOVO":
            subst_no = numero(subst_text)
            if subst_no:
                candidates_eq = equipment.get(subst_no, [])
                old_equipment = next((e for e in candidates_eq if e["status_id"] in (2, 3)), None)
                if not old_equipment and not reason:
                    reason = f"Equipamento de substituição {subst_no} não está alugado/reservado"
            elif not reason:
                reason = f"Tipo ambíguo em Substituição ou Novo: {subst_text}"

        requested = numero(row["Qtd Sol"])
        delivered = numero(row["Qtd Entregue"])
        separated_at = data_planilha(row["Data Separação"])
        # Qtd Entregue/Qtd Sol são totais do grupo, não quantidades por PAT.
        # Cada linha com PAT corresponde a uma unidade física para o movimento.
        movement_quantity = delivered if delivered else requested
        separator_name = str(row["Separado por"]).strip()
        separator_id = user_ids.get(normalizar(separator_name))
        # Usuários de separação não mapeados nunca impedem a requisição; apenas
        # bloqueiam a criação do movimento da linha, registrando a razão.
        create_movement = bool(separated_at and separator_id and not reason)

        record = {
            "source_line": line, "req": req, "status_id": STATUS_REQ.get(req, (None, None))[0],
            "situation": STATUS_REQ.get(req, (None, None))[1], "user_id": user_id,
            "requester_name": requester,
            "customer_id": int(customer["id"]) if customer else None,
            "customer_match": customer.get("name") if customer else "",
            "customer_score": round(score, 4), "organization_id": org_id,
            "mode_transport_id": mode, "created_at": request_date,
            # deadline é NOT NULL no schema destino; quando não há Previsão,
            # usa-se a Data da própria requisição como fallback.
            "deadline": data_planilha(row["Previsão"]) or request_date,
            # NOVO é aluguel avulso, sem contrato; AVULSO tem id 7 no destino.
            "movement_type_id": 7 if normalized_subst == "NOVO" else 2,
            "item_alias": str(row["Equipamento | Insumos"]).strip(),
            "requested_quantity": requested, "delivered_quantity": movement_quantity,
            "equipment_id": pat_equipment["id"] if pat_equipment else None,
            "equipment_number": pat, "old_equipment_id": old_equipment["id"] if old_equipment else None,
            "old_equipment_number": old_equipment["number"] if old_equipment else None,
            "separated_at": separated_at, "separator_id": separator_id,
            "separator_name": separator_name, "create_movement": create_movement,
            "source_req": str(row["Req"]),
        }
        if reason:
            audit.append({"linha": line, "resultado": "IGNORADA", "motivo": reason,
                          "cliente": row["Cliente"], "cliente_sugerido": " | ".join(
                              str(c.get("name", "")) for c in candidates), "similaridade": round(score, 4),
                          "PAT": row["PAT"], "Req": row["Req"]})
        else:
            prepared.append(record)
            if separated_at and not separator_id:
                audit.append({"linha": line, "resultado": "REQUISIÇÃO SEM MOVIMENTO",
                              "motivo": f"Separador sem correspondência: {separator_name}",
                              "cliente": row["Cliente"], "cliente_sugerido": record["customer_match"],
                              "similaridade": round(score, 4), "PAT": row["PAT"], "Req": row["Req"]})
    return prepared, audit


def criar(engine, registros: list[dict]) -> tuple[int, int, list[dict]]:
    # Agrupa cabeçalho por cliente/data/solicitante/transporte/organização/tipo
    # e situação; cada alias de equipamento fica em seu próprio item.
    headers = defaultdict(list)
    for r in registros:
        key = (r["customer_id"], r["user_id"], r["created_at"], r["deadline"],
               r["organization_id"], r["mode_transport_id"], r["movement_type_id"],
               r["status_id"], r["situation"])
        headers[key].append(r)
    order_count = movement_count = 0
    created_log_rows = []
    with engine.begin() as conn:
        for key, rows in headers.items():
            customer_id, user_id, created, deadline, org_id, transport, movement_type, status_id, situation = key
            cur = conn.execute(text("""
                INSERT INTO service_orders
                    (status_id, movement_type_id, contract_id, user_id, mode_transport_id,
                     requires_invoice, organization_id, recipient_customer_id,
                     maintenance_created_order, deadline, situation, details, created_at, updated_at)
                VALUES (:status_id, :movement_type_id, NULL, :user_id, :transport,
                        0, :org_id, :customer_id, 0, :deadline, :situation,
                        :details, :created, :created)
            """), {"status_id": status_id, "movement_type_id": movement_type,
                   "user_id": user_id, "transport": transport, "org_id": org_id,
                   "customer_id": customer_id, "deadline": deadline, "situation": situation,
                   "details": "Migração REQUISIÇÃO 2026.csv; requisição avulsa sem contrato",
                   "created": created})
            service_order_id = cur.lastrowid
            order_count += 1
            order_movement_logs = []

            by_item = defaultdict(list)
            for r in rows:
                # Em substituição, equipment_id da requisição é o equipamento
                # alugado/reservado indicado na coluna de referência.
                item_key = (r["item_alias"], r["old_equipment_id"])
                by_item[item_key].append(r)
            for (alias, requested_equipment_id), item_rows in by_item.items():
                entered_quantities = [r["requested_quantity"] for r in item_rows if r["requested_quantity"]]
                # A planilha informa Qtd Sol apenas na primeira linha do grupo;
                # as linhas seguintes representam equipamentos dessa quantidade.
                requested_qty = max(entered_quantities) if entered_quantities else len(item_rows)
                fulfilled_qty = sum(1 for r in item_rows if r["create_movement"])
                item_result = conn.execute(text("""
                    INSERT INTO service_order_items
                        (status_id, service_order_id, department_id, movement_type_id,
                         contract_item_id, alias, equipment_id, type_id, product_id,
                         is_exchange, is_extra, quantity_product, fulfilled_quantity_product,
                         quantity, fulfilled_quantity, details, created_at, updated_at)
                    VALUES (:status_id, :order_id, NULL, :movement_type, NULL, :alias,
                            :equipment_id, NULL, NULL, :is_exchange, 0, NULL, 0, :quantity,
                            :fulfilled, :details, :created, :created)
                """), {"status_id": status_id, "order_id": service_order_id,
                       "movement_type": movement_type, "alias": alias,
                       "equipment_id": requested_equipment_id,
                       "is_exchange": int(movement_type == 2), "quantity": requested_qty,
                       "fulfilled": fulfilled_qty,
                       "details": "AVULSO - sem vínculo contratual" if movement_type == 7 else "Substituição conforme planilha",
                       "created": created})
                service_item_id = item_result.lastrowid

                movement_rows = [r for r in item_rows if r["create_movement"]]
                by_movement = defaultdict(list)
                for r in movement_rows:
                    by_movement[(r["separated_at"], r["separator_id"])].append(r)
                for (movement_date, separator_id), separated in by_movement.items():
                    mov_result = conn.execute(text("""
                        INSERT INTO movements
                            (number, movement_date, service_order_id, recipient_customer_id,
                             organization_id, status_id, created_by, details,
                             maintenance_created_movement, created_at, updated_at)
                        VALUES (NULL, :movement_date, :order_id, :customer_id, :org_id,
                                3, :created_by, :details, 0, :movement_date, :movement_date)
                    """), {"movement_date": movement_date, "order_id": service_order_id,
                           "customer_id": customer_id, "org_id": org_id,
                           "created_by": separator_id,
                           "details": "Migração REQUISIÇÃO 2026.csv"})
                    movement_id = mov_result.lastrowid
                    movement_count += 1
                    movement_log = {
                        "movimento_id": movement_id,
                        "data_movimento": movement_date,
                        "separador_id": separator_id,
                        "separador": separated[0]["separator_name"],
                        "itens_movimento": [],
                    }
                    order_movement_logs.append(movement_log)
                    seen_equipment = set()
                    for r in separated:
                        equipment_id = r["equipment_id"]
                        if equipment_id in seen_equipment:
                            continue
                        seen_equipment.add(equipment_id)
                        operation = "AVULSO" if movement_type == 7 else "SUBSTITUIÇÃO"
                        movement_item_result = conn.execute(text("""
                            INSERT INTO movement_items
                                (movement_id, movement_type_id, service_order_item_id,
                                 equipment_id, status_id, alias, old_organization_id,
                                 new_organization_id, operation_type, created_at, updated_at)
                            VALUES (:movement_id, :movement_type, :service_item, :equipment_id,
                                    3, :alias, NULL, :org_id, :operation, :created, :created)
                        """), {"movement_id": movement_id, "movement_type": movement_type,
                               "service_item": service_item_id, "equipment_id": equipment_id,
                               "alias": r["item_alias"], "org_id": org_id,
                               "operation": operation, "created": movement_date})
                        movement_log["itens_movimento"].append({
                            "movement_item_id": movement_item_result.lastrowid,
                            "equipment_number": r["equipment_number"],
                            "operation_type": operation,
                        })

            base_log = {
                "requisicao_id": service_order_id,
                "cliente_id": customer_id,
                "cliente": rows[0]["customer_match"],
                "solicitante_id": user_id,
                "solicitante": rows[0]["requester_name"],
                "data_requisicao": created,
                "prazo": deadline,
                "tipo": "AVULSO" if movement_type == 7 else "SUBSTITUIÇÃO",
                "situacao": situation,
                "status_id": status_id,
                "linhas_origem": ";".join(str(r["source_line"]) for r in rows),
            }
            if order_movement_logs:
                for movement_log in order_movement_logs:
                    created_log_rows.append({
                        **base_log,
                        "movimento_id": movement_log["movimento_id"],
                        "data_movimento": movement_log["data_movimento"],
                        "separador_id": movement_log["separador_id"],
                        "separador": movement_log["separador"],
                        "itens_movimento": ";".join(
                            f"{item['equipment_number']} (item {item['movement_item_id']}, {item['operation_type']})"
                            for item in movement_log["itens_movimento"]
                        ),
                    })
            else:
                created_log_rows.append({
                    **base_log,
                    "movimento_id": None,
                    "data_movimento": None,
                    "separador_id": None,
                    "separador": None,
                    "itens_movimento": "",
                })
    return order_count, movement_count, created_log_rows


def recuperar_log_existentes(engine) -> tuple[int, int, list[dict]]:
    """Monta um log a partir de uma execução anterior sem recriar os dados."""
    log_rows = []
    with engine.connect() as conn:
        orders = conn.execute(text("""
            SELECT so.id, so.status_id, so.situation, so.movement_type_id,
                   so.recipient_customer_id, c.name AS customer_name,
                   so.user_id, u.username, so.created_at, so.deadline
            FROM service_orders so
            LEFT JOIN customers c ON c.id = so.recipient_customer_id
            LEFT JOIN users u ON u.id = so.user_id
            WHERE so.details LIKE :marker AND so.deleted_at IS NULL
            ORDER BY so.id
        """), {"marker": f"%{MARCADOR_MIGRACAO}%"}).mappings().all()
        movement_count = 0
        for order in orders:
            movements = conn.execute(text("""
                SELECT m.id AS movement_id, m.movement_date, m.created_by,
                       u.username AS separator_username
                FROM movements m
                LEFT JOIN users u ON u.id = m.created_by
                WHERE m.service_order_id = :order_id
                  AND m.details LIKE :marker AND m.deleted_at IS NULL
                ORDER BY m.id
            """), {"order_id": order["id"], "marker": f"%{MARCADOR_MIGRACAO}%"}).mappings().all()
            if not movements:
                log_rows.append({
                    "requisicao_id": order["id"], "cliente_id": order["recipient_customer_id"],
                    "cliente": order["customer_name"], "solicitante_id": order["user_id"],
                    "solicitante": order["username"], "data_requisicao": order["created_at"],
                    "prazo": order["deadline"],
                    "tipo": "AVULSO" if order["movement_type_id"] == 7 else "SUBSTITUIÇÃO",
                    "situacao": order["situation"], "status_id": order["status_id"],
                    "linhas_origem": "Não registrada na execução anterior",
                    "movimento_id": None, "data_movimento": None,
                    "separador_id": None, "separador": None, "itens_movimento": "",
                })
                continue

            for movement in movements:
                movement_count += 1
                items = conn.execute(text("""
                    SELECT mi.id, e.number, mi.operation_type
                    FROM movement_items mi
                    LEFT JOIN equipments e ON e.id = mi.equipment_id
                    WHERE mi.movement_id = :movement_id AND mi.deleted_at IS NULL
                    ORDER BY mi.id
                """), {"movement_id": movement["movement_id"]}).mappings().all()
                log_rows.append({
                    "requisicao_id": order["id"], "cliente_id": order["recipient_customer_id"],
                    "cliente": order["customer_name"], "solicitante_id": order["user_id"],
                    "solicitante": order["username"], "data_requisicao": order["created_at"],
                    "prazo": order["deadline"],
                    "tipo": "AVULSO" if order["movement_type_id"] == 7 else "SUBSTITUIÇÃO",
                    "situacao": order["situation"], "status_id": order["status_id"],
                    "linhas_origem": "Não registrada na execução anterior",
                    "movimento_id": movement["movement_id"],
                    "data_movimento": movement["movement_date"],
                    "separador_id": movement["created_by"], "separador": movement["separator_username"],
                    "itens_movimento": ";".join(
                        f"{item['number']} (item {item['id']}, {item['operation_type']})"
                        for item in items
                    ),
                })
    return len(orders), movement_count, log_rows


def salvar_log(log_rows: list[dict]) -> Path:
    DIR_LOGS.mkdir(parents=True, exist_ok=True)
    log_path = DIR_LOGS / f"requisicoes_2026_criadas_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    pd.DataFrame(log_rows).to_csv(log_path, index=False, encoding="utf-8-sig")
    return log_path


def main(argv=None, engine_new=None):
    parser = argparse.ArgumentParser(description="Migra requisições de 2026.")
    parser.add_argument("--dry-run", action="store_true", help="Só valida e gera auditoria, sem gravar no banco.")
    parser.add_argument("--arquivo", type=Path, default=ARQUIVO_FONTE)
    parser.add_argument("--auditoria", type=Path, default=ARQUIVO_AUDITORIA)
    args = parser.parse_args(argv)
    if engine_new is None:
        from config.database import obter_engines
        engine_new, _ = obter_engines()

    records, audit = preparar(engine_new, args.arquivo)
    args.auditoria.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(audit, columns=["linha", "resultado", "motivo", "cliente",
                                 "cliente_sugerido", "similaridade", "PAT", "Req"]).to_csv(
        args.auditoria, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    movement_records = sum(bool(r["create_movement"]) for r in records)
    print(f"Linhas aprovadas para requisição: {len(records)}")
    print(f"Movimentos previstos: {movement_records}")
    print(f"Linhas/ocorrências auditadas: {len(audit)} — {args.auditoria}")
    if args.dry_run:
        print("Dry-run: nenhuma gravação foi feita.")
        return
    with engine_new.connect() as conn:
        existing_count = conn.execute(text(
            "SELECT COUNT(*) FROM service_orders WHERE details LIKE :marker AND deleted_at IS NULL"
        ), {"marker": f"%{MARCADOR_MIGRACAO}%"}).scalar_one()
    if existing_count:
        orders, movements, log_rows = recuperar_log_existentes(engine_new)
        log_path = salvar_log(log_rows)
        print(f"Já existem {orders} requisições dessa migração; nenhuma duplicata foi criada.")
        print(f"Log dos registros existentes ({movements} movimentos): {log_path}")
        return
    orders, movements, log_rows = criar(engine_new, records)
    log_path = salvar_log(log_rows)
    print(f"Gravação concluída: {orders} requisições e {movements} movimentos.")
    print(f"Log de criação: {log_path}")


if __name__ == "__main__":
    main()
