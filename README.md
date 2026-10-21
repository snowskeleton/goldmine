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
  TaskQueue.lua   the trader's task queue window, /goldmine tasks (from Tasks.lua)
  Tasks.lua       the active trader plan, downloaded by `goldmine.py push` (not in git; optional like Data.lua)
server/           Goldmine server (Docker): does all the work and stores everything
  goldmine_server/
    app.py        web UI (passkey sign-in) + client API
    collector.py  background AH collection on a timer
    ledger.py     per-user game data: schema, SavedVariables import, text reports, Data.lua
    market.py     Blizzard API client + AH snapshot collector
    store.py      change-based AH storage (listing lifecycles + item states written on change)
    archive.py    finished days of that history packed into compressed per-item weekly chunks
    region.py     region baselines: each item's price across realms, adjusted for realm size
    pricing.py    market value / buy / sell prices, craft + deal reports, Data.lua writer
    marketdata.py read-only market data as JSON (/api/v1/market/*)
    account.py    read-only account state for the trader: balances, inventory, owned auctions,
                  ledger, recipes, crafts, WoW token price, item metadata (MCP only)
    mcp_server.py the same as MCP tools for assistants (/api/v1/mcp)
    tasks.py      trader plans (validation, active plan, Tasks.lua) and task results
    risk.py       trader risk rules (token floors, exposure caps, experiment budget) for publish_plan
    sim.py        market simulator: plan scoring, strategy backtests, conservative inventory value
    lots.py       FIFO cost-basis lots from the AH ledger, realized P&L, scorecard
    journal.py    the trader's journal (hypotheses, decisions, lessons) in the user's game DB
    svparse.py    SavedVariables (Lua table) parser
    releases.py   addon + app updates for clients
    cli.py        admin commands that use the server's API keys (probe, realms)
desktop/          the client, a thin layer over the server's API (Python 3.11+, stdlib only)
  goldmine.py     CLI: sync, watch, push, reports, export, addon install, app updates
  addon.py        installs/updates the addon in WoW's AddOns folders
  remote.py       HTTP client for the server
  mcp_bridge.py   `goldmine.py mcp`: local MCP server (stdio) forwarding the server's tools, plus sync/push
app/              Goldmine.app: macOS menu bar app (SwiftUI) around the client
addon/tests/      offline test harness with a mocked WoW API (runs on lua and luajit)
```

The client does nothing without a server: it has no Blizzard API access and no local database.
Its config is `~/.goldmine/config.toml`, which holds just the server address, your API key, and a few
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
| `/goldmine tasks` | show/hide the trader's task queue (opens by itself at the AH, mailbox, a merchant or a profession window when it has tasks there) |

## Client

```bash
cd /Users/snow/bin/goldmine/desktop
python3 goldmine.py server-check                 # after setting [server] url and key
python3 goldmine.py sync --push                  # upload every Goldmine.lua, then download prices
python3 goldmine.py watch --push                 # keep doing that whenever WoW writes the file
python3 goldmine.py summary --days 7
python3 goldmine.py export gold.csv --since 30d --char Snow
python3 goldmine.py export - --source auction_sale    # CSV to stdout
```

- Generate an API key on the server's **Clients** page and put it in `~/.goldmine/config.toml`:

  ```toml
  [server]
  url = "https://gold.example.com"
  key = "gm_…"
  ```

  `--server URL` and the `GOLDMINE_KEY` environment variable override them (the key stays off the
  command line, where other users could see it in `ps`). Old configs with the key inside the URL
  (`https://gm_…@host`) still work; the app splits them into `url` and `key` when it next saves.
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
  Battle pets and gear also get a separate series per configuration (negative `variant`): one per
  pet breed + quality + level, one per gear bonus set (item level, sockets, tertiaries). The
  item-wide series stays as it was, so tooltips and crafts are unchanged, while deals, flips
  and the price report judge each breed or bonus set on its own.
- Once a UTC day is over, the collector moves that day's item states and ended listings into
  compressed chunks, one per item per week (`server/goldmine_server/archive.py`): every row and
  column kept exactly, at about a seventh of the space. The hot tables hold about a day plus the
  listings still up; readers merge the two. `python -m goldmine_server.cli pack --vacuum` packs
  now and shrinks the file (SQLite otherwise reuses the freed space).
- Measured on retail US (commodities + 2 realms): about 3.2 MB per hour of new data before
  packing, about 0.4 MB per hour once packed. Two days of history went from 140 MB to 53 MB,
  44 MB of which is the listings currently up (a fixed size, not growth).

`goldmine.py timing <item>` (or Reports → Timing) replays item states into an hour-of-week
heatmap of sales and supply, in local time.

How the numbers are defined is documented at the top of `server/goldmine_server/pricing.py`. In short:
**market value** = average 10th-percentile price over 72h; **sold/day** = units that vanished
between snapshots before they could have expired (cancellations count too, so it's an upper
bound); your own in-game scans override API prices when they're fresher.

**Every realm at once.** An `all-realms` market (Markets page → Browse realms → "Collect every
realm in this namespace", or `kind = "all-realms"` with a region and namespace) collects every
connected realm in that namespace as its own market, named after one of its realms
(`us-moon-guard`). Realms you add on their own are skipped, new realms and merges are picked up
from the realm list (re-read every 6 hours), and removing a realm's own entry hands it to the
all-realms entry under the same name. Its pricing switch covers all of its realms; reports only
use realms you have characters on either way. US retail is 83 connected realms, about 2.2M
listings, roughly 50 MB of downloads an hour.

**Region baseline** (`server/goldmine_server/region.py`). Bigger auction houses list much cheaper:
across US retail, doubling a realm's listings takes its prices to ~70%, and FULL realms list at
~0.6x the region median. So after each collection run, every item's price on every realm is
rescaled to a typical realm size and the median taken (`us-retail-region`; the plain median,
TSM's "region price", is `us-retail-region-raw`). For any realm that gives "what other realms
charge, at this realm's size". Deals keep using your realm's own history but each gets a region
verdict: *confirmed* (cheap elsewhere too), *normal* (the usual price elsewhere, so it's your
realm's own history that's off) or *partial*. Price history charts draw the region line and,
with "Compare with", another realm's price rescaled to yours; the axis goes logarithmic when the
lines are far apart. The size effect is re-fitted from the data on every rollup.

**API findings (Oct 2026):** retail (`dynamic-us`) and Mists Classic (`dynamic-classic-us`) both
serve the retail-style layout (per-realm `/auctions`; Mists' region commodities endpoint is
empty). Era (`classic1x`) and Anniversary (`classicann`) list auction houses but every auction
path returns 404, so their AH data isn't published. The collector checks one of their auction
houses once a day and says so on the dashboard (and unlocks them on the Markets page) once
Blizzard serves the listings. Markets for a different game version
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

Task queue (retail), untested against a live client:

- `SendSearchQuery(MakeItemKey(id))` returns results for gear and other non-commodity items (the
  Blizzard UI browses first to get an item key with item level).
- `COMMODITY_PRICE_UPDATED` arrives after `StartCommoditiesPurchase`, and Confirm still works
  if you wait a while before clicking it. `COMMODITY_PURCHASE_SUCCEEDED/FAILED` arrive after Confirm.
- Item buyouts (`PlaceBid`), posts and cancels are counted when you click. An
  `AUCTION_HOUSE_SHOW_ERROR` within 5 s undoes the count. Is that the only failure signal?
- Retail AH prices are whole silver (posts are rounded down to it). `PostCommodity` takes a
  quantity across several stacks, and `PostItem(loc, duration, 1, nil, buyout)` posts one.
- Owned auctions' `buyoutAmount` is taken as the price per unit.
- `C_TradeSkillUI.OpenRecipe` works with the profession window closed.
  `ProfessionsFrame.CraftingPage.CreateMultipleInputBox:SetValue` fills in the count, and
  `TRADE_SKILL_ITEM_CRAFTED_RESULT` fires once per craft with `recipeID` or `itemID`.
- `C_Container.UseContainerItem` attaches a stack with the Send Mail tab open and sells it at a merchant.
  An attached stack shows as `isLocked` in the bag.
- None of these calls is blocked as protected when it's made from the queue's own buttons.

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
- **Clients:** the Clients page generates API keys (`gm_…`) for the Mac app and CLI: paste the
  server address and the key into the app (Settings → Goldmine server). The key is sent as a
  Bearer token, never in the URL, and only its hash is stored; revoke it any time. The same page
  makes OAuth client IDs and secrets for MCP connectors (below).
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
change anything. Authenticate with an API key from the **Clients** page as
`Authorization: Bearer gm_…` (or an OAuth access token, see below). Prices are copper per unit;
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
endpoint at `/api/v1/mcp`.

**Claude (claude.ai, Claude Desktop, mobile) via Connectors.** Connectors sign in with OAuth:

1. On the Clients page, under **Connectors (MCP)**, generate a client ID and secret.
2. In Claude: Settings → Connectors → Add custom connector. URL `https://gold.example.com/api/v1/mcp`;
   under Advanced settings paste the client ID and secret.
3. Connect: Claude sends you to your server, you sign in with your passkey and press Allow.

The server has to be reachable from the internet over https, with `GOLDMINE_ORIGIN` set to its public
URL (the OAuth metadata is built from it). Connector tokens are read-only: they reach the market data
and MCP tools and nothing else (no uploads, ledger or exports). Access tokens last an hour and refresh
for 30 days; revoking the connector on the Clients page cuts it off immediately. There's no dynamic
client registration, so only connectors you create can ask you to sign in.

**Claude Code.** Run the desktop client as a local MCP server. It uses the client's own
connection (`[server] url` and `key` in `~/.goldmine/config.toml`), forwards every server tool,
and adds the tools only this computer can run: `sync` (upload SavedVariables, then refresh
Data.lua and Tasks.lua), `push` (refresh the addon's Data.lua and Tasks.lua), `addon_status`
(the plan on disk, the plan the addon last loaded, game data not uploaded yet) and
`client_status` (the server, SavedVariables and when WoW last wrote them, installed addon
copies, whether WoW is running). It also keeps the round trip with the addon safe on its own:
before any tool that reads account state it uploads SavedVariables WoW wrote since their last
upload (tracked in `~/.goldmine/uploads.json`), and `publish_plan` pushes Tasks.lua as soon as
the server accepts the plan. When the addon loads a plan it records which one
(`GoldmineDB.tasksLoaded`), and that comes back as `get_plan`'s `loaded_by`. No key goes into
Claude's config:

```bash
claude mcp add --scope user goldmine -- python3 /path/to/goldmine/desktop/goldmine.py mcp
```

The server's HTTP endpoint still works directly (`claude mcp add --transport http ... --header
"Authorization: Bearer gm_…"`), but then there's no sync or push.

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

### Trader task queue

An assistant (the "trader", see `docs/trader.md`) plans AH work and you do every action by
hand. The flow:

1. The trader calls the MCP tool `publish_plan`. The server checks the plan's shape (task kinds
   `buy post cancel craft scan mail vendor`, required fields, prices as copper ints, `expires` in
   the future, total `est_min` ≤ `budget_min`) and stores it as the active plan. Older plans are
   kept for history. Real (non-paper) plans must then pass the risk rules in `risk.py` (token floors, exposure caps, buy exits, experiment budget); `get_risk_status` shows where the account stands.
2. `goldmine.py push` (the app does this after every sync) writes `Tasks.lua` next to `Data.lua`
   in every installed copy of the addon (`GET /api/v1/addon-tasks`; when no plan is active it writes an empty plan). `/reload` loads it.
3. In game, `/goldmine tasks` (or opening the AH, mailbox, a merchant or a profession) shows the
   queue: tasks in order with time estimates and expected profit. Every price is checked live
   before its button turns on:
   - **buy**: Search → Get quote → Confirm. Confirm only turns on when the quote is
     ≤ quantity × `max_unit`. For item auctions, only listings at or under `max_unit` can be bought. Partial fills are fine.
   - **post**: the price is the lowest listing that isn't yours, minus `undercut`, clamped to
     `floor`..`ceil` and rounded to whole silver. If that listing is below `floor`, it doesn't post and records `below_floor`.
   - **cancel**: lists your auctions priced below/above the limit, cheapest first; one cancel per click.
   - **craft**: opens the recipe and fills in the count; you press Create.
   - **scan**: one search, so the server gets a live price. **mail**: attaches one stack per
     click and fills in the recipient; you press Send. **vendor**: sells one stack per click.

   One click is one protected action. The addon never chains AH, craft or mail calls and never
   acts on its own. Paper plans (`paper = true`) show up but their buttons stay off. Expired plans
   are ignored. **Skip…** asks why (too expensive, no time, out of bag space, don't trust it,
   other, plus a note).
4. Each outcome (`done partial skipped blocked below_floor expired`, with the live price seen and
   gold paid) goes to `GoldmineDB.taskResults`. The next sync uploads it and the server imports it by id.
   The trader reads it with `get_task_results` and `get_plan`, the same plan with each task's result.

### Trader learning tools (MCP)

For the trader in [docs/trader.md](docs/trader.md): test strategies on history and paper plans
before real gold is used, and keep score afterwards. All read-only except `journal_write`.

| Tool | |
|---|---|
| `backtest` | replay `flip`, `deals` or `craft` over stored history (default: last 30 days): at each decision time (13h and 1h UTC) build the plan the strategy would have published from data up to then, fill it, step on. Realized P&L, ROI, capital used, max drawdown, sell-through, days held, unsold inventory at conservative value, a sample plan, and a `backtest_id` to cite as `bt:<id>` |
| `evaluate_plan` | score a plan in the Tasks.lua format against what happened after `start_ts`: per-task fills, sales, expiries and fees |
| `get_lots` | FIFO lots from the AH ledger: open lots at cost and conservative value, days held, realized P&L per item, deposit/cancel fees |
| `get_scorecard` | per strategy tag: realized P&L, ROI, sell-through, days held, open exposure; weekly AH cash flow; prediction error of journal hypotheses |
| `journal_write` / `journal_read` | hypotheses, decisions, lessons and notes with strategy, items, a prediction, evidence refs (`task:`, `plan:`, `tx:`, `bt:`, `journal:`), expiry and supersession |

The simulator is deliberately pessimistic (assumptions in `sim.py`'s docstring): buys fill only
from listings present at or below `max_unit` in the snapshot after the decision; posts fill only
from later *definite* purchases (units bought out of listings that stayed up) at or above our
price; 5% cut, deposits (vendor price based, else 0.5% of the price per 12h) lost on expiry.
Realm AHs list single items that vanish when bought, which never registers as a definite sale,
so realm-AH items neither fill nor hold value under the default rules (`params.gone_weight`
counts vanished listings as partial sales, for a sensitivity check only). Conservative value is
`min(market value, p25 of definite sale prices over 14 days) x 0.95 - one deposit`, 0 without
sales, halved past `max_hold_days`. Strategy tags arrive with task results; until then
`get_lots`/`get_scorecard` take a `tags` map (buy transaction id or item id -> tag).

Local test run without Docker:

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
GOLDMINE_DATA=$PWD/data GOLDMINE_PORT=8787 GOLDMINE_ORIGIN=http://localhost:8787 .venv/bin/python -m goldmine_server.app
.venv/bin/python -m unittest discover -s tests                  # end-to-end tests (software passkey authenticator)
```
