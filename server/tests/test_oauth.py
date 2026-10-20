"""OAuth for MCP connectors: discovery from the MCP endpoint's 401, passkey sign-in and consent,
PKCE code exchange, refresh rotation, revocation, and the market-only scope of the tokens."""

import base64
import hashlib
import os
import re
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("GOLDMINE_DATA", TMP.name)
os.environ["GOLDMINE_COLLECTOR"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from goldmine_server import db, settings  # noqa: E402
from goldmine_server.app import app  # noqa: E402
from test_server import SoftAuthenticator, register, sign_in  # noqa: E402

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthFlow(unittest.TestCase):
    """Steps run in order and share state through the class."""

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
        cls.browser = TestClient(app, follow_redirects=False)
        cls.passkey = SoftAuthenticator()
        assert register(cls.browser, cls.passkey, "snow", "setup").status_code == 200
        cls.api = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        db._schema_ready = False
        cls.dir.cleanup()

    def authorize(self, client, challenge, **extra):
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": CALLBACK,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
                  "scope": "market:read", "resource": "http://testserver/api/v1/mcp", **extra}
        return client.get("/oauth/authorize", params=params)

    def approve(self, client, challenge, decision="allow"):
        page = self.authorize(client, challenge)
        self.assertEqual(page.status_code, 200, page.text)
        nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
        r = client.post("/oauth/authorize", data={"nonce": nonce, "decision": decision})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(CALLBACK + "?"))
        return parse_qs(urlsplit(r.headers["location"]).query)

    def token(self, auth=None, **form):
        return self.api.post("/oauth/token", data=form, auth=auth or (self.client_id, self.secret))

    def test_01_discovery(self):
        r = self.api.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)
        meta_url = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"]).group(1)
        self.assertEqual(meta_url, "http://testserver/.well-known/oauth-protected-resource/api/v1/mcp")
        meta = self.api.get(urlsplit(meta_url).path).json()
        self.assertEqual(meta["resource"], "http://testserver/api/v1/mcp")
        (issuer,) = meta["authorization_servers"]
        asm = self.api.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(asm["issuer"], issuer)
        self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(asm["token_endpoint"], "http://testserver/oauth/token")
        bad = self.api.post("/api/v1/mcp", json={}, headers={"authorization": "Bearer nope"})
        self.assertIn('error="invalid_token"', bad.headers["www-authenticate"])

    def test_02_create_client_on_clients_page(self):
        page = self.browser.post("/clients/oauth", data={"name": "Claude"}).text
        type(self).client_id = re.search(r'id="cid" value="(gmc_[^"]+)"', page).group(1)
        type(self).secret = re.search(r'id="csecret" value="(gms_[^"]+)"', page).group(1)
        self.assertIn('value="http://testserver/api/v1/mcp"', page)
        self.assertNotIn(self.secret, self.browser.get("/clients").text)  # shown once
        bad = self.browser.post("/clients/oauth", data={"name": "x", "redirects": "http://evil.example/cb"})
        self.assertEqual(bad.status_code, 400)

    def test_03_bad_requests_never_redirect(self):
        _, challenge = pkce()
        r = self.authorize(self.browser, challenge, client_id="gmc_unknown")
        self.assertEqual(r.status_code, 400)
        r = self.authorize(self.browser, challenge, redirect_uri="https://evil.example/cb")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("location", r.headers)
        r = self.authorize(self.browser, challenge, code_challenge_method="plain")
        self.assertIn("error=invalid_request", r.headers["location"])
        self.assertIn("state=xyz", r.headers["location"])

    def test_04_sign_in_then_back_to_approval(self):
        _, challenge = pkce()
        with TestClient(app, follow_redirects=False) as fresh:
            r = self.authorize(fresh, challenge)
            self.assertEqual(r.headers["location"], "/login")
            r = sign_in(fresh, self.passkey)
            self.assertTrue(r.json()["redirect"].startswith("/oauth/authorize?"), r.text)
            self.assertIn("Connect Claude?", fresh.get(r.json()["redirect"]).text)

    def test_05_code_exchange(self):
        verifier, challenge = pkce()
        q = self.approve(self.browser, challenge)
        self.assertEqual((q["state"], q["iss"]), (["xyz"], ["http://testserver"]))
        code = q["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier}
        self.assertEqual(self.token(auth=(self.client_id, "gms_wrong"), **form).status_code, 401)
        self.assertEqual(self.token(**{**form, "code_verifier": pkce()[0]}).json()["error"], "invalid_grant")
        self.assertEqual(self.token(**{**form, "redirect_uri": "http://localhost:1/cb"}).json()["error"],
                         "invalid_grant")
        r = self.token(**form)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["cache-control"], "no-store")
        tok = r.json()
        self.assertEqual((tok["token_type"], tok["scope"], tok["expires_in"]), ("Bearer", "market:read", 3600))
        # The code is single use; replaying it also revokes what it issued (it may have leaked).
        r = self.api.post("/oauth/token", data={**form, "client_id": self.client_id, "client_secret": self.secret})
        self.assertEqual(r.json()["error"], "invalid_grant")
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        h = {"authorization": f"Bearer {tok['access_token']}"}
        self.assertEqual(self.api.post("/api/v1/mcp", json=ping, headers=h).status_code, 401)
        # A clean run, with client_secret_post this time.
        verifier, challenge = pkce()
        code = self.approve(self.browser, challenge)["code"][0]
        r = self.api.post("/oauth/token", data={**form, "code": code, "code_verifier": verifier,
                                                "client_id": self.client_id, "client_secret": self.secret})
        self.assertEqual(r.status_code, 200, r.text)
        type(self).tokens = r.json()

    def test_06_token_is_market_only(self):
        h = {"authorization": f"Bearer {self.tokens['access_token']}"}
        r = self.api.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(len(r.json()["result"]["tools"]), 3)
        self.assertEqual(self.api.get("/api/v1/me", headers=h).status_code, 401)
        self.assertEqual(self.api.get("/api/v1/export.csv", headers=h).status_code, 401)

    def test_07_refresh_rotates_and_detects_reuse(self):
        old = self.tokens["refresh_token"]
        r = self.token(grant_type="refresh_token", refresh_token=old)
        self.assertEqual(r.status_code, 200, r.text)
        new = r.json()
        self.assertNotEqual(new["refresh_token"], old)
        h = {"authorization": f"Bearer {new['access_token']}"}
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.api.post("/api/v1/mcp", json=ping, headers=h).status_code, 200)
        self.assertEqual(self.token(grant_type="refresh_token", refresh_token=old).json()["error"], "invalid_grant")
        self.assertEqual(self.api.post("/api/v1/mcp", json=ping, headers=h).status_code, 401)  # reuse revoked all

    def test_08_deny(self):
        q = self.approve(self.browser, pkce()[1], decision="deny")
        self.assertEqual(q["error"], ["access_denied"])
        self.assertNotIn("code", q)

    def test_09_revoking_the_client_kills_its_tokens(self):
        verifier, challenge = pkce()
        code = self.approve(self.browser, challenge)["code"][0]
        tok = self.token(grant_type="authorization_code", code=code, redirect_uri=CALLBACK,
                         code_verifier=verifier).json()
        h = {"authorization": f"Bearer {tok['access_token']}"}
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.api.post("/api/v1/mcp", json=ping, headers=h).status_code, 200)
        self.browser.post(f"/clients/oauth/{self.client_id}/revoke")
        self.assertEqual(self.api.post("/api/v1/mcp", json=ping, headers=h).status_code, 401)
        self.assertEqual(self.token(grant_type="refresh_token", refresh_token=tok["refresh_token"]).status_code, 401)

    def test_10_loopback_redirects_and_safe_next(self):
        from goldmine_server import oauth
        client = {"redirect_uris": '["https://claude.ai/api/mcp/auth_callback"]'}
        self.assertTrue(oauth.redirect_allowed(client, "http://localhost:53682/callback"))
        self.assertTrue(oauth.redirect_allowed(client, "http://127.0.0.1:8000/cb"))
        self.assertFalse(oauth.redirect_allowed(client, "https://localhost.evil.example/cb"))
        self.assertFalse(oauth.redirect_allowed(client, "https://claude.ai/other"))
        from goldmine_server import app as app_module
        self.assertEqual(app_module.after_login(mock.Mock(session={"next": "//evil.example/x"})), "/")
        self.assertEqual(app_module.after_login(mock.Mock(session={"next": "/oauth/authorize?a=1"})),
                         "/oauth/authorize?a=1")


if __name__ == "__main__":
    unittest.main()
