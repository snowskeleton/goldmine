"""Paths and environment. Everything persistent lives under GOLDMINE_DATA (a Docker volume)."""

import os
from pathlib import Path

DATA = Path(os.environ.get("GOLDMINE_DATA", Path(__file__).resolve().parents[1] / "data")).resolve()

CONFIG = DATA / "config.toml"        # Blizzard API credentials, markets, collection settings
MARKET_DB = DATA / "market.sqlite"   # shared AH data
SERVER_DB = DATA / "server.sqlite"   # users, passkeys, invites, client keys
USERS_DIR = DATA / "users"           # users/<id>/game.sqlite: ledger, scans, recipes, inventory
CACHE_DIR = DATA / "cache"
RELEASES = DATA / "releases"         # uploaded desktop app builds, releases/app/<platform>/
# The addon this server hands out for install/update (repo/addon/Goldmine; /app/addon/Goldmine in the image).
ADDON_DIR = Path(os.environ.get("GOLDMINE_ADDON", Path(__file__).resolve().parents[2] / "addon" / "Goldmine")).resolve()
APP_BUNDLE_ID = "net.snowskeleton.goldmine"

# Public origin, e.g. https://gold.example.com. Passkeys are bound to it. If unset, it's
# derived from each request (fine for localhost; set it behind a reverse proxy).
ORIGIN = os.environ.get("GOLDMINE_ORIGIN", "").rstrip("/") or None
APP_NAME = os.environ.get("GOLDMINE_NAME", "Goldmine")
MAX_UPLOAD = int(os.environ.get("GOLDMINE_MAX_UPLOAD_MB", "64")) * 1024 * 1024
COLLECTOR = os.environ.get("GOLDMINE_COLLECTOR", "1") != "0"   # tests turn the thread off
