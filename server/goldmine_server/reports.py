"""The reports (crafts, deals, flips, summary, price, timing) as structured data, plus a thin text layer.

Each report has a `<kind>_data(args) -> dict` builder (JSON-safe: rows are plain dicts, money is
copper) and a `<kind>_text(data) -> str` formatter. The web UI renders the dicts as HTML tables and
charts; the text API and the CLI-style `<kind>_cmd(args)` print the text. `args` is an argparse-style
namespace with the user's connection in `args.conn` (see services.cli_args).
"""

from __future__ import annotations

import datetime as dt
import json
import time

from . import ledger, pricing, store
from .ledger import fmt_gold

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
SHADES = " .:-=+*#%@"
WOWHEAD = {"retail": "https://www.wowhead.com/item={}", "classic": "https://www.wowhead.com/mop-classic/item={}",
           "classic1x": "https://www.wowhead.com/classic/item={}",
           "classicann": "https://www.wowhead.com/classic/item={}"}


def wowhead_url(item_id: int, game: str | None = None) -> str:
    return WOWHEAD.get(game or "retail", WOWHEAD["retail"]).format(item_id)


def wowhead_data(item_id: int, game: str | None = None) -> str:
    """A data-wowhead value, so Wowhead's tooltip script shows the item on hover over any link."""
    domain = wowhead_url(item_id, game).removeprefix("https://www.wowhead.com/").partition("item=")[0].strip("/")
    return f"item={item_id}" + (f"&domain={domain}" if domain else "")


def gold(copper: int | None, signed: bool = False) -> str:
    """Money at a sensible precision, truncated like the game: 19,999g from 100g, 12g 34s from 1g, else 4s 79c."""
    if copper is None:
        return "-"
    sign = "-" if copper < 0 else "+" if signed and copper > 0 else ""
    c = abs(copper)
    g, s, cu = c // 10000, c // 100 % 100, c % 100
    if g >= 100:
        return f"{sign}{g:,}g"
    if g:
        return f"{sign}{g}g {s:02d}s"
    if s:
        return f"{sign}{s}s {cu:02d}c"
    return f"{sign}{cu}c"


def table(headers: list[str], rows: list[list], right: set[int] = frozenset(), indent: str = "") -> list[str]:
    """Plain-text table with columns sized to fit and two spaces between them."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]

    def line(r):
        return indent + "  ".join(c.rjust(w) if i in right else c.ljust(w)
                                  for i, (c, w) in enumerate(zip(r, widths))).rstrip()
    return [line(headers)] + [line(r) for r in cells]


def short(name: str, n: int = 36) -> str:
    return name if len(name) <= n else name[:n - 1] + "…"


def first_name(char_key: str) -> str:
    return char_key.split("-")[0]


# ---------------------------------------------------------------------------
# Crafts and deals: one section per market set
# ---------------------------------------------------------------------------

def set_info(st: pricing.MarketSet, games: dict | None = None) -> dict:
    where = " / ".join(st.realms) if st.kind == "realm" else f"{st.region.upper()} commodities"
    return {"kind": st.kind, "game": st.game, "region": st.region, "where": where, "markets": st.markets,
            "chars": [first_name(k) for k in st.chars]}


def section_title(s: dict) -> str:
    return f"== {s['game']} · {s['where']} ({', '.join(s['chars']) or 'no characters'})"


def history(conn, markets: list[str], item_id: int, variant: int = 0, hours: int = 72,
            now: int | None = None) -> list[list[int]]:
    """[[time, min price], ...] over the last `hours`: the item's state changes (busiest market
    first) plus its state at the window start, so a step line covers the whole window."""
    now = now or int(time.time())
    since = now - hours * 3600
    best: list = []
    for mid in store.market_ids(conn, markets).values():
        rows = conn.execute("""
            SELECT s.last_modified, st.min_price FROM item_states st JOIN snapshots s ON s.id = st.snap
            WHERE st.market_id = ? AND st.item_id = ? AND st.variant = ? AND s.last_modified >= ?
            ORDER BY st.snap""", (mid, item_id, variant, since)).fetchall()
        before = conn.execute("""
            SELECT st.min_price FROM item_states st JOIN snapshots s ON s.id = st.snap
            WHERE st.market_id = ? AND st.item_id = ? AND st.variant = ? AND s.last_modified < ?
            ORDER BY st.snap DESC LIMIT 1""", (mid, item_id, variant, since)).fetchone()
        pts = ([[since, before[0]]] if before and before[0] else []) + [[t, p] for t, p in rows if p]
        if len(pts) > len(best):
            best = pts
    return best


def crafts_data(args) -> dict:
    conn = ledger.db_for(args)
    exports = [ex for ex in ledger.set_reports(conn, args) if ex.crafts]
    out = {"kind": "crafts", "sections": [],
           "note": "Sells for = min(market value, current lowest) after the 5% AH cut; cost uses current p10 prices.",
           "empty": None if exports else "No recipes yet. Open each profession window in game, then /reload and sync."}
    for ex in exports:
        crafts = ex.crafts[:args.limit]
        names = ledger.item_names(conn, {c.output_item for c in crafts} | {m for c in crafts for m in c.missing}, args)
        rows = [{"recipe_id": c.recipe_id, "name": c.name or names[c.output_item], "profession": c.profession,
                 "item_id": c.output_item, "output_qty": c.output_qty, "cost": c.cost, "revenue": c.revenue,
                 "profit": c.profit, "sold_per_day": round(c.sold_per_day, 2),
                 "crafters": [first_name(k) for k in c.crafters],
                 "missing": [{"item_id": m, "name": names[m]} for m in c.missing]} for c in crafts]
        out["sections"].append(set_info(ex.set) | {"rows": rows})
    return out


def crafts_text(data: dict) -> str:
    if data["empty"]:
        return data["empty"] + "\n"
    lines = []
    for s in data["sections"]:
        rows = []
        for r in s["rows"]:
            missing = f"(no price: {', '.join(m['name'] for m in r['missing'][:3])})" if r["missing"] else ""
            rows.append([short(r["name"]), gold(r["cost"]), gold(r["revenue"]), gold(r["profit"], True),
                         f"{r['sold_per_day']:.1f}", ", ".join(r["crafters"]), missing])
        lines += [section_title(s)] + table(["recipe", "cost", "sells for", "profit", "sold/day", "crafters", ""],
                                            rows, right={1, 2, 3, 4}) + [""]
    return "\n".join(lines + [data["note"]]) + "\n"


def deals_data(args) -> dict:
    conn = ledger.db_for(args)
    exports = [ex for ex in ledger.set_reports(conn, args) if ex.deals]
    out = {"kind": "deals", "sections": [], "hours": getattr(args, "hours", 72),
           "note": "Profit/unit is market value after the 5% AH cut, minus the listed price.",
           "empty": None if exports else
           "No deals right now (or not enough snapshots yet; sale rates need a few hours of data)."}
    spark = getattr(args, "history", False)
    for ex in exports:
        deals = ex.deals[:args.limit]
        names = ledger.item_names(conn, {d.item_id for d in deals}, args)
        rows = []
        for d in deals:
            row = {"item_id": d.item_id, "variant": d.variant, "name": names[d.item_id], "listed": d.current_min,
                   "market": d.market_value, "discount": round(d.discount, 4), "profit": d.profit_per_unit,
                   "sold_per_day": round(d.sold_per_day, 2)}
            if spark:
                row["history"] = history(conn, ex.set.markets, d.item_id, d.variant, out["hours"])
            rows.append(row)
        out["sections"].append(set_info(ex.set) | {"rows": rows})
    return out


def deals_text(data: dict) -> str:
    if data["empty"]:
        return data["empty"] + "\n"
    lines = []
    for s in data["sections"]:
        rows = [[short(r["name"]), gold(r["listed"]), gold(r["market"]), f"{r['discount']:.0%}",
                 gold(r["profit"], True), f"{r['sold_per_day']:.1f}"] for r in s["rows"]]
        lines += [section_title(s)] + table(["item", "listed at", "market", "off", "profit/unit", "sold/day"],
                                            rows, right={1, 2, 3, 4, 5}) + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Flips: commodities that swing between their own lows and highs
# ---------------------------------------------------------------------------

def flips_data(args) -> dict:
    """Index cells are UTC (index[weekday][hour], 0 = Sunday): min price over the item's own median,
    the median across liquid items; None where there were no samples."""
    days = args.days or 7
    conn = ledger.db_for(args)
    mkts = args.market or [n for n, in conn.execute(
        "SELECT name FROM markets WHERE kind = 'commodities' AND COALESCE(game, 'retail') = 'retail' ORDER BY name")]
    out = {"kind": "flips", "days": days, "sections": [],
           "note": "Buy below = P25 min price, sell at = P75; net is after the 5% AH cut. Depth@low = units within "
                   "5% of the min while at the lows; profit/trip buys min(depth@low, moved/day).",
           "empty": None if mkts else "No retail commodity market configured."}
    for mkt in mkts:
        flips, index, liquid = pricing.flip_report(conn, mkt, days=days)
        names = ledger.item_names(conn, {f.item_id for f in flips[:args.limit]}, args)
        rows = [{"item_id": f.item_id, "variant": f.variant, "name": names[f.item_id], "buy_below": f.buy_below,
                 "sell_at": f.sell_at, "median": f.median, "margin": round(f.margin, 4), "trips": f.trips,
                 "drift": round(f.drift, 4), "depth_low": f.depth_low, "sold_per_day": round(f.sold_per_day, 1),
                 "moved_per_day": round(f.moved_per_day, 1), "profit_per_trip": f.profit_per_trip}
                for f in flips[:args.limit]]
        out["sections"].append({"market": mkt, "game": "retail", "liquid": liquid, "count": len(flips),
                                "rows": rows, "index": index})
    return out


def flips_text(data: dict, offset: int = 0) -> str:
    if data["empty"]:
        return data["empty"] + "\n"
    lines = []
    for s in data["sections"]:
        lines.append(f"{s['market']}: flips over the last {data['days']:g} days ({s['liquid']} liquid items, "
                     f"{s['count']} with 2+ round trips and 3%+ net)")
        if s["rows"]:
            rows = [[short(r["name"], 30), gold(r["buy_below"]), gold(r["sell_at"]), f"{r['margin']:.0%}",
                     r["trips"], f"{r['drift']:+.0%}", f"{r['depth_low']:,}" if r["depth_low"] is not None else "-",
                     f"{r['sold_per_day']:,.0f}", f"{r['moved_per_day']:,.0f}", gold(r["profit_per_trip"])]
                    for r in s["rows"]]
            lines += table(["item", "buy below", "sell at", "net", "trips", "drift", "depth@low", "sold/d",
                            "moved/d", "profit/trip"], rows, right=set(range(1, 10)))
            lines.append("  " + data["note"])
        local = pricing.to_local([v for row in s["index"] for v in row], offset)
        cells = [v for v in local if v is not None]
        if not cells:
            lines.append("")
            continue
        pct = lambda v: f"{(v - 1) * 100:>+7.1f}" if v is not None else f"{'':>7}"  # noqa: E731
        lines += ["", f"  Price index (min price / own median, median over liquid items), % vs median, UTC{offset:+d}",
                  "  hour " + "".join(f"{d:>7}" for d in DAYS)]
        for h in range(24):
            lines.append(f"  {h:02d}h  " + "".join(pct(local[d * 24 + h]) for d in range(7)))
        day_avg = []
        for d in range(7):
            vals = [v for v in local[d * 24:(d + 1) * 24] if v is not None]
            day_avg.append(sum(vals) / len(vals) if vals else None)
        lines.append("  avg  " + "".join(pct(v) for v in day_avg))
        lo, hi = min(cells), max(cells)
        li, hi_i = local.index(lo), local.index(hi)
        lines += [f"  cheapest {DAYS[li // 24]} {li % 24:02d}h ({(lo - 1) * 100:+.1f}%), dearest {DAYS[hi_i // 24]} "
                  f"{hi_i % 24:02d}h ({(hi - 1) * 100:+.1f}%): swing {(hi / lo - 1) * 100:.1f}% vs the 5% AH cut. "
                  f"US weekly reset is Tue 15h UTC.", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Ledger summary
# ---------------------------------------------------------------------------

def summary_data(args) -> dict:
    conn = ledger.db_for(args)
    days, char = getattr(args, "days", None), getattr(args, "char", None)
    since = int(time.time()) - days * 86400 if days else None
    txs = ledger.query_rows(conn, char=char, since=since)
    by_src: dict[str, list[int]] = {}
    for r in txs:
        s = by_src.setdefault(r["source"], [0, 0, 0])
        s[0 if r["delta"] > 0 else 1] += r["delta"]
        s[2] += 1 if r["delta"] else 0
    rows = [{"source": src, "in": i, "out": o, "net": i + o, "count": n}
            for src, (i, o, n) in sorted(by_src.items(), key=lambda kv: -abs(kv[1][0] + kv[1][1]))]
    total = {"in": sum(r["in"] for r in rows), "out": sum(r["out"] for r in rows)}
    total["net"] = total["in"] + total["out"]
    balances = [{"key": k, "name": first_name(k), "balance": b or 0, "last_seen": seen}
                for k, b, seen in conn.execute("SELECT key, balance, last_seen FROM characters ORDER BY balance DESC")]
    return {"kind": "summary", "days": days, "char": char, "entries": len(txs), "rows": rows, "total": total,
            "balances": balances, "empty": None if txs else "No transactions in range."}


def summary_text(data: dict) -> str:
    if data["empty"]:
        return data["empty"] + "\n"
    span = f"last {data['days']} days" if data["days"] else "all time"
    t = data["total"]
    rows = [[r["source"], gold(r["in"]), gold(r["out"]), gold(r["net"], True), r["count"]] for r in data["rows"]]
    rows.append(["TOTAL", gold(t["in"]), gold(t["out"]), gold(t["net"], True), ""])
    lines = [f"{data['entries']} entries, {span}" + (f", {data['char']}" if data["char"] else "")]
    lines += table(["source", "in", "out", "net", "count"], rows, right={1, 2, 3, 4})
    lines += ["", "Balances:"]
    bal = [[b["key"], gold(b["balance"]),
            f"(as of {dt.datetime.fromtimestamp(b['last_seen']).strftime('%Y-%m-%d %H:%M') if b['last_seen'] else '?'})"]
           for b in data["balances"]]
    lines += table(["character", "gold", ""], bal, right={1}, indent="  ")[1:]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Price history and timing for one item
# ---------------------------------------------------------------------------

def price_data(args) -> dict:
    """Every change in an item's price and supply over the last args.hours, per market. Each
    market's first point is the state carried in from before the window (flagged carried)."""
    conn = ledger.db_for(args)
    hours = getattr(args, "hours", 72)
    since = int(time.time()) - hours * 3600
    ids = ledger.resolve_items(conn, args.item)
    out = {"kind": "price", "item": args.item, "hours": hours, "items": [],
           "empty": None if ids else
           "No matching item (names are known for items you've traded; otherwise pass the item id)."}
    names = ledger.item_names(conn, ids[:10]) if ids else {}
    games = dict(conn.execute("SELECT name, game FROM markets"))
    for item_id in ids[:10]:
        markets = {}
        for m, mid in store.market_ids(conn).items():
            pts = conn.execute("""
                SELECT s.last_modified, st.min_price, st.p10, st.median, st.quantity, st.sold + st.gone
                FROM item_states st JOIN snapshots s ON s.id = st.snap
                WHERE st.market_id = ? AND st.item_id = ? AND s.last_modified >= ?
                ORDER BY s.last_modified""", (mid, item_id, since)).fetchall()
            if not pts:
                continue
            prev = conn.execute("""
                SELECT st.min_price, st.p10, st.median, st.quantity
                FROM item_states st JOIN snapshots s ON s.id = st.snap
                WHERE st.market_id = ? AND st.item_id = ? AND s.last_modified < ?
                ORDER BY s.last_modified DESC LIMIT 1""", (mid, item_id, since)).fetchone()
            rows = [{"t": since, "min": prev[0], "p10": prev[1], "median": prev[2], "listed": prev[3],
                     "sold": 0, "carried": True}] if prev else []
            rows += [{"t": t, "min": mn, "p10": p10, "median": med, "listed": qty, "sold": sold}
                     for t, mn, p10, med, qty, sold in pts]
            markets[m] = {"market": m, "game": games.get(m), "points": rows}
        out["items"].append({"item_id": item_id, "name": names.get(item_id, f"item {item_id}"),
                             "markets": sorted(markets.values(), key=lambda x: x["market"])})
    return out


def price_text(data: dict) -> str:
    if data["empty"]:
        return data["empty"] + "\n"
    lines = []
    for it in data["items"]:
        lines += ["", f"{it['name']} ({it['item_id']})"]
        rows = []
        for m in it["markets"]:
            for p in m["points"]:
                if p.get("carried"):
                    continue
                rows.append([m["market"], dt.datetime.fromtimestamp(p["t"]).strftime("%m-%d %H:%M"),
                             gold(p["min"]), gold(p["p10"]), gold(p["median"]), f"{p['listed']:,}", p["sold"]])
        if not rows:
            lines.append("  no changes in range (prices are stored when they change)")
            continue
        lines += table(["market", "changed at", "min", "p10", "median", "listed", "sold"], rows,
                       right={2, 3, 4, 5, 6}, indent="  ")
    return "\n".join(lines) + "\n"


def timing_data(args) -> dict:
    """Hour-of-week buckets per market, rotated by args.utc_offset (0 = UTC)."""
    offset = getattr(args, "utc_offset", 0)
    conn = ledger.db_for(args)
    ids = ledger.resolve_items(conn, args.item)
    if not ids:
        return {"kind": "timing", "empty": "No matching item (pass the item id if its name isn't known yet).",
                "markets": []}
    item_id = ids[0]
    name = ledger.item_names(conn, [item_id], args)[item_id]
    games = dict(conn.execute("SELECT name, game FROM markets"))
    profiles = pricing.timing(conn, item_id, markets=getattr(args, "market", None) or None)
    return {"kind": "timing", "empty": None, "item_id": item_id, "name": name, "utc_offset_hours": offset,
            "markets": [{"market": m, "game": games.get(m), "buckets": pricing.to_local(b, offset)}
                        for m, b in profiles.items()]}


def timing_text(data: dict) -> str:
    if data["empty"]:
        return data["empty"] + "\n"
    offset = data["utc_offset_hours"]
    lines = [f"{data['name']} ({data['item_id']}) - UTC{offset:+d}"]
    if not data["markets"]:
        lines.append("  No sales recorded yet for this item. Timing needs a few days of hourly snapshots.")
    for m in data["markets"]:
        local = m["buckets"]
        peak = max((b["sold_avg"] or 0) for b in local) or 1
        lines += ["", f"{m['market']} ({m['game']}): average units sold per hour",
                  "      " + "".join(f"{h:<3}" for h in range(0, 24, 3)).ljust(24) + "  best hours"]
        for d in range(7):
            row = local[d * 24:(d + 1) * 24]
            cells = "".join(SHADES[min(int((b["sold_avg"] or 0) / peak * (len(SHADES) - 1) + 0.5), len(SHADES) - 1)]
                            if b["samples"] else " " for b in row)
            best = sorted(range(24), key=lambda h: -(row[h]["sold_avg"] or 0))[:3]
            best_txt = ", ".join(f"{h:02d}h ({row[h]['sold_avg']:.1f})" for h in best if row[h]["sold_avg"])
            day_total = sum(b["sold_avg"] or 0 for b in row)
            lines.append(f"  {DAYS[d]} {cells}  {best_txt}  [day ~{day_total:.0f}]")
        cheapest = min((b for b in local if b["p10_avg"]), key=lambda b: b["p10_avg"], default=None)
        if cheapest:
            i = local.index(cheapest)
            lines.append(f"  cheapest slot: {DAYS[i // 24]} {i % 24:02d}h (avg p10 {gold(cheapest['p10_avg'])})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Registry and CLI-style entry points
# ---------------------------------------------------------------------------

DATA = {"crafts": crafts_data, "deals": deals_data, "flips": flips_data, "summary": summary_data,
        "price": price_data, "timing": timing_data}


def text(kind: str, data: dict, args=None) -> str:
    if kind == "flips":
        return flips_text(data, getattr(args, "utc_offset", 0))
    return {"crafts": crafts_text, "deals": deals_text, "summary": summary_text, "price": price_text,
            "timing": timing_text}[kind](data)


def run(kind: str, args) -> int:
    data = DATA[kind](args)
    print(json.dumps(data) if getattr(args, "json", False) else text(kind, data, args), end="")
    return 1 if kind in ("price", "timing") and data["empty"] else 0


def crafts_cmd(args) -> int: return run("crafts", args)
def deals_cmd(args) -> int: return run("deals", args)
def flips_cmd(args) -> int: return run("flips", args)
def summary_cmd(args) -> int: return run("summary", args)
def price_cmd(args) -> int: return run("price", args)
def timing_cmd(args) -> int: return run("timing", args)
