"""End-to-end server tests: passkey setup/sign-in (with a software authenticator that performs
real WebAuthn signatures), invites, client keys, the client API, and per-user isolation."""

import base64
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = tempfile.TemporaryDirectory()
os.environ["GOLDMINE_DATA"] = TMP.name
os.environ["GOLDMINE_COLLECTOR"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cbor2  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from goldmine_server import db, settings, store  # noqa: E402
from goldmine_server.app import app  # noqa: E402


ORIGIN = "http://testserver"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "Goldmine.lua"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftAuthenticator:
    """A platform authenticator in software: P-256 key, 'none' attestation, resident credential."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.counter = 0
        self.user_handle = None

    def create(self, options: dict, origin: str = ORIGIN) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        client_data = json.dumps({"type": "webauthn.create", "challenge": options["challenge"],
                                  "origin": origin, "crossOrigin": False}).encode()
        auth_data = (hashlib.sha256(options["rp"]["id"].encode()).digest() + bytes([0x45])
                     + self.counter.to_bytes(4, "big") + bytes(16)
                     + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cose)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        self.user_handle = options["user"]["id"]
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(client_data), "attestationObject": b64(attestation),
                             "transports": ["internal"]},
                "clientExtensionResults": {}}

    def get(self, options: dict, origin: str = ORIGIN) -> dict:
        self.counter += 1
        client_data = json.dumps({"type": "webauthn.get", "challenge": options["challenge"],
                                  "origin": origin, "crossOrigin": False}).encode()
        auth_data = (hashlib.sha256(options["rpId"].encode()).digest() + bytes([0x05])
                     + self.counter.to_bytes(4, "big"))
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(client_data), "authenticatorData": b64(auth_data),
                             "signature": b64(sig), "userHandle": self.user_handle},
                "clientExtensionResults": {}}


def register(client: TestClient, auth: SoftAuthenticator, name: str, mode: str, invite=None):
    r = client.post("/auth/register/options", json={"name": name, "mode": mode, "invite": invite})
    if r.status_code != 200:
        return r
    return client.post("/auth/register/verify", json={"credential": auth.create(r.json()), "mode": mode,
                                                       "passkeyName": "test key"})


def sign_in(client: TestClient, auth: SoftAuthenticator):
    options = client.post("/auth/login/options").json()
    return client.post("/auth/login/verify", json={"credential": auth.get(options)})


class ServerFlow(unittest.TestCase):
    """Steps run in order (unittest sorts by name) and share state through the class."""

    @classmethod
    def setUpClass(cls):
        cls.admin = TestClient(app, follow_redirects=False)
        cls.admin.__enter__()
        cls.admin_key = SoftAuthenticator()

    @classmethod
    def tearDownClass(cls):
        cls.admin.__exit__(None, None, None)

    def test_01_first_visit_goes_to_setup(self):
        r = self.admin.get("/")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/setup"))
        self.assertIn("becomes the <strong>admin</strong>", self.admin.get("/setup").text)

    def test_02_setup_creates_admin_and_signs_in(self):
        r = register(self.admin, self.admin_key, "snow", "setup")
        self.assertEqual(r.status_code, 200, r.text)
        page = self.admin.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Dashboard", page.text)
        self.assertIn('href="/users"', page.text)  # admin nav

    def test_03_setup_closed_once_a_user_exists(self):
        with TestClient(app, follow_redirects=False) as stranger:
            self.assertEqual(stranger.get("/setup").headers["location"], "/login")
            r = register(stranger, SoftAuthenticator(), "mallory", "setup")
            self.assertEqual(r.status_code, 403)

    def test_04_sign_out_and_back_in(self):
        self.admin.post("/logout")
        self.assertEqual(self.admin.get("/").headers["location"], "/login")
        r = sign_in(self.admin, self.admin_key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.admin.get("/").status_code, 200)

    def test_05a_phishing_origin_and_tampering_rejected(self):
        # A passkey ceremony performed on another origin (a phishing site) must not verify.
        with TestClient(app) as other:
            auth = SoftAuthenticator()
            options = other.post("/auth/register/options", json={"name": "x", "mode": "setup"})
            self.assertEqual(options.status_code, 403)  # setup closed anyway; try sign-in instead
            options = other.post("/auth/login/options").json()
            cred = self.admin_key.get(options, origin="https://evil.example")
            self.assertEqual(other.post("/auth/login/verify", json={"credential": cred}).status_code, 400)
            # A valid response with a corrupted signature must not verify either.
            options = other.post("/auth/login/options").json()
            cred = self.admin_key.get(options)
            sig = bytearray(base64.urlsafe_b64decode(cred["response"]["signature"] + "=="))
            sig[-1] ^= 0xFF
            cred["response"]["signature"] = b64(bytes(sig))
            self.assertEqual(other.post("/auth/login/verify", json={"credential": cred}).status_code, 400)
            # Replaying a challenge from an old session doesn't work (challenge is single use).
            self.assertEqual(other.post("/auth/login/verify", json={"credential": cred}).status_code, 400)
            self.assertEqual(other.get("/", follow_redirects=False).status_code, 303)

    def test_05_unknown_passkey_rejected(self):
        with TestClient(app) as other:
            r = sign_in(other, SoftAuthenticator())
            self.assertEqual(r.status_code, 400)

    def test_06_invite_flow(self):
        page = self.admin.post("/users/invite", data={"note": "friend"}).text
        link = re.search(r'value="(http://testserver/invite/[^"]+)"', page).group(1)
        token = link.rsplit("/", 1)[1]
        with TestClient(app, follow_redirects=False) as friend:
            self.assertIn("You're invited", friend.get(f"/invite/{token}").text)
            r = register(friend, SoftAuthenticator(), "friend", "invite", invite=token)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(friend.get("/").status_code, 200)
            # members can't reach admin pages
            self.assertEqual(friend.get("/markets", headers={"accept": "application/json"}).status_code, 403)
        with TestClient(app) as late:
            self.assertEqual(register(late, SoftAuthenticator(), "late", "invite", invite=token).status_code, 403)
            self.assertIn("invalid, already used, or expired", late.get(f"/invite/{token}").text)

    def test_07_add_second_passkey(self):
        second = SoftAuthenticator()
        r = register(self.admin, second, "", "add")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.admin.get("/account").text.count('action="/account/passkeys/remove"'), 2)
        with TestClient(app) as laptop:
            self.assertEqual(sign_in(laptop, second).status_code, 200)

    def test_08_client_key_and_api(self):
        page = self.admin.post("/clients", data={"name": "MacBook"}).text
        url = re.search(r'value="(http://gm_[^"]+)"', page).group(1)
        key = url.split("://", 1)[1].split("@", 1)[0]
        self.assertTrue(url.endswith("@testserver"))
        type(self).key = key
        api = TestClient(app)
        self.assertEqual(api.get("/api/v1/me").status_code, 401)
        self.assertEqual(api.get("/api/v1/me", headers={"authorization": "Bearer gm_wrong"}).status_code, 401)
        me = api.get("/api/v1/me", headers={"authorization": f"Bearer {key}"}).json()
        self.assertEqual((me["user"], me["admin"]), ("snow", True))

    def test_08b_updates(self):
        import io
        import plistlib
        import zipfile
        h = {"authorization": f"Bearer {self.key}"}
        api = TestClient(app)
        self.assertEqual(api.get("/api/v1/updates").status_code, 401)
        m = api.get("/api/v1/updates", headers=h).json()
        self.assertEqual(m["app"], {})
        self.assertEqual(len(m["addon"]["hash"]), 64)
        z = api.get(m["addon"]["url"], headers=h).content
        self.assertEqual(hashlib.sha256(z).hexdigest(), m["addon"]["sha256"])
        names = zipfile.ZipFile(io.BytesIO(z)).namelist()
        self.assertIn("Goldmine/Goldmine.toc", names)
        self.assertNotIn("Goldmine/Data.lua", names)

        def app_zip(build, bundle="net.snowskeleton.goldmine"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("Goldmine.app/Contents/Info.plist", plistlib.dumps(
                    {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": "0.3.0", "CFBundleVersion": str(build)}))
                zf.writestr("Goldmine.app/Contents/MacOS/Goldmine", b"binary")
            return buf.getvalue()

        up = lambda data, **kw: api.post("/api/v1/releases/app", content=data, headers=h, params=kw)
        self.assertEqual(up(app_zip(5, bundle="com.evil")).status_code, 400)
        self.assertEqual(up(b"not a zip").status_code, 400)
        r = up(app_zip(5), notes="first")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(up(app_zip(5)).status_code, 409)  # builds only go forward
        self.assertEqual(up(app_zip(6)).status_code, 200)
        self.assertEqual(up(app_zip(7)).status_code, 200)
        rel = api.get("/api/v1/updates", headers=h).json()["app"]["macos"]
        self.assertEqual((rel["version"], rel["build"]), ("0.3.0", 7))
        z = api.get(rel["url"], headers=h).content
        self.assertEqual(hashlib.sha256(z).hexdigest(), rel["sha256"])
        kept = sorted(p.name for p in (settings.RELEASES / "app" / "macos").glob("*.zip"))
        self.assertEqual(kept, ["Goldmine-macos-6.zip", "Goldmine-macos-7.zip"])
        page = self.admin.get("/clients").text
        self.assertIn("/download/app/macos", page)
        self.assertEqual(self.admin.get("/download/app/macos").content, z)
        self.assertEqual(self.admin.get("/download/addon").status_code, 200)
        self.assertEqual(TestClient(app, follow_redirects=False).get("/download/addon").status_code, 303)

    def test_09_upload_game_data(self):
        h = {"authorization": f"Bearer {self.key}", "content-encoding": "gzip"}
        api = TestClient(app)
        r = api.post("/api/v1/game-data?source=test", content=gzip.compress(FIXTURE.read_bytes()), headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(r.json()["new"], 5)
        again = api.post("/api/v1/game-data", content=gzip.compress(FIXTURE.read_bytes()), headers=h).json()
        self.assertEqual(again["new"], 0)  # idempotent
        st = api.get("/api/v1/status", headers={"authorization": f"Bearer {self.key}"}).json()
        self.assertEqual([c["key"] for c in st["characters"]], ["Snow-Testrealm"])
        self.assertGreater(st["recipes"], 0)
        bad = api.post("/api/v1/game-data", content=b"not lua {{{", headers={"authorization": f"Bearer {self.key}"})
        self.assertEqual(bad.status_code, 400)

    def test_10_other_users_data_is_separate(self):
        conn = db.user_conn(2)  # "friend"
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
        finally:
            conn.close()

    def test_11_addon_data_and_reports(self):
        # Market data for the fixture character's realm, as the collector would store it.
        conn = db.market_conn()
        now = int(time.time()) // 3600 * 3600
        conn.execute("INSERT INTO markets (name, region, namespace, kind, game, realms) VALUES "
                     "('us-testrealm', 'us', 'dynamic-us', 'realm', 'retail', '[\"Testrealm\"]')")
        for h in range(3):
            auctions = [{"id": 1, "item": {"id": 2589}, "quantity": 100 - 10 * h, "unit_price": 15, "time_left": "LONG"},
                        {"id": 2, "item": {"id": 2996}, "quantity": 10, "unit_price": 100, "time_left": "LONG"}]
            store.store_snapshot(conn, "us-testrealm", {"auctions": auctions}, now - (2 - h) * 3600)
        conn.close()
        h = {"authorization": f"Bearer {self.key}"}
        api = TestClient(app)
        lua = api.get("/api/v1/addon-data", headers=h).text
        self.assertIn("Goldmine_Data", lua)
        self.assertIn('realms = {"Testrealm"}', lua)
        self.assertIn("[2589] = {15, 15, ", lua)
        crafts = api.get("/api/v1/report/crafts", headers=h).text
        self.assertIn("Bolt of Linen Cloth", crafts)
        timing = api.get("/api/v1/timing?item=2589", headers=h).json()
        self.assertEqual(timing["markets"][0]["market"], "us-testrealm")
        self.assertEqual(sum(b["sold"] for b in timing["markets"][0]["buckets"]), 20)
        flips = api.get("/api/v1/report/flips?days=3&utc_offset=-5", headers=h)
        self.assertEqual(flips.status_code, 200)
        self.assertIn("commodity market", flips.text)  # only a realm market here
        self.assertEqual(self.admin.get("/reports?kind=flips").status_code, 200)
        self.assertEqual(self.admin.get("/reports?kind=crafts").status_code, 200)
        self.assertEqual(self.admin.get("/web/timing?item=2589").json()["item_id"], 2589)

    def test_11b_export_csv_and_timing_offset(self):
        h = {"authorization": f"Bearer {self.key}"}
        api = TestClient(app)
        rows = api.get("/api/v1/export.csv", headers=h).text.splitlines()
        self.assertTrue(rows[0].startswith("id,character,time"))
        self.assertGreater(len(rows), 5)
        sales = api.get("/api/v1/export.csv?source=auction_sale", headers=h).text.splitlines()
        self.assertEqual(len(sales), 2)
        self.assertIn("auction_sale", sales[1])
        self.assertEqual(api.get("/api/v1/export.csv").status_code, 401)
        text = api.get("/api/v1/report/timing?item=2589&utc_offset=-5", headers=h).text
        self.assertIn("UTC-5", text)

    def test_12_settings_and_markets(self):
        r = self.admin.post("/settings", data={"client_id": "abc", "client_secret": "s3cret", "collect_minutes": 30,
                                               "auto_collect": "1"})
        self.assertEqual(r.status_code, 303)
        self.admin.post("/settings", data={"client_id": "abc", "client_secret": "", "collect_minutes": 30})
        from goldmine_server import market
        cfg = market.load_config(settings.CONFIG)
        self.assertEqual(cfg["api"]["client_secret"], "s3cret")  # blank keeps the old secret
        self.assertFalse(cfg["app"]["auto_collect"])
        self.assertEqual(oct(settings.CONFIG.stat().st_mode & 0o777), "0o600")
        self.admin.post("/markets/add", data={"name": "us-commodities", "kind": "commodities", "region": "us",
                                              "namespace": "dynamic-us"})
        self.admin.post("/markets/add", data={"name": "us-pagle", "kind": "realm", "region": "us",
                                              "namespace": "dynamic-classic-us", "realm": "pagle"})
        self.admin.post("/markets/pricing", data={"name": "us-pagle"})
        cfg = market.load_config(settings.CONFIG)
        self.assertEqual([m["name"] for m in cfg["market"]], ["us-commodities", "us-pagle"])
        self.assertIs(cfg["market"][1]["pricing"], False)
        self.admin.post("/markets/remove", data={"name": "us-pagle"})
        self.assertEqual(len(market.load_config(settings.CONFIG)["market"]), 1)
        self.assertIn("us-commodities", self.admin.get("/markets").text)

    def test_12b_browse_realms_namespace(self):
        from unittest import mock
        from goldmine_server import app as app_module
        with mock.patch.object(app_module.ledger, "list_realms", return_value=[]) as lr:
            self.admin.get("/markets", params={"region": "eu", "game": "classic"})
            self.assertEqual(lr.call_args[0][1:], ("eu", "dynamic-classic-eu"))
            self.admin.get("/markets", params={"region": "us", "game": "other", "namespace": " dynamic-foo-us "})
            self.assertEqual(lr.call_args[0][1:], ("us", "dynamic-foo-us"))
            text = self.admin.get("/markets", params={"region": "us", "namespace": "dynamic-classic1x-us"}).text
            self.assertIn('value="classic1x" selected', text)  # old ?namespace= links preselect the game
            lr.reset_mock()
            self.admin.get("/markets")
            lr.assert_not_called()

    def test_12c_api_namespaces(self):
        from goldmine_server import market
        api = TestClient(app)
        r = api.get("/api/v1/namespaces", headers={"authorization": f"Bearer {self.key}"})
        self.assertEqual(r.json(), market.NAMESPACE_GAMES)
        self.assertEqual(api.get("/api/v1/namespaces").status_code, 401)

    def test_13_cross_origin_post_refused(self):
        r = self.admin.post("/clients", data={"name": "x"}, headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_14_revoked_key_stops_working(self):
        with db.server_conn() as conn:
            kid = conn.execute("SELECT id FROM client_keys WHERE name = 'MacBook'").fetchone()[0]
        self.admin.post(f"/clients/{kid}/revoke")
        r = TestClient(app).get("/api/v1/me", headers={"authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
