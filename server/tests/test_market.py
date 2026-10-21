import os
import sqlite3
import sys
import calendar
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import ledger, market  # noqa: E402


HOUR = 3600


def commodity(aid, item, qty, unit, tl="LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "unit_price": unit, "time_left": tl}


def realm_auction(aid, item, qty, buyout=None, bid=None, tl="LONG", pet=None):
    a = {"id": aid, "item": {"id": item}, "quantity": qty, "time_left": tl}
    if pet:
        a["item"]["pet_species_id"] = pet
    if buyout is not None:
        a["buyout"] = buyout
    if bid is not None:
        a["bid"] = bid
    return a


class FakeClient:
    """Stands in for market.Client; serves canned responses keyed by path."""

    def __init__(self, responses):
        self.responses = responses  # path -> list of (json, last_modified) served in order
        self.calls = []

    def get(self, region, namespace, path, params=None, if_modified_since=None):
        self.calls.append((region, namespace, path, if_modified_since))
        if path not in self.responses:
            raise market.ApiError(f"GET {path} ({namespace}): HTTP 404")
        queue = self.responses[path]
        data, lm = queue.pop(0) if len(queue) > 1 else queue[0]
        if if_modified_since and lm == if_modified_since:
            return None, lm
        return data, lm


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_game_for(self):
        self.assertEqual(market.game_for("dynamic-us"), "retail")
        self.assertEqual(market.game_for("dynamic-classic-eu"), "classic")
        self.assertEqual(market.game_for("dynamic-classic1x-us"), "classic1x")
        self.assertEqual(market.game_for("dynamic-classic-us", "forever"), "forever")

    def test_collect_retail_and_classic(self):
        t0 = 1_790_000_000
        client = FakeClient({
            "/data/wow/auctions/commodities": [({"auctions": [commodity(1, 10, 5, 100)]}, t0)],
            "/data/wow/realm/proudmoore": [({"connected_realm": {"href": "https://x/data/wow/connected-realm/5?namespace=dynamic-us"}}, None)],
            "/data/wow/connected-realm/5": [({"realms": [{"name": "Proudmoore"}]}, None)],
            "/data/wow/connected-realm/4395": [({"realms": [{"name": "Whitemane"}, {"name": "Moon Guard"}]}, None)],
            "/data/wow/connected-realm/5/auctions": [({"auctions": [realm_auction(7, 20, 1, buyout=5000)]}, t0)],
            "/data/wow/realm/whitemane": [({"connected_realm": {"href": "https://x/data/wow/connected-realm/4395?namespace=x"}}, None)],
            "/data/wow/connected-realm/4395/auctions/index": [(
                {"auctions": [{"id": 2, "name": "Alliance Auction House"}, {"id": 6, "name": "Horde Auction House"}]}, None)],
            "/data/wow/connected-realm/4395/auctions/2": [({"auctions": [realm_auction(8, 2589, 20, buyout=300)]}, t0)],
            "/data/wow/connected-realm/4395/auctions/6": [({"auctions": []}, t0)],
            "/data/wow/token/index": [({"last_updated_timestamp": t0 * 1000, "price": 3_000_000_000}, None),
                                      ({"last_updated_timestamp": (t0 + 1200) * 1000, "price": 3_000_000_000}, None),
                                      ({"last_updated_timestamp": (t0 + 2400) * 1000, "price": 3_100_000_000}, None)],
        })
        cfg = {"market": [
            {"name": "us-commodities", "region": "us", "namespace": "dynamic-us", "kind": "commodities"},
            {"name": "us-proudmoore", "region": "us", "namespace": "dynamic-us", "kind": "realm", "realm": "proudmoore"},
            {"name": "era-whitemane", "region": "us", "namespace": "dynamic-classic1x-us", "kind": "classic", "realm": "whitemane"},
        ]}
        logs = []
        self.assertEqual(market.collect(self.conn, client, cfg, log=logs.append), 0)
        markets = sorted(r[0] for r in self.conn.execute("SELECT DISTINCT market FROM snapshots"))
        self.assertEqual(markets, ["era-whitemane:Alliance Auction House", "era-whitemane:Horde Auction House",
                                   "us-commodities", "us-proudmoore"])
        unit = self.conn.execute("SELECT min_price FROM latest_prices WHERE item_id = 2589").fetchone()[0]
        meta = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT name, game, realms FROM markets")}
        self.assertEqual(meta["us-proudmoore"], ("retail", '["Proudmoore"]'))
        self.assertEqual(meta["era-whitemane:Horde Auction House"], ("classic1x", '["Whitemane", "Moon Guard"]'))
        self.assertEqual(meta["us-commodities"], ("retail", "[]"))
        self.assertEqual(unit, 15)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0], 3)
        # Second run: snapshots unchanged -> conditional GET, nothing new stored, realm id cached.
        market.collect(self.conn, client, cfg, log=logs.append)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 4)
        self.assertIn("us-commodities: unchanged", logs)
        realm_lookups = [c for c in client.calls if c[2] == "/data/wow/realm/proudmoore"]
        self.assertEqual(len(realm_lookups), 1)
        # The WoW token is read once per retail region per run, and stored only when its price changes.
        market.collect(self.conn, client, cfg, log=logs.append)
        tokens = [c for c in client.calls if c[2] == "/data/wow/token/index"]
        self.assertEqual({(c[0], c[1]) for c in tokens}, {("us", "dynamic-us")})
        self.assertEqual(len(tokens), 3)
        self.assertEqual(self.conn.execute("SELECT updated, price FROM token_prices ORDER BY updated").fetchall(),
                         [(t0, 3_000_000_000), (t0 + 2400, 3_100_000_000)])

    def test_all_realms(self):
        t0 = 1_790_000_000
        market._realm_lists.clear()

        def group(crid, *slugs):
            return {"data": {"id": crid, "realms": [{"slug": s, "name": {"en_US": s.title()}} for s in slugs]}}
        client = FakeClient({
            "/data/wow/search/connected-realm": [({"pageCount": 1, "results": [
                group(5, "proudmoore"), group(60, "wyrmrest-accord", "moon-guard"), group(61, "stormrage")]}, None)],
            "/data/wow/realm/proudmoore": [({"connected_realm": {"href": "https://x/data/wow/connected-realm/5"}}, None)],
            "/data/wow/connected-realm/5": [({"realms": [{"name": "Proudmoore"}]}, None)],
            **{f"/data/wow/connected-realm/{c}/auctions": [({"auctions": [realm_auction(c, 20, 1, buyout=c)]}, t0)]
               for c in (5, 60, 61)},
            "/data/wow/token/index": [({"last_updated_timestamp": t0 * 1000, "price": 1}, None)],
        })
        own = {"name": "us-proudmoore", "region": "us", "namespace": "dynamic-us", "kind": "realm", "realm": "proudmoore"}
        every = {"name": "us-retail-all-realms", "region": "us", "namespace": "dynamic-us", "kind": "all-realms"}
        market.collect(self.conn, client, {"market": [own, every]}, log=lambda _: None)
        rows = {r[0]: r[1:] for r in self.conn.execute("SELECT name, connected_realm_id, source, realms FROM markets")}
        self.assertEqual(rows, {
            "us-proudmoore": (5, None, '["Proudmoore"]'),  # configured on its own: not doubled
            "us-moon-guard": (60, "us-retail-all-realms", '["Moon-Guard", "Wyrmrest-Accord"]'),
            "us-stormrage": (61, "us-retail-all-realms", '["Stormrage"]')})
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT market) FROM snapshots").fetchone()[0], 3)

        # The realm's own entry removed: the all-realms entry takes it over under the same name.
        market.collect(self.conn, client, {"market": [every]}, log=lambda _: None)
        self.assertEqual(self.conn.execute("SELECT source FROM markets WHERE name = 'us-proudmoore'").fetchone()[0],
                         "us-retail-all-realms")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0], 3)
        searches = [c for c in client.calls if c[2] == "/data/wow/search/connected-realm"]
        self.assertEqual(len(searches), 1)  # the realm list is reused between runs

    def test_collect_downloads_in_parallel(self):
        import threading
        t0 = 1_790_000_000

        class SlowClient:
            """Realm n takes (10 - n) ms, so later realms finish first; realm 3 fails."""
            def __init__(self):
                self.active = self.peak = 0
                self.lock = threading.Lock()

            def get(self, region, namespace, path, params=None, if_modified_since=None):
                if path == "/data/wow/token/index":
                    return None, None
                n = int(path.split("/")[-2])
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                time.sleep((10 - n) / 1000)
                with self.lock:
                    self.active -= 1
                if n == 3:
                    raise market.ApiError("HTTP 503")
                return {"auctions": [realm_auction(n, 20, 1, buyout=n)]}, t0

        client = SlowClient()
        for n in range(1, 10):  # realm ids already known, so no lookups
            self.conn.execute("INSERT INTO markets (name, region, namespace, kind, connected_realm_id, realms) "
                              "VALUES (?, 'us', 'dynamic-us', 'realm', ?, '[]')", (f"r{n}", n))
        cfg = {"market": [{"name": f"r{n}", "region": "us", "namespace": "dynamic-us", "kind": "realm",
                           "realm": f"r{n}"} for n in range(1, 10)]}
        logs = []
        self.assertEqual(market.collect(self.conn, client, cfg, log=logs.append), 1)
        self.assertEqual([m.split(":")[0] for m in logs[:9]], [f"r{n}" for n in range(1, 10)])  # config order
        self.assertIn("HTTP 503", logs[2])
        self.assertEqual(logs[9], "8 of 9 markets had new data (0s)")
        self.assertLessEqual(client.peak, market.FETCH_WORKERS)
        self.assertGreater(client.peak, 1)

    def test_probe_unpublished(self):
        t0 = 1_790_000_000
        realms = {"/data/wow/connected-realm/index": [({"connected_realms": [{"href": "https://x/data/wow/connected-realm/5085?n=1"}]}, None)],
                  "/data/wow/connected-realm/5085/auctions/index": [({"auctions": [{"id": 2}]}, None)]}
        client = FakeClient(realms)  # listings 404, as Era and Anniversary do
        logs = []
        market.probe_unpublished(self.conn, client, {"us"}, now=t0, log=logs.append)
        self.assertEqual(self.conn.execute("SELECT namespace, ok FROM api_probes ORDER BY 1").fetchall(),
                         [("dynamic-classic1x-us", 0), ("dynamic-classicann-us", 0)])
        self.assertEqual(logs, [])
        client.responses["/data/wow/connected-realm/5085/auctions/2"] = [({"auctions": [{"id": 1}]}, None)]
        market.probe_unpublished(self.conn, client, {"us"}, now=t0 + 3600, log=logs.append)
        self.assertEqual(logs, [])  # checked at most once a day
        market.probe_unpublished(self.conn, client, {"us"}, now=t0 + 86400, log=logs.append)
        self.assertEqual(len(logs), 2)
        self.assertIn("Classic Era (us): Blizzard now serves auction house data (1 listings", logs[0])
        games = {g["game"]: g["auctions"] for g in market.namespace_games(self.conn)}
        self.assertEqual(games, {"retail": True, "classic": True, "classic1x": True, "classicann": True})
        market.probe_unpublished(self.conn, client, {"us"}, now=t0 + 2 * 86400, log=logs.append)
        self.assertEqual(len(logs), 2)  # still fine: nothing new to say

    def test_fetch_item_info(self):
        client = FakeClient({
            "/data/wow/item/2589": [({"name": "Linen Cloth", "quality": {"type": "COMMON"}, "sell_price": 13,
                                      "purchase_price": 55, "item_class": {"id": 7, "name": "Tradeskill"},
                                      "item_subclass": {"id": 5, "name": "Cloth"}, "is_stackable": True,
                                      "max_count": 0, "level": 5, "inventory_type": {"type": "NON_EQUIP"}}, None)],
            "/data/wow/item/2996": [({"name": "Bolt of Linen Cloth", "sell_price": 40,
                                      "preview_item": {"binding": {"type": "ON_ACQUIRE"}}}, None)],
        })
        self.assertEqual(market.fetch_item_info(self.conn, client, "us", "dynamic-us", [2589, 2996, 1], log=lambda m: 0), 2)
        rows = {r[0]: r[1:] for r in self.conn.execute(
            "SELECT item_id, sell_price, binding, class_name, subclass_id, stackable FROM item_info")}
        self.assertEqual(rows, {2589: (13, None, "Tradeskill", 5, 1), 2996: (40, "ON_ACQUIRE", None, None, None),
                                1: (None,) * 5})  # 404: remembered so it isn't asked again
        self.assertEqual(self.conn.execute("SELECT name FROM items WHERE item_id = 2996").fetchone()[0],
                         "Bolt of Linen Cloth")
        self.assertEqual(client.calls[0][1], "static-us")
        n = len(client.calls)  # known items aren't fetched again, nor ones the API doesn't have (404)
        market.fetch_item_info(self.conn, client, "us", "dynamic-us", [2589, 2996, 1], log=lambda m: 0)
        self.assertEqual(len(client.calls), n)


if __name__ == "__main__":
    unittest.main()
