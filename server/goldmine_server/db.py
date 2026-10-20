"""Server database (users, passkeys, invites, client keys) and connections to the market and
per-user game databases.

Per-user reports run on a connection to the shared market DB with the user's game DB attached
as schema "u". Table names don't overlap between the two, so the ledger and pricing code's unqualified
queries (transactions, characters, markets, item_states, ...) resolve to the right file.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from . import ledger, market, settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    handle    BLOB NOT NULL UNIQUE,      -- WebAuthn user handle (random)
    is_admin  INTEGER NOT NULL DEFAULT 0,
    created   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    id          BLOB PRIMARY KEY,        -- WebAuthn credential id
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    public_key  BLOB NOT NULL,
    sign_count  INTEGER NOT NULL,
    transports  TEXT,
    name        TEXT,
    created     INTEGER NOT NULL,
    last_used   INTEGER
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash  TEXT PRIMARY KEY,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    note        TEXT,
    created     INTEGER NOT NULL,
    expires     INTEGER NOT NULL,
    used_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used        INTEGER
);
CREATE TABLE IF NOT EXISTS client_keys (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    prefix     TEXT NOT NULL,             -- first characters, to recognize a key in the list
    created    INTEGER NOT NULL,
    last_used  INTEGER,
    last_ip    TEXT,
    revoked    INTEGER
);
-- OAuth clients (Claude connectors and other MCP clients), created on the Clients page.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id      TEXT PRIMARY KEY,
    secret_hash    TEXT NOT NULL,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    redirect_uris  TEXT NOT NULL,        -- JSON list; loopback URIs match on any port
    created        INTEGER NOT NULL,
    last_used      INTEGER,
    revoked        INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash       TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    redirect_uri    TEXT NOT NULL,
    code_challenge  TEXT NOT NULL,
    scope           TEXT NOT NULL,
    resource        TEXT,
    expires         INTEGER NOT NULL,
    used            INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- access | refresh
    client_id   TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,
    created     INTEGER NOT NULL,
    expires     INTEGER NOT NULL,
    revoked     INTEGER
);
"""


_schema_ready = False


@contextmanager
def server_conn() -> Iterator[sqlite3.Connection]:
    """A short-lived connection: commits on success, rolls back on error, always closes."""
    global _schema_ready
    settings.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.SERVER_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if not _schema_ready:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            _schema_ready = True
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def market_conn() -> sqlite3.Connection:
    settings.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.MARKET_DB, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    market.ensure_schema(conn)
    return conn


def user_db_path(user_id: int):
    d = settings.USERS_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / "game.sqlite"


_migrated: set[str] = set()


def user_conn(user_id: int) -> sqlite3.Connection:
    """Market DB with the user's game DB attached: what reports and imports run on."""
    path = user_db_path(user_id)
    if str(path) not in _migrated or not path.exists():
        # Create/migrate the game tables in the user's file, once per process: the schema script
        # takes a write lock, which would make every read wait behind a running import.
        ledger.connect(str(path)).close()
        _migrated.add(str(path))
    conn = market_conn()
    conn.execute("ATTACH DATABASE ? AS u", (str(path),))
    return conn


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def now() -> int:
    return int(time.time())
