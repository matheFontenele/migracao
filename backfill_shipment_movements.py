#!/usr/bin/env python3
"""Backfill de `shipment_movements` para envios sem vinculo com o movimento.

Causa: o delta de producao importava `shipments` e `shipment_items`, mas nao
`shipment_movements` (e a extracao por janela de data nao via as linhas, que
nascem com created_at/updated_at nulos na producao).

O par (envio, movimento) e derivado dos proprios itens do envio
(`shipment_items` -> `movement_items`), o que reproduz exatamente os vinculos
existentes na producao. O script e idempotente: so insere pares ausentes.

Sem --apply, apenas audita e grava o CSV. Use --export-sql para gerar o SQL
revisavel de aplicacao manual.

Exemplos:
    python ajustes/backfill_shipment_movements.py
    python ajustes/backfill_shipment_movements.py --apply
    python ajustes/backfill_shipment_movements.py --export-sql ajustes/backfill_shipment_movements.sql
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from config.database import obter_engines


AUDIT_CSV = "ajustes/backfill_shipment_movements_audit.csv"

CONSULTA_PARES = """
    SELECT
        si.shipment_id,
        mi.movement_id,
        s.status_id AS shipment_status_id,
        s.created_at AS shipment_created_at,
        m.number AS movement_number,
        m.recipient_customer_id AS customer_id,
        COUNT(*) AS itens
    FROM shipment_items si
    INNER JOIN movement_items mi
        ON mi.id = si.movement_item_id
       AND mi.deleted_at IS NULL
    INNER JOIN shipments s
        ON s.id = si.shipment_id
       AND s.deleted_at IS NULL
    LEFT JOIN movements m ON m.id = mi.movement_id
    LEFT JOIN shipment_movements sm
        ON sm.shipment_id = si.shipment_id
       AND sm.movement_id = mi.movement_id
    WHERE si.deleted_at IS NULL
      AND sm.id IS NULL
    GROUP BY si.shipment_id, mi.movement_id,
             s.status_id, s.created_at, m.number, m.recipient_customer_id
    ORDER BY si.shipment_id, mi.movement_id
"""

# Envios que ja tem o par, mas com a linha soft-deleted: nao recriar.
CONSULTA_PARES_DELETADOS = """
    SELECT COUNT(*) AS pares
    FROM shipment_items si
    INNER JOIN movement_items mi
        ON mi.id = si.movement_item_id
       AND mi.deleted_at IS NULL
    INNER JOIN shipments s
        ON s.id = si.shipment_id
       AND s.deleted_at IS NULL
    INNER JOIN shipment_movements sm
        ON sm.shipment_id = si.shipment_id
       AND sm.movement_id = mi.movement_id
       AND sm.deleted_at IS NOT NULL
    WHERE si.deleted_at IS NULL
"""

ATUALIZAR = """
    INSERT INTO shipment_movements
        (shipment_id, movement_id, created_at, updated_at, deleted_at)
    SELECT DISTINCT
        si.shipment_id,
        mi.movement_id,
        COALESCE(s.created_at, CURRENT_TIMESTAMP),
        COALESCE(s.created_at, CURRENT_TIMESTAMP),
        NULL
    FROM shipment_items si
    INNER JOIN movement_items mi
        ON mi.id = si.movement_item_id
       AND mi.deleted_at IS NULL
    INNER JOIN shipments s
        ON s.id = si.shipment_id
       AND s.deleted_at IS NULL
    LEFT JOIN shipment_movements sm
        ON sm.shipment_id = si.shipment_id
       AND sm.movement_id = mi.movement_id
    WHERE si.deleted_at IS NULL
      AND sm.id IS NULL
"""

CONSULTA_PENDENTES = """
    SELECT COUNT(*) AS envios
    FROM shipments s
    LEFT JOIN shipment_movements sm ON sm.shipment_id = s.id
    WHERE s.deleted_at IS NULL
      AND sm.id IS NULL
"""


def carregar_pares(engine):
    with engine.connect() as conn:
        pares = pd.read_sql(text(CONSULTA_PARES), conn)
        deletados = int(conn.execute(text(CONSULTA_PARES_DELETADOS)).scalar() or 0)
    return pares, deletados


def contar_envios_sem_vinculo(engine):
    with engine.connect() as conn:
        return int(conn.execute(text(CONSULTA_PENDENTES)).scalar() or 0)


def salvar_auditoria(pares, caminho):
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    pares.to_csv(destino, index=False, encoding="utf-8-sig")
    print(f"Auditoria gravada em: {destino}")


def exportar_sql(caminho, esperado=None):
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    sql = f"""-- Backfill de shipment_movements gerado localmente.
-- Fonte do par: shipment_items -> movement_items (reproduz a producao).
-- Pares esperados: {esperado if esperado is not None else "nao auditado"}.
--
-- O script NAO conecta a producao. Rode este arquivo na sessao do banco de
-- producao, confira inserted_rows e finalize com COMMIT ou ROLLBACK.

START TRANSACTION;

{ATUALIZAR.strip()};

SELECT ROW_COUNT() AS inserted_rows;

-- Se inserted_rows estiver correto, execute: COMMIT;
-- Se estiver diferente do esperado, execute: ROLLBACK;
"""
    destino.write_text(sql, encoding="utf-8")
    print(f"SQL para execucao manual gravado em: {destino}")


def executar(engine_new, *, apply=False, audit_csv=AUDIT_CSV, export_sql_path=None):
    antes = contar_envios_sem_vinculo(engine_new)
    pares, deletados = carregar_pares(engine_new)
    salvar_auditoria(pares, audit_csv)
    if export_sql_path:
        exportar_sql(export_sql_path, len(pares))

    envios = pares["shipment_id"].nunique() if not pares.empty else 0
    movimentos = pares["movement_id"].nunique() if not pares.empty else 0
    print(f"Envios sem nenhum vinculo (antes): {antes}")
    print(f"Pares (envio, movimento) ausentes: {len(pares)}")
    print(f"  envios distintos: {envios} | movimentos distintos: {movimentos}")
    print(f"Pares ja existentes com deleted_at preenchido (preservados): {deletados}")
    if not pares.empty:
        print("\nAmostra:")
        print(pares.head(10).to_string(index=False))

    if not apply:
        print("\nDry-run: nada foi gravado. Use --apply para inserir.")
        return pares

    if pares.empty:
        print("\nNada a inserir.")
        return pares

    with engine_new.begin() as conn:
        resultado = conn.execute(text(ATUALIZAR))
        inseridos = int(resultado.rowcount or 0)

    depois = contar_envios_sem_vinculo(engine_new)
    print(f"\nLinhas inseridas em shipment_movements: {inseridos}")
    print(f"Envios sem nenhum vinculo (depois): {depois}")
    if inseridos != len(pares):
        print("ATENCAO: linhas inseridas diferente dos pares auditados.")
    return pares


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill de shipment_movements a partir dos itens do envio.")
    parser.add_argument("--apply", action="store_true",
                        help="Grava os vinculos ausentes (padrao: dry-run).")
    parser.add_argument("--audit-csv", default=AUDIT_CSV)
    parser.add_argument("--export-sql", default=None,
                        help="Gera SQL revisavel para aplicacao manual.")
    args = parser.parse_args(argv)

    engine_new, _ = obter_engines()
    executar(engine_new, apply=args.apply, audit_csv=args.audit_csv,
             export_sql_path=args.export_sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
