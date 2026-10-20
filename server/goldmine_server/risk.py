"""Portfolio risk rules for the trader (docs/trader.md, "Risk rules"), checked when a plan is published.

Paper plans risk no gold and always pass. A real plan's buys are costed at their worst case
(qty x max_unit) and must leave:

  raw gold                                  >= RAW_FLOOR_TOKENS x WoW token price
  gold + conservative value of everything   >= NET_FLOOR_TOKENS x WoW token price
  per item, trader lots at cost + the buy   <= ITEM_SHARE x working capital, and the units
                                               held + bought <= ITEM_DAYS x median daily sales
  all trader lots at cost + the plan's buys <= TOTAL_SHARE x working capital
  trader losses since the first real plan   <  EXPERIMENT_BUDGET (then no more buys)

and every buy needs an exit: `sell` (target unit price) and `hold_days`. Working capital is
raw gold minus the raw floor. "Trader lots" are FIFO lots (lots.py) bought on or after the first
real plan, so inventory the user already had doesn't count against the trader's caps (it still
counts toward net worth). Gold and inventory are the Proudmoore characters' plus the warband
bank's; values come from sim.conservative_value. Everything is copper.
"""

from __future__ import annotations

import bisect
import statistics

from . import account, lots, sim

REALM = "Proudmoore"
REGION = "us"
RAW_FLOOR_TOKENS = 1
NET_FLOOR_TOKENS = 2
EXPERIMENT_BUDGET = 100_000 * 10_000   # 100k gold
ITEM_SHARE = 0.10
TOTAL_SHARE = 0.40
ITEM_DAYS = 3
SALES_DAYS = 14
DAY = 86400


def _first_real_plan(conn) -> int | None:
    return conn.execute("SELECT MIN(published) FROM plans WHERE paper = 0").fetchone()[0]


def _held_items(conn, realm: str) -> dict[int, int]:
    """Units held in bags, bank, warband bank and active auctions."""
    suffix = "-" + realm.replace(" ", "")
    held: dict[int, int] = {}
    for item, n in conn.execute(
            "SELECT item_id, SUM(count) FROM inventory WHERE owner = 'warband' OR replace(owner, ' ', '') LIKE ? "
            "GROUP BY item_id", ("%" + suffix,)):
        held[item] = held.get(item, 0) + n
    for item, n in conn.execute(
            "SELECT item_id, SUM(quantity) FROM owned_auctions a WHERE status = 0 AND replace(char_key, ' ', '') LIKE ? "
            "AND last_seen = (SELECT ts FROM owned_auction_lists l WHERE l.char_key = a.char_key) GROUP BY item_id",
            ("%" + suffix,)):
        held[item] = held.get(item, 0) + n
    return held


def _median_daily_sales(hist, item: int, at: int) -> float:
    market = hist.market_of(item)
    if market is None:
        return 0.0
    times, events = hist.events(market, item)
    days = [0] * SALES_DAYS
    for t, _price, units in events[bisect.bisect_right(times, at - SALES_DAYS * DAY):bisect.bisect_right(times, at)]:
        days[min(SALES_DAYS - 1, int((at - t) // DAY))] += units
    return float(statistics.median(days))


class _State:
    """Everything the checks need, computed once."""

    def __init__(self, conn, realm: str, extra_items=()):
        self.at = lots._now(conn)
        acct = account.account(conn, realm)
        self.gold = acct["total_gold"]
        self.token = (acct["wow_token"].get(REGION) or {}).get("price")
        self.held = _held_items(conn, realm)
        self.start = _first_real_plan(conn)
        items = set(self.held) | {int(i) for i in extra_items}
        state = lots.build(conn, realm)
        self.hist = lots._valuer(conn, sorted(items | set(state["fifos"])), self.at) if items or state["fifos"] else None
        self.values = {i: self.unit_value(i) for i in items}
        self.inventory_value = sum(self.values[i] * n for i, n in self.held.items())
        names = sim._names(conn, set(state["fifos"]))
        open_lots = lots._open_lots(conn, state, self.at, names) if state["fifos"] else []
        mine = [x for x in open_lots if self.start is not None and x["acquired"] >= self.start]
        self.lot_cost: dict[int, int] = {}
        for x in mine:
            self.lot_cost[x["item_id"]] = self.lot_cost.get(x["item_id"], 0) + x["cost"]
        realized = sum(r["proceeds"] - r["cost"] for r in state["realized"]
                       if self.start is not None and r["acquired"] >= self.start)
        markdown = sum(x["conservative_value"] - x["cost"] for x in mine)
        self.trader_loss = max(0, round(-(realized + markdown)))
        self.trader_realized = round(realized)

    def unit_value(self, item: int) -> int:
        if self.hist is None:
            return 0
        return sim.conservative_value(self.hist, item, self.at)["unit_value"]

    @property
    def raw_floor(self) -> int | None:
        return None if self.token is None else RAW_FLOOR_TOKENS * self.token

    @property
    def net_floor(self) -> int | None:
        return None if self.token is None else NET_FLOOR_TOKENS * self.token

    @property
    def working_capital(self) -> int:
        return 0 if self.raw_floor is None else max(0, self.gold - self.raw_floor)


def status(conn, realm: str = REALM) -> dict:
    """Where the account stands against the risk rules (the trader's headline numbers)."""
    s = _State(conn, realm)
    tok = s.token
    in_tokens = (lambda c: None if not tok else round(c / tok, 3))  # noqa: E731
    net = s.gold + s.inventory_value
    trader_cost = sum(s.lot_cost.values())
    return {
        "as_of": s.at, "as_of_iso": sim.iso(s.at), "realm": realm,
        "token_price": tok,
        "gold": s.gold, "gold_tokens": in_tokens(s.gold),
        "inventory_conservative_value": s.inventory_value,
        "net_worth": net, "net_worth_tokens": in_tokens(net),
        "raw_floor": s.raw_floor, "net_floor": s.net_floor,
        "raw_headroom": None if tok is None else s.gold - s.raw_floor,
        "net_headroom": None if tok is None else net - s.net_floor,
        "working_capital": s.working_capital,
        "per_item_cap": round(ITEM_SHARE * s.working_capital),
        "trader_inventory_cost": trader_cost,
        "trader_inventory_cap": round(TOTAL_SHARE * s.working_capital),
        "trader_since": s.start, "trader_since_iso": sim.iso(s.start) if s.start else None,
        "trader_realized": s.trader_realized,
        "experiment_loss": s.trader_loss, "experiment_budget": EXPERIMENT_BUDGET,
        "experiment_budget_left": max(0, EXPERIMENT_BUDGET - s.trader_loss),
        "buys_allowed": tok is not None and s.trader_loss < EXPERIMENT_BUDGET and s.gold > (s.raw_floor or 0),
        "rules": {"raw_floor_tokens": RAW_FLOOR_TOKENS, "net_floor_tokens": NET_FLOOR_TOKENS,
                  "item_share": ITEM_SHARE, "total_share": TOTAL_SHARE, "item_days_of_sales": ITEM_DAYS},
    }


def validate(conn, plan: dict, realm: str = REALM) -> list[str]:
    """Risk errors for a (shape-valid) plan; empty when it may be published."""
    if plan.get("paper"):
        return []
    buys = [t for t in plan["tasks"] if t["kind"] == "buy"]
    if not buys:
        return []
    errors = []
    for t in buys:
        missing = [k for k in ("sell", "hold_days") if k not in t]
        if missing:
            errors.append(f"task {t['id']!r}: a real buy needs an exit ({', '.join(missing)})")
    s = _State(conn, realm, extra_items=[t["item"] for t in buys])
    if s.token is None:
        return errors + ["no WoW token price collected yet, so the floors can't be checked; publish as paper"]
    if s.trader_loss >= EXPERIMENT_BUDGET:
        return errors + [f"experiment budget spent (trader losses {s.trader_loss // 10000}g); no buys until a review"]

    cost = {t["id"]: t["qty"] * t["max_unit"] for t in buys}
    total = sum(cost.values())
    gold_after = s.gold - total
    if gold_after < s.raw_floor:
        errors.append(f"buys up to {total // 10000}g would leave {gold_after // 10000}g, under the raw floor "
                      f"of {s.raw_floor // 10000}g (1 token)")
    bought_value = sum(s.values.get(t["item"], 0) * t["qty"] for t in buys)
    net_after = gold_after + s.inventory_value + bought_value
    if net_after < s.net_floor:
        errors.append(f"net worth after buys would be {net_after // 10000}g at conservative value, under the floor "
                      f"of {s.net_floor // 10000}g (2 tokens)")
    wc = s.working_capital
    if sum(s.lot_cost.values()) + total > TOTAL_SHARE * wc:
        errors.append(f"trader inventory at cost would be {(sum(s.lot_cost.values()) + total) // 10000}g, over "
                      f"{TOTAL_SHARE:.0%} of working capital ({round(TOTAL_SHARE * wc) // 10000}g)")
    by_item: dict[int, list] = {}
    for t in buys:
        by_item.setdefault(t["item"], []).append(t)
    for item, ts in by_item.items():
        ids = ", ".join(repr(t["id"]) for t in ts)
        spend = s.lot_cost.get(item, 0) + sum(cost[t["id"]] for t in ts)
        if spend > ITEM_SHARE * wc:
            errors.append(f"task {ids}: item {item} exposure {spend // 10000}g over the per-item cap "
                          f"{round(ITEM_SHARE * wc) // 10000}g")
        daily = _median_daily_sales(s.hist, item, s.at) if s.hist else 0.0
        units = s.held.get(item, 0) + sum(t["qty"] for t in ts)
        if units > ITEM_DAYS * daily:
            errors.append(f"task {ids}: {units} units of item {item} held+bought is over {ITEM_DAYS} days of its "
                          f"median sales ({daily:g}/day)")
    return errors
