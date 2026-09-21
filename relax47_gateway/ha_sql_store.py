"""Small HA Store-compatible adapter. SQLite is authoritative; no JSON fallback.

Copied beside each explicitly migrated component, avoiding changes to HA itself.
Preserves complete envelopes, including unknown fields and unmodified dates.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

STORES = frozenset((
    'relax47_guest_journal.runtime', 'relax47_pass_gateway.runtime',
    'relax47_zone_access.runtime', 'relax47_ai.runtime',
    'relax47_stage75.runtime', 'relax47_integrations.runtime',
    'relax47_rbac.roles', 'relax47_realtycalendar.runtime',
))


def connect(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError('Unsafe RELAX47 SQL path')
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=15)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA trusted_schema=OFF')
    return conn


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
        row = conn.execute("SELECT value FROM relax47_meta WHERE key='runtime_backend'").fetchone()
        if row != ('sqlite_store_v1',):
            raise RuntimeError('RELAX47 SQL backend is not activated')

    def _load(self):
        with closing(connect(self.path)) as conn:
            self._check(conn)
            row = conn.execute(
                "SELECT version,payload_json FROM runtime_documents "
                "WHERE property_id='relax47' AND namespace=? AND item_key='ha_store'",
                (self.key,)).fetchone()
            if row is None:
                raise RuntimeError('Migrated RELAX47 Store is missing; JSON fallback forbidden')
            envelope = json.loads(row[1])
            if (envelope.get('key') != self.key or envelope.get('version') != self.version
                    or envelope.get('minor_version', 1) != 1 or 'data' not in envelope):
                raise RuntimeError('Unsupported migrated Store envelope')
            return row[0], envelope

    async def async_load(self):
        async with self.lock:
            self.revision, self.envelope = await self.hass.async_add_executor_job(self._load)
            # Isolate the saved envelope from the component's mutable state.
            return json.loads(json.dumps(self.envelope['data'], ensure_ascii=False))

    def _save(self, payload):
        with closing(connect(self.path)) as conn, conn:
            self._check(conn)
            changed = conn.execute(
                "UPDATE runtime_documents SET payload_json=?,version=version+1,updated_at=? "
                "WHERE property_id='relax47' AND namespace=? AND item_key='ha_store' AND version=?",
                (payload, datetime.now(timezone.utc).isoformat(), self.key, self.revision)).rowcount
            if changed != 1:
                raise RuntimeError('Concurrent RELAX47 Store update; stale write rejected')

    async def async_save(self, data):
        async with self.lock:
            if self.revision is None:
                raise RuntimeError('Load RELAX47 Store before saving')
            envelope = dict(self.envelope)
            envelope['data'] = data
            # Serialize in the HA event loop before handing work to another thread.
            payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
            # Cancelling an HA coroutine does not stop SQLite's executor thread.
            # Keep the lock until its transaction finishes, otherwise a committed
            # save can leave self.revision stale and break subsequent saves.
            pending = asyncio.ensure_future(self.hass.async_add_executor_job(self._save, payload))
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            pending.result()  # A failed transaction must not advance local state.
            self.revision += 1
            self.envelope = json.loads(payload)
            if cancelled:
                raise asyncio.CancelledError
