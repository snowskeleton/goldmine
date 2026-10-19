"""Read-only market data for tools and assistants: markets, item search, current prices, price
history, and the pricing.py outputs (market values, deals, crafts) as JSON.

Everything here runs on a read-only connection to market.sqlite (mode=ro + query_only), so
these paths can never write to it. Prices are copper per unit; times are unix seconds, with an
ISO 8601 UTC copy next to each one (`*_iso`).

The functions take plain arguments and return dicts, so the HTTP routes below (/api/v1/market/*)
and the MCP tools (mcp_server.py) share them.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from urllib.request import pathname2url

from fastapi import APIRouter, Depends, HTTPException, Query

from . import auth, db, ledger, pricing, services, settings, store
from .auth import User

MAX_ITEMS = 100          # items per current/values request
MAX_SEARCH = 100
MAX_ROWS = 1000          # history rows per page
MAX_WINDOW_DAYS = 90
RESOLUTIONS = ("raw", "hourly", "daily")


def iso(ts: int | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _ro_uri(path) -> str:
    return f"file:{pathname2url(str(path))}?mode=ro"


def ro_conn(user_id: int | None = None) -> sqlite3.Connection:
    """market.sqlite opened read-only; with user_id, that user's game DB attached as "u" (also
    read-only), the same layout reports use."""
    if not settings.MARKET_DB.exists():
        raise HTTPException(503, "no market data yet")
    conn = sqlite3.connect(_ro_uri(settings.MARKET_DB), uri=True, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA query_only = 1")
    if user_id is not None:
        path = db.user_db_path(user_id)
        ledger.connect(str(path)).close()  # create the user's (empty) game tables on first use
        conn.execute("ATTACH DATABASE ? AS u", (_ro_uri(path),))
    return conn


def _clamp(v: int | None, lo: int, hi: int, default: int) -> int:
    return default if v is None else max(lo, min(int(v), hi))


def _market_mid(conn, market: str) -> int:
    row = conn.execute("SELECT mid FROM markets WHERE name = ? AND mid IS NOT NULL", (market,)).fetchone()
    if not row:
        names = [r[0] for r in conn.execute("SELECT name FROM markets ORDER BY name")]
        raise HTTPException(404, f"unknown market {market!r}; markets: {', '.join(names)}")
    return row[0]


def _latest(conn, market: str) -> int | None:
    return conn.execute("SELECT MAX(last_modified) FROM snapshots WHERE market = ?", (market,)).fetchone()[0]


def _table(conn, market: str, hours: int) -> tuple[dict, int | None]:
    """pricing.price_table for one market (API snapshots only, no in-game scans), over the
    `hours` ending at the market's latest snapshot (so a stalled collector still answers)."""
    end = _latest(conn, market)
    return (pricing.price_table(conn, hours, [market], now=end + 1, scan_chars=[]) if end else {}), end


def _names(conn, ids) -> dict[int, str | None]:
    ids = list(set(ids))
    out: dict[int, str | None] = {i: None for i in ids}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(conn.execute(f"SELECT item_id, name FROM items WHERE item_id IN ({','.join('?' * len(chunk))})",
                                chunk))
    return out


def resolve_items(conn, items) -> list[int]:
    """Item ids, or names (exact match first, else the best partial match), in order."""
    if isinstance(items, (str, int)):
        items = [items]
    out = []
    for raw in items:
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            if part.isdigit():
                out.append(int(part))
                continue
            row = conn.execute("SELECT item_id FROM items WHERE name = ? COLLATE NOCASE ORDER BY item_id LIMIT 1",
                               (part,)).fetchone() or conn.execute(
                "SELECT item_id FROM items WHERE name LIKE ? ORDER BY length(name), item_id LIMIT 1",
                (f"%{part}%",)).fetchone()
            if not row:
                raise HTTPException(404, f"no known item named {part!r} (pass the item id instead)")
            out.append(row[0])
    if not out:
        raise HTTPException(400, "no items given")
    if len(out) > MAX_ITEMS:
        raise HTTPException(400, f"at most {MAX_ITEMS} items per request")
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def list_markets(conn) -> dict:
    rows = conn.execute("""
        SELECT m.name, m.kind, m.region, m.namespace, m.game, m.realms, COUNT(s.id), MIN(s.last_modified),
               MAX(s.last_modified), (SELECT COUNT(*) FROM item_current c WHERE c.market_id = m.mid)
        FROM markets m LEFT JOIN snapshots s ON s.market = m.name
        WHERE m.mid IS NOT NULL GROUP BY m.name ORDER BY m.name""").fetchall()
    return {"markets": [
        {"name": name, "kind": kind, "region": region, "namespace": ns, "game": game,
         "realms": json.loads(realms or "[]"), "snapshots": n, "first_snapshot": first,
         "first_snapshot_iso": iso(first), "last_snapshot": last, "last_snapshot_iso": iso(last),
         "items_listed": items}
        for name, kind, region, ns, game, realms, n, first, last, items in rows]}


def search_items(conn, query: str, limit: int | None = None, offset: int = 0) -> dict:
    """Items by id or (partial, case-insensitive) name. Only names the server has cached are
    searchable; ids always work."""
    query = (query or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    limit, offset = _clamp(limit, 1, MAX_SEARCH, 25), max(0, int(offset or 0))
    if query.isdigit():
        row = conn.execute("SELECT item_id, name, quality FROM items WHERE item_id = ?", (int(query),)).fetchone()
        rows, total = [row or (int(query), None, None)], 1
    else:
        like = f"%{query}%"
        total = conn.execute("SELECT COUNT(*) FROM items WHERE name LIKE ?", (like,)).fetchone()[0]
        rows = conn.execute("""SELECT item_id, name, quality FROM items WHERE name LIKE ?
                               ORDER BY name = ? COLLATE NOCASE DESC, name LIKE ? DESC, length(name), item_id
                               LIMIT ? OFFSET ?""", (like, query, f"{query}%", limit, offset)).fetchall()
    nxt = offset + len(rows)
    return {"query": query, "total": total, "offset": offset, "next_offset": nxt if nxt < total else None,
            "items": [{"item_id": i, "name": n, "quality": q} for i, n, q in rows]}


STATE_KEYS = store.STATE_FIELDS  # min_price, p10, median, quantity, auctions, depth5, depth10, depth25


def current_prices(conn, market: str, items) -> dict:
    """Latest state per item (and variant) in one market. `changed` is when that state was first
    seen; it holds through `as_of`, the market's latest snapshot."""
    mid = _market_mid(conn, market)
    ids = resolve_items(conn, items)
    last = _latest(conn, market)
    rows = conn.execute(f"""
        SELECT c.item_id, c.variant, s.last_modified, {', '.join('c.' + k for k in STATE_KEYS)}
        FROM item_current c JOIN snapshots s ON s.id = c.snap
        WHERE c.market_id = ? AND c.item_id IN ({','.join('?' * len(ids))})
        ORDER BY c.item_id, c.variant""", (mid, *ids)).fetchall()
    names = _names(conn, ids)
    found: dict[int, list] = {}
    for item, variant, changed, *state in rows:
        found.setdefault(item, []).append({"item_id": item, "variant": variant, "name": names.get(item),
                                           "listed": True, **dict(zip(STATE_KEYS, state)),
                                           "changed": changed, "changed_iso": iso(changed)})
    out = []
    for i in ids:
        out += found.get(i) or [{"item_id": i, "variant": 0, "name": names.get(i), "listed": False,
                                 **{k: None for k in STATE_KEYS}, "quantity": 0, "auctions": 0}]
    return {"market": market, "as_of": last, "as_of_iso": iso(last), "items": out}


def _bucket(t: int, resolution: str) -> int:
    return t // 3600 * 3600 if resolution == "hourly" else t // 86400 * 86400


def _aggregate(rows: list[dict], resolution: str) -> list[dict]:
    """Per bucket: lowest min_price, mean p10/median/quantity/auctions/depth over the snapshots
    in it, summed sold/gone, and the state at the bucket's last snapshot (close_*)."""
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(_bucket(r["time"], resolution), []).append(r)
    out = []
    for t, rs in groups.items():
        def mean(k):
            vals = [r[k] for r in rs if r[k] is not None]
            return round(sum(vals) / len(vals)) if vals else None
        mins = [r["min_price"] for r in rs if r["min_price"] is not None]
        out.append({"time": t, "time_iso": iso(t), "samples": len(rs), "min_price": min(mins) if mins else None,
                    **{k: mean(k) for k in STATE_KEYS if k != "min_price"},
                    "close_min_price": rs[-1]["min_price"], "close_quantity": rs[-1]["quantity"],
                    "sold": sum(r["sold"] for r in rs), "gone": sum(r["gone"] for r in rs)})
    return out


def price_history(conn, market: str, item, variant: int = 0, since: int | None = None, until: int | None = None,
                  resolution: str = "hourly", limit: int | None = None, offset: int = 0) -> dict:
    """One item's state at every snapshot in [since, until], rebuilt from item_states: a state
    row holds until the item's next row, so unchanged snapshots repeat it. sold/gone belong to
    the snapshot that recorded them (0 elsewhere). `hourly`/`daily` aggregate per UTC bucket."""
    if resolution not in RESOLUTIONS:
        raise HTTPException(400, f"resolution must be one of {', '.join(RESOLUTIONS)}")
    mid = _market_mid(conn, market)
    item_id = resolve_items(conn, item)[0]
    now = int(time.time())
    until = now if until is None else int(until)
    since = until - 7 * 86400 if since is None else int(since)
    if until - since > MAX_WINDOW_DAYS * 86400:
        raise HTTPException(400, f"window is limited to {MAX_WINDOW_DAYS} days")
    limit, offset = _clamp(limit, 1, MAX_ROWS, 500), max(0, int(offset or 0))
    snaps = conn.execute("SELECT id, last_modified FROM snapshots WHERE market = ? AND last_modified BETWEEN ? AND ? "
                         "ORDER BY id", (market, since, until)).fetchall()
    rows: list[dict] = []
    if snaps:
        cols = f"snap, {', '.join(STATE_KEYS)}, sold, gone"
        prior = conn.execute(f"SELECT {cols} FROM item_states WHERE market_id = ? AND item_id = ? AND variant = ? "
                             "AND snap < ? ORDER BY snap DESC LIMIT 1", (mid, item_id, variant, snaps[0][0])).fetchone()
        states = conn.execute(f"SELECT {cols} FROM item_states WHERE market_id = ? AND item_id = ? AND variant = ? "
                              "AND snap BETWEEN ? AND ? ORDER BY snap",
                              (mid, item_id, variant, snaps[0][0], snaps[-1][0])).fetchall()
        state, i = prior, 0
        for sid, lm in snaps:
            changed = False
            while i < len(states) and states[i][0] <= sid:
                state, changed = states[i], states[i][0] == sid
                i += 1
            if state is None:
                continue  # not seen in this market yet
            rows.append({"time": lm, "time_iso": iso(lm), "snapshot": sid, **dict(zip(STATE_KEYS, state[1:9])),
                         "sold": state[9] if changed else 0, "gone": state[10] if changed else 0,
                         "changed": changed})
    if resolution != "raw":
        rows = _aggregate(rows, resolution)
    total = len(rows)
    page = rows[offset:offset + limit]
    nxt = offset + len(page)
    return {"market": market, "item_id": item_id, "variant": variant, "name": _names(conn, [item_id])[item_id],
            "since": since, "since_iso": iso(since), "until": until, "until_iso": iso(until),
            "resolution": resolution, "total": total, "offset": offset,
            "next_offset": nxt if nxt < total else None, "rows": page}


def _price_dict(p: pricing.Price, name: str | None) -> dict:
    return {"item_id": p.item_id, "variant": p.variant, "name": name, "market_value": p.market_value,
            "current_min": p.current_min, "current_p10": p.current_p10, "buy": p.buy, "sell": p.sell,
            "sold_per_day": round(p.sold_per_day, 2), "as_of": p.as_of, "as_of_iso": iso(p.as_of)}


def market_values(conn, market: str, items=None, hours: int = 72, min_sold: float = 0.0,
                  limit: int | None = None, offset: int = 0) -> dict:
    """Goldmine's price model (see _table) for one market. With `items`, just those; otherwise
    every item, busiest (sold/day) first."""
    _market_mid(conn, market)
    hours = _clamp(hours, 1, 24 * 30, 72)
    table, end = _table(conn, market, hours)
    if items:
        ids = resolve_items(conn, items)
        by_item: dict[int, list] = {}
        for (i, _), p in sorted(table.items()):
            by_item.setdefault(i, []).append(p)
        prices = [p for i in ids for p in by_item.get(i) or [pricing.Price(i)]]
        total, offset = len(prices), 0
    else:
        limit, offset = _clamp(limit, 1, MAX_ITEMS, 50), max(0, int(offset or 0))
        ranked = sorted((p for p in table.values() if p.sold_per_day >= min_sold and (p.market_value or p.current_min)),
                        key=lambda p: (-p.sold_per_day, p.item_id))
        total, prices = len(ranked), ranked[offset:offset + limit]
    names = _names(conn, [p.item_id for p in prices])
    nxt = offset + len(prices)
    return {"market": market, "window_hours": hours, "as_of": end, "as_of_iso": iso(end), "total": total,
            "offset": offset, "next_offset": nxt if nxt < total else None,
            "items": [_price_dict(p, names.get(p.item_id)) for p in prices]}


def deals(conn, market: str, hours: int = 72, max_ratio: float = 0.7, min_sold: float = 1.0,
          limit: int | None = None) -> dict:
    """pricing.deal_report for one market: listed at <= max_ratio of market value, selling at
    least min_sold units/day. profit_per_unit is after the 5% AH cut."""
    _market_mid(conn, market)
    hours, limit = _clamp(hours, 1, 24 * 30, 72), _clamp(limit, 1, MAX_ITEMS, 40)
    table, end = _table(conn, market, hours)
    found = pricing.deal_report(table, max_ratio, min_sold)
    names = _names(conn, [d.item_id for d in found[:limit]])
    return {"market": market, "window_hours": hours, "as_of": end, "as_of_iso": iso(end),
            "max_ratio": max_ratio, "min_sold": min_sold,
            "total": len(found), "deals": [_deal_dict(d, names) for d in found[:limit]]}


def _deal_dict(d: pricing.Deal, names) -> dict:
    return {"item_id": d.item_id, "variant": d.variant, "name": names.get(d.item_id), "current_min": d.current_min,
            "market_value": d.market_value, "discount": round(d.discount, 3), "profit_per_unit": d.profit_per_unit,
            "sold_per_day": round(d.sold_per_day, 2)}


def crafts(conn, hours: int = 72, limit: int | None = None) -> dict:
    """The user's craft report per market set (needs `conn` from ro_conn(user_id)): every recipe
    their characters know, costed at current p10 and sold at min(market value, lowest) after
    the AH cut. Same numbers as `goldmine.py crafts`."""
    hours, limit = _clamp(hours, 1, 24 * 30, 72), _clamp(limit, 1, MAX_ITEMS, 40)
    exports = ledger.set_reports(conn, services.cli_args(conn, hours=hours))
    sets = []
    for ex in exports:
        if not ex.crafts:
            continue
        top = ex.crafts[:limit]
        names = _names(conn, {c.output_item for c in top} | {m for c in top for m in c.missing})
        st = ex.set
        sets.append({"set": {"kind": st.kind, "game": st.game, "region": st.region, "name": st.name,
                             "markets": st.markets, "realms": st.realms, "characters": st.chars},
                     "total": len(ex.crafts),
                     "crafts": [{"recipe_id": c.recipe_id, "name": c.name or names.get(c.output_item),
                                 "profession": c.profession, "crafters": c.crafters, "output_item": c.output_item,
                                 "output_qty": c.output_qty, "cost": c.cost, "revenue": c.revenue,
                                 "profit": c.profit, "sold_per_day": round(c.sold_per_day, 2),
                                 "missing_prices": [{"item_id": m, "name": names.get(m)} for m in c.missing]}
                                for c in top]})
    return {"window_hours": hours, "sets": sets}


# ---------------------------------------------------------------------------
# HTTP routes (client-key auth, like the rest of /api/v1)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/market")


def _run(fn, *args, user_id: int | None = None, **kw):
    conn = ro_conn(user_id)
    try:
        return fn(conn, *args, **kw)
    finally:
        conn.close()


@router.get("/markets")
def api_markets(user: User = Depends(auth.client_user)):
    return _run(list_markets)


@router.get("/items")
def api_items(q: str, limit: int | None = None, offset: int = 0, user: User = Depends(auth.client_user)):
    return _run(search_items, q, limit=limit, offset=offset)


@router.get("/current")
def api_current(market: str, item: list[str] = Query(...), user: User = Depends(auth.client_user)):
    return _run(current_prices, market, item)


@router.get("/history")
def api_history(market: str, item: str, variant: int = 0, since: int | None = None, until: int | None = None,
                resolution: str = "hourly", limit: int | None = None, offset: int = 0,
                user: User = Depends(auth.client_user)):
    return _run(price_history, market, item, variant=variant, since=since, until=until, resolution=resolution,
                limit=limit, offset=offset)


@router.get("/values")
def api_values(market: str, item: list[str] | None = Query(None), hours: int = 72, min_sold: float = 0.0,
               limit: int | None = None, offset: int = 0, user: User = Depends(auth.client_user)):
    return _run(market_values, market, item, hours=hours, min_sold=min_sold, limit=limit, offset=offset)


@router.get("/deals")
def api_deals(market: str, hours: int = 72, max_ratio: float = 0.7, min_sold: float = 1.0, limit: int | None = None,
              user: User = Depends(auth.client_user)):
    return _run(deals, market, hours=hours, max_ratio=max_ratio, min_sold=min_sold, limit=limit)


@router.get("/crafts")
def api_crafts(hours: int = 72, limit: int | None = None, user: User = Depends(auth.client_user)):
    return _run(crafts, hours=hours, limit=limit, user_id=user.id)
