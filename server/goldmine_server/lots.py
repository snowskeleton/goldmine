"""FIFO cost-basis lots from the ledger, realized P&L per item and strategy, and the scorecard.

What the addon records (Goldmine.lua) and how it's used here:
  auction_buy      delta = -paid. Commodity purchases carry meta {item_id, qty}: these open lots.
                   Item-auction buyouts (PlaceBid) carry no item, so they're reported as
                   unattributed purchases.
  auction_sale     from the AH invoice mail: delta = price + deposit - cut, meta {item (name),
                   price (total), deposit, cut, buyer} and, from newer addons, qty. Without qty the
                   units are estimated as price / the item's lowest listing at the time (flagged).
                   Units close the oldest lots first; sales with no open lot are "unmatched".
  auction_deposit  delta = -deposit, no item: account-level fee. Refunds arrive inside sales, so
                   net deposit loss = deposits paid - deposits refunded on sales.
  auction_cancel   delta = -fee, no item: account-level fee.

Crafts (craft_results) move cost from reagents to what was made: each craft takes its reagents,
less what resourcefulness returned, from the oldest lots, and opens a lot of the output (tag
"craft") at that cost divided by the units made. Reagents with no lot (farmed, vendor, mailed)
are costed at their lowest listing at the time, flagged estimated. Crafts recorded before the
addon kept reagents fall back to the recipe's basic reagents. A crafting order's output goes
to the customer, so it uses up reagents without opening a lot.

Strategy tags come later from task results; until then pass `tags`, mapping a lot ref (the
buy's transaction id) or an item id to a tag string or {"strategy": ..., "max_hold_days": ...}.
Everything else is "untagged".
"""

from __future__ import annotations

import json
import time

from . import archive, journal, sim, store

AH_SOURCES = ("auction_buy", "auction_sale", "auction_deposit", "auction_cancel")
DAY = sim.DAY


def _meta(raw) -> dict:
    try:
        m = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


def _tag_for(tags: dict, ref: str, item: int | None) -> tuple[str, float | None]:
    v = tags.get(ref)
    if v is None and item is not None:
        v = tags.get(str(item), tags.get(item))
    if isinstance(v, dict):
        return str(v.get("strategy") or "untagged"), v.get("max_hold_days")
    return (str(v) if v else "untagged"), None


def _name_ids(conn) -> dict[str, int]:
    """Lower-cased item name -> id, from the ledger's own links first, then the items cache."""
    out: dict[str, int] = {}
    try:
        for name, item in conn.execute("SELECT name, item_id FROM items WHERE name IS NOT NULL ORDER BY item_id DESC"):
            out[name.lower()] = item
    except Exception:  # noqa: BLE001 - no market tables attached
        pass
    for name, item in conn.execute("SELECT item_name, item_id FROM transactions WHERE item_id IS NOT NULL "
                                   "AND item_name IS NOT NULL"):
        out[name.lower()] = item
    return out


def _unit_price_at(conn, item: int, ts: int) -> int | None:
    """The item's lowest listing in the last snapshot at or before ts (any configured market)."""
    best = None  # (last_modified, min_price)
    try:
        for name, mid in store.market_ids(conn, sim.trader_markets(conn)).items():
            at = conn.execute("SELECT MAX(id) FROM snapshots WHERE market = ? AND last_modified <= ?",
                              (name, ts)).fetchone()[0]
            row = archive.last_state(conn, mid, item, 0, at, priced=True) if at is not None else None
            if row:
                t = conn.execute("SELECT last_modified FROM snapshots WHERE id = ?", (row[2],)).fetchone()[0]
                best = max(best or (t, row[3]), (t, row[3]))
    except Exception:  # noqa: BLE001 - no market tables attached
        return None
    return best[1] if best else None


def _window(realm, since, until) -> tuple[str, list]:
    sql, params = "", []
    if realm:
        sql += " AND lower(replace(replace(char_key, ' ', ''), '''', '')) LIKE ?"
        params.append("%-" + realm.lower().replace(" ", "").replace("'", ""))
    if since is not None:
        sql += " AND ts >= ?"
        params.append(int(since))
    if until is not None:
        sql += " AND ts < ?"
        params.append(int(until))
    return sql, params


def _crafts(conn, where: str, params: list) -> list[tuple]:
    """Crafts as (ts, char, id, recipe, item, qty, reagents, returned, source, estimated, order)."""
    rows = conn.execute("SELECT ts, char_key, id, recipe_id, item_id, quantity, reagents, returned, "
                        "reagents_estimated, order_id FROM craft_results WHERE 1 = 1" + where, params).fetchall()
    recipes: dict = {}
    for char, recipe, item, qty in conn.execute("SELECT char_key, recipe_id, item_id, quantity FROM recipe_reagents"):
        recipes.setdefault((char, recipe), []).append([item, qty])
    out = []
    for ts, char, cid, recipe, item, qty, reagents, returned, estimated, order in rows:
        if reagents is not None:
            used, source = json.loads(reagents), "recorded"
        else:
            used = recipes.get((char, recipe)) or next((v for (_, r), v in recipes.items() if r == recipe), None)
            source, estimated = ("recipe", 1) if used else (None, 0)
        out.append((ts, char, cid, recipe, item, qty or 1, used or [], json.loads(returned or "[]"), source,
                    bool(estimated), order))
    return out


def _craft(conn, st: dict, fifos: dict, tags: dict, max_hold_days: float, craft: tuple) -> None:
    """Take one craft's reagents from the lots and open a lot of what it made."""
    ts, char, cid, recipe, item, qty, reagents, returned, source, estimated, order = craft
    back: dict[int, int] = {}
    for i, n in returned:
        back[i] = back.get(i, 0) + n
    parts, cost, priced = [], 0.0, source is not None
    for i, n in reagents:
        use = n - back.pop(i, 0)
        if use <= 0:
            continue
        taken = fifos[i].take(use) if i in fifos else []
        got = sum(k for k, _ in taken)
        lot_cost = sum(k * lot.unit_cost for k, lot in taken)
        for k, lot in taken:
            st["crafted_with"][lot.tag] = st["crafted_with"].get(lot.tag, 0) + k
        short = use - got
        unit = _unit_price_at(conn, i, ts) if short > 1e-9 else None
        if short > 1e-9 and not unit:
            priced = False
        cost += lot_cost + (short * unit if unit else 0)
        parts.append({"item_id": i, "units": use, "from_lots": round(got), "cost_from_lots": round(lot_cost),
                      "estimated_units": round(short), "estimated_cost": round(short * unit) if unit else 0,
                      "unpriced": short > 1e-9 and not unit})
    st["crafts"].append({"id": cid, "character": char, "time": ts, "time_iso": sim.iso(ts), "recipe_id": recipe,
                         "item_id": item, "quantity": qty, "cost": round(cost), "unit_cost": round(cost / qty),
                         "reagents_source": source, "reagents_estimated": estimated, "order_id": order,
                         "fully_priced": priced, "reagents": parts})
    if order or not source or not item:
        return  # the customer's item, or nothing known about what it cost
    tag, hold = _tag_for(tags, cid, int(item))
    tag = "craft" if tag == "untagged" else tag
    fifos.setdefault(int(item), sim.Fifo()).add(
        sim.Lot(qty, cost / qty, ts, tag, cid, None, hold if hold is not None else max_hold_days))
    b = st["bought"].setdefault((tag, int(item)), [0, 0])
    b[0] += qty
    b[1] += cost


def build(conn, realm: str | None = None, tags: dict | None = None, max_hold_days: float = 7,
          since: int | None = None, until: int | None = None) -> dict:
    """Replay AH ledger rows and crafts into FIFO lots. -> internal state used by get_lots / get_scorecard."""
    tags = tags or {}
    where, params = _window(realm, since, until)
    sql = "SELECT ts, char_key, seq, id, delta, source, item_id, item_name, meta FROM transactions WHERE source IN (%s)" % (
        ",".join("?" * len(AH_SOURCES)))
    rows = [r[:3] + ("tx",) + r[3:] for r in conn.execute(sql + where, list(AH_SOURCES) + params)]
    # A craft sorts before ledger rows of the same second: its output may be posted right away.
    rows += [c[:2] + (-1, "craft", c) for c in _crafts(conn, where, params)]
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    names = None
    fifos: dict[int, sim.Fifo] = {}
    st = {"deposits_paid": 0, "deposits_refunded": 0, "cancel_fees": 0, "cuts": 0,
          "unattributed_purchases": {"count": 0, "spent": 0}, "unmatched_sales": [], "realized": [],
          "estimated_qty_sales": 0, "weekly": {}, "bought": {}, "crafts": [], "crafted_with": {}}
    for ts, char, _, kind, *rest in rows:
        if kind == "craft":
            _craft(conn, st, fifos, tags, max_hold_days, rest[0])
            continue
        tid, delta, source, item, item_name, meta = rest
        m = _meta(meta)
        week = time.strftime("%G-W%V", time.gmtime(ts))
        st["weekly"][week] = st["weekly"].get(week, 0) + delta
        if source == "auction_deposit":
            st["deposits_paid"] -= delta
        elif source == "auction_cancel":
            st["cancel_fees"] -= delta
        elif source == "auction_buy":
            item = item or m.get("item_id")
            qty = m.get("qty")
            if not item or not qty:
                st["unattributed_purchases"]["count"] += 1
                st["unattributed_purchases"]["spent"] -= delta
                continue
            tag, hold = _tag_for(tags, tid, int(item))
            fifos.setdefault(int(item), sim.Fifo()).add(
                sim.Lot(int(qty), -delta / int(qty), ts, tag, tid, None, hold if hold is not None else max_hold_days))
            b = st["bought"].setdefault((tag, int(item)), [0, 0])
            b[0] += int(qty)
            b[1] -= delta
        elif source == "auction_sale":
            price, cut, dep = m.get("price"), m.get("cut") or 0, m.get("deposit") or 0
            net = price - cut if isinstance(price, int) else delta - dep
            st["deposits_refunded"] += dep
            st["cuts"] += cut
            if item is None and item_name:
                if names is None:
                    names = _name_ids(conn)
                item = names.get(item_name.lower())
            qty = m.get("qty") or m.get("count")
            estimated = False
            if not qty:
                unit = _unit_price_at(conn, item, ts) if item and isinstance(price, int) else None
                qty = max(1, round(price / unit)) if unit else 1
                estimated = True
                st["estimated_qty_sales"] += 1
            qty = int(qty)
            taken = fifos[item].take(qty) if item in fifos else []
            matched = sum(k for k, _ in taken)
            for k, lot in taken:
                share = k / qty
                st["realized"].append({"item": item, "units": k, "proceeds": net * share, "cost": k * lot.unit_cost,
                                       "acquired": lot.t, "sold": ts, "tag": lot.tag, "lot": lot.ref, "sale": tid,
                                       "qty_estimated": estimated})
            if matched < qty:
                st["unmatched_sales"].append({"id": tid, "item_id": item, "item_name": item_name, "ts": ts,
                                              "units": qty - matched, "proceeds": round(net * (qty - matched) / qty),
                                              "qty_estimated": estimated})
    st["fifos"] = fifos
    return st


def _valuer(conn, items, at):
    hist = sim.History(conn, sim.trader_markets(conn), at - sim.VALUE_DAYS * DAY - sim.MV_HOURS * sim.HOUR, at, items=items)
    return hist


def _now(conn) -> int:
    try:
        latest = sim._latest_snapshot(conn, sim.trader_markets(conn))
    except Exception:  # noqa: BLE001
        latest = None
    return min(int(time.time()), latest) if latest else int(time.time())


def _open_lots(conn, state: dict, at: int, names: dict) -> list[dict]:
    items = [i for i, f in state["fifos"].items() if f.lots]
    hist = _valuer(conn, items, at) if items else None
    out = []
    for item in items:
        for lot in state["fifos"][item].lots:
            cv = sim.conservative_value(hist, item, at, lot.t, lot.max_hold_days)
            held = (at - lot.t) / DAY
            out.append({"item_id": item, "name": names.get(item), "lot": lot.ref, "tag": lot.tag,
                        "qty": round(lot.qty), "unit_cost": round(lot.unit_cost), "cost": round(lot.qty * lot.unit_cost),
                        "acquired": lot.t, "acquired_iso": sim.iso(lot.t), "days_held": round(held, 2),
                        "max_hold_days": lot.max_hold_days,
                        "over_max_hold": lot.max_hold_days is not None and held > lot.max_hold_days,
                        "unit_value": cv["unit_value"], "conservative_value": round(lot.qty * cv["unit_value"]),
                        "market_value": cv["market_value"], "sold_p25_14d": cv["sold_p25"]})
    out.sort(key=lambda r: -r["cost"])
    return out


def _realized_by(state: dict, key) -> dict:
    out: dict = {}
    for r in state["realized"]:
        g = out.setdefault(key(r), {"units": 0, "proceeds": 0.0, "cost": 0.0, "days": 0.0})
        g["units"] += r["units"]
        g["proceeds"] += r["proceeds"]
        g["cost"] += r["cost"]
        g["days"] += r["units"] * (r["sold"] - r["acquired"]) / DAY
    return out


def get_lots(conn, realm: str | None = None, tags: dict | None = None, max_hold_days: float = 7) -> dict:
    """Open lots at cost and at conservative value, and realized P&L per item."""
    state = build(conn, realm, tags, max_hold_days)
    at = _now(conn)
    crafts = state["crafts"][-50:][::-1]
    items = set(state["fifos"]) | {r["item"] for r in state["realized"] if r["item"]}
    items |= {c["item_id"] for c in crafts if c["item_id"]} | {p["item_id"] for c in crafts for p in c["reagents"]}
    names = sim._names(conn, items)
    for c in crafts:
        c["name"] = names.get(c["item_id"])
        for p in c["reagents"]:
            p["name"] = names.get(p["item_id"])
    lots = _open_lots(conn, state, at, names)
    realized = []
    for item, g in sorted(_realized_by(state, lambda r: r["item"]).items(), key=lambda kv: -(kv[1]["proceeds"] - kv[1]["cost"])):
        realized.append({"item_id": item, "name": names.get(item), "units": round(g["units"]),
                         "proceeds": round(g["proceeds"]), "cost": round(g["cost"]),
                         "realized": round(g["proceeds"] - g["cost"]),
                         "avg_days_held": round(g["days"] / g["units"], 2) if g["units"] else None})
    return {"as_of": at, "as_of_iso": sim.iso(at), "realm": realm,
            "open_lots": lots, "open_cost": sum(x["cost"] for x in lots),
            "open_conservative_value": sum(x["conservative_value"] for x in lots),
            "realized_by_item": realized, "fees": _fees(state),
            "unattributed_purchases": state["unattributed_purchases"],
            "unmatched_sales": state["unmatched_sales"][-50:],
            "estimated_qty_sales": state["estimated_qty_sales"],
            "crafts": crafts, "craft_totals": _craft_totals(state)}


def _craft_totals(state: dict) -> dict:
    cs = state["crafts"]
    return {"crafts": len(cs), "cost": sum(c["cost"] for c in cs),
            "cost_from_lots": sum(p["cost_from_lots"] for c in cs for p in c["reagents"]),
            "estimated_cost": sum(p["estimated_cost"] for c in cs for p in c["reagents"]),
            "not_fully_priced": sum(1 for c in cs if not c["fully_priced"]),
            "reagents_from_recipe": sum(1 for c in cs if c["reagents_source"] == "recipe"),
            "without_reagents": sum(1 for c in cs if c["reagents_source"] is None),
            "order_crafts": sum(1 for c in cs if c["order_id"])}


def _fees(state: dict) -> dict:
    return {"deposits_paid": state["deposits_paid"], "deposits_refunded": state["deposits_refunded"],
            "net_deposit_loss": state["deposits_paid"] - state["deposits_refunded"],
            "cancel_fees": state["cancel_fees"], "cuts": state["cuts"]}


def get_scorecard(conn, realm: str | None = None, tags: dict | None = None, max_hold_days: float = 7) -> dict:
    """Per strategy tag: realized P&L, ROI, sell-through, days held, open exposure at cost and at
    conservative value; weekly realized gold growth from AH cash flows; prediction error for
    journal hypotheses."""
    state = build(conn, realm, tags, max_hold_days)
    at = _now(conn)
    names = sim._names(conn, set(state["fifos"]))
    lots = _open_lots(conn, state, at, names)
    by_tag = _realized_by(state, lambda r: r["tag"])
    bought: dict[str, list] = {}
    for (tag, _), (units, spent) in state["bought"].items():
        b = bought.setdefault(tag, [0, 0])
        b[0] += units
        b[1] += spent
    strategies = []
    for tag in sorted(set(by_tag) | set(bought) | {x["tag"] for x in lots}):
        g = by_tag.get(tag, {"units": 0, "proceeds": 0.0, "cost": 0.0, "days": 0.0})
        open_ = [x for x in lots if x["tag"] == tag]
        units_bought = bought.get(tag, [0, 0])[0]
        realized = g["proceeds"] - g["cost"]
        strategies.append({
            "strategy": tag, "realized": round(realized), "cost_of_sold": round(g["cost"]),
            "roi": round(realized / g["cost"], 4) if g["cost"] else None,
            "units_bought": units_bought, "units_sold": round(g["units"]),
            "units_crafted_with": round(state["crafted_with"].get(tag, 0)),
            "sell_through": round((g["units"] + state["crafted_with"].get(tag, 0)) / units_bought, 4)
            if units_bought else None,
            "avg_days_held": round(g["days"] / g["units"], 2) if g["units"] else None,
            "open_cost": sum(x["cost"] for x in open_),
            "open_conservative_value": sum(x["conservative_value"] for x in open_),
            "open_over_max_hold": sum(1 for x in open_ if x["over_max_hold"])})
    fees = _fees(state)
    realized_total = sum(s["realized"] for s in strategies)
    weekly = [{"week": w, "auction_cash_flow": v} for w, v in sorted(state["weekly"].items())]
    return {"as_of": at, "as_of_iso": sim.iso(at), "realm": realm, "strategies": strategies,
            "realized_total": realized_total, "fees": fees,
            "realized_after_fees": realized_total - fees["net_deposit_loss"] - fees["cancel_fees"],
            "weekly": weekly,
            "weekly_note": "auction_cash_flow = net auction sales - purchases - deposits - cuts (sum of AH ledger deltas)",
            "unattributed_purchases": state["unattributed_purchases"],
            "unmatched_sales": {"count": len(state["unmatched_sales"]),
                                "proceeds": sum(u["proceeds"] for u in state["unmatched_sales"])},
            "crafts": _craft_totals(state),
            "predictions": _prediction_errors(conn, state, at)}


def _prediction_errors(conn, state: dict, at: int) -> list[dict]:
    """Journal hypotheses with prediction {"realized": copper} (or "pnl"), scored against the
    realized P&L of their strategy (and items, if listed) from created until expires_at/now."""
    out = []
    for e in journal.read(conn, kind="hypothesis", include_expired=True, include_superseded=True, limit=200)["entries"]:
        pred = e.get("prediction") or {}
        want = pred.get("realized", pred.get("pnl"))
        if not isinstance(want, (int, float)) or not e.get("strategy"):
            continue
        end = e.get("expires_at") or at
        items = set(e.get("items") or [])
        actual = sum(r["proceeds"] - r["cost"] for r in state["realized"]
                     if r["tag"] == e["strategy"] and e["created"] <= r["sold"] <= end
                     and (not items or r["item"] in items))
        out.append({"journal_id": e["id"], "strategy": e["strategy"], "predicted": want, "actual": round(actual),
                    "error": round(actual - want), "window_closed": end < at})
    return out
