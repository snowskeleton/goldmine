"""TradeSkillMaster's public price CSVs (https://tradeskillmaster.com/public-data), kept as a
second opinion next to Goldmine's own numbers.

TSM's region sale rate, average sale price and sold/day come from its users' real sales, which
API snapshots can't see. The files are static, public and need no key: realm files and
commodities change about every 3 hours, region files daily. Each file's latest copy is kept
(tsm_prices); it's TSM's data, not Goldmine's AH history, so nothing here is history.

Files fetched: region items (and pets on retail) for every game + region that has a market, the
region's commodities on retail, and the realm file of every realm market configured on its own.
"""

from __future__ import annotations

import calendar
import csv
import io
import time
import urllib.error
import urllib.request

BASE = "https://public-data.tradeskillmaster.com"
CHECK_EVERY = 3600 - 300  # a collection run a few minutes early still checks
GAME_TYPES = {"retail": "retail", "forever": "forever"}  # Goldmine game -> TSM gameType

SCHEMA = """
CREATE TABLE IF NOT EXISTS tsm_prices (
    game          TEXT NOT NULL,       -- Goldmine game (retail, forever)
    region        TEXT NOT NULL,
    scope         TEXT NOT NULL,       -- region | commodities | realm:<slug>
    kind          TEXT NOT NULL,       -- items | pets
    id            INTEGER NOT NULL,    -- item id, or pet species id
    name          TEXT,
    market_value  INTEGER,             -- copper
    min_buyout    INTEGER,             -- realm and commodity files
    recent        INTEGER,             -- realm and commodity files
    historical    INTEGER,
    avg_sale      INTEGER,             -- region files: from TSM users' sales
    sale_rate     REAL,                -- region files: share of TSM users' posts that sold
    sold_per_day  REAL,                -- region files
    updated       INTEGER,             -- TSM's scan time
    PRIMARY KEY (game, region, scope, kind, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tsm_files (
    path          TEXT PRIMARY KEY,
    checked       INTEGER NOT NULL,
    last_modified TEXT,                -- the HTTP header, sent back as If-Modified-Since
    rows          INTEGER,
    error         TEXT
);
"""


def _int(v: str) -> int | None:
    try:
        return int(float(v)) if v not in ("", None) else None
    except ValueError:
        return None


def _float(v: str) -> float | None:
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def _ts(v: str) -> int | None:
    try:
        return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ")) if v else None
    except ValueError:
        return None


def parse(text: str) -> list[tuple]:
    """A TSM CSV -> [(id, name, market_value, min_buyout, recent, historical, avg_sale, sale_rate,
    sold_per_day, updated)]; columns a file doesn't have are None."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        ident = _int(r.get("itemId") or r.get("petSpeciesId") or "")
        if ident is None:
            continue
        out.append((ident, r.get("name") or None, _int(r.get("marketValue", "")), _int(r.get("minBuyout", "")),
                    _int(r.get("recent", "")), _int(r.get("historical", "")), _int(r.get("avgSalePrice", "")),
                    _float(r.get("saleRate", "")), _float(r.get("soldPerDay", "")), _ts(r.get("updatedAt", ""))))
    return out


def files_for(cfg: dict) -> list[tuple[str, str, str, str, str]]:
    """[(game, region, scope, kind, path)] to keep, from the configured markets."""
    from .market import game_for  # late: market imports this module
    out, seen = [], set()

    def add(game, region, scope, kind, path):
        if path not in seen:
            seen.add(path)
            out.append((game, region, scope, kind, path))
    for m in cfg.get("market", []):
        game = game_for(m.get("namespace", ""), m.get("game"))
        gtype, region = GAME_TYPES.get(game), (m.get("region") or "").lower()
        if not gtype or not region:
            continue
        add(game, region, "region", "items", f"{gtype}/{region}/region/items.csv")
        if game == "retail":
            add(game, region, "region", "pets", f"{gtype}/{region}/region/pets.csv")
            add(game, region, "commodities", "items", f"{gtype}/{region}/commodities.csv")
            if m.get("kind") == "realm" and m.get("realm"):
                slug = m["realm"]
                add(game, region, f"realm:{slug}", "items", f"{gtype}/{region}/realm/{slug}/items.csv")
                add(game, region, f"realm:{slug}", "pets", f"{gtype}/{region}/realm/{slug}/pets.csv")
    return out


def _download(path: str, since: str | None) -> tuple[str | None, str | None]:
    """(text or None when unchanged, Last-Modified header)."""
    req = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": "Goldmine"})
    if since:
        req.add_header("If-Modified-Since", since)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8"), resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, since
        raise


def refresh(conn, cfg: dict, now: int | None = None, log=print, download=_download) -> int:
    """Re-check each wanted file at most hourly (If-Modified-Since, so an unchanged file is
    a tiny request) and replace its rows when it changed. Returns files updated."""
    now = int(time.time()) if now is None else now
    updated = 0
    for game, region, scope, kind, path in files_for(cfg):
        row = conn.execute("SELECT checked, last_modified FROM tsm_files WHERE path = ?", (path,)).fetchone()
        if row and now - row[0] < CHECK_EVERY:
            continue
        try:
            text, lm = download(path, row[1] if row else None)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            conn.execute("INSERT OR REPLACE INTO tsm_files VALUES (?, ?, ?, NULL, ?)",
                         (path, now, row[1] if row else None, str(exc)[:200]))
            conn.commit()
            log(f"TSM {path}: {exc}")
            continue
        if text is None:
            conn.execute("UPDATE tsm_files SET checked = ?, error = NULL WHERE path = ?", (now, path))
            conn.commit()
            continue
        rows = parse(text)
        conn.execute("DELETE FROM tsm_prices WHERE game = ? AND region = ? AND scope = ? AND kind = ?",
                     (game, region, scope, kind))
        conn.executemany("INSERT OR REPLACE INTO tsm_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(game, region, scope, kind, *r) for r in rows])
        conn.execute("INSERT OR REPLACE INTO tsm_files VALUES (?, ?, ?, ?, NULL)", (path, now, lm, len(rows)))
        conn.commit()
        updated += 1
        log(f"TSM {path}: {len(rows)} rows")
    return updated


def lookup(conn, game: str, region: str, ids, realm: str | None = None, kind: str = "items") -> dict[int, dict]:
    """{id: {region: {...}, realm: {...} | commodities: {...}}} for whatever TSM has."""
    ids = list(set(ids))
    if not ids:
        return {}
    scopes = ["region", "commodities"] + ([f"realm:{realm}"] if realm else [])
    out: dict[int, dict] = {}
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for (scope, ident, mv, mb, recent, hist, avg, rate, spd, upd) in conn.execute(
                    f"""SELECT scope, id, market_value, min_buyout, recent, historical, avg_sale, sale_rate,
                               sold_per_day, updated FROM tsm_prices
                        WHERE game = ? AND region = ? AND kind = ? AND scope IN ({','.join('?' * len(scopes))})
                          AND id IN ({','.join('?' * len(chunk))})""", (game, region, kind, *scopes, *chunk)):
                d = {"market_value": mv, "historical": hist, "updated": upd}
                if scope == "region":
                    d.update(avg_sale_price=avg, sale_rate=rate, sold_per_day=spd)
                else:
                    d.update(min_buyout=mb, recent=recent)
                out.setdefault(ident, {})[scope.split(":")[0]] = d
    except Exception as exc:  # no table yet (older database): no TSM data
        if "no such table" not in str(exc):
            raise
    return out


def realm_slug(cfg: dict, market: str) -> str | None:
    """The realm slug TSM files a realm market under: the realm it was configured with."""
    return next((m["realm"] for m in cfg.get("market", []) if m.get("name") == market and m.get("realm")), None)
