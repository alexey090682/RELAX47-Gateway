from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from sql_migration import Relax47SQLManager
from runtime_migration import STORES, _read_store, prepare_runtime
from backup_retention import BackupRetention


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.manager = Relax47SQLManager(data_dir=self.root/'data', config_dir=self.root/'config',
            backup_callback=lambda reason: {'verified': True}, audit_callback=Mock(return_value={}),
            stop_callback=lambda: self.calls.append('stop'), start_callback=lambda: self.calls.append('start'))
        self.storage = self.root/'config/.storage'
        self.storage.mkdir(parents=True)
        for key in STORES:
            (self.storage/key).write_text(json.dumps({'version': 1, 'key': key, 'unknown': ['keep', 1],
                'data': {'secret': 'do-not-expose', 'queue': [{'state': 'done'}]}}, indent=2))
        self.args = {'stop_home_assistant': True, 'confirm_other_writers_stopped': True}

    def test_capture_roundtrip_private_and_not_active(self):
        original = {key: (self.storage/key).read_bytes() for key in STORES}
        result = prepare_runtime(self.manager, self.args, 'test')
        self.assertEqual(result['source_count'], 8)
        self.assertFalse(result['active_backend_changed'])
        self.assertNotIn('do-not-expose', json.dumps(result))
        self.assertNotIn('do-not-expose', str(self.manager.audit_callback.call_args))
        self.assertFalse(self.manager.database_path.exists())
        candidate = self.manager.data_dir/'sql_candidates'/f"{result['candidate_id']}.db"
        self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(candidate)) as conn:
            for key, raw in original.items():
                stored = conn.execute('SELECT payload_json FROM runtime_documents WHERE namespace=?', (key,)).fetchone()[0]
                self.assertEqual(json.loads(stored).encode(), raw)
                self.assertEqual((self.storage/key).read_bytes(), raw)
        self.assertEqual(self.calls, ['stop', 'start'])

    def test_refuses_no_window_and_unverified_backup(self):
        with self.assertRaises(PermissionError):
            prepare_runtime(self.manager, {}, 'test')
        self.manager.backup_callback = lambda reason: {'verified': False}
        with self.assertRaises(RuntimeError):
            prepare_runtime(self.manager, self.args, 'test')
        self.assertEqual(self.calls, [])

    def test_missing_required_stops_capture_and_restarts(self):
        (self.storage/STORES[0]).unlink()
        with self.assertRaises(ValueError):
            prepare_runtime(self.manager, self.args, 'test')
        self.assertEqual(self.calls, ['stop', 'start'])

    def test_missing_optional_is_reported(self):
        (self.storage/STORES[-1]).unlink()
        result = prepare_runtime(self.manager, self.args, 'test')
        self.assertEqual(result['missing_stores'], [STORES[-1]])

    def test_denies_nonallowlisted_and_symlink(self):
        with self.assertRaises(PermissionError):
            _read_store(self.manager.config_dir, 'auth')
        (self.storage/STORES[0]).unlink()
        (self.storage/STORES[0]).symlink_to(self.storage/STORES[1])
        with self.assertRaises(OSError):
            prepare_runtime(self.manager, self.args, 'test')
        self.assertEqual(self.calls[-1], 'start')

    def test_duplicate_json_keys_rejected(self):
        (self.storage/STORES[0]).write_text('{"data":{},"data":[]}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            prepare_runtime(self.manager, self.args, 'test')

    def test_source_changed_candidate_removed(self):
        import runtime_migration
        real = runtime_migration._read_store
        counts = {}
        def changing(root, key):
            counts[key] = counts.get(key, 0) + 1
            result = real(root, key)
            if counts[key] == 2 and key == STORES[0]:
                return result[0] + b' ', result[1]
            return result
        with patch('runtime_migration._read_store', changing), self.assertRaisesRegex(RuntimeError, 'Source changed'):
            prepare_runtime(self.manager, self.args, 'test')
        self.assertEqual(list((self.manager.data_dir/'sql_candidates').glob('*.db')), [])
        self.assertEqual(self.calls[-1], 'start')


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = Mock(data_dir=Path(self.tmp.name))
        self.manager.cleanup_backups.return_value = {'deleted_count': 2, 'failed': []}
        self.retention = BackupRetention(self.manager, lambda: True)

    def test_opt_in_daily_persisted_and_disable(self):
        self.retention.tick()
        self.manager.cleanup_backups.assert_not_called()
        with patch('backup_retention.time.time', return_value=100):
            self.retention.configure(True, 'user approved')
        other = BackupRetention(self.manager, lambda: True)
        with patch('backup_retention.time.time', return_value=101):
            other.tick()
            self.manager.cleanup_backups.assert_not_called()
        with patch('backup_retention.time.time', return_value=86501):
            other.tick()
            other.tick()
        self.manager.cleanup_backups.assert_called_once()
        self.assertEqual(self.manager.cleanup_backups.call_args.args[0]['keep_last'], 3)
        other.configure(False, 'disabled')
        with patch('backup_retention.time.time', return_value=999999):
            other.tick()
        self.assertEqual(self.manager.cleanup_backups.call_count, 1)

    def test_readonly_mode_blocks_cleanup(self):
        self.retention.configure(True, 'test')
        with patch('backup_retention.time.time', return_value=99999999999):
            BackupRetention(self.manager, lambda: False).tick()
        self.manager.cleanup_backups.assert_not_called()


if __name__ == '__main__':
    unittest.main()
