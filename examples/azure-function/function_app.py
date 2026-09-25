"""brainfreeze-studio in an Azure Function: a person signs in with their own account, picks one of their
environments, and deploys an egg into it.

The Function has no service account, and holds a user's sign-in only while their deploy runs (jobs.py). Sign-in is
the user's own, through the device-code flow of a public client app registration (BFS_CLIENT_ID), which asks for
delegated Dataverse access only. The sign-in is for the Global Discovery Service, which lists the environments the
user belongs to; picking one trades the sign-in's refresh token for a token to that environment and checks that the
user's roles there let them make agents. The page keeps the tokens in memory and sends them with the deploy.
Dataverse checks the token (WhoAmI) before the Function does any work, and BFS_ALLOWED_TENANTS limits which
organizations it serves. The deploy then runs with exactly that user's rights: brainfreeze_studio.build lays out the
workspace, and brainfreeze_studio.deploy writes it into the environment through the Dataverse Web API, with no pac
and no az.

    GET  /api/page                the page: sign in, pick an environment and an egg, deploy
    POST /api/signin              {environment?}    -> device code (for Global Discovery, or that environment)
    POST /api/signin/poll         {device_code}     -> {status: pending} | {status: ok, access_token, refresh_token}
    GET  /api/environments        Bearer <discovery token> -> the user's environments
    POST /api/signin/environment  {environment, refreshToken} -> a token for that environment + the user's rights
    POST /api/deploy              Bearer <user token>, {environment, name, egg | eggUrl, ...} -> summary + log
    POST /api/jobs                the same, run in the background (any size): -> 202 {job}
                                  or {environment, rapplication: "@publisher/id", refreshToken}: a RAPP
                                  Store rapplication, as a Copilot Studio agent plus a Power Apps code app
    GET  /api/jobs/{job}          Bearer <user token> -> the job's state, log and result (its owner only)
    GET  /api/rapplications       the RAPP Store catalog: what /api/jobs can deploy

Small agents fit in one HTTP request (230 seconds). Anything bigger, such as a library of tens of flows, goes
through /api/jobs: a queue trigger runs it for up to an hour (see jobs.py).

A rapplication's code app is published with the user's own token for the Power Apps service, which the job gets with
their refresh token. That needs the user's consent to PowerApps Service's `User` permission for this app
registration; until they give it, the job deploys the agent and its flows and says the app is waiting, and
POST /api/signin {purpose: "powerapps"} starts the sign-in that asks for it.
"""
import base64
import json
import os
import re
import time
import shutil
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import azure.functions as func

import auth
import brainfreeze_studio as bs
import jobs
from brainfreeze_studio.deploy import DeployError, deploy

HERE = Path(__file__).resolve().parent
SDK_DIR = HERE / "sdk"            # copilot-harness-sdk's tutorial/ folder, copied in by publish.sh
HOST_JS = HERE / "codeapp-host.js"  # the code app host, built by publish.sh (npm brings Microsoft's SDK in)
RAPP_REF = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9-]*/)?[a-z][a-z0-9_-]*$")
CATALOG_TTL = 600
CLIENT_ID = os.environ.get("BFS_CLIENT_ID", "")
TENANT = os.environ.get("BFS_TENANT", "organizations")
ALLOWED_TENANTS = [t for t in os.environ.get("BFS_ALLOWED_TENANTS", "").replace(";", ",").split(",") if t.strip()]
# translations run the egg's code inside this Function, beside other users' in-flight sign-ins: only for the
# organizations it is set up to serve
TRANSLATIONS = os.environ.get("BFS_ALLOW_TRANSLATIONS", "false").lower() == "true" and bool(ALLOWED_TENANTS)
TRANSLATIONS_OFF = ("translations run the egg's code inside this Function for their parity proof, so they are on "
                    "only when BFS_ALLOW_TRANSLATIONS=true and BFS_ALLOWED_TENANTS lists the organizations it serves")
MAX_EGG = 16 * 1024 * 1024
MAX_ZIP = 32 * 1024 * 1024

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
verify = auth.Verifier(ALLOWED_TENANTS)
_store = None
_catalog = {"at": 0.0, "entries": None}


def store():
    global _store
    if _store is None:
        _store = jobs.BlobStore(os.environ["AzureWebJobsStorage__accountName"])
    return _store


def _json(obj, status=200):
    return func.HttpResponse(json.dumps(obj), status_code=status, mimetype="application/json")


def _environment(value):
    env = (value or "").strip()
    if not auth.ENV_URL.match(env):
        raise ValueError("environment must be a Dataverse URL such as https://yourorg.crm.dynamics.com/")
    return env.rstrip("/") + "/"


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def _user_token(req, environment):
    """(token, account, oid) for the caller: a signed-in user's delegated token for this environment, never an
    app-only one, that Dataverse accepted (auth.Verifier)."""
    token = auth.bearer(req)
    claims, _ = verify(token, environment)
    return token, claims.get("upn") or claims.get("unique_name") or claims.get("oid"), claims.get("oid")


def _fetch_egg(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            egg = r.read(MAX_EGG + 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ValueError(f"could not fetch the egg: {e}")
    if len(egg) > MAX_EGG:
        raise ValueError("the egg is larger than 16 MB")
    return egg


def _b64(value, limit, what):
    try:
        data = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except ValueError:
        data = b""
    if not data:
        raise ValueError(f"{what} must be base64")
    if len(data) > limit:
        raise ValueError(f"{what} is larger than {limit // (1024 * 1024)} MB")
    return data


def _body(req):
    try:
        body = req.get_json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@app.route(route="signin", methods=["POST"])
def signin(req: func.HttpRequest) -> func.HttpResponse:
    """Start the user's sign-in: for Global Discovery (to list their environments), or for one environment."""
    if not CLIENT_ID:
        return _json({"error": "BFS_CLIENT_ID is not set"}, 500)
    try:
        body = _body(req)
        wanted = body.get("environment")
        resource = _environment(wanted) if wanted else auth.DISCOVERY
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    # a second sign-in, once, lets this app publish Power Apps (code apps) for the user
    scope = (f"{auth.POWERAPPS_SCOPE} openid profile" if body.get("purpose") == "powerapps"
             else f"{resource}user_impersonation offline_access openid profile")
    r = _post_form(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/devicecode",
                   {"client_id": CLIENT_ID, "scope": scope})
    if "device_code" not in r:
        return _json({"error": r.get("error_description") or r.get("error") or "could not start sign-in"}, 502)
    return _json({k: r[k] for k in ("device_code", "user_code", "verification_uri", "expires_in", "interval", "message")})


@app.route(route="signin/poll", methods=["POST"])
def signin_poll(req: func.HttpRequest) -> func.HttpResponse:
    device_code = _body(req).get("device_code")
    if not device_code:
        return _json({"error": "device_code is required"}, 400)
    r = _post_form(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                   {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": CLIENT_ID,
                    "device_code": device_code})
    if r.get("access_token"):
        claims = auth.claims(r["access_token"]) or {}
        return _json({"status": "ok", "access_token": r["access_token"], "refresh_token": r.get("refresh_token"),
                      "expires_in": r.get("expires_in"), "account": claims.get("upn") or claims.get("unique_name"),
                      "environment": claims.get("aud")})
    if r.get("error") in ("authorization_pending", "slow_down"):
        return _json({"status": "pending"})
    return _json({"status": "error", "error": r.get("error_description") or r.get("error")}, 400)


@app.route(route="environments", methods=["GET"])
def environments(req: func.HttpRequest) -> func.HttpResponse:
    """The environments the signed-in user belongs to (Global Discovery, with their own token)."""
    try:
        return _json({"environments": verify.environments(auth.bearer(req))})
    except PermissionError as e:
        return _json({"error": str(e)}, 401)
    except RuntimeError as e:
        return _json({"error": str(e)}, 502)


@app.route(route="signin/environment", methods=["POST"])
def signin_environment(req: func.HttpRequest) -> func.HttpResponse:
    """Trade the sign-in's refresh token for the user's token to the environment they picked, have Dataverse accept
    it, and say what their roles there don't let them create. Nothing is stored."""
    if not CLIENT_ID:
        return _json({"error": "BFS_CLIENT_ID is not set"}, 500)
    try:
        body = _body(req)
        environment = _environment(body.get("environment"))
        if not body.get("refreshToken"):
            raise PermissionError("sign in first")
        user = auth.UserToken(None, environment, body["refreshToken"], CLIENT_ID, TENANT)
        token = user()
        claims, who = verify(token, environment)
        try:
            missing = verify.missing_rights(token, environment, who.get("UserId"))
        except RuntimeError:
            missing = None
        return _json({"status": "ok", "environment": environment, "access_token": token,
                      "refresh_token": user.refresh_token, "account": claims.get("upn") or claims.get("unique_name"),
                      "canMakeAgents": None if missing is None else not missing, "missing": missing or []})
    except PermissionError as e:
        return _json({"status": "error", "error": str(e)}, 401)
    except ValueError as e:
        return _json({"status": "error", "error": str(e)}, 400)
    except (urllib.error.URLError, OSError) as e:
        return _json({"status": "error", "error": f"could not reach the sign-in service: {e}"}, 502)


@app.route(route="deploy", methods=["POST"])
def deploy_egg(req: func.HttpRequest) -> func.HttpResponse:
    log = []
    work = None
    try:
        body = req.get_json() or {}
        environment = _environment(body.get("environment"))
        token, account, _ = _user_token(req, environment)
        name = (body.get("name") or "").strip()
        prefix = (body.get("publisherPrefix") or "rapp").strip()
        if not name:
            raise ValueError("name is required")
        if body.get("egg"):
            egg = _b64(body["egg"], MAX_EGG, "the egg")
        elif str(body.get("eggUrl", "")).startswith("https://"):
            egg = _fetch_egg(body["eggUrl"])
        else:
            raise ValueError("send the egg as base64 (egg) or an https eggUrl")
        translations = body.get("translations") or []
        if translations and not TRANSLATIONS:
            raise ValueError(TRANSLATIONS_OFF)
        work = Path(tempfile.mkdtemp(prefix="bfs-"))
        egg_path = work / "agent.egg"
        egg_path.write_bytes(egg)
        tdir = None
        if translations:
            tdir = work / "translations"
            tdir.mkdir()
            for i, spec in enumerate(translations):
                (tdir / f"{i:02d}.json").write_text(json.dumps(spec))
        log.append(f"signed in as {account}; deploying into {environment}")
        summary = bs.build(str(egg_path), str(work / "out"), name, prefix, schema_name=body.get("schemaName") or None,
                           sdk_dir=str(SDK_DIR), environment=environment, hn_api_name=body.get("hnApiName") or None,
                           translations=str(tdir) if tdir else None, model=body.get("model") or "Sonnet46")
        log.append("built the workspace: " + ", ".join(f"{k}={v}" for k, v in summary.items() if isinstance(v, (int, str)))[:400])
        result = deploy(work / "out" / "workspace", environment, lambda: token, log=log.append)
        return _json({"ok": True, "account": account, "result": result, "log": log})
    except PermissionError as e:
        return _json({"ok": False, "error": str(e), "log": log}, 401)
    except (ValueError, DeployError, bs.StudioBuildError) as e:
        return _json({"ok": False, "error": str(e), "log": log}, 400)
    except Exception as e:  # noqa: BLE001 - report the failure without echoing request data
        log.append(traceback.format_exc(limit=3))
        return _json({"ok": False, "error": f"{type(e).__name__}: {e}", "log": log}, 500)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


@app.route(route="jobs", methods=["POST"])
@app.queue_output(arg_name="queue", queue_name=jobs.QUEUE, connection="AzureWebJobsStorage")
def create_job(req: func.HttpRequest, queue: func.Out[str]) -> func.HttpResponse:
    try:
        body = req.get_json() or {}
        environment = _environment(body.get("environment"))
        token, account, owner = _user_token(req, environment)
        name = (body.get("name") or "").strip()
        ref = (body.get("rapplication") or "").strip()
        if ref:
            return _queue_rapplication(body, ref, name, environment, token, account, owner, queue)
        if not name:
            raise ValueError("name is required")
        request = {"environment": environment, "name": name}
        for key in ("schemaName", "publisherPrefix", "hnApiName", "model", "translations"):
            if body.get(key):
                request[key] = body[key]
        for key, limit, what in (("egg", MAX_EGG, "the egg"), ("translationsZip", MAX_ZIP, "translationsZip"),
                                 ("workspaceZip", MAX_ZIP, "workspaceZip")):
            if body.get(key):
                _b64(body[key], limit, what)
                request[key] = body[key]
        if not request.get("egg") and not request.get("workspaceZip"):
            if not str(body.get("eggUrl", "")).startswith("https://"):
                raise ValueError("send the egg as base64 (egg), an https eggUrl, or a built workspaceZip")
            request["egg"] = base64.b64encode(_fetch_egg(body["eggUrl"])).decode()
        if (request.get("translations") or request.get("translationsZip")) and not TRANSLATIONS:
            raise ValueError(TRANSLATIONS_OFF)
        job_id = jobs.new_job(store(), request, token, owner, account, refresh_token=body.get("refreshToken"))
        queue.set(json.dumps({"job": job_id}))
        return _json({"job": job_id, "state": "queued", "status": f"/api/jobs/{job_id}", "account": account}, 202)
    except PermissionError as e:
        return _json({"error": str(e)}, 401)
    except ValueError as e:
        return _json({"error": str(e)}, 400)


def _queue_rapplication(body, ref, name, environment, token, account, owner, queue):
    """A RAPP Store rapplication: its agent, the flows its app calls and its code app, deployed by a job."""
    if not RAPP_REF.match(ref):
        raise ValueError("rapplication must be a RAPP Store reference: @publisher/id or id")
    request = {"environment": environment, "name": name or ref, "rapplication": ref}
    if name:
        request["displayName"] = name
    store_url = body.get("store")
    if store_url:
        if not str(store_url).startswith("https://"):
            raise ValueError("store must be an https catalog root")
        request["store"] = store_url
    for key in ("schemaName", "publisherPrefix", "translations"):
        if body.get(key):
            request[key] = body[key]
    if request.get("translations") and not TRANSLATIONS:
        raise ValueError(TRANSLATIONS_OFF)
    if body.get("powerAppsToken"):
        # a token for publishing the app, from the same person as the Dataverse token
        claims = auth.claims(body["powerAppsToken"]) or {}
        mine = auth.claims(token) or {}
        if str(claims.get("aud", "")).rstrip("/") not in auth.POWERAPPS_AUDIENCES:
            raise PermissionError("powerAppsToken must be a token for https://service.powerapps.com/")
        if not claims.get("oid") or claims.get("oid") != mine.get("oid") or claims.get("tid") != mine.get("tid"):
            raise PermissionError("powerAppsToken must be yours: the same account as the Dataverse token")
        if (claims.get("exp") or 0) < time.time() + 300:
            raise PermissionError("powerAppsToken has expired; sign in again")
        request["powerapps_token"] = body["powerAppsToken"]
    job_id = jobs.new_job(store(), request, token, owner, account, refresh_token=body.get("refreshToken"))
    queue.set(json.dumps({"job": job_id}))
    return _json({"job": job_id, "state": "queued", "status": f"/api/jobs/{job_id}", "account": account}, 202)


@app.route(route="rapplications", methods=["GET"])
def rapplications(req: func.HttpRequest) -> func.HttpResponse:
    """The RAPP Store's rapplications a job can deploy (the catalog is public; cached for ten minutes)."""
    if _catalog["entries"] is None or time.time() - _catalog["at"] > CATALOG_TTL:
        from brainfreeze_studio import rapplication as rp
        try:
            with urllib.request.urlopen(rp.STORE.rstrip("/") + "/index.json", timeout=30) as r:
                index = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            return _json({"error": f"could not read the RAPP Store catalog: {e}"}, 502)
        _catalog["entries"] = [
            {"ref": f"{e.get('publisher')}/{e['id']}", "name": e.get("name"), "summary": e.get("summary"),
             "category": e.get("category"), "version": e.get("version"), "hasUi": bool(e.get("ui_url"))}
            for e in index.get("rapplications", [])
            if e.get("id") and e.get("singleton_url") and e.get("access") != "private"]
        _catalog["at"] = time.time()
    return _json({"rapplications": _catalog["entries"]})


@app.route(route="jobs/{job_id}", methods=["GET"])
def job_status(req: func.HttpRequest) -> func.HttpResponse:
    token = auth.bearer(req)
    try:
        claims, _ = verify(token, _environment(str((auth.claims(token) or {}).get("aud") or "")))
    except PermissionError as e:
        return _json({"error": str(e)}, 401)
    except ValueError:
        return _json({"error": "sign in first: send your Dataverse token as Authorization: Bearer <token>"}, 401)
    status = jobs.read_status(store(), req.route_params.get("job_id"), claims.get("oid"))
    return _json(status) if status else _json({"error": "no such job for this account"}, 404)


@app.queue_trigger(arg_name="msg", queue_name=jobs.QUEUE, connection="AzureWebJobsStorage")
def run_deploy_job(msg: func.QueueMessage) -> None:
    job_id = json.loads(msg.get_body().decode("utf-8"))["job"]
    jobs.run_job(job_id, store(), bs.build, deploy, sdk_dir=SDK_DIR, client_id=CLIENT_ID, tenant=TENANT,
                 allow_translations=TRANSLATIONS, host_js=HOST_JS if HOST_JS.is_file() else None)


@app.route(route="page", methods=["GET"])
def page(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse((HERE / "index.html").read_text(encoding="utf-8"), mimetype="text/html")
