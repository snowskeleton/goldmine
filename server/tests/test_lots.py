"""Ledger FIFO lots, the scorecard, and the journal."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import journal, ledger, lots, market, store  # noqa: E402

HOUR, DAY = 3600, 86400
T0 = 1792022400
ORE = 210930
CHAR = "Snow-Proudmoore"


class LotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))
        market.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO items VALUES (?, 'Bismuth', 'COMMON', 0)", (ORE,))
        self.seq = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def tx(self, ts, delta, source, meta=None, item_id=None, item_name=None, char=CHAR):
        self.seq += 1
        tid = f"{char}:{self.seq}"
        self.conn.execute("INSERT INTO transactions (id, char_key, seq, ts, delta, balance, source, item_id, item_name, "
                          "meta) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                          (tid, char, self.seq, ts, delta, source, item_id, item_name, json.dumps(meta) if meta else None))
        return tid

    def sale(self, ts, price, cut, deposit, qty=None):
        meta = {"item": "Bismuth", "price": price, "cut": cut, "deposit": deposit, "buyer": "x"}
        if qty:
            meta["qty"] = qty
        return self.tx(ts, price + deposit - cut, "auction_sale", meta, item_name="Bismuth")

    def test_fifo_lot_split_across_sales(self):
        first = self.tx(T0, -1000, "auction_buy", {"item_id": ORE, "qty": 10}, item_id=ORE)
        self.tx(T0 + DAY, -2000, "auction_buy", {"item_id": ORE, "qty": 10}, item_id=ORE)
        self.tx(T0 + DAY + 60, -30, "auction_deposit")
        self.sale(T0 + 2 * DAY, 4500, 225, 30, qty=15)   # 10 @ 100 + 5 @ 200 -> cost 2000, net 4275
        self.tx(T0 + 2 * DAY + 60, -20, "auction_deposit")
        self.tx(T0 + 2 * DAY + 120, -20, "auction_cancel")
        self.sale(T0 + 3 * DAY, 1500, 75, 0, qty=5)      # 5 @ 200 -> cost 1000, net 1425
        self.tx(T0 + 3 * DAY, -500, "auction_buy")       # an item auction buyout: no item recorded
        self.conn.commit()
        tags = {first: "flip", str(ORE): {"strategy": "deals", "max_hold_days": 2}}

        r = lots.get_lots(self.conn, tags=tags)
        self.assertEqual(r["open_lots"], [])
        [item] = r["realized_by_item"]
        self.assertEqual((item["units"], item["proceeds"], item["cost"], item["realized"]), (20, 5700, 3000, 2700))
        self.assertEqual(item["name"], "Bismuth")
        self.assertEqual(r["fees"], {"deposits_paid": 50, "deposits_refunded": 30, "net_deposit_loss": 20,
                                     "cancel_fees": 20, "cuts": 300})
        self.assertEqual(r["unattributed_purchases"], {"count": 1, "spent": 500})

        sc = lots.get_scorecard(self.conn, tags=tags)
        by = {s["strategy"]: s for s in sc["strategies"]}
        self.assertEqual((by["flip"]["realized"], by["flip"]["cost_of_sold"], by["flip"]["units_sold"]), (1850, 1000, 10))
        self.assertEqual(by["flip"]["avg_days_held"], 2.0)
        self.assertEqual((by["deals"]["realized"], by["deals"]["units_sold"], by["deals"]["units_bought"]), (850, 10, 10))
        self.assertEqual(by["deals"]["roi"], round(850 / 2000, 4))
        self.assertEqual(by["deals"]["sell_through"], 1.0)
        self.assertEqual(sc["realized_total"], 2700)
        self.assertEqual(sc["realized_after_fees"], 2700 - 20 - 20)
        # Weekly AH cash flow: every AH delta summed.
        self.assertEqual(sum(w["auction_cash_flow"] for w in sc["weekly"]),
                         -1000 - 2000 - 30 + 4305 - 20 - 20 + 1425 - 500)

    def test_open_lots_at_cost_and_conservative_value(self):
        # Market: 10 bought per hour at 300 for a day; we hold 10 bought at 100.
        for h in range(24):
            store.store_snapshot(self.conn, "us-commodities", {"auctions": [
                {"id": 1, "item": {"id": ORE}, "quantity": 10000 - 10 * h, "unit_price": 300, "time_left": "VERY_LONG"}]},
                T0 + h * HOUR)
        self.tx(T0 - 10 * DAY, -1000, "auction_buy", {"item_id": ORE, "qty": 10}, item_id=ORE)
        self.conn.commit()
        r = lots.get_lots(self.conn, max_hold_days=7)
        [lot] = r["open_lots"]
        self.assertEqual((lot["qty"], lot["cost"], lot["unit_cost"]), (10, 1000, 100))
        self.assertTrue(lot["over_max_hold"])
        dep = 300 * 0.005 * 2   # fallback deposit, 24h
        self.assertEqual(lot["unit_value"], int(300 * 0.95 - dep) // 2)
        self.assertEqual(r["open_conservative_value"], 10 * (int(300 * 0.95 - dep) // 2))

    def test_sale_without_qty_is_estimated_from_the_market(self):
        store.store_snapshot(self.conn, "us-commodities", {"auctions": [
            {"id": 1, "item": {"id": ORE}, "quantity": 50, "unit_price": 250, "time_left": "LONG"}]}, T0)
        self.tx(T0 - DAY, -1000, "auction_buy", {"item_id": ORE, "qty": 10}, item_id=ORE)
        self.sale(T0 + 60, 1000, 50, 0)   # 1000 / 250 -> 4 units
        self.conn.commit()
        r = lots.get_lots(self.conn)
        self.assertEqual(r["estimated_qty_sales"], 1)
        self.assertEqual(r["realized_by_item"][0]["units"], 4)
        self.assertEqual(r["open_lots"][0]["qty"], 6)

    def test_realm_filter(self):
        self.tx(T0, -1000, "auction_buy", {"item_id": ORE, "qty": 10}, item_id=ORE, char="Alt-Stormrage")
        self.conn.commit()
        self.assertEqual(lots.get_lots(self.conn, realm="Proudmoore")["open_lots"], [])
        self.assertEqual(len(lots.get_lots(self.conn, realm="Stormrage")["open_lots"]), 1)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = ledger.connect(os.path.join(self.tmp.name, "gm.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_write_read_supersede_expire(self):
        self.assertEqual(journal.read(self.conn)["entries"], [])   # no table yet
        h = journal.write(self.conn, "hypothesis", "Bismuth dips Tuesday nights", strategy="flip", items=[ORE],
                          prediction={"realized": 500000}, evidence=["bt:bt-abc123"], now=T0)["entry"]
        self.assertEqual((h["kind"], h["items"], h["prediction"]), ("hypothesis", [ORE], {"realized": 500000}))
        lesson = journal.write(self.conn, "lesson", "It dips Wednesday, not Tuesday", strategy="flip",
                               evidence=["task:p2026-10-20a:t1", "tx:Snow-Proudmoore:12"], supersedes=h["id"],
                               expires_at=T0 + 7 * DAY, now=T0 + DAY)
        self.assertEqual(lesson["superseded"], h["id"])
        live = journal.read(self.conn, now=T0 + 2 * DAY)["entries"]
        self.assertEqual([e["kind"] for e in live], ["lesson"])
        everything = journal.read(self.conn, include_superseded=True, now=T0 + 2 * DAY)["entries"]
        self.assertEqual(everything[1]["superseded_by"], lesson["entry"]["id"])
        self.assertEqual(journal.read(self.conn, now=T0 + 8 * DAY)["entries"], [])   # the lesson expired
        self.assertEqual(len(journal.read(self.conn, item=ORE, include_superseded=True)["entries"]), 1)
        self.assertEqual(journal.read(self.conn, kind="note")["total"], 0)
        with self.assertRaises(ValueError):
            journal.write(self.conn, "rumor", "x")
        with self.assertRaises(ValueError):
            journal.write(self.conn, "note", "x", evidence=["http://example.com"])
        with self.assertRaises(ValueError):
            journal.write(self.conn, "note", "x", supersedes=h["id"])   # already superseded

    def test_prediction_error_in_scorecard(self):
        market.ensure_schema(self.conn)
        journal.write(self.conn, "hypothesis", "flips make 1000", strategy="flip", prediction={"realized": 1000},
                      now=T0)
        for i, (ts, delta, src, meta) in enumerate([
                (T0 + 10, -1000, "auction_buy", {"item_id": ORE, "qty": 10}),
                (T0 + DAY, 1900, "auction_sale", {"item": "x", "price": 2000, "cut": 100, "deposit": 0, "qty": 10,
                                                  "item_id": ORE})]):
            self.conn.execute("INSERT INTO transactions (id, char_key, seq, ts, delta, balance, source, item_id, meta) "
                              "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)", (f"{CHAR}:{i}", CHAR, i, ts, delta, src, ORE,
                                                                     json.dumps(meta)))
        self.conn.commit()
        [p] = lots.get_scorecard(self.conn, tags={str(ORE): "flip"})["predictions"]
        self.assertEqual((p["predicted"], p["actual"], p["error"]), (1000, 900, -100))


if __name__ == "__main__":
    unittest.main()
