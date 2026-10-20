"""MCP (Model Context Protocol) endpoint: the read-only market data in marketdata.py as tools,
so an assistant like Claude Code can query this server directly.

Streamable HTTP transport, stateless: each JSON-RPC message is POSTed to /api/v1/mcp and answered
with a single application/json response (the spec allows JSON instead of an SSE stream). There
are no server-initiated messages, so GET (the optional SSE channel) returns 405. Authenticated
with an API key as a Bearer token, or with an OAuth access token (oauth.py), which is what
Claude's Connectors use: a 401 here points them at the OAuth metadata.

    claude mcp add --transport http goldmine https://gold.example.com/api/v1/mcp \
        --header "Authorization: Bearer gm_..."

Implemented by hand (it's a few JSON-RPC methods) rather than with the MCP SDK, whose
Starlette requirements would fight FastAPI's pin.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import account as acct, auth, db, journal, ledger, lots, marketdata as md, risk, sim, tasks
from .auth import User

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "goldmine", "title": "Goldmine market data", "version": "0.2.0"}
INSTRUCTIONS = (
    "World of Warcraft auction house data collected by this Goldmine server from Blizzard's API, "
    "about hourly per market. All prices are copper per unit (1 gold = 10000 copper). Times are unix "
    "seconds with *_iso UTC copies. Call list_markets first for market names (e.g. us-proudmoore for a "
    "realm AH, us-commodities for region-wide commodities). Items are ids or names; search_items finds ids. "
    "sold = units definitely bought from listings still up; gone = units in listings that vanished before "
    "they could expire (sold or cancelled, so an upper bound on sales). get_account, get_inventory, "
    "get_owned_auctions, get_ledger, get_recipes and get_craft_results read your own account as the addon last "
    "saw it (check the *_seen / as_of times); get_token_price gives the WoW token price in copper.")

_market = {"type": "string", "description": "Market name from list_markets, e.g. us-proudmoore or us-commodities"}
_items = {"type": "array", "items": {"type": ["integer", "string"]}, "maxItems": md.MAX_ITEMS,
          "description": "Item ids, or item names (best match)"}
_hours = {"type": "integer", "minimum": 1, "maximum": 720, "default": 72,
          "description": "Window for market value and sold/day, in hours"}


def _tool(name, title, description, properties, required, fn, user_db=False, read_only=True):
    return {"name": name, "title": title, "description": description, "fn": fn, "user_db": user_db,
            "inputSchema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "openWorldHint": False}}

_realm = {"type": "string", "description": "Only characters on this realm, e.g. Proudmoore (default: all)"}
_tags = {"type": "object", "description": "Strategy tags until task results carry them: {lot ref (buy transaction "
         "id) or item id: tag, or {strategy, max_hold_days}}. Default untagged."}
_hold = {"type": "number", "minimum": 0, "default": 7, "description": "Default max hold days for lots"}
_params = {"type": "object", "description": "Overrides; unknown keys are rejected and the error lists the allowed ones"}

# Learning (sim.py, lots.py, journal.py): backtests, paper-plan scoring, cost basis, journal.
LEARNING_TOOLS = [
    _tool("backtest", "Backtest a strategy",
          "Replay a built-in strategy over stored history: at each decision time (params.decision_hours_utc, default "
          "13h and 1h UTC) it builds the plan the strategy would have published from data up to then and fills it "
          "conservatively (buys only from listings present at <= max_unit in the next snapshot; posts only from later "
          "definite purchases at >= our price; 5% cut, deposits, expiries). Strategies: flip (commodity p25->p75 "
          "round trips), deals (listings <= 70% of market value), craft (your recipes, reagents at p10). Returns "
          "realized P&L, ROI, capital used, max drawdown, sell-through, days held, unsold inventory at conservative "
          "value, per-item results and a sample plan. backtest_id can be cited as evidence (bt:<id>).",
          {"strategy": {"type": "string", "enum": list(sim.STRATEGIES)}, "params": _params,
           "since": {"type": "integer", "description": "Unix seconds (default: until - 30 days)"},
           "until": {"type": "integer", "description": "Unix seconds (default: latest snapshot)"}},
          ["strategy"], lambda conn, strategy, **kw: sim.backtest(conn, strategy, **kw), user_db=True),
    _tool("evaluate_plan", "Score a plan",
          "Score a plan in the Tasks.lua format (an object with tasks: buy/post/craft/cancel...) against what "
          "actually happened after start_ts (default plan.created): what each task would have filled, sold, expired "
          "and earned after fees, with the same fill model as backtest. Items posted or crafted from that the plan "
          "didn't buy come from `inventory` (default: your synced counts) at cost_basis or conservative value.",
          {"plan": {"type": "object"}, "start_ts": {"type": "integer"},
           "horizon_hours": {"type": "number", "minimum": 1, "maximum": 720, "default": 72},
           "inventory": {"type": "object", "description": "{item id: count} held at start"},
           "cost_basis": {"type": "object", "description": "{item id: copper per unit} for held inventory"},
           "params": _params},
          ["plan"], lambda conn, plan, **kw: sim.evaluate_plan(conn, plan, **kw), user_db=True),
    _tool("get_lots", "Cost-basis lots",
          "FIFO lots from your AH ledger: open lots at cost and at conservative value (min(market value, p25 of "
          "definite sales over 14d) x 0.95 - a deposit; 0 with no sales; halved past max hold), days held, realized "
          "P&L per item, deposit/cancel fees, purchases and sales that couldn't be matched to an item or lot.",
          {"realm": _realm, "tags": _tags, "max_hold_days": _hold},
          [], lambda conn, **kw: lots.get_lots(conn, **kw), user_db=True),
    _tool("get_scorecard", "Scorecard",
          "Per strategy tag: realized P&L, ROI, sell-through, days held, open exposure at cost and conservative value; "
          "weekly realized gold growth (AH cash flow); prediction error of journal hypotheses with "
          "prediction.realized.",
          {"realm": _realm, "tags": _tags, "max_hold_days": _hold},
          [], lambda conn, **kw: lots.get_scorecard(conn, **kw), user_db=True),
    _tool("journal_write", "Write journal entry",
          "Record a hypothesis (strategy, items, prediction such as {\"realized\": copper, \"days\": 3}), decision, "
          "lesson or note. evidence: refs like task:<plan>:<task>, plan:<id>, tx:<transaction id>, bt:<backtest id>, "
          "journal:<id>. supersedes: id of an entry this one replaces. expires_at: unix seconds after which a "
          "lesson stops applying.",
          {"kind": {"type": "string", "enum": list(journal.KINDS)}, "text": {"type": "string"},
           "strategy": {"type": "string"}, "items": {"type": "array", "items": {"type": "integer"}},
           "prediction": {"type": "object"}, "evidence": {"type": "array", "items": {"type": "string"}},
           "expires_at": {"type": "integer"}, "supersedes": {"type": "integer"}},
          ["kind", "text"], lambda conn, kind, text, **kw: journal.write(conn, kind, text, **kw),
          user_db=True, read_only=False),
    _tool("journal_read", "Read journal",
          "Journal entries, newest first, filtered by kind, strategy, item, created since; expired and superseded "
          "entries are hidden unless asked for.",
          {"kind": {"type": "string", "enum": list(journal.KINDS)}, "strategy": {"type": "string"},
           "item": {"type": "integer"}, "since": {"type": "integer"},
           "ids": {"type": "array", "items": {"type": "integer"}},
           "include_expired": {"type": "boolean", "default": False},
           "include_superseded": {"type": "boolean", "default": False},
           "limit": {"type": "integer", "minimum": 1, "maximum": journal.MAX_LIMIT, "default": 50},
           "offset": {"type": "integer", "minimum": 0, "default": 0}},
          [], lambda conn, **kw: journal.read(conn, **kw), user_db=True),
]


# Task queue (tasks.py): plans the human works through in the addon's queue, and what happened.
_plan_id = {"type": "string", "description": "Plan id"}
_task = {"type": "object", "required": ["id", "kind", "est_min"], "properties": {
    "id": {"type": "string"}, "kind": {"type": "string", "enum": list(tasks.KINDS)},
    "est_min": {"type": "number", "minimum": 0}, "ev": {"type": "integer"}, "why": {"type": "string"},
    "after": {"type": "string"}}}
TASK_TOOLS = [
    _tool("publish_plan", "Publish plan",
          "Validate and store a plan of in-game tasks, making it the active plan the addon's queue shows "
          "after the next push and /reload (older plans stay for history). Format: docs/trader.md \"Plans and "
          "tasks\": {plan: id, expires: unix s (future), budget_min, paper?: bool, created?, tasks: [{id, kind, "
          "est_min, why?, ev? (copper), after? (earlier id), ...}]}. Kinds and required fields: buy item qty "
          "max_unit; post item qty floor ceil [undercut=1 duration=24]; cancel item below|above; craft recipe "
          "qty; scan item; mail item qty to; vendor item [qty]. Prices are copper per unit (ints). Total est_min "
          "must fit budget_min. Returns the stored plan id, or the reasons it was rejected.",
          {"plan": {"type": "object", "required": ["plan", "expires", "budget_min", "tasks"], "properties": {
              "plan": {"type": "string"}, "created": {"type": "integer"}, "expires": {"type": "integer"},
              "paper": {"type": "boolean"}, "budget_min": {"type": "number", "exclusiveMinimum": 0},
              "tasks": {"type": "array", "items": _task, "minItems": 1, "maxItems": tasks.MAX_TASKS}}}},
          ["plan"], lambda conn, plan: tasks.publish(conn, plan), user_db=True, read_only=False),
    _tool("get_plan", "Get plan",
          "The active plan (or the one with this id), each task with its latest result from the addon "
          "(status done/partial/skipped/blocked/below_floor/expired, qty, seen = live unit price, paid, reason). "
          "loaded_by: characters whose addon loaded this plan (at login or /reload), from their last upload; "
          "addon_last_loaded: the plan the addon loaded most recently.",
          {"plan": _plan_id}, [], lambda conn, plan=None: tasks.get_plan(conn, plan), user_db=True),
    _tool("get_task_results", "Task results",
          "What happened to tasks in game, newest first: status, qty, seen (live unit price the human saw, "
          "copper), paid, reason (why a task was skipped). Results arrive with the client's next upload.",
          {"plan": _plan_id, "since": {"type": "integer", "description": "Unix seconds"},
           "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
           "offset": {"type": "integer", "minimum": 0, "default": 0}},
          [], lambda conn, **kw: tasks.get_results(conn, **kw), user_db=True),
]

_char = {"type": "string", "description": "Character, Name or Name-Realm"}
_since = {"type": ["integer", "string"], "description": "Unix seconds, or 7d / 12h, or an ISO date"}
_limit = {"type": "integer", "minimum": 1, "maximum": md.MAX_ROWS, "default": 200}
_offset = {"type": "integer", "minimum": 0, "default": 0}

# Account state (account.py): what the addon saw in game, as of each *_seen / as_of time.
ACCOUNT_TOOLS = [
    _tool("get_account", "Account balances",
          "Gold (copper) per character and in the warband bank, total_gold, when each was last seen (and when "
          "its bags, bank, auctions and professions were), each character's professions (skill, max_skill, "
          "needs_catalogue = recipes missing or over a week old), and the latest WoW token price per region.",
          {"realm": {"type": "string", "description": "Only characters on this realm, e.g. Proudmoore"}},
          [], lambda conn, **kw: acct.account(conn, **kw), user_db=True),
    _tool("get_inventory", "Inventory",
          "Item counts in bags, bank and the warband bank: one entry per item (count = total) with held = "
          "[{owner, location, count, seen}]. Largest stacks first, paginated.",
          {"char": _char, "location": {"type": "string", "enum": ["bags", "bank", "warband"]}, "items": _items,
           "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.inventory(conn, **kw), user_db=True),
    _tool("get_owned_auctions", "Owned auctions",
          "Your auctions from each character's latest Auctions-tab list (lists[].as_of): unit_price, quantity, "
          "status active/sold, time_left and an estimated expires. include_ended adds auctions missing from the "
          "latest list (sold, expired or cancelled) last seen since `since` (default 14d).",
          {"char": _char, "include_ended": {"type": "boolean", "default": False}, "since": _since,
           "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.owned_auctions(conn, **kw), user_db=True),
    _tool("get_ledger", "Gold ledger",
          "Every gold change the addon recorded, newest first: delta and balance (copper), source (auction_sale, "
          "auction_buy, auction_deposit, vendor, mail, ...), item, meta; plus in/out totals per source for all "
          "matching rows. Default window: the last 7 days.",
          {"since": _since, "until": _since, "source": {"type": "string"},
           "item": {"type": ["integer", "string"], "description": "Item id or name"}, "char": _char,
           "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.ledger_rows(conn, **kw), user_db=True),
    _tool("get_recipes", "Recipes",
          "Recipes your characters know, with reagents and crafting operation info as of the last time the "
          "profession window was open (basic reagents, no concentration): skill vs difficulty, expected quality, "
          "multicraft/resourcefulness %, concentration_cost; plus each profession's concentration.",
          {"char": _char, "profession": {"type": "string"},
           "item": {"type": ["integer", "string"], "description": "Recipes making or using this item"},
           "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.recipes(conn, **kw), user_db=True),
    _tool("get_craft_results", "Craft results",
          "Crafts you made, newest first (quantity, quality, multicraft extra items, resources returned, "
          "concentration spent), and by_recipe totals with proc rates and average quality. Default: last 30 days.",
          {"since": _since, "char": _char, "recipe": {"type": "integer"},
           "item": {"type": ["integer", "string"], "description": "Crafted item id or name"},
           "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.craft_results(conn, **kw), user_db=True),
    _tool("get_token_price", "WoW token price",
          "Latest WoW token price (copper, and whole gold) and its history, a row per price change, newest first. "
          "Default: the last 30 days.",
          {"region": {"type": "string", "default": "us"}, "since": _since, "limit": _limit},
          [], lambda conn, **kw: acct.token_price(conn, **kw)),
    _tool("get_item_info", "Item info",
          "Item metadata: vendor_sell/vendor_buy (copper), binding and bound (true = can't be sold on the AH), "
          "stack_size, class/subclass, expansion, level. From the Blizzard item API (cached for items you own or "
          "craft) and the game client; sources says which answered.",
          {"items": _items}, ["items"], lambda conn, items: acct.item_info(conn, items), user_db=True),
    _tool("get_auction_mail", "Auction mail",
          "AH mail from your inboxes, newest first: sale invoices (player = buyer, bid/buyout, deposit, "
          "consignment = AH cut, money = what you received; the ground-truth sale prices), won, outbid, expired "
          "and cancelled auctions; plus sales_by_item totals. invoice seller_temp_invoice = sold, money pending. "
          "Default: last 30 days.",
          {"since": _since, "char": _char,
           "kind": {"type": "string", "enum": ["sold", "won", "outbid", "expired", "cancelled"]},
           "item": {"type": ["integer", "string"], "description": "Item id or name"},
           "player": {"type": "string", "description": "Buyer or seller name"}, "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.auction_mail(conn, **kw), user_db=True),
    _tool("get_crafting_orders", "Crafting orders",
          "Crafting orders your characters saw in the crafting orders window (public, guild, personal, patron), "
          "highest commission (tip, copper) first: item, recipe, min_quality, reagents_provided, patron rewards, "
          "expires; and public_board, the public order buckets (available, tip_avg, tip_max) as last seen.",
          {"order_type": {"type": "string", "enum": ["public", "guild", "personal", "patron"]},
           "recipe": {"type": "integer"}, "item": {"type": ["integer", "string"], "description": "Item id or name"},
           "active_only": {"type": "boolean", "default": True}, "limit": _limit, "offset": _offset},
          [], lambda conn, **kw: acct.crafting_orders(conn, **kw), user_db=True),
]

TOOLS = [
    _tool("list_markets", "List markets",
          "Markets this server collects: name, kind (commodities/realm/classic), region, game, realms served, "
          "snapshot count, first and last snapshot time, items currently listed.",
          {}, [], lambda conn: md.list_markets(conn)),
    _tool("search_items", "Search items",
          "Find item ids by (partial) name, or look up an id. Only names the server has cached are searchable.",
          {"query": {"type": "string", "description": "Name fragment or item id"},
           "limit": {"type": "integer", "minimum": 1, "maximum": md.MAX_SEARCH, "default": 25},
           "offset": {"type": "integer", "minimum": 0, "default": 0}},
          ["query"], lambda conn, query, limit=None, offset=0: md.search_items(conn, query, limit, offset)),
    _tool("get_current_prices", "Current prices",
          "Latest AH state of one or more items in a market: min_price, p10, median (copper), quantity "
          "(units listed), auctions, depth5/10/25 (units within +5/10/25% of min). listed=false means none up.",
          {"market": _market, "items": _items}, ["market", "items"],
          lambda conn, market, items: md.current_prices(conn, market, items)),
    _tool("get_price_history", "Price history",
          "One item's state at every snapshot in a window, rebuilt from change-based storage. resolution raw = "
          "per snapshot; hourly/daily = per UTC bucket (lowest min_price, mean p10/median/quantity, summed "
          "sold/gone, close_* = state at the bucket's last snapshot). Paginated with offset/next_offset.",
          {"market": _market, "item": {"type": ["integer", "string"], "description": "Item id or name"},
           "variant": {"type": "integer", "default": 0, "description": "Battle pet species id, else 0"},
           "since": {"type": "integer", "description": "Unix seconds (default: until - 7 days)"},
           "until": {"type": "integer", "description": "Unix seconds (default: now)"},
           "resolution": {"type": "string", "enum": list(md.RESOLUTIONS), "default": "hourly"},
           "limit": {"type": "integer", "minimum": 1, "maximum": md.MAX_ROWS, "default": 500},
           "offset": {"type": "integer", "minimum": 0, "default": 0}},
          ["market", "item"], lambda conn, market, item, **kw: md.price_history(conn, market, item, **kw)),
    _tool("get_market_values", "Market values",
          "Goldmine's price model for a market: market_value (time-weighted p10 over the window), current_min, "
          "current_p10, buy, sell, sold_per_day. With items: just those; without: every item, busiest first.",
          {"market": _market, "items": _items, "hours": _hours,
           "min_sold": {"type": "number", "minimum": 0, "default": 0, "description": "Only items selling this many/day"},
           "limit": {"type": "integer", "minimum": 1, "maximum": md.MAX_ITEMS, "default": 50},
           "offset": {"type": "integer", "minimum": 0, "default": 0}},
          ["market"], lambda conn, market, items=None, **kw: md.market_values(conn, market, items, **kw)),
    _tool("get_deals", "Deals",
          "Items in a market listed at or below max_ratio of their market value that sell at least min_sold "
          "units/day. profit_per_unit is after the 5% AH cut.",
          {"market": _market, "hours": _hours,
           "max_ratio": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7},
           "min_sold": {"type": "number", "minimum": 0, "default": 1.0},
           "limit": {"type": "integer", "minimum": 1, "maximum": md.MAX_ITEMS, "default": 40}},
          ["market"], lambda conn, market, **kw: md.deals(conn, market, **kw)),
    _tool("get_crafts", "Craft report",
          "Profit per craft for recipes the key owner's characters know, per realm: cost at current p10, "
          "revenue at min(market value, lowest listing) after the AH cut, and sold/day.",
          {"hours": _hours, "limit": {"type": "integer", "minimum": 1, "maximum": md.MAX_ITEMS, "default": 40}},
          [], lambda conn, **kw: md.crafts(conn, **kw), user_db=True),
    *TASK_TOOLS,
    *LEARNING_TOOLS,
    *ACCOUNT_TOOLS,
    _tool("get_risk_status", "Risk status",
          "Where the account stands against the trader's risk rules (docs/trader.md): gold and net worth (gold + "
          "conservative inventory value) in copper and in WoW tokens, the raw (1 token) and net (2 token) floors and "
          "headroom, working capital, per-item and total inventory caps, trader inventory at cost, trader realized "
          "P&L and experiment budget left, and whether real buys are allowed. publish_plan enforces the same rules.",
          {"realm": {"type": "string", "default": risk.REALM, "description": "Realm whose characters count"}},
          [], lambda conn, **kw: risk.status(conn, **kw), user_db=True),
]
BY_NAME = {t["name"]: t for t in TOOLS}


def _call_tool(user: User, name: str, arguments: dict) -> dict:
    tool = BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    allowed = tool["inputSchema"]["properties"]
    unknown = set(arguments) - set(allowed)
    if unknown:
        return _error_result(f"unknown arguments: {', '.join(sorted(unknown))}")
    conn = md.ro_conn(user.id if tool["user_db"] else None) if tool["annotations"]["readOnlyHint"] \
        else _write_conn(user.id)
    try:
        result = tool["fn"](conn, **arguments)
    except HTTPException as exc:
        return _error_result(str(exc.detail))
    except tasks.PlanRejected as exc:
        return _error_result(str(exc))
    except (TypeError, ValueError) as exc:
        return _error_result(f"bad arguments: {exc}")
    finally:
        conn.close()
    return {"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}],
            "structuredContent": result}


def _write_conn(user_id: int):
    """For write tools: market.sqlite still read-only (mode=ro), the user's game DB attached as "u" writable."""
    conn = md.ro_conn()
    conn.execute("PRAGMA query_only = 0")
    path = db.user_db_path(user_id)
    ledger.connect(str(path)).close()
    conn.execute("ATTACH DATABASE ? AS u", (str(path),))
    return conn


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _handle(user: User, msg) -> dict | None:
    """One JSON-RPC message -> response dict, or None for notifications and responses."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "invalid request")
    if "method" not in msg:
        return None  # a response to something we never send
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None  # notification (notifications/initialized, cancelled, ...)
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{k: v for k, v in t.items() if k not in ("fn", "user_db")} for t in TOOLS]}
    elif method == "tools/call":
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(mid, -32602, "arguments must be an object")
        try:
            result = _call_tool(user, params.get("name", ""), args)
        except KeyError:
            return _rpc_error(mid, -32602, f"unknown tool {params.get('name')!r}")
    else:
        return _rpc_error(mid, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


router = APIRouter()


@router.post("/api/v1/mcp")
async def mcp_post(request: Request, user: User = Depends(auth.market_user)):
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)
    if isinstance(body, list):  # JSON-RPC batch (2025-03-26)
        replies = [r for r in [await run_in_threadpool(_handle, user, m) for m in body] if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await run_in_threadpool(_handle, user, body)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/api/v1/mcp")
def mcp_get(user: User = Depends(auth.market_user)):
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/api/v1/mcp")
def mcp_delete(user: User = Depends(auth.market_user)):
    return Response(status_code=405, headers={"Allow": "POST"})
