#!/usr/bin/env python3
"""Transactional LocalTuya migration support for RELAX47.

Secrets are collected from Home Assistant's existing official Tuya entry and
stored in a fixed protected local file, or resolved from existing LocalTuya.
They are never accepted as tool arguments or returned.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
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

import relax47_pytuya as pytuya


SUPPORTED_PROTOCOLS = {"3.1", "3.2", "3.3", "3.4"}
SUPPORTED_PLATFORMS = {
    "binary_sensor", "climate", "cover", "fan", "light", "number",
    "select", "sensor", "switch", "vacuum",
}
CRITICAL_WORDS = {
    "alarm", "boiler", "climate", "heating", "lock", "pool", "security",
    "zont", "бассейн", "замок", "котел", "котёл", "отоп", "охран", "сигнал",
}
NONCRITICAL_CLASSES = {"light", "switch_noncritical"}
TUYA_HA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"


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
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _valid_device_id(value: Any) -> str:
    device_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{6,80}", device_id):
        raise ValueError("Invalid Tuya device_id")
    return device_id


def _valid_protocol(value: Any) -> str:
    protocol = str(value or "3.3").strip()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError("protocol_version must be 3.1, 3.2, 3.3 or 3.4")
    return protocol


def _safe_name(value: Any, fallback: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return name[:100] or fallback


class HomeAssistantFlowClient:
    """Small synchronous client for Home Assistant's REST config-flow API."""

    def __init__(self, token: str, api: str = "http://supervisor/core/api") -> None:
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
        self.token = token
        self.api = api.rstrip("/")

    def request(self, path: str, *, method: str = "POST", data: Any = None) -> Any:
        payload = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api}{path}", data=payload, method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                body = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Home Assistant config-flow request failed with HTTP {exc.code}: {detail[:300]}"
            ) from exc
        return json.loads(body.decode("utf-8")) if body else {}

    def start_config_flow(self, handler: str) -> Any:
        return self.request(
            "/config/config_entries/flow",
            data={"handler": handler, "show_advanced_options": True},
        )

    def configure_config_flow(self, flow_id: str, user_input: dict[str, Any]) -> Any:
        return self.request(
            f"/config/config_entries/flow/{urllib.parse.quote(flow_id, safe='')}",
            data=user_input,
        )

    def start_options_flow(self, entry_id: str) -> Any:
        return self.request(
            "/config/config_entries/options/flow",
            data={"handler": entry_id, "show_advanced_options": True},
        )

    def configure_options_flow(self, flow_id: str, user_input: dict[str, Any]) -> Any:
        return self.request(
            f"/config/config_entries/options/flow/{urllib.parse.quote(flow_id, safe='')}",
            data=user_input,
        )

    def delete_entry(self, entry_id: str) -> Any:
        return self.request(
            f"/config/config_entries/entry/{urllib.parse.quote(entry_id, safe='')}",
            method="DELETE",
        )


class LocalTuyaManager:
    """Stage, apply, verify and roll back one LocalTuya device at a time."""

    def __init__(
        self,
        *,
        config_dir: Path,
        share_dir: Path,
        data_dir: Path,
        lan_cidr: str,
        supervisor_token: str,
        backup_callback: Callable[[str], Any],
        audit_callback: Callable[..., dict[str, Any]],
    ) -> None:
        self.config_dir = config_dir
        self.share_dir = share_dir
        self.data_dir = data_dir
        self.network = ipaddress.ip_network(lan_cidr, strict=False)
        self.supervisor_token = supervisor_token
        self.backup_callback = backup_callback
        self.audit_callback = audit_callback
        self.stage_path = data_dir / "RELAX47_LOCALTUYA_STAGE.json"
        self.transaction_dir = data_dir / "localtuya_transactions"
        self.key_file = share_dir / "RELAX47_TUYA_KEYS.json"
        self.lock = threading.Lock()

    def _config_entries(self) -> list[dict[str, Any]]:
        raw = _load_json(
            self.config_dir / ".storage/core.config_entries",
            {"data": {"entries": []}},
        )
        return list(raw.get("data", {}).get("entries", []))

    def _local_entries(self) -> list[dict[str, Any]]:
        return [entry for entry in self._config_entries() if entry.get("domain") == "localtuya"]

    def _official_tuya_entries(self) -> list[dict[str, Any]]:
        return [
            entry for entry in self._config_entries()
            if entry.get("domain") == "tuya" and not entry.get("disabled_by")
        ]

    def _protected_devices(self) -> dict[str, Any]:
        protected = _load_json(self.key_file, {})
        devices = protected.get("devices", protected) if isinstance(protected, dict) else {}
        return dict(devices) if isinstance(devices, dict) else {}

    @staticmethod
    def _valid_local_key(value: Any) -> str:
        key = str(value or "").strip()
        if not 8 <= len(key) <= 128 or re.search(r"[\x00-\x20\x7f]", key):
            return ""
        return key

    def _find_device(self, device_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for entry in self._local_entries():
            devices = entry.get("data", {}).get("devices") or {}
            if isinstance(devices, dict) and device_id in devices:
                return entry, devices[device_id]
        return None

    def _resolve_key(self, device_id: str) -> tuple[str, str]:
        existing = self._find_device(device_id)
        if existing:
            key = str(existing[1].get("local_key") or "")
            if key:
                return key, "existing_localtuya"

        local = _load_json(self.key_file, {})
        devices = local.get("devices", local) if isinstance(local, dict) else {}
        item = devices.get(device_id) if isinstance(devices, dict) else None
        key = item.get("local_key") if isinstance(item, dict) else item
        if isinstance(key, str) and key.strip():
            return key.strip(), "protected_local_file"
        raise KeyError(
            "Local key is unavailable in approved local sources; run "
            "localtuya_sync_keys to collect it from the official Tuya integration"
        )

    def _validate_host(self, value: Any) -> str:
        address = ipaddress.ip_address(str(value or ""))
        if not isinstance(address, ipaddress.IPv4Address) or address not in self.network:
            raise ValueError(f"LocalTuya host must be inside {self.network}")
        return str(address)

    def capabilities(self) -> dict[str, Any]:
        manifest = _load_json(self.config_dir / "custom_components/localtuya/manifest.json", {})
        existing_ids: list[str] = []
        key_count = 0
        for entry in self._local_entries():
            for device_id, device in (entry.get("data", {}).get("devices") or {}).items():
                existing_ids.append(str(device_id))
                if isinstance(device, dict) and device.get("local_key"):
                    key_count += 1
        protected_devices = self._protected_devices()
        protected_count = sum(
            1 for item in protected_devices.values()
            if self._valid_local_key(item.get("local_key") if isinstance(item, dict) else item)
        )
        official_entries = self._official_tuya_entries()
        official_ready = sum(
            1 for entry in official_entries
            if all((entry.get("data") or {}).get(field) for field in (
                "user_code", "terminal_id", "endpoint", "token_info"
            ))
        )
        return {
            "integration_present": bool(manifest),
            "integration_version": manifest.get("version"),
            "config_entries": len(self._local_entries()),
            "configured_devices": len(existing_ids),
            "configured_device_ids": sorted(existing_ids),
            "approved_key_sources": {
                "existing_localtuya": key_count,
                "protected_local_file": protected_count,
                "protected_local_file_present": self.key_file.exists(),
            },
            "official_tuya": {
                "enabled_config_entries": len(official_entries),
                "credential_sets_available": official_ready,
            },
            "supports": {
                "sync_keys_from_official_tuya": True,
                "create_isolated_entry": True,
                "update_existing_device": True,
                "dp_read_probe": True,
                "noncritical_boolean_control_check": True,
                "per_device_rollback": True,
                "continue_batch_after_error": True,
            },
            "secrets_exposed": False,
        }

    def sync_keys_from_official_tuya(self, reason: str) -> dict[str, Any]:
        """Collect local keys through HA's official Tuya sharing session.

        The official config entry is read from HA's read-only config mount.  No
        credential or local key is included in the return value or audit log.
        """
        with self.lock:
            entries = self._official_tuya_entries()
            if not entries:
                raise RuntimeError("No enabled official Tuya config entry was found")

            before_devices = self._protected_devices()
            before_file_exists = self.key_file.exists()
            before_document = _load_json(self.key_file, {})
            before_count = sum(
                1 for item in before_devices.values()
                if self._valid_local_key(item.get("local_key") if isinstance(item, dict) else item)
            )
            manager = None
            wrote_file = False
            cloud_devices = 0
            collected: dict[str, dict[str, Any]] = {}
            try:
                from tuya_sharing import Manager

                for entry in entries:
                    data = entry.get("data") or {}
                    user_code = data.get("user_code")
                    terminal_id = data.get("terminal_id")
                    endpoint = data.get("endpoint")
                    token_info = data.get("token_info")
                    if not all((user_code, terminal_id, endpoint, token_info)):
                        continue
                    if not isinstance(token_info, dict):
                        continue

                    manager = Manager(
                        TUYA_HA_CLIENT_ID,
                        str(user_code),
                        str(terminal_id),
                        str(endpoint),
                        dict(token_info),
                    )
                    manager.update_device_cache()
                    device_map = getattr(manager, "device_map", {})
                    if not isinstance(device_map, dict):
                        raise RuntimeError("Tuya sharing SDK returned an invalid device cache")
                    cloud_devices += len(device_map)
                    for map_id, device in device_map.items():
                        try:
                            device_id = _valid_device_id(getattr(device, "id", map_id))
                        except ValueError:
                            continue
                        key = self._valid_local_key(getattr(device, "local_key", ""))
                        if not key:
                            continue
                        item: dict[str, Any] = {"local_key": key}
                        for source, target in (
                            ("name", "name"), ("ip", "ip"),
                            ("category", "category"), ("sub", "sub"),
                        ):
                            value = getattr(device, source, None)
                            if value not in (None, ""):
                                item[target] = value
                        collected[device_id] = item
                    try:
                        manager.unload()
                    except Exception:
                        pass
                    manager = None

                if not collected:
                    raise RuntimeError("Official Tuya session returned no usable local keys")

                merged = dict(before_devices)
                merged.update(collected)
                document = {
                    "version": 1,
                    "updated_at": _now_iso(),
                    "source": "home_assistant_tuya_device_sharing",
                    "devices": merged,
                }
                _atomic_json(self.key_file, document, mode=0o600)
                wrote_file = True

                verified_devices = self._protected_devices()
                verified = all(
                    self._valid_local_key(
                        verified_devices.get(device_id, {}).get("local_key")
                        if isinstance(verified_devices.get(device_id), dict)
                        else verified_devices.get(device_id)
                    ) == item["local_key"]
                    for device_id, item in collected.items()
                )
                if not verified:
                    raise RuntimeError("Protected Tuya key file verification failed")
                after_count = sum(
                    1 for item in verified_devices.values()
                    if self._valid_local_key(item.get("local_key") if isinstance(item, dict) else item)
                )
                audit = self.audit_callback(
                    action="localtuya.keys.sync",
                    reason=reason,
                    before={"protected_keys": before_count},
                    after={
                        "protected_keys": after_count,
                        "cloud_devices_seen": cloud_devices,
                        "keys_collected": len(collected),
                    },
                    verified=True,
                    changed=verified_devices != before_devices,
                )
                return {
                    "synced": True,
                    "cloud_devices_seen": cloud_devices,
                    "keys_collected": len(collected),
                    "protected_keys": after_count,
                    "protected_file": str(self.key_file),
                    "file_mode": "0600",
                    "cloud_tuya_changed": False,
                    "secrets_exposed": False,
                    "change_id": audit["change_id"],
                }
            except Exception as exc:
                if wrote_file:
                    try:
                        if before_file_exists:
                            _atomic_json(self.key_file, before_document, mode=0o600)
                        else:
                            self.key_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                self.audit_callback(
                    action="localtuya.keys.sync",
                    reason=reason,
                    before={"protected_keys": before_count},
                    after={"protected_keys": before_count},
                    verified=False,
                    changed=False,
                    error=type(exc).__name__,
                )
                raise
            finally:
                if manager is not None:
                    try:
                        manager.unload()
                    except Exception:
                        pass

    async def _probe_async(
        self, *, device_id: str, host: str, key: str, protocol: str
    ) -> dict[str, Any]:
        interface = await pytuya.connect(
            host, device_id, key, float(protocol), False
        )
        try:
            return await asyncio.wait_for(interface.detect_available_dps(), timeout=15)
        finally:
            await interface.close()

    def probe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device_id = _valid_device_id(arguments.get("device_id"))
        host = self._validate_host(arguments.get("host"))
        protocol = _valid_protocol(arguments.get("protocol_version"))
        key, source = self._resolve_key(device_id)
        values = asyncio.run(
            self._probe_async(device_id=device_id, host=host, key=key, protocol=protocol)
        )
        return {
            "reachable": True,
            "device_id": device_id,
            "host": host,
            "protocol_version": protocol,
            "key_source": source,
            "detected_dps": {str(dp): value for dp, value in values.items()},
            "secrets_exposed": False,
        }

    def _normalise_entities(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValueError("entities must be a non-empty array")
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Each LocalTuya entity must be an object")
            dp = int(item.get("dp"))
            if dp < 1 or dp > 255 or dp in seen:
                raise ValueError("Every entity DP must be unique and between 1 and 255")
            platform = str(item.get("platform") or "").strip().lower()
            if platform not in SUPPORTED_PLATFORMS:
                raise ValueError(f"Unsupported LocalTuya platform: {platform}")
            seen.add(dp)
            options = item.get("options") or {}
            if not isinstance(options, dict):
                raise ValueError("entity options must be an object")
            result.append({
                "dp": dp,
                "platform": platform,
                "friendly_name": _safe_name(item.get("friendly_name"), f"DP {dp}"),
                "options": options,
            })
        return result

    def stage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device_id = _valid_device_id(arguments.get("device_id"))
        host = self._validate_host(arguments.get("host"))
        protocol = _valid_protocol(arguments.get("protocol_version"))
        friendly_name = _safe_name(arguments.get("friendly_name"), device_id)
        entities = self._normalise_entities(arguments.get("entities"))
        verify_control = bool(arguments.get("verify_control", False))
        safety_class = str(arguments.get("safety_class") or "").strip().lower()
        lowered = friendly_name.lower()
        if verify_control:
            if safety_class not in NONCRITICAL_CLASSES:
                raise PermissionError("Control verification is allowed only for explicitly noncritical light/switch devices")
            if any(word in lowered for word in CRITICAL_WORDS):
                raise PermissionError("Critical device names are blocked from automatic control verification")

        probe = self.probe({
            "device_id": device_id,
            "host": host,
            "protocol_version": protocol,
        })
        detected = {int(dp) for dp in probe["detected_dps"]}
        missing = sorted(item["dp"] for item in entities if item["dp"] not in detected)
        if missing:
            raise ValueError(f"Configured DPs were not detected: {missing}")

        existing = self._find_device(device_id)
        mode = "update" if existing else "create"
        if existing:
            current_entities = existing[1].get("entities") or []
            current_signature = sorted(
                (int(item.get("id")), str(item.get("platform")))
                for item in current_entities if isinstance(item, dict) and item.get("id") is not None
            )
            staged_signature = sorted((item["dp"], item["platform"]) for item in entities)
            if current_signature != staged_signature:
                raise ValueError("Safe update requires the same DP/platform set; create/add entity changes must use a new isolated entry")

        transaction_id = uuid.uuid4().hex
        plan = {
            "transaction_id": transaction_id,
            "staged_at": _now_iso(),
            "mode": mode,
            "device_id": device_id,
            "host": host,
            "friendly_name": friendly_name,
            "protocol_version": protocol,
            "entities": entities,
            "verify_control": verify_control,
            "safety_class": safety_class,
            "target_entry_id": existing[0].get("entry_id") if existing else None,
            "probe_dps": probe["detected_dps"],
        }
        stages = _load_json(self.stage_path, {"plans": {}})
        if not isinstance(stages, dict) or not isinstance(stages.get("plans"), dict):
            stages = {"plans": {}}
        stages["plans"][transaction_id] = plan
        _atomic_json(self.stage_path, stages)
        return {
            "staged": True,
            "transaction_id": transaction_id,
            "mode": mode,
            "device_id": device_id,
            "host": host,
            "entity_count": len(entities),
            "detected_dp_count": len(probe["detected_dps"]),
            "key_source": probe["key_source"],
            "verify_control": verify_control,
            "applied": False,
            "secrets_exposed": False,
        }

    def _stage(self, transaction_id: Any) -> dict[str, Any]:
        identifier = str(transaction_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Invalid transaction_id")
        stage = _load_json(self.stage_path, {"plans": {}}).get("plans", {}).get(identifier)
        if not isinstance(stage, dict):
            raise KeyError("Staged LocalTuya transaction was not found")
        return stage

    @staticmethod
    def _flow_step(result: Any, expected: str | None = None) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise RuntimeError("Invalid Home Assistant flow response")
        if result.get("type") == "abort":
            raise RuntimeError(f"Home Assistant flow aborted: {result.get('reason')}")
        if result.get("errors"):
            raise RuntimeError(f"Home Assistant flow validation failed: {result.get('errors')}")
        if expected and result.get("step_id") != expected:
            raise RuntimeError(f"Expected flow step {expected}, got {result.get('step_id')}")
        return result

    @staticmethod
    def _field_schema(result: dict[str, Any], name: str) -> dict[str, Any] | None:
        schema = result.get("data_schema") or []
        if isinstance(schema, list):
            for field in schema:
                if isinstance(field, dict) and field.get("name") == name:
                    return field
        return None

    @classmethod
    def _dp_choice(cls, result: dict[str, Any], dp: int) -> Any:
        field = cls._field_schema(result, "id")
        if field:
            options = field.get("options") or []
            if isinstance(options, dict):
                options = [
                    {"value": value, "label": label}
                    for value, label in options.items()
                ]
            for option in options:
                if isinstance(option, dict):
                    value = option.get("value", option.get("id", option.get("key")))
                elif isinstance(option, (list, tuple)) and option:
                    value = option[0]
                else:
                    value = option
                if str(value).split(" ", 1)[0] == str(dp):
                    return value
        raise RuntimeError(f"DP {dp} is not offered by the LocalTuya config flow")

    @classmethod
    def _complete_required(cls, result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        schema = result.get("data_schema") or []
        if not isinstance(schema, list):
            return payload
        completed = dict(payload)
        for field in schema:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            if not name or name in completed or not field.get("required"):
                continue
            if "default" in field:
                completed[name] = field["default"]
                continue
            raise ValueError(f"Required LocalTuya entity option is missing: {name}")
        return completed

    @staticmethod
    def _configure(client: HomeAssistantFlowClient, flow_id: str, user_input: dict[str, Any]) -> Any:
        return client.configure_options_flow(flow_id, user_input)

    def _create_entry(self, client: HomeAssistantFlowClient, title: str) -> str:
        result = self._flow_step(client.start_config_flow("localtuya"), "user")
        flow_id = result["flow_id"]
        result = client.configure_config_flow(flow_id, {
            "region": "eu", "username": title, "no_cloud": True,
            "client_id": "", "client_secret": "", "user_id": "",
        })
        if not isinstance(result, dict) or result.get("type") != "create_entry":
            self._flow_step(result)
            raise RuntimeError("LocalTuya config entry was not created")
        entry = result.get("result") or {}
        entry_id = entry.get("entry_id") if isinstance(entry, dict) else None
        if not entry_id:
            raise RuntimeError("Home Assistant did not return the new config entry ID")
        return str(entry_id)

    def _add_device(
        self, client: HomeAssistantFlowClient, entry_id: str, plan: dict[str, Any], key: str
    ) -> None:
        result = self._flow_step(client.start_options_flow(entry_id), "init")
        flow_id = result["flow_id"]
        result = self._flow_step(self._configure(client, flow_id, {"action": "add_device"}), "add_device")
        result = self._flow_step(self._configure(client, flow_id, {"selected_device": "..."}), "configure_device")
        result = self._flow_step(self._configure(client, flow_id, {
            "friendly_name": plan["friendly_name"],
            "host": plan["host"],
            "device_id": plan["device_id"],
            "local_key": key,
            "protocol_version": plan["protocol_version"],
            "enable_debug": False,
        }), "pick_entity_type")
        for entity in plan["entities"]:
            result = self._flow_step(self._configure(client, flow_id, {
                "platform_to_add": entity["platform"]
            }), "configure_entity")
            payload = {
                "id": self._dp_choice(result, entity["dp"]),
                "friendly_name": entity["friendly_name"],
                **entity["options"],
            }
            if entity["platform"] == "switch":
                payload.setdefault("restore_on_reconnect", False)
                payload.setdefault("is_passive_entity", False)
            payload = self._complete_required(result, payload)
            result = self._flow_step(self._configure(client, flow_id, payload), "pick_entity_type")
        final = self._configure(client, flow_id, {
            "platform_to_add": plan["entities"][-1]["platform"],
            "no_additional_entities": True,
        })
        if not isinstance(final, dict) or final.get("type") != "create_entry":
            self._flow_step(final)
            raise RuntimeError("LocalTuya device flow did not complete")

    def _edit_device(
        self,
        client: HomeAssistantFlowClient,
        entry_id: str,
        device_id: str,
        device: dict[str, Any],
    ) -> None:
        result = self._flow_step(client.start_options_flow(entry_id), "init")
        flow_id = result["flow_id"]
        result = self._flow_step(self._configure(client, flow_id, {"action": "edit_device"}), "edit_device")
        result = self._flow_step(self._configure(client, flow_id, {"selected_device": device_id}), "configure_device")
        entities = device.get("entities") or []
        selected = [f"{item['id']}: {item['friendly_name']}" for item in entities]
        result = self._flow_step(self._configure(client, flow_id, {
            "friendly_name": device.get("friendly_name") or device_id,
            "host": device["host"],
            "local_key": device["local_key"],
            "protocol_version": str(device.get("protocol_version") or "3.3"),
            "enable_debug": bool(device.get("enable_debug", False)),
            "entities": selected,
            "add_entities": False,
        }), "configure_entity")
        for index, entity in enumerate(entities):
            payload = {
                key: value for key, value in entity.items()
                if key not in {"id", "platform"}
            }
            payload = self._complete_required(result, payload)
            result = self._configure(client, flow_id, payload)
            if index < len(entities) - 1:
                result = self._flow_step(result, "configure_entity")
            elif not isinstance(result, dict) or result.get("type") != "create_entry":
                self._flow_step(result)
                raise RuntimeError("LocalTuya edit flow did not complete")

    def _device_from_plan(self, plan: dict[str, Any], key: str, before: dict[str, Any] | None) -> dict[str, Any]:
        device = dict(before or {})
        device.update({
            "friendly_name": plan["friendly_name"],
            "host": plan["host"],
            "local_key": key,
            "protocol_version": plan["protocol_version"],
            "enable_debug": bool(device.get("enable_debug", False)),
        })
        configured: list[dict[str, Any]] = []
        for item in plan["entities"]:
            entity = {
                "id": item["dp"], "platform": item["platform"],
                "friendly_name": item["friendly_name"], **item["options"],
            }
            if item["platform"] == "switch":
                entity.setdefault("restore_on_reconnect", False)
                entity.setdefault("is_passive_entity", False)
            configured.append(entity)
        device["entities"] = configured
        return device

    def _delete_entry(self, entry_id: str) -> bool:
        HomeAssistantFlowClient(self.supervisor_token).delete_entry(entry_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not any(entry.get("entry_id") == entry_id for entry in self._config_entries()):
                return True
            time.sleep(1)
        return False

    def _wait_verify(self, entry_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        device: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            found = self._find_device(plan["device_id"])
            if found and found[0].get("entry_id") == entry_id:
                device = found[1]
                if str(device.get("host")) == plan["host"]:
                    break
            time.sleep(1)
        entity_registry = _load_json(
            self.config_dir / ".storage/core.entity_registry",
            {"data": {"entities": []}},
        )
        entity_ids = [
            item.get("entity_id")
            for item in entity_registry.get("data", {}).get("entities", [])
            if item.get("platform") == "localtuya" and item.get("config_entry_id") == entry_id
        ]
        return {
            "configured": bool(device and str(device.get("host")) == plan["host"]),
            "entry_id": entry_id,
            "entity_ids": sorted(item for item in entity_ids if item),
            "entity_count": len(entity_ids),
        }

    async def _control_check_async(
        self, *, plan: dict[str, Any], key: str
    ) -> dict[str, Any]:
        boolean_entities = [
            item for item in plan["entities"]
            if item["platform"] in {"switch", "light"}
            and isinstance(plan["probe_dps"].get(str(item["dp"])), bool)
        ]
        if not boolean_entities:
            return {"performed": False, "reason": "no_boolean_control_dp"}
        target = boolean_entities[0]
        interface = await pytuya.connect(
            plan["host"], plan["device_id"], key,
            float(plan["protocol_version"]), False,
        )
        original = bool(plan["probe_dps"][str(target["dp"])])
        changed_seen = False
        restored = False
        try:
            await interface.set_dp(not original, target["dp"])
            await asyncio.sleep(0.8)
            changed = await interface.detect_available_dps()
            changed_seen = changed.get(str(target["dp"]), changed.get(target["dp"])) == (not original)
        finally:
            try:
                await interface.set_dp(original, target["dp"])
                await asyncio.sleep(0.8)
                final = await interface.detect_available_dps()
                restored = final.get(str(target["dp"]), final.get(target["dp"])) == original
            finally:
                await interface.close()
        return {
            "performed": True, "dp": target["dp"],
            "changed_seen": changed_seen, "restored": restored,
        }

    def apply(self, transaction_id: Any, reason: str) -> dict[str, Any]:
        plan = self._stage(transaction_id)
        with self.lock:
            key, key_source = self._resolve_key(plan["device_id"])
            current = self._find_device(plan["device_id"])
            before_entry = current[0] if current else None
            before_device = current[1] if current else None
            if plan["mode"] == "create" and current:
                raise ValueError("Device became configured after staging; restage as update")
            if plan["mode"] == "update" and not current:
                raise ValueError("Configured device disappeared after staging")

            backup_result = self.backup_callback(
                f"RELAX47 before LocalTuya {plan['device_id']}"
            )
            ledger = {
                "transaction_id": plan["transaction_id"],
                "created_at": _now_iso(), "status": "applying", "plan": plan,
                "before_entry_id": before_entry.get("entry_id") if before_entry else None,
                "before_device": before_device,
                "created_entry_id": None,
            }
            ledger_path = self.transaction_dir / f"{plan['transaction_id']}.json"
            _atomic_json(ledger_path, ledger)
            entry_id: str | None = None
            rollback: dict[str, Any] | None = None
            try:
                client = HomeAssistantFlowClient(self.supervisor_token)
                if plan["mode"] == "create":
                    entry_id = self._create_entry(client, f"RELAX47 {plan['friendly_name']}")
                    ledger["created_entry_id"] = entry_id
                    _atomic_json(ledger_path, ledger)
                    self._add_device(client, entry_id, plan, key)
                else:
                    entry_id = str(before_entry["entry_id"])
                    updated = self._device_from_plan(plan, key, before_device)
                    self._edit_device(client, entry_id, plan["device_id"], updated)

                verification = self._wait_verify(entry_id, plan)
                if not verification["configured"]:
                    raise RuntimeError("LocalTuya configuration failed readback")
                control = {"performed": False, "reason": "not_requested"}
                if plan.get("verify_control"):
                    control = asyncio.run(self._control_check_async(plan=plan, key=key))
                    if control.get("performed") and not control.get("restored"):
                        raise RuntimeError("Control verification did not restore the original DP state")
                ledger.update({
                    "status": "applied", "completed_at": _now_iso(),
                    "entry_id": entry_id, "verification": verification,
                    "control_verification": control,
                })
                _atomic_json(ledger_path, ledger)
                audit = self.audit_callback(
                    action="localtuya.apply", reason=reason,
                    before={"mode": plan["mode"], "configured": bool(before_device)},
                    after={"entry_id": entry_id, **verification, "control": control},
                    verified=True, changed=True,
                )
                return {
                    "applied": True, "verified": True,
                    "transaction_id": plan["transaction_id"],
                    "change_id": audit["change_id"], "mode": plan["mode"],
                    "device_id": plan["device_id"], "entry_id": entry_id,
                    "key_source": key_source, "backup_requested": True,
                    "backup_response": backup_result, "verification": verification,
                    "control_verification": control, "secrets_exposed": False,
                }
            except Exception as exc:
                try:
                    if plan["mode"] == "create" and entry_id:
                        rollback = {"entry_deleted": self._delete_entry(entry_id)}
                    elif plan["mode"] == "update" and before_entry and before_device:
                        self._edit_device(
                            HomeAssistantFlowClient(self.supervisor_token),
                            str(before_entry["entry_id"]),
                            plan["device_id"], before_device,
                        )
                        rollback = {"previous_device_restored": True}
                except Exception as rollback_exc:
                    rollback = {"failed": type(rollback_exc).__name__}
                ledger.update({
                    "status": "failed", "completed_at": _now_iso(),
                    "error": type(exc).__name__, "rollback": rollback,
                })
                _atomic_json(ledger_path, ledger)
                self.audit_callback(
                    action="localtuya.apply", reason=reason,
                    before={"mode": plan["mode"], "configured": bool(before_device)},
                    after={}, verified=False, changed=bool(entry_id),
                    rollback=rollback, error=type(exc).__name__,
                )
                raise

    def rollback(self, transaction_id: Any, reason: str) -> dict[str, Any]:
        identifier = str(transaction_id or "").strip().lower()
        ledger_path = self.transaction_dir / f"{identifier}.json"
        ledger = _load_json(ledger_path, None)
        if not isinstance(ledger, dict):
            raise KeyError("LocalTuya transaction was not found")
        if ledger.get("status") == "rolled_back":
            return {"changed": False, "verified": True, "transaction_id": identifier}
        plan = ledger["plan"]
        with self.lock:
            if plan["mode"] == "create":
                entry_id = ledger.get("created_entry_id") or ledger.get("entry_id")
                verified = bool(entry_id) and self._delete_entry(str(entry_id))
            else:
                before = ledger.get("before_device")
                entry_id = ledger.get("before_entry_id")
                if not before or not entry_id:
                    raise RuntimeError("Rollback snapshot is incomplete")
                self._edit_device(
                    HomeAssistantFlowClient(self.supervisor_token),
                    str(entry_id), plan["device_id"], before,
                )
                found = self._find_device(plan["device_id"])
                verified = bool(found and found[1].get("host") == before.get("host"))
            ledger.update({"status": "rolled_back", "rolled_back_at": _now_iso()})
            _atomic_json(ledger_path, ledger)
            audit = self.audit_callback(
                action="localtuya.rollback", reason=reason,
                before={"transaction_id": identifier, "status": "applied"},
                after={"status": "rolled_back"}, verified=verified, changed=True,
            )
            return {
                "changed": True, "verified": verified,
                "transaction_id": identifier, "change_id": audit["change_id"],
                "device_id": plan["device_id"], "secrets_exposed": False,
            }

    def apply_batch(self, transaction_ids: Any, reason: str) -> dict[str, Any]:
        if not isinstance(transaction_ids, list) or not transaction_ids:
            raise ValueError("transaction_ids must be a non-empty array")
        results: list[dict[str, Any]] = []
        for identifier in transaction_ids[:100]:
            try:
                results.append({"transaction_id": identifier, "result": self.apply(identifier, reason)})
            except Exception as exc:
                results.append({
                    "transaction_id": identifier, "error": type(exc).__name__,
                    "continued": True,
                })
        return {
            "requested": len(transaction_ids[:100]),
            "succeeded": sum(1 for item in results if "result" in item),
            "failed": sum(1 for item in results if "error" in item),
            "results": results, "continued_after_error": True,
            "secrets_exposed": False,
        }
