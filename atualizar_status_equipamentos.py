#!/usr/bin/env python3
"""Atualiza no banco local a situacao dos equipamentos alterada em producao.

O importador do delta (`importar_delta_movimentos.py`) so insere registros
criados na janela; linhas apenas *atualizadas* na origem ficam de fora. Este
modulo cobre essa lacuna para `equipments.status_id`.

Regras:
- Mesma fonte do delta: JSON com `tabela`/`registro_json`, janela 04/11 a
  12/11/2026 (limite superior inclusivo, igual ao `in_window` do importador).
- So entram equipamentos cuja criacao esta FORA da janela e cujo `updated_at`
  cai DENTRO dela.
- Casamento com o registro local: mesmo `id` + mesmo numero; se o id nao
  existir, numero unico (com serie para desempatar).
- Nada e escrito sem `--apply`. Relatorio CSV sempre gerado.

Uso:
    .venv/bin/python atualizar_status_equipamentos.py
    .venv/bin/python atualizar_status_equipamentos.py --apply
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from importar_delta_movimentos import (
    as_date,
    creation_date,
    in_window,
    normalize,
    normalize_record,
    normalize_number,
)


JSON_PADRAO = "/tmp/opencode/delta_prod_04_12_11.json"
INICIO_PADRAO = "2026-11-04"
FIM_PADRAO = "2026-11-12"  # limite inclusivo (janela do importador do delta)
SAIDA_PADRAO = "ajustes/status_equipamentos_04_12.csv"


def carregar_alteracoes(caminho, inicio: date, fim: date):
    """Equipamentos atualizados (nao criados) dentro da janela."""
    dados = json.loads(Path(caminho).read_bytes())
    alteracoes = []
    for item in dados:
        if item.get("tabela") != "equipments":
            continue
        row = normalize_record(item.get("registro_json"))
        criado = creation_date("equipments", row)
        if in_window(criado, inicio, fim):
            continue
        if not in_window(as_date(row.get("updated_at")), inicio, fim):
            continue
        alteracoes.append(row)
    return alteracoes


def _indice_por_numero(locais):
    indice = defaultdict(list)
    for local in locais:
        numero = normalize_number(local.get("number"))
        if numero is not None and not local.get("deleted_at"):
            indice[numero].append(local)
    return indice


def casar_local(origem, por_id, por_numero):
    """Retorna (local, metodo) ou (None, motivo)."""
    numero = normalize_number(origem.get("number"))
    local = por_id.get(origem.get("id"))
    if local is not None and normalize_number(local.get("number")) == numero:
        return local, "id+numero"

    if numero is None:
        return None, "equipamento sem numero local"

    candidatos = por_numero.get(numero, [])
    if len(candidatos) == 1:
        return candidatos[0], "numero"
    if len(candidatos) > 1:
        serie = normalize(origem.get("serial_number"))
        if serie:
            exatos = [c for c in candidatos
                      if normalize(c.get("serial_number")) == serie]
            if len(exatos) == 1:
                return exatos[0], "numero+serie"
        return None, f"numero duplicado local ({len(candidatos)} candidatos)"
    return None, "equipamento nao encontrado localmente"


def planejar(alteracoes, locais, status_validos):
    """Monta a lista de updates sem tocar no banco."""
    por_id = {local["id"]: local for local in locais}
    por_numero = _indice_por_numero(locais)
    updates, pendencias = [], []

    for origem in alteracoes:
        local, metodo = casar_local(origem, por_id, por_numero)
        if local is None:
            pendencias.append({"origem_id": origem.get("id"),
                               "numero": origem.get("number"),
                               "status_origem": origem.get("status_id"),
                               "motivo": metodo})
            continue
        if local.get("deleted_at"):
            pendencias.append({"origem_id": origem.get("id"),
                               "numero": origem.get("number"),
                               "status_origem": origem.get("status_id"),
                               "motivo": "equipamento excluido no banco local"})
            continue
        novo = origem.get("status_id")
        if novo is None:
            pendencias.append({"origem_id": origem.get("id"),
                               "numero": origem.get("number"),
                               "status_origem": novo,
                               "motivo": "status_id nulo na origem"})
            continue
        if int(novo) not in status_validos:
            pendencias.append({"origem_id": origem.get("id"),
                               "numero": origem.get("number"),
                               "status_origem": novo,
                               "motivo": f"status {novo} inexistente no banco local"})
            continue
        if origem.get("deleted_at"):
            pendencias.append({"origem_id": origem.get("id"),
                               "numero": origem.get("number"),
                               "status_origem": novo,
                               "motivo": "equipamento excluido na origem"})
            continue
        atual = local.get("status_id")
        if atual == novo:
            continue
        updates.append({
            "origem_id": origem.get("id"),
            "local_id": local["id"],
            "numero": local.get("number"),
            "status_anterior": atual,
            "status_novo": novo,
            "metodo": metodo,
        })
    return updates, pendencias


def _carregar_locais(conn):
    rows = conn.execute(text(
        "SELECT `id`, `number`, `serial_number`, `status_id`, `deleted_at` FROM `equipments`"
    )).mappings().all()
    return [dict(row) for row in rows]


def _carregar_status(conn):
    return {int(row[0]) for row in conn.execute(text("SELECT `id` FROM `status`"))}


def _escrever_relatorio(caminho, updates, pendencias):
    import csv

    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    # Nunca sobescrever evidencia de uma execucao anterior: com updates, grava
    # tambem uma copia datada; sem updates, grava a revalidacao a parte.
    if caminho.exists():
        sufixo = "aplicado" if updates else "revalidacao"
        carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
        caminho = caminho.with_name(f"{caminho.stem}_{sufixo}_{carimbo}.csv")
    linhas = [
        {"origem_id": u["origem_id"], "local_id": u["local_id"], "numero": u["numero"],
         "status_anterior": u["status_anterior"], "status_novo": u["status_novo"],
         "acao": "atualizar", "motivo": u["metodo"]}
        for u in updates
    ] + [
        {"origem_id": p["origem_id"], "local_id": "", "numero": p["numero"],
         "status_anterior": p["status_origem"], "status_novo": "",
         "acao": "pendente", "motivo": p["motivo"]}
        for p in pendencias
    ]
    with caminho.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "origem_id", "local_id", "numero", "status_anterior", "status_novo",
            "acao", "motivo"])
        writer.writeheader()
        writer.writerows(linhas)
    return caminho


def executar(engine, caminho_json=JSON_PADRAO, inicio=INICIO_PADRAO, fim=FIM_PADRAO,
             saida=SAIDA_PADRAO, apply=False):
    ini, lim = date.fromisoformat(inicio), date.fromisoformat(fim)
    alteracoes = carregar_alteracoes(caminho_json, ini, lim)
    print(f"Equipamentos atualizados na origem entre {inicio} e {fim}: {len(alteracoes)}")

    with engine.connect() as conn:
        locais = _carregar_locais(conn)
        status_validos = _carregar_status(conn)

    updates, pendencias = planejar(alteracoes, locais, status_validos)
    print(f"  a atualizar: {len(updates)}   pendentes: {len(pendencias)}")
    for motivo, quantidade in Counter(p["motivo"] for p in pendencias).most_common():
        print(f"    pendente: {motivo} ({quantidade})")
    for origem, quantidade in Counter(
            f"{u['status_anterior']} -> {u['status_novo']}" for u in updates
    ).most_common():
        print(f"    {origem}: {quantidade}")

    if apply and updates:
        with engine.begin() as conn:
            for update in updates:
                conn.execute(text(
                    "UPDATE `equipments` SET `status_id`=:s WHERE `id`=:i"
                ), {"s": update["status_novo"], "i": update["local_id"]})
        print(f"✅ {len(updates)} equipamento(s) atualizado(s).")
    elif updates:
        print("🔍 Dry-run: nenhum registro alterado. Use --apply para gravar.")

    relatorio = _escrever_relatorio(saida, updates, pendencias)
    print(f"📄 Relatorio: {relatorio}")
    return {"alteracoes": len(alteracoes), "atualizados": len(updates),
            "pendentes": len(pendencias), "relatorio": str(relatorio)}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Atualiza equipments.status_id a partir do delta de producao.")
    parser.add_argument("--json", default=JSON_PADRAO)
    parser.add_argument("--inicio", default=INICIO_PADRAO)
    parser.add_argument("--fim", default=FIM_PADRAO,
                        help="limite exclusivo, como no importador do delta")
    parser.add_argument("--saida", default=SAIDA_PADRAO)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    from config.database import obter_engines
    engine, _ = obter_engines()
    try:
        executar(engine, caminho_json=args.json, inicio=args.inicio, fim=args.fim,
                 saida=args.saida, apply=args.apply)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
