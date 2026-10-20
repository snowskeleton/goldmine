"""Portfolio risk rules (risk.py) on the fixture market DB plus the addon's sample SavedVariables."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("GOLDMINE_DATA", TMP.name)
os.environ["GOLDMINE_COLLECTOR"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import account, db, ledger, marketdata as md, risk, settings, tasks  # noqa: E402
from test_account import FIXTURE  # noqa: E402
from test_marketdata import LINEN, build_fixture  # noqa: E402

NOW = 1760000000
REALM = "Testrealm"


def plan(*buys, paper=False):
    return {"plan": "p1", "created": NOW, "expires": NOW + 3600, "paper": paper, "budget_min": 20,
            "tasks": [{"id": f"b{i}", "kind": "buy", "est_min": 1, **b} for i, b in enumerate(buys)]}


def token(price):
    """Pretend the latest WoW token costs `price` copper."""
    return mock.patch.object(account, "token_price", lambda *a, **k: {"latest": {"price": price}})


class RiskTests(unittest.TestCase):
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
        db._schema_ready = False
        with db.server_conn() as conn:
            cls.uid = conn.execute("INSERT INTO users (name, handle, is_admin, created) VALUES ('snow', x'01', 1, 0)"
                                   ).lastrowid
        conn = db.user_conn(cls.uid)
        ledger.import_file(conn, FIXTURE)
        conn.close()
        cls.conn = md.ro_conn(cls.uid)
        cls.gold = account.account(cls.conn, REALM)["total_gold"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for p in cls.patches:
            p.stop()
        db._schema_ready = False
        cls.dir.cleanup()

    def validate(self, p):
        return risk.validate(self.conn, p, REALM)

    def test_status_in_tokens(self):
        with token(self.gold // 4):
            s = risk.status(self.conn, REALM)
        self.assertEqual((s["gold"], s["token_price"], s["raw_floor"], s["net_floor"]),
                         (self.gold, self.gold // 4, self.gold // 4, self.gold // 2))
        self.assertAlmostEqual(s["gold_tokens"], 4, places=2)
        self.assertEqual(s["working_capital"], self.gold - self.gold // 4)
        self.assertEqual(s["per_item_cap"], round(0.10 * s["working_capital"]))
        self.assertGreaterEqual(s["net_worth"], s["gold"])
        self.assertTrue(s["buys_allowed"])
        self.assertEqual((s["experiment_loss"], s["trader_since"]), (0, None))  # no real plan published yet

    def test_paper_and_buyless_plans_pass(self):
        big = {"item": LINEN, "qty": 10**6, "max_unit": 10**6}
        self.assertEqual(self.validate(plan(big, paper=True)), [])
        self.assertEqual(self.validate({**plan(), "tasks": [{"id": "s", "kind": "scan", "item": LINEN, "est_min": 1}]}), [])

    def test_no_token_price(self):
        with token(None):
            errors = self.validate(plan({"item": LINEN, "qty": 1, "max_unit": 1, "sell": 2, "hold_days": 3}))
        self.assertTrue(any("token price" in e for e in errors), errors)

    def test_buy_needs_an_exit(self):
        with token(1):
            errors = self.validate(plan({"item": LINEN, "qty": 1, "max_unit": 1}))
        self.assertTrue(any("needs an exit (sell, hold_days)" in e for e in errors), errors)

    def test_floors(self):
        buy = {"item": LINEN, "qty": 1, "max_unit": 1, "sell": 2, "hold_days": 3}
        with token(self.gold):  # all gold is the raw floor: any buy breaks it
            errors = self.validate(plan(buy))
        self.assertTrue(any("raw floor" in e for e in errors), errors)
        with token(self.gold * 10):
            errors = self.validate(plan(buy))
        self.assertTrue(any("net worth" in e for e in errors), errors)

    def test_caps(self):
        wc = self.gold - 1
        over_item = {"item": LINEN, "qty": 1, "max_unit": wc // 5, "sell": wc, "hold_days": 3}
        with token(1), mock.patch.object(risk, "_median_daily_sales", lambda *a: 10.0**9):
            errors = self.validate(plan(over_item))
            self.assertTrue(any("per-item cap" in e for e in errors), errors)
            self.assertFalse(any("% of working capital" in e for e in errors), errors)
            spread = [{"item": i, "qty": 1, "max_unit": wc // 10, "sell": wc, "hold_days": 3} for i in range(1, 6)]
            errors = self.validate(plan(*spread))
            self.assertTrue(any("40% of working capital" in e for e in errors), errors)
            ok = {"item": LINEN, "qty": 1, "max_unit": 1, "sell": 2, "hold_days": 3}
            self.assertEqual(self.validate(plan(ok)), [])

    def test_days_of_sales(self):
        with token(1):
            errors = self.validate(plan({"item": LINEN, "qty": 10**6, "max_unit": 1, "sell": 2, "hold_days": 3}))
        self.assertTrue(any("days of its median sales" in e for e in errors), errors)

    def test_publish_hook_runs_the_rules(self):
        with token(1):
            errors = tasks.validate_risk(self.conn, plan({"item": LINEN, "qty": 1, "max_unit": 1}))
        self.assertTrue(any("needs an exit" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
