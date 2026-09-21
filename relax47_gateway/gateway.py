#!/usr/bin/env python3
"""RELAX47 verified administration gateway for Home Assistant and Xiaomi AX6000."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import datetime as dt
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from localtuya_migration import LocalTuyaManager
from maintenance import MaintenanceManager
from sql_migration import Relax47SQLManager
from runtime_migration import prepare_runtime
from backup_retention import BackupRetention


VERSION = "7.14.11"
ROUTER_HOST = os.environ.get("RELAX47_ROUTER_HOST", "192.168.31.1")
ROUTER_MODEL = os.environ.get("RELAX47_ROUTER_MODEL", "RA72")
ROUTER_FIRMWARE = os.environ.get("RELAX47_ROUTER_FIRMWARE", "1.0.122")
ROUTER_PASSWORD = os.environ.get("RELAX47_ROUTER_PASSWORD", "")
LAN_CIDR = os.environ.get("RELAX47_LAN_CIDR", "192.168.31.0/24")
WRITE_MODE = os.environ.get("RELAX47_WRITE_MODE", "false").lower() == "true"
AUTO_VERIFY_WRITES = os.environ.get("RELAX47_AUTO_VERIFY_WRITES", "true").lower() == "true"
ADMIN_ALL_SERVICE_DOMAINS = os.environ.get("RELAX47_ADMIN_ALL_SERVICE_DOMAINS", "true").lower() == "true"
CONFIRMATION_CODE = os.environ.get("RELAX47_CONFIRMATION_CODE", "")
PUBLIC_MCP_ENABLED = os.environ.get("RELAX47_PUBLIC_MCP_ENABLED", "false").lower() == "true"
PUBLIC_MCP_PORT = int(os.environ.get("RELAX47_PUBLIC_MCP_PORT", "8766"))
PUBLIC_BASE_URL = os.environ.get("RELAX47_PUBLIC_BASE_URL", "").rstrip("/")
ALLOWED_DOMAINS = {
    item.strip()
    for item in os.environ.get(
        "RELAX47_ALLOWED_SERVICE_DOMAINS",
        "homeassistant,light,switch,scene,script,automation,input_boolean,input_select",
    ).split(",")
    if item.strip()
}
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API = "http://supervisor/core/api"
CONFIG_DIR = Path(os.environ.get("RELAX47_CONFIG_DIR", "/homeassistant"))
SHARE_DIR = Path(os.environ.get("RELAX47_SHARE_DIR", "/share"))
AUDIT_PATH = Path(os.environ.get("RELAX47_AUDIT_PATH", "/data/RELAX47_AUDIT.jsonl"))
DATA_DIR = Path(os.environ.get("RELAX47_DATA_DIR", "/data"))
TUNNEL_STATE_PATH = Path("/run/relax47/tunnel_state.json")
TUNNEL_LOG_PATH = Path("/data/tunnel-client.log")
ADMIN_ALLOWED_SOURCES = {
    item.strip()
    for item in os.environ.get(
        "RELAX47_ADMIN_ALLOWED_SOURCES", "127.0.0.1,::1,172.30.32.2"
    ).split(",")
    if item.strip()
}
STARTED_AT = dt.datetime.now(dt.timezone.utc).isoformat()
CHANGE_LOCK = threading.Lock()
AUDIT_LOCK = threading.Lock()
AUDIT_CONTEXT = threading.local()
DEFAULT_PORTS = [
    22, 53, 80, 81, 443, 554, 1883, 5353, 6668, 6669, 7000,
    8000, 8080, 8099, 8123, 8883, 9100, 9898, 10000, 22222,
]
TERMINAL_ALLOWED = {"ls", "find", "rg", "grep", "head", "tail", "stat", "du", "df", "ps", "ip", "ss", "ping", "getent"}
TERMINAL_BLOCKED_PARTS = {
    ".storage", "secrets.yaml", "credentials", "auth", "onboarding",
    "refresh_tokens", "private_key", "privkey", "/ssl", "/proc/", "/sys/",
    "relax47_oauth", "supervisor_token", "shadow",
}
TERMINAL_ROOTS = (CONFIG_DIR, SHARE_DIR, DATA_DIR, Path("/opt/relax47"), Path("/run/relax47"))

try:
    from xiaomi_miwifi import MiWiFiClient
except ImportError:
    MiWiFiClient = None


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def json_load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def mask(value: Any, keep: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}…{text[-keep:]}"


SECRET_PARTS = (
    "password", "passwd", "secret", "token", "local_key", "access_key",
    "api_key", "private_key", "credential",
)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SECRET_PARTS):
                result[str(key)] = "***"
            elif lowered in {"id", "routerid", "serial", "serialnumber"}:
                result[str(key)] = mask(item)
            else:
                result[str(key)] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def json_compatible(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_compatible(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_compatible(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        return json_compatible(value.value)
    if hasattr(value, "__dict__"):
        return json_compatible(vars(value))
    return str(value)


def request_json(
    url: str,
    *,
    method: str = "GET",
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(4 * 1024 * 1024)
        return json.loads(body.decode("utf-8")) if body else {}


def ha_request(
    path: str, *, method: str = "GET", data: Any = None,
    timeout: float = 20.0,
) -> Any:
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    return request_json(
        f"{HA_API}{path}", method=method, data=data,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}, timeout=timeout,
    )


async def router_library_call(method: str, *args: Any) -> Any:
    if MiWiFiClient is None:
        raise RuntimeError("Xiaomi MiWiFi client library is unavailable")
    if not ROUTER_PASSWORD:
        raise PermissionError("Router password is not configured locally")
    async with MiWiFiClient(ROUTER_HOST, password=ROUTER_PASSWORD) as client:
        function = getattr(client, method, None)
        if function is None or not callable(function):
            raise NotImplementedError(f"Router library method is unavailable: {method}")
        return await function(*args)


def router_call(method: str, *args: Any) -> Any:
    return sanitize(json_compatible(asyncio.run(router_library_call(method, *args))))


def append_audit(
    *, action: str, reason: str, before: Any, after: Any, verified: bool,
    changed: bool, rollback: Any = None, error: str | None = None,
) -> dict[str, Any]:
    entry = {
        "change_id": uuid.uuid4().hex, "timestamp": now_iso(), "action": action,
        "reason": reason, "changed": changed, "verified": verified,
        "before": sanitize(before), "after": sanitize(after),
        "rollback": sanitize(rollback), "error": error,
    }
    context = getattr(AUDIT_CONTEXT, "value", None)
    if isinstance(context, dict):
        entry.update({key: sanitize(value) for key, value in context.items()})
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with AUDIT_LOCK:
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return entry


def set_audit_context(value: dict[str, Any] | None) -> None:
    """Attach public client/session identity to write audit entries in this thread."""
    AUDIT_CONTEXT.value = dict(value or {})


def require_write(arguments: dict[str, Any], *, domain: str | None = None) -> str:
    if not WRITE_MODE:
        raise PermissionError("Write mode is disabled in the add-on configuration")
    if CONFIRMATION_CODE and arguments.get("confirmation_code") != CONFIRMATION_CODE:
        raise PermissionError("The locally configured owner confirmation code is missing or incorrect")
    if domain is not None and not ADMIN_ALL_SERVICE_DOMAINS and domain not in ALLOWED_DOMAINS:
        raise PermissionError(f"Home Assistant domain '{domain}' is not allowlisted")
    reason = str(arguments.get("change_reason", "")).strip()
    if len(reason) < 3:
        raise ValueError("change_reason is required for every write")
    return reason[:500]


def tunnel_status() -> dict[str, Any]:
    status = json_load(TUNNEL_STATE_PATH, {"state": "unknown", "detail": "state file is not available"})
    health = False
    ready = False
    for path, key in (("healthz", "health"), ("readyz", "ready")):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:8080/{path}", timeout=1.5) as response:
                value = response.status == 200
        except Exception:
            value = False
        if key == "health":
            health = value
        else:
            ready = value
    diagnosis = None
    try:
        tail = TUNNEL_LOG_PATH.read_text(encoding="utf-8", errors="replace")[-20000:].lower()
        if "unsupported_country_region_territory" in tail:
            diagnosis = "OpenAI control plane rejected this region (unsupported_country_region_territory)"
        elif "forbidden" in tail or "status 403" in tail:
            diagnosis = "OpenAI control plane returned 403; check region, tunnel association and Tunnels Read+Use"
        elif "unauthorized" in tail or "status 401" in tail:
            diagnosis = "Runtime API key was rejected"
        elif "connection refused" in tail and "8765" in tail:
            diagnosis = "Local MCP endpoint was not reachable by tunnel-client"
        elif "no such host" in tail or "dns" in tail:
            diagnosis = "DNS resolution failed for the outbound control-plane connection"
    except OSError:
        pass
    return {
        **status, "process_healthy": health, "ready": ready, "diagnosis": diagnosis,
        "network": "outbound HTTPS to api.openai.com:443; no inbound router port",
    }


def tool_gateway_status(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION, "started_at": STARTED_AT,
        "router": {"host": ROUTER_HOST, "model": ROUTER_MODEL, "firmware_expected": ROUTER_FIRMWARE},
        "lan_cidr": LAN_CIDR, "write_mode": WRITE_MODE,
        "auto_verify_writes": AUTO_VERIFY_WRITES,
        "admin_all_service_domains": ADMIN_ALL_SERVICE_DOMAINS,
        "router_password_configured": bool(ROUTER_PASSWORD),
        "home_assistant_api": "available" if SUPERVISOR_TOKEN else "unavailable",
        "tunnel": tunnel_status(), "audit_path": str(AUDIT_PATH),
        "public_mcp": {
            "enabled": PUBLIC_MCP_ENABLED,
            "internal_tls_port": PUBLIC_MCP_PORT,
            "endpoint": f"{PUBLIC_BASE_URL}/mcp" if PUBLIC_BASE_URL else "",
            "authentication": "OAuth Authorization Code + PKCE S256",
        },
        "inbound_ports_required": PUBLIC_MCP_ENABLED,
        "maintenance": maintenance_manager().status(),
    }


def tool_tunnel_status(_: dict[str, Any]) -> dict[str, Any]:
    return tunnel_status()


def tool_ha_health(_: dict[str, Any]) -> dict[str, Any]:
    config = sanitize(ha_request("/config"))
    return {
        "reachable": True, "version": config.get("version"),
        "location_name": config.get("location_name"), "state": config.get("state"),
        "time_zone": config.get("time_zone"),
    }


def validate_entity_id(value: str) -> str:
    entity_id = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id):
        raise ValueError("Invalid entity_id")
    return entity_id


def tool_ha_get_state(arguments: dict[str, Any]) -> dict[str, Any]:
    entity_id = validate_entity_id(str(arguments.get("entity_id", "")))
    return sanitize(ha_request(f"/states/{urllib.parse.quote(entity_id, safe='._')}"))


def tool_ha_list_entities(arguments: dict[str, Any]) -> dict[str, Any]:
    prefix = str(arguments.get("prefix", "")).strip().lower()
    limit = max(1, min(int(arguments.get("limit", 500)), 2000))
    result = []
    for state in ha_request("/states"):
        entity_id = str(state.get("entity_id", ""))
        if prefix and not entity_id.startswith(prefix):
            continue
        result.append({
            "entity_id": entity_id, "state": state.get("state"),
            "friendly_name": state.get("attributes", {}).get("friendly_name"),
            "device_class": state.get("attributes", {}).get("device_class"),
        })
        if len(result) >= limit:
            break
    return {"count": len(result), "entities": result}


def normalized_registry() -> dict[str, Any]:
    area_registry = json_load(CONFIG_DIR / ".storage/core.area_registry", {"data": {"areas": []}})
    device_registry = json_load(CONFIG_DIR / ".storage/core.device_registry", {"data": {"devices": []}})
    entity_registry = json_load(CONFIG_DIR / ".storage/core.entity_registry", {"data": {"entities": []}})
    config_entries = json_load(CONFIG_DIR / ".storage/core.config_entries", {"data": {"entries": []}})
    areas = area_registry.get("data", {}).get("areas", [])
    devices = device_registry.get("data", {}).get("devices", [])
    entities = entity_registry.get("data", {}).get("entities", [])
    entries = config_entries.get("data", {}).get("entries", [])
    area_rows = [{"id": item.get("id"), "name": item.get("name")} for item in areas]
    device_rows = [{
        "id": item.get("id"), "name": item.get("name_by_user") or item.get("name"),
        "manufacturer": item.get("manufacturer"), "model": item.get("model"),
        "area_id": item.get("area_id"), "configuration_url": item.get("configuration_url"),
        "connections": item.get("connections", []), "via_device_id": item.get("via_device_id"),
        "disabled_by": item.get("disabled_by"),
    } for item in devices]
    entity_rows = [{
        "entity_id": item.get("entity_id"), "platform": item.get("platform"),
        "device_id": item.get("device_id"), "area_id": item.get("area_id"),
        "name": item.get("name"), "original_name": item.get("original_name"),
        "disabled_by": item.get("disabled_by"),
    } for item in entities]
    integrations = Counter(str(item.get("domain", "unknown")) for item in entries)
    return sanitize({
        "counts": {"areas": len(area_rows), "devices": len(device_rows), "entities": len(entity_rows), "config_entries": len(entries)},
        "integrations": dict(sorted(integrations.items())), "areas": area_rows,
        "devices": device_rows, "entities": entity_rows,
    })


def tool_ha_registry_inventory(arguments: dict[str, Any]) -> dict[str, Any]:
    inventory = normalized_registry()
    if not bool(arguments.get("include_entities", True)):
        inventory.pop("entities", None)
    return inventory


def extract_target_entities(service_data: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    if "entity_id" in service_data:
        values.append(service_data.get("entity_id"))
    target = service_data.get("target")
    if isinstance(target, dict) and "entity_id" in target:
        values.append(target.get("entity_id"))
    result: list[str] = []
    for value in values:
        candidates = value if isinstance(value, list) else str(value or "").split(",")
        for candidate in candidates:
            try:
                result.append(validate_entity_id(str(candidate)))
            except ValueError:
                continue
    return sorted(set(result))[:100]


def read_entity_states(entity_ids: list[str]) -> dict[str, Any]:
    states: dict[str, Any] = {}
    for entity_id in entity_ids:
        try:
            state = tool_ha_get_state({"entity_id": entity_id})
            states[entity_id] = {"state": state.get("state"), "attributes": state.get("attributes", {})}
        except Exception as exc:
            states[entity_id] = {"error": type(exc).__name__}
    return states


def tool_ha_call_service(arguments: dict[str, Any]) -> dict[str, Any]:
    domain = str(arguments.get("domain", "")).strip()
    service = str(arguments.get("service", "")).strip()
    if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
        raise ValueError("Invalid domain or service")
    reason = require_write(arguments, domain=domain)
    service_data = arguments.get("service_data") or {}
    if not isinstance(service_data, dict):
        raise ValueError("service_data must be an object")
    targets = extract_target_entities(service_data)
    with CHANGE_LOCK:
        before = read_entity_states(targets) if targets else {}
        try:
            response = ha_request(f"/services/{domain}/{service}", method="POST", data=service_data)
            after = read_entity_states(targets) if AUTO_VERIFY_WRITES and targets else {}
            verified = not AUTO_VERIFY_WRITES or not targets or len(after) == len(targets)
            audit = append_audit(
                action=f"home_assistant.service.{domain}.{service}", reason=reason,
                before=before, after=after, verified=verified, changed=True,
            )
            return {
                "accepted": True, "verified_by_readback": verified,
                "change_id": audit["change_id"], "targets": targets,
                "before": before, "after": after, "response": sanitize(response),
            }
        except Exception as exc:
            append_audit(
                action=f"home_assistant.service.{domain}.{service}", reason=reason,
                before=before, after={}, verified=False, changed=False,
                error=type(exc).__name__,
            )
            raise


def supervisor_backup_ids() -> set[str]:
    if not SUPERVISOR_TOKEN:
        return set()
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    for path in ("/backups", "/snapshots"):
        try:
            response = request_json(
                f"http://supervisor{path}", headers=headers, timeout=10.0
            )
        except Exception:
            continue
        data = response.get("data", response) if isinstance(response, dict) else {}
        rows = data.get("backups", data.get("snapshots", [])) if isinstance(data, dict) else []
        if isinstance(rows, list):
            return {
                str(item.get("slug") or item.get("id"))
                for item in rows if isinstance(item, dict) and (item.get("slug") or item.get("id"))
            }
    return set()


def create_automatic_backup(reason: str) -> dict[str, Any]:
    """Create a Home Assistant backup through a dedicated audited path."""
    with CHANGE_LOCK:
        try:
            before_ids = supervisor_backup_ids()
            response = ha_request(
                "/services/backup/create_automatic", method="POST", data={},
                timeout=180.0,
            )
            new_ids: set[str] = set()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                current_ids = supervisor_backup_ids()
                new_ids = current_ids - before_ids
                if new_ids:
                    break
                time.sleep(2)
            verified = bool(new_ids)
            if not verified:
                raise RuntimeError("Home Assistant accepted the backup request but no new backup was visible during verification")
            audit = append_audit(
                action="home_assistant.backup.create_automatic", reason=reason,
                before={"backup_count": len(before_ids)},
                after={"requested": True, "new_backup_count": len(new_ids)},
                verified=True, changed=True,
            )
            return {
                "requested": True, "accepted": True, "verified": True,
                "change_id": audit["change_id"], "response": sanitize(response),
                "new_backup_ids": sorted(new_ids),
            }
        except Exception as exc:
            append_audit(
                action="home_assistant.backup.create_automatic", reason=reason,
                before={}, after={}, verified=False, changed=False,
                error=type(exc).__name__,
            )
            raise


def tool_ha_create_backup(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return create_automatic_backup(reason)


def tool_router_probe(_: dict[str, Any]) -> dict[str, Any]:
    endpoints = [
        "/cgi-bin/luci/api/xqsystem/init_info", "/cgi-bin/luci/;stok=/api/xqsystem/init_info",
        "/cgi-bin/luci/api/xqsystem/sys_info",
    ]
    attempts = []
    for path in endpoints:
        try:
            data = request_json(f"http://{ROUTER_HOST}{path}", timeout=4.0)
            attempts.append({"path": path, "ok": True, "data": sanitize(data)})
            if isinstance(data, dict) and data.get("code") in (0, "0"):
                return {"reachable": True, "selected_path": path, "router": sanitize(data), "attempts": attempts}
        except Exception as exc:
            attempts.append({"path": path, "ok": False, "error": type(exc).__name__})
    try:
        with socket.create_connection((ROUTER_HOST, 80), timeout=3.0):
            reachable = True
    except OSError:
        reachable = False
    return {"reachable": reachable, "public_api_verified": False, "attempts": attempts}


def router_method_names() -> list[str]:
    if MiWiFiClient is None:
        return []
    return sorted(name for name in dir(MiWiFiClient) if name.startswith("async_") and callable(getattr(MiWiFiClient, name, None)))


def tool_router_auth_status(_: dict[str, Any]) -> dict[str, Any]:
    if not ROUTER_PASSWORD:
        return {"configured": False, "authenticated": False, "reason": "router_password is empty"}
    try:
        status = router_call("async_get_status")
        return {
            "configured": True, "authenticated": True, "router": status,
            "write_mode": WRITE_MODE,
            "verified_write_tools": ["router_set_dhcp_reservation", "router_remove_dhcp_reservation"],
            "library_methods": router_method_names(),
        }
    except Exception as exc:
        return {"configured": True, "authenticated": False, "error_type": type(exc).__name__, "library_methods": router_method_names()}


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("reservations", "list", "items", "clients", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return [] if value is None else [value]


def tool_router_clients(_: dict[str, Any]) -> dict[str, Any]:
    clients = as_list(router_call("async_get_clients"))
    return {"count": len(clients), "clients": clients}


def current_reservations() -> list[dict[str, Any]]:
    rows = as_list(router_call("async_get_dhcp_reservations"))
    return [item for item in rows if isinstance(item, dict)]


def tool_router_dhcp_reservations(_: dict[str, Any]) -> dict[str, Any]:
    reservations = current_reservations()
    return {"count": len(reservations), "reservations": reservations}


def validate_mac(value: str) -> str:
    normalized = value.strip().lower().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", normalized):
        raise ValueError(f"Invalid MAC address: {value}")
    return normalized


def reservation_mac(item: dict[str, Any]) -> str:
    value = item.get("mac") or item.get("mac_address") or item.get("macaddr") or ""
    try:
        return validate_mac(str(value))
    except ValueError:
        return str(value).strip().lower()


def reservation_ip(item: dict[str, Any]) -> str:
    return str(item.get("ip") or item.get("ip_address") or item.get("ipaddr") or "").strip()


def reservation_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("hostname") or item.get("host_name") or "").strip()


def validate_reservation(arguments: dict[str, Any]) -> tuple[str, str, str]:
    mac = validate_mac(str(arguments.get("mac", "")))
    address = ipaddress.ip_address(str(arguments.get("ip", "")))
    network = ipaddress.ip_network(LAN_CIDR, strict=False)
    if address.version != 4 or address not in network:
        raise ValueError(f"IP {address} is outside configured LAN {network}")
    if address == network.network_address or address == network.broadcast_address:
        raise ValueError("Network and broadcast addresses cannot be reserved")
    if str(address) == ROUTER_HOST:
        raise ValueError("Router address cannot be assigned to a client")
    name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_. -]+", "-", str(arguments.get("name", "")).strip())[:64]
    if not name:
        name = f"device-{mac.replace(':', '')[-6:]}"
    return mac, str(address), name


def find_reservation(rows: list[dict[str, Any]], mac: str) -> dict[str, Any] | None:
    return next((row for row in rows if reservation_mac(row) == mac), None)


def reservation_matches(row: dict[str, Any] | None, mac: str, ip: str) -> bool:
    return bool(row and reservation_mac(row) == mac and reservation_ip(row) == ip)


def tool_router_set_dhcp_reservation(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    mac, ip, name = validate_reservation(arguments)
    with CHANGE_LOCK:
        before_all = current_reservations()
        previous = find_reservation(before_all, mac)
        collision = next((row for row in before_all if reservation_ip(row) == ip and reservation_mac(row) != mac), None)
        if collision:
            raise ValueError(f"IP {ip} is already reserved for another MAC")
        if reservation_matches(previous, mac, ip) and reservation_name(previous) == name:
            audit = append_audit(action="router.dhcp.set", reason=reason, before=previous, after=previous, verified=True, changed=False)
            return {"changed": False, "verified": True, "change_id": audit["change_id"], "reservation": previous}
        rollback: Any = None
        try:
            router_call("async_add_dhcp_reservation", mac, ip, name)
            current = find_reservation(current_reservations(), mac)
            verified = reservation_matches(current, mac, ip)
            if AUTO_VERIFY_WRITES and not verified:
                if previous:
                    router_call("async_add_dhcp_reservation", reservation_mac(previous), reservation_ip(previous), reservation_name(previous) or name)
                    rollback = {"restored": previous}
                else:
                    router_call("async_remove_dhcp_reservation", mac)
                    rollback = {"removed_unverified_change": mac}
                raise RuntimeError("Router did not return the requested DHCP reservation during readback")
            audit = append_audit(action="router.dhcp.set", reason=reason, before=previous, after=current, verified=verified, changed=True)
            return {"changed": True, "verified": verified, "change_id": audit["change_id"], "before": previous, "after": current}
        except Exception as exc:
            try:
                after_failure = find_reservation(current_reservations(), mac)
            except Exception:
                after_failure = None
            append_audit(action="router.dhcp.set", reason=reason, before=previous, after=after_failure, verified=False, changed=True, rollback=rollback, error=type(exc).__name__)
            raise


def tool_router_remove_dhcp_reservation(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    mac = validate_mac(str(arguments.get("mac", "")))
    with CHANGE_LOCK:
        previous = find_reservation(current_reservations(), mac)
        if previous is None:
            audit = append_audit(action="router.dhcp.remove", reason=reason, before=None, after=None, verified=True, changed=False)
            return {"changed": False, "verified": True, "change_id": audit["change_id"]}
        rollback: Any = None
        try:
            router_call("async_remove_dhcp_reservation", mac)
            current = find_reservation(current_reservations(), mac)
            verified = current is None
            if AUTO_VERIFY_WRITES and not verified:
                router_call("async_add_dhcp_reservation", reservation_mac(previous), reservation_ip(previous), reservation_name(previous) or f"device-{mac.replace(':', '')[-6:]}")
                rollback = {"restored": previous}
                raise RuntimeError("Router still returns the DHCP reservation after removal")
            audit = append_audit(action="router.dhcp.remove", reason=reason, before=previous, after=current, verified=verified, changed=True)
            return {"changed": True, "verified": verified, "change_id": audit["change_id"], "removed": previous}
        except Exception as exc:
            try:
                after_failure = find_reservation(current_reservations(), mac)
            except Exception:
                after_failure = None
            append_audit(action="router.dhcp.remove", reason=reason, before=previous, after=after_failure, verified=False, changed=True, rollback=rollback, error=type(exc).__name__)
            raise


def tool_router_network_exposure(_: dict[str, Any]) -> dict[str, Any]:
    methods = set(router_method_names())
    candidates = ["async_get_port_forwards", "async_get_port_forwarding", "async_get_dmz", "async_get_upnp", "async_get_nat_rules"]
    data: dict[str, Any] = {}
    for method in candidates:
        if method in methods:
            try:
                data[method] = router_call(method)
            except Exception as exc:
                data[method] = {"error": type(exc).__name__}
    return {
        "authenticated_read_methods": data,
        "library_support": sorted(method for method in candidates if method in methods),
        "unsupported": sorted(method for method in candidates if method not in methods),
        "writes_available": False,
        "note": "WAN forwarding/DMZ writes stay closed until the exact RA72 API surface is authenticated and readable.",
    }


def tool_list_neighbors(_: dict[str, Any]) -> dict[str, Any]:
    try:
        output = subprocess.check_output(["ip", "-json", "neigh", "show"], text=True, timeout=5)
        entries = json.loads(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        entries = []
    neighbors = []
    for entry in entries:
        destination = entry.get("dst")
        try:
            address = ipaddress.ip_address(destination)
        except ValueError:
            continue
        if address.version != 4:
            continue
        neighbors.append({"ip": destination, "mac": str(entry.get("lladdr", "")).lower(), "interface": entry.get("dev"), "state": entry.get("state", [])})
    return {"count": len(neighbors), "neighbors": sorted(neighbors, key=lambda item: ipaddress.ip_address(item["ip"]))}


def tcp_probe(ip: str, ports: list[int], timeout: float = 0.15) -> dict[str, Any] | None:
    open_ports: list[int] = []
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                open_ports.append(port)
        except OSError:
            pass
    return {"ip": ip, "open_ports": open_ports} if open_ports else None


def validate_scan(arguments: dict[str, Any]) -> tuple[ipaddress.IPv4Network, list[int]]:
    allowed = ipaddress.ip_network(LAN_CIDR, strict=False)
    network = ipaddress.ip_network(str(arguments.get("cidr") or LAN_CIDR), strict=False)
    if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen < 24:
        raise ValueError("Only IPv4 /24 or smaller scans are allowed")
    if not network.subnet_of(allowed):
        raise ValueError(f"Scan network must be inside configured LAN {allowed}")
    raw_ports = arguments.get("ports") or DEFAULT_PORTS
    if not isinstance(raw_ports, list):
        raise ValueError("ports must be an array")
    ports = sorted({int(port) for port in raw_ports if 1 <= int(port) <= 65535})[:48]
    if not ports:
        raise ValueError("At least one valid TCP port is required")
    return network, ports


def tool_scan_lan(arguments: dict[str, Any]) -> dict[str, Any]:
    network, ports = validate_scan(arguments)
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        futures = [executor.submit(tcp_probe, str(host), ports) for host in network.hosts()]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                found.append(result)
    neighbors = {item["ip"]: item for item in tool_list_neighbors({})["neighbors"]}
    for item in found:
        neighbor = neighbors.get(item["ip"])
        if neighbor:
            item.update({"mac": neighbor.get("mac"), "neighbor_state": neighbor.get("state")})
    return {"cidr": str(network), "ports": ports, "count": len(found), "hosts": sorted(found, key=lambda item: ipaddress.ip_address(item["ip"]))}


def item_field(item: dict[str, Any], *names: str) -> str:
    for name in names:
        if item.get(name) not in (None, ""):
            return str(item[name])
    return ""


def tool_port_inventory(arguments: dict[str, Any]) -> dict[str, Any]:
    clients = tool_router_clients({})["clients"]
    reservations = current_reservations()
    neighbors = tool_list_neighbors({})["neighbors"]
    scan = tool_scan_lan(arguments)
    rows: dict[str, dict[str, Any]] = {}
    for client in clients:
        if not isinstance(client, dict):
            continue
        ip = item_field(client, "ip", "ip_address", "ipaddr")
        if ip:
            rows.setdefault(ip, {"ip": ip})["router_client"] = client
    for reservation in reservations:
        ip = reservation_ip(reservation)
        if ip:
            rows.setdefault(ip, {"ip": ip})["dhcp_reservation"] = reservation
    for neighbor in neighbors:
        rows.setdefault(neighbor["ip"], {"ip": neighbor["ip"]})["neighbor"] = neighbor
    for host in scan["hosts"]:
        rows.setdefault(host["ip"], {"ip": host["ip"]})["open_tcp_ports"] = host["open_ports"]
    ordered = sorted(rows.values(), key=lambda item: ipaddress.ip_address(item["ip"]))
    return {
        "generated_at": now_iso(), "lan": LAN_CIDR,
        "scanned_tcp_ports": scan["ports"], "device_count": len(ordered),
        "devices": ordered, "router_exposure": tool_router_network_exposure({}),
    }


def tool_tuya_registry_summary(_: dict[str, Any]) -> dict[str, Any]:
    entity_registry = json_load(CONFIG_DIR / ".storage/core.entity_registry", {"data": {"entities": []}})
    config_entries = json_load(CONFIG_DIR / ".storage/core.config_entries", {"data": {"entries": []}})
    entities = entity_registry.get("data", {}).get("entities", [])
    tuya_entities = [entry for entry in entities if entry.get("platform") == "tuya"]
    local_entities = [entry for entry in entities if entry.get("platform") == "localtuya"]
    local_entries = [entry for entry in config_entries.get("data", {}).get("entries", []) if entry.get("domain") == "localtuya"]
    local_devices = []
    for local_entry in local_entries:
        for device_id, device in (local_entry.get("data", {}).get("devices") or {}).items():
            local_devices.append({
                "device_id": device_id, "friendly_name": device.get("friendly_name"),
                "host": device.get("host"), "protocol_version": device.get("protocol_version"),
                "entity_count": len(device.get("entities") or []),
                "local_key_present": bool(device.get("local_key")),
            })
    domains = Counter(str(item.get("entity_id", "")).split(".", 1)[0] for item in tuya_entities)
    device_ids = {item.get("device_id") for item in tuya_entities if item.get("device_id")}
    return sanitize({
        "cloud_tuya": {"entities": len(tuya_entities), "active_entities": sum(1 for item in tuya_entities if item.get("disabled_by") is None), "device_registry_ids": len(device_ids), "domains": dict(sorted(domains.items()))},
        "localtuya": {"entities": len(local_entities), "configured_devices": len(local_devices), "devices": local_devices, "cloud_disabled_entries": sum(1 for entry in local_entries if entry.get("data", {}).get("no_cloud"))},
        "secrets_exposed": False,
    })


def tool_tuya_migration_candidates(_: dict[str, Any]) -> dict[str, Any]:
    device_registry = json_load(CONFIG_DIR / ".storage/core.device_registry", {"data": {"devices": []}})
    entity_registry = json_load(CONFIG_DIR / ".storage/core.entity_registry", {"data": {"entities": []}})
    devices = device_registry.get("data", {}).get("devices", [])
    entities = entity_registry.get("data", {}).get("entities", [])
    tuya_device_ids = {item.get("device_id") for item in entities if item.get("platform") == "tuya" and item.get("device_id")}
    local_device_ids = {item.get("device_id") for item in entities if item.get("platform") == "localtuya" and item.get("device_id")}
    candidates = []
    for device in devices:
        if device.get("id") not in tuya_device_ids:
            continue
        candidates.append({
            "device_id": device.get("id"), "name": device.get("name_by_user") or device.get("name"),
            "manufacturer": device.get("manufacturer"), "model": device.get("model"),
            "area_id": device.get("area_id"),
            "entity_count": sum(1 for item in entities if item.get("device_id") == device.get("id")),
            "already_has_localtuya_entity": device.get("id") in local_device_ids,
        })
    return sanitize({"count": len(candidates), "candidates": candidates, "next_step": "Match candidates to router IP/MAC, then create and verify LocalTuya entries one device at a time."})


_LOCALTUYA_MANAGER: LocalTuyaManager | None = None


def localtuya_manager() -> LocalTuyaManager:
    global _LOCALTUYA_MANAGER
    if _LOCALTUYA_MANAGER is None:
        _LOCALTUYA_MANAGER = LocalTuyaManager(
            config_dir=CONFIG_DIR,
            share_dir=SHARE_DIR,
            data_dir=DATA_DIR,
            lan_cidr=LAN_CIDR,
            supervisor_token=SUPERVISOR_TOKEN,
            backup_callback=create_automatic_backup,
            audit_callback=append_audit,
        )
    return _LOCALTUYA_MANAGER


def tool_localtuya_capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return sanitize(localtuya_manager().capabilities())


def tool_localtuya_sync_keys(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(localtuya_manager().sync_keys_from_official_tuya(reason))


def tool_localtuya_probe_device(arguments: dict[str, Any]) -> dict[str, Any]:
    return sanitize(localtuya_manager().probe(arguments))


def tool_localtuya_stage_device(arguments: dict[str, Any]) -> dict[str, Any]:
    return sanitize(localtuya_manager().stage(arguments))


def tool_localtuya_apply_staged(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(localtuya_manager().apply(arguments.get("transaction_id"), reason))


def tool_localtuya_apply_batch(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(localtuya_manager().apply_batch(arguments.get("transaction_ids"), reason))


def tool_localtuya_rollback(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(localtuya_manager().rollback(arguments.get("transaction_id"), reason))


_MAINTENANCE_MANAGER: MaintenanceManager | None = None


def maintenance_manager() -> MaintenanceManager:
    global _MAINTENANCE_MANAGER
    if _MAINTENANCE_MANAGER is None:
        _MAINTENANCE_MANAGER = MaintenanceManager(
            data_dir=DATA_DIR,
            config_dir=CONFIG_DIR,
            share_dir=SHARE_DIR,
            supervisor_token=SUPERVISOR_TOKEN,
            current_version=VERSION,
            backup_callback=create_automatic_backup,
            audit_callback=append_audit,
        )
    return _MAINTENANCE_MANAGER


_SQL_MANAGER: Relax47SQLManager | None = None

def sql_manager() -> Relax47SQLManager:
    global _SQL_MANAGER
    if _SQL_MANAGER is None:
        _SQL_MANAGER = Relax47SQLManager(
            data_dir=DATA_DIR, config_dir=CONFIG_DIR,
            backup_callback=create_automatic_backup, audit_callback=append_audit,
            stop_callback=lambda: maintenance_manager()._supervisor_api(
                "/core/stop", method="POST", data={}, timeout=180),
            start_callback=lambda: maintenance_manager()._supervisor_api(
                "/core/start", method="POST", data={}, timeout=180),
        )
    return _SQL_MANAGER

def tool_sql_database_status(_: dict[str, Any]) -> dict[str, Any]: return sanitize(sql_manager().database_status())
def tool_sql_inspect_upload(arguments: dict[str, Any]) -> dict[str, Any]: return sanitize(sql_manager().inspect_upload(arguments.get("upload_id")))
def tool_sql_install_upload(arguments: dict[str, Any]) -> dict[str, Any]:
 reason=require_write(arguments); return sanitize(sql_manager().install_upload(arguments,reason))


def tool_sql_prepare_runtime(arguments):
    reason = require_write(arguments)
    return sanitize(prepare_runtime(sql_manager(), arguments, reason))


def tool_backup_retention(arguments):
    reason = require_write(arguments)
    return sanitize(BackupRetention(maintenance_manager(), lambda: WRITE_MODE).configure(arguments.get('enabled'), reason))


def tool_maintenance_status(arguments: dict[str, Any]) -> dict[str, Any]:
    return sanitize(maintenance_manager().status(arguments.get("upload_id")))


def tool_maintenance_upload_begin(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().begin(arguments, reason))


def tool_maintenance_upload_chunk(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().chunk(arguments, reason))


def tool_maintenance_upload_finalize(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().finalize(arguments, reason))


def tool_maintenance_backup_list(_: dict[str, Any]) -> dict[str, Any]:
    return sanitize(maintenance_manager().backup_list())



def tool_maintenance_backup_cleanup(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().cleanup_backups(arguments, reason))

def tool_maintenance_addon_list(_: dict[str, Any]) -> dict[str, Any]:
    return sanitize(maintenance_manager().addon_list())


def tool_maintenance_addon_action(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().addon_action(arguments, reason))


def tool_maintenance_backup_restore(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().restore_backup(arguments, reason))


def tool_maintenance_apply_gateway_update(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().apply_gateway_update(arguments, reason))


def tool_maintenance_config_list(arguments: dict[str, Any]) -> dict[str, Any]:
    return sanitize(maintenance_manager().config_list(arguments.get("path", "")))


def tool_maintenance_config_read(arguments: dict[str, Any]) -> dict[str, Any]:
    return sanitize(maintenance_manager().config_read(arguments.get("path")))


def tool_maintenance_config_apply_upload(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().apply_config_upload(arguments, reason))


def tool_maintenance_config_patch(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    return sanitize(maintenance_manager().patch_config(arguments, reason))


def tool_stage_dhcp_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    reservations = arguments.get("reservations")
    if not isinstance(reservations, list) or not reservations:
        raise ValueError("reservations must be a non-empty array")
    validated = []
    seen_ip: set[str] = set()
    seen_mac: set[str] = set()
    for item in reservations:
        if not isinstance(item, dict):
            raise ValueError("Each reservation must be an object")
        mac, ip, name = validate_reservation(item)
        if ip in seen_ip or mac in seen_mac:
            raise ValueError("Duplicate IP or MAC in reservation plan")
        seen_ip.add(ip)
        seen_mac.add(mac)
        validated.append({"name": name, "ip": ip, "mac": mac})
    live = current_reservations()
    conflicts = []
    for item in validated:
        collision = next((row for row in live if reservation_ip(row) == item["ip"] and reservation_mac(row) != item["mac"]), None)
        if collision:
            conflicts.append({"requested": item, "existing": collision})
    plan = {"generated_at": now_iso(), "router": ROUTER_HOST, "firmware": ROUTER_FIRMWARE, "reservations": validated, "live_conflicts": conflicts, "can_apply": not conflicts}
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    path = SHARE_DIR / "RELAX47_DHCP_PLAN.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"staged": True, "path": str(path), "count": len(validated), "conflicts": conflicts, "applied": False}


def tool_audit_log(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(arguments.get("limit", 50)), 500))
    if not AUDIT_PATH.exists():
        return {"count": 0, "entries": []}
    with AUDIT_LOCK:
        lines = AUDIT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return {"count": len(entries), "entries": entries}


def tool_export_inventory(arguments: dict[str, Any]) -> dict[str, Any]:
    include_ports = bool(arguments.get("include_ports", True))
    report: dict[str, Any] = {
        "generated_at": now_iso(), "gateway": tool_gateway_status({}),
        "router": tool_router_probe({}), "router_auth": tool_router_auth_status({}),
        "neighbors": tool_list_neighbors({}), "tuya": tool_tuya_registry_summary({}),
        "home_assistant_registry": tool_ha_registry_inventory({"include_entities": True}),
    }
    try:
        report["home_assistant"] = tool_ha_health({})
    except Exception as exc:
        report["home_assistant"] = {"reachable": False, "error": type(exc).__name__}
    if ROUTER_PASSWORD:
        try:
            report["router_clients"] = tool_router_clients({})
            report["dhcp_reservations"] = tool_router_dhcp_reservations({})
            report["router_network_exposure"] = tool_router_network_exposure({})
            if include_ports:
                report["port_inventory"] = tool_port_inventory(arguments)
        except Exception as exc:
            report["router_authenticated_inventory_error"] = type(exc).__name__
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    path = SHARE_DIR / "RELAX47_FULL_INVENTORY.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"exported": True, "path": str(path), "report": report}


def _terminal_safe_text(value: Any) -> str:
    text = str(value or "")
    lowered = text.lower()
    if any(part in lowered for part in TERMINAL_BLOCKED_PARTS):
        raise PermissionError("Terminal request targets protected credentials or host internals")
    if any(char in text for char in ("\x00", "\n", "\r")):
        raise ValueError("Terminal arguments must be single-line values")
    return text


def tool_terminal_exec(arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one non-shell, read-only diagnostic command inside the gateway container."""
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 24:
        raise ValueError("argv must contain between 1 and 24 items")
    clean = [_terminal_safe_text(item) for item in argv]
    command = Path(clean[0]).name
    if command not in TERMINAL_ALLOWED:
        raise PermissionError("Command is not in the read-only terminal allowlist")
    forbidden_options = {"-exec", "-execdir", "-delete", "-f", "--file", "--include", "--exclude-from"}
    if any(item.lower() in forbidden_options for item in clean[1:]):
        raise PermissionError("Command option is not allowed in the diagnostic terminal")
    cwd_text = _terminal_safe_text(arguments.get("cwd") or "/homeassistant")
    cwd = Path(cwd_text).resolve()
    if not any(cwd == root.resolve() or root.resolve() in cwd.parents for root in TERMINAL_ROOTS):
        raise PermissionError("Terminal working directory is outside approved gateway/config paths")
    content_commands = {"rg", "grep", "head", "tail"}
    content_roots = (
        CONFIG_DIR / "custom_components", CONFIG_DIR / "www", CONFIG_DIR / "packages",
        CONFIG_DIR / "blueprints", SHARE_DIR, Path("/opt/relax47"), Path("/run/relax47"),
    )
    if command in content_commands and not any(cwd == root.resolve() or root.resolve() in cwd.parents for root in content_roots):
        raise PermissionError("Content-search commands must run inside approved code/share directories; use maintenance_config_read for root configuration files")
    timeout = max(1, min(int(arguments.get("timeout_seconds", 20)), 60))
    started = time.monotonic()
    try:
        completed = subprocess.run(
            clean, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            check=False,
        )
        output = (completed.stdout + completed.stderr)[:1024 * 1024]
        redacted, replacements = MaintenanceManager._redact_text(output)
        return {
            "argv": clean, "cwd": str(cwd), "exit_code": completed.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": redacted, "output_truncated": len(output) >= 1024 * 1024,
            "redactions": replacements, "host_shell": False,
        }
    except subprocess.TimeoutExpired:
        raise TimeoutError("Diagnostic terminal command exceeded its timeout")


def tool_supervisor_diagnostics(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read approved Supervisor diagnostics without returning its bearer token."""
    target = str(arguments.get("target") or "supervisor").strip().lower()
    paths = {
        "supervisor": "/supervisor/info", "host": "/host/info", "core": "/core/info",
        "network": "/network/info", "addons": "/addons", "backups": "/backups",
    }
    if target not in paths:
        raise ValueError("Unsupported Supervisor diagnostic target")
    return {"target": target, "data": sanitize(maintenance_manager()._supervisor_api(paths[target], timeout=30)), "secrets_exposed": False}


def tool_maintenance_core_action(arguments: dict[str, Any]) -> dict[str, Any]:
    reason = require_write(arguments)
    action = str(arguments.get("action") or "").strip().lower()
    if action not in {"check", "restart"}:
        raise ValueError("Core action must be check or restart")
    safety = None
    if action == "restart":
        safety = create_automatic_backup(f"Safety backup before Home Assistant restart: {reason}")
    response = maintenance_manager()._supervisor_api(f"/core/{action}", method="POST", data={}, timeout=180)
    audit = append_audit(action=f"maintenance.core.{action}", reason=reason, before=None, after={"accepted": True}, verified=action == "check", changed=action == "restart")
    return {"accepted": True, "action": action, "safety_backup_created": bool(safety), "response": sanitize(response), "change_id": audit["change_id"]}


Tool = tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]], dict[str, Any]]
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE_SAFE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


TOOLS: dict[str, Tool] = {
    "gateway_status": ("Состояние шлюза, разрешений и канала Secure MCP.", {"type": "object", "properties": {}}, tool_gateway_status, READ_ONLY),
    "tunnel_status": ("Проверить исходящий канал OpenAI и получить безопасную диагностику.", {"type": "object", "properties": {}}, tool_tunnel_status, READ_ONLY),
    "ha_health": ("Проверить Home Assistant через внутренний API.", {"type": "object", "properties": {}}, tool_ha_health, READ_ONLY),
    "ha_get_state": ("Получить состояние одной сущности Home Assistant.", {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}, tool_ha_get_state, READ_ONLY),
    "ha_list_entities": ("Список состояний сущностей с фильтром.", {"type": "object", "properties": {"prefix": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}}, tool_ha_list_entities, READ_ONLY),
    "ha_registry_inventory": ("Инвентаризация областей, устройств, сущностей и интеграций Home Assistant.", {"type": "object", "properties": {"include_entities": {"type": "boolean"}}}, tool_ha_registry_inventory, READ_ONLY),
    "ha_call_service": ("Вызвать разрешённый сервис Home Assistant, затем прочитать целевые сущности и записать аудит.", {"type": "object", "properties": {"domain": {"type": "string"}, "service": {"type": "string"}, "service_data": {"type": "object"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["domain", "service", "change_reason"]}, tool_ha_call_service, WRITE_DESTRUCTIVE),
    "ha_create_backup": ("Создать автоматическую резервную копию Home Assistant через отдельный проверяемый путь и записать аудит.", {"type": "object", "properties": {"change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["change_reason"]}, tool_ha_create_backup, WRITE_SAFE),
    "router_probe": ("Проверить доступность и публичную информацию AX6000.", {"type": "object", "properties": {}}, tool_router_probe, READ_ONLY),
    "router_auth_status": ("Проверить локальную авторизацию AX6000 и доступные методы библиотеки.", {"type": "object", "properties": {}}, tool_router_auth_status, READ_ONLY),
    "router_clients": ("Получить авторизованный список клиентов AX6000.", {"type": "object", "properties": {}}, tool_router_clients, READ_ONLY),
    "router_dhcp_reservations": ("Прочитать текущие DHCP-привязки AX6000.", {"type": "object", "properties": {}}, tool_router_dhcp_reservations, READ_ONLY),
    "router_set_dhcp_reservation": ("Создать или заменить DHCP-привязку, проверить чтением и откатить непроверенный результат.", {"type": "object", "properties": {"mac": {"type": "string"}, "ip": {"type": "string"}, "name": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["mac", "ip", "change_reason"]}, tool_router_set_dhcp_reservation, WRITE_SAFE),
    "router_remove_dhcp_reservation": ("Удалить DHCP-привязку, проверить чтением и восстановить при неуспешной проверке.", {"type": "object", "properties": {"mac": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["mac", "change_reason"]}, tool_router_remove_dhcp_reservation, WRITE_DESTRUCTIVE),
    "router_network_exposure": ("Прочитать доступные данные о WAN-пробросах, DMZ, UPnP и NAT без изменений.", {"type": "object", "properties": {}}, tool_router_network_exposure, READ_ONLY),
    "list_neighbors": ("Прочитать локальную таблицу IP/MAC соседей.", {"type": "object", "properties": {}}, tool_list_neighbors, READ_ONLY),
    "scan_lan": ("Проверить TCP-порты только внутри настроенной локальной сети.", {"type": "object", "properties": {"cidr": {"type": "string"}, "ports": {"type": "array", "items": {"type": "integer"}}}}, tool_scan_lan, READ_ONLY),
    "port_inventory": ("Объединить клиентов, DHCP, ARP и открытые TCP-порты в одну карту устройств.", {"type": "object", "properties": {"cidr": {"type": "string"}, "ports": {"type": "array", "items": {"type": "integer"}}}}, tool_port_inventory, READ_ONLY),
    "tuya_registry_summary": ("Сводка облачной Tuya и LocalTuya без ключей и токенов.", {"type": "object", "properties": {}}, tool_tuya_registry_summary, READ_ONLY),
    "tuya_migration_candidates": ("Составить список устройств Tuya для переноса на LocalTuya.", {"type": "object", "properties": {}}, tool_tuya_migration_candidates, READ_ONLY),
    "localtuya_capabilities": ("Проверить версию LocalTuya, доступные локальные источники ключей и возможности безопасной миграции без раскрытия секретов.", {"type": "object", "properties": {}}, tool_localtuya_capabilities, READ_ONLY),
    "localtuya_sync_keys": ("Собрать local keys из уже авторизованной официальной Tuya, сохранить их только в защищённый файл 0600 и вернуть только счётчики без секретов; облачная Tuya не изменяется.", {"type": "object", "properties": {"change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["change_reason"]}, tool_localtuya_sync_keys, WRITE_SAFE),
    "localtuya_probe_device": ("Проверить локальную доступность Tuya и прочитать DP; local key берётся только из разрешённых локальных источников и не возвращается.", {"type": "object", "properties": {"device_id": {"type": "string"}, "host": {"type": "string"}, "protocol_version": {"type": "string", "enum": ["3.1", "3.2", "3.3", "3.4"]}}, "required": ["device_id", "host"]}, tool_localtuya_probe_device, READ_ONLY),
    "localtuya_stage_device": ("Проверить IP, ключ, протокол и DP, затем подготовить безопасную транзакцию одного устройства без изменения Home Assistant.", {"type": "object", "properties": {"device_id": {"type": "string"}, "host": {"type": "string"}, "friendly_name": {"type": "string"}, "protocol_version": {"type": "string", "enum": ["3.1", "3.2", "3.3", "3.4"]}, "entities": {"type": "array", "items": {"type": "object", "properties": {"dp": {"type": "integer", "minimum": 1, "maximum": 255}, "platform": {"type": "string"}, "friendly_name": {"type": "string"}, "options": {"type": "object"}}, "required": ["dp", "platform", "friendly_name"]}}, "verify_control": {"type": "boolean"}, "safety_class": {"type": "string", "enum": ["light", "switch_noncritical", "critical", "unknown"]}}, "required": ["device_id", "host", "friendly_name", "entities"]}, tool_localtuya_stage_device, WRITE_SAFE),
    "localtuya_apply_staged": ("Создать резервную копию, применить одну подготовленную LocalTuya-транзакцию, проверить результат и откатить только это устройство при ошибке.", {"type": "object", "properties": {"transaction_id": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["transaction_id", "change_reason"]}, tool_localtuya_apply_staged, WRITE_SAFE),
    "localtuya_apply_batch": ("Последовательно применить подготовленные LocalTuya-транзакции, продолжая после ошибки отдельного устройства.", {"type": "object", "properties": {"transaction_ids": {"type": "array", "items": {"type": "string"}}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["transaction_ids", "change_reason"]}, tool_localtuya_apply_batch, WRITE_SAFE),
    "localtuya_rollback": ("Откатить одну применённую LocalTuya-транзакцию по защищённому локальному снимку.", {"type": "object", "properties": {"transaction_id": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["transaction_id", "change_reason"]}, tool_localtuya_rollback, WRITE_DESTRUCTIVE),
    "sql_database_status": ("Проверить SQLite-базу RELAX47 без чтения строк.", {"type":"object","properties":{}}, tool_sql_database_status, READ_ONLY),
    "sql_prepare_runtime_migration": ("Создать приватный SQL-снимок восьми разрешённых Store RELAX47 с проверкой обратного чтения. Backup и остановка Core обязательны. Не переключает рабочую программу и не нормализует данные.", {"type":"object","properties":{"stop_home_assistant":{"type":"boolean"},"confirm_other_writers_stopped":{"type":"boolean"},"change_reason":{"type":"string"},"confirmation_code":{"type":"string"}},"required":["stop_home_assistant","confirm_other_writers_stopped","change_reason"]}, tool_sql_prepare_runtime, WRITE_SAFE),
    "maintenance_backup_retention": ("Включить или выключить ежедневную очистку: три последних и защищённые копии. Настройка сохраняется; первый запуск через 24 часа.", {"type":"object","properties":{"enabled":{"type":"boolean"},"change_reason":{"type":"string"},"confirmation_code":{"type":"string"}},"required":["enabled","change_reason"]}, tool_backup_retention, WRITE_SAFE),
    "sql_inspect_upload": ("Проверить staged SQLite.", {"type":"object","properties":{"upload_id":{"type":"string"}},"required":["upload_id"]}, tool_sql_inspect_upload, READ_ONLY),
    "sql_install_upload": ("Установить SQLite RELAX47 в согласованное окно обслуживания: backup, остановка Core, проверка, rollback при ошибке, запуск Core.", {"type":"object","properties":{"upload_id":{"type":"string"},"expected_sha256":{"type":"string"},"stop_home_assistant":{"type":"boolean"},"confirm_other_writers_stopped":{"type":"boolean"},"change_reason":{"type":"string"},"confirmation_code":{"type":"string"}},"required":["upload_id","expected_sha256","stop_home_assistant","confirm_other_writers_stopped","change_reason"]}, tool_sql_install_upload, WRITE_SAFE),
    "maintenance_status": ("Проверить загрузки, ограничения и результат автономного обновления без раскрытия содержимого файлов.", {"type": "object", "properties": {"upload_id": {"type": "string"}}}, tool_maintenance_status, READ_ONLY),
    "maintenance_upload_begin": ("Начать проверяемую передачу backup/share/config файла частями; задаются точный размер и SHA-256.", {"type": "object", "properties": {"filename": {"type": "string"}, "kind": {"type": "string", "enum": ["backup", "share", "config"]}, "target_path": {"type": "string"}, "size_bytes": {"type": "integer", "minimum": 1, "maximum": 268435456}, "sha256": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["filename", "kind", "size_bytes", "sha256", "change_reason"]}, tool_maintenance_upload_begin, WRITE_SAFE),
    "maintenance_upload_chunk": ("Передать следующий base64-фрагмент строго по ожидаемому смещению; содержимое не возвращается и не пишется в аудит.", {"type": "object", "properties": {"upload_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "data_base64": {"type": "string", "maxLength": 700000}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["upload_id", "offset", "data_base64", "change_reason"]}, tool_maintenance_upload_chunk, WRITE_SAFE),
    "maintenance_upload_finalize": ("Проверить размер и SHA-256, затем загрузить TAR в Supervisor или сохранить share/config-файл в защищённую staging-зону.", {"type": "object", "properties": {"upload_id": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["upload_id", "change_reason"]}, tool_maintenance_upload_finalize, WRITE_SAFE),
    "maintenance_backup_list": ("Получить список резервных копий Supervisor и доступные компоненты без паролей и токенов.", {"type": "object", "properties": {}}, tool_maintenance_backup_list, READ_ONLY),
    "maintenance_backup_cleanup": ("Удалить старые резервные копии, сохранив минимум последние 3 и protected; поддерживает dry-run.", {"type":"object","properties":{"keep_last":{"type":"integer","minimum":3,"maximum":30},"dry_run":{"type":"boolean"},"change_reason":{"type":"string"},"confirmation_code":{"type":"string"}},"required":["change_reason"]}, tool_maintenance_backup_cleanup, WRITE_SAFE),
    "maintenance_addon_list": ("Получить состояния и версии дополнений Home Assistant Supervisor.", {"type": "object", "properties": {}}, tool_maintenance_addon_list, READ_ONLY),
    "maintenance_addon_action": ("Запустить, остановить, перезапустить, обновить или установить выбранное дополнение; перед install/update создаётся страховочный backup.", {"type": "object", "properties": {"addon_slug": {"type": "string"}, "action": {"type": "string", "enum": ["start", "stop", "restart", "update", "install"]}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["addon_slug", "action", "change_reason"]}, tool_maintenance_addon_action, WRITE_DESTRUCTIVE),
    "maintenance_backup_restore": ("Создать страховочную копию и запустить частичное восстановление только выбранных компонентов существующего backup.", {"type": "object", "properties": {"backup_slug": {"type": "string"}, "folders": {"type": "array", "items": {"type": "string"}}, "addons": {"type": "array", "items": {"type": "string"}}, "homeassistant": {"type": "boolean"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["backup_slug", "change_reason"]}, tool_maintenance_backup_restore, WRITE_DESTRUCTIVE),
    "maintenance_apply_gateway_update": ("Создать backup, восстановить addons/local из загруженного архива, перечитать Store и автономно обновить RELAX47 Gateway; соединение может перезапуститься.", {"type": "object", "properties": {"backup_slug": {"type": "string"}, "expected_version": {"type": "string"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["backup_slug", "expected_version", "change_reason"]}, tool_maintenance_apply_gateway_update, WRITE_SAFE),
    "maintenance_config_list": ("Просмотреть безопасную часть каталога конфигурации Home Assistant; защищённые хранилища и credentials скрыты.", {"type": "object", "properties": {"path": {"type": "string"}}}, tool_maintenance_config_list, READ_ONLY),
    "maintenance_config_read": ("Прочитать разрешённый текстовый файл конфигурации с автоматическим скрытием секретных значений и получить SHA-256.", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, tool_maintenance_config_read, READ_ONLY),
    "maintenance_config_apply_upload": ("Применить staged config-файл: backup, атомарная запись, проверка HA, автоматический откат при ошибке и необязательный перезапуск.", {"type": "object", "properties": {"upload_id": {"type": "string"}, "expected_current_sha256": {"type": "string"}, "confirm_replace_existing": {"type": "boolean"}, "restart_home_assistant": {"type": "boolean"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["upload_id", "change_reason"]}, tool_maintenance_config_apply_upload, WRITE_SAFE),
    "maintenance_config_patch": ("Транзакционно изменить разрешённый config-файл точными заменами на сервере, не раскрывая его секреты: backup, HA check и rollback.", {"type": "object", "properties": {"path": {"type": "string"}, "expected_sha256": {"type": "string"}, "replacements": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}, "expected_count": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["old", "new"]}}, "restart_home_assistant": {"type": "boolean"}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["path", "expected_sha256", "replacements", "change_reason"]}, tool_maintenance_config_patch, WRITE_SAFE),
    "stage_dhcp_plan": ("Проверить план DHCP по живым привязкам и сохранить его в /share.", {"type": "object", "properties": {"reservations": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "ip": {"type": "string"}, "mac": {"type": "string"}}, "required": ["ip", "mac"]}}}, "required": ["reservations"]}, tool_stage_dhcp_plan, READ_ONLY),
    "audit_log": ("Прочитать журнал проверенных изменений.", {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}}}, tool_audit_log, READ_ONLY),
    "export_inventory": ("Сформировать полный безопасный отчёт Home Assistant, роутера, DHCP, Tuya и портов в /share.", {"type": "object", "properties": {"include_ports": {"type": "boolean"}, "cidr": {"type": "string"}, "ports": {"type": "array", "items": {"type": "integer"}}}}, tool_export_inventory, READ_ONLY),
    "terminal_exec": ("Выполнить одну неинтерактивную read-only терминальную команду внутри контейнера Gateway без shell и доступа к секретам.", {"type": "object", "properties": {"argv": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"type": "string"}}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, "required": ["argv"]}, tool_terminal_exec, READ_ONLY),
    "supervisor_diagnostics": ("Получить безопасную диагностику Supervisor, host, Core, network, apps или backups без токенов.", {"type": "object", "properties": {"target": {"type": "string", "enum": ["supervisor", "host", "core", "network", "addons", "backups"]}}}, tool_supervisor_diagnostics, READ_ONLY),
    "maintenance_core_action": ("Проверить конфигурацию или после страховочной копии перезапустить Home Assistant Core.", {"type": "object", "properties": {"action": {"type": "string", "enum": ["check", "restart"]}, "change_reason": {"type": "string"}, "confirmation_code": {"type": "string"}}, "required": ["action", "change_reason"]}, tool_maintenance_core_action, WRITE_DESTRUCTIVE),
}


def mcp_tool_list() -> list[dict[str, Any]]:
    return [{"name": name, "description": description, "inputSchema": schema, "annotations": annotations} for name, (description, schema, _, annotations) in TOOLS.items()]


def mcp_result(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload}


def safe_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, PermissionError, KeyError, NotImplementedError)):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: operation failed; inspect the add-on log and audit"


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "RELAX47Gateway/7.14.11"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {fmt % args}", flush=True)

    def send_json(self, payload: Any, status: int = 200, *, request_id: Any = None) -> None:
        if request_id is not None:
            payload = {"jsonrpc": "2.0", "id": request_id, "result": payload}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if self.server.server_port == 8765 and self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.server.server_port == 8099 and self.client_address[0] not in ADMIN_ALLOWED_SOURCES:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if path in {"/healthz", "/api/health"}:
            self.send_json({"ok": True, "version": VERSION, "write_mode": WRITE_MODE})
            return
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/api/status": tool_gateway_status, "/api/tunnel": tool_tunnel_status,
            "/api/router-auth": tool_router_auth_status, "/api/audit": tool_audit_log,
            "/api/inventory": tool_export_inventory,
        }
        if path in routes:
            try:
                payload = routes[path]({})
                self.send_json(payload.get("report", payload) if path == "/api/inventory" else payload)
            except Exception as exc:
                self.send_json({"error": safe_error(exc)}, status=500)
            return
        if path == "/":
            status = tool_gateway_status({})
            tunnel = status["tunnel"]
            html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>RELAX47 Local Gateway</title>
<style>body{{font-family:system-ui;background:#eef6fb;color:#15324b;margin:0;padding:20px}}main{{max-width:820px;margin:auto}}.card{{background:white;border-radius:18px;padding:20px;margin:14px 0;box-shadow:0 4px 18px #174a6b22}}h1{{color:#0586c7}}code{{background:#e6f3fa;padding:3px 7px;border-radius:7px}}.ok{{color:#17864b}}a{{color:#057db9}}li{{margin:8px 0}}</style></head><body><main>
<h1>RELAX47 Local Gateway</h1><div class='card'><h2>Состояние</h2><p class='ok'>Шлюз запущен, версия {VERSION}</p>
<p>AX6000: <code>{ROUTER_HOST}</code>, {ROUTER_MODEL}, прошивка {ROUTER_FIRMWARE}</p>
<p>Режим записи: <strong>{'ВКЛЮЧЁН' if WRITE_MODE else 'выключен'}</strong>; проверка после записи: <strong>{'включена' if AUTO_VERIFY_WRITES else 'выключена'}</strong></p></div>
<div class='card'><h2>Связь с OpenAI</h2><p>Состояние: <strong>{tunnel.get('state')}</strong>; ready: <strong>{'да' if tunnel.get('ready') else 'нет'}</strong></p><p>{tunnel.get('diagnosis') or tunnel.get('detail', '')}</p><p>Входящий порт на роутере не требуется.</p></div>
<div class='card'><h2>Что умеет версия</h2><ul><li>полная карта HA, DHCP, клиентов, Tuya и TCP-портов;</li><li>проверяемые DHCP-привязки с защитой конфликтов и откатом;</li><li>вызовы разрешённых сервисов HA с чтением результата;</li><li>постоянный локальный журнал всех изменений.</li></ul></div>
<div class='card'><h2>Диагностика</h2><p><a href='api/status'>Статус</a></p><p><a href='api/tunnel'>Secure MCP</a></p><p><a href='api/router-auth'>Авторизация AX6000</a></p><p><a href='api/inventory'>Полная инвентаризация</a></p><p><a href='api/audit'>Журнал изменений</a></p></div>
</main></body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request: Any = {}
        if self.server.server_port != 8765 or self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 2 * 1024 * 1024)
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "RELAX47 Local Gateway", "version": VERSION},
                    "instructions": "Read current state before writes. Write tools require change_reason, record audit, and perform readback. Never open inbound router ports for this connector.",
                }
            elif method in {"notifications/initialized", "notifications/cancelled"}:
                self.send_response(202)
                self.end_headers()
                return
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tool_list()}
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                if name not in TOOLS:
                    raise KeyError(f"Unknown tool: {name}")
                payload = TOOLS[name][2](params.get("arguments") or {})
                result = mcp_result(payload)
            elif method in {"resources/list", "prompts/list"}:
                result = {"resources" if method.startswith("resources") else "prompts": []}
            else:
                raise KeyError(f"Unsupported method: {method}")
            self.send_json(result, request_id=request_id)
        except PermissionError as exc:
            self.send_json({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32001, "message": safe_error(exc)}}, status=403)
        except Exception as exc:
            self.send_json({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32603, "message": safe_error(exc)}}, status=400)


def serve(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0" if port == 8099 else "127.0.0.1", port), GatewayHandler)
    server.serve_forever()


if __name__ == "__main__":
    retention = BackupRetention(maintenance_manager(), lambda: WRITE_MODE)
    threading.Thread(target=retention.run, daemon=True).start()
    threading.Thread(target=serve, args=(8099,), daemon=True).start()
    serve(8765)
