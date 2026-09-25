"""Deploy a built harness workspace into Copilot Studio with nothing but a Dataverse token.

    from brainfreeze_studio.deploy import deploy
    result = deploy("out/workspace", "https://org.crm.dynamics.com/", get_token=lambda: user_token)

The token is the signed-in user's own, delegated one, so everything lands with that user's rights, in any
environment they can make agents in. There is no pac, no az and no Node. Each step is a Dataverse Web API call,
the same calls pac makes under the hood: its push writes bots and botcomponents rows, and its publish runs the
PvaPublish message. The order is the copilot-harness-sdk deploy's:

1. connection references
2. environment variables
3. agent flows, created and activated
4. the harness bot, with its instructions
5. tools and skills, linked to their flows and references
6. stale components removed
7. publish, then read back

It is idempotent: a re-run updates what changed and leaves the rest alone.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HARNESS_TEMPLATE = "cliagent-1.0.0"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
AUTH_MODES = {"None": 1, "Integrated": 2, "Generic": 3}
AUTH_TRIGGERS = {"AsNeeded": 0, "Always": 1}
ACCESS_POLICIES = {"Any": 0, "ChatbotReaders": 1, "GroupMembership": 2, "Copilot": 3}
ENV_VAR_TYPES = {"String": 100000000, "Number": 100000001, "Boolean": 100000002, "JSON": 100000003,
                 "DataSource": 100000004, "Secret": 100000005}


class DeployError(RuntimeError):
    pass


class Dataverse:
    """A small Web API client. get_token() is called for every request, so it may refresh."""

    def __init__(self, environment, get_token, timeout=180, retries=4, opener=None):
        self.environment = environment.rstrip("/") + "/"
        self.base = self.environment + "api/data/v9.2/"
        self.get_token = get_token
        self.timeout = timeout
        self.retries = retries
        self.urlopen = opener or urllib.request.urlopen

    def __call__(self, method, path, body=None, prefer=None, headers=None, ok404=False):
        url = self.base + urllib.parse.quote(path, safe="/?&=$(),'@.:-_*!~")
        data = None if body is None else json.dumps(body).encode("utf-8")
        hdrs = {"Accept": "application/json", "Content-Type": "application/json", "OData-MaxVersion": "4.0",
                "OData-Version": "4.0"}
        if prefer:
            hdrs["Prefer"] = prefer
        hdrs.update(headers or {})
        for attempt in range(self.retries + 1):
            hdrs["Authorization"] = "Bearer " + self.get_token()
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with self.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}, {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 404 and ok404:
                    return None, {}
                if e.code in (429, 502, 503, 504) or (e.code == 500 and "deadlock" in text.lower()):
                    if attempt < self.retries:
                        time.sleep(min(60, 5 * 2 ** attempt))
                        continue
                raise DeployError(f"{method} {path.split('?')[0]} failed: HTTP {e.code} {text[:600]}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                raise DeployError(f"{method} {path.split('?')[0]} failed: {e}")
        raise DeployError(f"{method} {path} failed")

    def value(self, path):
        body, _ = self(path=path, method="GET")
        return body.get("value", [])

    def ref(self, entity_set, id_):
        return {"@odata.id": f"{self.base}{entity_set}({id_})"}


def _q(text):
    return str(text).replace("'", "''")


def _created_id(body, headers):
    for value in (body or {}).values():
        if isinstance(value, str) and GUID.fullmatch(value):
            return value
    match = GUID.search(headers.get("odata-entityid", ""))
    return match.group(0) if match else None


# ── reading a workspace ──────────────────────────────────────────────────────

def _scalar(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def split_component(text):
    """(componentName, description, data) of a component file: data is the file without its mcs.metadata block,
    which is what a botcomponent row carries (the name and description live in their own columns)."""
    text = text.lstrip("\ufeff")
    match = re.match(r"mcs\.metadata:[ \t]*\r?\n((?:[ \t]+.*(?:\r?\n|$))*)", text)
    meta = {}
    if match:
        for line in match.group(1).splitlines():
            m = re.match(r"[ \t]+(componentName|description):[ \t]*(.*)$", line)
            if m and not re.match(r"[|>]", m.group(2)):
                meta[m.group(1)] = _scalar(m.group(2))
        text = text[match.end():]
    return meta.get("componentName"), meta.get("description"), text


def _value(text, key):
    m = re.search(rf"^\s*(?:-\s*)?{key}:\s*(.+)$", text, re.M)
    return _scalar(m.group(1)) if m else None


def read_settings(text):
    out = {"displayName": _value(text, "displayName"), "schemaName": _value(text, "schemaName"),
           "series": (re.search(r"series:\s*(\S+)", text) or [None, "Sonnet46"])[1],
           "authenticationMode": _value(text, "authenticationMode") or "Integrated",
           "authenticationTrigger": _value(text, "authenticationTrigger") or "Always",
           "accessControlPolicy": _value(text, "accessControlPolicy") or "GroupMembership",
           "greetingText": _value(text, "greetingText")}
    seg = re.search(r"-\s*kind:\s*StaticSegment\s*\n(\s*)value:\s*\|-?\s*\n", text)
    if seg:
        lines, indent = [], None
        for line in text[seg.end():].split("\n"):
            if line.strip():
                width = len(line) - len(line.lstrip())
                if indent is None:
                    indent = width
                if width < indent:
                    break
            lines.append(line[indent:] if indent is not None else line)
        out["instructions"] = "\n".join(lines).rstrip("\n")
    out["conversationStarters"] = [{"title": _scalar(t), "text": _scalar(x)} for t, x in
                                   re.findall(r"-\s*title:\s*(.+)\n\s*text:\s*(.+)", text)]
    return out


def read_workspace(workspace):
    """What a deploy needs from a harness workspace, as brainfreeze-studio and the SDK lay it out."""
    ws = Path(workspace)
    if not (ws / "settings.mcs.yml").is_file():
        raise DeployError(f"{ws} has no settings.mcs.yml")
    settings = read_settings((ws / "settings.mcs.yml").read_text(encoding="utf-8"))
    if not settings.get("instructions"):
        raise DeployError("settings.mcs.yml has no instructions")
    components = []
    for folder, kind in (("capabilities/tools", "tool"), ("behaviors", "skill"), ("capabilities/knowledge", "knowledge")):
        for f in sorted((ws / folder).glob("*.mcs.yml")) if (ws / folder).is_dir() else []:
            text = f.read_text(encoding="utf-8")
            name, description, data = split_component(text)
            ckind = _value(data, "kind")
            components.append({"file": f"{folder}/{f.name}", "name": f.name[:-len(".mcs.yml")], "group": kind,
                               "kind": ckind, "displayName": name or f.name[:-len(".mcs.yml")],
                               "description": description or "", "data": data,
                               "workflowId": _value(data, "workflowId"),
                               "connectionReference": _value(data, "connectionReference"),
                               "connectorId": _value(data, "connectorId")})
    refs = {}
    for c in components:
        if c["connectionReference"]:
            refs.setdefault(c["connectionReference"], c["connectorId"])
    sync = ws / "infrastructure" / "connections"
    for f in sorted(sync.glob("*.sync.yaml")) if sync.is_dir() else []:
        text = f.read_text(encoding="utf-8")
        for logical, connector in re.findall(r"connectionReferenceLogicalName:\s*(\S+)\s*\n\s*connectorId:\s*(\S+)", text):
            refs[logical] = refs.get(logical) or connector
    workflows = []
    for d in sorted((ws / "workflows").iterdir()) if (ws / "workflows").is_dir() else []:
        if not (d / "workflow.json").is_file():
            continue
        definition = json.loads((d / "workflow.json").read_text(encoding="utf-8").lstrip("\ufeff"))
        meta = (d / "metadata.yml").read_text(encoding="utf-8") if (d / "metadata.yml").is_file() else ""
        wid = _value(meta, "workflowId") or (GUID.findall(d.name) or [None])[-1]
        for api, v in ((definition.get("properties") or {}).get("connectionReferences") or {}).items():
            logical = ((v or {}).get("connection") or {}).get("connectionReferenceLogicalName")
            if logical:
                refs[logical] = refs.get(logical) or f"/providers/Microsoft.PowerApps/apis/{(v.get('api') or {}).get('name') or api}"
        workflows.append({"folder": d.name, "id": wid, "name": _value(meta, "name") or d.name,
                          "description": _value(meta, "description") or "", "definition": definition})
    env_vars = []
    prov = ws.parent / "provenance.json"
    if prov.is_file():
        env_vars = json.loads(prov.read_text(encoding="utf-8")).get("environment_variables") or []
    return {"settings": settings, "components": components, "connection_references": refs,
            "workflows": workflows, "environment_variables": env_vars}


def component_schema_name(schema_name, c):
    if c["group"] == "tool":
        return f"{schema_name}.tool.connected-agent.{c['name']}" if c["kind"] == "ConnectedAgentTool" \
            else f"{schema_name}.tool.{c['name']}"
    return f"{schema_name}.{c['group']}.{c['name']}"


def bot_configuration(settings, instructions=None):
    agent = {"$kind": "AgentSettings", "model": {"$kind": "ModelConfig", "series": settings.get("series") or "Sonnet46"},
             "instructions": {"$kind": "Instructions",
                              "segments": [{"$kind": "StaticSegment", "value": instructions or settings["instructions"]}]}}
    if settings.get("greetingText"):
        agent["greetingText"] = settings["greetingText"]
    if settings.get("conversationStarters"):
        agent["conversationStarters"] = [{"$kind": "ConversationStarter", **s} for s in settings["conversationStarters"]]
    return {"$kind": "BotConfiguration", "recognizer": {"$kind": "CLICopilotRecognizer"}, "agentSettings": agent,
            "authoringModel": "CliCopilot"}


# ── the steps ────────────────────────────────────────────────────────────────

def ensure_connection_reference(dv, logical, connector_id, display_name, connections=None):
    """An agent-scoped reference bound to a connection this user can use: an explicit map entry, else the reference
    itself if already bound, else any bound reference for the same connector in the environment."""
    existing = dv.value(f"connectionreferences?$filter=connectionreferencelogicalname eq '{_q(logical)}'"
                        "&$select=connectionreferenceid,connectionid,connectorid")
    connection = (connections or {}).get(logical) or (connections or {}).get(connector_id or "")
    if not connection and existing and existing[0].get("connectionid"):
        return {"logicalName": logical, "operation": "existing", "connectionId": existing[0]["connectionid"]}
    if not connection and connector_id:
        rows = dv.value(f"connectionreferences?$filter=connectorid eq '{_q(connector_id)}' and connectionid ne null"
                        "&$select=connectionid,connectionreferencelogicalname&$orderby=createdon asc&$top=1")
        connection = rows[0]["connectionid"] if rows else None
    if not connection:
        raise DeployError(f"no connection for {logical} ({connector_id}): create one in Power Apps (Connections) "
                          "as this user, then deploy again")
    body = {"connectionreferencedisplayname": display_name, "connectionreferencelogicalname": logical,
            "connectorid": connector_id, "connectionid": connection}
    if existing:
        dv("PATCH", f"connectionreferences({existing[0]['connectionreferenceid']})", body)
        return {"logicalName": logical, "operation": "updated", "connectionId": connection}
    dv("POST", "connectionreferences", body)
    return {"logicalName": logical, "operation": "created", "connectionId": connection}


def ensure_environment_variable(dv, var):
    rows = dv.value(f"environmentvariabledefinitions?$filter=schemaname eq '{_q(var['schemaName'])}'"
                    "&$select=environmentvariabledefinitionid")
    if rows:
        return {"schemaName": var["schemaName"], "operation": "existing"}
    dv("POST", "environmentvariabledefinitions", {
        "schemaname": var["schemaName"], "displayname": var.get("displayName") or var["schemaName"],
        "type": ENV_VAR_TYPES[var.get("type") or "String"], "defaultvalue": str(var.get("defaultValue", ""))})
    return {"schemaName": var["schemaName"], "operation": "created"}


def _stored_definition(clientdata):
    """A stored flow definition without the field Power Automate adds on save (each connection reference's
    api.logicalName), so an unchanged flow compares equal to the workspace's copy."""
    stored = json.loads(clientdata or "null")
    refs = ((stored or {}).get("properties") or {}).get("connectionReferences") or {}
    for ref in refs.values():
        if isinstance(ref, dict) and isinstance(ref.get("api"), dict):
            ref["api"].pop("logicalName", None)
    return stored


def ensure_workflow(dv, wf):
    """Create or update an agent flow and activate it; an activated, unchanged flow is left alone."""
    clientdata = json.dumps(wf["definition"], separators=(",", ":"), ensure_ascii=False)
    rows = dv.value(f"workflows?$filter=workflowid eq {wf['id']}&$select=workflowid,name,description,statecode,clientdata")
    body = {"name": wf["name"], "description": wf["description"], "clientdata": clientdata}
    if rows:
        cur = rows[0]
        try:            # compare definitions, not strings: Dataverse stores the JSON reformatted
            same = _stored_definition(cur.get("clientdata")) == wf["definition"]
        except ValueError:
            same = False
        if cur.get("statecode") == 1 and same and cur.get("name") == wf["name"] \
                and (cur.get("description") or "") == wf["description"]:
            return {"name": wf["name"], "operation": "unchanged"}
        if cur.get("statecode") == 1:
            dv("PATCH", f"workflows({wf['id']})", {"statecode": 0, "statuscode": 1})
        dv("PATCH", f"workflows({wf['id']})", body)
        operation = "updated"
    else:
        dv("POST", "workflows", {"workflowid": wf["id"], "category": 5, "type": 1, "mode": 0, "scope": 4,
                                 "primaryentity": "none", "modernflowtype": 0, **body})
        operation = "created"
    dv("PATCH", f"workflows({wf['id']})", {"statecode": 1, "statuscode": 2})
    return {"name": wf["name"], "operation": operation}


def ensure_bot(dv, schema_name, display_name, settings, language=1033):
    config = json.dumps(bot_configuration(settings))
    rows = dv.value(f"bots?$filter=schemaname eq '{_q(schema_name)}'&$select=botid,template,configuration,name")
    fields = {"name": display_name, "configuration": config,
              "authenticationmode": AUTH_MODES.get(settings["authenticationMode"], 2),
              "authenticationtrigger": AUTH_TRIGGERS.get(settings["authenticationTrigger"], 1),
              "accesscontrolpolicy": ACCESS_POLICIES.get(settings["accessControlPolicy"], 2)}
    if rows:
        bot = rows[0]
        if bot.get("template") and not bot["template"].startswith("cliagent-"):
            raise DeployError(f"{schema_name} exists but is a classic agent ({bot['template']}); refusing to change it")
        if bot.get("configuration") != config or bot.get("name") != display_name:
            dv("PATCH", f"bots({bot['botid']})", fields, headers={"If-Match": "*"})
            return bot["botid"], "updated"
        return bot["botid"], "unchanged"
    body, headers = dv("POST", "bots", {"schemaname": schema_name, "template": HARNESS_TEMPLATE, "language": language,
                                        "runtimeprovider": 0, **fields}, prefer="return=representation")
    bot_id = _created_id({"botid": (body or {}).get("botid")}, headers)
    if not bot_id:
        raise DeployError("Dataverse did not return the new bot's id")
    return bot_id, "created"


def list_components(dv, bot_id):
    rows = dv.value(f"botcomponents?$filter=_parentbotid_value eq {bot_id}&$select=botcomponentid,schemaname,name,"
                    "description,data&$expand=botcomponent_workflow($select=workflowid),"
                    "botcomponent_connectionreference($select=connectionreferenceid,connectionreferencelogicalname)")
    return {r["schemaname"].lower(): r for r in rows}


def ensure_component(dv, bot_id, schema_name, c, live):
    want = {"name": c["displayName"], "description": c["description"], "data": c["data"]}
    cur = live.get(schema_name.lower())
    if cur:
        if any((cur.get(k) or "") != (v or "") for k, v in want.items()):
            dv("PATCH", f"botcomponents({cur['botcomponentid']})", want)
            return cur["botcomponentid"], "updated"
        return cur["botcomponentid"], "unchanged"
    body, headers = dv("POST", "botcomponents", {"schemaname": schema_name, "componenttype": 9,
                                                 "parentbotid@odata.bind": f"/bots({bot_id})", **want},
                       prefer="return=representation")
    comp_id = _created_id({"botcomponentid": (body or {}).get("botcomponentid")}, headers)
    if not comp_id:
        raise DeployError(f"Dataverse did not return the id of {schema_name}")
    return comp_id, "created"


def link_component(dv, comp_id, c, live_row, workflow_ids, reference_ids):
    ops = []
    if c["kind"] == "WorkflowTool" and c["workflowId"]:
        have = {w["workflowid"].lower() for w in (live_row or {}).get("botcomponent_workflow", [])}
        for wid in have - {c["workflowId"].lower()}:
            dv("DELETE", f"botcomponents({comp_id})/botcomponent_workflow({wid})/$ref")
            ops.append(f"unlinked flow {wid}")
        if c["workflowId"].lower() not in have:
            if c["workflowId"].lower() not in workflow_ids:
                raise DeployError(f"{c['file']} points at flow {c['workflowId']}, which the workspace doesn't carry")
            dv("POST", f"botcomponents({comp_id})/botcomponent_workflow/$ref", dv.ref("workflows", c["workflowId"]))
            ops.append("linked flow")
    if c["kind"] == "ConnectorTool" and c["connectionReference"]:
        have = {r["connectionreferencelogicalname"] for r in (live_row or {}).get("botcomponent_connectionreference", [])}
        if c["connectionReference"] not in have:
            ref_id = reference_ids.get(c["connectionReference"])
            if not ref_id:
                raise DeployError(f"connection reference {c['connectionReference']} is missing")
            dv("POST", f"botcomponents({comp_id})/botcomponent_connectionreference/$ref",
               dv.ref("connectionreferences", ref_id))
            ops.append("linked reference")
    return ops


def publish(dv, bot_id, timeout=600):
    """Run PvaPublish, then wait until the bot's own record says the publish finished."""
    started = time.time()
    for attempt in range(6):
        try:
            dv("POST", f"bots({bot_id})/Microsoft.Dynamics.CRM.PvaPublish", {})
            break
        except DeployError as e:
            if attempt == 5:
                raise
            time.sleep(20)                # a freshly created bot can still be provisioning
    while time.time() - started < timeout:
        row, _ = dv("GET", f"bots({bot_id})?$select=publishedon,synchronizationstatus")
        status = json.loads(row.get("synchronizationstatus") or "{}").get("lastFinishedPublishOperation") or {}
        end = status.get("operationEnd") or ""
        if status.get("status") == "Succeeded" and end >= time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started - 5)):
            return {"publishedon": row.get("publishedon"), "status": "Succeeded"}
        if status.get("status") == "Failed" and end >= time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started - 5)):
            raise DeployError(f"publish failed: {json.dumps(status)[:600]}")
        time.sleep(5)
    raise DeployError("publish did not finish in time")


def environment_id(dv):
    try:
        body, _ = dv("GET", "RetrieveCurrentOrganization(AccessType=@p)?@p=Microsoft.Dynamics.CRM.EndpointAccessType'Default'")
        return (body.get("Detail") or {}).get("EnvironmentId")
    except DeployError:
        return None


def deploy(workspace, environment, get_token, *, schema_name=None, display_name=None, connections=None,
           keep_extra_components=False, do_publish=True, log=print, dataverse=None):
    """Deploy a harness workspace as the user whose token get_token() returns. Returns a summary dict."""
    ws = read_workspace(workspace)
    settings = ws["settings"]
    schema = schema_name or settings["schemaName"]
    name = display_name or settings["displayName"] or schema
    if not schema or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+", schema):
        raise DeployError(f"schema name {schema!r} must look like <prefix>_<Name>")
    if len(name) > 42:
        raise DeployError(f"display name is {len(name)} characters; longer than 42 never finishes provisioning")
    dv = dataverse or Dataverse(environment, get_token)
    result = {"schemaName": schema, "displayName": name, "environment": dv.environment}

    log("1/7 connection references")
    references = []
    for logical, connector in sorted(ws["connection_references"].items()):
        references.append(ensure_connection_reference(dv, logical, connector, f"{name} - {logical.split('.')[-1]}", connections))
        log(f"   {logical}: {references[-1]['operation']}")
    reference_ids = {}
    for r in references:
        rows = dv.value(f"connectionreferences?$filter=connectionreferencelogicalname eq '{_q(r['logicalName'])}'"
                        "&$select=connectionreferenceid")
        reference_ids[r["logicalName"]] = rows[0]["connectionreferenceid"] if rows else None

    log("2/7 environment variables")
    variables = [ensure_environment_variable(dv, v) for v in ws["environment_variables"]]
    for v in variables:
        log(f"   {v['schemaName']}: {v['operation']}")

    log("3/7 agent flows")
    flows = []
    for wf in ws["workflows"]:
        flows.append(ensure_workflow(dv, wf))
        log(f"   {wf['name']}: {flows[-1]['operation']}")
    workflow_ids = {wf["id"].lower() for wf in ws["workflows"]}

    log("4/7 harness bot")
    bot_id, bot_op = ensure_bot(dv, schema, name, settings)
    log(f"   {schema}: {bot_op} ({bot_id})")

    log("5/7 tools and skills")
    live = list_components(dv, bot_id)
    expected = []
    for c in ws["components"]:
        schema_name_c = component_schema_name(schema, c)
        expected.append(schema_name_c.lower())
        comp_id, op = ensure_component(dv, bot_id, schema_name_c, c, live)
        links = link_component(dv, comp_id, c, live.get(schema_name_c.lower()), workflow_ids, reference_ids)
        log(f"   {schema_name_c.split('.', 1)[1]}: {op}{' + ' + ', '.join(links) if links else ''}")

    log("6/7 stale components")
    removed = []
    if not keep_extra_components:
        for key, row in list_components(dv, bot_id).items():
            if key not in expected:
                dv("DELETE", f"botcomponents({row['botcomponentid']})")
                removed.append(row["schemaname"])
    log(f"   {'removed ' + ', '.join(removed) if removed else 'none'}")

    log("7/7 publish and read back")
    published = publish(dv, bot_id) if do_publish else {"status": "skipped"}
    bot, _ = dv("GET", f"bots({bot_id})?$select=template,configuration,publishedon,statuscode")
    comps = list_components(dv, bot_id)
    missing = sorted(set(expected) - set(comps))
    if missing:
        raise DeployError(f"components missing on the live record: {missing}")
    unlinked = [row["schemaname"] for row in comps.values()
                if "kind: WorkflowTool" in (row.get("data") or "") and not row.get("botcomponent_workflow")]
    if unlinked:
        raise DeployError(f"WorkflowTools with no flow on the live record: {unlinked}")
    instructions = (json.loads(bot["configuration"])["agentSettings"]["instructions"]["segments"][0]["value"])
    if bot.get("template") != HARNESS_TEMPLATE or instructions != settings["instructions"]:
        raise DeployError("the live bot is not the harness agent this workspace describes")
    env_id = environment_id(dv)
    result.update(botId=bot_id, bot=bot_op, components=len(comps), removed=removed, flows=flows,
                  connectionReferences=references, environmentVariables=variables, published=published,
                  makerUrl=(f"https://copilotstudio.microsoft.com/environments/{env_id}/agents/{bot_id}/preview"
                            if env_id else None))
    log(f"deployed  {schema} ({bot_id}): {len(comps)} components, published {published.get('publishedon')}")
    return result
