"""Change-based auction house storage: keeps full fidelity without storing every listing hourly.

Tables (market_id is a small integer per market, see markets.mid):

  auctions         one row per listing for its whole life: first seen, quantity at start and
                   latest, time-left bucket at start and latest, when it ended and how.
                   Unchanged listings cost nothing per snapshot.
  auction_changes  a row whenever a listing's quantity drops (a definite partial purchase).
  item_states      per item, a row only when its summary changed (prices, units listed,
                   auctions, depth) or something sold. The state holds until the next row.
                   Once per UTC day a keyframe writes every item's state, so any time window
                   can be reconstructed from the nearest keyframe forward.
  item_current     latest state per item (a cache used for change detection and "now" prices).

Series (the variant column of item_states / item_current):
  variant >= 0     the item as a whole: battle pet species id for pet cages, else 0. Every
                   listing counts here, so these series read the same as they always have.
  variant < 0      one configuration of the item, as a separate copy: -variant is the
                   bonus_keys id of the listing's detail key, "pet:<species>:<breed>:<quality>:<level>"
                   (breed 3-12; the other-gender ids 13-22 count as the same breed)
                   for battle pets or the sorted bonus list ("1472,6652") for gear. Each listing
                   with a detail key counts in its item series and its detail series.

Any past snapshot can be rebuilt exactly from auctions (open between first_snap and
ended_snap). Sales signals per state row:
  sold   units removed from listings that are still up: a definite purchase
  gone   units in listings that vanished with time to spare: sold or cancelled
Listings that vanished when they could have expired are recorded (outcome 2) but not counted.
"""

from __future__ import annotations

import time

TIME_LEFT = {"SHORT": 1, "MEDIUM": 2, "LONG": 3, "VERY_LONG": 4}
TIME_LEFT_NAMES = {v: k for k, v in TIME_LEFT.items()}

OUTCOME_GONE_EARLY = 1   # vanished with time left: sold or cancelled
OUTCOME_MAY_EXPIRE = 2   # vanished when it could have expired
OUTCOME_UNKNOWN = 3      # vanished across a gap too long to judge

SCHEMA = """
CREATE TABLE IF NOT EXISTS bonus_keys (
    id   INTEGER PRIMARY KEY,
    key  TEXT NOT NULL UNIQUE           -- sorted bonus list ids ("1472,6652,7756") or pet:species:breed:quality:level
);
CREATE TABLE IF NOT EXISTS auctions (
    market_id   INTEGER NOT NULL,
    auction_id  INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    variant     INTEGER NOT NULL,       -- battle pet species id, else 0
    bonus       INTEGER NOT NULL,       -- bonus_keys.id of the detail key (gear bonuses, pet breed), else 0
    unit_price  INTEGER,                -- copper per unit; NULL for bid-only listings
    bid         INTEGER,
    qty_first   INTEGER NOT NULL,
    qty_last    INTEGER NOT NULL,
    tl_first    INTEGER,                -- TIME_LEFT code when first seen
    tl_last     INTEGER,                -- TIME_LEFT code when last seen
    first_snap  INTEGER NOT NULL,       -- snapshots.id it first appeared in
    ended_snap  INTEGER,                -- first snapshot it was missing from; NULL = still listed
    outcome     INTEGER,                -- OUTCOME_* once ended
    PRIMARY KEY (market_id, auction_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS auctions_open ON auctions(market_id) WHERE ended_snap IS NULL;
CREATE INDEX IF NOT EXISTS auctions_item ON auctions(item_id, market_id);
CREATE TABLE IF NOT EXISTS auction_changes (
    market_id   INTEGER NOT NULL,
    auction_id  INTEGER NOT NULL,
    snap        INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,       -- quantity from this snapshot on
    PRIMARY KEY (market_id, auction_id, snap)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS item_states (
    market_id  INTEGER NOT NULL,
    item_id    INTEGER NOT NULL,
    variant    INTEGER NOT NULL,
    snap       INTEGER NOT NULL,        -- valid from this snapshot until the item's next row
    min_price  INTEGER,
    p10        INTEGER,
    median     INTEGER,
    quantity   INTEGER NOT NULL,        -- units listed (0 = none on the AH)
    auctions   INTEGER NOT NULL,
    depth5     INTEGER,                 -- units listed within +5% / +10% / +25% of min_price
    depth10    INTEGER,
    depth25    INTEGER,
    sold       INTEGER NOT NULL,        -- definite purchases since the previous snapshot
    gone       INTEGER NOT NULL,        -- units in listings that vanished early (sold or cancelled)
    PRIMARY KEY (market_id, item_id, variant, snap)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS item_states_snap ON item_states(market_id, snap);
CREATE TABLE IF NOT EXISTS item_current (
    market_id  INTEGER NOT NULL,
    item_id    INTEGER NOT NULL,
    variant    INTEGER NOT NULL,
    snap       INTEGER NOT NULL,
    min_price  INTEGER,
    p10        INTEGER,
    median     INTEGER,
    quantity   INTEGER NOT NULL,
    auctions   INTEGER NOT NULL,
    depth5     INTEGER,
    depth10    INTEGER,
    depth25    INTEGER,
    PRIMARY KEY (market_id, item_id, variant)
) WITHOUT ROWID;
"""

STATE_FIELDS = ("min_price", "p10", "median", "quantity", "auctions", "depth5", "depth10", "depth25")
EMPTY_STATE = (None, None, None, 0, 0, None, None, None)


def expiring_codes(gap_seconds: float) -> set[int] | None:
    """Time-left codes that may have run out during a gap; None = gap too long to judge.
    Buckets: SHORT <30m, MEDIUM 30m-2h, LONG 2-12h, VERY_LONG 12-48h."""
    if gap_seconds <= 30 * 60:
        return {1}
    if gap_seconds <= 2 * 3600:
        return {1, 2}
    if gap_seconds <= 12 * 3600:
        return {1, 2, 3}
    return None


def market_id(conn, name: str) -> int:
    row = conn.execute("SELECT mid FROM markets WHERE name = ?", (name,)).fetchone()
    if row and row[0] is not None:
        return row[0]
    conn.execute("INSERT OR IGNORE INTO markets (name, region, namespace, kind) VALUES (?, '', '', '')", (name,))
    mid = conn.execute("SELECT COALESCE(MAX(mid), 0) + 1 FROM markets").fetchone()[0]
    conn.execute("UPDATE markets SET mid = ? WHERE name = ?", (mid, name))
    return mid


class BonusKeys:
    def __init__(self, conn):
        self.conn = conn
        self.cache = dict(conn.execute("SELECT key, id FROM bonus_keys"))

    def id_for(self, bonus_lists) -> int:
        if not bonus_lists:
            return 0
        return self.id_for_key(",".join(str(b) for b in sorted(bonus_lists)))

    def id_for_key(self, key: str) -> int:
        bid = self.cache.get(key)
        if bid is None:
            bid = self.conn.execute("INSERT INTO bonus_keys (key) VALUES (?)", (key,)).lastrowid
            self.cache[key] = bid
        return bid


PET_BREEDS = {3: "B/B", 4: "P/P", 5: "S/S", 6: "H/H", 7: "H/P", 8: "P/S", 9: "H/S", 10: "P/B",
              11: "S/B", 12: "H/B"}  # 13-22 are the same breeds (the other gender), counted as 3-12
PET_QUALITIES = {0: "poor", 1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary"}


def detail_key(item: dict) -> str | None:
    """What sets this listing apart from others of the same item (and pet species), or None."""
    if item.get("pet_species_id"):
        breed = item.get("pet_breed_id", 0)
        breed = breed - 10 if 13 <= breed <= 22 else breed
        return f"pet:{item['pet_species_id']}:{breed}:{item.get('pet_quality_id', 0)}:{item.get('pet_level', 0)}"
    if item.get("bonus_lists"):
        return ",".join(str(b) for b in sorted(item["bonus_lists"]))
    return None


def detail_label(key: str | None) -> str | None:
    """'pet:1234:4:3:25' -> 'P/P rare L25'; a bonus list -> 'bonuses 1472,6652'."""
    if not key:
        return None
    if key.startswith("pet:"):
        _, _, breed, quality, level = key.split(":")
        return (f"{PET_BREEDS.get(int(breed), f'breed {breed}')} "
                f"{PET_QUALITIES.get(int(quality), f'quality {quality}')} L{level}")
    return f"bonuses {key}"


def variant_labels(conn, variants) -> dict[int, str | None]:
    """{variant: label}: 'species N' for a pet species, the detail for a detail series, None for 0."""
    details = [-v for v in set(variants) if v < 0]
    keys = dict(conn.execute(f"SELECT id, key FROM bonus_keys WHERE id IN ({','.join('?' * len(details))})",
                             details)) if details else {}
    return {v: detail_label(keys.get(-v)) if v < 0 else f"species {v}" if v else None for v in set(variants)}


def parse_listing(a: dict, bonus: BonusKeys) -> tuple:
    """-> (auction_id, item_id, variant, detail_id, unit_price, bid, quantity, time_left_code);
    detail_id is the bonus_keys id of detail_key() (0 = none)."""
    item = a.get("item") or {}
    qty = a.get("quantity") or 1
    if "unit_price" in a:
        unit = a["unit_price"]
    elif a.get("buyout"):
        unit = a["buyout"] // qty  # realm and classic buyouts are per stack
    else:
        unit = None
    key = detail_key(item)
    return (a["id"], item["id"], item.get("pet_species_id") or 0, bonus.id_for_key(key) if key else 0,
            unit, a.get("bid"), qty, TIME_LEFT.get(a.get("time_left")))


def _percentile(pairs: list[tuple[int, int]], total: int, pct: float) -> int:
    seen = 0
    for price, q in pairs:
        seen += q
        if seen >= total * pct:
            return price
    return pairs[-1][0]


def series_keys(item: int, variant: int, detail: int) -> tuple:
    """The (item, variant) series a listing counts in: its item's, and its detail's if any."""
    return ((item, variant), (item, -detail)) if detail else ((item, variant),)


def summarize(listings: list[tuple]) -> dict[tuple[int, int], tuple]:
    """listings from parse_listing -> {(item, variant): state tuple in STATE_FIELDS order}"""
    groups: dict[tuple[int, int], list] = {}
    for _, item, variant, detail, unit, _, qty, _ in listings:
        for key in series_keys(item, variant, detail):
            g = groups.setdefault(key, [[], 0, 0])
            g[1] += qty
            g[2] += 1
            if unit is not None:
                g[0].append((unit, qty))
    out = {}
    for key, (pairs, quantity, auctions) in groups.items():
        if pairs:
            pairs.sort()
            priced = sum(q for _, q in pairs)
            lo = pairs[0][0]
            depth = [sum(q for p, q in pairs if p <= lo * f) for f in (1.05, 1.10, 1.25)]
            out[key] = (lo, _percentile(pairs, priced, 0.10), _percentile(pairs, priced, 0.50),
                        quantity, auctions, *depth)
        else:
            out[key] = (None, None, None, quantity, auctions, None, None, None)
    return out


def store_snapshot(conn, market: str, data: dict, last_modified: int | None,
                   fetched_at: int | None = None) -> dict:
    """Apply one API snapshot to the change-based tables. Returns counters for logging."""
    mid = market_id(conn, market)
    lm = last_modified or int(time.time())
    if conn.execute("SELECT 1 FROM snapshots WHERE market = ? AND last_modified = ?", (market, lm)).fetchone():
        return {"skipped": True}
    bonus = BonusKeys(conn)
    listings = [parse_listing(a, bonus) for a in data.get("auctions", [])]
    sid, prev, keyframe = new_snapshot(conn, market, lm, len(listings), fetched_at)

    open_auctions = {aid: [series_keys(item, variant, detail), qty, tl, detail]
                     for aid, item, variant, detail, qty, tl in conn.execute(
                         "SELECT auction_id, item_id, variant, bonus, qty_last, tl_last FROM auctions "
                         "WHERE market_id = ? AND ended_snap IS NULL", (mid,))}
    expiring = expiring_codes(lm - prev[1]) if prev else None

    sold: dict[tuple[int, int], int] = {}
    gone: dict[tuple[int, int], int] = {}
    new_rows, qty_updates, tl_updates, changes, detail_updates = [], [], [], [], []
    seen = set()
    for aid, item, variant, bonus_id, unit, bid, qty, tl in listings:
        seen.add(aid)
        old = open_auctions.get(aid)
        if old is None:
            new_rows.append((mid, aid, item, variant, bonus_id, unit, bid, qty, qty, tl, tl, sid))
            continue
        if bonus_id != old[3]:  # listed before pets had detail keys
            old[0] = series_keys(item, variant, bonus_id)
            detail_updates.append((bonus_id, mid, aid))
        keys, old_qty, old_tl, _ = old
        if qty != old_qty:
            if qty < old_qty:
                for key in keys:
                    sold[key] = sold.get(key, 0) + old_qty - qty
            qty_updates.append((qty, tl, mid, aid))
            changes.append((mid, aid, sid, qty))
        elif tl != old_tl:
            tl_updates.append((tl, mid, aid))
    ended = []
    for aid, (keys, qty, tl, _) in open_auctions.items():
        if aid in seen:
            continue
        if expiring is None:
            outcome = OUTCOME_UNKNOWN
        elif tl in expiring:
            outcome = OUTCOME_MAY_EXPIRE
        else:
            outcome = OUTCOME_GONE_EARLY
            for key in keys:
                gone[key] = gone.get(key, 0) + qty
        ended.append((sid, outcome, mid, aid))

    conn.executemany("INSERT OR REPLACE INTO auctions (market_id, auction_id, item_id, variant, bonus, unit_price, bid, "
                     "qty_first, qty_last, tl_first, tl_last, first_snap) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", new_rows)
    conn.executemany("UPDATE auctions SET qty_last = ?, tl_last = ? WHERE market_id = ? AND auction_id = ?", qty_updates)
    conn.executemany("UPDATE auctions SET tl_last = ? WHERE market_id = ? AND auction_id = ?", tl_updates)
    conn.executemany("UPDATE auctions SET bonus = ? WHERE market_id = ? AND auction_id = ?", detail_updates)
    conn.executemany("INSERT OR REPLACE INTO auction_changes VALUES (?, ?, ?, ?)", changes)
    conn.executemany("UPDATE auctions SET ended_snap = ?, outcome = ? WHERE market_id = ? AND auction_id = ?", ended)

    states = summarize(listings)
    written = write_states(conn, mid, sid, keyframe, states, sold, gone)
    conn.commit()
    return {"listings": len(listings), "items": len(states), "new": len(new_rows), "ended": len(ended),
            "partial": len(changes), "states": written, "keyframe": keyframe}


def new_snapshot(conn, market: str, lm: int, listings: int, fetched_at: int | None = None) -> tuple:
    """Add a market's snapshot row -> (snapshot id, previous (id, last_modified) or None, keyframe).
    The first snapshot of each UTC day is a keyframe."""
    prev = conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? ORDER BY last_modified DESC LIMIT 1",
                        (market,)).fetchone()
    day = time.strftime("%Y-%m-%d", time.gmtime(lm))
    keyframe = prev is None or time.strftime("%Y-%m-%d", time.gmtime(prev[1])) != day
    sid = conn.execute(
        "INSERT INTO snapshots (market, fetched_at, last_modified, auctions, keyframe) VALUES (?, ?, ?, ?, ?)",
        (market, fetched_at or int(time.time()), lm, listings, int(keyframe))).lastrowid
    return sid, prev, keyframe


def write_states(conn, mid: int, sid: int, keyframe: bool, states: dict, sold: dict, gone: dict) -> int:
    """Write item states for snapshot `sid`: a row when the summary changed, something sold, or
    on a keyframe; keeps item_current in step. Returns the rows written (no commit)."""
    current = {(i, v): tuple(rest) for i, v, *rest in conn.execute(
        f"SELECT item_id, variant, {', '.join(STATE_FIELDS)} FROM item_current WHERE market_id = ?", (mid,))}
    state_rows, upserts, removals = [], [], []
    for key in states.keys() | current.keys() | sold.keys() | gone.keys():
        new = states.get(key, EMPTY_STATE)
        old = current.get(key)
        s, g = sold.get(key, 0), gone.get(key, 0)
        if old is None and new == EMPTY_STATE and not (s or g):
            continue
        if keyframe or new != old or s or g:
            state_rows.append((mid, key[0], key[1], sid, *new, s, g))
        if key in states:
            if new != old:
                upserts.append((mid, key[0], key[1], sid, *new))
        elif old is not None:
            removals.append((mid, key[0], key[1]))
    conn.executemany(f"INSERT INTO item_states VALUES ({', '.join('?' * 14)})", state_rows)
    conn.executemany(f"INSERT OR REPLACE INTO item_current VALUES ({', '.join('?' * 12)})", upserts)
    conn.executemany("DELETE FROM item_current WHERE market_id = ? AND item_id = ? AND variant = ?", removals)
    return len(state_rows)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def market_ids(conn, names: list[str] | None = None) -> dict[str, int]:
    rows = conn.execute("SELECT name, mid FROM markets WHERE mid IS NOT NULL").fetchall()
    return {n: m for n, m in rows if names is None or n in names}


def snapshot_times(conn, market: str, since: int | None = None) -> list[tuple[int, int, int]]:
    """[(snapshot id, last_modified, keyframe)] in time order."""
    sql = "SELECT id, last_modified, keyframe FROM snapshots WHERE market = ?"
    params: list = [market]
    if since is not None:
        sql += " AND last_modified >= ?"
        params.append(since)
    return conn.execute(sql + " ORDER BY last_modified", params).fetchall()


def window_start_snapshot(conn, market: str, since: int) -> int | None:
    """The keyframe at or before `since` (so states can be replayed from there), or the
    market's first snapshot."""
    row = conn.execute("SELECT id FROM snapshots WHERE market = ? AND keyframe = 1 AND last_modified <= ? "
                       "ORDER BY last_modified DESC LIMIT 1", (market, since)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT id FROM snapshots WHERE market = ? ORDER BY last_modified LIMIT 1", (market,)).fetchone()
    return row[0] if row else None


def listings_at(conn, market: str, snapshot_id: int) -> list[tuple]:
    """Reconstruct the listings that were up in a given snapshot (exact, from lifecycles)."""
    from . import archive  # late: archive imports store
    return [(a[1], a[0], a[2], a[4], archive.quantity_at(a, snapshot_id))
            for a in archive.auctions(conn, market_id(conn, market), up_at=snapshot_id)]


def migrate(conn) -> None:
    """Move databases from the hourly-rows layout to change-based storage. The old tables only
    ever held a few days of data, so they're dropped rather than converted."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(markets)")}
    if cols and "mid" not in cols:
        conn.execute("ALTER TABLE markets ADD COLUMN mid INTEGER")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if scols and "keyframe" not in scols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN keyframe INTEGER NOT NULL DEFAULT 0")
    old = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} & {
        "prices", "last_listings", "prices_daily", "price_profile"}
    if old:
        for t in old:
            conn.execute(f"DROP TABLE {t}")
        conn.execute("DROP VIEW IF EXISTS latest_prices")
        conn.execute("DELETE FROM snapshots")
        conn.commit()
    conn.executescript(SCHEMA)

