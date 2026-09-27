"""The command line's sign-in commands, without a tenant: az, HTTP and the deploy itself are stand-ins."""
import hashlib
import io
import json
import shlex
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brainfreeze_studio import __main__ as cli  # noqa: E402
from brainfreeze_studio import discovery  # noqa: E402
from brainfreeze_studio.codeapp_publish import AUDIENCE  # noqa: E402


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Environments(unittest.TestCase):
    ROWS = {"value": [
        {"FriendlyName": "Zeta Dev", "Url": "https://zeta.crm.dynamics.com/", "EnvironmentId": "z",
         "OrganizationType": 13, "Region": "NA", "State": 0},
        {"FriendlyName": "alpha", "Url": "https://alpha.crm4.dynamics.com", "EnvironmentId": "a",
         "OrganizationType": 5, "Region": "EUR", "State": 0},
        {"FriendlyName": "Disabled", "Url": "https://off.crm.dynamics.com/", "State": 1},
        {"FriendlyName": "Not Dataverse", "Url": "https://example.com/", "State": 0},
    ]}

    def test_lists_the_enabled_environments_by_name_with_their_kind(self):
        seen = {}

        def opener(req, timeout):
            seen.update(url=req.full_url, auth=req.get_header("Authorization"))
            return _Response(json.dumps(self.ROWS).encode())

        found = discovery.environments("tok", opener=opener)
        self.assertEqual(seen, {"url": "https://globaldisco.crm.dynamics.com/api/discovery/v2.0/Instances",
                                "auth": "Bearer tok"})
        self.assertEqual(found, [
            {"name": "alpha", "url": "https://alpha.crm4.dynamics.com/", "id": "a", "kind": "Sandbox", "region": "EUR"},
            {"name": "Zeta Dev", "url": "https://zeta.crm.dynamics.com/", "id": "z", "kind": "Developer",
             "region": "NA"}])

    def test_a_refused_sign_in_says_to_sign_in_again(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with self.assertRaisesRegex(discovery.DiscoveryError, "az login"):
            discovery.environments("tok", opener=opener)

    def test_the_command_signs_in_for_global_discovery_and_prints_each(self):
        env = {"name": "Dev", "url": "https://dev.crm.dynamics.com/", "id": "1", "kind": "Developer", "region": "NA"}
        with mock.patch.object(cli, "az_token", return_value="tok") as az, \
                mock.patch.object(discovery, "environments", return_value=[env]):
            code, out, _ = run(["environments"])
        self.assertEqual(code, 0)
        az.assert_called_once_with(discovery.DISCOVERY)
        self.assertEqual(out.split(), ["Dev", "Developer", "NA", "https://dev.crm.dynamics.com/"])

    def test_no_environments_is_an_error_that_names_the_fix(self):
        with mock.patch.object(cli, "az_token", return_value="tok"), \
                mock.patch.object(discovery, "environments", return_value=[]):
            code, _, err = run(["environments"])
        self.assertEqual(code, 1)
        self.assertIn("az login", err)


class Deploy(unittest.TestCase):
    RESULT = {"schemaName": "rapp_Desk", "displayName": "Desk", "published": {"status": "skipped"},
              "makerUrl": "https://copilotstudio.microsoft.com/environments/e/agents/b/preview"}

    def setUp(self):
        # every test here stands in for az: no real sign-in is ever asked for a token
        az = mock.patch.object(cli, "az_token", side_effect=lambda resource: f"token-for:{resource}")
        az.start()
        self.addCleanup(az.stop)

    def deploy(self, *extra):
        calls = {}

        def fake(workspace, environment, get_token, **kw):
            calls.update(workspace=workspace, environment=environment, token=get_token(), **kw)
            calls.update(powerapps_token=kw["get_powerapps_token"](), apihub_token=kw["get_apihub_token"]())
            return dict(self.RESULT)

        with mock.patch("brainfreeze_studio.deploy.deploy", fake):
            code, out, err = run(["deploy", "build/workspace", "--environment", "https://org.crm.dynamics.com",
                                  *extra])
        return code, out, calls

    def test_draft_leaves_the_agent_unpublished_and_names_its_test_chat(self):
        code, out, calls = self.deploy("--draft")
        self.assertEqual(code, 0)
        self.assertIs(calls["do_publish"], False)
        self.assertEqual((calls["workspace"], calls["environment"]), ("build/workspace", "https://org.crm.dynamics.com/"))
        self.assertIn("Draft", out)
        self.assertIn(self.RESULT["makerUrl"], out)

    def test_without_draft_it_publishes(self):
        _, _, calls = self.deploy()
        self.assertIs(calls["do_publish"], True)

    def test_every_token_comes_from_the_persons_own_sign_in(self):
        _, _, calls = self.deploy("--draft")
        self.assertEqual(calls["token"], "token-for:https://org.crm.dynamics.com")
        self.assertEqual(calls["powerapps_token"], f"token-for:{AUDIENCE}")
        self.assertEqual(calls["apihub_token"], "token-for:https://apihub.azure.com")

    def test_a_deploy_error_is_reported_not_raised(self):
        from brainfreeze_studio.deploy import DeployError

        def fails(*a, **kw):
            raise DeployError("display name is 50 characters")

        with mock.patch("brainfreeze_studio.deploy.deploy", fails):
            code, _, err = run(["deploy", "ws", "--environment", "https://org.crm.dynamics.com/"])
        self.assertEqual(code, 1)
        self.assertIn("display name is 50 characters", err)


class RapplicationDraft(unittest.TestCase):
    SUMMARY = {"rapp": {"publisher": "@p", "id": "x", "version": "1"}, "rappid": "rappid:@p/x:0",
               "agent": {"schemaName": "rapp_X"}, "tools": [], "codeapp": None,
               "deployed": {"makerUrl": "https://copilotstudio.microsoft.com/environments/e/agents/b/preview"}}

    def setUp(self):
        az = mock.patch.object(cli, "az_token", side_effect=lambda resource: f"token-for:{resource}")
        az.start()
        self.addCleanup(az.stop)

    def deploy(self, *extra):
        seen = {}

        def fake(*a, **kw):
            seen.update(kw)
            return {"agent": {"makerUrl": self.SUMMARY["deployed"]["makerUrl"]}}

        with tempfile.TemporaryDirectory() as d:
            Path(d, "rapplication.json").write_text(json.dumps(self.SUMMARY))
            with mock.patch("brainfreeze_studio.rapplication.prepare", return_value=self.SUMMARY), \
                    mock.patch("brainfreeze_studio.rapplication.deploy", side_effect=fake):
                code, out, _ = run(["rapplication", "x", "--out", d, "--environment", "https://org.crm.dynamics.com/",
                                    "--deploy", *extra])
        return code, out, seen

    def test_draft_reaches_the_agent_deploy(self):
        code, out, seen = self.deploy("--draft", "--no-app")
        self.assertEqual(code, 0)
        self.assertIs(seen["publish_agent"], False)
        self.assertIs(seen["app"], False)
        self.assertIn("Draft", out)

    def test_without_draft_the_agent_is_published(self):
        _, out, seen = self.deploy()
        self.assertIs(seen["publish_agent"], True)
        self.assertNotIn("Draft", out)


class RapplicationNoApp(unittest.TestCase):
    def test_no_app_builds_without_a_host_and_says_why_it_is_absent(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("brainfreeze_studio.codeapp.host_bundle", side_effect=AssertionError("no Node")), \
                mock.patch("brainfreeze_studio.codeapp.build_host", side_effect=AssertionError("no npm")), \
                mock.patch.object(cli, "az_token", side_effect=AssertionError("an offline build needs no token")):
            code, out, err = run(["rapplication", str(root / "examples" / "rapplications" / "invoice_router"),
                                  "--translations", str(root / "translations"), "--out", d, "--no-app"])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("code app:     not built (--no-app)\n", out)
            self.assertIn("parity:       InvoiceRouter 72/72 PROVEN\n", out)
            self.assertNotIn("ships no UI", out)
            self.assertNotIn("answers the app", out)
            self.assertFalse((Path(d) / "codeapp").exists())
            self.assertEqual(list((Path(d) / "powerapps-flows").glob("*.json")), [])

    def test_no_app_deploy_writes_only_the_agent_and_its_agent_flow(self):
        from test_deploy import ENV, FakeDataverse
        root = Path(__file__).resolve().parent.parent
        dv = FakeDataverse()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cli, "az_token", return_value="token"), \
                mock.patch("brainfreeze_studio.deploy.Dataverse", return_value=dv), \
                mock.patch("brainfreeze_studio.codeapp.host_bundle", side_effect=AssertionError("no Node")), \
                mock.patch("brainfreeze_studio.codeapp_publish.publish", side_effect=AssertionError("no app")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("no real HTTP")):
            code, out, err = run(["rapplication", str(root / "examples" / "rapplications" / "invoice_router"),
                                  "--translations", str(root / "translations"), "--out", d, "--no-app", "--deploy",
                                  "--draft", "--environment", ENV])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("code app:     not built (--no-app)\n", out)
            summary = json.loads((Path(d) / "rapplication.json").read_text())
            self.assertEqual(summary["deployed"]["powerapps_flows"], [])
            self.assertIsNone(summary["deployed"]["codeapp"])
        self.assertEqual(len(dv.t["bots"]), 1)
        self.assertEqual(len(dv.t["workflows"]), 1)
        self.assertTrue(all("Power Apps" not in wf["name"] for wf in dv.t["workflows"].values()))
        self.assertFalse(any("PvaPublish" in path for _, path in dv.writes))


def plan_command(argv, dv, powerapps=None):
    from copy import deepcopy
    from test_codeapp import TOKEN
    from test_deploy import ReadOnlyDataverse

    readonly, resources = ReadOnlyDataverse(dv), []
    before = deepcopy((dv.t, dv.writes))

    def token(resource):
        resources.append(resource)
        return TOKEN if resource == AUDIENCE else "dv-token"

    def connect(environment, get_token):
        assert environment == dv.environment
        assert get_token() == "dv-token"
        return readonly

    def opener(req, **kw):
        assert req.get_method() == "GET", f"a plan tried to {req.get_method()} {req.full_url}"
        assert powerapps is not None, f"unexpected HTTP: {req.full_url}"
        return powerapps(req, **kw)

    with mock.patch.object(cli, "az_token", side_effect=token), \
            mock.patch("brainfreeze_studio.deploy.Dataverse", side_effect=connect), \
            mock.patch("brainfreeze_studio.deploy.deploy", side_effect=AssertionError("a plan ran deploy")), \
            mock.patch("brainfreeze_studio.rapplication.deploy", side_effect=AssertionError("a plan ran deploy")), \
            mock.patch("brainfreeze_studio.codeapp_publish.publish", side_effect=AssertionError("a plan ran publish")), \
            mock.patch("urllib.request.urlopen", side_effect=opener):
        result = run(argv)
    assert (dv.t, dv.writes) == before, "a plan changed the environment"
    return (*result, resources)


class DeployPlan(unittest.TestCase):
    CREATE = ('plan:        creates the agent "Test Desk" (rapp_TestDesk)\n'
              'tools:       adds 3, updates 0, removes 0, keeps 0\n'
              'flows:       creates 1, updates 0\n'
              'settings:    creates 1 environment variables, 1 connection references\n'
              'plan only:   nothing was changed\n')
    UPDATE = ('plan:        updates the agent "Test Desk" (rapp_TestDesk) in place\n'
              'tools:       adds 0, updates 0, removes 1: rapp_TestDesk.skill.rapp_desk-help, keeps 2\n'
              'plan only:   nothing was changed\n')
    REFUSE = ('plan:        refuses: rapp_TestDesk exists but is a classic agent (default-2.1.0); refusing to change it\n'
              'plan only:   nothing was changed\n')

    def setUp(self):
        from brainfreeze_studio import deploy as dp
        from test_deploy import ENV, FakeDataverse, make_workspace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws, self.dv, self.dp, self.env = make_workspace(tmp.name), FakeDataverse(), dp, ENV

    def command(self, *extra):
        code, out, err, resources = plan_command(["deploy", str(self.ws), "--environment", self.env.rstrip("/"),
                                                 "--plan", *extra], self.dv)
        self.assertEqual(resources, [self.env.rstrip("/")])
        return code, out, err

    def seed(self):
        return self.dp.deploy(self.ws, self.env, lambda: "token", dataverse=self.dv, do_publish=False, log=lambda *_: None)

    def test_create_plan_has_the_fixed_labels_with_or_without_draft(self):
        for draft in ([], ["--draft"]):
            with self.subTest(draft=draft):
                self.assertEqual(self.command(*draft), (0, self.CREATE, ""))

    def test_update_plan_names_every_removal(self):
        self.seed()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        settings = self.ws / "settings.mcs.yml"
        settings.write_text(settings.read_text().replace("Answer briefly.", "Answer carefully."))
        self.assertEqual(self.command(), (0, self.UPDATE, ""))

    def test_unchanged_plan_omits_zero_flow_and_setting_counts(self):
        self.seed()
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, 'plan:        leaves the agent "Test Desk" (rapp_TestDesk) unchanged\n'
                             'tools:       adds 0, updates 0, removes 0, keeps 3\n'
                             'plan only:   nothing was changed\n')

    def test_refusal_is_read_only_and_exits_one_in_human_and_json_modes(self):
        self.dv.t["bots"]["old"] = {"botid": "old", "schemaname": "rapp_TestDesk", "template": "default-2.1.0"}
        for draft in ([], ["--draft"]):
            with self.subTest(draft=draft):
                self.assertEqual(self.command(*draft), (1, self.REFUSE, ""))
                code, out, err = self.command("--json", *draft)
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(json.loads(out)["agent"]["operation"], "refuse")

    def test_json_is_the_plan_dict_not_a_deploy_summary(self):
        code, out, err = self.command("--json")
        self.assertEqual((code, err), (0, ""))
        r = json.loads(out)
        self.assertEqual(set(r), {"agent", "components", "flows", "environmentVariables", "connectionReferences"})
        self.assertEqual(r["agent"]["operation"], "create")

    def test_files_overrides_are_previewed_before_deploy(self):
        from test_deploy import files_workspace
        self.ws, _ = files_workspace(self.ws.parent / "files")
        self.dp.deploy(self.ws, self.env, lambda: "token", dataverse=self.dv, do_publish=False, log=lambda *_: None,
                        files_site="https://contoso.sharepoint.com",
                        connections={"rapp_TestDesk.shared_sharepointonline": "sp-1"})
        site, folder = "https://contoso.sharepoint.com/sites/new", "/Other Documents"
        code, out, err = self.command("--json", "--files-site", site, "--files-folder", folder)
        self.assertEqual((code, err), (0, ""))
        changes = json.loads(out)["flows"]["update"]
        self.assertEqual(changes, ["Test Desk JsonDoctorFlow"])
        deployed = self.dp.deploy(self.ws, self.env, lambda: "token", dataverse=self.dv, do_publish=False,
                                   log=lambda *_: None, files_site=site, files_folder=folder)
        self.assertEqual(changes, [f["name"] for f in deployed["flows"] if f["operation"] == "updated"])

    def test_an_empty_workspace_prints_only_the_two_required_lines(self):
        for path in (self.ws / "capabilities").rglob("*.mcs.yml"):
            path.unlink()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        for path in (self.ws / "workflows").rglob("*"):
            if path.is_file():
                path.unlink()
        (self.ws.parent / "provenance.json").write_text("{}")
        self.assertEqual(self.command(), (0, 'plan:        creates the agent "Test Desk" (rapp_TestDesk)\n'
                                            'plan only:   nothing was changed\n', ""))


class RapplicationPlan(unittest.TestCase):
    def setUp(self):
        from brainfreeze_studio import rapplication
        from test_deploy import ENV, FakeDataverse
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(__file__).resolve().parent.parent
        self.out, self.dv, self.env, self.rp = Path(tmp.name) / "out", FakeDataverse(), ENV, rapplication
        self.ref = self.root / "examples" / "rapplications" / "invoice_router"
        self.host = Path(tmp.name) / "host.js"
        self.host.write_text("/* stand-in host */")

    def command(self, *extra, app=False, powerapps=None):
        with mock.patch("brainfreeze_studio.codeapp.host_bundle", return_value=self.host) if app else \
                mock.patch("brainfreeze_studio.codeapp.host_bundle", side_effect=AssertionError("no Node")):
            return plan_command(["rapplication", str(self.ref), "--translations", str(self.root / "translations"),
                                 "--out", str(self.out), "--environment", self.env, "--deploy", "--plan",
                                 *([] if app else ["--no-app"]), *extra], self.dv, powerapps)

    def test_no_app_create_plan_is_read_only_with_or_without_draft(self):
        for draft in ([], ["--draft"]):
            with self.subTest(draft=draft):
                code, out, err, resources = self.command(*draft)
                self.assertEqual((code, err), (0, ""))
                self.assertIn('plan:        creates the agent "Invoice Router" (rapp_InvoiceRouter)\n', out)
                self.assertIn("flows:       creates 1, updates 0\n", out)
                self.assertIn("code app:     not built (--no-app)\n", out)
                self.assertTrue(out.endswith("plan only:   nothing was changed\n"))
                self.assertEqual(resources, [self.env.rstrip("/")])
                self.assertNotIn("deployed", json.loads((self.out / "rapplication.json").read_text()))

    def test_update_plan_names_the_components_it_would_remove(self):
        from brainfreeze_studio import deploy as dp
        self.rp.prepare(self.ref, self.out, translations=str(self.root / "translations"), app=False)
        made = dp.deploy(self.out / "workspace", self.env, lambda: "token", dataverse=self.dv, do_publish=False,
                          log=lambda *_: None)
        self.dv.t["bots"][made["botId"]]["name"] = "Old name"
        self.dv.t["botcomponents"]["legacy"] = {"botcomponentid": "legacy", "_parentbotid_value": made["botId"],
                                               "schemaname": "handmade.topic.Legacy", "name": "Legacy", "data": ""}
        code, out, err, _ = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertIn('updates the agent "Invoice Router" (rapp_InvoiceRouter) in place', out)
        self.assertIn("removes 1: handmade.topic.Legacy", out)

    def test_a_classic_agent_refuses_in_human_and_json_modes(self):
        self.dv.t["bots"]["old"] = {"botid": "old", "schemaname": "rapp_InvoiceRouter", "template": "default-2.1.0"}
        for extra in ([], ["--json", "--draft"]):
            with self.subTest(extra=extra):
                code, out, err, _ = self.command(*extra)
                self.assertEqual((code, err), (1, ""))
                if "--json" in extra:
                    self.assertEqual(json.loads(out)["agent"]["operation"], "refuse")
                else:
                    self.assertIn("plan:        refuses: rapp_InvoiceRouter exists but is a classic agent", out)

    def test_app_plan_uses_gets_and_the_same_power_apps_sign_in(self):
        from test_codeapp import FakePowerApps
        code, out, err, resources = self.command(app=True, powerapps=FakePowerApps())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("flows:       creates 2, updates 0\n", out)
        self.assertIn('code app:     creates "Invoice Router"\n', out)
        self.assertEqual(set(resources), {self.env.rstrip("/"), AUDIENCE})

    def test_json_prints_only_the_plan_and_says_the_app_would_update(self):
        from test_codeapp import FakePowerApps
        pa = FakePowerApps([{"name": "existing-app", "appType": "CodeApp",
                             "properties": {"displayName": "Invoice Router", "environment": {"name": "env-1"}}}])
        code, out, err, _ = self.command("--json", app=True, powerapps=pa)
        self.assertEqual((code, err), (0, ""))
        r = json.loads(out)
        self.assertEqual(r["codeapp"]["operation"], "update")
        self.assertEqual(r["codeapp"]["appId"], "existing-app")
        self.assertEqual(len(r["powerappsFlows"]["create"]), 1)
        self.assertNotIn("deployed", r)

    def test_json_keeps_host_build_logs_out_of_the_plan(self):
        from test_codeapp import FakePowerApps

        def host():
            print("stand-in host build")
            return self.host

        with mock.patch("brainfreeze_studio.codeapp.host_bundle", side_effect=host):
            code, out, err, _ = plan_command(["rapplication", str(self.ref), "--out", str(self.out), "--environment",
                                              self.env, "--deploy", "--plan", "--json"], self.dv, FakePowerApps())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["codeapp"]["operation"], "create")
        self.assertEqual(err, "stand-in host build\n")

    def test_plan_requires_a_deploy_target_before_building(self):
        with mock.patch.object(self.rp, "prepare", side_effect=AssertionError("no build")), \
                mock.patch.object(cli, "az_token", side_effect=AssertionError("no token")):
            for flags in (["--plan"], ["--deploy", "--plan"]):
                code, _, err = run(["rapplication", str(self.ref), *flags])
                self.assertEqual(code, 1)
                self.assertIn("--environment", err)

    def test_files_overrides_reach_the_rapplication_plan(self):
        from brainfreeze_studio import deploy as dp
        from test_deploy import files_workspace
        ws, _ = files_workspace(self.out)
        summary = {"agent": {"displayName": "Test Desk"}, "codeapp": None}
        (self.out / "rapplication.json").write_text(json.dumps(summary))
        dp.deploy(ws, self.env, lambda: "token", dataverse=self.dv, do_publish=False, log=lambda *_: None,
                  files_site="https://contoso.sharepoint.com",
                  connections={"rapp_TestDesk.shared_sharepointonline": "sp-1"})
        with mock.patch.object(self.rp, "prepare", return_value=summary):
            code, out, err, _ = self.command("--json", "--files-site", "https://contoso.sharepoint.com/sites/new",
                                             "--files-folder", "/Other Documents")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["flows"]["update"], ["Test Desk JsonDoctorFlow"])


class BuildMessages(unittest.TestCase):
    REASON = "no proven profile or translation spec for it"

    def setUp(self):
        from test_build import ROUTER, egg
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "out"
        self.source = ROUTER.replace("InvoiceRouter", "BABAComplianceCheck")
        self.egg = egg({"agents/check_agent.py": self.source.encode()}, str(self.root / "desk.egg"))

    def command(self, *extra):
        with mock.patch.object(cli, "az_token", side_effect=AssertionError("a build must stay offline")):
            return run(["build", str(self.egg), "--out", str(self.out), "--name", "Test Desk",
                        "--publisher-prefix", "rapp", *extra])

    def test_an_unmatched_skill_prints_and_records_its_reason(self):
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"  BABAComplianceCheck -> reasoning-only skill  ({self.REASON})\n", out)
        agents = json.loads((self.out / "provenance.json").read_text())["agents"]
        self.assertEqual((agents[0]["as"], agents[0]["note"]), ("reasoning-only skill", self.REASON))
        code, out, err = self.command("--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["agents"], agents)

    def test_specific_skill_reasons_are_not_replaced(self):
        from test_build import egg
        specs = self.root / "translations"
        specs.mkdir()
        digest = hashlib.sha256(self.source.encode()).hexdigest()[:12]
        cases = [
            ("profile", self.source.replace("BABAComplianceCheck", "HackerNews"), None,
             "no --sdk-dir: proven profiles unavailable, deployed as a reasoning-only skill"),
            ("proof", self.source, {"agent": "BABAComplianceCheck", "flow_name": "CheckFlow"},
             "translation not proven: stand-in proof refused"),
            ("materialized", self.source, {"agent": "BABAComplianceCheck", "mode": "materialized", "source_sha256": "0" * 64},
             f"materialized translation is for different code (sha256 000000000000, egg has {digest}); rematerialize it"),
        ]
        for kind, source, spec, reason in cases:
            with self.subTest(kind=kind), mock.patch("brainfreeze_studio.flows.prove",
                    return_value={"parity": False, "reason": "stand-in proof refused", "passed": 0, "cases": 1}):
                egg({"agents/check_agent.py": source.encode()}, str(self.egg))
                if spec:
                    (specs / "check.json").write_text(json.dumps(spec))
                code, out, err = self.command(*(["--translations", str(specs)] if spec else []))
                self.assertEqual((code, err), (0, ""))
                agent = json.loads((self.out / "provenance.json").read_text())["agents"][0]
                self.assertEqual((agent["as"], agent["note"]), ("reasoning-only skill", reason))
                self.assertIn(f"  ({reason})\n", out)

    def test_next_keeps_the_environment_and_quotes_a_workspace_with_spaces(self):
        self.out = self.root / "out with spaces"
        environment = "https://chosen.crm4.dynamics.com/"
        code, out, err = self.command("--environment", environment)
        self.assertEqual((code, err), (0, ""))
        workspace = str(self.out / "workspace")
        self.assertEqual(out.splitlines()[-2:], [
            f"next:        python3 -m brainfreeze_studio deploy '{workspace}' --environment {environment} --draft --plan",
            "             then the same without --plan"])
        self.assertEqual(shlex.split(out.splitlines()[-2].split("next:", 1)[1]),
                         ["python3", "-m", "brainfreeze_studio", "deploy", workspace,
                          "--environment", environment, "--draft", "--plan"])

    def test_next_uses_the_placeholder_when_no_environment_was_given(self):
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        workspace = shlex.quote(str(self.out / "workspace"))
        self.assertEqual(out.splitlines()[-2:], [
            f"next:        python3 -m brainfreeze_studio deploy {workspace} "
            "--environment https://<org>.crm.dynamics.com/ --draft --plan",
            "             then the same without --plan"])


class RapplicationMessages(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(__file__).resolve().parent.parent
        self.out = Path(tmp.name) / "out"
        self.host = Path(tmp.name) / "host.js"
        self.host.write_text("/* stand-in host */")

    def command(self, *extra):
        with mock.patch("brainfreeze_studio.codeapp.host_bundle", return_value=self.host), \
                mock.patch.object(cli, "az_token", side_effect=AssertionError("a build must stay offline")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("no real HTTP")):
            return run(["rapplication", str(self.root / "examples" / "rapplications" / "invoice_router"),
                        "--out", str(self.out), *extra])

    def test_the_flow_mapping_precedes_app_tools_and_keeps_parity(self):
        code, out, err = self.command("--translations", str(self.root / "translations"))
        self.assertEqual((code, err), (0, ""))
        mapping = f"  {'InvoiceRouter':<18} -> agent flow (translated, parity proven)"
        tool = f"  {'InvoiceRouter':<22} -> flow for the app: Invoice Router InvoiceRouterFlow (Power Apps)"
        lines = out.splitlines()
        self.assertIn(mapping, lines)
        self.assertIn(tool, lines)
        self.assertLess(lines.index(mapping), lines.index(tool))
        self.assertIn("parity:       InvoiceRouter 72/72 PROVEN", lines)
        agent = json.loads((self.out / "rapplication.json").read_text())["agent"]["agents"][0]
        self.assertEqual(agent["as"], "agent flow (translated, parity proven)")
        self.assertNotIn("note", agent)

    def test_the_skill_mapping_precedes_app_tools_and_includes_its_reason(self):
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        mapping = f"  {'InvoiceRouter':<18} -> reasoning-only skill  ({BuildMessages.REASON})"
        tool = f"  {'InvoiceRouter':<22} -> the agent answers the app"
        lines = out.splitlines()
        self.assertIn(mapping, lines)
        self.assertIn(tool, lines)
        self.assertLess(lines.index(mapping), lines.index(tool))
        summary = json.loads((self.out / "rapplication.json").read_text())
        provenance = json.loads((self.out / "provenance.json").read_text())
        self.assertEqual(summary["agent"]["agents"], provenance["agents"])
        self.assertEqual(provenance["agents"][0]["note"], BuildMessages.REASON)

    def test_next_adds_a_plan_without_changing_the_rapplication_hint(self):
        code, out, err = self.command("--environment", "https://chosen.crm4.dynamics.com/")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-2:], [
            "next:         --environment https://<org>.crm.dynamics.com/ --deploy --plan",
            "              then the same without --plan"])


class AzureCli(unittest.TestCase):
    def test_runs_the_az_that_path_lookup_finds(self):
        # On Windows az is az.cmd, which a bare "az" can't start without a shell.
        cli._TOKENS.clear()
        self.addCleanup(cli._TOKENS.clear)
        with mock.patch.object(cli.shutil, "which", return_value="C:/az/az.cmd"), \
                mock.patch.object(cli.subprocess, "run") as ran:
            ran.return_value.stdout = json.dumps({"accessToken": "t", "expires_on": time.time() + 3600})
            self.assertEqual(cli.az_token("https://r"), "t")
        self.assertEqual(ran.call_args[0][0][:4], ["C:/az/az.cmd", "account", "get-access-token", "--resource"])


if __name__ == "__main__":
    unittest.main()
