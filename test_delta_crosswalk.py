import unittest

from transferir_delta_producao import classify_candidates, normalize_key, candidate_for, _window, build_crosswalk


class CrosswalkTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_key(' ab-001 / Á '), 'AB001A')

    def test_only_unique_candidate_is_review_candidate(self):
        row = {'number': 'A-1'}
        self.assertEqual(candidate_for(row, [{'id': 9, 'number': 'a1'}], ['number'])['status'], 'candidate_review')
        self.assertEqual(candidate_for(row, [{'id': 9, 'number': 'a1'}, {'id': 10, 'number': 'A1'}], ['number'])['status'], 'conflict')

    def test_missing_key_pending_and_no_id_match(self):
        self.assertEqual(classify_candidates('', [22])['status'], 'pending')
        # Numeric IDs are not involved in candidate selection.
        self.assertEqual(candidate_for({'id': 4}, [{'id': 4, 'number': 'different'}], ['number'])['status'], 'pending')

    def test_exact_half_open_interval(self):
        self.assertEqual(_window('occurred_at', '2026-11-04', '2026-11-13'), '`occurred_at` >= :start AND `occurred_at` < :end')

    def test_service_order_composite_and_child_parent_mapping(self):
        source = {
            'equipments': [{'id': 1, 'number': 'EQ-1', 'serial_number': 'S1'}],
            'service_orders': [{'id': 20, 'created_at': '2026-11-05', 'movement_type_id': 2, 'recipient_customer_id': 8, 'organization_id': 3, 'user_id': 4}],
            'service_order_items': [{'id': 30, 'service_order_id': 20, 'alias': 'ITEM-A', 'quantity': 1, 'movement_type_id': 2}],
        }
        targets = {
            'equipments': [{'id': 101, 'number': 'eq1', 'serial_number': 'S1'}],
            'service_orders': [{'id': 200, 'created_at': '2026-11-05', 'movement_type_id': 2, 'recipient_customer_id': 8, 'organization_id': 3, 'user_id': 4}],
            'service_order_items': [{'id': 300, 'service_order_id': 200, 'alias': 'ITEM-A', 'quantity': 1, 'movement_type_id': 2}],
        }
        rows = build_crosswalk(source, targets)
        by_source = {r['source_table']: r for r in rows if r['source_table'] in ('service_orders', 'service_order_items')}
        self.assertEqual(by_source['service_orders']['candidate_target_id'], 200)
        self.assertEqual(by_source['service_order_items']['candidate_target_id'], 300)
        self.assertIn('mapped_parent=200', by_source['service_order_items']['target_parent_evidence'])

    def test_ambiguous_service_order_is_not_accepted_for_child(self):
        src = {'service_orders': [{'id': 1, 'created_at': 'd', 'movement_type_id': 1}], 'service_order_items': [{'id': 2, 'service_order_id': 1, 'alias': 'X'}]}
        targets = {'service_orders': [{'id': 10, 'created_at': 'd', 'movement_type_id': 1}, {'id': 11, 'created_at': 'd', 'movement_type_id': 1}], 'service_order_items': []}
        rows = build_crosswalk(src, targets)
        self.assertEqual(rows[0]['status'], 'conflict')
        self.assertEqual(rows[1]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
