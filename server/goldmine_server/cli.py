"""Admin commands: ones that use the server's Blizzard API credentials (the client has none),
and archive maintenance.

  python -m goldmine_server.cli probe us              # which API namespaces exist (find Forever's on launch day)
  python -m goldmine_server.cli realms us dynamic-us  # connected realms in a namespace
  python -m goldmine_server.cli pack --vacuum         # archive finished days now, then shrink the file

In Docker: docker compose -f server/docker-compose.yml exec goldmine python -m goldmine_server.cli probe us
"""

from __future__ import annotations

import argparse
import sys

from . import archive, db, ledger, market, settings


def client() -> market.Client:
    api = market.load_config(settings.CONFIG).get("api") or {}
    if not api.get("client_id") or not api.get("client_secret"):
        raise SystemExit(f"Set the Blizzard API credentials on the server's Settings page (or in {settings.CONFIG}).")
    return market.Client(api["client_id"], api["client_secret"], cache_dir=settings.CACHE_DIR)


def probe(args) -> int:
    c = client()
    for ns in [n.format(r=args.region) for n in ledger.PROBE_NAMESPACES] + (args.namespace or []):
        try:
            data, _ = c.get(args.region, ns, "/data/wow/connected-realm/index")
            print(f"{ns:<34} OK  ({len(data.get('connected_realms', []))} connected realms)")
        except market.ApiError as exc:
            print(f"{ns:<34} --  {exc}")
    return 0


def realms(args) -> int:
    rows = ledger.list_realms(client(), args.region, args.namespace)
    for r in sorted(rows, key=lambda r: (r["connected_realm_id"], r["slug"] or "")):
        print(f"{r['connected_realm_id']:>6}  {r['slug'] or '':<28} {r['name'] or '':<28} "
              f"{r['type'] or '':<8} {r['category'] or ''}")
    print(f"{len(rows)} realms in {len({r['connected_realm_id'] for r in rows})} connected realms")
    return 0


def pack(args) -> int:
    """Pack finished days into the archive now (the collector does this after each run). VACUUM
    hands the space the hot tables freed back to the filesystem; SQLite reuses it otherwise."""
    conn = db.market_conn()
    try:
        archive.pack_due(conn)
        if args.vacuum:
            before = settings.MARKET_DB.stat().st_size
            conn.execute("VACUUM")
            print(f"vacuum: {before / 1e6:.1f} MB -> {settings.MARKET_DB.stat().st_size / 1e6:.1f} MB")
    finally:
        conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="check which API namespaces exist in a region")
    p.add_argument("region")
    p.add_argument("--namespace", action="append", help="extra namespace(s) to try")
    p.set_defaults(fn=probe)
    p = sub.add_parser("realms", help="list connected realms in a namespace")
    p.add_argument("region")
    p.add_argument("namespace")
    p.set_defaults(fn=realms)
    p = sub.add_parser("pack", help="archive finished days of AH history into compressed chunks")
    p.add_argument("--vacuum", action="store_true", help="then rewrite the database file to reclaim the space")
    p.set_defaults(fn=pack)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
