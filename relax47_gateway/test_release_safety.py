"""Offline tests: real SQLite databases, simulated Supervisor only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from maintenance import MaintenanceManager
from sql_migration import Relax47SQLManager, TARGET, _sha


def database(path, value="old", version=9):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("CREATE TABLE relax47_meta(key TEXT PRIMARY KEY,value TEXT);"
                           "CREATE TABLE stays(id TEXT PRIMARY KEY);"
                           "CREATE TABLE passes(id TEXT PRIMARY KEY,stay_id TEXT REFERENCES stays(id));")
        conn.execute("INSERT INTO relax47_meta VALUES('schema_version',?)", (str(version),))
        conn.execute("INSERT INTO stays VALUES(?)", (value,))
        conn.execute("INSERT INTO passes VALUES('pass',?)", (value,))
        conn.commit()


class SQLSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.manager = Relax47SQLManager(data_dir=self.root/'data', config_dir=self.root/'config',
            backup_callback=lambda reason: self.calls.append('backup') or {'verified': True},
            audit_callback=lambda **kw: {'change_id': 'test'},
            stop_callback=lambda: self.calls.append('stop'), start_callback=lambda: self.calls.append('start'))
        uid = uuid.uuid4().hex
        self.source = self.root/'data/config_staged'/f'{uid}-new.db'
        database(self.source, 'new')
        self.meta_path = self.root/'data/maintenance_uploads'/f'{uid}.json'
        self.meta_path.parent.mkdir(parents=True)
        self.meta = {'upload_id':uid,'kind':'config','state':'staged_config','filename':'new.db',
                     'staged_path':str(self.source),'target_path':TARGET,
                     'sha256':_sha(self.source),'size_bytes':self.source.stat().st_size}
        self.write_meta()
        self.args = {'upload_id':uid,'expected_sha256':self.meta['sha256'],
                     'stop_home_assistant':True,'confirm_other_writers_stopped':True}

    def write_meta(self):
        self.meta_path.write_text(json.dumps(self.meta))

    def value(self, path=None):
        with closing(sqlite3.connect(path or self.manager.database_path)) as c:
            return c.execute('SELECT id FROM stays').fetchone()[0]

    def test_install_retains_previous_and_inode(self):
        database(self.manager.database_path)
        inode = self.manager.database_path.stat().st_ino
        result = self.manager.install_upload(self.args,'test')
        self.assertEqual(self.value(),'new')
        self.assertEqual(self.manager.database_path.stat().st_ino,inode)
        self.assertEqual(self.value(self.manager.backup_dir/result['backup_id']),'old')
        self.assertEqual(self.calls,['backup','stop','start'])

    def test_first_install(self):
        self.manager.install_upload(self.args,'test')
        self.assertEqual(self.value(),'new')

    def test_failed_verification_rolls_back(self):
        database(self.manager.database_path)
        inspect = self.manager._inspect
        failures = []
        def fail_once(path):
            if path == self.manager.database_path and not failures:
                failures.append(True)
                raise RuntimeError('injected validation failure')
            return inspect(path)
        self.manager._inspect = fail_once
        with self.assertRaisesRegex(RuntimeError,'injected'):
            self.manager.install_upload(self.args,'test')
        self.assertEqual(self.value(),'old')
        self.assertEqual(self.calls[-1],'start')

    def test_first_install_failure_removes_incomplete_database(self):
        self.manager.audit_callback = Mock(side_effect=RuntimeError('audit unavailable'))
        with self.assertRaisesRegex(RuntimeError,'audit'):
            self.manager.install_upload(self.args,'test')
        self.assertFalse(self.manager.database_path.exists())

    def test_checksum_and_confirmation_fail_before_backup(self):
        for overrides in ({'expected_sha256':'0'*64},{'stop_home_assistant':False},
                          {'confirm_other_writers_stopped':False}):
            with self.subTest(overrides=overrides), self.assertRaises((ValueError,PermissionError)):
                self.manager.install_upload({**self.args,**overrides},'test')
        self.assertEqual(self.calls,[])

    def test_unverified_backup_aborts(self):
        self.manager.backup_callback = lambda reason: {'verified':False}
        with self.assertRaisesRegex(RuntimeError,'verified'):
            self.manager.install_upload(self.args,'test')
        self.assertEqual(self.calls,[])

    def test_rejects_upload_path_escape(self):
        self.meta['staged_path'] = str(self.root/'outside.db')
        self.write_meta()
        with self.assertRaises(PermissionError):
            self.manager.inspect_upload(self.args['upload_id'])

    def test_rejects_symlink_target(self):
        outside = self.root/'outside'
        outside.mkdir()
        (self.root/'config').mkdir()
        (self.root/'config/relax47_v8').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.manager.database_status()

    def test_rejects_unknown_schema_and_foreign_key_violation(self):
        for version in (0,12):
            path = self.root/f'schema-{version}.db'
            database(path,version=version)
            with self.assertRaises(ValueError):
                self.manager._inspect(path)
        with closing(sqlite3.connect(self.source)) as c:
            c.execute("UPDATE passes SET stay_id='missing'")
            c.commit()
        with self.assertRaisesRegex(ValueError,'foreign_key'):
            self.manager._inspect(self.source)

    def test_status_includes_wal_rows(self):
        database(self.manager.database_path)
        with closing(sqlite3.connect(self.manager.database_path)) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute("INSERT INTO stays VALUES('wal-row')")
            c.commit()
            result = self.manager.database_status()
            self.assertEqual(next(o['row_count'] for o in result['objects'] if o['name']=='stays'),2)
            self.assertEqual(result['sha256_scope'],'sqlite_snapshot')

    def canonical_database(self, version):
        path = self.root / f'canonical-{version}-{uuid.uuid4().hex}.db'
        schema = Path(__file__).parent / 'sql_schemas' / f'schema-{version}.sql'
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(schema.read_text())
        return path

    def stage(self, path):
        self.manager._copy_database(path, self.source)
        self.meta.update(sha256=_sha(self.source), size_bytes=self.source.stat().st_size)
        self.write_meta()
        self.args['expected_sha256'] = self.meta['sha256']

    def test_canonical_preservation_and_media_schemas(self):
        for version in (10, 11):
            with self.subTest(version=version):
                path = self.canonical_database(version)
                result = self.manager._inspect(path)
                self.assertEqual(result['schema_version'], version)
                self.assertEqual(result['schema_validation'], 'canonical_contract')
                self.stage(path)
                result = self.manager.install_upload(self.args, 'schema test')
                self.assertEqual(result['schema_version'], version)

    def test_schema_number_alone_does_not_prove_compatibility(self):
        for version in (10, 11):
            path = self.root / f'fake-{version}.db'
            database(path, version=version)
            with self.assertRaisesRegex(ValueError, 'missing required'):
                self.manager._inspect(path)

    def test_changed_contract_and_missing_objects_fail(self):
        for statement in (
                'DROP VIEW system_journal',
                'DROP INDEX idx_stay_video_jobs_due',
                'ALTER TABLE stay_video_jobs ADD COLUMN unexpected TEXT'):
            with self.subTest(statement=statement):
                path = self.canonical_database(11)
                with closing(sqlite3.connect(path)) as conn:
                    conn.execute(statement)
                    conn.commit()
                with self.assertRaisesRegex(ValueError, 'Schema 11:'):
                    self.manager._inspect(path)

    def test_extra_tables_are_retained(self):
        path = self.canonical_database(11)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("CREATE TABLE extension_data(value TEXT);"
                               "INSERT INTO extension_data VALUES('keep me');")
        self.stage(path)
        self.manager.install_upload(self.args, 'extension preservation')
        with closing(sqlite3.connect(self.manager.database_path)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM extension_data').fetchone()[0], 'keep me')

    def test_invalid_composite_foreign_key_contract(self):
        path = self.canonical_database(11)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('DROP INDEX ux_stays_property_id')
        with self.assertRaisesRegex(ValueError, 'foreign_key_check'):
            self.manager._inspect(path)

    def test_schema_11_audit_failure_restores_schema_10(self):
        self.stage(self.canonical_database(10))
        self.manager.install_upload(self.args, 'baseline')
        self.stage(self.canonical_database(11))
        self.manager.audit_callback = Mock(side_effect=RuntimeError('audit failed'))
        with self.assertRaisesRegex(RuntimeError, 'audit failed'):
            self.manager.install_upload(self.args, 'upgrade')
        self.assertEqual(self.manager.database_status()['schema_version'], 10)
        self.assertEqual(self.calls[-1], 'start')

    def test_downgrade_rejected_without_replacing_target(self):
        self.stage(self.canonical_database(11))
        self.manager.install_upload(self.args, 'first install')
        self.stage(self.canonical_database(10))
        with self.assertRaisesRegex(ValueError, 'downgrade'):
            self.manager.install_upload(self.args, 'unsafe downgrade')
        self.assertEqual(self.manager.database_status()['schema_version'], 11)
        self.assertEqual(self.calls[-1], 'start')

    def test_status_advertises_support_before_install(self):
        result = self.manager.database_status()
        self.assertFalse(result['installed'])
        self.assertEqual(result['supported_schema_version'], 11)


class MaintenanceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manager = MaintenanceManager(data_dir=root/'data',config_dir=root/'config',share_dir=root/'share',
            supervisor_token='test',current_version='7.14.9',
            backup_callback=Mock(),audit_callback=lambda **kw: {'change_id':'test'})
        self.deleted = []
        self.rows = [{'slug':f'backup{i}','name':'ordinary','date':f'2026-09-{i:02d}T00:00:00+00:00','protected':i==1} for i in range(1,7)]
        self.rows[1]['name'] = 'NEVER DELETE preserved'
        def api(path, **kw):
            if kw.get('method') == 'DELETE':
                self.deleted.append(path.rsplit('/',1)[-1]); return {}
            return {'backups':self.rows}
        self.manager._supervisor_api = api

    def test_default_dry_run_and_minimum_retention(self):
        result = self.manager.cleanup_backups({},'test')
        self.assertEqual(result['would_delete_count'],1)
        self.assertEqual(self.deleted,[])
        with self.assertRaises(ValueError):
            self.manager.cleanup_backups({'keep_last':2},'test')

    def test_preserves_three_latest_protected_and_named(self):
        self.manager.cleanup_backups({'dry_run':False},'test')
        self.assertEqual(self.deleted,['backup3'])

    def test_repository_update_cannot_restore_local_gateway(self):
        self.manager._supervisor_api = Mock(return_value={'slug':'repo_relax47_gateway'})
        with self.assertRaises(PermissionError):
            self.manager.apply_gateway_update({'backup_slug':'abcd','expected_version':'7.14.9'},'test')
        self.manager._supervisor_api.assert_called_once_with('/addons/self/info',timeout=30)

    def test_generic_config_write_rejects_sql(self):
        self.manager._meta = lambda uid: {'kind':'config','state':'staged_config','target_path':TARGET,'filename':'new.db'}
        with self.assertRaises(PermissionError):
            self.manager.apply_config_upload({'upload_id':'a'*32},'test')


if __name__ == '__main__':
    unittest.main()
