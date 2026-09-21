import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ha_sql_store import Store
from runtime_cutover import preflight, migrate_runtime, runtime_status, NEW_IMPORT, HELPER
from runtime_migration import STORES
from sql_migration import Relax47SQLManager


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'config'
        self.calls = []
        self.manager = Relax47SQLManager(data_dir=self.root/'data', config_dir=self.config,
            backup_callback=Mock(return_value={'verified': True}), audit_callback=Mock(),
            stop_callback=lambda: self.calls.append('stop'), start_callback=lambda: self.calls.append('start'))
        self.storage = self.config/'.storage'
        self.storage.mkdir(parents=True)
        self.data = {'stays': [{'id': 'stay-1', 'rating': 5, 'date': '2026-09-22 14:00'}],
                     'violations': [{'photo': '/local/private/violation.jpg'}],
                     'queue': [{'status': 'queued', 'attempts': 2}],
                     'unrecognized': {'keep': [True, None, 123]}, 'secret': 'PRIVATE-NOT-FOR-LOGS'}
        for key in STORES:
            envelope = {'key': key, 'version': 1, 'minor_version': 1, 'unknown_envelope': 'preserve', 'data': self.data}
            (self.storage/key).write_text(json.dumps(envelope, ensure_ascii=False))
            folder = self.config/'custom_components'/key.split('.')[0]
            folder.mkdir(parents=True)
            (folder/'__init__.py').write_text(
                "from homeassistant.helpers.storage import Store\nSTORE_VERSION = 1\nSTORE_KEY = " + repr(key) +
                "\nasync def setup(hass):\n    store = Store(hass, STORE_VERSION, STORE_KEY)\n"
                "    data = await store.async_load()\n    await store.async_save(data)\n")
        self.originals = {p: p.read_bytes() for p in self.config.rglob('*') if p.is_file()}
        self.check = Mock()

    def args(self):
        return {'expected_plan_sha256': preflight(self.manager)['plan_sha256'],
                'stop_home_assistant': True, 'confirm_other_writers_stopped': True}

    def migrate(self, args=None):
        return migrate_runtime(self.manager, args or self.args(), 'test', 'Europe/Moscow', self.check)

    def hass(self):
        async def executor(fn, *args):
            return await asyncio.to_thread(fn, *args)
        return SimpleNamespace(config=SimpleNamespace(path=lambda *p: str(self.config.joinpath(*p))),
                               async_add_executor_job=executor)

    def test_migration_load_save_restart_preserves_full_payload(self):
        result = self.migrate()
        self.assertTrue(result['roundtrip_verified'])
        self.assertEqual(self.calls, ['stop', 'start'])
        self.assertEqual(self.check.call_count, 2)
        self.assertNotIn('PRIVATE-NOT-FOR-LOGS', json.dumps(result))
        self.assertNotIn('PRIVATE-NOT-FOR-LOGS', str(self.manager.audit_callback.call_args))
        self.assertEqual(self.manager.database_path.stat().st_mode & 0o777, 0o600)
        async def exercise():
            for key in STORES:
                store = Store(self.hass(), 1, key)
                loaded = await store.async_load()
                self.assertEqual(loaded, self.data)
                loaded['new_field'] = {'n': 42}
                await store.async_save(loaded)
                restarted = Store(self.hass(), 1, key)
                self.assertEqual(await restarted.async_load(), loaded)
                self.assertEqual(restarted.envelope['unknown_envelope'], 'preserve')
                self.assertEqual((self.storage/key).read_bytes(), self.originals[self.storage/key])
        asyncio.run(exercise())
        status = runtime_status(self.manager)
        self.assertEqual(status['backend'], 'sqlite_store_v1')
        self.assertEqual(len(status['patched_components']), 8)
        self.assertTrue(all(s['revision'] == 2 for s in status['stores']))
        with closing(sqlite3.connect(self.manager.database_path)) as conn:
            self.assertEqual(conn.execute('SELECT timezone FROM properties').fetchone()[0], 'Europe/Moscow')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_stale_writer_is_rejected(self):
        self.migrate()
        async def exercise():
            a, b = Store(self.hass(), 1, STORES[0]), Store(self.hass(), 1, STORES[0])
            await a.async_load()
            await b.async_load()
            await a.async_save({'winner': True})
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                await b.async_save({'winner': False})
            self.assertEqual(await b.async_load(), {'winner': True})
        asyncio.run(exercise())

    def test_missing_sql_fails_closed_without_recreating_or_json_fallback(self):
        self.migrate()
        self.manager.database_path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            asyncio.run(Store(self.hass(), 1, STORES[0]).async_load())
        self.assertFalse(self.manager.database_path.exists())

    def test_stale_plan_rejected_before_backup(self):
        args = self.args()
        p = next((self.config/'custom_components').rglob('__init__.py'))
        p.write_bytes(p.read_bytes()+b'\n# changed\n')
        with self.assertRaises(ValueError):
            self.migrate(args)
        self.manager.backup_callback.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_unknown_version_rolls_back_and_preserves_originals(self):
        path = self.storage/STORES[0]
        doc = json.loads(path.read_text()); doc['version'] = 2
        path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'version'):
            self.migrate()
        self.assertFalse(self.manager.database_path.exists())
        self.assertEqual(self.calls, ['stop', 'start'])
        self.assertEqual(json.loads(path.read_text())['version'], 2)

    def test_config_failure_restores_all_code_removes_sql_and_helpers(self):
        self.check.side_effect = [None, RuntimeError('check failed')]
        with self.assertRaises(RuntimeError):
            self.migrate()
        for path, raw in self.originals.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(list(self.config.rglob(HELPER)), [])
        self.assertFalse(self.manager.database_path.exists())
        self.assertEqual(self.calls[-1], 'start')
        self.assertTrue(preflight(self.manager)['ready'])

    def test_restart_failure_keeps_committed_sql_and_adapters(self):
        self.manager.start_callback = Mock(side_effect=RuntimeError('start failed'))
        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            self.migrate()
        self.assertTrue(self.manager.database_path.exists())
        self.assertEqual(len(runtime_status(self.manager)['patched_components']), 8)

    def test_existing_database_never_overwritten(self):
        args = self.args()
        path = self.manager.database_path
        path.parent.mkdir(); path.write_bytes(b'EXISTING')
        with self.assertRaises(ValueError):
            self.migrate(args)
        self.assertEqual(path.read_bytes(), b'EXISTING')
        self.assertEqual(self.calls, [])

    def test_source_mutation_aborts(self):
        import runtime_cutover
        real = runtime_cutover._read_store
        calls = {}
        def changing(root, key):
            calls[key] = calls.get(key, 0)+1
            source = real(root, key)
            return (source[0]+b' ', source[1]) if calls[key] == 2 else source
        with patch('runtime_cutover._read_store', changing), self.assertRaisesRegex(RuntimeError, 'changed'):
            self.migrate()
        self.assertFalse(self.manager.database_path.exists())

    def test_optional_missing_store_initializes_sql_without_losing_other_data(self):
        (self.storage/STORES[-1]).unlink()
        result = self.migrate()
        self.assertEqual(result['source_count'], 7)
        async def exercise():
            store = Store(self.hass(), 1, STORES[-1])
            self.assertIsNone(await store.async_load())
            await store.async_save({'initialized': True})
            self.assertEqual(await Store(self.hass(), 1, STORES[-1]).async_load(), {'initialized': True})
        asyncio.run(exercise())

    def test_failed_rollback_does_not_restart_core(self):
        import runtime_cutover
        write = runtime_cutover.atomic_write
        failed_check = False
        def check():
            nonlocal failed_check
            if self.check.call_count:
                failed_check = True
                raise RuntimeError('check failed')
            self.check()
        def broken_rollback(path, raw, mode=0o600):
            if failed_check and path.name == '__init__.py':
                raise OSError('disk failed')
            return write(path, raw, mode)
        with patch('runtime_cutover.atomic_write', broken_rollback), self.assertRaisesRegex(RuntimeError, 'rollback failed'):
            migrate_runtime(self.manager, self.args(), 'test', 'Europe/Moscow', check)
        self.assertEqual(self.calls, ['stop'])


if __name__ == '__main__':
    unittest.main()
