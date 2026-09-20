#!/usr/bin/env python3
"""Public HTTPS/OAuth facade for the RELAX47 MCP server.

The facade keeps Home Assistant and router credentials inside the add-on. ChatGPT
receives a short-lived OAuth token only after the owner enters the activation
code in the browser-based authorization screen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import ssl
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import gateway


PORT = int(os.environ.get("RELAX47_PUBLIC_MCP_PORT", "8766"))
BASE_URL = os.environ.get("RELAX47_PUBLIC_BASE_URL", "").rstrip("/")
ACTIVATION_CODE = os.environ.get("RELAX47_OAUTH_ACTIVATION_CODE", "")
TOKEN_HOURS = max(1, min(168, int(os.environ.get("RELAX47_OAUTH_TOKEN_HOURS", "12"))))
CERT_FILE = Path(os.environ.get("RELAX47_TLS_CERT_FILE", "/ssl/fullchain.pem"))
KEY_FILE = Path(os.environ.get("RELAX47_TLS_KEY_FILE", "/ssl/privkey.pem"))
STATE_FILE = Path(os.environ.get("RELAX47_OAUTH_STATE_FILE", "/data/RELAX47_OAUTH_STATE.json"))
STATE_LOCK = threading.Lock()
AUTH_FAILURE_LOCK = threading.Lock()
AUTH_FAILURES: dict[str, list[int]] = {}
ALLOWED_SCOPES = {"mcp:read", "mcp:write", "mcp:admin"}
MAX_REQUEST_BYTES = max(65536, min(4 * 1024 * 1024, int(os.environ.get("RELAX47_PUBLIC_MAX_REQUEST_BYTES", "2097152"))))
RATE_LIMIT = max(10, min(600, int(os.environ.get("RELAX47_PUBLIC_RATE_LIMIT_PER_MINUTE", "120"))))
SESSION_HOURS = max(1, min(168, int(os.environ.get("RELAX47_PUBLIC_SESSION_HOURS", "12"))))
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-11-25", "2026-07-28"}
SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
RATE_BUCKETS: dict[str, list[int]] = {}
WRITE_LOCK = threading.Lock()
IDEMPOTENCY_LOCK = threading.Lock()
IDEMPOTENCY: dict[str, tuple[int, dict[str, Any]]] = {}
CONNECTION_AUDIT = Path(os.environ.get("RELAX47_CONNECTION_AUDIT_FILE", "/data/RELAX47_CONNECTION_AUDIT.jsonl"))
CONNECTION_AUDIT_LOCK = threading.Lock()


def now() -> int:
    return int(time.time())


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def empty_state() -> dict[str, Any]:
    return {"clients": {}, "codes": {}, "tokens": {}, "refresh_tokens": {}}


def audit_connection(event: str, **fields: Any) -> None:
    entry = {"timestamp": now(), "event": event, **gateway.sanitize(fields)}
    CONNECTION_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with CONNECTION_AUDIT_LOCK:
        with CONNECTION_AUDIT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def allow_request(key: str) -> bool:
    cutoff = now() - 60
    with RATE_LOCK:
        recent = [stamp for stamp in RATE_BUCKETS.get(key, []) if stamp > cutoff]
        if len(recent) >= RATE_LIMIT:
            RATE_BUCKETS[key] = recent
            return False
        recent.append(now())
        RATE_BUCKETS[key] = recent
        return True


def create_session(identity: dict[str, Any], request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    params = request.get("params") or {}
    client_info = params.get("clientInfo") or {}
    requested = str(params.get("protocolVersion") or "2025-06-18")
    protocol = requested if requested in SUPPORTED_PROTOCOLS else "2025-06-18"
    session_id = uuid.uuid4().hex
    record = {
        "session_id": session_id,
        "client_id": identity.get("client_id", ""),
        "client_name": str(client_info.get("name") or "unknown")[:120],
        "client_version": str(client_info.get("version") or "")[:80],
        "protocol": protocol,
        "created_at": now(),
        "last_seen": now(),
        "expires_at": now() + SESSION_HOURS * 3600,
    }
    with SESSION_LOCK:
        SESSIONS[session_id] = record
    audit_connection("session.created", **record)
    return session_id, record


def get_session(session_id: str, identity: dict[str, Any]) -> dict[str, Any] | None:
    with SESSION_LOCK:
        record = SESSIONS.get(session_id)
        if not record or record.get("expires_at", 0) <= now() or record.get("client_id") != identity.get("client_id"):
            SESSIONS.pop(session_id, None)
            return None
        record["last_seen"] = now()
        return dict(record)


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty_state()
    except (OSError, ValueError, TypeError):
        return empty_state()
    for key in ("clients", "codes", "tokens", "refresh_tokens"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def prune_state(state: dict[str, Any]) -> None:
    timestamp = now()
    for key in ("clients", "codes", "tokens", "refresh_tokens"):
        state[key] = {
            identifier: item
            for identifier, item in state[key].items()
            if int(item.get("expires_at", 0)) > timestamp
        }


def activation_is_allowed(source: str) -> bool:
    cutoff = now() - 900
    with AUTH_FAILURE_LOCK:
        recent = [value for value in AUTH_FAILURES.get(source, []) if value > cutoff]
        AUTH_FAILURES[source] = recent
        return len(recent) < 5


def record_activation_failure(source: str) -> None:
    with AUTH_FAILURE_LOCK:
        AUTH_FAILURES.setdefault(source, []).append(now())


def clear_activation_failures(source: str) -> None:
    with AUTH_FAILURE_LOCK:
        AUTH_FAILURES.pop(source, None)


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_FILE)


def with_state(update: Any) -> Any:
    with STATE_LOCK:
        state = load_state()
        prune_state(state)
        result = update(state)
        save_state(state)
        return result


def requested_scopes(value: str | None) -> list[str]:
    requested = [item for item in (value or "mcp:read mcp:write mcp:admin").split() if item]
    if not requested or any(item not in ALLOWED_SCOPES for item in requested):
        raise ValueError("Unsupported OAuth scope")
    # ChatGPT connectors currently request read/write explicitly and therefore
    # omit the gateway-specific admin scope.  A connection that the owner has
    # approved for writes also needs the protected diagnostic tools; read-only
    # clients remain read-only and never receive admin implicitly.
    if "mcp:write" in requested and "mcp:admin" not in requested:
        requested.append("mcp:admin")
    return list(dict.fromkeys(requested))


def client_registration(client_id: str) -> dict[str, Any] | None:
    return with_state(lambda state: state["clients"].get(client_id))


def register_client(payload: dict[str, Any]) -> dict[str, Any]:
    redirect_uris = payload.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or len(redirect_uris) > 10:
        raise ValueError("redirect_uris is required")
    clean_uris: list[str] = []
    for value in redirect_uris:
        parsed = urllib.parse.urlparse(str(value))
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.netloc or parsed.fragment:
            raise ValueError("Every redirect URI must use HTTPS (HTTP is allowed only for loopback clients)")
        clean_uris.append(str(value))
    client_id = new_secret("relax47_client_")
    record = {
        "client_id": client_id,
        "client_id_issued_at": now(),
        "expires_at": now() + 30 * 24 * 3600,
        "client_name": str(payload.get("client_name") or "ChatGPT MCP")[:120],
        "redirect_uris": clean_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state["clients"][client_id] = record
        return record

    return with_state(update)


def issue_authorization_code(
    *, client_id: str, redirect_uri: str, scopes: list[str],
    code_challenge: str, resource: str | None,
) -> str:
    code = new_secret("relax47_code_")
    record = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scopes": scopes,
        "code_challenge": code_challenge,
        "resource": resource,
        "expires_at": now() + 300,
    }

    def update(state: dict[str, Any]) -> str:
        state["codes"][token_hash(code)] = record
        return code

    return with_state(update)


def issue_tokens(*, client_id: str, scopes: list[str]) -> dict[str, Any]:
    access_token = new_secret("relax47_access_")
    refresh_token = new_secret("relax47_refresh_")
    expires_in = TOKEN_HOURS * 3600
    access_record = {
        "client_id": client_id,
        "scopes": scopes,
        "expires_at": now() + expires_in,
    }
    refresh_record = {
        "client_id": client_id,
        "scopes": scopes,
        "expires_at": now() + 30 * 24 * 3600,
    }

    def update(state: dict[str, Any]) -> None:
        state["tokens"][token_hash(access_token)] = access_record
        state["refresh_tokens"][token_hash(refresh_token)] = refresh_record

    with_state(update)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_token": refresh_token,
        "scope": " ".join(scopes),
    }


def exchange_authorization_code(form: dict[str, str]) -> dict[str, Any]:
    code = form.get("code", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    verifier = form.get("code_verifier", "")

    def consume(state: dict[str, Any]) -> dict[str, Any]:
        record = state["codes"].pop(token_hash(code), None)
        if not record:
            raise PermissionError("Authorization code is invalid or expired")
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            raise PermissionError("OAuth client or redirect URI does not match")
        if not verifier or not hmac.compare_digest(b64url_sha256(verifier), record["code_challenge"]):
            raise PermissionError("PKCE verification failed")
        return record

    record = with_state(consume)
    return issue_tokens(client_id=client_id, scopes=record["scopes"])


def exchange_refresh_token(form: dict[str, str]) -> dict[str, Any]:
    refresh_token = form.get("refresh_token", "")
    client_id = form.get("client_id", "")

    def consume(state: dict[str, Any]) -> dict[str, Any]:
        record = state["refresh_tokens"].pop(token_hash(refresh_token), None)
        if not record or record["client_id"] != client_id:
            raise PermissionError("Refresh token is invalid or expired")
        return record

    record = with_state(consume)
    return issue_tokens(client_id=client_id, scopes=record["scopes"])


def authenticate(header: str | None) -> dict[str, Any] | None:
    if not header or not header.startswith("Bearer "):
        return None
    supplied = header[7:].strip()
    if not supplied:
        return None
    return with_state(lambda state: state["tokens"].get(token_hash(supplied)))


class PublicMCPHandler(BaseHTTPRequestHandler):
    server_version = "RELAX47PublicMCP/7.14.9"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[public-mcp {self.log_date_time_string()}] {self.client_address[0]} {fmt % args}", flush=True)

    def json_response(
        self, payload: Any, status: int = 200, *, headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def html_response(self, body: str, status: int = 200) -> None:
        content = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def read_body(self, limit: int = MAX_REQUEST_BYTES) -> bytes:
        announced = max(0, int(self.headers.get("Content-Length", "0")))
        if announced > limit:
            raise OverflowError("Request body is too large")
        length = announced
        return self.rfile.read(length)

    def form_data(self) -> dict[str, str]:
        values = urllib.parse.parse_qs(self.read_body().decode("utf-8"), keep_blank_values=True)
        return {key: items[-1] for key, items in values.items()}

    def unauthorized(self, message: str = "OAuth authorization is required") -> None:
        metadata = f'{BASE_URL}/.well-known/oauth-protected-resource/mcp'
        self.json_response(
            {"error": "unauthorized", "error_description": message},
            status=HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
        )

    def oauth_metadata(self) -> dict[str, Any]:
        return {
            "issuer": BASE_URL,
            "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
            "token_endpoint": f"{BASE_URL}/oauth/token",
            "registration_endpoint": f"{BASE_URL}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": sorted(ALLOWED_SCOPES),
        }

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": f"{BASE_URL}/mcp",
            "authorization_servers": [BASE_URL],
            "scopes_supported": sorted(ALLOWED_SCOPES),
            "bearer_methods_supported": ["header"],
        }

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in {"/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"}:
            self.json_response(self.oauth_metadata())
            return
        if path in {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}:
            self.json_response(self.protected_resource_metadata())
            return
        if path == "/oauth/authorize":
            self.show_authorization(urllib.parse.parse_qs(parsed.query, keep_blank_values=True))
            return
        if path == "/healthz":
            self.json_response({"ok": True, "version": gateway.VERSION, "oauth": True})
            return
        if path == "/":
            self.html_response("<!doctype html><html lang='ru'><meta charset='utf-8'><title>RELAX47 MCP</title><style>body{font-family:system-ui;max-width:680px;margin:4rem auto;padding:1rem}code{background:#eef;padding:.2rem .4rem}</style><h1>RELAX47 MCP</h1><p>Защищённый MCP-шлюз работает.</p><p>Endpoint: <code>/mcp</code></p></html>")
            return
        if path == "/mcp":
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "POST, DELETE")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version, Idempotency-Key")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id, MCP-Protocol-Version")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def show_authorization(self, query: dict[str, list[str]], *, error: str = "") -> None:
        values = {key: items[-1] for key, items in query.items()}
        client_id = values.get("client_id", "")
        redirect_uri = values.get("redirect_uri", "")
        response_type = values.get("response_type", "")
        challenge = values.get("code_challenge", "")
        challenge_method = values.get("code_challenge_method", "")
        registration = client_registration(client_id)
        valid = bool(
            registration
            and redirect_uri in registration.get("redirect_uris", [])
            and response_type == "code"
            and challenge
            and challenge_method == "S256"
        )
        if not valid:
            self.html_response("<!doctype html><html lang='ru'><meta charset='utf-8'><h1>Запрос авторизации недействителен</h1><p>Вернитесь в ChatGPT и создайте подключение заново.</p></html>", status=400)
            return
        hidden = "".join(
            f"<input type='hidden' name='{html.escape(key)}' value='{html.escape(value, quote=True)}'>"
            for key, value in values.items()
        )
        error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        client_name = html.escape(str(registration.get("client_name") or "ChatGPT MCP"))
        scope_text = html.escape(values.get("scope") or "mcp:read mcp:write")
        page = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>RELAX47 — авторизация</title><style>body{{font-family:system-ui;background:#eef6fb;color:#15324b;margin:0}}main{{max-width:520px;margin:8vh auto;background:#fff;padding:28px;border-radius:20px;box-shadow:0 8px 30px #174a6b22}}input{{box-sizing:border-box;width:100%;padding:14px;margin:10px 0;border:1px solid #8aa7b8;border-radius:10px;font-size:18px}}button{{width:100%;padding:14px;border:0;border-radius:10px;background:#0786c7;color:white;font-size:18px}}.error{{color:#b42318}}code{{background:#eef3f6;padding:3px 6px;border-radius:6px}}</style></head><body><main><h1>RELAX47 Gateway</h1><p>Подключение: <strong>{client_name}</strong></p><p>Запрашиваемые права: <code>{scope_text}</code></p><p>Введите код активации, сохранённый локально в настройках дополнения. Код не передаётся в чат.</p>{error_html}<form method='post' action='/oauth/authorize'>{hidden}<label>Код активации<input type='password' name='activation_code' minlength='12' required autocomplete='one-time-code'></label><button type='submit'>Разрешить подключение</button></form></main></body></html>"""
        self.html_response(page)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/oauth/register":
                payload = json.loads(self.read_body().decode("utf-8"))
                self.json_response(register_client(payload), status=HTTPStatus.CREATED)
                return
            if path == "/oauth/authorize":
                self.authorize_submission(self.form_data())
                return
            if path == "/oauth/token":
                self.token_exchange(self.form_data())
                return
            if path == "/mcp":
                self.handle_mcp()
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except OverflowError as exc:
            self.json_response({"error": "request_too_large", "error_description": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except (ValueError, PermissionError, KeyError) as exc:
            self.json_response({"error": "invalid_request", "error_description": str(exc)}, status=400)
        except Exception as exc:
            self.json_response({"error": "server_error", "error_description": gateway.safe_error(exc)}, status=500)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        identity = authenticate(self.headers.get("Authorization"))
        if not identity:
            self.unauthorized()
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        with SESSION_LOCK:
            record = SESSIONS.get(session_id)
            if record and record.get("client_id") == identity.get("client_id"):
                SESSIONS.pop(session_id, None)
                audit_connection("session.deleted", session_id=session_id, client_id=identity.get("client_id"))
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def authorize_submission(self, form: dict[str, str]) -> None:
        query = {key: [value] for key, value in form.items() if key != "activation_code"}
        source = self.client_address[0]
        if not activation_is_allowed(source):
            self.show_authorization(query, error="Слишком много попыток. Повторите через 15 минут")
            return
        if not ACTIVATION_CODE or not hmac.compare_digest(form.get("activation_code", ""), ACTIVATION_CODE):
            record_activation_failure(source)
            self.show_authorization(query, error="Неверный код активации")
            return
        clear_activation_failures(source)
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        registration = client_registration(client_id)
        if not registration or redirect_uri not in registration.get("redirect_uris", []):
            raise PermissionError("OAuth client is not registered")
        scopes = requested_scopes(form.get("scope"))
        code = issue_authorization_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            code_challenge=form.get("code_challenge", ""),
            resource=form.get("resource"),
        )
        parameters = {"code": code}
        if form.get("state"):
            parameters["state"] = form["state"]
        separator = "&" if urllib.parse.urlparse(redirect_uri).query else "?"
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", redirect_uri + separator + urllib.parse.urlencode(parameters))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def token_exchange(self, form: dict[str, str]) -> None:
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            payload = exchange_authorization_code(form)
        elif grant_type == "refresh_token":
            payload = exchange_refresh_token(form)
        else:
            raise ValueError("Unsupported grant_type")
        self.json_response(payload)

    def handle_mcp(self) -> None:
        identity = authenticate(self.headers.get("Authorization"))
        if not identity:
            self.unauthorized()
            return
        scopes = set(identity.get("scopes") or [])
        if "mcp:read" not in scopes:
            self.unauthorized("The token does not include mcp:read")
            return
        rate_key = f"{identity.get('client_id', '')}:{self.client_address[0]}"
        if not allow_request(rate_key):
            self.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32029, "message": "Rate limit exceeded"}}, status=HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "60"})
            return
        request = json.loads(self.read_body().decode("utf-8"))
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            session_id, session = create_session(identity, request)
            result = {
                "protocolVersion": session["protocol"],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "RELAX47 Local Gateway", "version": gateway.VERSION},
                "instructions": "Read before every write. Writes require owner approval, a reason, readback verification and audit.",
            }
            self.json_response({"jsonrpc": "2.0", "id": request_id, "result": result}, headers={"Mcp-Session-Id": session_id, "MCP-Protocol-Version": session["protocol"]})
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        session = get_session(session_id, identity)
        if not session:
            self.json_response({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "MCP session is missing or expired"}}, status=HTTPStatus.NOT_FOUND)
            return
        if method in {"notifications/initialized", "notifications/cancelled"}:
            self.send_response(HTTPStatus.ACCEPTED)
            self.end_headers()
            return
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            tools = gateway.mcp_tool_list()
            if "mcp:admin" not in scopes:
                tools = [item for item in tools if item.get("name") not in {"terminal_exec", "supervisor_diagnostics", "maintenance_core_action", "sql_database_status", "sql_inspect_upload", "sql_install_upload", "maintenance_backup_cleanup"}]
            result = {"tools": tools}
        elif method == "tools/call":
            params = request.get("params") or {}
            raw_name = str(params.get("name") or "")
            # Connector hosts may namespace MCP tool names as
            # "relax47_mcp.gateway_status".  The local registry stores the
            # canonical final component only.
            name = raw_name.rsplit(".", 1)[-1]
            if name not in gateway.TOOLS:
                raise KeyError(f"Unknown tool: {raw_name}")
            if name in {"terminal_exec", "supervisor_diagnostics", "maintenance_core_action", "sql_database_status", "sql_inspect_upload", "sql_install_upload", "maintenance_backup_cleanup"} and "mcp:admin" not in scopes:
                raise PermissionError("The token does not include mcp:admin")
            annotations = gateway.TOOLS[name][3]
            if not annotations.get("readOnlyHint", False) and "mcp:write" not in scopes:
                raise PermissionError("The token does not include mcp:write")
            context = {"client_id": identity.get("client_id"), "client_name": session.get("client_name"), "session_id": session_id, "request_id": request_id}
            gateway.set_audit_context(context)
            try:
                if annotations.get("readOnlyHint", False):
                    payload = gateway.TOOLS[name][2](params.get("arguments") or {})
                else:
                    idem_key = self.headers.get("Idempotency-Key", "").strip()
                    cache_key = f"{identity.get('client_id')}:{idem_key}" if idem_key else ""
                    with IDEMPOTENCY_LOCK:
                        cached = IDEMPOTENCY.get(cache_key) if cache_key else None
                    if cached and cached[0] > now():
                        payload = cached[1]
                    else:
                        with WRITE_LOCK:
                            payload = gateway.TOOLS[name][2](params.get("arguments") or {})
                        if cache_key:
                            with IDEMPOTENCY_LOCK:
                                IDEMPOTENCY[cache_key] = (now() + 24 * 3600, payload)
            finally:
                gateway.set_audit_context(None)
            result = gateway.mcp_result(payload)
        elif method in {"resources/list", "prompts/list"}:
            result = {"resources" if method.startswith("resources") else "prompts": []}
        else:
            raise KeyError(f"Unsupported method: {method}")
        audit_connection("mcp.request", client_id=identity.get("client_id"), client_name=session.get("client_name"), session_id=session_id, method=method, tool=(request.get("params") or {}).get("name"))
        self.json_response({"jsonrpc": "2.0", "id": request_id, "result": result}, headers={"Mcp-Session-Id": session_id, "MCP-Protocol-Version": session["protocol"]})


def main() -> None:
    if not BASE_URL.startswith("https://"):
        raise SystemExit("RELAX47_PUBLIC_BASE_URL must be an HTTPS origin")
    if len(ACTIVATION_CODE) < 12:
        raise SystemExit("RELAX47_OAUTH_ACTIVATION_CODE must contain at least 12 characters")
    if not CERT_FILE.is_file() or not KEY_FILE.is_file():
        raise SystemExit("TLS certificate files are missing in /ssl")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), PublicMCPHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"RELAX47 public HTTPS MCP {gateway.VERSION} listening on {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
