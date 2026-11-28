#!/usr/bin/env python3
"""Fase 1 — aplica a regra do canonico e gera o mapa, o impacto e os conflitos.

Le `out/candidatos.csv` (Fase 0) e grava:

- `mapa_canonico.csv` (artefato versionado, consumido pelo `03_aplicar.py`);
- `out/impacto.csv`;
- `out/conflitos.csv`;
- `out/revisao.md`.

Nao escreve no banco. Le `product_items.quantity` / `transaction_items.quantity`
apenas para registrar o estoque antes/depois no impacto.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import bindparam, text

try:
    from . import comum
except ImportError:  # execucao direta do script
    import comum


def _sql_in(comando: str):
    return text(comando).bindparams(bindparam("ids", expanding=True))


def _chave_grupo(linha: dict, escopo: str) -> tuple:
    tombo = comum.normalizar_int(linha.get("number"))
    if escopo == comum.ESCOPO_GLOBAL:
        return (tombo,)
    return (tombo, comum.normalizar_int(linha.get("current_organization_id")))


def _carregar_aprovacoes(mapa: Path) -> dict[tuple, dict]:
    """Preserva revisao humana de execucoes anteriores.

    A chave e (orgao, tombo). O valor so e reaproveitado se a composicao do
    grupo nao mudou (mesmos ids). O canonico manual (escolhido na revisao)
    sobrevive a reexecucao; aprovacao de um canonico automatico so sobrevive
    se a regra continuar elegendo o mesmo id.
    """
    if not mapa.exists():
        return {}
    try:
        linhas = comum.ler_csv(mapa)
    except Exception:
        return {}

    grupos: dict[tuple, list[dict]] = {}
    for linha in linhas:
        chave = (comum.normalizar_int(linha.get("orgao_id")),
                 comum.normalizar_int(linha.get("tombo")))
        grupos.setdefault(chave, []).append(linha)

    aprovacoes: dict[tuple, dict] = {}
    for chave, linhas_grupo in grupos.items():
        ids = tuple(sorted(comum.normalizar_int(l.get("equipment_id")) or 0
                           for l in linhas_grupo))
        manual = any(comum.sim(l.get("canonical_manual")) for l in linhas_grupo)
        canonico = next(
            (comum.normalizar_int(l.get("canonical_id")) for l in linhas_grupo
             if comum.sim(l.get("canonical_manual"))), None)
        if canonico is None:
            canonico = comum.normalizar_int(linhas_grupo[0].get("canonical_id"))
        aprovacoes[chave] = {
            "ids": ids,
            "canonical_id": canonico,
            "canonical_manual": "S" if manual else "N",
            "revisado_por": next((l.get("revisado_por") for l in linhas_grupo
                                  if l.get("revisado_por")), ""),
            "aprovado": next((l.get("aprovado") for l in linhas_grupo
                              if l.get("aprovado")), ""),
            "justificativa": next((l.get("justificativa") for l in linhas_grupo
                                   if l.get("justificativa")), ""),
        }
    return aprovacoes


def _quantidades(engine, pi_ids: list[int], ti_ids: list[int]) -> tuple[dict, dict]:
    quantidades_pi: dict[int, float] = {}
    quantidades_ti: dict[int, float] = {}
    if engine is None:
        return quantidades_pi, quantidades_ti
    with engine.connect() as conn:
        if pi_ids:
            for pi_id, quantidade in conn.execute(
                _sql_in("SELECT id, quantity FROM product_items WHERE id IN :ids"),
                {"ids": tuple(pi_ids)},
            ).all():
                quantidades_pi[int(pi_id)] = float(quantidade)
        if ti_ids:
            for ti_id, quantidade in conn.execute(
                _sql_in("SELECT id, quantity FROM transaction_items WHERE id IN :ids"),
                {"ids": tuple(ti_ids)},
            ).all():
                quantidades_ti[int(ti_id)] = float(quantidade)
    return quantidades_pi, quantidades_ti


def _formatar_numero(valor) -> str:
    if valor is None:
        return ""
    if float(valor).is_integer():
        return str(int(valor))
    return f"{valor:.4f}".rstrip("0").rstrip(".")


def planejar(candidatos_csv: Path = comum.OUT / "candidatos.csv",
             mapa_saida: Path = comum.MAPA_PADRAO,
             escopo: str = comum.ESCOPO_ORGAO,
             regra: str = comum.REGRA_ATUAL,
             permitir_global: bool = False,
             aprovar_auto: bool = False,
             engine=None,
             pasta_saida: Path = comum.OUT,
             log=print) -> dict:
    if not Path(candidatos_csv).exists():
        raise FileNotFoundError(
            f"{candidatos_csv} nao existe; rode o 01_extrair.py primeiro.")
    if escopo not in (comum.ESCOPO_ORGAO, comum.ESCOPO_GLOBAL):
        raise ValueError(f"escopo invalido: {escopo}")
    if regra not in (comum.REGRA_ATUAL, comum.REGRA_HISTORICO):
        raise ValueError(f"regra invalida: {regra}")
    if escopo == comum.ESCOPO_GLOBAL and not permitir_global:
        raise ValueError("escopo global exige --permitir-global (nunca aplica).")

    candidatos = comum.ler_csv(candidatos_csv)
    if candidatos:
        escopo_arquivo = candidatos[0].get("escopo", escopo)
        if escopo_arquivo and escopo_arquivo != escopo:
            raise ValueError(
                f"candidatos.csv foi gerado no escopo {escopo_arquivo}, "
                f"nao em {escopo}; rode o 01_extrair.py com o escopo certo.")

    aprovacoes = _carregar_aprovacoes(Path(mapa_saida))

    grupos: dict[tuple, list[dict]] = {}
    for linha in candidatos:
        grupos.setdefault(_chave_grupo(linha, escopo), []).append(linha)

    chaves_ordenadas = sorted(
        grupos,
        key=lambda c: (c[1] if len(c) > 1 else 0, c[0] or 0,
                       min(comum.normalizar_int(l.get("id")) or 0
                           for l in grupos[c])),
    )

    mapa: list[dict] = []
    conflitos: list[dict] = []
    descartaveis: list[dict] = []
    por_motivo: dict[str, int] = {}
    por_acao = {"MANTER": 0, "DESCARTAR": 0, "MESCLAR": 0, "NAO_MEXER": 0}

    for grupo_id, chave in enumerate(chaves_ordenadas, start=1):
        linhas = grupos[chave]
        tombo = chave[0]
        orgao = chave[1] if len(chave) > 1 else ""
        motivos = comum.avaliar_travas(linhas)

        canonico = None
        decisor = ""
        canonico_manual = "N"
        ids_grupo = tuple(sorted(comum.normalizar_int(l.get("id")) or 0
                                 for l in linhas))
        anterior = aprovacoes.get((orgao, tombo))
        if escopo == comum.ESCOPO_GLOBAL:
            motivos = list(motivos) + ["ESCOPO_GLOBAL_SOMENTE_RELATORIO"]
        else:
            canonico, decisor = comum.escolher_canonico(linhas, regra)
            if (anterior and anterior["ids"] == ids_grupo
                    and anterior["canonical_manual"] == "S"
                    and anterior["canonical_id"] in ids_grupo):
                canonico = next(
                    l for l in linhas
                    if comum.normalizar_int(l.get("id")) == anterior["canonical_id"])
                decisor = "manual"
                canonico_manual = "S"
            if (canonico.get("deleted_at")
                    or comum.normalizar_int(canonico.get("status_id")) == 9):
                motivos = list(motivos) + ["CANONICO_INVALIDO"]

        bloqueado = bool(motivos)
        name_divergente = any(comum.sim(l.get("name_divergente_no_grupo"))
                              for l in linhas)
        exige_revisao = bloqueado or name_divergente

        anterior = aprovacoes.get((orgao, tombo))
        revisado_por, aprovado, justificativa = "", "", ""
        if anterior and anterior["ids"] == ids_grupo:
            canonico_atual = (comum.normalizar_int(canonico.get("id"))
                              if canonico else None)
            mesmo_canonico = (anterior["canonical_manual"] == "S"
                              or anterior["canonical_id"] == canonico_atual)
            if mesmo_canonico:
                revisado_por = anterior["revisado_por"]
                aprovado = anterior["aprovado"]
                justificativa = anterior["justificativa"]

        if (not revisado_por and not aprovado and aprovar_auto
                and not exige_revisao):
            revisado_por, aprovado = "auto", "S"

        for linha in linhas:
            equipamento_id = comum.normalizar_int(linha.get("id"))
            if bloqueado:
                acao = "NAO_MEXER"
                canonico_id = ""
            elif equipamento_id == comum.normalizar_int(canonico.get("id")):
                acao = "MANTER"
                canonico_id = equipamento_id
            else:
                acao = ("MESCLAR"
                        if (comum.normalizar_int(linha.get("refs_total_repointar")) or 0) > 0
                        else "DESCARTAR")
                canonico_id = comum.normalizar_int(canonico.get("id"))

            registro = {
                "grupo_id": grupo_id,
                "escopo": escopo,
                "tombo": tombo,
                "orgao_id": orgao,
                "equipment_id": equipamento_id,
                "canonical_id": canonico_id,
                "acao": acao,
                "regra_decisora": decisor if not bloqueado else "",
                "status_id": comum.normalizar_int(linha.get("status_id")),
                "tem_parquet": linha.get("tem_parquet", ""),
                "qtd_movimentos": comum.normalizar_int(linha.get("qtd_movimentos")) or 0,
                "ultimo_movimento": linha.get("ultimo_movimento", ""),
                "product_item_id": comum.normalizar_int(linha.get("product_item_id")),
                "transaction_item_id": comum.normalizar_int(linha.get("transaction_item_id")),
                "name": linha.get("name", ""),
                "revisado_por": revisado_por,
                "aprovado": aprovado,
                "motivo": ", ".join(motivos),
                "exige_revisao": "S" if exige_revisao else "N",
                "justificativa": justificativa,
                "canonical_manual": canonico_manual,
            }
            mapa.append(registro)
            por_acao[acao] += 1
            if acao in ("DESCARTAR", "MESCLAR"):
                descartaveis.append(registro)

        if bloqueado:
            for motivo in motivos:
                por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
            conflitos.append({
                "grupo_id": grupo_id,
                "tombo": tombo,
                "orgao_id": orgao,
                "motivos": ", ".join(motivos),
                "equipment_ids": ", ".join(str(l.get("id")) for l in linhas),
                "canonical_id": comum.normalizar_int(canonico.get("id")) if canonico else "",
                "name_divergente": "S" if name_divergente else "N",
                "exige_revisao": "S",
            })

    comum.escrever_csv(mapa_saida, comum.CABECALHO_MAPA, mapa)
    refs_por_id = {comum.normalizar_int(l.get("id")): l for l in candidatos}
    impacto = _escrever_impacto(descartaveis, engine, pasta_saida, refs_por_id)
    comum.escrever_csv(
        pasta_saida / "conflitos.csv",
        ["grupo_id", "tombo", "orgao_id", "motivos", "equipment_ids",
         "canonical_id", "name_divergente", "exige_revisao"],
        conflitos,
    )

    hash_mapa = comum.sha256_arquivo(mapa_saida)
    (pasta_saida / "mapa_canonico.sha256").write_text(
        f"{hash_mapa}  {Path(mapa_saida).name}\n", encoding="utf-8")

    stats = {
        "escopo": escopo,
        "regra": regra,
        "grupos": len(grupos),
        "linhas": len(mapa),
        "grupos_travados": len(conflitos),
        "grupos_descartaveis": len(chaves_ordenadas) - len(conflitos),
        "linhas_descartaveis": len(descartaveis),
        "por_acao": por_acao,
        "por_motivo": por_motivo,
        "aprovados": sum(1 for l in mapa if comum.sim(l.get("aprovado"))),
        "hash_mapa": hash_mapa,
        "impacto": impacto,
    }
    _escrever_revisao(pasta_saida / "revisao.md", stats, mapa, conflitos)
    return stats


def _escrever_impacto(descartaveis: list[dict], engine, pasta_saida: Path,
                      refs_por_id: dict[int, dict] | None = None) -> dict:
    colunas = ["tipo", "chave", "campo", "valor_antes", "delta",
               "valor_depois", "linhas", "observacao"]
    linhas_saida: list[dict] = []
    refs_por_id = refs_por_id or {}

    def _refs(linha: dict) -> dict:
        """O mapa nao carrega refs_*; elas vivem no candidato de cada id."""
        candidato = refs_por_id.get(comum.normalizar_int(linha.get("equipment_id")))
        return candidato or linha

    for tabela, coluna, _ in comum.TABELAS_REF:
        campo = f"refs_{tabela}"
        if tabela == "equipment_history":
            total = sum(max((comum.normalizar_int(_refs(l).get(campo)) or 0)
                            - (comum.normalizar_int(_refs(l).get("refs_equipment_history_entrada")) or 0), 0)
                        for l in descartaveis)
        else:
            total = sum(comum.normalizar_int(_refs(l).get(campo)) or 0
                        for l in descartaveis)
        equipamentos = sum(
            1 for l in descartaveis
            if (comum.normalizar_int(_refs(l).get("refs_total_repointar")) or 0) > 0
            and tabela != "equipment_history")
        if total:
            linhas_saida.append({
                "tipo": "REF",
                "chave": f"{tabela}.{coluna}",
                "campo": "",
                "valor_antes": "",
                "delta": total,
                "valor_depois": "",
                "linhas": equipamentos if tabela != "equipment_history" else "",
                "observacao": "repointar para o canonico",
            })

    entradas = sum(comum.normalizar_int(_refs(l).get("refs_equipment_history_entrada")) or 0
                   for l in descartaveis)
    if entradas:
        linhas_saida.append({
            "tipo": "HIST",
            "chave": "equipment_history.entrance",
            "campo": "deleted_at",
            "valor_antes": "",
            "delta": entradas,
            "valor_depois": "",
            "linhas": entradas,
            "observacao": "ocultar a linha de entrada da copia descartada",
        })

    # Estoque: k = quantidade de copias descartadas com o mesmo pi/ti.
    k_por_pi: dict[int, int] = {}
    k_por_ti: dict[int, int] = {}
    for linha in descartaveis:
        pi_id = comum.normalizar_int(linha.get("product_item_id"))
        ti_id = comum.normalizar_int(linha.get("transaction_item_id"))
        if pi_id:
            k_por_pi[pi_id] = k_por_pi.get(pi_id, 0) + 1
        if ti_id:
            k_por_ti[ti_id] = k_por_ti.get(ti_id, 0) + 1

    quantidades_pi, quantidades_ti = _quantidades(
        engine, sorted(k_por_pi), sorted(k_por_ti))

    for pi_id, k in sorted(k_por_pi.items()):
        antes = quantidades_pi.get(pi_id)
        observacao = ""
        depois = None
        if antes is not None:
            depois = antes - k
            if antes < k:
                observacao = "ESTOQUE_INSUFICIENTE: decremento nao sera aplicado"
        linhas_saida.append({
            "tipo": "ESTOQUE_PI",
            "chave": pi_id,
            "campo": "quantity",
            "valor_antes": _formatar_numero(antes),
            "delta": -k,
            "valor_depois": _formatar_numero(depois),
            "linhas": k,
            "observacao": observacao,
        })

    for ti_id, k in sorted(k_por_ti.items()):
        antes = quantidades_ti.get(ti_id)
        observacao = ""
        depois = None
        if antes is not None:
            depois = antes - k
            if antes < k:
                observacao = "ESTOQUE_INSUFICIENTE: decremento nao sera aplicado"
        linhas_saida.append({
            "tipo": "ESTOQUE_TI",
            "chave": ti_id,
            "campo": "quantity",
            "valor_antes": _formatar_numero(antes),
            "delta": -k,
            "valor_depois": _formatar_numero(depois),
            "linhas": k,
            "observacao": observacao,
        })

    comum.escrever_csv(pasta_saida / "impacto.csv", colunas, linhas_saida)
    return {
        "refs": sum(1 for l in linhas_saida if l["tipo"] == "REF"),
        "pis": len(k_por_pi),
        "tis": len(k_por_ti),
        "estoque_insuficiente": sum(
            1 for l in linhas_saida if l["observacao"].startswith("ESTOQUE_INSUFICIENTE")),
    }


def _escrever_revisao(caminho: Path, stats: dict, mapa: list[dict],
                      conflitos: list[dict]) -> None:
    linhas = [
        "# Plano de ajuste das duplicatas de equipamentos",
        "",
        f"Gerado em {comum.agora_iso()} — escopo `{stats['escopo']}`, "
        f"regra `{stats['regra']}`.",
        "",
        "## Resumo",
        "",
        "| Metrica | Valor |",
        "|---|---:|",
        f"| Grupos | {stats['grupos']} |",
        f"| Linhas | {stats['linhas']} |",
        f"| Grupos travados (`NAO_MEXER`) | {stats['grupos_travados']} |",
        f"| Grupos descartaveis | {stats['grupos_descartaveis']} |",
        f"| Linhas a descartar/mesclar | {stats['linhas_descartaveis']} |",
        f"| Linhas `MANTER` | {stats['por_acao']['MANTER']} |",
        f"| Linhas `DESCARTAR` | {stats['por_acao']['DESCARTAR']} |",
        f"| Linhas `MESCLAR` | {stats['por_acao']['MESCLAR']} |",
        f"| Linhas `NAO_MEXER` | {stats['por_acao']['NAO_MEXER']} |",
        f"| Linhas aprovadas | {stats['aprovados']} |",
        "",
        f"Hash do `mapa_canonico.csv`: `{stats['hash_mapa']}`",
        "",
        "## Travas por motivo",
        "",
        "| Motivo | Grupos | Descricao |",
        "|---|---:|---|",
    ]
    for motivo, total in sorted(stats["por_motivo"].items(), key=lambda par: -par[1]):
        descricao = comum.MOTIVOS_TRADUCAO.get(motivo, motivo)
        linhas.append(f"| {motivo} | {total} | {descricao} |")
    if not stats["por_motivo"]:
        linhas.append("| (nenhum) | 0 | — |")

    linhas += [
        "",
        "## Impacto",
        "",
        f"- Tabelas referenciadas: {stats['impacto']['refs']}",
        f"- `product_items` a decrementar: {stats['impacto']['pis']}",
        f"- `transaction_items` a decrementar: {stats['impacto']['tis']}",
        f"- Estoque insuficiente (nao decrementa): {stats['impacto']['estoque_insuficiente']}",
        "",
        "## Grupos com `name` divergente",
        "",
        "| grupo_id | tombo | orgao_id | ids | acao |",
        "|---:|---:|---:|---|---|",
    ]
    grupos_nome = {}
    for linha in mapa:
        grupos_nome.setdefault(linha["grupo_id"], []).append(linha)
    linhas_nome = []
    for grupo_id, registros in sorted(grupos_nome.items()):
        nomes = {str(r.get("name", "")).strip() for r in registros}
        if len(nomes) > 1:
            primeiro = registros[0]
            linhas_nome.append(
                f"| {grupo_id} | {primeiro.get('tombo')} | {primeiro.get('orgao_id')} | "
                f"{', '.join(str(r.get('equipment_id')) for r in registros)} | "
                f"{'/'.join(sorted({str(r.get('acao')) for r in registros}))} |")
    if linhas_nome:
        linhas += linhas_nome
    else:
        linhas.append("| — | — | — | — | — |")

    linhas += [
        "",
        "## Grupos travados",
        "",
        "| grupo_id | tombo | orgao_id | ids | motivos |",
        "|---:|---:|---:|---|---|",
    ]
    if conflitos:
        for conflito in conflitos:
            linhas.append(
                f"| {conflito['grupo_id']} | {conflito['tombo']} | {conflito['orgao_id']} | "
                f"{conflito['equipment_ids']} | {conflito['motivos']} |")
    else:
        linhas.append("| — | — | — | — | — |")

    linhas += [
        "",
        "## Revisao humana (Fase 2)",
        "",
        "1. Revise 100% dos grupos travados (`conflitos.csv`) — eles nao serao tocados.",
        "2. Revise 100% dos grupos com `name` divergente da tabela acima.",
        "3. Amostre 20 grupos com `status_id` divergente.",
        "4. Para aprovar, preencha `revisado_por` e `aprovado = S` nas linhas do",
        "   `mapa_canonico.csv` (o `02_planejar.py` preserva esses campos na",
        "   reexecucao, desde que a composicao do grupo nao mude).",
        "",
        "`03_aplicar.py` so age em linhas com `aprovado = S` e acao",
        "`DESCARTAR`/`MESCLAR`; `MANTER` e `NAO_MEXER` sao ignoradas.",
        "",
    ]
    Path(caminho).write_text("\n".join(linhas) + "\n", encoding="utf-8")
