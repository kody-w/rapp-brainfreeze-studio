"""Deploy tests. Offline: an in-memory Dataverse that answers the queries brainfreeze_studio.deploy makes."""
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
        return dp.deploy(self.ws, ENV, lambda: "token", dataverse=self.dv, log=lambda *_: None, **kw)

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
            self.run_deploy()

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
        return dp.deploy(self.ws, ENV, lambda: "token", dataverse=self.dv, log=lambda *_: None,
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
            dp.deploy(self.ws, ENV, lambda: "token", dataverse=self.dv, log=lambda *_: None,
                      connections={"rapp_TestDesk.shared_sharepointonline": "sp-1"})

    def test_an_empty_default_is_filled_and_a_set_one_is_left_alone(self):
        var = {"schemaName": "rapp_X", "displayName": "X", "defaultValue": ""}
        self.assertEqual(dp.ensure_environment_variable(self.dv, var)["operation"], "created")
        self.assertEqual(dp.ensure_environment_variable(self.dv, dict(var, defaultValue="a"))["operation"], "updated")
        self.assertEqual(dp.ensure_environment_variable(self.dv, dict(var, defaultValue="b"))["operation"], "existing")
        self.assertEqual(dp.ensure_environment_variable(self.dv, dict(var, defaultValue="b", replace=True))["operation"],
                         "updated")
        self.assertEqual(self.variables()["rapp_X"], "b")


if __name__ == "__main__":
    unittest.main()
