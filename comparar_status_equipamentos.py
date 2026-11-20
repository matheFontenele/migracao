#!/usr/bin/env python3
"""Relatório comparativo dos status de equipamentos (opcionalmente aplica ajustes)."""
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime

from sqlalchemy import text

from config.database import obter_engines


MAPA_STATUS = {
    1: 2, 2: 4, 3: 6, 4: 9, 5: 5, 6: 7, 7: 1, 8: 1,
    9: 6, 10: 9, 11: 1, 12: 7, 13: 6, 14: 8, 15: 3, 16: 5,
}
CAMPOS = [
    "equipment_id", "tombo", "situacao_legado_id", "situacao_legado_nome",
    "status_atual_id", "status_atual_nome", "status_esperado_id",
    "status_esperado_nome", "classificacao",
]


def limpar_tombo(valor):
    if valor is None:
        return ""
    valor = str(valor).strip()
    return valor[:-2] if valor.endswith(".0") else valor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="aplica divergências após confirmação")
    args = parser.parse_args()
    novo, legado = obter_engines()

    with novo.connect() as conn:
        equipments = conn.execute(text("""
            SELECT e.id, e.number, e.status_id, s.name AS status_nome
            FROM equipments e LEFT JOIN status s ON s.id = e.status_id
            WHERE e.deleted_at IS NULL
        """)).mappings().all()
        statuses = dict(conn.execute(text("SELECT id, name FROM status")).all())
    with legado.connect() as conn:
        legacy = conn.execute(text("""
            SELECT a.id, a.numero, a.situacao_id, s.nome AS situacao_nome
            FROM aluguel_equipamentos a
            LEFT JOIN aluguel_situacao s ON s.id = a.situacao_id
            WHERE a.deleted_at IS NULL
        """)).mappings().all()

    by_key_new = {(int(r["id"]), limpar_tombo(r["number"])): r for r in equipments}
    by_key_old = {(int(r["id"]), limpar_tombo(r["numero"])): r for r in legacy}
    keys = set(by_key_new) | set(by_key_old)
    report = []
    updates = []
    counts = Counter()
    divergence_names = Counter()
    for key in sorted(keys):
        old, new = by_key_old.get(key), by_key_new.get(key)
        expected = MAPA_STATUS.get(int(old["situacao_id"])) if old and old["situacao_id"] is not None else None
        current = int(new["status_id"]) if new and new["status_id"] is not None else None
        if not old:
            classification = "SEM_MATCH_LEGADO"
        elif not new:
            classification = "SEM_MATCH_REFATORADO"
        elif expected is None:
            classification = "SITUACAO_NAO_MAPEADA"
        elif current == expected:
            classification = "OK"
        else:
            classification = "DIVERGENTE"
        counts[classification] += 1
        if classification == "DIVERGENTE":
            divergence_names[old["situacao_nome"] or "(sem nome)"] += 1
            updates.append({"equipment_id": key[0], "status_id": expected})
        report.append({
            "equipment_id": key[0], "tombo": key[1],
            "situacao_legado_id": old["situacao_id"] if old else "",
            "situacao_legado_nome": old["situacao_nome"] if old else "",
            "status_atual_id": new["status_id"] if new else "",
            "status_atual_nome": new["status_nome"] if new else "",
            "status_esperado_id": expected if expected is not None else "",
            "status_esperado_nome": statuses.get(expected, "") if expected is not None else "",
            "classificacao": classification,
        })

    os.makedirs("ajustes", exist_ok=True)
    output = "ajustes/divergencias_status_equipamentos.csv"
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS)
        writer.writeheader()
        writer.writerows(report)

    print(f"Total legado: {len(legacy)}")
    print(f"Total refatorado: {len(equipments)}")
    print(f"Matched: {counts['OK'] + counts['DIVERGENTE'] + counts['SITUACAO_NAO_MAPEADA']}")
    print(f"OK: {counts['OK']}")
    print(f"Divergente: {counts['DIVERGENTE']}")
    print(f"Sem match legado: {counts['SEM_MATCH_LEGADO']}")
    print(f"Sem match refatorado: {counts['SEM_MATCH_REFATORADO']}")
    print(f"Situação não mapeada: {counts['SITUACAO_NAO_MAPEADA']}")
    print("Divergências por situação legada (top 20):")
    for name, total in divergence_names.most_common(20):
        print(f"  {name}: {total}")
    print(f"CSV: {output}")

    if args.apply and updates:
        print("Digite exatamente CONFIRMO CORRIGIR STATUS para aplicar:")
        if sys.stdin.readline().rstrip("\r\n") != "CONFIRMO CORRIGIR STATUS":
            print("Confirmação inválida; nenhuma alteração realizada.")
            return 1
        with novo.begin() as conn:
            for start in range(0, len(updates), 500):
                conn.execute(text("UPDATE equipments SET status_id=:status_id WHERE id=:equipment_id"), updates[start:start + 500])
        log = f"ajustes/status_corrigidos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(log, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["equipment_id", "status_id"])
            writer.writeheader()
            writer.writerows(updates)
        print(f"Correções aplicadas; log: {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
