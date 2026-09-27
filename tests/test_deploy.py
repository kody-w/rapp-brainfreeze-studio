"""Deploy tests. Offline: an in-memory Dataverse that answers the queries brainfreeze_studio.deploy makes."""
import json
import hashlib
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from copy import deepcopy
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brainfreeze_studio import deploy as dp  # noqa: E402

ENV = "https://example.crm.dynamics.com/"
WF_ID = "8641231d-38d2-5f6b-8d7d-eb83eb364bcb"


class FakeDataverse:
    """Tables as dicts; just enough OData to exercise the deployer, and a log of every write."""

    def __init__(self):
        self.environment = ENV
        self.base = ENV + "api/data/v9.2/"
        self.t = {"connectionreferences": {}, "environmentvariabledefinitions": {}, "workflows": {}, "bots": {},
                  "botcomponents": {}}
        self.writes = []
        self.calls = []
        self.n = 0
        self.t["connectionreferences"]["ref-env"] = {"connectionreferenceid": "ref-env", "connectionreferencelogicalname":
                                                     "shared_env_dataverse", "connectorid":
                                                     "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps",
                                                     "connectionid": "conn-1", "createdon": "2026-01-01"}

    def _id(self):
        self.n += 1
        return f"00000000-0000-0000-0000-{self.n:012d}"

    def ref(self, entity_set, id_):
        return {"@odata.id": f"{self.base}{entity_set}({id_})"}

    def value(self, path):
        return self("GET", path)[0].get("value", [])

    def _filter(self, table, path):
        rows = list(self.t[table].values())
        m = re.search(r"\$filter=([^&]*)", path)
        for clause in (m.group(1).split(" and ") if m else []):
            eq = re.fullmatch(r"(\w+) eq '?([^']*)'?", clause.strip())
            if eq:
                rows = [r for r in rows if str(r.get(eq.group(1), "")).lower() == eq.group(2).lower()]
            elif re.fullmatch(r"(\w+) ne null", clause.strip()):
                key = clause.split()[0]
                rows = [r for r in rows if r.get(key)]
        return rows

    def __call__(self, method, path, body=None, prefer=None, headers=None, ok404=False):
        self.calls.append((method, path))
        if method != "GET":
            self.writes.append((method, path.split("?")[0]))
        if path.startswith("RetrieveCurrentOrganization"):
            return {"Detail": {"EnvironmentId": "env-1"}}, {}
        m = re.match(r"(\w+)\(([^)]+)\)(/.*)?", path.split("?")[0])
        if m:
            table, id_, rest = m.group(1), m.group(2), m.group(3) or ""
            row = self.t[table][id_]
            if rest == "/Microsoft.Dynamics.CRM.PvaPublish":
                now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 1))
                row.update(publishedon=now, synchronizationstatus=json.dumps(
                    {"lastFinishedPublishOperation": {"status": "Succeeded", "operationEnd": now + ".0Z"}}))
                return {}, {}
            link = re.fullmatch(r"/(botcomponent_workflow|botcomponent_connectionreference)(?:\(([^)]+)\))?/\$ref", rest)
            if link:
                rel = row.setdefault(link.group(1), [])
                if method == "DELETE":
                    row[link.group(1)] = [x for x in rel if x.get("workflowid") != link.group(2)]
                else:
                    target = re.search(r"/(\w+)\(([^)]+)\)$", body["@odata.id"])
                    if target.group(1) == "workflows":
                        rel.append({"workflowid": target.group(2)})
                    else:
                        ref = self.t["connectionreferences"][target.group(2)]
                        rel.append({"connectionreferenceid": target.group(2),
                                    "connectionreferencelogicalname": ref["connectionreferencelogicalname"]})
                return {}, {}
            if method == "GET":
                return dict(row), {}
            if method == "PATCH":
                row.update(body)
                return {}, {}
            if method == "DELETE":
                del self.t[table][id_]
                return {}, {}
        table = path.split("?")[0]
        if method == "GET":
            rows = self._filter(table, path)
            if table == "botcomponents":
                rows = [dict(r, botcomponent_workflow=r.get("botcomponent_workflow", []),
                             botcomponent_connectionreference=r.get("botcomponent_connectionreference", []))
                        for r in rows]
            return {"value": rows}, {}
        key = {"connectionreferences": "connectionreferenceid", "environmentvariabledefinitions":
               "environmentvariabledefinitionid", "workflows": "workflowid", "bots": "botid",
               "botcomponents": "botcomponentid"}[table]
        row = dict(body)
        id_ = row.get(key) or self._id()
        row[key] = id_
        if table == "botcomponents":
            row["_parentbotid_value"] = re.search(r"\(([^)]+)\)", row.pop("parentbotid@odata.bind")).group(1)
        self.t[table][id_] = row
        return ({key: id_} if prefer else {}), {"odata-entityid": f"{self.base}{table}({id_})"}


class ReadOnlyDataverse(FakeDataverse):
    """Use the same tables and GETs, but fail before any attempted write can reach the backing fake."""

    def __init__(self, source=None):
        self.source = source or FakeDataverse()
        self.environment, self.base = self.source.environment, self.source.base
        self.t, self.writes = self.source.t, self.source.writes
        self.reads = []

    def __call__(self, method, path, *args, **kw):
        if method != "GET":
            raise AssertionError(f"a plan tried to {method} {path}")
        self.reads.append(path)
        return self.source(method, path, *args, **kw)


def make_workspace(root, instructions="You are the test desk.\nAnswer briefly."):
    ws = Path(root) / "workspace"
    (ws / "capabilities" / "tools").mkdir(parents=True)
    (ws / "behaviors").mkdir()
    (ws / "workflows" / f"InvoiceRouterFlow-{WF_ID}").mkdir(parents=True)
    body = "\n".join("            " + line for line in instructions.split("\n"))
    (ws / "settings.mcs.yml").write_text(
        "displayName: Test Desk\nschemaName: rapp_TestDesk\nconfiguration:\n  agentSettings:\n    model:\n"
        "      series: Sonnet46\n    instructions:\n      segments:\n        - kind: StaticSegment\n"
        f"          value: |\n{body}\n    greetingText: Hello\n")
    (ws / "behaviors" / "rapp_desk-help.mcs.yml").write_text(
        'mcs.metadata:\n  componentName: desk-help\n  description: "Explains the desk."\nkind: InlineAgentSkill\n'
        "content: |\n  ---\n  name: desk-help\n  ---\n  Explain the desk.\n")
    (ws / "capabilities" / "tools" / "InvoiceRouterFlow.mcs.yml").write_text(
        'mcs.metadata:\n  componentName: "Invoice Router"\n  description: "Set `operation` to one of: a, b."\n'
        f"kind: WorkflowTool\nworkflowId: {WF_ID}\ntoolOutputs:\n  - name: result\ntoolInputs:\n  - name: amount\n")
    (ws / "capabilities" / "tools" / "rapp_dataverse-add-memory.mcs.yml").write_text(
        "mcs.metadata:\n  componentName: Add a new row\n  description: Create a row.\nkind: ConnectorTool\n"
        "connectionReference: rapp_TestDesk.cr.shared_commondataserviceforapps\n"
        "connectorId: /providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps\n")
    (ws / "workflows" / f"InvoiceRouterFlow-{WF_ID}" / "workflow.json").write_text(
        json.dumps({"properties": {"connectionReferences": {}, "definition": {"actions": {}}}}))
    (ws / "workflows" / f"InvoiceRouterFlow-{WF_ID}" / "metadata.yml").write_text(
        f'workflowId: {WF_ID}\nname: Test Desk InvoiceRouterFlow\ndescription: "Routes one invoice."\n')
    (Path(root) / "provenance.json").write_text(json.dumps({"environment_variables": [
        {"schemaName": "rapp_InvoiceApprovalLimit", "displayName": "Invoice approval limit", "type": "String",
         "defaultValue": "10000"}]}))
    return ws


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bfs-deploy-"))
        self.ws = make_workspace(self.root)
        self.dv = FakeDataverse()

    def run_deploy(self, **kw):
        kw.setdefault("connections", {"rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})
        return dp.deploy(self.ws, ENV, lambda: DATAVERSE_TOKEN, dataverse=self.dv, log=lambda *_: None, **kw)

    def test_the_workspace_is_read_as_the_build_lays_it_out(self):
        ws = dp.read_workspace(self.ws)
        s = ws["settings"]
        self.assertEqual((s["displayName"], s["schemaName"], s["series"]), ("Test Desk", "rapp_TestDesk", "Sonnet46"))
        self.assertEqual(s["instructions"], "You are the test desk.\nAnswer briefly.")
        tool = next(c for c in ws["components"] if c["kind"] == "WorkflowTool")
        self.assertEqual((tool["displayName"], tool["description"]), ("Invoice Router", "Set `operation` to one of: a, b."))
        self.assertTrue(tool["data"].startswith("kind: WorkflowTool\n"))
        self.assertNotIn("mcs.metadata", tool["data"])
        self.assertEqual(ws["connection_references"], {"rapp_TestDesk.cr.shared_commondataserviceforapps":
                                                       "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps"})

    def test_first_deploy_creates_links_and_publishes(self):
        r = self.run_deploy()
        self.assertEqual((r["bot"], r["components"], r["published"]["status"]), ("created", 3, "Succeeded"))
        self.assertEqual(r["makerUrl"], f"https://copilotstudio.microsoft.com/environments/env-1/agents/{r['botId']}/preview")
        bot = self.dv.t["bots"][r["botId"]]
        self.assertEqual((bot["template"], bot["name"]), ("cliagent-1.0.0", "Test Desk"))
        config = json.loads(bot["configuration"])
        self.assertEqual(config["recognizer"]["$kind"], "CLICopilotRecognizer")
        self.assertEqual(config["agentSettings"]["greetingText"], "Hello")
        comps = {c["schemaname"]: c for c in self.dv.t["botcomponents"].values()}
        self.assertEqual(sorted(comps), ["rapp_TestDesk.skill.rapp_desk-help", "rapp_TestDesk.tool.InvoiceRouterFlow",
                                         "rapp_TestDesk.tool.rapp_dataverse-add-memory"])
        self.assertEqual(comps["rapp_TestDesk.tool.InvoiceRouterFlow"]["botcomponent_workflow"], [{"workflowid": WF_ID}])
        self.assertEqual(comps["rapp_TestDesk.tool.InvoiceRouterFlow"]["description"], "Set `operation` to one of: a, b.")
        linked = comps["rapp_TestDesk.tool.rapp_dataverse-add-memory"]["botcomponent_connectionreference"]
        self.assertEqual(linked[0]["connectionreferencelogicalname"], "rapp_TestDesk.cr.shared_commondataserviceforapps")
        ref = next(x for x in self.dv.t["connectionreferences"].values()
                   if x["connectionreferencelogicalname"] == "rapp_TestDesk.cr.shared_commondataserviceforapps")
        self.assertEqual(ref["connectionid"], "conn-1")
        self.assertEqual(self.dv.t["workflows"][WF_ID]["statecode"], 1)

    def test_a_second_deploy_changes_nothing_and_a_trimmed_workspace_removes_stale_parts(self):
        self.run_deploy()
        self.dv.writes.clear()
        r = self.run_deploy()
        self.assertEqual(r["bot"], "unchanged")
        self.assertEqual([w for w in self.dv.writes if not w[1].endswith("PvaPublish")], [])
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        r = self.run_deploy()
        self.assertEqual(r["removed"], ["rapp_TestDesk.skill.rapp_desk-help"])
        self.assertEqual(r["components"], 2)

    def test_a_flow_that_dataverse_reformatted_is_still_unchanged(self):
        self.run_deploy()
        row = self.dv.t["workflows"][WF_ID]
        stored = json.loads(row["clientdata"])
        stored["properties"]["connectionReferences"] = {"shared_x": {"api": {"name": "shared_x", "logicalName": "new_x"}}}
        row["clientdata"] = json.dumps(stored, indent=2, sort_keys=True)
        wf = self.ws / "workflows" / f"InvoiceRouterFlow-{WF_ID}" / "workflow.json"
        wf.write_text(json.dumps({"properties": {"connectionReferences": {"shared_x": {"api": {"name": "shared_x"}}},
                                                 "definition": {"actions": {}}}}))
        self.dv.writes.clear()
        r = self.run_deploy()
        self.assertEqual([f["operation"] for f in r["flows"]], ["unchanged"])
        self.assertFalse([w for w in self.dv.writes if w[1].startswith("workflows")])

    def test_a_classic_agent_is_never_changed(self):
        self.dv.t["bots"]["b1"] = {"botid": "b1", "schemaname": "rapp_TestDesk", "template": "default-2.1.0",
                                   "configuration": "{}", "name": "Old"}
        with self.assertRaises(dp.DeployError):
            self.run_deploy()

    def test_no_connection_means_a_clear_error(self):
        self.dv.t["connectionreferences"].clear()
        with self.assertRaisesRegex(dp.DeployError, "no connection for"):
            self.run_deploy(connections={})

    def test_guards(self):
        with self.assertRaises(dp.DeployError):
            self.run_deploy(display_name="x" * 43)
        with self.assertRaises(dp.DeployError):
            self.run_deploy(schema_name="nounderscore")


def jwt(claims):
    import base64
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{part({'alg': 'none'})}.{part(claims)}.x"


ME, SOMEONE = "11111111-0000-0000-0000-000000000001", "22222222-0000-0000-0000-000000000002"
POWERAPPS_TOKEN, HUB_TOKEN = jwt({"oid": ME, "aud": "https://service.powerapps.com/"}), jwt({"oid": ME})
DATAVERSE_TOKEN = jwt({"oid": ME, "aud": ENV.rstrip("/"), "scp": "user_impersonation", "idtyp": "user"})
SITES = [{"Name": "https://contoso.sharepoint.com/sites/team", "DisplayName": "Team"},
         {"Name": "https://contoso.sharepoint.com", "DisplayName": "Communication site"}]
SIGN_IN_ALONE = {"token": {"type": "oauthSetting", "oAuthSettings": {"properties": {
                     "IsFirstParty": "True", "IsOnbehalfofLoginSupported": True}}},
                 "token:TenantId": {"type": "string", "uiDefinition": {"constraints": {"required": "false",
                                                                                         "hidden": "true"}}},
                 "gateway": {"type": "gatewaySetting", "uiDefinition": {"constraints": {"capability": ["gateway"]}}}}


class _Reply:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePowerApps:
    """The Power Apps resource provider's connections, API Hub's first-party login and the SharePoint connector's
    runtime, as an opener. Connections: {(api, name): connection}."""

    def __init__(self, connections=(), parameters=SIGN_IN_ALONE, login_works=True):
        self.connections = {(c["api"], c["name"]): c for c in connections}
        self.parameters, self.login_works = parameters, login_works
        self.requests = []

    def __call__(self, req, timeout=None):
        import io
        import urllib.error
        import urllib.parse
        url = urllib.parse.urlsplit(req.full_url)
        method, body = req.get_method(), json.loads(req.data) if req.data else None
        self.requests.append((method, req.full_url, body, req.headers.get("Authorization")))
        if url.netloc == "consent.example":                       # the first-party login
            key = tuple(urllib.parse.parse_qs(url.query)["c"][0].split("|"))
            if not self.login_works or body != {"accessToken": HUB_TOKEN}:
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"Message": "no"}'))
            self.connections[key]["properties"]["statuses"] = [{"status": "Connected"}]
            return _Reply({})
        if url.netloc == "runtime.example":
            return _Reply({"value": SITES})
        m = re.match(r"/providers/Microsoft\.PowerApps/apis/([^/]+)(?:/connections(?:/([^/]+))?)?$", url.path)
        api, listing, name = m.group(1), "/connections" in url.path, m.group(2)
        if not listing:
            return _Reply({"name": api, "properties": {"displayName": "SharePoint", "connectionParameters":
                                                        self.parameters,
                                                        "runtimeUrls": ["https://runtime.example/apis/x/connections/"]}})
        if name is None:
            return _Reply({"value": [c for (a, _), c in self.connections.items() if a == api]})
        if method == "PUT":
            self.connections[(api, name)] = {"api": api, "name": name, "properties": {
                **body["properties"], "createdBy": {"id": ME}, "statuses": [{"status": "Error"}],
                "consentInfo": {"firstPartyLoginUri": f"https://consent.example/login?c={api}|{name}"}
                if "ConsentLink" in url.query else {}}}
            return _Reply(self.connections[(api, name)])
        if method == "DELETE":
            self.connections.pop((api, name), None)
            return _Reply({})
        return _Reply(self.connections[(api, name)])


def connection(api, name, owner=ME, status="Connected", modified="2026-09-01"):
    return {"api": api, "name": name, "properties": {"createdBy": {"id": owner}, "statuses": [{"status": status}],
                                                      "lastModifiedTime": modified}}


class UserConnectionTests(unittest.TestCase):
    def find(self, rp, hub=True):
        return dp.user_connection(lambda: POWERAPPS_TOKEN, "env-1", "shared_sharepointonline",
                                  (lambda: HUB_TOKEN) if hub else None, opener=rp, wait=0)

    def test_the_users_own_connected_connection_is_used(self):
        rp = FakePowerApps([connection("shared_sharepointonline", "theirs", owner=SOMEONE, modified="2026-09-09"),
                            connection("shared_sharepointonline", "mine-broken", status="Error", modified="2026-09-08"),
                            connection("shared_sharepointonline", "mine-old", modified="2026-01-01"),
                            connection("shared_sharepointonline", "mine", modified="2026-09-02")])
        self.assertEqual(self.find(rp), ("mine", "existing"))
        self.assertFalse([r for r in rp.requests if r[0] != "GET"])

    def test_with_none_one_is_made_with_the_users_sign_in_alone(self):
        rp = FakePowerApps([connection("shared_sharepointonline", "theirs", owner=SOMEONE)])
        name, how = self.find(rp)
        self.assertEqual(how, "created")
        put = next(r for r in rp.requests if r[0] == "PUT")
        self.assertIn("$expand=ConsentLink", put[1])
        self.assertEqual(put[2]["properties"]["connectionParameters"], {})
        login = next(r for r in rp.requests if r[1].startswith("https://consent.example/"))
        self.assertEqual((login[0], login[2], login[3]), ("POST", {"accessToken": HUB_TOKEN}, "Bearer " + HUB_TOKEN))
        self.assertEqual(rp.connections[("shared_sharepointonline", name)]["properties"]["statuses"],
                         [{"status": "Connected"}])

    def test_a_failed_login_leaves_nothing_behind(self):
        rp = FakePowerApps(login_works=False)
        name, why = self.find(rp)
        self.assertIsNone(name)
        self.assertIn("HTTP 400", why)
        self.assertEqual(rp.connections, {})

    def test_a_connector_that_needs_an_interactive_sign_in_is_left_alone(self):
        rp = FakePowerApps(parameters={"token": {"type": "oauthSetting", "oAuthSettings": {"properties": {}}}})
        self.assertEqual(self.find(rp), (None, "its connections need an interactive sign-in"))
        self.assertIsNone(self.find(FakePowerApps(), hub=False)[0])
        self.assertFalse([r for r in rp.requests if r[0] == "PUT"])

    def test_which_connectors_sign_in_alone(self):
        self.assertEqual(dp.silent_sign_in({"properties": {"connectionParameters": SIGN_IN_ALONE}}), (True, None))
        visible = dict(SIGN_IN_ALONE, siteUrl={"type": "string", "uiDefinition": {"constraints": {"required": "true"}}})
        self.assertEqual(dp.silent_sign_in({"properties": {"connectionParameters": visible}}), (False, None))
        sets = {"values": [{"name": "key", "parameters": {"api_key": {"type": "securestring"}}},
                           {"name": "oauth", "parameters": SIGN_IN_ALONE}]}
        self.assertEqual(dp.silent_sign_in({"properties": {"connectionParameterSets": sets}}), (True, "oauth"))
        self.assertEqual(dp.root_site([{"url": s["Name"]} for s in SITES]), "https://contoso.sharepoint.com")


def files_workspace(root):
    """make_workspace plus a flow that reads files from SharePoint, as connector_code lays one out."""
    ws = make_workspace(root)
    wid = "4b1d7d3e-0000-5000-8000-00000000f11e"
    folder = ws / "workflows" / f"JsonDoctorFlow-{wid}"
    folder.mkdir()
    params = {"$connections": {"defaultValue": {}, "type": "Object"},
              "RAPP Files Site (rapp_RappFilesSite)": {"defaultValue": "", "type": "String",
                                                       "metadata": {"schemaName": "rapp_RappFilesSite"}},
              "RAPP Files Folder (rapp_RappFilesFolder)": {"defaultValue": "/Shared Documents", "type": "String",
                                                           "metadata": {"schemaName": "rapp_RappFilesFolder"}}}
    (folder / "workflow.json").write_text(json.dumps({"properties": {"connectionReferences": {
        "shared_sharepointonline": {"api": {"name": "shared_sharepointonline"}, "connection": {
            "connectionReferenceLogicalName": "rapp_TestDesk.shared_sharepointonline"}}},
        "definition": {"parameters": params, "actions": {}}}}))
    (folder / "metadata.yml").write_text(f"workflowId: {wid}\nname: Test Desk JsonDoctorFlow\n")
    prov = json.loads((Path(root) / "provenance.json").read_text())
    prov["environment_variables"] += [
        {"schemaName": "rapp_RappFilesSite", "displayName": "RAPP Files Site", "type": "String", "defaultValue": "",
         "files": "site"},
        {"schemaName": "rapp_RappFilesFolder", "displayName": "RAPP Files Folder", "type": "String",
         "defaultValue": "/Shared Documents", "files": "folder"}]
    (Path(root) / "provenance.json").write_text(json.dumps(prov))
    return ws, wid


class FilesDeployTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bfs-deploy-files-"))
        self.ws, self.wid = files_workspace(self.root)
        self.dv = FakeDataverse()
        self.rp = FakePowerApps([connection("shared_commondataserviceforapps", "dv-mine")])

    def run_deploy(self, **kw):
        return dp.deploy(self.ws, ENV, lambda: DATAVERSE_TOKEN, dataverse=self.dv, log=lambda *_: None,
                         get_powerapps_token=lambda: POWERAPPS_TOKEN, get_apihub_token=lambda: HUB_TOKEN,
                         powerapps_opener=self.rp, **kw)

    def variables(self):
        return {v["schemaname"]: v["defaultvalue"] for v in self.dv.t["environmentvariabledefinitions"].values()}

    def site_in_flow(self):
        stored = json.loads(self.dv.t["workflows"][self.wid]["clientdata"])
        return stored["properties"]["definition"]["parameters"]["RAPP Files Site (rapp_RappFilesSite)"]["defaultValue"]

    def test_the_files_live_in_the_tenants_root_site_through_a_connection_made_as_the_user(self):
        r = self.run_deploy()
        refs = {x["connectionreferencelogicalname"]: x["connectionid"] for x in self.dv.t["connectionreferences"].values()}
        made = [n for (a, n) in self.rp.connections if a == "shared_sharepointonline"]
        self.assertEqual(refs["rapp_TestDesk.shared_sharepointonline"], made[0])       # made with the sign-in alone
        self.assertEqual(refs["rapp_TestDesk.cr.shared_commondataserviceforapps"], "dv-mine")   # their own
        self.assertEqual(r["files_home"]["site"], "https://contoso.sharepoint.com")
        self.assertEqual(self.variables()["rapp_RappFilesSite"], "https://contoso.sharepoint.com")
        self.assertEqual(self.variables()["rapp_RappFilesFolder"], "/Shared Documents")
        self.assertEqual(self.site_in_flow(), "https://contoso.sharepoint.com")

    def test_a_site_given_wins_and_the_environments_own_value_is_kept_otherwise(self):
        self.run_deploy(files_site="https://contoso.sharepoint.com/sites/team")
        self.assertEqual(self.variables()["rapp_RappFilesSite"], "https://contoso.sharepoint.com/sites/team")
        r = self.run_deploy()
        self.assertEqual(r["files_home"]["site"], "https://contoso.sharepoint.com/sites/team")
        self.assertEqual(self.site_in_flow(), "https://contoso.sharepoint.com/sites/team")

    def test_no_site_and_no_way_to_find_one_is_a_clear_error(self):
        with self.assertRaisesRegex(dp.DeployError, "no site was given or found"):
            dp.deploy(self.ws, ENV, lambda: DATAVERSE_TOKEN, dataverse=self.dv, log=lambda *_: None,
                      connections={"rapp_TestDesk.shared_sharepointonline": "sp-1",
                                   "rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})

    def test_an_empty_default_is_filled_and_a_set_one_is_left_alone(self):
        var = {"schemaName": "rapp_X", "displayName": "X", "defaultValue": ""}
        self.assertEqual(dp.ensure_environment_variable(self.dv, var)["operation"], "created")
        self.assertEqual(dp.ensure_environment_variable(self.dv, dict(var, defaultValue="a"))["operation"], "updated")
        self.assertEqual(dp.ensure_environment_variable(self.dv, dict(var, defaultValue="b"))["operation"], "existing")
        self.assertEqual(dp.ensure_environment_variable(self.dv, dict(var, defaultValue="b", replace=True))["operation"],
                         "updated")
        self.assertEqual(self.variables()["rapp_X"], "b")


class PlanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = make_workspace(self.root)
        self.dv = FakeDataverse()

    def run_deploy(self, **kw):
        kw.setdefault("connections", {"rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})
        return dp.deploy(self.ws, ENV, lambda: DATAVERSE_TOKEN, dataverse=self.dv, do_publish=False, log=lambda *_: None, **kw)

    def run_plan(self, **kw):
        readonly = ReadOnlyDataverse(self.dv)
        before = deepcopy(self.dv.t)
        writes = list(self.dv.writes)
        kw.setdefault("connections", {"rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})
        result = dp.plan(self.ws, ENV, lambda: DATAVERSE_TOKEN, dataverse=readonly, **kw)
        self.assertEqual(self.dv.t, before)
        self.assertEqual(self.dv.writes, writes)
        return result

    def test_create_is_get_only_and_matches_the_deploy(self):
        r = self.run_plan()
        self.assertEqual(r["agent"], {"schemaName": "rapp_TestDesk", "displayName": "Test Desk", "operation": "create"})
        self.assertEqual(r["components"], {"add": ["rapp_TestDesk.tool.InvoiceRouterFlow",
                          "rapp_TestDesk.tool.rapp_dataverse-add-memory", "rapp_TestDesk.skill.rapp_desk-help"],
                          "update": [], "remove": [], "keep": []})
        self.assertEqual(r["flows"], {"create": ["Test Desk InvoiceRouterFlow"], "update": []})
        self.assertEqual(r["environmentVariables"], {"create": ["rapp_InvoiceApprovalLimit"], "keep": []})
        self.assertEqual(r["connectionReferences"], {"create": ["rapp_TestDesk.cr.shared_commondataserviceforapps"],
                                                    "keep": [], "sources": {
                                                        "rapp_TestDesk.cr.shared_commondataserviceforapps":
                                                        "explicit connections map"}})
        self.assertEqual(self.run_deploy()["bot"], "created")

    def test_update_and_removals_agree_with_the_deploy(self):
        made = self.run_deploy()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        settings = self.ws / "settings.mcs.yml"
        settings.write_text(settings.read_text().replace("Answer briefly.", "Answer carefully."))
        self.dv.t["botcomponents"]["other"] = {"botcomponentid": "other", "_parentbotid_value": made["botId"],
                                              "schemaname": "handmade.topic.Legacy", "name": "Legacy", "data": ""}
        before = {id_: row["schemaname"] for id_, row in self.dv.t["botcomponents"].items()}
        r = self.run_plan()
        self.assertEqual(r["agent"]["operation"], "update")
        self.assertEqual(r["components"]["remove"], ["rapp_TestDesk.skill.rapp_desk-help", "handmade.topic.Legacy"])
        self.dv.writes.clear()
        deployed = self.run_deploy()
        deleted = [before[m.group(1)] for method, path in self.dv.writes
                   if method == "DELETE" and (m := re.fullmatch(r"botcomponents\(([^)]+)\)", path))]
        self.assertEqual(r["components"]["remove"], deleted)
        self.assertEqual(r["components"]["remove"], deployed["removed"])
        self.assertEqual(deployed["bot"], "updated")

    def test_unchanged_rows_are_kept_and_the_deploy_agrees(self):
        self.run_deploy()
        self.dv.writes.clear()
        r = self.run_plan()
        self.assertEqual(r["agent"]["operation"], "unchanged")
        self.assertEqual([len(r["components"][k]) for k in ("add", "update", "remove", "keep")], [0, 0, 0, 3])
        self.assertEqual(r["flows"], {"create": [], "update": []})
        self.assertEqual(r["environmentVariables"], {"create": [], "keep": ["rapp_InvoiceApprovalLimit"]})
        self.assertEqual(r["connectionReferences"], {"create": [],
                         "keep": ["rapp_TestDesk.cr.shared_commondataserviceforapps"],
                         "sources": {"rapp_TestDesk.cr.shared_commondataserviceforapps": "explicit connections map"}})
        self.assertEqual(self.run_deploy()["bot"], "unchanged")
        self.assertEqual(self.dv.writes, [])

    def test_keep_extra_components_turns_removals_into_keeps(self):
        self.run_deploy()
        (self.ws / "behaviors" / "rapp_desk-help.mcs.yml").unlink()
        r = self.run_plan(keep_extra_components=True)
        self.assertEqual(r["components"]["remove"], [])
        self.assertIn("rapp_TestDesk.skill.rapp_desk-help", r["components"]["keep"])
        self.dv.writes.clear()
        self.assertEqual(self.run_deploy(keep_extra_components=True)["removed"], [])
        self.assertFalse([w for w in self.dv.writes if w[0] == "DELETE"])

    def test_a_classic_agent_refuses_with_gets_only(self):
        self.dv.t["bots"]["old"] = {"botid": "old", "schemaname": "rapp_TestDesk", "template": "default-2.1.0"}
        r = self.run_plan()
        reason = "rapp_TestDesk exists but is a classic agent (default-2.1.0); refusing to change it"
        self.assertEqual(r["agent"]["operation"], "refuse")
        self.assertEqual(r["agent"]["reason"], reason)
        self.assertFalse(any(r["components"].values()))
        self.assertFalse(any(r["flows"].values()))
        self.assertEqual(self.dv.writes, [])
        with self.assertRaises(dp.DeployError) as refused:
            self.run_deploy()
        self.assertEqual(str(refused.exception), reason)

    def test_name_overrides_and_guards_are_the_deploys_own(self):
        r = self.run_plan(schema_name="other_Desk", display_name="Other Desk")
        self.assertEqual(r["agent"], {"schemaName": "other_Desk", "displayName": "Other Desk", "operation": "create"})
        self.assertTrue(all(n.startswith("other_Desk.") for n in r["components"]["add"]))
        self.assertEqual(self.run_deploy(schema_name="other_Desk", display_name="Other Desk")["bot"], "created")
        for kw in ({"schema_name": "nounderscore"}, {"display_name": "x" * 43}):
            with self.subTest(kw=kw):
                r = self.run_plan(**kw)
                self.assertEqual(r["agent"]["operation"], "refuse")
                with self.assertRaises(dp.DeployError) as refused:
                    self.run_deploy(**kw)
                self.assertEqual(r["agent"]["reason"], str(refused.exception))

    def test_component_updates_include_content_and_links(self):
        self.run_deploy()
        for c in self.dv.t["botcomponents"].values():
            if c["schemaname"].endswith("InvoiceRouterFlow"):
                c["botcomponent_workflow"] = [{"workflowid": "aaaaaaaa-0000-0000-0000-000000000000"}]
            elif c["schemaname"].endswith("rapp_dataverse-add-memory"):
                c["botcomponent_connectionreference"] = []
            else:
                c["description"] = "Old description"
        r = self.run_plan()
        self.assertEqual(len(r["components"]["update"]), 3)
        self.assertEqual(r["components"]["keep"], [])
        self.run_deploy()
        self.assertEqual(self.run_plan()["components"]["update"], [])

    def test_flow_updates_use_the_same_state_and_content_comparison(self):
        self.run_deploy()
        saved = deepcopy(self.dv.t)
        for change in ({"statecode": 0}, {"name": "Old"}, {"description": "Old"},
                       {"clientdata": "not JSON"}, {"clientdata": '{"properties": {}}'}):
            with self.subTest(change=change):
                self.dv.t = deepcopy(saved)
                self.dv.t["workflows"][WF_ID].update(change)
                self.assertEqual(self.run_plan()["flows"], {"create": [], "update": ["Test Desk InvoiceRouterFlow"]})
                self.assertEqual(self.run_deploy()["flows"][0]["operation"], "updated")

    def test_reformatted_flow_json_and_reference_metadata_are_unchanged(self):
        wf = self.ws / "workflows" / f"InvoiceRouterFlow-{WF_ID}" / "workflow.json"
        definition = json.loads(wf.read_text())
        definition["properties"]["connectionReferences"] = {"shared_x": {"api": {"name": "shared_x"}}}
        wf.write_text(json.dumps(definition))
        self.run_deploy()
        definition["properties"]["connectionReferences"]["shared_x"]["api"]["logicalName"] = "added-on-save"
        self.dv.t["workflows"][WF_ID]["clientdata"] = json.dumps(definition, indent=2, sort_keys=True)
        self.assertEqual(self.run_plan()["flows"], {"create": [], "update": []})
        self.assertEqual(self.run_deploy()["flows"][0]["operation"], "unchanged")

    def test_a_missing_tool_flow_refuses_like_the_deploy(self):
        shutil.rmtree(self.ws / "workflows")
        r = self.run_plan()
        self.assertEqual(r["agent"]["operation"], "refuse")
        with self.assertRaises(dp.DeployError) as refused:
            self.run_deploy()
        self.assertEqual(r["agent"]["reason"], str(refused.exception))

    def test_existing_files_defaults_are_used_before_comparing_flows(self):
        self.ws, _ = files_workspace(self.root / "files")
        self.run_deploy(files_site="https://contoso.sharepoint.com",
                        connections={"rapp_TestDesk.shared_sharepointonline": "sp-1",
                                     "rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"})
        self.assertEqual(self.run_plan()["flows"], {"create": [], "update": []})
        self.assertTrue(all(f["operation"] == "unchanged" for f in self.run_deploy()["flows"]))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = make_workspace(self.root)
        self.dv = FakeDataverse()
        self.bindings = {"rapp_TestDesk.cr.shared_commondataserviceforapps": "conn-1"}

    def deploy(self, **kw):
        options = dict(dataverse=self.dv, connections=self.bindings, do_publish=False, log=lambda *_: None)
        options.update(kw)
        return dp.deploy(self.ws, ENV, lambda: DATAVERSE_TOKEN, **options)

    def plan(self, **kw):
        options = dict(dataverse=ReadOnlyDataverse(self.dv), connections=self.bindings)
        options.update(kw)
        return dp.plan(self.ws, ENV, lambda: DATAVERSE_TOKEN, **options)

    def test_digest_covers_sorted_posix_names_hidden_files_and_exact_bytes(self):
        root = self.root / "digest"
        root.mkdir()
        entries = [(".hidden", b"x"), ("a/file.bin", b"\x00\xff\r\n"), ("z.txt", b"last\n")]
        for name, data in reversed(entries):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        framed = b""
        for name, data in entries:
            for part in (name.encode(), data):
                framed += len(part).to_bytes(8, "big") + part
        expected = hashlib.sha256(framed).hexdigest()
        self.assertEqual(dp.workspace_digest(root), expected)
        (root / "z.txt").touch()
        self.assertEqual(dp.workspace_digest(root), expected)
        (root / ".hidden").write_bytes(b"changed")
        self.assertNotEqual(dp.workspace_digest(root), expected)
        (root / ".hidden").write_bytes(b"x")
        (root / "z.txt").rename(root / "renamed.txt")
        self.assertNotEqual(dp.workspace_digest(root), expected)

    def test_expect_refuses_changed_bytes_before_tokens_or_requests(self):
        planned = dp.workspace_digest(self.ws)[:12]
        path = self.ws / "behaviors" / "rapp_desk-help.mcs.yml"
        path.write_text(path.read_text() + "\nchanged\n")
        now = dp.workspace_digest(self.ws)[:12]
        for function in (dp.plan, dp.deploy):
            with self.subTest(function=function.__name__):
                token = mock.Mock(side_effect=AssertionError("no token before the digest check"))
                with self.assertRaises(dp.DeployError) as error:
                    function(self.ws, ENV, token, dataverse=self.dv, expect=planned)
                self.assertEqual(str(error.exception),
                                 f"the build changed since the plan (digest {now}, planned {planned}); plan again")
                token.assert_not_called()
                self.assertEqual(self.dv.calls, [])

    def test_plan_and_deploy_return_the_approved_digest(self):
        plan = self.plan()
        result = self.deploy(expect=plan["digest"])
        self.assertEqual(result["digest"], plan["digest"])
        self.assertRegex(result["digest"], r"^[0-9a-f]{12}$")
        self.assertEqual(self.deploy(expect=dp.workspace_digest(self.ws))["bot"], "unchanged")

    def test_classic_refusal_has_only_gets_and_no_partial_deploy(self):
        self.dv.t["bots"]["classic"] = {"botid": "classic", "schemaname": "rapp_TestDesk", "template": "default-2.1.0"}
        with self.assertRaisesRegex(dp.DeployError, "classic agent"):
            self.deploy()
        self.assertTrue(self.dv.calls)
        self.assertTrue(all(method == "GET" for method, _ in self.dv.calls))
        self.assertEqual(self.dv.writes, [])
        self.assertEqual(self.dv.t["workflows"], {})
        self.assertEqual(self.dv.t["environmentvariabledefinitions"], {})

    def test_app_only_tokens_are_refused_before_any_request(self):
        for claims in ({"idtyp": "app"}, {"roles": ["Maker"], "oid": ME}, {"scp": "User"}, {}):
            for function in (dp.plan, dp.deploy):
                with self.subTest(claims=claims, function=function.__name__):
                    with self.assertRaisesRegex(dp.DeployError, "this is an app-only token; sign in as yourself"):
                        function(self.ws, ENV, lambda: jwt(claims), dataverse=self.dv)
                    self.assertEqual(self.dv.calls, [])
        opener = mock.Mock(side_effect=AssertionError("no HTTP for an app-only token"))
        dv = dp.Dataverse(ENV, lambda: jwt({"idtyp": "app"}), opener=opener)
        with self.assertRaisesRegex(dp.DeployError, "app-only"):
            dv.value("bots")
        opener.assert_not_called()
        self.assertEqual(self.plan()["agent"]["operation"], "create")

    def test_reference_priority_and_shared_connection_opt_in(self):
        connector = dp.POWERAPPS_APIS + "shared_commondataserviceforapps"
        find = mock.Mock(return_value="mine")
        chosen = dp.ensure_connection_reference(self.dv, "rapp_explicit", connector, "Explicit",
                                                 {"rapp_explicit": "chosen"}, find)
        self.assertEqual((chosen["connectionId"], chosen["source"]), ("chosen", "explicit connections map"))
        find.assert_not_called()
        kept = dp.ensure_connection_reference(self.dv, "rapp_explicit", connector, "Explicit", find_connection=find)
        self.assertEqual((kept["connectionId"], kept["source"]), ("chosen", "existing binding"))
        find.assert_not_called()
        mine = dp.ensure_connection_reference(self.dv, "rapp_mine", connector, "Mine", find_connection=find)
        self.assertEqual((mine["connectionId"], mine["source"]), ("mine", "your connection"))
        before = list(self.dv.writes)
        with self.assertRaisesRegex(dp.DeployError, "create one in Power Apps .*as this user"):
            dp.ensure_connection_reference(self.dv, "rapp_shared", connector, "Shared")
        self.assertEqual(self.dv.writes, before)
        shared = dp.ensure_connection_reference(self.dv, "rapp_shared", connector, "Shared", use_shared_connection=True)
        self.assertEqual((shared["connectionId"], shared["source"]), ("conn-1", "shared connection (not yours)"))

    def test_plan_reports_sources_without_using_someone_elses_connection(self):
        logical = "rapp_TestDesk.cr.shared_commondataserviceforapps"
        api = "shared_commondataserviceforapps"
        self.assertEqual(self.plan()["connectionReferences"]["sources"][logical], "explicit connections map")
        refused = self.plan(connections={})
        self.assertEqual(refused["agent"]["operation"], "refuse")
        self.assertIn("create one in Power Apps", refused["agent"]["reason"])
        shared = self.plan(connections={}, use_shared_connection=True)
        self.assertEqual(shared["connectionReferences"]["sources"][logical], "shared connection (not yours)")
        pa = FakePowerApps([connection(api, "mine")])
        mine = self.plan(connections={}, get_powerapps_token=lambda: POWERAPPS_TOKEN, powerapps_opener=pa)
        self.assertEqual(mine["connectionReferences"]["sources"][logical], "your connection")
        self.assertTrue(all(method == "GET" for method, *_ in pa.requests))
        self.deploy()
        self.assertEqual(self.plan(connections={})["connectionReferences"]["sources"][logical], "existing binding")

    def test_code_connections_are_reused_only_for_the_same_owner(self):
        for owner, operation in ((SOMEONE, "created"), (ME, "existing")):
            calls = []

            def opener(req, timeout=None):
                calls.append(req.get_method())
                return _Reply({"value": [{"name": "a-connection", "properties": {
                    "createdBy": {"id": owner}, "statuses": [{"status": "Connected"}]}}]})

            with self.subTest(owner=owner):
                name, actual = dp.ensure_code_connection(lambda: POWERAPPS_TOKEN, "env-1", "custom_code", "Code",
                                                          opener=opener, wait=0)
                self.assertEqual(actual, operation)
                self.assertEqual(calls, ["GET"] if owner == ME else ["GET", "PUT"])
                self.assertEqual(name == "a-connection", owner == ME)

    def test_draft_status_is_exact_and_is_logged_before_writes(self):
        messages = []

        def log(message):
            if message.startswith("status:"):
                self.assertEqual(self.dv.writes, [])
                messages.append(message)

        first = self.deploy(log=log)
        self.assertEqual(messages, ["status:      Draft: new agent, not published"])
        self.assertEqual(self.plan()["status"], "Draft: not published")
        bot = self.dv.t["bots"][first["botId"]]
        bot["publishedon"] = "2026-09-26T10:00:00Z"
        path = self.ws / "workflows" / f"InvoiceRouterFlow-{WF_ID}" / "workflow.json"
        definition = json.loads(path.read_text())
        definition["properties"]["definition"]["actions"]["Changed"] = {"type": "Compose", "inputs": "new"}
        path.write_text(json.dumps(definition))
        expected = ("Draft changes to an agent published 2026-09-26T10:00:00Z: its published version keeps its old "
                    "settings until you publish, but the 1 flows changed now and it already uses them")
        self.assertEqual(self.plan()["status"], expected)
        self.dv.writes.clear()
        messages.clear()
        result = self.deploy(log=log)
        self.assertEqual(messages, ["status:      " + expected])
        self.assertEqual(result["status"], expected)
        self.assertEqual(result["flows"][0]["operation"], "updated")
        self.assertFalse(any("PvaPublish" in path for _, path in self.dv.writes))

    def test_readback_rejects_corrupt_content_links_bindings_and_missing_variables(self):
        class Corrupting(FakeDataverse):
            def __init__(self, mode):
                super().__init__()
                self.mode = mode

            def __call__(self, method, path, body=None, **kw):
                reply = super().__call__(method, path, body, **kw)
                if method == "POST" and path == "botcomponents" and "InlineAgentSkill" in body["data"]:
                    row = next(r for r in self.t["botcomponents"].values() if r["schemaname"] == body["schemaname"])
                    if self.mode == "data":
                        row["data"] += "\nwrong content"
                    if self.mode == "description":
                        row["description"] = "wrong description"
                if method == "POST" and path.endswith("/botcomponent_workflow/$ref") and self.mode == "link":
                    id_ = path.split("(", 1)[1].split(")", 1)[0]
                    self.t["botcomponents"][id_]["botcomponent_workflow"] = [{"workflowid": "wrong-flow"}]
                if method == "PATCH" and path == f"workflows({WF_ID})" and body.get("statecode") == 1:
                    if self.mode == "flow":
                        self.t["workflows"][WF_ID]["clientdata"] = '{"properties":{"definition":{"actions":{"wrong":{}}}}}'
                    if self.mode == "inactive":
                        self.t["workflows"][WF_ID]["statecode"] = 0
                if method == "POST" and path == "connectionreferences" and self.mode == "binding":
                    row = next(r for r in self.t[path].values() if r["connectionreferencelogicalname"] ==
                               body["connectionreferencelogicalname"])
                    row["connectionid"] = "wrong-connection"
                if method == "POST" and path == "environmentvariabledefinitions" and self.mode == "variable":
                    self.t[path].clear()
                return reply

        cases = {"data": "component data differs.*rapp_desk-help", "description": "component description differs",
                 "link": "workflow link differs.*InvoiceRouterFlow", "flow": "flow definition differs.*InvoiceRouterFlow",
                 "inactive": "flow is not active", "binding": "connection binding differs",
                 "variable": "environment variable missing.*rapp_InvoiceApprovalLimit"}
        for mode, error in cases.items():
            with self.subTest(mode=mode):
                dv = Corrupting(mode)
                with self.assertRaisesRegex(dp.DeployError, error):
                    self.deploy(dataverse=dv, do_publish=True)
                self.assertFalse(any("PvaPublish" in path for _, path in dv.writes))

    def test_readback_accepts_only_the_allowed_text_and_json_normalization(self):
        self.deploy()
        for row in self.dv.t["botcomponents"].values():
            row["data"] = row["data"].replace("\n", " \t\r\n") + " \t\r\n"
        wf = self.dv.t["workflows"][WF_ID]
        definition = json.loads(wf["clientdata"])
        definition["properties"]["templateName"] = "server metadata"
        wf["clientdata"] = json.dumps(definition, indent=2, sort_keys=True)
        self.dv.writes.clear()
        self.assertEqual(self.deploy()["bot"], "unchanged")
        self.assertEqual(self.dv.writes, [])


if __name__ == "__main__":
    unittest.main()
