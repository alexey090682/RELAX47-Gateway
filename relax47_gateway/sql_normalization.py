"""Bounded, read-only audit of current SQL compatibility documents.

Never reads stale .storage files, returns payloads, creates business rows,
replays queues, or grants permission for cutover. Results are snapshot-scoped.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
from zoneinfo import ZoneInfo

from runtime_migration import STORES, MAX_STORE_BYTES, _decode
from sql_migration import _validate_schema

COLLECTIONS = (
    ('entries', 'vehicles', 'vehicle_events', 'system_events', 'notification_queue', 'spa_sessions'),
    ('vehicles', 'passes', 'requests', 'queue', 'gate_events', 'audit'),
    ('sources', 'events', 'dedupe'),
    ('violation_events', 'audit'),
    ('events',),
    ('queue', 'deliveries', 'audit', 'subscribers', 'webhook_events'),
    (), ('bookings', 'events'),
)
TARGETS = ('stays', 'vehicles', 'pass_requests', 'passes', 'gate_events',
           'stay_violations', 'guests', 'stay_spa_sessions', 'source_snapshots')


def _identity(value):
    return value if isinstance(value, str) and value.strip() == value and value else None


def _index(rows, field='id'):
    ids = [_identity(r.get(field)) for r in rows if isinstance(r, dict)]
    counts = Counter(x for x in ids if x is not None)
    return set(counts), sum(n - 1 for n in counts.values()), ids.count(None)


def _date(value, tz):
    if not isinstance(value, str):
        raise ValueError('Invalid date')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        options = []
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=tz, fold=fold)
            if candidate.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == parsed:
                options.append(candidate)
        if not options or len({v.utcoffset() for v in options}) != 1:
            raise ValueError('Ambiguous or nonexistent local date')
        parsed = options[0]
    return parsed.astimezone(timezone.utc)


def normalization_plan(manager):
    path = manager._target()
    if not path.is_file():
        return {'installed': False, 'cutover_ready': False}
    deadline = time.monotonic() + 10
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute('BEGIN')
        meta = dict(conn.execute("SELECT key,value FROM relax47_meta WHERE key IN ('schema_version','runtime_backend')"))
        if meta != {'schema_version': '11', 'runtime_backend': 'sqlite_store_v1'}:
            raise ValueError('Expected schema 11 SQL compatibility backend')
        _validate_schema(conn, 11)
        prop = conn.execute("SELECT timezone FROM properties WHERE id='relax47'").fetchone()
        if prop is None:
            raise ValueError('Missing property')
        tz = ZoneInfo(prop[0])
        documents, inventory, fingerprints, findings = {}, [], [], Counter()
        for key, fields in zip(STORES, COLLECTIONS):
            if time.monotonic() > deadline:
                raise TimeoutError('Normalization audit timed out')
            size = conn.execute("SELECT length(CAST(payload_json AS BLOB)) FROM runtime_documents WHERE property_id='relax47' AND namespace=? AND item_key='ha_store'", (key,)).fetchone()
            if size is None:
                findings['missing_runtime_document'] += 1
                inventory.append({'store': key, 'present': False})
                documents[key] = {}
                continue
            if size[0] > MAX_STORE_BYTES:
                raise ValueError('Oversized runtime document')
            revision, payload = conn.execute("SELECT version,payload_json FROM runtime_documents WHERE property_id='relax47' AND namespace=? AND item_key='ha_store'", (key,)).fetchone()
            envelope = _decode(payload)
            if (not isinstance(envelope, dict) or envelope.get('key') != key
                    or type(envelope.get('version')) is not int or envelope['version'] != 1
                    or envelope.get('minor_version', 1) != 1 or 'data' not in envelope):
                raise ValueError('Unsupported runtime envelope')
            data = envelope['data']
            if data is None:
                findings['uninitialized_runtime_document'] += 1
                data = {}
            elif not isinstance(data, dict):
                raise ValueError('Unsupported runtime data')
            documents[key] = data
            collections = {}
            for name in fields:
                if name not in data:
                    collections[name] = {'type': 'absent', 'count': 0}
                    continue
                value = data[name]
                kind = 'array' if isinstance(value, list) else 'object' if isinstance(value, dict) else 'other'
                collections[name] = {'type': kind, 'count': len(value) if kind != 'other' else None}
            inventory.append({'store': key, 'present': True, 'revision': revision,
                              'bytes': size[0], 'collections': collections})
            fingerprints.append([key, revision, hashlib.sha256(payload.encode()).hexdigest()])

        def rows(store, field):
            value = documents[STORES[store]].get(field, [])
            if store == 1 and field in ('vehicles', 'passes', 'requests') and isinstance(value, dict):
                result = []
                for key, item in value.items():
                    if not isinstance(item, dict):
                        findings['invalid_' + field + '_records'] += 1
                        continue
                    item = dict(item)
                    # A map is a legacy container, not a business lookup table.
                    # Vehicle/request keys are IDs; pass keys are plates, NOT IDs.
                    identity_field = 'vehicle_id' if field == 'vehicles' else 'id'
                    if field != 'passes':
                        explicit = item.get(identity_field)
                        if explicit is not None and explicit != key:
                            findings[field + '_key_identity_mismatch'] += 1
                        if explicit is None:
                            item[identity_field] = key
                    elif item.get('plate') not in (None, key):
                        findings['passes_key_plate_mismatch'] += 1
                    result.append(item)
                return result
            if not isinstance(value, list):
                findings['non_array_' + field] += 1
                return []
            findings['invalid_' + field + '_records'] += sum(not isinstance(x, dict) for x in value)
            return [dict(x) for x in value if isinstance(x, dict)]

        stays = rows(0, 'entries')
        stay_ids, duplicates, missing = _index(stays)
        findings['duplicate_stay_ids'] += duplicates
        findings['missing_stay_ids'] += missing
        for stay in stays:
            try:
                start, end = _date(stay.get('check_in'), tz), _date(stay.get('check_out'), tz)
                if end <= start:
                    findings['nonpositive_stay_periods'] += 1
            except (ValueError, TypeError, OverflowError):
                findings['invalid_stay_dates'] += 1
        current = documents[STORES[0]].get('current_stay')
        current_status = 'absent'
        if current:
            if not isinstance(current, dict):
                findings['invalid_current_stay'] += 1
            elif _identity(current.get('id')) not in stay_ids:
                findings['current_stay_not_in_journal'] += 1
                current_status = 'not_in_journal'
            else:
                equal = any(current == row for row in stays if row.get('id') == current['id'])
                current_status = 'exact_match' if equal else 'same_id_different_payload'
                if not equal:
                    findings['current_stay_requires_reconciliation'] += 1
        vehicles = rows(1, 'vehicles')
        # Older list fixtures use id; live registry uses vehicle_id. Conflicting
        # identities must be reviewed, never silently rebound.
        for vehicle in vehicles:
            if 'vehicle_id' not in vehicle:
                vehicle['vehicle_id'] = vehicle.get('id')
            elif vehicle.get('id') is not None and vehicle['vehicle_id'] != vehicle['id']:
                findings['conflicting_vehicle_identity_fields'] += 1
        vehicle_ids, duplicates, missing = _index(vehicles, 'vehicle_id')
        findings['duplicate_vehicle_ids'] += duplicates
        findings['missing_vehicle_ids'] += missing
        plates = Counter(v.get('plate') for v in vehicles if _identity(v.get('plate')))
        findings['vehicle_plate_identity_review'] += sum(n-1 for n in plates.values())
        passes = rows(1, 'passes')
        requests = rows(1, 'requests')
        pass_ids, duplicates, missing = _index(passes)
        findings['duplicate_pass_ids'] += duplicates
        findings['legacy_pass_ids_to_assign'] += missing
        request_ids, duplicates, missing = _index(requests)
        findings['duplicate_request_ids'] += duplicates
        findings['missing_request_ids'] += missing
        for collection, records, required in (
                ('passes', passes, ('stay_id', 'vehicle_id', 'request_id')),
                ('requests', requests, ('stay_id', 'vehicle_id')),
                ('gate_events', rows(1, 'gate_events'), ('vehicle_id',))):
            for row in records:
                for field, ids in (('stay_id', stay_ids), ('vehicle_id', vehicle_ids),
                                   ('request_id', request_ids), ('pass_id', pass_ids)):
                    value = row.get(field)
                    if value is None or value == '':
                        if field in required:
                            findings[collection + '_missing_' + field] += 1
                    elif field == 'stay_id' and value in ('administrative_passes', 'unknown'):
                        # Non-guest history is legitimate. Preserve its category;
                        # the target model needs nullable stay_id, not fake stays.
                        findings[collection + '_non_guest_context'] += 1
                    elif field == 'stay_id' and value == 'current':
                        # Never attach an old event to today's current stay.
                        findings[collection + '_historical_current_context_review'] += 1
                    elif _identity(value) not in ids:
                        findings[collection + '_unresolved_' + field] += 1
        target_counts = {name: conn.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in TARGETS}
        # No automatic cutover even with no findings: mapping and shadow parity
        # for every business domain remain separate acceptance gates.
        return {'installed': True, 'source': 'current_sql_runtime_documents',
                'read_only': True, 'consistent_snapshot': True, 'schema_version': 11,
                'timezone': prop[0], 'inventory': inventory, 'target_counts': target_counts,
                'findings': dict(sorted((k,v) for k,v in findings.items() if v)),
                'current_stay': current_status,
                'plan_sha256': hashlib.sha256(json.dumps([2, prop[0], fingerprints, target_counts], sort_keys=True).encode()).hexdigest(),
                'audit_version': 2,
                'cutover_ready': False, 'normalized_business_rows_written': False,
                'coverage': 'inventory_and_identity_checks_only',
                'remaining_gates': ['domain_mapping', 'historical_reconciliation', 'shadow_parity', 'controlled_cutover'],
                'row_data_exposed': False, 'queues_replayed': False}
