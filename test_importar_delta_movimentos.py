import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from importar_delta_movimentos import (
    NEW,
    Planner,
    creation_date,
    equipment_choice,
    load_delta,
    normalize,
    normalize_number,
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
        p.rows = {"movement_items": [{"id": 10, "movement_id": 1}], "movements": [{"id": 1}]}
        p.plan_new_parents("movements")
        p.plan_children("movement_items", "movements", "movement_id")
        self.assertEqual(p.status[("movement_items", 10)], "new")

    def test_child_of_missing_parent_is_blocked(self):
        p = self.make(fks={"movement_items": {"movement_id": "movements"}},
                      nullables={"movement_items": set()})
        p.rows = {"movement_items": [{"id": 10, "movement_id": 1}]}
        p.plan_children("movement_items", "movements", "movement_id")
        self.assertEqual(p.status[("movement_items", 10)], "blocked")

    def test_product_item_code_collision_blocks(self):
        p = self.make(lookups={"product_items": [
            {"id": 1, "code": "X", "deleted_at": None},
            {"id": 2, "code": "X", "deleted_at": None},
        ]})
        p.rows = {"product_items": [{"id": 9, "code": "X"}]}
        p.plan_product_items()
        self.assertEqual(p.status[("product_items", 9)], "blocked")


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
