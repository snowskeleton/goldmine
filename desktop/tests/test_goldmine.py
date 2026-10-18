import csv
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import goldmine  # noqa: E402
import svparse  # noqa: E402

FIXTURE = HERE / "fixtures" / "Goldmine.lua"


class ParserTests(unittest.TestCase):
    def test_wow_style_output(self):
        text = '''
Foo = {
\t["a"] = 1,
\t["neg"] = -2.5,
\t["list"] = {
\t\t"x", -- [1]
\t\t"y", -- [2]
\t},
\t[5] = true,
\tbare = nil,
\t["nested"] = { ["deep"] = { 1, 2, 3 } },
}
Bar = "hi"
'''
        out = svparse.parse(text)
        self.assertEqual(out["Bar"], "hi")
        foo = out["Foo"]
        self.assertEqual(foo["a"], 1)
        self.assertEqual(foo["neg"], -2.5)
        self.assertEqual(foo["list"], ["x", "y"])
        self.assertIs(foo[5], True)
        self.assertIsNone(foo["bare"])
        self.assertEqual(foo["nested"]["deep"], [1, 2, 3])

    def test_string_escapes_and_utf8(self):
        out = svparse.parse(r'X = { "a\"b\\c\nd", "\124cffffd700", "\195\169" }')
        self.assertEqual(out["X"], ['a"b\\c\nd', "|cffffd700", "é"])
        out = svparse.parse('X = "Kel\'Thuzad — Ragnarös"'.encode("utf-8"))
        self.assertEqual(out["X"], "Kel'Thuzad — Ragnarös")

    def test_comments_and_long_strings(self):
        out = svparse.parse("--[[ block\ncomment ]]\nX = { [[long\nstr]], -- c\n 3 }")
        self.assertEqual(out["X"], ["long\nstr", 3])

    def test_sparse_numeric_keys_stay_dict(self):
        out = svparse.parse("X = { [1] = 'a', [3] = 'c' }")
        self.assertEqual(out["X"], {1: "a", 3: "c"})

    def test_error_reports_line(self):
        with self.assertRaisesRegex(svparse.ParseError, "line 2"):
            svparse.parse("X = {\n @ }")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "gm.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = goldmine.main(["--db", self.db, *argv])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_sync_is_idempotent(self):
        out = self.run_cli("sync", "--file", str(FIXTURE))
        total = svparse.parse_file(FIXTURE)["GoldmineDB"]["log"]
        self.assertIn(f"{len(total)} new of {len(total)}", out)
        out = self.run_cli("sync", "--file", str(FIXTURE))
        self.assertIn(f"0 new of {len(total)}", out)

    def test_imported_values(self):
        self.run_cli("sync", "--file", str(FIXTURE))
        conn = goldmine.connect(self.db)
        rows = goldmine.query_rows(conn)
        sources = [r["source"] for r in rows]
        for expected in ("baseline", "vendor", "repair", "auction_sale", "mail_send", "quest",
                         "loot", "flight", "barber", "unknown", "reconcile"):
            self.assertIn(expected, sources)
        # Running balance is consistent after import.
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(prev["balance"] + cur["delta"], cur["balance"])
        char = conn.execute("SELECT name, realm, class, balance FROM characters").fetchone()
        self.assertEqual(tuple(char), ("Snow", "Testrealm", "MAGE", rows[-1]["balance"]))

    def test_csv_export_and_filters(self):
        self.run_cli("sync", "--file", str(FIXTURE))
        out_path = os.path.join(self.tmp.name, "out.csv")
        self.run_cli("export", out_path, "--source", "auction_sale", "--char", "Snow")
        with open(out_path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delta_gold"], "0.9500")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertIn('"buyer": "Buyerguy"', rows[0]["meta"])

    def test_summary_runs(self):
        self.run_cli("sync", "--file", str(FIXTURE))
        out = self.run_cli("summary")
        self.assertIn("auction_sale", out)
        self.assertIn("TOTAL", out)

    def test_item_link_parsing(self):
        link = "|cnIQ1:|Hitem:81055::::::::3:70:::::::::|h[Darkmoon Ride Ticket]|h|r"
        self.assertEqual(goldmine.parse_item(link), (81055, "Darkmoon Ride Ticket"))
        self.assertEqual(goldmine.parse_item("Linen Cloth"), (None, "Linen Cloth"))
        self.assertEqual(goldmine.parse_item(None), (None, None))

    def test_item_columns_and_region(self):
        self.run_cli("sync", "--file", str(FIXTURE))
        conn = goldmine.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT item_name FROM transactions WHERE item_name IS NOT NULL")}
        self.assertIn("Linen Cloth", names)  # from the AH sale invoice
        self.assertEqual(conn.execute("SELECT region FROM characters").fetchone()[0], "US")

    def test_market_data_import(self):
        self.run_cli("sync", "--file", str(FIXTURE))
        self.run_cli("sync", "--file", str(FIXTURE))  # re-import must not duplicate rows
        conn = goldmine.connect(self.db)
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
        self.assertEqual(q("SELECT unit_price, quantity FROM scan_rows WHERE scan_id LIKE '%:s1' ORDER BY unit_price"),
                         [(15, 200), (16, 40), (20, 500)])
        self.assertIn((82800, 42, 90000, 2), q("SELECT item_id, variant, unit_price, quantity FROM scan_rows"))
        self.assertEqual(q("SELECT recipe_id, profession, output_item FROM recipes"), [(100, "Tailoring", 2996)])
        self.assertEqual(q("SELECT item_id, quantity FROM recipe_reagents"), [(2589, 2)])
        inv = dict(((o, l, i), n) for o, l, i, n in q("SELECT owner, location, item_id, count FROM inventory"))
        self.assertEqual(inv[("Snow-Testrealm", "bags", 2589)], 25)
        self.assertEqual(inv[("Snow-Testrealm", "bank", 2996)], 7)
        self.assertEqual(inv[("warband", "warband", 4306)], 50)

    def test_migrates_old_database(self):
        conn = goldmine.sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE characters (key TEXT PRIMARY KEY, name TEXT, realm TEXT, class TEXT,
                faction TEXT, balance INTEGER, last_seen INTEGER, source_file TEXT);
            CREATE TABLE transactions (id TEXT PRIMARY KEY, char_key TEXT NOT NULL, seq INTEGER NOT NULL,
                ts INTEGER NOT NULL, delta INTEGER NOT NULL, balance INTEGER NOT NULL, source TEXT NOT NULL,
                zone TEXT, subzone TEXT, map_id INTEGER, session TEXT, meta TEXT);
            CREATE VIEW transactions_view AS SELECT id FROM transactions;
        """)
        conn.close()
        self.run_cli("sync", "--file", str(FIXTURE))
        conn = goldmine.connect(self.db)
        conn.execute("SELECT item_name FROM transactions_view").fetchall()

    def test_config_roundtrip(self):
        import json
        from unittest import mock
        cfg_path = os.path.join(self.tmp.name, "config.toml")
        cfg = {"api": {"client_id": "abc", "client_secret": 'q"uote'},
               "app": {"collect_minutes": 20, "auto_collect": True},
               "market": [{"name": "m1", "region": "us", "namespace": "dynamic-us", "kind": "commodities"},
                          {"name": "m2", "region": "us", "namespace": "dynamic-us", "kind": "realm", "realm": "x"}]}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(cfg))):
            self.run_cli("config", "set", "--config", cfg_path)
        self.assertEqual(oct(os.stat(cfg_path).st_mode & 0o777), "0o600")
        out = self.run_cli("config", "get", "--config", cfg_path)
        self.assertEqual(json.loads(out), cfg)

    def test_money_format(self):
        self.assertEqual(goldmine.fmt_gold(34), "34c")
        self.assertEqual(goldmine.fmt_gold(-479), "-4s 79c")
        self.assertEqual(goldmine.fmt_gold(10005, True), "+1g 00s 05c")
        self.assertEqual(goldmine.fmt_gold(8077059418), "807,705g 94s 18c")

    def test_discovery_finds_any_flavor_folder(self):
        sv = Path(self.tmp.name, "WoW", "_forever_", "WTF", "Account", "ACCT", "SavedVariables")
        sv.mkdir(parents=True)
        (sv / "Goldmine.lua").write_bytes(FIXTURE.read_bytes())
        (sv / "Goldmine.lua.bak").write_bytes(b"")
        found = goldmine.find_saved_variables([str(Path(self.tmp.name, "WoW"))])
        self.assertEqual(found, [sv / "Goldmine.lua"])


if __name__ == "__main__":
    unittest.main()
