"""A local stand-in for the Power Apps player, to run a packaged code app headless under the code app policy.

It serves the app's dist/ with the default code app content security policy, a player page that embeds it, and the
stand-in SDK's answers: a flow runs through brainfreeze_studio.flows.run_flow (the same offline evaluator the parity
proofs use), and the app's chat flow, which reaches the Copilot Studio agent, is answered by a function the test
supplies. Every SDK call is recorded.
"""
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brainfreeze_studio import codeapp, flows

HERE = Path(__file__).parent / "browser"
FAKE_SDK = HERE / "fake_power_apps.js"
CONTEXT = {"app": {"appId": "test-app", "environmentId": "00000000-0000-0000-0000-000000000000", "appSettings": {},
                   "queryParams": {}},
           "user": {"fullName": "Ada Lovelace", "userPrincipalName": "ada@example.com",
                    "objectId": "11111111-1111-1111-1111-111111111111", "tenantId": "22222222-2222-2222-2222-222222222222"},
           "host": {"sessionId": "test-session"}}


def build_test_host(outfile):
    """host.js bundled with the stand-in SDK instead of @microsoft/power-apps."""
    return codeapp.build_host(outfile=outfile, sdk_alias=str(FAKE_SDK), log=lambda *a: None)


def condition(expr, ctx):
    """A flow If's expression ({"and": [{"not": {"equals": [a, b]}}, ...]}), evaluated as Power Automate does."""
    (op, arg), = expr.items()
    if op in ("and", "or"):
        values = [condition(x, ctx) for x in arg]
        return all(values) if op == "and" else any(values)
    if op == "not":
        return not condition(arg, ctx)
    return flows.FUNCTIONS[op](*(flows.evaluate(x, ctx) for x in arg))


def files_sent(flow, given, library):
    """What a files flow hands its code: each Read_file_<input> If runs its guard and, when it holds, reads the path
    from the SharePoint folder (library: {path in the folder: bytes}); then Run_the_agent's files member is evaluated.
    Everything is the flow's own expressions; only SharePoint is a stand-in."""
    import base64
    d = flow["properties"]["definition"]
    params = {k: v.get("defaultValue") for k, v in d["parameters"].items()}
    folder = next(v for k, v in params.items() if "Files Folder" in k)
    sharepoint = {f"{folder}/{rel}": data for rel, data in library.items()}
    ctx = {"trigger": dict(given), "outputs": {}, "parameters": params, "actions": {}}
    for name, action in d["actions"].items():
        if action["type"] == "Compose":
            ctx["outputs"][name] = flows.evaluate(action["inputs"], ctx)
        elif name.startswith("Read_file_"):
            (get_name, get), = action["actions"].items()
            if not condition(action["expression"], ctx):
                ctx["actions"][get_name] = {"status": "Skipped"}
                continue
            path = flows.evaluate(get["inputs"]["parameters"]["path"], ctx)
            if path in sharepoint and not sharepoint[path]:    # an empty file comes back with no body (seen live)
                ctx["actions"][get_name] = {"status": "Succeeded", "outputs": {"statusCode": 200,
                                                                               "headers": {"Content-Length": "0"}}}
            elif path in sharepoint:
                ctx["actions"][get_name] = {"status": "Succeeded", "outputs": {"body": {
                    "$content-type": "application/octet-stream",
                    "$content": base64.b64encode(sharepoint[path]).decode()}}}
            else:
                ctx["actions"][get_name] = {"status": "Failed", "outputs": {"body": {"status": 404,
                                                                                    "message": "File not found"}}}
    sent = d["actions"]["Run_the_agent"]["inputs"]["parameters"]["body/files"]
    return {k: {kk: flows.evaluate(vv, ctx) for kk, vv in entry.items()} for k, entry in sent.items()}


class Player:
    def __init__(self, dist, flow_definitions=None, copilot=None, csp=codeapp.DEFAULT_CSP, connector_code=None):
        self.dist = Path(dist)
        self.flows = dict(flow_definitions or {})
        self.copilot = copilot or (lambda message: "")
        self.csp = csp
        self.calls = []
        self.server = None
        # connector-code flows: {placeholder display name: compiled runner dll}; their workspace files live here,
        # as the flow keeps them in Dataverse notes
        self.connector_code = dict(connector_code or {})
        self.notes = {}
        # the SharePoint folder those flows read the files a call names from: {path: bytes}
        self.library = {}

    def _sdk(self, request):
        kind, payload = request.get("kind"), request.get("payload") or {}
        if kind == "context":
            return CONTEXT
        op = (payload.get("operation") or {}).get("connectorOperation") or {}
        table, name, params = op.get("tableName"), op.get("operationName"), op.get("parameters") or {}
        call = {"table": table, "operation": name, "parameters": params,
                "known": table in (payload.get("dataSources") or [])}
        self.calls.append(call)
        if not call["known"]:
            return {"success": False, "error": {"message": f"{table} isn't in the app's dataSourcesInfo"}}
        if name == "Run" and table in self.flows:
            flow, given = self.flows[table], params.get("input") or {}
            if codeapp.is_chat_broker(flow):            # the agentic runtime: the test's stand-in agent answers
                return {"success": True, "data": {"reply": self.copilot(given.get("message") or ""),
                                                  "conversation_id": "conv-1"}}
            code = self._connector_code(flow)
            if code:
                return {"success": True, "data": self._run_code(flow, code, given)}
            try:
                return {"success": True, "data": flows.run_flow(flow, given)}
            except Exception as e:  # noqa: BLE001 - reported to the app as the SDK would
                return {"success": False, "error": {"message": f"{type(e).__name__}: {e}"}}
        return {"success": False, "error": {"message": f"no stand-in for {table}.{name}"}}

    def _connector_code(self, flow):
        text = json.dumps(flow)
        return next((dll for name, dll in self.connector_code.items() if "{{CONNECTOR:%s}}" % name in text), None)

    def _run_code(self, flow, dll, given):
        """What a connector-code flow does: read its files, run the code, keep what it wrote, answer its output."""
        import time
        import uuid
        from brainfreeze_studio import connector_code
        actions = flow["properties"]["definition"]["actions"]
        run = actions["Run_the_agent"]["inputs"]["parameters"]
        files = list(run["body/state"])
        body = {"args": {k: given.get(k) for k in run["body/args"]}, "state": {f: self.notes.get(f) for f in files},
                "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "id": str(uuid.uuid4())}
        if "body/files" in run:
            body["files"] = files_sent(flow, given, self.library)
        raw = connector_code.run_script(dll, [{"operationId": "Run", "body": body}])[0]
        reply = json.loads(raw["body"])
        self.notes.update(reply.get("state") or {})
        return {"result": reply["output"]}

    def start(self):
        player = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype, csp=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if csp:
                    self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/player.html"):
                    return self._send(200, (HERE / "player.html").read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/app/"):
                    f = (player.dist / path[len("/app/"):]).resolve()
                    if player.dist.resolve() in f.parents and f.is_file():
                        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                        if ctype.startswith("text/") or ctype.endswith("javascript"):
                            ctype += "; charset=utf-8"
                        return self._send(200, f.read_bytes(), ctype, player.csp)
                return self._send(404, b"not found", "text/plain", player.csp)

            def do_POST(self):
                if self.path != "/_sdk":
                    return self._send(404, b"not found", "text/plain")
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                body = json.dumps(player._sdk(request)).encode()
                return self._send(200, body, "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


VIOLATION_PROBE = ("document.addEventListener('securitypolicyviolation', e => console.error("
                   "'CSP-VIOLATION ' + e.violatedDirective + ' ' + e.blockedURI + ' ' + (e.sourceFile || '') + ':' + e.lineNumber));")


def open_player(playwright, base):
    """A headless page on the player, with every content security policy violation in any frame collected."""
    browser = playwright.chromium.launch()
    page = browser.new_page()
    log = {"violations": [], "errors": [], "console": []}

    def on_console(m):
        log["console"].append(f"{m.type}: {m.text}")
        if m.text.startswith("CSP-VIOLATION") or "Content Security Policy" in m.text:
            log["violations"].append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: log["errors"].append(str(e)))
    page.add_init_script(VIOLATION_PROBE)
    page.goto(base + "/player.html")
    return browser, page, log
