"""Compressed history: item states and ended listings packed into one chunk per item per week.

store.py writes every snapshot to plain tables (the hot tables). Once a UTC day is over, pack()
moves that day's rows out of them into chunks, so the hot tables only ever hold about a day:

  state_chunks    item_states rows of one (market, item, variant) in one week
  auction_chunks  ended listings of one (market, item) in the week they ended, with their
                  auction_changes folded in

Nothing is lost: every row and column comes back exactly as written. A chunk stores each column
as deltas from the previous row in the narrowest fixed-width integer type that fits, then zlib,
which takes item states from ~53 bytes a row (row + index) to ~7, and decodes at C speed.

Readers go through states() / last_state() / auctions() here, which merge the hot tables with
the chunks. They read the hot tables first: pack() moves rows to chunks in one transaction, so a
pack between the two reads shows the row in the chunks instead, never in neither. A week is
last_modified // WEEK of the row's snapshot (an item state's own, a listing's ended_snap).
"""

from __future__ import annotations

import array
import struct
import sys
import time
import zlib
from itertools import accumulate, groupby

from . import store

WEEK = 7 * 86400
FORMAT = 1
_WIDTHS = (("b", 7), ("h", 15), ("i", 31), ("q", 63))
_BIG_ENDIAN = sys.byteorder == "big"

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_chunks (
    market_id   INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    variant     INTEGER NOT NULL,
    week        INTEGER NOT NULL,       -- last_modified // WEEK of the rows' snapshots
    first_snap  INTEGER NOT NULL,       -- snapshot range of the rows, to find chunks by snapshot
    last_snap   INTEGER NOT NULL,
    row_count   INTEGER NOT NULL,
    data        BLOB NOT NULL,
    PRIMARY KEY (market_id, item_id, variant, week)
);
CREATE INDEX IF NOT EXISTS state_chunks_snap ON state_chunks(market_id, last_snap);
CREATE TABLE IF NOT EXISTS auction_chunks (
    market_id   INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    week        INTEGER NOT NULL,       -- last_modified // WEEK of the listings' ended_snap
    first_snap  INTEGER NOT NULL,       -- earliest first_snap / latest ended_snap of the listings
    last_snap   INTEGER NOT NULL,
    row_count   INTEGER NOT NULL,
    data        BLOB NOT NULL,
    PRIMARY KEY (market_id, item_id, week)
);
CREATE INDEX IF NOT EXISTS auction_chunks_snap ON auction_chunks(market_id, last_snap);
-- Per market: everything from snapshots before this time (unix seconds) has been packed.
CREATE TABLE IF NOT EXISTS archive_packed (
    market_id  INTEGER PRIMARY KEY,
    before     INTEGER NOT NULL
);
"""

STATE_COLS = ("snap", *store.STATE_FIELDS, "sold", "gone")
AUCTION_COLS = ("auction_id", "variant", "bonus", "unit_price", "bid", "qty_first", "qty_last",
                "tl_first", "tl_last", "first_snap", "ended_snap", "outcome")


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def encode(columns: list[list]) -> bytes:
    """Integer columns (None allowed) -> compressed blob. Columns may differ in length."""
    out = bytearray((FORMAT, len(columns)))
    for col in columns:
        nulls = any(v is None for v in col)
        if nulls:  # None -> 0, v >= 0 -> v + 1, negatives unchanged
            col = [0 if v is None else v + 1 if v >= 0 else v for v in col]
        deltas = [b - a for a, b in zip([0, *col], col)]
        lo, hi = min(deltas, default=0), max(deltas, default=0)
        code = next(c for c, bits in _WIDTHS if -(1 << bits) <= lo and hi < (1 << bits))
        arr = array.array(code, deltas)
        if _BIG_ENDIAN:
            arr.byteswap()
        out += struct.pack("<IBc", len(col), nulls, code.encode()) + arr.tobytes()
    return zlib.compress(bytes(out), 9)


def decode(blob: bytes) -> list[list]:
    raw = zlib.decompress(blob)
    if raw[0] != FORMAT:
        raise ValueError(f"unknown chunk format {raw[0]}")
    cols, pos = [], 2
    for _ in range(raw[1]):
        n, nulls, code = struct.unpack_from("<IBc", raw, pos)
        pos += 6
        arr = array.array(code.decode())
        size = n * arr.itemsize
        arr.frombytes(raw[pos:pos + size])
        pos += size
        if _BIG_ENDIAN:
            arr.byteswap()
        vals = list(accumulate(arr))
        if nulls:
            vals = [None if v == 0 else v - 1 if v > 0 else v for v in vals]
        cols.append(vals)
    return cols


def encode_states(rows: list[tuple]) -> bytes:
    """rows (snap, *STATE_FIELDS, sold, gone), sorted by snap."""
    return encode([list(c) for c in zip(*rows)] if rows else [[] for _ in STATE_COLS])


def decode_states(blob: bytes) -> list[tuple]:
    return list(zip(*decode(blob)))


def encode_auctions(rows: list[tuple]) -> bytes:
    """rows (*AUCTION_COLS, changes) sorted by auction_id; changes = ((snap, quantity), ...)."""
    cols = [list(c) for c in zip(*(r[:-1] for r in rows))] if rows else [[] for _ in AUCTION_COLS]
    changes = [r[-1] for r in rows]
    cols.append([len(c) for c in changes])
    cols.append([s for c in changes for s, _ in c])
    cols.append([q for c in changes for _, q in c])
    return encode(cols)


def decode_auctions(blob: bytes) -> list[tuple]:
    *cols, counts, snaps, qtys = decode(blob)
    out, pos = [], 0
    for row, n in zip(zip(*cols), counts):
        out.append((*row, tuple(zip(snaps[pos:pos + n], qtys[pos:pos + n]))))
        pos += n
    return out


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------

def pack(conn, market: str, before: int) -> dict:
    """Move a market's item states and ended listings from snapshots before `before` (unix
    seconds) into chunks, in one transaction. Returns counters for logging."""
    mid = store.market_id(conn, market)
    times = dict(conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? AND last_modified < ?",
                              (market, before)))
    done = {"states": 0, "listings": 0, "chunks": 0}
    if not times:
        return done
    try:
        rows = conn.execute(f"""
            SELECT st.item_id, st.variant, {', '.join('st.' + c for c in STATE_COLS)}
            FROM item_states st JOIN snapshots s ON s.id = st.snap
            WHERE st.market_id = ? AND s.market = ? AND s.last_modified < ?
            ORDER BY st.item_id, st.variant, st.snap""", (mid, market, before)).fetchall()
        for (item, variant, week), group in groupby(rows, key=lambda r: (r[0], r[1], times[r[2]] // WEEK)):
            new = [r[2:] for r in group]
            old = conn.execute("SELECT data FROM state_chunks WHERE market_id = ? AND item_id = ? AND variant = ? "
                               "AND week = ?", (mid, item, variant, week)).fetchone()
            merged = sorted({r[0]: r for r in (decode_states(old[0]) if old else []) + new}.values())
            conn.execute("""INSERT INTO state_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market_id, item_id, variant, week) DO UPDATE SET first_snap = excluded.first_snap,
                    last_snap = excluded.last_snap, row_count = excluded.row_count, data = excluded.data""",
                         (mid, item, variant, week, merged[0][0], merged[-1][0], len(merged), encode_states(merged)))
            done["states"] += len(new)
            done["chunks"] += 1
        conn.execute("DELETE FROM item_states WHERE market_id = ? AND snap IN "
                     "(SELECT id FROM snapshots WHERE market = ? AND last_modified < ?)", (mid, market, before))

        ended = """SELECT a.auction_id FROM auctions a JOIN snapshots s ON s.id = a.ended_snap
                   WHERE a.market_id = ? AND s.market = ? AND s.last_modified < ?"""
        changes: dict[int, list] = {}
        for aid, snap, qty in conn.execute(f"""
                SELECT auction_id, snap, quantity FROM auction_changes
                WHERE market_id = ? AND auction_id IN ({ended}) ORDER BY auction_id, snap""",
                                           (mid, mid, market, before)):
            changes.setdefault(aid, []).append((snap, qty))
        listings = conn.execute(f"""
            SELECT a.item_id, {', '.join('a.' + c for c in AUCTION_COLS)}
            FROM auctions a JOIN snapshots s ON s.id = a.ended_snap
            WHERE a.market_id = ? AND s.market = ? AND s.last_modified < ?
            ORDER BY a.item_id, a.auction_id""", (mid, market, before)).fetchall()
        for (item, week), group in groupby(listings, key=lambda r: (r[0], times[r[11]] // WEEK)):
            new = [(*r[1:], tuple(changes.get(r[1], ()))) for r in group]
            old = conn.execute("SELECT data FROM auction_chunks WHERE market_id = ? AND item_id = ? AND week = ?",
                               (mid, item, week)).fetchone()
            merged = sorted({r[0]: r for r in (decode_auctions(old[0]) if old else []) + new}.values())
            conn.execute("""INSERT INTO auction_chunks VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market_id, item_id, week) DO UPDATE SET first_snap = excluded.first_snap,
                    last_snap = excluded.last_snap, row_count = excluded.row_count, data = excluded.data""",
                         (mid, item, week, min(r[9] for r in merged), max(r[10] for r in merged), len(merged),
                          encode_auctions(merged)))
            done["listings"] += len(new)
            done["chunks"] += 1
        conn.execute(f"DELETE FROM auction_changes WHERE market_id = ? AND auction_id IN ({ended})",
                     (mid, mid, market, before))
        conn.execute(f"DELETE FROM auctions WHERE market_id = ? AND auction_id IN ({ended})",
                     (mid, mid, market, before))
        conn.execute("INSERT OR REPLACE INTO archive_packed VALUES (?, ?)", (mid, before))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return done


def pack_due(conn, now: int | None = None, log=print) -> None:
    """Pack every market's finished UTC days (a no-op until the day rolls over)."""
    now = int(time.time()) if now is None else now
    before = now - now % 86400
    packed = dict(conn.execute("SELECT market_id, before FROM archive_packed"))
    for name, mid in store.market_ids(conn).items():
        if packed.get(mid, 0) >= before:
            continue
        t = time.time()
        r = pack(conn, name, before)
        if r["states"] or r["listings"]:
            log(f"{name}: archived {r['states']} item states and {r['listings']} ended listings "
                f"into {r['chunks']} chunks ({time.time() - t:.1f}s)")
        else:
            conn.execute("INSERT OR REPLACE INTO archive_packed VALUES (?, ?)", (mid, before))
            conn.commit()


# ---------------------------------------------------------------------------
# Reading (hot tables + chunks)
# ---------------------------------------------------------------------------

def _in(ids) -> str:
    return ",".join("?" * len(ids))


def states(conn, mid: int, items: list[int] | None = None, variant: int | None = None,
           lo: int | None = None, hi: int | None = None) -> list[tuple]:
    """Item state rows (item_id, variant, snap, *STATE_FIELDS, sold, gone) with lo <= snap <= hi,
    sorted by item, variant, snap. items/variant None = all."""
    lo = 0 if lo is None else lo
    hi = 2 ** 62 if hi is None else hi
    cols = ", ".join(("item_id", "variant", *STATE_COLS))
    out: dict[tuple, tuple] = {}
    batches = [None] if items is None else [items[i:i + 500] for i in range(0, len(items), 500)]
    for batch in batches:
        where = "market_id = ? AND snap BETWEEN ? AND ?"
        params: list = [mid, lo, hi]
        if batch is not None:
            where += f" AND item_id IN ({_in(batch)})"
            params += batch
        if variant is not None:
            where += " AND variant = ?"
            params.append(variant)
        for r in conn.execute(f"SELECT {cols} FROM item_states WHERE {where}", params):
            out[r[:3]] = r
        where = "market_id = ? AND last_snap >= ? AND first_snap <= ?"
        params = [mid, lo, hi]
        if batch is not None:
            where += f" AND item_id IN ({_in(batch)})"
            params += batch
        if variant is not None:
            where += " AND variant = ?"
            params.append(variant)
        for item, var, data in conn.execute(f"SELECT item_id, variant, data FROM state_chunks WHERE {where}", params):
            for r in decode_states(data):
                if lo <= r[0] <= hi:
                    out.setdefault((item, var, r[0]), (item, var, *r))
    return sorted(out.values())


def last_state(conn, mid: int, item: int, variant: int | None, at: int, priced: bool = False) -> tuple | None:
    """The item's latest state row with snap <= at (with a min_price when priced), as in states().
    With variant None, the lowest variant wins a tie."""
    cols = ", ".join(("item_id", "variant", *STATE_COLS))
    where = "market_id = ? AND item_id = ? AND snap <= ?" + (" AND min_price IS NOT NULL" if priced else "")
    params: list = [mid, item, at]
    if variant is not None:
        where += " AND variant = ?"
        params.append(variant)
    row = conn.execute(f"SELECT {cols} FROM item_states WHERE {where} ORDER BY snap DESC, variant LIMIT 1",
                       params).fetchone()
    if row:
        return row  # hot rows are always newer than packed ones
    where = "market_id = ? AND item_id = ? AND first_snap <= ?"
    params = [mid, item, at]
    if variant is not None:
        where += " AND variant = ?"
        params.append(variant)
    best = best_week = None
    for var, week, data in conn.execute(
            f"SELECT variant, week, data FROM state_chunks WHERE {where} ORDER BY week DESC", params):
        if best_week is not None and week < best_week:
            break  # every variant's later weeks have been seen
        for r in reversed(decode_states(data)):
            if r[0] <= at and (not priced or r[1] is not None):
                if best is None or (r[0], -var) > (best[2], -best[1]):  # latest; lowest variant on ties
                    best, best_week = (item, var, *r), week
                break
    return best


def auctions(conn, mid: int, item: int | None = None, up_at: int | None = None, cache: dict | None = None) -> list[tuple]:
    """Listings (item_id, *AUCTION_COLS, changes) of a market, hot and archived, sorted by
    auction_id; changes = ((snap, quantity), ...). item None = every item; up_at = only those
    up in that snapshot. `cache` keeps decoded chunks between calls."""
    where, params = "a.market_id = ?", [mid]
    if item is not None:
        where += " AND a.item_id = ?"
        params.append(item)
    if up_at is not None:
        where += " AND a.first_snap <= ? AND (a.ended_snap IS NULL OR a.ended_snap > ?)"
        params += [up_at, up_at]
    out: dict[int, tuple] = {}
    rows = conn.execute(f"SELECT a.item_id, {', '.join('a.' + c for c in AUCTION_COLS)} FROM auctions a WHERE {where}",
                        params).fetchall()
    changes: dict[int, list] = {}
    if rows:
        for aid, snap, qty in conn.execute(f"""
                SELECT c.auction_id, c.snap, c.quantity FROM auction_changes c
                WHERE c.market_id = ? AND c.auction_id IN (SELECT a.auction_id FROM auctions a WHERE {where})
                ORDER BY c.auction_id, c.snap""", [mid, *params]):
            changes.setdefault(aid, []).append((snap, qty))
    for r in rows:
        out[r[1]] = (*r, tuple(changes.get(r[1], ())))
    where, params = "market_id = ?", [mid]
    if item is not None:
        where += " AND item_id = ?"
        params.append(item)
    if up_at is not None:
        where += " AND first_snap <= ? AND last_snap > ?"
        params += [up_at, up_at]
    for rowid, n, chunk_item, data in conn.execute(
            f"SELECT rowid, row_count, item_id, data FROM auction_chunks WHERE {where}", params):
        decoded = cache.get((rowid, n)) if cache is not None else None
        if decoded is None:
            decoded = decode_auctions(data)
            if cache is not None:
                cache[(rowid, n)] = decoded
        for r in decoded:
            if up_at is None or r[9] <= up_at < r[10]:
                out.setdefault(r[0], (chunk_item, *r))
    return [out[k] for k in sorted(out)]


def quantity_at(listing: tuple, snap: int) -> int:
    """A listing's quantity in snapshot `snap` (from auctions())."""
    qty = listing[6]
    for s, q in listing[-1]:
        if s > snap:
            break
        qty = q
    return qty
