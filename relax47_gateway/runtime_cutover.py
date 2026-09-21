"""Bounded initial migration of eight existing HA Stores to SQLite storage.

Not a normalized V8 business migration, nor an import of HA recorder history.
Only fixed code paths and one exact Store import can be changed.
"""
import ast
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from runtime_migration import STORES, REQUIRED, _read_store

OLD_IMPORT = b'from homeassistant.helpers.storage import Store'
NEW_IMPORT = b'from ._relax47_sql_store import Store'
HELPER = '_relax47_sql_store.py'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, raw, mode=0o600):
    tmp = path.with_name('.sql-' + uuid.uuid4().hex)
    try:
        with tmp.open('xb') as stream:
            os.chmod(tmp, mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def component_path(root, key):
    path = root / 'custom_components' / key.split('.')[0] / '__init__.py'
    for part in (root/'custom_components', path.parent, path):
        if part.is_symlink():
            raise PermissionError('Component symlinks cannot be migrated')
    if not path.resolve().is_relative_to(root.resolve()):
        raise PermissionError('Component path escaped configuration')
    return path


def checked_code(root, key):
    path = component_path(root, key)
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024 or raw.count(OLD_IMPORT) != 1 or NEW_IMPORT in raw:
        raise ValueError('Unsupported or already patched component: ' + key)
    tree = ast.parse(raw)
    values = {node.targets[0].id: node.value.value for node in tree.body
              if isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
    if values.get('STORE_KEY') != key or values.get('STORE_VERSION') != 1:
        raise ValueError('Unsupported Store identity/version: ' + key)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'Store']
    if len(calls) != 1 or calls[0].keywords or [ast.unparse(n) for n in calls[0].args] != ['hass', 'STORE_VERSION', 'STORE_KEY']:
        raise ValueError('Unsupported Store constructor: ' + key)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            receiver = node.value
            is_store = (isinstance(receiver, ast.Name) and receiver.id == 'store') or (
                isinstance(receiver, ast.Attribute) and receiver.attr == 'store')
            if is_store and node.attr not in {'async_load', 'async_save'}:
                raise ValueError('Unsupported Store method: ' + key)
    helper = path.with_name(HELPER)
    if helper.exists() or helper.is_symlink():
        raise ValueError('Existing SQL adapter requires separate review: ' + key)
    changed = raw.replace(OLD_IMPORT, NEW_IMPORT)
    compile(changed, str(path), 'exec')
    return path, raw, changed


def preflight(manager):
    # No Store contents are read or returned by this diagnostic.
    components = []
    for key in STORES:
        path, raw, _ = checked_code(manager.config_dir, key)
        components.append({'store': key, 'code_sha256': digest(raw)})
    adapter = Path(__file__).with_name('ha_sql_store.py').read_bytes()
    schema = Path(__file__).with_name('sql_schemas').joinpath('schema-11.sql').read_bytes()
    plan = {'components': components, 'adapter_sha256': digest(adapter), 'schema_sha256': digest(schema)}
    installed = manager._target().exists()
    pending = []
    folder = manager.data_dir / 'runtime_cutovers'
    if folder.is_symlink():
        raise PermissionError('Unsafe migration journal directory')
    for path in folder.glob('*/state.json'):
        state = json.loads(path.read_text())
        if state.get('phase') not in {'rolled_back', 'active'}:
            pending.append(path.parent.name)
    return {**plan, 'plan_sha256': digest(json.dumps(plan, sort_keys=True).encode()),
            'ready': not installed and not pending, 'database_exists': installed,
            'incomplete_transactions': pending, 'stop_required': True,
            'normalized_business_rows': False, 'media_files_moved': False}


def _make_database(manager, target, sources, timezone_name):
    target.touch(mode=0o600, exist_ok=False)
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(target)) as conn:
        conn.executescript(Path(__file__).with_name('sql_schemas').joinpath('schema-11.sql').read_text())
        with conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('INSERT INTO properties VALUES(?,?,?,?,?)', ('relax47', 1, timezone_name, now, now))
            conn.execute('INSERT INTO relax47_meta VALUES(?,?)', ('runtime_backend', 'sqlite_store_v1'))
            conn.execute('INSERT INTO relax47_meta VALUES(?,?)', ('migration_stage', 'ha_store_compatibility'))
            for key in STORES:
                source = sources[key]
                if source is None:
                    envelope = {'version': 1, 'minor_version': 1, 'key': key, 'data': None}
                else:
                    raw, envelope = source
                    if envelope['version'] != 1 or envelope.get('minor_version', 1) != 1:
                        raise ValueError('Unsupported source Store version: ' + key)
                    conn.execute('INSERT INTO runtime_documents VALUES(?,?,?,?,?,?)',
                        ('relax47', key, 'source_envelope', 1, now, json.dumps(raw.decode('utf-8'), ensure_ascii=False)))
                payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
                conn.execute('INSERT INTO runtime_documents VALUES(?,?,?,?,?,?)',
                    ('relax47', key, 'ha_store', 1, now, payload))
                restored = conn.execute("SELECT payload_json FROM runtime_documents WHERE namespace=? AND item_key='ha_store'", (key,)).fetchone()[0]
                if json.loads(restored) != envelope:
                    raise RuntimeError('SQL roundtrip mismatch')
                if source is not None:
                    saved = conn.execute("SELECT payload_json FROM runtime_documents WHERE namespace=? AND item_key='source_envelope'", (key,)).fetchone()[0]
                    if json.loads(saved).encode('utf-8') != raw:
                        raise RuntimeError('Source envelope checksum mismatch')
    return manager._inspect(target)


def runtime_status(manager):
    target = manager._target()
    adapters = []
    for key in STORES:
        path = component_path(manager.config_dir, key)
        if path.is_file() and NEW_IMPORT in path.read_bytes():
            adapters.append(key)
    summary = {'installed': target.exists(), 'patched_components': adapters,
               'normalized_business_rows': False, 'runtime_health_verified': False}
    if target.exists():
        with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            row = conn.execute("SELECT value FROM relax47_meta WHERE key='runtime_backend'").fetchone()
            summary['backend'] = row[0] if row and row[0] == 'sqlite_store_v1' else 'other'
            summary['stores'] = [{'store': key, 'revision': revision} for key, revision in conn.execute(
                "SELECT namespace,version FROM runtime_documents WHERE property_id='relax47' AND item_key='ha_store'")
                if key in STORES]
    return summary


def migrate_runtime(manager, args, reason, timezone_name, check_callback):
    if args.get('stop_home_assistant') is not True or args.get('confirm_other_writers_stopped') is not True:
        raise PermissionError('Explicit stopped-writer maintenance window required')
    ZoneInfo(timezone_name)
    if not manager.stop_callback or not manager.start_callback:
        raise RuntimeError('Lifecycle callbacks required')
    manager.data_dir.mkdir(parents=True, exist_ok=True)
    with (manager.data_dir/'sql-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = preflight(manager)
        if not plan['ready'] or args.get('expected_plan_sha256') != plan['plan_sha256']:
            raise ValueError('Fresh reviewed preflight plan required; existing SQL is never overwritten')
        safety = manager.backup_callback('Before RELAX47 runtime SQL migration: ' + reason)
        if not isinstance(safety, dict) or safety.get('verified') is not True:
            raise RuntimeError('Verified HA backup required')
        check_callback()
        target = manager._target()
        original = {}
        helpers = []
        changed = []
        target_created = False
        safe_to_start = True
        journal = None
        try:
            manager.stop_callback()
            current = preflight(manager)
            if current['plan_sha256'] != plan['plan_sha256'] or not current['ready']:
                raise ValueError('Component code changed before cutover')
            sources = {key: _read_store(manager.config_dir, key) for key in STORES}
            if any(sources[key] is None for key in REQUIRED):
                raise ValueError('Required guest journal or pass Store missing')
            folder = manager.data_dir/'runtime_cutovers'
            folder.mkdir(mode=0o700, exist_ok=True)
            folder.chmod(0o700)
            txn = folder/uuid.uuid4().hex
            txn.mkdir(mode=0o700)
            journal = txn/'state.json'
            for key in STORES:
                path, raw, patched = checked_code(manager.config_dir, key)
                original[key] = (path, raw, patched, path.stat().st_mode & 0o777)
                atomic_write(txn/(key+'.py'), raw)
            state = {'phase': 'preparing', 'plan_sha256': plan['plan_sha256'],
                     'stores': list(STORES), 'timezone': timezone_name}
            atomic_write(journal, json.dumps(state).encode())
            candidate = txn/'candidate.db'
            result = _make_database(manager, candidate, sources, timezone_name)
            for key, source in sources.items():
                latest = _read_store(manager.config_dir, key)
                if (latest[0] if latest else None) != (source[0] if source else None):
                    raise RuntimeError('Store changed while Core was stopped')
            state['phase'] = 'applying'
            atomic_write(journal, json.dumps(state).encode())
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.touch(mode=0o600, exist_ok=False)
            target_created = True
            manager._copy_database(candidate, target)
            installed = manager._inspect(target)
            if installed['objects'] != result['objects']:
                raise RuntimeError('Installed SQL row counts differ')
            adapter = Path(__file__).with_name('ha_sql_store.py').read_bytes()
            for key, (path, raw, patched, mode) in original.items():
                if path.read_bytes() != raw:
                    raise RuntimeError('Component changed during cutover')
                helper = path.with_name(HELPER)
                if helper.exists() or helper.is_symlink():
                    raise RuntimeError('Adapter destination changed')
                helpers.append(helper)
                atomic_write(helper, adapter, 0o644)
                changed.append(key)
                atomic_write(path, patched, mode)
                if path.read_bytes() != patched or helper.read_bytes() != adapter:
                    raise RuntimeError('Adapter installation verification failed')
            check_callback()
            summary = {'migration_id': txn.name, 'backend': 'sqlite_store_v1',
                       'source_count': sum(v is not None for v in sources.values()),
                       'store_count': len(STORES), 'roundtrip_verified': True,
                       'timezone': timezone_name, 'normalized_business_rows': False,
                       'media_files_moved': False, 'legacy_sources_preserved': True,
                       'quick_check': 'ok', 'integrity_check': 'ok', 'foreign_key_check': 'ok',
                       'runtime_health_verified': False}
            manager.audit_callback(action='maintenance.sql.runtime_cutover', reason=reason,
                before={'backend': 'ha_store'}, after=summary, verified=True, changed=True)
            state['phase'] = 'active'
            atomic_write(journal, json.dumps(state).encode())
            return summary
        except Exception:
            try:
                for key in reversed(changed):
                    path, raw, _, mode = original[key]
                    atomic_write(path, raw, mode)
                    if path.read_bytes() != raw:
                        raise RuntimeError('Code rollback mismatch')
                for helper in helpers:
                    helper.unlink(missing_ok=True)
                if target_created:
                    target.unlink(missing_ok=True)
                    for suffix in ('-wal', '-shm', '-journal'):
                        Path(str(target)+suffix).unlink(missing_ok=True)
                if journal:
                    atomic_write(journal, json.dumps({'phase': 'rolled_back'}).encode())
            except Exception as exc:
                safe_to_start = False
                raise RuntimeError('Migration rollback failed; Core remains stopped; retain migration journal') from exc
            raise
        finally:
            if safe_to_start:
                # Failure here leaves valid SQL + adapters in place. Never roll back
                # after Core may have started writing new data.
                manager.start_callback()
