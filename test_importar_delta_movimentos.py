import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from importar_delta_movimentos import (
    DISCARD,
    NEW,
    Planner,
    creation_date,
    equipment_choice,
    load_delta,
    mark_insertable,
    normalize,
    normalize_number,
    status_equipamento_insercao,
)


class EquipmentChoiceTests(unittest.TestCase):
    def test_serial_disambiguation(self):
        rows = [{"id": 1, "number": 5, "serial_number": "A"},
                {"id": 2, "number": 5, "serial_number": "B"}]
        chosen, status, _, _ = equipment_choice({"number": 5, "serial_number": "B"}, rows)
        self.assertEqual((chosen["id"], status), (2, "matched"))

    def test_duplicate_number_without_serial_blocked(self):
        rows = [{"id": 1, "number": 5}, {"id": 2, "number": 5}]
        _, status, _, _ = equipment_choice({"number": 5}, rows)
        self.assertEqual(status, "blocked")

    def test_unique_number_is_review_not_autoaccepted(self):
        rows = [{"id": 1, "number": 5, "serial_number": "A"}]
        _, status, confidence, _ = equipment_choice({"number": 5}, rows)
        self.assertEqual((status, confidence), ("candidate_review", "low"))

    def test_number_missing_is_new(self):
        _, status, _, _ = equipment_choice({"number": None}, [])
        self.assertEqual(status, "new")

    def test_number_normalization(self):
        self.assertEqual(normalize_number("106284.0"), "106284")
        self.assertEqual(normalize(" ab-001 / Á "), "AB001A")


class CreationDateTests(unittest.TestCase):
    def test_equipment_history_uses_occurred_at(self):
        row = {"created_at": "2026-01-01", "occurred_at": "2026-11-06 10:00:00"}
        self.assertEqual(creation_date("equipment_history", row), date(2026, 11, 6))


class StatusEquipamentoInsercaoTests(unittest.TestCase):
    """Equipamento excluído na origem entra como BAIXADO, não com o status antigo."""

    def test_excluido_na_origem_entra_baixado(self):
        self.assertEqual(
            status_equipamento_insercao(
                {"status_id": 1, "deleted_at": "2026-11-09 20:02:46"}),
            9,
        )

    def test_ativo_mantem_status_da_origem(self):
        self.assertEqual(
            status_equipamento_insercao({"status_id": 2, "deleted_at": None}), 2)


class PlannerTests(unittest.TestCase):
    def make(self, lookups=None, fks=None, nullables=None):
        return Planner(lookups or {}, fks or {}, nullables or {})

    def test_dimension_missing_blocks(self):
        p = self.make(lookups={"customers": []},
                      fks={"movements": {"recipient_customer_id": "customers"}})
        value, missing = p.resolve_fk("movements", "recipient_customer_id", 99)
        self.assertTrue(missing)
        self.assertIsNone(value)

    def test_dimension_existing_maps(self):
        p = self.make(lookups={"customers": [{"id": 48, "deleted_at": None}]})
        value, missing = p.resolve_fk("movements", "recipient_customer_id", 48)
        self.assertEqual((value, missing), (48, False))

    def test_local_id_fallback_maps_pre_existing_entity(self):
        p = self.make(lookups={"equipments": [{"id": 140774, "deleted_at": None}]})
        value, missing = p.resolve_fk("equipment_history", "equipment_id", 140774)
        self.assertEqual((value, missing), (140774, False))

    def test_address_ambiguity_blocks(self):
        p = self.make(lookups={"addresses": [
            {"id": 1, "addressable_type": "customer", "addressable_id": 4, "deleted_at": None},
            {"id": 2, "addressable_type": "customer", "addressable_id": 4, "deleted_at": None},
        ]})
        address, why = p.customer_address(4)
        self.assertIsNone(address)
        self.assertIn("multiple", why)

    def test_unmapped_not_null_fk_blocks(self):
        p = self.make(fks={"movement_items": {"movement_id": "movements"}},
                      nullables={"movement_items": set()})
        _, blocking, nulled = p.apply_fks("movement_items", {"movement_id": 999})
        self.assertEqual(blocking, ["movement_id"])
        self.assertEqual(nulled, [])

    def test_nullable_fk_is_nulled(self):
        p = self.make(fks={"movement_items": {"shipment_item_id": "shipment_items"}},
                      nullables={"movement_items": {"shipment_item_id"}})
        resolved, blocking, nulled = p.apply_fks("movement_items", {"shipment_item_id": 999})
        self.assertEqual((resolved["shipment_item_id"], blocking, nulled),
                         (None, [], ["shipment_item_id"]))

    def test_child_of_new_parent_is_plannable(self):
        p = self.make(fks={"movement_items": {"movement_id": "movements"}},
                      nullables={"movement_items": set()})
        p.map[("movements", 1)] = NEW
        p.insertable_ids["movement_items"].add(10)
        p._plan_child("movement_items", {"id": 10, "movement_id": 1},
                      "movements", "movement_id")
        self.assertEqual(p.status[("movement_items", 10)], "new")

    def test_child_of_missing_parent_is_blocked(self):
        p = self.make(fks={"movement_items": {"movement_id": "movements"}},
                      nullables={"movement_items": set()})
        p.map[("movements", 1)] = None  # pai sem mapa
        p.insertable_ids["movement_items"].add(10)
        p._plan_child("movement_items", {"id": 10, "movement_id": 1},
                      "movements", "movement_id")
        self.assertEqual(p.status[("movement_items", 10)], "blocked")

    def test_child_with_pending_parent_waits(self):
        p = self.make(fks={"movement_items": {"movement_id": "movements"}},
                      nullables={"movement_items": set()})
        p.insertable_ids["movement_items"].add(10)
        p._plan_child("movement_items", {"id": 10, "movement_id": 1},
                      "movements", "movement_id")
        self.assertNotIn(("movement_items", 10), p.map)

    def test_product_item_code_collision_blocks(self):
        p = self.make(lookups={"product_items": [
            {"id": 1, "code": "X", "deleted_at": None},
            {"id": 2, "code": "X", "deleted_at": None},
        ]})
        p._ensure_product_items({"id": 9, "code": "X"})
        self.assertEqual(p.status[("product_items", 9)], "blocked")


class ShipmentMovementLinkTests(unittest.TestCase):
    """Vinculo envio x movimento importado pelo delta de producao."""

    def make(self, lookups=None):
        p = Planner(lookups or {}, {}, {})
        p.build_indexes()
        return p

    def test_item_key_is_shipment_and_movement(self):
        self.assertEqual(
            Planner.item_key("shipment_movements", {"shipment_id": 4, "movement_id": 9}),
            (4, 9))

    def test_new_parents_generate_new_link(self):
        p = self.make()
        p.rows["shipment_movements"] = [{"id": 1, "shipment_id": 10, "movement_id": 20}]
        p.map[("shipments", 10)] = NEW
        p.map[("movements", 20)] = NEW
        p.insertable_ids["shipment_movements"].add(1)
        p.match_shipment_movements()
        self.assertEqual(p.map[("shipment_movements", 1)], NEW)
        self.assertEqual(p.status[("shipment_movements", 1)], "new")

    def test_pending_parent_waits(self):
        p = self.make()
        p.rows["shipment_movements"] = [{"id": 1, "shipment_id": 10, "movement_id": 20}]
        p.map[("shipments", 10)] = NEW  # movimento ainda sem mapa
        p.insertable_ids["shipment_movements"].add(1)
        p.match_shipment_movements()
        self.assertNotIn(("shipment_movements", 1), p.map)

    def test_existing_local_pair_is_matched(self):
        p = self.make(lookups={"shipment_movements": [
            {"id": 77, "shipment_id": 90386, "movement_id": 41032, "deleted_at": None},
        ]})
        p.rows["shipment_movements"] = [{"id": 5, "shipment_id": 1, "movement_id": 2}]
        p.map[("shipments", 1)] = 90386
        p.map[("movements", 2)] = 41032
        p.insertable_ids["shipment_movements"].add(5)
        p.match_shipment_movements()
        self.assertEqual(p.map[("shipment_movements", 5)], 77)
        self.assertEqual(p.status[("shipment_movements", 5)], "matched")

    def test_local_parents_without_link_are_skipped(self):
        p = self.make()
        p.rows["shipment_movements"] = [{"id": 5, "shipment_id": 1, "movement_id": 2}]
        p.map[("shipments", 1)] = 90386
        p.map[("movements", 2)] = 41032
        p.match_shipment_movements()
        self.assertEqual(p.status[("shipment_movements", 5)], "skipped")

    def test_blocked_parent_blocks_link(self):
        p = self.make()
        p.rows["shipment_movements"] = [{"id": 5, "shipment_id": 1, "movement_id": 2}]
        p.map[("shipments", 1)] = DISCARD
        p.map[("movements", 2)] = NEW
        p.insertable_ids["shipment_movements"].add(5)
        p.match_shipment_movements()
        self.assertEqual(p.status[("shipment_movements", 5)], "blocked")

    def test_load_children_brings_links_of_new_shipments(self):
        p = self.make()
        rows = {
            "shipment_items": [{"id": 1, "shipment_id": 10, "movement_item_id": 100}],
            "movement_items": [{"id": 100, "movement_id": 20}],
            "shipments": [{"id": 10}, {"id": 99}],
            "shipment_movements": [
                {"id": 1, "shipment_id": 10, "movement_id": 20},
                {"id": 2, "shipment_id": 99, "movement_id": 30},
            ],
        }
        p.insertable_ids["movements"].add(20)
        p.load_children(rows)
        self.assertEqual([r["id"] for r in p.rows["shipment_movements"]], [1])

    def test_mark_insertable_marks_links_of_new_shipments(self):
        p = self.make()
        p.rows["movements"] = [{"id": 20}]
        p.rows["movement_items"] = [{"id": 100, "movement_id": 20}]
        p.rows["shipment_items"] = [{"id": 1, "shipment_id": 10, "movement_item_id": 100}]
        p.rows["shipments"] = [{"id": 10}]
        p.rows["shipment_movements"] = [
            {"id": 1, "shipment_id": 10, "movement_id": 20},
            {"id": 2, "shipment_id": 99, "movement_id": 20},
        ]
        p.map[("movements", 20)] = NEW
        mark = mark_insertable(p)
        self.assertEqual(mark["shipment_movements"], {1})


class WindowTests(unittest.TestCase):
    def write_delta(self, entries):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(entries, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_window_is_inclusive_and_updated_only_is_out_of_scope(self):
        entries = [
            {"tabela": "movements", "registro_json": {"id": 1, "created_at": "2026-11-04 00:00:00"}},
            {"tabela": "movements", "registro_json": {"id": 2, "created_at": "2026-11-12 23:59:59"}},
            {"tabela": "movements", "registro_json": {"id": 3, "created_at": "2026-11-13 00:00:00"}},
            {"tabela": "movements", "registro_json": {"id": 4, "created_at": "2026-10-01", "updated_at": "2026-11-06"}},
            {"tabela": "audit_logs", "registro_json": {"action": "update"}},
        ]
        _, rows, updated_only, excluded, audit = load_delta(
            self.write_delta(entries), date(2026, 11, 4), date(2026, 11, 12)
        )
        self.assertEqual([r["id"] for r in rows["movements"]], [1, 2])
        self.assertEqual(updated_only["movements"], 1)
        self.assertEqual(excluded["movements"], 1)
        self.assertEqual(audit["update"], 1)


if __name__ == "__main__":
    unittest.main()
