import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import ledger, market, pricing, region, reports, store  # noqa: E402

HOUR = 3600
T0 = 1_790_000_000 - 1_790_000_000 % 86400 + 2 * HOUR
ITEMS = range(100, 160)   # listed on every realm
FILLER = 999              # pads each realm to its size


def listing(aid, item, buyout):
    return {"id": aid, "item": {"id": item}, "quantity": 1, "buyout": buyout, "time_left": "VERY_LONG"}


class RegionTests(unittest.TestCase):
    """Ten realms from 100 to 800 listings; each item's price falls as size ** -0.5."""

    SIZES = [100, 130, 170, 220, 280, 360, 460, 590, 700, 800]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)
        for n, size in enumerate(self.SIZES):
            name = f"us-r{n}"
            self.conn.execute("INSERT INTO markets (name, region, namespace, kind, game) "
                              "VALUES (?, 'us', 'dynamic-us', 'realm', 'retail')", (name,))
            self.put(name, n, size, T0)

    def put(self, name, n, size, t, odd=None):
        aid = n * 100_000
        auctions = [listing(aid + i, i, self.price(i, size)) for i in ITEMS]
        if odd:
            auctions.append(listing(aid + 500, *odd))
        auctions += [listing(aid + 1000 + k, FILLER, 1) for k in range(size - len(auctions))]
        return store.store_snapshot(self.conn, name, {"auctions": auctions}, t)

    @staticmethod
    def price(item, size):
        return round(item * 10_000 * (size / 280) ** -0.5)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_size_exponent_and_expected_prices(self):
        r = region.rollup(self.conn, "us", "retail", log=lambda _: None)
        self.assertAlmostEqual(r["beta"], -0.5, delta=0.02)
        self.assertEqual(r["ref"], 320)  # median realm size
        for n in (0, 9):  # the region, sized like each realm, expects that realm's own price
            view = region.expected(self.conn, f"us-r{n}")
            self.assertAlmostEqual(view[(130, 0)]["expected"] / self.price(130, self.SIZES[n]), 1, delta=0.03)
            self.assertEqual(view[(130, 0)]["realms"], 10)
        raw = sorted(self.price(130, s) for s in self.SIZES)
        self.assertEqual(view[(130, 0)]["raw"], round((raw[4] + raw[5]) / 2))
        self.assertIsNone(region.rollup(self.conn, "us", "retail", log=lambda _: None))  # nothing new

    def test_items_on_few_realms_get_no_series(self):
        self.put("us-r0", 0, 100, T0 + HOUR, odd=(777, 5000))
        region.rollup(self.conn, "us", "retail", log=lambda _: None)
        self.assertNotIn((777, 0), region.expected(self.conn, "us-r0"))

    def test_deal_verdicts(self):
        region.rollup(self.conn, "us", "retail", log=lambda _: None)
        fair = self.price(130, 100)
        deals = [pricing.Deal(130, 0, fair // 10, fair, 5),          # wildly cheap here and everywhere
                 pricing.Deal(140, 0, self.price(140, 100), self.price(140, 100) * 50, 5),  # inflated history here
                 pricing.Deal(999_999, 0, 10, 20, 5)]                # no region view
        region.annotate(self.conn, "us-r0", deals)
        self.assertEqual([d.verdict for d in deals], ["confirmed", "normal", None])
        self.assertAlmostEqual(deals[0].region_ratio, 0.1, delta=0.01)

    def test_history_rescales_to_the_realm(self):
        region.rollup(self.conn, "us", "retail", log=lambda _: None)
        for n in range(10):  # an hour later every realm doubles the price of item 130
            self.put(f"us-r{n}", n, self.SIZES[n], T0 + HOUR)
            self.conn.execute("UPDATE item_current SET p10 = p10 * 2 WHERE item_id = 130 AND market_id = "
                              "(SELECT mid FROM markets WHERE name = ?)", (f"us-r{n}",))
        self.conn.execute("INSERT INTO snapshots (market, fetched_at, last_modified, auctions) "
                          "VALUES ('us-r0', 0, ?, 100)", (T0 + 2 * HOUR,))
        region.rollup(self.conn, "us", "retail", log=lambda _: None)
        h = region.history(self.conn, "us-r0", 130, 0, T0 - HOUR)
        own = self.price(130, 100)
        self.assertEqual(len(h), 2)
        self.assertAlmostEqual(h[0]["region"] / own, 1, delta=0.03)
        self.assertAlmostEqual(h[1]["region"] / own, 2, delta=0.06)
        cmp = region.compare(self.conn, "us-r0", "us-r9", 130, 0, T0 - HOUR)
        self.assertEqual(cmp[0]["compare_raw"], self.price(130, 800))
        self.assertAlmostEqual(cmp[0]["compare"] / own, 1, delta=0.03)  # us-r9's price, sized like us-r0

    def test_merge_overlays(self):
        pts = [{"t": 10, "p10": 5, "sold": 1}, {"t": 30, "p10": 6, "sold": 2}]
        extra = [{"t": 5, "region": 9}, {"t": 20, "region": 8}, {"t": 30, "region": 7}]
        got = reports._merge(pts, extra, ("region",))
        self.assertEqual(got, [{"t": 10, "p10": 5, "sold": 1, "region": 9},
                               {"t": 20, "p10": 5, "sold": 0, "carried": True, "region": 8},
                               {"t": 30, "p10": 6, "sold": 2, "region": 7}])


if __name__ == "__main__":
    unittest.main()
