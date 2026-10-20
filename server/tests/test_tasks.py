"""Task queue: plan validation, the active plan, Tasks.lua, results import, and the HTTP/MCP surface.
Round trip: publish_plan -> /api/v1/addon-tasks (Tasks.lua) -> SavedVariables upload -> get_task_results."""

import copy
import gzip
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("GOLDMINE_DATA", TMP.name)
os.environ["GOLDMINE_COLLECTOR"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from goldmine_server import auth, db, ledger, settings, svparse, tasks  # noqa: E402
from goldmine_server.app import app  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = 1760000000  # the addon test harness's clock (time() = 1760000000 + seconds played)


def sample_plan(**over) -> dict:
    """The docs/trader.md example, with an est_min on every task."""
    plan = {
        "plan": "p2026-10-20a", "created": NOW, "expires": NOW + 90000, "paper": False, "budget_min": 20,
        "tasks": [
            {"id": "t1", "kind": "buy", "item": 210930, "qty": 200, "max_unit": 412000,
             "why": "craft input for t3", "est_min": 2, "ev": 0},
            {"id": "t2", "kind": "post", "item": 211879, "qty": 20, "floor": 950000, "ceil": 1400000,
             "undercut": 1, "duration": 24, "est_min": 2, "ev": 180000},
            {"id": "t3", "kind": "craft", "recipe": 430102, "qty": 20, "est_min": 4, "after": "t1"},
            {"id": "t4", "kind": "cancel", "item": 211879, "below": 900000, "est_min": 1},
            {"id": "t5", "kind": "scan", "item": 222731, "est_min": 0.5},
            {"id": "t6", "kind": "mail", "item": 210930, "qty": 50, "to": "Altname", "est_min": 1},
            {"id": "t7", "kind": "vendor", "item": 12345, "est_min": 0.5},
        ],
    }
    plan.update(over)
    return plan


def mem_conn():
    return ledger.connect(":memory:")


class ValidationTests(unittest.TestCase):
    def errors(self, plan):
        return tasks.validate(plan, NOW)

    def test_sample_is_valid(self):
        self.assertEqual(self.errors(sample_plan()), [])

    def test_plan_level_errors(self):
        self.assertIn("expires is in the past", self.errors(sample_plan(expires=NOW - 1)))
        self.assertIn("tasks must be a non-empty list", self.errors(sample_plan(tasks=[])))
        self.assertIn("unknown plan field market", self.errors(sample_plan(market="x")))
        self.assertIn("paper must be true or false", self.errors(sample_plan(paper=1)))
        self.assertTrue(any("budget_min" in e for e in self.errors(sample_plan(budget_min=0))))
        self.assertTrue(any("over budget_min 10" in e for e in self.errors(sample_plan(budget_min=10))))
        self.assertTrue(any("plan (the id)" in e for e in self.errors(sample_plan(plan="bad id!"))))

    def bad_task(self, i, **change):
        plan = sample_plan()
        t = plan["tasks"][i]
        for k, v in change.items():
            if v is None:
                t.pop(k)
            else:
                t[k] = v
        return self.errors(plan)

    def test_task_errors(self):
        self.assertTrue(any("kind must be one of" in e for e in self.bad_task(0, kind="sell")))
        self.assertIn("task 't1': missing max_unit", self.bad_task(0, max_unit=None))
        self.assertIn("task 't1': missing est_min", self.bad_task(0, est_min=None))
        # Copper must be ints: no gold floats, no strings, no bools.
        for v in (41.2, "412000", True, 0, -5):
            self.assertTrue(any("max_unit must be a positive integer" in e for e in self.bad_task(0, max_unit=v)), v)
        self.assertIn("task 't2': floor is above ceil", self.bad_task(1, floor=2000000))
        self.assertTrue(any("duration must be" in e for e in self.bad_task(1, duration=36)))
        self.assertTrue(any("undercut" in e for e in self.bad_task(1, undercut=-1)))
        self.assertTrue(any("unknown field max_unit" in e for e in self.bad_task(1, max_unit=5)))
        self.assertTrue(any("cancel needs below" in e for e in self.bad_task(3, below=None)))
        self.assertTrue(any("after must name an earlier" in e for e in self.bad_task(2, after="t9")))
        self.assertTrue(any("duplicate id" in e for e in self.bad_task(1, id="t1")))
        self.assertTrue(any("control characters" in e for e in self.bad_task(5, to="Alt\nname")))
        self.assertTrue(any("ev must be an integer" in e for e in self.bad_task(0, ev=1.5)))

    def test_normalize_fills_post_defaults(self):
        plan = sample_plan()
        del plan["tasks"][1]["undercut"], plan["tasks"][1]["duration"]
        post = tasks.normalize(plan, NOW)["tasks"][1]
        self.assertEqual((post["undercut"], post["duration"]), (1, 24))
        self.assertEqual(list(post)[:3], ["id", "kind", "item"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = mem_conn()

    def test_publish_supersedes_and_expires(self):
        out = tasks.publish(self.conn, sample_plan(), NOW)
        self.assertEqual((out["plan"], out["tasks"], out["est_min"]), ("p2026-10-20a", 7, 11))
        self.assertEqual(tasks.active_plan(self.conn, NOW)["plan"], "p2026-10-20a")
        with self.assertRaises(tasks.PlanRejected) as cm:
            tasks.publish(self.conn, sample_plan(), NOW)
        self.assertIn("already exists", str(cm.exception))
        tasks.publish(self.conn, sample_plan(plan="p2026-10-20b", expires=NOW + 100), NOW + 10)
        self.assertEqual(tasks.active_plan(self.conn, NOW + 20)["plan"], "p2026-10-20b")
        old = tasks.get_plan(self.conn, "p2026-10-20a", NOW + 20)
        self.assertEqual((old["active"], old["superseded_by"]), (False, "p2026-10-20b"))
        # Once the newest plan expires there is no active plan; the older one doesn't come back.
        self.assertIsNone(tasks.active_plan(self.conn, NOW + 200))
        self.assertIsNone(tasks.get_plan(self.conn, now=NOW + 200)["plan"])
        self.assertIn("tasks = {},", tasks.addon_tasks(self.conn, NOW + 200))

    def test_rejected_plan_is_not_stored(self):
        with self.assertRaises(tasks.PlanRejected) as cm:
            tasks.publish(self.conn, sample_plan(budget_min=5), NOW)
        self.assertTrue(cm.exception.errors)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)

    def test_risk_hook_is_consulted(self):
        with mock.patch.object(tasks, "validate_risk", return_value=["raw gold below 1 token"]):
            with self.assertRaises(tasks.PlanRejected) as cm:
                tasks.publish(self.conn, sample_plan(), NOW)
        self.assertEqual(cm.exception.errors, ["raw gold below 1 token"])
        self.assertEqual(tasks.validate_risk(self.conn, sample_plan()), [])

    def test_lua_round_trip(self):
        plan = tasks.normalize(sample_plan(), NOW)
        parsed = svparse.parse(tasks.render_lua(plan, NOW))["GoldmineTasks"]
        self.assertEqual(parsed["generated"], NOW)
        self.assertEqual({k: parsed[k] for k in plan if k != "tasks"}, {k: v for k, v in plan.items() if k != "tasks"})
        self.assertEqual(parsed["tasks"], plan["tasks"])
        empty = svparse.parse(tasks.render_lua(None, NOW))["GoldmineTasks"]
        self.assertEqual(empty, {"generated": NOW, "tasks": {}})  # an empty Lua table parses as {}
        quoted = sample_plan()
        quoted["tasks"][0]["why"] = 'say "hi" \\ bye'
        parsed = svparse.parse(tasks.render_lua(tasks.normalize(quoted, NOW), NOW))["GoldmineTasks"]
        self.assertEqual(parsed["tasks"][0]["why"], 'say "hi" \\ bye')

    def test_addon_fixture_is_current(self):
        """fixtures/Tasks.lua is what the addon test harness loads; keep it in step with render_lua."""
        expected = tasks.render_lua(tasks.normalize(sample_plan(), NOW), NOW)
        path = FIXTURES / "Tasks.lua"
        if os.environ.get("REGENERATE_FIXTURES"):
            path.write_text(expected)
        self.assertEqual(path.read_text(), expected, "run with REGENERATE_FIXTURES=1 to update")

    def test_import_results_idempotent(self):
        tasks.publish(self.conn, sample_plan(), NOW)
        r1 = {"id": "p2026-10-20a:t1", "plan": "p2026-10-20a", "task": "t1", "c": "Snow-Proudmoore", "t": NOW + 5,
              "status": "partial", "qty": 120, "seen": 409000, "paid": 49080000}
        data = {"taskResults": [r1, {"id": "p2026-10-20a:t5", "status": "skipped", "t": NOW + 6,
                                     "reason": "no time"}, {"bogus": 1}]}
        self.assertEqual(tasks.import_results(self.conn, data), 2)
        self.assertEqual(tasks.import_results(self.conn, data), 0)  # same upload again: nothing changes
        r1.update(status="done", qty=200, t=NOW + 50, paid=81000000)
        self.assertEqual(tasks.import_results(self.conn, data), 1)
        rows = tasks.get_results(self.conn)["results"]
        self.assertEqual([(r["task"], r["status"]) for r in rows], [("t1", "done"), ("t5", "skipped")])
        self.assertEqual(rows[1]["plan"], "p2026-10-20a")  # filled from the id
        self.assertEqual(tasks.get_results(self.conn, since=NOW + 10)["results"][0]["qty"], 200)
        self.assertEqual(tasks.get_results(self.conn, plan="other")["results"], [])
        by_task = {t["id"]: t["result"] for t in tasks.get_plan(self.conn, now=NOW)["plan"]["tasks"]}
        self.assertEqual((by_task["t1"]["seen"], by_task["t5"]["reason"], by_task["t2"]), (409000, "no time", None))

    def test_import_from_addon_saved_variables(self):
        """ledger.import_data picks up taskResults from the addon harness's SavedVariables fixture."""
        data = svparse.parse_file(FIXTURES / "Goldmine.lua")["GoldmineDB"]
        self.assertTrue(data.get("taskResults"), "regenerate fixtures/Goldmine.lua from the addon test")
        ledger.import_data(self.conn, data, "fixture")
        rows = tasks.get_results(self.conn, limit=1000)["results"]
        self.assertEqual(len(rows), len(data["taskResults"]))
        self.assertTrue({r["status"] for r in rows} <= set(tasks.STATUSES))
        self.assertTrue(all(r["char"] == "Snow-Testrealm" for r in rows))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        d = Path(cls.dir.name)
        cls.patches = [mock.patch.object(settings, k, v) for k, v in
                       {"DATA": d, "MARKET_DB": d / "market.sqlite", "SERVER_DB": d / "server.sqlite",
                        "USERS_DIR": d / "users", "CONFIG": d / "config.toml"}.items()]
        for p in cls.patches:
            p.start()
        db._schema_ready = False
        db.market_conn().close()
        with db.server_conn() as conn:
            uid = conn.execute("INSERT INTO users (name, handle, is_admin, created) VALUES ('snow', x'02', 1, 0)"
                               ).lastrowid
        cls.h = {"authorization": f"Bearer {auth.create_client_key(auth.User(uid, 'snow', True), 'trader')}"}
        cls.api = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        db._schema_ready = False
        cls.dir.cleanup()

    def call(self, name, arguments):
        r = self.api.post("/api/v1/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["result"]

    def test_round_trip(self):
        self.assertEqual(self.api.get("/api/v1/addon-tasks").status_code, 401)
        empty = self.api.get("/api/v1/addon-tasks", headers=self.h)
        self.assertFalse(svparse.parse(empty.content)["GoldmineTasks"]["tasks"])  # {} : an empty Lua table

        tools = {t["name"]: t for t in self.api.post("/api/v1/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
        self.assertFalse(tools["publish_plan"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["get_plan"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["get_task_results"]["annotations"]["readOnlyHint"])

        now = int(time.time())
        plan = sample_plan(created=now, expires=now + 6 * 3600)
        bad = copy.deepcopy(plan)
        bad["tasks"][0]["max_unit"] = 41.2
        res = self.call("publish_plan", {"plan": bad})
        self.assertTrue(res["isError"])
        self.assertIn("max_unit must be a positive integer", res["content"][0]["text"])

        res = self.call("publish_plan", {"plan": plan})
        self.assertNotIn("isError", res, res)
        self.assertEqual(res["structuredContent"]["plan"], "p2026-10-20a")

        lua = svparse.parse(self.api.get("/api/v1/addon-tasks", headers=self.h).content)["GoldmineTasks"]
        self.assertEqual((lua["plan"], len(lua["tasks"]), lua["tasks"][0]["max_unit"]), ("p2026-10-20a", 7, 412000))

        sv = ('GoldmineDB = { ["log"] = {}, ["chars"] = {}, ["taskResults"] = { { ["id"] = "p2026-10-20a:t1", '
              '["plan"] = "p2026-10-20a", ["task"] = "t1", ["c"] = "Snow-Proudmoore", ["t"] = %d, '
              '["status"] = "blocked", ["seen"] = 455000, ["qty"] = 0, ["reason"] = "too expensive" } } }' % now)
        r = self.api.post("/api/v1/game-data", headers={**self.h, "content-encoding": "gzip"},
                          content=gzip.compress(sv.encode()))
        self.assertEqual(r.status_code, 200, r.text)

        res = self.call("get_task_results", {"plan": "p2026-10-20a"})["structuredContent"]["results"]
        self.assertEqual([(r["task"], r["status"], r["seen"]) for r in res], [("t1", "blocked", 455000)])
        got = self.call("get_plan", {})["structuredContent"]
        self.assertTrue(got["active"])
        self.assertEqual(got["plan"]["tasks"][0]["result"]["reason"], "too expensive")
        self.assertIsNone(self.call("get_plan", {"plan": "nope"})["structuredContent"]["plan"])


if __name__ == "__main__":
    unittest.main()
