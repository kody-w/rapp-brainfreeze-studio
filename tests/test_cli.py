"""The command line's sign-in commands, without a tenant: az, HTTP and the deploy itself are stand-ins."""
import io
import json
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
