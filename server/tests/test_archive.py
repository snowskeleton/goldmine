import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import archive, ledger, market, pricing, sim, store  # noqa: E402

HOUR = 3600
DAY = 86400
T0 = 1_790_000_000 - 1_790_000_000 % (7 * DAY) + 2 * HOUR  # 02:00 UTC on the first day of a week


def commodity(aid, item, qty, unit, tl="VERY_LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty, "unit_price": unit, "time_left": tl}


class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        cols = [[1, 2, 3], [None, 0, -5], [2 ** 40, None, -(2 ** 40)], [], [7] * 1000]
        self.assertEqual(archive.decode(archive.encode(cols)), cols)

    def test_listing_changes_round_trip(self):
        rows = [(5, 0, 0, 100, None, 20, 15, 4, 3, 10, 12, 1, ((11, 18), (12, 15))),
                (9, 0, 3, None, 50, 1, 1, 4, 4, 10, 13, 2, ())]
        self.assertEqual(archive.decode_auctions(archive.encode_auctions(rows)), rows)


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)
        self.mid = store.market_id(self.conn, "us-commodities")
        # Three days: listings partly bought, vanishing early, replaced, and prices moving.
        aid = 100
        for h in range(0, 3 * 24 - 2, 2):  # D0 02:00 to D2 22:00
            listings = []
            for item in (10, 11, 12):
                for k in range(3):
                    if (h // 2 + k + item) % 5 == 0:
                        continue  # some listings vanish and come back as new ones
                    listings.append(commodity(aid + item * 10 + k, item, max(1, 40 - h // 2 - k),
                                              100 + item + k * 7 + h % 5))
            if h % 12 == 0:
                aid += 1000  # a fresh batch now and then
            store.store_snapshot(self.conn, "us-commodities", {"auctions": listings}, T0 + h * HOUR)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def everything(self):
        snaps = [s for s, *_ in store.snapshot_times(self.conn, "us-commodities")]
        h = sim.History(self.conn, markets=("us-commodities",), since=T0, items=[10, 11, 12], gone_weight=0.5)
        return {
            "states": archive.states(self.conn, self.mid),
            "item": archive.states(self.conn, self.mid, [11], 0, snaps[5], snaps[20]),
            "last": [archive.last_state(self.conn, self.mid, 12, 0, s) for s in snaps],
            "priced": [archive.last_state(self.conn, self.mid, 12, None, s, priced=True) for s in snaps],
            "listings": [store.listings_at(self.conn, "us-commodities", s) for s in snaps],
            "events": [h.events("us-commodities", i) for i in (10, 11, 12)],
            "book": [h.listings("us-commodities", 11, s) for s in snaps],
            "stats": pricing.market_stats(self.conn, "us-commodities", T0 + 6 * HOUR),
        }

    def hot(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_pack_loses_nothing(self):
        before = self.everything()
        archive.pack_due(self.conn, now=T0 + 2 * DAY + HOUR, log=lambda _: None)
        self.assertEqual(self.everything(), before)
        # Only the last day is left in the hot tables.
        day3 = self.conn.execute("SELECT MIN(id) FROM snapshots WHERE last_modified >= ?",
                                 (T0 - 2 * HOUR + 2 * DAY,)).fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM item_states WHERE snap < ?", (day3,)).fetchone()[0], 0)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM state_chunks").fetchone()[0], 0)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM auction_chunks").fetchone()[0], 0)

    def test_daily_packs_merge_into_the_week(self):
        before = self.everything()
        states, listings = self.hot("item_states"), self.hot("auctions")
        for day in (1, 2, 3):
            archive.pack_due(self.conn, now=T0 - 2 * HOUR + day * DAY + HOUR, log=lambda _: None)
        self.assertEqual(self.everything(), before)
        self.assertEqual(self.hot("item_states"), 0)
        self.assertLess(self.hot("auctions"), listings)  # open listings stay hot
        self.assertEqual(self.conn.execute("SELECT SUM(row_count) FROM state_chunks").fetchone()[0], states)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM state_chunks").fetchone()[0], 3)  # one per item

    def test_pack_due_runs_once_a_day(self):
        logs = []
        archive.pack_due(self.conn, now=T0 + DAY, log=logs.append)
        archive.pack_due(self.conn, now=T0 + DAY + HOUR, log=logs.append)
        self.assertEqual(len(logs), 1)

    def test_collection_continues_after_pack(self):
        archive.pack_due(self.conn, now=T0 + 3 * DAY, log=lambda _: None)
        open_ids = {a[0] for a in store.listings_at(self.conn, "us-commodities",
                                                     self.conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0])}
        r = store.store_snapshot(self.conn, "us-commodities", {"auctions": []}, T0 + 3 * DAY)
        self.assertEqual(r["ended"], len(open_ids))  # still-open listings were kept hot and end normally


if __name__ == "__main__":
    unittest.main()
