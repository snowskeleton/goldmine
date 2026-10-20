# Goldmine

Logs every change to your gold in WoW (Forever / retail-API clients), collects auction house
prices, and turns both into in-game price tooltips, craft and deal reports, and a ledger you can
export to CSV.

```
addon/Goldmine/
  Goldmine.lua    money ledger: every gold change + its source
  Market.lua      records AH prices you browse, your recipes (+ crafting operation info) and craft
                  results, professions, bags/bank/warband contents and warband gold, your own
                  auctions, AH mail (sale invoices), crafting orders
  Advice.lua      price tooltips, /goldmine crafts, /goldmine deals (from Data.lua)
  Data.lua        downloaded from the server by `goldmine.py push` (not in git; the addon works without it)
server/           Goldmine server (Docker): does all the work and stores everything
  goldmine_server/
    app.py        web UI (passkey sign-in) + client API
    collector.py  background AH collection on a timer
    ledger.py     per-user game data: schema, SavedVariables import, text reports, Data.lua
    market.py     Blizzard API client + AH snapshot collector
    store.py      change-based AH storage (listing lifecycles + item states written on change)
    pricing.py    market value / buy / sell prices, craft + deal reports, Data.lua writer
    marketdata.py read-only market data as JSON (/api/v1/market/*)
    account.py    read-only account state for the trader: balances, inventory, owned auctions,
                  ledger, recipes, crafts, WoW token price, item metadata (MCP only)
    mcp_server.py the same as MCP tools for assistants (/api/v1/mcp)
    svparse.py    SavedVariables (Lua table) parser
    releases.py   addon + app updates for clients
    cli.py        admin commands that use the server's API keys (probe, realms)
desktop/          the client, a thin layer over the server's API (Python 3.11+, stdlib only)
  goldmine.py     CLI: sync, watch, push, reports, export, addon install, app updates
  addon.py        installs/updates the addon in WoW's AddOns folders
  remote.py       HTTP client for the server
app/              Goldmine.app: macOS menu bar app (SwiftUI) around the client
addon/tests/      offline test harness with a mocked WoW API (runs on lua and luajit)
```

The client does nothing without a server: it has no Blizzard API access and no local database.
Its config is `~/.goldmine/config.toml`, which holds just the server connection URL and a few
app toggles.

## How it works

1. **In game**, the addon watches `PLAYER_MONEY`, diffs `GetMoney()`, and figures out *why*
   the money changed using:
   - **hooks** on money-moving calls (`TakeInboxMoney`, `SendMail`, `RepairAllItems`,
     `TakeTaxiNode`, `BuyMerchantItem`, AH posting/buying, guild/warband bank deposits…),
     many of which know the exact amount to expect;
   - **open NPC windows** (vendor, mailbox, AH, trainer, barber, transmog, …) via the
     interaction manager. Unrecognised window types are labeled by their enum name, so
     new ones still get a sensible label;
   - `QUEST_TURNED_IN` (exact reward) and coin-loot messages.

   Anything unexplained is logged as `unknown`; if your balance changed while the addon
   wasn't running, the next login logs a `reconcile` entry. Nothing gets lost.
2. WoW writes the log to `WTF/Account/<ACCOUNT>/SavedVariables/Goldmine.lua` on
   **logout, `/reload`, or exit** (addons can't write files directly).
3. **On your computer**, the client (Goldmine.app, or `goldmine.py watch`) uploads that file to
   your Goldmine server, which parses it and imports it into your ledger. Imports are keyed on
   entry id, so re-uploading is safe.
4. The server collects AH prices on a timer and computes prices, crafts and deals for the realms
   your characters are on; the client downloads them into the addon as `Data.lua`.

Each entry has: id, character, timestamp, delta, balance after, source, zone/subzone,
map id, session id, and a `meta` object with details (item, NPC, buyer, mail sender,
quest name, flight destination, AH cut/deposit, …).

## Install the addon

Goldmine.app installs and updates it for you (**Updates** tab: pick the game folders). From the
command line:

```bash
python3 desktop/goldmine.py addon status                      # where it's installed, current or not
python3 desktop/goldmine.py addon install --flavor _retail_   # install (or update) into a game folder
python3 desktop/goldmine.py addon install                     # update every installed copy
```

The addon comes from the server (the image ships it), so redeploying the server rolls out addon
changes. Updates are detected by a content hash of the addon's files, so you don't have to bump
`## Version:`. `Data.lua` is kept.

For development, symlink it instead so edits here apply in game; a symlinked copy shows as a
"development link" and is never replaced by updates. The Forever folder name below is a guess;
use whatever flavor folder sits next to `_retail_`:

```bash
ln -s /Users/snow/bin/goldmine/addon/Goldmine "/Applications/World of Warcraft/_forever_/Interface/AddOns/Goldmine"
```

If the addon shows as "out of date", bump `## Interface:` in `Goldmine.toc` to match the
client (`/dump select(4, GetBuildInfo())` in game).

### In-game commands

| Command | |
|---|---|
| `/goldmine` | session summary: net, gold/hr, breakdown by source |
| `/goldmine recent [n]` | last n entries for this character |
| `/goldmine stats` | number of stored entries, oldest entry |
| `/goldmine debug` | print every money change as it happens (good for checking labels) |
| `/goldmine prune <days>` | drop old entries from SavedVariables. **Sync first.** |

## Client

```bash
cd /Users/snow/bin/goldmine/desktop
python3 goldmine.py server-check                 # after setting [server] url (or pass --server URL)
python3 goldmine.py sync --push                  # upload every Goldmine.lua, then download prices
python3 goldmine.py watch --push                 # keep doing that whenever WoW writes the file
python3 goldmine.py summary --days 7
python3 goldmine.py export gold.csv --since 30d --char Snow
python3 goldmine.py export - --source auction_sale    # CSV to stdout
```

- Get a connection URL (`https://gm_…@gold.example.com`) from the server's **Clients** page and
  put it in `~/.goldmine/config.toml` as `[server] url = "…"`, or pass `--server`.
- The WoW folder defaults to standard install locations (override with `--wow-dir` or
  `GOLDMINE_WOW_DIR`). Any flavor folder (`_retail_`, `_forever_`, …) is searched.
- Reports run on the server; `timing` shows hours in your local time.

## Goldmine.app

A menu bar app around the client. Until it's connected to a server it shows only a connect
screen. Once connected, it:

- watches WoW's SavedVariables and uploads them whenever the game writes them (logout, `/reload`),
- downloads fresh prices into the addon's `Data.lua` after each upload,
- keeps itself and the addon up to date from the server,
- window tabs: **Status** (markets, characters, activity log), **Reports** (crafts, deals, timing,
  ledger, item prices), **Updates** (app and addon versions, install the addon per game folder),
  **Settings** (server connection, background work, launch at login).

It shells out to `desktop/goldmine.py` for everything, so the CLI and the app always agree.
The app bundles its own copy of the client, so it's self-contained (it still needs Python 3.11+;
it finds Homebrew/python.org installs, or set the path in Settings).

```bash
app/build.sh --install     # build, copy to ~/Applications, launch
app/build.sh --release     # build and upload to your server; connected apps update to it
```

Rebuild after changing Swift or client code. For development without rebuilding, set Settings →
Paths → Goldmine folder to this checkout; the app then runs the client here instead.

**Updates.** The app checks at launch and every 6 hours. It keeps installed copies of the addon
current and installs new app builds: download, check checksum, bundle id, build number and code
signature, then quit, swap the bundle, relaunch. Both are toggles in the Updates tab
(`[app] auto_update_addon` / `auto_update_app`). The version is `app/VERSION`; the build number
is the UTC build time, so every build is newer than the last. The app has to live somewhere it
can write (e.g. `~/Applications`) to update itself.

## Market data and advice

1. Create an API client at <https://develop.battle.net/access/clients> and enter its ID and
   secret on the server's **Settings** page. Add markets on its **Markets** page (it has a realm
   browser).
2. The server collects AH snapshots on a timer (default every 20 min; unchanged snapshots cost
   one tiny request).
3. In game, open each profession window once (records recipes) and browse the AH as usual
   (records the prices you see). `/reload`; the client uploads it.
4. Reports, in the app's Reports tab, on the server's web UI, or:

   ```bash
   python3 goldmine.py crafts                # profit per craft for every known recipe
   python3 goldmine.py deals                 # listed well below market and actually selling
   python3 goldmine.py price "Linen Cloth"   # price history for one item
   ```
5. After `push` (automatic in the app), `/reload` in game: item tooltips show market value,
   lowest listing, and estimated sales per day, and `/goldmine crafts` and `/goldmine deals`
   show the reports.

**Several game versions at once.** Collect as many markets as you like, from any game version.
Each market has a `game` (derived from its namespace: `retail`, `classic` for Mists, ...;
override with `game = "..."`) and knows which realms it serves. `push` writes one price set per
realm you have characters on, plus one commodity set per game; in game, the addon uses the
set for the realm you're logged into. Retail and Classic prices never mix, even though they
share item ids. In-game scans are attributed the same way, through the character that ran them.

**Storage: changes, not snapshots** (`server/goldmine_server/store.py`). Nothing is ever condensed or deleted:
- `auctions`: one row per listing for its whole life (first seen, price, quantity at start and
  latest, time-left bucket, when it ended and whether it vanished early or may have expired).
  Any past snapshot can be rebuilt exactly (`store.listings_at`).
- `auction_changes`: a row when a listing's quantity drops: a definite partial purchase.
- `item_states`: per item, a row only when its price/supply summary changes or something
  sold: min, p10, median, units listed, auctions, depth within +5/10/25%, sold, gone. Once per
  UTC day a keyframe writes every item, so windows replay from the nearest keyframe.
- Measured on retail US (commodities + 2 realms): about 3.2 MB per hour of new data.

`goldmine.py timing <item>` (or Reports → Timing) replays item states into an hour-of-week
heatmap of sales and supply, in local time.

How the numbers are defined is documented at the top of `server/goldmine_server/pricing.py`. In short:
**market value** = average 10th-percentile price over 72h; **sold/day** = units that vanished
between snapshots before they could have expired (cancellations count too, so it's an upper
bound); your own in-game scans override API prices when they're fresher.

**API findings (Oct 2026):** retail (`dynamic-us`) and Mists Classic (`dynamic-classic-us`) both
serve the retail-style layout (per-realm `/auctions`; Mists' region commodities endpoint is
empty). Era (`classic1x`) and Anniversary (`classicann`) list auction houses but every auction
path returns 404, so their AH data isn't published. Markets for a different game version
should get `pricing = false` so overlapping item ids don't mix into your prices.

**Launch day:** on the server, run `python -m goldmine_server.cli probe us` to see which API
namespaces exist (it tries likely Forever names) and `python -m goldmine_server.cli realms us
<namespace>` to list realms, then add a market on the Markets page. (In Docker, prefix with
`docker compose -f server/docker-compose.yml exec goldmine`.) Until the API serves Forever,
browsing the AH in game is the price source.

## Tests

```bash
cd addon && lua tests/test_goldmine.lua                       # addon logic against a mocked API
cd addon && lua tests/test_goldmine.lua ../server/tests/fixtures/Goldmine.lua   # regenerate fixture
cd addon && luajit tests/test_goldmine.lua                    # same, on Lua 5.1 like the game
cd desktop && python3 -m unittest discover -s tests           # client, against a stub server
cd server && .venv/bin/python -m unittest discover -s tests   # server: import, pricing, storage, API
```

## Notes on Forever's economy

- Forever uses **mega-realms per realm type (PvP / PvE / RP / Hardcore) and region**, and the
  AH is shared across each region + type. Characters record their `region`; future price
  data should be keyed by region + realm type, not realm name.
- Gold is much scarcer than in retail; a single gold is a lot. Amounts are stored in copper
  everywhere, and the summary prints like the game does (`4s 79c`, not `0g 04s 79c`).

## Verify on the beta / at launch

The tests run against a mock, so these need checking in the real client
(`/goldmine debug` makes it easy):

- `## Interface:` number for Forever.
- Which `Enum.PlayerInteractionType` values exist, and whether the hooked functions
  (`SendMail`, `TakeTaxiNode`, `C_AuctionHouse.*`, `C_Bank.*`) exist. Missing ones are
  skipped safely; they just fall back to window-based labels.
- Owned auctions: that `buyoutAmount` is per unit for commodities (the import assumes so), and
  `C_TradeSkillUI.GetCraftingOperationInfo(recipeID, {}, nil, false)` / the
  `TRADE_SKILL_ITEM_CRAFTED_RESULT` payload fields (`multicraft`, `resourcesReturned`,
  `concentrationSpent`) match what Market.lua reads.
- Crafting orders: that `C_CraftingOrders.GetCrafterOrders()`/`GetCrafterBuckets()` hold the
  orders the tab last requested (read after hooking `RequestCrafterOrders`), and the
  `CRAFTINGORDERS_*` event names.
- Whether `GetRepairAllCost()` still returns the cost immediately after `RepairAllItems()`.
  If not, repairs are still labeled correctly, just matched by sign instead of amount.

## Server

`server/` does all the work, so your Mac doesn't have to be on: it collects prices, stores
everything, computes reports, serves a web UI, and the clients talk to it.

```bash
docker compose -f server/docker-compose.yml up -d --build      # from the repository root
```

- Set `GOLDMINE_ORIGIN` to the public URL your reverse proxy serves (e.g. `https://gold.example.com`);
  passkeys are bound to it. The proxy must pass `X-Forwarded-Proto`/`Host` (standard). The
  container listens on 8080; the compose file publishes it on 127.0.0.1 only.
- All state is in the `/data` volume: `config.toml` (API credentials, markets), `market.sqlite`
  (shared AH data), `server.sqlite` (users, passkeys, invites, client keys), `users/<id>/`
  (each user's ledger, scans, recipes, inventory).
- **Accounts:** the first visitor creates the admin account with a passkey, so sign up right after
  deploying. Later users need an invite link (Users page, single use, 7 days).
- **Clients:** the Clients page generates a connection URL like `https://gm_…@gold.example.com`.
  Paste it into the Mac app (Settings → Goldmine server). The key is sent as a Bearer token,
  never in the URL path, and only its hash is stored; revoke it any time.
- **Releases:** the image ships the addon, and connected apps install/update it from there. Mac
  app builds are uploaded by an admin with `app/build.sh --release` (or `goldmine.py release-app
  Goldmine.app`) and kept in `/data/releases`; the Clients page has download links for both. The
  manifest (`/api/v1/updates`) is keyed by platform, ready for a Windows build.
- **Export:** `GET /api/v1/export.csv` (what `goldmine.py export` uses). Each user's ledger is
  also a plain SQLite file in `/data/users/<id>/game.sqlite`; its `transactions_view` shows
  amounts in gold, handy for Grafana, Metabase, etc.

### Market data for assistants (JSON API and MCP)

Read-only access to the collected AH data, so an assistant (or a script) can pull live and
historical prices straight from the server. It opens `market.sqlite` read-only, so it can't
change anything. Authenticate with a client key from the **Clients** page (`gm_…`, the part
before `@` in the connection URL) as `Authorization: Bearer gm_…`. Prices are copper per unit;
times are unix seconds with an ISO 8601 UTC copy (`*_iso`). Lists are capped and paginated
(`limit`/`offset`, follow `next_offset` until it's `null`).

| `GET /api/v1/market/…` | |
|---|---|
| `markets` | every market: name, kind, region, game, realms, snapshot count, first/last snapshot |
| `items?q=linen` | item search by (partial) name or id; only names the server has cached are searchable (`limit` ≤ 100) |
| `current?market=us-proudmoore&item=2589&item=Wool Cloth` | latest state: min_price, p10, median, quantity, auctions, depth5/10/25 (≤ 100 items; ids or names, comma-separated works too) |
| `history?market=…&item=…&since=&until=&resolution=hourly` | the item's state at every snapshot, rebuilt from change-based storage (a state holds until the item's next row); `raw`, `hourly` or `daily` (UTC buckets: lowest min, mean p10/median/quantity, summed sold/gone, `close_*`). Default window 7 days, at most 90; ≤ 1000 rows per page |
| `values?market=…[&item=…]&hours=72` | the price model: market value, current min/p10, buy, sell, sold/day; without `item`, every item busiest first (`min_sold`, paged) |
| `deals?market=…&max_ratio=0.7&min_sold=1` | listed well below market value and actually selling |
| `crafts?hours=72` | your craft report (recipes your characters know), per realm |

`values` and `deals` use API snapshots only (not your in-game scans), over the window ending at
the market's latest snapshot.

The same queries are MCP tools (`list_markets`, `search_items`, `get_current_prices`,
`get_price_history`, `get_market_values`, `get_deals`, `get_crafts`) on a streamable-HTTP MCP
endpoint at `/api/v1/mcp`. To use it from Claude Code (create a dedicated key on the Clients
page, e.g. "Claude", so you can revoke it separately):

```bash
claude mcp add --transport http --scope user goldmine https://gold.example.com/api/v1/mcp --header "Authorization: Bearer gm_…"
```

Your own account state, as the addon last saw it, is also there (MCP only; every tool is
read-only, see `docs/trader.md`):

| MCP tool | |
|---|---|
| `get_account` | gold per character and in the warband bank, `total_gold`, last-seen times (character, bags, bank, auctions, professions), each character's professions (skill, `needs_catalogue`), latest WoW token price |
| `get_inventory` | items in bags/bank/warband: total `count` and `held` by owner and location (`char`, `location`, `items` filters) |
| `get_owned_auctions` | your auctions from each character's latest Auctions-tab list: unit price, quantity, active/sold, time left; `include_ended` adds ones that dropped off the list |
| `get_ledger` | gold changes newest first, with per-source totals (`since`/`until` as unix seconds, `7d`, or a date; `source`, `item`, `char`) |
| `get_recipes` | known recipes, reagents, and crafting operation info (skill vs difficulty, expected quality, multicraft/resourcefulness %, concentration cost), and concentration per profession |
| `get_craft_results` | crafts made (quality, multicraft, resources returned, concentration) and per-recipe proc rates |
| `get_token_price` | WoW token price, latest and history (a row per change, collected with the AH snapshots) |
| `get_auction_mail` | AH mail seen in the inbox: sale invoices (buyer, bid/buyout, deposit, AH cut, money received), won/outbid/expired/cancelled; `sales_by_item` totals |
| `get_crafting_orders` | crafting orders the crafting orders window showed (public, guild, personal, patron): commission, min quality, reagents provided, patron rewards, expiry; plus the public order board |
| `get_item_info` | vendor sell/buy price, binding (`bound`), stack size, class, expansion: Blizzard's item API (cached by the collector for items you own or craft) plus what the game client reported |

The only request the addon sends itself is for your own auction list, when the AH opens and
a couple of seconds after you post or cancel (debounced). `/run GoldmineDB.settings.queryOwnedAuctions = false`
turns that off. Everything else is read from what the game UI already loaded: the inbox
(mail has no id, so a mail is recognized by its contents and its arrival time estimated
from days left), the crafting orders the orders tab requested, and `GetProfessions()` on
login. On login the addon also prints, once per session, which profession windows to open
because their recipes were never catalogued or are more than a week old.

`C_AuctionHouse.ReplicateItems` (a full realm AH snapshot) is deliberately not used: since
the 9.0.2 hotfix it returns no seller names except your own, so it adds nothing over the hourly
API snapshots. It is also known to disconnect clients on realms with more than ~80k auctions,
which includes Proudmoore.

`claude mcp get goldmine` should say "Connected". `--scope user` makes it available in every
project; leave it out to register it for the current folder only. Keep the key out of shared
`.mcp.json` files.

Local test run without Docker:

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
GOLDMINE_DATA=$PWD/data GOLDMINE_PORT=8787 GOLDMINE_ORIGIN=http://localhost:8787 .venv/bin/python -m goldmine_server.app
.venv/bin/python -m unittest discover -s tests                  # end-to-end tests (software passkey authenticator)
```
