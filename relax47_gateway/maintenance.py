#!/usr/bin/env python3
"""Constrained, audited file transfer and Supervisor maintenance for RELAX47."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 512 * 1024
ALLOWED_SUFFIXES = {
    ".tar", ".tgz", ".gz", ".zip", ".json", ".yaml", ".yml", ".md", ".txt",
    ".py", ".js", ".css", ".html", ".svg", ".png", ".jpg", ".jpeg", ".webp",
    ".db", ".sqlite", ".sqlite3",
}
TEXT_SUFFIXES = {".json", ".yaml", ".yml", ".md", ".txt", ".py", ".js", ".css", ".html", ".svg"}
PROTECTED_CONFIG_PARTS = {".storage", ".cloud", ".ssh", ".git"}
PROTECTED_CONFIG_NAMES = {
    "secrets.yaml", "home-assistant_v2.db", "auth", "auth_provider.homeassistant",
    "onboarding", "hassio", "core.config_entries",
}
LOCAL_GATEWAY_ADDON = "local_relax47_gateway"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _atomic_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _atomic_bytes(path: Path, value: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _valid_upload_id(value: Any) -> str:
    identifier = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Invalid upload_id")
    return identifier


def _valid_backup_slug(value: Any) -> str:
    slug = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{4,128}", slug):
        raise ValueError("Invalid backup_slug")
    return slug


def _valid_filename(value: Any) -> str:
    filename = str(value or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", filename)
        or Path(filename).name != filename
        or Path(filename).suffix.lower() not in ALLOWED_SUFFIXES
    ):
        raise ValueError("filename must be a safe basename with an approved extension")
    return filename


def _public_upload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: meta.get(key)
        for key in (
            "upload_id", "filename", "kind", "size_bytes", "received_bytes",
            "created_at", "updated_at", "state", "backup_slug", "share_path",
            "target_path",
        )
        if meta.get(key) is not None
    }


class MaintenanceManager:
    """Receive verified chunks and perform explicitly scoped Supervisor actions."""

    def __init__(
        self,
        *,
        data_dir: Path,
        config_dir: Path,
        share_dir: Path,
        supervisor_token: str,
        current_version: str,
        backup_callback: Callable[[str], Any],
        audit_callback: Callable[..., dict[str, Any]],
    ) -> None:
        self.data_dir = data_dir
        self.config_dir = config_dir
        self.share_dir = share_dir
        self.supervisor_token = supervisor_token
        self.current_version = current_version
        self.backup_callback = backup_callback
        self.audit_callback = audit_callback
        self.upload_dir = data_dir / "maintenance_uploads"
        self.pending_path = data_dir / "RELAX47_MAINTENANCE_PENDING.json"
        self.lock = threading.Lock()

    def _config_path(self, value: Any, *, must_exist: bool = False) -> tuple[str, Path]:
        relative = str(value or "").strip().replace("\\", "/").lstrip("/")
        if not relative or len(relative) > 240 or "\x00" in relative:
            raise ValueError("Invalid config path")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Config path must stay inside the Home Assistant config directory")
        if any(part in PROTECTED_CONFIG_PARTS for part in candidate.parts):
            raise PermissionError("Direct access to protected Home Assistant storage is blocked")
        if candidate.name in PROTECTED_CONFIG_NAMES:
            raise PermissionError("Direct access to credential or database files is blocked")
        resolved_root = self.config_dir.resolve()
        resolved = (self.config_dir / candidate).resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise PermissionError("Config path escaped the Home Assistant config directory")
        if must_exist and not resolved.exists():
            raise KeyError("Home Assistant config path was not found")
        return candidate.as_posix(), resolved

    @staticmethod
    def _redact_text(value: str) -> tuple[str, int]:
        pattern = re.compile(
            r"(?im)([\"']?(?:password|passwd|token|api_key|secret|client_secret|"
            r"access_token|refresh_token|private_key)[\"']?\s*[:=]\s*)([^\r\n]+)"
        )
        return pattern.subn(r"\1***", value)

    def _meta_path(self, upload_id: str) -> Path:
        return self.upload_dir / f"{upload_id}.json"

    def _part_path(self, upload_id: str) -> Path:
        return self.upload_dir / f"{upload_id}.part"

    def _meta(self, upload_id: Any) -> dict[str, Any]:
        identifier = _valid_upload_id(upload_id)
        meta = _load_json(self._meta_path(identifier), {})
        if not isinstance(meta, dict) or meta.get("upload_id") != identifier:
            raise KeyError("Upload transaction was not found")
        return meta

    def _supervisor_api(
        self,
        path: str,
        *,
        method: str = "GET",
        data: Any = None,
        timeout: float = 60.0,
    ) -> Any:
        if not self.supervisor_token:
            raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
        payload = None if data is None else json.dumps(data).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.supervisor_token}",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://supervisor{path}", data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Supervisor request failed with HTTP {exc.code}") from exc
        result = json.loads(body.decode("utf-8")) if body else {}
        if isinstance(result, dict) and result.get("result") == "error":
            raise RuntimeError("Supervisor rejected the maintenance request")
        return result.get("data", result) if isinstance(result, dict) else result

    def _upload_backup(self, path: Path, filename: str) -> dict[str, Any]:
        if not self.supervisor_token:
            raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
        boundary = f"relax47-{uuid.uuid4().hex}"
        preamble = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/x-tar\r\n\r\n"
        ).encode("ascii")
        ending = f"\r\n--{boundary}--\r\n".encode("ascii")
        content_length = len(preamble) + path.stat().st_size + len(ending)
        connection = http.client.HTTPConnection("supervisor", timeout=600)
        try:
            connection.putrequest("POST", "/backups/new/upload")
            connection.putheader("Accept", "application/json")
            connection.putheader("Authorization", f"Bearer {self.supervisor_token}")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            connection.send(preamble)
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    connection.send(block)
            connection.send(ending)
            response = connection.getresponse()
            body = response.read(4 * 1024 * 1024)
            if response.status >= 300:
                raise RuntimeError(f"Supervisor backup upload failed with HTTP {response.status}")
        finally:
            connection.close()
        result = json.loads(body.decode("utf-8")) if body else {}
        if isinstance(result, dict) and result.get("result") == "error":
            raise RuntimeError("Supervisor rejected the backup archive")
        data = result.get("data", result) if isinstance(result, dict) else {}
        if not isinstance(data, dict) or not (data.get("slug") or data.get("backup_slug")):
            raise RuntimeError("Supervisor did not return the uploaded backup slug")
        return data

    def begin(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        filename = _valid_filename(arguments.get("filename"))
        kind = str(arguments.get("kind") or "backup").strip().lower()
        if kind not in {"backup", "share", "config"}:
            raise ValueError("kind must be backup, share or config")
        if kind == "backup" and Path(filename).suffix.lower() != ".tar":
            raise ValueError("Home Assistant backup uploads must use a .tar archive")
        size_bytes = int(arguments.get("size_bytes") or 0)
        if not 1 <= size_bytes <= MAX_UPLOAD_BYTES:
            raise ValueError(f"size_bytes must be between 1 and {MAX_UPLOAD_BYTES}")
        sha256 = str(arguments.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must contain exactly 64 lowercase hexadecimal characters")
        config_target = None
        if kind == "config":
            config_target, _ = self._config_path(arguments.get("target_path"))
            if Path(config_target).suffix.lower() not in ALLOWED_SUFFIXES:
                raise ValueError("Config target extension is not approved")

        with self.lock:
            upload_id = uuid.uuid4().hex
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            part_path = self._part_path(upload_id)
            with part_path.open("xb"):
                pass
            os.chmod(part_path, 0o600)
            meta = {
                "upload_id": upload_id,
                "filename": filename,
                "kind": kind,
                "size_bytes": size_bytes,
                "received_bytes": 0,
                "sha256": sha256,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "state": "receiving",
            }
            if config_target is not None:
                meta["target_path"] = config_target
            _atomic_json(self._meta_path(upload_id), meta)
            audit = self.audit_callback(
                action="maintenance.upload.begin",
                reason=reason,
                before={},
                after={"upload_id": upload_id, "filename": filename, "kind": kind, "size_bytes": size_bytes},
                verified=True,
                changed=True,
            )
            return {**_public_upload(meta), "max_chunk_bytes": MAX_CHUNK_BYTES, "change_id": audit["change_id"]}

    def chunk(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        upload_id = _valid_upload_id(arguments.get("upload_id"))
        offset = int(arguments.get("offset") or 0)
        encoded = str(arguments.get("data_base64") or "")
        try:
            block = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data_base64 is not valid base64") from exc
        if not 1 <= len(block) <= MAX_CHUNK_BYTES:
            raise ValueError(f"Decoded chunk must be between 1 and {MAX_CHUNK_BYTES} bytes")

        with self.lock:
            meta = self._meta(upload_id)
            if meta.get("state") != "receiving":
                raise RuntimeError("Upload transaction is not accepting chunks")
            received = int(meta.get("received_bytes") or 0)
            if offset != received:
                raise ValueError(f"Chunk offset must equal the next expected offset {received}")
            if received + len(block) > int(meta["size_bytes"]):
                raise ValueError("Chunk exceeds the declared upload size")
            part_path = self._part_path(upload_id)
            with part_path.open("ab") as handle:
                handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            meta["received_bytes"] = received + len(block)
            meta["updated_at"] = _now_iso()
            _atomic_json(self._meta_path(upload_id), meta)
            audit = self.audit_callback(
                action="maintenance.upload.chunk",
                reason=reason,
                before={"upload_id": upload_id, "received_bytes": received},
                after={"upload_id": upload_id, "received_bytes": meta["received_bytes"], "chunk_bytes": len(block)},
                verified=part_path.stat().st_size == meta["received_bytes"],
                changed=True,
            )
            return {**_public_upload(meta), "complete": meta["received_bytes"] == meta["size_bytes"], "change_id": audit["change_id"]}

    def status(self, upload_id: Any = None) -> dict[str, Any]:
        pending = _load_json(self.pending_path, {})
        if isinstance(pending, dict) and pending.get("expected_version") == self.current_version:
            if pending.get("state") not in {"verified", "failed"}:
                pending["state"] = "verified"
                pending["verified_at"] = _now_iso()
                pending["running_version"] = self.current_version
                _atomic_json(self.pending_path, pending)
        if upload_id:
            uploads = [_public_upload(self._meta(upload_id))]
        else:
            uploads = []
            if self.upload_dir.exists():
                for path in sorted(self.upload_dir.glob("*.json"))[-50:]:
                    meta = _load_json(path, {})
                    if isinstance(meta, dict) and meta.get("upload_id"):
                        uploads.append(_public_upload(meta))
        return {
            "current_version": self.current_version,
            "uploads": uploads,
            "pending_update": pending if isinstance(pending, dict) else {},
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_chunk_bytes": MAX_CHUNK_BYTES,
            "supervisor_write_access_requested": True,
        }

    def finalize(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        upload_id = _valid_upload_id(arguments.get("upload_id"))
        with self.lock:
            meta = self._meta(upload_id)
            if meta.get("state") != "receiving":
                raise RuntimeError("Upload transaction cannot be finalized")
            part_path = self._part_path(upload_id)
            actual_size = part_path.stat().st_size
            if actual_size != int(meta["size_bytes"]) or actual_size != int(meta["received_bytes"]):
                raise RuntimeError("Uploaded size does not match the declared size")
            digest = hashlib.sha256()
            with part_path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
            if digest.hexdigest() != meta["sha256"]:
                raise RuntimeError("Uploaded SHA-256 does not match")

            if meta["kind"] == "backup":
                response = self._upload_backup(part_path, meta["filename"])
                meta["backup_slug"] = str(response.get("slug") or response.get("backup_slug"))
                meta["state"] = "uploaded_to_supervisor"
            elif meta["kind"] == "share":
                target_dir = self.share_dir / "RELAX47_UPLOADS"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / meta["filename"]
                os.replace(part_path, target)
                os.chmod(target, 0o600)
                meta["share_path"] = str(target)
                meta["state"] = "stored_in_share"
            else:
                stage_dir = self.data_dir / "config_staged"
                stage_dir.mkdir(parents=True, exist_ok=True)
                target = stage_dir / f"{upload_id}-{meta['filename']}"
                os.replace(part_path, target)
                os.chmod(target, 0o600)
                meta["staged_path"] = str(target)
                meta["state"] = "staged_config"
            if part_path.exists():
                part_path.unlink()
            meta["updated_at"] = _now_iso()
            _atomic_json(self._meta_path(upload_id), meta)
            audit = self.audit_callback(
                action="maintenance.upload.finalize",
                reason=reason,
                before={"upload_id": upload_id, "state": "receiving"},
                after=_public_upload(meta),
                verified=True,
                changed=True,
            )
            return {**_public_upload(meta), "sha256_verified": True, "change_id": audit["change_id"]}

    def config_list(self, relative_path: Any = "") -> dict[str, Any]:
        if str(relative_path or "").strip():
            relative, path = self._config_path(relative_path, must_exist=True)
        else:
            relative, path = "", self.config_dir.resolve()
        if not path.is_dir():
            raise ValueError("Config list path must be a directory")
        items = []
        for item in sorted(path.iterdir(), key=lambda value: value.name.lower())[:500]:
            if item.name in PROTECTED_CONFIG_PARTS or item.name in PROTECTED_CONFIG_NAMES:
                continue
            stat = item.stat()
            items.append({
                "name": item.name,
                "path": f"{relative}/{item.name}".lstrip("/"),
                "type": "directory" if item.is_dir() else "file",
                "size_bytes": stat.st_size if item.is_file() else None,
                "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
            })
        return {"path": relative, "count": len(items), "items": items, "protected_storage_hidden": True}

    def config_read(self, relative_path: Any) -> dict[str, Any]:
        relative, path = self._config_path(relative_path, must_exist=True)
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("Only approved text configuration files can be read")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Config file is larger than the 1 MiB read limit")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        redacted, redactions = self._redact_text(text)
        return {
            "path": relative,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": redacted,
            "redactions": redactions,
            "replace_whole_file_safe": redactions == 0,
            "protected_storage_hidden": True,
        }

    def _core_check(self) -> Any:
        return self._supervisor_api("/core/check", method="POST", data={}, timeout=180)

    def apply_config_upload(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        upload_id = _valid_upload_id(arguments.get("upload_id"))
        restart = bool(arguments.get("restart_home_assistant", False))
        expected_current = str(arguments.get("expected_current_sha256") or "").strip().lower()
        if expected_current and not re.fullmatch(r"[0-9a-f]{64}", expected_current):
            raise ValueError("expected_current_sha256 must be a SHA-256 digest")
        with self.lock:
            meta = self._meta(upload_id)
            if meta.get("kind") != "config" or meta.get("state") != "staged_config":
                raise RuntimeError("Upload is not a finalized config transaction")
            relative, target = self._config_path(meta.get("target_path"))
            if target.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or Path(meta.get("filename", "")).suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                raise PermissionError("Database uploads must use sql_install_upload")
            staged = Path(str(meta.get("staged_path") or ""))
            if not staged.is_file() or staged.parent != (self.data_dir / "config_staged"):
                raise RuntimeError("Staged configuration file is unavailable")
            existed = target.exists()
            before = target.read_bytes() if existed and target.is_file() else None
            before_sha = hashlib.sha256(before).hexdigest() if before is not None else None
            if expected_current and before_sha != expected_current:
                raise RuntimeError("Current config file changed since it was inspected")
            if existed and not expected_current and not bool(arguments.get("confirm_replace_existing", False)):
                raise PermissionError("Replacing an existing config file requires its current SHA-256 or explicit confirmation")
            replacement = staged.read_bytes()
            safety = self.backup_callback(f"Safety backup before config write {relative}: {reason}")
            mode = (target.stat().st_mode & 0o777) if existed else 0o644
            try:
                _atomic_bytes(target, replacement, mode=mode)
                check = self._core_check()
            except Exception:
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_bytes(target, before, mode=mode)
                try:
                    self._core_check()
                except Exception:
                    pass
                raise
            after_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            if after_sha != meta["sha256"]:
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_bytes(target, before, mode=mode)
                raise RuntimeError("Config write verification failed and was rolled back")
            meta["state"] = "config_applied"
            meta["applied_at"] = _now_iso()
            meta["updated_at"] = _now_iso()
            _atomic_json(self._meta_path(upload_id), meta)
            audit = self.audit_callback(
                action="maintenance.config.apply_upload",
                reason=reason,
                before={"path": relative, "sha256": before_sha, "existed": existed},
                after={"path": relative, "sha256": after_sha, "config_check": True, "restart_requested": restart},
                verified=True,
                changed=before_sha != after_sha,
            )
            if restart:
                self._supervisor_api("/core/restart", method="POST", data={}, timeout=60)
            return {
                "applied": True,
                "path": relative,
                "sha256": after_sha,
                "config_check_passed": True,
                "restart_requested": restart,
                "safety_backup_created": bool(safety),
                "check_response": check,
                "change_id": audit["change_id"],
            }

    def patch_config(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        relative, target = self._config_path(arguments.get("path"), must_exist=True)
        if not target.is_file() or target.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("Only approved text configuration files can be patched")
        expected_sha = str(arguments.get("expected_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ValueError("expected_sha256 is required")
        replacements = arguments.get("replacements")
        if not isinstance(replacements, list) or not 1 <= len(replacements) <= 20:
            raise ValueError("replacements must contain between 1 and 20 exact replacements")
        restart = bool(arguments.get("restart_home_assistant", False))
        with self.lock:
            before = target.read_bytes()
            before_sha = hashlib.sha256(before).hexdigest()
            if before_sha != expected_sha:
                raise RuntimeError("Current config file changed since it was inspected")
            text = before.decode("utf-8")
            changed = text
            counts = []
            for item in replacements:
                if not isinstance(item, dict):
                    raise ValueError("Each replacement must be an object")
                old = str(item.get("old") or "")
                new = str(item.get("new") or "")
                expected_count = int(item.get("expected_count", 1))
                if not old or len(old) > 131072 or len(new) > 131072 or not 1 <= expected_count <= 1000:
                    raise ValueError("Replacement size or expected_count is invalid")
                actual = changed.count(old)
                if actual != expected_count:
                    raise RuntimeError("Exact replacement count did not match; config was not changed")
                changed = changed.replace(old, new)
                counts.append(actual)
            if changed == text:
                raise ValueError("Patch produced no change")
            safety = self.backup_callback(f"Safety backup before config patch {relative}: {reason}")
            mode = target.stat().st_mode & 0o777
            try:
                _atomic_bytes(target, changed.encode("utf-8"), mode=mode)
                check = self._core_check()
            except Exception:
                _atomic_bytes(target, before, mode=mode)
                try:
                    self._core_check()
                except Exception:
                    pass
                raise
            after_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            audit = self.audit_callback(
                action="maintenance.config.patch",
                reason=reason,
                before={"path": relative, "sha256": before_sha},
                after={"path": relative, "sha256": after_sha, "replacement_counts": counts, "config_check": True, "restart_requested": restart},
                verified=True,
                changed=True,
            )
            if restart:
                self._supervisor_api("/core/restart", method="POST", data={}, timeout=60)
            return {
                "patched": True,
                "path": relative,
                "before_sha256": before_sha,
                "after_sha256": after_sha,
                "replacement_counts": counts,
                "config_check_passed": True,
                "restart_requested": restart,
                "safety_backup_created": bool(safety),
                "check_response": check,
                "change_id": audit["change_id"],
            }

    def backup_list(self) -> dict[str, Any]:
        data = self._supervisor_api("/backups", timeout=30)
        rows = data.get("backups", []) if isinstance(data, dict) else []
        result = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            result.append({
                key: item.get(key)
                for key in ("slug", "name", "date", "type", "size", "protected", "compressed", "location", "content")
                if key in item
            })
        return {"count": len(result), "backups": result, "secrets_exposed": False}

    def addon_list(self) -> dict[str, Any]:
        data = self._supervisor_api("/addons", timeout=30)
        rows = data.get("addons", []) if isinstance(data, dict) else []
        result = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            result.append({
                key: item.get(key)
                for key in (
                    "slug", "name", "state", "version", "version_latest",
                    "update_available", "repository", "installed", "available",
                )
                if key in item
            })
        return {"count": len(result), "addons": result, "secrets_exposed": False}

    def addon_action(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        slug = str(arguments.get("addon_slug") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", slug):
            raise ValueError("Invalid addon_slug")
        action = str(arguments.get("action") or "").strip().lower()
        if action not in {"start", "stop", "restart", "update", "install"}:
            raise ValueError("Unsupported addon action")
        safety = None
        if action in {"update", "install"}:
            safety = self.backup_callback(f"Safety backup before addon {action} {slug}: {reason}")
        payload: dict[str, Any] = {}
        if action == "update":
            payload = {"backup": False, "background": True}
        elif action == "install":
            payload = {"background": True}
        response = self._supervisor_api(
            f"/addons/{urllib.parse.quote(slug, safe='')}/{action}",
            method="POST",
            data=payload,
            timeout=180,
        )
        audit = self.audit_callback(
            action=f"maintenance.addon.{action}",
            reason=reason,
            before={"addon_slug": slug},
            after={"addon_slug": slug, "action": action, "accepted": True},
            verified=action in {"start", "stop", "restart"},
            changed=True,
        )
        return {
            "accepted": True,
            "addon_slug": slug,
            "action": action,
            "safety_backup_created": bool(safety),
            "supervisor_response": response,
            "change_id": audit["change_id"],
        }

    def cleanup_backups(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        keep_last = int(arguments.get("keep_last", 3))
        if keep_last < 3 or keep_last > 30:
            raise ValueError("keep_last must be between 3 and 30")
        dry_run = bool(arguments.get("dry_run", True))
        data = self._supervisor_api("/backups", timeout=30)
        rows = data.get("backups", []) if isinstance(data, dict) else []
        rows = [r for r in rows if isinstance(r, dict)]
        rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
        protected = []
        candidates = []
        for idx, row in enumerate(rows):
            slug = str(row.get("slug") or "")
            name = str(row.get("name") or "")
            keep = idx < keep_last or bool(row.get("protected")) or "NEVER DELETE" in name.upper()
            item = {"slug": slug, "name": name, "date": row.get("date"), "size": row.get("size")}
            (protected if keep else candidates).append(item)
        if dry_run:
            return {"dry_run": True, "keep_last": keep_last, "kept": protected, "would_delete": candidates,
                    "would_delete_count": len(candidates), "deleted_count": 0}
        deleted=[]
        failed=[]
        for item in candidates:
            try:
                self._supervisor_api(f"/backups/{urllib.parse.quote(item['slug'], safe='')}", method="DELETE", timeout=30)
                deleted.append(item)
            except Exception as exc:
                failed.append({"slug": item["slug"], "name": item["name"], "error": type(exc).__name__})
        audit = self.audit_callback(action="maintenance.backup.cleanup", reason=reason,
            before={"count": len(rows)}, after={"keep_last": keep_last, "deleted": len(deleted), "failed": len(failed)},
            verified=(len(failed)==0), changed=bool(deleted))
        return {"dry_run": False, "keep_last": keep_last, "deleted": deleted, "deleted_count": len(deleted),
                "failed": failed, "kept": protected, "change_id": audit["change_id"]}

    def restore_backup(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        slug = _valid_backup_slug(arguments.get("backup_slug"))
        info = self._supervisor_api(f"/backups/{urllib.parse.quote(slug, safe='')}/info", timeout=30)
        content = info.get("content", {}) if isinstance(info, dict) else {}
        available_folders = set(content.get("folders") or []) if isinstance(content, dict) else set()
        available_addons = set(content.get("addons") or []) if isinstance(content, dict) else set()
        folders = [str(item) for item in (arguments.get("folders") or [])]
        addons = [str(item) for item in (arguments.get("addons") or [])]
        homeassistant = bool(arguments.get("homeassistant", False))
        if not folders and not addons and not homeassistant:
            raise ValueError("Select at least one backup component to restore")
        if not set(folders).issubset(available_folders):
            raise ValueError("Requested folders are not present in the selected backup")
        if not set(addons).issubset(available_addons):
            raise ValueError("Requested addons are not present in the selected backup")
        if homeassistant and not bool(content.get("homeassistant")):
            raise ValueError("Home Assistant configuration is not present in the selected backup")

        safety = self.backup_callback(f"Safety backup before partial restore: {reason}")
        payload: dict[str, Any] = {"background": True}
        if folders:
            payload["folders"] = folders
        if addons:
            payload["addons"] = addons
        if homeassistant:
            payload["homeassistant"] = True
        response = self._supervisor_api(
            f"/backups/{urllib.parse.quote(slug, safe='')}/restore/partial",
            method="POST",
            data=payload,
            timeout=120,
        )
        audit = self.audit_callback(
            action="maintenance.backup.restore_partial",
            reason=reason,
            before={"safety_backup": bool(safety)},
            after={"backup_slug": slug, "folders": folders, "addons": addons, "homeassistant": homeassistant, "scheduled": True},
            verified=False,
            changed=True,
        )
        return {
            "scheduled": True,
            "backup_slug": slug,
            "folders": folders,
            "addons": addons,
            "homeassistant": homeassistant,
            "supervisor_response": response,
            "change_id": audit["change_id"],
        }

    def apply_gateway_update(self, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
        own = self._supervisor_api("/addons/self/info", timeout=30)
        if not isinstance(own, dict) or own.get("slug") != LOCAL_GATEWAY_ADDON:
            raise PermissionError("Repository installations must update through Supervisor Store; local backup restore is only for local_relax47_gateway")
        slug = _valid_backup_slug(arguments.get("backup_slug"))
        expected_version = str(arguments.get("expected_version") or "").strip()
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version):
            raise ValueError("expected_version must use semantic version format")
        info = self._supervisor_api(f"/backups/{urllib.parse.quote(slug, safe='')}/info", timeout=30)
        content = info.get("content", {}) if isinstance(info, dict) else {}
        folders = set(content.get("folders") or []) if isinstance(content, dict) else set()
        if "addons/local" not in folders:
            raise ValueError("Selected backup does not contain addons/local")

        safety = self.backup_callback(f"Safety backup before RELAX47 gateway update: {reason}")
        self._supervisor_api(
            f"/backups/{urllib.parse.quote(slug, safe='')}/restore/partial",
            method="POST",
            data={"folders": ["addons/local"], "background": False},
            timeout=600,
        )
        self._supervisor_api("/store/reload", method="POST", data={}, timeout=120)
        addon = self._supervisor_api(f"/addons/{LOCAL_GATEWAY_ADDON}/info", timeout=60)
        installed = str(addon.get("version") or "") if isinstance(addon, dict) else ""
        latest = str(addon.get("version_latest") or "") if isinstance(addon, dict) else ""
        if installed == expected_version:
            return {"scheduled": False, "already_installed": True, "version": installed}
        if latest and latest != expected_version:
            raise RuntimeError("Supervisor did not detect the expected local gateway version")

        pending = {
            "state": "scheduled",
            "scheduled_at": _now_iso(),
            "backup_slug": slug,
            "expected_version": expected_version,
            "previous_version": installed or self.current_version,
            "safety_backup_created": bool(safety),
        }
        _atomic_json(self.pending_path, pending)
        audit = self.audit_callback(
            action="maintenance.gateway.update",
            reason=reason,
            before={"version": installed or self.current_version},
            after={"expected_version": expected_version, "scheduled": True},
            verified=False,
            changed=True,
        )

        def update_worker() -> None:
            time.sleep(1.0)
            try:
                response = self._supervisor_api(
                    f"/addons/{LOCAL_GATEWAY_ADDON}/update",
                    method="POST",
                    data={"backup": False, "background": True},
                    timeout=120,
                )
                state = _load_json(self.pending_path, pending)
                if isinstance(state, dict):
                    state["state"] = "update_request_accepted"
                    state["accepted_at"] = _now_iso()
                    state["supervisor_job_present"] = bool(response)
                    _atomic_json(self.pending_path, state)
            except Exception as exc:
                state = _load_json(self.pending_path, pending)
                if isinstance(state, dict):
                    state["state"] = "failed"
                    state["failed_at"] = _now_iso()
                    state["error"] = type(exc).__name__
                    _atomic_json(self.pending_path, state)

        threading.Thread(target=update_worker, name="relax47-self-update", daemon=True).start()
        return {
            "scheduled": True,
            "expected_version": expected_version,
            "previous_version": installed or self.current_version,
            "safety_backup_created": bool(safety),
            "connection_may_restart": True,
            "change_id": audit["change_id"],
        }
