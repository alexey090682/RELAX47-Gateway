"""Opt-in daily retention, persisted across Gateway restarts."""
import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager


class BackupRetention:
    def __init__(self, manager, writes_enabled):
        self.manager = manager
        self.writes_enabled = writes_enabled
        self.path = manager.data_dir / 'backup-retention.json'
        self.mutex = threading.RLock()

    @contextmanager
    def _guard(self):
        self.manager.data_dir.mkdir(parents=True, exist_ok=True)
        with self.mutex, (self.manager.data_dir / 'backup-retention.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _read(self):
        if not self.path.exists():
            return {'enabled': False, 'keep_last': 3, 'next_run': 0}
        value = json.loads(self.path.read_text())
        if type(value.get('enabled')) is not bool or value.get('keep_last') != 3:
            raise ValueError('Invalid retention policy; cleanup disabled')
        return value

    def _save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.retention-' + uuid.uuid4().hex)
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def configure(self, enabled, reason):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        with self._guard():
            previous = self._read()
            policy = {'enabled': enabled, 'keep_last': 3,
                      'next_run': time.time() + 86400, 'interval_hours': 24}
            self.manager.audit_callback(action='maintenance.backup.retention', reason=reason,
                before={'enabled': previous['enabled']}, after=policy, verified=False, changed=True)
            self._save(policy)
            return self._read()

    def tick(self):
        with self._guard():
            policy = self._read()
            if not self.writes_enabled() or not policy['enabled'] or time.time() < policy['next_run']:
                return
            self.manager.data_dir.mkdir(parents=True, exist_ok=True)
            # Do not remove a safety backup while SQL install/capture is running.
            with (self.manager.data_dir / 'sql-install.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                # Claim before deleting: a restart cannot immediately repeat a run.
                policy['next_run'] = time.time() + 86400
                self._save(policy)
                result = self.manager.cleanup_backups({'keep_last': 3, 'dry_run': False},
                    'Enabled daily policy: latest three plus protected backups')
                policy['last_deleted_count'] = result['deleted_count']
                policy['last_failed_count'] = len(result['failed'])
                self._save(policy)

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                # Never log credentials or raw server responses.
                print('Backup retention check failed: ' + type(exc).__name__, flush=True)
            time.sleep(60)
