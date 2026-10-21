"""Auction house price collection from the Blizzard Game Data API.

Each run fetches the current snapshot for every configured market and applies it to the
change-based storage in store.py (listing lifecycles + item states written only on change).

Response shapes handled:
  retail commodities  {"auctions": [{"id", "item": {"id"}, "quantity", "unit_price", "time_left"}]}
  retail realm        {"auctions": [{"id", "item": {"id", "pet_species_id"?...}, "buyout"?, "bid"?, "quantity", "time_left"}]}
  classic             same as retail realm, per auction house (/auctions/{houseId})
"""

from __future__ import annotations

import base64
import sys
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from . import archive, store

TOKEN_URL = "https://oauth.battle.net/token"
API_HOST = "https://{region}.api.blizzard.com"

MARKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    name                TEXT PRIMARY KEY,   -- e.g. us-commodities, us-proudmoore:Alliance
    region              TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    kind                TEXT NOT NULL,
    connected_realm_id  INTEGER,
    auction_house_id    INTEGER,
    game                TEXT,               -- retail | classic | classic1x | classicann | ...
    realms              TEXT,               -- JSON list of realm names this AH serves
    source              TEXT,               -- the all-realms config entry it came from; NULL = its own entry
    mid                 INTEGER UNIQUE      -- compact id used by the storage tables
);
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market         TEXT NOT NULL,
    fetched_at     INTEGER NOT NULL,   -- unix seconds
    last_modified  INTEGER,            -- unix seconds, from Blizzard's Last-Modified header
    auctions       INTEGER NOT NULL,
    keyframe       INTEGER NOT NULL DEFAULT 0,  -- 1 = every item's state was written
    UNIQUE (market, last_modified)
);
CREATE TABLE IF NOT EXISTS items (
    item_id  INTEGER PRIMARY KEY,
    name     TEXT,
    quality  TEXT,
    fetched  INTEGER
);
-- Blizzard item API details for items the users own or craft (market.fetch_item_info).
CREATE TABLE IF NOT EXISTS item_info (
    item_id         INTEGER PRIMARY KEY,
    sell_price      INTEGER,            -- vendor sell price, copper
    purchase_price  INTEGER,            -- vendor buy price, copper
    binding         TEXT,               -- ON_ACQUIRE | ON_EQUIP | ON_USE | TO_ACCOUNT | ... (NULL = not bound)
    class_id        INTEGER,
    class_name      TEXT,
    subclass_id     INTEGER,
    subclass_name   TEXT,
    stackable       INTEGER,            -- 1/0
    max_count       INTEGER,            -- unique limit (0 = none); not the stack size
    level           INTEGER,
    inventory_type  TEXT,
    fetched         INTEGER
);
-- WoW token price per region, a row only when Blizzard's price changes.
CREATE TABLE IF NOT EXISTS token_prices (
    region   TEXT NOT NULL,
    updated  INTEGER NOT NULL,          -- unix seconds, Blizzard's last_updated_timestamp
    price    INTEGER NOT NULL,          -- copper
    fetched  INTEGER NOT NULL,
    PRIMARY KEY (region, updated)
);
DROP VIEW IF EXISTS latest_prices;
CREATE VIEW latest_prices AS
SELECT m.name AS market, datetime(s.last_modified, 'unixepoch', 'localtime') AS since,
       c.item_id, c.variant, i.name, c.min_price, c.p10, c.median, c.quantity, c.auctions
FROM item_current c
JOIN markets m ON m.mid = c.market_id
JOIN snapshots s ON s.id = c.snap
LEFT JOIN items i ON i.item_id = c.item_id;
"""


class ApiError(RuntimeError):
    pass


def load_config(path) -> dict:
    try:
        import tomllib
    except ImportError as exc:  # Python < 3.11
        raise SystemExit("Reading config.toml needs Python 3.11+ (tomllib).") from exc
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Client:
    """Minimal Blizzard API client: client-credentials token, namespace header, conditional GETs."""

    client_id: str
    client_secret: str
    cache_dir: Path | None = None
    _tokens: dict = field(default_factory=dict)

    def token(self, region: str) -> str:
        cached = self._tokens.get(region)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        if self.cache_dir:
            path = self.cache_dir / f"token-{region}.json"
            try:
                data = json.loads(path.read_text())
                if data["expires"] > time.time() + 60:
                    self._tokens[region] = (data["token"], data["expires"])
                    return data["token"]
            except (OSError, ValueError, KeyError):
                pass
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL, data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ApiError(f"token request failed: HTTP {exc.code} (check client_id/secret)") from exc
        tok, expires = body["access_token"], time.time() + body.get("expires_in", 3600)
        self._tokens[region] = (tok, expires)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / f"token-{region}.json"
            path.write_text(json.dumps({"token": tok, "expires": expires}))
            path.chmod(0o600)
        return tok

    def get(self, region: str, namespace: str, path: str, params: dict | None = None,
            if_modified_since: int | None = None):
        """Returns (json_or_None, last_modified_unix). None body means 304 Not Modified."""
        query = {"namespace": namespace, "locale": "en_US", **(params or {})}
        url = API_HOST.format(region=region) + path + "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self.token(region)}", "Accept-Encoding": "gzip"}
        if if_modified_since:
            headers["If-Modified-Since"] = formatdate(if_modified_since, usegmt=True)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    lm = resp.headers.get("Last-Modified")
                    return json.loads(raw), int(parsedate_to_datetime(lm).timestamp()) if lm else None
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return None, if_modified_since
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise ApiError(f"GET {path} ({namespace}): HTTP {exc.code}") from exc
        raise ApiError(f"GET {path}: retries exhausted")


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

MIGRATIONS = {"markets": [("game", "TEXT"), ("realms", "TEXT"), ("source", "TEXT")]}


SCHEMA_VERSION = 5  # bump when MARKET_SCHEMA / store.SCHEMA / archive.SCHEMA change


def ensure_schema(conn) -> None:
    # Cheap no-op once applied, so readers don't take write locks on every call.
    if conn.execute("PRAGMA main.user_version").fetchone()[0] >= SCHEMA_VERSION and \
            conn.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'item_current'").fetchone():
        return
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if existing and name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    store.migrate(conn)  # before the view below, which needs item_current
    conn.executescript(MARKET_SCHEMA)
    conn.executescript(store.SCHEMA)
    conn.executescript(archive.SCHEMA)
    conn.execute(f"PRAGMA main.user_version = {SCHEMA_VERSION}")


# The game versions offered when browsing realms (the server's web page and API, and the Mac app via
# the server). The namespace is prefix + "-" + region. Add new game versions here.
NAMESPACE_GAMES = [
    {"game": "retail", "label": "Retail", "prefix": "dynamic", "auctions": True},
    {"game": "classic", "label": "Mists Classic", "prefix": "dynamic-classic", "auctions": True},
    {"game": "classic1x", "label": "Classic Era", "prefix": "dynamic-classic1x", "auctions": False},
    {"game": "classicann", "label": "Anniversary", "prefix": "dynamic-classicann", "auctions": False},
]


def game_for(namespace: str, override: str | None = None) -> str:
    """dynamic-us -> retail, dynamic-classic-us -> classic, dynamic-classic1x-eu -> classic1x."""
    if override:
        return override
    core = namespace.removeprefix("dynamic-").removeprefix("static-")
    core = core.rsplit("-", 1)[0] if "-" in core else ""
    return core or "retail"


def resolve_connected_realm(client: Client, region: str, namespace: str, realm_slug: str) -> int:
    data, _ = client.get(region, namespace, f"/data/wow/realm/{realm_slug}")
    href = data["connected_realm"]["href"]
    return int(href.split("/connected-realm/")[1].split("?")[0])


def connected_realm_names(client: Client, region: str, namespace: str, crid: int) -> list[str]:
    data, _ = client.get(region, namespace, f"/data/wow/connected-realm/{crid}")
    names = []
    for r in data.get("realms", []):
        n = r.get("name")
        names.append(n.get("en_US") if isinstance(n, dict) else n)
    return [n for n in names if n]


def list_realms(client: Client, region: str, namespace: str) -> list[dict]:
    """Every realm in a namespace: connected_realm_id, slug, name, type, category."""
    page, rows = 1, []
    while True:
        data, _ = client.get(region, namespace, "/data/wow/search/connected-realm",
                             {"_page": page, "_pageSize": 100, "orderby": "id"})
        for r in data.get("results", []):
            d = r["data"]
            for realm in d.get("realms", []):
                name = realm.get("name")
                name = name.get("en_US") if isinstance(name, dict) else name
                cat = realm.get("category")
                rows.append({"connected_realm_id": d["id"], "slug": realm.get("slug"), "name": name,
                             "type": (realm.get("type") or {}).get("type"),
                             "category": cat.get("en_US") if isinstance(cat, dict) else cat})
        if page >= data.get("pageCount", 1):
            break
        page += 1
    return sorted(rows, key=lambda r: r["slug"] or "")


# All-realms entries re-list the namespace's connected realms this often (merges and new realms are
# rare), so a collection run doesn't page through the realm search every time.
REALM_LIST_TTL = 6 * 3600
_realm_lists: dict[tuple[str, str], tuple[float, list]] = {}


def _realm_list(client: Client, region: str, namespace: str) -> list[dict]:
    hit = _realm_lists.get((region, namespace))
    if hit and time.time() - hit[0] < REALM_LIST_TTL:
        return hit[1]
    rows = list_realms(client, region, namespace)
    _realm_lists[(region, namespace)] = (time.time(), rows)
    return rows


def expand_all_realms(conn, client: Client, entry: dict, taken: set[int], reserved: set[str]) -> list[dict]:
    """An all-realms config entry -> a realm (classic: per-house) entry for every connected realm
    in its namespace whose id isn't in `taken`. A connected realm that already has a market (say
    one configured on its own and since removed) keeps its name and history; a new one is named
    region-<first slug alphabetically>, plus the id if that name is taken."""
    region, ns = entry["region"], entry["namespace"]
    groups: dict[int, list[dict]] = {}
    for r in _realm_list(client, region, ns):
        groups.setdefault(r["connected_realm_id"], []).append(r)
    named = {crid: name.split(":")[0] for name, crid in conn.execute(
        "SELECT name, connected_realm_id FROM markets WHERE namespace = ? AND connected_realm_id IS NOT NULL "
        "ORDER BY source = ?", (ns, entry["name"]))}  # this entry's own names win
    used = {n.split(":")[0] for n, in conn.execute("SELECT name FROM markets")} | reserved
    kind = "realm" if game_for(ns, entry.get("game")) == "retail" else "classic"
    out = []
    for crid, realms in sorted(groups.items()):
        if crid in taken:
            continue
        name = named.get(crid)
        if name is None:
            name = f"{region}-{min(r['slug'] for r in realms)}"
            if name in used:
                name = f"{name}-{crid}"
            used.add(name)
        out.append({"name": name, "region": region, "namespace": ns, "kind": kind, "crid": crid,
                    "realms": sorted(r["name"] for r in realms if r["name"]), "game": entry.get("game"),
                    "source": entry["name"]})
    return out


def market_targets(conn, client: Client, cfg: dict) -> list[dict]:
    """Expand config markets into concrete endpoints (classic realms expand per auction house;
    all-realms entries into every connected realm not configured on its own)."""
    targets: list[dict] = []

    def add(m: dict) -> None:
        region, ns, kind = m["region"], m["namespace"], m["kind"]
        if kind == "commodities":
            targets.append({"name": m["name"], "region": region, "namespace": ns, "kind": kind,
                            "path": "/data/wow/auctions/commodities", "crid": None, "house": None,
                            "realms": [], "game": game_for(ns, m.get("game"))})
            return
        if m.get("crid"):  # from an all-realms entry
            crid, realms = m["crid"], m["realms"]
        else:
            row = conn.execute("SELECT connected_realm_id, realms FROM markets WHERE (name = ? OR name LIKE ?) "
                               "AND namespace = ?", (m["name"], m["name"] + ":%", ns)).fetchone()
            crid = row[0] if row and row[0] else resolve_connected_realm(client, region, ns, m["realm"])
            realms = json.loads(row[1]) if row and row[1] else connected_realm_names(client, region, ns, crid)
        common = {"region": region, "namespace": ns, "kind": kind, "crid": crid, "realms": realms,
                  "game": m.get("game"), "source": m.get("source")}
        if kind == "realm":
            targets.append({"name": m["name"], "path": f"/data/wow/connected-realm/{crid}/auctions",
                            "house": None, **common})
        elif kind == "classic":
            index, _ = client.get(region, ns, f"/data/wow/connected-realm/{crid}/auctions/index")
            for house in index.get("auctions", []):
                hname = house.get("name") or str(house["id"])
                targets.append({"name": f"{m['name']}:{hname}", "house": house["id"],
                                "path": f"/data/wow/connected-realm/{crid}/auctions/{house['id']}", **common})
        else:
            raise ValueError(f"market {m['name']}: unknown kind {kind!r}")

    markets = cfg.get("market", [])
    for m in markets:
        if m["kind"] != "all-realms":
            add(m)
    for m in markets:
        if m["kind"] == "all-realms":
            taken = {t["crid"] for t in targets if t["namespace"] == m["namespace"] and t["crid"]}
            for e in expand_all_realms(conn, client, m, taken, {c["name"] for c in markets}):
                add(e)
    games = {m["name"]: game_for(m["namespace"], m.get("game")) for m in markets}
    for t in targets:
        game = t.get("game") or games.get(t["name"].split(":")[0]) or game_for(t["namespace"])
        conn.execute("""INSERT INTO markets (name, region, namespace, kind, connected_realm_id, auction_house_id,
                                             game, realms, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET region=excluded.region, namespace=excluded.namespace,
                          kind=excluded.kind, connected_realm_id=excluded.connected_realm_id,
                          auction_house_id=excluded.auction_house_id, game=excluded.game,
                          realms=excluded.realms, source=excluded.source""",
                     (t["name"], t["region"], t["namespace"], t["kind"], t["crid"], t["house"],
                      game, json.dumps(t["realms"]), t.get("source")))
    conn.commit()
    return targets


def collect(conn, client: Client, cfg: dict, log=print) -> int:
    ensure_schema(conn)
    errors = 0
    for t in market_targets(conn, client, cfg):
        row = conn.execute("SELECT MAX(last_modified) FROM snapshots WHERE market = ?", (t["name"],)).fetchone()
        try:
            data, lm = client.get(t["region"], t["namespace"], t["path"], if_modified_since=row[0])
        except ApiError as exc:
            log(f"{t['name']}: {exc}")
            errors += 1
            continue
        if data is None or (lm and lm == row[0]):
            log(f"{t['name']}: unchanged")
            continue
        r = store.store_snapshot(conn, t["name"], data, lm)
        if r.get("skipped"):
            log(f"{t['name']}: unchanged")
            continue
        log(f"{t['name']}: {r['listings']} listings, {r['items']} items; {r['new']} new, {r['ended']} ended, "
            f"{r['partial']} partly bought; {r['states']} item states written{' (daily keyframe)' if r['keyframe'] else ''}")
    for region in sorted({m["region"] for m in cfg.get("market", [])
                          if m.get("namespace") == f"dynamic-{m.get('region')}"}):  # retail regions
        try:
            collect_token(conn, client, region, log)
        except (ApiError, KeyError, TypeError, ValueError) as exc:
            log(f"wow token ({region}): {exc}")
            errors += 1
    return errors


def collect_token(conn, client: Client, region: str, log=print) -> bool:
    """Store the region's WoW token price if it changed. Returns True when a row was added."""
    data, _ = client.get(region, f"dynamic-{region}", "/data/wow/token/index")
    if data is None:
        return False
    price, updated = int(data["price"]), int(data["last_updated_timestamp"]) // 1000
    last = conn.execute("SELECT price FROM token_prices WHERE region = ? ORDER BY updated DESC LIMIT 1",
                        (region,)).fetchone()
    if last and last[0] == price:
        return False
    conn.execute("INSERT OR IGNORE INTO token_prices VALUES (?, ?, ?, ?)", (region, updated, price, int(time.time())))
    conn.commit()
    log(f"wow token ({region}): {price // 10000:,}g")
    return True


def fetch_item_names(conn, client: Client, region: str, namespace: str, item_ids, log=print) -> int:
    """Cache item names for ids not yet known (static namespace)."""
    static_ns = namespace.replace("dynamic-", "static-", 1)
    known = {r[0] for r in conn.execute("SELECT item_id FROM items")}
    n = 0
    for item_id in item_ids:
        if item_id in known:
            continue
        try:
            data, _ = client.get(region, static_ns, f"/data/wow/item/{item_id}")
        except ApiError as exc:
            log(f"item {item_id}: {exc}")
            if "HTTP 404" in str(exc):  # no such item in the API: don't ask again
                conn.execute("INSERT OR REPLACE INTO item_info (item_id, fetched) VALUES (?, ?)",
                             (item_id, int(time.time())))
            continue
        _store_item(conn, item_id, data)
        n += 1
        if n % 50 == 0:
            conn.commit()
    conn.commit()
    return n


def _store_item(conn, item_id: int, data: dict) -> None:
    """Name (items) and vendor price, binding, class, ... (item_info) from an item API response."""
    now = int(time.time())
    conn.execute("INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)",
                 (item_id, data.get("name"), (data.get("quality") or {}).get("type"), now))
    cls, sub = data.get("item_class") or {}, data.get("item_subclass") or {}
    name = lambda v: v.get("en_US") if isinstance(v, dict) else v  # noqa: E731
    binding = (data.get("preview_item") or {}).get("binding") or {}
    stackable = data.get("is_stackable")
    conn.execute("INSERT OR REPLACE INTO item_info VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (item_id, data.get("sell_price"), data.get("purchase_price"), binding.get("type"),
                  cls.get("id"), name(cls.get("name")), sub.get("id"), name(sub.get("name")),
                  None if stackable is None else int(bool(stackable)), data.get("max_count"),
                  data.get("level"), (data.get("inventory_type") or {}).get("type"), now))


def fetch_item_info(conn, client: Client, region: str, namespace: str, item_ids, limit: int = 200,
                    log=print) -> int:
    """Fetch items with no item_info row yet (at most `limit` per call, so a run stays short)."""
    static_ns = namespace.replace("dynamic-", "static-", 1)
    known = {r[0] for r in conn.execute("SELECT item_id FROM item_info")}
    todo = sorted(set(item_ids) - known)[:limit]
    n = 0
    for item_id in todo:
        try:
            data, _ = client.get(region, static_ns, f"/data/wow/item/{item_id}")
        except ApiError as exc:
            log(f"item {item_id}: {exc}")
            if "HTTP 404" in str(exc):  # no such item in the API: don't ask again
                conn.execute("INSERT OR REPLACE INTO item_info (item_id, fetched) VALUES (?, ?)",
                             (item_id, int(time.time())))
            continue
        _store_item(conn, item_id, data)
        n += 1
        if n % 50 == 0:
            conn.commit()
    conn.commit()
    return n
