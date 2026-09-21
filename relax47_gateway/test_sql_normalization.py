from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_runtime_cutover as fixture
from runtime_migration import STORES
from sql_normalization import normalization_plan


class NormalizationTests(unittest.TestCase):
    setUp = fixture.CutoverTests.setUp
    migrate = fixture.CutoverTests.migrate
    args = fixture.CutoverTests.args
    def data_setup(self):
        self.migrate()
        for key in STORES:
            self.write(key, {})
        self.write(STORES[0], {'entries': [{'id': 's1', 'check_in': '2026-09-22 14:00',
                                          'check_out': '2026-09-23 12:00', 'guest': 'PRIVATE'}]})
        self.write(STORES[1], {'vehicles': [{'id': 'v1', 'plate': 'SECRET-PLATE'}],
                              'requests': [{'id': 'r1', 'stay_id': 's1', 'vehicle_id': 'v1'}],
                              'passes': [{'id': 'p1', 'stay_id': 's1', 'vehicle_id': 'v1', 'request_id': 'r1'}],
                              'gate_events': [{'id': 'e1', 'vehicle_id': 'v1', 'pass_id': 'p1'}],
                              'queue': [{'command': 'PRIVATE-COMMAND', 'status': 'queued'}]})

    def write(self, key, data):
        payload = json.dumps({'key': key, 'version': 1, 'data': data})
        with closing(sqlite3.connect(self.manager.database_path)) as conn, conn:
            conn.execute("UPDATE runtime_documents SET payload_json=? WHERE namespace=? AND item_key='ha_store'", (payload,key))

    def snapshot(self):
        with closing(sqlite3.connect(self.manager.database_path)) as conn:
            return '\n'.join(conn.iterdump())

    def test_audit_reads_current_sql_without_writes_or_private_values(self):
        self.data_setup()
        before = self.snapshot()
        callbacks = self.manager.audit_callback.call_count
        plan = normalization_plan(self.manager)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(callbacks, self.manager.audit_callback.call_count)
        self.assertEqual(plan['findings'], {})
        self.assertFalse(plan['cutover_ready'])
        self.assertEqual(plan['inventory'][1]['collections']['queue']['count'], 1)
        self.assertEqual(plan['target_counts']['stays'], 0)
        for value in ['PRIVATE', 'SECRET-PLATE', 'PRIVATE-COMMAND']:
            self.assertNotIn(value, json.dumps(plan))
        self.assertEqual(plan['plan_sha256'], normalization_plan(self.manager)['plan_sha256'])
        self.write(STORES[0], {'entries': []})
        self.assertNotEqual(plan['plan_sha256'], normalization_plan(self.manager)['plan_sha256'])

    def test_conflicts_and_missing_relations_are_not_silently_repaired(self):
        self.data_setup()
        self.write(STORES[0], {'entries': [
            {'id': 's1', 'check_in': 'bad', 'check_out': 'bad'},
            {'id': 's1', 'check_in': '2026-09-23', 'check_out': '2026-09-22'},
            {'check_in': '2026-09-22', 'check_out': '2026-09-23'}],
            'current_stay': {'id': 's1', 'guest': 'DIFFERENT'}})
        self.write(STORES[1], {'vehicles': [], 'requests': [{'id': 'r', 'stay_id': 'absent', 'vehicle_id': []}],
                              'passes': [{'id': 'p'}]})
        f = normalization_plan(self.manager)['findings']
        for name in ['duplicate_stay_ids', 'missing_stay_ids', 'invalid_stay_dates',
                     'nonpositive_stay_periods', 'current_stay_requires_reconciliation',
                     'requests_unresolved_vehicle_id', 'requests_unresolved_stay_id',
                     'passes_missing_request_id']:
            self.assertEqual(f[name], 1)

    def test_rejects_oversized_envelope_and_symlink(self):
        self.data_setup()
        with patch('sql_normalization.MAX_STORE_BYTES', 10):
            with self.assertRaisesRegex(ValueError, 'Oversized'):
                normalization_plan(self.manager)
        path = self.manager.database_path
        other = path.with_suffix('.other')
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(PermissionError):
            normalization_plan(self.manager)

    def test_rejects_nonfinite_and_duplicate_json_keys(self):
        self.data_setup()
        for payload in ['{"key":"x","key":"y"}', '{"x":NaN}']:
            with closing(sqlite3.connect(self.manager.database_path)) as conn, conn:
                # CHECK(json_valid()) rejects NaN before the audit; duplicate keys
                # are valid SQLite JSON and must be caught by the strict decoder.
                if 'NaN' in payload:
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute("UPDATE runtime_documents SET payload_json=? WHERE item_key='ha_store'", (payload,))
                else:
                    conn.execute("UPDATE runtime_documents SET payload_json=? WHERE item_key='ha_store'", (payload,))
            with self.assertRaises(ValueError):
                normalization_plan(self.manager)

    def test_dst_ambiguous_date_requires_review(self):
        self.data_setup()
        with closing(sqlite3.connect(self.manager.database_path)) as conn, conn:
            conn.execute("UPDATE properties SET timezone='Europe/Berlin'")
        self.write(STORES[0], {'entries': [{'id': 's', 'check_in': '2026-10-25 02:30', 'check_out': '2026-10-26 12:00'}]})
        self.assertEqual(normalization_plan(self.manager)['findings']['invalid_stay_dates'], 1)


if __name__ == '__main__':
    unittest.main()
