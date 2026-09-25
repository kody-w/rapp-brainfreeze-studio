"""Connector code: agent logic ported to C# (custom connector code), proven against the agent's Python, and the flow
and deploy steps that run it. The C# is compiled locally with the .NET SDK; those tests skip without `dotnet`. The
Thoughtbox proof also needs a RAPP_Store checkout: BFS_RAPP_STORE=~/src/RAPP_Store."""
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import brainfreeze_studio as bs  # noqa: E402
from brainfreeze_studio import connector_code as cc, materialize, rapplication  # noqa: E402

HAVE_DOTNET = shutil.which("dotnet") is not None
STORE = os.path.expanduser(os.getenv("BFS_RAPP_STORE", ""))
THOUGHTBOX = Path(STORE, "apps", "@kody-w", "thoughtbox") if STORE else None
HAVE_THOUGHTBOX = bool(THOUGHTBOX) and (THOUGHTBOX / "singleton" / "thoughtbox_agent.py").is_file()
SPEC = json.loads((ROOT / "translations" / "thoughtbox.json").read_text())
HN = Path(os.path.expanduser(os.getenv("HARNESS_SDK_DIR", "~/Documents/GitHub/copilot-harness-sdk"))) / \
    "tutorial" / "profiles" / "hackernews" / "connector" / "script.csx"

# A port whose operations expose PyCompat, so the C# can be checked against Python itself.
PROBE = r"""
public class Script : ScriptBase
{
    public override async Task<HttpResponseMessage> ExecuteAsync()
    {
        var body = (JObject)Call.ParseJson(await this.Context.Request.Content.ReadAsStringAsync());
        var outs = new JArray();
        foreach (var item in (JArray)body["items"])
        {
            string op = this.Context.OperationId;
            try
            {
                if (op == "Dumps") outs.Add(PyJson.Dumps(item, 2));
                else if (op == "DumpsFlat") outs.Add(PyJson.Dumps(item));
                else if (op == "Loads") outs.Add(PyJson.Dumps(PyJson.Loads((string)item), 2));
                else if (op == "Repr") outs.Add(PyJson.FloatRepr((double)item, true));
                else if (op == "Int") outs.Add(Py.Int(item).ToString(System.Globalization.CultureInfo.InvariantCulture));
                else if (op == "Strip") outs.Add(Py.Strip((string)item));
            }
            catch (Exception e) { outs.Add(Call.Raised(e)); }
        }
        var r = new HttpResponseMessage(HttpStatusCode.OK);
        r.Content = CreateJsonContent(new JObject { ["out"] = outs }.ToString(Newtonsoft.Json.Formatting.None));
        return r;
    }
}
"""


def python311():
    return materialize.agent_python("3.11")


def py(code, items):
    """Run `code` (a function of x) over items under the engine's Python; returns the outputs as text."""
    script = ("import json, sys\n" + code + "\nout = []\nfor x in json.loads(sys.stdin.read()):\n"
              "    try:\n        out.append(f(x))\n    except Exception as e:\n"
              "        out.append(f'{type(e).__name__}: {e}')\nprint(json.dumps(out))")
    p = subprocess.run([python311(), "-c", script], input=json.dumps(items), capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


@unittest.skipUnless(HAVE_DOTNET, "needs the .NET SDK (dotnet)")
class PyCompatTests(unittest.TestCase):
    """PyCompat against Python 3.11 itself: the ground every port stands on."""
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.dll = cc.compile_script(cc.linked(PROBE))

    def cs(self, op, items):
        return json.loads(cc.run_script(self.dll, [{"operationId": op, "body": {"items": items}}])[0]["body"])["out"]

    def test_json_dumps_matches_byte_for_byte(self):
        rnd = random.Random(11)
        pieces = ["", "plain", "café ☕", "𝄞 clef", "quote \" and \\ back", "tab\tnew\nline\r", "\x00\x1f\x7f", "é" * 3,
                  "<script>&amp;</script>", "\u2028\u2029", "中文", "emoji 😀👍🏽"]

        def value(depth=0):
            kind = rnd.randrange(8 if depth < 3 else 5)
            if kind == 0:
                return rnd.choice(pieces)
            if kind == 1:
                return rnd.randint(-10**12, 10**12)
            if kind == 2:
                return rnd.choice([True, False, None])
            if kind == 3:
                return rnd.choice([0.1, 1.5, -2.25, 1e16, 1e-5, 123456.789, 0.0, -0.0, 3.0, 2.675, 1e300, 5e-324])
            if kind == 4:
                return rnd.choice(pieces) + str(rnd.randint(0, 9))
            if kind == 5:
                return [value(depth + 1) for _ in range(rnd.randrange(4))]
            return {rnd.choice(pieces) + str(i): value(depth + 1) for i in range(rnd.randrange(4))}
        items = [value() for _ in range(300)] + [[], {}, [[]], {"a": {}}]
        self.assertEqual(self.cs("Dumps", items), py("def f(x): return json.dumps(x, indent=2)", items))
        self.assertEqual(self.cs("DumpsFlat", items), py("def f(x): return json.dumps(x)", items))

    def test_float_repr_is_pythons(self):
        rnd = random.Random(5)
        items = [rnd.uniform(-1e6, 1e6) for _ in range(300)] + [10.0 ** e for e in range(-8, 22)] + \
            [1 / 3, 2 / 3, 0.1 + 0.2, 1e15, 1e16, 9007199254740993.0, 1.7976931348623157e308, 5e-324, -1e-5, 0.0001]
        self.assertEqual(self.cs("Repr", items), py("def f(x): return repr(float(x))", items))

    def test_json_loads_errors_are_python_311s(self):
        bad = ["", "not json", "{\"a\": 1,}", "[1, 2,]", "{\"a\" 1}", "{1: 2}", "[1 2]", "\"open", "\"bad \\q\"",
               "\"ctl \x01\"", "{\"a\": 1} trailing", "[", "{", "nul", "\"\\u12\"", "  ", "[1,]x", "{\"a\":}",
               "{\"x\": [1, {\"y\": tru}]}", "\n\n  {\"a\": 1,\n  }"]
        good = ["{\"a\": [1, 2.5, \"x\", null, true]}", "[]", "{}", "\"\\ud83d\\ude00\"", "123456789012345678901234567890",
                "-0", "1e400", "NaN", "{\"dup\": 1, \"dup\": 2, \"z\": 0}", "  [ 1 , 2 ]  "]
        items = bad + good
        self.assertEqual(self.cs("Loads", items),
                         py("def f(x): return json.dumps(json.loads(x), indent=2)", items))

    def test_int_and_strip_are_pythons(self):
        ints = ["5", " 12 ", "+7", "-3", "1_000", "1__0", "5.0", "abc", "", "0x10", "٣", "12\n", 3, 4.9, -4.9, True, None]
        self.assertEqual(self.cs("Int", ints), py("def f(x): return str(int(x))", ints))
        strips = ["  a  ", "\u3000x\u2003", "\x1cy\x1f", "\u200bz\u200b", "\ta\n", "\x85n\xa0", ""]
        self.assertEqual(self.cs("Strip", strips), py("def f(x): return x.strip()", strips))


@unittest.skipUnless(HAVE_DOTNET, "needs the .NET SDK (dotnet)")
class HarnessTests(unittest.TestCase):
    @unittest.skipUnless(HN.is_file(), "needs a copilot-harness-sdk checkout (HARNESS_SDK_DIR)")
    def test_the_proven_hacker_news_connector_code_runs_on_recorded_responses(self):
        dll = cc.compile_script(HN.read_text())
        top = "https://hacker-news.firebaseio.com/v0/topstories.json"
        item = "https://hacker-news.firebaseio.com/v0/item/{}.json"
        responses = {f"GET {top}": {"status": 200, "body": "[101]"},
                     f"GET {item.format(101)}": {"status": 200, "body": json.dumps(
                         {"id": 101, "title": "Hello", "url": "https://example.com/a", "score": 42, "by": "ada"})}}
        out = cc.run_script(dll, [{"operationId": "GetTopStoriesFormatted", "body": {"count": 1}, "responses": responses},
                                  {"operationId": "GetTopStoriesFormatted", "body": {"count": 1}, "responses": {}}])
        body = json.loads(out[0]["body"])
        self.assertEqual((out[0]["status"], body["status"], body["stories"][0]["title"]), (200, "success", "Hello"))
        self.assertIn("fetch failed", json.loads(out[1]["body"])["message"])       # no recorded response, no network

    def test_a_port_that_does_not_compile_is_refused_with_the_compiler_error(self):
        with self.assertRaisesRegex(cc.ConnectorCodeError, "doesn't compile"):
            cc.compile_script("public class Script : ScriptBase { this is not C# }")


@unittest.skipUnless(HAVE_DOTNET and HAVE_THOUGHTBOX, "needs dotnet and a RAPP_Store checkout (BFS_RAPP_STORE)")
class ThoughtboxTests(unittest.TestCase):
    def test_the_port_matches_the_python_on_every_call(self):
        r = cc.prove(SPEC, THOUGHTBOX / "singleton" / "thoughtbox_agent.py", ROOT / "brainfreeze_studio" / "basic_agent.py",
                     ROOT / "translations" / "thoughtbox.csx", python=python311())
        self.assertTrue(r["parity"], json.dumps(r["mismatches"][:2])[:2000])
        self.assertEqual(r["cases"], sum(len(s) for s in SPEC["sequences"]))

    def test_a_wrong_port_fails_the_gate(self):
        wrong = (ROOT / "translations" / "thoughtbox.csx").read_text().replace('"(no entries)"', '"(nothing yet)"')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrong.csx"
            path.write_text(wrong)
            r = cc.prove(SPEC, THOUGHTBOX / "singleton" / "thoughtbox_agent.py",
                         ROOT / "brainfreeze_studio" / "basic_agent.py", path, python=python311())
        self.assertFalse(r["parity"])
        self.assertTrue(all("(no entries)" in m["python"]["output"] for m in r["mismatches"]))


FORUM = Path(STORE, "apps", "@kody-w", "rapp_god_forum", "singleton", "forum_agent.py") if STORE else None


@unittest.skipUnless(HAVE_DOTNET and FORUM and FORUM.is_file(), "needs dotnet and a RAPP_Store checkout (BFS_RAPP_STORE)")
class ForumTests(unittest.TestCase):
    """A network agent: the code makes its own HTTP calls; the proof replays recorded responses to both sides."""

    def setUp(self):
        self.spec = json.loads((ROOT / "translations" / "forum.json").read_text())

    def prove(self, spec, script=ROOT / "translations" / "forum.csx"):
        return cc.prove(spec, FORUM, ROOT / "brainfreeze_studio" / "basic_agent.py", script, python=python311())

    def test_the_port_matches_the_python_on_every_recorded_response(self):
        r = self.prove(self.spec)
        self.assertTrue(r["parity"], json.dumps(r["mismatches"][:2])[:2000])
        notes = [c.get("note") for c in self.spec["sequences"][0]]
        self.assertIn("live: the forum's host is stopped (403)", notes)

    def test_a_response_the_proof_did_not_record_fails_it(self):
        spec = json.loads(json.dumps(self.spec))
        call = next(c for c in spec["sequences"][0] if c.get("note") == "topics and replies")
        call["responses"] = {k: v for k, v in call["responses"].items() if "neighborhood" in k}
        spec["sequences"] = [[call]]
        self.assertFalse(self.prove(spec)["parity"])

    def test_the_python_really_takes_the_path_without_its_key(self):
        spec = json.loads(json.dumps(self.spec))
        spec["hide_modules"] = []
        spec["sequences"] = [[{"args": {"action": "whoami"}}]]
        r = self.prove(spec)
        try:
            import importlib.util
            have = subprocess.run([python311(), "-c", "import cryptography"], capture_output=True).returncode == 0
        except OSError:
            have = False
        if have:                                  # with the package the Python mints a key; the port can't
            self.assertFalse(r["parity"])


JSON_DOCTOR = Path(STORE, "apps", "@rapp", "json_doctor", "singleton", "json_doctor_agent.py") if STORE else None


@unittest.skipUnless(HAVE_DOTNET and JSON_DOCTOR and JSON_DOCTOR.is_file(),
                     "needs dotnet and a RAPP_Store checkout (BFS_RAPP_STORE)")
class JsonDoctorTests(unittest.TestCase):
    """A files agent: its flow reads the files a call names from SharePoint; the proof gives both sides the same
    bytes (the Python finds them in its working folder, the C# in the request's files)."""

    def setUp(self):
        self.spec = json.loads((ROOT / "translations" / "json_doctor.json").read_text())

    def prove(self, spec, script=ROOT / "translations" / "json_doctor.csx"):
        return cc.prove(spec, JSON_DOCTOR, ROOT / "brainfreeze_studio" / "basic_agent.py", script, python=python311(),
                        records=True)

    def test_the_port_matches_the_python_on_every_file(self):
        r = self.prove(self.spec)
        self.assertTrue(r["parity"], json.dumps(r["mismatches"][:2])[:2000])
        self.assertEqual(r["cases"], 60)
        out = {json.dumps(x["args"], sort_keys=True): json.loads(x["python"]["output"]) for x in r["records"]}

        def answer(**args):
            return out[json.dumps(args, sort_keys=True)]
        users = answer(action="inspect", path="data/users.json")          # it read the file: its shape and size
        self.assertEqual((users["records"], users["bytes"]), (40, 6460))
        self.assertEqual(users["fields"]["legacy"]["coverage"], "12%")     # 5 of 40: Python rounds half to even
        self.assertEqual(answer(action="inspect", path="data/events_crlf.jsonl")["records"], 3)
        self.assertEqual(answer(action="validate", path="data/broken.json")["error"],
                         "Expecting value: line 3 column 21 (char 42)")
        self.assertTrue(answer(action="validate", path="data/bom.json")["error"].startswith("Unexpected UTF-8 BOM"))
        self.assertEqual(answer(action="inspect", path="data/missing.json")["message"], "file not found: data/missing.json")

    def test_a_wrong_port_fails_the_gate(self):
        script = (ROOT / "translations" / "json_doctor.csx").read_text()
        self.assertIn('"file not found: "', script)
        tmp = Path(tempfile.mkdtemp(prefix="bfs-jd-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "wrong.csx").write_text(script.replace('"file not found: "', '"no such file: "'))
        self.assertFalse(self.prove(self.spec, tmp / "wrong.csx")["parity"])


class FlowTests(unittest.TestCase):
    FILES_SPEC = {"agent": "JsonDoctor", "flow_name": "JsonDoctorFlow", "description": "Reads a file.",
                  "inputs": {"action": {"type": "string"}, "path": {"type": "string"}, "other": {"type": "string"}},
                  "required": ["action", "path"], "file_inputs": ["path", "other"],
                  "fixtures": {"data/a.json": {"text": "[1]"}, "b.json": {"base64": "77u/e30="}, "empty.json": {"text": ""}}}

    def files_flow(self, **home):
        return cc.compile_flow(self.FILES_SPEC, "rapp_JsonDoctor", "JSON Doctor", "JSONDoctor JsonDoctor code",
                               "shared_rapp_code_x", files_home=home or None)

    def test_a_files_flow_reads_each_named_file_from_sharepoint_and_hands_them_to_the_code(self):
        flow = self.files_flow(site="https://contoso.sharepoint.com/sites/team")
        d = flow["properties"]["definition"]
        self.assertEqual(list(d["actions"]), ["Read_file_path", "Read_file_other", "Run_the_agent", "Respond_to_agent"])
        get = d["actions"]["Read_file_path"]["actions"]["Get_file_path"]["inputs"]
        self.assertEqual(get["host"]["operationId"], "GetFileContentByPath")
        self.assertIs(get["parameters"]["inferContentType"], False)          # the bytes, never a parsed body
        self.assertEqual(get["parameters"]["dataset"], "@parameters('RAPP Files Site (rapp_RappFilesSite)')")
        # a file that isn't there fails its read; the flow still runs the code, which says so
        self.assertEqual(d["actions"]["Read_file_other"]["runAfter"], {"Read_file_path": ["Succeeded", "Failed", "TimedOut"]})
        self.assertEqual(d["actions"]["Run_the_agent"]["runAfter"], {"Read_file_other": ["Succeeded", "Failed", "TimedOut"]})
        sent = d["actions"]["Run_the_agent"]["inputs"]["parameters"]["body/files"]
        self.assertEqual(sorted(sent), ["other", "path"])                   # fixed keys: a path can't be one
        self.assertEqual(sent["path"]["path"], "@triggerBody()?['path']")
        self.assertNotIn("setProperty", json.dumps(flow))
        site = d["parameters"]["RAPP Files Site (rapp_RappFilesSite)"]
        folder = d["parameters"]["RAPP Files Folder (rapp_RappFilesFolder)"]
        self.assertEqual((site["defaultValue"], site["metadata"]["schemaName"]),
                         ("https://contoso.sharepoint.com/sites/team", "rapp_RappFilesSite"))
        self.assertEqual(folder["defaultValue"], "/Shared Documents")
        self.assertEqual(flow["properties"]["connectionReferences"]["shared_sharepointonline"]["connection"],
                         {"connectionReferenceLogicalName": "rapp_JsonDoctor.shared_sharepointonline"})
        body = cc.openapi(self.FILES_SPEC, "x")["paths"]["/run"]["post"]["parameters"][0]["schema"]["properties"]
        self.assertIn("files", body)
        self.assertNotIn("files", cc.openapi(SPEC, "x")["paths"]["/run"]["post"]["parameters"][0]["schema"]["properties"])
        self.assertNotIn("shared_sharepointonline", cc.compile_flow(SPEC, "rapp_T", "T", "T code", "x")["properties"]
                         ["connectionReferences"])

    def test_the_flows_own_expressions_hand_the_code_what_the_proof_hands_it(self):
        from codeapp_harness import files_sent
        flow = self.files_flow()
        library = {k: cc.fixture_bytes(v) for k, v in self.FILES_SPEC["fixtures"].items()}
        cases = [{"action": "a", "path": "data/a.json"}, {"action": "a", "path": "b.json", "other": "data/a.json"},
                 {"action": "a", "path": "missing.json"}, {"action": "a", "path": ""}, {"action": "a"},
                 {"action": "a", "path": "data/a.json", "other": "data/a.json"},
                 {"action": "a", "path": "../data/a.json"}, {"action": "a", "path": "/data/a.json"},
                 {"action": "a", "path": "data/../data/a.json"}, {"action": "a", "path": "\\data\\a.json"},
                 {"action": "a", "path": "empty.json"}]
        for args in cases:
            self.assertEqual(files_sent(flow, args, library), cc.files_body(self.FILES_SPEC, args), args)
        self.assertEqual(cc.files_body(self.FILES_SPEC, cases[1]), {"path": {"path": "b.json", "content": "77u/e30="},
                                                                   "other": {"path": "data/a.json", "content": "WzFd"}})
        self.assertEqual(cc.files_body(self.FILES_SPEC, cases[2])["path"], {"path": "missing.json", "content": None})
        self.assertEqual(cc.files_body(self.FILES_SPEC, cases[-1])["path"], {"path": "empty.json", "content": ""})
        self.assertEqual(cc.files_body(self.FILES_SPEC, cases[4]), {"path": {"path": None, "content": None},
                                                                   "other": {"path": None, "content": None}})

    def test_only_paths_inside_the_folder_are_read(self):
        for path, inside in [("data/a.json", True), ("a.json", True), ("x/./a.json", True), ("..a.json", True),
                             ("../a.json", False), ("/a.json", False), ("x/../../a.json", False),
                             ("\\srv\\a.json", False), ("x\\..\\a.json", False)]:
            self.assertEqual(cc.library_path(path), inside, path)

    def test_the_flow_reads_runs_and_saves_the_workspace(self):
        flow = cc.compile_flow(SPEC, "rapp_Thoughtbox", "Thoughtbox", "Thoughtbox Thoughtbox code", "shared_rapp_code_x")
        d = flow["properties"]["definition"]
        self.assertEqual(list(d["actions"]), ["Read_entries_json", "Run_the_agent", "Save_entries_json", "Respond_to_agent"])
        read = d["actions"]["Read_entries_json"]["inputs"]
        self.assertEqual((read["host"]["operationId"], read["parameters"]["entityName"], read["parameters"]["$filter"]),
                         ("ListRecords", "annotations", "subject eq 'rapp-workspace/rapp_Thoughtbox/entries.json'"))
        run = d["actions"]["Run_the_agent"]["inputs"]
        self.assertEqual(run["host"]["apiId"], "/providers/Microsoft.PowerApps/apis/{{CONNECTOR:Thoughtbox Thoughtbox code}}")
        self.assertEqual(sorted(run["parameters"]["body/args"]), sorted(SPEC["inputs"]))
        self.assertEqual((run["parameters"]["body/now"], run["parameters"]["body/id"]),
                         ("@utcNow('yyyy-MM-ddTHH:mm:ssZ')", "@guid()"))
        save = d["actions"]["Save_entries_json"]
        keep = save["actions"]["Keep_entries_json"]
        self.assertEqual(keep["actions"]["Add_entries_json"]["inputs"]["host"]["operationId"], "CreateRecord")
        self.assertEqual(keep["else"]["actions"]["Update_entries_json"]["inputs"]["host"]["operationId"], "UpdateRecord")
        self.assertEqual(d["actions"]["Respond_to_agent"]["inputs"]["body"], {"result": "@body('Run_the_agent')?['output']"})
        refs = flow["properties"]["connectionReferences"]
        self.assertEqual(sorted(refs), ["shared_commondataserviceforapps", "shared_rapp_code"])
        filled = cc.fill_connectors(flow, {"Thoughtbox Thoughtbox code": "shared_rapp-5fx-5f123"})
        self.assertEqual(filled["properties"]["definition"]["actions"]["Run_the_agent"]["inputs"]["host"]["apiId"],
                         "/providers/Microsoft.PowerApps/apis/shared_rapp-5fx-5f123")
        self.assertNotIn("{{CONNECTOR", json.dumps(filled))


@unittest.skipUnless(HAVE_DOTNET and HAVE_THOUGHTBOX, "needs dotnet and a RAPP_Store checkout (BFS_RAPP_STORE)")
class BuildAndDeployTests(unittest.TestCase):
    def test_build_lays_the_connector_and_deploy_creates_it_connects_it_and_fills_the_flows(self):
        from test_codeapp import ENV_ID, TOKEN, FakePowerApps
        from test_deploy import ENV, FakeDataverse

        class Dataverse(FakeDataverse):
            def __init__(self):
                super().__init__()
                self.t["connectors"] = {}

            def __call__(self, method, path, body=None, prefer=None, headers=None, ok404=False):
                if path.startswith("RetrieveCurrentOrganization"):
                    return {"Detail": {"EnvironmentId": ENV_ID}}, {}
                if path.split("?")[0] == "connectors" and method == "POST":
                    row = dict(body, connectorid="c-1", connectorinternalid="shared_rapp-5fthoughtbox-5fabc")
                    self.t["connectors"]["c-1"] = row
                    self.writes.append(("POST", "connectors"))
                    return ({"connectorid": "c-1", "connectorinternalid": row["connectorinternalid"]}, {})
                return super().__call__(method, path, body, prefer, headers, ok404)

        class PowerApps(FakePowerApps):
            def __call__(self, req, timeout=None):
                import io
                import urllib.parse
                url = urllib.parse.urlsplit(req.full_url)
                path = url.path.split("/providers/Microsoft.PowerApps/", 1)[-1]
                if "/connections" in path and path.startswith("apis/"):
                    self.log.append((req.get_method(), url.netloc, url.path, {}))
                    if req.get_method() == "GET":
                        conns = [{"name": n, "properties": {"statuses": [{"status": "Connected"}]}} for n in self.conns]
                        return _R(200, json.dumps({"value": conns}).encode())
                    self.conns.append(path.rsplit("/", 1)[-1])
                    return _R(201, json.dumps({"name": self.conns[-1]}).encode())
                return super().__call__(req, timeout)

        from test_codeapp import _Response as _R
        tmp = Path(tempfile.mkdtemp(prefix="bfs-code-deploy-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        host = tmp / "host.js"
        host.write_bytes(b"/* host */")
        out = tmp / "out"
        s = rapplication.prepare("@kody-w/thoughtbox", out, store=STORE, translations=str(ROOT / "translations"),
                                 host_js=host, fetch_vendor=None)
        self.assertEqual(s["agent"]["agents"][0]["as"], "agent flow + connector code (ported, parity proven)")
        conn_dir = out / "workspace" / "connectors" / "ThoughtboxFlow"
        self.assertIn("public static class PyJson", (conn_dir / "script.csx").read_text())      # linked in
        dv, rp = Dataverse(), PowerApps()
        rp.conns = []
        r = rapplication.deploy(out, ENV, lambda: "dv", lambda: TOKEN, log=lambda *a: None, dataverse=dv, opener=rp)
        self.assertEqual([c["operation"] for c in r["agent"]["connectors"]], ["created"])
        self.assertEqual(r["agent"]["connectors"][0]["connectionOperation"], "created")
        flow = dv.t["workflows"][bs.workflow_id_for("rapp_Thoughtbox", "ThoughtboxFlow")]
        self.assertIn("shared_rapp-5fthoughtbox-5fabc", flow["clientdata"])
        self.assertNotIn("{{CONNECTOR", flow["clientdata"])
        twin = dv.t["workflows"][s["powerapps_flows"][0]["id"]]
        self.assertIn('"kind":"PowerAppV2"', twin["clientdata"])
        self.assertNotIn("{{CONNECTOR", twin["clientdata"])
        refs = {x["connectionreferencelogicalname"]: x for x in dv.t["connectionreferences"].values()}
        self.assertEqual(refs["rapp_Thoughtbox.shared_rapp_code_thoughtboxflow"]["connectionid"], rp.conns[0])
        self.assertEqual(refs["rapp_Thoughtbox.shared_commondataserviceforapps"]["connectionid"], "conn-1")   # the user's own
        again = rapplication.deploy(out, ENV, lambda: "dv", lambda: TOKEN, log=lambda *a: None, dataverse=dv, opener=rp)
        self.assertEqual([(c["operation"], c["connectionOperation"]) for c in again["agent"]["connectors"]],
                         [("unchanged", "existing")])
        self.assertEqual(len(rp.conns), 1)


if __name__ == "__main__":
    unittest.main()
