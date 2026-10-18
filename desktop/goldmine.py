#!/usr/bin/env python3
"""Goldmine desktop tool: import the Goldmine addon's SavedVariables into SQLite and export CSV.

  goldmine.py sync                      # import from every Goldmine.lua found under the WoW folder
  goldmine.py watch                     # keep syncing whenever WoW writes the file (logout / /reload)
  goldmine.py export out.csv            # dump transactions to CSV (filters: --char, --since, --source)
  goldmine.py summary --days 7          # income/expense by source
  goldmine.py collect                   # fetch AH snapshots for markets in ~/.goldmine/config.toml
  goldmine.py price "Linen Cloth"       # latest + recent prices for an item
  goldmine.py realms us dynamic-us      # list connected realms (slugs for the config)
  goldmine.py probe us                  # which API namespaces exist (find Forever's)
  goldmine.py crafts                    # profit per craft for every recipe your characters know
  goldmine.py deals                     # items listed well below market value that sell
  goldmine.py push                      # write prices/crafts/deals into the addon (Data.lua)
  goldmine.py timing "Linen Cloth"      # when an item sells: hour-of-week heatmap

The WoW client only writes SavedVariables on logout, /reload, or exit, so that's when new
data appears. Imports are idempotent (keyed on entry id), so syncing repeatedly is safe,
and pruning old entries in-game (/goldmine prune) after a sync loses nothing.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market  # noqa: E402
import pricing  # noqa: E402
import remote  # noqa: E402
import svparse  # noqa: E402

DEFAULT_WOW_DIRS = [
    "/Applications/World of Warcraft",
    os.path.expanduser("~/Applications/World of Warcraft"),
    "C:/Program Files (x86)/World of Warcraft",
    "C:/Program Files/World of Warcraft",
]
DEFAULT_DB = os.environ.get("GOLDMINE_DB", os.path.expanduser("~/.goldmine/goldmine.sqlite"))
DEFAULT_ADDON_DATA = Path(__file__).resolve().parent.parent / "addon" / "Goldmine" / "Data.lua"
DEFAULT_CONFIG = os.environ.get("GOLDMINE_CONFIG", os.path.expanduser("~/.goldmine/config.toml"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    key          TEXT PRIMARY KEY,   -- Name-Realm
    name         TEXT,
    realm        TEXT,
    class        TEXT,
    faction      TEXT,
    region       TEXT,               -- e.g. US; with realm type, identifies the shared AH
    balance      INTEGER,            -- copper, as of last sync
    last_seen    INTEGER,
    source_file  TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id        TEXT PRIMARY KEY,      -- Name-Realm:seq (stable across syncs)
    char_key  TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ts        INTEGER NOT NULL,      -- unix seconds
    delta     INTEGER NOT NULL,      -- copper, + income / - expense
    balance   INTEGER NOT NULL,      -- copper after this change
    source    TEXT NOT NULL,
    zone      TEXT,
    subzone   TEXT,
    map_id    INTEGER,
    session   TEXT,
    item_id   INTEGER,               -- parsed from meta.item when present
    item_name TEXT,
    meta      TEXT                   -- JSON object of extra details
);
CREATE INDEX IF NOT EXISTS tx_char_ts ON transactions(char_key, ts);
CREATE INDEX IF NOT EXISTS tx_source ON transactions(source);
-- Prices seen in game (AH searches you ran). One row per price level.
CREATE TABLE IF NOT EXISTS scans (
    id        TEXT PRIMARY KEY,      -- Name-Realm:sN
    char_key  TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    kind      TEXT NOT NULL          -- commodity | item | browse
);
CREATE TABLE IF NOT EXISTS scan_rows (
    scan_id     TEXT NOT NULL,
    item_id     INTEGER NOT NULL,
    variant     INTEGER NOT NULL DEFAULT 0,
    unit_price  INTEGER NOT NULL,     -- copper; for browse rows, the minimum price
    quantity    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scan_rows_item ON scan_rows(item_id, variant);
CREATE TABLE IF NOT EXISTS recipes (
    char_key     TEXT NOT NULL,
    recipe_id    INTEGER NOT NULL,
    profession   TEXT,
    name         TEXT,
    output_item  INTEGER,
    output_qty   REAL,
    PRIMARY KEY (char_key, recipe_id)
);
CREATE TABLE IF NOT EXISTS recipe_reagents (
    char_key   TEXT NOT NULL,
    recipe_id  INTEGER NOT NULL,
    item_id    INTEGER NOT NULL,
    quantity   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory (
    owner     TEXT NOT NULL,          -- Name-Realm, or "warband"
    location  TEXT NOT NULL,          -- bags | bank | warband
    item_id   INTEGER NOT NULL,
    count     INTEGER NOT NULL,
    updated   INTEGER,
    PRIMARY KEY (owner, location, item_id)
);
DROP VIEW IF EXISTS transactions_view;
CREATE VIEW transactions_view AS
SELECT id, char_key AS character,
       datetime(ts, 'unixepoch', 'localtime') AS time,
       delta / 10000.0 AS gold, balance / 10000.0 AS balance_gold,
       source, item_name, zone, subzone, meta
FROM transactions ORDER BY char_key, seq;
"""

CSV_COLUMNS = ["id", "character", "time", "unix_ts", "delta_copper", "delta_gold",
               "balance_copper", "balance_gold", "source", "direction", "zone", "subzone",
               "item_id", "item_name", "map_id", "session", "meta"]

# Columns added after the first release, applied to existing databases on connect.
MIGRATIONS = {
    "characters": [("region", "TEXT")],
    "transactions": [("item_id", "INTEGER"), ("item_name", "TEXT")],
}

_ITEM_ID = re.compile(r"\|Hitem:(\d+)")
_ITEM_NAME = re.compile(r"\|h\[(.*?)\]\|h")


def parse_item(value) -> tuple[int | None, str | None]:
    """Split an in-game item link (or a plain item name) into (item_id, name)."""
    if not isinstance(value, str) or not value:
        return None, None
    m_id, m_name = _ITEM_ID.search(value), _ITEM_NAME.search(value)
    if m_id or m_name:
        return (int(m_id.group(1)) if m_id else None), (m_name.group(1) if m_name else None)
    return None, value


def find_saved_variables(wow_dirs: list[str]) -> list[Path]:
    found = []
    for base in wow_dirs:
        # <WoW>/<_flavor_>/WTF/Account/<ACCOUNT>/SavedVariables/Goldmine.lua — any flavor folder.
        found += glob.glob(os.path.join(base, "*", "WTF", "Account", "*", "SavedVariables", "Goldmine.lua"))
    return sorted({Path(p) for p in found})


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(os.path.expanduser(db_path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the collector, and vice versa
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if existing and name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.executescript(SCHEMA)
    return conn


def db_for(args) -> sqlite3.Connection:
    """The connection a command should use: one handed in by the server (args.conn), else the
    local database."""
    return getattr(args, "conn", None) or connect(args.db)


def import_file(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Import one SavedVariables file. Returns (new_rows, total_rows_in_file)."""
    data = svparse.parse_file(path).get("GoldmineDB")
    if not isinstance(data, dict):
        raise ValueError(f"{path}: no GoldmineDB table found")
    return import_data(conn, data, str(path))


def import_data(conn: sqlite3.Connection, data: dict, source: str) -> tuple[int, int]:
    """Import a parsed GoldmineDB table. Returns (new_rows, total_rows)."""

    chars = data.get("chars") or {}
    if isinstance(chars, list):  # only possible if keys were 1..n, which ours never are
        chars = {}
    for key, c in chars.items():
        conn.execute(
            """INSERT INTO characters (key, name, realm, class, faction, region, balance, last_seen, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 name=excluded.name, realm=excluded.realm, class=excluded.class,
                 faction=excluded.faction, source_file=excluded.source_file,
                 region=COALESCE(excluded.region, characters.region),
                 balance=CASE WHEN excluded.last_seen >= COALESCE(characters.last_seen, 0)
                              THEN excluded.balance ELSE characters.balance END,
                 last_seen=MAX(COALESCE(characters.last_seen, 0), COALESCE(excluded.last_seen, 0))""",
            (key, c.get("name"), c.get("realm"), c.get("class"), c.get("faction"), c.get("region"),
             c.get("money"), c.get("lastSeen"), source),
        )

    log = data.get("log") or []
    if isinstance(log, dict):  # sparse table (shouldn't happen) — keep whatever is there
        log = [log[k] for k in sorted(log)]
    count = "SELECT COUNT(*) FROM transactions"
    before = conn.execute(count).fetchone()[0]
    for e in log:
        entry_id = e["id"]
        meta = e.get("m")
        item_id, item_name = parse_item(meta.get("item") if isinstance(meta, dict) else None)
        if item_id is None and isinstance(meta, dict) and isinstance(meta.get("item_id"), int):
            item_id = meta["item_id"]
        conn.execute(
            """INSERT INTO transactions
               (id, char_key, seq, ts, delta, balance, source, zone, subzone, map_id, session,
                item_id, item_name, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET  -- backfill columns added in later versions
                 item_id=COALESCE(transactions.item_id, excluded.item_id),
                 item_name=COALESCE(transactions.item_name, excluded.item_name)""",
            (entry_id, e.get("c") or entry_id.rsplit(":", 1)[0], int(entry_id.rsplit(":", 1)[1]),
             e["t"], e["d"], e["b"], e.get("src", "unknown"), e.get("zone"), e.get("sub"),
             e.get("map"), e.get("s"), item_id, item_name, json.dumps(meta, ensure_ascii=False) if meta else None),
        )
    import_market_data(conn, data, chars)
    conn.commit()
    return conn.execute(count).fetchone()[0] - before, len(log)


def _pairs(flat, width):
    flat = flat or []
    return [flat[i:i + width] for i in range(0, len(flat) - width + 1, width)]


def _as_dict(value) -> dict:
    # Lua tables with keys 1..n parse as lists; recipe ids and item ids are sparse, but be safe.
    if isinstance(value, list):
        return {i + 1: v for i, v in enumerate(value)}
    return value or {}


def import_market_data(conn: sqlite3.Connection, data: dict, chars: dict) -> None:
    """AH scans, recipes, and inventory recorded by the addon's Market.lua."""
    for scan in data.get("scans") or []:
        cur = conn.execute("INSERT OR IGNORE INTO scans (id, char_key, ts, kind) VALUES (?, ?, ?, ?)",
                           (scan["id"], scan["c"], scan["t"], scan["kind"]))
        if not cur.rowcount:
            # Already imported; a merged scan may have grown since, so refresh its rows.
            conn.execute("UPDATE scans SET ts = ? WHERE id = ?", (scan["t"], scan["id"]))
            conn.execute("DELETE FROM scan_rows WHERE scan_id = ?", (scan["id"],))
        if scan["kind"] == "browse":
            rows = [(scan["id"], i, v, p, q) for i, v, p, q in _pairs(scan.get("rows"), 4)]
        else:
            rows = [(scan["id"], scan["item"], scan.get("v") or 0, p, q) for p, q in _pairs(scan.get("rows"), 2)]
        conn.executemany("INSERT INTO scan_rows VALUES (?, ?, ?, ?, ?)", rows)

    for key, c in chars.items():
        for prof, pdata in _as_dict(c.get("professions")).items():
            recipes = _as_dict(pdata.get("recipes"))
            conn.execute("""DELETE FROM recipe_reagents WHERE char_key = ? AND recipe_id IN
                            (SELECT recipe_id FROM recipes WHERE char_key = ? AND profession = ?)""", (key, key, prof))
            conn.execute("DELETE FROM recipes WHERE char_key = ? AND profession = ?", (key, prof))
            for rid, r in recipes.items():
                conn.execute("INSERT OR REPLACE INTO recipes VALUES (?, ?, ?, ?, ?, ?)",
                             (key, int(rid), prof, r.get("n"), r.get("o"), r.get("q")))
                conn.executemany("INSERT INTO recipe_reagents VALUES (?, ?, ?, ?)",
                                 [(key, int(rid), item, qty) for item, qty in _pairs(r.get("r"), 2)])
        inv = c.get("inventory") or {}
        for loc in ("bags", "bank"):
            if loc in inv:
                conn.execute("DELETE FROM inventory WHERE owner = ? AND location = ?", (key, loc))
                conn.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?, ?)",
                                 [(key, loc, int(i), n, inv.get(loc + "At")) for i, n in _as_dict(inv[loc]).items()])
    warband = data.get("warband")
    if warband:
        conn.execute("DELETE FROM inventory WHERE owner = 'warband'")
        conn.executemany("INSERT INTO inventory VALUES ('warband', 'warband', ?, ?, ?)",
                         [(int(i), n, warband.get("t")) for i, n in _as_dict(warband.get("items")).items()])


def dump_toml(cfg: dict) -> str:
    """Write the config back as TOML. Supports what Goldmine uses: tables of scalars/lists
    and arrays of tables. Comments are not preserved."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    out = ["# Goldmine config. Managed by Goldmine.app; hand edits are fine (comments aren't kept).", ""]
    is_array = lambda v: isinstance(v, list) and v and all(isinstance(x, dict) for x in v)  # noqa: E731
    for key, value in sorted(cfg.items(), key=lambda kv: (bool(is_array(kv[1])), kv[0])):
        if isinstance(value, dict):
            out.append(f"[{key}]")
            out += [f"{k} = {val(v)}" for k, v in value.items() if v is not None]
            out.append("")
        elif is_array(value):
            for item in value:
                out.append(f"[[{key}]]")
                out += [f"{k} = {val(v)}" for k, v in item.items() if v not in (None, "")]
                out.append("")
        else:
            out.insert(2, f"{key} = {val(value)}")
    return "\n".join(out)


def config_cmd(args) -> int:
    path = Path(args.config).expanduser()
    if args.action == "get":
        cfg = market.load_config(path) if path.exists() else {}
        print(json.dumps(cfg))
        return 0
    cfg = json.load(sys.stdin)
    if not isinstance(cfg, dict):
        raise SystemExit("config must be a JSON object")
    for m in cfg.get("market", []):
        missing = [k for k in ("name", "region", "namespace", "kind") if not m.get(k)]
        if missing or (m["kind"] != "commodities" and not m.get("realm")):
            raise SystemExit(f"market {m.get('name')!r} is missing {missing or ['realm']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(".toml.bak")
        backup.write_bytes(path.read_bytes())
        backup.chmod(0o600)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_toml(cfg))
    tmp.chmod(0o600)
    tmp.replace(path)
    market.load_config(path)  # make sure it round-trips
    print("saved")
    return 0


def status_cmd(args) -> int:
    rem = remote_for(args)
    if rem is not None:
        out = rem.get_json("/api/v1/status")
        out["saved_variables"] = [str(p) for p in find_saved_variables(args.wow_dir or DEFAULT_WOW_DIRS)]
        out["server"] = rem.base
        print(json.dumps(out))
        return 0
    conn = db_for(args)
    market.ensure_schema(conn)
    q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
    out = {
        "db_path": os.path.expanduser(args.db),
        "db_bytes": os.path.getsize(os.path.expanduser(args.db)),
        "transactions": q("SELECT COUNT(*) FROM transactions")[0][0],
        "last_transaction": q("SELECT MAX(ts) FROM transactions")[0][0],
        "scans": q("SELECT COUNT(*) FROM scans")[0][0],
        "recipes": q("SELECT COUNT(*) FROM recipes")[0][0],
        "characters": [dict(zip(("key", "balance", "last_seen", "region"), r)) for r in
                       q("SELECT key, balance, last_seen, region FROM characters ORDER BY balance DESC")],
        "markets": [dict(zip(("name", "snapshots", "last_modified", "last_fetched", "items", "game"), r)) for r in q("""
            SELECT m.name, COUNT(s.id), MAX(s.last_modified), MAX(s.fetched_at),
                   (SELECT COUNT(*) FROM item_current c WHERE c.market_id = m.mid), m.game
            FROM markets m LEFT JOIN snapshots s ON s.market = m.name
            GROUP BY m.name ORDER BY m.name""")],
        "saved_variables": [str(p) for p in find_saved_variables(args.wow_dir or DEFAULT_WOW_DIRS)],
    }
    print(json.dumps(out))
    return 0


def remote_for(args) -> remote.Remote | None:
    """The configured Goldmine server ([server] url in config.toml, or --server), unless --local."""
    if getattr(args, "local", False) or getattr(args, "conn", None) is not None:
        return None
    url = getattr(args, "server", None)
    if not url:
        try:
            url = (market.load_config(getattr(args, "config", DEFAULT_CONFIG)).get("server") or {}).get("url")
        except (OSError, SystemExit):
            url = None
    return remote.Remote(url) if url else None


def remote_sync(args, rem: remote.Remote) -> int:
    paths = [Path(p) for p in args.file] if args.file else find_saved_variables(args.wow_dir)
    if not paths:
        print("No Goldmine.lua SavedVariables found.", file=sys.stderr)
        return 1
    errors = 0
    for p in paths:
        try:
            r = rem.upload_saved_variables(p.read_bytes(), source=str(p))
            print(f"{p}: {r['new']} new of {r['total']} entries (uploaded to {rem.base})")
        except (OSError, remote.RemoteError) as exc:
            print(f"{p}: FAILED: {exc}", file=sys.stderr)
            errors += 1
    if getattr(args, "push", False):
        errors += remote_push(args, rem)
    return 1 if errors else 0


def remote_push(args, rem: remote.Remote) -> int:
    try:
        data = rem.request("GET", "/api/v1/addon-data")
    except remote.RemoteError as exc:
        print(f"addon data: FAILED: {exc}", file=sys.stderr)
        return 1
    Path(args.addon_data).write_bytes(data)
    print(f"wrote {args.addon_data} ({len(data) / 1024:.0f} KB from {rem.base}). /reload in game to load it.")
    return 0


def remote_report(args, kind: str, **params) -> bool:
    """Print a report from the server if one is configured. Returns False to run locally."""
    rem = remote_for(args)
    if rem is None:
        return False
    print(rem.text(f"/api/v1/report/{kind}", params), end="")
    return True


def server_check_cmd(args) -> int:
    rem = remote_for(args)
    if rem is None:
        print("No server configured (set [server] url, or pass --server).")
        return 1
    me = rem.get_json("/api/v1/me")
    print(json.dumps({"ok": True, "server": rem.base, **me}) if args.json
          else f"Connected to {rem.base} as {me['user']} (server {me['server_version']})")
    return 0


def sync(args) -> int:
    rem = remote_for(args)
    if rem is not None:
        return remote_sync(args, rem)
    paths = [Path(p) for p in args.file] if args.file else find_saved_variables(args.wow_dir)
    if not paths:
        print("No Goldmine.lua SavedVariables found. Pass --wow-dir or --file. Searched:",
              *args.wow_dir, sep="\n  ", file=sys.stderr)
        return 1
    conn = connect(args.db)
    for p in paths:
        try:
            new, total = import_file(conn, p)
            print(f"{p}: {new} new of {total} entries")
        except (OSError, ValueError, KeyError) as exc:
            print(f"{p}: FAILED: {exc}", file=sys.stderr)
    if getattr(args, "csv", None):
        n = write_csv(conn, args.csv)
        print(f"wrote {n} rows to {args.csv}")
    if getattr(args, "push", False):
        push(conn, args)
    return 0


def watch(args) -> int:
    paths = [Path(p) for p in args.file] if args.file else find_saved_variables(args.wow_dir)
    if not paths:
        print("Nothing to watch yet; will keep looking...", file=sys.stderr)
    mtimes: dict[Path, float] = {}
    parent = os.getppid()
    print(f"Watching (every {args.interval}s). Ctrl-C to stop.")
    try:
        while True:
            if args.exit_with_parent and os.getppid() != parent:
                return 0  # the app that started us is gone
            if not args.file:
                paths = find_saved_variables(args.wow_dir)
            changed = []
            for p in paths:
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if mtimes.get(p) != m:
                    mtimes[p] = m
                    changed.append(p)
            if changed:
                time.sleep(1)  # let WoW finish writing
                ns = argparse.Namespace(**{**vars(args), "file": [str(p) for p in changed]})
                print(dt.datetime.now().strftime("[%H:%M:%S]"), end=" ")
                sync(ns)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def parse_since(value: str | None) -> int | None:
    if not value:
        return None
    if value[-1] in "dh" and value[:-1].isdigit():
        secs = int(value[:-1]) * (86400 if value[-1] == "d" else 3600)
        return int(time.time()) - secs
    return int(dt.datetime.fromisoformat(value).timestamp())


def query_rows(conn, char=None, since=None, until=None, source=None):
    sql = "SELECT * FROM transactions WHERE 1=1"
    params: list = []
    if char:
        sql += " AND char_key LIKE ?"
        params.append(char if "-" in char else f"{char}-%")
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    if until is not None:
        sql += " AND ts < ?"
        params.append(until)
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY ts, char_key, seq"
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def write_csv(conn, out: str, **filters) -> int:
    rows = query_rows(conn, **filters)
    f = sys.stdout if out == "-" else open(out, "w", newline="", encoding="utf-8")
    try:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([
                r["id"], r["char_key"],
                dt.datetime.fromtimestamp(r["ts"]).isoformat(sep=" "), r["ts"],
                r["delta"], f"{r['delta'] / 10000:.4f}",
                r["balance"], f"{r['balance'] / 10000:.4f}",
                r["source"], "in" if r["delta"] > 0 else "out" if r["delta"] < 0 else "",
                r["zone"], r["subzone"], r["item_id"], r["item_name"], r["map_id"], r["session"], r["meta"] or "",
            ])
    finally:
        if f is not sys.stdout:
            f.close()
    return len(rows)


def export(args) -> int:
    conn = db_for(args)
    n = write_csv(conn, args.out, char=args.char, since=parse_since(args.since),
                  until=parse_since(args.until), source=args.source)
    if args.out != "-":
        print(f"wrote {n} rows to {args.out}")
    return 0


def fmt_gold(copper: int, signed: bool = False) -> str:
    """Like the game's money display, skipping leading zero units: 4s 79c, 1g 00s 05c, 34c."""
    sign = "-" if copper < 0 else "+" if signed and copper > 0 else ""
    c = abs(copper)
    g, s, cu = c // 10000, c // 100 % 100, c % 100
    if g:
        return f"{sign}{g:,}g {s:02d}s {cu:02d}c"
    if s:
        return f"{sign}{s}s {cu:02d}c"
    return f"{sign}{cu}c"


def summary(args) -> int:
    if remote_report(args, "summary", days=args.days):
        return 0
    conn = db_for(args)
    since = int(time.time()) - args.days * 86400 if args.days else None
    rows = query_rows(conn, char=args.char, since=since)
    if not rows:
        print("No transactions in range.")
        return 0
    by_src: dict[str, list[int]] = {}
    for r in rows:
        s = by_src.setdefault(r["source"], [0, 0, 0])
        if r["delta"] > 0:
            s[0] += r["delta"]
        else:
            s[1] += r["delta"]
        s[2] += 1 if r["delta"] else 0
    span = f"last {args.days} days" if args.days else "all time"
    print(f"{len(rows)} entries, {span}" + (f", {args.char}" if args.char else ""))
    print(f"{'source':<16}{'in':>18}{'out':>18}{'net':>18}{'count':>7}")
    tin = tout = 0
    for src, (i, o, n) in sorted(by_src.items(), key=lambda kv: -abs(kv[1][0] + kv[1][1])):
        tin, tout = tin + i, tout + o
        print(f"{src:<16}{fmt_gold(i):>18}{fmt_gold(o):>18}{fmt_gold(i + o, True):>18}{n:>7}")
    print(f"{'TOTAL':<16}{fmt_gold(tin):>18}{fmt_gold(tout):>18}{fmt_gold(tin + tout, True):>18}")
    print("\nBalances:")
    for key, bal, seen in conn.execute("SELECT key, balance, last_seen FROM characters ORDER BY balance DESC"):
        when = dt.datetime.fromtimestamp(seen).strftime("%Y-%m-%d %H:%M") if seen else "?"
        print(f"  {key:<28}{fmt_gold(bal or 0):>18}   (as of {when})")
    return 0


def api_client(args) -> tuple[market.Client, dict]:
    cfg = market.load_config(args.config)
    api = cfg.get("api", {})
    if not api.get("client_id") or not api.get("client_secret"):
        raise SystemExit(f"Set api.client_id and api.client_secret in {args.config}")
    cache = Path(args.config).expanduser().parent / "cache"
    return market.Client(api["client_id"], api["client_secret"], cache_dir=cache), cfg


def collect_cmd(args) -> int:
    if remote_for(args) is not None:
        print("A Goldmine server is configured; it collects prices itself. (Use --local to collect here.)")
        return 0
    client, cfg = api_client(args)
    conn = connect(args.db)
    stamp = dt.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    errors = market.collect(conn, client, cfg, log=lambda msg: print(stamp, msg, flush=True))
    # Names for items you've actually traded, so reports are readable.
    market.ensure_schema(conn)
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT item_id FROM transactions WHERE item_id IS NOT NULL "
        "AND item_id NOT IN (SELECT item_id FROM items)")]
    first = (cfg.get("market") or [{}])[0]
    if ids and first.get("region"):
        market.fetch_item_names(conn, client, first["region"], first["namespace"], ids)
    if args.push:
        push(conn, args, cfg, client)
    return 1 if errors else 0


def item_names(conn, ids, args=None, cfg=None, client=None) -> dict[int, str]:
    """Names from the items cache, fetching missing ones from the API when credentials exist."""
    market.ensure_schema(conn)
    ids = set(ids)
    known = dict(conn.execute("SELECT item_id, name FROM items"))
    known.update((i, n) for i, n in conn.execute(
        "SELECT item_id, item_name FROM transactions WHERE item_id IS NOT NULL AND item_name IS NOT NULL")
        if i not in known)
    missing = [i for i in ids if i not in known]
    if missing and args is not None:
        try:
            if client is None:
                client, cfg = api_client(args)
            first = (cfg.get("market") or [{}])[0]
            if first.get("region"):
                market.fetch_item_names(conn, client, first["region"], first["namespace"], missing[:200],
                                        log=lambda m: None)
                known.update(conn.execute("SELECT item_id, name FROM items"))
        except (SystemExit, market.ApiError, OSError):
            pass
    return {i: known.get(i) or f"item {i}" for i in ids}


def load_cfg(args) -> dict:
    try:
        return market.load_config(args.config)
    except (OSError, SystemExit):
        return {}


def set_reports(conn, args, cfg=None) -> list[pricing.SetExport]:
    """Prices, crafts and deals for each market set (a realm's AH, or a game's commodities).
    Realms where you have no characters are skipped unless none have characters yet."""
    market.ensure_schema(conn)
    cfg = load_cfg(args) if cfg is None else cfg
    sets = pricing.build_sets(conn, cfg)
    if any(st.chars for st in sets):
        sets = [st for st in sets if st.chars]
    hours = getattr(args, "hours", 72)
    max_ratio, min_sold = getattr(args, "max_ratio", 0.7), getattr(args, "min_sold", 1.0)
    exports = []
    for st in sets:
        if st.kind == "commodities":
            prices = pricing.price_table(conn, hours, st.markets, scan_chars=st.chars or None)
            exports.append(pricing.SetExport(st, prices, [], pricing.deal_report(prices, max_ratio, min_sold)))
            continue
        cs = pricing.commodity_set_for(sets, st)
        realm_prices = pricing.price_table(conn, hours, st.markets, scan_chars=st.chars or None)
        merged = pricing.price_table(conn, hours, st.markets + (cs.markets if cs else []),
                                     scan_chars=st.chars or None)
        exports.append(pricing.SetExport(st, realm_prices, pricing.craft_report(conn, merged, st.chars),
                                         pricing.deal_report(realm_prices, max_ratio, min_sold)))
    return exports


def set_title(st: pricing.MarketSet) -> str:
    who = ", ".join(k.split("-")[0] for k in st.chars) or "no characters"
    where = " / ".join(st.realms) if st.kind == "realm" else f"{st.region.upper()} commodities"
    return f"== {st.game} · {where} ({who})"


def crafts_cmd(args) -> int:
    if remote_report(args, "crafts", limit=args.limit):
        return 0
    conn = db_for(args)
    exports = [ex for ex in set_reports(conn, args) if ex.crafts]
    if not exports:
        print("No recipes yet. Open each profession window in game, then /reload and sync.")
        return 0
    for ex in exports:
        crafts = ex.crafts
        names = item_names(conn, {c.output_item for c in crafts} | {m for c in crafts for m in c.missing}, args)
        print(set_title(ex.set))
        print(f"{'recipe':<34}{'cost':>15}{'sells for':>15}{'profit':>16}{'sold/day':>10}  crafters")
        for c in crafts[:args.limit]:
            f = lambda v, signed=False: fmt_gold(v, signed) if v is not None else "?"  # noqa: E731
            line = f"{(c.name or names[c.output_item])[:33]:<34}{f(c.cost):>15}{f(c.revenue):>15}{f(c.profit, True):>16}"
            line += f"{c.sold_per_day:>10.1f}  {', '.join(k.split('-')[0] for k in c.crafters)}"
            if c.missing:
                line += f"   (no price: {', '.join(names[m] for m in c.missing[:3])})"
            print(line)
        print()
    print("sells for = min(market value, current lowest) after the 5% AH cut; cost uses current p10 prices.")
    return 0


def deals_cmd(args) -> int:
    if remote_report(args, "deals", limit=args.limit):
        return 0
    conn = db_for(args)
    exports = [ex for ex in set_reports(conn, args) if ex.deals]
    if not exports:
        print("No deals right now (or not enough snapshots yet; sale rates need a few hours of data).")
        return 0
    for ex in exports:
        deals = ex.deals[:args.limit]
        names = item_names(conn, {d.item_id for d in deals}, args)
        print(set_title(ex.set))
        print(f"{'item':<34}{'listed at':>15}{'market':>15}{'off':>6}{'profit/unit':>16}{'sold/day':>10}")
        for d in deals:
            print(f"{names[d.item_id][:33]:<34}{fmt_gold(d.current_min):>15}{fmt_gold(d.market_value):>15}"
                  f"{d.discount:>6.0%}{fmt_gold(d.profit_per_unit, True):>16}{d.sold_per_day:>10.1f}")
        print()
    return 0


def push(conn, args, cfg=None, client=None) -> None:
    exports = set_reports(conn, args, cfg)
    n = pricing.write_addon_data(args.addon_data, exports)
    size = os.path.getsize(args.addon_data) / 1024
    print(f"wrote {args.addon_data} ({len(exports)} market sets, {n} item prices, {size:.0f} KB). "
          "/reload in game to load it.")


def push_cmd(args) -> int:
    rem = remote_for(args)
    if rem is not None:
        return remote_push(args, rem)
    push(connect(args.db), args)
    return 0


def resolve_items(conn, query: str) -> list[int]:
    market.ensure_schema(conn)
    if query.isdigit():
        return [int(query)]
    ids = [r[0] for r in conn.execute("SELECT item_id FROM items WHERE name LIKE ?", (f"%{query}%",))]
    ids += [r[0] for r in conn.execute(
        "SELECT DISTINCT item_id FROM transactions WHERE item_name LIKE ? AND item_id IS NOT NULL",
        (f"%{query}%",)) if r[0] not in ids]
    return ids


DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
SHADES = " .:-=+*#%@"


def timing_cmd(args) -> int:
    offset = time.localtime().tm_gmtoff // 3600
    rem = remote_for(args)
    if rem is not None:
        data = rem.get_json("/api/v1/timing", {"item": args.item})  # UTC buckets
        item_id, name = data["item_id"], data["name"]
        profiles = {m["market"]: m["buckets"] for m in data["markets"]
                    if not args.market or m["market"] in args.market}
        games = {m["market"]: m.get("game") for m in data["markets"]}
    else:
        conn = db_for(args)
        ids = resolve_items(conn, args.item)
        if not ids:
            print("No matching item (pass the item id if its name isn't known yet).")
            return 1
        item_id = ids[0]
        name = item_names(conn, [item_id], args)[item_id]
        games = dict(conn.execute("SELECT name, game FROM markets"))
        profiles = pricing.timing(conn, item_id, markets=args.market or None)
    if args.json:
        print(json.dumps({"item_id": item_id, "name": name, "utc_offset_hours": offset,
                          "markets": [{"market": m, "game": games.get(m), "buckets": pricing.to_local(b, offset)}
                                      for m, b in profiles.items()]}))
        return 0
    print(f"{name} ({item_id}) - local time (UTC{offset:+d})")
    if not profiles:
        print("  No sales recorded yet for this item. Timing needs a few days of hourly snapshots.")
        return 0
    for mkt, buckets in profiles.items():
        local = pricing.to_local(buckets, offset)
        peak = max((b["sold_avg"] or 0) for b in local) or 1
        print(f"\n{mkt} ({games.get(mkt)}): average units sold per hour")
        print("      " + "".join(f"{h:<3}" for h in range(0, 24, 3)).ljust(24) + "  best hours")
        for d in range(7):
            row = local[d * 24:(d + 1) * 24]
            cells = "".join(SHADES[min(int((b["sold_avg"] or 0) / peak * (len(SHADES) - 1) + 0.5), len(SHADES) - 1)]
                            if b["samples"] else " " for b in row)
            best = sorted(range(24), key=lambda h: -(row[h]["sold_avg"] or 0))[:3]
            best_txt = ", ".join(f"{h:02d}h ({row[h]['sold_avg']:.1f})" for h in best if row[h]["sold_avg"])
            day_total = sum(b["sold_avg"] or 0 for b in row)
            print(f"  {DAYS[d]} {cells}  {best_txt}  [day ~{day_total:.0f}]")
        cheapest = min((b for b in local if b["p10_avg"]), key=lambda b: b["p10_avg"], default=None)
        if cheapest:
            i = local.index(cheapest)
            print(f"  cheapest slot: {DAYS[i // 24]} {i % 24:02d}h (avg p10 {fmt_gold(cheapest['p10_avg'])})")
    return 0


def list_realms(client, region: str, namespace: str) -> list[dict]:
    """Every realm in a namespace: connected_realm_id, slug, name, type, category."""
    page, rows = 1, []
    while True:
        data, _ = client.get(region, namespace, "/data/wow/search/connected-realm",
                             {"_page": page, "_pageSize": 100, "orderby": "id"})
        for r in data.get("results", []):
            d = r["data"]
            for realm in d.get("realms", []):
                name = realm.get("name")
                name = name.get("en_US") if isinstance(name, dict) else name
                cat = realm.get("category")
                rows.append({"connected_realm_id": d["id"], "slug": realm.get("slug"), "name": name,
                             "type": (realm.get("type") or {}).get("type"),
                             "category": cat.get("en_US") if isinstance(cat, dict) else cat})
        if page >= data.get("pageCount", 1):
            break
        page += 1
    return sorted(rows, key=lambda r: r["slug"] or "")


def realms_cmd(args) -> int:
    client, _ = api_client(args)
    realms = list_realms(client, args.region, args.namespace)
    rows = [(r["connected_realm_id"], r["slug"], r["name"], r["type"], r["category"]) for r in realms]
    if args.json:
        print(json.dumps(realms))
        return 0
    for crid, slug, name, rtype, cat in sorted(rows, key=lambda r: (r[0], r[1] or "")):
        print(f"{crid:>6}  {slug:<28} {name or '':<28} {rtype or '':<8} {cat or ''}")
    print(f"{len(rows)} realms in {len({r[0] for r in rows})} connected realms")
    return 0


PROBE_NAMESPACES = ["dynamic-{r}", "dynamic-classic-{r}", "dynamic-classic1x-{r}", "dynamic-classicann-{r}",
                    # Guesses for Forever; add more with --namespace once Blizzard documents it.
                    "dynamic-forever-{r}", "dynamic-classicforever-{r}", "dynamic-classicfor-{r}",
                    "dynamic-wowforever-{r}", "dynamic-classicplus-{r}"]


def probe_cmd(args) -> int:
    client, _ = api_client(args)
    for ns in [n.format(r=args.region) for n in PROBE_NAMESPACES] + (args.namespace or []):
        try:
            data, _ = client.get(args.region, ns, "/data/wow/connected-realm/index")
            print(f"{ns:<34} OK  ({len(data.get('connected_realms', []))} connected realms)")
        except market.ApiError as exc:
            print(f"{ns:<34} --  {exc}")
    return 0


def price_cmd(args) -> int:
    if remote_report(args, "price", item=args.item):
        return 0
    conn = db_for(args)
    ids = resolve_items(conn, args.item)
    if not ids:
        print("No matching item (names are known for items you've traded; otherwise pass the item id).")
        return 1
    for item_id in ids[:10]:
        name = conn.execute("SELECT name FROM items WHERE item_id = ?", (item_id,)).fetchone()
        print(f"\n{name[0] if name else 'item'} ({item_id})")
        rows = conn.execute(
            """SELECT m.name, s.last_modified, st.min_price, st.p10, st.median, st.quantity, st.sold + st.gone
               FROM item_states st
               JOIN markets m ON m.mid = st.market_id
               JOIN snapshots s ON s.id = st.snap
               WHERE st.item_id = ? AND s.last_modified >= ?
               ORDER BY m.name, s.last_modified""",
            (item_id, int(time.time()) - args.hours * 3600)).fetchall()
        if not rows:
            print("  no changes in range (prices are stored when they change)")
        print(f"  {'market':<22}{'changed at':<14}{'min':>16}{'p10':>16}{'median':>16}{'listed':>8}{'sold':>6}")
        for m, lm, mn, p10, med, qty, sold in rows:
            when = dt.datetime.fromtimestamp(lm).strftime("%m-%d %H:%M") if lm else "?"
            f = lambda v: fmt_gold(v) if v is not None else "-"  # noqa: E731
            print(f"  {m:<22}{when:<14}{f(mn):>16}{f(p10):>16}{f(med):>16}{qty:>8}{sold:>6}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default {DEFAULT_DB})")
    ap.add_argument("--server", help="Goldmine server URL with client key (overrides [server] url in the config)")
    ap.add_argument("--local", action="store_true", help="ignore any configured server; use the local database")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_sources(p):
        p.add_argument("--wow-dir", action="append", default=None,
                       help="WoW install folder (repeatable). Default: standard install locations")
        p.add_argument("--file", action="append", help="explicit Goldmine.lua path(s); skips discovery")

    def add_config(p):
        p.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")

    def add_pricing(p):
        p.add_argument("--hours", type=int, default=72, help="market value window (default 72h)")
        p.add_argument("--addon-data", default=str(DEFAULT_ADDON_DATA), help="where push writes Data.lua")

    p = sub.add_parser("sync", help="import SavedVariables into the database")
    add_sources(p)
    add_config(p)
    add_pricing(p)
    p.add_argument("--csv", help="also write a full CSV export here after syncing")
    p.add_argument("--push", action="store_true", help="also update the addon's Data.lua")
    p.set_defaults(fn=sync)

    p = sub.add_parser("watch", help="sync automatically whenever WoW writes the file")
    add_sources(p)
    add_config(p)
    add_pricing(p)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--csv", help="rewrite this CSV after each sync")
    p.add_argument("--push", action="store_true", help="also update the addon's Data.lua after each sync")
    p.add_argument("--exit-with-parent", action="store_true", help="stop when the launching process exits")
    p.set_defaults(fn=watch)

    p = sub.add_parser("config", help="read (JSON) or write (JSON on stdin) the config file")
    p.add_argument("action", choices=["get", "set"])
    add_config(p)
    p.set_defaults(fn=config_cmd)

    p = sub.add_parser("status", help="database and collection status as JSON")
    add_sources(p)
    add_config(p)
    p.set_defaults(fn=status_cmd)

    p = sub.add_parser("export", help="write transactions to CSV ('-' for stdout)")
    p.add_argument("out")
    p.add_argument("--char", help="Name or Name-Realm")
    p.add_argument("--since", help="ISO date/time, or relative like 7d / 12h")
    p.add_argument("--until", help="ISO date/time, or relative like 1d")
    p.add_argument("--source", help="only this source (vendor, auction_sale, quest, ...)")
    p.set_defaults(fn=export)

    p = sub.add_parser("summary", help="income/expense by source")
    p.add_argument("--days", type=int, help="limit to the last N days")
    p.add_argument("--char", help="Name or Name-Realm")
    add_config(p)
    p.set_defaults(fn=summary)

    p = sub.add_parser("collect", help="fetch AH snapshots for configured markets")
    add_config(p)
    add_pricing(p)
    p.add_argument("--push", action="store_true", help="also update the addon's Data.lua")
    p.set_defaults(fn=collect_cmd)

    p = sub.add_parser("realms", help="list connected realms in a namespace")
    p.add_argument("region")
    p.add_argument("namespace", help="e.g. dynamic-us, dynamic-classic-us")
    p.add_argument("--json", action="store_true")
    add_config(p)
    p.set_defaults(fn=realms_cmd)

    p = sub.add_parser("probe", help="check which API namespaces exist in a region")
    p.add_argument("region")
    p.add_argument("--namespace", action="append", help="extra namespace(s) to try")
    add_config(p)
    p.set_defaults(fn=probe_cmd)

    p = sub.add_parser("price", help="show recent prices for an item (id or name)")
    p.add_argument("item")
    p.add_argument("--hours", type=int, default=24)
    add_config(p)
    p.set_defaults(fn=price_cmd)

    p = sub.add_parser("crafts", help="profit per craft for recipes your characters know")
    add_config(p)
    add_pricing(p)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=crafts_cmd)

    p = sub.add_parser("deals", help="items listed well below market value")
    add_config(p)
    add_pricing(p)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--max-ratio", type=float, default=0.7, help="listed at or below this fraction of market")
    p.add_argument("--min-sold", type=float, default=1.0, help="minimum estimated sales per day")
    p.set_defaults(fn=deals_cmd)

    p = sub.add_parser("server-check", help="test the connection to the configured Goldmine server")
    p.add_argument("--json", action="store_true")
    add_config(p)
    p.set_defaults(fn=server_check_cmd)

    p = sub.add_parser("timing", help="when an item sells: hour-of-week heatmap (local time)")
    p.add_argument("item", help="item id or name")
    p.add_argument("--market", action="append", help="limit to market(s)")
    p.add_argument("--json", action="store_true")
    add_config(p)
    p.set_defaults(fn=timing_cmd)

    p = sub.add_parser("push", help="write prices, crafts and deals into the addon's Data.lua")
    add_config(p)
    add_pricing(p)
    p.set_defaults(fn=push_cmd)

    args = ap.parse_args(argv)
    if getattr(args, "wow_dir", "unset") is None:
        env = os.environ.get("GOLDMINE_WOW_DIR")
        args.wow_dir = [env] if env else DEFAULT_WOW_DIRS
    try:
        return args.fn(args)
    except remote.RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
