"""brainfreeze-studio in an Azure Function: a person signs in with their own account and deploys an egg into a
Copilot Studio environment they can make agents in.

The Function holds no service account and stores no tokens. Sign-in is the user's own, through the device-code
flow of a public client app registration (BFS_CLIENT_ID), which asks for delegated Dataverse access only. The
page keeps the token in memory and sends it with the deploy. The deploy then runs with exactly that user's rights:
brainfreeze_studio.build lays out the workspace, and brainfreeze_studio.deploy writes it into the environment
through the Dataverse Web API, with no pac and no az.

    GET  /api/page           the page: sign in, pick an egg, deploy
    POST /api/signin         {environment}           -> device code for that environment
    POST /api/signin/poll    {device_code}           -> {status: pending} | {status: ok, access_token, ...}
    POST /api/deploy         Bearer <user token>, {environment, name, egg | eggUrl, ...} -> deploy summary + log
"""
import base64
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
from pathlib import Path

import azure.functions as func

import brainfreeze_studio as bs
from brainfreeze_studio.deploy import DeployError, deploy

HERE = Path(__file__).resolve().parent
SDK_DIR = HERE / "sdk"            # copilot-harness-sdk's tutorial/ folder, copied in by publish.sh
CLIENT_ID = os.environ.get("BFS_CLIENT_ID", "")
TENANT = os.environ.get("BFS_TENANT", "organizations")
ALLOW_TRANSLATIONS = os.environ.get("BFS_ALLOW_TRANSLATIONS", "false").lower() == "true"
MAX_EGG = 16 * 1024 * 1024
ENV_URL = re.compile(r"^https://[a-z0-9-]+\.crm[0-9]*\.dynamics\.com/?$")

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _json(obj, status=200):
    return func.HttpResponse(json.dumps(obj), status_code=status, mimetype="application/json")


def _environment(value):
    env = (value or "").strip()
    if not ENV_URL.match(env):
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


def _claims(token):
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None


def _user_token(req, environment):
    """The caller's own delegated Dataverse token for this environment. Dataverse validates it; this check only makes
    sure it is a signed-in user's token for the environment being deployed to, never an app-only one."""
    header = req.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    claims = _claims(token) if token else None
    if not claims:
        raise PermissionError("sign in first: send your Dataverse token as Authorization: Bearer <token>")
    if str(claims.get("aud", "")).rstrip("/") != environment.rstrip("/"):
        raise PermissionError(f"the token is for {claims.get('aud')}, not {environment}")
    if "user_impersonation" not in str(claims.get("scp", "")).split():
        raise PermissionError("only a signed-in user's delegated token is accepted, not an app-only token")
    if claims.get("exp", 0) < time.time() + 60:
        raise PermissionError("the token has expired; sign in again")
    return token, claims.get("upn") or claims.get("unique_name") or claims.get("oid")


@app.route(route="signin", methods=["POST"])
def signin(req: func.HttpRequest) -> func.HttpResponse:
    if not CLIENT_ID:
        return _json({"error": "BFS_CLIENT_ID is not set"}, 500)
    try:
        environment = _environment((req.get_json() or {}).get("environment"))
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    r = _post_form(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/devicecode",
                   {"client_id": CLIENT_ID, "scope": f"{environment}user_impersonation offline_access openid profile"})
    if "device_code" not in r:
        return _json({"error": r.get("error_description") or r.get("error") or "could not start sign-in"}, 502)
    return _json({k: r[k] for k in ("device_code", "user_code", "verification_uri", "expires_in", "interval", "message")})


@app.route(route="signin/poll", methods=["POST"])
def signin_poll(req: func.HttpRequest) -> func.HttpResponse:
    device_code = (req.get_json() or {}).get("device_code")
    if not device_code:
        return _json({"error": "device_code is required"}, 400)
    r = _post_form(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                   {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": CLIENT_ID,
                    "device_code": device_code})
    if r.get("access_token"):
        claims = _claims(r["access_token"]) or {}
        return _json({"status": "ok", "access_token": r["access_token"], "expires_in": r.get("expires_in"),
                      "account": claims.get("upn") or claims.get("unique_name"), "environment": claims.get("aud")})
    if r.get("error") in ("authorization_pending", "slow_down"):
        return _json({"status": "pending"})
    return _json({"status": "error", "error": r.get("error_description") or r.get("error")}, 400)


@app.route(route="deploy", methods=["POST"])
def deploy_egg(req: func.HttpRequest) -> func.HttpResponse:
    log = []
    work = None
    try:
        body = req.get_json() or {}
        environment = _environment(body.get("environment"))
        token, account = _user_token(req, environment)
        name = (body.get("name") or "").strip()
        prefix = (body.get("publisherPrefix") or "rapp").strip()
        if not name:
            raise ValueError("name is required")
        if body.get("egg"):
            egg = base64.b64decode(body["egg"])
        elif str(body.get("eggUrl", "")).startswith("https://"):
            with urllib.request.urlopen(body["eggUrl"], timeout=60) as r:
                egg = r.read(MAX_EGG + 1)
        else:
            raise ValueError("send the egg as base64 (egg) or an https eggUrl")
        if len(egg) > MAX_EGG:
            raise ValueError("the egg is larger than 16 MB")
        translations = body.get("translations") or []
        if translations and not ALLOW_TRANSLATIONS:
            raise ValueError("translations run the egg's code for their parity proof; this deployment has them off "
                             "(BFS_ALLOW_TRANSLATIONS)")
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


@app.route(route="page", methods=["GET"])
def page(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse((HERE / "index.html").read_text(encoding="utf-8"), mimetype="text/html")
