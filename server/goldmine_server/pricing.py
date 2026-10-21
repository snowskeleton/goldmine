"""Price model and reports built on collected snapshots, in-game scans, recipes, and the ledger.

Definitions (all copper per unit):
  market value  time-weighted average of the 10th-percentile price over the window — resistant
                to single cheap listings and to short spikes
  current min   lowest price in the freshest data (API snapshot or your own in-game scan)
  buy price     what restocking costs now: freshest p10 (falls back to min, then market value)
  sell price    what you can realistically list at: min(market value, current min)
  sold/day      units per day that left the AH early: definite partial purchases plus listings
                that vanished with time to spare (sold or cancelled, so an upper bound)

Market data comes from store.py's change-based tables: item states are replayed from the
daily keyframe at or before the window start.

Forever note: the AH is shared per region + realm type, so configure one market per
region/type and these numbers apply to every character on it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from . import archive, store

AH_CUT = 0.05  # auction house takes 5% of the sale price


@dataclass
class Price:
    item_id: int
    variant: int = 0
    market_value: int | None = None
    current_min: int | None = None
    current_p10: int | None = None
    sold_per_day: float = 0.0
    as_of: int | None = None
    sources: set = field(default_factory=set)

    @property
    def buy(self) -> int | None:
        return self.current_p10 or self.current_min or self.market_value

    @property
    def sell(self) -> int | None:
        options = [p for p in (self.market_value, self.current_min) if p]
        return min(options) if options else None


def replay(conn, market: str, since: int):
    """Item states for a market from the keyframe at/before `since`.
    -> (snapshot times {id: last_modified}, {(item, variant): [(snap, state, sold, gone)]})"""
    start = store.window_start_snapshot(conn, market, since)
    mid = store.market_ids(conn, [market]).get(market)
    if start is None or mid is None:
        return {}, {}
    times = dict(conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? AND id >= ?", (market, start)))
    items: dict[tuple[int, int], list] = {}
    for item, variant, snap, *rest in archive.states(conn, mid, lo=start):
        items.setdefault((item, variant), []).append((snap, tuple(rest[:8]), rest[8], rest[9]))
    return times, items


def market_stats(conn, market: str, since: int) -> dict[tuple[int, int], tuple]:
    """Per item over [since, latest snapshot]: (time-weighted p10, units sold per day)."""
    times, items = replay(conn, market, since)
    in_window = sorted(t for t in times.values() if t >= since)
    if not in_window:
        return {}
    last = in_window[-1]
    end = last + 3600  # the latest state holds until (roughly) the next hourly snapshot
    days = (last - in_window[0]) / 86400 if len(in_window) > 1 else 0
    out = {}
    for key, states in items.items():
        weighted = weight = 0.0
        moved = 0
        for i, (snap, state, sold, gone) in enumerate(states):
            t0 = times[snap]
            t1 = times[states[i + 1][0]] if i + 1 < len(states) else end
            if t0 > since:
                moved += sold + gone
            lo, hi = max(t0, since), min(t1, end)
            p10, qty = state[1], state[3]
            if hi > lo and p10 is not None and qty > 0:
                weighted += p10 * (hi - lo)
                weight += hi - lo
        mv = int(weighted / weight) if weight else None
        out[key] = (mv, moved / days if days else 0.0)
    return out


# market_stats replays every item state in the window, which is most of a report's time, yet its
# result only changes when the market gets a new snapshot. Cache it per (database, market, window
# start to the nearest STATS_BUCKET seconds, latest snapshot); concurrent requests for the same key
# wait for one computation instead of each doing it.
STATS_BUCKET = 600
STATS_CACHE_SIZE = 32
_stats_cache: OrderedDict = OrderedDict()
_stats_locks: dict = {}
_stats_guard = threading.Lock()


def _db_file(conn) -> str:
    return next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")


def cached_market_stats(conn, market: str, since: int) -> dict[tuple[int, int], tuple]:
    path = _db_file(conn)
    if not path:  # in-memory databases can't be told apart; just compute
        return market_stats(conn, market, since)
    latest = conn.execute("SELECT MAX(id) FROM snapshots WHERE market = ?", (market,)).fetchone()[0]
    key = (path, market, since // STATS_BUCKET, latest)
    with _stats_guard:
        if key in _stats_cache:
            _stats_cache.move_to_end(key)
            return _stats_cache[key]
        lock = _stats_locks.setdefault(key, threading.Lock())
    with lock:
        with _stats_guard:
            if key in _stats_cache:
                return _stats_cache[key]
        stats = market_stats(conn, market, since)
        with _stats_guard:
            # A newer snapshot makes this market's older entries dead weight.
            for old in [k for k in _stats_cache if k[:2] == key[:2] and k[3] != latest]:
                del _stats_cache[old]
            _stats_cache[key] = stats
            while len(_stats_cache) > STATS_CACHE_SIZE:
                _stats_cache.popitem(last=False)
            _stats_locks.pop(key, None)
        return stats


def price_table(conn, window_hours: int = 72, markets: list[str] | None = None,
                now: int | None = None, scan_chars: list[str] | None = None) -> dict[tuple[int, int], Price]:
    """markets: limit to these market names (None = all). scan_chars: only use in-game scans
    from these characters (None = all), so scans from one game version don't leak into another."""
    now = now or int(time.time())
    since = now - window_hours * 3600
    table: dict[tuple[int, int], Price] = {}
    mids = store.market_ids(conn, markets)

    # Smoothed value and sales per market; when an item is in several markets keep the busiest.
    for mkt in mids:
        for key, (mv, per_day) in cached_market_stats(conn, mkt, since).items():
            cur = table.get(key)
            if cur and cur.sold_per_day >= per_day:
                continue
            table[key] = Price(key[0], key[1], market_value=mv, sold_per_day=per_day, sources={mkt})

    # Current prices from each market's latest state.
    for mkt, mid in mids.items():
        for item, variant, mn, p10, lm in conn.execute("""
                SELECT c.item_id, c.variant, c.min_price, c.p10, s.last_modified
                FROM item_current c JOIN snapshots s ON s.id = (SELECT MAX(id) FROM snapshots WHERE market = ?)
                WHERE c.market_id = ? AND s.last_modified >= ?""", (mkt, mid, since)):
            pr = table.setdefault((item, variant), Price(item, variant, sources={mkt}))
            if mkt in pr.sources and mn is not None:
                pr.current_min, pr.current_p10, pr.as_of = mn, p10, lm

    # Your own in-game scans override "current" when they're fresher than the API snapshot.
    for item, variant, ts, mn, p10 in _latest_scans(conn, since, scan_chars):
        pr = table.setdefault((item, variant), Price(item, variant))
        if pr.as_of is None or ts > pr.as_of:
            pr.current_min, pr.current_p10, pr.as_of = mn, p10, ts
            pr.sources.add("scan")
            if pr.market_value is None:
                pr.market_value = p10
    return table


def _latest_scans(conn, since: int, chars: list[str] | None = None):
    """Latest in-game scan per item -> (item, variant, ts, min, p10)."""
    if chars is not None and not chars:
        return
    char_filter = f" AND s.char_key IN ({','.join('?' * len(chars))})" if chars else ""
    rows = conn.execute(f"""
        SELECT r.item_id, r.variant, s.ts, r.unit_price, r.quantity, s.kind
        FROM scan_rows r JOIN scans s ON s.id = r.scan_id
        WHERE s.ts >= ?{char_filter} ORDER BY r.item_id, r.variant, s.ts""", (since, *(chars or []))).fetchall()
    latest: dict[tuple[int, int], tuple[int, list]] = {}
    for item, variant, ts, price, qty, kind in rows:
        key = (item, variant)
        cur = latest.get(key)
        if cur is None or ts > cur[0]:
            latest[key] = (ts, [(price, qty)])
        elif ts == cur[0]:
            cur[1].append((price, qty))
    for (item, variant), (ts, pairs) in latest.items():
        pairs.sort()
        total = sum(q for _, q in pairs) or 1
        seen, p10 = 0, pairs[-1][0]
        for price, q in pairs:
            seen += q
            if seen >= total * 0.10:
                p10 = price
                break
        yield item, variant, ts, pairs[0][0], p10


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@dataclass
class Craft:
    recipe_id: int
    name: str
    profession: str
    crafters: list[str]
    output_item: int
    output_qty: float
    cost: int | None
    revenue: int | None
    sold_per_day: float
    missing: list[int]

    @property
    def profit(self) -> int | None:
        if self.cost is None or self.revenue is None:
            return None
        return self.revenue - self.cost


def craft_report(conn, prices: dict, chars: list[str] | None = None) -> list[Craft]:
    """Recipes known by `chars` (None = everyone), priced with `prices`."""
    recipes: dict[int, dict] = {}
    for char_key, rid, prof, name, out, qty in conn.execute(
            "SELECT char_key, recipe_id, profession, name, output_item, output_qty FROM recipes"):
        if chars is not None and char_key not in chars:
            continue
        r = recipes.setdefault(rid, {"name": name, "prof": prof, "out": out, "qty": qty or 1,
                                     "crafters": [], "reagents": {}})
        r["crafters"].append(char_key)
    for char_key, rid, item, qty in conn.execute("SELECT char_key, recipe_id, item_id, quantity FROM recipe_reagents"):
        if rid in recipes and char_key in recipes[rid]["crafters"]:
            recipes[rid]["reagents"][item] = qty  # same across crafters

    out = []
    for rid, r in recipes.items():
        cost, missing = 0, []
        for item, qty in r["reagents"].items():
            p = prices.get((item, 0))
            if p and p.buy:
                cost += p.buy * qty
            else:
                missing.append(item)
        op = prices.get((r["out"], 0))
        revenue = int(op.sell * r["qty"] * (1 - AH_CUT)) if op and op.sell else None
        out.append(Craft(rid, r["name"], r["prof"], sorted(set(r["crafters"])), r["out"], r["qty"],
                         None if missing else cost, revenue, op.sold_per_day if op else 0.0, missing))
    out.sort(key=lambda c: (c.profit is None, -(c.profit or 0)))
    return out


@dataclass
class Deal:
    item_id: int
    variant: int
    current_min: int
    market_value: int
    sold_per_day: float

    @property
    def discount(self) -> float:
        return 1 - self.current_min / self.market_value

    @property
    def profit_per_unit(self) -> int:
        return int(self.market_value * (1 - AH_CUT)) - self.current_min


def deal_report(prices: dict, max_ratio: float = 0.7, min_sold_per_day: float = 1.0) -> list[Deal]:
    """Items listed well below market value that actually sell."""
    deals = [Deal(p.item_id, p.variant, p.current_min, p.market_value, p.sold_per_day)
             for p in prices.values()
             if p.current_min and p.market_value and p.current_min <= p.market_value * max_ratio
             and p.sold_per_day >= min_sold_per_day]
    deals.sort(key=lambda d: -d.profit_per_unit * min(d.sold_per_day, 20))
    return deals


def norm_realm(name: str) -> str:
    """'Moon Guard' / 'MoonGuard' / "Kel'Thuzad" -> comparable key."""
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


@dataclass
class MarketSet:
    """What one character sees: their realm's AH plus their game's region-wide commodities.
    kind 'commodities' sets hold the commodity market alone (shared by every realm of a game)."""
    kind: str                       # realm | commodities
    game: str
    region: str
    name: str                       # base market name
    markets: list[str]              # market names (classic houses expand to name:house)
    realms: list[str] = field(default_factory=list)
    chars: list[str] = field(default_factory=list)


def build_sets(conn, cfg: dict) -> list[MarketSet]:
    """Group configured markets into per-realm and per-game commodity sets, with the
    characters (from the ledger) that live on each realm."""
    enabled = {m["name"] for m in cfg.get("market", []) if m.get("pricing", True)} if cfg.get("market") else None
    rows = conn.execute("SELECT name, region, kind, game, realms FROM markets").fetchall()
    chars = conn.execute("SELECT key, realm, region FROM characters").fetchall()
    realm_sets: dict[str, MarketSet] = {}
    commodity_sets: dict[tuple[str, str], MarketSet] = {}
    for name, region, kind, game, realms in rows:
        base = name.split(":")[0]
        if enabled is not None and base not in enabled:
            continue
        game = game or "retail"
        if kind == "commodities":
            cs = commodity_sets.setdefault((game, region), MarketSet("commodities", game, region, base, []))
            cs.markets.append(name)
            continue
        rs = realm_sets.setdefault(base, MarketSet("realm", game, region, base, [], json.loads(realms or "[]")))
        rs.markets.append(name)
    for rs in realm_sets.values():
        keys = {norm_realm(r) for r in rs.realms}
        rs.chars = sorted(k for k, realm, region in chars
                          if norm_realm(realm) in keys and (not region or region.lower() == rs.region.lower()))
    for (game, region), cs in commodity_sets.items():
        cs.chars = sorted({c for rs in realm_sets.values() if rs.game == game and rs.region == region
                           for c in rs.chars})
    return list(realm_sets.values()) + list(commodity_sets.values())


def commodity_set_for(sets: list[MarketSet], rs: MarketSet) -> MarketSet | None:
    return next((s for s in sets if s.kind == "commodities" and s.game == rs.game and s.region == rs.region), None)


# ---------------------------------------------------------------------------
# Timing: when things sell (hour of week)
# ---------------------------------------------------------------------------

def timing(conn, item_id: int, variant: int = 0, markets: list[str] | None = None,
           since: int | None = None) -> dict[str, list[dict]]:
    """Hour-of-week pattern per market for one item, replayed exactly from item states at every
    snapshot since the item first appeared. Buckets are UTC (how = weekday*24 + hour,
    0 = Sunday 00h). Returns {market: [168 dicts: samples, sold, sold_avg, p10_avg, min, qty_avg]}."""
    out = {}
    for mkt, mid in store.market_ids(conn, markets).items():
        states = [r[2:] for r in archive.states(conn, mid, [item_id], variant)]
        if not states:
            continue
        snaps = store.snapshot_times(conn, mkt, since)
        acc = [[0, 0, 0, 0, None, 0] for _ in range(168)]  # samples, sold, p10 sum, p10 n, min, qty sum
        i, state = 0, None
        for sid, lm, _ in snaps:
            moved = 0
            while i < len(states) and states[i][0] <= sid:
                state = states[i]
                if state[0] == sid:
                    moved = state[9] + state[10]
                i += 1
            if state is None:
                continue  # item hadn't appeared yet
            t = time.gmtime(lm)
            b = acc[((t.tm_wday + 1) % 7) * 24 + t.tm_hour]  # tm_wday: Monday = 0
            b[0] += 1
            b[1] += moved
            mn, p10, qty = state[1], state[2], state[4]
            if p10 is not None and qty:
                b[2] += p10
                b[3] += 1
            if mn is not None and qty:
                b[4] = mn if b[4] is None else min(b[4], mn)
            b[5] += qty
        out[mkt] = [{"samples": n, "sold": sold, "sold_avg": sold / n if n else None,
                     "p10_avg": int(ps / pn) if pn else None, "min": mn, "qty_avg": q / n if n else None}
                    for n, sold, ps, pn, mn, q in acc]
    return out


def to_local(buckets: list, utc_offset_hours: int) -> list:
    """Rotate 168 UTC buckets into local time (index 0 = local Sunday 00:00)."""
    return [buckets[(i - utc_offset_hours) % 168] for i in range(168)]


# ---------------------------------------------------------------------------
# Flips: commodities that swing between their own lows and highs
# ---------------------------------------------------------------------------

@dataclass
class Flip:
    item_id: int
    variant: int
    p25: int
    p75: int
    median: int
    trips: int              # completed round trips: touched <= p25, later >= p75
    drift: float            # median of the last quarter of the window vs the first quarter
    depth_low: int | None   # median units within 5% of the min while the price was at/below p25
    sold_per_day: float     # definite partial purchases
    moved_per_day: float    # plus listings that vanished early (sold or cancelled: an upper bound)
    samples: int

    @property
    def margin(self) -> float:
        """Net return buying at p25 and selling at p75, after the AH cut."""
        return (self.p75 * (1 - AH_CUT) - self.p25) / self.p25

    @property
    def buy_below(self) -> int:
        return self.p25

    @property
    def sell_at(self) -> int:
        return self.p75

    @property
    def profit_per_trip(self) -> int:
        """Buying what sits near the lows (capped at a day's sales) and selling it at p75."""
        units = min(self.depth_low or 0, self.moved_per_day)
        return int(units * (self.p75 * (1 - AH_CUT) - self.p25))


def _quantile(sorted_vals: list, q: float):
    return sorted_vals[min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))]


def hourly_series(conn, market: str, since: int, until: int | None = None):
    """Each item's state at every snapshot in [since, until], carried forward from its last row
    (an item's row holds until its next one). -> (snapshot times, {(item, variant): [(t, state, sold, gone)]})
    where state is in store.STATE_FIELDS order; sold/gone are what left the AH since the previous snapshot."""
    times, items = replay(conn, market, since)
    snaps = sorted((t, sid) for sid, t in times.items() if t >= since and (until is None or t <= until))
    out = {}
    for key, rows in items.items():
        series, i, state = [], 0, None
        for t, sid in snaps:
            sold = gone = 0
            while i < len(rows) and rows[i][0] <= sid:
                state = rows[i][1]
                if rows[i][0] == sid:
                    sold, gone = rows[i][2], rows[i][3]
                i += 1
            if state is not None:
                series.append((t, state, sold, gone))
        if series:
            out[key] = series
    return [t for t, _ in snaps], out


def flip_report(conn, market: str = "us-commodities", days: float = 7, now: int | None = None,
                min_trips: int = 2, min_margin: float = 0.03, min_moved_per_day: float = 2000,
                min_gold_per_day: int = 20_000_0000, min_presence: float = 0.9):
    """Commodities that repeatedly round-trip between their own P25 and P75 min price.
    Liquid = listed in at least `min_presence` of the snapshots, moving `min_moved_per_day` units and
    `min_gold_per_day` copper a day. `now` defaults to the latest snapshot, so the window ends at the data.
    -> (flips sorted by trips x profit per trip, 7x24 price index, number of liquid items)
    The index is [weekday 0=Sunday][UTC hour]: each liquid item's min price over its own window median,
    the median across items (robust to the odd item selling out and spiking; None where there were no samples)."""
    if now is None:
        now = conn.execute("SELECT MAX(last_modified) FROM snapshots WHERE market = ?", (market,)).fetchone()[0]
    if now is None:
        return [], [[None] * 24 for _ in range(7)], 0
    since = now - int(days * 86400)
    snaps, series = hourly_series(conn, market, since, now)
    span_days = (snaps[-1] - snaps[0]) / 86400 if len(snaps) > 1 else 0
    acc = [[[] for _ in range(24)] for _ in range(7)]
    flips, liquid = [], 0
    for (item, variant), rows in series.items():
        priced = [(t, st[0], st[5]) for t, st, _, _ in rows if st[0] is not None and st[3] > 0]
        if not span_days or len(priced) < max(4, min_presence * len(snaps)):
            continue
        sold = sum(s for t, _, s, _ in rows if t > snaps[0])
        moved = sold + sum(g for t, _, _, g in rows if t > snaps[0])
        prices = sorted(p for _, p, _ in priced)
        median = _quantile(prices, 0.5)
        if moved / span_days < min_moved_per_day or moved / span_days * median < min_gold_per_day:
            continue
        liquid += 1
        for t, p, _ in priced:
            g = time.gmtime(t)
            acc[(g.tm_wday + 1) % 7][g.tm_hour].append(p / median)
        p25, p75 = _quantile(prices, 0.25), _quantile(prices, 0.75)
        trips, holding = 0, False
        for _, p, _ in priced if p75 > p25 else ():
            if not holding and p <= p25:
                holding = True
            elif holding and p >= p75:
                trips, holding = trips + 1, False
        q = max(1, len(priced) // 4)
        first, last = sorted(p for _, p, _ in priced[:q]), sorted(p for _, p, _ in priced[-q:])
        lows = sorted(d for _, p, d in priced if p <= p25 and d is not None)
        f = Flip(item, variant, p25, p75, median, trips, _quantile(last, 0.5) / _quantile(first, 0.5) - 1,
                 _quantile(lows, 0.5) if lows else None, sold / span_days, moved / span_days, len(priced))
        if trips >= min_trips and f.margin >= min_margin:
            flips.append(f)
    flips.sort(key=lambda f: -f.trips * f.profit_per_trip)
    index = [[_quantile(sorted(b), 0.5) if b else None for b in row] for row in acc]
    return flips, index, liquid


# ---------------------------------------------------------------------------
# Export to the addon (Data.lua, loaded on login / /reload)
# ---------------------------------------------------------------------------

def _lua(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


@dataclass
class SetExport:
    set: MarketSet
    prices: dict
    crafts: list = field(default_factory=list)
    deals: list = field(default_factory=list)


def write_addon_data(path, exports: list[SetExport], now: int | None = None) -> int:
    """One entry per market set; the addon picks its realm's set and its game's commodity set."""
    now = now or int(time.time())
    lines = ["-- Generated by goldmine.py push. Do not edit; it's overwritten.",
             "Goldmine_Data = {", f"  generated = {now},", "  sets = {"]
    n = 0
    for ex in exports:
        st = ex.set
        lines.append("    {")
        lines.append(f"      kind = {_lua(st.kind)}, game = {_lua(st.game)}, region = {_lua(st.region)}, "
                     f"name = {_lua(st.name)},")
        lines.append("      realms = {" + ", ".join(_lua(r) for r in st.realms) + "},")
        lines.append("      prices = {")
        for (item, variant), p in sorted(ex.prices.items()):
            if variant or not (p.market_value or p.current_min):
                continue  # tooltips key on item id; pets are skipped for now
            lines.append(f"        [{item}] = {{{_lua(p.market_value)}, {_lua(p.current_min)}, "
                         f"{_lua(round(p.sold_per_day, 1))}}},")
            n += 1
        lines.append("      },")
        lines.append("      crafts = {")
        for c in [c for c in ex.crafts if c.profit is not None][:30]:
            lines.append(f"        {{{c.recipe_id}, {_lua(c.name)}, {c.output_item}, {c.cost}, {c.revenue}, "
                         f"{_lua(round(c.sold_per_day, 1))}, {_lua(', '.join(c.crafters))}}},")
        lines.append("      },")
        lines.append("      deals = {")
        for d in ex.deals[:30]:
            lines.append(f"        {{{d.item_id}, {d.current_min}, {d.market_value}, "
                         f"{_lua(round(d.sold_per_day, 1))}}},")
        lines.append("      },")
        lines.append("    },")
    lines += ["  },", "}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n
