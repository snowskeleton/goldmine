"""OAuth 2.1 authorization server for MCP connectors (Claude's Connectors, other MCP clients).

A connector can't send a fixed API key; it discovers sign-in from the MCP endpoint's 401
(WWW-Authenticate -> protected-resource metadata -> authorization-server metadata), sends you
to /oauth/authorize, where you sign in with your passkey and approve it, and then trades the
code for tokens at /oauth/token.

  * Clients are created on the Clients page: a client ID + secret to paste into the connector's
    advanced settings. There's no dynamic registration, so only clients you made can ask.
  * Authorization code flow only, PKCE (S256) required, codes single use for 10 minutes.
  * Tokens carry one scope, market:read: the read-only market API and MCP tools, nothing else
    (auth.market_user). Access tokens last an hour; refresh tokens 30 days and rotate on use.
    Reusing an old refresh token revokes that client's tokens for you (it was probably stolen).
  * Only hashes of secrets, codes and tokens are stored. Revoking a client kills its tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import unquote_plus, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import auth, db
from .auth import User

SCOPE = "market:read"
MCP_PATH = "/api/v1/mcp"
CODE_TTL = 600
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
# Claude's connector callbacks (claude.ai and claude.com); loopback URIs are always allowed too.
DEFAULT_REDIRECTS = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")

router = APIRouter()


# ---------------------------------------------------------------------------
# Clients (managed on the Clients page)
# ---------------------------------------------------------------------------

def parse_redirects(text: str) -> list[str]:
    uris = [u.strip() for u in (text or "").replace(",", "\n").splitlines() if u.strip()]
    for u in uris:
        p = urlsplit(u)
        if p.fragment or not p.hostname or p.scheme not in ("https", "http") or \
                (p.scheme == "http" and p.hostname not in LOOPBACK):
            raise HTTPException(400, f"redirect URI must be https (or http on localhost): {u}")
    return uris or list(DEFAULT_REDIRECTS)


def create_client(user: User, name: str, redirects: str = "") -> tuple[str, str]:
    """-> (client_id, client_secret). The secret is shown once; only its hash is kept."""
    client_id, secret = db.new_token("gmc"), db.new_token("gms")
    with db.server_conn() as conn:
        conn.execute("INSERT INTO oauth_clients (client_id, secret_hash, user_id, name, redirect_uris, created) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (client_id, db.hash_token(secret), user.id,
                                                   name.strip()[:60] or "Claude", json.dumps(parse_redirects(redirects)),
                                                   db.now()))
    return client_id, secret


def list_clients(user: User) -> list[dict]:
    with db.server_conn() as conn:
        rows = conn.execute("""
            SELECT c.*, (SELECT COUNT(*) FROM oauth_tokens t WHERE t.client_id = c.client_id AND t.kind = 'refresh'
                         AND t.revoked IS NULL AND t.expires > ?) AS active
            FROM oauth_clients c WHERE c.user_id = ? ORDER BY c.revoked IS NOT NULL, c.created DESC""",
                            (db.now(), user.id)).fetchall()
    return [dict(r) | {"redirects": json.loads(r["redirect_uris"])} for r in rows]


def revoke_client(user: User, client_id: str) -> None:
    with db.server_conn() as conn:
        conn.execute("UPDATE oauth_clients SET revoked = ? WHERE client_id = ? AND user_id = ?",
                     (db.now(), client_id, user.id))
        conn.execute("UPDATE oauth_tokens SET revoked = ? WHERE client_id = ? AND revoked IS NULL",
                     (db.now(), client_id))


def _client(conn, client_id: str):
    return conn.execute("SELECT * FROM oauth_clients WHERE client_id = ? AND revoked IS NULL",
                        (client_id or "",)).fetchone()


def redirect_allowed(client, uri: str) -> bool:
    allowed = json.loads(client["redirect_uris"])
    if uri in allowed:
        return True
    p = urlsplit(uri)
    # Native apps (Claude Code, MCP inspectors) listen on a random loopback port (RFC 8252).
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1") and not p.fragment


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _resource_metadata(request: Request) -> dict:
    origin = auth.origin_for(request)
    return {"resource": origin + MCP_PATH, "authorization_servers": [origin], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_name": "Goldmine market data"}


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
def protected_resource(request: Request, path: str = ""):
    return _resource_metadata(request)


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{path:path}")
def authorization_server(request: Request, path: str = ""):
    origin = auth.origin_for(request)
    return {"issuer": origin, "authorization_endpoint": f"{origin}/oauth/authorize",
            "token_endpoint": f"{origin}/oauth/token", "revocation_endpoint": f"{origin}/oauth/revoke",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": [SCOPE],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]}


# ---------------------------------------------------------------------------
# Authorization (browser: passkey sign-in, then approve)
# ---------------------------------------------------------------------------

def _back_to_client(redirect_uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(redirect_uri + sep + urlencode({k: v for k, v in params.items() if v is not None}),
                            status_code=303)


@router.get("/oauth/authorize")
def authorize(request: Request, client_id: str = "", redirect_uri: str = "", response_type: str = "",
              code_challenge: str = "", code_challenge_method: str = "", state: str | None = None,
              scope: str = "", resource: str | None = None):
    from .app import page  # the web UI's page renderer (app imports this module)
    with db.server_conn() as conn:
        client = _client(conn, client_id)
    # Until the client and redirect URI check out, errors are shown here, never sent anywhere.
    if client is None:
        raise HTTPException(400, "unknown or revoked OAuth client; create one on the Clients page")
    if not redirect_allowed(client, redirect_uri):
        raise HTTPException(400, f"redirect URI not registered for {client['name']}: {redirect_uri}")
    iss = auth.origin_for(request)
    if response_type != "code":
        return _back_to_client(redirect_uri, {"error": "unsupported_response_type", "state": state, "iss": iss})
    if not code_challenge or code_challenge_method != "S256":
        return _back_to_client(redirect_uri, {"error": "invalid_request", "state": state, "iss": iss,
                                              "error_description": "PKCE with S256 is required"})
    user = auth.session_user(request)
    if user is None:
        request.session["next"] = str(request.url.path) + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse("/login", status_code=303)
    if client["user_id"] != user.id:
        raise HTTPException(403, "this OAuth client belongs to another account")
    nonce = secrets.token_urlsafe(16)
    request.session["oauth"] = {"nonce": nonce, "client_id": client_id, "redirect_uri": redirect_uri,
                                "challenge": code_challenge, "state": state, "resource": resource}
    return page(request, "oauth_consent.html", user, client=client, nonce=nonce,
                redirect_host=urlsplit(redirect_uri).netloc)


@router.post("/oauth/authorize")
def authorize_decision(request: Request, nonce: str = Form(...), decision: str = Form(...),
                       user: User = Depends(auth.require_user)):
    pending = request.session.pop("oauth", None)
    if not pending or not secrets.compare_digest(pending["nonce"], nonce):
        raise HTTPException(400, "this sign-in request expired; start again from the app")
    iss = auth.origin_for(request)
    if decision != "allow":
        return _back_to_client(pending["redirect_uri"], {"error": "access_denied", "state": pending["state"],
                                                          "iss": iss})
    code = secrets.token_urlsafe(32)
    with db.server_conn() as conn:
        if _client(conn, pending["client_id"]) is None:
            raise HTTPException(400, "this OAuth client was revoked")
        conn.execute("INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri, code_challenge, scope, "
                     "resource, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (db.hash_token(code), pending["client_id"], user.id, pending["redirect_uri"],
                      pending["challenge"], SCOPE, pending["resource"], db.now() + CODE_TTL))
    return _back_to_client(pending["redirect_uri"], {"code": code, "state": pending["state"], "iss": iss})


# ---------------------------------------------------------------------------
# Token endpoint (server to server)
# ---------------------------------------------------------------------------

def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="goldmine"'
    return JSONResponse({"error": error, "error_description": description}, status_code=status, headers=headers)


def _client_auth(request: Request, form) -> tuple[str | None, str | None]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
            return unquote_plus(cid), unquote_plus(secret)
        except (ValueError, UnicodeDecodeError):
            return None, None
    return form.get("client_id"), form.get("client_secret")


def _issue(conn, client_id: str, user_id: int, scope: str) -> dict:
    access, refresh = db.new_token("gma"), db.new_token("gmr")
    now = db.now()
    conn.executemany("INSERT INTO oauth_tokens (token_hash, kind, client_id, user_id, scope, created, expires) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(db.hash_token(access), "access", client_id, user_id, scope, now, now + ACCESS_TTL),
                      (db.hash_token(refresh), "refresh", client_id, user_id, scope, now, now + REFRESH_TTL)])
    conn.execute("UPDATE oauth_clients SET last_used = ? WHERE client_id = ?", (now, client_id))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh,
            "scope": scope}


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return 43 <= len(verifier) <= 128 and secrets.compare_digest(digest, challenge)


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    client_id, secret = _client_auth(request, form)
    with db.server_conn() as conn:
        client = _client(conn, client_id)
        if client is None or not secret or not secrets.compare_digest(client["secret_hash"], db.hash_token(secret)):
            return _oauth_error("invalid_client", "unknown client or wrong secret", 401)
        grant = form.get("grant_type")
        now = db.now()
        if grant == "authorization_code":
            code_hash = db.hash_token(form.get("code") or "")
            row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (code_hash,)).fetchone()
            if not row or row["client_id"] != client_id:
                return _oauth_error("invalid_grant", "unknown code")
            if row["used"]:  # replayed code: revoke what it issued
                conn.execute("UPDATE oauth_tokens SET revoked = ? WHERE client_id = ? AND user_id = ? AND created >= ?",
                             (now, client_id, row["user_id"], row["used"]))
                return _oauth_error("invalid_grant", "code already used")
            if row["expires"] < now:
                return _oauth_error("invalid_grant", "code expired")
            if form.get("redirect_uri") != row["redirect_uri"]:
                return _oauth_error("invalid_grant", "redirect_uri doesn't match the authorization request")
            if not _pkce_ok(form.get("code_verifier") or "", row["code_challenge"]):
                return _oauth_error("invalid_grant", "PKCE verification failed")
            conn.execute("UPDATE oauth_codes SET used = ? WHERE code_hash = ?", (now, code_hash))
            body = _issue(conn, client_id, row["user_id"], row["scope"])
        elif grant == "refresh_token":
            token_hash = db.hash_token(form.get("refresh_token") or "")
            row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                               (token_hash,)).fetchone()
            if not row or row["client_id"] != client_id:
                return _oauth_error("invalid_grant", "unknown refresh token")
            if row["revoked"]:  # an old, rotated-out token came back: assume it leaked
                conn.execute("UPDATE oauth_tokens SET revoked = ? WHERE client_id = ? AND user_id = ? "
                             "AND revoked IS NULL", (now, client_id, row["user_id"]))
                return _oauth_error("invalid_grant", "refresh token was already used")
            if row["expires"] < now:
                return _oauth_error("invalid_grant", "refresh token expired")
            conn.execute("UPDATE oauth_tokens SET revoked = ? WHERE token_hash = ?", (now, token_hash))
            body = _issue(conn, client_id, row["user_id"], row["scope"])
        else:
            return _oauth_error("unsupported_grant_type", f"grant_type {grant!r} isn't supported")
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/revoke")
async def revoke(request: Request):
    """RFC 7009: always 200, whether or not the token existed."""
    form = await request.form()
    client_id, secret = _client_auth(request, form)
    with db.server_conn() as conn:
        client = _client(conn, client_id)
        if client is None or not secret or not secrets.compare_digest(client["secret_hash"], db.hash_token(secret)):
            return _oauth_error("invalid_client", "unknown client or wrong secret", 401)
        conn.execute("UPDATE oauth_tokens SET revoked = ? WHERE token_hash = ? AND client_id = ? AND revoked IS NULL",
                     (db.now(), db.hash_token(form.get("token") or ""), client_id))
    return JSONResponse({})
