#!/usr/bin/env python3
"""Extrai da producao a janela do delta e grava o JSON do importador.

Saida no formato esperado por `importar_delta_movimentos.load_delta` e por
`atualizar_status_equipamentos.carregar_alteracoes`:

    [{"tabela": "<nome>", "registro_json": "{...}"}, ...]

Regras:
- Somente SELECT na origem (`--prod-url`); nada e escrito em banco.
- A janela e [inicio, fim] por DIA, com limite superior inclusivo, igual ao
  `in_window` do importador (no SQL: fim + 1 dia, exclusivo).
- Tabelas "pai" entram por data de criacao (equipment_history por
  `occurred_at`; equipments tambem por `updated_at`, para o ajuste de status).
- Tabelas "filho" entram por vinculo com um pai da janela, nunca por
  `created_at`: volumes/itens foram recarregados em bloco e carregam a data
  da carga, nao a da operacao.
- `maintenances` entram por criacao; as antigas referenciadas por itens novos
  sao buscadas pelo proprio importador (closure na origem).

Uso:
    python extrair_delta_prod.py
    python extrair_delta_prod.py --inicio 2026-11-04 --fim 2026-11-12 \
        --out /tmp/opencode/delta_prod_04_12_11.json
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

DEFAULT_PROD = "mysql+pymysql://root:root@localhost:3307/prod_14_11"
CHUNK = 900


def _json_ready(row):
    """Converte tipos do driver em valores JSON sem perder informacao."""
    out = {}
    for column, value in row.items():
        if isinstance(value, (datetime, date)):
            out[column] = str(value)
        elif isinstance(value, bytes):
            out[column] = value.decode("utf-8", "replace")
        else:
            out[column] = value
    return out


def _select(conn, sql, params=None):
    from sqlalchemy import text
    return [_json_ready(dict(r._mapping)) for r in conn.execute(text(sql), params or {})]


def _window(conn, table, column, start, end):
    return _select(
        conn,
        f"SELECT * FROM `{table}` WHERE `{column}` >= :start AND `{column}` < :end",
        {"start": start, "end": end})


def _by_ids(conn, table, column, ids, extra="", extra_params=None):
    """Busca linhas por lista de ids, em blocos; `extra` e um OR opcional."""
    from sqlalchemy import bindparam, text

    ids = sorted({int(i) for i in ids if i is not None})
    rows = []
    if not ids:
        if not extra:
            return rows
        params = dict(extra_params or {})
        return _select(conn, f"SELECT * FROM `{table}` WHERE {extra}", params)
    for start in range(0, len(ids), CHUNK):
        params = dict(extra_params or {})
        params["ids"] = ids[start:start + CHUNK]
        sql = f"SELECT * FROM `{table}` WHERE `{column}` IN :ids"
        if extra:
            sql += f" OR ({extra})"
        stmt = text(sql).bindparams(bindparam("ids", expanding=True))
        rows.extend(_json_ready(dict(r._mapping)) for r in conn.execute(stmt, params))
    return _dedup(rows)


def _dedup(rows, key="id"):
    vistos = {}
    for row in rows:
        vistos[row.get(key)] = row
    return list(vistos.values())


def _ids(rows, column="id"):
    return {r.get(column) for r in rows if r.get(column) is not None}


def extrair(prod_url, inicio, fim, saida, relatorio):
    from sqlalchemy import create_engine

    start = datetime.combine(inicio, time.min)
    end = datetime.combine(fim + timedelta(days=1), time.min)
    engine = create_engine(prod_url, pool_pre_ping=True, future=True)
    dados = {}

    with engine.connect() as conn:
        movements = _window(conn, "movements", "created_at", start, end)
        orders = _window(conn, "service_orders", "created_at", start, end)
        equipments = _window(conn, "equipments", "created_at", start, end)
        equipments_atualizados = _select(
            conn,
            "SELECT * FROM equipments WHERE updated_at >= :start AND updated_at < :end "
            "AND (created_at IS NULL OR created_at < :start)",
            {"start": start, "end": end})
        maintenances = _window(conn, "maintenances", "created_at", start, end)
        historico = _window(conn, "equipment_history", "occurred_at", start, end)

        movement_ids = _ids(movements)
        order_ids = _ids(orders)
        maintenance_ids = _ids(maintenances)

        itens_mov = _by_ids(conn, "movement_items", "movement_id", movement_ids)
        volumes = _by_ids(conn, "volumes", "movement_id", movement_ids)
        itens_os = _by_ids(conn, "service_order_items", "service_order_id", order_ids)
        itens_manut = _by_ids(
            conn, "maintenance_items", "maintenance_id", maintenance_ids,
            extra="`created_at` >= :start AND `created_at` < :end",
            extra_params={"start": start, "end": end})

        volume_items = _by_ids(conn, "volume_items", "volume_id", _ids(volumes))
        volume_items += _by_ids(conn, "volume_items", "movement_item_id", _ids(itens_mov))
        volume_items = _dedup(volume_items)

        shipment_items = _by_ids(
            conn, "shipment_items", "movement_item_id", _ids(itens_mov))
        shipments = _by_ids(conn, "shipments", "id", shipment_ids_of(shipment_items))
        # `shipment_movements` entra por criacao: o vinculo com o envio nao
        # delimita a janela (o envio pode ser antigo e receber movimento novo).
        shipment_movements = _window(
            conn, "shipment_movements", "created_at", start, end)

        # Itens de transacao citados pelos equipamentos/movimentos e a transacao
        # pai. Sem `transaction_items` no JSON o importador nao descobre a FK do
        # item (`transaction_id` nao e FK declarada) e bloqueia o equipamento.
        transaction_item_ids = {
            row["transaction_item_id"]
            for row in equipments + equipments_atualizados + itens_mov
            if row.get("transaction_item_id") is not None}
        transaction_items = _by_ids(
            conn, "transaction_items", "id", transaction_item_ids)
        transaction_ids = {r.get("transaction_id") for r in transaction_items
                           if r.get("transaction_id") is not None}
        transactions = _by_ids(conn, "transactions", "id", transaction_ids)

        dados.update({
            "movements": movements,
            "service_orders": orders,
            "equipments": _dedup(equipments + equipments_atualizados),
            "maintenances": maintenances,
            "equipment_history": historico,
            "service_order_items": itens_os,
            "movement_items": itens_mov,
            "volumes": volumes,
            "volume_items": volume_items,
            "shipment_items": shipment_items,
            "shipments": shipments,
            "shipment_movements": shipment_movements,
            "maintenance_items": itens_manut,
            "transaction_items": transaction_items,
            "transactions": transactions,
        })

    entries = []
    for tabela, rows in dados.items():
        for row in rows:
            entries.append({
                "tabela": tabela,
                "registro_json": json.dumps(row, ensure_ascii=False, default=str),
            })

    saida = Path(saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(entries, ensure_ascii=False, default=str), encoding="utf-8")

    criados = len(equipments)
    relatorio = Path(relatorio)
    relatorio.parent.mkdir(parents=True, exist_ok=True)
    with relatorio.open("w", newline="", encoding="utf-8") as handle:
        escritor = csv.writer(handle)
        escritor.writerow(["table", "rows", "created_in_window", "updated_only"])
        for tabela in sorted(dados):
            rows = dados[tabela]
            if tabela == "equipments":
                escritor.writerow([tabela, len(rows), criados, len(rows) - criados])
            else:
                escritor.writerow([tabela, len(rows), len(rows), 0])

    print(f"JSON: {saida} ({len(entries)} registros)")
    print(f"Relatorio: {relatorio}")
    for tabela in sorted(dados):
        print(f"  {tabela:24} {len(dados[tabela]):>6}")
    return dados


def shipment_ids_of(shipment_items):
    return {r.get("shipment_id") for r in shipment_items
            if r.get("shipment_id") is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extrai a janela do delta da producao")
    parser.add_argument("--prod-url", default=DEFAULT_PROD)
    parser.add_argument("--inicio", default="2026-11-04")
    parser.add_argument("--fim", default="2026-11-12")
    parser.add_argument("--out", default="ajustes/delta_prod_04_12_11.json")
    parser.add_argument("--relatorio", default="ajustes/delta_extracao.csv")
    args = parser.parse_args(argv)

    extrair(args.prod_url, date.fromisoformat(args.inicio),
            date.fromisoformat(args.fim), args.out, args.relatorio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
