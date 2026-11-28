#!/usr/bin/env python3
"""Fase 3 — aplicacao do mapa canonico (uma transacao por grupo).

Consome o `mapa_canonico.csv` da Fase 1 e executa, por grupo aprovado, a ordem
obrigatoria do plano (§9):

1. guardas de entrada (grupo ainda ativo e igual ao mapa);
2. repoint das dependentes para o canonico;
3. ocultacao da linha de entrada do historico da copia (§9.3);
4. soft delete com `rowcount = 1` como trava de idempotencia (§9.6);
5. decremento de estoque apenas das copias efetivamente descartadas (§9.5);
6. consolidacao do canonico (ultimo movimento + marcacao em `details`);
7. auditoria em `duplicatas_equipamentos_ajuste` e em `auditoria.csv`.

`--apply` e obrigatorio para escrever; o default e dry-run. `reverter()` le o
`auditoria.csv` de um lote e desfaz as escritas registradas.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import bindparam, text

try:
    from . import comum
except ImportError:  # execucao direta do script
    import comum


ACOES_DESCARTE = ("DESCARTAR", "MESCLAR")

CABECALHO_AUDITORIA = [
    "batch_id", "grupo_id", "tombo", "orgao_id", "canonical_id", "discarded_id",
    "acao", "regra", "status", "conflito", "observacao",
    "pi_id", "quantity_pi_antes", "quantity_pi_depois",
    "ti_id", "quantity_ti_antes", "quantity_ti_depois",
    "entrada_ocultada", "refs", "aplicado_em",
]

CABECALHO_LOTE = [
    "batch_id", "grupo_id", "tombo", "orgao_id", "canonical_id",
    "descartados", "refs_total", "pi_id", "estoque_pi", "ti_id", "estoque_ti",
    "status", "observacao",
]

CABECALHO_SKIPS = ["grupo_id", "tombo", "orgao_id", "status", "motivo"]

DDL_AUDITORIA = """
CREATE TABLE IF NOT EXISTS duplicatas_equipamentos_ajuste (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  batch_id      VARCHAR(64)  NOT NULL,
  grupo_id      INT          NOT NULL,
  tombo         INT          NULL,
  orgao_id      BIGINT UNSIGNED NOT NULL,
  canonical_id  BIGINT UNSIGNED NOT NULL,
  discarded_id  BIGINT UNSIGNED NOT NULL,
  acao          VARCHAR(20)  NOT NULL,
  regra         VARCHAR(40)  NULL,
  refs          JSON         NULL,
  quantity_pi_antes   DECIMAL(18,4) NULL,
  quantity_pi_depois  DECIMAL(18,4) NULL,
  quantity_ti_antes   DECIMAL(18,4) NULL,
  quantity_ti_depois  DECIMAL(18,4) NULL,
  aplicado_em   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revertido_em  TIMESTAMP    NULL,
  UNIQUE KEY uk_batch_discarded (batch_id, discarded_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class GrupoAbortado(Exception):
    """Interrompe a transacao do grupo e vira um SKIP no relatorio."""

    def __init__(self, status: str, motivo: str, detalhe: str = ""):
        super().__init__(f"{status}: {motivo}")
        self.status = status
        self.motivo = motivo
        self.detalhe = detalhe


def _sql_in(comando: str):
    return text(comando).bindparams(bindparam("ids", expanding=True))


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _numero(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float):
        if valor.is_integer():
            return str(int(valor))
        return f"{valor:.4f}".rstrip("0").rstrip(".")
    return str(valor)


def gerar_batch_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def garantir_tabela(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(DDL_AUDITORIA))


# ---------------------------------------------------------------------------
# Leitura e agrupamento do mapa
# ---------------------------------------------------------------------------

def _carregar_mapa(mapa_csv: Path) -> list[dict]:
    caminho = Path(mapa_csv)
    if not caminho.exists():
        raise FileNotFoundError(f"{caminho} nao existe; rode o 02_planejar.py antes.")
    registros = comum.ler_csv(caminho)
    if not registros:
        raise ValueError(f"{caminho} esta vazio.")
    for linha in registros:
        linha["equipment_id"] = comum.normalizar_int(linha.get("equipment_id"))
        linha["canonical_id"] = comum.normalizar_int(linha.get("canonical_id"))
        linha["grupo_id"] = comum.normalizar_int(linha.get("grupo_id"))
        linha["tombo"] = comum.normalizar_int(linha.get("tombo"))
        linha["orgao_id"] = comum.normalizar_int(linha.get("orgao_id"))
        linha["product_item_id"] = comum.normalizar_int(linha.get("product_item_id"))
        linha["transaction_item_id"] = comum.normalizar_int(
            linha.get("transaction_item_id"))
    return registros


def _agrupar(registros: list[dict]) -> dict[int, list[dict]]:
    grupos: dict[int, list[dict]] = {}
    for linha in registros:
        grupos.setdefault(linha["grupo_id"], []).append(linha)
    return grupos


def _selecionar(grupos: dict[int, list[dict]], somente_grupos, limite):
    chaves = sorted(grupos)
    if somente_grupos:
        alvo = {int(g) for g in somente_grupos}
        chaves = [c for c in chaves if c in alvo]
    if limite:
        chaves = chaves[: int(limite)]
    return chaves


def _conferir_parquet(registros: list[dict]) -> None:
    """Plano §8: nenhum id citado em Parquet pode ser descartado."""
    risco = [
        linha for linha in registros
        if linha.get("acao") in ACOES_DESCARTE
        and comum.sim(linha.get("tem_parquet"))
    ]
    if risco:
        ids = ", ".join(str(l["equipment_id"]) for l in risco)
        raise RuntimeError(
            "ABORTADO: o mapa tenta descartar ids citados nos Parquets "
            f"({ids}). Corrija o mapa antes de aplicar.")


# ---------------------------------------------------------------------------
# Guardas (§9.1)
# ---------------------------------------------------------------------------

def _ler_equipamentos(conn, ids: set[int]) -> dict[int, dict]:
    if not ids:
        return {}
    linhas = conn.execute(
        _sql_in(
            "SELECT id, number, current_organization_id, status_id, deleted_at, "
            "       product_item_id, transaction_item_id "
            "FROM equipments WHERE id IN :ids"
        ),
        {"ids": tuple(sorted(ids))},
    ).mappings().all()
    return {int(l["id"]): dict(l) for l in linhas}


def _validar_grupo(linhas_db: dict[int, dict], canonico: int,
                   disc_ids: list[int], tombo, orgao) -> tuple[list[str], bool]:
    problemas: list[str] = []
    ja_aplicado = False

    linha_canon = linhas_db.get(canonico)
    if linha_canon is None:
        problemas.append(f"canonico {canonico} ausente")
    else:
        if linha_canon["deleted_at"] is not None:
            problemas.append(f"canonico {canonico} descartado")
        if comum.normalizar_int(linha_canon["status_id"]) == 9:
            problemas.append(f"canonico {canonico} baixado")
        if comum.normalizar_int(linha_canon["number"]) != tombo:
            problemas.append(f"canonico {canonico} com number divergente")
        if comum.normalizar_int(linha_canon["current_organization_id"]) != orgao:
            problemas.append(f"canonico {canonico} com orgao divergente")

    for equipamento_id in disc_ids:
        linha = linhas_db.get(equipamento_id)
        if linha is None:
            problemas.append(f"copia {equipamento_id} ausente")
            continue
        if linha["deleted_at"] is not None:
            ja_aplicado = True
            continue
        if comum.normalizar_int(linha["number"]) != tombo:
            problemas.append(f"copia {equipamento_id} com number divergente")
        if comum.normalizar_int(linha["current_organization_id"]) != orgao:
            problemas.append(f"copia {equipamento_id} com orgao divergente")
    return problemas, ja_aplicado


# ---------------------------------------------------------------------------
# Escritas (§9.2 a §9.6)
# ---------------------------------------------------------------------------

def _contar_refs(conn, exclu: int) -> dict:
    contagem: dict[str, int] = {}
    for tabela, coluna, _ in comum.TABELAS_REF:
        filtro = ""
        params = {"exclu": exclu}
        if tabela == "equipment_history":
            filtro = " AND (reason IS NULL OR reason <> :entrada)"
            params["entrada"] = comum.REASON_ENTRADA
        total = conn.execute(
            text(f"SELECT COUNT(*) FROM {tabela} WHERE {coluna} = :exclu{filtro}"),
            params,
        ).scalar()
        contagem[tabela] = int(total or 0)
    total_rel = conn.execute(
        text(
            "SELECT COUNT(*) FROM related_equipments "
            "WHERE (new_equipment_id = :e AND new_equipment_id <> old_equipment_id) "
            "   OR (old_equipment_id = :e AND old_equipment_id <> new_equipment_id)"
        ),
        {"e": exclu},
    ).scalar()
    contagem["related_equipments"] = int(total_rel or 0)
    return contagem


def _repointar(conn, exclu: int, canonico: int) -> dict:
    info: dict = {}
    for tabela, coluna, _ in comum.TABELAS_REF:
        filtro = ""
        params = {"exclu": exclu}
        if tabela == "equipment_history":
            filtro = " AND (reason IS NULL OR reason <> :entrada)"
            params["entrada"] = comum.REASON_ENTRADA
        ids = [
            int(valor) for valor in conn.execute(
                text(f"SELECT id FROM {tabela} WHERE {coluna} = :exclu{filtro}"),
                params,
            ).scalars()
        ]
        if ids:
            conn.execute(
                _sql_in(f"UPDATE {tabela} SET equipment_id = :canon WHERE id IN :ids"),
                {"canon": canonico, "ids": tuple(ids)},
            )
        info[tabela] = ids

    repointados, removidos = _repointar_relacionados(conn, exclu, canonico)
    info["related_equipments"] = repointados
    info["related_equipments_removidos"] = removidos
    return info


def _repointar_relacionados(conn, exclu: int, canonico: int):
    """related_equipments nao tem id e tem unique (new, old) — trata conflito."""
    pares = conn.execute(
        text(
            "SELECT new_equipment_id, old_equipment_id FROM related_equipments "
            "WHERE new_equipment_id = :e OR old_equipment_id = :e"
        ),
        {"e": exclu},
    ).all()

    repointados: list[dict] = []
    removidos: list[dict] = []
    for novo, antigo in pares:
        novo, antigo = int(novo), int(antigo)
        alvo_novo = canonico if novo == exclu else novo
        alvo_antigo = canonico if antigo == exclu else antigo
        if (alvo_novo, alvo_antigo) == (novo, antigo):
            continue
        chave_de = {"new": novo, "old": antigo}
        if alvo_novo == alvo_antigo:
            # repoint criaria auto-relacao: a linha perde o sentido.
            conn.execute(
                text(
                    "DELETE FROM related_equipments "
                    "WHERE new_equipment_id = :n AND old_equipment_id = :o"
                ),
                {"n": novo, "o": antigo},
            )
            continue
        chave_para = {"new": alvo_novo, "old": alvo_antigo}
        existe = conn.execute(
            text(
                "SELECT 1 FROM related_equipments "
                "WHERE new_equipment_id = :n AND old_equipment_id = :o"
            ),
            chave_para,
        ).first()
        if existe:
            conn.execute(
                text(
                    "DELETE FROM related_equipments "
                    "WHERE new_equipment_id = :n AND old_equipment_id = :o"
                ),
                {"n": novo, "o": antigo},
            )
            removidos.append(chave_de)
        else:
            conn.execute(
                text(
                    "UPDATE related_equipments SET new_equipment_id = :nn, "
                    "       old_equipment_id = :no "
                    "WHERE new_equipment_id = :n AND old_equipment_id = :o"
                ),
                {**chave_para, "n": novo, "o": antigo},
            )
            repointados.append({"de": chave_de, "para": chave_para})
    return repointados, removidos


def _ocultar_entrada(conn, exclu: int) -> int:
    resultado = conn.execute(
        text(
            "UPDATE equipment_history SET deleted_at = NOW() "
            "WHERE equipment_id = :exclu AND reason = :entrada "
            "  AND deleted_at IS NULL"
        ),
        {"exclu": exclu, "entrada": comum.REASON_ENTRADA},
    )
    return int(resultado.rowcount or 0)


def _soft_delete(conn, exclu: int) -> int:
    resultado = conn.execute(
        text(
            "UPDATE equipments SET deleted_at = NOW(), updated_at = NOW() "
            "WHERE id = :exclu AND deleted_at IS NULL"
        ),
        {"exclu": exclu},
    )
    return int(resultado.rowcount or 0)


def _decrementar(conn, tabela: str, chave: int, k: int) -> dict:
    antes = conn.execute(
        text(f"SELECT quantity FROM {tabela} WHERE id = :id"), {"id": chave}
    ).scalar()
    if antes is None:
        return {"antes": None, "depois": None, "aplicado": False,
                "conflito": "REGISTRO_AUSENTE"}
    antes = float(antes)
    if antes < k:
        return {"antes": antes, "depois": antes, "aplicado": False,
                "conflito": "ESTOQUE_INSUFICIENTE"}
    conn.execute(
        text(
            f"UPDATE {tabela} SET quantity = GREATEST(quantity - :k, 0), "
            "       updated_at = NOW() WHERE id = :id"
        ),
        {"k": k, "id": chave},
    )
    return {"antes": antes, "depois": antes - k, "aplicado": True, "conflito": ""}


def _consolidar_canonico(conn, canonico: int, descartados: list[int]) -> None:
    ultimo_item = conn.execute(
        text(
            "SELECT mi.id FROM movement_items mi "
            "JOIN movements m ON m.id = mi.movement_id AND m.deleted_at IS NULL "
            "WHERE mi.deleted_at IS NULL AND mi.equipment_id = :canon "
            "ORDER BY m.movement_date DESC, mi.id DESC LIMIT 1"
        ),
        {"canon": canonico},
    ).scalar()

    marcador = "".join(
        f"[DUPLICATA MESCLADA: {exclu} -> {canonico} em {datetime.now():%Y-%m-%d}] "
        for exclu in descartados
    )

    conn.execute(
        text(
            "UPDATE equipments SET "
            "  last_movement_item_customer_id = COALESCE(:item, "
            "      last_movement_item_customer_id), "
            "  details = CONCAT(COALESCE(details, ''), :marcador), "
            "  updated_at = NOW() "
            "WHERE id = :canon"
        ),
        {
            "item": int(ultimo_item) if ultimo_item else None,
            "marcador": marcador,
            "canon": canonico,
        },
    )


def _auditar(conn, registro: dict) -> None:
    conn.execute(
        text(
            "INSERT INTO duplicatas_equipamentos_ajuste "
            "(batch_id, grupo_id, tombo, orgao_id, canonical_id, discarded_id, "
            " acao, regra, refs, quantity_pi_antes, quantity_pi_depois, "
            " quantity_ti_antes, quantity_ti_depois) "
            "VALUES (:batch_id, :grupo_id, :tombo, :orgao_id, :canonical_id, "
            "        :discarded_id, :acao, :regra, :refs, :qpi_antes, "
            "        :qpi_depois, :qti_antes, :qti_depois) "
            "ON DUPLICATE KEY UPDATE aplicado_em = CURRENT_TIMESTAMP, "
            "                        revertido_em = NULL"
        ),
        {
            "batch_id": registro["batch_id"],
            "grupo_id": registro["grupo_id"],
            "tombo": registro["tombo"],
            "orgao_id": registro["orgao_id"],
            "canonical_id": registro["canonical_id"],
            "discarded_id": registro["discarded_id"],
            "acao": registro["acao"],
            "regra": registro.get("regra", ""),
            "refs": registro["refs"],
            "qpi_antes": registro.get("quantity_pi_antes"),
            "qpi_depois": registro.get("quantity_pi_depois"),
            "qti_antes": registro.get("quantity_ti_antes"),
            "qti_depois": registro.get("quantity_ti_depois"),
        },
    )


# ---------------------------------------------------------------------------
# Execucao de um grupo
# ---------------------------------------------------------------------------

def _separar(registros: list[dict]) -> tuple[list[dict], list[dict]]:
    manter = [l for l in registros if l.get("acao") == "MANTER"]
    disc = [l for l in registros if l.get("acao") in ACOES_DESCARTE]
    return manter, disc


def _pre_checagem_mapa(registros: list[dict]) -> str:
    """Motivo de pular o grupo antes de abrir transacao (vazio = ok)."""
    if any(l.get("acao") == "NAO_MEXER" for l in registros):
        return "GRUPO_TRAVADO"
    if registros[0].get("tombo") is None or registros[0].get("orgao_id") is None:
        return "GRUPO_SEM_CHAVE"
    manter, disc = _separar(registros)
    if len(manter) != 1:
        return "MANTER_INCONSISTENTE"
    if not disc:
        return "SEM_DESCARTE"
    if not all(comum.sim(l.get("aprovado")) for l in manter + disc):
        return "NAO_APROVADO"
    canonico = manter[0]["equipment_id"]
    if any(l.get("canonical_id") != canonico for l in disc):
        return "CANONICO_INCONSISTENTE"
    return ""


def _processar_grupo(conn, registros: list[dict], batch_id: str, dry_run: bool):
    """Devolve (status, auditoria, resumo) sem nunca misturar grupos."""
    motivo = _pre_checagem_mapa(registros)
    if motivo:
        return motivo, [], {"grupo_id": registros[0]["grupo_id"], "motivo": motivo}

    manter, disc = _separar(registros)
    canonico = int(manter[0]["equipment_id"])
    disc_ids = [int(l["equipment_id"]) for l in disc]
    tombo = registros[0]["tombo"]
    orgao = registros[0]["orgao_id"]

    linhas_db = _ler_equipamentos(conn, {canonico, *disc_ids})
    problemas, ja_aplicado = _validar_grupo(
        linhas_db, canonico, disc_ids, tombo, orgao)
    if ja_aplicado:
        return "SKIP_JA_APLICADO", [], {
            "grupo_id": registros[0]["grupo_id"], "motivo": "copias ja descartadas"}
    if problemas:
        return "SKIP_DIVERGENCIA", [], {
            "grupo_id": registros[0]["grupo_id"], "motivo": "; ".join(problemas)}

    k_por_pi: dict[int, int] = {}
    k_por_ti: dict[int, int] = {}
    for linha in disc:
        if linha.get("product_item_id"):
            k_por_pi[linha["product_item_id"]] = \
                k_por_pi.get(linha["product_item_id"], 0) + 1
        if linha.get("transaction_item_id"):
            k_por_ti[linha["transaction_item_id"]] = \
                k_por_ti.get(linha["transaction_item_id"], 0) + 1

    if dry_run:
        refs_por_disc = {d: _contar_refs(conn, d) for d in disc_ids}
        return "DRY_RUN", [], {
            "batch_id": batch_id,
            "grupo_id": registros[0]["grupo_id"],
            "tombo": tombo,
            "orgao_id": orgao,
            "canonical_id": canonico,
            "descartados": ",".join(str(d) for d in disc_ids),
            "refs_total": sum(sum(v.values()) for v in refs_por_disc.values()),
            "pi_id": ",".join(str(k) for k in sorted(k_por_pi)) or "",
            "estoque_pi": _previsto_estoque(conn, "product_items", k_por_pi),
            "ti_id": ",".join(str(k) for k in sorted(k_por_ti)) or "",
            "estoque_ti": _previsto_estoque(conn, "transaction_items", k_por_ti),
            "status": "DRY_RUN",
            "observacao": "",
        }

    refs_por_disc: dict[int, dict] = {}
    for exclu in disc_ids:
        refs_por_disc[exclu] = _repointar(conn, exclu, canonico)
        refs_por_disc[exclu]["entrada_ocultada"] = _ocultar_entrada(conn, exclu)

    for exclu in disc_ids:
        if _soft_delete(conn, exclu) != 1:
            raise GrupoAbortado(
                "SKIP_JA_APLICADO",
                f"copia {exclu} ja estava descartada no momento do soft delete")

    estoque_pi = {
        pi: _decrementar(conn, "product_items", pi, k)
        for pi, k in sorted(k_por_pi.items())
    }
    estoque_ti = {
        ti: _decrementar(conn, "transaction_items", ti, k)
        for ti, k in sorted(k_por_ti.items())
    }

    _consolidar_canonico(conn, canonico, disc_ids)

    auditoria = []
    for linha in disc:
        exclu = int(linha["equipment_id"])
        estoque_pi_item = estoque_pi.get(linha.get("product_item_id"), {})
        estoque_ti_item = estoque_ti.get(linha.get("transaction_item_id"), {})
        conflito = bool(estoque_pi_item.get("conflito")
                        or estoque_ti_item.get("conflito"))
        refs = refs_por_disc.get(exclu, {})
        registro = {
            "batch_id": batch_id,
            "grupo_id": linha["grupo_id"],
            "tombo": tombo,
            "orgao_id": orgao,
            "canonical_id": canonico,
            "discarded_id": exclu,
            "acao": linha.get("acao"),
            "regra": linha.get("regra_decisora", ""),
            "status": "APLICADO",
            "conflito": "S" if conflito else "N",
            "observacao": estoque_pi_item.get("conflito", "")
            or estoque_ti_item.get("conflito", ""),
            "pi_id": linha.get("product_item_id") or "",
            "quantity_pi_antes": _numero(estoque_pi_item.get("antes")),
            "quantity_pi_depois": _numero(estoque_pi_item.get("depois")),
            "ti_id": linha.get("transaction_item_id") or "",
            "quantity_ti_antes": _numero(estoque_ti_item.get("antes")),
            "quantity_ti_depois": _numero(estoque_ti_item.get("depois")),
            "entrada_ocultada": refs.get("entrada_ocultada", 0),
            "refs": json.dumps(refs, ensure_ascii=False),
            "aplicado_em": _agora(),
        }
        _auditar(conn, registro)
        auditoria.append(registro)

    resumo = {
        "batch_id": batch_id,
        "grupo_id": registros[0]["grupo_id"],
        "tombo": tombo,
        "orgao_id": orgao,
        "canonical_id": canonico,
        "descartados": ",".join(str(d) for d in disc_ids),
        "refs_total": sum(
            sum(len(v) for v in refs.values() if isinstance(v, list))
            for refs in refs_por_disc.values()),
        "pi_id": ",".join(str(k) for k in sorted(k_por_pi)) or "",
        "estoque_pi": "; ".join(
            f"{k}:{_numero(v.get('antes'))}->{_numero(v.get('depois'))}"
            for k, v in estoque_pi.items()),
        "ti_id": ",".join(str(k) for k in sorted(k_por_ti)) or "",
        "estoque_ti": "; ".join(
            f"{k}:{_numero(v.get('antes'))}->{_numero(v.get('depois'))}"
            for k, v in estoque_ti.items()),
        "status": "APLICADO",
        "observacao": "; ".join(
            f"{k}:{v['conflito']}" for k, v in {**estoque_pi, **estoque_ti}.items()
            if v.get("conflito")),
    }
    return "APLICADO", auditoria, resumo


def _previsto_estoque(conn, tabela: str, k_por_chave: dict[int, int]) -> str:
    partes = []
    for chave, k in sorted(k_por_chave.items()):
        antes = conn.execute(
            text(f"SELECT quantity FROM {tabela} WHERE id = :id"), {"id": chave}
        ).scalar()
        if antes is None:
            partes.append(f"{chave}:ausente")
        else:
            depois = max(float(antes) - k, 0)
            marca = "!" if float(antes) < k else ""
            partes.append(f"{chave}:{_numero(float(antes))}->{_numero(depois)}{marca}")
    return "; ".join(partes)


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------

def aplicar(engine, mapa_csv: Path = comum.MAPA_PADRAO, batch_id: str | None = None,
            somente_grupos=None, limite: int | None = None, dry_run: bool = True,
            pasta_saida: Path = comum.OUT, log=print) -> dict:
    registros = _carregar_mapa(mapa_csv)
    escopos = {str(l.get("escopo") or comum.ESCOPO_ORGAO) for l in registros}
    if comum.ESCOPO_GLOBAL in escopos:
        raise RuntimeError(
            "ABORTADO: mapa no escopo global e somente relatorio (nunca aplica).")
    _conferir_parquet(registros)

    batch_id = batch_id or gerar_batch_id()
    grupos = _agrupar(registros)
    chaves = _selecionar(grupos, somente_grupos, limite)

    pasta_run = Path(pasta_saida) / f"run_{batch_id}"
    pasta_run.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        garantir_tabela(engine)

    auditoria: list[dict] = []
    lote: list[dict] = []
    skips: list[dict] = []
    por_status: dict[str, int] = {}

    log(f"{'DRY-RUN' if dry_run else 'APLICACAO'} do lote {batch_id}: "
        f"{len(chaves)} grupo(s) de {len(grupos)}")

    for grupo_id in chaves:
        registros_grupo = grupos[grupo_id]
        try:
            if dry_run:
                with engine.connect() as conn:
                    status, registros_auditoria, resumo = _processar_grupo(
                        conn, registros_grupo, batch_id, True)
            else:
                with engine.begin() as conn:
                    status, registros_auditoria, resumo = _processar_grupo(
                        conn, registros_grupo, batch_id, False)
                    if status != "APLICADO":
                        # sem escrita a confirmar: desfaz a transacao do grupo
                        raise GrupoAbortado(status, resumo.get("motivo", ""))
        except GrupoAbortado as erro:
            status, resumo = erro.status, {
                "grupo_id": grupo_id, "motivo": erro.motivo}
        except Exception as erro:  # falha real: aborta o lote inteiro
            log(f"ERRO no grupo {grupo_id}: {erro}")
            raise

        por_status[status] = por_status.get(status, 0) + 1
        if status == "APLICADO":
            auditoria.extend(registros_auditoria)
            lote.append(resumo)
        elif status == "DRY_RUN":
            lote.append(resumo)
        else:
            skips.append({
                "grupo_id": resumo.get("grupo_id", grupo_id),
                "tombo": registros_grupo[0].get("tombo", ""),
                "orgao_id": registros_grupo[0].get("orgao_id", ""),
                "status": status,
                "motivo": resumo.get("motivo", ""),
            })

    if dry_run:
        comum.escrever_csv(pasta_run / "lote.csv", CABECALHO_LOTE, lote)
    else:
        comum.escrever_csv(pasta_run / "auditoria.csv",
                           CABECALHO_AUDITORIA, auditoria)
        comum.escrever_csv(pasta_run / "lote.csv", CABECALHO_LOTE, lote)
    comum.escrever_csv(pasta_run / "skips.csv", CABECALHO_SKIPS, skips)

    stats = {
        "batch_id": batch_id,
        "dry_run": dry_run,
        "grupos_selecionados": len(chaves),
        "por_status": por_status,
        "descartados": sum(
            len([d for d in str(r.get("descartados", "")).split(",") if d])
            for r in lote if r.get("status") in ("APLICADO", "DRY_RUN")),
        "pasta": str(pasta_run),
        "auditoria": auditoria,
        "skips": skips,
        "lote": lote,
    }
    _escrever_relatorio(pasta_run / "relatorio.md", stats, log)
    log(f"Lote {batch_id}: {json.dumps(por_status, ensure_ascii=False)}")
    log(f"Artefatos em {pasta_run}")
    return stats


def _escrever_relatorio(caminho: Path, stats: dict, log) -> None:
    titulo = "Dry-run" if stats["dry_run"] else "Aplicacao"
    linhas = [
        f"# {titulo} das duplicatas — lote {stats['batch_id']}",
        "",
        f"Gerado em {comum.agora_iso()}.",
        "",
        "| Status | Grupos |",
        "|---|---:|",
    ]
    for status, total in sorted(stats["por_status"].items()):
        linhas.append(f"| {status} | {total} |")
    linhas += [
        "",
        f"Copias descartadas: {stats['descartados']}",
        "",
        "## Detalhe",
        "",
        "| grupo_id | tombo | orgao | canonico | descartados | refs | estoque pi | estoque ti | status |",
        "|---:|---:|---:|---:|---|---:|---|---|---|",
    ]
    for linha in stats["lote"]:
        linhas.append(
            f"| {linha.get('grupo_id')} | {linha.get('tombo', '')} | "
            f"{linha.get('orgao_id', '')} | {linha.get('canonical_id', '')} | "
            f"{linha.get('descartados', '')} | {linha.get('refs_total', 0)} | "
            f"{linha.get('estoque_pi', '')} | {linha.get('estoque_ti', '')} | "
            f"{linha.get('status', '')} |")
    if stats["skips"]:
        linhas += ["", "## Grupos pulados", "", "| grupo_id | status | motivo |",
                   "|---:|---|---|"]
        for skip in stats["skips"]:
            linhas.append(
                f"| {skip['grupo_id']} | {skip['status']} | {skip['motivo']} |")
    if not stats["dry_run"]:
        linhas += [
            "",
            "Rollback:",
            "",
            "```bash",
            f"python -m ajustes.duplicatas_equipamentos.03_aplicar "
            f"--rollback {Path(stats['pasta']) / 'auditoria.csv'} --apply",
            "```",
        ]
    linhas.append("")
    Path(caminho).write_text("\n".join(linhas) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def _carregar_auditoria(caminho: Path) -> list[dict]:
    caminho = Path(caminho)
    if not caminho.exists():
        raise FileNotFoundError(f"{caminho} nao existe.")
    registros = comum.ler_csv(caminho)
    if not registros:
        raise ValueError(f"{caminho} esta vazio.")
    return registros


def _reverter_registro(conn, registro: dict) -> None:
    batch_id = registro["batch_id"]
    exclu = int(registro["discarded_id"])
    refs = json.loads(registro.get("refs") or "{}")

    pendente = conn.execute(
        text(
            "SELECT revertido_em FROM duplicatas_equipamentos_ajuste "
            "WHERE batch_id = :b AND discarded_id = :d"
        ),
        {"b": batch_id, "d": exclu},
    ).scalar()
    if pendente is not None:
        raise GrupoAbortado("SKIP_JA_REVERTIDO",
                            f"copia {exclu} ja revertida neste lote")

    # Refs de volta para a copia (ids foram registrados antes do repoint).
    for tabela, _coluna, _ in comum.TABELAS_REF:
        ids = refs.get(tabela) or []
        if ids:
            conn.execute(
                _sql_in(f"UPDATE {tabela} SET equipment_id = :exclu WHERE id IN :ids"),
                {"exclu": exclu, "ids": tuple(int(i) for i in ids)},
            )

    for item in refs.get("related_equipments") or []:
        para = item["para"]
        de = item["de"]
        conn.execute(
            text(
                "UPDATE related_equipments SET new_equipment_id = :de_n, "
                "       old_equipment_id = :de_o "
                "WHERE new_equipment_id = :para_n AND old_equipment_id = :para_o"
            ),
            {"de_n": de["new"], "de_o": de["old"],
             "para_n": para["new"], "para_o": para["old"]},
        )
    for item in refs.get("related_equipments_removidos") or []:
        conn.execute(
            text(
                "INSERT IGNORE INTO related_equipments "
                "(new_equipment_id, old_equipment_id) VALUES (:n, :o)"
            ),
            {"n": item["new"], "o": item["old"]},
        )

    # Desfazer o soft delete e reexibir a entrada do historico.
    conn.execute(
        text(
            "UPDATE equipments SET deleted_at = NULL, updated_at = NOW() "
            "WHERE id = :exclu"
        ),
        {"exclu": exclu},
    )
    occulta = int(registro.get("entrada_ocultada") or 0)
    if occulta:
        conn.execute(
            text(
                "UPDATE equipment_history SET deleted_at = NULL "
                "WHERE equipment_id = :exclu AND reason = :entrada "
                "  AND deleted_at IS NOT NULL "
                "ORDER BY id DESC LIMIT :limite"
            ),
            {"exclu": exclu, "entrada": comum.REASON_ENTRADA, "limite": occulta},
        )

    # Estoque volta ao valor de antes.
    pi_id = registro.get("pi_id") or ""
    if pi_id and registro.get("quantity_pi_antes") not in ("", None):
        conn.execute(
            text("UPDATE product_items SET quantity = :q, updated_at = NOW() "
                 "WHERE id = :id"),
            {"q": float(registro["quantity_pi_antes"]), "id": int(pi_id)},
        )
    ti_id = registro.get("ti_id") or ""
    if ti_id and registro.get("quantity_ti_antes") not in ("", None):
        conn.execute(
            text("UPDATE transaction_items SET quantity = :q, updated_at = NOW() "
                 "WHERE id = :id"),
            {"q": int(float(registro["quantity_ti_antes"])), "id": int(ti_id)},
        )

    conn.execute(
        text(
            "UPDATE duplicatas_equipamentos_ajuste SET revertido_em = NOW() "
            "WHERE batch_id = :b AND discarded_id = :d"
        ),
        {"b": batch_id, "d": exclu},
    )


def reverter(engine, auditoria_csv: Path, dry_run: bool = True,
             log=print) -> dict:
    registros = _carregar_auditoria(auditoria_csv)
    if not dry_run:
        garantir_tabela(engine)

    revertidos = 0
    pulados: list[dict] = []
    for registro in reversed(registros):
        try:
            if dry_run:
                with engine.connect() as conn:
                    pendente = conn.execute(
                        text(
                            "SELECT revertido_em FROM duplicatas_equipamentos_ajuste "
                            "WHERE batch_id = :b AND discarded_id = :d"
                        ),
                        {"b": registro["batch_id"],
                         "d": int(registro["discarded_id"])},
                    ).scalar()
                if pendente is not None:
                    pulados.append({"discarded_id": registro["discarded_id"],
                                    "motivo": "ja revertido"})
                    continue
            else:
                with engine.begin() as conn:
                    _reverter_registro(conn, registro)
            revertidos += 1
        except GrupoAbortado as erro:
            pulados.append({"discarded_id": registro["discarded_id"],
                            "motivo": f"{erro.status}: {erro.motivo}"})
        except Exception as erro:
            log(f"ERRO ao reverter {registro['discarded_id']}: {erro}")
            raise

    stats = {"dry_run": dry_run, "lidos": len(registros),
             "revertidos": revertidos, "pulados": pulados}
    log(f"Rollback {'(dry-run)' if dry_run else ''}: "
        f"{revertidos}/{len(registros)} revertidos, {len(pulados)} pulados")
    return stats
