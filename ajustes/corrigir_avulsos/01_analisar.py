#!/usr/bin/env python
"""Dry-run das divergências de histórico apontadas no tombo 48704 (equip. 57351).

100% leitura: nenhum INSERT/UPDATE/DELETE. Gera relatórios em out/.

Frentes:
  A) Aluguel: último movimento ELEGÍVEL do legado (tipo 1/2/5, fora de ciclo de
     substituição) x o que existe no banco local. Mede quantos itens de aluguel
     ficaram de fora por causa do seletor antigo (bug do tombo 48704).
  B) Envio de substituição: movimentos gravados como AVULSO (tipo 7) que são,
     na verdade, o lado "envio" de um ciclo de substituição.
  C) Requisição 2026: AVULSO criado pela planilha que coincide com movimento
     real do legado, e itens de substituição que herdaram tipo 7 do cabeçalho.

Uso: .venv/bin/python ajustes/corrigir_avulsos/01_analisar.py
"""
from __future__ import annotations

import csv
import glob
import os
from collections import defaultdict
from datetime import datetime

import pandas as pd
from sqlalchemy import create_engine, text

URL_LOCAL = "mysql+pymysql://root:root@127.0.0.1:3307/controle-interno"
ENG_LOCAL = create_engine(URL_LOCAL)
ENG_LEGADO = create_engine("mysql+pymysql://root:root@127.0.0.1:3307/aluguel_legado")

AQUI = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(AQUI, "out")
os.makedirs(OUT, exist_ok=True)

MARCADOR_REQ = "Migração REQUISIÇÃO 2026.csv%"

TIPOS = {1: "ALUGUEL", 2: "DEVOLUÇÃO", 3: "SUBSTITUIÇÃO_DEV", 5: "SA", 6: "TRANSFERÊNCIA"}


def carregar_parquet() -> pd.DataFrame:
    arquivos = sorted(glob.glob("./docs/parquets/*.parquet"))
    if not arquivos:
        raise SystemExit("Nenhum parquet em ./docs/parquets")
    dfs = [pd.read_parquet(a).rename(columns=str.upper) for a in arquivos]
    df = pd.concat(dfs, ignore_index=True)
    print(f"parquet(s): {len(arquivos)} | linhas: {len(df):,}")
    return df


def ultimos_movimentos_elegiveis(tombos: list[str]) -> pd.DataFrame:
    """Mesma regra do módulo de aluguel, já corrigida (filtros também na subquery)."""
    lista = ",".join(f"'{t}'" for t in tombos)
    sql = f"""
        SELECT eq.id AS eq_legado, eq.numero AS tombo,
               mov.id AS mov_id, mov.data AS data_mov, mov.tipo_id,
               mov.cliente_id, movi.id AS item_legado, movi.novo_orgao_id
        FROM aluguel_equipamentos eq
        INNER JOIN aluguel_movimento_itens movi ON movi.equipamento_id = eq.id
        INNER JOIN aluguel_movimento mov ON mov.id = movi.movimento_id
        WHERE mov.deleted_at IS NULL AND movi.deleted_at IS NULL
          AND eq.deleted_at IS NULL AND eq.situacao_id = 1
          AND mov.tipo_id IN (1, 2, 5)
          AND NOT EXISTS (
              SELECT 1 FROM aluguel_substituicao sub
              WHERE sub.substituicao_devolucao_id = mov.id)
          AND eq.numero IN ({lista})
          AND mov.id = COALESCE(
              -- A (1) / SA (5) elegível primeiro; devolução (2) só como fallback.
              (SELECT mov_x.id
              FROM aluguel_movimento_itens movi_x
              INNER JOIN aluguel_movimento mov_x ON mov_x.id = movi_x.movimento_id
              WHERE movi_x.equipamento_id = eq.id
                AND mov_x.deleted_at IS NULL AND movi_x.deleted_at IS NULL
                AND mov_x.tipo_id IN (1, 5)
                AND NOT EXISTS (
                    SELECT 1 FROM aluguel_substituicao sub_x
                    WHERE sub_x.substituicao_devolucao_id = mov_x.id)
              ORDER BY mov_x.data DESC, mov_x.id DESC
              LIMIT 1),
              (SELECT mov_x.id
              FROM aluguel_movimento_itens movi_x
              INNER JOIN aluguel_movimento mov_x ON mov_x.id = movi_x.movimento_id
              WHERE movi_x.equipamento_id = eq.id
                AND mov_x.deleted_at IS NULL AND movi_x.deleted_at IS NULL
                AND mov_x.tipo_id = 2
                AND NOT EXISTS (
                    SELECT 1 FROM aluguel_substituicao sub_x
                    WHERE sub_x.substituicao_devolucao_id = mov_x.id)
              ORDER BY mov_x.data DESC, mov_x.id DESC
              LIMIT 1))
    """
    with ENG_LEGADO.connect() as conn:
        return pd.read_sql(text(sql), conn)


def pares_locais() -> set[tuple[str, int]]:
    with ENG_LOCAL.connect() as conn:
        rows = conn.execute(text("""
            SELECT m.number, mi.equipment_id
            FROM movements m JOIN movement_items mi ON mi.movement_id = m.id
            WHERE m.number IS NOT NULL AND m.number <> ''
        """))
        return {(str(n), int(e)) for n, e in rows if e is not None}


def frente_a(df_parquet: pd.DataFrame) -> pd.DataFrame:
    tombos = sorted({str(t).strip() for t in df_parquet["TOMBO"].dropna()})
    print(f"\n[A] tombos no parquet: {len(tombos):,}")
    esperado = ultimos_movimentos_elegiveis(tombos)
    print(f"[A] pares (movimento legado x equipamento) esperados: {len(esperado):,}")
    locais = pares_locais()

    linhas = []
    for _, r in esperado.iterrows():
        chave = (str(int(r["mov_id"])), int(r["eq_legado"]))
        if chave in locais:
            continue
        linhas.append({
            "tombo": r["tombo"],
            "equip_legado_id": int(r["eq_legado"]),
            "movimento_legado_id": int(r["mov_id"]),
            "data_movimento": str(r["data_mov"])[:19],
            "tipo_legado": TIPOS.get(int(r["tipo_id"]), str(r["tipo_id"])),
            "cliente_legado": r["cliente_id"],
            "orgao_novo": r["novo_orgao_id"],
        })
    df = pd.DataFrame(linhas).sort_values(["data_movimento", "tombo"], ascending=[False, True]) \
        if linhas else pd.DataFrame(columns=["tombo"])
    print(f"[A] itens de aluguel AUSENTES no banco local: {len(df):,} "
          f"({df['tombo'].nunique() if len(df) else 0:,} tombos, "
          f"{df['movimento_legado_id'].nunique() if len(df) else 0:,} movimentos)")
    return df


def frente_b() -> pd.DataFrame:
    with ENG_LOCAL.connect() as conn:
        rows = conn.execute(text("""
            SELECT m.id AS mov_local, m.number, m.movement_date, m.recipient_customer_id,
                   COUNT(*) AS itens, GROUP_CONCAT(DISTINCT mt.name) AS tipos,
                   MIN(mi.equipment_id) AS exemplo_equip
            FROM movements m
            JOIN movement_items mi ON mi.movement_id = m.id
            LEFT JOIN movement_types mt ON mt.id = mi.movement_type_id
            WHERE m.details LIKE 'Envio da Substitui%'
            GROUP BY m.id, m.number, m.movement_date, m.recipient_customer_id
            HAVING SUM(mi.movement_type_id = 7) > 0
            ORDER BY m.movement_date DESC
        """)).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    if len(df):
        df["ano"] = pd.to_datetime(df["movement_date"]).dt.year
    print(f"\n[B] envios de substituição com item AVULSO: {len(df):,} movimentos "
          f"({df['ano'].eq(2026).sum() if len(df) else 0:,} em 2026)")
    return df


def frente_c() -> tuple[pd.DataFrame, pd.DataFrame]:
    # C1: AVULSO de requisição sobre movimento real ausente no local
    with ENG_LOCAL.connect() as conn:
        req = {mid: {"num": num, "data": data, "so": so}
               for mid, num, data, so in conn.execute(text(
                   "SELECT id, number, movement_date, service_order_id "
                   "FROM movements WHERE details LIKE :m"), {"m": MARCADOR_REQ})}
        eqreq = defaultdict(list)
        for mid, eid, tname in conn.execute(text("""
                SELECT m.id, mi.equipment_id, mt.name
                FROM movements m JOIN movement_items mi ON mi.movement_id = m.id
                LEFT JOIN movement_types mt ON mt.id = mi.movement_type_id
                WHERE m.details LIKE :m AND mi.equipment_id IS NOT NULL"""),
                {"m": MARCADOR_REQ}):
            eqreq[eid].append((mid, tname))
        locais = {str(r[0]) for r in conn.execute(text(
            "SELECT number FROM movements WHERE number IS NOT NULL AND number <> ''"))}
        tombos = dict(conn.execute(text("SELECT id, number FROM equipments")).all())

    real = defaultdict(dict)
    with ENG_LEGADO.connect() as conn:
        for eid, mid, num, data, tipo in conn.execute(text("""
                SELECT mi.equipamento_id, m.id, m.numero, m.data, m.tipo
                FROM aluguel_movimento_itens mi
                JOIN aluguel_movimento m ON m.id = mi.movimento_id
                WHERE m.data >= '2026-08-01' AND m.tipo IN ('A','SA','SD','D')
                  AND mi.equipamento_id IS NOT NULL""")):
            real[eid][mid] = (str(num), data, tipo)

    c1 = []
    for eid, movs in eqreq.items():
        for mid, tname in movs:
            if tname != "AVULSO" or req[mid]["data"] is None:
                continue
            d = req[mid]["data"]
            visiveis = [(rid,) + v for rid, v in real.get(eid, {}).items()
                        if -30 <= (v[1] - d).days <= 90]
            faltam = [x for x in visiveis if str(x[0]) not in locais]
            if not faltam:
                continue
            presentes = [x for x in visiveis if str(x[0]) in locais]
            c1.append({
                "equip_local_id": eid, "tombo": tombos.get(eid),
                "mov_req_id": mid, "mov_req_number": req[mid]["num"],
                "mov_req_data": d.date().isoformat(), "service_order": req[mid]["so"],
                "legado_ausente": "; ".join(f"{x[0]}:{x[2].date()}:{x[3]}" for x in faltam),
                "legado_presente": "; ".join(f"{x[0]}:{x[2].date()}:{x[3]}" for x in presentes),
            })
    df_c1 = pd.DataFrame(c1).sort_values("mov_req_data", ascending=False) if c1 else pd.DataFrame()

    # C2: movimentos de requisição AVULSO que contêm itens de substituição
    with ENG_LOCAL.connect() as conn:
        rows = conn.execute(text("""
            SELECT m.id AS mov_local, m.number, m.movement_date,
                   COUNT(DISTINCT mi.equipment_id) AS equipamentos,
                   COUNT(*) AS itens_substituicao
            FROM movements m
            JOIN movement_items mi ON mi.movement_id = m.id
            JOIN service_order_items soi ON soi.id = mi.service_order_item_id
            WHERE m.details LIKE :m AND mi.movement_type_id = 7 AND soi.is_exchange = 1
            GROUP BY m.id, m.number, m.movement_date
            ORDER BY m.movement_date DESC
        """), {"m": MARCADOR_REQ}).mappings().all()
    df_c2 = pd.DataFrame([dict(r) for r in rows])

    print(f"\n[C1] AVULSO de requisição + movimento real ausente: {len(df_c1):,} casos")
    print(f"[C2] movimentos de requisição AVULSO com item de substituição: {len(df_c2):,} "
          f"movimentos / {int(df_c2['itens_substituicao'].sum()) if len(df_c2) else 0:,} itens")
    return df_c1, df_c2


def main() -> None:
    print("=" * 78)
    print("DRY-RUN — divergências de histórico (somente leitura)")
    print("=" * 78)

    df_parquet = carregar_parquet()
    a = frente_a(df_parquet)
    b = frente_b()
    c1, c2 = frente_c()

    a.to_csv(os.path.join(OUT, "frente_a_aluguel_ausente.csv"), index=False, encoding="utf-8-sig")
    b.to_csv(os.path.join(OUT, "frente_b_envio_avulso.csv"), index=False, encoding="utf-8-sig")
    c1.to_csv(os.path.join(OUT, "frente_c1_requisicao_sobre_real.csv"), index=False, encoding="utf-8-sig")
    c2.to_csv(os.path.join(OUT, "frente_c2_requisicao_mista.csv"), index=False, encoding="utf-8-sig")

    resumo = {
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "a_itens_aluguel_ausentes": len(a),
        "a_tombos_ausentes": int(a["tombo"].nunique()) if len(a) and "tombo" in a else 0,
        "a_movimentos_ausentes": int(a["movimento_legado_id"].nunique()) if len(a) and "movimento_legado_id" in a else 0,
        "b_envios_avulso": len(b),
        "b_envios_avulso_2026": int(b["ano"].eq(2026).sum()) if len(b) and "ano" in b else 0,
        "c1_casos": len(c1),
        "c2_movimentos": len(c2),
        "c2_itens": int(c2["itens_substituicao"].sum()) if len(c2) else 0,
    }
    with open(os.path.join(OUT, "resumo.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(resumo))
        w.writeheader()
        w.writerow(resumo)

    print("\n" + "=" * 78)
    for k, v in resumo.items():
        print(f"  {k}: {v}")
    print(f"\nrelatórios em {OUT}")
    print("Nenhuma escrita no banco.")


if __name__ == "__main__":
    main()
