"""RELAX47 relational Store compatibility layer, schema 12.

Business rows and item rows are authoritative. Frozen runtime_documents are never
read after cutover and never updated. Complete original envelopes are archived.
No provider calls, entity actions, or queue replay take place in migration.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

ADAPTER_VERSION = 'relational-12.1'
BACKEND = 'sqlite_relational_v1'
PROPERTY = 'relax47'
STORES = (
    'relax47_guest_journal.runtime', 'relax47_pass_gateway.runtime',
    'relax47_zone_access.runtime', 'relax47_ai.runtime',
    'relax47_stage75.runtime', 'relax47_integrations.runtime',
    'relax47_rbac.roles', 'relax47_realtycalendar.runtime',
)
REFS = {'stays': 'stay_id', 'vehicles': 'vehicle_id', 'pass_requests': 'request_id',
        'passes': 'pass_id', 'gate_events': 'gate_id', 'stay_violations': 'violation_id', 'events': 'event_id'}
TABLES = set(REFS)
CORE_FIELDS = {(STORES[0], 'entries'): 'stays', (STORES[1], 'vehicles'): 'vehicles',
               (STORES[1], 'requests'): 'pass_requests', (STORES[1], 'passes'): 'passes',
               (STORES[1], 'gate_events'): 'gate_events', (STORES[3], 'violation_events'): 'stay_violations'}
EVENT_FIELDS = {'system_events', 'vehicle_events', 'events', 'audit', 'webhook_events'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError('Unsafe RELAX47 SQL path')
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA trusted_schema=OFF')
    return conn


def statements(script):
    pending = ''
    for line in script.splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            stmt = pending.strip()
            pending = ''
            if stmt.upper() not in {'BEGIN TRANSACTION;', 'COMMIT;', 'PRAGMA FOREIGN_KEYS=ON;'}:
                yield stmt
    if pending.strip():
        raise ValueError('Incomplete SQL statement')


def check_deployment(path):
    root = Path(path).parent.parent / 'custom_components'
    expected = digest(Path(__file__).read_bytes())
    for key in STORES:
        file = root / key.split('.')[0] / '_relax47_sql_store.py'
        if file.is_symlink() or not file.is_file() or digest(file.read_bytes()) != expected:
            raise RuntimeError('All eight relational adapters must be installed before cutover')


def _upsert(conn, table, values, key='id'):
    if table not in TABLES | {'event_context', 'gate_event_links'}:
        raise ValueError('Unsupported table')
    columns = list(values)
    keys = [key] if isinstance(key, str) else list(key)
    updates = [c for c in columns if c not in keys]
    where = ' OR '.join(f'{table}.{c} IS NOT excluded.{c}' for c in updates)
    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                 f"ON CONFLICT({','.join(keys)}) DO UPDATE SET " + ','.join(f'{c}=excluded.{c}' for c in updates)
                 + ' WHERE ' + where, tuple(values.values()))


def _record_exists(conn, table, identity):
    if table not in TABLES or not isinstance(identity, str) or not identity:
        return False
    column = 'event_id' if table == 'events' else 'id'
    return conn.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (identity,)).fetchone() is not None


def _unresolved(conn, namespace, table, identity, field, raw, importing):
    encoded = dumps(raw)
    previous = conn.execute('SELECT raw_value_json FROM runtime_unresolved_links WHERE namespace=? AND table_name=? AND record_id=? AND field=?',
                            (namespace, table, identity, field)).fetchone()
    if not importing and previous != (encoded,):
        raise ValueError('Unresolved new relational link: ' + table + '.' + field)
    conn.execute('INSERT OR IGNORE INTO runtime_unresolved_links VALUES(?,?,?,?,?,?)',
                 (namespace, table, identity, field, encoded, now()))


def _link(conn, namespace, table, identity, field, raw, target, importing, optional=True):
    if raw in (None, ''):
        if not optional:
            _unresolved(conn, namespace, table, identity, field, raw, importing)
        return None
    if _record_exists(conn, target, raw):
        return raw
    _unresolved(conn, namespace, table, identity, field, raw, importing)
    return None


def _stay_context(conn, namespace, table, identity, raw, importing):
    if raw == 'administrative_passes':
        return None, 'administrative'
    if raw in (None, '', 'unknown'):
        return None, 'unknown'
    stay = _link(conn, namespace, table, identity, 'stay_id', raw, 'stays', importing)
    return stay, 'stay' if stay else 'unresolved'


def _vehicle_link(conn, namespace, table, identity, row, importing):
    raw = row.get('vehicle_id')
    if raw:
        return _link(conn, namespace, table, identity, 'vehicle_id', raw, 'vehicles', importing, False)
    # Only legacy rows may use a uniquely matching exact context. A plate alone
    # is never an identity; duplicate legacy vehicle IDs are retained separately.
    if importing:
        matches = []
        for vid, payload in conn.execute('SELECT id,payload_json FROM vehicles WHERE plate=?', (str(row.get('plate') or ''),)):
            value = json.loads(payload)
            if value.get('stay_id') == row.get('stay_id'):
                matches.append(vid)
        if len(matches) == 1:
            return matches[0]
    # Preserve the resolved ID on later unchanged legacy saves.
    existing = conn.execute(f'SELECT vehicle_id,payload_json FROM {table} WHERE id=?', (identity,)).fetchone()
    if existing and json.loads(existing[1]).get('vehicle_id') == raw:
        return existing[0]
    _unresolved(conn, namespace, table, identity, 'vehicle_id', raw, importing)
    return None


def _identity(conn, namespace, field, item_key, row, table, importing):
    if table == 'events':
        raw_id = row.get('id') or row.get('event_id')
        origin = str(raw_id) if raw_id else digest(dumps(row))
        identity = 'event-' + digest(dumps([namespace, field, origin]))
    else:
        raw_id = row.get('vehicle_id') if table == 'vehicles' else row.get('id')
        if table in {'vehicles', 'pass_requests'} and field in {'vehicles', 'requests'} and not raw_id:
            raw_id = item_key
        if raw_id is not None and (not isinstance(raw_id, str) or not raw_id.strip()):
            raise ValueError('Invalid business identity: ' + table)
        # Missing legacy pass IDs receive a deterministic, persistent identity.
        origin = str(raw_id) if raw_id else dumps([item_key, row.get('created_at')])
        identity = raw_id or ('legacy-' + table + '-' + digest(dumps([namespace, field, origin])))
    existing = conn.execute('SELECT table_name,record_id FROM runtime_record_origins WHERE namespace=? AND field=? AND origin_key=?',
                            (namespace, field, origin)).fetchone()
    if existing and existing != (table, identity):
        raise ValueError('Changed relational identity')
    conn.execute('INSERT OR IGNORE INTO runtime_record_origins VALUES(?,?,?,?,?,?)',
                 (namespace, field, origin, table, identity, int(importing)))
    return identity


def _event(conn, identity, namespace, row, timestamp, *, stay=None, vehicle=None, pass_id=None, category=None):
    kind = str(row.get('event_type') or row.get('action') or row.get('event') or row.get('type') or 'record')
    _upsert(conn, 'events', dict(event_id=identity, event_type=kind,
        occurred_at=str(row.get('occurred_at') or row.get('created_at') or row.get('at') or timestamp),
        correlation_id=str(row.get('correlation_id') or row.get('request_id') or ''),
        actor=str(row.get('actor') or row.get('source') or namespace), schema_version=1, payload_json=dumps(row)), 'event_id')
    severity = row.get('severity', 'info')
    if severity not in {'debug', 'info', 'warning', 'error', 'critical'}:
        severity = 'info'
    _upsert(conn, 'event_context', dict(event_id=identity, property_id=PROPERTY,
        category=category or namespace, severity=severity, stay_id=stay, vehicle_id=vehicle,
        pass_id=pass_id, zone_id=None, sensor_entity_id=row.get('entity_id'), parent_event_id=None,
        received_at=str(row.get('received_at') or timestamp)), 'event_id')


def write_record(conn, namespace, field, item_key, row, table, importing):
    identity = _identity(conn, namespace, field, item_key, row, table, importing)
    payload = dumps(row)
    pk = 'event_id' if table == 'events' else 'id'
    existing = conn.execute(f'SELECT payload_json FROM {table} WHERE {pk}=?', (identity,)).fetchone()
    if existing == (payload,):
        return identity
    stamp = now()
    previous = conn.execute(f'SELECT version,created_at FROM {table} WHERE id=?', (identity,)).fetchone() if table not in {'events','gate_events'} else None
    version = row.get('version')
    version = version if type(version) is int and version > 0 else previous[0]+1 if previous else 1
    common = dict(id=identity, property_id=PROPERTY, version=version,
        created_at=str(row.get('created_at') or (previous[1] if previous else stamp)), updated_at=str(row.get('updated_at') or stamp), payload_json=payload)
    if table == 'stays':
        _upsert(conn, table, {**common, 'check_in': str(row.get('check_in') or ''),
            'check_out': str(row.get('check_out') or ''), 'reserved_end': str(row.get('reserved_end') or row.get('check_out') or ''),
            'status': str(row.get('status') or 'legacy')})
    elif table == 'vehicles':
        _upsert(conn, table, {**common, 'plate': str(row.get('plate') or '')})
        stay, _ = _stay_context(conn, namespace, table, identity, row.get('stay_id'), importing)
        if stay:
            conn.execute('INSERT INTO stay_vehicles VALUES(?,?,?,?,?) ON CONFLICT(property_id,stay_id,vehicle_id) DO UPDATE SET payload_json=excluded.payload_json',
                         (PROPERTY, stay, identity, 0, payload))
    elif table in {'pass_requests', 'passes', 'gate_events'}:
        # Map containers use their plate as the key. Preserve the original
        # payload exactly; use the key only to resolve a legacy vehicle link.
        link_row = {**row, 'plate': row.get('plate') or (item_key if table == 'passes' else '')}
        vehicle = _vehicle_link(conn, namespace, table, identity, link_row, importing)
        stay, context = _stay_context(conn, namespace, table, identity, row.get('stay_id'), importing)
        if context == 'stay' and not vehicle and not importing:
            old = conn.execute('SELECT 1 FROM runtime_record_origins WHERE namespace=? AND field=? AND record_id=? AND imported=1', (namespace, field, identity)).fetchone()
            if not old:
                raise ValueError('New guest records require a vehicle')
        values = {**common, 'vehicle_id': vehicle, 'stay_id': stay, 'context_kind': context}
        if table == 'pass_requests':
            values.update(status=str(row.get('status') or 'requested'), provider_operation_id=row.get('provider_operation_id'))
        elif table == 'passes':
            request = _link(conn, namespace, table, identity, 'request_id', row.get('request_id'), 'pass_requests', importing)
            if context == 'stay' and not request and not importing:
                legacy = conn.execute('SELECT 1 FROM runtime_record_origins WHERE namespace=? AND field=? AND record_id=? AND imported=1', (namespace,field,identity)).fetchone()
                if not legacy:
                    raise ValueError('New guest passes require a request')
            values.update(request_id=request, status=str(row.get('status') or 'active'),
                valid_from=row.get('starts_at') or row.get('check_in') or None,
                valid_until=row.get('expires_at') or row.get('check_out') or None,
                provider_ref=row.get('provider_ref') or row.get('_v8_provider_ref'))
        else:
            values.pop('version'); values.pop('created_at'); values.pop('updated_at')
            pass_id = _link(conn, namespace, table, identity, 'pass_id', row.get('pass_id'), 'passes', importing)
            direction = row.get('direction') or row.get('event')
            if direction not in {'entry', 'exit'}:
                if not importing:
                    raise ValueError('Invalid gate direction')
                direction = None
            values.update(pass_id=pass_id, direction=direction,
                occurred_at=str(row.get('occurred_at') or row.get('created_at') or stamp),
                source=str(row.get('source') or namespace), source_event_id=str(row.get('source_id') or row.get('source_event_id') or identity))
        _upsert(conn, table, values)
        if table == 'gate_events':
            event_id = 'gate-' + digest(identity)
            _event(conn, event_id, namespace, row, stamp, stay=stay, vehicle=vehicle, pass_id=pass_id, category='gate')
            _upsert(conn, 'gate_event_links', dict(property_id=PROPERTY, gate_event_id=identity, event_id=event_id), ('property_id', 'gate_event_id'))
    elif table == 'stay_violations':
        stay, _ = _stay_context(conn, namespace, table, identity, row.get('stay_id'), importing)
        _upsert(conn, table, {**common, 'stay_id': stay, 'zone_id': None,
            'rule_id': row.get('rule_id'), 'status': str(row.get('status') or 'recorded')}, ('property_id', 'id'))
    elif table == 'events':
        # Historic generic audit fields may refer to requests or deleted stays.
        # Only known identities become FKs; originals always remain in payload.
        stay = row.get('stay_id') if _record_exists(conn, 'stays', row.get('stay_id')) else None
        vehicle = row.get('vehicle_id') if _record_exists(conn, 'vehicles', row.get('vehicle_id')) else None
        pass_id = row.get('pass_id') if _record_exists(conn, 'passes', row.get('pass_id')) else None
        _event(conn, identity, namespace, row, stamp, stay=stay, vehicle=vehicle, pass_id=pass_id)
    return identity


def _order_field(field):
    return ({'entries': 0, 'vehicles': 1, 'requests': 2, 'passes': 3, 'gate_events': 4}.get(field, 10), field)


def write_data(conn, namespace, data, *, importing=False):
    if data is None:
        conn.execute('UPDATE runtime_heads SET data_kind=? WHERE namespace=?', ('null', namespace))
        conn.execute('DELETE FROM runtime_fields WHERE namespace=?', (namespace,))
        return
    if not isinstance(data, dict):
        raise ValueError('Runtime data must be an object or null')
    conn.execute('UPDATE runtime_heads SET data_kind=? WHERE namespace=?', ('object', namespace))
    positions = {field: i for i, field in enumerate(data)}
    for field in sorted(data, key=_order_field):
        value = data[field]
        kind = 'dict' if isinstance(value, dict) else 'list' if isinstance(value, list) else 'scalar'
        scalar = dumps(value) if kind == 'scalar' else None
        conn.execute('INSERT INTO runtime_fields VALUES(?,?,?,?,?) ON CONFLICT(namespace,field) DO UPDATE SET position=excluded.position,kind=excluded.kind,payload_json=excluded.payload_json WHERE runtime_fields.position IS NOT excluded.position OR runtime_fields.kind IS NOT excluded.kind OR runtime_fields.payload_json IS NOT excluded.payload_json',
                     (namespace, field, positions[field], kind, scalar))
        items = list(value.items()) if kind == 'dict' else list(enumerate(value)) if kind == 'list' else []
        keep = set()
        for position, (item_key, row) in enumerate(items):
            item_key = str(item_key)
            keep.add(item_key)
            table = CORE_FIELDS.get((namespace, field))
            if table is None and field in EVENT_FIELDS and kind == 'list':
                table = 'events'
            refs = {c: None for c in REFS.values()}
            raw = dumps(row)
            if table and isinstance(row, dict):
                refs[REFS[table]] = write_record(conn, namespace, field, item_key, row, table, importing)
                raw = None
            values = [namespace, field, item_key, position, raw, *refs.values()]
            columns = ['namespace', 'field', 'item_key', 'position', 'payload_json', *refs]
            updates = columns[3:]
            conn.execute(f"INSERT INTO runtime_items ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(namespace,field,item_key) DO UPDATE SET "
                         + ','.join(c+'=excluded.'+c for c in updates) + ' WHERE '
                         + ' OR '.join('runtime_items.'+c+' IS NOT excluded.'+c for c in updates), values)
        old_keys = {r[0] for r in conn.execute('SELECT item_key FROM runtime_items WHERE namespace=? AND field=?', (namespace, field))}
        conn.executemany('DELETE FROM runtime_items WHERE namespace=? AND field=? AND item_key=?', ((namespace, field, k) for k in old_keys-keep))
    old_fields = {r[0] for r in conn.execute('SELECT field FROM runtime_fields WHERE namespace=?', (namespace,))}
    conn.executemany('DELETE FROM runtime_fields WHERE namespace=? AND field=?', ((namespace, f) for f in old_fields-set(data)))


def read_data(conn, namespace):
    header = conn.execute('SELECT data_kind FROM runtime_heads WHERE namespace=?', (namespace,)).fetchone()
    if header is None:
        raise RuntimeError('Missing relational runtime head')
    if header[0] == 'null':
        return None
    data = {}
    for field, kind, scalar in conn.execute('SELECT field,kind,payload_json FROM runtime_fields WHERE namespace=? ORDER BY position', (namespace,)):
        if kind == 'scalar':
            data[field] = json.loads(scalar)
            continue
        result = {} if kind == 'dict' else []
        query = 'SELECT item_key,payload_json,' + ','.join(REFS.values()) + ' FROM runtime_items WHERE namespace=? AND field=? ORDER BY position'
        for key, raw, *refs in conn.execute(query, (namespace, field)):
            if raw is None:
                targets = [(table, ref) for table, ref in zip(REFS, refs) if ref is not None]
                if len(targets) != 1:
                    raise RuntimeError('Invalid relational membership')
                table, identity = targets[0]
                pk = 'event_id' if table == 'events' else 'id'
                row = conn.execute(f'SELECT payload_json FROM {table} WHERE {pk}=?', (identity,)).fetchone()
                if row is None:
                    raise RuntimeError('Relational record is missing')
                raw = row[0]
            value = json.loads(raw)
            if kind == 'dict':
                result[key] = value
            else:
                result.append(value)
        data[field] = result
    return data


def migrate(conn, path, *, verify_deployment=True):
    conn.execute('BEGIN IMMEDIATE')
    try:
        meta = dict(conn.execute('SELECT key,value FROM relax47_meta'))
        if meta.get('runtime_backend') == BACKEND and meta.get('schema_version') == '12':
            conn.commit()
            return
        if meta.get('schema_version') != '11' or meta.get('runtime_backend') != 'sqlite_store_v1':
            raise RuntimeError('Unexpected schema/backend; migration refused')
        if verify_deployment:
            check_deployment(path)
        for table in TABLES | {'source_snapshots', 'runtime_heads'}:
            exists = conn.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()
            if exists and conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:
                raise RuntimeError('Expected untouched relational target: '+table)
        docs = {}
        for key in STORES:
            record = conn.execute("SELECT version,payload_json FROM runtime_documents WHERE property_id=? AND namespace=? AND item_key='ha_store'", (PROPERTY, key)).fetchone()
            if record is None:
                raise RuntimeError('Missing current SQL document')
            envelope = json.loads(record[1])
            if envelope.get('key') != key or envelope.get('version') != 1 or 'data' not in envelope:
                raise ValueError('Unexpected source envelope')
            docs[key] = (record[0], record[1], envelope)
        # The schema-11 domain tables are empty. Rebuild only the four tables
        # with incompatible legacy restrictions, keeping all other objects.
        definitions = list(statements(SCHEMA_SQL))
        changed = {'vehicles', 'pass_requests', 'passes', 'gate_events'}
        for table in ('gate_events', 'passes', 'pass_requests', 'vehicles'):
            conn.execute('DROP TABLE '+table)
        for stmt in definitions:
            if stmt.startswith('CREATE TABLE '):
                name = stmt.split()[2]
                if name in changed or name.startswith('runtime_') and name != 'runtime_documents':
                    conn.execute(stmt)
        for stmt in definitions:
            if stmt.startswith(('CREATE INDEX ', 'CREATE UNIQUE INDEX ')):
                name = stmt.split()[3] if stmt.startswith('CREATE UNIQUE') else stmt.split()[2]
                if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone():
                    conn.execute(stmt)
        stamp = now()
        source_fingerprints = []
        for key, (revision, raw, envelope) in docs.items():
            snapshot_id = 'relational12-' + digest(key)
            encoded = raw.encode()
            conn.execute('INSERT INTO source_snapshots VALUES(?,?,?,?,?,?,?,?)',
                         (PROPERTY, snapshot_id, key, stamp, stamp, digest(encoded), len(encoded), encoded))
            head = {k:v for k,v in envelope.items() if k != 'data'}
            conn.execute('INSERT INTO runtime_heads VALUES(?,?,?,?,?)', (key, revision, dumps(head), 'null' if envelope['data'] is None else 'object', stamp))
            source_fingerprints.append([key, revision, digest(encoded)])
        # Parents are written first; all modules use one transaction and snapshot.
        for key in STORES:
            write_data(conn, key, docs[key][2]['data'], importing=True)
        # Resolve journal FKs once every parent domain exists. Raw historical
        # aliases such as `current` are never rebound to today's stay.
        for event_id, raw in conn.execute('SELECT event_id,payload_json FROM events').fetchall():
            row = json.loads(raw)
            for column, table in (('stay_id','stays'),('vehicle_id','vehicles'),('pass_id','passes')):
                value = row.get(column)
                if _record_exists(conn,table,value):
                    conn.execute(f'UPDATE event_context SET {column}=? WHERE event_id=?',(value,event_id))
        for key, (revision, raw, envelope) in docs.items():
            restored = read_data(conn, key)
            if dumps(restored) != dumps(envelope['data']):
                raise RuntimeError('Full reverse-read parity failed for '+key)
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign-key verification failed')
        if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQL integrity verification failed')
        counts = {table:conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in sorted(TABLES)}
        conn.execute('INSERT INTO runtime_migration_runs VALUES(?,?,?,?,?,?,?,?,?)',
            ('relational12', ADAPTER_VERSION, 11, 12, stamp, now(), digest(dumps(source_fingerprints)), 1, dumps(counts)))
        conn.execute("UPDATE relax47_meta SET value='12' WHERE key='schema_version'")
        conn.execute("UPDATE relax47_meta SET value=? WHERE key='runtime_backend'", (BACKEND,))
        conn.execute("UPDATE relax47_meta SET value='relational_primary' WHERE key='migration_stage'")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def rollback_to_compatibility(conn):
    """Explicit maintenance operation: preserve *current* rows, not an old backup.

    Caller must stop other writers and redeploy the previous eight adapters before
    restarting HA. Schema 12 is retained, and no business/history rows are deleted.
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        if conn.execute("SELECT value FROM relax47_meta WHERE key='runtime_backend'").fetchone() != (BACKEND,):
            raise RuntimeError('Relational backend required for rollback')
        for namespace, revision, header in conn.execute('SELECT namespace,revision,envelope_json FROM runtime_heads').fetchall():
            envelope = json.loads(header)
            envelope['data'] = read_data(conn, namespace)
            payload = dumps(envelope)
            changed = conn.execute("UPDATE runtime_documents SET payload_json=?,version=?,updated_at=? WHERE property_id=? AND namespace=? AND item_key='ha_store'",
                (payload, revision+1, now(), PROPERTY, namespace)).rowcount
            if changed != 1:
                raise RuntimeError('Missing compatibility destination')
        conn.execute("UPDATE relax47_meta SET value='sqlite_store_v1' WHERE key='runtime_backend'")
        conn.execute("UPDATE relax47_meta SET value='relational_rollback_current_data' WHERE key='migration_stage'")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class Store:
    @classmethod
    def __class_getitem__(cls, _):
        return cls

    def __init__(self, hass, version, key):
        if key not in STORES or version != 1:
            raise ValueError('Unsupported RELAX47 Store contract')
        self.hass, self.version, self.key = hass, version, key
        self.path = Path(hass.config.path('relax47_v8', 'relax47.db'))
        self.revision = None
        self.envelope = None
        self.lock = asyncio.Lock()

    def _check(self, conn):
        if conn.execute("SELECT value FROM relax47_meta WHERE key='runtime_backend'").fetchone() != (BACKEND,):
            raise RuntimeError('Relational SQL backend is not activated')

    def _load(self):
        with closing(connect(self.path)) as conn:
            migrate(conn, self.path)
            conn.execute('BEGIN')
            self._check(conn)
            revision, header = conn.execute('SELECT revision,envelope_json FROM runtime_heads WHERE namespace=?', (self.key,)).fetchone()
            envelope = json.loads(header)
            envelope['data'] = read_data(conn, self.key)
            conn.commit()
            return revision, envelope

    async def async_load(self):
        async with self.lock:
            self.revision, self.envelope = await self.hass.async_add_executor_job(self._load)
            return json.loads(dumps(self.envelope['data']))

    def _save(self, payload):
        data = json.loads(payload)['data']
        with closing(connect(self.path)) as conn, conn:
            self._check(conn)
            changed = conn.execute('UPDATE runtime_heads SET revision=revision+1,updated_at=? WHERE namespace=? AND revision=?',
                                   (now(), self.key, self.revision)).rowcount
            if changed != 1:
                raise RuntimeError('Concurrent RELAX47 update; stale write rejected')
            write_data(conn, self.key, data)
            if dumps(read_data(conn, self.key)) != dumps(data):
                raise RuntimeError('Relational save reverse-read mismatch')

    async def async_save(self, data):
        async with self.lock:
            if self.revision is None:
                raise RuntimeError('Load Store before saving')
            envelope = dict(self.envelope)
            envelope['data'] = data
            payload = dumps(envelope)
            pending = asyncio.ensure_future(self.hass.async_add_executor_job(self._save, payload))
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            pending.result()
            self.revision += 1
            self.envelope = json.loads(payload)
            if cancelled:
                raise asyncio.CancelledError


# SCHEMA_SQL is embedded by build_adapter.py; all eight copies are byte-identical.

SCHEMA_SQL = "PRAGMA foreign_keys=ON;\nBEGIN TRANSACTION;\nCREATE TABLE access_decisions (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, stay_id TEXT NOT NULL, zone_id TEXT NOT NULL, evaluated_for TEXT NOT NULL, allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), reason TEXT NOT NULL, evaluated_at TEXT NOT NULL, payload_json TEXT NOT NULL);\nCREATE TABLE access_policies (property_id TEXT NOT NULL, zone_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), mode TEXT NOT NULL CHECK(mode IN ('stay_window','always','deny')), requires_pass INTEGER NOT NULL CHECK(requires_pass IN (0,1)), entry_offset_seconds INTEGER NOT NULL DEFAULT 0, exit_offset_seconds INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,zone_id));\nCREATE TABLE audit_changes (\n        event_id TEXT PRIMARY KEY REFERENCES event_context(event_id), target_type TEXT NOT NULL,\n        target_id TEXT NOT NULL, before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),\n        after_json TEXT CHECK(after_json IS NULL OR json_valid(after_json)), result TEXT NOT NULL);\nCREATE TABLE billing_accounts (stay_id TEXT PRIMARY KEY REFERENCES stays(id), property_id TEXT NOT NULL, current_quote_id TEXT NOT NULL UNIQUE REFERENCES billing_quotes(id), version INTEGER NOT NULL CHECK(version>0));\nCREATE TABLE billing_quotes (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, stay_id TEXT NOT NULL REFERENCES stays(id), stay_version INTEGER NOT NULL, tariff_id TEXT NOT NULL, tariff_version INTEGER NOT NULL, payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(property_id,tariff_id,tariff_version) REFERENCES rate_plans(property_id,id,version));\nCREATE TABLE booking_commands (property_id TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(property_id,actor,request_id));\nCREATE TABLE bs2_commands (property_id TEXT NOT NULL, command_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('create_pass','delete_pass')), fingerprint TEXT NOT NULL, command_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('unknown','accepted','confirmed')), job_id TEXT, receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,command_id), UNIQUE(property_id,job_id));\nCREATE TABLE configuration_revisions (\n        property_id TEXT NOT NULL REFERENCES properties(id), namespace TEXT NOT NULL, item_key TEXT NOT NULL,\n        revision INTEGER NOT NULL CHECK(revision>0), actor TEXT NOT NULL, created_at TEXT NOT NULL,\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,namespace,item_key,revision));\nCREATE TABLE device_imports (property_id TEXT NOT NULL REFERENCES properties(id), source_sha256 TEXT NOT NULL, actor TEXT NOT NULL, imported_at TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(property_id,source_sha256));\nCREATE TABLE event_context (\n        event_id TEXT PRIMARY KEY REFERENCES events(event_id),\n        property_id TEXT NOT NULL REFERENCES properties(id), category TEXT NOT NULL,\n        severity TEXT NOT NULL CHECK(severity IN ('debug','info','warning','error','critical')),\n        stay_id TEXT, vehicle_id TEXT, pass_id TEXT, zone_id TEXT, sensor_entity_id TEXT,\n        parent_event_id TEXT, received_at TEXT NOT NULL,\n        UNIQUE(property_id,event_id),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),\n        FOREIGN KEY(property_id,vehicle_id) REFERENCES vehicles(property_id,id),\n        FOREIGN KEY(property_id,pass_id) REFERENCES passes(property_id,id),\n        FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id),\n        FOREIGN KEY(property_id,parent_event_id) REFERENCES event_context(property_id,event_id));\nCREATE TABLE event_media (\n        property_id TEXT NOT NULL, event_id TEXT NOT NULL, media_id TEXT NOT NULL, role TEXT NOT NULL,\n        PRIMARY KEY(property_id,event_id,media_id,role),\n        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id),\n        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));\nCREATE TABLE event_origins (\n        property_id TEXT NOT NULL, source TEXT NOT NULL, source_event_id TEXT NOT NULL,\n        event_id TEXT NOT NULL, fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),\n        PRIMARY KEY(property_id,source,source_event_id),\n        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));\nCREATE TABLE events (\n            event_id TEXT PRIMARY KEY,\n            event_type TEXT NOT NULL,\n            occurred_at TEXT NOT NULL,\n            correlation_id TEXT NOT NULL,\n            actor TEXT NOT NULL,\n            schema_version INTEGER NOT NULL,\n            payload_json TEXT NOT NULL\n        );\nCREATE TABLE gate_event_links (\n        property_id TEXT NOT NULL, gate_event_id TEXT NOT NULL, event_id TEXT NOT NULL,\n        PRIMARY KEY(property_id,gate_event_id), UNIQUE(property_id,event_id),\n        FOREIGN KEY(property_id,gate_event_id) REFERENCES gate_events(property_id,id),\n        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));\nCREATE TABLE gate_events (id TEXT PRIMARY KEY, context_kind TEXT NOT NULL DEFAULT 'unresolved' CHECK(context_kind IN ('stay','administrative','unknown','unresolved')), property_id TEXT NOT NULL, vehicle_id TEXT REFERENCES vehicles(id), stay_id TEXT REFERENCES stays(id), pass_id TEXT REFERENCES passes(id), direction TEXT CHECK(direction IN ('entry','exit')), occurred_at TEXT NOT NULL, source TEXT NOT NULL, source_event_id TEXT, payload_json TEXT NOT NULL, UNIQUE(property_id,source,source_event_id), CHECK(context_kind!='stay' OR stay_id IS NOT NULL));\nCREATE TABLE guests (\n        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,\n        version INTEGER NOT NULL CHECK(version>0), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,id));\nCREATE TABLE incidents (\n            incident_id TEXT PRIMARY KEY,\n            error_code TEXT NOT NULL,\n            component TEXT NOT NULL,\n            severity TEXT NOT NULL,\n            status TEXT NOT NULL,\n            message TEXT NOT NULL,\n            correlation_id TEXT NOT NULL,\n            first_seen TEXT NOT NULL,\n            last_seen TEXT NOT NULL,\n            count INTEGER NOT NULL DEFAULT 1,\n            probable_cause TEXT NOT NULL DEFAULT '',\n            recommended_action TEXT NOT NULL DEFAULT ''\n        , property_id TEXT NOT NULL DEFAULT '', device_id TEXT, zone_id TEXT, details_json TEXT NOT NULL DEFAULT '{}', automatic_action TEXT NOT NULL DEFAULT '');\nCREATE TABLE integration_circuits (property_id TEXT NOT NULL, component TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('closed','open','half_open')), failure_count INTEGER NOT NULL DEFAULT 0, open_until TEXT, version INTEGER NOT NULL CHECK(version>0), updated_at TEXT NOT NULL, PRIMARY KEY(property_id,component));\nCREATE TABLE logical_devices (property_id TEXT NOT NULL, id TEXT NOT NULL, zone_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), compatibility_entity_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,id), UNIQUE(property_id,compatibility_entity_id), FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id));\nCREATE TABLE media_assets (\n        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,\n        mime_type TEXT NOT NULL, sha256 TEXT CHECK(sha256 IS NULL OR length(sha256)=64),\n        size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes>=0), captured_at TEXT,\n        storage_kind TEXT NOT NULL CHECK(storage_kind IN ('blob','file','object')),\n        content BLOB, storage_uri TEXT, verified_at TEXT,\n        state TEXT NOT NULL CHECK(state IN ('pending','available','missing','damaged')),\n        retain_until TEXT, preserve INTEGER NOT NULL DEFAULT 1 CHECK(preserve IN (0,1)),\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(property_id,id),\n        UNIQUE(property_id,sha256),\n        CHECK((storage_kind='blob' AND content IS NOT NULL AND size_bytes IS NOT NULL AND storage_uri IS NULL AND length(content)=size_bytes)\n           OR (storage_kind IN ('file','object') AND content IS NULL AND storage_uri IS NOT NULL)),\n        CHECK(state!='available' OR (verified_at IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL)));\nCREATE TABLE media_cleanup_requests (\n        property_id TEXT NOT NULL, media_id TEXT NOT NULL, reason TEXT NOT NULL,\n        requested_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','blocked','deleted','failed')),\n        last_error TEXT, completed_at TEXT, PRIMARY KEY(property_id,media_id,reason),\n        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));\nCREATE TABLE media_delivery_receipts (\n        property_id TEXT NOT NULL, id TEXT NOT NULL, media_id TEXT NOT NULL,\n        channel TEXT NOT NULL, destination_ref TEXT NOT NULL, message_id TEXT NOT NULL,\n        confirmed_at TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,id), UNIQUE(property_id,channel,destination_ref,message_id,media_id),\n        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));\nCREATE TABLE migration_findings (\n        property_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, id TEXT NOT NULL,\n        json_pointer TEXT NOT NULL, code TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),\n        details_json TEXT NOT NULL CHECK(json_valid(details_json)), PRIMARY KEY(property_id,id),\n        FOREIGN KEY(property_id,snapshot_id) REFERENCES source_snapshots(property_id,id));\nCREATE TABLE notification_deliveries (\n        property_id TEXT NOT NULL, id TEXT NOT NULL, event_id TEXT NOT NULL,\n        channel TEXT NOT NULL, recipient_ref TEXT NOT NULL, status TEXT NOT NULL,\n        attempt INTEGER NOT NULL CHECK(attempt>=0), updated_at TEXT NOT NULL,\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(property_id,id),\n        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));\nCREATE TABLE pass_reference_photos (\n        property_id TEXT NOT NULL, pass_id TEXT NOT NULL, media_id TEXT NOT NULL,\n        created_at TEXT NOT NULL, PRIMARY KEY(property_id,pass_id),\n        FOREIGN KEY(property_id,pass_id) REFERENCES passes(property_id,id),\n        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));\nCREATE TABLE pass_requests (id TEXT PRIMARY KEY, context_kind TEXT NOT NULL DEFAULT 'unresolved' CHECK(context_kind IN ('stay','administrative','unknown','unresolved')), property_id TEXT NOT NULL, vehicle_id TEXT REFERENCES vehicles(id), stay_id TEXT REFERENCES stays(id), status TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), provider_operation_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, CHECK(context_kind!='stay' OR stay_id IS NOT NULL));\nCREATE TABLE passes (id TEXT PRIMARY KEY, context_kind TEXT NOT NULL DEFAULT 'unresolved' CHECK(context_kind IN ('stay','administrative','unknown','unresolved')), property_id TEXT NOT NULL, vehicle_id TEXT REFERENCES vehicles(id), stay_id TEXT REFERENCES stays(id), request_id TEXT REFERENCES pass_requests(id), status TEXT NOT NULL, valid_from TEXT, valid_until TEXT, provider_ref TEXT, version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, CHECK(context_kind!='stay' OR stay_id IS NOT NULL));\nCREATE TABLE payments (id TEXT PRIMARY KEY, quote_id TEXT NOT NULL REFERENCES billing_quotes(id), kind TEXT NOT NULL CHECK(kind IN ('advance','payment','refund','deposit','deposit_refund')), amount_minor INTEGER NOT NULL CHECK(amount_minor>0), actor TEXT NOT NULL, created_at TEXT NOT NULL);\nCREATE TABLE properties (id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), timezone TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);\nCREATE TABLE provider_operations (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, provider TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('create_pass','delete_pass','list_passes','status')), aggregate_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','processing','succeeded','failed','dead_letter')), attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0), available_at TEXT NOT NULL, lease_until TEXT, request_json TEXT NOT NULL, response_json TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);\nCREATE TABLE rate_plans (property_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(property_id,id,version));\nCREATE TABLE recovery_operations (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, incident_id TEXT NOT NULL REFERENCES incidents(incident_id), action TEXT NOT NULL CHECK(action IN ('fallback','reload','restart','notify')), safety_class TEXT NOT NULL CHECK(safety_class IN ('informational','physical')), status TEXT NOT NULL CHECK(status IN ('queued','processing','succeeded','dead_letter','cancelled')), attempt INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_until TEXT, request_json TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);\nCREATE TABLE relax47_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\nCREATE TABLE runtime_documents (\n        property_id TEXT NOT NULL REFERENCES properties(id), namespace TEXT NOT NULL, item_key TEXT NOT NULL,\n        version INTEGER NOT NULL CHECK(version>0), updated_at TEXT NOT NULL,\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,namespace,item_key));\nCREATE TABLE sensor_observations (\n        event_id TEXT PRIMARY KEY REFERENCES event_context(event_id), entity_id TEXT NOT NULL,\n        old_state_json TEXT CHECK(old_state_json IS NULL OR json_valid(old_state_json)),\n        new_state_json TEXT NOT NULL CHECK(json_valid(new_state_json)),\n        attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)));\nCREATE TABLE source_mappings (\n        property_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, json_pointer TEXT NOT NULL,\n        target_table TEXT NOT NULL, target_key_json TEXT NOT NULL CHECK(json_valid(target_key_json)),\n        status TEXT NOT NULL CHECK(status IN ('mapped','preserved','unresolved')),\n        PRIMARY KEY(property_id,snapshot_id,json_pointer,target_table),\n        FOREIGN KEY(property_id,snapshot_id) REFERENCES source_snapshots(property_id,id));\nCREATE TABLE source_snapshots (\n        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,\n        source_key TEXT NOT NULL, captured_at TEXT NOT NULL, imported_at TEXT NOT NULL,\n        sha256 TEXT NOT NULL CHECK(length(sha256)=64), size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),\n        content BLOB NOT NULL, PRIMARY KEY(property_id,id), UNIQUE(property_id,source_key,sha256),\n        CHECK(length(content)=size_bytes));\nCREATE TABLE stay_commands (actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(actor,request_id));\nCREATE TABLE stay_guests (\n        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, guest_id TEXT NOT NULL,\n        role TEXT NOT NULL, position INTEGER NOT NULL CHECK(position>=0),\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,stay_id,guest_id),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),\n        FOREIGN KEY(property_id,guest_id) REFERENCES guests(property_id,id));\nCREATE TABLE stay_reviews (\n        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, id TEXT NOT NULL,\n        actor TEXT NOT NULL, created_at TEXT NOT NULL,\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,stay_id,id),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id));\nCREATE TABLE stay_spa_sessions (\n        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, id TEXT NOT NULL,\n        position INTEGER NOT NULL CHECK(position>=0), start_at TEXT, end_at TEXT,\n        timezone TEXT, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,stay_id,id),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id));\nCREATE TABLE stay_vehicles (\n        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,\n        position INTEGER NOT NULL CHECK(position>=0), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,stay_id,vehicle_id),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),\n        FOREIGN KEY(property_id,vehicle_id) REFERENCES vehicles(property_id,id));\nCREATE TABLE stay_video_assets (\n        property_id TEXT NOT NULL, job_id TEXT NOT NULL, media_id TEXT NOT NULL,\n        PRIMARY KEY(property_id,job_id),\n        FOREIGN KEY(property_id,job_id) REFERENCES stay_video_jobs(property_id,id),\n        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));\nCREATE TABLE stay_video_jobs (\n        property_id TEXT NOT NULL, id TEXT NOT NULL, stay_id TEXT NOT NULL,\n        stay_version INTEGER NOT NULL CHECK(stay_version>0),\n        mode TEXT NOT NULL CHECK(mode IN ('full','presentation','rules')),\n        profile_json TEXT NOT NULL CHECK(json_valid(profile_json)),\n        profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),\n        state TEXT NOT NULL CHECK(state IN ('queued','building','ready','failed','superseded','cancelled')),\n        due_at TEXT NOT NULL, lease_until TEXT, claim_token TEXT,\n        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), last_error TEXT,\n        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,\n        PRIMARY KEY(property_id,id), UNIQUE(property_id,stay_id,stay_version,mode),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id));\nCREATE TABLE stay_violations (\n        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,\n        stay_id TEXT, zone_id TEXT, rule_id TEXT, status TEXT NOT NULL,\n        version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(property_id,id),\n        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),\n        FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id));\nCREATE TABLE stays (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), check_in TEXT NOT NULL, check_out TEXT NOT NULL, reserved_end TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);\nCREATE TABLE vehicles (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, plate TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(property_id,id));\nCREATE TABLE violation_events (\n        property_id TEXT NOT NULL, violation_id TEXT NOT NULL, event_id TEXT NOT NULL,\n        PRIMARY KEY(property_id,violation_id,event_id),\n        FOREIGN KEY(property_id,violation_id) REFERENCES stay_violations(property_id,id),\n        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));\nCREATE TABLE violation_media (\n        property_id TEXT NOT NULL, violation_id TEXT NOT NULL, media_id TEXT NOT NULL, role TEXT NOT NULL,\n        PRIMARY KEY(property_id,violation_id,media_id,role),\n        FOREIGN KEY(property_id,violation_id) REFERENCES stay_violations(property_id,id),\n        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));\nCREATE TABLE zone_configuration (\n        property_id TEXT NOT NULL, zone_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),\n        position INTEGER NOT NULL CHECK(position>=0), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),\n        PRIMARY KEY(property_id,zone_id,revision),\n        FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id));\nCREATE TABLE zones (property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,id));\nCREATE INDEX idx_events_type_time\n            ON events(event_type, occurred_at);\nCREATE INDEX idx_events_correlation\n            ON events(correlation_id);\nCREATE INDEX idx_stays_property_period ON stays(property_id,check_in,reserved_end);\nCREATE INDEX idx_quotes_stay ON billing_quotes(property_id,stay_id);\nCREATE INDEX idx_payments_quote ON payments(quote_id);\nCREATE INDEX idx_vehicles_property ON vehicles(property_id,id);\nCREATE INDEX idx_pass_requests_stay ON pass_requests(property_id,stay_id,status);\nCREATE INDEX idx_passes_vehicle_period ON passes(property_id,vehicle_id,valid_from,valid_until);\nCREATE INDEX idx_provider_operations_due ON provider_operations(provider,status,available_at);\nCREATE INDEX idx_gate_events_vehicle_time ON gate_events(property_id,vehicle_id,occurred_at);\nCREATE INDEX idx_access_decisions_stay_time ON access_decisions(property_id,stay_id,evaluated_for);\nCREATE INDEX idx_incidents_property_status ON incidents(property_id,status,severity);\nCREATE INDEX idx_recovery_due ON recovery_operations(status,available_at);\nCREATE UNIQUE INDEX ux_stays_property_id ON stays(property_id,id);\nCREATE UNIQUE INDEX ux_vehicles_property_id ON vehicles(property_id,id);\nCREATE UNIQUE INDEX ux_passes_property_id ON passes(property_id,id);\nCREATE UNIQUE INDEX ux_gate_events_property_id ON gate_events(property_id,id);\nCREATE INDEX idx_event_context_stay ON event_context(property_id,stay_id,event_id);\nCREATE INDEX idx_event_context_problem ON event_context(property_id,severity,category,event_id);\nCREATE INDEX idx_events_time ON events(occurred_at,event_id);\nCREATE INDEX idx_event_context_vehicle ON event_context(property_id,vehicle_id,event_id);\nCREATE INDEX idx_event_context_zone ON event_context(property_id,zone_id,event_id);\nCREATE INDEX idx_sensor_observations_entity ON sensor_observations(entity_id,event_id);\nCREATE INDEX idx_violations_stay ON stay_violations(property_id,stay_id,status);\nCREATE VIEW system_journal AS\n        SELECT e.event_id,e.event_type,e.occurred_at,e.correlation_id,e.actor,e.payload_json,\n               c.property_id,c.category,c.severity,c.stay_id,c.vehicle_id,c.pass_id,c.zone_id,\n               c.sensor_entity_id,c.parent_event_id,c.received_at\n        FROM events e LEFT JOIN event_context c ON c.event_id=e.event_id;\nCREATE INDEX idx_stay_video_jobs_due ON stay_video_jobs(state,due_at,lease_until);\nCREATE TABLE runtime_heads (\n namespace TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),\n envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)),\n data_kind TEXT NOT NULL CHECK(data_kind IN ('object','null')), updated_at TEXT NOT NULL);\nCREATE TABLE runtime_fields (\n namespace TEXT NOT NULL REFERENCES runtime_heads(namespace), field TEXT NOT NULL,\n position INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('scalar','list','dict')),\n payload_json TEXT CHECK(payload_json IS NULL OR json_valid(payload_json)),\n PRIMARY KEY(namespace,field));\nCREATE TABLE runtime_items (\n namespace TEXT NOT NULL, field TEXT NOT NULL, item_key TEXT NOT NULL, position INTEGER NOT NULL,\n payload_json TEXT CHECK(payload_json IS NULL OR json_valid(payload_json)),\n stay_id TEXT REFERENCES stays(id), vehicle_id TEXT REFERENCES vehicles(id),\n request_id TEXT REFERENCES pass_requests(id), pass_id TEXT REFERENCES passes(id),\n gate_id TEXT REFERENCES gate_events(id), violation_id TEXT,\n event_id TEXT REFERENCES events(event_id), property_id TEXT NOT NULL DEFAULT 'relax47',\n PRIMARY KEY(namespace,field,item_key),\n FOREIGN KEY(namespace,field) REFERENCES runtime_fields(namespace,field) ON DELETE CASCADE,\n FOREIGN KEY(property_id,violation_id) REFERENCES stay_violations(property_id,id),\n CHECK((payload_json IS NOT NULL)+(stay_id IS NOT NULL)+(vehicle_id IS NOT NULL)+\n       (request_id IS NOT NULL)+(pass_id IS NOT NULL)+(gate_id IS NOT NULL)+\n       (violation_id IS NOT NULL)+(event_id IS NOT NULL)=1));\nCREATE TABLE runtime_record_origins (\n namespace TEXT NOT NULL, field TEXT NOT NULL, origin_key TEXT NOT NULL,\n table_name TEXT NOT NULL, record_id TEXT NOT NULL, imported INTEGER NOT NULL CHECK(imported IN (0,1)),\n PRIMARY KEY(namespace,field,origin_key));\nCREATE TABLE runtime_unresolved_links (\n namespace TEXT NOT NULL, table_name TEXT NOT NULL, record_id TEXT NOT NULL, field TEXT NOT NULL,\n raw_value_json TEXT NOT NULL CHECK(json_valid(raw_value_json)),\n first_seen_at TEXT NOT NULL, PRIMARY KEY(namespace,table_name,record_id,field));\nCREATE TABLE runtime_migration_runs (\n id TEXT PRIMARY KEY, adapter_version TEXT NOT NULL, source_schema INTEGER NOT NULL,\n target_schema INTEGER NOT NULL, started_at TEXT NOT NULL, verified_at TEXT NOT NULL,\n source_sha256 TEXT NOT NULL, parity_verified INTEGER NOT NULL CHECK(parity_verified=1),\n counts_json TEXT NOT NULL CHECK(json_valid(counts_json)));\nCREATE INDEX idx_runtime_items_stay ON runtime_items(stay_id);\nCREATE INDEX idx_runtime_items_vehicle ON runtime_items(vehicle_id);\nCREATE INDEX idx_runtime_items_event ON runtime_items(event_id);\nCREATE INDEX idx_vehicles_plate_lookup ON vehicles(property_id,plate);\nINSERT INTO relax47_meta VALUES('schema_version','12');\nCOMMIT;\n"
