"""Simulator: fills, fees, expiries and valuation on synthetic snapshot histories where the right
answer is known, plus a backtest on a market that swings between two prices."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import ledger, market, sim, store  # noqa: E402

HOUR, DAY = 3600, 86400
T0 = 1792022400  # 2026-10-15 00:00 UTC
ORE, HERB, BAR = 210930, 210796, 222417
CM = "us-commodities"


def lot(aid, item, qty, unit, tl="VERY_LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "unit_price": unit, "time_left": tl}


class SimBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def snaps(self, hours: list[list[dict]], start=T0):
        for h, auctions in enumerate(hours):
            store.store_snapshot(self.conn, CM, {"auctions": auctions}, start + h * HOUR, fetched_at=start + h * HOUR)


class FillTests(SimBase):
    def test_buy_fills_only_from_the_next_snapshot_at_or_below_max_unit(self):
        # h0: a cheap 5 @ 90 that's gone by h1; h1 on: only 100 @ 120.
        self.snaps([[lot(1, ORE, 5, 90), lot(2, ORE, 100, 120)]] + [[lot(2, ORE, 100, 120)]] * 4)
        plan = {"plan": "p1", "created": T0, "tasks": [{"id": "t1", "kind": "buy", "item": ORE, "qty": 5,
                                                         "max_unit": 100}]}
        r = sim.evaluate_plan(self.conn, plan, inventory={})
        [t1] = r["tasks"]
        self.assertEqual((t1["status"], t1["qty"], t1["paid"]), ("skipped", 0, 0))
        self.assertEqual(t1["executed"], T0 + HOUR)
        self.assertEqual((r["units_acquired"], r["cash_flow"], r["capital_used"]), (0, 0, 0))

        # Raising max_unit to 120 buys from the 100 listed then, cheapest first, at their price.
        plan["tasks"][0]["max_unit"] = 120
        [t1] = sim.evaluate_plan(self.conn, plan, inventory={})["tasks"]
        self.assertEqual((t1["status"], t1["qty"], t1["paid"]), ("done", 5, 600))

    def test_buy_partial_and_dependent_tasks(self):
        self.snaps([[lot(2, ORE, 100, 120)], [lot(1, ORE, 3, 95), lot(2, ORE, 100, 120)], [lot(2, ORE, 100, 120)]])
        plan = {"created": T0, "tasks": [
            {"id": "t1", "kind": "buy", "item": ORE, "qty": 10, "max_unit": 100},
            {"id": "t2", "kind": "post", "item": ORE, "qty": 3, "floor": 200, "after": "t1"},
            {"id": "t3", "kind": "buy", "item": HERB, "qty": 1, "max_unit": 1},
            {"id": "t4", "kind": "post", "item": HERB, "qty": 1, "after": "t3"},
            {"id": "t5", "kind": "scan", "item": ORE}]}
        r = sim.evaluate_plan(self.conn, plan, inventory={})
        t1, t2, t3, t4, t5 = r["tasks"]
        self.assertEqual((t1["status"], t1["qty"], t1["paid"]), ("partial", 3, 285))
        self.assertEqual(t2["status"], "below_floor")       # lowest left is 120, floor 200
        self.assertEqual(t3["status"], "no_data")
        self.assertEqual(t4["status"], "blocked")
        self.assertEqual(t5["status"], "not_simulated")
        self.assertEqual(r["unsold_units"], 3)
        self.assertEqual(r["unsold_cost"], 285)

    def test_undercut_post_never_sells(self):
        # We undercut the 100 listing to 99; a competitor then lists at 80 and buyers only take theirs.
        hours = [[lot(2, ORE, 50, 100)], [lot(2, ORE, 50, 100)]]
        hours += [[lot(2, ORE, 50, 100), lot(3, ORE, 1000 - 50 * k, 80)] for k in range(30)]
        self.snaps(hours)
        plan = {"created": T0, "tasks": [{"id": "t1", "kind": "post", "item": ORE, "qty": 10, "floor": 50,
                                           "ceil": 500, "undercut": 1, "duration": 24}]}
        r = sim.evaluate_plan(self.conn, plan, inventory={ORE: 10}, cost_basis={ORE: 50},
                              params={"reposts": 0, "vendor_prices": {ORE: 20}}, horizon_hours=30)
        [t1] = r["tasks"]
        self.assertEqual((t1["status"], t1["price"], t1["seen"]), ("done", 99, 100))
        self.assertEqual((t1["sold"], t1["expired"]), (0, 10))
        deposit = 20 * 10 * 0.15 * 2  # vendor 20c x 10 x 15% per 12h x 24h
        self.assertEqual(t1["deposit"], deposit)
        self.assertEqual(r["deposits_lost"], deposit)
        self.assertEqual(r["realized_pnl"], -deposit)
        self.assertEqual(r["units_sold"], 0)
        self.assertEqual(r["cash_flow"], -deposit)

    def test_fees_are_exact_and_unsold_deposits_are_lost(self):
        # Posted at 100 (lowest 101) at h1. A purchase in that same snapshot doesn't count; later
        # 2 + 4 units are bought from the 101 listing (>= our price), so 6 of our 10 sell.
        q = [200, 190, 188, 184, 184]
        hours = [[lot(5, ORE, q[0], 101)]] + [[lot(5, ORE, x, 101)] for x in q[1:]]
        hours += [[lot(5, ORE, 184, 101)]] * 30
        self.snaps(hours)
        plan = {"created": T0, "tasks": [{"id": "t1", "kind": "post", "item": ORE, "qty": 10, "floor": 60,
                                           "ceil": 1000, "duration": 24}]}
        r = sim.evaluate_plan(self.conn, plan, inventory={ORE: 10}, cost_basis={ORE: 50},
                              params={"reposts": 0, "vendor_prices": {ORE: 20}}, horizon_hours=30)
        [t1] = r["tasks"]
        self.assertEqual((t1["price"], t1["sold"], t1["expired"]), (100, 6, 4))
        deposit = 60.0                                   # 20 x 10 x 0.15 x 2, 6/10 refunded on sale
        self.assertEqual(r["deposits_lost"], 24)
        self.assertEqual(r["realized_pnl_before_deposits"], 6 * 100 * 0.95 - 6 * 50)   # 270
        self.assertEqual(r["realized_pnl"], 270 - 24)
        self.assertEqual(r["cash_flow"], round(-deposit + 600 - 30 + 36))              # 546
        self.assertEqual((r["units_sold"], r["unsold_units"], r["unsold_cost"]), (6, 4, 200))
        self.assertEqual(r["avg_days_held"], round((2 * 2 + 4 * 3) / 6 * HOUR / DAY, 2))  # 2 sold at h2, 4 at h3

    def test_expired_post_is_reposted(self):
        # Nothing sells for 24h; after the repost, buyers take 10 units at 101.
        hours = [[lot(5, ORE, 500, 101)]] * 27 + [[lot(5, ORE, 490, 101)]] * 5
        self.snaps(hours)
        plan = {"created": T0, "tasks": [{"id": "t1", "kind": "post", "item": ORE, "qty": 4, "floor": 60,
                                           "duration": 24}]}
        r = sim.evaluate_plan(self.conn, plan, inventory={ORE: 4}, cost_basis={ORE: 50},
                              params={"reposts": 1}, horizon_hours=31)
        [t1] = r["tasks"]
        self.assertEqual((t1["sold"], t1["expired"]), (0, 4))
        [again] = t1["reposts"]
        self.assertEqual((again["status"], again["qty"], again["sold"]), ("done", 4, 4))
        self.assertEqual(again["executed"], T0 + 26 * HOUR)  # first snapshot after h1 + 24h
        self.assertEqual(r["units_sold"], 4)

    def test_craft_uses_bought_reagents(self):
        self.conn.execute("INSERT INTO recipes VALUES ('Snow-Proudmoore', 7, 'Blacksmithing', 'Bar', ?, 1)", (BAR,))
        self.conn.execute("INSERT INTO recipe_reagents VALUES ('Snow-Proudmoore', 7, ?, 5)", (ORE,))
        self.snaps([[lot(1, ORE, 100, 10), lot(9, BAR, 50, 100)]] * 3)
        plan = {"created": T0, "tasks": [
            {"id": "t1", "kind": "buy", "item": ORE, "qty": 12, "max_unit": 10},
            {"id": "t2", "kind": "craft", "recipe": 7, "qty": 3, "after": "t1"},
            {"id": "t3", "kind": "post", "item": BAR, "qty": 3, "floor": 1, "after": "t2"}]}
        r = sim.evaluate_plan(self.conn, plan, inventory={})
        t1, t2, t3 = r["tasks"]
        self.assertEqual((t2["status"], t2["qty"], t2["output"], t2["cost"]), ("partial", 2, 2, 100))
        self.assertEqual((t3["qty"], t3["price"]), (2, 99))
        bar = next(i for i in r["items"] if i["item_id"] == BAR)
        self.assertEqual((bar["crafted"], bar["held_cost"]), (2, 100))


class ValueTests(SimBase):
    def test_conservative_value(self):
        # 15 days: p10 = min = 100 throughout; 10 units bought per hour at 100 for the first 12h,
        # then 10 per hour at 80 from another listing (cheaper) -> sold p25 is 80.
        hours = []
        for h in range(15 * 24):
            a = [lot(1, ORE, 100000 - 10 * min(h, 12), 100)]
            if h >= 12:
                a.append(lot(2, ORE, 100000 - 10 * (h - 12), 80))
            hours.append(a)
        self.snaps(hours)
        at = T0 + (15 * 24 - 1) * HOUR
        hist = sim.History(self.conn, [CM], at - 20 * DAY, at, items=[ORE, HERB], vendor_prices={ORE: 10})
        cv = sim.conservative_value(hist, ORE, at)
        self.assertEqual(cv["market_value"], 80)
        self.assertEqual(cv["sold_p25"], 80)
        dep = 10 * 0.15 * 2
        self.assertEqual(cv["unit_value"], int(80 * 0.95 - dep))
        # Held past its max hold: halved.
        self.assertEqual(sim.conservative_value(hist, ORE, at, at - 8 * DAY, 7)["unit_value"], int(80 * 0.95 - dep) // 2)
        self.assertEqual(sim.conservative_value(hist, ORE, at, at - 6 * DAY, 7)["unit_value"], int(80 * 0.95 - dep))
        # The 12 hourly sales at 100 are more than 14 days back from 30 days later: only the 80s count;
        # an item that never sold is worth 0.
        self.assertEqual(sim.conservative_value(hist, HERB, at)["unit_value"], 0)

    def test_no_sales_in_14_days_is_zero(self):
        hours = [[lot(1, ORE, 100 - min(h, 3), 100)] for h in range(24)] + [[lot(1, ORE, 97, 100)]] * (15 * 24)
        self.snaps(hours)
        at = T0 + (len(hours) - 1) * HOUR
        hist = sim.History(self.conn, [CM], at - 20 * DAY, at, items=[ORE])
        cv = sim.conservative_value(hist, ORE, at)
        self.assertEqual((cv["sold_units_14d"], cv["unit_value"]), (0, 0))
        self.assertEqual(cv["market_value"], 100)


class BacktestTests(SimBase):
    def build_swing(self, days=6):
        """ORE: 12h at 1.0M (a big cheap listing) then 12h at 1.5M, every day. Buyers take 200/h
        from whatever is cheapest... recorded as partial purchases of the 1.5M listing too."""
        hours, cheap = [], 0
        for h in range(days * 24):
            dear = lot(1, ORE, 10_000_000 - 200 * h, 1_500_000)
            a = [dear]
            if h % 24 < 12:
                if h % 24 == 0:
                    cheap = 100 + h
                a.append(lot(cheap, ORE, 50_000 - 300 * (h % 24), 1_000_000))
            hours.append(a)
        self.snaps(hours)

    def test_flip_buys_lows_and_sells_highs(self):
        self.build_swing()
        r = sim.backtest(self.conn, "flip", {"capital": 10_000_000_000, "min_trips": 1})
        self.assertGreater(r["decisions_count"], 6)
        self.assertGreater(r["units_acquired"], 0)
        self.assertGreater(r["units_sold"], 0)
        self.assertGreater(r["realized_pnl"], 0)
        buys = [t for t in r["sample_plan"]["plan"]["tasks"] if t["kind"] == "buy"]
        self.assertTrue(all(t["max_unit"] == 1_000_000 and t["target"] == 1_500_000 for t in buys))
        self.assertTrue(r["backtest_id"].startswith("bt-"))
        self.assertEqual(r["backtest_id"], sim.backtest(self.conn, "flip", {"capital": 10_000_000_000,
                                                                             "min_trips": 1})["backtest_id"])
        # Exposure rules: at most 10% of capital per item at cost.
        self.assertLessEqual(r["capital_used"], 10_000_000_000 * 0.10 + 1)

    def test_unknown_params_are_rejected(self):
        self.build_swing(1)
        with self.assertRaises(ValueError):
            sim.backtest(self.conn, "flip", {"bogus": 1})
        with self.assertRaises(ValueError):
            sim.backtest(self.conn, "nope")


class TraderMarketsTests(SimBase):
    build_swing = BacktestTests.build_swing

    def add_market(self, name, kind, realms="[]", game="retail", source=None):
        mid = store.market_id(self.conn, name)
        self.conn.execute("UPDATE markets SET kind = ?, region = 'us', game = ?, realms = ?, source = ? WHERE mid = ?",
                          (kind, game, realms, source, mid))

    def test_markets_are_found_by_kind_and_realm(self):
        self.assertEqual(sim.trader_markets(self.conn, ["Proudmoore", "Aerie Peak"]),
                         ["us-commodities", "us-proudmoore", "us-aerie-peak"])  # nothing known: default names
        self.add_market("us-retail-commodities", "commodities")
        self.add_market("us-classic-commodities", "commodities", game="classic")
        self.add_market("us-moon-guard", "realm", '["Moon Guard"]')
        self.add_market("us-aegwynn", "realm", '["Aegwynn", "Bonechewer"]', source="us-retail-all-realms")
        self.add_market("us-bonechewer-own", "realm", '["Bonechewer"]')
        self.assertEqual(sim.trader_markets(self.conn, ["moon guard", "Bonechewer", "Aegwynn"]),
                         ["us-retail-commodities", "us-moon-guard", "us-bonechewer-own", "us-aegwynn"])

    def test_backtest_finds_a_renamed_commodity_market(self):
        self.build_swing()
        self.conn.execute("UPDATE snapshots SET market = 'us-retail-commodities' WHERE market = ?", (CM,))
        self.conn.execute("UPDATE markets SET name = 'us-retail-commodities' WHERE name = ?", (CM,))
        self.add_market("us-retail-commodities", "commodities")
        r = sim.backtest(self.conn, "flip", {"capital": 10_000_000_000, "min_trips": 1})
        self.assertGreater(r["units_sold"], 0)

    def test_evaluate_a_published_plan_by_id(self):
        from goldmine_server import tasks
        self.build_swing(2)
        plan = {"plan": "paper1", "created": T0 + 2 * HOUR, "expires": T0 + 10 * DAY, "paper": True, "budget_min": 5,
                "tasks": [{"id": "t1", "kind": "buy", "item": ORE, "qty": 10, "max_unit": 1_000_000, "est_min": 1}]}
        tasks.publish(self.conn, plan, T0 + 2 * HOUR)
        self.assertEqual(sim.evaluate_plan(self.conn, "paper1"), sim.evaluate_plan(self.conn, plan))
        with self.assertRaises(ValueError):
            sim.evaluate_plan(self.conn, "nope")


class PaperScorecardTests(SimBase):
    build_swing = BacktestTests.build_swing

    def test_paper_plans_are_scored_and_totalled_by_strategy(self):
        from goldmine_server import tasks
        self.build_swing(3)

        def plan(pid, at, **over):
            return {"plan": pid, "created": at, "expires": T0 + 10 * DAY, "paper": True, "budget_min": 5,
                    "tasks": [{"id": "t1", "kind": "buy", "item": ORE, "qty": 10, "max_unit": 1_000_000,
                               "est_min": 1, "ev": 50_000}], **over}
        for pid, at, over in (("a", T0 + 2 * HOUR, {"strategy": "flip"}), ("b", T0 + 4 * HOUR, {"strategy": "flip"}),
                              ("c", T0 + 6 * HOUR, {})):
            tasks.publish(self.conn, plan(pid, at, **over), at)
        r = sim.paper_scorecard(self.conn, since=T0, until=T0 + 3 * DAY, horizon_hours=24)
        self.assertEqual([p["plan"] for p in r["plans"]], ["c", "b", "a"])  # newest first
        one = sim.evaluate_plan(self.conn, "a", horizon_hours=24)
        a = r["plans"][2]
        self.assertEqual((a["strategy"], a["marked_pnl"], a["units_acquired"], a["predicted_ev"]),
                         ("flip", one["marked_pnl"], one["units_acquired"], 50_000))
        by = {t["strategy"]: t for t in r["strategies"]}
        self.assertEqual((by["flip"]["plans"], by["untagged"]["plans"]), (2, 1))
        flip = [p for p in r["plans"] if p["strategy"] == "flip"]
        self.assertEqual(by["flip"]["marked_pnl"], sum(p["marked_pnl"] for p in flip))
        self.assertEqual(by["flip"]["prediction_error"], by["flip"]["marked_pnl"] - 100_000)
        only = sim.paper_scorecard(self.conn, since=T0, until=T0 + 3 * DAY, strategy="untagged", limit=5)
        self.assertEqual([p["plan"] for p in only["plans"]], ["c"])
        self.assertEqual(sim.paper_scorecard(self.conn, since=T0 + 5 * HOUR, until=T0 + 3 * DAY, limit=1)["plans"][0]["plan"], "c")


if __name__ == "__main__":
    unittest.main()
