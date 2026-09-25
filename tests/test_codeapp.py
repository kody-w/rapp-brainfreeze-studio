"""Code app tests: the UI rewrite for the code app policy, the Power Apps flow twin, and publishing through the
Power Apps resource provider. Offline: an in-memory resource provider and blob store answer the publisher."""
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brainfreeze_studio import codeapp, codeapp_publish as cp, rapplication  # noqa: E402

ENV_ID = "00000000-aaaa-4000-8000-000000000001"
OID = "00000000-bbbb-4000-8000-000000000002"


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


TOKEN = jwt({"aud": "https://service.powerapps.com/", "oid": OID, "scp": "user_impersonation"})

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>t</title>
<link rel="preconnect" href="https://fonts.gstatic.com">
<link rel="stylesheet" href="https://cdn.example.com/lib.css" integrity="sha384-x" crossorigin="anonymous">
<script src="https://cdn.example.com/lib.js" integrity="sha384-y"></script>
<script src="https://cdn.example.com/missing.js"></script>
<script type="application/json" id="data">{"a": 1}</script>
<script>window.boot = () => document.body.dataset.booted = "yes";</script>
</head><body onload="boot()">
<button id="b" onclick="go('x'); return false">Go</button>
<a id="l" href="javascript:go('link')">link</a>
<p>&amp; &#169; text</p>
<script>function go(v) { document.body.dataset.went = v; }
fetch("https://api.example.org/v1/items"); const svg = "http://www.w3.org/2000/svg";</script>
</body></html>"""


class Attrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers, self.inline, self.scripts, self.in_script = [], [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.handlers += [k for k in a if re.match(r"^on[a-z]+$", k)]
        if tag == "a" and (a.get("href") or "").lower().startswith("javascript:"):
            self.handlers.append("javascript:")
        if tag == "script":
            self.scripts.append(a)
            self.in_script = a

    def handle_data(self, data):
        if self.in_script is not None and data.strip():
            self.inline.append((self.in_script.get("type"), data))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = None


def vendor(url):
    if url.endswith("missing.js"):
        raise OSError("404")
    return b"/* lib */" if url.endswith(".js") else b".lib{}"


class TransformTests(unittest.TestCase):
    def setUp(self):
        self.files, self.report = codeapp.transform_ui(PAGE, fetch_vendor=vendor)
        self.page = self.files["ui.html"].decode()

    def test_nothing_inline_is_left(self):
        a = Attrs()
        a.feed(self.page)
        self.assertEqual(a.handlers, [])
        self.assertEqual([t for t, _ in a.inline], ["application/json"])       # a data block isn't a script
        self.assertTrue(all(s.get("src") or s.get("type") == "application/json" for s in a.scripts))
        self.assertEqual(self.report["inline_scripts"], 2)
        self.assertEqual(self.report["handlers"], 3)

    def test_the_bridge_runs_first_and_the_wiring_last(self):
        a = Attrs()
        a.feed(self.page)
        srcs = [s.get("src") for s in a.scripts if s.get("src")]
        self.assertEqual(srcs[:2], ["rapp-bridge.js", "rapp-handlers.js"])
        self.assertEqual(srcs[-1], "rapp-wire.js")
        self.assertEqual(self.files["rapp-bridge.js"], (codeapp.HOST_DIR / "rapp-bridge.js").read_bytes())

    def test_handlers_keep_their_code_and_event(self):
        js = self.files["rapp-handlers.js"].decode()
        self.assertIn('event: "load"', js)
        self.assertIn("boot()", js)
        self.assertIn("go('x'); return false", js)
        self.assertIn("event.preventDefault();go('link')", js)
        self.assertRegex(self.page, r'<button id="b" data-rapp-h="h\d+">')
        self.assertIn('href="#"', self.page)

    def test_remote_assets_are_vendored_with_their_digest_or_reported(self):
        vendored = {v["url"]: v for v in self.report["vendored"]}
        self.assertEqual(sorted(vendored), ["https://cdn.example.com/lib.css", "https://cdn.example.com/lib.js"])
        js = vendored["https://cdn.example.com/lib.js"]
        self.assertEqual(self.files[js["file"]], b"/* lib */")
        self.assertIn(f'src="{js["file"]}"', self.page)
        self.assertNotIn("integrity", self.page)
        self.assertNotIn("fonts.gstatic.com", self.page)
        self.assertTrue(any("missing.js" in d for d in self.report["dropped"]))
        self.assertIn("<!-- https://cdn.example.com/missing.js: not available in a code app -->", self.page)

    def test_text_survives_byte_for_byte(self):
        self.assertIn("<p>&amp; &#169; text</p>", self.page)
        self.assertIn('{"a": 1}', self.page)

    def test_what_cant_work_is_reported(self):
        self.assertEqual(self.report["remote_hosts"], ["api.example.org"])
        self.assertTrue(any("api.example.org" in r for r in self.report["risks"]))
        _, rep = codeapp.transform_ui("<script>eval('1'); el.innerHTML = '<b onclick=\"x()\">';</script>", fetch_vendor=None)
        self.assertTrue(any("eval" in r for r in rep["risks"]))
        self.assertTrue(any("inline event handlers at run time" in r for r in rep["risks"]))

    def test_a_page_without_head_or_body_still_gets_the_bridge(self):
        files, _ = codeapp.transform_ui("<button onclick='a()'>x</button>", fetch_vendor=None)
        page = files["ui.html"].decode()
        self.assertTrue(page.startswith('<script src="rapp-bridge.js">'))
        self.assertTrue(page.endswith('<script src="rapp-wire.js"></script>'))


class TwinTests(unittest.TestCase):
    def test_trigger_and_response_change_and_nothing_else(self):
        flow = {"properties": {"definition": {
            "triggers": {"manual": {"type": "Request", "kind": "Skills", "inputs": {"schema": {"type": "object",
                         "properties": {"q": {"type": "string", "title": "q", "x-ms-content-hint": "NUMBER"}}}}}},
            "actions": {"A": {"type": "Compose", "inputs": "@triggerBody()?['q']"},
                        "R": {"type": "Response", "kind": "Skills", "inputs": {"statusCode": 200, "body": {"result": "@outputs('A')"}}}}}}}
        twin = codeapp.powerapps_twin(flow)
        d = twin["properties"]["definition"]
        self.assertEqual(d["triggers"]["manual"]["kind"], "PowerAppV2")
        self.assertEqual(d["triggers"]["manual"]["inputs"]["schema"]["properties"]["q"],
                         {"type": "string", "title": "q", "x-ms-content-hint": "NUMBER", "x-ms-dynamically-added": True})
        self.assertEqual(d["actions"]["A"], flow["properties"]["definition"]["actions"]["A"])
        self.assertEqual(d["actions"]["R"]["kind"], "PowerApp")
        self.assertEqual(flow["properties"]["definition"]["actions"]["R"]["kind"], "Skills")     # the original is untouched

    def test_data_source_names_follow_the_power_apps_tooling(self):
        self.assertEqual(codeapp.data_source_name("RAPP Prototyping Copilot Chat Broker 20260918 A2"),
                         "rappprototypingcopilotchatbroker20260918a2")
        self.assertEqual(codeapp.data_source_name("Invoice Router InvoiceRouterFlow (Power Apps)"),
                         "invoicerouterinvoicerouterflowpowerapps")


class _Response(io.BytesIO):
    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePowerApps:
    """The resource provider's app calls and a blob container, in memory; a log of every request."""

    def __init__(self, apps=None, lease_conflict=False, fail_put=False):
        self.apps = {a["name"]: a for a in (apps or [])}
        self.blobs, self.log, self.leases = {}, [], {}
        self.lease_conflict, self.fail_put = lease_conflict, fail_put

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        method, body = req.get_method(), req.data
        self.log.append((method, url.netloc, url.path, urllib.parse.parse_qs(url.query)))
        if url.netloc == "blob.example.net":
            self.blobs[urllib.parse.unquote(url.path.split("/", 2)[2])] = (body, req.get_header("Content-type"),
                                                                           req.get_header("X-ms-blob-type"))
            return _Response(201)
        assert req.get_header("Authorization") == "Bearer " + TOKEN
        path = url.path.split("/providers/Microsoft.PowerApps/", 1)[1]
        data = json.loads(body) if body else None
        ok = lambda obj: _Response(200, json.dumps(obj).encode())
        if method == "GET" and path == "apps":
            return ok({"value": list(self.apps.values())})
        if path == f"objectIds/{OID}/generateResourceStorage":
            assert data == {"environment": {"name": ENV_ID, "id": f"/providers/Microsoft.PowerApps/environments/{ENV_ID}"}}
            return ok({"sharedAccessSignature": "https://blob.example.net/tmpcontainer?sv=1&sig=abc"})
        m = re.fullmatch(r"apps/([^/]+)(?:/(\w+))?", path)
        name, verb = (m.group(1), m.group(2)) if m else (None, None)
        if method == "POST" and path == "apps":
            app = {"name": "new-app-1", "appType": data["appType"], "appSubtype": data["appSubtype"],
                   "properties": {**data["properties"], "owner": {"id": OID},
                                  "appPlayUri": f"https://apps.powerapps.com/play/e/{ENV_ID}/app/new-app-1?tenantId=t"}}
            self.apps[app["name"]] = app
            return _Response(201, json.dumps(app).encode())
        if name not in self.apps:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b'{"error":{"code":"AppNotFound"}}'))
        if method == "GET" and verb is None:
            return ok(self.apps[name])
        if verb == "acquireLease":
            if self.lease_conflict:
                raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, io.BytesIO(
                    b'{"error":{"code":"AppLeaseSameUserConflict","message":"locked in another session"}}'))
            self.leases[name] = "lease-1"
            return ok({"leaseId": "lease-1"})
        if verb == "releaseLease":
            assert data == {"leaseId": self.leases.pop(name)}
            return ok({})
        if method == "PUT" and verb is None:
            if self.fail_put:
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error":{"message":"bad record"}}'))
            assert name in self.leases, "PUT without a lease"
            self.apps[name]["properties"].update(data["properties"])
            return ok(self.apps[name])
        if verb == "publish":
            self.apps[name]["published"] = True
            return ok({})
        if method == "DELETE":
            self.apps.pop(name)
            return ok({})
        raise AssertionError(f"unexpected {method} {path}")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bfs-publish-test-"))
        host = self.tmp / "host.js"
        host.write_bytes(b"/* host */")
        r = rapplication.load(ROOT / "examples" / "rapplications" / "invoice_router")
        self.app = codeapp.package(r, self.tmp, schema_name="rapp_InvoiceRouter", tools=[
            {"name": "InvoiceRouter", "aliases": [], "flow": {"workflowId": "7be9e641-e9fc-5ba5-9133-28991a9ca0a2",
                                                          "displayName": "Invoice Router InvoiceRouterFlow (Power Apps)"}}],
            fetch_vendor=None, host_js=host)
        (self.app["dist"] / "logo.png").write_bytes(bytes(range(256)))        # binary files go up byte for byte

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, rp):
        return cp.publish(self.app["dir"], ENV_ID, lambda: TOKEN, opener=rp, log=lambda *a: None)

    def test_a_first_publish_uploads_creates_and_publishes(self):
        rp = FakePowerApps()
        r = self.publish(rp)
        self.assertEqual((r["appId"], r["operation"]), ("new-app-1", "created"))
        self.assertTrue(r["playUrl"].startswith(f"https://apps.powerapps.com/play/e/{ENV_ID}/app/new-app-1"))
        dist = self.app["dist"]
        want = {f.relative_to(dist).as_posix(): f.read_bytes() for f in dist.rglob("*") if f.is_file()}
        self.assertEqual({k: v[0] for k, v in rp.blobs.items()}, want)
        self.assertEqual(rp.blobs["index.html"][1:], ("text/html", "BlockBlob"))
        self.assertEqual(rp.blobs["logo.png"][1], "image/png")
        app = rp.apps["new-app-1"]
        self.assertTrue(app["published"])
        self.assertEqual((app["appType"], app["appSubtype"]), ("CodeApp", "BYOCApp"))
        props = app["properties"]
        self.assertEqual(props["appUris"]["codeAppPackageUri"]["value"], "https://blob.example.net/tmpcontainer/index.html?sv=1&sig=abc")
        refs = props["connectionReferences"]
        self.assertEqual(sorted(refs), sorted(self.app["power_config"]["connectionReferences"]))
        flow = next(r for r in refs.values() if r["id"] == codeapp.LOGIC_FLOWS_API)
        hint = {"workflowEntityId": {"value": "7be9e641-e9fc-5ba5-9133-28991a9ca0a2"},
                "workflowDisplayName": {"value": "Invoice Router InvoiceRouterFlow (Power Apps)"},
                "workflowName": {"value": "7be9e641-e9fc-5ba5-9133-28991a9ca0a2"}}
        self.assertEqual((flow["parameterHints"], flow["parameterHintsV2"]), (hint, hint))
        self.assertNotIn("workflowDetails", flow)                 # the resource provider refuses that member
        self.assertEqual((flow["dataSources"], flow["dependencies"], flow["dependents"]),
                         (["invoicerouterinvoicerouterflowpowerapps"], [], []))
        self.assertEqual(props["environment"]["name"], ENV_ID)
        self.assertEqual(props["displayName"], "Invoice Router")
        self.assertEqual(json.loads((self.app["dir"] / "power.config.json").read_text())["appId"], "new-app-1")
        versions = {p: q.get("api-version", [None])[0] for m, host, p, q in rp.log if host == "api.powerapps.com"}
        self.assertTrue(all(v for v in versions.values()))

    def test_a_second_publish_updates_the_same_app_under_a_lease(self):
        rp = FakePowerApps()
        self.publish(rp)
        (self.app["dist"] / "index.html").write_text("<!doctype html><title>v2</title>")
        r = self.publish(rp)
        self.assertEqual((r["appId"], r["operation"]), ("new-app-1", "updated"))
        self.assertEqual(len(rp.apps), 1)
        verbs = [p.rsplit("/", 1)[-1] for m, h, p, q in rp.log if h == "api.powerapps.com"]
        self.assertEqual(verbs[verbs.index("acquireLease"):], ["acquireLease", "new-app-1", "releaseLease", "publish", "new-app-1"])
        self.assertEqual(rp.leases, {})
        self.assertEqual(rp.blobs["index.html"][0], b"<!doctype html><title>v2</title>")

    def test_an_app_of_the_same_name_is_found_without_an_app_id(self):
        existing = {"name": "old-app", "appType": "CodeApp", "properties": {
            "displayName": "Invoice Router", "owner": {"id": OID}, "environment": {"name": ENV_ID}}}
        rp = FakePowerApps(apps=[existing, {"name": "canvas", "appType": "ClassicCanvasApp",
                                            "properties": {"displayName": "Invoice Router"}}])
        r = self.publish(rp)
        self.assertEqual((r["appId"], r["operation"]), ("old-app", "updated"))

    def test_the_lease_is_released_even_when_the_save_fails(self):
        rp = FakePowerApps()
        self.publish(rp)
        rp.fail_put = True
        with self.assertRaisesRegex(cp.PublishError, "bad record"):
            self.publish(rp)
        self.assertEqual(rp.leases, {})

    def test_clear_errors(self):
        rp = FakePowerApps()
        self.publish(rp)
        rp.lease_conflict = True
        with self.assertRaisesRegex(cp.PublishError, "open for editing in another session"):
            self.publish(rp)
        wrong = jwt({"aud": "https://api.powerplatform.com", "oid": OID})
        with self.assertRaisesRegex(cp.PublishError, "token is for https://api.powerplatform.com"):
            cp.publish(self.app["dir"], ENV_ID, lambda: wrong, opener=FakePowerApps(), log=lambda *a: None)
        app_only = jwt({"aud": "https://service.powerapps.com/"})
        with self.assertRaisesRegex(cp.PublishError, "no oid claim"):
            cp.publish(self.app["dir"], ENV_ID, lambda: app_only, opener=FakePowerApps(), log=lambda *a: None)

    def test_database_references_are_shaped_as_the_app_record_wants(self):
        self.assertEqual(cp.database_references({"default.cds": {"dataSources": {"notes": {"entitySetName": "annotations"}}}}),
                         {"default.cds": {"databaseDetails": {"environmentName": "default.cds",
                                                              "overrideValues": {"environmentVariableName": ""}},
                                          "dataSources": {"notes": {"entitySetName": "annotations"}}}})


@unittest.skipUnless(shutil.which("node"), "needs node")
class BridgeInlineHandlerTests(unittest.TestCase):
    """Markup built at run time with inline handlers: calls with literal arguments run, anything else is refused."""

    def test_literal_calls_run_and_expressions_are_refused(self):
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
global.window = { addEventListener() {}, fetch: null };
global.location = { href: 'https://x/', origin: 'https://x' };
global.document = { documentElement: {} };
eval(src);
const calls = [];
window.edit = (id, n) => calls.push(['edit', id, n]);
window.ui = { tabs: { show: (t) => calls.push(['show', t]) } };
const ev = { stopPropagation() { calls.push(['stop']); } };
window.__rappInline("event.stopPropagation(); edit('p\\'1', 2)").call({}, ev);
window.__rappInline('ui.tabs.show("x")').call({}, ev);
console.log(JSON.stringify({ calls, refused: ['alert(1+2)', 'x = 1', 'edit(a)', 'fetch(`u`)'].map(c => window.__rappInline(c) === null) }));
"""
        out = subprocess.run(["node", "-e", script, str(codeapp.HOST_DIR / "rapp-bridge.js")], capture_output=True,
                             text=True, check=True).stdout
        got = json.loads(out)
        self.assertEqual(got["calls"], [["stop"], ["edit", "p'1", 2], ["show", "x"]])
        self.assertEqual(got["refused"], [True, True, True, True])


if __name__ == "__main__":
    unittest.main()
