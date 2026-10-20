"""Market simulator: score a plan against what actually happened after it was made, and backtest
built-in strategies by generating the plan each would have published at every decision time.

Plans use the task format in docs/trader.md (buy / post / craft / cancel; scan, mail and vendor
are recorded as not simulated). Everything is copper; times are unix seconds.

Fill model, deliberately conservative (each assumption errs toward fewer fills and lower value):

  Execution    A plan decided at time T is executed against the first snapshot of the item's market
               strictly after T: that snapshot stands in for the "live" AH the human sees.
  Buys         Fill only from listings present in that snapshot priced at or below max_unit,
               cheapest first, up to qty. A commodity quote whose average is under max_unit but
               includes units above it is not modelled: units above max_unit never fill. Listings
               the simulation already bought are not bought again. Our buys don't change later
               history (the demand that ate those cheap listings in reality isn't redirected).
  Posts        Price = clamp(lowest remaining listing in the execution snapshot - undercut, floor,
               ceil); below floor -> no post (status below_floor), like the addon. No listings ->
               ceil. A deposit is paid up front and refunded per unit sold (retail AH behaviour);
               unsold units at expiry or cancel lose theirs.
  Post fills   Only definite purchases count: units removed from a listing that stayed up
               (auction_changes, the `sold` signal), never listings that vanished (`gone`: sold
               OR cancelled), unless params.gone_weight > 0 says otherwise. A purchase at price Q
               after our post would have reached our listing at P <= Q (buyers take the cheapest
               first, and we were cheaper). Purchases observed in the snapshot where we posted
               don't count (they may have happened before we listed). Within a snapshot the
               dearest purchases are matched first to our cheapest listings, the pessimistic
               order. Sales below our price never fill us, so undercutters starve us correctly.
               Realm AHs list mostly single items that vanish when bought, so they record almost
               no `sold`: under the default rules realm-AH posts essentially never fill.
  Fees         5% AH cut on every sale. Deposit = vendor sell price x qty x 15% per 12h of
               duration when the vendor price is known (item_info, or params.vendor_prices),
               else params.deposit_fallback_12h (default 0.5%) of the post price per 12h.
  Expiry       Unsold units come back at expiry; evaluate_plan reposts them `reposts` times with
               the same post rule at the next snapshot; backtests leave it to the next session's
               plan (which reposts what's held).
  Crafts       Instant once the reagents are held; output = floor(crafts x output_qty) (no
               multicraft, no resourcefulness), at the cost of the reagents consumed (FIFO).
  Valuation    Unsold inventory is marked at conservative_value (docs/trader.md): min(market
               value, p25 of definite sold prices over 14d) x 0.95 minus one deposit; 0 with no
               definite sales in 14 days; halved for lots held past their max_hold_days.
  Universe     Backtests only consider items that moved at least params.universe_min_moved units
               per day over the whole history window (a mild look-ahead in which items exist;
               every signal and fill uses only data up to its own time).

History is loaded once per call (item_states rows plus prefix sums for time-weighted p10 and
moved/sold volumes), so a 30-day backtest doesn't re-query per step.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field

from . import pricing

DAY, HOUR = 86400, 3600
AH_CUT = pricing.AH_CUT
MARKETS = ("us-commodities", "us-proudmoore")   # an item is priced in the first market that has it
VALUE_DAYS = 14          # conservative value: definite sales in this window
VALUE_HAIRCUT = 0.95
MV_HOURS = 72            # market value window (time-weighted p10), as in pricing
DEPOSIT_RATE_12H = 0.15  # of the vendor sell price, per 12h of duration
DEPOSIT_FALLBACK_12H = 0.005  # of the post price per 12h, when the vendor price is unknown
DURATIONS = (12, 24, 48)


# ---------------------------------------------------------------------------
# History: per-item series with prefix sums
# ---------------------------------------------------------------------------

class Series:
    """One item in one market: its state rows (a row holds until the next one) and prefix sums."""
    __slots__ = ("t", "mn", "p10", "qty", "cum_sold", "cum_moved", "cum_pw", "cum_w")

    def __init__(self, rows):  # rows: (t, min_price, p10, quantity, sold, gone), sorted by t
        self.t = [r[0] for r in rows]
        self.mn = [r[1] for r in rows]
        self.p10 = [r[2] for r in rows]
        self.qty = [r[3] for r in rows]
        self.cum_sold, self.cum_moved, self.cum_pw, self.cum_w = [0], [0], [0.0], [0.0]
        for i, (t, _, p10, qty, sold, gone) in enumerate(rows):
            self.cum_sold.append(self.cum_sold[-1] + sold)
            self.cum_moved.append(self.cum_moved[-1] + sold + gone)
            if i + 1 < len(rows):
                dt = rows[i + 1][0] - t
                ok = p10 is not None and qty > 0
                self.cum_pw.append(self.cum_pw[-1] + (p10 * dt if ok else 0))
                self.cum_w.append(self.cum_w[-1] + (dt if ok else 0))

    def _row(self, at: float) -> int:
        return bisect.bisect_right(self.t, at) - 1

    def _integral(self, x: float) -> tuple[float, float]:
        j = self._row(x)
        if j < 0:
            return 0.0, 0.0
        pw, w = self.cum_pw[j], self.cum_w[j]
        if self.p10[j] is not None and self.qty[j] > 0:
            pw += self.p10[j] * (x - self.t[j])
            w += x - self.t[j]
        return pw, w

    def twa_p10(self, a: float, b: float) -> int | None:
        """Time-weighted p10 over [a, b] while listed (pricing's market value)."""
        pa, wa = self._integral(a)
        pb, wb = self._integral(b)
        return int((pb - pa) / (wb - wa)) if wb > wa else None

    def sold(self, a: float, b: float) -> int:
        """Definite purchases recorded by snapshots in (a, b]."""
        return self.cum_sold[bisect.bisect_right(self.t, b)] - self.cum_sold[bisect.bisect_right(self.t, a)]

    def moved(self, a: float, b: float) -> int:
        """sold + gone recorded by snapshots in (a, b]."""
        return self.cum_moved[bisect.bisect_right(self.t, b)] - self.cum_moved[bisect.bisect_right(self.t, a)]

    def state(self, at: float) -> tuple | None:
        """(min_price, p10, quantity) as of `at`, or None before the item's first row."""
        j = self._row(at)
        return (self.mn[j], self.p10[j], self.qty[j]) if j >= 0 else None


class History:
    """Snapshots and item series for `markets` over [since, until] (since already includes any
    lookback the caller needs). `items` limits the load to those ids; otherwise items moving at
    least `min_moved_per_day` (sold + gone) over the window are loaded."""

    def __init__(self, conn, markets=MARKETS, since: int | None = None, until: int | None = None,
                 items=None, min_moved_per_day: float = 0.0, gone_weight: float = 0.0,
                 vendor_prices: dict | None = None):
        self.conn = conn
        self.gone_weight = gone_weight
        self.markets: list[str] = []
        self.mids: dict[str, int] = {}
        self.snap_t: dict[str, list[int]] = {}
        self.snap_id: dict[str, list[int]] = {}
        self.sid_t: dict[str, dict[int, int]] = {}
        self.series: dict[str, dict[int, Series]] = {}
        self._events: dict[tuple[str, int], tuple[list, list]] = {}
        self._listings: dict[tuple[str, int, int], list] = {}
        self._cv: dict = {}
        items = None if items is None else sorted({int(i) for i in items})
        mids = dict(conn.execute("SELECT name, mid FROM markets WHERE mid IS NOT NULL"))
        for market in markets:
            if market not in mids:
                continue
            self._load(market, mids[market], since, until, items, min_moved_per_day)
        self.until = max((t[-1] for t in self.snap_t.values() if t), default=until)
        self.vendor = self._vendor_prices(items)
        self.vendor.update({int(k): int(v) for k, v in (vendor_prices or {}).items()})

    def _load(self, market, mid, since, until, items, min_moved_per_day):
        until = until if until is not None else 2 ** 62
        start = None
        if since is not None:
            row = self.conn.execute("SELECT id FROM snapshots WHERE market = ? AND keyframe = 1 AND last_modified <= ? "
                                    "ORDER BY last_modified DESC LIMIT 1", (market, since)).fetchone()
            start = row[0] if row else None
        if start is None:
            start = self.conn.execute("SELECT MIN(id) FROM snapshots WHERE market = ?", (market,)).fetchone()[0]
        if start is None:
            return
        snaps = self.conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? AND id >= ? AND "
                                  "last_modified <= ? ORDER BY last_modified", (market, start, until)).fetchall()
        if not snaps:
            return
        self.markets.append(market)
        self.mids[market] = mid
        self.snap_id[market] = [s for s, _ in snaps]
        self.snap_t[market] = [t for _, t in snaps]
        sid_t = self.sid_t[market] = dict(snaps)
        last_sid = max(sid_t)
        cols = "item_id, snap, min_price, p10, quantity, sold, gone"
        base = f"SELECT {cols} FROM item_states WHERE market_id = ? AND variant = 0 AND snap >= ? AND snap <= ?"
        if items is not None:
            rows = []
            for i in range(0, len(items), 500):
                chunk = items[i:i + 500]
                rows += self.conn.execute(base + f" AND item_id IN ({','.join('?' * len(chunk))})",
                                          (mid, start, last_sid, *chunk)).fetchall()
            rows.sort(key=lambda r: (r[0], r[1]))
        else:
            days = max((snaps[-1][1] - snaps[0][1]) / DAY, 1 / 24)
            rows = self.conn.execute(
                base + " AND item_id IN (SELECT item_id FROM item_states WHERE market_id = ? AND variant = 0 "
                "AND snap > ? AND snap <= ? GROUP BY item_id HAVING SUM(sold + gone) >= ?) ORDER BY item_id, snap",
                (mid, start, last_sid, mid, start, last_sid, min_moved_per_day * days)).fetchall()
        out: dict[int, Series] = {}
        cur, buf = None, []
        for item, snap, mn, p10, qty, sold, gone in rows:
            if item != cur:
                if buf:
                    out[cur] = Series(buf)
                cur, buf = item, []
            buf.append((sid_t[snap], mn, p10, qty, sold, gone))
        if buf:
            out[cur] = Series(buf)
        self.series[market] = out

    def _vendor_prices(self, items) -> dict[int, int]:
        try:  # item_info comes from the account-state branch; absent before that's merged
            sql = "SELECT item_id, sell_price FROM item_info WHERE sell_price IS NOT NULL"
            if items is not None and len(items) <= 900:
                return dict(self.conn.execute(sql + f" AND item_id IN ({','.join('?' * len(items))})", items))
            return dict(self.conn.execute(sql))
        except sqlite3.OperationalError:
            return {}

    # --- lookups ---

    def market_of(self, item: int) -> str | None:
        return next((m for m in self.markets if item in self.series[m]), None)

    def get(self, market: str, item: int) -> Series | None:
        return self.series.get(market, {}).get(item)

    def snaps_between(self, market: str, a: float, b: float) -> list[int]:
        """Snapshot times in [a, b]."""
        ts = self.snap_t.get(market, [])
        return ts[bisect.bisect_left(ts, a):bisect.bisect_right(ts, b)]

    def all_snaps_between(self, a: float, b: float) -> list[int]:
        """Snapshot times of every market in (a, b], merged."""
        out = set()
        for ts in self.snap_t.values():
            out.update(ts[bisect.bisect_right(ts, a):bisect.bisect_right(ts, b)])
        return sorted(out)

    def next_snap(self, market: str, after: float) -> tuple[int, int] | None:
        """(snapshot id, time) of the first snapshot strictly after `after`."""
        ts = self.snap_t.get(market, [])
        i = bisect.bisect_right(ts, after)
        return (self.snap_id[market][i], ts[i]) if i < len(ts) else None

    def latest(self) -> int | None:
        return max((t[-1] for t in self.snap_t.values() if t), default=None)

    def listings(self, market: str, item: int, sid: int) -> list[tuple[int, int, int]]:
        """[(auction_id, unit_price, quantity)] up in snapshot `sid`, cheapest first (exact, from lifecycles)."""
        key = (market, item, sid)
        if key not in self._listings:
            rows = self.conn.execute("""
                SELECT a.auction_id, a.unit_price,
                       COALESCE((SELECT c.quantity FROM auction_changes c
                                 WHERE c.market_id = a.market_id AND c.auction_id = a.auction_id AND c.snap <= ?
                                 ORDER BY c.snap DESC LIMIT 1), a.qty_first)
                FROM auctions a
                WHERE a.item_id = ? AND a.market_id = ? AND a.variant = 0 AND a.unit_price IS NOT NULL
                  AND a.first_snap <= ? AND (a.ended_snap IS NULL OR a.ended_snap > ?)""",
                (sid, item, self.mids[market], sid, sid)).fetchall()
            self._listings[key] = sorted(rows, key=lambda r: (r[1], r[0]))
        return self._listings[key]

    def events(self, market: str, item: int) -> tuple[list, list]:
        """Purchases with their prices, sorted by time -> (times, [(t, unit_price, units)]).
        Definite: quantity drops of listings that stayed up. With gone_weight > 0, listings that
        vanished early also count, at gone_weight units per unit."""
        key = (market, item)
        if key in self._events:
            return self._events[key]
        mid, sid_t = self.mids[market], self.sid_t[market]
        out = []
        prev_aid = prev_q = None
        for aid, price, q0, snap, q in self.conn.execute("""
                SELECT a.auction_id, a.unit_price, a.qty_first, c.snap, c.quantity
                FROM auctions a JOIN auction_changes c ON c.market_id = a.market_id AND c.auction_id = a.auction_id
                WHERE a.item_id = ? AND a.market_id = ? AND a.variant = 0 AND a.unit_price IS NOT NULL
                ORDER BY a.auction_id, c.snap""", (item, mid)):
            if aid != prev_aid:
                prev_aid, prev_q = aid, q0
            if q < prev_q and snap in sid_t:
                out.append((sid_t[snap], price, prev_q - q))
            prev_q = q
        if self.gone_weight > 0:
            for price, q, snap in self.conn.execute(
                    "SELECT unit_price, qty_last, ended_snap FROM auctions WHERE item_id = ? AND market_id = ? "
                    "AND variant = 0 AND outcome = 1 AND unit_price IS NOT NULL", (item, mid)):
                if snap in sid_t:
                    out.append((sid_t[snap], price, q * self.gone_weight))
        out.sort()
        self._events[key] = ([e[0] for e in out], out)
        return self._events[key]


# ---------------------------------------------------------------------------
# Fees and conservative value
# ---------------------------------------------------------------------------

def _duration(hours) -> int:
    hours = int(hours or 24)
    return min(DURATIONS, key=lambda d: abs(d - hours))


def deposit(hist: History, item: int, qty: float, duration_hours, unit_price: float, params: dict | None = None) -> float:
    """Deposit for posting qty units: vendor price x 15% per 12h when known, else a fallback
    fraction of the post price per 12h."""
    p = params or {}
    periods = _duration(duration_hours) / 12
    vendor = hist.vendor.get(item)
    if vendor is not None:
        return vendor * qty * p.get("deposit_rate_12h", DEPOSIT_RATE_12H) * periods
    return unit_price * qty * p.get("deposit_fallback_12h", DEPOSIT_FALLBACK_12H) * periods


def _weighted_quantile(pairs: list[tuple[int, float]], q: float) -> int:
    pairs = sorted(pairs)
    total = sum(n for _, n in pairs)
    seen = 0.0
    for price, n in pairs:
        seen += n
        if seen >= total * q:
            return price
    return pairs[-1][0]


def conservative_value(hist: History, item: int, at: int, held_since: int | None = None,
                       max_hold_days: float | None = None, params: dict | None = None) -> dict:
    """Per-unit conservative value (docs/trader.md, Risk rules):
    min(market value, p25 of definite sold prices over 14d) x 0.95 - expected deposit loss;
    0 if nothing definitely sold in 14 days; halved once held longer than max_hold_days.
    Market value = time-weighted p10 over the 72h before `at`. Expected deposit loss = one 24h
    deposit at that price (assume one expiry before it sells). `hist` must cover [at - 14d, at]."""
    key = (item, at)
    base = hist._cv.get(key)
    if base is None:
        market = hist.market_of(item)
        base = {"item_id": item, "market": market, "market_value": None, "sold_p25": None, "sold_units_14d": 0,
                "deposit_loss": 0, "unit_value": 0}
        if market is not None:
            s = hist.get(market, item)
            base["market_value"] = s.twa_p10(at - MV_HOURS * HOUR, at)
            times, events = hist.events(market, item)
            window = [(price, units) for t, price, units in events[bisect.bisect_right(times, at - VALUE_DAYS * DAY):
                                                                    bisect.bisect_right(times, at)]]
            units = sum(n for _, n in window)
            base["sold_units_14d"] = units
            if units > 0:
                p25 = _weighted_quantile(window, 0.25)
                base["sold_p25"] = p25
                ref = min(p25, base["market_value"]) if base["market_value"] else p25
                dep = deposit(hist, item, 1, 24, ref, params)
                base["deposit_loss"] = round(dep)
                base["unit_value"] = max(0, int(ref * VALUE_HAIRCUT - dep))
        hist._cv[key] = base
    out = dict(base)
    out["halved"] = bool(held_since is not None and max_hold_days is not None
                         and at - held_since > max_hold_days * DAY)
    if out["halved"]:
        out["unit_value"] //= 2
    return out


# ---------------------------------------------------------------------------
# Inventory (FIFO lots) and the execution engine
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    qty: float
    unit_cost: float
    t: int
    tag: str = "untagged"
    ref: str | None = None          # where it came from: task id, transaction id, "owned"
    target: int | None = None       # exit: target sell price
    max_hold_days: float | None = None


class Fifo:
    """First-in first-out lots of one item."""

    def __init__(self):
        self.lots: deque[Lot] = deque()

    def add(self, lot: Lot) -> None:
        if lot.qty > 0:
            self.lots.append(lot)

    @property
    def qty(self) -> float:
        return sum(lot.qty for lot in self.lots)

    @property
    def cost(self) -> float:
        return sum(lot.qty * lot.unit_cost for lot in self.lots)

    def take(self, qty: float) -> list[tuple[float, Lot]]:
        """Remove up to qty units, oldest first -> [(units, lot)]."""
        out = []
        while qty > 1e-9 and self.lots:
            lot = self.lots[0]
            n = min(qty, lot.qty)
            out.append((n, lot))
            lot.qty -= n
            qty -= n
            if lot.qty <= 1e-9:
                self.lots.popleft()
        return out


@dataclass
class Listing:
    item: int
    market: str
    qty: int
    price: int
    posted: int
    expires: int
    dep_unit: float
    tag: str
    task: dict = field(default_factory=dict)
    reposts: int = 0
    sold: int = 0
    credit: float = 0.0
    open: bool = True
    result: dict | None = None      # the task result this listing reports into


ENGINE_DEFAULTS = {"gone_weight": 0.0, "deposit_rate_12h": DEPOSIT_RATE_12H,
                   "deposit_fallback_12h": DEPOSIT_FALLBACK_12H, "max_hold_days": 7, "reposts": 0}


class Engine:
    def __init__(self, hist: History, cash: float = 0, params: dict | None = None, recipes: dict | None = None,
                 enforce_cash: bool = False, now: int = 0):
        self.hist = hist
        self.p = {**ENGINE_DEFAULTS, **(params or {})}
        self.recipes = recipes or {}
        self.enforce_cash = enforce_cash
        self.cash = self.start_cash = float(cash)
        self.now = now
        self.inv: dict[int, Fifo] = {}
        self.listings: list[Listing] = []
        self.consumed: dict[tuple[str, int], int] = {}
        self.items: dict[int, dict] = {}
        self.peak_cost = 0.0
        self.sold_lots: list[dict] = []   # one per (sale, lot) piece

    # --- bookkeeping ---

    def _stats(self, item: int) -> dict:
        return self.items.setdefault(item, {"bought": 0, "bought_cost": 0.0, "crafted": 0, "posted": 0, "sold": 0,
                                            "gross": 0.0, "cut": 0.0, "cost_sold": 0.0, "deposits_lost": 0.0,
                                            "realized": 0.0})

    def fifo(self, item: int) -> Fifo:
        return self.inv.setdefault(item, Fifo())

    def listed(self, item: int) -> int:
        return sum(lst.qty - lst.sold for lst in self.listings if lst.open and lst.item == item)

    def available(self, item: int) -> int:
        return int(self.fifo(item).qty + 1e-9) - self.listed(item)

    def inventory_cost(self) -> float:
        return sum(f.cost for f in self.inv.values())

    def _track_peak(self):
        self.peak_cost = max(self.peak_cost, self.inventory_cost())

    def add_owned(self, item: int, qty: int, unit_cost: float, t: int, tag: str = "owned") -> None:
        self.fifo(item).add(Lot(qty, unit_cost, t, tag, "owned"))

    # --- execution ---

    def execute(self, plan: dict, at: int) -> list[dict]:
        """Run a plan's tasks in order against the snapshot after `at`. -> per-task results."""
        tag = plan.get("strategy") or "untagged"
        results, status = [], {}
        for task in plan.get("tasks") or []:
            kind = task.get("kind")
            res = {"id": task.get("id"), "kind": kind}
            if "item" in task:
                res["item"] = task["item"]
            dep = task.get("after")
            if dep and status.get(dep) not in ("done", "partial"):
                res.update(status="blocked", reason=f"depends on {dep} ({status.get(dep, 'unknown')})")
            elif kind == "buy":
                self._buy(task, at, task.get("strategy") or tag, res)
            elif kind == "post":
                self._post(task, at, task.get("strategy") or tag, res, self.p["reposts"])
            elif kind == "craft":
                self._craft(task, at, task.get("strategy") or tag, res)
            elif kind == "cancel":
                self._cancel(task, at, res)
            else:
                res.update(status="not_simulated")
            status[res["id"]] = res["status"]
            results.append(res)
        self._track_peak()
        return results

    def _exec_snap(self, item: int, at: int, res: dict):
        market = self.hist.market_of(item)
        snap = self.hist.next_snap(market, at) if market else None
        if snap is None:
            res.update(status="no_data", reason="no snapshot of this item after the decision time")
            return None, None
        res["executed"] = snap[1]
        return market, snap

    def _buy(self, task, at, tag, res):
        item, want = int(task["item"]), int(task.get("qty") or 0)
        max_unit = task.get("max_unit")
        market, snap = self._exec_snap(item, at, res)
        if market is None:
            return
        got, paid, broke = 0, 0, False
        for aid, price, qty in self.hist.listings(market, item, snap[0]):
            if got >= want or (max_unit is not None and price > max_unit):
                break
            n = min(qty - self.consumed.get((market, aid), 0), want - got)
            if self.enforce_cash and n > (self.cash - paid) // price:
                n, broke = int((self.cash - paid) // price), True
            if n <= 0:
                continue
            self.consumed[(market, aid)] = self.consumed.get((market, aid), 0) + n
            got, paid = got + n, paid + n * price
        res.update(qty=got, paid=paid)
        if got:
            self.cash -= paid
            self.fifo(item).add(Lot(got, paid / got, snap[1], tag, task.get("id"), task.get("target"),
                                    task.get("max_hold_days", self.p["max_hold_days"])))
            st = self._stats(item)
            st["bought"] += got
            st["bought_cost"] += paid
            res["avg_unit"] = paid // got
        res["status"] = "done" if got >= want else "partial" if got else "skipped"
        if got < want:
            res["reason"] = "not enough cash" if broke else "no more listings at or below max_unit in the snapshot"

    def _craft(self, task, at, tag, res):
        rid = int(task["recipe"])
        recipe = self.recipes.get(rid)
        res["recipe"] = rid
        if not recipe:
            res.update(status="skipped", reason="unknown recipe")
            return
        n = int(task.get("qty") or 1)
        for item, q in recipe["reagents"].items():
            n = min(n, self.available(item) // q)
        if n <= 0:
            res.update(status="skipped", reason="missing reagents", qty=0)
            return
        cost = 0.0
        for item, q in recipe["reagents"].items():
            cost += sum(k * lot.unit_cost for k, lot in self.fifo(item).take(n * q))
        out = int(n * recipe["output_qty"] + 1e-9)
        if out:
            self.fifo(recipe["output"]).add(Lot(out, cost / out, max(at, self.now), tag, task.get("id"),
                                                task.get("target"), task.get("max_hold_days", self.p["max_hold_days"])))
            self._stats(recipe["output"])["crafted"] += out
        res.update(status="done" if n >= int(task.get("qty") or 1) else "partial", qty=n, output=out,
                   output_item=recipe["output"], cost=round(cost))

    def _post(self, task, at, tag, res, reposts):
        item = int(task["item"])
        market, snap = self._exec_snap(item, at, res)
        if market is None:
            return
        n = min(int(task.get("qty") or 0), self.available(item))
        if n <= 0:
            res.update(status="skipped", reason="nothing held to post", qty=0)
            return
        live = [p for aid, p, q in self.hist.listings(market, item, snap[0])
                if q - self.consumed.get((market, aid), 0) > 0]
        lowest = live[0] if live else None
        floor, ceil = int(task.get("floor") or 1), task.get("ceil")
        res["seen"] = lowest
        if lowest is None:
            price = int(ceil or floor)
        else:
            price = lowest - int(task.get("undercut", 1))
            if price < floor:
                res.update(status="below_floor", qty=0)
                return
            if ceil:
                price = min(price, int(ceil))
        duration = _duration(task.get("duration"))
        dep = deposit(self.hist, item, n, duration, price, self.p)
        self.cash -= dep
        res.update(status="done" if n >= int(task.get("qty") or 0) else "partial", qty=n, price=price,
                   deposit=round(dep), duration=duration)
        res.setdefault("sold", 0)
        res.setdefault("expired", 0)
        self.listings.append(Listing(item, market, n, price, snap[1], snap[1] + duration * HOUR, dep / n, tag,
                                     task, reposts, result=res))
        self._stats(item)["posted"] += n

    def _cancel(self, task, at, res):
        item = int(task["item"])
        below, above = task.get("below"), task.get("above")
        n = 0
        for lst in self.listings:
            if lst.open and lst.item == item and lst.posted <= max(at, self.now) and (
                    (below is not None and lst.price < below) or (above is not None and lst.price > above)):
                n += self._close(lst, cancelled=True)
        res.update(status="done" if n else "skipped", qty=n)

    def _close(self, lst: Listing, cancelled=False) -> int:
        lst.open = False
        unsold = lst.qty - lst.sold
        lost = lst.dep_unit * unsold
        self._stats(lst.item)["deposits_lost"] += lost
        if lst.result is not None:
            lst.result["cancelled" if cancelled else "expired"] = lst.result.get(
                "cancelled" if cancelled else "expired", 0) + unsold
        return unsold

    # --- time ---

    def advance(self, until: int) -> None:
        """Step through every snapshot in (now, until]: expiries, then fills from purchases."""
        for ts in self.hist.all_snaps_between(self.now, until):
            self._expire(ts)
            self._fill(ts)
        self._expire(until)
        self.now = max(self.now, until)

    def _expire(self, ts: int) -> None:
        for lst in [x for x in self.listings if x.open and x.expires <= ts]:
            unsold = self._close(lst)
            if unsold and lst.reposts > 0:
                res = lst.result
                again = {**lst.task, "qty": unsold}
                sub = {"id": lst.task.get("id"), "kind": "post"}
                self._post(again, lst.expires, lst.tag, sub, lst.reposts - 1)
                if res is not None:
                    res.setdefault("reposts", []).append(sub)
        self.listings = [x for x in self.listings if x.open]

    def _fill(self, ts: int) -> None:
        by_item: dict[tuple[str, int], list[Listing]] = {}
        for lst in self.listings:
            if lst.open and lst.posted < ts:
                by_item.setdefault((lst.market, lst.item), []).append(lst)
        for (market, item), lsts in by_item.items():
            times, events = self.hist.events(market, item)
            now_events = events[bisect.bisect_left(times, ts):bisect.bisect_right(times, ts)]
            lsts.sort(key=lambda x: (x.price, x.posted))
            for _, price, units in sorted(now_events, key=lambda e: -e[1]):  # dearest purchases first
                for lst in lsts:
                    if units <= 1e-9 or lst.price > price:
                        break
                    room = lst.qty - lst.credit
                    if room <= 1e-9:
                        continue
                    take = min(room, units)
                    lst.credit += take
                    units -= take
                    n = int(lst.credit + 1e-9) - lst.sold
                    if n > 0:
                        self._sell(lst, n, ts)
            for lst in lsts:
                if lst.sold >= lst.qty:
                    lst.open = False

    def _sell(self, lst: Listing, n: int, ts: int) -> None:
        gross = n * lst.price
        cut = gross * AH_CUT
        self.cash += gross - cut + lst.dep_unit * n
        lst.sold += n
        if lst.result is not None:
            lst.result["sold"] = lst.result.get("sold", 0) + n
        st = self._stats(lst.item)
        st["sold"] += n
        st["gross"] += gross
        st["cut"] += cut
        for k, lot in self.fifo(lst.item).take(n):
            cost = k * lot.unit_cost
            st["cost_sold"] += cost
            st["realized"] += k * lst.price * (1 - AH_CUT) - cost
            self.sold_lots.append({"item": lst.item, "units": k, "price": lst.price, "unit_cost": lot.unit_cost,
                                   "acquired": lot.t, "sold": ts, "tag": lot.tag, "ref": lot.ref})

    # --- valuation and results ---

    def equity(self, at: int) -> float:
        """Cash plus held (and listed) inventory at conservative value."""
        return self.cash + self.held_value(at)

    def held_value(self, at: int) -> float:
        total = 0.0
        for item, f in self.inv.items():
            for lot in f.lots:
                cv = conservative_value(self.hist, item, at, lot.t, lot.max_hold_days, self.p)
                total += lot.qty * cv["unit_value"]
        return total

    def summary(self, at: int, names: dict | None = None) -> dict:
        names = names or {}
        sold = sum(s["sold"] for s in self.items.values())
        acquired = sum(s["bought"] + s["crafted"] for s in self.items.values())
        realized_units = sum(s["realized"] for s in self.items.values())
        deposits_lost = sum(s["deposits_lost"] for s in self.items.values())
        # Deposits on listings still up are spent but may yet be refunded; count them as spent.
        deposits_open = sum(lst.dep_unit * (lst.qty - lst.sold) for lst in self.listings if lst.open)
        held_cost = self.inventory_cost()
        held_value = self.held_value(at)
        held_units = sum(f.qty for f in self.inv.values())
        days = [(s["sold"] - s["acquired"]) / DAY for s in self.sold_lots]
        weights = [s["units"] for s in self.sold_lots]
        realized = realized_units - deposits_lost
        per_item = []
        for item, s in sorted(self.items.items(), key=lambda kv: -abs(kv[1]["realized"])):
            f = self.inv.get(item)
            per_item.append({"item_id": item, "name": names.get(item), "bought": s["bought"],
                             "bought_cost": round(s["bought_cost"]), "crafted": s["crafted"], "posted": s["posted"],
                             "sold": s["sold"], "gross": round(s["gross"]), "cut": round(s["cut"]),
                             "deposits_lost": round(s["deposits_lost"]),
                             "realized": round(s["realized"] - s["deposits_lost"]),
                             "held": round(f.qty) if f else 0, "held_cost": round(f.cost) if f else 0,
                             "held_value": round(sum(lot.qty * conservative_value(
                                 self.hist, item, at, lot.t, lot.max_hold_days, self.p)["unit_value"]
                                 for lot in f.lots)) if f else 0})
        return {
            "realized_pnl": round(realized),
            "realized_pnl_before_deposits": round(realized_units),
            "deposits_lost": round(deposits_lost),
            "deposits_on_open_listings": round(deposits_open),
            "cash_flow": round(self.cash - self.start_cash),
            "capital_used": round(self.peak_cost),
            "roi": round(realized / self.peak_cost, 4) if self.peak_cost else None,
            "units_acquired": acquired,
            "units_sold": sold,
            "sell_through": round(sold / acquired, 4) if acquired else None,
            "avg_days_held": round(sum(d * w for d, w in zip(days, weights)) / sum(weights), 2) if weights else None,
            "unsold_units": round(held_units),
            "unsold_cost": round(held_cost),
            "unsold_conservative_value": round(held_value),
            "marked_pnl": round(self.cash - self.start_cash + held_value),
            "items": per_item,
        }


def load_recipes(conn) -> dict[int, dict]:
    """{recipe_id: {output, output_qty, reagents {item: qty}}} from the user's recipes (any character)."""
    recipes: dict[int, dict] = {}
    try:
        for rid, out, qty in conn.execute("SELECT recipe_id, output_item, output_qty FROM recipes"):
            if out:
                recipes.setdefault(rid, {"output": out, "output_qty": qty or 1, "reagents": {}})
        for rid, item, qty in conn.execute("SELECT recipe_id, item_id, quantity FROM recipe_reagents"):
            if rid in recipes:
                recipes[rid]["reagents"][item] = qty
    except sqlite3.OperationalError:
        return {}
    return {rid: r for rid, r in recipes.items() if r["reagents"]}


def _names(conn, ids) -> dict[int, str]:
    ids = list(set(ids))
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            out.update(conn.execute(f"SELECT item_id, name FROM items WHERE item_id IN ({','.join('?' * len(chunk))})",
                                    chunk))
        except sqlite3.OperationalError:
            break
    return out


def iso(ts) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def _latest_snapshot(conn, markets) -> int | None:
    return conn.execute(f"SELECT MAX(last_modified) FROM snapshots WHERE market IN ({','.join('?' * len(markets))})",
                        list(markets)).fetchone()[0]


# ---------------------------------------------------------------------------
# evaluate_plan
# ---------------------------------------------------------------------------

def _plan_items(plan: dict, recipes: dict) -> set[int]:
    items = set()
    for t in plan.get("tasks") or []:
        if "item" in t:
            items.add(int(t["item"]))
        if t.get("kind") == "craft" and int(t.get("recipe", 0)) in recipes:
            r = recipes[int(t["recipe"])]
            items.add(r["output"])
            items.update(r["reagents"])
    return items


def _owned_inventory(conn, items) -> dict[int, int]:
    try:
        rows = conn.execute(f"SELECT item_id, SUM(count) FROM inventory WHERE item_id IN "
                            f"({','.join('?' * len(items))}) GROUP BY item_id", list(items)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {i: n for i, n in rows}


def evaluate_plan(conn, plan: dict, start_ts: int | None = None, horizon_hours: float = 72,
                  inventory: dict | None = None, cost_basis: dict | None = None, params: dict | None = None,
                  markets=MARKETS) -> dict:
    """Score a plan (docs/trader.md task format) against what happened after start_ts (default:
    plan.created). Items the plan posts or crafts from that it didn't buy come from `inventory`
    ({item: count}; default: the user's synced bags/bank/warband counts), costed at `cost_basis`
    ({item: copper per unit}) or else their conservative value at start_ts. params: the engine
    defaults (reposts 1 here, gone_weight, deposit rates, max_hold_days), vendor_prices, markets."""
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise ValueError("plan must be an object with a tasks list")
    p = {**ENGINE_DEFAULTS, "reposts": 1, **(params or {})}
    extra = {"vendor_prices", "markets"}
    unknown = set(p) - set(ENGINE_DEFAULTS) - extra
    if unknown:
        raise ValueError(f"unknown params: {', '.join(sorted(unknown))}; allowed: "
                         f"{', '.join(sorted(set(ENGINE_DEFAULTS) | extra))}")
    markets = list(p.pop("markets", None) or markets)
    start = int(start_ts if start_ts is not None else plan.get("created") or 0)
    if not start:
        raise ValueError("start_ts is required when the plan has no created time")
    end = start + int(float(horizon_hours) * HOUR)
    recipes = load_recipes(conn)
    items = _plan_items(plan, recipes)
    hist = History(conn, markets, start - VALUE_DAYS * DAY - MV_HOURS * HOUR, end, items=items,
                   gone_weight=p["gone_weight"], vendor_prices=p.get("vendor_prices"))
    engine = Engine(hist, 0, p, recipes, now=start)
    owned = {int(k): int(v) for k, v in (inventory or {}).items()} if inventory is not None \
        else _owned_inventory(conn, items)
    basis = {int(k): v for k, v in (cost_basis or {}).items()}
    opening = 0.0
    for item, n in owned.items():
        if n > 0 and item in items:
            unit = basis.get(item)
            if unit is None:
                unit = conservative_value(hist, item, start, params=p)["unit_value"]
            engine.add_owned(item, n, unit, start)
            opening += n * unit
    results = engine.execute(plan, start)
    data_until = hist.latest()
    engine.advance(min(end, data_until or end))
    at = min(end, data_until or end)
    names = _names(conn, items)
    for r in results:
        if "item" in r:
            r["name"] = names.get(int(r["item"]))
    out = {"plan": plan.get("plan"), "start": start, "start_iso": iso(start), "end": at, "end_iso": iso(at),
           "horizon_hours": horizon_hours, "data_until": data_until, "data_until_iso": iso(data_until),
           "complete": bool(data_until and data_until >= end), "opening_inventory_value": round(opening),
           "tasks": results, **engine.summary(at, names)}
    out["note"] = ("marked_pnl = cash flow + unsold inventory at conservative value; owned inventory enters at "
                   "cost_basis or its conservative value at start, so it measures what the plan added.")
    return out


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------

COMMON = {
    "capital": 4_000_000_000,        # working capital in copper (400k gold)
    "decision_hours_utc": [13, 1],   # morning and evening sessions
    "duration": 24,                  # post duration, hours
    "max_hold_days": 3,
    "max_item_share": 0.10,          # per-item exposure at cost, share of capital
    "max_item_days": 3,              # per-item exposure, days of definite sold/day
    "max_inventory_share": 0.40,     # total inventory at cost, share of capital
    "max_buys": 10,                  # new positions per decision
    "universe_min_moved": 1.0,       # items moving at least this many units/day over the window
    "gone_weight": 0.0,
    "deposit_rate_12h": DEPOSIT_RATE_12H,
    "deposit_fallback_12h": DEPOSIT_FALLBACK_12H,
}

STRATEGIES = {
    "flip": {**COMMON, "markets": ["us-commodities"], "days": 7, "min_trips": 2, "min_margin": 0.03,
             "min_moved_per_day": 2000, "min_gold_per_day": 20_000_0000, "min_presence": 0.9,
             "floor_ratio": 0.95, "ceil_ratio": 2.0, "universe_min_moved": 500},
    "deals": {**COMMON, "markets": ["us-commodities", "us-proudmoore"], "hours": 72, "max_ratio": 0.7,
              "min_sold": 1.0, "floor_ratio": 0.9, "ceil_ratio": 1.5},
    "craft": {**COMMON, "markets": ["us-commodities", "us-proudmoore"], "hours": 72, "min_roi": 0.10,
              "min_sold": 1.0, "buy_slack": 1.05, "sell_days": 1.0, "floor_ratio": 0.0, "ceil_ratio": 1.5},
}


def price_table_at(hist: History, at: int, hours: int, markets, items=None, min_per_day: float = 0.0) -> dict:
    """pricing.price_table rebuilt from History as of `at` (API snapshots only): per item the busiest
    market's time-weighted p10 and moved/day over `hours`, and its state at the last snapshot <= at."""
    table: dict[tuple[int, int], pricing.Price] = {}
    a = at - hours * HOUR
    for market in markets:
        snaps = hist.snaps_between(market, a, at)
        if not snaps:
            continue
        last = snaps[-1]
        days = (last - snaps[0]) / DAY if len(snaps) > 1 else 0
        for item in (items if items is not None else hist.series.get(market, {})):
            s = hist.get(market, item)
            if s is None:
                continue
            per_day = s.moved(a, last) / days if days else 0.0
            if per_day < min_per_day:
                continue
            cur = table.get((item, 0))
            if cur and cur.sold_per_day >= per_day:
                continue
            st = s.state(last)
            if st is None:
                continue
            mn, p10, qty = st
            table[(item, 0)] = pricing.Price(item, 0, s.twa_p10(a, last + HOUR), mn if qty else None,
                                             p10 if qty else None, per_day, last, {market})
    return table


def _definite_per_day(hist: History, item: int, at: int, hours: float) -> float:
    market = hist.market_of(item)
    s = hist.get(market, item) if market else None
    snaps = hist.snaps_between(market, at - hours * HOUR, at) if market else []
    if not s or len(snaps) < 2:
        return 0.0
    return s.sold(snaps[0], snaps[-1]) / ((snaps[-1] - snaps[0]) / DAY)


def _cap_units(ctx, item: int, unit_price: float, sold_per_day: float) -> int:
    """Units we may still buy under the docs/trader.md exposure rules and our cash, counting what
    this plan already intends to buy (ctx["pending"]: {item: (units, cost)}), and reserve them."""
    engine, p, pending = ctx["engine"], ctx["p"], ctx["pending"]
    if unit_price <= 0:
        return 0
    capital = p["capital"]
    f = engine.fifo(item)
    units, cost = pending.get(item, (0, 0.0))
    planned = sum(c for _, c in pending.values())
    by_cost = (capital * p["max_item_share"] - f.cost - cost) / unit_price
    by_days = p["max_item_days"] * sold_per_day - f.qty - units
    by_total = (capital * p["max_inventory_share"] - engine.inventory_cost() - planned) / unit_price
    by_cash = (engine.cash - planned) / unit_price
    n = max(0, int(min(by_cost, by_days, by_total, by_cash)))
    if n:
        pending[item] = (units + n, cost + n * unit_price)
    return n


def _exit_posts(engine: Engine, at: int, p: dict) -> list[dict]:
    """Post everything held and not listed: floor = max(break-even, target x floor_ratio), or
    1 copper (liquidate) once any lot is past its max hold."""
    tasks = []
    for item, f in engine.inv.items():
        n = engine.available(item)
        if n <= 0 or not f.lots:
            continue
        unit_cost = f.cost / f.qty
        breakeven = math.ceil(unit_cost / (1 - AH_CUT))
        target = max((lot.target or 0) for lot in f.lots) or breakeven
        stale = any(lot.max_hold_days is not None and at - lot.t > lot.max_hold_days * DAY for lot in f.lots)
        floor = 1 if stale else max(breakeven, int(target * p["floor_ratio"]))
        tasks.append({"id": f"x{item}", "kind": "post", "item": item, "qty": n, "floor": floor,
                      "ceil": max(floor, int(target * p["ceil_ratio"])), "undercut": 1, "duration": p["duration"],
                      "est_min": 1, "why": "liquidate: past max hold" if stale else "exit"})
    return tasks


def _flip_signals(hist: History, market: str, at: int, p: dict) -> list[tuple[pricing.Flip, tuple]]:
    """pricing.flip_report's logic over History as of `at` -> [(flip, current state)]."""
    snaps = hist.snaps_between(market, at - p["days"] * DAY, at)
    if len(snaps) < 2:
        return []
    span = (snaps[-1] - snaps[0]) / DAY
    out = []
    for item, s in hist.series.get(market, {}).items():
        moved = s.moved(snaps[0], snaps[-1])
        if moved / span < p["min_moved_per_day"]:
            continue
        sold = s.sold(snaps[0], snaps[-1])
        priced = []
        for t in snaps:
            j = s._row(t)
            if j >= 0 and s.mn[j] is not None and s.qty[j] > 0:
                priced.append((t, s.mn[j]))
        if len(priced) < max(4, p["min_presence"] * len(snaps)):
            continue
        prices = sorted(x for _, x in priced)
        median = pricing._quantile(prices, 0.5)
        if moved / span * median < p["min_gold_per_day"]:
            continue
        p25, p75 = pricing._quantile(prices, 0.25), pricing._quantile(prices, 0.75)
        trips, holding = 0, False
        for _, x in priced if p75 > p25 else ():
            if not holding and x <= p25:
                holding = True
            elif holding and x >= p75:
                trips, holding = trips + 1, False
        flip = pricing.Flip(item, 0, p25, p75, median, trips, 0.0, None, sold / span, moved / span, len(priced))
        if trips >= p["min_trips"] and flip.margin >= p["min_margin"]:
            out.append((flip, s.state(at)))
    out.sort(key=lambda fs: -fs[0].trips * (fs[0].p75 * (1 - AH_CUT) - fs[0].p25) * fs[0].moved_per_day)
    return out


def _plan_flip(ctx, at: int) -> list[dict]:
    engine, hist, p = ctx["engine"], ctx["hist"], ctx["p"]
    tasks = _exit_posts(engine, at, p)
    n = 0
    for market in p["markets"]:
        for flip, state in _flip_signals(hist, market, at, p):
            if n >= p["max_buys"]:
                break
            if not state or state[0] is None or state[0] > flip.p25:
                continue  # only buy while it's at its lows
            qty = _cap_units(ctx, flip.item_id, flip.p25, flip.sold_per_day)
            if qty <= 0:
                continue
            n += 1
            tasks.append({"id": f"b{flip.item_id}", "kind": "buy", "item": flip.item_id, "qty": qty,
                          "max_unit": flip.p25, "target": flip.p75, "max_hold_days": p["max_hold_days"],
                          "est_min": 2, "ev": int(qty * (flip.p75 * (1 - AH_CUT) - flip.p25)),
                          "why": f"flip: {flip.trips} trips, p25 {flip.p25} -> p75 {flip.p75}"})
    return tasks


def _plan_deals(ctx, at: int) -> list[dict]:
    engine, hist, p = ctx["engine"], ctx["hist"], ctx["p"]
    tasks = _exit_posts(engine, at, p)
    prices = price_table_at(hist, at, p["hours"], p["markets"], min_per_day=p["min_sold"])
    n = 0
    for d in pricing.deal_report(prices, p["max_ratio"], p["min_sold"]):
        if n >= p["max_buys"]:
            break
        definite = _definite_per_day(hist, d.item_id, at, p["hours"])
        qty = _cap_units(ctx, d.item_id, d.current_min, definite)
        if qty <= 0:
            continue
        n += 1
        bid = f"b{d.item_id}"
        tasks.append({"id": bid, "kind": "buy", "item": d.item_id, "qty": qty,
                      "max_unit": int(d.market_value * p["max_ratio"]), "target": d.market_value,
                      "max_hold_days": p["max_hold_days"], "est_min": 2, "ev": qty * d.profit_per_unit,
                      "why": f"deal: listed at {d.discount:.0%} under market value"})
        tasks.append({"id": f"p{d.item_id}", "kind": "post", "item": d.item_id, "qty": qty, "after": bid,
                      "floor": int(d.market_value * p["floor_ratio"]), "ceil": int(d.market_value * p["ceil_ratio"]),
                      "undercut": 1, "duration": p["duration"], "est_min": 1})
    return tasks


def _plan_craft(ctx, at: int) -> list[dict]:
    engine, hist, p, recipes = ctx["engine"], ctx["hist"], ctx["p"], ctx["recipes"]
    tasks = _exit_posts(engine, at, p)
    prices = price_table_at(hist, at, p["hours"], p["markets"], items=ctx["items"])
    n = 0
    for c in pricing.craft_report(ctx["conn"], prices):
        if n >= p["max_buys"]:
            break
        r = recipes.get(c.recipe_id)
        if not r or c.profit is None or not c.cost or c.profit / c.cost < p["min_roi"] or c.sold_per_day < p["min_sold"]:
            continue
        out_price = c.cost / c.output_qty
        definite = _definite_per_day(hist, c.output_item, at, p["hours"])
        units = min(_cap_units(ctx, c.output_item, out_price, definite), c.sold_per_day * p["sell_days"])
        crafts = int(units / c.output_qty)
        if crafts <= 0:
            continue
        ctx["pending"][c.output_item] = (crafts * c.output_qty, crafts * c.cost)  # reserve what we'll make
        n += 1
        ids = []
        for item, q in r["reagents"].items():
            need = crafts * q - engine.available(item)
            if need > 0:
                ids.append(f"b{c.recipe_id}-{item}")
                tasks.append({"id": ids[-1], "kind": "buy", "item": item, "qty": need,
                              "max_unit": int(prices[(item, 0)].buy * p["buy_slack"]), "max_hold_days": p["max_hold_days"],
                              "est_min": 1, "why": f"reagent for {c.name or c.output_item}"})
        cid = f"c{c.recipe_id}"
        tasks.append({"id": cid, "kind": "craft", "recipe": c.recipe_id, "qty": crafts,
                      "target": prices[(c.output_item, 0)].market_value or prices[(c.output_item, 0)].sell,
                      "max_hold_days": p["max_hold_days"], "est_min": 3, "ev": c.profit * crafts,
                      **({"after": ids[-1]} if ids else {})})
        mv = prices[(c.output_item, 0)].market_value or prices[(c.output_item, 0)].sell
        tasks.append({"id": f"p{c.recipe_id}", "kind": "post", "item": c.output_item,
                      "qty": int(crafts * c.output_qty), "after": cid,
                      "floor": math.ceil(out_price / (1 - AH_CUT)), "ceil": max(int(mv * p["ceil_ratio"]), 1),
                      "undercut": 1, "duration": p["duration"], "est_min": 1})
    return tasks


PLANNERS = {"flip": _plan_flip, "deals": _plan_deals, "craft": _plan_craft}


def _decision_times(since: int, until: int, hours: list[int]) -> list[int]:
    out = []
    day = since // DAY * DAY
    while day <= until:
        out += [day + h * HOUR for h in hours if since <= day + h * HOUR < until]
        day += DAY
    return sorted(out)


def _drawdown(curve: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        worst = max(worst, peak - v)
    return worst


def backtest(conn, strategy: str, params: dict | None = None, since: int | None = None,
             until: int | None = None) -> dict:
    """Replay `strategy` over [since, until] (default: the last 30 days of data): at every decision
    time, generate the plan it would have published from data up to then, execute it with the
    fill model above, and step to the next decision. Starts with params.capital in cash."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; one of {', '.join(STRATEGIES)}")
    defaults = STRATEGIES[strategy]
    unknown = set(params or {}) - set(defaults) - {"vendor_prices"}
    if unknown:
        raise ValueError(f"unknown params for {strategy}: {', '.join(sorted(unknown))}; "
                         f"allowed: {', '.join(sorted(defaults))}")
    p = {**defaults, **(params or {})}
    markets = list(p["markets"])
    latest = _latest_snapshot(conn, markets)
    if latest is None:
        raise ValueError("no snapshots for these markets")
    until = min(int(until), latest) if until is not None else latest
    first = conn.execute(f"SELECT MIN(last_modified) FROM snapshots WHERE market IN ({','.join('?' * len(markets))})",
                         markets).fetchone()[0]
    since = max(int(since), first) if since is not None else max(first, until - 30 * DAY)
    started = time.time()
    lookback = max(VALUE_DAYS * DAY, p.get("days", 0) * DAY, p.get("hours", MV_HOURS) * HOUR, MV_HOURS * HOUR)
    recipes = load_recipes(conn) if strategy == "craft" else {}
    items = None
    if strategy == "craft":
        if not recipes:
            raise ValueError("craft needs recipes: open each profession window in game, then sync")
        items = {r["output"] for r in recipes.values()} | {i for r in recipes.values() for i in r["reagents"]}
    hist = History(conn, markets, since - lookback, until, items=items,
                   min_moved_per_day=p["universe_min_moved"], gone_weight=p["gone_weight"],
                   vendor_prices=p.get("vendor_prices"))
    loaded = time.time()
    engine = Engine(hist, p["capital"], {k: p[k] for k in ENGINE_DEFAULTS if k in p}, recipes,
                    enforce_cash=True, now=since)
    ctx = {"engine": engine, "hist": hist, "p": p, "recipes": recipes, "conn": conn, "items": items}
    decisions, curve, sample = [], [], None
    for at in _decision_times(since, until, p["decision_hours_utc"]):
        engine.advance(at)
        ctx["pending"] = {}
        equity = engine.equity(at)
        curve.append(equity)
        tasks = PLANNERS[strategy](ctx, at)
        plan = {"plan": f"{strategy}-{iso(at)}", "created": at, "strategy": strategy, "paper": True, "tasks": tasks}
        results = engine.execute(plan, at)
        if sample is None and any(t["kind"] == "buy" for t in tasks):
            sample = {"plan": plan, "results": results}
        decisions.append({
            "at": at, "at_iso": iso(at), "tasks": len(tasks), "equity": round(equity), "cash": round(engine.cash),
            "bought_units": sum(r.get("qty", 0) for r in results if r["kind"] == "buy"),
            "spent": sum(r.get("paid", 0) for r in results if r["kind"] == "buy"),
            "posted_units": sum(r.get("qty", 0) for r in results if r["kind"] == "post" and r["status"] in ("done", "partial")),
            "below_floor": sum(1 for r in results if r["status"] == "below_floor")})
    engine.advance(until)
    final = engine.equity(until)
    curve.append(final)
    names = _names(conn, engine.items)
    summary = engine.summary(until, names)
    summary["items"] = summary["items"][:25]
    key = json.dumps({"s": strategy, "p": p, "since": since, "until": until}, sort_keys=True, default=str)
    return {"backtest_id": "bt-" + hashlib.sha1(key.encode()).hexdigest()[:12], "strategy": strategy,
            "since": since, "since_iso": iso(since), "until": until, "until_iso": iso(until),
            "days": round((until - since) / DAY, 2), "params": p, "decisions_count": len(decisions),
            "start_equity": p["capital"], "end_equity": round(final),
            "max_drawdown": round(_drawdown(curve)), **summary,
            "decisions": decisions[-60:], "sample_plan": sample,
            "universe": {m: len(s) for m, s in hist.series.items()},
            "seconds": {"load": round(loaded - started, 2), "total": round(time.time() - started, 2)},
            "note": "A smoke test until weeks of history exist. Fills are conservative (see evaluate_plan); "
                    "realm-AH items rarely record definite sales, so they mostly stay unsold at 0 value."}
