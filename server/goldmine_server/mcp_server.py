"""MCP (Model Context Protocol) endpoint: the read-only market data in marketdata.py as tools,
so an assistant like Claude Code can query this server directly.

Streamable HTTP transport, stateless: each JSON-RPC message is POSTed to /api/v1/mcp and answered
with a single application/json response (the spec allows JSON instead of an SSE stream). There
are no server-initiated messages, so GET (the optional SSE channel) returns 405. Authenticated
with a client key as a Bearer token, like the rest of /api/v1:

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

from . import auth, db, marketdata as md, tasks
from .auth import User

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "goldmine", "title": "Goldmine market data", "version": "0.2.0"}
INSTRUCTIONS = (
    "World of Warcraft auction house data collected by this Goldmine server from Blizzard's API, "
    "about hourly per market. All prices are copper per unit (1 gold = 10000 copper). Times are unix "
    "seconds with *_iso UTC copies. Call list_markets first for market names (e.g. us-proudmoore for a "
    "realm AH, us-commodities for region-wide commodities). Items are ids or names; search_items finds ids. "
    "sold = units definitely bought from listings still up; gone = units in listings that vanished before "
    "they could expire (sold or cancelled, so an upper bound on sales).")

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
          "(status done/partial/skipped/blocked/below_floor/expired, qty, seen = live unit price, paid, reason).",
          {"plan": _plan_id}, [], lambda conn, plan=None: tasks.get_plan(conn, plan), user_db=True),
    _tool("get_task_results", "Task results",
          "What happened to tasks in game, newest first: status, qty, seen (live unit price the human saw, "
          "copper), paid, reason (why a task was skipped). Results arrive with the client's next upload.",
          {"plan": _plan_id, "since": {"type": "integer", "description": "Unix seconds"},
           "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
           "offset": {"type": "integer", "minimum": 0, "default": 0}},
          [], lambda conn, **kw: tasks.get_results(conn, **kw), user_db=True),
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
    if tool["annotations"]["readOnlyHint"]:
        conn = md.ro_conn(user.id if tool["user_db"] else None)
    else:
        conn = db.user_conn(user.id)  # write tools: the user's game DB, writable
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
async def mcp_post(request: Request, user: User = Depends(auth.client_user)):
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
def mcp_get(user: User = Depends(auth.client_user)):
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/api/v1/mcp")
def mcp_delete(user: User = Depends(auth.client_user)):
    return Response(status_code=405, headers={"Allow": "POST"})
