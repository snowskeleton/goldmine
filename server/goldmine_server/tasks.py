"""Task queue for the trader (docs/trader.md, "Plans and tasks" and "Results").

An assistant publishes a plan through MCP (publish_plan); the client downloads the active plan as
Tasks.lua next to Data.lua; the addon's queue window walks the human through it with live price
checks and appends what happened to GoldmineDB.taskResults, which the next upload imports here.

Plans and results live in the user's game DB. Only the newest live plan is active; older ones
stay for history (superseded_by says which plan replaced them).
"""

from __future__ import annotations

import json
import re
import time

from .pricing import _lua

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id             TEXT PRIMARY KEY,
    created        INTEGER NOT NULL,
    expires        INTEGER NOT NULL,
    paper          INTEGER NOT NULL DEFAULT 0,
    budget_min     REAL NOT NULL,
    published      INTEGER NOT NULL,     -- when the server stored it
    superseded_by  TEXT,                 -- the plan published after it, if any
    body           TEXT NOT NULL         -- the normalized plan as JSON
);
CREATE TABLE IF NOT EXISTS task_results (
    id        TEXT PRIMARY KEY,          -- plan:task
    plan      TEXT NOT NULL,
    task      TEXT NOT NULL,
    char_key  TEXT,
    ts        INTEGER,
    status    TEXT NOT NULL,             -- done | partial | skipped | blocked | below_floor | expired
    qty       INTEGER,
    seen      INTEGER,                   -- live unit price the human saw, copper
    paid      INTEGER,                   -- copper spent (buys)
    reason    TEXT,
    imported  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS task_results_plan ON task_results(plan, ts);
CREATE TABLE IF NOT EXISTS plan_loads (  -- the addon's acknowledgement: the plan it last loaded, per character
    char_key   TEXT PRIMARY KEY,
    plan       TEXT NOT NULL,
    generated  INTEGER,                  -- Tasks.lua's generated time
    ts         INTEGER NOT NULL          -- when the addon loaded it (login or /reload)
);
"""

STATUSES = ("done", "partial", "skipped", "blocked", "below_floor", "expired")
PLAN_KEYS = {"plan", "created", "expires", "paper", "in_game", "budget_min", "tasks"}
# Plans the addon gets: real ones, and paper ones published with in_game. Other paper plans are
# only scored (evaluate_plan), so they neither replace nor are replaced by what's in the game.
IN_GAME = "(paper = 0 OR json_extract(body, '$.in_game') = 1)"
MAX_TASKS = 200
MAX_TEXT = 200
DURATIONS = (12, 24, 48)  # hours, as the AH offers them

# Per kind: required fields, optional fields. Every task also has id, kind, est_min (required)
# and why, ev, after (optional).
KINDS = {
    "buy": ({"item", "qty", "max_unit"}, {"sell", "hold_days"}),
    "post": ({"item", "qty", "floor", "ceil"}, {"undercut", "duration"}),
    "cancel": ({"item"}, {"below", "above"}),
    "craft": ({"recipe", "qty"}, set()),
    "scan": ({"item"}, set()),
    "mail": ({"item", "qty", "to"}, set()),
    "vendor": ({"item"}, {"qty"}),
}
COMMON_REQUIRED = {"id", "kind", "est_min"}
COMMON_OPTIONAL = {"why", "ev", "after"}
POSITIVE_INTS = {"item", "recipe", "qty", "max_unit", "sell", "floor", "ceil", "below", "above"}  # ids and copper
_PLAN_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Field order in Tasks.lua, for readable diffs; anything else follows alphabetically.
_ORDER = ["id", "kind", "item", "recipe", "qty", "max_unit", "sell", "hold_days", "floor", "ceil", "undercut", "duration",
          "below", "above", "to", "after", "est_min", "ev", "why"]


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text_error(where: str, v) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return f"{where} must be a non-empty string"
    if len(v) > MAX_TEXT or _CONTROL.search(v):
        return f"{where} must be at most {MAX_TEXT} characters with no control characters"
    return None


def _task_errors(i: int, t, seen_ids: set) -> list[str]:
    if not isinstance(t, dict):
        return [f"tasks[{i}] must be an object"]
    tid = t.get("id")
    where = f"task {tid!r}" if isinstance(tid, str) else f"tasks[{i}]"
    kind = t.get("kind")
    if kind not in KINDS:
        return [f"{where}: kind must be one of {', '.join(KINDS)}"]
    required, optional = KINDS[kind]
    required = required | COMMON_REQUIRED
    errors = [f"{where}: missing {k}" for k in sorted(required - set(t))]
    errors += [f"{where}: unknown field {k} for kind {kind}" for k in sorted(set(t) - required - optional - COMMON_OPTIONAL)]
    if not isinstance(tid, str) or not _TASK_ID.match(tid):
        errors.append(f"{where}: id must be 1-32 letters, digits, _ or -")
    elif tid in seen_ids:
        errors.append(f"{where}: duplicate id")
    for k in POSITIVE_INTS & set(t):
        if not _is_int(t[k]) or t[k] <= 0:
            errors.append(f"{where}: {k} must be a positive integer (copper for prices)")
    if "undercut" in t and (not _is_int(t["undercut"]) or t["undercut"] < 0):
        errors.append(f"{where}: undercut must be an integer >= 0 (copper)")
    if "ev" in t and not _is_int(t["ev"]):
        errors.append(f"{where}: ev must be an integer (copper)")
    if "duration" in t and t["duration"] not in DURATIONS:
        errors.append(f"{where}: duration must be 12, 24 or 48 (hours)")
    if "hold_days" in t and (not _is_num(t["hold_days"]) or t["hold_days"] <= 0):
        errors.append(f"{where}: hold_days must be a number > 0 (days)")
    if "est_min" in t and (not _is_num(t["est_min"]) or t["est_min"] < 0):
        errors.append(f"{where}: est_min must be a number >= 0 (minutes)")
    for k in ("why", "to"):
        if k in t and (err := _text_error(f"{where}: {k}", t[k])):
            errors.append(err)
    if "after" in t and t["after"] not in seen_ids:
        errors.append(f"{where}: after must name an earlier task's id")
    if kind == "post" and all(_is_int(t.get(k)) for k in ("floor", "ceil")) and t["floor"] > t["ceil"]:
        errors.append(f"{where}: floor is above ceil")
    if kind == "cancel" and "below" not in t and "above" not in t:
        errors.append(f"{where}: cancel needs below and/or above (copper per unit)")
    return errors


def validate(plan, now: int | None = None) -> list[str]:
    """Shape errors in a plan (empty when it's fine). Prices are copper ints; the plan must expire in
    the future and its tasks' est_min must fit in budget_min."""
    now = now or int(time.time())
    if not isinstance(plan, dict):
        return ["plan must be an object"]
    errors = [f"unknown plan field {k}" for k in sorted(set(plan) - PLAN_KEYS)]
    if not isinstance(plan.get("plan"), str) or not _PLAN_ID.match(plan["plan"]):
        errors.append("plan (the id) must be 1-64 letters, digits, '.', '_' or '-'")
    if not _is_int(plan.get("expires")):
        errors.append("expires must be unix seconds")
    elif plan["expires"] <= now:
        errors.append("expires is in the past")
    if "created" in plan and not _is_int(plan["created"]):
        errors.append("created must be unix seconds")
    for k in ("paper", "in_game"):
        if k in plan and not isinstance(plan[k], bool):
            errors.append(f"{k} must be true or false")
    budget = plan.get("budget_min")
    if not _is_num(budget) or budget <= 0:
        errors.append("budget_min must be a positive number (minutes)")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errors + ["tasks must be a non-empty list"]
    if len(tasks) > MAX_TASKS:
        errors.append(f"at most {MAX_TASKS} tasks")
    seen: set = set()
    for i, t in enumerate(tasks):
        errors += _task_errors(i, t, seen)
        if isinstance(t, dict) and isinstance(t.get("id"), str):
            seen.add(t["id"])
    total = sum(t["est_min"] for t in tasks if isinstance(t, dict) and _is_num(t.get("est_min")))
    if _is_num(budget) and total > budget:
        errors.append(f"tasks take {total:g} min, over budget_min {budget:g}")
    return errors


def validate_risk(conn, plan: dict) -> list[str]:
    """Portfolio risk rules (docs/trader.md, "Risk rules"): token floors, exposure caps, exits."""
    from . import risk  # risk -> lots/sim/account; imported late to keep tasks importable from ledger
    return risk.validate(conn, plan)


def normalize(plan: dict, now: int | None = None) -> dict:
    """The plan as stored and shipped: defaults filled in, tasks' keys in a fixed order."""
    out = {"plan": plan["plan"], "created": plan.get("created") or now or int(time.time()),
           "expires": plan["expires"], "paper": bool(plan.get("paper", False)), "budget_min": plan["budget_min"],
           "tasks": []}
    out["in_game"] = not out["paper"] or bool(plan.get("in_game", False))
    for t in plan["tasks"]:
        t = dict(t)
        if t["kind"] == "post":
            t.setdefault("undercut", 1)
            t.setdefault("duration", 24)
        rank = {k: i for i, k in enumerate(_ORDER)}
        out["tasks"].append({k: t[k] for k in sorted(t, key=lambda k: (rank.get(k, len(rank)), k))})
    return out


class PlanRejected(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("plan rejected: " + "; ".join(errors))
        self.errors = errors


def publish(conn, plan: dict, now: int | None = None) -> dict:
    """Validate and store a plan, making it the active one. Raises PlanRejected."""
    now = now or int(time.time())
    errors = validate(plan, now)
    if not errors:
        if conn.execute("SELECT 1 FROM plans WHERE id = ?", (plan["plan"],)).fetchone():
            errors = [f"plan id {plan['plan']!r} already exists; publish under a new id"]
        else:
            errors = validate_risk(conn, plan)
    if errors:
        raise PlanRejected(errors)
    p = normalize(plan, now)
    lane = IN_GAME if p["in_game"] else f"NOT {IN_GAME}"
    conn.execute(f"UPDATE plans SET superseded_by = ? WHERE superseded_by IS NULL AND {lane}", (p["plan"],))
    conn.execute("INSERT INTO plans (id, created, expires, paper, budget_min, published, body) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (p["plan"], p["created"], p["expires"], int(p["paper"]), p["budget_min"], now, json.dumps(p)))
    conn.commit()
    return {"plan": p["plan"], "active": True, "tasks": len(p["tasks"]),
            "est_min": sum(t["est_min"] for t in p["tasks"]), "budget_min": p["budget_min"],
            "expires": p["expires"], "paper": p["paper"], "in_game": p["in_game"]}


def active_plan(conn, now: int | None = None) -> dict | None:
    """The newest plan for the addon, unless it has expired (an expired plan is never replaced by an
    older one)."""
    now = now or int(time.time())
    row = conn.execute(f"SELECT body FROM plans WHERE superseded_by IS NULL AND expires > ? AND {IN_GAME} "
                       "ORDER BY published DESC LIMIT 1", (now,)).fetchone()
    return json.loads(row[0]) if row else None


def _result_row(r) -> dict:
    keys = ("id", "plan", "task", "char", "t", "status", "qty", "seen", "paid", "reason", "imported")
    return dict(zip(keys, r))


def get_plan(conn, plan: str | None = None, now: int | None = None) -> dict:
    """A plan (default: the active one) with each task's latest result."""
    now = now or int(time.time())
    if plan:
        row = conn.execute("SELECT body, published, superseded_by FROM plans WHERE id = ?", (plan,)).fetchone()
    else:
        row = conn.execute("SELECT body, published, superseded_by FROM plans WHERE superseded_by IS NULL "
                           "AND expires > ? ORDER BY published DESC LIMIT 1", (now,)).fetchone()
    if not row:
        return {"plan": None, "note": f"no plan {plan!r}" if plan else "no active plan",
                "in_game_plan": _in_game_id(conn, now), "addon_last_loaded": last_load(conn)}
    body = json.loads(row[0])
    body.setdefault("in_game", not body["paper"])
    results = {r["task"]: r for r in map(_result_row, conn.execute(
        "SELECT id, plan, task, char_key, ts, status, qty, seen, paid, reason, imported FROM task_results "
        "WHERE plan = ?", (body["plan"],)))}
    for t in body["tasks"]:
        t["result"] = results.get(t["id"])
    loads = [{"character": c, "at": ts} for c, ts in conn.execute(
        "SELECT char_key, ts FROM plan_loads WHERE plan = ? ORDER BY ts DESC", (body["plan"],))]
    return {"plan": body, "published": row[1], "superseded_by": row[2],
            "active": row[2] is None and body["expires"] > now, "expired": body["expires"] <= now,
            "loaded_by": loads, "in_game_plan": _in_game_id(conn, now), "addon_last_loaded": last_load(conn)}


def _in_game_id(conn, now: int) -> str | None:
    """The plan Tasks.lua carries (it can differ from the newest plan when that one is paper only)."""
    p = active_plan(conn, now)
    return p and p["plan"]


def last_load(conn) -> dict | None:
    """The plan the addon most recently loaded (any character), from its last upload."""
    row = conn.execute("SELECT char_key, plan, generated, ts FROM plan_loads ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(zip(("character", "plan", "generated", "at"), row)) if row else None


def get_results(conn, plan: str | None = None, since: int | None = None, limit: int = 200,
                offset: int = 0) -> dict:
    """Task results, newest first."""
    where, params = [], []
    if plan:
        where.append("plan = ?")
        params.append(plan)
    if since is not None:
        where.append("ts >= ?")
        params.append(int(since))
    sql = ("SELECT id, plan, task, char_key, ts, status, qty, seen, paid, reason, imported FROM task_results"
           + (" WHERE " + " AND ".join(where) if where else ""))
    limit = max(1, min(int(limit), 1000))
    rows = [_result_row(r) for r in conn.execute(sql + " ORDER BY ts DESC, id LIMIT ? OFFSET ?",
                                                 (*params, limit + 1, int(offset)))]
    more = len(rows) > limit
    return {"results": rows[:limit], "next_offset": int(offset) + limit if more else None}


def import_results(conn, data: dict, now: int | None = None) -> int:
    """Upsert GoldmineDB.taskResults by id (the addon updates a partial result in place). Returns
    how many rows were new or changed."""
    loaded = data.get("tasksLoaded")
    if isinstance(loaded, dict) and isinstance(loaded.get("plan"), str) and loaded.get("c") and loaded.get("t"):
        conn.execute("""INSERT INTO plan_loads (char_key, plan, generated, ts) VALUES (?, ?, ?, ?)
                        ON CONFLICT(char_key) DO UPDATE SET plan=excluded.plan, generated=excluded.generated,
                          ts=excluded.ts WHERE excluded.ts >= plan_loads.ts""",
                     (loaded["c"], loaded["plan"], loaded.get("generated"), loaded["t"]))
    rows = data.get("taskResults") or []
    if isinstance(rows, dict):  # sparse Lua table
        rows = [rows[k] for k in sorted(rows)]
    now = now or int(time.time())
    n = 0
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r.get("status"):
            continue
        plan, _, task = r["id"].partition(":")
        cur = conn.execute(
            """INSERT INTO task_results (id, plan, task, char_key, ts, status, qty, seen, paid, reason, imported)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 char_key=excluded.char_key, ts=excluded.ts, status=excluded.status, qty=excluded.qty,
                 seen=excluded.seen, paid=excluded.paid, reason=excluded.reason, imported=excluded.imported
               WHERE excluded.ts > task_results.ts OR excluded.status != task_results.status
                  OR excluded.qty IS NOT task_results.qty""",
            (r["id"], r.get("plan") or plan, r.get("task") or task, r.get("c"), r.get("t"), str(r["status"]),
             r.get("qty"), r.get("seen"), r.get("paid"), r.get("reason"), now))
        n += cur.rowcount
    return n


# ---------------------------------------------------------------------------
# Tasks.lua (loaded by the addon on login / /reload)
# ---------------------------------------------------------------------------

def render_lua(plan: dict | None, now: int | None = None) -> str:
    """GoldmineTasks for the addon. With no active plan, an empty plan the queue shows as such."""
    now = now or int(time.time())
    lines = ["-- Generated by goldmine.py push. Do not edit; it's overwritten.", "GoldmineTasks = {",
             f"  generated = {now},"]
    if plan:
        lines.append(f"  plan = {_lua(plan['plan'])}, created = {plan['created']}, expires = {plan['expires']},")
        lines.append(f"  paper = {_lua(bool(plan['paper']))}, budget_min = {_lua(plan['budget_min'])},")
        lines.append("  tasks = {")
        for t in plan["tasks"]:
            lines.append("    { " + ", ".join(f"{k} = {_lua(v)}" for k, v in t.items() if k != "result") + " },")
        lines.append("  },")
    else:
        lines.append("  tasks = {},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def addon_tasks(conn, now: int | None = None) -> str:
    return render_lua(active_plan(conn, now), now)
