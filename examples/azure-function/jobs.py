"""Background deploys for the Azure Function, for agents too big for one HTTP request (230 seconds).

POST /api/jobs stores the job and queues its id. A queue trigger then runs build + deploy for up to an hour,
writing progress as it goes, and GET /api/jobs/{id} reads the progress back.

The user's sign-in rides with the job only while it runs. It sits in the job's request blob, in storage that only
the Function's managed identity can reach (private endpoint, no shared keys). The blob is deleted when the job ends,
and a storage lifecycle rule removes anything a crashed job left behind. With a refresh token, a job can outlast the
access token (about an hour); without one, it must finish before that token expires. Deploys are idempotent, so
running a stopped job again picks up where it stopped.

Standard library only: storage is reached over its REST API with a managed-identity token.
"""
import base64
import calendar
import io
import json
import os
import re
import shutil
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from auth import claims

CONTAINER = "bfs-deploy-jobs"
QUEUE = "bfs-deploy-jobs"
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_LOG = 500
TIME_LIMIT = 60 * 60              # host.json functionTimeout: the host stops a job that runs longer
MAX_UNZIPPED = 256 * 1024 * 1024
MAX_FILES = 20000


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def managed_identity_token(resource):
    """(token, expires_on) for this Function's system-assigned identity."""
    endpoint, header = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    if not endpoint or not header:
        raise RuntimeError("no managed identity: IDENTITY_ENDPOINT/IDENTITY_HEADER are not set")
    url = f"{endpoint}?resource={urllib.parse.quote(resource, safe='')}&api-version=2019-08-01"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"X-IDENTITY-HEADER": header}), timeout=30) as r:
        body = json.loads(r.read().decode())
    return body["access_token"], int(body.get("expires_on") or time.time() + 3000)


class BlobStore:
    """JSON documents in one blob container, over the Blob REST API with a managed-identity bearer token."""

    def __init__(self, account, container=CONTAINER, token_source=managed_identity_token, opener=None):
        self.base = f"https://{account}.blob.core.windows.net/{container}"
        self.token_source = token_source
        self.urlopen = opener or urllib.request.urlopen
        self._token, self._expires, self._ready = None, 0, False

    def _request(self, method, name="", data=None, headers=None, query=""):
        if time.time() > self._expires - 300:
            self._token, self._expires = self.token_source("https://storage.azure.com/")
        url = self.base + (f"/{urllib.parse.quote(name)}" if name else "") + query
        hdrs = {"x-ms-version": "2021-12-02", "Authorization": "Bearer " + self._token, **(headers or {})}
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _ensure(self):
        if not self._ready:
            status, body = self._request("PUT", query="?restype=container")
            if status not in (201, 409):
                raise RuntimeError(f"could not create the job container: HTTP {status} {body[:200]!r}")
            self._ready = True

    def put(self, name, doc):
        self._ensure()
        status, body = self._request("PUT", name, json.dumps(doc).encode("utf-8"),
                                     {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json"})
        if status != 201:
            raise RuntimeError(f"could not write {name}: HTTP {status} {body[:200]!r}")

    def get(self, name):
        status, body = self._request("GET", name)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"could not read {name}: HTTP {status} {body[:200]!r}")
        return json.loads(body.decode("utf-8"))

    def delete(self, name):
        self._request("DELETE", name)


class UserToken:
    """The user's delegated Dataverse token, refreshed through the public client when it nears expiry (if the job
    carries a refresh token). Called before every Dataverse request."""

    def __init__(self, access_token, environment, refresh_token=None, client_id=None, tenant="organizations",
                 opener=None, now=time.time):
        self.access_token, self.refresh_token = access_token, refresh_token
        self.environment, self.client_id, self.tenant = environment, client_id, tenant
        self.urlopen = opener or urllib.request.urlopen
        self.now = now
        self.refreshed = 0

    def expires(self):
        return (claims(self.access_token) or {}).get("exp", 0)

    def __call__(self):
        if self.expires() - self.now() > 300:
            return self.access_token
        if not (self.refresh_token and self.client_id):
            if self.expires() > self.now():
                return self.access_token
            raise PermissionError("your sign-in expired while the job ran; sign in again and rerun it (a deploy "
                                  "picks up where it stopped)")
        data = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": self.client_id,
                                       "refresh_token": self.refresh_token,
                                       "scope": f"{self.environment}user_impersonation offline_access"}).encode()
        req = urllib.request.Request(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", data=data,
                                     method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise PermissionError(f"could not refresh your sign-in: {e.read().decode()[:300]}")
        self.access_token = body["access_token"]
        self.refresh_token = body.get("refresh_token") or self.refresh_token
        self.refreshed += 1
        return self.access_token


def new_job(store, request, access_token, owner, account, refresh_token=None):
    """Store a job's request (with the user's sign-in) and its first status; returns the job id to queue."""
    job_id = uuid.uuid4().hex
    store.put(f"{job_id}/request.json", {**request, "access_token": access_token, "refresh_token": refresh_token})
    store.put(f"{job_id}/status.json", {"job": job_id, "state": "queued", "owner": owner, "account": account,
                                        "environment": request["environment"], "name": request["name"],
                                        "created": utc(), "log": []})
    return job_id


def _age(stamp, now):
    try:
        return now - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def read_status(store, job_id, owner, now=None):
    """The job's status for its owner only (None when there is no such job or it isn't theirs). A job still open
    past the Function's time limit was stopped by its host (a timeout or a restart) and is reported as failed."""
    if not JOB_ID.match(job_id or ""):
        return None
    status = store.get(f"{job_id}/status.json")
    if not status or not owner or status.get("owner") != owner:
        return None
    status = {k: v for k, v in status.items() if k != "owner"}
    now = time.time() if now is None else now
    if status.get("state") in ("queued", "running", "deploying") and \
            _age(status.get("started") or status.get("created"), now) > TIME_LIMIT + 300:
        status.update(state="failed", error="the job stopped without finishing (its host timed out or restarted); "
                                            "queue it again: a deploy picks up where it stopped")
    return status


def _extract(blob_b64, dest):
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(blob_b64))) as z:
        members = z.infolist()
        for member in members:
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe path in zip: {member.filename}")
        if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_UNZIPPED:
            raise ValueError(f"the zip unpacks to more than {MAX_FILES} files or {MAX_UNZIPPED // 2**20} MB")
        z.extractall(dest)


def run_job(job_id, store, build, deploy, *, sdk_dir, client_id=None, tenant="organizations",
            allow_translations=False, flush_every=3.0):
    """Run one queued job end to end. Never raises: the outcome is written to the job's status."""
    status = store.get(f"{job_id}/status.json") or {"job": job_id, "log": []}
    log = status.setdefault("log", [])
    last = [0.0]

    def save(state=None, force=False, **fields):
        if state:
            status["state"] = state
        status.update(fields)
        status["log"] = log[-MAX_LOG:]
        status["updated"] = utc()
        if force or time.time() - last[0] >= flush_every:
            store.put(f"{job_id}/status.json", status)
            last[0] = time.time()

    def say(line):
        log.append(line)
        save()

    work = None
    try:
        request = store.get(f"{job_id}/request.json")
        if request is None:
            raise RuntimeError("the job's request is gone; queue the job again")
        started = time.time()
        save("running", force=True, started=utc())
        environment = request["environment"]
        token = UserToken(request["access_token"], environment, request.get("refresh_token"), client_id, tenant)
        token()                                  # fails fast when the sign-in has already expired
        work = Path(tempfile.mkdtemp(prefix="bfs-job-"))
        if request.get("workspaceZip"):
            _extract(request["workspaceZip"], work / "out")
            workspace = work / "out" / "workspace"
            if not workspace.is_dir():
                raise ValueError("workspaceZip must hold a workspace/ folder (and, optionally, provenance.json)")
            say("using the uploaded workspace (no build)")
        else:
            egg = work / "agent.egg"
            egg.write_bytes(base64.b64decode(request["egg"]))
            tdir = None
            if request.get("translations") or request.get("translationsZip"):
                if not allow_translations:
                    raise ValueError("translations run the egg's code for their parity proof; this deployment has "
                                     "them off (BFS_ALLOW_TRANSLATIONS)")
                tdir = work / "translations"
                tdir.mkdir()
                for i, spec in enumerate(request.get("translations") or []):
                    (tdir / f"{i:03d}.json").write_text(json.dumps(spec))
                if request.get("translationsZip"):
                    _extract(request["translationsZip"], tdir)
            say(f"building {request['name']} from the egg" + (f" with {len(list(tdir.glob('*.json')))} translations"
                                                              if tdir else ""))
            summary = build(str(egg), str(work / "out"), request["name"], request.get("publisherPrefix") or "rapp",
                            schema_name=request.get("schemaName") or None, sdk_dir=str(sdk_dir),
                            environment=environment, hn_api_name=request.get("hnApiName") or None,
                            translations=str(tdir) if tdir else None, model=request.get("model") or "Sonnet46")
            prov = work / "out" / "provenance.json"
            parity = (json.loads(prov.read_text()).get("parity") or {}) if prov.is_file() else {}
            proven = sum(1 for p in parity.values() if p.get("parity"))
            say(f"built {summary.get('schema_name')} in {time.time() - started:.0f}s"
                + (f"; {proven}/{len(parity)} translations proven" if parity else ""))
            workspace = work / "out" / "workspace"
        save("deploying", force=True)
        result = deploy(workspace, environment, token, log=say)
        save("succeeded", force=True, result=result, finished=utc(), seconds=round(time.time() - started),
             tokenRefreshes=token.refreshed)
    except Exception as e:  # noqa: BLE001 - every failure becomes the job's outcome
        log.append(f"failed: {type(e).__name__}: {e}")
        log.extend(traceback.format_exc(limit=2).splitlines()[-3:])
        save("failed", force=True, error=f"{type(e).__name__}: {e}", finished=utc())
    finally:
        store.delete(f"{job_id}/request.json")    # the user's sign-in and the egg leave with it
        if work:
            shutil.rmtree(work, ignore_errors=True)
    return status
