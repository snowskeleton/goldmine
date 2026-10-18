#!/usr/bin/env python3
"""Goldmine client: connects this computer's WoW install to your Goldmine server.

The server collects auction prices and stores everything; this tool uploads the addon's
SavedVariables, downloads prices into the addon, shows reports, and keeps the addon (and the
Goldmine app) up to date. Goldmine.app runs it for all of that; it works on its own too.

  goldmine.py server-check              # test the connection
  goldmine.py sync                      # upload every Goldmine.lua found under the WoW folder
  goldmine.py watch --push              # upload whenever WoW saves (logout, /reload), then refresh prices
  goldmine.py push                      # download prices/crafts/deals into the addon (Data.lua)
  goldmine.py summary --days 7          # income/expense by source
  goldmine.py crafts | deals            # profit per craft; items listed well below market value
  goldmine.py price "Linen Cloth"       # recent prices for an item
  goldmine.py timing "Linen Cloth"      # when an item sells: hour-of-week heatmap (local time)
  goldmine.py export gold.csv --since 30d   # your ledger as CSV
  goldmine.py addon status | install    # install or update the addon from the server

Config: ~/.goldmine/config.toml, `[server] url = "https://gm_KEY@your.server"` (generate the URL
on the server's Clients page), or pass --server. WoW only writes SavedVariables on logout,
/reload, or exit, so that's when new data appears. Uploads are idempotent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import addon  # noqa: E402
import remote  # noqa: E402

DEFAULT_WOW_DIRS = [
    "/Applications/World of Warcraft",
    os.path.expanduser("~/Applications/World of Warcraft"),
    "C:/Program Files (x86)/World of Warcraft",
    "C:/Program Files/World of Warcraft",
]
DEFAULT_CONFIG = os.environ.get("GOLDMINE_CONFIG", os.path.expanduser("~/.goldmine/config.toml"))
APP_PLATFORM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


class NoServer(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path) -> dict:
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def dump_toml(cfg: dict) -> str:
    """Write the config back as TOML: tables of scalars/lists and arrays of tables. Comments
    aren't kept. Tables this client doesn't use (say, an old [api]) are written back unchanged."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    out = ["# Goldmine config. Managed by Goldmine.app; hand edits are fine (comments aren't kept).", ""]
    is_array = lambda v: isinstance(v, list) and v and all(isinstance(x, dict) for x in v)  # noqa: E731
    for key, value in sorted(cfg.items(), key=lambda kv: (bool(is_array(kv[1])), kv[0])):
        if isinstance(value, dict):
            out.append(f"[{key}]")
            out += [f"{k} = {val(v)}" for k, v in value.items() if v is not None]
            out.append("")
        elif is_array(value):
            for item in value:
                out.append(f"[[{key}]]")
                out += [f"{k} = {val(v)}" for k, v in item.items() if v not in (None, "")]
                out.append("")
        else:
            out.insert(2, f"{key} = {val(value)}")
    return "\n".join(out)


def config_cmd(args) -> int:
    path = Path(args.config).expanduser()
    if args.action == "get":
        print(json.dumps(load_config(path)))
        return 0
    cfg = json.load(sys.stdin)
    if not isinstance(cfg, dict):
        raise SystemExit("config must be a JSON object")
    url = (cfg.get("server") or {}).get("url")
    if url:
        remote.Remote(url)  # refuse a URL that can't work (no key, wrong scheme)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(".toml.bak")
        backup.write_bytes(path.read_bytes())
        backup.chmod(0o600)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_toml(cfg))
    tmp.chmod(0o600)
    load_config(tmp)  # make sure it round-trips
    tmp.replace(path)
    print("saved")
    return 0


def server(args) -> remote.Remote:
    """The configured Goldmine server ([server] url in config.toml, or --server)."""
    url = getattr(args, "server", None)
    if not url:
        try:
            url = (load_config(getattr(args, "config", DEFAULT_CONFIG)).get("server") or {}).get("url")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise NoServer(f"can't read {args.config}: {exc}") from exc
    if not url:
        raise NoServer("no Goldmine server configured. Generate a connection URL on your server's Clients page, "
                       f"then set [server] url in {DEFAULT_CONFIG} (or pass --server).")
    return remote.Remote(url)


def wow_dirs(args) -> list[str]:
    if getattr(args, "wow_dir", None):
        return args.wow_dir
    env = os.environ.get("GOLDMINE_WOW_DIR")
    return [env] if env else DEFAULT_WOW_DIRS


def find_saved_variables(dirs: list[str]) -> list[Path]:
    found = []
    for base in dirs:
        # <WoW>/<_flavor_>/WTF/Account/<ACCOUNT>/SavedVariables/Goldmine.lua: any flavor folder.
        found += glob.glob(os.path.join(base, "*", "WTF", "Account", "*", "SavedVariables", "Goldmine.lua"))
    return sorted({Path(p) for p in found})


# ---------------------------------------------------------------------------
# Game data up, prices down
# ---------------------------------------------------------------------------

def server_check_cmd(args) -> int:
    rem = server(args)
    me = rem.get_json("/api/v1/me")
    print(json.dumps({"ok": True, "server": rem.base, **me}) if args.json
          else f"Connected to {rem.base} as {me['user']} (server {me['server_version']})")
    return 0


def status_cmd(args) -> int:
    rem = server(args)
    out = rem.get_json("/api/v1/status")
    out["saved_variables"] = [str(p) for p in find_saved_variables(wow_dirs(args))]
    out["server"] = rem.base
    print(json.dumps(out))
    return 0


def sync_cmd(args) -> int:
    rem = server(args)
    paths = [Path(p) for p in args.file] if args.file else find_saved_variables(wow_dirs(args))
    if not paths:
        print("No Goldmine.lua SavedVariables found. Pass --wow-dir or --file. Searched:",
              *wow_dirs(args), sep="\n  ", file=sys.stderr)
        return 1
    errors = 0
    for p in paths:
        try:
            r = rem.upload_saved_variables(p.read_bytes(), source=str(p))
            print(f"{p}: {r['new']} new of {r['total']} entries (uploaded to {rem.base})")
        except (OSError, remote.RemoteError) as exc:
            print(f"{p}: FAILED: {exc}", file=sys.stderr)
            errors += 1
    if args.push:
        errors += push_cmd(args)
    return 1 if errors else 0


def watch_cmd(args) -> int:
    server(args)  # fail now, not on the first upload
    mtimes: dict[Path, float] = {}
    parent = os.getppid()
    print(f"Watching (every {args.interval}s). Ctrl-C to stop.")
    try:
        while True:
            if args.exit_with_parent and os.getppid() != parent:
                return 0  # the app that started us is gone
            paths = [Path(p) for p in args.file] if args.file else find_saved_variables(wow_dirs(args))
            changed = []
            for p in paths:
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if mtimes.get(p) != m:
                    mtimes[p] = m
                    changed.append(p)
            if changed:
                time.sleep(1)  # let WoW finish writing
                print(dt.datetime.now().strftime("[%H:%M:%S]"), end=" ")
                try:
                    sync_cmd(argparse.Namespace(**{**vars(args), "file": [str(p) for p in changed]}))
                except remote.RemoteError as exc:  # server down: keep watching, retry on the next save
                    print(f"error: {exc}", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def push_cmd(args) -> int:
    """Download Data.lua (prices, crafts, deals for your realms) into every installed copy of the addon."""
    rem = server(args)
    if args.addon_data:
        targets = [Path(args.addon_data)]
    else:
        targets = addon.data_targets(wow_dirs(args))
        if not targets:
            print("The Goldmine addon isn't installed in any WoW folder; run `goldmine.py addon install` "
                  "(or pass --addon-data).", file=sys.stderr)
            return 1
    try:
        data = rem.request("GET", "/api/v1/addon-data")
    except remote.RemoteError as exc:
        print(f"addon data: FAILED: {exc}", file=sys.stderr)
        return 1
    for t in targets:
        t.write_bytes(data)
        print(f"wrote {t} ({len(data) / 1024:.0f} KB from {rem.base}). /reload in game to load it.")
    return 0


# ---------------------------------------------------------------------------
# Reports (computed on the server)
# ---------------------------------------------------------------------------

def utc_offset_hours() -> int:
    return time.localtime().tm_gmtoff // 3600


def report_cmd(kind: str, **params):
    def run(args) -> int:
        values = {k: getattr(args, a) for k, a in params.items()}
        print(server(args).text(f"/api/v1/report/{kind}", values), end="")
        return 0
    return run


def to_local(buckets: list, offset: int) -> list:
    """Rotate 168 UTC hour-of-week buckets into local time (index 0 = local Sunday 00:00)."""
    return [buckets[(i - offset) % 168] for i in range(168)]


def timing_cmd(args) -> int:
    rem = server(args)
    offset = utc_offset_hours()
    if not args.json:
        print(rem.text("/api/v1/report/timing", {"item": args.item, "utc_offset": offset}), end="")
        return 0
    data = rem.get_json("/api/v1/timing", {"item": args.item})  # UTC buckets
    markets = [m | {"buckets": to_local(m["buckets"], offset)} for m in data["markets"]
               if not args.market or m["market"] in args.market]
    print(json.dumps({**data, "utc_offset_hours": offset, "markets": markets}))
    return 0


def parse_since(value: str | None) -> int | None:
    if not value:
        return None
    if value[-1] in "dh" and value[:-1].isdigit():
        return int(time.time()) - int(value[:-1]) * (86400 if value[-1] == "d" else 3600)
    return int(dt.datetime.fromisoformat(value).timestamp())


def export_cmd(args) -> int:
    rem = server(args)
    data = rem.request("GET", "/api/v1/export.csv", params={
        "since": parse_since(args.since), "until": parse_since(args.until), "source": args.source, "char": args.char})
    if args.out == "-":
        sys.stdout.write(data.decode())
    else:
        Path(args.out).write_bytes(data)
        rows = max(data.count(b"\n") - 1, 0)
        print(f"wrote {rows} rows to {args.out}")
    return 0


# ---------------------------------------------------------------------------
# Addon install/update and app updates
# ---------------------------------------------------------------------------

def addon_cmd(args) -> int:
    dirs = wow_dirs(args)
    copies = addon.installed(dirs)
    rem = None
    try:
        rem = server(args)
        release = rem.get_json("/api/v1/updates")["addon"] or {"error": "the server has no addon to serve"}
        release["source"] = rem.base
    except (NoServer, remote.RemoteError) as exc:
        if args.action == "install":
            raise
        release = {"error": str(exc)}
    for c in copies:
        c["current"] = bool(c["installed"] and c["hash"] == release.get("hash"))

    if args.action == "status":
        if args.json:
            print(json.dumps({"available": release, "copies": copies, "wow_dirs": dirs}))
            return 0
        print(f"available: {release.get('version') or '?'} ({(release.get('hash') or '')[:10]}) from "
              f"{release.get('source') or release.get('error')}")
        if not copies:
            print("no WoW flavor folders found under:", *dirs, sep="\n  ")
        for c in copies:
            state = ("linked to " + c["link_target"] if c["linked"] else
                     "current" if c["current"] else "outdated" if c["installed"] else "not installed")
            print(f"  {c['flavor']:<14} {c['version'] or '':<8} {state}   {c['path']}")
        return 0

    if "error" in release:
        raise remote.RemoteError(release["error"])
    by_name = {c["flavor"]: c for c in copies}
    if args.flavor:
        unknown = [f for f in args.flavor if f not in by_name]
        if unknown:
            raise addon.AddonError(f"no such game folder: {', '.join(unknown)} "
                                   f"(found: {', '.join(by_name) or 'none'})")
        chosen = [by_name[f] for f in args.flavor]
    else:
        chosen = [c for c in copies if c["installed"]]
        if not chosen and not args.auto:
            if len(copies) != 1:
                raise addon.AddonError("pick a game folder with --flavor: " + (", ".join(by_name) or
                                       "no WoW install found (use --wow-dir)"))
            chosen = copies
    if args.auto:
        chosen = [c for c in chosen if c["installed"] and not c["linked"] and not c["current"]]
    data, errors, results = None, 0, []
    for c in chosen:
        try:
            skip = (c["current"] or c["linked"]) and not args.force
            if data is None and not skip:
                data = rem.request("GET", release["url"])
                if hashlib.sha256(data).hexdigest() != release["sha256"]:
                    raise addon.AddonError("addon download doesn't match the server's checksum")
            result = addon.install(data or b"", Path(c["path"]).parents[2], release["hash"], force=args.force)
        except (OSError, remote.RemoteError, addon.AddonError) as exc:
            result, errors = f"failed: {exc}", errors + 1
        results.append({"flavor": c["flavor"], "path": c["path"], "result": result})
        if not args.json:
            note = {"linked": " (development symlink; left alone, use --force to replace)",
                    "installed": " (restart WoW or /reload to load it)", "updated": " (/reload in game)"}
            print(f"{c['flavor']}: {result}{note.get(result, '')}")
    if args.json:
        print(json.dumps({"available": release, "results": results}))
    return 1 if errors else 0


def app_update_cmd(args) -> int:
    rem = server(args)
    rel = (rem.get_json("/api/v1/updates").get("app") or {}).get(args.platform)
    newer = bool(rel and int(rel.get("build") or 0) > args.current_build)
    if args.action == "check":
        if args.json:
            print(json.dumps({"available": newer, "release": rel}))
        elif newer:
            print(f"Goldmine {rel['version']} (build {rel['build']}) is available.")
        else:
            print("Goldmine is up to date." if rel else "The server has no app release yet.")
        return 0

    if not newer:
        raise remote.RemoteError("no newer app release on the server")
    if not args.dest:
        raise remote.RemoteError("--dest is required")
    data = rem.request("GET", rel["url"], timeout=900)
    if hashlib.sha256(data).hexdigest() != rel["sha256"]:
        raise remote.RemoteError("app download doesn't match the server's checksum")
    dest = Path(args.dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    zpath = dest / "update.zip"
    zpath.write_bytes(data)
    if sys.platform == "darwin":  # ditto keeps permissions, symlinks and the code signature intact
        subprocess.run(["ditto", "-x", "-k", str(zpath), str(dest)], check=True)
    else:
        import zipfile
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    raise remote.RemoteError(f"unexpected path in app zip: {name!r}")
            zf.extractall(dest)
    zpath.unlink()
    apps = [p for p in dest.iterdir() if not p.name.startswith(".")]
    if len(apps) != 1:
        raise remote.RemoteError("app download should contain exactly one app")
    out = {"path": str(apps[0]), "version": rel["version"], "build": rel["build"], "notes": rel.get("notes", "")}
    print(json.dumps(out) if args.json else f"unpacked Goldmine {rel['version']} to {apps[0]}")
    return 0


def release_app_cmd(args) -> int:
    rem = server(args)
    path = Path(args.path)
    with tempfile.TemporaryDirectory() as tmp:
        if path.suffix == ".zip":
            data = path.read_bytes()
        elif sys.platform == "darwin":
            # Clients refuse a build whose signature doesn't verify, so don't publish one.
            check = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(path)],
                                   capture_output=True, text=True)
            if check.returncode != 0:
                raise remote.RemoteError(f"{path} fails code signature verification ({check.stderr.strip()}); "
                                         "rebuild it before releasing")
            zpath = Path(tmp) / "app.zip"
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(path), str(zpath)], check=True)
            data = zpath.read_bytes()
        else:
            data = Path(shutil.make_archive(str(Path(tmp) / "app"), "zip", path.parent, path.name)).read_bytes()
    r = json.loads(rem.request("POST", "/api/v1/releases/app", body=data, timeout=900,
                               params={"platform": args.platform, "notes": args.notes or None},
                               headers={"Content-Type": "application/zip"}))
    print(f"released Goldmine {r['version']} (build {r['build']}, {r['size'] / 1e6:.1f} MB) to {rem.base}")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="server connection URL with client key (overrides [server] url in the config)")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_wow(p, files=False):
        p.add_argument("--wow-dir", action="append", default=None,
                       help="WoW install folder (repeatable). Default: standard install locations")
        if files:
            p.add_argument("--file", action="append", help="explicit Goldmine.lua path(s); skips discovery")

    def add_push(p):
        p.add_argument("--push", action="store_true", help="then download fresh prices into the addon")
        p.add_argument("--addon-data", help="where to write Data.lua (default: every installed copy of the addon)")

    p = sub.add_parser("config", help="read (JSON) or write (JSON on stdin) the config file")
    p.add_argument("action", choices=["get", "set"])
    p.set_defaults(fn=config_cmd)

    p = sub.add_parser("server-check", help="test the connection to the server")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=server_check_cmd)

    p = sub.add_parser("status", help="your data on the server, as JSON")
    add_wow(p)
    p.set_defaults(fn=status_cmd)

    p = sub.add_parser("sync", help="upload SavedVariables to the server")
    add_wow(p, files=True)
    add_push(p)
    p.set_defaults(fn=sync_cmd)

    p = sub.add_parser("watch", help="upload automatically whenever WoW writes the file")
    add_wow(p, files=True)
    add_push(p)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--exit-with-parent", action="store_true", help="stop when the launching process exits")
    p.set_defaults(fn=watch_cmd)

    p = sub.add_parser("push", help="download prices, crafts and deals into the addon's Data.lua")
    add_wow(p)
    p.add_argument("--addon-data", help="where to write Data.lua (default: every installed copy of the addon)")
    p.set_defaults(fn=push_cmd)

    p = sub.add_parser("summary", help="income/expense by source")
    p.add_argument("--days", type=int, help="limit to the last N days")
    p.set_defaults(fn=report_cmd("summary", days="days"))

    p = sub.add_parser("crafts", help="profit per craft for recipes your characters know")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=report_cmd("crafts", limit="limit"))

    p = sub.add_parser("deals", help="items listed well below market value")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=report_cmd("deals", limit="limit"))

    p = sub.add_parser("price", help="recent prices for an item (id or name)")
    p.add_argument("item")
    p.set_defaults(fn=report_cmd("price", item="item"))

    p = sub.add_parser("timing", help="when an item sells: hour-of-week heatmap (local time)")
    p.add_argument("item", help="item id or name")
    p.add_argument("--market", action="append", help="limit to market(s) (with --json)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=timing_cmd)

    p = sub.add_parser("export", help="download your ledger as CSV ('-' for stdout)")
    p.add_argument("out")
    p.add_argument("--char", help="Name or Name-Realm")
    p.add_argument("--since", help="ISO date/time, or relative like 7d / 12h")
    p.add_argument("--until", help="ISO date/time, or relative like 1d")
    p.add_argument("--source", help="only this source (vendor, auction_sale, quest, ...)")
    p.set_defaults(fn=export_cmd)

    p = sub.add_parser("addon", help="install or update the Goldmine addon in WoW")
    p.add_argument("action", choices=["status", "install"])
    add_wow(p)
    p.add_argument("--flavor", action="append", help="game folder to install into, e.g. _retail_ (repeatable)")
    p.add_argument("--auto", action="store_true", help="only update copies that are already installed")
    p.add_argument("--force", action="store_true", help="reinstall even if current; replace a linked dev copy")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=addon_cmd)

    p = sub.add_parser("app-update", help="check for / download a new Goldmine app from the server")
    p.add_argument("action", choices=["check", "download"])
    p.add_argument("--platform", default=APP_PLATFORM)
    p.add_argument("--current-build", type=int, default=0, help="build number of the running app")
    p.add_argument("--dest", help="download: folder to unpack the new app into")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=app_update_cmd)

    p = sub.add_parser("release-app", help="upload a Goldmine app build to the server (admin key)")
    p.add_argument("path", help="Goldmine.app, or a zip of it")
    p.add_argument("--platform", default=APP_PLATFORM)
    p.add_argument("--notes", default="", help="release notes shown in the app")
    p.set_defaults(fn=release_app_cmd)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (NoServer, remote.RemoteError, addon.AddonError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
