"""Publish a built code app (codeapp.package's dist/ and power.config.json) as the signed-in user, without pac or pa.

    from brainfreeze_studio.codeapp_publish import AUDIENCE, publish
    r = publish("out/codeapp", environment_id, get_token=lambda: user_token_for(AUDIENCE))
    r["playUrl"]

The calls go to the Power Apps resource provider (https://api.powerapps.com, token audience
https://service.powerapps.com/), the service the Power Apps maker portal itself uses:

  1. POST objectIds/{you}/generateResourceStorage   a blob container in Power Apps' own storage, with a SAS
  2. PUT  <container>/<file>                         each file of dist/, byte for byte (x-ms-blob-type: BlockBlob)
  3. POST apps                                       a new app (appType CodeApp, appSubtype BYOCApp), or, for one
     PUT  apps/{name}                                that exists, under an edit lease (acquireLease, releaseLease)
  4. POST apps/{name}/publish

Power Apps copies the uploaded files into the app's own storage as it saves the app. Running it again updates the
same app (found by the appId power.config.json records, else by display name), so its play URL stays the same.

The token is the user's own, delegated one: PowerApps Service's `User` permission, which any app registration can
ask for and a user can consent to; the Azure CLI's token for https://service.powerapps.com/ works as it is.
(`pac code push` and `pa app push` call the environment's Power Platform API host instead, whose code app scopes
Microsoft grants only to its own tools as of September 2026.)
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUDIENCE = "https://service.powerapps.com/"
POWERAPPS_SERVICE_APP_ID = "475226c6-020e-4fb2-8a90-7a972cbfc1d4"   # the audience of a v2 token for the same service
RP = "https://api.powerapps.com/providers/Microsoft.PowerApps"
STORAGE_VERSION = "2016-11-01"
CREATE_VERSION = "2017-06-01"     # dataSets on connection references need 2017-02-01 or later
EDIT_VERSION = "2017-06-01"        # PUT, leases and publish; publish refuses older versions, PUT needs a lease
MIME = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".json": "application/json",
        ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2", ".txt": "text/plain",
        ".map": "application/json", ".webp": "image/webp"}


class PublishError(RuntimeError):
    pass


def token_claims(token):
    """The claims of a JWT, unverified: only to learn whose token it is (the storage call names the user)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise PublishError("the token isn't a JWT access token") from None


def _detail(text):
    try:
        body = json.loads(text)
    except ValueError:
        return text[:300]
    inner = body.get("error") or {}
    if isinstance(inner, dict) and inner.get("message"):
        return f"{inner.get('code', '')}: {inner['message'][:400]}".lstrip(": ")
    return (body.get("message") or text)[:400]


class _Api:
    def __init__(self, get_token, opener=None):
        self.get_token, self.urlopen = get_token, opener or urllib.request.urlopen

    def call(self, method, path, body=None, retries=3, ok404=False):
        url = path if path.startswith("https://") else RP + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={"Authorization": "Bearer " + self.get_token(),
                                                  "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with self.urlopen(req, timeout=120) as r:
                    raw = r.read()
                    return json.loads(raw) if raw and raw.strip()[:1] in (b"{", b"[") else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", errors="replace")
                e.close()
                if e.code == 404 and ok404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 ** attempt * 2)
                    continue
                where = urllib.parse.urlsplit(url).path.split("/providers/Microsoft.PowerApps/")[-1]
                if e.code == 409 and "AppLease" in text:
                    raise PublishError(f"{where}: the app is open for editing in another session (Power Apps "
                                       f"Studio or an unfinished run); close it or wait a few minutes. {_detail(text)}")
                if e.code in (401, 403):
                    raise PublishError(f"{method} {where}: HTTP {e.code} {_detail(text)} (the token must be yours, "
                                       f"for {AUDIENCE}, and you must be able to make apps in the environment)")
                raise PublishError(f"{method} {where}: HTTP {e.code} {_detail(text)}")


def _upload(sas, dist, opener, log):
    base, _, query = sas.partition("?")
    files = sorted(f for f in Path(dist).rglob("*") if f.is_file())
    total = 0
    for f in files:
        rel = f.relative_to(dist).as_posix()
        data = f.read_bytes()
        total += len(data)
        req = urllib.request.Request(f"{base}/{urllib.parse.quote(rel)}?{query}", data=data, method="PUT",
                                     headers={"x-ms-blob-type": "BlockBlob",
                                              "Content-Type": MIME.get(f.suffix.lower(), "application/octet-stream")})
        try:
            with opener(req, timeout=120) as r:
                if r.status not in (200, 201):
                    raise PublishError(f"upload {rel}: HTTP {r.status}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            e.close()
            raise PublishError(f"upload {rel}: HTTP {e.code} {_detail(text)}") from None
    log(f"   uploaded {len(files)} files, {total:,} bytes")
    return f"{base}/index.html?{query}"


def connection_references(refs):
    """power.config.json's connectionReferences as the app record carries them (as `pa app push` 1.0 converts them):
    a flow's workflowDetails become the parameter hints canvas apps use for flows."""
    out = {}
    for key, r in (refs or {}).items():
        out[key] = {"id": r["id"], "displayName": r.get("displayName"), "dataSources": r.get("dataSources") or [],
                    "nestedActions": [], "actions": [], "parameterHints": {}, "parameterHintsV2": {}, "dependents": [],
                    "dependencies": [], "dataSets": r.get("dataSets") or {},
                    "sharedConnectionId": r.get("sharedConnectionId"), "authenticationType": r.get("authenticationType"),
                    "isOnPremiseConnection": r.get("isOnPremiseConnection"), "gatewayObjectIdHint": r.get("gatewayObjectIdHint")}
    for key, r in (refs or {}).items():
        wf = r.get("workflowDetails")
        if not wf:
            continue
        hints = {"workflowEntityId": {"value": wf["workflowEntityId"]},
                 "workflowDisplayName": {"value": wf["workflowDisplayName"]}, "workflowName": {"value": wf["workflowName"]}}
        v2, deps = json.loads(json.dumps(hints)), []
        for name, ref_key in (wf.get("dependencies") or {}).items():
            v2[name] = {"value": ref_key}
            deps.append(ref_key)
            if ref_key in out:
                out[ref_key]["dependents"].append(key)
        out[key].update(parameterHintsV2=v2, parameterHints=hints, dependencies=deps)
    return {k: {f: v for f, v in r.items() if v is not None or f in ("sharedConnectionId", "authenticationType")}
            for k, r in out.items()}


def database_references(refs):
    """power.config.json's databaseReferences as the app record carries them (as `pa app push` builds them)."""
    return {key: {"databaseDetails": {"environmentName": key,
                                      "overrideValues": {"environmentVariableName": (v or {}).get("environmentVariableName", "")}},
                  "dataSources": (v or {}).get("dataSources")}
            for key, v in (refs or {}).items()}


def app_record(config, environment_id, package_uri):
    """The app record, field for field as `pa app push` writes it from power.config.json."""
    return {
        "appType": "CodeApp", "appSubtype": "BYOCApp",
        "properties": {
            "lifeCycleId": "Draft", "displayName": config["appDisplayName"], "description": config.get("description") or "",
            "backgroundColor": "RGBA(255,255,255,1)", "backgroundImageUri": "",
            "createdByClientVersion": {"major": 1},
            "appUris": {"codeAppPackageUri": {"value": package_uri, "readonlyValue": package_uri}},
            "environment": {"name": environment_id, "id": f"/providers/Microsoft.PowerApps/environments/{environment_id}"},
            "connectionReferences": connection_references(config.get("connectionReferences")),
            "databaseReferences": database_references(config.get("databaseReferences")),
        },
    }


def list_code_apps(api, environment_id):
    f = urllib.parse.quote(f"environment eq '{environment_id}'")
    apps = (api.call("GET", f"/apps?api-version={STORAGE_VERSION}&$filter={f}") or {}).get("value") or []
    return [a for a in apps if a.get("appType") == "CodeApp"]


def find_app(api, environment_id, config, owner=None):
    """The app to update: the one power.config.json names, else the caller's code app of the same display name."""
    if config.get("appId"):
        app = api.call("GET", f"/apps/{config['appId']}?api-version={EDIT_VERSION}", ok404=True)
        if app and ((app.get("properties") or {}).get("environment") or {}).get("name") == environment_id:
            return app
    same = [a for a in list_code_apps(api, environment_id)
            if (a.get("properties") or {}).get("displayName") == config["appDisplayName"]]
    mine = [a for a in same if ((a.get("properties") or {}).get("owner") or {}).get("id") == owner]
    return (mine or same or [None])[0]


def publish(codeapp_dir, environment_id, get_token, *, opener=None, log=print):
    """Upload dist/, create or update the app, publish it. Returns {"appId", "playUrl", "operation", "packageUri"}."""
    codeapp_dir = Path(codeapp_dir)
    config = json.loads((codeapp_dir / "power.config.json").read_text())
    dist = codeapp_dir / "dist"
    if not (dist / "index.html").is_file():
        raise PublishError(f"{dist} has no index.html; build the code app first")
    opener = opener or urllib.request.urlopen
    api = _Api(get_token, opener)
    claims = token_claims(get_token())
    aud = str(claims.get("aud", "")).rstrip("/")
    if aud not in (AUDIENCE.rstrip("/"), POWERAPPS_SERVICE_APP_ID):
        raise PublishError(f"the token is for {aud or 'an unknown audience'}; code apps publish with a token for {AUDIENCE}")
    oid = claims.get("oid")
    if not oid:
        raise PublishError("the token has no oid claim; sign in as a user")
    environment = {"name": environment_id, "id": f"/providers/Microsoft.PowerApps/environments/{environment_id}"}
    existing = find_app(api, environment_id, config, owner=oid)
    sas = (api.call("POST", f"/objectIds/{oid}/generateResourceStorage?api-version={STORAGE_VERSION}",
                    {"environment": environment}) or {}).get("sharedAccessSignature")
    if not sas:
        raise PublishError("generateResourceStorage returned no storage URL")
    package_uri = _upload(sas, dist, opener, log)
    record = app_record(config, environment_id, package_uri)
    if existing:
        name = existing["name"]
        lease = api.call("POST", f"/apps/{name}/acquireLease?api-version={EDIT_VERSION}", {})
        try:
            app = api.call("PUT", f"/apps/{name}?api-version={EDIT_VERSION}", record)
        finally:
            if (lease or {}).get("leaseId"):
                api.call("POST", f"/apps/{name}/releaseLease?api-version={EDIT_VERSION}", {"leaseId": lease["leaseId"]})
        operation = "updated"
    else:
        app = api.call("POST", f"/apps?api-version={CREATE_VERSION}", record)
        name = app["name"]
        operation = "created"
    api.call("POST", f"/apps/{name}/publish?api-version={EDIT_VERSION}")
    app = api.call("GET", f"/apps/{name}?api-version={EDIT_VERSION}") or app
    props = (app or {}).get("properties") or {}
    play = props.get("appPlayUri") or f"https://apps.powerapps.com/play/e/{environment_id}/app/{name}"
    log(f"   {operation} code app {config['appDisplayName']} ({name}), published")
    config["appId"] = name
    (codeapp_dir / "power.config.json").write_text(json.dumps(config, indent=2) + "\n")
    return {"appId": name, "playUrl": play, "operation": operation,
            "packageUri": ((props.get("appUris") or {}).get("codeAppPackageUri") or {}).get("value")}


def delete_app(app_id, get_token, opener=None):
    """Remove a code app (used by tests and to undo a prototype)."""
    return _Api(get_token, opener).call("DELETE", f"/apps/{app_id}?api-version={EDIT_VERSION}", ok404=True)
