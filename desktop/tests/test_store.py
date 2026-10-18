import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import goldmine  # noqa: E402
import market  # noqa: E402
import store  # noqa: E402

HOUR = 3600
DAY = 86400
T0 = 1_790_000_000 - 1_790_000_000 % DAY + 2 * HOUR  # 02:00 UTC


def commodity(aid, item, qty, unit, tl="VERY_LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "unit_price": unit, "time_left": tl}


def gear(aid, item, buyout, bonus=None, tl="VERY_LONG"):
    a = {"id": aid, "item": {"id": item}, "quantity": 1, "buyout": buyout, "time_left": tl}
    if bonus:
        a["item"]["bonus_lists"] = bonus
    return a


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = goldmine.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def put(self, auctions, t, name="m"):
        return store.store_snapshot(self.conn, name, {"auctions": auctions}, t)

    def q(self, sql, *params):
        return self.conn.execute(sql, params).fetchall()

    def test_expiring_codes(self):
        self.assertEqual(store.expiring_codes(20 * 60), {1})
        self.assertEqual(store.expiring_codes(HOUR), {1, 2})
        self.assertEqual(store.expiring_codes(5 * HOUR), {1, 2, 3})
        self.assertIsNone(store.expiring_codes(30 * HOUR))

    def test_summarize(self):
        listings = [store.parse_listing(a, store.BonusKeys(self.conn)) for a in [
            commodity(1, 10, 1, 100), commodity(2, 10, 9, 104), commodity(3, 10, 90, 200),
            {"id": 4, "item": {"id": 11}, "quantity": 1, "bid": 50, "time_left": "LONG"},
        ]]
        s = store.summarize(listings)
        # min, p10, median, quantity, auctions, depth5, depth10, depth25
        self.assertEqual(s[(10, 0)], (100, 104, 200, 100, 3, 10, 10, 10))
        self.assertEqual(s[(11, 0)], (None, None, None, 1, 1, None, None, None))

    def test_unchanged_items_write_nothing(self):
        auctions = [commodity(1, 10, 5, 100), gear(2, 20, 5000)]
        r1 = self.put(auctions, T0)
        r2 = self.put(auctions, T0 + HOUR)
        self.assertTrue(r1["keyframe"])
        self.assertEqual((r2["states"], r2["new"], r2["ended"], r2["keyframe"]), (0, 0, 0, False))
        self.assertEqual(self.q("SELECT COUNT(*) FROM item_states")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM auctions")[0][0], 2)

    def test_lifecycle_and_sales_signals(self):
        self.put([commodity(1, 10, 50, 100), commodity(2, 10, 5, 120, tl="SHORT"), gear(3, 20, 9000)], T0)
        # 1: 20 bought (partial). 2: SHORT, vanished -> may have expired. 3: vanished early -> gone.
        r = self.put([commodity(1, 10, 30, 100)], T0 + HOUR)
        self.assertEqual((r["partial"], r["ended"]), (1, 2))
        outcomes = dict(self.q("SELECT auction_id, outcome FROM auctions WHERE ended_snap IS NOT NULL"))
        self.assertEqual(outcomes, {2: store.OUTCOME_MAY_EXPIRE, 3: store.OUTCOME_GONE_EARLY})
        st = {r[0]: r[1:] for r in self.q(
            "SELECT item_id, quantity, sold, gone FROM item_states WHERE snap = (SELECT MAX(id) FROM snapshots)")}
        self.assertEqual(st[10], (30, 20, 0))
        self.assertEqual(st[20], (0, 0, 1))  # item 20 left the AH: recorded with quantity 0
        self.assertEqual(self.q("SELECT item_id FROM item_current"), [(10,)])
        self.assertEqual(self.q("SELECT quantity FROM auction_changes"), [(30,)])

    def test_long_gap_outcome_unknown(self):
        self.put([gear(3, 20, 9000)], T0)
        self.put([], T0 + 30 * HOUR)
        self.assertEqual(self.q("SELECT outcome FROM auctions"), [(store.OUTCOME_UNKNOWN,)])
        self.assertEqual(self.q("SELECT SUM(gone) FROM item_states")[0][0], 0)

    def test_daily_keyframe(self):
        self.put([commodity(1, 10, 5, 100), commodity(2, 11, 5, 100)], T0)
        self.put([commodity(1, 10, 5, 100), commodity(2, 11, 5, 100)], T0 + HOUR)
        r = self.put([commodity(1, 10, 5, 100), commodity(2, 11, 5, 100)], T0 + DAY)
        self.assertTrue(r["keyframe"])
        self.assertEqual(r["states"], 2)  # every item written, though nothing changed
        self.assertEqual(store.window_start_snapshot(self.conn, "m", T0 + DAY + 5 * HOUR),
                         self.q("SELECT MAX(id) FROM snapshots")[0][0])

    def test_reconstruct_any_snapshot(self):
        snaps = [
            [commodity(1, 10, 50, 100), gear(2, 20, 9000)],
            [commodity(1, 10, 40, 100), gear(2, 20, 9000), gear(3, 20, 8000)],
            [commodity(1, 10, 25, 100), gear(3, 20, 8000)],
        ]
        ids = []
        for i, s in enumerate(snaps):
            self.put(s, T0 + i * HOUR)
            ids.append(self.q("SELECT MAX(id) FROM snapshots")[0][0])
        for sid, s in zip(ids, snaps):
            got = [(a, i, q) for a, i, _, _, q in store.listings_at(self.conn, "m", sid)]
            want = sorted((a["id"], a["item"]["id"], a["quantity"]) for a in s)
            self.assertEqual(got, want)

    def test_bonus_variants_dictionary_encoded(self):
        self.put([gear(1, 20, 100, [6652, 1472]), gear(2, 20, 200, [1472, 6652]), gear(3, 20, 300, [99])], T0)
        self.assertEqual(self.q("SELECT key FROM bonus_keys ORDER BY id"), [("1472,6652",), ("99",)])
        self.assertEqual(self.q("SELECT bonus FROM auctions ORDER BY auction_id"), [(1,), (1,), (2,)])

    def test_same_snapshot_twice_is_skipped(self):
        self.put([commodity(1, 10, 5, 100)], T0)
        self.assertTrue(self.put([commodity(1, 10, 5, 100)], T0)["skipped"])

    def test_migrates_hourly_layout(self):
        path = os.path.join(self.tmp.name, "old.sqlite")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE markets (name TEXT PRIMARY KEY, region TEXT NOT NULL, namespace TEXT NOT NULL,
                kind TEXT NOT NULL, connected_realm_id INTEGER, auction_house_id INTEGER, game TEXT, realms TEXT);
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL,
                fetched_at INTEGER NOT NULL, last_modified INTEGER, auctions INTEGER NOT NULL,
                UNIQUE (market, last_modified));
            CREATE TABLE prices (snapshot_id INTEGER, item_id INTEGER);
            CREATE TABLE last_listings (market TEXT);
            INSERT INTO markets (name, region, namespace, kind) VALUES ('m', 'us', 'dynamic-us', 'commodities');
            INSERT INTO snapshots (market, fetched_at, last_modified, auctions) VALUES ('m', 1, 1, 1);
        """)
        conn.close()
        conn = goldmine.connect(path)
        market.ensure_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertFalse(tables & {"prices", "last_listings"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        store.store_snapshot(conn, "m", {"auctions": [commodity(1, 10, 5, 100)]}, T0)
        self.assertEqual(conn.execute("SELECT mid FROM markets WHERE name = 'm'").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
