"""Read-only market data: the query functions, the /api/v1/market routes, and the MCP endpoint,
on a small fixture market DB built with store.store_snapshot."""

import hashlib
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

from goldmine_server import auth, db, market, marketdata as md, settings, store  # noqa: E402
from goldmine_server.app import app  # noqa: E402

HOUR = 3600
T0 = 1792022400 + HOUR  # 2026-10-15 01:00 UTC
LINEN, WOOL, SILK = 2589, 2592, 4306


def realm(aid, item, qty, unit, tl="LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "buyout": unit * qty, "time_left": tl}


def build_fixture(path: Path) -> None:
    """us-testrealm, four hourly snapshots:
      linen  20 @ 10c + 5 @ 30c; unchanged at T1; 5 bought from the 20 at T2; the 5 @ 30c vanish at T3
      wool   10 @ 100c, 2 bought at T1 and at T2, then 1 @ 20c appears at T3 (a deal)
      silk   1 @ 500c, the whole time"""
    conn = sqlite3.connect(path)
    market.ensure_schema(conn)
    conn.execute("INSERT INTO markets (name, region, namespace, kind, game, realms) VALUES "
                 "('us-testrealm', 'us', 'dynamic-us', 'realm', 'retail', '[\"Testrealm\"]')")
    conn.executemany("INSERT INTO items VALUES (?, ?, 'COMMON', 0)",
                     [(LINEN, "Linen Cloth"), (WOOL, "Wool Cloth"), (SILK, "Silk Cloth")])
    snaps = [
        [realm(1, LINEN, 20, 10), realm(2, LINEN, 5, 30), realm(3, WOOL, 10, 100), realm(5, SILK, 1, 500)],
        [realm(1, LINEN, 20, 10), realm(2, LINEN, 5, 30), realm(3, WOOL, 8, 100), realm(5, SILK, 1, 500)],
        [realm(1, LINEN, 15, 10), realm(2, LINEN, 5, 30), realm(3, WOOL, 6, 100), realm(5, SILK, 1, 500)],
        [realm(1, LINEN, 15, 10), realm(3, WOOL, 6, 100), realm(4, WOOL, 1, 20), realm(5, SILK, 1, 500)],
    ]
    for i, auctions in enumerate(snaps):
        store.store_snapshot(conn, "us-testrealm", {"auctions": auctions}, T0 + i * HOUR, fetched_at=T0 + i * HOUR)
    conn.close()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MarketDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        d = Path(cls.dir.name)
        cls.db = d / "market.sqlite"
        build_fixture(cls.db)
        cls.before = digest(cls.db)
        # Point the server at this test's own files (other test modules share the settings module).
        cls.patches = [mock.patch.object(settings, k, v) for k, v in
                       {"DATA": d, "MARKET_DB": cls.db, "SERVER_DB": d / "server.sqlite", "USERS_DIR": d / "users",
                        "CONFIG": d / "config.toml"}.items()]
        for p in cls.patches:
            p.start()
        db._schema_ready = False
        with db.server_conn() as conn:
            uid = conn.execute("INSERT INTO users (name, handle, is_admin, created) VALUES ('snow', x'01', 1, 0)"
                               ).lastrowid
        cls.key = auth.create_client_key(auth.User(uid, "snow", True), "assistant")
        cls.h = {"authorization": f"Bearer {cls.key}"}
        cls.api = TestClient(app)
        cls.conn = md.ro_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for p in cls.patches:
            p.stop()
        db._schema_ready = False
        cls.dir.cleanup()

    def get(self, path, **params):
        r = self.api.get(path, params=params, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_connection_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("DELETE FROM item_states")

    def test_list_markets(self):
        (m,) = md.list_markets(self.conn)["markets"]
        self.assertEqual((m["name"], m["kind"], m["region"], m["realms"], m["snapshots"]),
                         ("us-testrealm", "realm", "us", ["Testrealm"], 4))
        self.assertEqual((m["first_snapshot"], m["last_snapshot"]), (T0, T0 + 3 * HOUR))
        self.assertEqual(m["first_snapshot_iso"], "2026-10-15T01:00:00Z")
        self.assertEqual(m["items_listed"], 3)

    def test_search_items(self):
        r = md.search_items(self.conn, "cloth", limit=2)
        self.assertEqual((r["total"], r["next_offset"]), (3, 2))
        self.assertEqual(len(md.search_items(self.conn, "cloth", limit=2, offset=2)["items"]), 1)
        self.assertEqual(md.search_items(self.conn, "linen")["items"][0]["item_id"], LINEN)
        self.assertEqual(md.search_items(self.conn, str(WOOL))["items"][0]["name"], "Wool Cloth")
        self.assertEqual(md.search_items(self.conn, "999999")["items"], [{"item_id": 999999, "name": None,
                                                                          "quality": None}])

    def test_current_prices(self):
        r = md.current_prices(self.conn, "us-testrealm", [LINEN, "wool cloth", 999999])
        self.assertEqual(r["as_of"], T0 + 3 * HOUR)
        linen, wool, missing = r["items"]
        self.assertEqual((linen["min_price"], linen["quantity"], linen["auctions"], linen["changed"]),
                         (10, 15, 1, T0 + 3 * HOUR))
        self.assertEqual((wool["item_id"], wool["min_price"], wool["quantity"], wool["depth5"]), (WOOL, 20, 7, 1))
        self.assertEqual((missing["listed"], missing["quantity"], missing["min_price"]), (False, 0, None))

    def test_history_raw_carries_state_forward(self):
        r = md.price_history(self.conn, "us-testrealm", "Linen", since=T0, until=T0 + 3 * HOUR, resolution="raw")
        rows = r["rows"]
        self.assertEqual([x["time"] for x in rows], [T0 + i * HOUR for i in range(4)])
        self.assertEqual([x["quantity"] for x in rows], [25, 25, 20, 15])
        self.assertEqual([x["changed"] for x in rows], [True, False, True, True])  # T1 repeats T0's state
        self.assertEqual([(x["sold"], x["gone"]) for x in rows], [(0, 0), (0, 0), (5, 0), (0, 5)])
        self.assertEqual(rows[1]["median"], rows[0]["median"])
        # A window that starts between state rows picks up the earlier one.
        late = md.price_history(self.conn, "us-testrealm", LINEN, since=T0 + HOUR, until=T0 + HOUR, resolution="raw")
        self.assertEqual([(x["time"], x["quantity"], x["changed"]) for x in late["rows"]], [(T0 + HOUR, 25, False)])
        # Silk never changes after T0: every snapshot still has a row.
        silk = md.price_history(self.conn, "us-testrealm", SILK, since=T0, until=T0 + 9 * HOUR, resolution="raw")
        self.assertEqual([x["min_price"] for x in silk["rows"]], [500] * 4)

    def test_history_downsampling_and_pages(self):
        daily = md.price_history(self.conn, "us-testrealm", LINEN, since=T0 - HOUR, until=T0 + 5 * HOUR,
                                 resolution="daily")["rows"]
        self.assertEqual(len(daily), 1)
        d = daily[0]
        self.assertEqual((d["time_iso"], d["samples"], d["min_price"], d["sold"], d["gone"], d["close_quantity"]),
                         ("2026-10-15T00:00:00Z", 4, 10, 5, 5, 15))
        self.assertEqual(d["quantity"], round((25 + 25 + 20 + 15) / 4))
        hourly = md.price_history(self.conn, "us-testrealm", LINEN, since=T0, until=T0 + 3 * HOUR)
        self.assertEqual((hourly["resolution"], hourly["total"]), ("hourly", 4))
        page = md.price_history(self.conn, "us-testrealm", LINEN, since=T0, until=T0 + 3 * HOUR,
                                resolution="raw", limit=3)
        self.assertEqual((len(page["rows"]), page["next_offset"]), (3, 3))
        rest = md.price_history(self.conn, "us-testrealm", LINEN, since=T0, until=T0 + 3 * HOUR,
                                resolution="raw", limit=3, offset=3)
        self.assertEqual((rest["rows"][0]["time"], rest["next_offset"]), (T0 + 3 * HOUR, None))

    def test_values_and_deals(self):
        v = md.market_values(self.conn, "us-testrealm", [WOOL])
        (w,) = v["items"]
        self.assertEqual((w["current_min"], w["market_value"]), (20, 80))  # 3h at 100c, 1h at 20c
        self.assertAlmostEqual(w["sold_per_day"], 4 / (3 / 24))
        busiest = md.market_values(self.conn, "us-testrealm", limit=1)
        self.assertEqual((busiest["total"], busiest["items"][0]["item_id"], busiest["next_offset"]), (3, LINEN, 1))
        (deal,) = md.deals(self.conn, "us-testrealm")["deals"]
        self.assertEqual((deal["item_id"], deal["name"], deal["profit_per_unit"]), (WOOL, "Wool Cloth", 56))

    def test_http_routes(self):
        self.assertEqual(self.api.get("/api/v1/market/markets").status_code, 401)
        self.assertEqual(self.get("/api/v1/market/markets")["markets"][0]["name"], "us-testrealm")
        self.assertEqual(self.get("/api/v1/market/items", q="wool")["items"][0]["item_id"], WOOL)
        cur = self.get("/api/v1/market/current", market="us-testrealm", item=[f"{LINEN},{WOOL}", str(SILK)])
        self.assertEqual([i["item_id"] for i in cur["items"]], [LINEN, WOOL, SILK])
        hist = self.get("/api/v1/market/history", market="us-testrealm", item=LINEN, since=T0, resolution="raw")
        self.assertEqual(hist["total"], 4)
        self.assertEqual(self.get("/api/v1/market/values", market="us-testrealm", item=WOOL)["items"][0]["market_value"],
                         80)
        self.assertEqual(self.get("/api/v1/market/deals", market="us-testrealm")["total"], 1)
        self.assertEqual(self.get("/api/v1/market/crafts")["sets"], [])  # no recipes uploaded
        r = self.api.get("/api/v1/market/current", params={"market": "nope", "item": LINEN}, headers=self.h)
        self.assertEqual(r.status_code, 404)
        self.assertIn("us-testrealm", r.json()["error"])
        r = self.api.get("/api/v1/market/history", headers=self.h,
                         params={"market": "us-testrealm", "item": LINEN, "since": 0, "until": T0})
        self.assertEqual(r.status_code, 400)  # window too long

    def rpc(self, method, params=None, mid=1, headers=None):
        msg = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}),
               **({"params": params} if params is not None else {})}
        return self.api.post("/api/v1/mcp", json=msg, headers=headers or {
            **self.h, "accept": "application/json, text/event-stream"})

    def test_mcp(self):
        self.assertEqual(self.api.post("/api/v1/mcp", json={}).status_code, 401)
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        self.assertEqual((init["protocolVersion"], init["serverInfo"]["name"]), ("2025-06-18", "goldmine"))
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(self.rpc("notifications/initialized", mid=None).status_code, 202)
        tools = {t["name"]: t for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual(set(tools), {"list_markets", "search_items", "get_current_prices", "get_price_history",
                                      "get_market_values", "get_deals", "get_crafts", "get_account", "get_inventory",
                                      "get_owned_auctions", "get_ledger", "get_recipes", "get_craft_results",
                                      "get_token_price", "get_item_info", "get_auction_mail",
                                      "get_crafting_orders"})
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools.values()))
        res = self.rpc("tools/call", {"name": "get_price_history", "arguments": {
            "market": "us-testrealm", "item": "Linen Cloth", "since": T0, "resolution": "raw"}}).json()["result"]
        self.assertNotIn("isError", res)
        self.assertEqual([x["quantity"] for x in res["structuredContent"]["rows"]], [25, 25, 20, 15])
        self.assertIn('"quantity":25', res["content"][0]["text"])
        res = self.rpc("tools/call", {"name": "get_current_prices",
                                      "arguments": {"market": "us-testrealm", "items": [WOOL]}}).json()["result"]
        self.assertEqual(res["structuredContent"]["items"][0]["min_price"], 20)
        err = self.rpc("tools/call", {"name": "get_deals", "arguments": {"market": "nope"}}).json()["result"]
        self.assertTrue(err["isError"])
        err = self.rpc("tools/call", {"name": "list_markets", "arguments": {"bogus": 1}}).json()["result"]
        self.assertTrue(err["isError"])
        self.assertEqual(self.rpc("tools/call", {"name": "drop_tables"}).json()["error"]["code"], -32602)
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)
        batch = self.api.post("/api/v1/mcp", headers=self.h, json=[
            {"jsonrpc": "2.0", "id": 7, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(batch.json(), [{"jsonrpc": "2.0", "id": 7, "result": {}}])
        self.assertEqual(self.api.get("/api/v1/mcp", headers=self.h).status_code, 405)

    def test_zz_nothing_was_written(self):
        self.assertEqual(digest(self.db), self.before)


if __name__ == "__main__":
    unittest.main()
