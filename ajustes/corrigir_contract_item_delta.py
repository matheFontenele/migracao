#!/usr/bin/env python3
"""Corrige as referencias de contrato importadas pelo delta de producao.

O importador do delta nao traduzia `contracts.contract_id` nem
`service_order_items.contract_item_id`: copiava o id cru da producao. Como os
ids de contrato/item nao alinham entre `prod_14_11` e o banco local, algumas
OS do delta ficaram ligadas ao contrato errado.

Este script recalcula o vinculo pela chave natural - contrato pelo numero,
item por (contrato, alias, aditivo, tipo, datas) com desempate por preco e
quantidade - usando as mesmas funcoes do importador. Quando o contrato de
origem nao existe localmente (ex.: contrato ignorado pela planilha), o vinculo
e anulado em vez de apontar para outro contrato.

Sem `--apply` e dry-run: mostra o plano e grava o CSV de auditoria.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import create_engine, text

from config.database import obter_engines
from importar_delta_movimentos import build_contract_maps

PROD_URL = "mysql+pymysql://root:root@localhost:3307/prod_14_11"
CROSSWALK = "ajustes/delta_apply_20261123_1034/delta_crosswalk.csv"
LEGADO_CSV = "ajustes/refs_contrato_cruzadas_legado.csv"


def pares_do_crosswalk(caminho):
    """Pares prod -> local das linhas inseridas (delta_aplicado.csv)."""
    aplicado = Path(caminho)
    if aplicado.name != "delta_aplicado.csv":
        aplicado = aplicado.with_name("delta_aplicado.csv")
    df = pd.read_csv(aplicado, usecols=["table", "source_id", "local_id", "action"])
    inseridos = df[df.action == "insert"]
    pares_so = {int(r.source_id): int(r.local_id) for r in
                inseridos[inseridos.table == "service_orders"].itertuples()}
    pares_soi = {int(r.source_id): int(r.local_id) for r in
                 inseridos[inseridos.table == "service_order_items"].itertuples()}
    return pares_so, pares_soi


def coluna_origem(engine_prod, tabela, coluna, ids):
    if not ids:
        return {}
    lista = ",".join(str(int(v)) for v in ids)
    with engine_prod.connect() as conn:
        return {int(r[0]): (int(r[1]) if r[1] is not None else None)
                for r in conn.execute(text(
                    f"SELECT id, {coluna} FROM {tabela} WHERE id IN ({lista})"))}


def montar_plano(engine, engine_prod, pares_so, pares_soi):
    prod_sos = coluna_origem(engine_prod, "service_orders", "contract_id", pares_so)
    prod_sois = coluna_origem(
        engine_prod, "service_order_items", "contract_item_id", pares_soi)
    maps = build_contract_maps(
        engine, engine_prod, [v for v in prod_sois.values() if v is not None])
    contratos, itens = maps["contracts"], maps["contract_items"]
    com_item = sum(1 for v in prod_sois.values() if v is not None)
    print(f"Contratos resolvidos por numero: {len(contratos)} | "
          f"itens resolvidos por chave natural: {len(itens)}/{com_item}")
    atual_so = coluna_origem(engine, "service_orders", "contract_id",
                             pares_so.values())
    atual_soi = coluna_origem(engine, "service_order_items", "contract_item_id",
                              pares_soi.values())

    plano = []
    for source, local in sorted(pares_so.items()):
        prod_contrato = prod_sos.get(source)
        if prod_contrato is None or prod_contrato in contratos:
            continue
        atual = atual_so.get(local)
        if atual is not None:
            plano.append({
                "tabela": "service_orders", "local_id": local,
                "source_id": source, "coluna": "contract_id",
                "de": atual, "para": "",
                "motivo": f"contrato prod {prod_contrato} sem correspondencia local",
            })

    for source, local in sorted(pares_soi.items()):
        prod_item = prod_sois.get(source)
        if prod_item is None:
            continue
        atual = atual_soi.get(local)
        alvo = itens.get(prod_item)
        if alvo is None:
            if atual is not None:
                plano.append({
                    "tabela": "service_order_items", "local_id": local,
                    "source_id": source, "coluna": "contract_item_id",
                    "de": atual, "para": "",
                    "motivo": f"item prod {prod_item} sem correspondencia local",
                })
            continue
        if atual != alvo:
            plano.append({
                "tabela": "service_order_items", "local_id": local,
                "source_id": source, "coluna": "contract_item_id",
                "de": atual if atual is not None else "", "para": alvo,
                "motivo": "chave natural (contrato/alias/aditivo)",
            })
    return plano


def gravar_plano(plano, caminho):
    campos = ["tabela", "local_id", "source_id", "coluna", "de", "para", "motivo"]
    with open(caminho, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=campos)
        writer.writeheader()
        writer.writerows(plano)


def relatorio_legado(engine, caminho):
    """Exporta as refs cruzadas que vem do legado e nao tem match deterministico."""
    query = """
    SELECT soi.id AS service_order_item_id, soi.service_order_id,
           so.contract_id AS contrato_os, ce.contract_id AS contrato_item,
           TRIM(ci.alias) AS alias, ci.price, ci.quantity, soi.created_at
    FROM service_order_items soi
    JOIN service_orders so ON so.id = soi.service_order_id
    JOIN contract_items ci ON ci.id = soi.contract_item_id
    JOIN event_additives ea ON ea.id = ci.event_additive_id
    JOIN contract_events ce ON ce.id = ea.event_id
    WHERE soi.contract_item_id IS NOT NULL
      AND ce.contract_id <> so.contract_id
    ORDER BY soi.id
    """
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(text(query))]
    if not rows:
        print("✅ Nenhuma referencia cruzada restante.")
        return
    with open(caminho, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"📄 {len(rows)} ref(s) cruzada(s) em {caminho}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Correcao das refs de contrato do delta")
    parser.add_argument("--crosswalk", default=CROSSWALK,
                        help="Pasta do delta: usa o delta_aplicado.csv ao lado")
    parser.add_argument("--prod-url", default=PROD_URL)
    parser.add_argument("--out", default=None)
    parser.add_argument("--legado-csv", default=LEGADO_CSV)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    engine, _ = obter_engines()
    engine_prod = create_engine(args.prod_url, pool_pre_ping=True, future=True)

    pares_so, pares_soi = pares_do_crosswalk(args.crosswalk)
    print(f"Delta: {len(pares_so)} OS e {len(pares_soi)} itens novos")
    plano = montar_plano(engine, engine_prod, pares_so, pares_soi)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out or f"ajustes/corrigir_contract_item_delta_{stamp}.csv")
    gravar_plano(plano, out)

    n_so = sum(1 for p in plano if p["tabela"] == "service_orders")
    print(f"\nPlano: {n_so} OS e {len(plano) - n_so} itens de OS")
    for p in plano:
        print(f"  {p['tabela']} {p['local_id']}: {p['coluna']} "
              f"{p['de'] or 'NULL'} -> {p['para'] or 'NULL'} ({p['motivo']})")
    print(f"📄 {out}")

    if args.apply and plano:
        with engine.begin() as conn:
            for p in plano:
                conn.execute(text(
                    f"UPDATE `{p['tabela']}` SET `{p['coluna']}` = :valor "
                    f"WHERE id = :local"),
                    {"valor": p["para"] or None, "local": p["local_id"]})
        print(f"🏁 Aplicado: {len(plano)} vinculo(s) atualizado(s).")
    elif not args.apply:
        print("🔒 Dry-run: nada foi gravado.")

    relatorio_legado(engine, args.legado_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
