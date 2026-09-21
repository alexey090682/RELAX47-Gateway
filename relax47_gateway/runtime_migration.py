"""Explicitly authorized, allowlisted local Store capture; never an active cutover."""
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

STORES = (
    'relax47_guest_journal.runtime', 'relax47_pass_gateway.runtime',
    'relax47_zone_access.runtime', 'relax47_ai.runtime',
    'relax47_stage75.runtime', 'relax47_integrations.runtime',
    'relax47_rbac.roles', 'relax47_realtycalendar.runtime',
)
REQUIRED = frozenset(STORES[:2])
MAX_STORE_BYTES = 32 * 1024 * 1024


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key in source Store')
        result[key] = value
    return result


def _decode(raw):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON value')
    return json.loads(raw, object_pairs_hook=_strict_pairs, parse_constant=invalid_constant)


def _read_store(root, key):
    # No caller-supplied path; no auth/config-entry stores; no symlink following.
    if key not in STORES:
        raise PermissionError('Store is not allowlisted')
    directory = root / '.storage'
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise PermissionError('Unsafe Store directory')
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORE_BYTES:
                raise ValueError('Invalid or oversized Store')
            raw = stream.read(MAX_STORE_BYTES + 1)
            if len(raw) > MAX_STORE_BYTES:
                raise ValueError('Oversized Store')
    finally:
        os.close(directory_fd)
    envelope = _decode(raw)
    if (not isinstance(envelope, dict) or envelope.get('key') != key
            or 'data' not in envelope or type(envelope.get('version')) is not int):
        raise ValueError('Invalid Home Assistant Store envelope')
    return raw, envelope


def prepare_runtime(manager, args, reason):
    """Create a private schema-11 candidate, preserving complete source envelopes.

    Rows are compatibility documents, NOT normalized business rows. Original
    Stores remain authoritative. No data/secret values are returned or logged.
    """
    if args.get('stop_home_assistant') is not True or args.get('confirm_other_writers_stopped') is not True:
        raise PermissionError('Explicit stopped-writer maintenance window required')
    if manager.stop_callback is None or manager.start_callback is None:
        raise RuntimeError('Supervisor lifecycle callbacks unavailable')
    manager.data_dir.mkdir(parents=True, exist_ok=True)
    with (manager.data_dir / 'sql-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        safety = manager.backup_callback('Before local RELAX47 Store capture: ' + reason)
        if not isinstance(safety, dict) or safety.get('verified') is not True:
            raise RuntimeError('Verified safety backup required')
        manager.stop_callback()
        candidate = None
        complete = False
        try:
            sources = {key: _read_store(manager.config_dir, key) for key in STORES}
            if any(sources[key] is None for key in REQUIRED):
                raise ValueError('Required guest journal or pass Store missing')
            folder = manager.data_dir / 'sql_candidates'
            if folder.is_symlink():
                raise PermissionError('Unsafe candidate directory')
            folder.mkdir(mode=0o700, exist_ok=True)
            folder.chmod(0o700)
            candidate_id = uuid.uuid4().hex
            candidate = folder / (candidate_id + '.db')
            candidate.touch(mode=0o600, exist_ok=False)
            now = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(candidate)) as conn:
                conn.executescript((Path(__file__).with_name('sql_schemas') / 'schema-11.sql').read_text())
                conn.execute('PRAGMA foreign_keys=ON')
                # Snapshot-only property. Never assume the production property timezone.
                conn.execute('INSERT INTO properties VALUES(?,?,?,?,?)', ('relax47', 1, 'UTC', now, now))
                conn.execute('INSERT INTO relax47_meta VALUES(?,?)', ('migration_stage', 'raw_store_snapshot_not_active'))
                for key, source in sources.items():
                    if source is None:
                        continue
                    raw, envelope = source
                    # JSON string contains the original UTF-8 text including unknown fields,
                    # key order, whitespace and the full Store version envelope.
                    payload = json.dumps(raw.decode('utf-8'), ensure_ascii=False)
                    conn.execute('INSERT INTO runtime_documents VALUES(?,?,?,?,?,?)',
                                 ('relax47', key, 'source_envelope', 1, now, payload))
                    restored = json.loads(conn.execute(
                        'SELECT payload_json FROM runtime_documents WHERE namespace=? AND item_key=?',
                        (key, 'source_envelope')).fetchone()[0]).encode('utf-8')
                    if restored != raw:
                        raise RuntimeError('Store roundtrip mismatch')
                conn.commit()
            # Detect external writers despite the caller's acknowledgement.
            for key, source in sources.items():
                current = _read_store(manager.config_dir, key)
                if (None if current is None else current[0]) != (None if source is None else source[0]):
                    raise RuntimeError('Source changed during capture; candidate rejected')
            inspection = manager._inspect(candidate)
            summary = {
                'candidate_id': candidate_id, 'schema_version': 11,
                'source_count': sum(source is not None for source in sources.values()),
                'missing_stores': [key for key, source in sources.items() if source is None],
                'roundtrip_verified': True, 'active_backend_changed': False,
                'normalized_business_rows': False, 'media_files_copied': False,
                'sha256': inspection['sha256'], 'row_data_exposed': False,
            }
            manager.audit_callback(action='maintenance.sql.capture_runtime', reason=reason,
                                   before={}, after=summary, verified=True, changed=True)
            complete = True
            return summary
        finally:
            try:
                if candidate is not None and not complete:
                    candidate.unlink(missing_ok=True)
            finally:
                manager.start_callback()
