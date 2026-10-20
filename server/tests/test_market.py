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
