"""Per-user operations: game-data import, addon data, reports, CSV export, status."""

from __future__ import annotations

import argparse
import gzip
import io
import os
import sys
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException

from . import db, ledger, pricing, reports, settings, svparse
from .collector import load_config

class _ThreadStdout:
    """sys.stdout stand-in that sends a thread's prints to its own buffer while it's capturing.
    ledger.push prints; redirect_stdout swaps stdout for the whole process, which forced builds
    (and every report waiting on them) to run one at a time. This captures per request thread."""

    def __init__(self, fallback):
        self.fallback = fallback
        self.local = threading.local()

    def _target(self):
        buf = getattr(self.local, "buf", None)
        return self.fallback if buf is None else buf

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._target(), name)


_install_lock = threading.Lock()
_addon_locks: dict[int, threading.Lock] = {}


@contextmanager
def capture_stdout(buf):
    """Send this thread's prints to buf; other threads (and other requests) are unaffected."""
    with _install_lock:
        if not isinstance(sys.stdout, _ThreadStdout):
            sys.stdout = _ThreadStdout(sys.stdout)
        proxy = sys.stdout
    proxy.local.buf = buf
    try:
        yield buf
    finally:
        proxy.local.buf = None


def cli_args(conn, **kw) -> argparse.Namespace:
    base = dict(conn=conn, config=str(settings.CONFIG), db=":memory:", hours=72, limit=40, max_ratio=0.7,
                min_sold=1.0, json=False, market=None, char=None, days=None, item=None, addon_data=None)
    base.update(kw)
    return argparse.Namespace(**base)


# Finished reports, reused until new AH data or game data arrives (or REPORT_TTL passes, since some
# windows are relative to now), so flipping between report tabs doesn't recompute each one.
REPORT_TTL = 600
_report_cache: dict = {}
_report_guard = threading.Lock()


def data_version(user_id: int) -> tuple:
    """Changes whenever the market DB gets a snapshot or the user's game DB is written."""
    path = db.user_db_path(user_id)
    stamps = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (path, f"{path}-wal"))
    conn = db.market_conn()
    try:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM snapshots").fetchone()[0], stamps
    finally:
        conn.close()


def report_data(user_id: int, kind: str, **kw) -> dict:
    """A report as structured data (see reports.py). Shared between requests: don't mutate it."""
    if kind not in reports.DATA:
        raise HTTPException(404, f"unknown report {kind!r}")
    if kind in ("price", "timing") and not kw.get("item"):
        raise HTTPException(400, "item is required")
    key = (user_id, kind, tuple(sorted(kw.items())))
    version = data_version(user_id)
    with _report_guard:
        hit = _report_cache.get(key)
    if hit and hit[0] == version and time.time() - hit[1] < REPORT_TTL:
        return hit[2]
    conn = db.user_conn(user_id)
    try:
        data = reports.DATA[kind](cli_args(conn, **kw))
    finally:
        conn.close()
    with _report_guard:
        for k in [k for k, v in _report_cache.items() if time.time() - v[1] >= REPORT_TTL]:
            del _report_cache[k]
        _report_cache[key] = (version, time.time(), data)
    return data


def report(user_id: int, kind: str, **kw) -> str:
    """A report as plain text, as the desktop client prints it."""
    return reports.text(kind, report_data(user_id, kind, **kw), cli_args(None, **kw))


def export_csv(user_id: int, **filters) -> str:
    conn = db.user_conn(user_id)
    try:
        buf = io.StringIO()
        ledger.write_csv(conn, buf, **filters)
        return buf.getvalue()
    finally:
        conn.close()


def import_saved_variables(user_id: int, body: bytes, encoding: str | None, source: str) -> dict:
    if encoding == "gzip":
        body = gzip.decompress(body)
    if len(body) > settings.MAX_UPLOAD:
        raise HTTPException(413, "upload too large")
    try:
        data = svparse.parse(body).get("GoldmineDB")
    except svparse.ParseError as exc:
        raise HTTPException(400, f"couldn't parse SavedVariables: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "no GoldmineDB table in upload")
    conn = db.user_conn(user_id)
    try:
        new, total = ledger.import_data(conn, data, source[:200])
    finally:
        conn.close()
    return {"new": new, "total": total}


def addon_data(user_id: int) -> bytes:
    """Data.lua for this user (their realms' price sets), cached until new AH data or game data."""
    user_dir = settings.USERS_DIR / str(user_id)
    out = user_dir / "Data.lua"
    stamp_file = user_dir / "Data.lua.stamp"
    with _install_lock:
        lock = _addon_locks.setdefault(user_id, threading.Lock())
    conn = db.user_conn(user_id)
    try:
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM snapshots").fetchone()[0]
        stamp = f"{latest}:{os.path.getmtime(db.user_db_path(user_id))}"
        if out.exists() and stamp_file.exists() and stamp_file.read_text() == stamp:
            return out.read_bytes()
        with lock:  # one build per user at a time; whoever waited gets the fresh file
            if out.exists() and stamp_file.exists() and stamp_file.read_text() == stamp:
                return out.read_bytes()
            args = cli_args(conn, addon_data=str(out))
            with capture_stdout(io.StringIO()):
                ledger.push(conn, args, load_config())
            stamp_file.write_text(stamp)
            return out.read_bytes()
    finally:
        conn.close()


def timing_json(user_id: int, item: str) -> dict:
    conn = db.user_conn(user_id)
    try:
        ids = ledger.resolve_items(conn, item)
        if not ids:
            raise HTTPException(404, "no matching item")
        item_id = ids[0]
        games = dict(conn.execute("SELECT name, game FROM markets"))
        profiles = pricing.timing(conn, item_id)
        name = ledger.item_names(conn, [item_id], cli_args(conn))[item_id]
        # Buckets stay in UTC (offset 0); clients rotate them into their own local time.
        return {"item_id": item_id, "name": name, "utc_offset_hours": 0,
                "markets": [{"market": m, "game": games.get(m), "buckets": b} for m, b in profiles.items()]}
    finally:
        conn.close()


def status(user_id: int) -> dict:
    """Same shape as `goldmine.py status`, so the desktop app can show either."""
    conn = db.user_conn(user_id)
    try:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
        size = sum(p.stat().st_size for p in settings.DATA.glob("market.sqlite*"))
        return {
            "db_path": "server",
            "db_bytes": size,
            "transactions": q("SELECT COUNT(*) FROM transactions")[0][0],
            "last_transaction": q("SELECT MAX(ts) FROM transactions")[0][0],
            "scans": q("SELECT COUNT(*) FROM scans")[0][0],
            "recipes": q("SELECT COUNT(*) FROM recipes")[0][0],
            "characters": [dict(zip(("key", "balance", "last_seen", "region"), r)) for r in
                           q("SELECT key, balance, last_seen, region FROM characters ORDER BY balance DESC")],
            "markets": market_status(conn),
            "saved_variables": [],
        }
    finally:
        conn.close()


def market_status(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT m.name, COUNT(s.id), MAX(s.last_modified), MAX(s.fetched_at),
               (SELECT COUNT(*) FROM item_current c WHERE c.market_id = m.mid), m.game,
               (SELECT COUNT(*) FROM auctions a WHERE a.market_id = m.mid AND a.ended_snap IS NULL), m.source
        FROM markets m LEFT JOIN snapshots s ON s.market = m.name
        GROUP BY m.name ORDER BY m.name""").fetchall()
    return [dict(zip(("name", "snapshots", "last_modified", "last_fetched", "items", "game", "open_auctions",
                      "source"), r))
            for r in rows]


def storage_breakdown() -> list[tuple[str, int]]:
    """Bytes per table in the market DB (needs SQLite's dbstat, present in standard builds)."""
    conn = db.market_conn()
    try:
        return conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
    except Exception:
        return []
    finally:
        conn.close()
