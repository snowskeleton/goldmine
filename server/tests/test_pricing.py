import argparse
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from goldmine_server import ledger, market, pricing, reports, store, svparse  # noqa: E402


HOUR = 3600
NOW = int(time.time()) // HOUR * HOUR - 6 * HOUR  # recent, so default 72h windows include it
LINEN, BOLT = 2589, 2996


def commodity(aid, item, qty, unit, tl="LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "unit_price": unit, "time_left": tl}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "gm.sqlite")
        self.conn = ledger.connect(self.db)
        market.ensure_schema(self.conn)
        # 5 hourly snapshots: linen ~15c, bolts ~100c with one dumped bolt at 40c in the last one.
        for h in range(5):
            auctions = [commodity(10 + h, LINEN, 100, 15), commodity(1, LINEN, 500, 18)]
            auctions += [commodity(20 + h, BOLT, 10, 100), commodity(2, BOLT, 50, 110)]
            if h == 4:
                auctions.append(commodity(99, BOLT, 3, 40))
            store.store_snapshot(self.conn, "us-commodities", {"auctions": auctions}, NOW - (4 - h) * HOUR)
        # Recipe: 2 linen -> 1 bolt, known by Snow.
        self.conn.execute("INSERT INTO recipes (char_key, recipe_id, profession, name, output_item, output_qty) VALUES ('Snow-Testrealm', 100, 'Tailoring', 'Bolt of Linen Cloth', ?, 1)", (BOLT,))
        self.conn.execute("INSERT INTO recipe_reagents VALUES ('Snow-Testrealm', 100, ?, 2)", (LINEN,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_price_table(self):
        prices = pricing.price_table(self.conn, now=NOW)
        bolt = prices[(BOLT, 0)]
        self.assertEqual(bolt.current_min, 40)
        self.assertEqual(bolt.market_value, 100)
        self.assertEqual(bolt.sell, 40)            # can't sell above the current lowest listing
        # Each hour the previous 10-bolt auction vanished: 4 intervals x 10 over 4 hours.
        self.assertAlmostEqual(bolt.sold_per_day, 40 / (4 / 24))
        self.assertEqual(bolt.bought_per_day, 0)  # whole auctions vanished: none definitely bought
        self.assertEqual(bolt.depth[0], 3)        # units within 5% of the 40c minimum

    def test_market_stats_cache_follows_new_snapshots(self):
        before = pricing.price_table(self.conn, now=NOW)[(BOLT, 0)]
        self.assertEqual(pricing.price_table(self.conn, now=NOW)[(BOLT, 0)].market_value, before.market_value)
        # A new snapshot with pricier bolts has to show up, not the cached stats.
        store.store_snapshot(self.conn, "us-commodities", {"auctions": [commodity(30, BOLT, 10, 500)]}, NOW + HOUR)
        self.conn.commit()
        after = pricing.price_table(self.conn, now=NOW + HOUR)[(BOLT, 0)]
        self.assertNotEqual(after.sold_per_day, before.sold_per_day)

    def test_fresher_scan_overrides_current(self):
        self.conn.execute("INSERT INTO scans VALUES ('Snow-Testrealm:s1', 'Snow-Testrealm', ?, 'commodity')", (NOW + 600,))
        self.conn.executemany("INSERT INTO scan_rows VALUES ('Snow-Testrealm:s1', ?, 0, ?, ?)",
                              [(BOLT, 95, 5), (BOLT, 99, 50)])
        bolt = pricing.price_table(self.conn, now=NOW + 700)[(BOLT, 0)]
        self.assertEqual((bolt.current_min, bolt.market_value), (95, 100))
        self.assertIn("scan", bolt.sources)

    def test_craft_report(self):
        prices = pricing.price_table(self.conn, now=NOW)
        [craft] = pricing.craft_report(self.conn, prices)
        self.assertEqual(craft.cost, 2 * 15)
        self.assertEqual(craft.revenue, int(40 * 0.95))
        self.assertEqual(craft.profit, 38 - 30)
        self.assertEqual(craft.crafters, ["Snow-Testrealm"])

    def test_market_value_ignores_a_few_hours_of_troll_listings(self):
        self.assertEqual(pricing.weighted_median([(100, 3.0), (5_000_000, 1.0)]), 100)
        self.assertEqual(pricing.weighted_median([]), None)

    def test_cost_for_a_thin_cheap_tier_under_a_wall(self):
        # 145 units near 1.74g, then most of 64k units at 25.9g: a few crafts cost the cheap price.
        p = pricing.Price(2447, current_min=17_400, current_p10=259_000, current_median=259_000,
                          quantity=64_064, depth=(145, 145, 145))
        self.assertEqual(p.cost_for(10), 17_400)
        self.assertEqual(p.cost_for(1_000), 259_000)
        self.assertEqual(pricing.Price(1, current_min=10, book=[(10, 5), (20, 100)]).cost_for(6), 20)
        self.assertEqual(pricing.Price(1, current_p10=30, market_value=40).cost_for(5), 30)  # nothing listed

    def test_craft_with_unlisted_output_has_no_revenue(self):
        P = pricing.Price
        prices = {(LINEN, 0): P(LINEN, current_min=15, depth=(100, 100, 100)),
                  (BOLT, 0): P(BOLT, market_value=1_000_000)}  # an old troll listing, none up now
        [craft] = pricing.craft_report(self.conn, prices)
        self.assertEqual((craft.cost, craft.revenue, craft.profit, craft.listed), (30, None, None, False))

    def test_recipe_ranks_collapse_into_the_best(self):
        self.conn.execute("INSERT INTO recipes (char_key, recipe_id, profession, name, output_item, output_qty, "
                          "cooldown, ready_at) VALUES ('Snow-Testrealm', 101, 'Tailoring', 'Bolt of Linen Cloth', ?, 2,"
                          " 72000, ?)", (BOLT, NOW + 10 ** 9))
        self.conn.execute("INSERT INTO recipe_reagents VALUES ('Snow-Testrealm', 101, ?, 2)", (LINEN,))
        prices = pricing.price_table(self.conn, now=NOW)
        [craft] = pricing.craft_report(self.conn, prices)
        self.assertEqual((craft.recipe_id, craft.ranks, craft.output_qty), (101, [100], 2))
        self.assertEqual((craft.cooldown, craft.ready_at), (72000, NOW + 10 ** 9))

    def test_deal_report(self):
        prices = pricing.price_table(self.conn, now=NOW)
        deals = pricing.deal_report(prices)
        self.assertEqual([d.item_id for d in deals], [BOLT])
        self.assertEqual(deals[0].profit_per_unit, 95 - 40)

    def test_deal_report_judges_breeds_not_the_blend(self):
        P = pricing.Price
        prices = {(82800, 1234): P(82800, 1234, market_value=50_000, current_min=5_000, sold_per_day=3),
                  (82800, -1): P(82800, -1, market_value=6_000, current_min=5_000, sold_per_day=2),    # cheap breed
                  (82800, -2): P(82800, -2, market_value=90_000, current_min=40_000, sold_per_day=1)}  # a real deal
        self.assertEqual([(d.item_id, d.variant) for d in pricing.deal_report(prices)], [(82800, -2)])

    def test_addon_data_roundtrip(self):
        st = pricing.MarketSet("commodities", "retail", "us", "us-commodities", ["us-commodities"])
        prices = pricing.price_table(self.conn, now=NOW)
        out = os.path.join(self.tmp.name, "Data.lua")
        ex = pricing.SetExport(st, prices, pricing.craft_report(self.conn, prices), pricing.deal_report(prices))
        n = pricing.write_addon_data(out, [ex], now=NOW)
        self.assertEqual(n, 2)
        data = svparse.parse_file(out)["Goldmine_Data"]
        [s0] = data["sets"]
        self.assertEqual((s0["kind"], s0["game"]), ("commodities", "retail"))
        self.assertEqual(s0["prices"][BOLT][:2], [100, 40])
        self.assertEqual(s0["crafts"][0][1], "Bolt of Linen Cloth")
        self.assertEqual(s0["deals"][0][0], BOLT)


class MultiGameTests(unittest.TestCase):
    """Retail and Mists Classic share item ids; their prices must never mix."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "gm.sqlite")
        self.conn = ledger.connect(self.db)
        market.ensure_schema(self.conn)
        c = self.conn
        c.executemany("INSERT INTO markets (name, region, namespace, kind, game, realms) VALUES (?, 'us', ?, ?, ?, ?)", [
            ("us-commodities", "dynamic-us", "commodities", "retail", "[]"),
            ("us-proudmoore", "dynamic-us", "realm", "retail", '["Proudmoore"]'),
            ("us-stormrage", "dynamic-us", "realm", "retail", '["Stormrage"]'),
            ("us-mists-pagle", "dynamic-classic-us", "realm", "classic", '["Pagle", "Nazgrim"]'),
        ])
        c.executemany("INSERT INTO characters (key, name, realm, region) VALUES (?, ?, ?, 'US')", [
            ("Phin-Proudmoore", "Phin", "Proudmoore"),
            ("Old-Nazgrim", "Old", "Nazgrim"),
        ])
        for h in range(2):
            t = NOW - (1 - h) * HOUR
            store.store_snapshot(c, "us-commodities", {"auctions": [commodity(1, LINEN, 100, 900)]}, t)
            store.store_snapshot(c, "us-proudmoore", {"auctions": [commodity(2, 5000, 1, 70000)]}, t)
            store.store_snapshot(c, "us-stormrage", {"auctions": [commodity(3, 5000, 1, 99999)]}, t)
            store.store_snapshot(c, "us-mists-pagle", {"auctions": [commodity(4, LINEN, 100, 15)]}, t)
        # A Mists character's in-game scan of linen must not affect retail prices.
        c.execute("INSERT INTO scans VALUES ('Old-Nazgrim:s1', 'Old-Nazgrim', ?, 'commodity')", (NOW + 60,))
        c.execute("INSERT INTO scan_rows VALUES ('Old-Nazgrim:s1', ?, 0, 12, 50)", (LINEN,))
        c.execute("INSERT INTO recipes (char_key, recipe_id, profession, name, output_item, output_qty) VALUES ('Old-Nazgrim', 100, 'Tailoring', 'Bolt', ?, 1)", (BOLT,))
        c.execute("INSERT INTO recipe_reagents VALUES ('Old-Nazgrim', 100, ?, 2)", (LINEN,))
        c.commit()
        self.args = argparse.Namespace(config=os.path.join(self.tmp.name, "none.toml"), hours=72,
                                       addon_data=os.path.join(self.tmp.name, "Data.lua"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_sets_and_characters(self):
        sets = {s.name: s for s in pricing.build_sets(self.conn, {})}
        self.assertEqual(sets["us-proudmoore"].chars, ["Phin-Proudmoore"])
        self.assertEqual(sets["us-mists-pagle"].chars, ["Old-Nazgrim"])  # connected realm member
        self.assertEqual(sets["us-stormrage"].chars, [])
        self.assertEqual(sets["us-commodities"].chars, ["Phin-Proudmoore"])  # retail only

    def test_push_keeps_games_separate(self):
        with redirect_stdout(io.StringIO()):
            ledger.push(self.conn, self.args, cfg={})
        data = svparse.parse_file(self.args.addon_data)["Goldmine_Data"]
        by_name = {s["name"]: s for s in data["sets"]}
        self.assertNotIn("us-stormrage", by_name)  # no characters there
        self.assertEqual(by_name["us-commodities"]["prices"][LINEN][1], 900)
        self.assertEqual(by_name["us-mists-pagle"]["prices"][LINEN][1], 12)  # Mists scan, Mists set
        self.assertEqual(by_name["us-mists-pagle"]["realms"], ["Pagle", "Nazgrim"])
        self.assertEqual(by_name["us-proudmoore"]["prices"][5000][1], 70000)
        self.assertFalse(by_name["us-commodities"]["crafts"])
        self.assertEqual(by_name["us-mists-pagle"]["game"], "classic")

    def test_pricing_flag_excludes_market(self):
        cfg = {"market": [{"name": n} for n in ("us-commodities", "us-proudmoore")]
               + [{"name": "us-mists-pagle", "pricing": False}]}
        names = {s.name for s in pricing.build_sets(self.conn, cfg)}
        self.assertEqual(names, {"us-commodities", "us-proudmoore"})

    def test_all_realms_pricing_flag_covers_its_realms(self):
        self.conn.execute("UPDATE markets SET source = 'us-all' WHERE name = 'us-stormrage'")
        cfg = {"market": [{"name": "us-commodities"}, {"name": "us-all"}]}
        self.assertEqual({s.name for s in pricing.build_sets(self.conn, cfg)}, {"us-commodities", "us-stormrage"})
        cfg["market"][1]["pricing"] = False
        self.assertEqual({s.name for s in pricing.build_sets(self.conn, cfg)}, {"us-commodities"})

    def test_timing_from_hourly_rows(self):
        # Sales: listing 1 shrinks by 40 between the two snapshots.
        t = NOW + 2 * HOUR
        store.store_snapshot(self.conn, "us-commodities", {"auctions": [commodity(1, LINEN, 60, 900)]}, t)
        prof = pricing.timing(self.conn, LINEN, markets=["us-commodities"])["us-commodities"]
        g = time.gmtime(t)
        b = prof[((g.tm_wday + 1) % 7) * 24 + g.tm_hour]
        self.assertEqual((b["sold"], b["p10_avg"]), (40, 900))
        self.assertEqual(sum(x["sold"] for x in prof), 40)
        local = pricing.to_local(prof, -5)
        self.assertEqual(local[(((g.tm_wday + 1) % 7) * 24 + g.tm_hour - 5) % 168]["sold"], 40)


class FlipTests(unittest.TestCase):
    """48 hourly commodity snapshots: one item swings 100c <-> 130c every 6 hours, one sits flat."""

    SWING, FLAT, TIGHT = 1001, 1002, 1003

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO markets (name, region, namespace, kind, game, realms) "
                          "VALUES ('us-commodities', 'us', 'dynamic-us', 'commodities', 'retail', '[]')")
        self.start = NOW - 47 * HOUR
        for h in range(48):
            low = (h // 6) % 2 == 0
            auctions = [commodity(1000 + h, self.SWING, 50, 100 if low else 130),  # sells out every hour
                        commodity(1, self.SWING, 500 - h, 200),                     # one unit bought per hour
                        commodity(2, self.FLAT, 500, 50),                           # never changes: no new rows
                        commodity(2000 + h, self.TIGHT, 50, 100 if low else 102)]
            store.store_snapshot(self.conn, "us-commodities", {"auctions": auctions}, self.start + h * HOUR)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def report(self, **kw):
        return pricing.flip_report(self.conn, "us-commodities", days=7, min_moved_per_day=100,
                                   min_gold_per_day=0, **kw)

    def test_state_carries_forward(self):
        snaps, series = pricing.hourly_series(self.conn, "us-commodities", self.start)
        self.assertEqual(len(snaps), 48)
        flat = series[(self.FLAT, 0)]
        self.assertEqual(len(flat), 48)
        self.assertTrue(all(state[0] == 50 and state[3] == 500 for _, state, _, _ in flat))

    def test_round_trips(self):
        flips, _, liquid = self.report()
        self.assertEqual(liquid, 2)  # flat never sells
        [f] = flips  # tight's 2% swing doesn't cover the AH cut
        self.assertEqual((f.item_id, f.buy_below, f.sell_at, f.trips), (self.SWING, 100, 130, 4))
        self.assertAlmostEqual(f.margin, (130 * 0.95 - 100) / 100)
        self.assertEqual(f.depth_low, 50)
        self.assertAlmostEqual(f.drift, 0)
        days = 47 / 24
        self.assertAlmostEqual(f.sold_per_day, 47 / days)
        self.assertAlmostEqual(f.moved_per_day, (47 + 47 * 50) / days)
        self.assertEqual(f.profit_per_trip, int(50 * (130 * 0.95 - 100)))
        self.assertEqual(self.report(min_trips=5)[0], [])
        self.assertEqual(self.report(min_margin=0.3)[0], [])

    def test_weekly_index(self):
        _, index, _ = self.report()
        cells = {}
        for h in range(48):
            g = time.gmtime(self.start + h * HOUR)
            cells[((g.tm_wday + 1) % 7, g.tm_hour)] = (h // 6) % 2 == 0
        self.assertEqual(sum(v is not None for row in index for v in row), len(cells))
        for (d, hr), low in cells.items():
            if low:
                self.assertLess(index[d][hr], 1)  # swing and tight sit below their medians
            else:
                self.assertEqual(index[d][hr], 1)

    def test_report_text(self):
        args = argparse.Namespace(conn=self.conn, config=os.path.join(self.tmp.name, "none.toml"), days=7,
                                  limit=10, market=None, utc_offset=0)
        orig = pricing.flip_report
        pricing.flip_report = lambda conn, mkt, days: orig(conn, mkt, days=days, min_moved_per_day=100,
                                                           min_gold_per_day=0)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                reports.flips_cmd(args)
        finally:
            pricing.flip_report = orig
        text = buf.getvalue()
        self.assertIn("us-commodities: flips over the last 7 days (2 liquid items, 1 with", text)
        self.assertIn(f"item {self.SWING}", text)
        self.assertIn("Price index", text)


if __name__ == "__main__":
    unittest.main()
