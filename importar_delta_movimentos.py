#!/usr/bin/env python3
"""Planejador (dry-run) do delta de producao para o banco local migrado.

Regras centrais:
- A PK de producao nunca e reutilizada localmente; ela e apenas chave de origem.
- Linhas com criacao dentro da janela sao candidatas a insercao. Linhas apenas
  atualizadas na janela ficam fora da v1 (somente insercao).
- Reconciliacao por chave natural antes de criar IDs novos: movimentos por
  `number`, requisicoes pelo movimento, manutencoes por equipamento+data,
  historico por equipamento+ocorrido+motivo, volumes e itens por pai. Assim o
  delta nao duplica o que a migracao de 13/11 ja trouxe.
- Pais criados fora da janela (manutencoes antigas, equipamentos antigos,
  enderecos/localizacoes) sao buscados na origem para fechar a FK.
- Toda FK e resolvida por um mapa explicito; FK obrigatoria sem mapa bloqueia.
- Nenhuma escrita e feita neste modulo no modo dry-run. Apenas SELECT.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

VERSION = "3.2.0"
NEW = "NEW"
DISCARD = "DISCARD"

CONTRACT_ITEM_SQL = """
SELECT ci.id AS id, TRIM(ci.alias) AS alias, ci.price AS price,
       ci.quantity AS quantity, ea.id AS aditivo,
       ea.contract_event_type_id AS tipo, ce.contract_id AS contrato,
       DATE(ci2.start_date) AS ini, DATE(ci2.end_date) AS fim
FROM contract_items ci
JOIN event_additives ea ON ea.id = ci.event_additive_id
JOIN contract_events ce ON ce.id = ea.event_id
LEFT JOIN contract_infos ci2 ON ci2.event_additive_id = ea.id
WHERE ci.deleted_at IS NULL
"""

CLOSURE_TABLES = {
    "addresses", "locations", "equipments", "movements", "service_orders",
    "movement_items", "service_order_items", "volumes", "volume_items",
    "shipments", "shipment_items", "maintenances", "maintenance_items",
    "product_items", "products", "brands", "groups", "types",
    "transactions", "transaction_items",
}

DESCARTES_DEFAULT = "ajustes/equipamentos_descartados_base.csv"

DATE_FIELDS = ("created_at", "updated_at", "occurred_at", "movement_date", "transaction_date", "maintenance_date")
CREATION_FIELD = {
    "equipment_history": "occurred_at",
    "movements": "created_at",
    "service_orders": "created_at",
    "work_orders": "created_at",
}

DIMENSIONS = [
    "customers", "organizations", "users", "status", "movement_types", "movement_status",
    "service_order_status", "shipment_status", "maintenance_types", "maintenance_status",
    "technician_status", "document_types", "transaction_types", "categories", "conditions",
    "suppliers", "products", "brands", "groups", "types",
]

# Ordem de processamento: dimensoes -> estoque -> transacoes -> malha de movimentos.
PHASES = [
    "brands", "groups", "types", "products", "product_items", "equipments",
    "transactions", "transaction_items", "service_orders", "service_order_items",
    "movements", "movement_items", "shipments", "shipment_items",
    "shipment_movements", "volumes", "volume_items", "equipment_history",
    "maintenances", "maintenance_items", "return_confirmations",
    "return_confirmations_items", "romaneios",
]

# Tabelas cujo nome e chave natural (quando nao houver id local).
NAME_TABLES = {"brands", "groups", "types"}
PRODUCT_TABLE = "products"

# Status "BAIXADO" no banco refatorado.
STATUS_BAIXADO = 9


def status_equipamento_insercao(values):
    """Status com que um equipamento novo do delta entra no banco local.

    Equipamento excluido na origem (deleted_at preenchido) e baixado: nao pode
    herdar o status operacional que tinha antes da exclusao.
    """
    if values.get("deleted_at"):
        return STATUS_BAIXADO
    return values.get("status_id")


def normalize(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def normalize_record(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def as_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def creation_date(table, row):
    field = CREATION_FIELD.get(table, "created_at")
    return as_date(row.get(field)) or as_date(row.get("created_at")) or as_date(row.get("occurred_at"))


def in_window(value, start, end):
    return value is not None and start <= value <= end


def normalize_number(value):
    if value is None:
        return None
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return normalize(value) or None


def price_key(value) -> str:
    return str(Decimal(str(0 if value is None else value)).quantize(Decimal("0.01")))


def quantity_key(value) -> str:
    return str(Decimal(str(0 if value is None else value)).quantize(Decimal("0.0001")))


def contract_item_indexes(rows):
    """Indexa contract_items locais pelas chaves naturais de reconciliacao."""
    indexes = {"completa": defaultdict(list), "aditivo": defaultdict(list),
               "preco": defaultdict(list)}
    for row in rows:
        alias = normalize(row.get("alias"))
        if not alias:
            continue
        contrato = row.get("contrato")
        indexes["completa"][(
            contrato, alias, row.get("aditivo"), row.get("tipo"),
            str(row.get("ini")), str(row.get("fim")))].append(int(row["id"]))
        indexes["aditivo"][(contrato, alias, row.get("aditivo"))].append(int(row["id"]))
        indexes["preco"][(
            contrato, alias, price_key(row.get("price")),
            quantity_key(row.get("quantity")))].append(int(row["id"]))
    return indexes


def match_contract_item(source, contrato_local, indexes):
    """Resolve um item de producao no item local equivalente; None se ambiguo."""
    alias = normalize(source.get("alias"))
    if not alias or contrato_local is None:
        return None
    tentativas = (
        ((contrato_local, alias, source.get("aditivo"), source.get("tipo"),
          str(source.get("ini")), str(source.get("fim"))), "completa"),
        ((contrato_local, alias, source.get("aditivo")), "aditivo"),
        ((contrato_local, alias, price_key(source.get("price")),
          quantity_key(source.get("quantity"))), "preco"),
    )
    for chave, nome in tentativas:
        hits = indexes[nome].get(chave, [])
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def build_contract_maps(engine, engine_prod, item_ids=()):
    """Mapas prod->local de contratos (por numero) e itens (por chave natural).

    Ids de contrato e de item de contrato nao alinham entre producao e banco
    local: contratos ignorados pela planilha deixam buracos e a correcao dos
    aditivos desloca a sequencia de itens. Copiar o id cru liga o registro ao
    contrato errado, por isso a traducao e sempre por chave natural.
    """
    from sqlalchemy import text

    contratos = {}
    with engine.connect() as conn:
        locais = [(int(i), numero) for i, numero in conn.execute(
            text("SELECT id, number FROM contracts WHERE deleted_at IS NULL"))]
    por_numero = defaultdict(list)
    for local_id, numero in locais:
        chave = normalize(numero)
        if chave:
            por_numero[chave].append(local_id)
    with engine_prod.connect() as conn:
        origem = [(int(i), numero) for i, numero in conn.execute(
            text("SELECT id, number FROM contracts"))]
    for prod_id, numero in origem:
        hits = por_numero.get(normalize(numero), [])
        if len(hits) == 1:
            contratos[prod_id] = hits[0]

    itens = {}
    wanted = {int(v) for v in item_ids if v is not None}
    if wanted:
        with engine.connect() as conn:
            indexes = contract_item_indexes(
                [dict(row._mapping) for row in conn.execute(text(CONTRACT_ITEM_SQL))])
        with engine_prod.connect() as conn:
            for row in conn.execute(text(CONTRACT_ITEM_SQL)):
                source = dict(row._mapping)
                if int(source["id"]) not in wanted:
                    continue
                local = match_contract_item(
                    source, contratos.get(source.get("contrato")), indexes)
                if local is not None:
                    itens[int(source["id"])] = local
    return {"contracts": contratos, "contract_items": itens}


def equipment_choice(source, candidates):
    """Return (local_row or None, status, confidence, reason)."""
    number = normalize_number(source.get("number"))
    if number is None:
        # Equipamentos sem tombo existem no legado/producao; sao novos e nao
        # podem ser deduplicados por numero. O numero e nullable no destino.
        return None, "new", "low", "equipment without number; new row"
    same = [r for r in candidates if normalize_number(r.get("number")) == number]
    serial = normalize(source.get("serial_number")) if source.get("serial_number") not in (None, "") else ""
    if serial:
        exact = [r for r in same if normalize(r.get("serial_number")) == serial]
        if len(exact) == 1:
            return exact[0], "matched", "high", "number and serial"
        if len(same) > 1:
            return None, "blocked", "none", "duplicate number without matching serial"
    if len(same) == 1:
        if serial and same[0].get("serial_number") and normalize(same[0]["serial_number"]) != serial:
            return None, "blocked", "none", "serial conflict"
        return same[0], "candidate_review", "low", "unique number, serial not corroborated"
    if len(same) > 1:
        return None, "blocked", "none", "ambiguous equipment number"
    return None, "new", "high", "no local equipment for number"


def stamp(value):
    """Normaliza datetime/string para comparacao estavel (segundos)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d 00:00:00")
    text = str(value).strip().replace("T", " ")
    try:
        return datetime.fromisoformat(text[:26]).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def stamp_num(value):
    """Normaliza Decimal/float para comparacao estavel."""
    if value in (None, ""):
        return None
    return f"{Decimal(value).normalize():f}"


TX_FIELDS = (
    "id", "transaction_type_id", "doc_number", "doc_date", "transaction_date",
    "supplier_id", "buyer_id", "sender_id", "receiver_id", "customer_id",
    "created_at", "deleted_at",
)
TXI_FIELDS = ("id", "transaction_id", "product_id", "unit_cost", "quantity",
              "created_at", "deleted_at")


def tx_key(row):
    """Identidade de uma transacao: IDs de organizacao alinham, enderecos nao."""
    return (
        row.get("transaction_type_id"), normalize(row.get("doc_number")),
        stamp(row.get("doc_date")), stamp(row.get("transaction_date")),
        row.get("supplier_id"), row.get("buyer_id"), row.get("sender_id"),
        row.get("receiver_id"), row.get("customer_id"), stamp(row.get("created_at")),
    )


def load_transactions(engine):
    """Carrega transacoes/itens locais para o casamento por chave natural."""
    from sqlalchemy import text
    found = {}
    with engine.connect() as conn:
        for table, fields in (("transactions", TX_FIELDS),
                              ("transaction_items", TXI_FIELDS)):
            rows = conn.execute(text(
                "SELECT " + ", ".join(f"`{c}`" for c in fields)
                + f" FROM `{table}`")).all()
            found[table] = [dict(zip(fields, r)) for r in rows]
    return found


def day(value):
    """Normaliza data (YYYY-MM-DD) para comparacao."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def load_discards(path):
    """Carrega ids/tombos de equipamentos descartados de proposito."""
    ids, tombos = set(), set()
    target = Path(path)
    if not target.exists():
        return ids, tombos
    with target.open(encoding="utf-8-sig", newline="") as stream:
        for line in csv.DictReader(stream):
            try:
                ids.add(int(line.get("id_legado")))
            except (TypeError, ValueError):
                pass
            number = normalize_number(line.get("tombo"))
            if number is not None:
                tombos.add(number)
    return ids, tombos


def chunks(values, size=500):
    """Divide um conjunto de IDs em lotes estaveis para o IN (...)."""
    ordered = sorted(set(values))
    for start in range(0, len(ordered), size):
        yield ordered[start:start + size]


class Crosswalk:
    """Traduz clientes/enderecos de producao para os IDs locais da migracao.

    Producao e local sao duas migracoes do mesmo legado: os IDs de cliente e
    de endereco NAO coincidem. A ponte e `addresses.legacy_customer_id`
    (id legado), com o de/para de duplicados aplicado - a mesma regra usada
    pela migracao.
    """

    def __init__(self, address_rows, customer_rows, depara):
        self.depara = depara
        self.addr_by_legacy = defaultdict(list)
        self.addr_by_key = defaultdict(list)
        self.addr_by_id = {}
        for row in address_rows:
            if row.get("deleted_at"):
                continue
            self.addr_by_id[row["id"]] = row
            legacy = row.get("legacy_customer_id")
            if legacy is not None:
                self.addr_by_legacy[int(legacy)].append(row)
            self.addr_by_key[(row.get("addressable_type"), row.get("addressable_id"),
                              normalize(row.get("alias")))].append(row)
        self.customer_by_name = defaultdict(list)
        for row in customer_rows:
            if not row.get("deleted_at"):
                self.customer_by_name[normalize(row.get("name"))].append(row)
        self.prod_address = {}
        self.prod_customer = {}
        self.prod_customer_addresses = defaultdict(list)
        self._addr_cache = {}
        self._cust_cache = {}
        self.pending_address = set()
        self.pending_customer = set()

    # ------------------------------------------------------------------ load
    def prepare(self, engine_prod, address_ids, customer_ids):
        from sqlalchemy import text
        address_ids = {int(v) for v in address_ids if v is not None}
        customer_ids = {int(v) for v in customer_ids if v is not None}
        with engine_prod.connect() as conn:
            for batch in chunks(address_ids):
                sql = ("SELECT id, addressable_type, addressable_id, alias, legacy_customer_id "
                       "FROM addresses WHERE id IN (" + ",".join(str(v) for v in batch) + ")")
                for row in conn.execute(text(sql)).all():
                    self.prod_address[row[0]] = dict(
                        id=row[0], addressable_type=row[1], addressable_id=row[2],
                        alias=row[3], legacy_customer_id=row[4])
            for batch in chunks(customer_ids):
                sql = ("SELECT id, name FROM customers WHERE id IN ("
                       + ",".join(str(v) for v in batch) + ")")
                for row in conn.execute(text(sql)).all():
                    self.prod_customer[row[0]] = {"id": row[0], "name": row[1]}
                sql = ("SELECT id, addressable_id, alias, legacy_customer_id FROM addresses "
                       "WHERE addressable_type='customer' AND addressable_id IN ("
                       + ",".join(str(v) for v in batch) + ") ORDER BY id")
                for row in conn.execute(text(sql)).all():
                    self.prod_customer_addresses[row[1]].append(
                        {"id": row[0], "addressable_id": row[1], "alias": row[2],
                         "legacy_customer_id": row[3]})
                    self.prod_address.setdefault(row[0], {
                        "id": row[0], "addressable_type": "customer",
                        "addressable_id": row[1], "alias": row[2],
                        "legacy_customer_id": row[3]})

    # --------------------------------------------------------------- mapping
    def address(self, source_id):
        if source_id is None:
            return None
        source_id = int(source_id)
        if source_id in self._addr_cache:
            return self._addr_cache[source_id]
        prod = self.prod_address.get(source_id)
        result = None
        if prod is not None:
            legacy = prod.get("legacy_customer_id")
            if legacy is not None:
                legacy = int(legacy)
                for candidate in (self.depara.get(legacy, legacy), legacy):
                    hits = self.addr_by_legacy.get(candidate, [])
                    if not hits:
                        continue
                    same = [h for h in hits
                            if normalize(h.get("alias")) == normalize(prod.get("alias"))]
                    result = (same or hits)[0]["id"]
                    break
            if result is None:
                hits = self.addr_by_key.get((prod.get("addressable_type"),
                                             prod.get("addressable_id"),
                                             normalize(prod.get("alias"))), [])
                if len(hits) == 1:
                    result = hits[0]["id"]
        self._addr_cache[source_id] = result
        if result is None:
            self.pending_address.add(source_id)
        return result

    def customer(self, source_id):
        if source_id is None:
            return None
        source_id = int(source_id)
        if source_id in self._cust_cache:
            return self._cust_cache[source_id]
        result = None
        for address in self.prod_customer_addresses.get(source_id, []):
            local_address = self.address(address["id"])
            if local_address is None:
                continue
            row = self.addr_by_id.get(local_address)
            if row is not None:
                result = row["addressable_id"]
                break
        if result is None:
            prod = self.prod_customer.get(source_id)
            if prod is not None:
                hits = self.customer_by_name.get(normalize(prod.get("name")), [])
                if len(hits) == 1:
                    result = hits[0]["id"]
        self._cust_cache[source_id] = result
        if result is None:
            self.pending_customer.add(source_id)
        return result

    def customer_address(self, source_customer_id):
        local_customer = self.customer(source_customer_id)
        if local_customer is None:
            return None
        hits = [row for row in self.addr_by_id.values()
                if row.get("addressable_type") == "customer"
                and row.get("addressable_id") == local_customer]
        linked = [row for row in hits if row.get("legacy_customer_id") is not None]
        if len(linked) == 1:
            return linked[0]["id"]
        if len(hits) == 1:
            return hits[0]["id"]
        return linked[0]["id"] if linked else (hits[0]["id"] if hits else None)


class Planner:
    def __init__(self, lookups, fks, nullables, crosswalk=None,
                 discard_ids=None, discard_tombos=None):
        self.lookups = lookups          # table -> list[dict]
        self.fks = fks                  # table -> {column: referenced_table}
        self.nullables = nullables      # table -> set(column)
        self.map = {}                   # (table, source_id) -> local_id | NEW | DISCARD | None
        self.status = {}                # (table, source_id) -> status
        self.evidence = {}              # (table, source_id) -> evidence
        self.rows = defaultdict(list)   # table -> linhas candidatas a insercao
        self.refs = defaultdict(dict)   # table -> {id: row} apenas para FK
        self.extras = defaultdict(list)  # linhas descobertas no fecho de FK
        self.extra_seen = set()
        self.planned_ids = defaultdict(set)  # ids ja presentes em self.rows
        self.insertable_ids = defaultdict(set)
        self.discard_ids = discard_ids or set()
        self.discard_tombos = discard_tombos or set()
        self.crosswalk = crosswalk
        self.updates = []               # (table, local_id, {coluna: valor}, origem)
        self.index = {}
        self.fixed_map = {}             # table -> {id_prod: id_local} por chave natural
        self.prod = None                # engine da origem (prod_14_11)
        self.tx_rows = {"transactions": [], "transaction_items": []}

    # --------------------------------------------------------------- indexes
    def build_indexes(self):
        idx = self.index
        idx["movements_by_number"] = defaultdict(list)
        for row in self.lookups.get("movements", []):
            if row.get("number") is not None and not row.get("deleted_at"):
                idx["movements_by_number"][str(row["number"]).strip()].append(row)
        idx["so_by_org_created"] = defaultdict(list)
        for row in self.lookups.get("service_orders", []):
            if not row.get("deleted_at"):
                idx["so_by_org_created"][
                    (row.get("organization_id"), stamp(row.get("created_at")))].append(row)
        idx["equip_by_number"] = defaultdict(list)
        for row in self.lookups.get("equipments", []):
            number = normalize_number(row.get("number"))
            if number is not None and not row.get("deleted_at"):
                idx["equip_by_number"][number].append(row)
        idx["maint_by_equip_day"] = defaultdict(list)
        for row in self.lookups.get("maintenances", []):
            if not row.get("deleted_at"):
                idx["maint_by_equip_day"][
                    (row.get("equipment_id"), day(row.get("maintenance_date")))].append(row)
        idx["maint_item"] = defaultdict(list)
        for row in self.lookups.get("maintenance_items", []):
            idx["maint_item"][(row.get("maintenance_id"), row.get("maintenance_type_id"),
                               normalize(row.get("details")),
                               stamp(row.get("created_at")))].append(row)
        idx["history"] = defaultdict(list)
        for row in self.lookups.get("equipment_history", []):
            idx["history"][(row.get("equipment_id"), stamp(row.get("occurred_at")),
                            normalize(row.get("reason")))].append(row)
        idx["history_status"] = defaultdict(list)
        for row in self.lookups.get("equipment_history", []):
            idx["history_status"][(row.get("equipment_id"), row.get("status_id"),
                                   stamp(row.get("occurred_at")))].append(row)
        idx["tx_key"] = defaultdict(list)
        for row in self.tx_rows.get("transactions", []):
            if not row.get("deleted_at"):
                idx["tx_key"][tx_key(row)].append(row)
        idx["txi_key"] = defaultdict(list)
        for row in self.tx_rows.get("transaction_items", []):
            if not row.get("deleted_at"):
                idx["txi_key"][(row.get("transaction_id"), row.get("product_id"),
                                stamp_num(row.get("unit_cost")),
                                stamp_num(row.get("quantity")),
                                stamp(row.get("created_at")))].append(row)
        for table in ("movement_items", "service_order_items", "shipment_items",
                      "shipment_movements", "volume_items", "volumes", "shipments"):
            idx[table] = defaultdict(list)
            for row in self.lookups.get(table, []):
                idx[table][self.item_key(table, row)].append(row)
        return self

    @staticmethod
    def item_key(table, row):
        if table == "movement_items":
            return (row.get("movement_id"), row.get("equipment_id"), row.get("product_item_id"),
                    normalize(row.get("alias")))
        if table == "service_order_items":
            return (row.get("service_order_id"), row.get("equipment_id"), row.get("product_id"),
                    normalize(row.get("alias")))
        if table == "shipment_items":
            return (row.get("shipment_id"), row.get("movement_item_id"))
        if table == "shipment_movements":
            return (row.get("shipment_id"), row.get("movement_id"))
        if table == "volume_items":
            return (row.get("volume_id"), row.get("equipment_id"), row.get("product_item_id"),
                    row.get("movement_item_id"))
        if table == "volumes":
            return row.get("movement_id")
        if table == "shipments":
            return (row.get("status_id"), row.get("requires_invoice"), stamp(row.get("created_at")))
        return None

    def local_item(self, table, key):
        """Retorna e consome (FIFO) o proximo item local da chave."""
        bucket = self.index.get(table, {}).get(key)
        if not bucket:
            return None
        return bucket.pop(0)

    # ------------------------------------------------------------ candidatos
    def prepare_scope(self, scope):
        """Carrega as linhas candidatas; a insercao e marcada depois."""
        for table, rows in scope.items():
            self.rows[table].extend(rows)
            self.planned_ids[table].update(
                row.get("id") for row in rows if row.get("id") is not None)
        return self

    def add_refs(self, table, rows):
        for row in rows:
            if row.get("id") is not None:
                self.refs[table][row["id"]] = row
        return self

    def add_extra(self, table, row, reason):
        sid = row.get("id")
        key = (table, sid)
        if key in self.extra_seen:
            return NEW
        if sid in self.planned_ids.get(table, set()):
            # Linha ja esta no escopo principal: nao duplicar em extras.
            self.extra_seen.add(key)
            if key not in self.map:
                self.register(table, sid, NEW, "new", reason)
            return self.map.get(key)
        self.extra_seen.add(key)
        self.register(table, sid, NEW, "new", reason)
        self.insertable_ids[table].add(sid)
        self.extras[table].append(row)
        return NEW

    def allow_new(self, table, source_id):
        if table in {"brands", "groups", "types", "products", "product_items", "locations"}:
            return True
        if table == "maintenances":
            return True
        return source_id in self.insertable_ids.get(table, set())


    # ---------------------------------------------------------------- helpers
    def dim_exists(self, table, source_id):
        for row in self.lookups.get(table, []):
            if row.get("id") == source_id and not row.get("deleted_at"):
                return row["id"]
        return None

    def local_by_id(self, table, source_id):
        for row in self.lookups.get(table, []):
            if row.get("id") == source_id and not row.get("deleted_at"):
                return row
        return None

    def name_match(self, table, name):
        target = normalize(name)
        if not target:
            return None
        hits = [r for r in self.lookups.get(table, [])
                if not r.get("deleted_at") and normalize(r.get("name")) == target]
        return hits[0]["id"] if len(hits) == 1 else None

    def register(self, table, source_id, value, status, evidence=""):
        self.map[(table, source_id)] = value
        self.status[(table, source_id)] = status
        self.evidence[(table, source_id)] = evidence

    def source_row(self, table, source_id):
        row = self.refs.get(table, {}).get(source_id)
        if row is not None:
            return row
        for candidate in self.rows.get(table, []):
            if candidate.get("id") == source_id:
                return candidate
        return None

    def translate(self, table, source_id):
        """Traduz uma FK de origem para o id local. Nunca usa id de equipamento."""
        if source_id is None:
            return None
        key = (table, source_id)
        if key in self.map:
            mapped = self.map[key]
            return None if mapped in (None, DISCARD) else mapped
        fixo = self.fixed_map.get(table, {}).get(source_id)
        if fixo is not None:
            self.register(table, source_id, fixo, "matched", "chave natural")
            return fixo
        if table == "customers" and self.crosswalk is not None:
            local = self.crosswalk.customer(source_id)
            if local is not None:
                self.register(table, source_id, local, "matched", "legado do cliente")
                return local
        if table == "addresses" and self.crosswalk is not None:
            local = self.crosswalk.address(source_id)
            if local is not None:
                self.register(table, source_id, local, "matched", "legado do endereco")
                return local
        if table in DIMENSIONS and table not in NAME_TABLES and table != PRODUCT_TABLE:
            local = self.dim_exists(table, source_id)
            if local is not None:
                self.register(table, source_id, local, "matched", "dimensao por id")
                return local
            return None
        if source_id in self.refs.get(table, {}) or source_id in self.insertable_ids.get(table, set()):
            return self.ensure_table(table, source_id)
        if table == "equipments":
            # IDs de equipamento NAO alinham entre prod e local.
            return None
        if table in ("contracts", "contract_items"):
            # IDs de contrato/item tambem nao alinham: copiar o id cru liga o
            # registro ao contrato errado. Sem chave natural, fica sem vinculo.
            return None
        local = self.local_by_id(table, source_id)
        if local is not None:
            self.register(table, source_id, local["id"], "matched", "id local existente")
            return local["id"]
        return None

    def ensure_table(self, table, source_id):
        handler = getattr(self, "_ensure_" + table, None)
        if handler is None:
            return None
        row = self.source_row(table, source_id)
        if row is None:
            return None
        return handler(row)

    def resolve_fk(self, table, column, value):
        """Resolve uma FK. Retorna (valor_local_ou_NEW_ou_None, ausente)."""
        if value is None:
            return None, False
        target = self.fks.get(table, {}).get(column)
        if not target:
            return value, False
        key = (target, value)
        if key in self.map:
            mapped = self.map[key]
            if mapped in (None, DISCARD):
                return None, True
            return mapped, False
        mapped = self.translate(target, value)
        if mapped is None:
            return None, True
        return mapped, False

    def apply_fks(self, table, row, address=None, location=None):
        """Retorna (valores resolvidos, colunas bloqueantes, colunas anuladas)."""
        resolved, blocking, nulled = {}, [], []
        for column, value in row.items():
            if column == "id" or not column.endswith("_id"):
                continue
            if column == "address_id" and address is not None:
                resolved[column] = address
                continue
            if column == "location_id" and location is not None:
                resolved[column] = location
                continue
            mapped, missing = self.resolve_fk(table, column, value)
            if not missing:
                resolved[column] = mapped
                continue
            if column in self.nullables.get(table, set()):
                resolved[column] = None
                nulled.append(column)
            else:
                blocking.append(column)
        return resolved, blocking, nulled

    def customer_address(self, customer_id):
        if self.crosswalk is not None:
            local = self.crosswalk.customer_address(customer_id)
            if local is not None:
                return local, "endereco do cliente (legado)"
        hits = [r for r in self.lookups.get("addresses", [])
                if r.get("addressable_type") == "customer"
                and r.get("addressable_id") == customer_id
                and not r.get("deleted_at")]
        linked = [r for r in hits if r.get("legacy_customer_id") is not None]
        if len(linked) == 1:
            return linked[0]["id"], "customer legacy address"
        if len(hits) == 1:
            return hits[0]["id"], "unique customer address"
        return None, "customer has multiple addresses"

    def location_match(self, address_id, area=None):
        if address_id is None:
            return None
        hits = [r for r in self.lookups.get("locations", [])
                if r.get("address_id") == address_id and not r.get("deleted_at")
                and (area is None or normalize(r.get("area")) == normalize(area))]
        return hits[0]["id"] if len(hits) == 1 else None

    def resolve_location(self, source_id):
        row = self.source_row("locations", source_id)
        if row is None:
            return None
        local_address = self.translate("addresses", row.get("address_id"))
        if local_address is None:
            self.register("locations", source_id, None, "blocked",
                          "endereco da localizacao nao mapeado")
            return None
        hits = [r for r in self.lookups.get("locations", [])
                if r.get("address_id") == local_address and not r.get("deleted_at")
                and normalize(r.get("area")) == normalize(row.get("area"))]
        if hits:
            self.register("locations", source_id, hits[0]["id"],
                          "matched" if len(hits) == 1 else "candidate_review",
                          "endereco+area")
            return hits[0]["id"]
        if not self.allow_new("locations", source_id):
            return None
        return self.add_extra("locations", row, "localizacao nova (endereco+area)")

    # ------------------------------------------------------- fecho de FK
    def _equipment_local(self, source_id):
        """Equipamento de origem -> id local. Nunca compara IDs, so tombo."""
        if source_id is None:
            return None
        value = self.map.get(("equipments", source_id))
        if value not in (None, NEW, DISCARD):
            return value
        row = self.source_row("equipments", source_id)
        number = normalize_number(row.get("number")) if row else None
        hits = self.index["equip_by_number"].get(number, []) if number is not None else []
        return hits[0]["id"] if len(hits) == 1 else None

    def _ensure_existing(self, table, row):
        """Tabelas de itens/pais so entram no delta pelo escopo, nunca por FK."""
        key = (table, row.get("id"))
        value = self.map.get(key)
        return None if value in (None, DISCARD) else value

    def _ensure_movement_items(self, row):
        return self._ensure_existing("movement_items", row)

    def _ensure_service_order_items(self, row):
        return self._ensure_existing("service_order_items", row)

    def _ensure_shipment_items(self, row):
        return self._ensure_existing("shipment_items", row)

    def _ensure_volume_items(self, row):
        return self._ensure_existing("volume_items", row)

    def _ensure_volumes(self, row):
        return self._ensure_existing("volumes", row)

    def _ensure_shipments(self, row):
        return self._ensure_existing("shipments", row)

    def _ensure_movements(self, row):
        return self._ensure_existing("movements", row)

    def _ensure_service_orders(self, row):
        return self._ensure_existing("service_orders", row)

    def _ensure_equipment_history(self, row):
        return None

    def _ensure_named(self, table, row):
        sid = row.get("id")
        key = (table, sid)
        if key in self.map:
            value = self.map[key]
            return None if value in (None, DISCARD) else value
        local = self.dim_exists(table, sid) or self.name_match(table, row.get("name"))
        if local is not None:
            self.register(table, sid, local, "matched", "dimensao por id ou nome")
            return local
        return self.add_extra(table, row, "nova dimensao por nome")

    def _ensure_brands(self, row):
        return self._ensure_named("brands", row)

    def _ensure_groups(self, row):
        return self._ensure_named("groups", row)

    def _ensure_types(self, row):
        return self._ensure_named("types", row)

    def _ensure_products(self, row):
        sid = row.get("id")
        key = ("products", sid)
        if key in self.map:
            return self.map[key]
        local = self.dim_exists("products", sid) or self.name_match("products", row.get("name"))
        if local is not None:
            self.register("products", sid, local, "matched", "produto por id ou nome")
            return local
        return self.add_extra("products", row, "produto novo")

    def _ensure_product_items(self, row):
        sid = row.get("id")
        key = ("product_items", sid)
        if key in self.map:
            return self.map[key]
        code = row.get("code")
        hits = [r for r in self.lookups.get("product_items", [])
                if code is not None and r.get("code") == code and not r.get("deleted_at")]
        if len(hits) == 1:
            self.register("product_items", sid, hits[0]["id"], "matched", "codigo do product_item")
            return hits[0]["id"]
        if len(hits) > 1:
            self.register("product_items", sid, None, "blocked", "codigo duplicado local")
            return None
        address = None
        if row.get("address_id") is not None:
            address = self.translate("addresses", row["address_id"])
        location = None
        if row.get("location_id") is not None:
            location = self.resolve_location(row["location_id"])
        _, blocking, _ = self.apply_fks("product_items", row, address=address, location=location)
        if blocking:
            self.register("product_items", sid, None, "blocked",
                          "product_item sem mapa: " + ",".join(blocking))
            return None
        return self.add_extra("product_items", row, "product_item referenciado")

    def _ensure_equipments(self, row):
        sid = row.get("id")
        key = ("equipments", sid)
        if key in self.map:
            value = self.map[key]
            return None if value in (None, DISCARD) else value
        number = normalize_number(row.get("number"))
        hits = self.index["equip_by_number"].get(number, []) if number is not None else []
        if len(hits) == 1:
            self.register("equipments", sid, hits[0]["id"], "matched", "tombo unico")
            return hits[0]["id"]
        if len(hits) > 1:
            self.register("equipments", sid, None, "blocked", "tombo duplicado local")
            return None
        if sid not in self.insertable_ids.get("equipments", set()):
            self.register("equipments", sid, None, "skipped",
                          "equipamento fora do escopo do delta")
            return None
        self.register("equipments", sid, NEW, "new", "equipamento novo (referencia)")
        return NEW

    def _ensure_transactions(self, row):
        sid = row.get("id")
        key = ("transactions", sid)
        if key in self.map:
            return self.map[key]
        hits = self.index["tx_key"].get(tx_key(row), [])
        if len(hits) == 1:
            self.register("transactions", sid, hits[0]["id"], "matched", "chave natural")
            return hits[0]["id"]
        if len(hits) > 1:
            self.register("transactions", sid, None, "blocked", "transacao ambigua local")
            return None
        _, blocking, _ = self.apply_fks("transactions", row)
        if blocking:
            self.register("transactions", sid, None, "blocked",
                          "transacao sem mapa: " + ",".join(blocking))
            return None
        return self.add_extra("transactions", row, "transacao nova")

    def _ensure_transaction_items(self, row):
        sid = row.get("id")
        key = ("transaction_items", sid)
        if key in self.map:
            return self.map[key]
        parent = self.translate("transactions", row.get("transaction_id"))
        if isinstance(parent, int):
            hits = self.index["txi_key"].get(
                (parent, row.get("product_id"), stamp_num(row.get("unit_cost")),
                 stamp_num(row.get("quantity")), stamp(row.get("created_at"))), [])
            if len(hits) == 1:
                self.register("transaction_items", sid, hits[0]["id"], "matched",
                              "chave natural do item")
                return hits[0]["id"]
        elif parent == NEW:
            pass
        else:
            self.register("transaction_items", sid, None, "blocked",
                          "transacao do item sem mapa")
            return None
        _, blocking, _ = self.apply_fks("transaction_items", row)
        if blocking:
            self.register("transaction_items", sid, None, "blocked",
                          "item de transacao sem mapa: " + ",".join(blocking))
            return None
        return self.add_extra("transaction_items", row, "item de transacao novo")

    def _ensure_maintenances(self, row):
        sid = row.get("id")
        key = ("maintenances", sid)
        if key in self.map:
            return self.map[key]
        equipment = self._equipment_local(row.get("equipment_id"))
        hits = self.index["maint_by_equip_day"].get(
            (equipment, day(row.get("maintenance_date"))), []) if equipment else []
        if len(hits) == 1:
            self.register("maintenances", sid, hits[0]["id"], "matched", "equipamento+dia")
            return hits[0]["id"]
        _, blocking, _ = self.apply_fks("maintenances", row)
        if blocking:
            self.register("maintenances", sid, None, "blocked",
                          "manutencao sem mapa: " + ",".join(blocking))
            return None
        return self.add_extra("maintenances", row, "manutencao nova")

    # ------------------------------------------------- itens de pai existente
    def lookup_item(self, table, row):
        """Equivalente local de um item de pai que ja existe (nunca cria)."""
        if row is None:
            return None
        if table == "movement_items":
            parent = self.map.get(("movements", row.get("movement_id")))
            if parent in (None, NEW, DISCARD):
                return None
            equipment = self._equipment_local(row.get("equipment_id"))
            alias = normalize(row.get("alias"))
            hits = [r for r in self.lookups.get("movement_items", [])
                    if r.get("movement_id") == parent and not r.get("deleted_at")
                    and (equipment is None or r.get("equipment_id") == equipment)
                    and normalize(r.get("alias")) == alias]
            return hits[0]["id"] if len(hits) == 1 else None
        if table == "service_order_items":
            parent = self.map.get(("service_orders", row.get("service_order_id")))
            if parent in (None, NEW, DISCARD):
                return None
            equipment = self._equipment_local(row.get("equipment_id"))
            alias = normalize(row.get("alias"))
            hits = [r for r in self.lookups.get("service_order_items", [])
                    if r.get("service_order_id") == parent and not r.get("deleted_at")
                    and (equipment is None or r.get("equipment_id") == equipment)
                    and normalize(r.get("alias")) == alias]
            return hits[0]["id"] if len(hits) == 1 else None
        if table in ("shipment_items", "volume_items"):
            item = self.map.get(("movement_items", row.get("movement_item_id")))
            if item in (None, NEW, DISCARD):
                return None
            hits = [r for r in self.lookups.get(table, [])
                    if r.get("movement_item_id") == item and not r.get("deleted_at")]
            return hits[0]["id"] if len(hits) == 1 else None
        return None

    def _parent_state(self, table, row, column):
        parent_id = row.get(column)
        if parent_id is None:
            return "none"
        key = (table, parent_id)
        if key not in self.map:
            return "pending"
        value = self.map[key]
        if value in (None, DISCARD):
            return "discarded"
        return "new" if value == NEW else "local"

    def _plan_child(self, table, row, parent_table, parent_column):
        sid = row.get("id")
        if (table, sid) in self.map:
            return self.map[(table, sid)]
        state = self._parent_state(parent_table, row, parent_column)
        if state == "pending":
            return None
        if state == "discarded":
            self.register(table, sid, None, "blocked",
                          f"pai {parent_table}:{row.get(parent_column)} sem mapa")
            return None
        if sid not in self.insertable_ids.get(table, set()):
            local = self.lookup_item(table, row)
            if local is not None:
                self.register(table, sid, local, "matched", "item local equivalente")
            else:
                self.register(table, sid, None, "skipped", "item de pai ja existente local")
            return self.map[(table, sid)]
        address = None
        if row.get("address_id") is not None:
            address = self.translate("addresses", row["address_id"])
        location = None
        if row.get("location_id") is not None:
            location = self.resolve_location(row["location_id"])
        _, blocking, nulled = self.apply_fks(table, row, address=address, location=location)
        if blocking:
            self.register(table, sid, None, "blocked",
                          "sem mapa para NOT NULL: " + ",".join(blocking))
            return None
        evidence = "item novo"
        if nulled:
            evidence += "; anuladas: " + ",".join(nulled)
        self.register(table, sid, NEW, "new", evidence)
        return NEW

    # ------------------------------------------------ natural-key matching
    def match_equipments(self):
        for row in list(self.rows.get("equipments", [])):
            sid = row.get("id")
            if ("equipments", sid) in self.map:
                continue
            number = normalize_number(row.get("number"))
            if sid in self.discard_ids or (number is not None and number in self.discard_tombos):
                self.register("equipments", sid, DISCARD, "discarded",
                              "equipamento descartado na migracao")
                continue
            hits = self.index["equip_by_number"].get(number, []) if number is not None else []
            if len(hits) == 1:
                self.register("equipments", sid, hits[0]["id"], "matched", "tombo unico")
            elif len(hits) > 1:
                self.register("equipments", sid, None, "blocked", "tombo duplicado local")
            else:
                self.register("equipments", sid, NEW, "new", "equipamento sem par por tombo")

    def plan_equipment_rows(self):
        for row in list(self.rows.get("equipments", [])):
            sid = row.get("id")
            if self.map.get(("equipments", sid)) != NEW:
                continue
            address = None
            if row.get("address_id") is not None:
                address = self.translate("addresses", row["address_id"])
            if address is None:
                self.register("equipments", sid, None, "blocked",
                              "endereco do equipamento nao mapeado")
                continue
            location = None
            if row.get("location_id") is not None:
                location = self.resolve_location(row["location_id"])
            _, blocking, nulled = self.apply_fks("equipments", row,
                                                 address=address, location=location)
            if blocking:
                self.register("equipments", sid, None, "blocked",
                              "sem mapa para NOT NULL: " + ",".join(blocking))
                continue
            evidence = "equipamento novo"
            if nulled:
                evidence += "; anuladas: " + ",".join(nulled)
            self.register("equipments", sid, NEW, "new", evidence)

    def match_movements(self):
        for row in list(self.rows.get("movements", [])):
            sid = row.get("id")
            if ("movements", sid) in self.map:
                continue
            number = str(row.get("number")).strip() if row.get("number") is not None else None
            candidates = self.index["movements_by_number"].get(number, []) if number else []
            # O numero NAO e unico: prod reusa numeros do legado. So casa com
            # identidade completa (number + criacao + organizacao).
            exact = [r for r in candidates
                     if stamp(r.get("created_at")) == stamp(row.get("created_at"))
                     and r.get("organization_id") == row.get("organization_id")]
            if len(exact) == 1:
                self.register("movements", sid, exact[0]["id"], "matched",
                              "number+criacao+organizacao")
            elif len(exact) > 1:
                self.register("movements", sid, None, "blocked", "identidade duplicada local")
            elif candidates:
                self.register("movements", sid, NEW, "new",
                              "numero local pertence a outro movimento")
            else:
                self.register("movements", sid, NEW, "new", "number ausente local")

    def match_service_orders(self):
        by_so = defaultdict(list)
        for row in self.rows.get("movements", []):
            if row.get("service_order_id"):
                by_so[row["service_order_id"]].append(row)
        for row in list(self.rows.get("service_orders", [])):
            sid = row.get("id")
            if ("service_orders", sid) in self.map:
                continue
            local_movements = []
            for movement in by_so.get(sid, []):
                lid = self.map.get(("movements", movement.get("id")))
                if lid not in (None, NEW, DISCARD):
                    local = self.local_by_id("movements", lid)
                    if local is not None:
                        local_movements.append(local)
            if len(local_movements) == 1 and local_movements[0].get("service_order_id"):
                self.register("service_orders", sid, local_movements[0]["service_order_id"],
                              "matched", "via movimento casado")
                continue
            key = (row.get("organization_id"), stamp(row.get("created_at")))
            cands = self.index["so_by_org_created"].get(key, [])
            claimed = {value for (table, _), value in self.map.items()
                       if table == "service_orders" and isinstance(value, int)}
            exact = [r for r in cands
                     if normalize(r.get("details")) == normalize(row.get("details"))
                     and r.get("id") not in claimed]
            if len(exact) == 1:
                self.register("service_orders", sid, exact[0]["id"], "matched",
                              "organizacao+criacao+detalhes")
            else:
                self.register("service_orders", sid, NEW, "new",
                              "requisicao sem par local (criacao/detalhes divergem)")

    def resolve_movement_so_conflicts(self):
        for row in list(self.rows.get("movements", [])):
            sid = row.get("id")
            if self.map.get(("movements", sid)) != NEW:
                continue
            so = self.map.get(("service_orders", row.get("service_order_id"))) \
                if row.get("service_order_id") else None
            if so in (None, NEW, DISCARD):
                continue
            local = [m for m in self.lookups.get("movements", [])
                     if m.get("service_order_id") == so and not m.get("deleted_at")]
            if len(local) == 1:
                self.register("movements", sid, local[0]["id"], "candidate_review",
                              "requisicao ja possui movimento local")
            elif len(local) > 1:
                self.register("movements", sid, None, "blocked",
                              "requisicao local com multiplos movimentos")

    def match_service_order_items(self):
        for row in list(self.rows.get("service_order_items", [])):
            self._plan_child("service_order_items", row,
                             "service_orders", "service_order_id")

    def match_movement_items(self):
        for row in list(self.rows.get("movement_items", [])):
            self._plan_child("movement_items", row, "movements", "movement_id")

    def match_volumes(self):
        for row in list(self.rows.get("volumes", [])):
            self._plan_child("volumes", row, "movements", "movement_id")

    def match_shipments(self):
        new_movements = self.insertable_ids.get("movements", set())
        by_ship = defaultdict(list)
        for item in self.rows.get("shipment_items", []):
            by_ship[item.get("shipment_id")].append(item)
        item_owner = {r.get("id"): r.get("movement_id")
                      for r in self.rows.get("movement_items", [])}
        for row in list(self.rows.get("shipments", [])):
            sid = row.get("id")
            if ("shipments", sid) in self.map:
                continue
            owned = {item_owner.get(i.get("movement_item_id")) for i in by_ship.get(sid, [])}
            if owned & new_movements:
                self.register("shipments", sid, NEW, "new", "envio de movimento novo")
                continue
            hits = self.index["shipments"].get(self.item_key("shipments", row), [])
            if len(hits) == 1:
                self.register("shipments", sid, hits[0]["id"], "matched", "envio equivalente")
            else:
                self.register("shipments", sid, None, "skipped", "envio de movimento ja local")

    def match_shipment_items(self):
        for row in list(self.rows.get("shipment_items", [])):
            self._plan_child("shipment_items", row, "shipments", "shipment_id")

    def match_shipment_movements(self):
        """Vinculo envio x movimento: depende dos dois pais ja mapeados."""
        for row in list(self.rows.get("shipment_movements", [])):
            sid = row.get("id")
            if ("shipment_movements", sid) in self.map:
                continue
            shipment_source = row.get("shipment_id")
            movement_source = row.get("movement_id")
            if ("shipments", shipment_source) not in self.map:
                continue
            if ("movements", movement_source) not in self.map:
                continue
            shipment = self.map[("shipments", shipment_source)]
            movement = self.map[("movements", movement_source)]
            if shipment in (None, DISCARD) or movement in (None, DISCARD):
                self.register("shipment_movements", sid, None, "blocked",
                              "vinculo com envio/movimento sem mapa")
                continue
            par = None
            if NEW not in (shipment, movement):
                par = self.local_item("shipment_movements", (shipment, movement))
            if sid not in self.insertable_ids.get("shipment_movements", set()):
                if par is not None:
                    self.register("shipment_movements", sid, par["id"], "matched",
                                  "vinculo local equivalente")
                else:
                    self.register("shipment_movements", sid, None, "skipped",
                                  "vinculo de envio ja local")
                continue
            if par is not None:
                self.register("shipment_movements", sid, par["id"], "matched",
                              "vinculo local equivalente")
                continue
            self.register("shipment_movements", sid, NEW, "new", "vinculo de envio novo")

    def match_volume_items(self):
        for row in list(self.rows.get("volume_items", [])):
            self._plan_child("volume_items", row, "volumes", "volume_id")

    def match_maintenances(self):
        for row in list(self.rows.get("maintenances", [])):
            self._ensure_maintenances(row)

    def match_maintenance_items(self):
        for row in list(self.rows.get("maintenance_items", [])):
            sid = row.get("id")
            if ("maintenance_items", sid) in self.map:
                continue
            state = self._parent_state("maintenances", row, "maintenance_id")
            if state == "pending":
                parent = self.source_row("maintenances", row.get("maintenance_id"))
                if parent is not None and ("maintenances", parent.get("id")) not in self.map:
                    self._ensure_maintenances(parent)
                    state = self._parent_state("maintenances", row, "maintenance_id")
            if state == "pending":
                continue
            if state == "discarded":
                self.register("maintenance_items", sid, None, "blocked",
                              "manutencao sem mapa")
                continue
            parent = self.map[("maintenances", row.get("maintenance_id"))]
            key = (parent, row.get("maintenance_type_id"), normalize(row.get("details")),
                   stamp(row.get("created_at")))
            hits = self.index["maint_item"].get(key, [])
            if len(hits) == 1:
                self.register("maintenance_items", sid, hits[0]["id"], "matched",
                              "tipo+detalhes+criacao")
                continue
            _, blocking, nulled = self.apply_fks("maintenance_items", row)
            if blocking:
                self.register("maintenance_items", sid, None, "blocked",
                              "item de manutencao sem mapa: " + ",".join(blocking))
                continue
            evidence = "item de manutencao novo"
            if nulled:
                evidence += "; anuladas: " + ",".join(nulled)
            self.register("maintenance_items", sid, NEW, "new", evidence)

    def match_equipment_history(self):
        refs = (("movement_item_id", "movement_items"),
                ("service_order_item_id", "service_order_items"),
                ("shipment_item_id", "shipment_items"))
        for row in list(self.rows.get("equipment_history", [])):
            sid = row.get("id")
            if ("equipment_history", sid) in self.map:
                continue
            for column, target in refs:
                source = row.get(column)
                if source is None or (target, source) in self.map:
                    continue
                reference = self.source_row(target, source)
                local = self.lookup_item(target, reference) if reference else None
                if local is not None:
                    self.register(target, source, local, "matched",
                                  "item local equivalente")
            state = self._parent_state("equipments", row, "equipment_id")
            if state == "pending":
                equipment = self._equipment_local(row.get("equipment_id"))
                state = "local" if equipment is not None else "discarded"
            if state == "discarded":
                self.register("equipment_history", sid, None, "blocked",
                              "equipamento sem mapa")
                continue
            equipment = None
            if state != "new":
                equipment = self._equipment_local(row.get("equipment_id"))
                if equipment is None:
                    self.register("equipment_history", sid, None, "blocked",
                                  "equipamento sem mapa")
                    continue
            if state != "new":
                hits = self.index["history_status"].get(
                    (equipment, row.get("status_id"), stamp(row.get("occurred_at"))), [])
                if len(hits) == 1:
                    self.register("equipment_history", sid, hits[0]["id"], "matched",
                                  "equipamento+ocorrido+situacao")
                    continue
            _, blocking, nulled = self.apply_fks("equipment_history", row)
            if blocking:
                self.register("equipment_history", sid, None, "blocked",
                              "historico sem mapa: " + ",".join(blocking))
                continue
            evidence = "historico novo"
            if nulled:
                evidence += "; anuladas: " + ",".join(nulled)
            self.register("equipment_history", sid, NEW, "new", evidence)

    def plan_status_updates(self):
        """Atualiza a situacao dos movimentos/requisicoes casados na origem."""
        for table, columns in (("movements", ("status_id",)),
                               ("service_orders", ("status_id", "situation"))):
            for row in self.rows.get(table, []):
                sid = row.get("id")
                local_id = self.map.get((table, sid))
                if local_id in (None, NEW, DISCARD):
                    continue
                local = self.local_by_id(table, local_id)
                if local is None:
                    continue
                changes = {}
                for column in columns:
                    if column not in row:
                        continue
                    if local.get(column) != row.get(column):
                        changes[column] = row.get(column)
                if not changes:
                    continue
                if stamp(row.get("updated_at")) and stamp(local.get("updated_at")) \
                        and stamp(row.get("updated_at")) <= stamp(local.get("updated_at")):
                    continue
                self.updates.append((table, local_id, changes,
                                     f"origem atualizada em {stamp(row.get('updated_at'))}"))

    # ---------------------------------------------------------------- driver
    def load_children(self, rows):
        """Carrega os filhos cujo pai sera inserido (escopo do delta)."""
        new_movements = set(self.insertable_ids.get("movements", set()))
        new_service_orders = set(self.insertable_ids.get("service_orders", set()))
        self.rows["service_order_items"].extend(
            r for r in rows.get("service_order_items", [])
            if r.get("service_order_id") in new_service_orders)
        items = [r for r in rows.get("movement_items", [])
                 if r.get("movement_id") in new_movements]
        self.rows["movement_items"].extend(items)
        item_ids = {r.get("id") for r in items}
        self.rows["volumes"].extend(
            r for r in rows.get("volumes", []) if r.get("movement_id") in new_movements)
        volume_ids = {r.get("id") for r in self.rows["volumes"]}
        shipment_items = [r for r in rows.get("shipment_items", [])
                          if r.get("movement_item_id") in item_ids]
        self.rows["shipment_items"].extend(shipment_items)
        shipment_ids = {r.get("shipment_id") for r in shipment_items}
        self.rows["shipments"].extend(
            r for r in rows.get("shipments", []) if r.get("id") in shipment_ids)
        self.rows["shipment_movements"].extend(
            r for r in rows.get("shipment_movements", [])
            if r.get("shipment_id") in shipment_ids)
        self.rows["volume_items"].extend(
            r for r in rows.get("volume_items", [])
            if r.get("volume_id") in volume_ids or r.get("movement_item_id") in item_ids)
        self.rows["maintenance_items"].extend(rows.get("maintenance_items", []))
        return self

    def mark_insertable_children(self):
        return mark_insertable(self)

    def run(self, passes=5):
        for _ in range(passes):
            before = len(self.map)
            self.match_equipments()
            self.plan_equipment_rows()
            self.match_movements()
            self.match_service_orders()
            self.resolve_movement_so_conflicts()
            self.match_service_order_items()
            self.match_movement_items()
            self.match_volumes()
            self.match_shipments()
            self.match_shipment_items()
            self.match_shipment_movements()
            self.match_volume_items()
            self.match_maintenances()
            self.match_maintenance_items()
            self.match_equipment_history()
            if len(self.map) == before:
                break
        self.plan_status_updates()
        return self



def load_delta(path, start, end):
    raw = Path(path).read_bytes()
    entries = json.loads(raw)
    rows = defaultdict(list)
    updated_only = Counter()
    excluded = Counter()
    audit = Counter()
    for item in entries:
        table = item.get("tabela", "unknown")
        row = normalize_record(item.get("registro_json"))
        if table == "audit_logs":
            audit[str(row.get("action", "")).lower()] += 1
            continue
        if table == "report_exports":
            excluded[table] += 1
            continue
        created = creation_date(table, row)
        if created is None:
            rows[table].append(row)  # sem data: mantem para revisao
        elif in_window(created, start, end):
            rows[table].append(row)
        elif in_window(as_date(row.get("updated_at")), start, end):
            updated_only[table] += 1
        else:
            excluded[table] += 1
    return raw, rows, updated_only, excluded, audit


def load_lookups(engine, tables):
    from sqlalchemy import text
    lookups, fks, nullables = {}, {}, {}
    with engine.connect() as conn:
        database = conn.execute(text("SELECT DATABASE()")).scalar()
        existing = {r[0] for r in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"))}
        for table in tables:
            if table not in existing:
                continue
            columns = {r[0]: r[1] for r in conn.execute(text(
                "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": table})}
            needed = [c for c in (
                "id", "deleted_at", "name", "number", "serial_number", "code",
                "addressable_type", "addressable_id", "legacy_customer_id",
                "product_id", "category_id", "condition_id", "address_id",
                "location_id", "organization_id", "area", "customer_id",
                "created_at", "updated_at", "status_id", "movement_date",
                "movement_id", "equipment_id", "service_order_id",
                "maintenance_date", "maintenance_id", "occurred_at", "reason",
                "alias", "shipment_id", "movement_item_id", "volume_id",
                "product_item_id", "transaction_id", "recipient_customer_id",
                "movement_type_id", "details", "situation", "is_conversion",
                "transaction_item_id", "product_item_id") if c in columns]
            if "id" not in needed:
                continue
            rows = conn.execute(text(
                "SELECT " + ", ".join(f"`{c}`" for c in needed) + f" FROM `{table}`")).all()
            lookups[table] = [dict(zip(needed, r)) for r in rows]
            nullables[table] = {c for c, v in columns.items() if v == "YES"}
            fks[table] = {r[0]: r[1] for r in conn.execute(text(
                "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND REFERENCED_TABLE_NAME IS NOT NULL"),
                {"t": table})}
        # Tabelas referenciadas apenas para comprovar existencia do ID.
        referenced = set().union(*(set(v.values()) for v in fks.values())) if fks else set()
        for table in sorted(referenced - set(lookups)):
            if table not in existing:
                continue
            columns = {r[0] for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": table})}
            wanted = [c for c in ("id", "deleted_at", "name") if c in columns]
            if "id" not in wanted:
                continue
            rows = conn.execute(text(
                "SELECT " + ", ".join(f"`{c}`" for c in wanted) + f" FROM `{table}`")).all()
            lookups[table] = [dict(zip(wanted, r)) for r in rows]
    return database, lookups, fks, nullables


def write_csv(path, rows, fields):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Fase 2: aplicacao transacional com IDs novos (escopo controlado)
# ---------------------------------------------------------------------------
SCOPE_ORDER = [
    "brands", "groups", "types", "products", "product_items", "locations",
    "transactions", "transaction_items", "equipments",
    "service_orders", "service_order_items", "movements", "movement_items",
    "volumes", "shipments", "shipment_items", "shipment_movements",
    "volume_items", "maintenances", "maintenance_items", "equipment_history",
]


def select_scope(rows):
    """Pais do delta; os filhos entram depois, ja amarrados a um pai novo."""
    parents = ["movements", "service_orders", "equipments", "maintenances",
               "equipment_history"]
    return {table: list(rows.get(table, [])) for table in parents}


def mark_insertable(planner):
    """Decide o que pode virar linha nova: filhos acompanham o pai novo."""
    mark = planner.insertable_ids
    for table in ("equipments", "movements", "service_orders", "maintenances",
                  "equipment_history"):
        for row in planner.rows.get(table, []):
            if planner.map.get((table, row.get("id"))) == NEW:
                mark[table].add(row.get("id"))
    new_movements = set(mark["movements"])
    new_service_orders = set(mark["service_orders"])
    for row in planner.rows.get("service_order_items", []):
        if row.get("service_order_id") in new_service_orders:
            mark["service_order_items"].add(row.get("id"))
    for row in planner.rows.get("movement_items", []):
        if row.get("movement_id") in new_movements:
            mark["movement_items"].add(row.get("id"))
    new_items = set(mark["movement_items"])
    for row in planner.rows.get("volumes", []):
        if row.get("movement_id") in new_movements:
            mark["volumes"].add(row.get("id"))
    for row in planner.rows.get("shipment_items", []):
        if row.get("movement_item_id") in new_items:
            mark["shipment_items"].add(row.get("id"))
    shipment_ids = {r.get("shipment_id") for r in planner.rows.get("shipment_items", [])
                    if r.get("id") in mark["shipment_items"]}
    for row in planner.rows.get("shipments", []):
        if row.get("id") in shipment_ids:
            mark["shipments"].add(row.get("id"))
    for row in planner.rows.get("shipment_movements", []):
        if row.get("shipment_id") in shipment_ids:
            mark["shipment_movements"].add(row.get("id"))
    for row in planner.rows.get("volume_items", []):
        if (row.get("movement_item_id") in new_items
                or row.get("id") in mark["shipment_items"]):
            mark["volume_items"].add(row.get("id"))
    return mark


def fetch_source_rows(engine_prod, table, ids):
    from sqlalchemy import text
    found = []
    with engine_prod.connect() as conn:
        for batch in chunks(ids):
            sql = ("SELECT * FROM `" + table + "` WHERE `id` IN ("
                   + ",".join(str(int(v)) for v in batch) + ")")
            for row in conn.execute(text(sql)).all():
                found.append(dict(row._mapping))
    return found


def close_scope(planner, engine_prod, rounds=3):
    """Carrega na origem os pais referenciados que ainda nao tem mapa."""
    added = 0
    skip = {"addresses", "customers", "status", "organizations", "users"}
    for _ in range(rounds):
        needed = defaultdict(set)
        groups = (list(planner.rows.items()) + list(planner.extras.items())
                  + list(planner.refs.items()))
        for table, rows in groups:
            rows = rows.values() if isinstance(rows, dict) else rows
            for row in rows:
                source_id = row.get("id")
                if source_id is None:
                    continue
                planner.refs[table].setdefault(source_id, row)
                for column, value in row.items():
                    target = planner.fks.get(table, {}).get(column)
                    if target is None or value is None:
                        continue
                    if target not in CLOSURE_TABLES or target in skip:
                        continue
                    if (target, value) in planner.map:
                        continue
                    if value in planner.refs.get(target, {}):
                        continue
                    needed[target].add(value)
        if not needed:
            break
        for table, ids in needed.items():
            rows = fetch_source_rows(engine_prod, table, sorted(ids))
            planner.add_refs(table, rows)
            added += len(rows)
    return added


def resolve_values(planner, inserted, table, row):
    """Monta os valores de INSERT trocando FKs pelos IDs locais ja criados."""
    values = {}
    for column, value in row.items():
        if column == "id":
            continue
        if isinstance(value, (dict, list)):
            values[column] = json.dumps(value, ensure_ascii=False)
            continue
        if value is None:
            values[column] = None
            continue
        target = planner.fks.get(table, {}).get(column)
        if target is None:
            values[column] = value
            continue
        if table == "transaction_items" and column == "equipment_id":
            # Elo circular com equipments: preenchido apos a insercao.
            values[column] = None
            continue
        mapped = planner.map.get((target, value))
        if mapped is None and (target, value) not in planner.map:
            mapped = planner.translate(target, value)
        if mapped == NEW:
            local = inserted.get((target, value))
            if local is None:
                if column in planner.nullables.get(table, set()):
                    values[column] = None
                    continue
                raise RuntimeError(
                    f"{table}.{column}: dependencia {target}:{value} nao inserida")
            values[column] = local
            continue
        if mapped in (None, DISCARD):
            if column in planner.nullables.get(table, set()):
                values[column] = None
                continue
            raise RuntimeError(f"{table}.{column}: FK {target}:{value} nao mapeada")
        values[column] = mapped
    return values


def prune_scope(scope, planner):
    """Compatibilidade: o planejamento atual nao remove equipamentos."""
    return scope, set()


class SimulationRollback(Exception):
    """Sentinela para desfazer a transacao do modo --simular."""


def sincronizar_sequencia_movimentos(conn):
    """Alinha `movement_number_sequences.last_number` com MAX(movements.number).

    O delta insere protocolos explicitos da producao sem passar pelo hook do
    Laravel; sem esta sincronizacao a sequencia fica atras e o proximo
    movimento criado pela tela reutilizaria um numero.
    """
    from sqlalchemy import text

    existe = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'movement_number_sequences'")).scalar()
    if not existe:
        return None
    ultimo = int(conn.execute(text(
        "SELECT COALESCE(MAX(CAST(`number` AS UNSIGNED)), 0) FROM `movements` "
        "WHERE `number` IS NOT NULL AND `number` <> ''")).scalar() or 0)
    alterado = conn.execute(text(
        "UPDATE `movement_number_sequences` "
        "SET `last_number` = GREATEST(`last_number`, :ultimo) WHERE `id` = 1"),
        {"ultimo": ultimo})
    if alterado.rowcount == 0:
        conn.execute(text(
            "INSERT INTO `movement_number_sequences` (`id`, `last_number`) "
            "VALUES (1, :ultimo)"), {"ultimo": ultimo})
    return ultimo


def apply_scope(engine, planner, digest, ledger_path, commit=True):
    from sqlalchemy import text

    ledger = {}
    path = Path(ledger_path)
    if path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            for line in csv.DictReader(stream):
                if line.get("digest") == digest and line.get("local_id"):
                    ledger[(line["table"], str(line["source_id"]))] = int(line["local_id"])

    inserted = {}
    actions = []
    processed = set()
    try:
        with engine.begin() as conn:
            volume_codes = {r[0] for r in conn.execute(text(
                "SELECT `code` FROM `volumes` WHERE `code` IS NOT NULL"))}
            for table in SCOPE_ORDER:
                rows = list(planner.rows.get(table, [])) + list(planner.extras.get(table, []))
                for row in rows:
                    source_id = row.get("id")
                    if (table, source_id) in processed:
                        continue
                    processed.add((table, source_id))
                    key = (table, str(source_id))
                    if key in ledger:
                        raise RuntimeError(
                            f"ledger ja aplicado para {table}:{source_id}; refaca o dump")
                    planned = planner.map.get((table, source_id))
                    if planned in (None, DISCARD):
                        actions.append({"table": table, "source_id": source_id,
                                        "local_id": "", "action": "blocked"})
                        continue
                    if planned != NEW:
                        inserted[(table, source_id)] = planned
                        actions.append({"table": table, "source_id": source_id,
                                        "local_id": planned, "action": "matched"})
                        continue
                    values = resolve_values(planner, inserted, table, row)
                    if table == "equipments":
                        values["status_id"] = status_equipamento_insercao(values)
                    if table == "volumes":
                        code = values.get("code")
                        if code in volume_codes:
                            # Código do prod já existe localmente. O fallback
                            # precisa ser único: dois volumes do MESMO
                            # movimento gerariam `MIG-{movement_id}` repetido
                            # e violariam volumes_code_unique.
                            base = f"MIG-{values.get('movement_id')}-{source_id}"
                            code = base
                            contador = 1
                            while code in volume_codes:
                                contador += 1
                                code = f"{base}-{contador}"
                            values["code"] = code
                        volume_codes.add(code)
                    columns = list(values)
                    binds = {f"v{i}": values[c] for i, c in enumerate(columns)}
                    sql = ("INSERT INTO `" + table + "` ("
                           + ", ".join(f"`{c}`" for c in columns) + ") VALUES ("
                           + ", ".join(f":v{i}" for i in range(len(columns))) + ")")
                    result = conn.execute(text(sql), binds)
                    local_id = result.lastrowid
                    inserted[(table, source_id)] = local_id
                    planner.map[(table, source_id)] = local_id
                    actions.append({"table": table, "source_id": source_id,
                                    "local_id": local_id, "action": "insert"})
            for table, local_id, changes, reason in planner.updates:
                if not changes:
                    continue
                assignments = ", ".join(f"`{c}`=:c{i}" for i, c in enumerate(changes))
                binds = {f"c{i}": v for i, v in enumerate(changes.values())}
                binds["i"] = local_id
                conn.execute(text(f"UPDATE `{table}` SET {assignments} WHERE `id`=:i"), binds)
                actions.append({"table": table, "source_id": "", "local_id": local_id,
                                "action": "update_status"})
            for row in planner.rows.get("transaction_items", []):
                equipment_source = row.get("equipment_id")
                transaction_item = inserted.get(("transaction_items", row.get("id")))
                if not equipment_source or not transaction_item:
                    continue
                equipment = inserted.get(("equipments", equipment_source))
                if equipment:
                    conn.execute(text("UPDATE `transaction_items` SET `equipment_id`=:e "
                                      "WHERE `id`=:i"), {"e": equipment, "i": transaction_item})

            sincronizar_sequencia_movimentos(conn)

            if not commit:
                raise SimulationRollback()
    except SimulationRollback:
        return actions
    write_csv(Path(ledger_path), [
        {"digest": digest, "table": a["table"], "source_id": a["source_id"],
         "local_id": a["local_id"], "action": a["action"]}
        for a in actions
    ], ["digest", "table", "source_id", "local_id", "action"])
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description="Importador do delta de producao")
    parser.add_argument("--json", default="/tmp/opencode/delta_prod_04_12_11.json")
    parser.add_argument("--inicio", default="2026-11-04")
    parser.add_argument("--fim", default="2026-11-12")
    parser.add_argument("--out-dir", default="ajustes")
    parser.add_argument("--prod-url",
                        default="mysql+pymysql://root:root@localhost:3307/prod_14_11")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--simular", action="store_true",
                        help="Executa os INSERTs em transacao e desfaz no final.")
    parser.add_argument("--ledger", default="ajustes/delta_ledger.csv")
    args = parser.parse_args(argv)

    start, end = date.fromisoformat(args.inicio), date.fromisoformat(args.fim)
    raw, rows, updated_only, excluded, audit = load_delta(args.json, start, end)

    from config.database import obter_engines
    from sqlalchemy import create_engine
    engine, _ = obter_engines()
    engine_prod = create_engine(args.prod_url, pool_pre_ping=True, future=True)

    tables = set(rows) | set(DIMENSIONS) | {
        "addresses", "locations", "contracts", "contract_items", "equipments",
        "product_items", "products", "shipment_items", "shipment_movements",
        "volume_items",
    }
    database, lookups, fks, nullables = load_lookups(engine, tables)

    discard_ids, discard_tombos = load_discards(DESCARTES_DEFAULT)
    from utils.depara_clientes import carregar_depara_clientes
    crosswalk = Crosswalk(lookups.get("addresses", []), lookups.get("customers", []),
                          carregar_depara_clientes())
    address_ids, customer_ids = set(), set()
    for table, table_rows in rows.items():
        for row in table_rows:
            for column, value in row.items():
                if value is None:
                    continue
                target = fks.get(table, {}).get(column)
                if target == "addresses":
                    address_ids.add(value)
                elif target == "customers":
                    customer_ids.add(value)
    crosswalk.prepare(engine_prod, address_ids, customer_ids)

    planner = Planner(lookups, fks, nullables, crosswalk,
                      discard_ids=discard_ids, discard_tombos=discard_tombos)
    planner.prod = engine_prod
    planner.tx_rows = load_transactions(engine)
    planner.build_indexes()
    item_ids = set()
    for table_rows in rows.values():
        for row in table_rows:
            valor = row.get("contract_item_id")
            if valor is not None:
                item_ids.add(int(valor))
    planner.fixed_map = build_contract_maps(engine, engine_prod, item_ids)
    print(f"Contratos resolvidos por numero: {len(planner.fixed_map['contracts'])} | "
          f"itens de contrato por chave natural: "
          f"{len(planner.fixed_map['contract_items'])}/{len(item_ids)}")
    planner.prepare_scope(select_scope(rows))
    loaded = close_scope(planner, engine_prod)

    planner.match_equipments()
    planner.match_movements()
    planner.match_service_orders()
    planner.resolve_movement_so_conflicts()
    mark_insertable(planner)
    planner.load_children(rows)
    close_scope(planner, engine_prod)
    mark_insertable(planner)
    planner.run()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    crosswalk_rows, pending, plans = [], [], []
    for table in SCOPE_ORDER:
        for row in planner.rows.get(table, []) + planner.extras.get(table, []):
            source_id = row.get("id")
            status = planner.status.get((table, source_id), "blocked")
            target = planner.map.get((table, source_id))
            evidence = planner.evidence.get((table, source_id), "nao planejado")
            crosswalk_rows.append({
                "source_table": table, "source_id": source_id, "target_table": table,
                "target_id": "" if target in (None, NEW, DISCARD) else target,
                "method": "new" if target == NEW else (
                    "discard" if target == DISCARD else ("mapped" if target else "blocked")),
                "confidence": "high" if status in ("matched", "new", "discarded") else "low",
                "status": status, "evidence": evidence,
                "conflict": "" if status not in ("blocked", "candidate_review") else evidence,
            })
            if status in ("blocked", "candidate_review"):
                pending.append({"table": table, "source_id": source_id,
                                "reason": status, "evidence": evidence})
        counts = Counter(r["status"] for r in crosswalk_rows if r["source_table"] == table)
        for action in ("matched", "candidate_review", "new", "discarded", "skipped", "blocked"):
            if counts[action]:
                plans.append({"table": table, "action": action,
                              "count": counts[action], "detail": "dry-run"})
        if counts:
            print(f"{table}: source={sum(counts.values())} "
                  + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    write_csv(out / "delta_crosswalk.csv", crosswalk_rows,
              ["source_table", "source_id", "target_table", "target_id", "method",
               "confidence", "status", "evidence", "conflict"])
    write_csv(out / "delta_plano.csv", plans, ["table", "action", "count", "detail"])
    write_csv(out / "delta_pendencias.csv", pending, ["table", "source_id", "reason", "evidence"])
    summary = {
        "source_digest_sha256": hashlib.sha256(raw).hexdigest(),
        "window": {"inicio": args.inicio, "fim": args.fim},
        "database": database,
        "importer_version": VERSION,
        "closure_rows_loaded": loaded,
        "audit_actions": dict(audit),
        "updated_only_out_of_scope": dict(updated_only),
        "excluded_outside_window": dict(excluded),
        "status_updates": [
            {"table": t, "local_id": lid, "changes": ch, "reason": why}
            for t, lid, ch, why in planner.updates
        ],
        "per_table": {
            table: dict(Counter(r["status"] for r in crosswalk_rows
                                if r["source_table"] == table))
            for table in SCOPE_ORDER
        },
    }
    (out / "delta_resumo.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if args.apply or args.simular:
        print("\nAplicando delta (IDs novos):")
        actions = apply_scope(engine, planner, hashlib.sha256(raw).hexdigest(),
                              args.ledger, commit=args.apply and not args.simular)
        counts = Counter(a["action"] for a in actions)
        print("Resultado:", dict(counts))
        write_csv(out / ("delta_aplicado.csv" if args.apply and not args.simular
                         else "delta_simulado.csv"), actions,
                  ["table", "source_id", "local_id", "action"])
    engine.dispose()
    engine_prod.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
