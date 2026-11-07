import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import ledger, market, tsm  # noqa: E402

REGION = """itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt
2319,Medium Leather,222,241,151,0.946,906.268,2026-11-06T08:14:34Z
252437,,1674,1681,1414,0.407,16.484,2026-11-06T08:14:34Z
"""
REALM = """itemId,name,marketValue,minBuyout,recent,historical,updatedAt
2319,Medium Leather,230,200,210,250,2026-11-07T11:22:35Z
"""


class TsmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_files_for_markets(self):
        cfg = {"market": [{"name": "us-proudmoore", "region": "us", "namespace": "dynamic-us", "kind": "realm",
                           "realm": "proudmoore"},
                          {"name": "us-forever-all-realms", "region": "us", "namespace": "dynamic-forever-us",
                           "kind": "all-realms"},
                          {"name": "us-mists-pagle", "region": "us", "namespace": "dynamic-classic-us",
                           "kind": "classic", "realm": "pagle"}]}  # no TSM mapping for Mists
        self.assertEqual([f[4] for f in tsm.files_for(cfg)], [
            "retail/us/region/items.csv", "retail/us/region/pets.csv", "retail/us/commodities.csv",
            "retail/us/realm/proudmoore/items.csv", "retail/us/realm/proudmoore/pets.csv",
            "forever/us/region/items.csv"])

    def test_refresh_and_lookup(self):
        cfg = {"market": [{"name": "us-proudmoore", "region": "us", "namespace": "dynamic-us", "kind": "realm",
                           "realm": "proudmoore"}]}
        served, calls = {"retail/us/region/items.csv": REGION, "retail/us/realm/proudmoore/items.csv": REALM}, []

        def download(path, since):
            calls.append((path, since))
            if path not in served:
                raise OSError("HTTP Error 404")
            return (None, since) if since else (served[path], "Fri, 06 Nov 2026 08:20:00 GMT")
        logs = []
        self.assertEqual(tsm.refresh(self.conn, cfg, now=1000, log=logs.append, download=download), 2)
        got = tsm.lookup(self.conn, "retail", "us", [2319, 252437, 1], realm="proudmoore")
        self.assertEqual(got[2319]["region"], {"market_value": 222, "historical": 241, "updated": 1793952874,
                                               "avg_sale_price": 151, "sale_rate": 0.946, "sold_per_day": 906.268})
        self.assertEqual(got[2319]["realm"]["min_buyout"], 200)
        self.assertEqual(got[252437]["region"]["sale_rate"], 0.407)
        self.assertNotIn(1, got)
        self.assertEqual(len(calls), 5)
        self.assertEqual(tsm.refresh(self.conn, cfg, now=2000, log=logs.append, download=download), 0)
        self.assertEqual(len(calls), 5)  # checked at most hourly
        self.assertEqual(tsm.refresh(self.conn, cfg, now=1000 + 3600, log=logs.append, download=download), 0)
        self.assertIn(("retail/us/region/items.csv", "Fri, 06 Nov 2026 08:20:00 GMT"), calls)  # If-Modified-Since
        self.assertEqual(tsm.lookup(self.conn, "retail", "us", [2319])[2319]["region"]["market_value"], 222)


if __name__ == "__main__":
    unittest.main()
