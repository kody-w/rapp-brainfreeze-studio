"""Rapplication tests: loading RAPP Store bundles, the rapp/1 rapplication egg, and preparing the agent, the Power
Apps flows and the code app. Offline; stdlib only."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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


def app_service():
    from test_codeapp import OID, FakePowerApps, _Response

    class Service(FakePowerApps):
        def __call__(self, req, timeout=None):
            if "/apis/" in req.full_url and "/connections?" in req.full_url:
                if req.get_method() != "GET":
                    raise AssertionError("the fixture's own connection already exists")
                return _Response(200, json.dumps({"value": [{"name": "cs-conn-1", "properties": {
                    "createdBy": {"id": OID}, "statuses": [{"status": "Connected"}]}}]}).encode())
            return super().__call__(req, timeout)

    return Service()


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

    def test_a_ports_example_opens_the_app_filled_in(self):
        self.assertNotIn("example", app_config(self.out))                   # this port's UI has its own defaults
        translations = self.tmp / "with-example"
        shutil.copytree(TRANSLATIONS, translations)
        spec = json.loads((translations / "invoice_router.json").read_text())
        spec["ui_example"] = {"#vendor": "Northwind Traders", "#amount": "4200"}
        (translations / "invoice_router.json").write_text(json.dumps(spec))
        s = rapplication.prepare(EXAMPLE, self.tmp / "out-example", translations=str(translations), fetch_vendor=None,
                                 host_js=self.host, created_utc=UTC)
        self.assertEqual(s["agent"]["agents"][0]["ui_example"], spec["ui_example"])
        self.assertEqual(app_config(self.tmp / "out-example")["example"], spec["ui_example"])

    def test_running_it_again_keeps_the_rappid(self):
        again = rapplication.prepare(EXAMPLE, self.out, translations=str(TRANSLATIONS), fetch_vendor=None,
                                     host_js=self.host, created_utc=UTC)
        self.assertEqual(again["rappid"], self.summary["rappid"])
        self.assertEqual(again["egg_address"], self.summary["egg_address"])


class NoAppTests(unittest.TestCase):
    def test_a_literal_cold_machine_build_has_no_node_npm_host_override_or_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, binaries, guard, out = (root / n for n in ("home", "bin", "guard", "out"))
            for folder in (home, binaries, guard):
                folder.mkdir()
            called, ready = guard / "npm-called", guard / "ready"
            (guard / "sitecustomize.py").write_text(
                "import pathlib, shutil, subprocess\n"
                "assert shutil.which('node') is None and shutil.which('npm') is None\n"
                f"assert pathlib.Path.home() == pathlib.Path({str(home)!r})\n"
                f"pathlib.Path({str(ready)!r}).touch()\n"
                "_popen = subprocess.Popen\n"
                "def guarded(args, *a, **kw):\n"
                "    command = args[0] if isinstance(args, (list, tuple)) else args.split()[0]\n"
                "    if pathlib.Path(command).stem in ('node', 'npm', 'npx'):\n"
                f"        pathlib.Path({str(called)!r}).touch()\n"
                "        raise RuntimeError('Node and npm are forbidden in this cold-machine test')\n"
                "    return _popen(args, *a, **kw)\n"
                "subprocess.Popen = guarded\n")
            env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PATH=str(binaries), XDG_CACHE_HOME=str(root / "cache"),
                       TMPDIR=str(root), PYTHONPATH=os.pathsep.join((str(guard), str(ROOT))))
            env.pop("BFS_CODEAPP_HOST", None)
            result = subprocess.run([sys.executable, "-m", "brainfreeze_studio", "rapplication", str(EXAMPLE),
                                     "--out", str(out), "--no-app"], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(ready.is_file(), "the subprocess guard must actually load")
            self.assertFalse(called.exists())
            self.assertFalse((home / ".cache").exists())
            self.assertFalse((root / "cache").exists())
            self.assertFalse((out / "codeapp").exists())
            self.assertEqual(list((out / "powerapps-flows").glob("*.json")), [])
            self.assertIn("code app:     not built (--no-app)", result.stdout)

    def test_host_install_output_uses_the_logger_so_json_can_stay_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = []

            def run(cmd, **kw):
                self.assertTrue(kw["capture_output"])
                if cmd[0] == "npm":
                    (Path(tmp) / "node_modules").mkdir()
                    return subprocess.CompletedProcess(cmd, 0, stdout="installed the host\n", stderr="npm note\n")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("subprocess.run", side_effect=run):
                codeapp.build_host(tmp, log=logs.append)
            self.assertEqual(logs[1:], ["installed the host", "npm note"])

    def test_no_app_needs_neither_the_host_nor_power_apps_flows(self):
        for translations in (str(TRANSLATIONS), None):
            with self.subTest(translations=translations), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(codeapp, "host_bundle", side_effect=AssertionError("no Node")), \
                    mock.patch.object(codeapp, "build_host", side_effect=AssertionError("no npm")), \
                    mock.patch.object(codeapp, "package", side_effect=AssertionError("no code app")), \
                    mock.patch.object(rapplication, "twins", side_effect=AssertionError("no twins")), \
                    mock.patch.object(codeapp, "chat_broker", side_effect=AssertionError("no chat broker")):
                out = Path(tmp) / "out"
                s = rapplication.prepare(EXAMPLE, out, translations=translations, app=False)
                self.assertIsNone(s["codeapp"])
                self.assertEqual(s["codeapp_skipped"], "--no-app")
                self.assertEqual(s["powerapps_flows"], [])
                self.assertIsNone(s["chat"])
                self.assertFalse((out / "codeapp").exists())
                self.assertFalse((out / "powerapps-flows").exists())
                self.assertTrue((out / "workspace" / "settings.mcs.yml").is_file())
                self.assertEqual(json.loads((out / "rapplication.json").read_text()), s)
                if translations:
                    prov = json.loads((out / "provenance.json").read_text())
                    self.assertEqual(prov["parity"]["InvoiceRouter"], {"passed": 72, "cases": 72, "parity": True})

    def test_app_true_still_packages_the_app_and_its_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp) / "host.js"
            host.write_bytes(FAKE_HOST)
            s = rapplication.prepare(EXAMPLE, tmp, translations=str(TRANSLATIONS), host_js=host, app=True)
            self.assertIsNotNone(s["codeapp"])
            self.assertNotIn("codeapp_skipped", s)
            self.assertEqual(len(s["powerapps_flows"]), 1)
            self.assertEqual((Path(tmp) / "codeapp" / "dist" / "host.js").read_bytes(), FAKE_HOST)

    def test_no_app_clears_old_app_outputs_when_the_folder_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp) / "host.js"
            host.write_bytes(FAKE_HOST)
            before = rapplication.prepare(EXAMPLE, tmp, host_js=host)
            self.assertIsNotNone(before["chat"])
            after = rapplication.prepare(EXAMPLE, tmp, translations=str(TRANSLATIONS), app=False)
            self.assertEqual(after["rappid"], before["rappid"])
            self.assertFalse((Path(tmp) / "codeapp").exists())
            self.assertEqual(list((Path(tmp) / "powerapps-flows").glob("*.json")), [])

    def test_deploy_no_app_ignores_even_previously_built_twins_and_chat_flows(self):
        from brainfreeze_studio import deploy as dp
        from test_deploy import DATAVERSE_TOKEN, ENV, FakeDataverse

        for translations in (str(TRANSLATIONS), None):
            with self.subTest(translations=translations), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch("brainfreeze_studio.codeapp_publish.publish", side_effect=AssertionError("no app")):
                host = Path(tmp) / "host.js"
                host.write_bytes(FAKE_HOST)
                rapplication.prepare(EXAMPLE, tmp, translations=translations, host_js=host)
                dv = FakeDataverse()
                result = rapplication.deploy(tmp, ENV, lambda: DATAVERSE_TOKEN, dataverse=dv, app=False,
                                              publish_agent=False, log=lambda *_: None,
                                              get_powerapps_token=lambda: self.fail("no Power Apps token needed"))
                self.assertIsNone(result["codeapp"])
                self.assertEqual(result["powerapps_flows"], [])
                expected = {wf["id"] for wf in dp.read_workspace(Path(tmp) / "workspace")["workflows"]}
                self.assertEqual(set(dv.t["workflows"]), expected)
                self.assertEqual(set(dv.t["connectionreferences"]), {"ref-env"})


class PlanTests(unittest.TestCase):
    def setUp(self):
        from test_codeapp import ENV_ID
        from test_deploy import FakeDataverse

        class Dataverse(FakeDataverse):
            def __call__(self, method, path, *a, **kw):
                if path.startswith("RetrieveCurrentOrganization"):
                    return {"Detail": {"EnvironmentId": ENV_ID}}, {}
                return super().__call__(method, path, *a, **kw)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.host = self.root / "host.js"
        self.host.write_bytes(FAKE_HOST)
        self.out = self.root / "out"
        self.summary = rapplication.prepare(EXAMPLE, self.out, translations=str(TRANSLATIONS), host_js=self.host)
        self.dv, self.pa = Dataverse(), app_service()

    def run_plan(self, **kw):
        from copy import deepcopy
        from test_codeapp import TOKEN
        from test_deploy import DATAVERSE_TOKEN, ENV, ReadOnlyDataverse

        def readonly(req, **args):
            self.assertEqual(req.get_method(), "GET", f"a plan tried to write: {req.full_url}")
            return self.pa(req, **args)

        before = deepcopy((self.dv.t, self.dv.writes, self.pa.apps, self.pa.blobs, self.pa.leases))
        files = {p: p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        r = rapplication.plan(self.out, ENV, lambda: DATAVERSE_TOKEN, lambda: TOKEN,
                              dataverse=ReadOnlyDataverse(self.dv), opener=readonly, **kw)
        self.assertEqual((self.dv.t, self.dv.writes, self.pa.apps, self.pa.blobs, self.pa.leases), before)
        self.assertEqual({p: p.read_bytes() for p in self.out.rglob("*") if p.is_file()}, files)
        return r

    def run_deploy(self, **kw):
        from test_codeapp import TOKEN
        from test_deploy import DATAVERSE_TOKEN, ENV
        return rapplication.deploy(self.out, ENV, lambda: DATAVERSE_TOKEN, lambda: TOKEN, dataverse=self.dv, opener=self.pa,
                                    log=lambda *_: None, **kw)

    def test_create_plan_includes_the_twin_and_the_code_app_without_writing(self):
        r = self.run_plan()
        twin = self.summary["powerapps_flows"][0]["name"]
        self.assertEqual(r["agent"]["operation"], "create")
        self.assertEqual(r["powerappsFlows"], {"create": [twin], "update": []})
        self.assertEqual(r["flows"], {"create": ["Invoice Router InvoiceRouterFlow", twin], "update": []})
        self.assertEqual(r["codeapp"], {"displayName": "Invoice Router", "operation": "create", "appId": None})
        deployed = self.run_deploy()
        self.assertEqual(deployed["agent"]["bot"], "created")
        self.assertEqual(deployed["powerapps_flows"][0]["operation"], "created")
        self.assertEqual(deployed["codeapp"]["operation"], "created")

    def test_update_plan_reuses_the_app_id_and_includes_changed_twins(self):
        before = self.run_deploy()
        twin = self.summary["powerapps_flows"][0]
        self.dv.t["workflows"][twin["id"]]["statecode"] = 0
        r = self.run_plan()
        self.assertEqual(r["agent"]["operation"], "unchanged")
        self.assertEqual(r["flows"], {"create": [], "update": [twin["name"]]})
        self.assertEqual(r["powerappsFlows"], r["flows"])
        self.assertEqual(r["codeapp"]["operation"], "update")
        self.assertEqual(r["codeapp"]["appId"], before["codeapp"]["appId"])
        deployed = self.run_deploy()
        self.assertEqual(deployed["powerapps_flows"][0]["operation"], "updated")
        self.assertEqual(deployed["codeapp"]["operation"], "updated")

    def test_a_rebuild_finds_the_same_code_app_by_name_with_gets_only(self):
        before = self.run_deploy()
        rapplication.prepare(EXAMPLE, self.out, translations=str(TRANSLATIONS), host_js=self.host)
        r = self.run_plan()
        self.assertEqual(r["codeapp"]["operation"], "update")
        self.assertEqual(r["codeapp"]["appId"], before["codeapp"]["appId"])
        self.assertEqual(r["flows"], {"create": [], "update": []})

    def test_no_app_does_not_even_look_up_the_app_or_its_flows(self):
        r = self.run_plan(app=False)
        self.assertEqual(r["powerappsFlows"], {"create": [], "update": []})
        self.assertEqual(r["flows"], {"create": ["Invoice Router InvoiceRouterFlow"], "update": []})
        self.assertIsNone(r["codeapp"])
        self.assertEqual(r["codeapp_skipped"], "--no-app")
        self.assertEqual(self.pa.log, [])

    def test_chat_flow_and_its_reference_are_in_the_plan(self):
        self.summary = rapplication.prepare(EXAMPLE, self.out, host_js=self.host)
        chat = self.summary["chat"]
        r = self.run_plan()
        self.assertEqual(r["flows"], {"create": [chat["name"]], "update": []})
        self.assertEqual(r["powerappsFlows"], r["flows"])
        self.assertIn("rapp_InvoiceRouter.shared_microsoftcopilotstudio", r["connectionReferences"]["create"])
        no_app = self.run_plan(app=False)
        self.assertEqual(no_app["flows"], {"create": [], "update": []})
        self.assertEqual(no_app["connectionReferences"], {"create": [], "keep": [], "sources": {}})

    def test_refusal_never_looks_up_the_code_app(self):
        self.dv.t["bots"]["old"] = {"botid": "old", "schemaname": "rapp_InvoiceRouter", "template": "default-2.1.0"}
        r = self.run_plan()
        self.assertEqual(r["agent"]["operation"], "refuse")
        self.assertIsNone(r["codeapp"])
        self.assertEqual(self.pa.log, [])

    def test_without_an_app_token_the_plan_says_it_could_not_check(self):
        from test_deploy import DATAVERSE_TOKEN, ENV, ReadOnlyDataverse
        r = rapplication.plan(self.out, ENV, lambda: DATAVERSE_TOKEN, dataverse=ReadOnlyDataverse(self.dv))
        self.assertEqual(r["codeapp"]["operation"], "unchecked")
        self.assertIn("read-only app lookup needs a Power Apps token", r["codeapp"]["reason"])

    def test_draft_does_not_upload_publish_or_look_up_the_code_app(self):
        from test_deploy import DATAVERSE_TOKEN, ENV
        with mock.patch("brainfreeze_studio.codeapp_publish.publish", side_effect=AssertionError("no app publish")), \
                mock.patch("brainfreeze_studio.codeapp_publish.plan", side_effect=AssertionError("no app lookup")):
            preview = rapplication.plan(self.out, ENV, lambda: DATAVERSE_TOKEN, dataverse=self.dv,
                                         publish_agent=False)
            self.assertEqual(preview["codeapp_skipped"], "--draft")
            result = self.run_deploy(publish_agent=False)
        self.assertIsNone(result["codeapp"])
        self.assertEqual(result["codeapp_skipped"], "--draft")
        self.assertEqual(self.pa.log, [])
        self.assertFalse(any("PvaPublish" in path for _, path in self.dv.writes))
        saved = json.loads((self.out / "rapplication.json").read_text())
        self.assertEqual(saved["deployed"]["codeapp_skipped"], "--draft")
        self.assertEqual(saved["digest"], preview["digest"])

    def test_expect_refuses_a_changed_prepared_workspace_without_requesting_a_token(self):
        from brainfreeze_studio import deploy as dp
        from test_deploy import ENV
        digest = dp.workspace_digest(self.out / "workspace")[:12]
        settings = self.out / "workspace" / "settings.mcs.yml"
        settings.write_text(settings.read_text() + "\n")
        for function in (rapplication.plan, rapplication.deploy):
            with self.subTest(function=function.__name__):
                token = mock.Mock(side_effect=AssertionError("no token for an unapproved build"))
                with self.assertRaisesRegex(dp.DeployError, "the build changed since the plan"):
                    function(self.out, ENV, token, expect=digest, dataverse=self.dv)
                token.assert_not_called()
                self.assertEqual(self.dv.calls, [])
                self.assertEqual(self.pa.log, [])

    def test_corrupt_app_flow_is_refused_before_the_agent_or_app_is_published(self):
        from brainfreeze_studio import deploy as dp
        twin = self.summary["powerapps_flows"][0]

        class Corrupting(type(self.dv)):
            def __call__(self, method, path, body=None, **kw):
                reply = super().__call__(method, path, body, **kw)
                if method == "PATCH" and path == f"workflows({twin['id']})" and body.get("statecode") == 1:
                    self.t["workflows"][twin["id"]]["clientdata"] = '{"properties":{"definition":{}}}'
                return reply

        self.dv = Corrupting()
        with self.assertRaisesRegex(dp.DeployError, r"flow definition differs.*\(Power Apps\)"):
            self.run_deploy()
        self.assertEqual(self.pa.log, [])
        self.assertFalse(any("PvaPublish" in path for _, path in self.dv.writes))


class DeployTests(unittest.TestCase):
    """rapplication.deploy against an in-memory Dataverse and Power Apps resource provider."""

    def test_agent_flows_chat_flow_and_code_app_deploy_and_redeploy_unchanged(self):
        from test_codeapp import ENV_ID, TOKEN
        from test_deploy import DATAVERSE_TOKEN, ENV, FakeDataverse

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
        dv, rp = Dataverse(), app_service()
        dv.t["connectionreferences"]["ref-cs"] = {
            "connectionreferenceid": "ref-cs", "connectionreferencelogicalname": "someone.shared_microsoftcopilotstudio",
            "connectorid": "/providers/Microsoft.PowerApps/apis/shared_microsoftcopilotstudio", "connectionid": "cs-conn-1",
            "createdon": "2026-01-01"}
        deploy = lambda: rapplication.deploy(out, ENV, lambda: DATAVERSE_TOKEN, lambda: TOKEN, log=lambda *a: None,
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
