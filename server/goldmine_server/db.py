"""Server database (users, passkeys, invites, client keys) and connections to the market and
per-user game databases.

Per-user reports run on a connection to the shared market DB with the user's game DB attached
as schema "u". Table names don't overlap between the two, so the desktop code's unqualified
queries (transactions, characters, markets, item_states, ...) resolve to the right file.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from . import settings

import goldmine  # noqa: E402  (desktop module, on sys.path via settings)
import market  # noqa: E402

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


def user_conn(user_id: int) -> sqlite3.Connection:
    """Market DB with the user's game DB attached: what reports and imports run on."""
    path = user_db_path(user_id)
    goldmine.connect(str(path)).close()  # create/migrate the game tables in the user's file
    conn = market_conn()
    conn.execute("ATTACH DATABASE ? AS u", (str(path),))
    return conn


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def now() -> int:
    return int(time.time())
