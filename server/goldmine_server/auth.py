"""Passkey (WebAuthn) registration and sign-in, invites, sessions, and client API keys.

Accounts:
  * When no user exists, whoever visits first creates the admin account (deploy, then sign up
    right away).
  * After that, new accounts need an invite link from an admin.
  * Users can add more passkeys to their account from the Account page.

API keys (gm_...) authenticate the desktop app: shown once on the Clients page, next to the
server's address, and sent as a Bearer token, so they never appear in request lines or proxy logs.
Only their SHA-256 hash is stored. MCP connectors use OAuth instead (oauth.py); their access
tokens only reach the read-only market data (market_user).
"""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import db, settings

INVITE_DAYS = 7


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def origin_for(request: Request) -> str:
    if settings.ORIGIN:
        return settings.ORIGIN
    return f"{request.url.scheme}://{request.url.netloc}"


def rp_id_for(request: Request) -> str:
    return urlsplit(origin_for(request)).hostname or "localhost"


@dataclass
class User:
    id: int
    name: str
    is_admin: bool


def _user(row) -> User | None:
    return User(row["id"], row["name"], bool(row["is_admin"])) if row else None


def user_count() -> int:
    with db.server_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def session_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    with db.server_conn() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def require_user(request: Request) -> User:
    user = session_user(request)
    if not user:
        raise HTTPException(401, "sign in first")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(403, "admins only")
    return user


# ---------------------------------------------------------------------------
# Registration (first user, invited users, and extra passkeys for signed-in users)
# ---------------------------------------------------------------------------

def registration_options(request: Request, name: str, mode: str, invite: str | None = None) -> str:
    """mode: setup (first user) | invite (new user with invite token) | add (signed-in user)."""
    name = (name or "").strip()
    with db.server_conn() as conn:
        if mode == "add":
            user = require_user(request)
            row = conn.execute("SELECT handle, name FROM users WHERE id = ?", (user.id,)).fetchone()
            handle, name = row["handle"], row["name"]
            exclude = [PublicKeyCredentialDescriptor(id=r["id"]) for r in
                       conn.execute("SELECT id FROM credentials WHERE user_id = ?", (user.id,))]
        else:
            if not 1 <= len(name) <= 40:
                raise HTTPException(400, "pick a name (1-40 characters)")
            if mode == "setup" and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise HTTPException(403, "this server already has users; ask an admin for an invite")
            if mode == "invite":
                _check_invite(conn, invite)
            elif mode != "setup":
                raise HTTPException(400, "bad mode")
            if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise HTTPException(400, "that name is taken")
            handle, exclude = secrets.token_bytes(16), []
    options = generate_registration_options(
        rp_id=rp_id_for(request), rp_name=settings.APP_NAME, user_id=handle, user_name=name,
        user_display_name=name, exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED))
    request.session["reg"] = {"challenge": b64url(options.challenge), "handle": b64url(handle),
                              "name": name, "mode": mode, "invite": invite}
    return options_to_json(options)


def finish_registration(request: Request, credential: dict, passkey_name: str | None = None) -> User:
    pending = request.session.pop("reg", None)
    if not pending:
        raise HTTPException(400, "registration expired; try again")
    try:
        verified = verify_registration_response(
            credential=credential, expected_challenge=unb64url(pending["challenge"]),
            expected_origin=origin_for(request), expected_rp_id=rp_id_for(request))
    except Exception as exc:  # the library raises several exception types for bad responses
        raise HTTPException(400, f"passkey registration failed: {exc}") from exc
    transports = json.dumps((credential.get("response") or {}).get("transports") or [])
    with db.server_conn() as conn:
        mode = pending["mode"]
        if mode == "add":
            user = require_user(request)
            uid = user.id
        else:
            if mode == "setup" and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise HTTPException(403, "someone else finished setup first")
            if mode == "invite":
                _check_invite(conn, pending["invite"])
            uid = conn.execute("INSERT INTO users (name, handle, is_admin, created) VALUES (?, ?, ?, ?)",
                               (pending["name"], unb64url(pending["handle"]), int(mode == "setup"), db.now())).lastrowid
            if mode == "invite":
                conn.execute("UPDATE invites SET used_by = ?, used = ? WHERE token_hash = ?",
                             (uid, db.now(), db.hash_token(pending["invite"])))
        conn.execute("INSERT INTO credentials (id, user_id, public_key, sign_count, transports, name, created) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (verified.credential_id, uid, verified.credential_public_key, verified.sign_count,
                      transports, (passkey_name or "").strip()[:60] or "Passkey", db.now()))
        user = _user(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    request.session["uid"] = user.id
    return user


# ---------------------------------------------------------------------------
# Sign-in (discoverable credentials: no username needed)
# ---------------------------------------------------------------------------

def authentication_options(request: Request) -> str:
    options = generate_authentication_options(rp_id=rp_id_for(request),
                                              user_verification=UserVerificationRequirement.PREFERRED)
    request.session["auth"] = b64url(options.challenge)
    return options_to_json(options)


def finish_authentication(request: Request, credential: dict) -> User:
    challenge = request.session.pop("auth", None)
    if not challenge:
        raise HTTPException(400, "sign-in expired; try again")
    try:
        cred_id = unb64url(credential["rawId"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "malformed credential") from exc
    with db.server_conn() as conn:
        row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        if not row:
            raise HTTPException(400, "unknown passkey for this server")
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=unb64url(challenge),
                expected_rp_id=rp_id_for(request), expected_origin=origin_for(request),
                credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"])
        except Exception as exc:
            raise HTTPException(400, f"sign-in failed: {exc}") from exc
        conn.execute("UPDATE credentials SET sign_count = ?, last_used = ? WHERE id = ?",
                     (verified.new_sign_count, db.now(), cred_id))
        user = _user(conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone())
    request.session.clear()
    request.session["uid"] = user.id
    return user


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def create_invite(admin: User, note: str = "") -> str:
    token = db.new_token("inv")
    with db.server_conn() as conn:
        conn.execute("INSERT INTO invites (token_hash, created_by, note, created, expires) VALUES (?, ?, ?, ?, ?)",
                     (db.hash_token(token), admin.id, note.strip()[:80], db.now(), db.now() + INVITE_DAYS * 86400))
    return token


def _check_invite(conn, token: str | None) -> None:
    row = conn.execute("SELECT * FROM invites WHERE token_hash = ?", (db.hash_token(token or ""),)).fetchone()
    if not row or row["used"] or row["expires"] < db.now():
        raise HTTPException(403, "this invite link is invalid, used, or expired")


def invite_valid(token: str) -> bool:
    with db.server_conn() as conn:
        try:
            _check_invite(conn, token)
            return True
        except HTTPException:
            return False


# ---------------------------------------------------------------------------
# Client keys
# ---------------------------------------------------------------------------

def create_client_key(user: User, name: str) -> str:
    key = db.new_token("gm")
    with db.server_conn() as conn:
        conn.execute("INSERT INTO client_keys (user_id, name, key_hash, prefix, created) VALUES (?, ?, ?, ?, ?)",
                     (user.id, name.strip()[:60] or "Desktop app", db.hash_token(key), key[:10], db.now()))
    return key


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def _key_user(conn, request: Request, key_hash: str) -> User | None:
    row = conn.execute("SELECT u.id AS uid, u.name, u.is_admin FROM client_keys k JOIN users u ON u.id = k.user_id "
                       "WHERE k.key_hash = ? AND k.revoked IS NULL", (key_hash,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE client_keys SET last_used = ?, last_ip = ? WHERE key_hash = ?",
                 (db.now(), request.client.host if request.client else None, key_hash))
    return User(row["uid"], row["name"], bool(row["is_admin"]))


def client_user(request: Request) -> User:
    """Authenticate an API request by its Bearer API key (full access to the client API)."""
    token = _bearer(request)
    if not token:
        raise HTTPException(401, "missing API key")
    with db.server_conn() as conn:
        user = _key_user(conn, request, db.hash_token(token))
    if not user:
        raise HTTPException(401, "invalid or revoked API key")
    return user


def market_user(request: Request) -> User:
    """Read-only market data and MCP: an API key, or an OAuth access token (connectors). A 401
    points OAuth clients at the protected-resource metadata, which is how they discover sign-in."""
    token = _bearer(request)
    user = None
    if token:
        token_hash = db.hash_token(token)
        with db.server_conn() as conn:
            user = _key_user(conn, request, token_hash)
            if user is None:
                row = conn.execute("""SELECT u.id, u.name, u.is_admin FROM oauth_tokens t
                                      JOIN users u ON u.id = t.user_id
                                      JOIN oauth_clients c ON c.client_id = t.client_id
                                      WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked IS NULL
                                        AND t.expires > ? AND c.revoked IS NULL""", (token_hash, db.now())).fetchone()
                user = _user(row)
    if user is None:
        meta = f"{origin_for(request)}/.well-known/oauth-protected-resource{request.url.path}"
        error = ', error="invalid_token"' if token else ""
        raise HTTPException(401, "invalid or expired token" if token else "missing API key or access token",
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"{error}'})
    return user
