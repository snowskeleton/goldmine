"""The trader's journal: hypotheses, decisions, lessons and notes in the user's game DB.

Each entry can name a strategy and items, carry a prediction (JSON object, e.g. {"realized":
2500000, "days": 3}), link evidence and expire. Evidence refs are strings: task:<plan>:<task>,
plan:<plan>, tx:<transaction id>, bt:<backtest id>, journal:<entry id>. Writing an entry with
`supersedes` marks the older one superseded_by the new one; reads hide superseded and expired
entries unless asked.

The table lives in the user's DB (schema "u" when attached to the market DB, as on server
connections), created on first write; reads before then return nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

KINDS = ("hypothesis", "decision", "lesson", "note")
EVIDENCE = re.compile(r"^(task|plan|tx|bt|journal):\S+$")
MAX_LIMIT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS {s}journal (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,          -- hypothesis | decision | lesson | note
    strategy       TEXT,
    items          TEXT,                   -- JSON list of item ids
    text           TEXT NOT NULL,
    prediction     TEXT,                   -- JSON object
    evidence       TEXT,                   -- JSON list of refs (task:, plan:, tx:, bt:, journal:)
    expires_at     INTEGER,
    created        INTEGER NOT NULL,
    superseded_by  INTEGER REFERENCES journal(id)
);
CREATE INDEX IF NOT EXISTS {s}journal_created ON journal(created);
"""


def _schema(conn) -> str:
    return "u." if any(r[1] == "u" for r in conn.execute("PRAGMA database_list")) else ""


def ensure(conn) -> str:
    s = _schema(conn)
    conn.executescript(SCHEMA.format(s=s))
    return s


def _row(r) -> dict:
    id_, kind, strategy, items, text, prediction, evidence, expires_at, created, superseded_by = r
    return {"id": id_, "kind": kind, "strategy": strategy, "items": json.loads(items) if items else [],
            "text": text, "prediction": json.loads(prediction) if prediction else None,
            "evidence": json.loads(evidence) if evidence else [], "expires_at": expires_at,
            "created": created, "superseded_by": superseded_by}


COLS = "id, kind, strategy, items, text, prediction, evidence, expires_at, created, superseded_by"


def write(conn, kind: str, text: str, strategy: str | None = None, items=None, prediction=None, evidence=None,
          expires_at: int | None = None, supersedes: int | None = None, now: int | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (text or "").strip():
        raise ValueError("text is required")
    items = [int(i) for i in (items or [])]
    if prediction is not None and not isinstance(prediction, dict):
        raise ValueError("prediction must be an object")
    evidence = list(evidence or [])
    bad = [e for e in evidence if not isinstance(e, str) or not EVIDENCE.match(e)]
    if bad:
        raise ValueError(f"evidence refs must look like task:<plan>:<task>, plan:, tx:, bt: or journal:; got {bad}")
    s = ensure(conn)
    if supersedes is not None:
        old = conn.execute(f"SELECT superseded_by FROM {s}journal WHERE id = ?", (int(supersedes),)).fetchone()
        if old is None:
            raise ValueError(f"no journal entry {supersedes}")
        if old[0] is not None:
            raise ValueError(f"journal entry {supersedes} is already superseded by {old[0]}")
    now = now or int(time.time())
    new_id = conn.execute(
        f"INSERT INTO {s}journal (kind, strategy, items, text, prediction, evidence, expires_at, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, strategy, json.dumps(items) if items else None, text.strip(),
         json.dumps(prediction) if prediction is not None else None,
         json.dumps(evidence) if evidence else None, expires_at, now)).lastrowid
    if supersedes is not None:
        conn.execute(f"UPDATE {s}journal SET superseded_by = ? WHERE id = ?", (new_id, int(supersedes)))
    conn.commit()
    return {"entry": _row(conn.execute(f"SELECT {COLS} FROM {s}journal WHERE id = ?", (new_id,)).fetchone()),
            "superseded": supersedes}


def read(conn, kind: str | None = None, strategy: str | None = None, item: int | None = None,
         since: int | None = None, ids=None, include_expired: bool = False, include_superseded: bool = False,
         limit: int = 50, offset: int = 0, now: int | None = None) -> dict:
    """Newest first. Expired = expires_at in the past."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    limit, offset = max(1, min(int(limit or 50), MAX_LIMIT)), max(0, int(offset or 0))
    where, params = ["1=1"], []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if strategy:
        where.append("strategy = ?")
        params.append(strategy)
    if item is not None:
        where.append("EXISTS (SELECT 1 FROM json_each(journal.items) WHERE value = ?)")
        params.append(int(item))
    if since is not None:
        where.append("created >= ?")
        params.append(int(since))
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params += [int(i) for i in ids]
    if not include_expired:
        where.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now or int(time.time()))
    if not include_superseded:
        where.append("superseded_by IS NULL")
    sql = " WHERE " + " AND ".join(where)
    try:
        total = conn.execute("SELECT COUNT(*) FROM journal" + sql, params).fetchone()[0]
        rows = conn.execute(f"SELECT {COLS} FROM journal{sql} ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
                            (*params, limit, offset)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {"total": 0, "offset": offset, "next_offset": None, "entries": []}
        raise
    nxt = offset + len(rows)
    return {"total": total, "offset": offset, "next_offset": nxt if nxt < total else None,
            "entries": [_row(r) for r in rows]}
