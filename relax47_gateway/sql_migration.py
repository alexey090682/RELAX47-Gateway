"""Bounded RELAX47 installation through SQLite transactions, never inode replacement."""
from __future__ import annotations

from contextlib import closing
import hashlib
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

SUPPORTED_SCHEMA_VERSION = 11
TARGET = "relax47_v8/relax47.db"


def _validate_schema(conn, version):
    """New preservation/media schemas must match the shipped structural contract.

    This validates an upload; it never runs migrations against user data.
    Older schemas retain their existing validation for rollback compatibility.
    Additional application objects are preserved, not removed.
    """
    if version < 10:
        return "legacy_identity"
    reference = Path(__file__).with_name("sql_schemas") / f"schema-{version}.sql"
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(reference.read_text(encoding="utf-8"))
        for name, kind, definition in expected.execute(
                "SELECT name,type,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"):
            actual = conn.execute(
                "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if actual is None or actual[0] != kind:
                raise ValueError(f"Schema {version}: missing required {kind} {name}")
            # Canonical definitions retain CHECKs, composite FKs, unique/partial
            # indexes and journal view semantics, not just names/row counts.
            if actual[1].strip() != definition.strip():
                raise ValueError(f"Schema {version}: incompatible definition for {name}")
    return "canonical_contract"


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def _read(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


class Relax47SQLManager:
    def __init__(self, *, data_dir, config_dir, backup_callback, audit_callback,
                 stop_callback=None, start_callback=None):
        self.data_dir = Path(data_dir)
        self.config_dir = Path(config_dir)
        self.database_path = self.config_dir / TARGET
        self.backup_dir = self.data_dir / "sql_backups"
        self.upload_dir = self.data_dir / "maintenance_uploads"
        self.backup_callback = backup_callback
        self.audit_callback = audit_callback
        self.stop_callback = stop_callback
        self.start_callback = start_callback
        self.lock = threading.RLock()

    def _target(self):
        root = self.config_dir.resolve()
        if self.database_path.parent.is_symlink() or self.database_path.is_symlink():
            raise PermissionError("SQL target must not be a symlink")
        if not self.database_path.resolve().is_relative_to(root):
            raise PermissionError("SQL target escaped the config directory")
        return self.database_path

    def _meta(self, uid):
        uid = str(uid or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", uid):
            raise ValueError("Invalid upload_id")
        meta = json.loads((self.upload_dir / f"{uid}.json").read_text())
        if (meta.get("upload_id") != uid or meta.get("kind") != "config"
                or meta.get("state") != "staged_config" or meta.get("target_path") != TARGET):
            raise ValueError("Expected a finalized config upload for " + TARGET)
        name = str(meta.get("filename", ""))
        if Path(name).name != name or Path(name).suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise ValueError("Not a SQLite upload")
        path = self.data_dir / "config_staged" / f"{uid}-{name}"
        if (path.is_symlink() or path.parent.is_symlink()
                or Path(meta.get("staged_path", "")) != path or not path.is_file()):
            raise PermissionError("Invalid staged database path")
        if path.stat().st_size != meta.get("size_bytes") or _sha(path) != meta.get("sha256"):
            raise ValueError("Uploaded size or SHA-256 verification failed")
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise ValueError("Upload must be a standalone SQLite backup")
        return meta, path

    @staticmethod
    def _inspect(path):
        with closing(_read(path)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + 30
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            for check in ("quick_check", "integrity_check"):
                if conn.execute("PRAGMA " + check).fetchall() != [("ok",)]:
                    raise ValueError(check + " failed")
            try:
                foreign_key_error = conn.execute("PRAGMA foreign_key_check").fetchone()
            except sqlite3.Error as exc:
                raise ValueError("foreign_key_check failed: invalid schema relationships") from exc
            if foreign_key_error is not None:
                raise ValueError("foreign_key_check failed")
            try:
                row = conn.execute("SELECT value FROM relax47_meta WHERE key='schema_version'").fetchone()
                schema = int(row[0]) if row else 0
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise ValueError("Missing RELAX47 schema identity") from exc
            if not 1 <= schema <= SUPPORTED_SCHEMA_VERSION:
                raise ValueError("Unsupported RELAX47 schema version")
            schema_validation = _validate_schema(conn, schema)
            objects = []
            for name, kind in conn.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type,name"):
                count = None
                if kind == "table":
                    count = conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                objects.append({"name": name, "type": kind, "row_count": count})
        return {"sha256": _sha(path), "size_bytes": path.stat().st_size,
                "schema_version": schema, "supported_schema_version": SUPPORTED_SCHEMA_VERSION,
                "schema_validation": schema_validation,
                "quick_check": "ok", "integrity_check": "ok", "foreign_key_check": "ok",
                "objects": objects, "row_data_exposed": False}

    @staticmethod
    def _copy_database(source, destination):
        # Backup includes WAL and atomically commits into the existing destination inode.
        deadline = time.monotonic() + 30
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("SQLite backup timed out")
        with closing(_read(source)) as src, closing(sqlite3.connect(destination, timeout=5)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)

    def database_status(self):
        with self.lock:
            target = self._target()
            if not target.exists():
                return {"installed": False, "path": TARGET,
                        "supported_schema_version": SUPPORTED_SCHEMA_VERSION}
            self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            snapshot = self.backup_dir / (".status-" + uuid.uuid4().hex + ".db")
            try:
                snapshot.touch(mode=0o600, exist_ok=False)
                self._copy_database(target, snapshot)
                return {**self._inspect(snapshot), "installed": True, "path": TARGET,
                        "sha256_scope": "sqlite_snapshot"}
            finally:
                snapshot.unlink(missing_ok=True)

    def inspect_upload(self, uid):
        with self.lock:
            meta, path = self._meta(uid)
            return {**self._inspect(path), "upload_id": meta["upload_id"],
                    "filename": meta["filename"], "staged": True, "install_target": TARGET}

    def install_upload(self, args, reason):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Gateway and public MCP run in separate processes: a thread lock is insufficient.
        with (self.data_dir / "sql-install.lock").open("a") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._install_locked(args, reason)

    def _install_locked(self, args, reason):
        with self.lock:
            meta, source = self._meta(args.get("upload_id"))
            inspected = self._inspect(source)
            expected = str(args.get("expected_sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != inspected["sha256"]:
                raise ValueError("Matching expected_sha256 is required")
            if (args.get("stop_home_assistant") is not True
                    or args.get("confirm_other_writers_stopped") is not True):
                raise PermissionError("Explicit maintenance window and stopped external writers are required")
            if self.stop_callback is None or self.start_callback is None:
                raise RuntimeError("Supervisor lifecycle callbacks are unavailable")
            target = self._target()
            safety = self.backup_callback("Safety backup before RELAX47 SQL install: " + reason)
            if not isinstance(safety, dict) or safety.get("verified") is not True:
                raise RuntimeError("A verified Home Assistant backup is required")
            self.stop_callback()
            previous = None
            changed = False
            safe_to_start = True
            existed = target.exists()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if existed:
                    current = self._inspect(target)
                    if current["schema_version"] > inspected["schema_version"]:
                        raise ValueError("SQL schema downgrade is not allowed")
                    previous = self.backup_dir / (uuid.uuid4().hex + "-relax47.db")
                    previous.touch(mode=0o600, exist_ok=False)
                    self._copy_database(target, previous)
                    self._inspect(previous)
                self._meta(meta["upload_id"])
                if _sha(source) != expected:
                    raise ValueError("Staged database changed before installation")
                if not existed:
                    target.touch(mode=0o600, exist_ok=False)
                changed = True
                self._copy_database(source, target)
                result = self._inspect(target)
                if result["schema_version"] != inspected["schema_version"] or result["objects"] != inspected["objects"]:
                    raise RuntimeError("Installed database verification failed")
                audit = self.audit_callback(action="maintenance.sql.install", reason=reason,
                    before={"backup_created": bool(previous)},
                    after={"path": TARGET, "source_sha256": expected, "schema_version": result["schema_version"]},
                    verified=True, changed=True)
                return {**result, "installed": True, "path": TARGET, "source_sha256": expected,
                        "database_backup_created": bool(previous), "backup_id": previous.name if previous else None,
                        "change_id": audit["change_id"]}
            except Exception:
                if changed:
                    try:
                        if previous is not None:
                            self._copy_database(previous, target)
                            self._inspect(target)
                        else:
                            target.unlink(missing_ok=True)
                            for suffix in ("-wal", "-shm", "-journal"):
                                Path(str(target) + suffix).unlink(missing_ok=True)
                    except Exception as rollback_error:
                        safe_to_start = False
                        raise RuntimeError("Rollback failed; Core remains stopped; retain SQL backup " + str(previous)) from rollback_error
                raise
            finally:
                if safe_to_start:
                    self.start_callback()
