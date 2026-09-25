"""Rapplication tests: loading RAPP Store bundles, the rapp/1 rapplication egg, and preparing the agent, the Power
Apps flows and the code app. Offline; stdlib only."""
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import brainfreeze_studio as bs  # noqa: E402
from brainfreeze_studio import codeapp, flows, rapp1, rapplication  # noqa: E402

EXAMPLE = ROOT / "examples" / "rapplications" / "invoice_router"
TRANSLATIONS = ROOT / "translations"
UTC = "2026-09-25T12:00:00.000Z"
RID = "rappid:@brainfreeze/invoice-router:" + "d" * 64
FAKE_HOST = b"/* stand-in host bundle for tests */\n"


def run_python_agent(agent_file, cases):
    """The real agent's output for each {"args", "env"} case, through the same runner the parity proofs use."""
    p = subprocess.run([sys.executable, "-c", flows._RUN_AGENT, str(agent_file)], text=True, capture_output=True,
                       input="".join(json.dumps(c) + "\n" for c in cases), check=True)
    return [json.loads(line)["out"] for line in p.stdout.splitlines()]


def mini_store(tmp, tamper=False):
    """A local store root laid out like RAPP_Store: index.json + apps/@publisher/id/{manifest.json,singleton,ui}."""
    root = Path(tmp) / "store"
    folder = root / "apps" / "@brainfreeze" / "invoice_router"
    shutil.copytree(EXAMPLE, folder)
    agent = (folder / "singleton" / "invoice_router_agent.py").read_bytes()
    ui = (folder / "ui" / "index.html").read_bytes()
    raw = "https://raw.githubusercontent.com/kody-w/RAPP_Store/main/apps/@brainfreeze/invoice_router"
    entries = [
        {"id": "invoice_router", "name": "Invoice Router", "version": "1.0.0", "publisher": "@brainfreeze",
         "summary": "Routes one invoice.", "singleton_url": f"{raw}/singleton/invoice_router_agent.py",
         "singleton_filename": "invoice_router_agent.py", "ui_url": f"{raw}/ui/index.html",
         "singleton_sha256": hashlib.sha256(agent + (b"x" if tamper else b"")).hexdigest(),
         "ui_sha256": hashlib.sha256(ui).hexdigest()},
        {"id": "gated", "name": "Gated", "version": "1.0.0", "publisher": "@someone", "access": "private",
         "singleton_url": f"{raw}/singleton/invoice_router_agent.py"},
        {"id": "whole_app", "name": "Whole app", "version": "2.0.0", "publisher": "@someone",
         "application_schema": "rapp-application/2.0"},
    ]
    (root / "index.json").write_text(json.dumps({"rapplications": entries}))
    return root


def app_config(out):
    cfg = (Path(out) / "codeapp" / "dist" / "rapp-config.js").read_text()
    return json.loads(cfg[len("window.RAPP_CONFIG = "):].rstrip().rstrip(";"))


class LoadTests(unittest.TestCase):
    def test_a_bundle_folder_loads(self):
        r = rapplication.load(EXAMPLE)
        self.assertEqual((r.id, r.name, r.version, r.publisher), ("invoice_router", "Invoice Router", "1.0.0", "@brainfreeze"))
        self.assertEqual(r.agent_filename, "invoice_router_agent.py")
        self.assertEqual(r.agent, (ROOT / "examples" / "invoice_router_agent.py").read_bytes())
        self.assertIn(b"Use the InvoiceRouter tool", r.ui)
        self.assertEqual(r.source["kind"], "bundle")

    def test_a_zip_with_a_wrapper_folder_loads_the_same(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in EXAMPLE.rglob("*"):
                if f.is_file():
                    z.write(f, "invoice_router/" + f.relative_to(EXAMPLE).as_posix())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            path.write_bytes(buf.getvalue())
            r = rapplication.load(path)
        self.assertEqual(r.agent, rapplication.load(EXAMPLE).agent)

    def test_the_store_is_checked_against_its_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mini_store(tmp)
            r = rapplication.load("@brainfreeze/invoice_router", store=str(store))
            self.assertEqual(r.source["checks"], {"agent": "sha256 matches the catalog", "ui": "sha256 matches the catalog"})
            self.assertEqual(r.name, "Invoice Router")
            self.assertEqual(rapplication.load("invoice_router", store=str(store)).agent, r.agent)
            with self.assertRaisesRegex(rapplication.RapplicationError, "gated"):
                rapplication.load("@someone/gated", store=str(store))
            with self.assertRaisesRegex(rapplication.RapplicationError, "no single-file agent"):
                rapplication.load("whole_app", store=str(store))
            with self.assertRaisesRegex(rapplication.RapplicationError, "no catalog entries"):
                rapplication.load("@nobody/invoice_router", store=str(store))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(rapplication.RapplicationError, "does not match the catalog"):
                rapplication.load("invoice_router", store=str(mini_store(tmp, tamper=True)))

    def test_refs_must_look_like_refs(self):
        with self.assertRaisesRegex(rapplication.RapplicationError, "not a store reference"):
            rapplication.from_store("../../etc/passwd")


class EggTests(unittest.TestCase):
    def test_pack_gives_a_verified_rapplication_egg_that_loads_back(self):
        r = rapplication.load(EXAMPLE)
        blob = rapplication.pack(r, RID, UTC)
        ok, step, why = rapp1.verify_egg(blob)
        self.assertTrue(ok, f"{step}: {why}")
        manifest, files = rapp1.read_egg(blob)
        self.assertEqual(manifest["variant"], "rapplication")
        self.assertEqual(sorted(files), ["agent.py", "rappid.json", "ui.html"])
        self.assertEqual(files["agent.py"], r.agent)
        self.assertEqual(manifest["payload"]["rapplication"]["id"], "invoice_router")
        back = rapplication.from_egg(blob)
        self.assertEqual((back.id, back.name, back.agent, back.ui), (r.id, r.name, r.agent, r.ui))
        self.assertEqual(rapplication.pack(back, created_utc=UTC), blob)     # an egg keeps its rappid

    def test_a_fresh_rappid_is_minted_keyless_never_from_the_name(self):
        r = rapplication.load(EXAMPLE)
        a = rapp1.read_egg(rapplication.pack(r, created_utc=UTC))[0]["rappid"]
        b = rapp1.read_egg(rapplication.pack(r, created_utc=UTC))[0]["rappid"]
        self.assertTrue(a.startswith("rappid:@brainfreeze/invoice-router:"))
        self.assertNotEqual(a, b)

    def test_the_build_reads_it_as_a_one_agent_brainstem(self):
        r = rapplication.load(EXAMPLE)
        manifest, files = rapp1.read_egg(rapplication.pack(r, RID, UTC))
        laid, meta = rapplication.organism_files(manifest, files)
        self.assertEqual(sorted(laid), ["agents/basic_agent.py", "agents/invoice_router_agent.py", "rappid.json",
                                        "soul.md", "ui.html"])
        vendor = json.loads((ROOT / "brainfreeze_studio" / "basic_agent.vendor.json").read_text())
        self.assertEqual(hashlib.sha256(laid["agents/basic_agent.py"]).hexdigest(), vendor["sha256"])
        self.assertIn(b"Invoice Router", laid["soul.md"])
        with tempfile.TemporaryDirectory() as tmp:
            egg_path = Path(tmp) / "r.egg"
            egg_path.write_bytes(rapplication.pack(r, RID, UTC))
            out = bs.build(str(egg_path), Path(tmp) / "out", "Invoice Router", "rapp", translations=str(TRANSLATIONS))
            prov = json.loads((Path(tmp) / "out" / "provenance.json").read_text())
        self.assertEqual([a["name"] for a in out["agents"]], ["InvoiceRouter"])
        self.assertEqual(out["agents"][0]["as"], "agent flow (translated, parity proven)")
        self.assertEqual(prov["rapplication"]["id"], "invoice_router")
        self.assertEqual(prov["egg"]["rappid"], RID)


class PrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bfs-rapp-test-"))
        cls.host = cls.tmp / "host.js"
        cls.host.write_bytes(FAKE_HOST)
        cls.out = cls.tmp / "out"
        cls.summary = rapplication.prepare(EXAMPLE, cls.out, translations=str(TRANSLATIONS), fetch_vendor=None,
                                           host_js=cls.host, created_utc=UTC)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_summary_says_what_went_where(self):
        s = self.summary
        self.assertEqual(s["agent"]["schemaName"], "rapp_InvoiceRouter")
        self.assertEqual(s["tools"], [{"name": "InvoiceRouter", "aliases": ["InvoiceRouterAgent", "invoice_router"],
                                       "flow": {"workflowId": s["powerapps_flows"][0]["id"],
                                                "displayName": "Invoice Router InvoiceRouterFlow (Power Apps)"}}])
        self.assertEqual(s["powerapps_flows"][0]["id"], bs.workflow_id_for("rapp_InvoiceRouter", "InvoiceRouterFlowPowerApps"))
        self.assertEqual(json.loads((self.out / "rapplication.json").read_text()), s)

    def test_the_twin_keeps_every_action_and_only_changes_its_trigger_and_response(self):
        agent_flow = next((self.out / "workspace" / "workflows").glob("InvoiceRouterFlow-*/workflow.json"))
        original = json.loads(agent_flow.read_text())
        twin = json.loads((self.out / "powerapps-flows" / "InvoiceRouterFlowPowerApps.json").read_text())["definition"]
        o, t = original["properties"]["definition"], twin["properties"]["definition"]
        self.assertEqual(o["triggers"]["manual"]["kind"], "Skills")
        self.assertEqual(t["triggers"]["manual"]["kind"], "PowerAppV2")
        for name, prop in t["triggers"]["manual"]["inputs"]["schema"]["properties"].items():
            self.assertEqual(prop["title"], name)
            self.assertTrue(prop["x-ms-dynamically-added"])
        respond = [n for n, a in t["actions"].items() if a["type"] == "Response"]
        self.assertEqual([t["actions"][n]["kind"] for n in respond], ["PowerApp"])
        for name, action in o["actions"].items():
            if action["type"] != "Response":
                self.assertEqual(t["actions"][name], action)
        self.assertEqual({k: v for k, v in t["actions"][respond[0]].items() if k != "kind"},
                         {k: v for k, v in o["actions"][respond[0]].items() if k != "kind"})
        self.assertEqual(t["parameters"], o["parameters"])

    def test_the_twin_gives_the_agents_exact_output(self):
        spec = json.loads((TRANSLATIONS / "invoice_router.json").read_text())
        twin = json.loads((self.out / "powerapps-flows" / "InvoiceRouterFlowPowerApps.json").read_text())["definition"]
        cases = [{"args": v, "env": {}} for v in spec["vectors"]]
        python = run_python_agent(EXAMPLE / "singleton" / "invoice_router_agent.py", cases)
        for case, py in zip(cases, python):
            # the app passes every input as text, as the Power Apps trigger does
            got = flows.run_flow(twin, {k: str(v) for k, v in case["args"].items()})["result"]
            self.assertEqual(got, py, case)

    def test_the_code_app_references_the_twin_and_needs_no_chat_flow(self):
        power = json.loads((self.out / "codeapp" / "power.config.json").read_text())
        refs = list(power["connectionReferences"].values())
        self.assertEqual([r["id"] for r in refs], [codeapp.LOGIC_FLOWS_API])
        twin = self.summary["powerapps_flows"][0]
        self.assertEqual(refs[0]["workflowDetails"], {"workflowEntityId": twin["id"], "workflowDisplayName": twin["name"],
                                                      "workflowName": twin["id"]})
        self.assertEqual(refs[0]["dataSources"], [codeapp.data_source_name(twin["name"])])
        config = app_config(self.out)
        self.assertEqual(config["agent"], {"schemaName": "rapp_InvoiceRouter"})    # every tool has its own flow
        self.assertIsNone(self.summary["chat"])
        self.assertEqual(config["tools"]["InvoiceRouter"]["flow"]["workflowId"], twin["id"])
        self.assertEqual(list(config["dataSourcesInfo"]), [refs[0]["dataSources"][0]])
        self.assertEqual((self.out / "codeapp" / "dist" / "host.js").read_bytes(), FAKE_HOST)
        self.assertEqual(power["buildEntryPoint"], "index.html")

    def test_a_tool_without_a_flow_reaches_the_agent_through_the_chat_flow(self):
        out = self.tmp / "no-translation"
        s = rapplication.prepare(EXAMPLE, out, fetch_vendor=None, host_js=self.host, created_utc=UTC)
        self.assertEqual(s["tools"][0]["flow"], None)
        self.assertEqual(s["chat"]["id"], bs.workflow_id_for("rapp_InvoiceRouter", "ChatBrokerPowerApps"))
        broker = json.loads((out / "powerapps-flows" / "ChatBrokerPowerApps.json").read_text())["definition"]
        self.assertTrue(codeapp.is_chat_broker(broker))
        d = broker["properties"]["definition"]
        self.assertEqual(d["triggers"]["manual"]["kind"], "PowerAppV2")
        ask = d["actions"]["Ask_the_agent"]
        self.assertEqual((ask["type"], ask["inputs"]["host"]["operationId"], ask["inputs"]["parameters"]["Copilot"]),
                         ("OpenApiConnectionWebhook", "ExecuteCopilotAsyncV2OnAgenticRuntime", "rapp_InvoiceRouter"))
        self.assertEqual(broker["properties"]["connectionReferences"]["shared_microsoftcopilotstudio"]["connection"],
                         {"connectionReferenceLogicalName": "rapp_InvoiceRouter.shared_microsoftcopilotstudio"})
        config = app_config(out)
        self.assertEqual(config["agent"]["flow"], {"dataSource": codeapp.data_source_name(s["chat"]["name"]),
                                                   "workflowId": s["chat"]["id"]})
        self.assertEqual(config["tools"]["InvoiceRouter"], {"aliases": ["InvoiceRouterAgent", "invoice_router"]})
        refs = json.loads((out / "codeapp" / "power.config.json").read_text())["connectionReferences"]
        self.assertEqual([r["id"] for r in refs.values()], [codeapp.LOGIC_FLOWS_API])   # no connector in the app
        self.assertEqual(s["codeapp"]["report"]["tools"], {"InvoiceRouter": "agent"})

    def test_running_it_again_keeps_the_rappid(self):
        again = rapplication.prepare(EXAMPLE, self.out, translations=str(TRANSLATIONS), fetch_vendor=None,
                                     host_js=self.host, created_utc=UTC)
        self.assertEqual(again["rappid"], self.summary["rappid"])
        self.assertEqual(again["egg_address"], self.summary["egg_address"])


class DeployTests(unittest.TestCase):
    """rapplication.deploy against an in-memory Dataverse and Power Apps resource provider."""

    def test_agent_flows_chat_flow_and_code_app_deploy_and_redeploy_unchanged(self):
        from test_codeapp import ENV_ID, TOKEN, FakePowerApps
        from test_deploy import ENV, FakeDataverse

        class Dataverse(FakeDataverse):
            def __call__(self, method, path, *a, **k):
                if path.startswith("RetrieveCurrentOrganization"):
                    return {"Detail": {"EnvironmentId": ENV_ID}}, {}
                return super().__call__(method, path, *a, **k)

        tmp = Path(tempfile.mkdtemp(prefix="bfs-rapp-deploy-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        host = tmp / "host.js"
        host.write_bytes(FAKE_HOST)
        out = tmp / "out"
        rapplication.prepare(EXAMPLE, out, fetch_vendor=None, host_js=host, created_utc=UTC)     # no translation
        dv, rp = Dataverse(), FakePowerApps()
        dv.t["connectionreferences"]["ref-cs"] = {
            "connectionreferenceid": "ref-cs", "connectionreferencelogicalname": "someone.shared_microsoftcopilotstudio",
            "connectorid": "/providers/Microsoft.PowerApps/apis/shared_microsoftcopilotstudio", "connectionid": "cs-conn-1",
            "createdon": "2026-01-01"}
        deploy = lambda: rapplication.deploy(out, ENV, lambda: "dv-token", lambda: TOKEN, log=lambda *a: None,
                                             dataverse=dv, opener=rp)
        r = deploy()
        chat_id = bs.workflow_id_for("rapp_InvoiceRouter", "ChatBrokerPowerApps")
        self.assertEqual([f["operation"] for f in r["powerapps_flows"]], ["created"])
        self.assertEqual(dv.t["workflows"][chat_id]["statecode"], 1)
        ref = next(x for x in dv.t["connectionreferences"].values()
                   if x["connectionreferencelogicalname"] == "rapp_InvoiceRouter.shared_microsoftcopilotstudio")
        self.assertEqual(ref["connectionid"], "cs-conn-1")                # bound to the user's own connection
        self.assertEqual(r["codeapp"]["operation"], "created")
        self.assertTrue(rp.apps[r["codeapp"]["appId"]]["published"])
        summary = json.loads((out / "rapplication.json").read_text())
        self.assertEqual(summary["deployed"]["codeapp"]["appId"], r["codeapp"]["appId"])
        again = deploy()
        self.assertEqual([f["operation"] for f in again["powerapps_flows"]], ["unchanged"])
        self.assertEqual((again["codeapp"]["appId"], again["codeapp"]["operation"]), (r["codeapp"]["appId"], "updated"))
        self.assertEqual(len(rp.apps), 1)


if __name__ == "__main__":
    unittest.main()
