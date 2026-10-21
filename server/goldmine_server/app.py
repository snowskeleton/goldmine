"""Goldmine server: web UI (passkey sign-in) + client API (Bearer client keys) + collector."""

from __future__ import annotations

import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, ledger, market, marketdata, mcp_server, oauth, releases, services, settings, tasks
from .auth import User
from .collector import collector, load_config


HERE = Path(__file__).resolve().parent
VERSION = "0.2.0"


def secret_key() -> str:
    if os.environ.get("GOLDMINE_SECRET"):
        return os.environ["GOLDMINE_SECRET"]
    path = settings.DATA / "secret.key"
    settings.DATA.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.market_conn().close()  # create/migrate the market DB up front
    if settings.COLLECTOR:
        collector.start()
    yield
    collector.stop()


app = FastAPI(title="Goldmine", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=secret_key(), session_cookie="goldmine_session",
                   max_age=30 * 86400, same_site="lax",
                   https_only=bool(settings.ORIGIN and settings.ORIGIN.startswith("https")))
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(fmt_gold=ledger.fmt_gold, app_name=settings.APP_NAME, version=VERSION)


def ago(ts) -> str:
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 0:
        return f"in {(-d) // 60} min"
    if d < 90:
        return "just now"
    if d < 5400:
        return f"{d // 60} min ago"
    if d < 172800:
        return f"{d // 3600} h ago"
    return f"{d // 86400} days ago"


templates.env.filters["ago"] = ago
templates.env.filters["bytes"] = lambda n: f"{(n or 0) / 1e6:,.1f} MB" if (n or 0) < 1e9 else f"{n / 1e9:,.2f} GB"


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """Cookie-authenticated POSTs must come from our own pages (SameSite=Lax already blocks most
    cross-site requests; this closes the rest). The client API uses Bearer keys instead, and the
    OAuth token endpoints are called server to server with client credentials."""
    if request.method == "POST" and not request.url.path.startswith(("/api/", "/oauth/token", "/oauth/revoke")):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != auth.origin_for(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
    return await call_next(request)


def page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


def signed_in(request: Request) -> User | None:
    return auth.session_user(request)


def to_signin(request: Request) -> RedirectResponse:
    return RedirectResponse("/setup" if auth.user_count() == 0 else "/login", status_code=303)


# ---------------------------------------------------------------------------
# Sign-in pages and passkey endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if auth.user_count():
        return RedirectResponse("/login", status_code=303)
    return page(request, "register.html", None, mode="setup", invite=None)


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    return page(request, "register.html", signed_in(request), mode="invite", invite=token,
                valid=auth.invite_valid(token))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if signed_in(request):
        return RedirectResponse(after_login(request), status_code=303)
    return page(request, "login.html", None)


def after_login(request: Request) -> str:
    """Where to go after signing in: back to an OAuth approval that sent us here, else home."""
    nxt = request.session.pop("next", None) or "/"
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


@app.post("/auth/register/options")
def register_options(request: Request, body: dict = Body(...)):
    return Response(auth.registration_options(request, body.get("name", ""), body.get("mode", ""),
                                              body.get("invite")), media_type="application/json")


@app.post("/auth/register/verify")
def register_verify(request: Request, body: dict = Body(...)):
    auth.finish_registration(request, body.get("credential") or {}, body.get("passkeyName"))
    return {"ok": True, "redirect": "/account" if body.get("mode") == "add" else "/"}


@app.post("/auth/login/options")
def login_options(request: Request):
    return Response(auth.authentication_options(request), media_type="application/json")


@app.post("/auth/login/verify")
def login_verify(request: Request, body: dict = Body(...)):
    nxt = after_login(request)  # read before sign-in resets the session
    auth.finish_authentication(request, body.get("credential") or {})
    return {"ok": True, "redirect": nxt}


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = signed_in(request)
    if not user:
        return to_signin(request)
    st = services.status(user.id)
    cfg = load_config()
    return page(request, "dashboard.html", user, status=st, collector=collector,
                log=list(collector.log)[-60:][::-1], configured=bool((cfg.get("api") or {}).get("client_id")),
                enabled=collector.settings(cfg)[0])


@app.post("/collect")
def collect_now(request: Request, user: User = Depends(auth.require_admin)):
    collector.trigger()
    return RedirectResponse("/", status_code=303)


REPORT_KINDS = [("crafts", "Crafts"), ("deals", "Deals"), ("flips", "Flips"), ("timing", "Timing"),
                ("summary", "Ledger"), ("price", "Price history")]


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, kind: str = "crafts", item: str = "", days: int = 7, hours: int = 72):
    user = signed_in(request)
    if not user:
        return to_signin(request)
    if kind not in dict(REPORT_KINDS):
        kind = "crafts"
    hours = max(1, min(hours, 24 * 90))
    data = None
    if kind in ("crafts", "deals", "flips", "summary") or (kind == "price" and item):
        kw = {"days": days} if kind in ("summary", "flips") else {"item": item, "hours": hours} if kind == "price" \
            else {"history": True} if kind == "deals" else {}
        data = services.report_data(user.id, kind, **kw)
    return page(request, "reports.html", user, kinds=REPORT_KINDS, kind=kind, item=item, days=days, hours=hours,
                data=data, gold=services.reports.gold, wowhead=services.reports.wowhead_url,
                wowhead_data=services.reports.wowhead_data)


@app.get("/web/timing")
def web_timing(request: Request, item: str, user: User = Depends(auth.require_user)):
    return services.timing_json(user.id, item)


# ---------------------------------------------------------------------------
# Admin: markets and settings
# ---------------------------------------------------------------------------

def save_config(cfg: dict) -> None:
    settings.DATA.mkdir(parents=True, exist_ok=True)
    tmp = settings.CONFIG.with_suffix(".tmp")
    tmp.write_text(ledger.dump_toml(cfg))
    tmp.chmod(0o600)
    market.load_config(tmp)  # refuse to save something that doesn't parse
    tmp.replace(settings.CONFIG)


# The realm list takes several paged Blizzard API calls and hardly ever changes, so the Markets
# page doesn't wait on them every time it loads.
REALMS_TTL = 6 * 3600
_realms_cache: dict[tuple[str, str], tuple[float, list]] = {}


def realms_for(api: dict, region: str, namespace: str) -> list[dict]:
    hit = _realms_cache.get((region, namespace))
    if hit and time.time() - hit[0] < REALMS_TTL:
        return hit[1]
    client = market.Client(api.get("client_id", ""), api.get("client_secret", ""), cache_dir=settings.CACHE_DIR)
    realms = ledger.list_realms(client, region, namespace)
    _realms_cache[(region, namespace)] = (time.time(), realms)
    return realms


@app.get("/markets", response_class=HTMLResponse)
def markets_page(request: Request, region: str = "us", game: str = "", namespace: str = "",
                 user: User = Depends(auth.require_admin)):
    prefixes = {g["game"]: g["prefix"] for g in market.NAMESPACE_GAMES}
    namespace = namespace.strip()
    if game in prefixes:
        namespace = f"{prefixes[game]}-{region}"
    elif not game:  # plain ?namespace= links still work; preselect the matching game
        game = next((k for k, p in prefixes.items() if namespace == f"{p}-{region}"), "other" if namespace else "retail")
    cfg = load_config()
    realms, error = [], None
    if namespace:
        api = cfg.get("api") or {}
        try:
            realms = realms_for(api, region, namespace)
        except Exception as exc:
            error = str(exc)
    conn = db.market_conn()
    try:
        rows = services.market_status(conn)
    finally:
        conn.close()
    status = {m["name"].split(":")[0]: m for m in rows}
    for m in rows:  # an all-realms entry shows the totals of the realms it expanded to
        if m["source"]:
            st = status.setdefault(m["source"], {"snapshots": 0, "last_modified": None, "realms": 0})
            st["snapshots"] += m["snapshots"]
            st["realms"] += 1
            st["last_modified"] = max(filter(None, (st["last_modified"], m["last_modified"])), default=None)
    return page(request, "markets.html", user, markets=cfg.get("market", []), status=status, realms=realms,
                region=region, namespace=namespace, game=game, namespace_games=market.NAMESPACE_GAMES,
                error=error, game_for=market.game_for)


@app.post("/markets/add")
def markets_add(request: Request, name: str = Form(...), region: str = Form("us"), namespace: str = Form(...),
                kind: str = Form(...), realm: str = Form(""), game: str = Form(""),
                user: User = Depends(auth.require_admin)):
    cfg = load_config()
    entry = {"name": name.strip(), "region": region.strip().lower(), "namespace": namespace.strip(), "kind": kind}
    if kind not in ("commodities", "all-realms"):
        if not realm.strip():
            raise HTTPException(400, "realm markets need a realm slug")
        entry["realm"] = realm.strip()
    if game.strip():
        entry["game"] = game.strip()
    if kind == "all-realms" and not next((g["auctions"] for g in market.NAMESPACE_GAMES
                                          if g["game"] == market.game_for(entry["namespace"], entry.get("game"))), True):
        raise HTTPException(400, "that game version has no auction house data in the API")
    if kind not in ("commodities", "realm", "classic", "all-realms") or not entry["name"] or not entry["namespace"]:
        raise HTTPException(400, "name, namespace and a valid kind are required")
    markets = [m for m in cfg.get("market", []) if m.get("name") != entry["name"]]
    cfg["market"] = markets + [entry]
    save_config(cfg)
    return RedirectResponse("/markets", status_code=303)


@app.post("/markets/remove")
def markets_remove(request: Request, name: str = Form(...), user: User = Depends(auth.require_admin)):
    cfg = load_config()
    cfg["market"] = [m for m in cfg.get("market", []) if m.get("name") != name]
    save_config(cfg)
    return RedirectResponse("/markets", status_code=303)


@app.post("/markets/pricing")
def markets_pricing(request: Request, name: str = Form(...), user: User = Depends(auth.require_admin)):
    cfg = load_config()
    for m in cfg.get("market", []):
        if m.get("name") == name:
            if m.get("pricing", True):
                m["pricing"] = False
            else:
                m.pop("pricing", None)
    save_config(cfg)
    return RedirectResponse("/markets", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(auth.require_admin)):
    cfg = load_config()
    return page(request, "settings.html", user, api=cfg.get("api") or {}, app_cfg=cfg.get("app") or {},
                storage=services.storage_breakdown(), data_dir=str(settings.DATA))


@app.post("/settings")
def settings_save(request: Request, client_id: str = Form(""), client_secret: str = Form(""),
                  collect_minutes: int = Form(20), auto_collect: str = Form(""),
                  user: User = Depends(auth.require_admin)):
    cfg = load_config()
    api = cfg.setdefault("api", {})
    api["client_id"] = client_id.strip()
    if client_secret.strip():  # blank = keep the current secret
        api["client_secret"] = client_secret.strip()
    app_cfg = cfg.setdefault("app", {})
    app_cfg["collect_minutes"] = max(5, min(int(collect_minutes), 720))
    app_cfg["auto_collect"] = bool(auto_collect)
    save_config(cfg)
    collector.trigger()
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Client keys, users and invites, account
# ---------------------------------------------------------------------------

def my_keys(user: User):
    with db.server_conn() as conn:
        return conn.execute("SELECT * FROM client_keys WHERE user_id = ? ORDER BY revoked IS NOT NULL, created DESC",
                            (user.id,)).fetchall()


def clients_context(request: Request, user: User, **new) -> dict:
    origin = auth.origin_for(request)
    return {"keys": my_keys(user), "oauth_clients": oauth.list_clients(user), "server_url": origin,
            "mcp_url": origin + oauth.MCP_PATH, "default_redirects": oauth.DEFAULT_REDIRECTS,
            "downloads": releases.manifest(), "new_key": None, "new_client": None, **new}


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, user: User = Depends(auth.require_user)):
    return page(request, "clients.html", user, **clients_context(request, user))


@app.post("/clients", response_class=HTMLResponse)
def clients_create(request: Request, name: str = Form("Desktop app"), user: User = Depends(auth.require_user)):
    key = auth.create_client_key(user, name)
    return page(request, "clients.html", user, **clients_context(request, user, new_key=key))


@app.post("/clients/oauth", response_class=HTMLResponse)
def oauth_client_create(request: Request, name: str = Form("Claude"), redirects: str = Form(""),
                        user: User = Depends(auth.require_user)):
    client_id, secret = oauth.create_client(user, name, redirects)
    return page(request, "clients.html", user,
                **clients_context(request, user, new_client={"id": client_id, "secret": secret}))


@app.post("/clients/oauth/{client_id}/revoke")
def oauth_client_revoke(request: Request, client_id: str, user: User = Depends(auth.require_user)):
    oauth.revoke_client(user, client_id)
    return RedirectResponse("/clients", status_code=303)


@app.get("/download/app/{platform}")
def download_app(platform: str, user: User = Depends(auth.require_user)):
    info = releases.app_release(platform)
    return FileResponse(releases.app_file(platform), media_type="application/zip",
                        filename=f"Goldmine-{info['version']}-{platform}.zip")


@app.get("/download/addon")
def download_addon(user: User = Depends(auth.require_user)):
    info, data = releases.addon_zip()
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="Goldmine-addon-{info["version"] or info["hash"][:8]}.zip"'})


@app.post("/clients/{key_id}/revoke")
def clients_revoke(request: Request, key_id: int, user: User = Depends(auth.require_user)):
    with db.server_conn() as conn:
        conn.execute("UPDATE client_keys SET revoked = ? WHERE id = ? AND user_id = ?", (db.now(), key_id, user.id))
    return RedirectResponse("/clients", status_code=303)


def users_context() -> dict:
    with db.server_conn() as conn:
        users = conn.execute("""SELECT u.*, (SELECT COUNT(*) FROM credentials c WHERE c.user_id = u.id) AS passkeys,
                                       (SELECT MAX(last_used) FROM credentials c WHERE c.user_id = u.id) AS last_login
                                FROM users u ORDER BY u.id""").fetchall()
        invites = conn.execute("""SELECT i.*, u.name AS used_by_name FROM invites i
                                  LEFT JOIN users u ON u.id = i.used_by ORDER BY i.created DESC LIMIT 50""").fetchall()
    return {"users": users, "invites": invites, "now": db.now()}


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(auth.require_admin)):
    return page(request, "users.html", user, new_invite=None, **users_context())


@app.post("/users/invite", response_class=HTMLResponse)
def users_invite(request: Request, note: str = Form(""), user: User = Depends(auth.require_admin)):
    token = auth.create_invite(user, note)
    link = f"{auth.origin_for(request)}/invite/{token}"
    return page(request, "users.html", user, new_invite=link, **users_context())


@app.post("/users/invites/revoke")
def invite_revoke(request: Request, token_hash: str = Form(...), user: User = Depends(auth.require_admin)):
    with db.server_conn() as conn:
        conn.execute("DELETE FROM invites WHERE token_hash = ? AND used IS NULL", (token_hash,))
    return RedirectResponse("/users", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(auth.require_user)):
    with db.server_conn() as conn:
        creds = conn.execute("SELECT id, name, created, last_used FROM credentials WHERE user_id = ? ORDER BY created",
                             (user.id,)).fetchall()
    return page(request, "account.html", user, creds=[dict(c) | {"hex": c["id"].hex()} for c in creds])


@app.post("/account/passkeys/remove")
def passkey_remove(request: Request, cred: str = Form(...), user: User = Depends(auth.require_user)):
    with db.server_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM credentials WHERE user_id = ?", (user.id,)).fetchone()[0]
        if n <= 1:
            raise HTTPException(400, "you can't remove your only passkey")
        conn.execute("DELETE FROM credentials WHERE id = ? AND user_id = ?", (bytes.fromhex(cred), user.id))
    return RedirectResponse("/account", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/auth/", "/web/")) or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if exc.status_code == 401:
        return to_signin(request)
    return templates.TemplateResponse(request, "error.html", {"user": signed_in(request), "error": exc.detail},
                                      status_code=exc.status_code)


# ---------------------------------------------------------------------------
# Client API (desktop app), authenticated with a client key
# ---------------------------------------------------------------------------

@app.get("/api/v1/me")
def api_me(user: User = Depends(auth.client_user)):
    return {"user": user.name, "admin": user.is_admin, "server_version": VERSION}


@app.post("/api/v1/game-data")
async def api_game_data(request: Request, source: str = "upload", user: User = Depends(auth.client_user)):
    body = await request.body()
    # Parsing and importing take a while; on the event loop they'd stall every other request.
    return await run_in_threadpool(services.import_saved_variables, user.id, body,
                                   request.headers.get("content-encoding"), source)


@app.get("/api/v1/addon-data")
def api_addon_data(user: User = Depends(auth.client_user)):
    return Response(services.addon_data(user.id), media_type="text/plain; charset=utf-8")


@app.get("/api/v1/addon-tasks")
def api_addon_tasks(user: User = Depends(auth.client_user)):
    """Tasks.lua: the active trader plan for the addon's queue (an empty plan when there is none)."""
    conn = db.user_conn(user.id)
    try:
        return Response(tasks.addon_tasks(conn), media_type="text/plain; charset=utf-8")
    finally:
        conn.close()


@app.get("/api/v1/status")
def api_status(user: User = Depends(auth.client_user)):
    return services.status(user.id)


@app.get("/api/v1/report/{kind}")
def api_report(kind: str, item: str | None = None, days: int | None = None, limit: int = 40, utc_offset: int = 0,
               format: str = "text", user: User = Depends(auth.client_user)):
    """A report as plain text (what the desktop client prints), or with format=json as structured rows."""
    extra = {"utc_offset": max(-12, min(utc_offset, 14))} if kind in ("timing", "flips") else {}
    if format == "json":
        return services.report_data(user.id, kind, item=item, days=days, limit=limit, **extra)
    return PlainTextResponse(services.report(user.id, kind, item=item, days=days, limit=limit, **extra))


@app.get("/api/v1/export.csv")
def api_export(since: int | None = None, until: int | None = None, source: str | None = None,
               char: str | None = None, user: User = Depends(auth.client_user)):
    """The user's ledger as CSV. since/until are unix timestamps."""
    return Response(services.export_csv(user.id, since=since, until=until, source=source, char=char),
                    media_type="text/csv; charset=utf-8")


@app.get("/api/v1/namespaces")
def api_namespaces(user: User = Depends(auth.client_user)):
    """The game versions to offer when browsing realms; namespace = prefix + "-" + region."""
    return market.NAMESPACE_GAMES


@app.get("/api/v1/timing")
def api_timing(item: str, user: User = Depends(auth.client_user)):
    return services.timing_json(user.id, item)


# Updates: the addon ships inside this image; app builds are uploaded with an admin client key
# (goldmine.py release-app, or app/build.sh --release).

@app.get("/api/v1/updates")
def api_updates(user: User = Depends(auth.client_user)):
    return releases.manifest()


@app.get("/api/v1/updates/addon.zip")
def api_update_addon(user: User = Depends(auth.client_user)):
    return Response(releases.addon_zip()[1], media_type="application/zip")


@app.get("/api/v1/updates/app/{platform}.zip")
def api_update_app(platform: str, user: User = Depends(auth.client_user)):
    return FileResponse(releases.app_file(platform), media_type="application/zip")


@app.post("/api/v1/releases/app")
async def api_release_app(request: Request, platform: str = "macos", notes: str = "",
                          user: User = Depends(auth.client_user)):
    if not user.is_admin:
        raise HTTPException(403, "admins only")
    body = await request.body()
    return await run_in_threadpool(releases.save_app_release, platform, body, notes)


# Read-only market data (/api/v1/market/*) and the same as MCP tools (/api/v1/mcp).
app.include_router(marketdata.router)
app.include_router(mcp_server.router)
app.include_router(oauth.router)  # sign-in for MCP connectors
mcp_server.SERVER_INFO["version"] = VERSION


def run() -> None:
    import uvicorn
    host = os.environ.get("GOLDMINE_HOST", "0.0.0.0")
    port = int(os.environ.get("GOLDMINE_PORT", "8080"))
    # proxy_headers: trust X-Forwarded-Proto/Host from the reverse proxy in front of the container.
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    run()
