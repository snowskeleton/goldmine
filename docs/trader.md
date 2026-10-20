# Goldmine trader: design contract

Claude plans AH work (buys, posts, crafts) for one retail realm. A human does every in-game
action. This file is the contract between the pieces built in parallel: the server's account
state, the task queue (server + addon), and the simulator/journal. Change it here first, then in
code.

## Ground rules

- **No botting.** The addon never chains protected actions: one click is one AH/craft action,
  and the human reads the live price before clicking. The trader never drives the game client.
- **Scope:** retail, Proudmoore (`us-proudmoore`) plus `us-commodities`. No material farming.
- **Time budget:** about 20 min in the morning and 30 min in the evening. Every task carries an estimate
  in minutes; a plan never exceeds the session's budget.
- **Roles:** the *trader* (a scheduled or on-demand Claude run) uses only the MCP tools. *Engineer*
  sessions change code. The trader never edits the code that scores it.

## Risk rules

Enforced in code at two points: the server rejects a plan that breaks them (`publish_plan`), and
the addon refuses a task whose live price is outside its limits. Prompt instructions are not
enough on their own.

| Rule | Default |
|---|---|
| Raw gold floor (all Proudmoore chars + warband bank) | ≥ 1 WoW token price |
| Net-worth floor: gold + conservative inventory value | ≥ 2 WoW token prices |
| Experiment loss budget (cumulative realized loss + markdowns on experiment positions) | 100,000g; when spent, only proven strategies run |
| Per-item exposure, at cost | ≤ 10% of working capital and ≤ 3 days of that item's median `sold`/day |
| Total inventory bought by the trader, at cost | ≤ 40% of working capital |
| Every buy | has an exit: target sell price, expected days to sell, max hold days |
| Max hold | after `max_hold_days`, the position is marked to conservative value and a liquidate-or-justify decision is required |

*Working capital* = raw gold − raw gold floor.

**Conservative inventory value**, per unit: `min(market value, p25 of sold prices over 14d) × 0.95`
minus the expected deposit loss. It is 0 if nothing sold in 14 days, and halved after `max_hold_days`.
This is how "you can't buy 1M mithril and say it'll sell" is enforced: buying anything moves
gold into inventory at a markdown, so a buy only pays off by actually selling.

**Score:** realized gold growth (net auction sales − purchases − deposits − cuts), weekly, and
conservative net worth in WoW tokens.

## Plans and tasks (server → addon)

The trader publishes a plan through MCP. The client downloads it as `Tasks.lua` next to
`Data.lua`, and the addon loads it on login or `/reload`.

```lua
GoldmineTasks = {
  plan = "p2026-10-20a", created = 1792500000, expires = 1792540000, -- addon ignores expired plans
  paper = false,            -- paper plans show in the addon but never enable action buttons
  budget_min = 20,
  tasks = {
    { id = "t1", kind = "buy",  item = 210930, qty = 200, max_unit = 412000,
      sell = 520000, hold_days = 5,   -- the exit; required on real (non-paper) buys
      why = "craft input for t3", est_min = 2, ev = 0 },
    { id = "t2", kind = "post", item = 211879, qty = 20, floor = 950000, ceil = 1400000,
      undercut = 1, duration = 24, est_min = 2, ev = 180000 },
    { id = "t3", kind = "craft", recipe = 430102, qty = 20, est_min = 4 },
    { id = "t4", kind = "cancel", item = 211879, below = 900000 },  -- cancel mine priced below/above
    { id = "t5", kind = "scan", item = 222731 },   -- just search it, so we get a live price
    { id = "t6", kind = "mail", item = 210930, qty = 50, to = "Altname" },
    { id = "t7", kind = "vendor", item = 12345 },
  },
}
```

Prices are copper per unit. `ev` is the trader's expected profit in copper (for sorting and
scoring). Tasks run in list order; `after = "t1"` marks a dependency.

**Live checks in the addon:**
- `buy`: commodity purchases quote a total. Confirm is enabled only if quote ≤ `qty_now × max_unit`.
  For item auctions, only listings with buyout ≤ `max_unit` can be bought. Partial fills are fine.
- `post`: price = clamp(lowest live listing that isn't mine − `undercut`, `floor`, `ceil`). If the
  lowest live listing is below `floor`, don't post; record `below_floor` and the price seen.
- Every task records the live price the human saw, which also measures how stale the snapshots were.

## Results (addon → server)

These are appended to SavedVariables, uploaded by the client like the ledger, and imported
idempotently by id.

```lua
GoldmineDB.taskResults = {
  { id = "p2026-10-20a:t1", plan = "p2026-10-20a", task = "t1", c = "Name-Proudmoore", t = 1792501234,
    status = "done" | "partial" | "skipped" | "blocked" | "below_floor" | "expired",
    qty = 120, seen = 409000, paid = 49080000, reason = "price jumped" },
}
```

A skip opens a prompt asking why: preset buttons (too expensive, no time, out of bag space, don't
trust it, other) plus free text.

When the addon loads a plan (login or `/reload`) it writes `GoldmineDB.tasksLoaded = { plan,
generated, t, c }`. That's the acknowledgement: after the next upload, `get_plan` lists the plan
under `loaded_by`, and the local client's `addon_status` reports `loaded`.

## The trader's interface

The trader talks only to the local client (`goldmine.py mcp`), never to the game's files. Reads
of account state sync new SavedVariables first, and `publish_plan` pushes `Tasks.lua` at once.
`addon_status` shows the round trip: not delivered → delivered (on disk) → loaded (acknowledged).

## Account state the trader can read (MCP)

Balances per character and in the warband bank; bags, bank and warband inventory; owned auctions
(from `OWNER_AUCTIONS_UPDATED`); the ledger (transactions); recipes with crafting operation info
(skill, expected quality, multicraft/resourcefulness, concentration cost); craft outcomes
(`TRADE_SKILL_ITEM_CRAFTED_RESULT`); item metadata (vendor price, binding, stack size); WoW token
price history (Blizzard `/data/wow/token/index`, namespace `dynamic-us`).

## Learning

- **Journal:** hypotheses (strategy, item, prediction, expected numbers), decisions, and lessons.
  Each lesson links to the trades or tasks that are its evidence and has an optional expiry.
- **Lots:** FIFO cost basis per item from the ledger. Realized P&L per item and strategy feeds
  the scorecard: ROI, sell-through, days held, prediction error.
- **Simulator:** replays stored snapshots and listing lifecycles to backtest a strategy or score a
  plan. It models fills conservatively, charges fees, and runs before any strategy uses real gold.

## MCP tool names (one owner each)

Build sessions also add a `read_only=True` keyword to `_tool()` in `mcp_server.py`, written
identically (`def _tool(..., user_db=False, read_only=True)` → `readOnlyHint: read_only`), so
their branches merge cleanly.

- Account state: `get_account`, `get_inventory`, `get_owned_auctions`, `get_ledger`,
  `get_recipes`, `get_craft_results`, `get_token_price`, `get_item_info`
- Task queue: `publish_plan`, `get_plan`, `get_task_results`
- Learning: `journal_write`, `journal_read`, `get_lots`, `get_scorecard`, `backtest`, `evaluate_plan`
- Risk (risk.py): enforced inside `publish_plan`; `get_risk_status` reports the same numbers
