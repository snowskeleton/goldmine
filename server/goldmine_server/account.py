"""Read-only account state for the trader (docs/trader.md): balances, inventory, owned auctions,
the ledger, recipes with crafting operation info, craft results, the WoW token price, and item
metadata, as JSON for the MCP tools in mcp_server.py.

The functions take a connection from marketdata.ro_conn(user_id) (market.sqlite with the user's
game DB attached as "u", both read-only), except token_price, which only needs market.sqlite.
Money is copper; times are unix seconds with an ISO 8601 UTC copy (`*_iso`).
"""

from __future__ import annotations

import json
import sqlite3
import time

from fastapi import HTTPException

from . import ledger, market, settings
from .marketdata import MAX_ROWS, _clamp, _names, iso, resolve_items

DEFAULT_LIMIT = 200


def _since(value, default_days: float | None = None) -> int | None:
    """Unix seconds, or "7d" / "12h" / an ISO date; None -> `default_days` ago (or no limit)."""
    if value is None or value == "":
        return int(time.time() - default_days * 86400) if default_days else None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(value)
    try:
        return ledger.parse_since(str(value))
    except ValueError as exc:
        raise HTTPException(400, f"since: expected unix seconds, 7d/12h, or an ISO date, not {value!r}") from exc


def _char_clause(char: str | None, column: str = "char_key") -> tuple[str, list]:
    if not char:
        return "", []
    return f" AND {column} LIKE ?", [char if "-" in char else f"{char}-%"]


def _page(rows: list, limit: int | None, offset: int, default: int = DEFAULT_LIMIT) -> tuple[list, dict]:
    limit, offset = _clamp(limit, 1, MAX_ROWS, default), max(0, int(offset or 0))
    page = rows[offset:offset + limit]
    nxt = offset + len(page)
    return page, {"total": len(rows), "offset": offset, "next_offset": nxt if nxt < len(rows) else None}


# ---------------------------------------------------------------------------
# Balances, inventory, owned auctions
# ---------------------------------------------------------------------------

def account(conn, realm: str | None = None) -> dict:
    """Gold per character and in the warband bank, with when each was last seen, and the latest
    WoW token price for the characters' regions."""
    sql = "SELECT key, name, realm, class, faction, region, balance, last_seen FROM characters"
    params = []
    if realm:
        sql += " WHERE realm = ? COLLATE NOCASE OR replace(realm, ' ', '') = ? COLLATE NOCASE"
        params = [realm, realm.replace(" ", "")]
    chars = conn.execute(sql + " ORDER BY balance DESC", params).fetchall()
    seen = lambda q: dict(conn.execute(q))  # noqa: E731
    bags = seen("SELECT owner, MAX(updated) FROM inventory WHERE location = 'bags' GROUP BY owner")
    bank = seen("SELECT owner, MAX(updated) FROM inventory WHERE location = 'bank' GROUP BY owner")
    lists = seen("SELECT char_key, ts FROM owned_auction_lists")
    profs = seen("SELECT char_key, MAX(scanned) FROM professions GROUP BY char_key")
    out = []
    for key, name, rlm, cls, faction, region, balance, last_seen in chars:
        out.append({"character": key, "name": name, "realm": rlm, "class": cls, "faction": faction,
                    "region": region, "gold": balance or 0, "last_seen": last_seen, "last_seen_iso": iso(last_seen),
                    "bags_seen": bags.get(key), "bank_seen": bank.get(key), "auctions_seen": lists.get(key),
                    "professions_seen": profs.get(key)})
    wb = conn.execute("SELECT money, updated, seen_by FROM warband_money WHERE id = 1").fetchone()
    wb_items = conn.execute("SELECT MAX(updated) FROM inventory WHERE owner = 'warband'").fetchone()[0]
    warband = {"gold": wb[0] if wb else None, "seen": wb[1] if wb else None, "seen_iso": iso(wb[1]) if wb else None,
               "seen_by": wb[2] if wb else None, "items_seen": wb_items}
    regions = sorted({(c["region"] or "us").lower() for c in out}) or ["us"]
    tokens = {r: token_price(conn, r, limit=1)["latest"] for r in regions}
    return {"characters": out, "warband": warband,
            "total_gold": sum(c["gold"] for c in out) + (warband["gold"] or 0), "wow_token": tokens}


def inventory(conn, char: str | None = None, location: str | None = None, items=None,
              limit: int | None = None, offset: int = 0) -> dict:
    """Item counts across bags, bank and the warband bank, one entry per item with who holds it."""
    sql, params = "SELECT owner, location, item_id, count, updated FROM inventory WHERE 1=1", []
    if char:
        clause, p = _char_clause(char, "owner")
        sql, params = sql + clause, params + p
    if location:
        if location not in ("bags", "bank", "warband"):
            raise HTTPException(400, "location must be bags, bank or warband")
        sql, params = sql + " AND location = ?", params + [location]
    if items:
        ids = resolve_items(conn, items)
        sql, params = sql + f" AND item_id IN ({','.join('?' * len(ids))})", params + ids
    by_item: dict[int, dict] = {}
    for owner, loc, item, count, updated in conn.execute(sql, params):
        e = by_item.setdefault(item, {"item_id": item, "count": 0, "held": []})
        e["count"] += count
        e["held"].append({"owner": owner, "location": loc, "count": count, "seen": updated})
    rows = sorted(by_item.values(), key=lambda e: (-e["count"], e["item_id"]))
    page, meta = _page(rows, limit, offset)
    names = _names(conn, [e["item_id"] for e in page])
    for e in page:
        e["name"] = names.get(e["item_id"])
    return {**meta, "items": page}


def owned_auctions(conn, char: str | None = None, include_ended: bool = False, since=None,
                   limit: int | None = None, offset: int = 0) -> dict:
    """Your auctions as of each character's latest Auctions-tab list. With include_ended, also
    auctions that were in an earlier list but not the latest (sold, expired or cancelled)."""
    clause, params = _char_clause(char, "a.char_key")
    lists = conn.execute(f"SELECT char_key, ts, count FROM owned_auction_lists a WHERE 1=1{clause} ORDER BY char_key",
                         params).fetchall()
    sql = f"""SELECT a.*, l.ts FROM owned_auctions a LEFT JOIN owned_auction_lists l ON l.char_key = a.char_key
              WHERE 1=1{clause}"""
    if not include_ended:
        sql += " AND a.last_seen = l.ts"
    else:
        since_ts = _since(since, 14)
        sql += " AND (a.last_seen = l.ts OR a.last_seen >= ?)"
        params = params + [since_ts]
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql + " ORDER BY a.char_key, a.item_id, a.unit_price", params).fetchall()
    finally:
        conn.row_factory = None
    page, meta = _page(rows, limit, offset)
    names = _names(conn, [r["item_id"] for r in page])
    status = {0: "active", 1: "sold"}
    out = []
    for r in page:
        current = r["last_seen"] == r["ts"]
        expires = r["last_seen"] + r["time_left"] if r["time_left"] is not None else None
        out.append({"auction_id": r["auction_id"], "character": r["char_key"], "item_id": r["item_id"],
                    "name": names.get(r["item_id"]), "variant": r["variant"], "item_level": r["item_level"],
                    "quantity": r["quantity"], "unit_price": r["unit_price"], "buyout": r["buyout"], "bid": r["bid"],
                    "commodity": None if r["commodity"] is None else bool(r["commodity"]),
                    "status": status.get(r["status"], r["status"]), "current": current,
                    "time_left": r["time_left"], "expires": expires, "expires_iso": iso(expires),
                    "first_seen": r["first_seen"], "last_seen": r["last_seen"], "last_seen_iso": iso(r["last_seen"])})
    return {"lists": [{"character": k, "as_of": ts, "as_of_iso": iso(ts), "count": n} for k, ts, n in lists],
            **meta, "auctions": out}


# ---------------------------------------------------------------------------
# Ledger, recipes, crafts
# ---------------------------------------------------------------------------

def ledger_rows(conn, since=None, until=None, source: str | None = None, item=None, char: str | None = None,
                limit: int | None = None, offset: int = 0) -> dict:
    """Gold changes (newest first) with per-source totals for everything the filters match."""
    since_ts, until_ts = _since(since, 7), _since(until)
    sql, params = "SELECT * FROM transactions WHERE ts >= ?", [since_ts]
    if until_ts is not None:
        sql, params = sql + " AND ts < ?", params + [until_ts]
    if source:
        sql, params = sql + " AND source = ?", params + [source]
    if item:
        ids = resolve_items(conn, item)
        sql, params = sql + f" AND item_id IN ({','.join('?' * len(ids))})", params + ids
    clause, p = _char_clause(char)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql + clause + " ORDER BY ts DESC, char_key, seq DESC", params + p).fetchall()
    finally:
        conn.row_factory = None
    totals: dict[str, dict] = {}
    for r in rows:
        t = totals.setdefault(r["source"], {"source": r["source"], "in": 0, "out": 0, "count": 0})
        t["in" if r["delta"] > 0 else "out"] += r["delta"]
        t["count"] += 1
    page, meta = _page(rows, limit, offset)
    return {"since": since_ts, "since_iso": iso(since_ts), "until": until_ts, "until_iso": iso(until_ts), **meta,
            "totals": sorted(totals.values(), key=lambda t: -abs(t["in"] + t["out"])),
            "transactions": [{"id": r["id"], "character": r["char_key"], "time": r["ts"], "time_iso": iso(r["ts"]),
                              "delta": r["delta"], "balance": r["balance"], "source": r["source"],
                              "item_id": r["item_id"], "item_name": r["item_name"], "zone": r["zone"],
                              "meta": json.loads(r["meta"]) if r["meta"] else None} for r in page]}


_OP_COLS = ("base_skill", "bonus_skill", "difficulty", "bonus_difficulty", "quality", "quality_id", "lower_skill",
            "upper_skill", "quality_craft", "concentration_cost", "ingenuity_refund", "multicraft", "resourcefulness",
            "stats", "updated")


def recipes(conn, char: str | None = None, profession: str | None = None, item=None,
            limit: int | None = None, offset: int = 0) -> dict:
    """Known recipes with reagents and crafting operation info (as of the last time the profession
    window was open), plus each profession's concentration."""
    clause, params = _char_clause(char, "r.char_key")
    sql = f"""SELECT r.char_key, r.recipe_id, r.profession, r.name, r.output_item, r.output_qty,
                     {', '.join('o.' + c for c in _OP_COLS)}
              FROM recipes r LEFT JOIN recipe_ops o ON o.char_key = r.char_key AND o.recipe_id = r.recipe_id
              WHERE 1=1{clause}"""
    if profession:
        sql, params = sql + " AND r.profession = ? COLLATE NOCASE", params + [profession]
    if item:
        ids = resolve_items(conn, item)
        marks = ",".join("?" * len(ids))
        sql += f""" AND (r.output_item IN ({marks}) OR EXISTS (SELECT 1 FROM recipe_reagents g WHERE
                    g.char_key = r.char_key AND g.recipe_id = r.recipe_id AND g.item_id IN ({marks})))"""
        params += ids + ids
    rows = conn.execute(sql + " ORDER BY r.char_key, r.profession, r.name", params).fetchall()
    page, meta = _page(rows, limit, offset)
    reagents: dict[tuple, list] = {}
    keys = {(r[0], r[1]) for r in page}
    for key, rid, item_id, qty in conn.execute("SELECT char_key, recipe_id, item_id, quantity FROM recipe_reagents"):
        if (key, rid) in keys:
            reagents.setdefault((key, rid), []).append((item_id, qty))
    names = _names(conn, [r[4] for r in page if r[4]] + [i for v in reagents.values() for i, _ in v])
    out = []
    for key, rid, prof, name, output, qty, *op in page:
        o = dict(zip(_OP_COLS, op))
        if o["updated"] is None and o["base_skill"] is None:
            o = None
        else:
            o["stats"] = json.loads(o["stats"]) if o["stats"] else {}
            o["quality_craft"] = bool(o["quality_craft"])
            o["skill"] = (o["base_skill"] or 0) + (o["bonus_skill"] or 0)
        out.append({"character": key, "recipe_id": rid, "profession": prof, "name": name, "output_item": output,
                    "output_name": names.get(output), "output_qty": qty,
                    "reagents": [{"item_id": i, "name": names.get(i), "quantity": q}
                                 for i, q in reagents.get((key, rid), [])],
                    "operation": o})
    clause, params = _char_clause(char)
    profs = conn.execute(f"""SELECT char_key, profession, scanned, concentration, concentration_max FROM professions
                             WHERE 1=1{clause} ORDER BY char_key, profession""", params).fetchall()
    return {"professions": [{"character": k, "profession": p, "scanned": t, "scanned_iso": iso(t),
                             "concentration": c, "concentration_max": m} for k, p, t, c, m in profs],
            **meta, "recipes": out}


def craft_results(conn, since=None, char: str | None = None, recipe: int | None = None, item=None,
                  limit: int | None = None, offset: int = 0) -> dict:
    """Crafts you made (newest first), and per recipe: crafts, items made, proc rates, quality."""
    since_ts = _since(since, 30)
    clause, params = _char_clause(char)
    sql, params = f"SELECT * FROM craft_results WHERE ts >= ?{clause}", [since_ts] + params
    if recipe is not None:
        sql, params = sql + " AND recipe_id = ?", params + [int(recipe)]
    if item:
        ids = resolve_items(conn, item)
        sql, params = sql + f" AND item_id IN ({','.join('?' * len(ids))})", params + ids
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql + " ORDER BY ts DESC, id DESC", params).fetchall()
    finally:
        conn.row_factory = None
    stats: dict = {}
    for r in rows:
        s = stats.setdefault(r["recipe_id"], {"recipe_id": r["recipe_id"], "item_id": r["item_id"], "crafts": 0,
                                              "items": 0, "multicraft_procs": 0, "resourcefulness_procs": 0,
                                              "ingenuity_procs": 0, "concentration": 0, "_q": []})
        s["crafts"] += 1
        s["items"] += r["quantity"] or 0
        s["multicraft_procs"] += 1 if r["multicraft"] else 0
        s["resourcefulness_procs"] += 1 if r["returned"] not in (None, "[]") else 0
        s["ingenuity_procs"] += r["ingenuity"] or 0
        s["concentration"] += r["concentration"] or 0
        if r["quality"] is not None:
            s["_q"].append(r["quality"])
    for s in stats.values():
        q = s.pop("_q")
        s["avg_quality"] = round(sum(q) / len(q), 2) if q else None
        s["multicraft_rate"] = round(s["multicraft_procs"] / s["crafts"], 3)
        s["resourcefulness_rate"] = round(s["resourcefulness_procs"] / s["crafts"], 3)
    page, meta = _page(rows, limit, offset)
    names = _names(conn, [r["item_id"] for r in page if r["item_id"]] + [s["item_id"] for s in stats.values()])
    for s in stats.values():
        s["name"] = names.get(s["item_id"])
    return {"since": since_ts, "since_iso": iso(since_ts), "by_recipe": sorted(stats.values(), key=lambda s: -s["crafts"]),
            **meta,
            "crafts": [{"id": r["id"], "character": r["char_key"], "time": r["ts"], "time_iso": iso(r["ts"]),
                        "recipe_id": r["recipe_id"], "item_id": r["item_id"], "name": names.get(r["item_id"]),
                        "quantity": r["quantity"], "quality": r["quality"], "multicraft": r["multicraft"],
                        "concentration": r["concentration"], "ingenuity": bool(r["ingenuity"]),
                        "returned": [{"item_id": i, "quantity": q} for i, q in json.loads(r["returned"] or "[]")]}
                       for r in page]}


# ---------------------------------------------------------------------------
# WoW token, item metadata
# ---------------------------------------------------------------------------

def token_price(conn, region: str = "us", since=None, limit: int | None = None) -> dict:
    """The latest WoW token price and its history (a row per price change), newest first."""
    region = (region or "us").lower()
    since_ts = _since(since, 30)
    rows = conn.execute("SELECT updated, price FROM token_prices WHERE region = ? AND updated >= ? "
                        "ORDER BY updated DESC LIMIT ?", (region, since_ts, _clamp(limit, 1, MAX_ROWS, 500))).fetchall()
    last = conn.execute("SELECT updated, price, fetched FROM token_prices WHERE region = ? ORDER BY updated DESC LIMIT 1",
                        (region,)).fetchone()
    latest = {"price": last[1], "gold": last[1] // 10000, "updated": last[0], "updated_iso": iso(last[0])} if last else None
    return {"region": region, "latest": latest, "since": since_ts, "since_iso": iso(since_ts),
            "history": [{"updated": t, "updated_iso": iso(t), "price": p} for t, p in rows]}


BIND_TYPES = {0: None, 1: "ON_ACQUIRE", 2: "ON_EQUIP", 3: "ON_USE", 4: "QUEST", 7: "TO_ACCOUNT",
              8: "TO_BNETACCOUNT", 9: "TO_ACCOUNT_UNTIL_EQUIPPED"}  # Enum.ItemBind -> the API's binding.type


def item_info(conn, items) -> dict:
    """Vendor prices, binding, stack size, class and expansion: the Blizzard item API's answer
    (cached by the collector for items you own or craft) merged with what the game client
    reported (stack size and expansion only come from there)."""
    ids = resolve_items(conn, items)
    marks = ",".join("?" * len(ids))
    api = {r[0]: r for r in conn.execute(f"""SELECT item_id, sell_price, purchase_price, binding, class_id, class_name,
                                                    subclass_id, subclass_name, stackable, max_count, level,
                                                    inventory_type, fetched
                                             FROM item_info WHERE item_id IN ({marks})""", ids)}
    game = {r[0]: r for r in conn.execute(f"""SELECT item_id, stack_size, sell_price, class_id, subclass_id, bind_type,
                                                     expansion, updated FROM item_meta WHERE item_id IN ({marks})""", ids)}
    quality = dict(conn.execute(f"SELECT item_id, quality FROM items WHERE item_id IN ({marks})", ids))
    names = _names(conn, ids)
    out = []
    for i in ids:
        a, g = api.get(i), game.get(i)
        bind = a[3] if a and a[12] and a[3] is not None else (BIND_TYPES.get(g[5], g[5]) if g else None)
        stack = g[1] if g else (1 if a and a[8] == 0 else None)
        out.append({"item_id": i, "name": names.get(i), "quality": quality.get(i),
                    "vendor_sell": a[1] if a and a[1] is not None else (g[2] if g else None),
                    "vendor_buy": a[2] if a else None, "binding": bind,
                    "bound": None if not (a or g) else bind not in (None, "ON_EQUIP", "ON_USE"),
                    "stack_size": stack, "stackable": (a[8] == 1) if a and a[8] is not None else
                    (stack > 1 if stack else None),
                    "class_id": a[4] if a and a[4] is not None else (g[3] if g else None),
                    "class": a[5] if a else None,
                    "subclass_id": a[6] if a and a[6] is not None else (g[4] if g else None),
                    "subclass": a[7] if a else None, "expansion": g[6] if g else None,
                    "level": a[10] if a else None, "inventory_type": a[11] if a else None,
                    "max_count": a[9] if a else None,
                    "sources": [s for s, have in (("api", a and a[12]), ("game", g)) if have]})
    return {"items": out}


# ---------------------------------------------------------------------------
# Collector: item metadata for what the users own or craft
# ---------------------------------------------------------------------------

_ITEM_QUERIES = ("SELECT item_id FROM inventory", "SELECT item_id FROM owned_auctions",
                 "SELECT output_item FROM recipes WHERE output_item IS NOT NULL", "SELECT item_id FROM recipe_reagents",
                 "SELECT item_id FROM craft_results WHERE item_id IS NOT NULL")


def owned_item_ids() -> set[int]:
    """Every item any user holds, has listed, or crafts (with or from)."""
    ids: set[int] = set()
    for path in sorted(settings.USERS_DIR.glob("*/game.sqlite")):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            continue
        try:
            for q in _ITEM_QUERIES:
                try:
                    ids.update(r[0] for r in conn.execute(q))
                except sqlite3.OperationalError:  # older file without that table yet
                    pass
        finally:
            conn.close()
    return ids


def refresh_item_info(conn, client, cfg: dict, log=print) -> int:
    """Cache Blizzard item details for owned items that don't have them yet (bounded per run)."""
    retail = [m for m in cfg.get("market", []) if m.get("namespace") == f"dynamic-{m.get('region')}"]
    if not retail:
        return 0
    n = market.fetch_item_info(conn, client, retail[0]["region"], retail[0]["namespace"], owned_item_ids(),
                               log=log)
    if n:
        log(f"item info: {n} items cached")
    return n
