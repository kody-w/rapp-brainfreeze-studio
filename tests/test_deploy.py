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


if __name__ == "__main__":
    unittest.main()
