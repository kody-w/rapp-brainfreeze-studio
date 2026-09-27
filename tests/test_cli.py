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
from test_deploy import DATAVERSE_TOKEN  # noqa: E402


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

    def test_deploy_safety_flags_reach_the_rapplication_deployer(self):
        code, _, seen = self.deploy("--expect", "123456abcdef", "--keep-extra", "--use-shared-connection")
        self.assertEqual(code, 0)
        self.assertEqual(seen["expect"], "123456abcdef")
        self.assertTrue(seen["keep_extra_components"])
        self.assertTrue(seen["use_shared_connection"])


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
                mock.patch.object(cli, "az_token", return_value=DATAVERSE_TOKEN), \
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
    from test_codeapp import OID, TOKEN
    from test_deploy import ReadOnlyDataverse

    readonly, resources = ReadOnlyDataverse(dv), []
    before = deepcopy((dv.t, dv.writes))

    def token(resource):
        resources.append(resource)
        return TOKEN if resource == AUDIENCE else DATAVERSE_TOKEN

    def connect(environment, get_token):
        assert environment == dv.environment
        assert get_token() == DATAVERSE_TOKEN
        return readonly

    def opener(req, **kw):
        assert req.get_method() == "GET", f"a plan tried to {req.get_method()} {req.full_url}"
        if "/apis/" in req.full_url and "/connections?" in req.full_url:
            return _Response(json.dumps({"value": [{"name": "conn-1", "properties": {
                "createdBy": {"id": OID}, "statuses": [{"status": "Connected"}]}}]}).encode())
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
    CREATE = ('agent:       Test Desk  (rapp_TestDesk)\n'
              'plan:        create: no agent with this schema name exists\n'
              'status:      Draft: new agent, not published\n'
              'tools:       adds 3, updates 0, removes 0, keeps 0\n'
              'remove:      none\n'
              'flows:       creates 1, updates 0\n'
              'settings:    creates 1 environment variables, 1 connection references\n'
              'connection:  rapp_TestDesk.cr.shared_commondataserviceforapps: your connection\n'
              'plan only:   nothing was changed\n')
    UPDATE = ('agent:       Test Desk  (rapp_TestDesk)\n'
              'plan:        update: an agent with this schema name exists ("Test Desk"), components below change\n'
              'status:      Draft: not published\n'
              'tools:       adds 0, updates 0, removes 1, keeps 2\n'
              'remove:      rapp_TestDesk.skill.rapp_desk-help\n'
              'connection:  rapp_TestDesk.cr.shared_commondataserviceforapps: existing binding\n'
              'plan only:   nothing was changed\n')
    REFUSE = ('agent:       Test Desk  (rapp_TestDesk)\n'
              'plan:        refuse: rapp_TestDesk exists but is a classic agent (default-2.1.0); refusing to change it\n'
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
        self.assertEqual(resources[0], self.env.rstrip("/"))
        self.assertTrue(set(resources) <= {self.env.rstrip("/"), AUDIENCE, cli.APIHUB})
        return code, out, err

    def output(self, body):
        return f"digest:      {self.dp.workspace_digest(self.ws)[:12]}\n" + body

    def seed(self):
        return self.dp.deploy(self.ws, self.env, lambda: DATAVERSE_TOKEN, dataverse=self.dv, do_publish=False,
                               connections={"rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"},
                               log=lambda *_: None)

    def test_create_plan_has_the_fixed_labels_with_or_without_draft(self):
        for draft in ([], ["--draft"]):
            with self.subTest(draft=draft):
                self.assertEqual(self.command(*draft), (0, self.output(self.CREATE), ""))

    def test_update_plan_names_every_removal(self):
        self.seed()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        settings = self.ws / "settings.mcs.yml"
        settings.write_text(settings.read_text().replace("Answer briefly.", "Answer carefully."))
        self.assertEqual(self.command(), (0, self.output(self.UPDATE), ""))

    def test_unchanged_plan_omits_zero_flow_and_setting_counts(self):
        self.seed()
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, self.output(
            'agent:       Test Desk  (rapp_TestDesk)\n'
            'plan:        unchanged: agent settings match ("Test Desk"); component and flow changes are listed below\n'
            'status:      Draft: not published\n'
            'tools:       adds 0, updates 0, removes 0, keeps 3\n'
            'remove:      none\n'
            'connection:  rapp_TestDesk.cr.shared_commondataserviceforapps: existing binding\n'
            'plan only:   nothing was changed\n'))

    def test_refusal_is_read_only_and_exits_one_in_human_and_json_modes(self):
        self.dv.t["bots"]["old"] = {"botid": "old", "schemaname": "rapp_TestDesk", "template": "default-2.1.0"}
        for draft in ([], ["--draft"]):
            with self.subTest(draft=draft):
                self.assertEqual(self.command(*draft), (1, self.output(self.REFUSE), ""))
                code, out, err = self.command("--json", *draft)
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(json.loads(out)["agent"]["operation"], "refuse")

    def test_json_is_the_plan_dict_not_a_deploy_summary(self):
        code, out, err = self.command("--json")
        self.assertEqual((code, err), (0, ""))
        r = json.loads(out)
        self.assertEqual(set(r), {"agent", "components", "flows", "environmentVariables", "connectionReferences",
                                  "digest", "status", "keepExtraComponents", "keptExtraComponents"})
        self.assertEqual(r["agent"]["operation"], "create")

    def test_keep_extra_names_the_components_that_will_be_kept(self):
        self.seed()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        code, out, err = self.command("--keep-extra")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("remove:      none\n", out)
        self.assertIn("keep:        rapp_TestDesk.skill.rapp_desk-help (--keep-extra)\n", out)

    def test_plan_reveals_the_existing_display_name_for_a_schema_collision(self):
        made = self.seed()
        self.dv.t["bots"][made["botId"]]["name"] = "Test-Desk"
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("agent:       Test Desk  (rapp_TestDesk)\n", out)
        self.assertIn('plan:        update: an agent with this schema name exists ("Test-Desk"), components below change\n',
                      out)

    def test_expect_refuses_before_the_cli_requests_any_token(self):
        digest = self.dp.workspace_digest(self.ws)[:12]
        settings = self.ws / "settings.mcs.yml"
        settings.write_text(settings.read_text() + "\nchanged\n")
        now = self.dp.workspace_digest(self.ws)[:12]
        with mock.patch.object(cli, "az_token", side_effect=AssertionError("no token")):
            for flags in ([], ["--plan"]):
                code, out, err = run(["deploy", str(self.ws), "--environment", self.env, "--expect", digest, *flags])
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, f"brainfreeze-studio: the build changed since the plan "
                                     f"(digest {now}, planned {digest}); plan again\n")

    def test_app_only_sign_in_is_refused_without_constructing_a_transport(self):
        from test_deploy import jwt
        with mock.patch.object(cli, "az_token", return_value=jwt({"idtyp": "app", "roles": ["Maker"]})), \
                mock.patch.object(self.dp, "Dataverse", side_effect=AssertionError("no transport")):
            for flags in ([], ["--plan"]):
                code, out, err = run(["deploy", str(self.ws), "--environment", self.env, *flags])
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, "brainfreeze-studio: this is an app-only token; sign in as yourself (az login)\n")

    def test_files_overrides_are_previewed_before_deploy(self):
        from test_deploy import files_workspace
        self.ws, _ = files_workspace(self.ws.parent / "files")
        self.dp.deploy(self.ws, self.env, lambda: DATAVERSE_TOKEN, dataverse=self.dv, do_publish=False, log=lambda *_: None,
                        files_site="https://contoso.sharepoint.com",
                        connections={"rapp_TestDesk.shared_sharepointonline": "sp-1",
                                     "rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})
        site, folder = "https://contoso.sharepoint.com/sites/new", "/Other Documents"
        code, out, err = self.command("--json", "--files-site", site, "--files-folder", folder)
        self.assertEqual((code, err), (0, ""))
        changes = json.loads(out)["flows"]["update"]
        self.assertEqual(changes, ["Test Desk JsonDoctorFlow"])
        deployed = self.dp.deploy(self.ws, self.env, lambda: DATAVERSE_TOKEN, dataverse=self.dv, do_publish=False,
                                   log=lambda *_: None, files_site=site, files_folder=folder)
        self.assertEqual(changes, [f["name"] for f in deployed["flows"] if f["operation"] == "updated"])

    def test_an_empty_workspace_omits_zero_counts_but_keeps_the_safety_lines(self):
        for path in (self.ws / "capabilities").rglob("*.mcs.yml"):
            path.unlink()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        for path in (self.ws / "workflows").rglob("*"):
            if path.is_file():
                path.unlink()
        (self.ws.parent / "provenance.json").write_text("{}")
        self.assertEqual(self.command(), (0, self.output(
            'agent:       Test Desk  (rapp_TestDesk)\n'
            'plan:        create: no agent with this schema name exists\n'
            'status:      Draft: new agent, not published\n'
            'remove:      none\n'
            'plan only:   nothing was changed\n'), ""))


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
                self.assertIn('agent:       Invoice Router  (rapp_InvoiceRouter)\n', out)
                self.assertIn('plan:        create: no agent with this schema name exists\n', out)
                self.assertIn("flows:       creates 1, updates 0\n", out)
                self.assertIn("code app:     not built (--no-app)\n", out)
                self.assertTrue(out.endswith("plan only:   nothing was changed\n"))
                self.assertEqual(set(resources), {self.env.rstrip("/")})
                self.assertNotIn("deployed", json.loads((self.out / "rapplication.json").read_text()))

    def test_update_plan_names_the_components_it_would_remove(self):
        from brainfreeze_studio import deploy as dp
        self.rp.prepare(self.ref, self.out, translations=str(self.root / "translations"), app=False)
        made = dp.deploy(self.out / "workspace", self.env, lambda: DATAVERSE_TOKEN, dataverse=self.dv, do_publish=False,
                          log=lambda *_: None)
        self.dv.t["bots"][made["botId"]]["name"] = "Old name"
        self.dv.t["botcomponents"]["legacy"] = {"botcomponentid": "legacy", "_parentbotid_value": made["botId"],
                                               "schemaname": "handmade.topic.Legacy", "name": "Legacy", "data": ""}
        code, out, err, _ = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertIn('update: an agent with this schema name exists ("Old name")', out)
        self.assertIn("remove:      handmade.topic.Legacy", out)

    def test_a_classic_agent_refuses_in_human_and_json_modes(self):
        self.dv.t["bots"]["old"] = {"botid": "old", "schemaname": "rapp_InvoiceRouter", "template": "default-2.1.0"}
        for extra in ([], ["--json", "--draft"]):
            with self.subTest(extra=extra):
                code, out, err, _ = self.command(*extra)
                self.assertEqual((code, err), (1, ""))
                if "--json" in extra:
                    self.assertEqual(json.loads(out)["agent"]["operation"], "refuse")
                else:
                    self.assertIn("plan:        refuse: rapp_InvoiceRouter exists but is a classic agent", out)

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
        dp.deploy(ws, self.env, lambda: DATAVERSE_TOKEN, dataverse=self.dv, do_publish=False, log=lambda *_: None,
                  files_site="https://contoso.sharepoint.com",
                  connections={"rapp_TestDesk.shared_sharepointonline": "sp-1",
                               "rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})
        with mock.patch.object(self.rp, "prepare", return_value=summary):
            code, out, err, _ = self.command("--json", "--files-site", "https://contoso.sharepoint.com/sites/new",
                                             "--files-folder", "/Other Documents")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["flows"]["update"], ["Test Desk JsonDoctorFlow"])

    def test_expect_checks_the_workspace_after_prepare(self):
        import shutil
        from brainfreeze_studio import deploy as dp
        bundle = self.out.parent / "bundle"
        shutil.copytree(self.ref, bundle)
        self.rp.prepare(bundle, self.out, app=False)
        digest = dp.workspace_digest(self.out / "workspace")[:12]
        agent = bundle / "singleton" / "invoice_router_agent.py"
        agent.write_text(agent.read_text() + "\n# changed after approval\n")
        with mock.patch.object(cli, "az_token", side_effect=AssertionError("no token")):
            code, out, err = run(["rapplication", str(bundle), "--out", str(self.out), "--environment", self.env,
                                  "--deploy", "--draft", "--no-app", "--expect", digest])
        self.assertEqual((code, out), (1, ""))
        now = dp.workspace_digest(self.out / "workspace")[:12]
        self.assertNotEqual(now, digest)
        self.assertEqual(err, f"brainfreeze-studio: the build changed since the plan "
                             f"(digest {now}, planned {digest}); plan again\n")

    def test_draft_plan_names_the_app_that_will_not_be_published(self):
        code, out, err, _ = self.command("--draft", app=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("code app:     not published (--draft); run again without --draft to publish it\n", out)


class GenericRapplicationDeploy(unittest.TestCase):
    def test_generic_deploy_handles_a_no_app_port_with_file_readers(self):
        from brainfreeze_studio import deploy as dp, rapplication
        from test_deploy import (ENV, HUB_TOKEN, POWERAPPS_TOKEN, FakeDataverse, FakePowerApps, connection,
                                 files_workspace)
        root = Path(__file__).resolve().parent.parent
        reader = "Reader code"

        class Dataverse(FakeDataverse):
            def __init__(self):
                super().__init__()
                self.t["connectors"] = {}

            def __call__(self, method, path, body=None, **kw):
                if method == "POST" and path == "connectors":
                    self.calls.append((method, path))
                    self.writes.append((method, path))
                    row = dict(body, connectorid="reader", connectorinternalid="shared_reader")
                    self.t["connectors"]["reader"] = row
                    return row, {}
                return super().__call__(method, path, body, **kw)

        def build_port(egg, out, *args, **kw):
            ws, wid = files_workspace(out)
            port = ws / "connectors" / "Reader"
            port.mkdir(parents=True)
            (port / "connector.json").write_text(json.dumps({
                "name": "rapp_reader", "displayName": reader, "referenceLogicalName": "rapp_TestDesk.shared_reader"}))
            (port / "openapi.json").write_text(json.dumps({"info": {"description": "A stand-in file reader port."}}))
            (port / "apiProperties.json").write_text('{"properties":{"connectionParameters":{}}}')
            (port / "script.csx").write_text("public class Script {}")
            flow = ws / "workflows" / f"JsonDoctorFlow-{wid}" / "workflow.json"
            data = json.loads(flow.read_text())
            placeholder = "{{CONNECTOR:Reader code}}"
            data["properties"]["connectionReferences"]["shared_reader"] = {
                "api": {"name": placeholder}, "connection": {"connectionReferenceLogicalName": "rapp_TestDesk.shared_reader"}}
            data["properties"]["definition"]["actions"]["Run"] = {
                "type": "OpenApiConnection", "inputs": {"host": {"apiId": dp.POWERAPPS_APIS + placeholder}}}
            flow.write_text(json.dumps(data))
            return {"workspace": ws, "schema_name": "rapp_TestDesk", "agents": []}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with mock.patch("brainfreeze_studio.build", side_effect=build_port), \
                    mock.patch("brainfreeze_studio.codeapp.host_bundle", side_effect=AssertionError("no Node")):
                summary = rapplication.prepare(root / "examples" / "rapplications" / "invoice_router",
                                                out, name="Test Desk", app=False)
            self.assertIsNone(summary["codeapp"])
            self.assertEqual(list((out / "powerapps-flows").glob("*.json")), [])
            dv = Dataverse()
            pa = FakePowerApps([connection("shared_commondataserviceforapps", "dv-mine"),
                                connection("shared_sharepointonline", "sp-mine"), connection("shared_reader", "code-mine")])

            def token(resource):
                return HUB_TOKEN if resource == cli.APIHUB else POWERAPPS_TOKEN if resource == AUDIENCE else DATAVERSE_TOKEN

            with mock.patch.object(cli, "az_token", side_effect=token), \
                    mock.patch.object(dp, "Dataverse", return_value=dv), \
                    mock.patch.object(dp, "deploy", wraps=dp.deploy) as deployed, \
                    mock.patch("urllib.request.urlopen", side_effect=pa):
                code, output, err = run(["deploy", str(out / "workspace"), "--environment", ENV, "--draft", "--json",
                                         "--files-site", "https://contoso.sharepoint.com/sites/team",
                                         "--files-folder", "/Other Documents"])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(deployed.call_args.kwargs["get_apihub_token"](), HUB_TOKEN)
            result = json.loads(output)
            self.assertEqual(result["digest"], dp.workspace_digest(out / "workspace")[:12])
            self.assertEqual(result["connectors"][0]["internalId"], "shared_reader")
            self.assertEqual(result["files_home"]["site"], "https://contoso.sharepoint.com/sites/team")
            self.assertEqual(result["files_home"]["folder"], "/Other Documents")
            refs = {r["connectionreferenceid"]: r["connectionid"] for r in dv.t["connectionreferences"].values()
                    if r["connectionreferencelogicalname"].startswith("rapp_TestDesk.")}
            self.assertEqual(set(refs.values()), {"dv-mine", "sp-mine", "code-mine"})
            self.assertTrue(all(wf["statecode"] == 1 and "{{CONNECTOR" not in wf["clientdata"]
                                for wf in dv.t["workflows"].values()))
            self.assertFalse(any("PvaPublish" in path for _, path in dv.writes))
            again = dp.deploy(out / "workspace", ENV, lambda: DATAVERSE_TOKEN, dataverse=dv, do_publish=False,
                                connections={"rapp_TestDesk.shared_reader": "manual-code"}, log=lambda *_: None,
                                get_powerapps_token=mock.Mock(side_effect=AssertionError("explicit binding wins")))
            ref = next(r for r in again["connectionReferences"] if r["logicalName"] == "rapp_TestDesk.shared_reader")
            self.assertEqual((ref["connectionId"], ref["source"]), ("manual-code", "explicit connections map"))
            self.assertEqual(again["connectors"][0]["connectionOperation"], "explicit")


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
             "named HackerNews but its code isn't the reviewed grail agent "
             f"(sha256 {hashlib.sha256(self.source.replace('BABAComplianceCheck', 'HackerNews').encode()).hexdigest()[:12]}); "
             "reasoning-only"),
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

    def test_next_deploys_the_prepared_workspace_without_rebuilding(self):
        code, out, err = self.command("--environment", "https://chosen.crm4.dynamics.com/")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-2:], [
            f"next:         python3 -m brainfreeze_studio deploy {shlex.quote(str(self.out / 'workspace'))} "
            "--environment https://chosen.crm4.dynamics.com/ --draft --plan",
            "             then the same without --plan"])


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
