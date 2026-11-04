#!/usr/bin/env python3
"""Backfill controlado de delivery_date para itens AVULSO já entregues.

Sem --apply, o script apenas audita os registros e gera um CSV. O escopo deve
ser informado explicitamente com --customer-id ou --all.

Exemplos:
    python backfill_shipment_delivery_date.py --customer-id 2
    python backfill_shipment_delivery_date.py --customer-id 2 --apply
    python backfill_shipment_delivery_date.py --all
    python backfill_shipment_delivery_date.py --all --apply
"""

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from config.database import obter_engines


FILTRO_COMUM = """
      si.delivery_date IS NULL
  AND si.deleted_at IS NULL
  AND ss.name = 'ENTREGUE'
  AND s.deleted_at IS NULL
  AND mi.deleted_at IS NULL
  AND mi.operation_type = 'AVULSO'
  AND m.deleted_at IS NULL
"""

CONSULTA_CANDIDATOS = f"""
    SELECT
        si.id AS shipment_item_id,
        si.shipment_id,
        si.movement_item_id,
        m.recipient_customer_id AS customer_id,
        c.name AS customer_name,
        m.organization_id,
        m.id AS movement_id,
        m.number AS legacy_movement_number,
        ss.name AS shipment_status,
        mi.operation_type,
        si.delivery_date AS current_delivery_date,
        si.created_at AS shipment_item_created_at,
        s.created_at AS shipment_created_at,
        m.created_at AS movement_created_at,
        m.movement_date,
        DATE(m.created_at) AS proposed_delivery_date,
        CASE
            WHEN si.created_at IS NOT NULL
             AND s.created_at IS NOT NULL
             AND m.created_at IS NOT NULL
             AND m.movement_date IS NOT NULL
             AND DATE(si.created_at) = DATE(s.created_at)
             AND DATE(s.created_at) = DATE(m.created_at)
             AND DATE(m.created_at) = DATE(m.movement_date)
            THEN 1 ELSE 0
        END AS dates_agree
    FROM shipment_items si
    INNER JOIN shipment_status ss ON ss.id = si.status_id
    INNER JOIN shipments s ON s.id = si.shipment_id
    INNER JOIN movement_items mi ON mi.id = si.movement_item_id
    INNER JOIN movements m ON m.id = mi.movement_id
    LEFT JOIN customers c ON c.id = m.recipient_customer_id
    WHERE {FILTRO_COMUM}
      {{customer_filter}}
    ORDER BY si.id
"""

ATUALIZAR_CANDIDATOS = f"""
    UPDATE shipment_items si
    INNER JOIN shipment_status ss ON ss.id = si.status_id
    INNER JOIN shipments s ON s.id = si.shipment_id
    INNER JOIN movement_items mi ON mi.id = si.movement_item_id
    INNER JOIN movements m ON m.id = mi.movement_id
    SET si.delivery_date = DATE(m.created_at)
    WHERE {FILTRO_COMUM}
      AND si.created_at IS NOT NULL
      AND s.created_at IS NOT NULL
      AND m.created_at IS NOT NULL
      AND m.movement_date IS NOT NULL
      AND DATE(si.created_at) = DATE(s.created_at)
      AND DATE(s.created_at) = DATE(m.created_at)
      AND DATE(m.created_at) = DATE(m.movement_date)
      {{customer_filter}}
"""


def carregar_candidatos(engine, customer_id=None):
    filtro = "AND m.recipient_customer_id = :customer_id" if customer_id is not None else ""
    params = {"customer_id": customer_id} if customer_id is not None else {}
    with engine.connect() as conn:
        return pd.read_sql(
            text(CONSULTA_CANDIDATOS.format(customer_filter=filtro)),
            conn,
            params=params,
        )


def salvar_auditoria(df, caminho):
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destino, index=False, encoding="utf-8-sig")
    print(f"Auditoria gravada em: {destino}")


def exportar_sql(caminho, customer_id=None, expected_rows=None):
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    filtro_cliente = (
        f"\n  AND m.recipient_customer_id = {int(customer_id)}"
        if customer_id is not None
        else ""
    )
    sql = f"""-- Backfill de delivery_date gerado localmente.
-- Escopo: {'customer_id=' + str(customer_id) if customer_id is not None else 'todos os clientes'}
-- Registros atualizáveis encontrados localmente: {expected_rows if expected_rows is not None else 'não auditado'}
-- Fonte da data: DATE(movements.created_at).
--
-- O script NÃO conecta à produção. Execute este arquivo na sessão do banco
-- de produção, confira updated_rows e finalize manualmente com COMMIT ou
-- ROLLBACK. Não feche a sessão antes de finalizar a transação.

START TRANSACTION;

UPDATE shipment_items AS si
INNER JOIN shipment_status AS ss ON ss.id = si.status_id
INNER JOIN shipments AS s ON s.id = si.shipment_id
INNER JOIN movement_items AS mi ON mi.id = si.movement_item_id
INNER JOIN movements AS m ON m.id = mi.movement_id
SET si.delivery_date = DATE(m.created_at)
WHERE si.delivery_date IS NULL
  AND si.deleted_at IS NULL
  AND ss.name = 'ENTREGUE'
  AND s.deleted_at IS NULL
  AND mi.deleted_at IS NULL
  AND mi.operation_type = 'AVULSO'
  AND m.deleted_at IS NULL
  AND si.created_at IS NOT NULL
  AND s.created_at IS NOT NULL
  AND m.created_at IS NOT NULL
  AND m.movement_date IS NOT NULL
  AND DATE(si.created_at) = DATE(s.created_at)
  AND DATE(s.created_at) = DATE(m.created_at)
  AND DATE(m.created_at) = DATE(m.movement_date){filtro_cliente};

SELECT ROW_COUNT() AS updated_rows;

-- Se updated_rows estiver correto, execute: COMMIT;
-- Se estiver diferente do esperado, execute: ROLLBACK;
"""
    destino.write_text(sql, encoding="utf-8")
    print(f"SQL para execução manual gravado em: {destino}")


def executar_backfill(
    engine_new,
    *,
    customer_id=None,
    all_customers=False,
    apply=False,
    audit_csv="ajustes/backfill_delivery_date_audit.csv",
    export_sql_path=None,
):
    if (customer_id is None) == (not all_customers):
        raise ValueError("Informe exatamente um escopo: customer_id ou all_customers=True.")

    df = carregar_candidatos(engine_new, customer_id)
    if not df.empty:
        df["dates_agree"] = df["dates_agree"].astype(bool)

    seguros = df[df["dates_agree"]] if not df.empty else df
    divergentes = df[~df["dates_agree"]] if not df.empty else df
    salvar_auditoria(df, audit_csv)
    if export_sql_path:
        exportar_sql(export_sql_path, customer_id, len(seguros))

    escopo_texto = f"customer_id={customer_id}" if customer_id is not None else "todos os clientes"
    print(f"Escopo: {escopo_texto}")
    print(f"Itens AVULSO entregues com delivery_date nula: {len(df)}")
    print(f"Itens com datas concordantes (atualizáveis): {len(seguros)}")
    print(f"Itens com datas divergentes/incompletas (preservados): {len(divergentes)}")

    if not df.empty:
        colunas_amostra = [
            "shipment_item_id",
            "customer_id",
            "customer_name",
            "shipment_item_created_at",
            "shipment_created_at",
            "movement_created_at",
            "movement_date",
            "proposed_delivery_date",
            "dates_agree",
        ]
        print("\nAmostra:")
        print(df[colunas_amostra].head(10).to_string(index=False))

    if not apply:
        print("\nDry-run: nenhuma linha foi alterada. Revise o CSV antes de usar --apply.")
        return

    if seguros.empty:
        print("\nNenhum item seguro para atualizar; nenhuma alteração aplicada.")
        return

    filtro = "AND m.recipient_customer_id = :customer_id" if customer_id is not None else ""
    params = {"customer_id": customer_id} if customer_id is not None else {}
    query = text(ATUALIZAR_CANDIDATOS.format(customer_filter=filtro))

    with engine_new.begin() as conn:
        resultado = conn.execute(query, params)
        atualizados = resultado.rowcount
        if atualizados != len(seguros):
            raise RuntimeError(
                "A quantidade atualizada divergiu da auditoria: "
                f"esperado={len(seguros)}, atualizado={atualizados}. "
                "A transação será revertida; gere uma nova auditoria antes de tentar novamente."
            )

    print(f"\nBackfill concluído: {atualizados} delivery_date(s) preenchida(s).")
    if len(divergentes):
        print("Os registros divergentes foram mantidos sem alteração; consulte o CSV.")


def main(argv=None, engine_new=None):
    parser = argparse.ArgumentParser(description=__doc__)
    escopo = parser.add_mutually_exclusive_group(required=True)
    escopo.add_argument("--customer-id", type=int, help="Limita a execução a um cliente.")
    escopo.add_argument("--all", action="store_true", help="Considera todos os clientes.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica o backfill. Sem esta opção, apenas audita (dry-run).",
    )
    parser.add_argument(
        "--audit-csv",
        default="ajustes/backfill_delivery_date_audit.csv",
        help="Caminho do CSV de auditoria.",
    )
    parser.add_argument(
        "--export-sql",
        help="Gera SQL para execução manual no destino; não conecta ao destino.",
    )
    args = parser.parse_args(argv)

    if engine_new is None:
        engine_new, _ = obter_engines()
    executar_backfill(
        engine_new,
        customer_id=args.customer_id,
        all_customers=args.all,
        apply=args.apply,
        audit_csv=args.audit_csv,
        export_sql_path=args.export_sql,
    )


if __name__ == "__main__":
    main()
