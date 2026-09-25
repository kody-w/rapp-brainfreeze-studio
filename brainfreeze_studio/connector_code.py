"""Agent logic as custom connector code (C#), proven against the agent's real Python before anything deploys.

Some agents do more than flow expressions can say: they parse, search, keep state or call APIs. That logic ports to a
custom connector's code (`script.csx`: C# on .NET Standard 2.0, with Newtonsoft JSON and Regex), the way the proven
Hacker News connector does it. The port is gated like every translation: the same cases run through the real Python
and through the compiled C#, and every output must match byte for byte.

    from brainfreeze_studio import connector_code
    report = connector_code.prove(spec, agent_file, basic_file, script_file)

The contract between a flow and the code (one operation, `Run`, POST):

    request  {"args": {...the tool's inputs...}, "state": {"<file>": text | null}, "now": "2026-09-25T10:00:00Z",
              "id": "<a fresh guid>"}
    response {"output": "<what perform() returns>", "state": {"<file>": "<new text>"}}   (only the files it wrote)

`state` carries the files an agent keeps through the RAPP workspace contract (`workspace_read`, `workspace_write`); the
flow reads and writes them as Dataverse notes. `now` and `id` are the flow's `utcNow()` and `guid()`, so the code is a
pure function of its request and a proof can fix them. A proof case is a sequence of calls: state carries from one call
to the next, in both runs. Compiling needs the .NET SDK (`dotnet`); the first build restores Newtonsoft.Json from
NuGet.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HARNESS_ROOT = Path("~/.cache/brainfreeze-studio/connector-harness").expanduser()
NEWTONSOFT = "13.0.3"

# The connector runtime's surface, as Microsoft documents it (custom connector code: ScriptBase, IScriptContext), and
# the namespaces a script may use, imported as the runtime imports them. Anything else (System.Globalization,
# System.IO) must be written out in full: the runtime refused CultureInfo on 25 Sep 2026.
USINGS = """using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Dynamic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web;
using System.Xml;
using System.Xml.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
"""

STUBS = USINGS + r"""
public interface IScriptContext
{
    string CorrelationId { get; }
    string OperationId { get; }
    HttpRequestMessage Request { get; }
    Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken);
}

public abstract class ScriptBase
{
    public IScriptContext Context { get; set; }
    public CancellationToken CancellationToken { get; set; }
    public static StringContent CreateJsonContent(string serializedJson)
    {
        return new StringContent(serializedJson ?? string.Empty, Encoding.UTF8, "application/json");
    }
    public abstract Task<HttpResponseMessage> ExecuteAsync();
}
"""

PROGRAM = USINGS + r"""
// One case per stdin line: {"operationId", "body": <request JSON>, "responses": {"GET <url>": {"status", "body"}}}.
// Writes {"status", "body"} per case. SendAsync answers only from the recorded responses: a proof has no network.
class ProofContext : IScriptContext
{
    public string CorrelationId { get; set; } = "proof";
    public string OperationId { get; set; }
    public HttpRequestMessage Request { get; set; }
    public JObject Responses { get; set; } = new JObject();
    public Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var key = request.Method.Method + " " + request.RequestUri.AbsoluteUri;
        var recorded = Responses[key] as JObject;
        if (recorded == null) throw new HttpRequestException("no recorded response for " + key);
        var status = (int)recorded["status"];
        var response = new HttpResponseMessage((HttpStatusCode)status);
        response.ReasonPhrase = (string)recorded["reason"] ?? response.ReasonPhrase;
        response.Content = new StringContent((string)recorded["body"] ?? "", Encoding.UTF8,
                                             (string)recorded["contentType"] ?? "application/json");
        response.RequestMessage = request;
        return Task.FromResult(response);
    }
}

public static class Program
{
    public static async Task Main()
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        var stdin = new System.IO.StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
        string line;
        while ((line = await stdin.ReadLineAsync()) != null)
        {
            if (line.Trim().Length == 0) continue;
            JObject c;
            using (var reader = new JsonTextReader(new System.IO.StringReader(line)) { DateParseHandling = DateParseHandling.None })
                c = (JObject)JToken.ReadFrom(reader);
            var context = new ProofContext { OperationId = (string)c["operationId"],
                                             Responses = (c["responses"] as JObject) ?? new JObject() };
            var request = new HttpRequestMessage(HttpMethod.Post, "https://connector.proof/" + context.OperationId);
            request.Content = new StringContent(c["body"].ToString(Newtonsoft.Json.Formatting.None), Encoding.UTF8, "application/json");
            context.Request = request;
            var script = new Script { Context = context, CancellationToken = CancellationToken.None };
            JObject result;
            try
            {
                var response = await script.ExecuteAsync();
                var body = response.Content == null ? "" : await response.Content.ReadAsStringAsync();
                result = new JObject { ["status"] = (int)response.StatusCode, ["body"] = body };
            }
            catch (Exception e)
            {
                result = new JObject { ["status"] = -1, ["body"] = e.GetType().Name + ": " + e.Message };
            }
            Console.WriteLine(result.ToString(Newtonsoft.Json.Formatting.None));
        }
    }
}
"""

PROJECT = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net{{framework}}</TargetFramework>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <LangVersion>latest</LangVersion>
    <InvariantGlobalization>true</InvariantGlobalization>
    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>
    <NoWarn>CS1998;CS0168;CS0219</NoWarn>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="{NEWTONSOFT}" />
  </ItemGroup>
</Project>
"""


class ConnectorCodeError(RuntimeError):
    pass


def _framework():
    """The newest .NET the local SDK can target (net8.0 or later)."""
    try:
        out = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise ConnectorCodeError("proving connector code needs the .NET SDK (dotnet) on PATH") from None
    majors = sorted({int(line.split(".")[0]) for line in out.splitlines() if line[:1].isdigit()})
    if not majors or majors[-1] < 8:
        raise ConnectorCodeError("proving connector code needs the .NET 8 SDK or later")
    return f"{majors[-1]}.0"


def compile_script(script):
    """Compile script.csx (text) with the runtime stubs into a runner; cached by content. Returns the runner's dll."""
    framework = _framework()
    digest = hashlib.sha256((script + STUBS + PROGRAM + framework + NEWTONSOFT).encode("utf-8")).hexdigest()[:16]
    build = HARNESS_ROOT / digest
    dll = build / "out" / "ConnectorProof.dll"
    if dll.is_file():
        return dll
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    (build / "ConnectorProof.csproj").write_text(PROJECT.replace("{framework}", framework))
    (build / "Stubs.cs").write_text(STUBS)
    (build / "Program.cs").write_text(PROGRAM)
    own = [line for line in script.splitlines() if line.lstrip().startswith("using ") and line.rstrip().endswith(";")]
    body = "\n".join(line for line in script.splitlines() if line not in own)
    (build / "Script.cs").write_text(USINGS + "\n".join(u for u in own if u.strip() not in USINGS) + "\n" + body + "\n")
    p = subprocess.run(["dotnet", "build", "-c", "Release", "-o", "out", "-nologo", "-v", "quiet"], cwd=build,
                       capture_output=True, text=True)
    if p.returncode != 0 or not dll.is_file():
        errors = [line for line in (p.stdout + p.stderr).splitlines() if "error" in line]
        shutil.rmtree(build, ignore_errors=True)
        raise ConnectorCodeError("the connector code doesn't compile:\n" + "\n".join(errors[:12]))
    return dll


def run_script(dll, cases):
    """Run cases [{"operationId", "body", "responses"?}] through the compiled code; returns [{"status", "body"}]."""
    stdin = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)
    p = subprocess.run(["dotnet", str(dll)], input=stdin, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise ConnectorCodeError(f"the connector code runner failed: {p.stderr.strip()[-600:]}")
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


# ── the Python side: the real agent, with its workspace, clock and ids fixed per call ───────────────────────────

PY_RUNNER = r'''
import datetime as _dt, importlib.util, json, sys, types, uuid

agent_file, basic_file = sys.argv[1:3]
_now = [None]
_ids = []

class _Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        t = _dt.datetime.strptime(_now[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        return t if tz is not None else t.replace(tzinfo=None)
    @classmethod
    def utcnow(cls):
        return cls.now().replace(tzinfo=None)
_dt.datetime = _Frozen

def _uuid4():
    return uuid.UUID(_ids.pop(0))
uuid.uuid4 = _uuid4

spec = importlib.util.spec_from_file_location("basic_agent", basic_file)
basic = importlib.util.module_from_spec(spec); spec.loader.exec_module(basic)
pkg = types.ModuleType("agents"); pkg.basic_agent = basic
sys.modules["agents"] = pkg; sys.modules["agents.basic_agent"] = basic; sys.modules["basic_agent"] = basic
spec = importlib.util.spec_from_file_location("agent_under_proof", agent_file)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
cls = next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, basic.BasicAgent)
           and v is not basic.BasicAgent and v.__module__ == mod.__name__)
agent = cls()

for line in sys.stdin:
    case = json.loads(line)
    files = dict(case.get("state") or {})
    written = {}
    def workspace_read(key):
        return files.get(key)
    def workspace_write(key, text):
        files[key] = text
        written[key] = text
    _now[0] = case["now"]
    _ids[:] = case["ids"]
    args = dict(case["args"])
    if case.get("workspace"):
        args["_context"] = {"workspace_read": workspace_read, "workspace_write": workspace_write}
    try:
        out = agent.perform(**args)
        out = out if isinstance(out, str) else json.dumps(out)
    except Exception as e:
        out = f"{type(e).__name__}: {e}"
    print(json.dumps({"output": out, "state": written}), flush=True)
'''


def derived_ids(base, count):
    """The ids a call may mint: the flow's guid(), then the same guid counting up in its last 12 hex digits. The C#
    derives them the same way."""
    head, tail = base[:-12], int(base[-12:], 16)
    return [base] + [f"{head}{(tail + k) % (1 << 48):012x}" for k in range(1, count)]


class _Session:
    """A long-running process that answers one JSON line with one JSON line."""

    def __init__(self, cmd, **kw):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", bufsize=1, **kw)

    def ask(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise ConnectorCodeError(f"{self.p.args[0]} stopped: {self.p.stderr.read()[-600:]}")
        return json.loads(line)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


LIBRARY = Path(__file__).with_name("connector_lib") / "PyCompat.cs"


def linked(script_text):
    """The script as it deploys: the port, then the PyCompat library it builds on (one file, as connector code must
    be). The library's own using lines go to the top."""
    lib = LIBRARY.read_text(encoding="utf-8")
    return script_text.rstrip() + "\n\n" + lib


def prove(spec, agent_file, basic_file, script_file, python=None):
    """Run every sequence of the spec through the real Python and the compiled C#; each carries its own state from
    call to call. Parity means every output and every file written is identical."""
    script = linked(Path(script_file).read_text(encoding="utf-8"))
    dll = compile_script(script)
    workspace = bool((spec.get("state") or {}).get("files"))
    tmp = tempfile.mkdtemp(prefix="bfs-connector-proof-")
    runner = Path(tmp) / "runner.py"
    runner.write_text(PY_RUNNER)
    py = _Session([python or sys.executable, str(runner), str(agent_file), str(basic_file)],
                  env={**os.environ, "PYTHONHASHSEED": "0"})
    cs = _Session(["dotnet", str(dll)])
    results = []
    try:
        for n, sequence in enumerate(spec["sequences"]):
            initial = ((spec.get("initial_states") or [])[n:n + 1] or [None])[0] or spec.get("initial_state") or {}
            py_state, cs_state = dict(initial), dict(initial)
            for k, call in enumerate(sequence):
                now = call.get("now") or spec.get("now") or "2026-09-25T10:00:00Z"
                ident = call.get("id") or f"00000000-0000-4000-8000-{n:04x}{k:08x}"
                out_py = py.ask({"args": call["args"], "state": py_state, "now": now, "ids": derived_ids(ident, 64),
                                 "workspace": workspace})
                raw = cs.ask({"operationId": spec.get("operation", "Run"), "responses": call.get("responses") or {},
                              "body": {"args": call["args"], "state": cs_state, "now": now, "id": ident}})
                try:
                    out_cs = json.loads(raw["body"]) if raw["status"] == 200 else {
                        "output": f"HTTP {raw['status']}: {raw['body']}", "state": {}}
                except ValueError:
                    out_cs = {"output": f"not JSON: {raw['body'][:300]}", "state": {}}
                match = out_py["output"] == out_cs.get("output") and out_py["state"] == (out_cs.get("state") or {})
                results.append({"sequence": n, "call": k, "args": call["args"], "python": out_py, "code": out_cs,
                                "match": match})
                py_state.update(out_py["state"])
                cs_state.update(out_cs.get("state") or {})
    finally:
        py.close()
        cs.close()
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(r["match"] for r in results)
    return {"agent": spec["agent"], "mode": "connector-code", "cases": len(results), "passed": passed,
            "parity": passed == len(results) and bool(results), "sequences": len(spec["sequences"]),
            "mismatches": [r for r in results if not r["match"]][:10],
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest()}


# ── what a proven port becomes: a custom connector, and the flow that runs it with the agent's state ─────────────

STATE_PREFIX = "rapp-workspace"
CONNECTOR_PLACEHOLDER = "{{CONNECTOR:%s}}"          # the connector's internal id, known once it exists


def connector_name(schema_name, spec):
    """The connector's display name (its internal id derives from it) and its Dataverse name."""
    display = f"{schema_name.split('_', 1)[-1]} {spec['agent']} code"[:60]
    return display, f"{schema_name.split('_', 1)[0]}_{re.sub(r'[^a-z0-9]', '', (schema_name.split('_', 1)[-1] + spec['agent']).lower())}code"[:60]


def openapi(spec, display):
    """The connector's definition: one operation, Run, answered by its code (the host is never called)."""
    body = {"type": "object", "properties": {
        "args": {"type": "object", "description": "The tool's inputs"},
        "state": {"type": "object", "description": "The agent's workspace files: name → text (null when absent)"},
        "now": {"type": "string", "description": "The time of the call, yyyy-MM-ddTHH:mm:ssZ"},
        "id": {"type": "string", "description": "A fresh guid for anything the call creates"}}}
    reply = {"type": "object", "properties": {"output": {"type": "string", "description": "What the agent returned"},
                                               "state": {"type": "object", "description": "The files it wrote"}}}
    return {"swagger": "2.0",
            "info": {"title": display, "version": "1.0",
                     "description": f"Runs the {spec['agent']} RAPP agent's logic, ported to connector code and proven "
                                    "byte for byte against its Python (brainfreeze-studio)."},
            "host": "example.com", "basePath": "/", "schemes": ["https"],
            "consumes": ["application/json"], "produces": ["application/json"],
            "paths": {"/run": {"post": {"operationId": "Run", "summary": f"Run {spec['agent']}",
                                        "description": (spec.get("description") or "")[:500],
                                        "parameters": [{"name": "body", "in": "body", "required": True, "schema": body}],
                                        "responses": {"200": {"description": "The agent's output", "schema": reply}}}}},
            "securityDefinitions": {}, "security": []}


def api_properties():
    return {"properties": {"connectionParameters": {}, "iconBrandColor": "#5a4fcf", "capabilities": [],
                           "scriptOperations": ["Run"], "publisher": "RAPP", "stackOwner": "RAPP"}}


def _action_name(prefix, file):
    return prefix + "_" + re.sub(r"[^A-Za-z0-9]", "_", file)


def state_subject(schema_name, file):
    return f"{STATE_PREFIX}/{schema_name}/{file}"


def compile_flow(spec, schema_name, display_name, connector_display, connector_logical):
    """The agent flow for a connector-code tool: read the workspace files (Dataverse notes), run the code with the
    tool's inputs, write back the files it changed, return its output. `{{CONNECTOR:...}}` is the connector's
    internal id, filled in at deploy."""
    from .flows import _within_limits
    placeholder = CONNECTOR_PLACEHOLDER % connector_display
    files = list((spec.get("state") or {}).get("files") or [])
    dataverse = {"apiId": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps",
                 "connectionName": "shared_commondataserviceforapps"}
    auth = "@parameters('$authentication')"
    actions, last = {}, None
    for f in files:
        name = _action_name("Read", f)
        actions[name] = {"type": "OpenApiConnection", "runAfter": {last: ["Succeeded"]} if last else {},
                         "inputs": {"host": {**dataverse, "operationId": "ListRecords"},
                                    "parameters": {"entityName": "annotations", "$select": "annotationid,documentbody",
                                                   "$filter": f"subject eq '{state_subject(schema_name, f)}'",
                                                   "$top": 1},
                                    "authentication": auth}}
        last = name
    state = {f: (f"@if(empty(outputs('{_action_name('Read', f)}')?['body/value']), null, "
                 f"base64ToString(first(outputs('{_action_name('Read', f)}')?['body/value'])?['documentbody']))")
             for f in files}
    args = {k: f"@triggerBody()?['{k}']" for k in spec["inputs"]}
    actions["Run_the_agent"] = {"type": "OpenApiConnection", "runAfter": {last: ["Succeeded"]} if last else {},
                                "inputs": {"host": {"apiId": f"/providers/Microsoft.PowerApps/apis/{placeholder}",
                                                    "connectionName": "shared_rapp_code", "operationId": "Run"},
                                           "parameters": {"body/args": args, "body/state": state,
                                                          "body/now": "@utcNow('yyyy-MM-ddTHH:mm:ssZ')",
                                                          "body/id": "@guid()"},
                                           "authentication": auth}}
    last = "Run_the_agent"
    for f in files:
        written = f"body('Run_the_agent')?['state']?['{f}']"
        read = f"outputs('{_action_name('Read', f)}')?['body/value']"
        name = _action_name("Save", f)
        actions[name] = {
            "type": "If", "runAfter": {last: ["Succeeded"]},
            "expression": {"and": [{"not": {"equals": [f"@{written}", "@null"]}}]},
            "actions": {_action_name("Keep", f): {
                "type": "If", "runAfter": {}, "expression": {"and": [{"equals": [f"@empty({read})", "@true"]}]},
                "actions": {_action_name("Add", f): {
                    "type": "OpenApiConnection", "runAfter": {},
                    "inputs": {"host": {**dataverse, "operationId": "CreateRecord"},
                               "parameters": {"entityName": "annotations",
                                              "item/subject": state_subject(schema_name, f),
                                              "item/filename": f, "item/mimetype": "application/json",
                                              "item/isdocument": True,
                                              "item/notetext": f"The {display_name} agent's workspace file {f} (RAPP).",
                                              "item/documentbody": f"@base64({written})"},
                               "authentication": auth}}},
                "else": {"actions": {_action_name("Update", f): {
                    "type": "OpenApiConnection", "runAfter": {},
                    "inputs": {"host": {**dataverse, "operationId": "UpdateRecord"},
                               "parameters": {"entityName": "annotations",
                                              "recordId": f"@first({read})?['annotationid']",
                                              "item/documentbody": f"@base64({written})"},
                               "authentication": auth}}}}}},
            "else": {"actions": {}}}
        last = name
    props = {}
    for name, meta in spec["inputs"].items():
        p_ = {"title": name, "type": "string", "description": meta.get("description", ""), "x-ms-dynamically-added": True}
        if meta.get("type") == "number":
            p_["x-ms-content-hint"] = "NUMBER"
        props[name] = p_
    actions["Respond_to_agent"] = {
        "type": "Response", "kind": "Skills", "runAfter": {last: ["Succeeded"]},
        "inputs": {"statusCode": 200, "body": {"result": "@body('Run_the_agent')?['output']"},
                   "schema": {"type": "object", "properties": {"result": {"type": "string"}}}}}
    refs = {"shared_rapp_code": {"api": {"name": placeholder}, "runtimeSource": "embedded",
                                 "connection": {"connectionReferenceLogicalName": f"{schema_name}.{connector_logical}"}}}
    if files:
        refs["shared_commondataserviceforapps"] = {
            "api": {"name": "shared_commondataserviceforapps"}, "runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": f"{schema_name}.shared_commondataserviceforapps"}}
    return _within_limits({"properties": {"connectionReferences": refs, "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                       "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
        "triggers": {"manual": {"type": "Request", "kind": "Skills", "inputs": {"schema": {
            "type": "object", "properties": props, "required": list(spec.get("required", []))}}}},
        "actions": actions, "outputs": {}}, "templateName": ""}, "schemaVersion": "1.0.0.0"})


def fill_connectors(definition, internal_ids):
    """The flow with each connector placeholder replaced by the connector's internal id."""
    text = json.dumps(definition)
    for display, internal in internal_ids.items():
        text = text.replace(CONNECTOR_PLACEHOLDER % display, internal)
    return json.loads(text)
