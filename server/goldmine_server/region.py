"""Region baselines: what an item goes for across a region's realm auction houses.

Bigger auction houses list for much less: on US retail (Oct 2026), doubling a realm's listings
takes its prices to ~70%, and FULL realms list at ~0.6x the region median. A plain region average
makes nearly everything on a big realm look cheap, so the baseline is adjusted for size.

After each collection run, rollup() writes two series per region and game version, as markets of
kind "region" in the change-based tables (so they get history, archiving and charts like any
market):

  <region>-<game>-region      each realm's prices rescaled to the median realm size, then the
                              median across realms: the size-adjusted baseline
  <region>-<game>-region-raw  the plain median across realms (what TSM calls the region price)

Their state fields: min_price / p10 / median = medians across realms of each realm's own;
quantity / depth = totals; auctions = how many realms list the item; sold / gone = totals since
the previous rollup. Only items priced on at least MIN_LISTING realms get a series. region_scales keeps each rollup's reference size and size exponent, so a
realm's expected price is region p10 x (its listings / ref_size) ** beta, at any time.
"""

from __future__ import annotations

import bisect
import math
import statistics
import time
from itertools import groupby

from . import archive, store

SCHEMA = """
-- One row per rollup of a region's size-adjusted series (snap = its snapshot in that market).
CREATE TABLE IF NOT EXISTS region_scales (
    snap          INTEGER PRIMARY KEY,
    ref_size      INTEGER NOT NULL,     -- median listings per realm: prices are rescaled to this size
    beta          REAL NOT NULL,        -- price ~ listings ** beta (0 = no size effect)
    through_snap  INTEGER NOT NULL,     -- the newest realm snapshot id included
    members       INTEGER NOT NULL      -- realm auction houses included
);
"""

MIN_MEMBERS = 3        # fewer realms than this: no region series
MIN_LISTING = 3        # an item (or pet breed / gear bonus set) needs priced listings on this many realms
FIT_MIN_REALMS = 8     # fewer than this, or too few shared items: no size adjustment (beta 0)
FIT_MIN_ITEMS = 50
FRESH = 6 * 3600       # realms whose latest snapshot is older than the newest by more are left out


def names(region: str, game: str) -> tuple[str, str]:
    """(size-adjusted, raw) region market names."""
    return f"{region}-{game}-region", f"{region}-{game}-region-raw"


def _members(conn, region: str, game: str) -> list[tuple]:
    """[(market name, mid, latest snapshot id, its last_modified, its listings)] of fresh realm AHs."""
    rows = []
    for name, mid in conn.execute("SELECT name, mid FROM markets WHERE kind IN ('realm', 'classic') AND region = ? "
                                  "AND COALESCE(game, 'retail') = ? AND mid IS NOT NULL", (region, game)):
        snap = conn.execute("SELECT id, last_modified, auctions FROM snapshots WHERE market = ? "
                            "ORDER BY last_modified DESC LIMIT 1", (name,)).fetchone()
        if snap and snap[2]:
            rows.append((name, mid, *snap))
    newest = max((r[3] for r in rows), default=0)
    return [r for r in rows if r[3] >= newest - FRESH]


def size_exponent(conn, members: list[tuple]) -> float:
    """Fit price ~ listings ** beta across realms, from items most realms list (item-wide series).
    Each realm's level is its median log(p10 / the item's cross-realm median)."""
    need = max(FIT_MIN_REALMS, int(len(members) * 0.6))
    if len(members) < FIT_MIN_REALMS:
        return 0.0
    mids = {m[1]: i for i, m in enumerate(members)}
    devs: list[list[float]] = [[] for _ in members]
    items = 0
    rows = conn.execute(f"""SELECT item_id, market_id, p10 FROM item_current
                            WHERE variant = 0 AND p10 > 0 AND market_id IN ({','.join('?' * len(mids))})
                            ORDER BY item_id""", list(mids))
    for _, group in groupby(rows, key=lambda r: r[0]):
        group = list(group)
        if len(group) < need:
            continue
        logs = [math.log(p) for _, _, p in group]
        center = statistics.median(logs)
        for (_, mid, _), lp in zip(group, logs):
            devs[mids[mid]].append(lp - center)
        items += 1
    if items < FIT_MIN_ITEMS:
        return 0.0
    xs = [math.log(m[4]) for m, d in zip(members, devs) if d]
    ys = [statistics.median(d) for d in devs if d]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.0
    return max(-1.0, min(0.0, beta))


def _median(vals: list) -> int | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals)) if vals else None


def rollup(conn, region: str, game: str, log=print) -> dict | None:
    """Write the region's two series for its realms' latest states (if any realm has new data)."""
    members = _members(conn, region, game)
    adj_name, raw_name = names(region, game)
    if len(members) < MIN_MEMBERS:
        return None
    through = max(m[2] for m in members)
    adj_mid = store.market_id(conn, adj_name)
    last = conn.execute("SELECT MAX(r.through_snap) FROM region_scales r JOIN snapshots s ON s.id = r.snap "
                        "WHERE s.market = ?", (adj_name,)).fetchone()[0]
    if last is not None and through <= last:
        return None
    started = time.time()
    namespace = conn.execute("SELECT namespace FROM markets WHERE mid = ?", (members[0][1],)).fetchone()[0]
    raw_mid = store.market_id(conn, raw_name)
    for name in (adj_name, raw_name):
        conn.execute("UPDATE markets SET region = ?, namespace = ?, kind = 'region', game = ?, realms = '[]' "
                     "WHERE name = ?", (region, namespace, game, name))

    beta = size_exponent(conn, members)
    ref = round(statistics.median(m[4] for m in members))
    factor = {m[1]: (ref / m[4]) ** beta for m in members}
    member_ids = list(factor)
    marks = ",".join("?" * len(member_ids))

    adjusted: dict[tuple[int, int], tuple] = {}
    raw: dict[tuple[int, int], tuple] = {}
    rows = conn.execute(f"""SELECT item_id, variant, market_id, {', '.join(store.STATE_FIELDS)} FROM item_current
                            WHERE market_id IN ({marks}) ORDER BY item_id, variant""", member_ids)
    for key, group in groupby(rows, key=lambda r: (r[0], r[1])):
        group = list(group)
        priced = [r for r in group if r[4] is not None]
        if len(priced) < MIN_LISTING:
            continue
        totals = (sum(r[6] for r in group), len(group),
                  *(sum(r[i] or 0 for r in group) or None for i in (8, 9, 10)))
        for out, scale in ((raw, lambda r: 1.0), (adjusted, lambda r: factor[r[2]])):
            out[key] = (*(_median([r[i] * scale(r) for r in priced]) for i in (3, 4, 5)), *totals)
    sold, gone = {}, {}
    since = last or 0
    for item, variant, s, g in conn.execute(
            f"""SELECT item_id, variant, SUM(sold), SUM(gone) FROM item_states WHERE market_id IN ({marks})
                AND snap > ? AND snap <= ? AND (sold > 0 OR gone > 0) GROUP BY item_id, variant""",
            [*member_ids, since, through]):
        sold[(item, variant)], gone[(item, variant)] = s, g

    lm = max(m[3] for m in members)
    listings = sum(m[4] for m in members)
    written = 0
    for name, mid, states in ((adj_name, adj_mid, adjusted), (raw_name, raw_mid, raw)):
        if conn.execute("SELECT 1 FROM snapshots WHERE market = ? AND last_modified >= ?", (name, lm)).fetchone():
            lm = conn.execute("SELECT MAX(last_modified) FROM snapshots WHERE market = ?", (name,)).fetchone()[0] + 1
        sid, _, keyframe = store.new_snapshot(conn, name, lm, listings)
        written += store.write_states(conn, mid, sid, keyframe, states, sold, gone)
        if name == adj_name:
            conn.execute("INSERT INTO region_scales VALUES (?, ?, ?, ?, ?)", (sid, ref, beta, through, len(members)))
    conn.commit()
    log(f"{adj_name}: {len(members)} realms, {len(adjusted)} items, size exponent {beta:.2f} "
        f"(doubling listings -> prices x{2 ** beta:.2f}); {written} states written ({time.time() - started:.0f}s)")
    return {"members": len(members), "items": len(adjusted), "beta": beta, "ref": ref, "states": written}


def rollup_all(conn, log=print) -> None:
    for region, game in conn.execute("SELECT DISTINCT region, COALESCE(game, 'retail') FROM markets "
                                     "WHERE kind IN ('realm', 'classic')").fetchall():
        rollup(conn, region, game, log)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _region_of(conn, market: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT region, COALESCE(game, 'retail'), kind FROM markets WHERE name = ?", (market,)).fetchone()
    return (row[0], row[1]) if row and row[2] in ("realm", "classic") else None


def _size(conn, market: str, at: int | None = None) -> int | None:
    sql = "SELECT auctions FROM snapshots WHERE market = ?" + (" AND last_modified <= ?" if at else "")
    row = conn.execute(sql + " ORDER BY last_modified DESC LIMIT 1", (market, at) if at else (market,)).fetchone()
    return row[0] if row else None


def expected(conn, market: str, keys=None) -> dict[tuple[int, int], dict]:
    """The region's current view of items on one realm market: {(item, variant): {expected: region
    p10 rescaled to this realm's size, raw: plain region median p10, realms: how many list it}}.
    Empty for markets with no region series (commodities, a region with too few realms)."""
    where = _region_of(conn, market)
    size = _size(conn, market)
    if not where or not size:
        return {}
    adj_name, raw_name = names(*where)
    mids = store.market_ids(conn, [adj_name, raw_name])
    if len(mids) < 2:
        return {}
    scale = conn.execute("SELECT r.ref_size, r.beta FROM region_scales r JOIN snapshots s ON s.id = r.snap "
                         "WHERE s.market = ? ORDER BY r.snap DESC LIMIT 1", (adj_name,)).fetchone()
    if not scale:
        return {}
    k = (size / scale[0]) ** scale[1]
    sql = "SELECT item_id, variant, p10, auctions FROM item_current WHERE market_id = ? AND p10 IS NOT NULL"
    raw = {(i, v): p for i, v, p, _ in conn.execute(sql, (mids[raw_name],))}
    out = {}
    for item, variant, p10, realms in conn.execute(sql, (mids[adj_name],)):
        if keys is None or (item, variant) in keys:
            out[(item, variant)] = {"expected": round(p10 * k), "raw": raw.get((item, variant)), "realms": realms}
    return out


def annotate(conn, market: str, deals: list) -> list:
    """Fill each deal's region_* fields from the region's view of `market` (in place)."""
    view = expected(conn, market, {(d.item_id, d.variant) for d in deals})
    for d in deals:
        v = view.get((d.item_id, d.variant))
        if v:
            d.region_expected, d.region_raw, d.region_realms = v["expected"], v["raw"], v["realms"]
    return deals


def history(conn, market: str, item: int, variant: int, since: int) -> list[dict]:
    """The region lines for one realm market's chart from `since`: [{t, region: size-adjusted to this
    realm (at its size then), region_raw, realms}], each holding until the next."""
    where = _region_of(conn, market)
    if not where:
        return []
    adj_name, raw_name = names(*where)
    mids = store.market_ids(conn, [adj_name, raw_name])
    if len(mids) < 2:
        return []
    points: dict[int, dict] = {}
    for name, key in ((adj_name, "region"), (raw_name, "region_raw")):
        times = dict(conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? AND last_modified >= ?",
                                  (name, since)))
        first = min(times, default=None)
        rows = archive.states(conn, mids[name], [item], variant, first) if first else []
        prev = archive.last_state(conn, mids[name], item, variant, (first or 2 ** 62) - 1)
        if prev:
            points.setdefault(since, {})[key] = prev[4]
            if key == "region":
                points[since]["snap"] = prev[2]
        for r in rows:
            p = points.setdefault(times[r[2]], {})
            p[key] = r[4]
            if key == "region":
                p["snap"], p["realms"] = r[2], r[7]
    # The scale in force at each region snapshot, and this realm's size at each of its own.
    scales = conn.execute("SELECT r.snap, r.ref_size, r.beta FROM region_scales r JOIN snapshots s ON s.id = r.snap "
                          "WHERE s.market = ? ORDER BY r.snap", (adj_name,)).fetchall()
    sizes = conn.execute("SELECT last_modified, auctions FROM snapshots WHERE market = ? ORDER BY last_modified",
                         (market,)).fetchall()
    out, cur = [], {}
    for t in sorted(points):
        cur.update(points[t])
        snap = cur.get("snap")
        j = bisect.bisect_right(scales, (snap, math.inf)) - 1 if snap is not None else -1
        scale = scales[j][1:] if j >= 0 else None
        i = bisect.bisect_right(sizes, (t, math.inf)) - 1
        size = sizes[max(i, 0)][1] if sizes else None
        adj = cur.get("region")
        region = round(adj * (size / scale[0]) ** scale[1]) if adj is not None and scale and size else None
        out.append({"t": t, "region": region, "region_raw": cur.get("region_raw"), "realms": cur.get("realms")})
    return out


def compare(conn, market: str, other: str, item: int, variant: int, since: int) -> list[dict]:
    """Another realm's p10 for one item from `since`, for `market`'s chart: [{t, compare: rescaled to
    `market`'s size (with the region's latest size exponent), compare_raw}], each holding until the next."""
    where = _region_of(conn, market)
    mids = store.market_ids(conn, [other])
    if other not in mids:
        return []
    beta = 0.0
    if where:
        row = conn.execute("SELECT r.beta FROM region_scales r JOIN snapshots s ON s.id = r.snap "
                           "WHERE s.market = ? ORDER BY r.snap DESC LIMIT 1", (names(*where)[0],)).fetchone()
        beta = row[0] if row else 0.0
    times = dict(conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? AND last_modified >= ?",
                              (other, since)))
    first = min(times, default=None)
    points = [(times[r[2]], r[4]) for r in archive.states(conn, mids[other], [item], variant, first)] if first else []
    prev = archive.last_state(conn, mids[other], item, variant, (first or 2 ** 62) - 1)
    if prev:
        points.insert(0, (since, prev[4]))
    mine, theirs = (conn.execute("SELECT last_modified, auctions FROM snapshots WHERE market = ? ORDER BY last_modified",
                                 (m,)).fetchall() for m in (market, other))

    def size(sizes, t):
        i = bisect.bisect_right(sizes, (t, math.inf)) - 1
        return sizes[max(i, 0)][1] if sizes else None

    out = []
    for t, p10 in points:
        a, b = size(mine, t), size(theirs, t)
        adj = round(p10 * (a / b) ** beta) if p10 is not None and a and b else None
        out.append({"t": t, "compare": adj, "compare_raw": p10})
    return out
