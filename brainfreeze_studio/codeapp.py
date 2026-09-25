"""A rapplication's UI (ui.html) as a Power Apps code app.

    from brainfreeze_studio import codeapp
    app = codeapp.package(rapp, "out", schema_name="rapp_JsonDoctor", environment_id="…", tools=[...])
    # out/codeapp/dist/  the app to publish (brainfreeze_studio.codeapp_publish)
    # out/codeapp/power.config.json, out/codeapp/report.json

The UI runs unchanged in a same-origin iframe inside a host page (brainfreeze_studio/codeapp_host, built once). Code
apps enforce a content security policy by default: scripts only from the app itself, no inline scripts or `on*=`
attributes, no outbound network. So the build rewrites the UI, without running any of it:

  inline <script> blocks   → same-origin files (rapp/inline-N.js)
  on*= attributes          → a same-origin file of handlers, attached as the page builds (rapp/rapp-handlers.js)
  javascript: links        → click handlers, the same way
  external scripts, styles → vendored into rapp/vendor/ with their SHA-256 (size-capped); fonts can't load
  calls to its brainstem   → rapp/rapp-bridge.js (first script) sends /chat and friends to the host page

and reports what still can't work under the default policy: remote fetches, eval and new Function, document.write,
and markup the page builds at run time with inline handlers in it.
"""
import hashlib
import html
import json
import re
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path

HOST_DIR = Path(__file__).with_name("codeapp_host")
VENDOR_LIMIT = 8 * 1024 * 1024    # per vendored file (mermaid is 3 MB)
FLOW_API_VERSION = "2015-02-01-preview"

# The default content security policy for code apps (Microsoft Learn, code-apps/how-to/content-security-policy),
# with the platform's own sources left out: what the app itself may use.
DEFAULT_CSP = ("frame-ancestors 'self' https://*.powerapps.com; script-src 'self'; img-src 'self' data:; "
               "style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'none'; frame-src 'self'; "
               "form-action 'none'; base-uri 'self'; child-src 'none'; default-src 'self'; manifest-src 'none'; "
               "media-src 'self' data:; object-src 'self' data:; worker-src 'none'")

COPILOT_STUDIO_API = "/providers/Microsoft.PowerApps/apis/shared_microsoftcopilotstudio"
LOGIC_FLOWS_API = "/providers/Microsoft.PowerApps/apis/shared_logicflows"
AGENTIC_RUNTIME_OPERATION = "ExecuteCopilotAsyncV2OnAgenticRuntime"


def flow_info():
    return {"tableId": "", "version": "", "primaryKey": "", "dataSourceType": "Connector",
            "apis": {"Run": {"path": "/{connectionId}/triggers/manual/run", "method": "POST",
                             "parameters": [{"name": "connectionId", "in": "path", "required": True, "type": "string"},
                                            {"name": "input", "in": "body", "required": True, "type": "object"},
                                            {"name": "api-version", "in": "query", "required": True, "type": "string"}],
                             "responseInfo": {"200": {"type": "object"}, "default": {"type": "object"}}}}}


def data_source_name(display_name):
    """How the Power Apps tooling names a flow's data source: its display name, lowercased, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", display_name.lower())


# ── flows the app calls ───────────────────────────────────────────────────────────────────────────────────────────

def powerapps_twin(flow):
    """The same agent flow for Power Apps: code apps call only instant flows with the Power Apps trigger, so only the
    trigger and the response change. The actions, and so the flow's proven output, stay exactly the same."""
    twin = json.loads(json.dumps(flow))
    definition = twin["properties"]["definition"]
    trigger = definition["triggers"]["manual"]
    trigger["kind"] = "PowerAppV2"
    props = trigger.setdefault("inputs", {}).setdefault("schema", {}).setdefault("properties", {})
    for name, p in props.items():
        p.setdefault("title", name)
        p.setdefault("x-ms-dynamically-added", True)
        p.setdefault("x-ms-content-hint", "TEXT")
    for action in definition["actions"].values():
        if action.get("type") == "Response":
            action["kind"] = "PowerApp"
    return twin


def chat_broker(schema_name, reference_logical_name):
    """The flow a code app talks to its agent through. A GitHub Copilot harness agent can't be called with the
    Copilot Studio connector's plain Execute Agent actions (they answer "This action doesn't support agents built
    with the GitHub Copilot harness"), so the app runs this Power Apps flow, which calls the agent on the agentic
    runtime (ExecuteCopilotAsyncV2OnAgenticRuntime, a webhook action) and returns its last reply and the
    conversation, so the next message continues it."""
    agent = "Ask_the_agent"
    text = {"type": "string", "x-ms-content-hint": "TEXT", "x-ms-dynamically-added": True}
    return {"properties": {"connectionReferences": {"shared_microsoftcopilotstudio": {
        "api": {"name": "shared_microsoftcopilotstudio"}, "runtimeSource": "embedded",
        "connection": {"connectionReferenceLogicalName": reference_logical_name}}},
        "definition": {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                           "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
            "triggers": {"manual": {"type": "Request", "kind": "PowerAppV2", "inputs": {"schema": {
                "type": "object", "required": ["message"], "properties": {
                    "message": {**text, "title": "message", "description": "What to send the agent"},
                    "conversation_id": {**text, "title": "conversation_id",
                                        "description": "The conversation to continue; empty starts one"}}}}}},
            "actions": {
                agent: {"type": "OpenApiConnectionWebhook", "inputs": {
                    "host": {"apiId": COPILOT_STUDIO_API, "connectionName": "shared_microsoftcopilotstudio",
                             "operationId": AGENTIC_RUNTIME_OPERATION},
                    "parameters": {"Copilot": schema_name, "body/message": "@triggerBody()?['message']",
                                   "x-ms-conversation-id": "@triggerBody()?['conversation_id']"},
                    "retryPolicy": {"type": "none"}}},
                "Respond_to_the_app": {"type": "Response", "kind": "PowerApp", "runAfter": {agent: ["Succeeded"]},
                                       "inputs": {"statusCode": 200, "body": {
                    "reply": f"@coalesce(body('{agent}')?['lastResponse'], last(coalesce(body('{agent}')?['responses'], json('[]'))), '')",
                    "conversation_id": f"@coalesce(body('{agent}')?['conversationId'], body('{agent}')?['conversationID'], triggerBody()?['conversation_id'], '')"},
                    "schema": {"type": "object", "properties": {"reply": {"type": "string"},
                                                                 "conversation_id": {"type": "string"}}}}}},
            "outputs": {}},
        "templateName": ""}, "schemaVersion": "1.0.0.0"}


def is_chat_broker(definition):
    return AGENTIC_RUNTIME_OPERATION in json.dumps(((definition.get("properties") or {}).get("definition") or {}).get("actions") or {})


# ── the UI, rewritten for the code app policy ─────────────────────────────────────────────────────────────────────

EVENT_ATTR = re.compile(r"^on[a-z]+$")
REMOTE = re.compile(r"^(https?:)?//", re.I)
RISKS = [
    (re.compile(r"\beval\s*\("), "calls eval(), which the default policy blocks"),
    (re.compile(r"\bnew\s+Function\s*\("), "builds code with new Function(), which the default policy blocks"),
    (re.compile(r"\bdocument\.write(ln)?\s*\("), "uses document.write()"),
    (re.compile(r"""\bon(?:click|change|input|submit|keydown|keyup|load|mouseover|mouseout|focus|blur)\s*=\s*\\?["']"""),
     "builds markup with inline event handlers at run time: calls with literal arguments run through the bridge, "
     "anything else is blocked by the default policy"),
    (re.compile(r"""\bfetch\s*\(\s*[`'"]https?://(?!localhost|127\.0\.0\.1)"""),
     "fetches remote URLs; code apps allow no network access"),
    (re.compile(r"\bnew\s+(WebSocket|EventSource)\s*\("), "opens a live connection; code apps allow none"),
    (re.compile(r"\bnew\s+(RTCPeerConnection|Peer)\s*\("), "connects to peers (WebRTC), whose signaling code apps block"),
]
# hosts a script names: a fetch to any of them is blocked under connect-src 'none'. (XML namespaces aren't hosts.)
REMOTE_HOST = re.compile(r"""["'`]https?://([a-z0-9.-]+\.[a-z]{2,})(?::\d+)?[/"'`?]""", re.I)
NOT_FETCHED = {"www.w3.org", "schemas.xmlsoap.org", "schemas.microsoft.com", "purl.org", "json-schema.org"}


class _Rewriter(HTMLParser):
    def __init__(self, fetch_vendor):
        super().__init__(convert_charrefs=False)
        self.out, self.files, self.report = [], {}, {"inline_scripts": 0, "handlers": 0, "vendored": [],
                                                     "dropped": [], "risks": []}
        self.handlers, self.fetch_vendor = [], fetch_vendor
        self.in_script, self.script_attrs, self.script_text = False, None, []
        self.bridge_placed = False
        self.skip_script = False      # inside a dropped remote <script src>: its end tag goes too

    # the bridge must run before any of the page's own scripts
    def _bridge(self):
        if not self.bridge_placed:
            self.out.append('<script src="rapp-bridge.js"></script><script src="rapp-handlers.js"></script>')
            self.bridge_placed = True

    def _tag(self, tag, attrs, closed=False):
        parts = [tag]
        for k, v in attrs:
            parts.append(k if v is None else f'{k}="{html.escape(v, quote=True)}"')
        return "<" + " ".join(parts) + (" />" if closed else ">")

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        raw = self.get_starttag_text()
        if tag == "head" and not closed:
            self.out.append(raw)
            self._bridge()
            return
        if tag == "script":
            src = dict(attrs).get("src")
            if src is not None and REMOTE.match(src):
                local = self._vendor(src, "js")
                if not local:
                    self.report["dropped"].append(src)
                    self._bridge()
                    self.out.append(f"<!-- {html.escape(src)}: not available in a code app -->")
                    self.skip_script = True
                    return
                attrs = [(k, local if k == "src" else v) for k, v in attrs if k not in ("integrity", "crossorigin")]
            if src is None:
                self._bridge()
                self.in_script, self.script_attrs, self.script_text = True, attrs, []
                return
            self._bridge()
            self.out.append(self._tag(tag, attrs, closed))
            return
        if tag == "link" and "stylesheet" in (dict(attrs).get("rel") or "").lower():
            href = dict(attrs).get("href") or ""
            if REMOTE.match(href):
                local = self._vendor(href, "css")
                if local:
                    attrs = [(k, local if k == "href" else v) for k, v in attrs if k not in ("integrity", "crossorigin")]
                else:
                    self.report["dropped"].append(href)
                    self.out.append(f"<!-- {html.escape(href)}: not available in a code app -->")
                    return
                self.out.append(self._tag(tag, attrs, closed))
                return
        if tag == "link" and (dict(attrs).get("rel") or "").lower() in ("preconnect", "dns-prefetch", "preload"):
            if REMOTE.match(dict(attrs).get("href") or ""):
                return
        events = [(k, v) for k, v in attrs if EVENT_ATTR.match(k)]
        js_link = tag == "a" and (dict(attrs).get("href") or "").strip().lower().startswith("javascript:")
        if not events and not js_link:
            self.out.append(raw)
            return
        ids = []
        for k, v in events:
            ids.append(self._handler(k[2:], v or ""))
        rest = [(k, v) for k, v in attrs if not EVENT_ATTR.match(k)]
        if js_link:
            code = dict(attrs)["href"].strip()[len("javascript:"):]
            ids.append(self._handler("click", "event.preventDefault();" + code))
            rest = [(k, "#" if k == "href" else v) for k, v in rest]
        rest.append(("data-rapp-h", " ".join(ids)))
        self.out.append(self._tag(tag, rest, closed))

    def _handler(self, event, code):
        hid = f"h{len(self.handlers) + 1}"
        self.handlers.append((hid, event, code))
        self.report["handlers"] += 1
        return hid

    def _vendor(self, url, kind):
        full = ("https:" + url) if url.startswith("//") else url
        if not self.fetch_vendor:
            return None
        try:
            data = self.fetch_vendor(full)
        except Exception as e:  # noqa: BLE001 - reported per URL
            self.report["dropped"].append(f"{full} ({type(e).__name__})")
            return None
        if data is None or len(data) > VENDOR_LIMIT:
            return None
        digest = hashlib.sha256(data).hexdigest()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", full.split("?")[0].rstrip("/").split("/")[-1])[:60] or f"asset.{kind}"
        if not name.endswith("." + kind):
            name += "." + kind
        rel = f"vendor/{digest[:12]}-{name}"
        self.files[rel] = data
        self.report["vendored"].append({"url": full, "file": rel, "sha256": digest, "bytes": len(data)})
        return rel

    def handle_endtag(self, tag):
        if tag == "script" and self.skip_script:
            self.skip_script = False
            return
        if tag == "script" and self.in_script:
            code = "".join(self.script_text)
            self.in_script = False
            attrs = [(k, v) for k, v in self.script_attrs if k not in ("nonce",)]
            kind = (dict(attrs).get("type") or "").lower()
            if kind and kind not in ("text/javascript", "module", "application/javascript"):
                # data blocks (JSON, templates) aren't scripts: they stay where they are
                self.out.append(self._tag("script", self.script_attrs) + code + "</script>")
                return
            if code.strip():
                self.report["inline_scripts"] += 1
                rel = f"inline-{self.report['inline_scripts']}.js"
                self.files[rel] = code.encode("utf-8")
                self.out.append(self._tag("script", attrs + [("src", rel)]) + "</script>")
            return
        if tag == "body":
            self.out.append('<script src="rapp-wire.js"></script>')
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        (self.script_text if self.in_script else self.out).append(data)

    def handle_entityref(self, name):
        (self.script_text if self.in_script else self.out).append(f"&{name};")

    def handle_charref(self, name):
        (self.script_text if self.in_script else self.out).append(f"&#{name};")

    def handle_comment(self, data):
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.out.append(f"<!{decl}>")

    def unknown_decl(self, data):
        self.out.append(f"<![{data}]>")

    def handle_pi(self, data):
        self.out.append(f"<?{data}>")


def _default_fetch(url):
    if not url.startswith("https://"):
        raise ValueError("only https assets are vendored")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "brainfreeze-studio"}), timeout=30) as r:
        data = r.read(VENDOR_LIMIT + 1)
    return None if len(data) > VENDOR_LIMIT else data


# How a rapplication UI reaches its brainstem: /chat and the binder over HTTP, or the cartridge messages.
AGENT_CALLS = re.compile(r"""["'`]/(?:chat|api/binder)|rapp:(?:invoke|chat)|rapp\s*:\s*["']invoke["']|"""
                         r"""(?:localhost|127\.0\.0\.1):\d+/(?:chat|api)""")


def ui_calls_agent(ui_html):
    """Whether the UI ever asks its agent anything (a UI that computes everything itself needs no chat flow)."""
    text = ui_html.decode("utf-8", errors="replace") if isinstance(ui_html, (bytes, bytearray)) else (ui_html or "")
    return bool(AGENT_CALLS.search(text))


def transform_ui(ui_html, fetch_vendor=_default_fetch):
    """ui.html (bytes or text) → ({path under rapp/: bytes}, report). Nothing in the page runs."""
    text = ui_html.decode("utf-8", errors="replace") if isinstance(ui_html, (bytes, bytearray)) else ui_html
    p = _Rewriter(fetch_vendor)
    p.feed(text)
    p.close()
    if not p.bridge_placed:
        p.out.insert(0, '<script src="rapp-bridge.js"></script><script src="rapp-handlers.js"></script>')
    page = "".join(p.out)
    if "rapp-wire.js" not in page:
        page += '<script src="rapp-wire.js"></script>'
    files = dict(p.files)
    files["ui.html"] = page.encode("utf-8")
    lines = ["/* The UI's inline event handlers, moved out of its markup for the code app policy. */",
             "window.__rappHandlers = window.__rappHandlers || {};"]
    for hid, event, code in p.handlers:
        lines.append(f"window.__rappHandlers[{json.dumps(hid)}] = {{ event: {json.dumps(event)}, "
                     f"fn: function (event) {{\n{code}\n}} }};")
    files["rapp-handlers.js"] = ("\n".join(lines) + "\n").encode("utf-8")
    files["rapp-wire.js"] = b"window.__rappWire && window.__rappWire();\n"
    files["rapp-bridge.js"] = (HOST_DIR / "rapp-bridge.js").read_bytes()
    scripts = "\n".join(v.decode("utf-8", errors="replace") for k, v in files.items()
                        if k.endswith(".js") and not k.startswith("vendor/") and k.startswith("inline-"))
    for pattern, why in RISKS:
        if pattern.search(scripts):
            p.report["risks"].append(why)
    fetches = re.search(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b|\bimport\s*\(", scripts)
    hosts = sorted({h.lower() for h in REMOTE_HOST.findall(scripts)} - NOT_FETCHED
                   - {"localhost", "127.0.0.1"}) if fetches else []
    p.report["remote_hosts"] = hosts
    if hosts:
        p.report["risks"].append(f"names remote hosts in its scripts ({', '.join(hosts[:8])}"
                                 f"{', …' if len(hosts) > 8 else ''}): fetches to them are blocked, since code apps "
                                 "allow no network access unless the environment's policy adds them")
    return files, p.report


# ── the app ───────────────────────────────────────────────────────────────────────────────────────────────────────

HOST_BUILD = Path("~/.cache/brainfreeze-studio/codeapp-host-build").expanduser()


def host_sources_digest():
    """What a built host.js was built from: codeapp_host/host.js and package.json."""
    return hashlib.sha256((HOST_DIR / "host.js").read_bytes() + b"\0" + (HOST_DIR / "package.json").read_bytes()).hexdigest()


def host_bundle(build=True):
    """The built host (host.js): BFS_CODEAPP_HOST as it is, else this package's codeapp_host/dist or the build cache
    when built from the current sources, else a fresh build (it needs node and npm)."""
    import os
    if os.environ.get("BFS_CODEAPP_HOST"):
        return Path(os.environ["BFS_CODEAPP_HOST"])
    digest = host_sources_digest()
    for candidate in (HOST_DIR / "dist" / "host.js", HOST_BUILD / "dist" / "host.js"):
        stamp = candidate.with_name(candidate.name + ".sources")
        if candidate.is_file() and stamp.is_file() and stamp.read_text().strip() == digest:
            return candidate
    if build:
        return build_host()
    raise FileNotFoundError("the code app host isn't built from the current sources: run "
                            "`python3 -m brainfreeze_studio codeapp-host` (it needs Node and npm; the SDK comes from "
                            "npm under Microsoft's license)")


def build_host(build_dir=None, outfile=None, sdk_alias=None, npm="npm", log=print):
    """Build host.js with esbuild: npm installs @microsoft/power-apps (its license forbids shipping it here, so each
    user's build fetches it) and bundles it with codeapp_host/host.js. `sdk_alias` points both SDK entry points at
    one stand-in module instead, for tests. Returns the built file."""
    import shutil
    import subprocess
    build = Path(build_dir or HOST_BUILD).expanduser()
    build.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "host.js"):
        shutil.copyfile(HOST_DIR / name, build / name)
    esbuild = build / "node_modules" / ".bin" / "esbuild"
    stamp = build / "node_modules" / ".bfs-package.json"
    if not esbuild.exists() or not stamp.exists() or stamp.read_bytes() != (HOST_DIR / "package.json").read_bytes():
        log(f"   npm install in {build}")
        subprocess.run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=build, check=True)
        stamp.write_bytes((HOST_DIR / "package.json").read_bytes())
    out = Path(outfile).expanduser() if outfile else build / "dist" / "host.js"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(esbuild), "host.js", "--bundle", "--format=iife", "--target=es2020", "--minify",
           "--legal-comments=eof", f"--outfile={out}"]
    if sdk_alias:
        cmd += [f"--alias:@microsoft/power-apps/app={sdk_alias}", f"--alias:@microsoft/power-apps/data={sdk_alias}"]
    subprocess.run(cmd, cwd=build, check=True, capture_output=True)
    if not sdk_alias:
        out.with_name(out.name + ".sources").write_text(host_sources_digest() + "\n")
    return out


def package(rapp, out_dir, *, schema_name, environment_id=None, display_name=None, tools=None, description=None,
            fetch_vendor=_default_fetch, host_js=None, chat=None, example=None):
    """Write out_dir/codeapp: dist/ (the app), power.config.json and report.json. `tools` lists the agent's tools:
    [{"name", "aliases", "flow": {"workflowId", "displayName"} | None}]; `chat` is the chat broker flow
    ({"workflowId", "displayName"}) the app reaches the agent through, for tools without a flow and free-form chat.
    `example` ({CSS selector: value}) fills the UI's empty fields when it opens, so it can be tried at once."""
    if not rapp.ui:
        raise ValueError(f"{rapp.id} has no UI to make a code app from")
    out = Path(out_dir).expanduser() / "codeapp"
    dist = out / "dist"
    if dist.exists():
        for f in sorted(dist.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
    (dist / "rapp").mkdir(parents=True, exist_ok=True)
    # Power Apps refuses these characters in an app's name
    display_name = re.sub(r'[.\\/:*?"<>|]+', "-", display_name or rapp.name).strip(" -") or rapp.id
    files, report = transform_ui(rapp.ui, fetch_vendor)
    for rel, data in files.items():
        target = dist / "rapp" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (dist / "index.html").write_text((HOST_DIR / "index.html").read_text().replace("__TITLE__", html.escape(display_name)))
    (dist / "host.js").write_bytes(Path(host_js or host_bundle()).read_bytes())

    sources, refs = {}, {}

    def flow_ref(flow):
        ds = data_source_name(flow["displayName"])
        sources[ds] = flow_info()
        refs[str(uuid.uuid5(uuid.NAMESPACE_URL, f"{schema_name}/flow/{flow['workflowId']}"))] = {
            "id": LOGIC_FLOWS_API, "displayName": "Logic flows", "dataSources": [ds],
            "workflowDetails": {"workflowEntityId": flow["workflowId"], "workflowDisplayName": flow["displayName"],
                                "workflowName": flow["workflowId"]}}
        return {"dataSource": ds, "workflowId": flow["workflowId"]}

    tool_cfg = {}
    for t in tools or []:
        entry = {"aliases": [a for a in t.get("aliases", []) if a and a != t["name"]]}
        if t.get("flow"):
            entry["flow"] = flow_ref(t["flow"])
        tool_cfg[t["name"]] = entry
    agent = {"schemaName": schema_name}
    if chat:
        agent["flow"] = flow_ref(chat)
    rapp_entry = {"id": rapp.id, "name": rapp.name, "version": rapp.version, "publisher": rapp.publisher,
                  "summary": rapp.summary, "tagline": rapp.tagline, "category": rapp.category,
                  "catalog_source": "kody-w/RAPP_Store" if rapp.source.get("kind") == "store" else rapp.source.get("kind")}
    config = {"rapp": rapp_entry, "agent": agent, "tools": tool_cfg, "dataSourcesInfo": sources}
    if example:
        config["example"] = example
    (dist / "rapp-config.js").write_text("window.RAPP_CONFIG = " + json.dumps(config, indent=1) + ";\n")
    power = {"version": "1.0", "appId": None, "appDisplayName": display_name, "region": "prod", "appType": "CodeApp",
             "environmentId": environment_id, "description": description or rapp.summary[:300], "buildPath": "./dist",
             "buildEntryPoint": "index.html", "logoPath": "Default", "connectionReferences": refs,
             "databaseReferences": {}}
    (out / "power.config.json").write_text(json.dumps(power, indent=2) + "\n")
    report = {"app": display_name, "files": sorted(str(f.relative_to(dist)) for f in dist.rglob("*") if f.is_file()),
              "tools": {k: ("flow" if v.get("flow") else "agent" if chat else "unavailable") for k, v in tool_cfg.items()},
              "chat": bool(chat), **report}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return {"dir": out, "dist": dist, "power_config": power, "report": report}
