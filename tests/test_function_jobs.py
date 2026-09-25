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
