"""Client tests: addon install/update and the server-facing commands, against a stub server."""

import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import addon  # noqa: E402
import goldmine  # noqa: E402

REPO_ADDON = Path(__file__).resolve().parents[2] / "addon" / "Goldmine"
KEY = "gm_testkey"

# The same files and hash are pinned in server/tests/test_releases.py: client and server must agree.
HASH_TEST_FILES = {"Goldmine.toc": "## Version: 1.2\n", "Goldmine.lua": "print(1)\n", "sub/x.lua": "x\n",
                   "Data.lua": "ignored", ".hidden": "ignored"}
HASH_TEST_VECTOR = "634544549ee09d88b49a4ec8ae84ba8ae41458e2fad3d23e65efb4885e710b01"


def zip_addon(src: Path) -> bytes:
    """A release zip like the server makes: Goldmine/<files>, no Data.lua."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src).as_posix()
            if p.is_file() and rel != "Data.lua" and not rel.startswith("."):
                zf.writestr(f"Goldmine/{rel}", p.read_bytes())
    return buf.getvalue()


class StubServer:
    """Just enough of the Goldmine client API, checking the Bearer key on every request."""

    def __init__(self):
        self.addon_zip = zip_addon(REPO_ADDON)
        self.uploads = []
        self.mcp_calls = []
        self.active_plan = None   # what the stub's get_plan reports
        self.paper_only = False   # ... and whether that plan is paper only (newer servers say so)
        self.last_load = None
        self.tasks_lua = b"GoldmineTasks = { generated = 1, tasks = {} }\n"  # None: a server without the queue
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json"):
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authed(self):
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    self.send(401, {"error": "invalid or revoked client key"})
                    return False
                return True

            def do_GET(self):
                if not self.authed():
                    return
                path = self.path.split("?")[0]
                if path == "/api/v1/me":
                    self.send(200, {"user": "snow", "admin": True, "server_version": "test"})
                elif path == "/api/v1/updates":
                    self.send(200, {"addon": {"version": "0.1.0", "hash": addon.content_hash(REPO_ADDON),
                                              "sha256": hashlib.sha256(stub.addon_zip).hexdigest(),
                                              "size": len(stub.addon_zip), "url": "/api/v1/updates/addon.zip"},
                                    "app": {}})
                elif path == "/api/v1/updates/addon.zip":
                    self.send(200, stub.addon_zip, "application/zip")
                elif path == "/api/v1/addon-data":
                    self.send(200, b"Goldmine_Data = {}\n", "text/plain")
                elif path == "/api/v1/addon-tasks" and stub.tasks_lua is not None:
                    self.send(200, stub.tasks_lua, "text/plain")
                elif path == "/api/v1/export.csv":
                    self.send(200, f"id,character\n1,Snow-Testrealm\n#{self.path}\n".encode(), "text/csv")
                else:
                    self.send(404, {"error": "not found"})

            def do_POST(self):
                if not self.authed():
                    return
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/v1/mcp":
                    msg = json.loads(body)
                    stub.mcp_calls.append(msg)
                    p = msg.get("params") or {}
                    if msg["method"] == "tools/call" and p.get("name") == "get_plan":
                        sc = {"plan": {"plan": stub.active_plan} if stub.active_plan else None, "loaded_by": [],
                              "addon_last_loaded": stub.last_load}
                        if stub.paper_only:
                            sc["plan"]["in_game"], sc["in_game_plan"] = False, None
                        self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [], "structuredContent": sc}})
                        return
                    if msg["method"] == "tools/call" and p.get("name") == "publish_plan":
                        sc = {"plan": p["arguments"]["plan"]["plan"], "active": True}
                        self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [], "structuredContent": sc}})
                        return
                    result = {"initialize": {"instructions": "stub server"},
                              "tools/list": {"tools": [{"name": "list_markets"}, {"name": "sync"}]},
                              "tools/call": {"content": [{"type": "text", "text": "echo"}],
                                             "structuredContent": msg["params"]}}.get(msg["method"])
                    self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": result} if result is not None
                              else {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}})
                    return
                stub.uploads.append(gzip.decompress(body))
                self.send(200, {"new": 3, "total": 5})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = StubServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wow = Path(self.tmp.name) / "World of Warcraft"
        for flavor in ("_retail_", "_classic_"):
            (self.wow / flavor).mkdir(parents=True)
        self.config = str(Path(self.tmp.name) / "config.toml")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, server=True, key=KEY) -> tuple[int, str, str]:
        import unittest.mock as mock
        out, err = io.StringIO(), io.StringIO()
        pre = ["--config", self.config] + (["--server", self.server.url] if server else [])
        env = {"GOLDMINE_KEY": key} if server and key else {}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, env) as e:
            if not env:
                e.pop("GOLDMINE_KEY", None)
            code = goldmine.main([*pre, *args])
        return code, out.getvalue(), err.getvalue()

    def wow_args(self):
        return ["--wow-dir", str(self.wow)]

    def dest(self, flavor="_retail_") -> Path:
        return addon.target_for(self.wow / flavor)

    def test_hash_matches_server_vector(self):
        d = Path(self.tmp.name) / "vec"
        for name, text in HASH_TEST_FILES.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)
        self.assertEqual(addon.content_hash(d), HASH_TEST_VECTOR)

    def test_every_command_needs_a_server(self):
        for argv in (["sync"], ["push"], ["summary"], ["addon", "install"], ["status"]):
            code, _, err = self.cli(*argv, *(self.wow_args() if argv[0] in ("sync", "push", "addon", "status") else []),
                                    server=False)
            self.assertEqual(code, 1, argv)
            self.assertIn("no Goldmine server configured", err)

    def test_bad_key_is_reported(self):
        code, _, err = self.cli("server-check", key="gm_wrong")
        self.assertEqual(code, 1)
        self.assertIn("401", err)
        code, _, err = self.cli("server-check", key="https://gold.example.com/invite/x")
        self.assertIn("isn't a Goldmine API key", err)

    def test_install_needs_a_flavor_when_ambiguous(self):
        code, _, err = self.cli("addon", "install", *self.wow_args())
        self.assertEqual(code, 1)
        self.assertIn("--flavor", err)
        self.assertFalse(self.dest().exists())

    def test_install_update_keeps_data_lua(self):
        code, out, err = self.cli("addon", "install", "--flavor", "_retail_", *self.wow_args())
        self.assertEqual(code, 0, err)
        self.assertIn("_retail_: installed", out)
        self.assertEqual(addon.content_hash(self.dest()), addon.content_hash(REPO_ADDON))
        self.assertFalse(self.dest("_classic_").exists())

        (self.dest() / "Data.lua").write_text("Goldmine_Data = {}")
        (self.dest() / "Tasks.lua").write_text("GoldmineTasks = {}")
        self.assertEqual(addon.content_hash(self.dest()), addon.content_hash(REPO_ADDON))  # generated files don't count
        (self.dest() / "Goldmine.lua").write_text("-- old version")
        st = json.loads(self.cli("addon", "status", "--json", *self.wow_args())[1])
        retail = next(c for c in st["copies"] if c["flavor"] == "_retail_")
        self.assertEqual((retail["installed"], retail["current"]), (True, False))

        code, out, err = self.cli("addon", "install", "--auto", *self.wow_args())
        self.assertEqual(code, 0, err)
        self.assertIn("_retail_: updated", out)
        self.assertEqual((self.dest() / "Data.lua").read_text(), "Goldmine_Data = {}")
        self.assertEqual((self.dest() / "Tasks.lua").read_text(), "GoldmineTasks = {}")
        self.assertEqual(addon.content_hash(self.dest()), addon.content_hash(REPO_ADDON))
        self.assertEqual([p.name for p in self.dest().parent.iterdir()], ["Goldmine"])  # no staging left
        self.assertIn("_retail_: current", self.cli("addon", "install", *self.wow_args())[1])

    def test_linked_dev_copy_left_alone(self):
        self.dest().parent.mkdir(parents=True)
        os.symlink(REPO_ADDON, self.dest())
        code, out, _ = self.cli("addon", "install", "--flavor", "_retail_", *self.wow_args())
        self.assertEqual(code, 0)
        self.assertIn("linked", out)
        self.assertTrue(self.dest().is_symlink())

    def test_push_writes_every_installed_copy(self):
        code, _, err = self.cli("push", *self.wow_args())
        self.assertEqual(code, 1)
        self.assertIn("isn't installed", err)
        self.cli("addon", "install", "--flavor", "_retail_", "--flavor", "_classic_", *self.wow_args())
        code, out, err = self.cli("push", *self.wow_args())
        self.assertEqual(code, 0, err)
        for flavor in ("_retail_", "_classic_"):
            self.assertEqual((self.dest(flavor) / "Data.lua").read_text(), "Goldmine_Data = {}\n")
            self.assertEqual((self.dest(flavor) / "Tasks.lua").read_bytes(), self.server.tasks_lua)

    def test_push_against_server_without_task_queue(self):
        self.cli("addon", "install", "--flavor", "_retail_", *self.wow_args())
        self.server.tasks_lua = None
        try:
            code, _, err = self.cli("push", *self.wow_args())
        finally:
            self.server.tasks_lua = b"GoldmineTasks = { generated = 1, tasks = {} }\n"
        self.assertEqual(code, 0, err)
        self.assertIn("addon tasks: skipped", err)
        self.assertTrue((self.dest() / "Data.lua").exists())
        self.assertFalse((self.dest() / "Tasks.lua").exists())

    def test_sync_uploads_saved_variables(self):
        sv = self.wow / "_retail_" / "WTF" / "Account" / "ACCT" / "SavedVariables"
        sv.mkdir(parents=True)
        (sv / "Goldmine.lua").write_text("GoldmineDB = {}")
        (sv / "Goldmine.lua.bak").write_text("old")
        code, out, err = self.cli("sync", *self.wow_args())
        self.assertEqual(code, 0, err)
        self.assertIn("3 new of 5 entries", out)
        self.assertEqual(self.server.uploads[-1], b"GoldmineDB = {}")

    def mcp(self, *msgs, server=True) -> list[dict]:
        """Run `goldmine.py mcp` over these JSON-RPC messages -> its replies."""
        import unittest.mock as mock
        stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in msgs) + "not json\n")
        stdout = io.StringIO()
        pre = ["--config", self.config] + (["--server", self.server.url] if server else [])
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                mock.patch.dict(os.environ, {"GOLDMINE_KEY": KEY}):
            self.assertEqual(goldmine.main([*pre, "mcp", *self.wow_args()]), 0)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_mcp_forwards_server_tools_and_adds_local_ones(self):
        self.cli("addon", "install", "--flavor", "_retail_", *self.wow_args())
        sv = self.wow / "_retail_" / "WTF" / "Account" / "ACCT" / "SavedVariables"
        sv.mkdir(parents=True)
        (sv / "Goldmine.lua").write_text("GoldmineDB = { log = {} }")
        call = lambda i, name, **a: {"jsonrpc": "2.0", "id": i, "method": "tools/call",  # noqa: E731
                                     "params": {"name": name, "arguments": a}}
        init, listed, fwd, status, sync, push, unknown, parse_err = self.mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call(3, "list_markets"), call(4, "client_status"), call(5, "sync"), call(6, "push"),
            {"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertTrue(init["result"]["instructions"].startswith("stub server "))
        names = [t["name"] for t in listed["result"]["tools"]]
        self.assertEqual(names, ["sync", "push", "addon_status", "client_status", "list_markets"])  # the local sync wins
        self.assertEqual(fwd["result"]["structuredContent"], {"name": "list_markets", "arguments": {}})
        st = status["result"]["structuredContent"]
        self.assertEqual((st["server"], st["me"]["user"], len(st["saved_variables"])), (self.server.url, "snow", 1))
        self.assertTrue(st["addon"])
        self.assertTrue(sync["result"]["structuredContent"]["ok"], sync)
        self.assertIn("3 new of 5 entries", sync["result"]["structuredContent"]["output"])
        self.assertEqual(self.server.uploads[-1], b"GoldmineDB = { log = {} }")
        self.assertTrue((self.dest() / "Tasks.lua").exists())
        self.assertTrue(push["result"]["structuredContent"]["ok"])
        self.assertEqual(unknown["error"]["code"], -32601)
        self.assertEqual(parse_err["error"]["code"], -32700)
        self.assertEqual({m["method"] for m in self.server.mcp_calls}, {"initialize", "tools/list", "tools/call"})

    def test_mcp_keeps_reads_fresh_and_delivers_plans(self):
        self.cli("addon", "install", "--flavor", "_retail_", *self.wow_args())
        sv = self.wow / "_retail_" / "WTF" / "Account" / "ACCT" / "SavedVariables" / "Goldmine.lua"
        sv.parent.mkdir(parents=True)
        sv.write_text("GoldmineDB = {}")
        call = lambda i, name, **a: {"jsonrpc": "2.0", "id": i, "method": "tools/call",  # noqa: E731
                                     "params": {"name": name, "arguments": a}}
        texts = lambda r: [c["text"] for c in r["result"]["content"]]  # noqa: E731
        before = len(self.server.uploads)
        acct, again, market = self.mcp(call(1, "get_account"), call(2, "get_account"), call(3, "list_markets"))[:3]
        self.assertIn("auto-synced 1 SavedVariables file first", texts(acct))
        self.assertFalse(any("auto-synced" in t for t in texts(again) + texts(market)))
        self.assertEqual(len(self.server.uploads), before + 1)
        os.utime(sv, (sv.stat().st_mtime + 10,) * 2)  # WoW wrote it again (a /reload)
        (ledger,) = self.mcp(call(1, "get_ledger"))[:1]
        self.assertIn("auto-synced 1 SavedVariables file first", texts(ledger))

        self.server.active_plan = "p1"
        (st,) = self.mcp(call(1, "addon_status"))[:1]
        self.assertEqual(st["result"]["structuredContent"]["state"], "not_delivered")
        old_lua = self.server.tasks_lua
        self.server.tasks_lua = b'GoldmineTasks = {\n  generated = 5,\n  plan = "p1", tasks = {} }\n'
        try:
            pub, st = self.mcp(call(1, "publish_plan", plan={"plan": "p1"}), call(2, "addon_status"))[:2]
            self.assertTrue(pub["result"]["structuredContent"]["delivered"]["ok"], pub)
            self.assertIn("pushed Tasks.lua to the addon", texts(pub)[-1])
            self.assertIn(b'plan = "p1"', (self.dest() / "Tasks.lua").read_bytes())
            s1 = st["result"]["structuredContent"]
            self.assertEqual((s1["state"], s1["tasks_on_disk"][0]["plan"], s1["tasks_on_disk"][0]["generated"]),
                             ("delivered", "p1", 5))
            self.assertFalse(s1["saved_variables"][0]["pending_upload"])
            self.server.last_load = {"character": "Snow-Proudmoore", "plan": "p1", "at": 6}
            (st,) = self.mcp(call(1, "addon_status"))[:1]
            self.assertEqual(st["result"]["structuredContent"]["state"], "loaded")
            self.server.paper_only = True
            (st,) = self.mcp(call(1, "addon_status"))[:1]
            self.assertEqual(st["result"]["structuredContent"]["state"], "paper_only")
        finally:
            self.server.tasks_lua, self.server.active_plan, self.server.last_load = old_lua, None, None
            self.server.paper_only = False

    def test_mcp_without_a_server_still_answers(self):
        init, listed, fwd = self.mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_plan", "arguments": {}}},
            server=False)[:3]
        self.assertEqual(init["result"]["serverInfo"]["name"], "goldmine")
        self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["sync", "push", "addon_status", "client_status"])
        self.assertTrue(fwd["result"]["isError"])
        self.assertIn("no Goldmine server configured", fwd["result"]["content"][0]["text"])

    def test_export_passes_filters(self):
        out_path = Path(self.tmp.name) / "out.csv"
        code, out, err = self.cli("export", str(out_path), "--source", "auction_sale", "--since", "7d")
        self.assertEqual(code, 0, err)
        text = out_path.read_text()
        self.assertIn("source=auction_sale", text)
        self.assertIn("since=", text)

    def test_config_keeps_tables_it_doesnt_use(self):
        import unittest.mock as mock
        cfg = {"server": {"url": self.server.url, "key": KEY}, "app": {"auto_sync": True},
               "api": {"client_id": "abc"}, "market": [{"name": "m1", "kind": "commodities"}]}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(cfg))):
            self.assertEqual(self.cli("config", "set", server=False)[0], 0)
        self.assertEqual(oct(os.stat(self.config).st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(self.cli("config", "get", server=False)[1]), cfg)
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"server": {"url": "https://gold.example.com/invite/x"}}))):
            code, _, err = self.cli("config", "set", server=False)
        self.assertEqual(code, 1)
        self.assertIn("no API key", err)

    def test_config_url_and_key(self):
        Path(self.config).write_text(f'[server]\nurl = "{self.server.url}"\nkey = "{KEY}"\n')
        code, out, err = self.cli("server-check", "--json", server=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["server"], self.server.url)

    def test_old_style_url_still_works_and_is_split(self):
        import unittest.mock as mock
        legacy = self.server.url.replace("://", f"://{KEY}@")
        Path(self.config).write_text(f'[server]\nurl = "{legacy}"\n')
        code, out, err = self.cli("server-check", "--json", server=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["server"], self.server.url)  # never echoes the key
        got = json.loads(self.cli("config", "get", server=False)[1])
        self.assertEqual(got["server"], {"url": self.server.url, "key": KEY})
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"server": {"url": legacy}}))):
            self.assertEqual(self.cli("config", "set", server=False)[0], 0)
        text = Path(self.config).read_text()
        self.assertIn(f'url = "{self.server.url}"', text)
        self.assertIn(f'key = "{KEY}"', text)
        self.assertEqual(self.cli("--server", legacy, "server-check", server=False)[0], 0)

    def test_rejects_unsafe_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Goldmine/../../evil.lua", "x")
        with self.assertRaises(addon.AddonError):
            addon.install(buf.getvalue(), self.wow / "_retail_")
        self.assertFalse((self.wow / "_retail_" / "Interface" / "evil.lua").exists())

    def test_discovery_finds_any_flavor_folder(self):
        sv = self.wow / "_forever_" / "WTF" / "Account" / "ACCT" / "SavedVariables"
        sv.mkdir(parents=True)
        (sv / "Goldmine.lua").write_text("x")
        self.assertEqual(goldmine.find_saved_variables([str(self.wow)]), [sv / "Goldmine.lua"])


if __name__ == "__main__":
    unittest.main()
