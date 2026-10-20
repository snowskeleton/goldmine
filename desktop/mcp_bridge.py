"""`goldmine.py mcp`: a local MCP server (stdio) for assistants like Claude Code.

One connection gives an assistant everything: the server's tools (market data, account state,
the trader's plans, journal and risk rules), forwarded to the server's /api/v1/mcp with this
client's API key from config.toml, plus tools that only this computer can run:

  sync           upload the addon's SavedVariables, then download Data.lua and Tasks.lua into it
  push           download Data.lua and Tasks.lua into every installed copy of the addon
  client_status  the server this client talks to, the SavedVariables it found (and when WoW
                 last wrote them), installed copies of the addon, and whether WoW is running

    claude mcp add --scope user goldmine -- python3 /path/to/desktop/goldmine.py mcp

Newline-delimited JSON-RPC on stdin/stdout (the MCP stdio transport). stdout carries only
protocol messages; the commands' own output is captured and returned in tool results.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import subprocess
import sys
from pathlib import Path

import addon
import remote

SERVER_INFO = {"name": "goldmine", "title": "Goldmine (local client)", "version": "0.3.0"}
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LOCAL_INSTRUCTIONS = (
    "This is the local Goldmine client: server tools are forwarded with this computer's API key, and "
    "sync / push / client_status act on this computer's WoW install. Call sync before reading account state "
    "(WoW writes SavedVariables only on logout, /reload or exit) and push after publish_plan so Tasks.lua "
    "reaches the addon; the player then /reloads.")

_wow = {"type": "array", "items": {"type": "string"}, "description": "WoW install folders (default: the client's)"}
LOCAL_TOOLS = [
    {"name": "sync", "title": "Sync game data",
     "description": "Upload every Goldmine.lua SavedVariables file under the WoW folder to the server (idempotent), "
                    "then, unless push=false, download prices (Data.lua) and the active plan (Tasks.lua) into the "
                    "addon. Returns the command output and each file's last-written time.",
     "inputSchema": {"type": "object", "properties": {"push": {"type": "boolean", "default": True}},
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False}},
    {"name": "push", "title": "Push to the addon",
     "description": "Download prices (Data.lua) and the trader's active plan (Tasks.lua) into every installed copy "
                    "of the addon. Run after publish_plan; the player /reloads to load it.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False}},
    {"name": "client_status", "title": "Client status",
     "description": "The server this client uses, the SavedVariables files it found with when WoW last wrote each, "
                    "installed copies of the addon, and whether World of Warcraft is running.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]
LOCAL_NAMES = {t["name"] for t in LOCAL_TOOLS}


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Bridge:
    def __init__(self, gm, args):
        self.gm = gm            # the goldmine module (its commands and helpers)
        self.args = args        # global options: --config, --server, --wow-dir
        self._remote = None
        self._server_tools = None
        self.server_instructions = None

    # -- server side --------------------------------------------------------

    def remote(self) -> remote.Remote:
        if self._remote is None:
            self._remote = self.gm.server(self.args)
        return self._remote

    def rpc(self, method: str, params: dict | None = None) -> dict:
        """One request to the server's MCP endpoint -> its result. Raises RuntimeError on errors."""
        msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        raw = self.remote().request("POST", "/api/v1/mcp", body=json.dumps(msg).encode(), timeout=600,
                                    headers={"Content-Type": "application/json",
                                             "Accept": "application/json, text/event-stream",
                                             "MCP-Protocol-Version": PROTOCOL_VERSIONS[0]})
        reply = json.loads(raw)
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "server error"))
        return reply["result"]

    def server_tools(self) -> list[dict]:
        if self._server_tools is None:
            self._server_tools = [t for t in self.rpc("tools/list")["tools"] if t["name"] not in LOCAL_NAMES]
        return self._server_tools

    # -- local side ---------------------------------------------------------

    def _argv(self, *cmd: str, wow=True) -> list[str]:
        argv = ["--config", self.args.config]
        if self.args.server:
            argv += ["--server", self.args.server]
        argv += list(cmd)
        if wow:
            for d in self.args.wow_dir or []:
                argv += ["--wow-dir", d]
        return argv

    def _run(self, *cmd: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.gm.main(self._argv(*cmd))
        return code, out.getvalue().strip()

    def _saved_variables(self) -> list[dict]:
        out = []
        for p in self.gm.find_saved_variables(self.gm.wow_dirs(self.args)):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            out.append({"path": str(p), "written": int(m), "written_iso": _iso(m)})
        return out

    def local(self, name: str, arguments: dict) -> dict:
        if name == "client_status":
            try:
                srv = self.remote().base
                me = self.remote().get_json("/api/v1/me")
            except (self.gm.NoServer, remote.RemoteError) as exc:
                srv, me = None, {"error": str(exc)}
            wow = subprocess.run(["pgrep", "-f", "World of Warcraft"], capture_output=True).returncode == 0 \
                if sys.platform != "win32" else None
            return {"server": srv, "me": me, "wow_dirs": self.gm.wow_dirs(self.args),
                    "saved_variables": self._saved_variables(),
                    "addon": [{k: str(v) if isinstance(v, Path) else v for k, v in a.items()}
                              for a in addon.installed(self.gm.wow_dirs(self.args))],
                    "wow_running": wow}
        if name == "sync":
            code, text = self._run("sync", *(["--push"] if arguments.get("push", True) else []))
            return {"ok": code == 0, "output": text, "saved_variables": self._saved_variables()}
        if name == "push":
            code, text = self._run("push")
            return {"ok": code == 0, "output": text}
        raise KeyError(name)

    # -- protocol -----------------------------------------------------------

    def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        if "method" not in msg or "id" not in msg:
            return None  # a response, or a notification (initialized, cancelled, ...)
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            try:
                self.server_instructions = self.rpc("initialize", params).get("instructions")
            except Exception:  # noqa: BLE001 - work offline; server tools report the error when called
                self.server_instructions = None
            result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                      "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                      "instructions": " ".join(filter(None, [self.server_instructions, LOCAL_INSTRUCTIONS]))}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            try:
                tools = self.server_tools()
            except Exception:  # noqa: BLE001
                tools = []
            result = {"tools": LOCAL_TOOLS + tools}
        elif method == "tools/call":
            name, arguments = params.get("name", ""), params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error(mid, -32602, "arguments must be an object")
            if name in LOCAL_NAMES:
                try:
                    data = self.local(name, arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(data, separators=(",", ":"))}],
                              "structuredContent": data}
                    if data.get("ok") is False:
                        result["isError"] = True
                except Exception as exc:  # noqa: BLE001
                    result = _tool_error(f"{name} failed: {exc}")
            else:
                try:
                    result = self.rpc("tools/call", {"name": name, "arguments": arguments})
                except (self.gm.NoServer, remote.RemoteError, RuntimeError, ValueError) as exc:
                    result = _tool_error(f"server: {exc}")
        else:
            return _error(mid, -32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def serve(gm, args, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    bridge = Bridge(gm, args)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            replies = [_error(None, -32700, "parse error")]
        else:
            msgs = msg if isinstance(msg, list) else [msg]
            replies = [r for r in (bridge.handle(m) for m in msgs) if r is not None]
            if isinstance(msg, list) and replies:
                replies = [replies]
        for r in replies:
            stdout.write(json.dumps(r, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0
