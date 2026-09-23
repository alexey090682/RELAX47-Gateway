"""Isolated SQL storage for public Relax47 website accounts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid


SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 310_000


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Укажите корректную электронную почту.")
    return email


def password_digest(password: str, salt: bytes, iterations: int = PASSWORD_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def clean_quote(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Некорректный расчёт.")
    arrival = str(value.get("arrival") or "")
    departure = str(value.get("departure") or "")
    try:
        arrival_date = datetime.strptime(arrival, "%Y-%m-%d").date()
        departure_date = datetime.strptime(departure, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Проверьте даты расчёта.") from exc
    nights = (departure_date - arrival_date).days
    if arrival_date < date.today() or not 2 <= nights <= 60:
        raise ValueError("Расчёт должен содержать от 2 до 60 будущих ночей.")

    def number(name: str, minimum: int, maximum: int, default: int = 0) -> int:
        try:
            result = int(value.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ValueError("Проверьте параметры расчёта.") from exc
        if not minimum <= result <= maximum:
            raise ValueError("Проверьте параметры расчёта.")
        return result

    spa = str(value.get("spa") or "none")
    if spa not in {"none", "heated", "cold"}:
        raise ValueError("Проверьте параметры бани и бассейна.")
    return {
        "arrival": arrival,
        "departure": departure,
        "guests": number("guests", 1, 50, 2),
        "rooms": number("rooms", 0, 4),
        "spa": spa,
        "spaDays": number("spaDays", 1, 60, 1),
        "spaExtra": number("spaExtra", 0, 8),
        "early": number("early", 0, 8),
        "late": number("late", 0, 8),
    }


class SiteAccountStore:
    def __init__(self, path: Path | str = "/data/RELAX47_SITE.db") -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS website_users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    consent_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS website_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES website_users(id) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS website_quotes (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES website_users(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    input_json TEXT NOT NULL CHECK(json_valid(input_json)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_website_sessions_expiry ON website_sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_website_quotes_user_updated ON website_quotes(user_id,updated_at DESC);
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, str]:
        return {"id": row["id"], "name": row["name"], "email": row["email"]}

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _new_session(self, connection: sqlite3.Connection, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        timestamp = int(time.time())
        connection.execute(
            "INSERT INTO website_sessions(token_hash,user_id,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?)",
            (self._token_hash(token), user_id, timestamp, timestamp + SESSION_SECONDS, timestamp),
        )
        return token

    def session_user(self, token: str) -> dict[str, str] | None:
        if not token:
            return None
        timestamp = int(time.time())
        token_hash = self._token_hash(token)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM website_sessions WHERE expires_at<=?", (timestamp,))
            row = connection.execute(
                """SELECT u.id,u.name,u.email FROM website_sessions s
                   JOIN website_users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1""",
                (token_hash, timestamp),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE website_sessions SET last_seen_at=? WHERE token_hash=?",
                    (timestamp, token_hash),
                )
            return self._public_user(row) if row else None

    def register(self, payload: dict[str, Any]) -> tuple[dict[str, str], str]:
        name = " ".join(str(payload.get("name") or "").strip().split())
        email = normalize_email(payload.get("email"))
        password = str(payload.get("password") or "")
        consented = str(payload.get("consent") or "").casefold() in {"1", "true", "yes", "on"}
        if not 2 <= len(name) <= 80:
            raise ValueError("Укажите имя длиной от 2 до 80 символов.")
        if not 12 <= len(password) <= 128:
            raise ValueError("Пароль должен содержать от 12 до 128 символов.")
        if not consented:
            raise ValueError("Для регистрации требуется согласие на обработку данных.")
        salt = secrets.token_bytes(16)
        digest = password_digest(password, salt)
        user_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO website_users(
                       id,email,name,password_salt,password_hash,password_iterations,
                       consent_at,created_at,updated_at,is_active
                       ) VALUES(?,?,?,?,?,?,?,?,?,1)""",
                    (user_id, email, name, salt, digest, PASSWORD_ITERATIONS,
                     created_at, created_at, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Аккаунт с такой электронной почтой уже существует.") from exc
            token = self._new_session(connection, user_id)
            row = connection.execute("SELECT id,name,email FROM website_users WHERE id=?", (user_id,)).fetchone()
        return self._public_user(row), token

    def login(self, payload: dict[str, Any]) -> tuple[dict[str, str], str]:
        email = normalize_email(payload.get("email"))
        password = str(payload.get("password") or "")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT id,name,email,password_salt,password_hash,password_iterations
                   FROM website_users WHERE email=? AND is_active=1""",
                (email,),
            ).fetchone()
            valid = bool(row and hmac.compare_digest(
                password_digest(password, row["password_salt"], row["password_iterations"]),
                row["password_hash"],
            ))
            if not valid:
                raise PermissionError("Неверная электронная почта или пароль.")
            return self._public_user(row), self._new_session(connection, row["id"])

    def logout(self, token: str) -> None:
        if token:
            with self._lock, self._connect() as connection:
                connection.execute("DELETE FROM website_sessions WHERE token_hash=?", (self._token_hash(token),))

    def list_quotes(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id,input_json,created_at,updated_at FROM website_quotes WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
                (user_id,),
            ).fetchall()
        return [{"id": row["id"], "input": json.loads(row["input_json"]),
                 "createdAt": row["created_at"], "updatedAt": row["updated_at"]} for row in rows]

    def save_quote(self, user_id: str, payload: Any) -> dict[str, Any]:
        clean = clean_quote(payload)
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        quote_id = "quote-" + hashlib.sha256(f"{user_id}|{fingerprint}".encode()).hexdigest()[:24]
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO website_quotes(id,user_id,fingerprint,input_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,fingerprint)
                   DO UPDATE SET input_json=excluded.input_json,updated_at=excluded.updated_at""",
                (quote_id, user_id, fingerprint, encoded, timestamp, timestamp),
            )
        return {"id": quote_id, "input": clean, "updatedAt": timestamp}
