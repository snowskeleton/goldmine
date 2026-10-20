"""Per-user game data: the ledger schema, importing the addon's SavedVariables, and the text
reports (crafts, deals, summary, price, timing) and Data.lua the server hands to clients.

Report functions take an argparse-style namespace with the user's connection in `args.conn`
and print their output; services.run_report captures it.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path
import os

from . import market, pricing, svparse

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
-- Account state for the trader (Market.lua): see docs/trader.md.
CREATE TABLE IF NOT EXISTS recipe_ops (   -- crafting operation info, basic reagents, no concentration
    char_key            TEXT NOT NULL,
    recipe_id           INTEGER NOT NULL,
    base_skill          INTEGER,
    bonus_skill         INTEGER,
    difficulty          INTEGER,
    bonus_difficulty    INTEGER,
    quality             INTEGER,          -- expected crafting quality (tier)
    quality_id          INTEGER,
    lower_skill         INTEGER,          -- skill thresholds of the expected tier
    upper_skill         INTEGER,
    quality_craft       INTEGER,
    concentration_cost  INTEGER,
    ingenuity_refund    INTEGER,
    multicraft          REAL,             -- percent
    resourcefulness     REAL,             -- percent
    stats               TEXT,             -- JSON {bonus stat name: percent}
    updated             INTEGER,
    PRIMARY KEY (char_key, recipe_id)
);
CREATE TABLE IF NOT EXISTS professions (
    char_key           TEXT NOT NULL,
    profession         TEXT NOT NULL,
    scanned            INTEGER,           -- recipes last catalogued (profession window open)
    concentration      INTEGER,
    concentration_max  INTEGER,
    skill              INTEGER,           -- from GetProfessionInfo on login
    max_skill          INTEGER,
    skill_line         INTEGER,
    skill_seen         INTEGER,
    PRIMARY KEY (char_key, profession)
);
CREATE TABLE IF NOT EXISTS auction_mail (  -- AH mail seen in the inbox (sale invoices etc.)
    id           TEXT PRIMARY KEY,         -- Name-Realm:mN
    char_key     TEXT NOT NULL,
    arrived      INTEGER NOT NULL,         -- estimated from days left (auction mail lasts 30 days)
    last_seen    INTEGER,
    kind         TEXT,                     -- sold | won | outbid | expired | cancelled (from the subject)
    invoice      TEXT,                     -- seller | seller_temp_invoice | buyer (GetInboxInvoiceInfo)
    item_id      INTEGER,
    item_name    TEXT,
    quantity     INTEGER,
    player       TEXT,                     -- buyer on seller invoices, seller on buyer invoices
    bid          INTEGER,
    buyout       INTEGER,
    deposit      INTEGER,
    consignment  INTEGER,                  -- the AH cut
    money        INTEGER,                  -- gold attached
    sender       TEXT
);
CREATE INDEX IF NOT EXISTS auction_mail_arrived ON auction_mail(arrived);
CREATE TABLE IF NOT EXISTS crafting_orders (
    order_id        INTEGER PRIMARY KEY,
    seen_by         TEXT NOT NULL,
    order_type      INTEGER,               -- Enum.CraftingOrderType: 0 public, 1 guild, 2 personal, 3 patron (NPC)
    order_state     INTEGER,
    item_id         INTEGER,
    recipe_id       INTEGER,
    min_quality     INTEGER,
    tip             INTEGER,               -- commission, copper
    consortium_cut  INTEGER,
    expires         INTEGER,
    claim_end       INTEGER,
    recraft         INTEGER,
    fulfillable     INTEGER,
    reagent_state   INTEGER,
    customer        TEXT,
    npc_customer    INTEGER,
    reagents        TEXT,                  -- JSON [[item_id, quantity], ...] provided by the customer
    rewards         TEXT,                  -- JSON [[item_id, count], ...] (patron orders)
    first_seen      INTEGER,
    last_seen       INTEGER
);
CREATE TABLE IF NOT EXISTS crafting_order_buckets (  -- the public order board, per character's last look
    char_key   TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    item_id    INTEGER,
    recipe_id  INTEGER,
    available  INTEGER,
    tip_avg    INTEGER,
    tip_max    INTEGER
);
CREATE TABLE IF NOT EXISTS craft_results (
    id             TEXT PRIMARY KEY,      -- Name-Realm:cN
    char_key       TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    recipe_id      INTEGER,
    item_id        INTEGER,
    quantity       INTEGER,
    quality        INTEGER,
    multicraft     INTEGER,               -- extra items from a multicraft proc (0 = none)
    concentration  INTEGER,               -- concentration spent
    ingenuity      INTEGER,               -- 1 = ingenuity proc
    returned       TEXT,                  -- JSON [[item_id, quantity], ...] from resourcefulness
    operation_id   INTEGER
);
CREATE INDEX IF NOT EXISTS craft_results_ts ON craft_results(ts);
CREATE TABLE IF NOT EXISTS owned_auctions (
    auction_id   INTEGER PRIMARY KEY,
    char_key     TEXT NOT NULL,
    item_id      INTEGER NOT NULL,
    variant      INTEGER NOT NULL DEFAULT 0,
    item_level   INTEGER,
    quantity     INTEGER NOT NULL,
    buyout       INTEGER,                 -- as the AH reports it: per unit for commodities
    bid          INTEGER,
    unit_price   INTEGER,                 -- copper per unit
    commodity    INTEGER,
    time_left    INTEGER,                 -- seconds, as of last_seen
    status       INTEGER,                 -- Enum.AuctionStatus: 0 active, 1 sold
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS owned_auction_lists (  -- when each character's list was last seen
    char_key  TEXT PRIMARY KEY,
    ts        INTEGER NOT NULL,
    count     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS warband_money (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    money    INTEGER NOT NULL,            -- copper
    updated  INTEGER NOT NULL,
    seen_by  TEXT
);
CREATE TABLE IF NOT EXISTS item_meta (    -- from the game client's GetItemInfo
    item_id      INTEGER PRIMARY KEY,
    stack_size   INTEGER,
    sell_price   INTEGER,
    class_id     INTEGER,
    subclass_id  INTEGER,
    bind_type    INTEGER,                 -- Enum.ItemBind: 0 none, 1 on pickup, 2 on equip, 3 on use, ...
    expansion    INTEGER,                 -- 0 classic ... 10 The War Within, 11 Midnight
    updated      INTEGER
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
    """The user's connection, handed in by the server."""
    return args.conn


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
            for table in ("recipe_reagents", "recipe_ops"):
                conn.execute(f"""DELETE FROM {table} WHERE char_key = ? AND recipe_id IN
                                 (SELECT recipe_id FROM recipes WHERE char_key = ? AND profession = ?)""",
                             (key, key, prof))
            conn.execute("DELETE FROM recipes WHERE char_key = ? AND profession = ?", (key, prof))
            for rid, r in recipes.items():
                conn.execute("INSERT OR REPLACE INTO recipes VALUES (?, ?, ?, ?, ?, ?)",
                             (key, int(rid), prof, r.get("n"), r.get("o"), r.get("q")))
                conn.executemany("INSERT INTO recipe_reagents VALUES (?, ?, ?, ?)",
                                 [(key, int(rid), item, qty) for item, qty in _pairs(r.get("r"), 2)])
                if isinstance(r.get("op"), dict):
                    _import_op(conn, key, int(rid), r["op"], pdata.get("t"))
            conc = pdata.get("conc") or {}
            conn.execute("""INSERT INTO professions (char_key, profession, scanned, concentration, concentration_max)
                            VALUES (?, ?, ?, ?, ?) ON CONFLICT(char_key, profession) DO UPDATE SET scanned=excluded.scanned,
                              concentration=excluded.concentration, concentration_max=excluded.concentration_max""",
                         (key, prof, pdata.get("t"), conc.get("q"), conc.get("max")))
        for prof, info in _as_dict(c.get("profInfo")).items():
            if isinstance(info, dict):
                conn.execute("""INSERT INTO professions (char_key, profession, skill, max_skill, skill_line, skill_seen)
                                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(char_key, profession) DO UPDATE SET skill=excluded.skill,
                                  max_skill=excluded.max_skill, skill_line=excluded.skill_line,
                                  skill_seen=excluded.skill_seen""",
                             (key, prof, info.get("rank"), info.get("max"), info.get("line"), info.get("t")))
        inv = c.get("inventory") or {}
        for loc in ("bags", "bank"):
            if loc in inv:
                conn.execute("DELETE FROM inventory WHERE owner = ? AND location = ?", (key, loc))
                conn.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?, ?)",
                                 [(key, loc, int(i), n, inv.get(loc + "At")) for i, n in _as_dict(inv[loc]).items()])
        if isinstance(c.get("auctions"), dict):
            _import_owned_auctions(conn, key, c["auctions"])
    warband = data.get("warband")
    if warband:
        conn.execute("DELETE FROM inventory WHERE owner = 'warband'")
        conn.executemany("INSERT INTO inventory VALUES ('warband', 'warband', ?, ?, ?)",
                         [(int(i), n, warband.get("t")) for i, n in _as_dict(warband.get("items")).items()])
    money = data.get("warbandMoney")
    if isinstance(money, dict) and money.get("m") is not None:
        conn.execute("""INSERT INTO warband_money VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                          money=excluded.money, updated=excluded.updated, seen_by=excluded.seen_by
                        WHERE excluded.updated >= warband_money.updated""",
                     (money["m"], money.get("t") or 0, money.get("by")))
    for craft in data.get("crafts") or []:
        conn.execute("INSERT OR IGNORE INTO craft_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (craft["id"], craft["c"], craft["t"], craft.get("r"), craft.get("item"), craft.get("q"),
                      craft.get("ql"), craft.get("mc") or 0, craft.get("cs") or 0, 1 if craft.get("ing") else 0,
                      json.dumps(_pairs(craft.get("rs"), 2)), craft.get("op")))
    for m in data.get("mail") or []:
        conn.execute("""INSERT INTO auction_mail VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET last_seen=MAX(COALESCE(auction_mail.last_seen, 0),
                                                                     COALESCE(excluded.last_seen, 0))""",
                     (m["id"], m["c"], m["arr"], m.get("seen"), m.get("kind"), m.get("inv"), m.get("iid"),
                      m.get("item"), m.get("n"), m.get("who"), m.get("bid"), m.get("buy"), m.get("dep"),
                      m.get("cut"), m.get("money"), m.get("from")))
    for order_id, o in _as_dict(data.get("craftOrders")).items():
        if not isinstance(o, dict):
            continue
        conn.execute("""INSERT INTO crafting_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(order_id) DO UPDATE SET
                          first_seen=MIN(crafting_orders.first_seen, excluded.first_seen),
                          last_seen=MAX(crafting_orders.last_seen, excluded.last_seen),
                          order_state=CASE WHEN excluded.last_seen >= crafting_orders.last_seen
                                           THEN excluded.order_state ELSE crafting_orders.order_state END,
                          fulfillable=CASE WHEN excluded.last_seen >= crafting_orders.last_seen
                                           THEN excluded.fulfillable ELSE crafting_orders.fulfillable END""",
                     (int(order_id), o.get("c"), o.get("type"), o.get("state"), o.get("item"), o.get("r"),
                      o.get("minq"), o.get("tip"), o.get("cut"), o.get("exp"), o.get("claim"),
                      1 if o.get("recraft") else 0, None if o.get("ok") is None else int(bool(o["ok"])),
                      o.get("rstate"), o.get("who"), o.get("npc"), json.dumps(_pairs(o.get("rg"), 2)),
                      json.dumps(_pairs(o.get("rw"), 2)), o.get("first") or 0, o.get("seen") or 0))
    for key, b in _as_dict(data.get("craftBuckets")).items():
        if not isinstance(b, dict):
            continue
        row = conn.execute("SELECT MAX(ts) FROM crafting_order_buckets WHERE char_key = ?", (key,)).fetchone()
        if row[0] is not None and row[0] > (b.get("t") or 0):
            continue  # an older upload
        conn.execute("DELETE FROM crafting_order_buckets WHERE char_key = ?", (key,))
        conn.executemany("INSERT INTO crafting_order_buckets VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [(key, b.get("t") or 0, x.get("item"), x.get("r"), x.get("n"), x.get("avg"), x.get("max"))
                          for x in b.get("list") or [] if isinstance(x, dict)])
    for item_id, m in _as_dict(data.get("itemInfo")).items():
        if isinstance(m, dict):
            conn.execute("INSERT OR REPLACE INTO item_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (int(item_id), m.get("s"), m.get("v"), m.get("c"), m.get("sc"), m.get("b"), m.get("x"),
                          m.get("t")))


def _import_op(conn, key: str, recipe_id: int, op: dict, updated) -> None:
    stats = op.get("stats") if isinstance(op.get("stats"), dict) else {}
    conn.execute("INSERT OR REPLACE INTO recipe_ops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key, recipe_id, op.get("sk"), op.get("bsk"), op.get("df"), op.get("bdf"), op.get("ql"),
                  op.get("qid"), op.get("lo"), op.get("hi"), 1 if op.get("qc") else 0, op.get("cc"), op.get("ing"),
                  stats.get("Multicraft"), stats.get("Resourcefulness"),
                  json.dumps(stats, ensure_ascii=False), updated))


def _import_owned_auctions(conn, key: str, snap: dict) -> None:
    """One character's owned-auction list. Auctions are upserted by id (first/last seen), and
    owned_auction_lists says which list is the latest, so a vanished auction drops out of it."""
    ts = snap.get("t") or 0
    row = conn.execute("SELECT ts FROM owned_auction_lists WHERE char_key = ?", (key,)).fetchone()
    latest = row is None or ts >= row[0]
    auctions = snap.get("list") or []
    for a in auctions:
        qty = a.get("q") or 1
        buyout = a.get("b")
        unit = buyout if a.get("cm") == 1 or buyout is None else buyout // max(qty, 1)
        conn.execute("""INSERT INTO owned_auctions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(auction_id) DO UPDATE SET
                          first_seen=MIN(owned_auctions.first_seen, excluded.first_seen),
                          last_seen=MAX(owned_auctions.last_seen, excluded.last_seen),
                          quantity=CASE WHEN excluded.last_seen >= owned_auctions.last_seen
                                        THEN excluded.quantity ELSE owned_auctions.quantity END,
                          time_left=CASE WHEN excluded.last_seen >= owned_auctions.last_seen
                                         THEN excluded.time_left ELSE owned_auctions.time_left END,
                          status=CASE WHEN excluded.last_seen >= owned_auctions.last_seen
                                      THEN excluded.status ELSE owned_auctions.status END""",
                     (a["id"], key, a["item"], a.get("v") or 0, a.get("il"), qty, buyout, a.get("bid"), unit,
                      a.get("cm"), a.get("tl"), a.get("st"), ts, ts))
    if latest:
        conn.execute("INSERT OR REPLACE INTO owned_auction_lists VALUES (?, ?, ?)", (key, ts, len(auctions)))


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


def write_csv(conn, f, **filters) -> int:
    """Write transactions as CSV to a text file object. Returns the row count."""
    rows = query_rows(conn, **filters)
    w = csv.writer(f)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([
            r["id"], r["char_key"],
            dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc).isoformat(sep=" "), r["ts"],
            r["delta"], f"{r['delta'] / 10000:.4f}",
            r["balance"], f"{r['balance'] / 10000:.4f}",
            r["source"], "in" if r["delta"] > 0 else "out" if r["delta"] < 0 else "",
            r["zone"], r["subzone"], r["item_id"], r["item_name"], r["map_id"], r["session"], r["meta"] or "",
        ])
    return len(rows)


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
    """Write the addon's Data.lua (prices, crafts, deals for the user's realms) to args.addon_data."""
    exports = set_reports(conn, args, cfg)
    n = pricing.write_addon_data(args.addon_data, exports)
    size = os.path.getsize(args.addon_data) / 1024
    print(f"wrote {args.addon_data} ({len(exports)} market sets, {n} item prices, {size:.0f} KB)")


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
    offset = getattr(args, "utc_offset", 0)  # the server reports in UTC; clients pass their own offset
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
    print(f"{name} ({item_id}) - UTC{offset:+d}")
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



def flips_cmd(args) -> int:
    """Commodities that keep swinging from their lows to their highs, plus a weekday x hour price index."""
    offset = getattr(args, "utc_offset", 0)
    days = args.days or 7
    conn = db_for(args)
    mkts = args.market or [n for n, in conn.execute(
        "SELECT name FROM markets WHERE kind = 'commodities' AND COALESCE(game, 'retail') = 'retail' ORDER BY name")]
    if not mkts:
        print("No retail commodity market configured.")
        return 0
    for mkt in mkts:
        flips, index, liquid = pricing.flip_report(conn, mkt, days=days)
        names = item_names(conn, {f.item_id for f in flips[:args.limit]}, args)
        print(f"{mkt}: flips over the last {days:g} days ({liquid} liquid items, "
              f"{len(flips)} with 2+ round trips and 3%+ net)")
        if flips:
            print(f"{'item':<30}{'buy below':>15}{'sell at':>15}{'net':>6}{'trips':>6}{'drift':>8}"
                  f"{'depth@low':>10}{'sold/d':>9}{'moved/d':>11}{'profit/trip':>13}")
            for f in flips[:args.limit]:
                depth = f"{f.depth_low:,}" if f.depth_low is not None else "-"
                print(f"{names[f.item_id][:29]:<30}{fmt_gold(f.buy_below):>15}{fmt_gold(f.sell_at):>15}"
                      f"{f.margin:>6.0%}{f.trips:>6}{f.drift:>+8.0%}{depth:>10}{f.sold_per_day:>9,.0f}"
                      f"{f.moved_per_day:>11,.0f}{f.profit_per_trip // 10000:>12,}g")
            print("  buy below = P25 min price, sell at = P75; net is after the 5% AH cut. depth@low = units within"
                  " 5% of the min while at the lows; profit/trip buys min(depth@low, moved/d).")
        local = pricing.to_local([v for row in index for v in row], offset)
        cells = [v for v in local if v is not None]
        if not cells:
            print()
            continue
        print(f"\n  Price index (min price / own median, median over liquid items), % vs median, UTC{offset:+d}")
        print("  hour " + "".join(f"{d:>7}" for d in DAYS))
        for h in range(24):
            row = [local[d * 24 + h] for d in range(7)]
            print(f"  {h:02d}h  " + "".join(f"{(v - 1) * 100:>+7.1f}" if v is not None else f"{'':>7}" for v in row))
        day_avg = []
        for d in range(7):
            vals = [v for v in local[d * 24:(d + 1) * 24] if v is not None]
            day_avg.append(sum(vals) / len(vals) if vals else None)
        print("  avg  " + "".join(f"{(v - 1) * 100:>+7.1f}" if v is not None else f"{'':>7}" for v in day_avg))
        lo, hi = min(cells), max(cells)
        li, hi_i = local.index(lo), local.index(hi)
        print(f"  cheapest {DAYS[li // 24]} {li % 24:02d}h ({(lo - 1) * 100:+.1f}%), dearest {DAYS[hi_i // 24]} "
              f"{hi_i % 24:02d}h ({(hi - 1) * 100:+.1f}%): swing {(hi / lo - 1) * 100:.1f}% vs the 5% AH cut. "
              f"US weekly reset is Tue 15h UTC.")
        print()
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


PROBE_NAMESPACES = [g["prefix"] + "-{r}" for g in market.NAMESPACE_GAMES] + [
                    # Guesses for Forever; add more with --namespace once Blizzard documents it.
                    "dynamic-forever-{r}", "dynamic-classicforever-{r}", "dynamic-classicfor-{r}",
                    "dynamic-wowforever-{r}", "dynamic-classicplus-{r}"]


def price_cmd(args) -> int:
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
