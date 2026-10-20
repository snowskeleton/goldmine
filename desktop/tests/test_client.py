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
                stub.uploads.append(gzip.decompress(body))
                self.send(200, {"new": 3, "total": 5})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://{KEY}@127.0.0.1:{self.httpd.server_port}"
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

    def cli(self, *args, server=True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        pre = ["--config", self.config] + (["--server", self.server.url] if server else [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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
        code, _, err = self.cli("--server", self.server.url.replace(KEY, "gm_wrong"), "server-check", server=False)
        self.assertEqual(code, 1)
        self.assertIn("401", err)

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

    def test_export_passes_filters(self):
        out_path = Path(self.tmp.name) / "out.csv"
        code, out, err = self.cli("export", str(out_path), "--source", "auction_sale", "--since", "7d")
        self.assertEqual(code, 0, err)
        text = out_path.read_text()
        self.assertIn("source=auction_sale", text)
        self.assertIn("since=", text)

    def test_config_keeps_tables_it_doesnt_use(self):
        import unittest.mock as mock
        cfg = {"server": {"url": self.server.url}, "app": {"auto_sync": True},
               "api": {"client_id": "abc"}, "market": [{"name": "m1", "kind": "commodities"}]}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(cfg))):
            self.assertEqual(self.cli("config", "set", server=False)[0], 0)
        self.assertEqual(oct(os.stat(self.config).st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(self.cli("config", "get", server=False)[1]), cfg)
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"server": {"url": "https://gold.example.com/invite/x"}}))):
            code, _, err = self.cli("config", "set", server=False)
        self.assertEqual(code, 1)
        self.assertIn("no client key", err)

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
