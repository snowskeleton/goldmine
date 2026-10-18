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

import goldmine  # noqa: E402
import market  # noqa: E402
import store  # noqa: E402
import pricing  # noqa: E402
import svparse  # noqa: E402

HOUR = 3600
NOW = int(time.time()) // HOUR * HOUR - 6 * HOUR  # recent, so default 72h windows include it
LINEN, BOLT = 2589, 2996


def commodity(aid, item, qty, unit, tl="LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "unit_price": unit, "time_left": tl}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "gm.sqlite")
        self.conn = goldmine.connect(self.db)
        market.ensure_schema(self.conn)
        # 5 hourly snapshots: linen ~15c, bolts ~100c with one dumped bolt at 40c in the last one.
        for h in range(5):
            auctions = [commodity(10 + h, LINEN, 100, 15), commodity(1, LINEN, 500, 18)]
            auctions += [commodity(20 + h, BOLT, 10, 100), commodity(2, BOLT, 50, 110)]
            if h == 4:
                auctions.append(commodity(99, BOLT, 3, 40))
            store.store_snapshot(self.conn, "us-commodities", {"auctions": auctions}, NOW - (4 - h) * HOUR)
        # Recipe: 2 linen -> 1 bolt, known by Snow.
        self.conn.execute("INSERT INTO recipes VALUES ('Snow-Testrealm', 100, 'Tailoring', 'Bolt of Linen Cloth', ?, 1)", (BOLT,))
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

    def test_deal_report(self):
        prices = pricing.price_table(self.conn, now=NOW)
        deals = pricing.deal_report(prices)
        self.assertEqual([d.item_id for d in deals], [BOLT])
        self.assertEqual(deals[0].profit_per_unit, 95 - 40)

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
        self.conn = goldmine.connect(self.db)
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
        c.execute("INSERT INTO recipes VALUES ('Old-Nazgrim', 100, 'Tailoring', 'Bolt', ?, 1)", (BOLT,))
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
            goldmine.push(self.conn, self.args, cfg={})
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


if __name__ == "__main__":
    unittest.main()
