"""The Azure Function example (examples/azure-function): who may call it (auth.py), background deploy jobs (jobs.py)
and the routes themselves. Offline: fake storage, fake Dataverse, fake build and deploy."""
import base64
import importlib.util
import io
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FUNCTION_DIR = HERE.parent / "examples" / "azure-function"
sys.path.insert(0, str(FUNCTION_DIR))
import auth  # noqa: E402
import jobs  # noqa: E402

try:
    import azure.functions as func
except ImportError:
    func = None

ENV = "https://example.crm.dynamics.com/"


def token(exp_in=3600, oid="user-1", tid="tenant-1", scp="user_impersonation", aud=None):
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + exp_in, "oid": oid, "tid": tid,
                                                "aud": aud or ENV.rstrip("/"), "scp": scp,
                                                "upn": f"{oid}@example.com"}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def who_am_i(accepted, calls):
    """A fake Dataverse WhoAmI: accepts every token, or refuses every token the way Dataverse refuses a forged one."""
    def opener(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization")))
        if not accepted:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        return Resp(json.dumps({"UserId": "u-1", "OrganizationId": "o-1"}).encode())
    return opener


USER_ID = "00000000-0000-0000-0000-0000000000aa"
DISCOVERY_ROWS = [
    {"FriendlyName": "zeta", "Url": "https://zeta.crm4.dynamics.com", "State": 0, "OrganizationType": 5,
     "EnvironmentId": "e-z", "Region": "EMEA"},
    {"FriendlyName": "Alpha (default)", "Url": "https://alpha.crm.dynamics.com", "State": 0, "OrganizationType": 12,
     "EnvironmentId": "e-a", "Region": "NA"},
    {"FriendlyName": "Disabled", "Url": "https://off.crm.dynamics.com", "State": 1, "OrganizationType": 5},
    {"FriendlyName": "Elsewhere", "Url": "https://gov.crm.microsoftdynamics.us", "State": 0, "OrganizationType": 0},
    {"FriendlyName": "beta", "Url": "https://beta.crm.dynamics.com/", "State": 0, "OrganizationType": 99},
]


class FakeCloud:
    """Microsoft sign-in, Global Discovery and Dataverse, as one urlopen."""

    def __init__(self, lacking=(), absent=(), refuse_refresh=False, discovery_status=200, rights_status=200):
        self.lacking, self.absent, self.rights_status = set(lacking), set(absent), rights_status
        self.refuse_refresh, self.discovery_status = refuse_refresh, discovery_status
        self.requests = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.requests.append((req.get_method(), url, req.get_header("Authorization"),
                              req.data.decode() if req.data else None))
        fail = lambda code, body=b"{}": urllib.error.HTTPError(url, code, "no", {}, io.BytesIO(body))
        if url.startswith("https://login.microsoftonline.com/"):
            if self.refuse_refresh:
                raise fail(400, json.dumps({"error": "invalid_grant", "error_description":
                                            "AADSTS70000: The refresh token was revoked. Trace ID: abc"}).encode())
            return Resp(json.dumps({"access_token": token(), "refresh_token": "rt-2"}).encode())
        if url.startswith("https://globaldisco.crm.dynamics.com/"):
            if self.discovery_status != 200:
                raise fail(self.discovery_status)
            return Resp(json.dumps({"value": DISCOVERY_ROWS}).encode())
        if url.endswith("/WhoAmI"):
            return Resp(json.dumps({"UserId": USER_ID}).encode())
        privilege = url.split("PrivilegeName='")[1].rstrip("')")
        if self.rights_status != 200:
            raise fail(self.rights_status)
        if privilege in self.absent:
            raise fail(400)
        return Resp(json.dumps({"RolePrivileges": [] if privilege in self.lacking else [{"Depth": "Global"}]}).encode())


class FakeStore:
    def __init__(self):
        self.docs, self.writes = {}, 0

    def put(self, name, doc):
        self.docs[name] = json.loads(json.dumps(doc))
        self.writes += 1

    def get(self, name):
        return json.loads(json.dumps(self.docs[name])) if name in self.docs else None

    def delete(self, name):
        self.docs.pop(name, None)


class VerifierTests(unittest.TestCase):
    def test_dataverse_decides_so_a_forged_token_is_refused(self):
        calls = []
        verify = auth.Verifier(opener=who_am_i(False, calls))
        with self.assertRaises(PermissionError) as e:
            verify(token(), ENV)
        self.assertIn("did not accept this sign-in (HTTP 401)", str(e.exception))
        self.assertEqual(calls[0][0], ENV + "api/data/v9.2/WhoAmI")
        self.assertTrue(calls[0][1].startswith("Bearer "))

    def test_an_accepted_token_is_checked_once(self):
        calls = []
        verify = auth.Verifier(opener=who_am_i(True, calls))
        t = token()
        found, who = verify(t, ENV)
        self.assertEqual((found["oid"], who["UserId"]), ("user-1", "u-1"))
        verify(t, ENV)
        self.assertEqual(len(calls), 1)

    def test_tokens_it_cannot_act_on_never_reach_dataverse(self):
        calls = []
        verify = auth.Verifier(["TENANT-1"], opener=who_am_i(True, calls))
        for bad, why in ((token(aud="https://other.crm.dynamics.com"), "not " + ENV), (token(scp=""), "app-only"),
                         (token(exp_in=30), "expired"), (token(tid="tenant-2"), "own organizations"),
                         ("", "sign in first"), ("not-a-token", "sign in first")):
            with self.assertRaises(PermissionError) as e:
                verify(bad, ENV)
            self.assertIn(why, str(e.exception))
        odd = lambda payload: "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".s"
        for bad in (odd(["not", "claims"]), odd({"aud": ENV.rstrip("/"), "scp": "user_impersonation", "exp": "9e9"})):
            with self.assertRaises(PermissionError):
                verify(bad, ENV)
        self.assertEqual(calls, [])
        verify(token(tid="tenant-1"), ENV)
        self.assertEqual(len(calls), 1)


class EnvironmentTests(unittest.TestCase):
    def test_the_users_environments_come_from_global_discovery(self):
        cloud = FakeCloud()
        found = auth.Verifier(opener=cloud).environments(token(aud=auth.DISCOVERY))
        self.assertEqual([(e["name"], e["url"], e["kind"]) for e in found], [
            ("Alpha (default)", "https://alpha.crm.dynamics.com/", "Default"),
            ("beta", "https://beta.crm.dynamics.com/", ""),
            ("zeta", "https://zeta.crm4.dynamics.com/", "Sandbox")])
        self.assertEqual(found[2]["region"], "EMEA")
        method, url, header, _ = cloud.requests[0]
        self.assertEqual((method, url), ("GET", "https://globaldisco.crm.dynamics.com/api/discovery/v2.0/Instances"))
        self.assertTrue(header.startswith("Bearer "))

    def test_only_a_discovery_token_from_an_allowed_tenant_is_sent_there(self):
        cloud = FakeCloud()
        verify = auth.Verifier(["tenant-1"], opener=cloud)
        for bad in (token(), token(aud=auth.DISCOVERY, tid="tenant-2"), token(aud=auth.DISCOVERY, scp=""), ""):
            with self.assertRaises(PermissionError):
                verify.environments(bad)
        self.assertEqual(cloud.requests, [])

    def test_discovery_refusing_the_sign_in_asks_for_a_new_one(self):
        with self.assertRaises(PermissionError) as e:
            auth.Verifier(opener=FakeCloud(discovery_status=401)).environments(token(aud=auth.DISCOVERY))
        self.assertIn("sign in again", str(e.exception))
        with self.assertRaises(RuntimeError):
            auth.Verifier(opener=FakeCloud(discovery_status=503)).environments(token(aud=auth.DISCOVERY))

    def test_missing_rights_name_what_a_deploy_could_not_create(self):
        verify = auth.Verifier(opener=FakeCloud())
        self.assertEqual(verify.missing_rights(token(), ENV, USER_ID), [])
        cloud = FakeCloud(lacking={"prvCreatebot", "prvCreateWorkflow"}, absent={"prvCreateEnvironmentVariableDefinition"})
        self.assertEqual(auth.Verifier(opener=cloud).missing_rights(token(), ENV, USER_ID), ["agents", "flows"])
        self.assertEqual(len(cloud.requests), len(auth.MAKER_RIGHTS))
        self.assertIn(f"systemusers({USER_ID})/Microsoft.Dynamics.CRM.RetrieveUserPrivilegeByPrivilegeName"
                      "(PrivilegeName='prvCreatebot')", " ".join(r[1] for r in cloud.requests))
        with self.assertRaises(RuntimeError):
            verify.missing_rights(token(), ENV, "') or 1 eq 1 or ('")

    def test_a_refused_refresh_says_why_without_the_trace(self):
        user = auth.UserToken(None, ENV, "rt-1", client_id="app", opener=FakeCloud(refuse_refresh=True))
        with self.assertRaises(PermissionError) as e:
            user()
        self.assertEqual(str(e.exception), "could not refresh your sign-in; sign in again "
                                           "(AADSTS70000: The refresh token was revoked.)")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.calls = []

    def build(self, egg, out, name, prefix, **kw):
        self.calls.append(("build", Path(egg).read_bytes(), name, kw))
        ws = Path(out) / "workspace"
        ws.mkdir(parents=True)
        (Path(out) / "provenance.json").write_text(json.dumps({"parity": {"A": {"parity": True}, "B": {"parity": True}}}))
        return {"schema_name": kw.get("schema_name") or "rapp_X"}

    def deploy(self, workspace, environment, get_token, log):
        self.calls.append(("deploy", Path(workspace).name, environment, get_token()))
        log("1/7 connection references")
        return {"botId": "b1", "makerUrl": "https://copilotstudio.microsoft.com/environments/e/agents/b1/preview"}

    def queue(self, request, access=None, refresh=None):
        return jobs.new_job(self.store, {"environment": ENV, "name": "Desk", **request}, access or token(), "user-1",
                            "user@example.com", refresh_token=refresh)

    def execute(self, job_id, **kw):
        return jobs.run_job(job_id, self.store, self.build, self.deploy, sdk_dir="sdk", flush_every=0, **kw)

    def test_a_job_builds_deploys_and_keeps_no_sign_in(self):
        job = self.queue({"egg": base64.b64encode(b"EGG").decode(), "schemaName": "rapp_Desk"})
        self.assertEqual(self.store.get(f"{job}/status.json")["state"], "queued")
        status = self.execute(job)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["result"]["botId"], "b1")
        self.assertIn("built rapp_Desk", " ".join(status["log"]))
        self.assertIn("2/2 translations proven", " ".join(status["log"]))
        self.assertEqual(self.calls[0][:3], ("build", b"EGG", "Desk"))
        self.assertEqual(self.calls[1][:3], ("deploy", "workspace", ENV))
        self.assertIsNone(self.store.get(f"{job}/request.json"))
        self.assertNotIn("access_token", json.dumps(self.store.docs))

    def test_status_is_for_the_owner_only(self):
        job = self.queue({"egg": "RUdH"})
        self.assertEqual(jobs.read_status(self.store, job, "user-1")["state"], "queued")
        self.assertIsNone(jobs.read_status(self.store, job, "someone-else"))
        self.assertIsNone(jobs.read_status(self.store, "../other/status", "user-1"))
        self.assertNotIn("owner", jobs.read_status(self.store, job, "user-1"))

    def test_a_failure_is_recorded_and_still_drops_the_sign_in(self):
        def broken(*a, **k):
            raise RuntimeError("Dataverse said no")
        job = self.queue({"egg": "RUdH"})
        status = jobs.run_job(job, self.store, self.build, broken, sdk_dir="sdk", flush_every=0)
        self.assertEqual((status["state"], status["error"]), ("failed", "RuntimeError: Dataverse said no"))
        self.assertIsNone(self.store.get(f"{job}/request.json"))

    def test_an_expired_sign_in_without_refresh_fails_fast(self):
        job = self.queue({"egg": "RUdH"}, access=token(exp_in=-10))
        status = self.execute(job)
        self.assertEqual(status["state"], "failed")
        self.assertIn("sign in again", status["error"])
        self.assertEqual([c[0] for c in self.calls], [])

    def test_translations_are_refused_unless_allowed(self):
        job = self.queue({"egg": "RUdH", "translations": [{"agent": "A"}]})
        self.assertIn("BFS_ALLOW_TRANSLATIONS", self.execute(job)["error"])
        job = self.queue({"egg": "RUdH", "translations": [{"agent": "A"}]})
        self.assertEqual(self.execute(job, allow_translations=True)["state"], "succeeded")
        self.assertTrue(self.calls[-2][3]["translations"])

    def test_an_uploaded_workspace_deploys_without_a_build(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("workspace/settings.mcs.yml", "displayName: X\n")
        job = self.queue({"workspaceZip": base64.b64encode(buf.getvalue()).decode()})
        self.assertEqual(self.execute(job)["state"], "succeeded")
        self.assertEqual([c[0] for c in self.calls], ["deploy"])
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("../escape.txt", "x")
        job = self.queue({"workspaceZip": base64.b64encode(bad.getvalue()).decode()})
        self.assertIn("unsafe path", self.execute(job)["error"])

    def test_a_job_its_host_stopped_reads_as_failed(self):
        job = self.queue({"egg": "RUdH"})
        later = time.time() + jobs.TIME_LIMIT + 600
        self.assertEqual(jobs.read_status(self.store, job, "user-1")["state"], "queued")
        stopped = jobs.read_status(self.store, job, "user-1", now=later)
        self.assertEqual(stopped["state"], "failed")
        self.assertIn("queue it again", stopped["error"])
        self.assertIsNone(jobs.read_status(self.store, job, None))
        self.execute(job)
        self.assertEqual(jobs.read_status(self.store, job, "user-1", now=later)["state"], "succeeded")

    def test_a_zip_that_unpacks_too_big_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("workspace/big.txt", "0" * 4096)
        with mock.patch.object(jobs, "MAX_UNZIPPED", 1024):
            job = self.queue({"workspaceZip": base64.b64encode(buf.getvalue()).decode()})
            self.assertIn("unpacks to more than", self.execute(job)["error"])
        self.assertEqual(self.calls, [])

    def test_the_sign_in_refreshes_near_expiry_with_a_refresh_token(self):
        fresh = token(exp_in=3600)
        seen = []

        def opener(req, timeout=None):
            seen.append(req.data.decode())
            return Resp(json.dumps({"access_token": fresh, "refresh_token": "rt-2"}).encode())
        t = jobs.UserToken(token(exp_in=120), ENV, "rt-1", client_id="app", opener=opener)
        self.assertEqual(t(), fresh)
        self.assertEqual((t.refresh_token, t.refreshed), ("rt-2", 1))
        self.assertIn("grant_type=refresh_token", seen[0])
        self.assertIn("user_impersonation", seen[0])
        self.assertEqual(t(), fresh)
        self.assertEqual(t.refreshed, 1)

    def rapp_module(self, codeapp=True, files=False):
        calls = self.calls

        class Rapps:
            STORE = "https://raw.githubusercontent.com/kody-w/RAPP_Store/main/"

            @staticmethod
            def prepare(ref, out, **kw):
                calls.append(("prepare", ref, kw))
                if files:                          # an agent that reads files, as the build records one
                    Path(out).mkdir(parents=True, exist_ok=True)
                    (Path(out) / "provenance.json").write_text(json.dumps({"environment_variables": [
                        {"schemaName": "rapp_RappFilesSite", "defaultValue": "", "files": "site"}]}))
                return {"rappid": "rappid:@x/y:" + "0" * 64, "rapp": {"id": "y"}, "chat": None,
                        "agent": {"schemaName": "rapp_Y"}, "tools": [{"name": "Y", "flow": None}],
                        "powerapps_flows": [{"id": "f1"}],
                        "codeapp": {"report": {"risks": ["calls eval()"]}} if codeapp else None}

            @staticmethod
            def deploy(out, environment, get_dv, get_pa, log, **kw):
                calls.append(("deploy", environment, get_dv(), get_pa() if get_pa else None, kw))
                log("flows for the code app")
                return {"agent": {"makerUrl": "https://copilotstudio.microsoft.com/x"}, "powerapps_flows": [],
                        "codeapp": {"appId": "app-1", "playUrl": "https://apps.powerapps.com/play/x"} if get_pa else None}
        return Rapps

    def test_a_rapplication_job_deploys_its_agent_flows_and_app(self):
        pa = token(aud="https://service.powerapps.com/")
        job = self.queue({"rapplication": "@x/y", "powerapps_token": pa})
        status = self.execute(job, rapplications=self.rapp_module(), host_js="host.js")
        self.assertEqual(status["state"], "succeeded", status.get("error"))
        prepare, deploy = self.calls
        self.assertEqual((prepare[0], prepare[1], prepare[2]["host_js"], prepare[2]["store"], prepare[2]["name"]),
                         ("prepare", "@x/y", "host.js", "https://raw.githubusercontent.com/kody-w/RAPP_Store/main/", None))
        self.assertEqual(deploy[:2], ("deploy", ENV))
        self.assertEqual(deploy[3], pa)
        self.assertEqual(status["result"]["codeapp"]["playUrl"], "https://apps.powerapps.com/play/x")
        self.assertIn("  ! calls eval()", status["log"])
        self.assertIsNone(self.store.get(f"{job}/request.json"))           # both tokens leave with the request

    def test_bundled_ports_are_used_and_run_nothing_unless_translations_are_allowed(self):
        job = self.queue({"rapplication": "@x/y", "powerapps_token": token(aud="https://service.powerapps.com/")})
        self.execute(job, rapplications=self.rapp_module(), bundled_translations=Path("/bundle"))
        self.assertEqual((self.calls[0][2]["translations"], self.calls[0][2]["run_proofs"]), ("/bundle", False))
        self.calls.clear()
        job = self.queue({"rapplication": "@x/y", "powerapps_token": token(aud="https://service.powerapps.com/")})
        self.execute(job, rapplications=self.rapp_module(), bundled_translations=Path("/bundle"), allow_translations=True)
        self.assertEqual((self.calls[0][2]["translations"], self.calls[0][2]["run_proofs"]), ("/bundle", True))

    def test_without_consent_the_agent_deploys_and_the_app_waits(self):
        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps(
                {"error": "invalid_grant", "error_description": "AADSTS65001: The user or administrator has not "
                 "consented to use the application. Trace ID: 1"}).encode()))
        job = self.queue({"rapplication": "@x/y"}, refresh="rt-1")
        status = self.execute(job, rapplications=self.rapp_module(), client_id="app", opener=refuse)
        self.assertEqual(status["state"], "succeeded", status.get("error"))
        self.assertEqual(self.calls[1][3], None)                             # no Power Apps token to deploy with
        self.assertEqual(status["result"]["codeapp"]["skipped"], "consent")
        self.assertIn("AADSTS65001", status["result"]["codeapp"]["why"])
        self.assertIn("the code app waits", " ".join(status["log"]))
        job = self.queue({"rapplication": "@x/y"})
        status = self.execute(job, rapplications=self.rapp_module())
        self.assertEqual(status["result"]["codeapp"]["skipped"], "no-token")

    def test_an_agent_that_reads_files_gets_an_api_hub_token_and_where_its_files_are(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(urllib.parse.parse_qs(req.data.decode())["scope"][0])
            return Resp(json.dumps({"access_token": token(aud="x"), "refresh_token": "rt-2"}).encode())
        job = self.queue({"rapplication": "@x/y", "files_site": "https://contoso.sharepoint.com/sites/team",
                          "files_folder": "/Shared Documents/RAPP"}, refresh="rt-1")
        status = self.execute(job, rapplications=self.rapp_module(files=True), client_id="app", opener=opener)
        self.assertEqual(status["state"], "succeeded", status.get("error"))
        kw = self.calls[1][4]
        self.assertIsNotNone(kw["get_apihub_token"])
        self.assertEqual((kw["files_site"], kw["files_folder"]),
                         ("https://contoso.sharepoint.com/sites/team", "/Shared Documents/RAPP"))
        self.assertIn(auth.APIHUB_SCOPE, seen)

    def test_without_an_api_hub_token_the_deploy_uses_a_connection_the_user_has(self):
        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps(
                {"error": "invalid_grant", "error_description": "AADSTS65001: not consented. Trace ID: 1"}).encode()))
        job = self.queue({"rapplication": "@x/y"}, refresh="rt-1")
        status = self.execute(job, rapplications=self.rapp_module(codeapp=False, files=True), client_id="app",
                              opener=refuse)
        self.assertEqual(status["state"], "succeeded", status.get("error"))
        self.assertIsNone(self.calls[1][4]["get_apihub_token"])
        self.assertIn("a SharePoint connection you already have", " ".join(status["log"]))
        job = self.queue({"rapplication": "@x/y"}, refresh="rt-1")          # no files: no API Hub token asked for
        self.calls.clear()
        self.execute(job, rapplications=self.rapp_module(codeapp=False), client_id="app", opener=refuse)
        self.assertIsNone(self.calls[1][4]["get_apihub_token"])

    def test_a_powerapps_token_is_got_with_the_refresh_token_and_its_own_scope(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req.data.decode())
            return Resp(json.dumps({"access_token": token(aud="https://service.powerapps.com/")}).encode())
        t = jobs.UserToken(None, None, "rt-1", client_id="app", opener=opener, scope=auth.POWERAPPS_SCOPE)
        self.assertEqual(auth.claims(t())["aud"], "https://service.powerapps.com/")
        self.assertIn("scope=https%3A%2F%2Fservice.powerapps.com%2F%2FUser+offline_access", seen[0])

    def test_the_blob_store_speaks_rest_with_a_bearer_token(self):
        seen = []

        class Created(Resp):
            status = 201

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url, dict(req.header_items())))
            return Created(b"")
        store = jobs.BlobStore("acct", token_source=lambda resource: ("mi-token", time.time() + 3600), opener=opener)
        store.put("j1/status.json", {"state": "queued"})
        self.assertEqual(seen[0][:2], ("PUT", "https://acct.blob.core.windows.net/bfs-deploy-jobs?restype=container"))
        method, url, headers = seen[1]
        self.assertEqual((method, url), ("PUT", "https://acct.blob.core.windows.net/bfs-deploy-jobs/j1/status.json"))
        self.assertEqual(headers["Authorization"], "Bearer mi-token")
        self.assertEqual(headers["X-ms-blob-type"], "BlockBlob")



@unittest.skipIf(func is None, "azure-functions is not installed")
class FunctionRouteTests(unittest.TestCase):
    """The routes, called the way the Functions host calls them."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HERE.parent))       # the repo's brainfreeze_studio, not a copy bundled for publish
        import function_app
        cls.fa = function_app
        cls.create = staticmethod(function_app.create_job.build().get_user_function())
        cls.status = staticmethod(function_app.job_status.build().get_user_function())
        cls.deploy = staticmethod(function_app.deploy_egg.build().get_user_function())

    def setUp(self):
        self.store, self.calls, self.accept = FakeStore(), [], True
        self.saved = (self.fa._store, self.fa.verify, self.fa.TRANSLATIONS)
        self.fa._store, self.fa.TRANSLATIONS = self.store, False
        self.fa.verify = auth.Verifier(opener=lambda req, timeout=None: who_am_i(self.accept, self.calls)(req, timeout))

    def tearDown(self):
        self.fa._store, self.fa.verify, self.fa.TRANSLATIONS = self.saved

    def post(self, handler, route, body, tok):
        queued = []

        class Out:
            def set(self, value):
                queued.append(value)
        req = func.HttpRequest("POST", f"/api/{route}", headers={"Authorization": "Bearer " + tok},
                               body=json.dumps(body).encode())
        resp = handler(req, Out()) if route == "jobs" else handler(req)
        return resp.status_code, json.loads(resp.get_body()), queued

    def read(self, job, tok):
        req = func.HttpRequest("GET", f"/api/jobs/{job}", headers={"Authorization": "Bearer " + tok}, body=b"",
                               route_params={"job_id": job})
        resp = self.status(req)
        return resp.status_code, json.loads(resp.get_body())

    def test_a_forged_token_does_nothing(self):
        self.accept = False
        with mock.patch.object(self.fa, "_fetch_egg", side_effect=AssertionError("fetched the egg")), \
                mock.patch.object(self.fa.bs, "build", side_effect=AssertionError("built the egg")):
            status, body, queued = self.post(self.create, "jobs", {"environment": ENV, "name": "Desk",
                                                                   "eggUrl": "https://example.com/a.egg"}, token())
            self.assertEqual((status, queued, self.store.docs), (401, [], {}))
            self.assertIn("did not accept", body["error"])
            status, body, _ = self.post(self.deploy, "deploy", {"environment": ENV, "name": "Desk", "egg": "RUdH"},
                                        token(oid="user-2"))
            self.assertEqual(status, 401)

    def test_a_signed_in_user_queues_a_job_only_they_can_read(self):
        status, body, queued = self.post(self.create, "jobs", {"environment": ENV, "name": "Desk", "egg": "RUdH"},
                                         token())
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(queued[0]), {"job": body["job"]})
        self.assertEqual(self.read(body["job"], token())[1]["state"], "queued")
        self.assertEqual(self.read(body["job"], token(oid="user-2"))[0], 404)
        self.accept = False
        self.assertEqual(self.read(body["job"], token(oid="user-3"))[0], 401)
        self.assertEqual(self.read(body["job"], "not-a-token")[0], 401)

    def test_translations_are_refused_without_the_tenant_allow_list(self):
        status, body, queued = self.post(self.create, "jobs", {"environment": ENV, "name": "Desk", "egg": "RUdH",
                                                               "translations": [{"agent": "A"}]}, token())
        self.assertEqual((status, queued), (400, []))
        self.assertIn("BFS_ALLOWED_TENANTS", body["error"])
        for env, on in (({"BFS_ALLOW_TRANSLATIONS": "true", "BFS_ALLOWED_TENANTS": ""}, False),
                        ({"BFS_ALLOW_TRANSLATIONS": "true", "BFS_ALLOWED_TENANTS": "tenant-1"}, True),
                        ({"BFS_ALLOW_TRANSLATIONS": "", "BFS_ALLOWED_TENANTS": "tenant-1"}, False)):
            with mock.patch.dict(os.environ, env):
                spec = importlib.util.spec_from_file_location("function_app_probe", FUNCTION_DIR / "function_app.py")
                probe = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(probe)
            self.assertEqual(probe.TRANSLATIONS, on, env)

    def call(self, handler, method, route, body=None, tok=None):
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        req = func.HttpRequest(method, f"/api/{route}", headers=headers,
                               body=b"" if body is None else json.dumps(body).encode())
        resp = handler(req)
        return resp.status_code, json.loads(resp.get_body())

    def test_sign_in_is_for_global_discovery_unless_an_environment_is_named(self):
        signin = self.fa.signin.build().get_user_function()
        asked = []
        fake = lambda url, fields: asked.append((url, fields)) or {k: "x" for k in (
            "device_code", "user_code", "verification_uri", "expires_in", "interval", "message")}
        with mock.patch.object(self.fa, "CLIENT_ID", "app"), mock.patch.object(self.fa, "_post_form", fake):
            self.assertEqual(self.call(signin, "POST", "signin", {})[0], 200)
            self.assertEqual(self.call(signin, "POST", "signin")[0], 200)
            self.assertEqual(self.call(signin, "POST", "signin", {"environment": ENV})[0], 200)
            status, body = self.call(signin, "POST", "signin", {"environment": "https://evil.example.com/"})
        scopes = [fields["scope"].split()[0] for _, fields in asked]
        self.assertEqual(scopes, [auth.DISCOVERY + "user_impersonation"] * 2 + [ENV + "user_impersonation"])
        self.assertIn("offline_access", asked[0][1]["scope"])
        self.assertEqual(status, 400)

    def test_the_environments_route_lists_the_users_environments(self):
        environments = self.fa.environments.build().get_user_function()
        self.fa.verify = auth.Verifier(opener=FakeCloud())
        status, body = self.call(environments, "GET", "environments", tok=token(aud=auth.DISCOVERY))
        self.assertEqual((status, len(body["environments"])), (200, 3))
        self.fa.verify = auth.Verifier(opener=FakeCloud(discovery_status=401))
        self.assertEqual(self.call(environments, "GET", "environments", tok=token(aud=auth.DISCOVERY))[0], 401)
        self.assertEqual(self.call(environments, "GET", "environments")[0], 401)

    def test_picking_an_environment_trades_the_refresh_token_and_checks_rights(self):
        pick = self.fa.signin_environment.build().get_user_function()
        cloud = FakeCloud(lacking={"prvCreatebot"})
        self.fa.verify = auth.Verifier(opener=cloud)
        with mock.patch.object(self.fa, "CLIENT_ID", "app"), \
                mock.patch.object(self.fa.auth.urllib.request, "urlopen", cloud):
            status, body = self.call(pick, "POST", "signin/environment", {"environment": ENV, "refreshToken": "rt-1"})
            self.assertEqual(status, 200, body)
            self.assertEqual((body["environment"], body["refresh_token"]), (ENV, "rt-2"))
            self.assertEqual(auth.claims(body["access_token"])["aud"], ENV.rstrip("/"))
            self.assertEqual((body["canMakeAgents"], body["missing"]), (False, ["agents"]))
            _, url, _, form = cloud.requests[0]
            self.assertTrue(url.endswith("/oauth2/v2.0/token"))
            self.assertIn("grant_type=refresh_token", form)
            self.assertIn("client_id=app", form)
            self.assertIn("scope=https%3A%2F%2Fexample.crm.dynamics.com%2Fuser_impersonation+offline_access", form)
            self.assertEqual(self.call(pick, "POST", "signin/environment", {"environment": ENV})[0], 401)
            self.assertEqual(self.call(pick, "POST", "signin/environment", {"environment": "https://x.example/",
                                                                            "refreshToken": "rt-1"})[0], 400)
        unreadable = FakeCloud(rights_status=403)
        self.fa.verify = auth.Verifier(opener=unreadable)
        with mock.patch.object(self.fa, "CLIENT_ID", "app"), \
                mock.patch.object(self.fa.auth.urllib.request, "urlopen", unreadable):
            status, body = self.call(pick, "POST", "signin/environment", {"environment": ENV, "refreshToken": "rt-1"})
        self.assertEqual((status, body["canMakeAgents"], body["missing"]), (200, None, []))
        refused = FakeCloud(refuse_refresh=True)
        with mock.patch.object(self.fa, "CLIENT_ID", "app"), \
                mock.patch.object(self.fa.auth.urllib.request, "urlopen", refused):
            status, body = self.call(pick, "POST", "signin/environment", {"environment": ENV, "refreshToken": "old"})
        self.assertEqual(status, 401)
        self.assertIn("sign in again", body["error"])

    def test_a_rapplication_job_is_queued_with_a_checked_powerapps_token(self):
        pa = token(aud="https://service.powerapps.com/")
        status, body, queued = self.post(self.create, "jobs", {"environment": ENV, "rapplication": "@kody-w/agent_team",
                                                               "refreshToken": "rt-1", "powerAppsToken": pa}, token())
        self.assertEqual(status, 202, body)
        request = self.store.get(f"{body['job']}/request.json")
        self.assertEqual((request["rapplication"], request["name"], request["powerapps_token"], request["refresh_token"]),
                         ("@kody-w/agent_team", "@kody-w/agent_team", pa, "rt-1"))
        self.assertNotIn("displayName", request)                 # the job names it after the rapplication itself
        for bad, code, why in (({"rapplication": "../../etc"}, 400, "RAPP Store reference"),
                               ({"rapplication": "agent_team", "store": "http://x"}, 400, "https"),
                               ({"rapplication": "agent_team", "powerAppsToken": token(aud=ENV)}, 401, "service.powerapps"),
                               ({"rapplication": "agent_team",
                                 "powerAppsToken": token(oid="user-2", aud="https://service.powerapps.com/")}, 401, "yours"),
                               ({"rapplication": "agent_team",
                                 "powerAppsToken": token(exp_in=60, aud="https://service.powerapps.com/")}, 401, "expired")):
            status, body, queued = self.post(self.create, "jobs", {"environment": ENV, **bad}, token())
            self.assertEqual((status, queued), (code, []), bad)
            self.assertIn(why, body["error"])

    def test_where_a_rapplication_reads_files_is_checked_and_queued(self):
        status, body, queued = self.post(self.create, "jobs", {
            "environment": ENV, "rapplication": "@rapp/json_doctor", "filesSite": "https://contoso.sharepoint.com/sites/team/",
            "filesFolder": "/Shared Documents/RAPP"}, token())
        self.assertEqual(status, 202, body)
        request = self.store.get(f"{body['job']}/request.json")
        self.assertEqual((request["files_site"], request["files_folder"]),
                         ("https://contoso.sharepoint.com/sites/team", "/Shared Documents/RAPP"))
        for bad in ({"filesSite": "https://evil.example.com/sites/x"}, {"filesSite": "http://contoso.sharepoint.com"},
                    {"filesFolder": "Shared Documents"}, {"filesFolder": "/Shared Documents/../x"}):
            status, body, queued = self.post(self.create, "jobs", {"environment": ENV, "rapplication": "json_doctor",
                                                                   **bad}, token())
            self.assertEqual((status, queued), (400, []), bad)

    def test_the_powerapps_sign_in_asks_for_the_power_apps_permission(self):
        signin = self.fa.signin.build().get_user_function()
        asked = []
        fake = lambda url, fields: asked.append(fields) or {k: "x" for k in (
            "device_code", "user_code", "verification_uri", "expires_in", "interval", "message")}
        with mock.patch.object(self.fa, "CLIENT_ID", "app"), mock.patch.object(self.fa, "_post_form", fake):
            self.assertEqual(self.call(signin, "POST", "signin", {"purpose": "powerapps"})[0], 200)
        self.assertEqual(asked[0]["scope"], "https://service.powerapps.com//User offline_access openid profile")

    def test_the_catalog_lists_what_can_be_deployed(self):
        listing = self.fa.rapplications.build().get_user_function()
        index = {"rapplications": [
            {"id": "a", "name": "A", "publisher": "@p", "singleton_url": "https://x/a.py", "ui_url": "https://x/a.html"},
            {"id": "gated", "publisher": "@p", "singleton_url": "https://x/g.py", "access": "private"},
            {"id": "whole", "publisher": "@p"}]}
        self.fa._catalog.update(at=0.0, entries=None)
        with mock.patch.object(self.fa.urllib.request, "urlopen", lambda url, timeout=None: Resp(json.dumps(index).encode())):
            status, body = self.call(listing, "GET", "rapplications")
        self.assertEqual(status, 200)
        self.assertEqual(body["rapplications"], [{"ref": "@p/a", "name": "A", "summary": None, "category": None,
                                                  "version": None, "hasUi": True}])

    def test_oversized_uploads_are_refused(self):
        with mock.patch.object(self.fa, "MAX_ZIP", 8):
            status, body, queued = self.post(self.create, "jobs", {
                "environment": ENV, "name": "Desk", "workspaceZip": base64.b64encode(b"x" * 64).decode()}, token())
        self.assertEqual((status, queued), (400, []))
        self.assertIn("larger than", body["error"])
        status, body, _ = self.post(self.create, "jobs", {"environment": ENV, "name": "Desk", "egg": "not base64!"},
                                    token())
        self.assertEqual((status, body["error"]), (400, "the egg must be base64"))
        with mock.patch.object(self.fa.urllib.request, "urlopen", side_effect=OSError("unreachable")):
            status, body, _ = self.post(self.create, "jobs", {"environment": ENV, "name": "Desk",
                                                              "eggUrl": "https://example.com/a.egg"}, token())
        self.assertEqual((status, body["error"]), (400, "could not fetch the egg: unreachable"))


if __name__ == "__main__":
    unittest.main()
