"""Account state for the trader (account.py) and its MCP tools, on the fixture market DB plus the
addon's sample SavedVariables imported as a user's game data."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("GOLDMINE_DATA", TMP.name)
os.environ["GOLDMINE_COLLECTOR"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from goldmine_server import account, auth, db, ledger, marketdata as md, settings  # noqa: E402
from goldmine_server.app import app  # noqa: E402
from test_marketdata import LINEN, SILK, WOOL, build_fixture  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "Goldmine.lua"
BOLT = 2996


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        d = Path(cls.dir.name)
        cls.patches = [mock.patch.object(settings, k, v) for k, v in
                       {"DATA": d, "MARKET_DB": d / "market.sqlite", "SERVER_DB": d / "server.sqlite",
                        "USERS_DIR": d / "users", "CONFIG": d / "config.toml"}.items()]
        for p in cls.patches:
            p.start()
        build_fixture(settings.MARKET_DB)
        conn = sqlite3.connect(settings.MARKET_DB)
        conn.executemany("INSERT INTO token_prices VALUES ('us', ?, ?, ?)",
                         [(1000, 3_000_000_000, 1000), (5000, 3_200_000_000, 5000)])
        conn.execute("INSERT INTO items VALUES (?, 'Bolt of Linen Cloth', 'COMMON', 0)", (BOLT,))
        conn.execute("INSERT INTO item_info (item_id, sell_price, purchase_price, binding, class_id, class_name, "
                     "stackable, fetched) VALUES (?, 13, 55, NULL, 7, 'Tradeskill', 1, 1)", (LINEN,))
        conn.execute("INSERT INTO item_info (item_id, binding, stackable, fetched) VALUES (?, 'ON_ACQUIRE', 0, 1)",
                     (SILK,))
        conn.commit()
        conn.close()
        db._schema_ready = False
        with db.server_conn() as conn:
            cls.uid = conn.execute("INSERT INTO users (name, handle, is_admin, created) VALUES ('snow', x'01', 1, 0)"
                                   ).lastrowid
        conn = db.user_conn(cls.uid)
        ledger.import_file(conn, FIXTURE)
        conn.close()
        cls.h = {"authorization": f"Bearer {auth.create_client_key(auth.User(cls.uid, 'snow', True), 'claude')}"}
        cls.api = TestClient(app)
        cls.conn = md.ro_conn(cls.uid)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for p in cls.patches:
            p.stop()
        db._schema_ready = False
        cls.dir.cleanup()

    def call(self, name, **arguments):
        r = self.api.post("/api/v1/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        result = r.json()["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]

    def test_account(self):
        a = account.account(self.conn)
        (c,) = a["characters"]
        self.assertEqual((c["character"], c["region"]), ("Snow-Testrealm", "US"))
        self.assertTrue(c["gold"] > 0 and c["last_seen"] and c["bags_seen"] and c["auctions_seen"])
        self.assertEqual((a["warband"]["gold"], a["warband"]["seen_by"]), (2000000, "Snow-Testrealm"))
        self.assertEqual(a["total_gold"], c["gold"] + 2000000)
        self.assertEqual(a["wow_token"]["us"]["gold"], 320000)
        self.assertEqual(account.account(self.conn, realm="Elsewhere")["characters"], [])
        self.assertEqual(self.call("get_account", realm="Testrealm")["total_gold"], a["total_gold"])

    def test_inventory(self):
        inv = account.inventory(self.conn)
        by = {e["item_id"]: e for e in inv["items"]}
        self.assertEqual((by[LINEN]["count"], by[LINEN]["name"]), (25, "Linen Cloth"))
        self.assertEqual(by[4306]["held"], [{"owner": "warband", "location": "warband", "count": 50,
                                             "seen": by[4306]["held"][0]["seen"]}])
        self.assertEqual([e["item_id"] for e in account.inventory(self.conn, location="bank")["items"]], [BOLT])
        self.assertEqual(account.inventory(self.conn, char="Snow", items=["Linen Cloth"])["total"], 1)
        page = self.call("get_inventory", limit=1)
        self.assertEqual((len(page["items"]), page["next_offset"]), (1, 1))
        self.assertEqual(page["items"][0]["item_id"], 4306)  # largest count first

    def test_owned_auctions(self):
        r = self.call("get_owned_auctions")
        self.assertEqual(r["lists"][0]["count"], 2)
        a = {x["auction_id"]: x for x in r["auctions"]}
        self.assertEqual((a[501]["unit_price"], a[501]["commodity"], a[501]["status"], a[501]["current"]),
                         (18, True, "active", True))
        self.assertEqual((a[502]["status"], a[502]["expires"] - a[502]["last_seen"]), ("sold", 3600))

    def test_ledger(self):
        r = self.call("get_ledger", since=0, source="auction_sale")
        (tx,) = r["transactions"]
        self.assertEqual((tx["delta"], tx["item_name"], tx["meta"]["buyer"]), (9500, "Linen Cloth", "Buyerguy"))
        self.assertEqual(r["totals"], [{"source": "auction_sale", "in": 9500, "out": 0, "count": 1}])
        everything = account.ledger_rows(self.conn, since=0)
        times = [t["time"] for t in everything["transactions"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(account.ledger_rows(self.conn)["total"], 0)  # fixture is older than the 7-day default
        self.assertEqual(account.ledger_rows(self.conn, since=0, char="Nobody")["total"], 0)

    def test_recipes(self):
        r = self.call("get_recipes", item=LINEN)
        (rec,) = r["recipes"]
        self.assertEqual((rec["recipe_id"], rec["output_name"], rec["reagents"]),
                         (100, "Bolt of Linen Cloth", [{"item_id": LINEN, "name": "Linen Cloth", "quantity": 2}]))
        op = rec["operation"]
        self.assertEqual((op["skill"], op["difficulty"], op["quality"], op["concentration_cost"], op["multicraft"]),
                         (85, 100, 2, 30, 12.5))
        self.assertEqual(op["stats"]["Resourcefulness"], 9)
        profs = {x["profession"]: x for x in r["professions"]}
        self.assertEqual((profs["Tailoring"]["concentration"], profs["Tailoring"]["skill"]), (640, 90))
        # The fixture's catalogue is old; Alchemy was never opened; Fishing has no recipes.
        self.assertEqual({k: v["needs_catalogue"] for k, v in profs.items()},
                         {"Tailoring": True, "Alchemy": True, "Fishing": False})
        self.assertEqual(account.recipes(self.conn, item=WOOL)["recipes"], [])

    def test_craft_results(self):
        r = self.call("get_craft_results", since=0)
        (c,) = r["crafts"]
        self.assertEqual((c["recipe_id"], c["name"], c["quantity"], c["multicraft"], c["returned"]),
                         (100, "Bolt of Linen Cloth", 2, 1, [{"item_id": LINEN, "quantity": 1}]))
        self.assertEqual((c["reagents"], c["reagents_estimated"], c["order_id"]),
                         ([{"item_id": LINEN, "quantity": 2}], True, None))
        (s,) = r["by_recipe"]
        self.assertEqual((s["crafts"], s["multicraft_rate"], s["resourcefulness_rate"], s["avg_quality"]),
                         (1, 1.0, 1.0, 3))
        self.assertEqual(account.craft_results(self.conn, since=0, recipe=999)["crafts"], [])

    def test_token_price(self):
        r = self.call("get_token_price", since=0)
        self.assertEqual(r["latest"]["price"], 3_200_000_000)
        self.assertEqual([h["price"] for h in r["history"]], [3_200_000_000, 3_000_000_000])
        self.assertEqual(account.token_price(self.conn, "eu")["latest"], None)

    def test_item_info(self):
        r = {i["item_id"]: i for i in self.call("get_item_info", items=[LINEN, SILK, BOLT, 999999])["items"]}
        self.assertEqual((r[LINEN]["vendor_sell"], r[LINEN]["stack_size"], r[LINEN]["class"], r[LINEN]["bound"],
                          r[LINEN]["sources"]), (13, 200, "Tradeskill", False, ["api", "game"]))
        self.assertEqual((r[SILK]["binding"], r[SILK]["bound"], r[SILK]["stack_size"]), ("ON_ACQUIRE", True, 200))  # the game client's stack size wins
        self.assertEqual((r[BOLT]["sources"], r[BOLT]["stack_size"], r[BOLT]["expansion"]), (["game"], 200, 0))
        self.assertEqual((r[999999]["sources"], r[999999]["bound"]), ([], None))

    def test_professions_in_account(self):
        (c,) = account.account(self.conn)["characters"]
        self.assertEqual(sorted(p["profession"] for p in c["professions"]), ["Alchemy", "Fishing", "Tailoring"])

    def test_auction_mail(self):
        r = self.call("get_auction_mail", since=0)
        self.assertEqual(r["total"], 5)
        (linen,) = r["sales_by_item"]
        self.assertEqual((linen["sales"], linen["quantity"], linen["gross"], linen["net"], linen["buyers"],
                          linen["avg_unit_price"]), (3, 300, 5400, 5130, 1, 18))
        sold = self.call("get_auction_mail", since=0, kind="sold", item=LINEN)["mail"]
        self.assertEqual({(m["player"], m["unit_price"]) for m in sold}, {("Buyerguy", 18)})
        self.assertEqual(account.auction_mail(self.conn, since=0, player="Sellerguy")["mail"][0]["kind"], "won")
        self.assertEqual(account.auction_mail(self.conn)["total"], 0)  # the fixture is older than 30 days

    def test_crafting_orders(self):
        active = self.call("get_crafting_orders")["orders"]
        self.assertEqual([o["order_id"] for o in active], [9002])  # 9001 expired long ago
        r = self.call("get_crafting_orders", active_only=False)
        first = r["orders"][0]
        self.assertEqual((first["order_id"], first["type"], first["tip"], first["name"], first["reagents_provided"]),
                         (9001, "public", 50000, "Bolt of Linen Cloth",
                          [{"item_id": LINEN, "name": "Linen Cloth", "quantity": 2}]))
        self.assertEqual(r["orders"][1]["rewards"], [{"item_id": 210814, "name": None, "count": 3}])
        self.assertEqual(r["public_board"][0]["available"], 4)
        self.assertEqual(self.call("get_crafting_orders", order_type="patron", active_only=False)["total"], 1)

    def test_bad_since(self):
        r = self.api.post("/api/v1/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_ledger", "arguments": {"since": "last tuesday"}}})
        self.assertTrue(r.json()["result"]["isError"])

    def test_owned_item_ids(self):
        ids = account.owned_item_ids()
        self.assertTrue({LINEN, 4306, BOLT} <= ids)


if __name__ == "__main__":
    unittest.main()
