"""Serve a frozen brainstem's agents as MCP tools, so Copilot Studio runs the real agent.py.

    python3 -m brainfreeze_studio serve invoice-desk.egg --port 7700 [--api-key ...]

The agents run inside the RAPP engine they expect: the grail brainstem.py at the commit the egg
pins, loaded with the kernel's own ``load_agents()``. They get the same BasicAgent, the same
storage shims and their own memory folder, exactly as in a brainstem. Only the transport
changes: each agent becomes an MCP tool (Streamable HTTP, JSON responses), which a Copilot
Studio ``McpTool`` reaches through a custom connector.

Security: bound to loopback by default. Binding to any other address requires an API key,
sent as ``Authorization: Bearer <key>`` or ``x-api-key: <key>``. Agent code runs with this
process's permissions: serve only eggs you trust.

Standard library only (the engine itself needs its own requirements; ``serve`` sets up a venv).
"""
import hmac
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import StudioBuildError, _open_egg, rapp1

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
GRAIL = "https://github.com/kody-w/rapp-installer.git"
HOME = Path(os.getenv("BRAINFREEZE_STUDIO_HOME", Path.home() / ".brainfreeze-studio"))
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# ── hosting the egg's agents in their engine ─────────────────────────────────

def engine_for(manifest, engine_dir=None):
    """A grail rapp_brainstem/ folder at the commit the egg expects (cached, read-only clone)."""
    if engine_dir:
        d = Path(engine_dir).expanduser()
        d = d / "rapp_brainstem" if (d / "rapp_brainstem" / "brainstem.py").exists() else d
        if not (d / "brainstem.py").exists():
            raise StudioBuildError(f"no brainstem.py in {d}")
        return d
    engine = (manifest.get("payload") or {}).get("engine") or {}
    commit = engine.get("commit") if engine.get("source") == "grail" else ""
    ref = commit or "main"
    cache = HOME / "engines" / (commit[:12] if commit else "grail-main")
    if not (cache / "rapp_brainstem" / "brainstem.py").exists():
        shutil.rmtree(cache, ignore_errors=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", GRAIL, str(cache)], check=True)
        if commit:
            subprocess.run(["git", "-C", str(cache), "fetch", "-q", "--depth", "1", "origin", ref], check=True)
            subprocess.run(["git", "-C", str(cache), "checkout", "-q", "FETCH_HEAD"], check=True)
        subprocess.run(["git", "-C", str(cache), "remote", "set-url", "--push", "origin", "DISABLED-read-only"],
                       check=True)
    return cache / "rapp_brainstem"


def host_agents(egg, engine_dir=None):
    """Lay the egg onto a private copy of its engine and load its agents with the kernel's loader."""
    manifest, files = _open_egg(egg, "organism")
    engine = engine_for(manifest, engine_dir)
    work = HOME / "hosts" / rapp1.egg_address(manifest)[:16] / "rapp_brainstem"
    if not work.exists():
        shutil.copytree(engine, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".env",
                                                                     ".copilot_token", ".copilot_session",
                                                                     ".brainstem_secret"))
        for f in (work / "agents").glob("*_agent.py"):          # only the egg's agents, not the stock ones
            if f.name != "basic_agent.py":
                f.unlink()
        for path, octets in files.items():                      # memory is laid once; it then lives here
            if path == "rappid.json":
                continue
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(octets)
    sys.path.insert(0, str(work))
    os.chdir(work)
    spec = importlib.util.spec_from_file_location("brainstem", work / "brainstem.py")
    kernel = importlib.util.module_from_spec(spec)
    sys.modules["brainstem"] = kernel
    spec.loader.exec_module(kernel)
    agents = kernel.load_agents()
    return agents, {"rappid": manifest["rappid"], "address": rapp1.egg_address(manifest), "workdir": str(work)}


# ── MCP over Streamable HTTP ─────────────────────────────────────────────────

def tool_list(agents):
    tools = []
    for name, agent in sorted(agents.items()):
        fn = agent.to_tool().get("function", {})
        schema = fn.get("parameters") or {}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        tools.append({"name": fn.get("name") or name, "description": fn.get("description") or "",
                      "inputSchema": schema})
    return tools


class McpApp:
    """The JSON-RPC side of MCP: initialize, ping, tools/list, tools/call. Transport-free, so it is testable."""

    def __init__(self, agents, info=None, server_name="brainfreeze-studio"):
        self.agents, self.info, self.server_name = agents, info or {}, server_name
        self.lock = threading.Lock()                              # agent code is not assumed thread-safe

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None                                           # a notification: no response
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(mid, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": self.server_name, "version": "0.2.0"},
                                 "instructions": "Tools are the agents of a frozen RAPP brainstem "
                                                 f"({self.info.get('rappid', 'unknown egg')}); each runs its real code."})
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": tool_list(self.agents)})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            agent = self.agents.get(name)
            if agent is None:
                return _error(mid, -32602, f"unknown tool: {name}")
            if not isinstance(args, dict):
                return _error(mid, -32602, "arguments must be an object")
            try:
                with self.lock:
                    out = agent.perform(**args)
                return _result(mid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
            except Exception as e:                                # a tool error, reported to the model
                return _result(mid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                                     "isError": True})
        return _error(mid, -32601, f"method not found: {method}")


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def make_server(app, host="127.0.0.1", port=7700, api_key=None, path="/mcp"):
    if host not in _LOOPBACK and not api_key:
        raise StudioBuildError("refusing to serve beyond this machine without --api-key")

    class Handler(BaseHTTPRequestHandler):
        server_version = "brainfreeze-studio"

        def log_message(self, fmt, *a):                           # quiet; one line per call
            sys.stderr.write("[mcp] %s %s\n" % (self.command, fmt % a if a else fmt))

        def _send(self, status, body=None, headers=None):
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _authorized(self):
            if not api_key:
                return True
            given = self.headers.get("x-api-key") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                given = given or auth[7:]
            return hmac.compare_digest(given.encode(), api_key.encode())

        def do_POST(self):
            if self.path.split("?")[0] != path:
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "missing or wrong API key"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                msg = json.loads(self.rfile.read(min(length, 8 * 1024 * 1024)) or b"null")
            except ValueError:
                return self._send(400, _error(None, -32700, "parse error"))
            if isinstance(msg, list):
                replies = [r for r in (app.handle(m) for m in msg) if r is not None]
                return self._send(200, replies) if replies else self._send(202)
            reply = app.handle(msg)
            if reply is None:
                return self._send(202)
            headers = {"Mcp-Session-Id": "brainfreeze"} if msg.get("method") == "initialize" else None
            return self._send(200, reply, headers)

        def do_GET(self):                                         # no server-initiated stream
            self._send(405, {"error": "this server answers POST only"}, {"Allow": "POST"})

        def do_DELETE(self):
            self._send(200 if self.path.split("?")[0] == path else 404)

    return ThreadingHTTPServer((host, port), Handler)


# ── the connector Copilot Studio uses to reach this server ───────────────────

def connector_definition(host, base_path="/", title="Brainstem Agents MCP"):
    """OpenAPI 2.0 + properties for `pac connector create`: a Streamable-HTTP MCP server behind an API key."""
    openapi = {
        "swagger": "2.0",
        "info": {"title": title, "description": "The agents of a frozen RAPP brainstem, each running its real "
                                                "agent.py, served over MCP by brainfreeze-studio.",
                 "version": "1.0.0"},
        "host": host, "basePath": base_path, "schemes": ["https"],
        "consumes": ["application/json"], "produces": ["application/json"],
        "securityDefinitions": {"api_key": {"type": "apiKey", "in": "header", "name": "x-api-key"}},
        "security": [{"api_key": []}],
        "paths": {"/mcp": {"post": {
            "summary": title, "description": "Invoke the brainstem's agents over MCP.",
            "operationId": "InvokeMCP", "x-ms-agentic-protocol": "mcp-streamable-1.0",
            "responses": {"200": {"description": "Success"}}}}},
    }
    properties = {"properties": {"connectionParameters": {"api_key": {
        "type": "securestring",
        "uiDefinition": {"displayName": "API key", "description": "The --api-key the MCP server was started with",
                         "tooltip": "Sent as the x-api-key header", "constraints": {"tabIndex": 2, "clearText": False,
                                                                                     "required": "true"}}}},
        "iconBrandColor": "#007ee5", "capabilities": [], "publisher": "brainfreeze-studio",
        "stackOwner": "brainfreeze-studio"}}
    return openapi, properties
