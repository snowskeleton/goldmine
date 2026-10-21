# Working in this repo (for coding agents)

Goldmine is a WoW gold ledger and auction house toolkit: an in-game addon (`addon/`), a server
that does all the work (`server/`, Docker, FastAPI + SQLite), a thin stdlib-only Python client
(`desktop/`), and a macOS menu bar app around that client (`app/`). `README.md` is the full
reference (layout, every feature, every endpoint); `docs/trader.md` is the trader's design
contract. Read the README section for whatever you're touching before you change it.

## You can reach the live server. Passkeys don't stop you.

The user's server is **https://goldmine-server.lyonslabs.net**. Passkeys only gate the
*web UI* (the HTML pages: `/`, `/reports`, `/markets`, `/settings`, `/clients`, ...). Everything
under `/api/v1/*` takes the desktop client's API key as a Bearer token, and that key is already
on this machine in `~/.goldmine/config.toml` (`[server] url`, the key may be inside the URL as
`https://gm_…@host`; `remote.py` handles both). The user's key is an **admin** key.

In order of preference:

1. **The `goldmine` MCP tools** (`mcp__goldmine__*`), registered at user scope as
   `goldmine.py mcp`. They forward every server tool with this computer's key and add `sync`,
   `push`, `addon_status`, `client_status`. If they're "still connecting", search for them with
   ToolSearch before deciding they're missing.
2. **The CLI**: `python3 desktop/goldmine.py server-check | status | crafts | deals | flips |
   price <item> | timing <item> | export - | push | sync` (see `--help`).
3. **Any `/api/v1` endpoint from Python**, reusing the client's own config loading:

   ```bash
   python3 -c '
   import sys, argparse; sys.path.insert(0, "desktop")
   import goldmine
   rem = goldmine.server(argparse.Namespace(config=goldmine.DEFAULT_CONFIG))
   print(rem.get_json("/api/v1/me"))
   print(rem.get_json("/api/v1/market/current", {"market": "us-proudmoore", "item": "2589"}))
   '
   ```

   Useful paths: `/api/v1/me`, `/api/v1/status`, `/api/v1/report/{kind}`,
   `/api/v1/market/{markets,items,current,history,values,deals,crafts}`, `/api/v1/mcp`
   (streamable-HTTP MCP), `/api/v1/updates`. `/healthz` needs no auth.

**Never print, echo, log or commit the key.** Don't `cat ~/.goldmine/config.toml` unredacted,
don't put it on a command line (use the snippet above, or `GOLDMINE_KEY` in the environment),
and pipe output through `sed -E 's/gm_[A-Za-z0-9_-]+/gm_XXX/g'` if it might contain one. The
same file holds Blizzard API credentials; treat them the same way.

If you truly need a web-UI-only page on the live server, ask the user to open it (or to sign in
in the browser pane), or check the same thing on a local server (below).

## Deploying

You can't deploy the server from here, and pushing to `main` does **not** deploy it: the user
redeploys it themselves. `/healthz` reports `0.2.0` no matter which commit is running, so don't
use it to tell. To check whether a change is live, probe for it (a new MCP tool in the tool list,
a new endpoint answering instead of 404). After you push server changes, tell the user they need
a redeploy; never say a change is live without checking.

- The addon ships inside the server image; a redeploy rolls addon changes out to clients.
- The Mac app: `app/build.sh --install` (local), `app/build.sh --release` uploads a build to
  the live server with the admin key and every connected app updates to it. `--release` is
  outward-facing: only on the user's say-so.

## Running a server locally

For UI work and anything you want to see end to end, run your own instance, never point
experiments at production:

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # once
S=<your scratchpad>; mkdir -p $S/data
GOLDMINE_DATA=$S/data GOLDMINE_COLLECTOR=0 GOLDMINE_PORT=8799 GOLDMINE_ORIGIN=http://localhost:8799 \
  .venv/bin/python -m goldmine_server.app
```

- `GOLDMINE_COLLECTOR=0` keeps it from hitting Blizzard on a timer. Pick a free port; other
  sessions may already be running servers on 8787/8798/8799.
- The first visitor creates the admin with a passkey. The tests have a software passkey
  authenticator (`server/tests/test_server.py`) if you need a signed-in session in a script.
- `server/data/` is a **stale local copy** (from mid-October) of a server's data, including a
  `market.sqlite` with real AH history and `config.toml` with Blizzard credentials. Copy files
  into your scratchpad before opening them; don't write to the originals. For current data, use
  the live server's read-only API instead.
- Calling Blizzard directly: `market.load_config("server/data/config.toml")` then
  `market.Client(cfg["api"]["client_id"], cfg["api"]["client_secret"], ...)`.
- Check pages in the browser pane at light, dark and phone width.

## Tests

`pytest` isn't installed; use `unittest`. Run what you touched, and everything before a merge:

```bash
cd server && .venv/bin/python -B -m unittest discover -s tests   # server (needs the venv above)
cd desktop && python3 -B -m unittest discover -s tests           # client, against a stub server
cd addon && lua tests/test_goldmine.lua && luajit tests/test_goldmine.lua   # addon, mocked WoW API
cd app && swiftc -typecheck -swift-version 5 -parse-as-library -target "$(uname -m)-apple-macos13" Sources/*.swift
```

`-B` keeps `__pycache__` out of the tree. If you change what the addon writes to SavedVariables,
regenerate the server's fixture: `cd addon && lua tests/test_goldmine.lua ../server/tests/fixtures/Goldmine.lua`.

## The game

- You can't run WoW. Addon code is tested only against the mock; things that need the real client
  go in the README's "Verify on the beta / at launch" lists rather than being claimed as working.
- **ToS:** never drive the game client, never use computer use on WoW, never write anything that
  chains protected actions. The task queue is one click per protected action, by a human.
- The addon has no file access: WoW writes SavedVariables on logout or `/reload`, and the client
  uploads them. Don't read or write the game's files directly; use the client (`sync`, `push`,
  `addon_status`).
- Retail AH prices are whole silver; Forever gold is scarce.

## Conventions

- Money is **integer copper** everywhere (1g = 10000c); times are unix seconds, with `*_iso` UTC
  copies in API output. Retail and Classic prices never mix even though item ids overlap.
- The desktop client is **stdlib only** (Python 3.11+, it ships inside the app bundle). The
  server's dependencies are in `server/requirements.txt`.
- AH storage is change-based and never deletes history (`store.py`, `archive.py`). Don't add
  anything that condenses or drops rows.
- Match the surrounding code: short docstrings that say *why*, few comments, no new frameworks.
- New features get documented in `README.md` in the same commit; it's the user-facing manual.
- Commit subjects describe the change in plain words (`Collector: download up to 4 markets at
  once`), and the user wants every commit pushed. Worktree branches get merged into `main` as
  soon as they're done.
- `/trade` (`.claude/skills/trade/`) is a trading run, not a coding session: no code edits
  during one.
