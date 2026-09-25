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
flow reads and writes them as Dataverse notes. An agent that calls the network does it from the code itself
(`this.Context.SendAsync`, as the Hacker News connector does); its proof replays recorded responses to both sides,
the Python's `urllib.request.urlopen` included. `now` and `id` are the flow's `utcNow()` and `guid()`, so the code is a
pure function of its request and a proof can fix them. A proof case is a sequence of calls: state carries from one call
to the next, in both runs. Compiling needs the .NET SDK (`dotnet`); the first build restores Newtonsoft.Json from
NuGet.
"""
import base64
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
        var mediaType = ((string)recorded["contentType"] ?? "application/json").Split(';')[0].Trim();
        response.Content = new StringContent((string)recorded["body"] ?? "", Encoding.UTF8, mediaType);
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


_CAN_PROVE = {}


def can_prove():
    """Whether connector code can be proven here: a .NET 8 (or later) SDK on PATH. A runtime alone isn't enough
    (the Azure Functions host is .NET, so `dotnet` can be there with no SDK)."""
    if "ok" not in _CAN_PROVE:
        try:
            _framework()
            _CAN_PROVE["ok"] = True
        except ConnectorCodeError:
            _CAN_PROVE["ok"] = False
    return _CAN_PROVE["ok"]


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
    # a script's own using directives (not using statements inside its methods)
    own = [line for line in script.splitlines() if re.match(r"^using\s+[A-Za-z_][\w.]*(\s*=\s*[\w.]+)?\s*;\s*$", line)]
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
import datetime as _dt, importlib.abc, importlib.util, io, json, sys, types, urllib.error, urllib.request, uuid

agent_file, basic_file = sys.argv[1:3]
hidden = set(json.loads(sys.argv[3])) if len(sys.argv) > 3 else set()
_now = [None]
_ids = []
_responses = {}
_fetched = []

# Modules the proof hides, so the agent takes the path it takes without them (the connector has no keys to sign with)
class _Hide(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in hidden:
            raise ImportError(f"No module named {name!r}")
        return None
sys.meta_path.insert(0, _Hide())

# The network, replayed: every request must have a recorded response, as the connector code's proof does it
class _Recorded(io.BytesIO):
    def __init__(self, url, rec):
        super().__init__(rec["body"].encode("utf-8"))
        self.url, self.status, self.reason, self.code = url, rec["status"], rec.get("reason") or "", rec["status"]
        self.headers = {"Content-Type": rec.get("contentType") or "application/json"}
    def getcode(self):
        return self.status
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def _urlopen(req, data=None, timeout=None, **kw):
    url = req if isinstance(req, str) else req.full_url
    method = "GET" if isinstance(req, str) else req.get_method()
    key = f"{method} {url}"
    _fetched.append(key)
    rec = _responses.get(key)
    if rec is None:
        raise RuntimeError(f"the proof has no recorded response for {key}")
    if rec["status"] >= 400:
        raise urllib.error.HTTPError(url, rec["status"], rec.get("reason") or "", rec.get("headers") or {},
                                     io.BytesIO(rec["body"].encode("utf-8")))
    return _Recorded(url, rec)
urllib.request.urlopen = _urlopen

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

import base64 as _b64, os as _os, shutil as _shutil, tempfile as _tempfile
_home = _os.getcwd()

for line in sys.stdin:
    case = json.loads(line)
    # the files the call names, as the flow read them from the user's library: a fresh folder holding just those
    _library = _tempfile.mkdtemp(prefix="bfs-library-")
    for _rel, _data in (case.get("library") or {}).items():
        if _data is None:
            continue
        if _os.path.isabs(_rel) or ".." in _rel.replace("\\", "/").split("/"):
            raise SystemExit(f"fixture path must stay inside the library: {_rel}")
        _target = _os.path.join(_library, _rel)
        _os.makedirs(_os.path.dirname(_target) or _library, exist_ok=True)
        with open(_target, "wb") as _f:
            _f.write(_b64.b64decode(_data))
    _os.chdir(_library)
    files = dict(case.get("state") or {})
    written = {}
    def workspace_read(key):
        return files.get(key)
    def workspace_write(key, text):
        files[key] = text
        written[key] = text
    _now[0] = case["now"]
    _ids[:] = case["ids"]
    _responses.clear()
    _responses.update(case.get("responses") or {})
    args = dict(case["args"])
    if case.get("workspace"):
        args["_context"] = {"workspace_read": workspace_read, "workspace_write": workspace_write}
    try:
        out = agent.perform(**args)
        out = out if isinstance(out, str) else json.dumps(out)
    except Exception as e:
        out = f"{type(e).__name__}: {e}"
    finally:
        _os.chdir(_home)
        _shutil.rmtree(_library, ignore_errors=True)
    print(json.dumps({"output": out, "state": written}), flush=True)
'''


def derived_ids(base, count):
    """The ids a call may mint: the flow's guid(), then the same guid counting up in its last 12 hex digits. The C#
    derives them the same way."""
    head, tail = base[:-12], int(base[-12:], 16)
    return [base] + [f"{head}{(tail + k) % (1 << 48):012x}" for k in range(1, count)]


def fixture_bytes(fixture):
    return base64.b64decode(fixture["base64"]) if "base64" in fixture else fixture["text"].encode("utf-8")


def library_path(path):
    """Whether the flow reads this path from the library: a relative path that stays inside the folder (no leading
    slash, no `..` step). Anything else reaches the code as a file that isn't there."""
    p = path.replace("\\", "/")
    return not p.startswith("/") and ".." not in p.split("/")


def files_body(spec, args):
    """The files member of the request the flow sends: each file input as {path, content}, the path the call named
    (or null) and the file's bytes as base64 (null when there is no such file, or it isn't inside the folder)."""
    named = named_files(spec, args)
    return {name: {"path": args.get(name), "content": named.get(args.get(name)) if isinstance(args.get(name), str) else None}
            for name in spec.get("file_inputs") or []}


def named_files(spec, args):
    """The files a call names ({path: base64 | None}), as its flow reads them from the user's library: only the file
    inputs the call gives, each with its content, or None when the library has no such file."""
    fixtures = spec.get("fixtures") or {}
    out = {}
    for name in spec.get("file_inputs") or []:
        path = args.get(name)
        if isinstance(path, str) and path:
            found = path in fixtures and library_path(path)
            out[path] = base64.b64encode(fixture_bytes(fixtures[path])).decode() if found else None
    return out


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
            self.p.wait()
        for pipe in (self.p.stdout, self.p.stderr):
            pipe.close()


LIBRARY = Path(__file__).with_name("connector_lib") / "PyCompat.cs"


def linked(script_text):
    """The script as it deploys: the port, then the PyCompat library it builds on (one file, as connector code must
    be). The library's own using lines go to the top."""
    lib = LIBRARY.read_text(encoding="utf-8")
    return script_text.rstrip() + "\n\n" + lib


def prove(spec, agent_file, basic_file, script_file, python=None, records=False):
    """Run every sequence of the spec through the real Python and the compiled C#; each carries its own state from
    call to call. Parity means every output and every file written is identical. records=True keeps every call's
    pair of outputs in the result (evidence)."""
    script = linked(Path(script_file).read_text(encoding="utf-8"))
    dll = compile_script(script)
    workspace = bool((spec.get("state") or {}).get("files"))
    tmp = tempfile.mkdtemp(prefix="bfs-connector-proof-")
    runner = Path(tmp) / "runner.py"
    runner.write_text(PY_RUNNER)
    py = _Session([python or sys.executable, str(runner), str(agent_file), str(basic_file),
                   json.dumps(spec.get("hide_modules") or [])], env={**os.environ, "PYTHONHASHSEED": "0"})
    cs = _Session(["dotnet", str(dll)])
    results = []
    try:
        for n, sequence in enumerate(spec["sequences"]):
            initial = ((spec.get("initial_states") or [])[n:n + 1] or [None])[0] or spec.get("initial_state") or {}
            py_state, cs_state = dict(initial), dict(initial)
            for k, call in enumerate(sequence):
                now = call.get("now") or spec.get("now") or "2026-09-25T10:00:00Z"
                ident = call.get("id") or f"00000000-0000-4000-8000-{n:04x}{k:08x}"
                responses = {**(spec.get("responses") or {}), **(call.get("responses") or {})}
                library = named_files(spec, call["args"])
                out_py = py.ask({"args": call["args"], "state": py_state, "now": now, "ids": derived_ids(ident, 64),
                                 "workspace": workspace, "responses": responses, "library": library})
                body = {"args": call["args"], "state": cs_state, "now": now, "id": ident}
                if spec.get("file_inputs"):
                    body["files"] = files_body(spec, call["args"])
                raw = cs.ask({"operationId": spec.get("operation", "Run"), "responses": responses, "body": body})
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
    proof = {"agent": spec["agent"], "mode": "connector-code", "cases": len(results), "passed": passed,
             "parity": passed == len(results) and bool(results), "sequences": len(spec["sequences"]),
             "mismatches": [r for r in results if not r["match"]][:10],
             "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest()}
    if records:
        proof["records"] = results
    return proof


# ── recorded proofs: for places without the .NET SDK (the Azure Function) ─────────────────────────────────────────

def spec_sha256(spec):
    """The spec as its proof ran it (calls, fixtures, recorded responses), without the build's own fields."""
    body = {k: v for k, v in spec.items() if not k.startswith("_")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def proof_record_file(spec):
    return Path(spec["_dir"]) / (Path(spec["script"]).stem + ".proof.json")


def record(spec, report, source_sha256, basic_sha256, when):
    """What a passing proof leaves behind: the exact bytes it held for (the agent's source, its BasicAgent, the
    linked script, the spec) and its count, so a build without the .NET SDK can lay the port without rerunning it."""
    if not report["parity"]:
        raise ConnectorCodeError(f"only a passing proof is recorded ({report['passed']}/{report['cases']})")
    return {"agent": spec["agent"], "source_sha256": source_sha256, "basic_sha256": basic_sha256,
            "script_sha256": report["script_sha256"], "spec_sha256": spec_sha256(spec), "python": spec.get("python"),
            "cases": report["cases"], "passed": report["passed"], "parity": True, "proven": when}


def recorded_proof(spec, source_sha256, basic_sha256, script_text):
    """The proof recorded for these exact bytes, as a report like prove()'s (with `recorded`: its date), or None
    when there is none or it was for other code: a changed agent, script, library or spec needs a new proof."""
    f = proof_record_file(spec)
    if not f.is_file():
        return None
    rec = json.loads(f.read_text(encoding="utf-8"))
    script_sha = hashlib.sha256(linked(script_text).encode("utf-8")).hexdigest()
    if not (rec.get("parity") and rec.get("source_sha256") == source_sha256 and rec.get("basic_sha256") == basic_sha256
            and rec.get("script_sha256") == script_sha and rec.get("spec_sha256") == spec_sha256(spec)):
        return None
    return {"agent": spec["agent"], "mode": "connector-code", "cases": rec["cases"], "passed": rec["passed"],
            "parity": True, "sequences": len(spec["sequences"]), "mismatches": [], "script_sha256": script_sha,
            "recorded": rec["proven"]}


# ── what a proven port becomes: a custom connector, and the flow that runs it with the agent's state ─────────────

STATE_PREFIX = "rapp-workspace"
CONNECTOR_PLACEHOLDER = "{{CONNECTOR:%s}}"          # the connector's internal id, known once it exists


MAX_CONNECTOR_DISPLAY = 30        # Dataverse refuses a longer one ("Connector name cannot be longer than 30 characters")


def connector_name(schema_name, spec):
    """The connector's display name (its internal id derives from it) and its Dataverse name."""
    short, agent = schema_name.split("_", 1)[-1], spec["agent"]
    display = f"{short} {agent} code"
    if len(display) > MAX_CONNECTOR_DISPLAY:
        base = short if agent.lower() in short.lower() else f"{short} {agent}"
        display = base[:MAX_CONNECTOR_DISPLAY - len(" code")].rstrip() + " code"
    return display, f"{schema_name.split('_', 1)[0]}_{re.sub(r'[^a-z0-9]', '', (schema_name.split('_', 1)[-1] + spec['agent']).lower())}code"[:60]


def openapi(spec, display):
    """The connector's definition: one operation, Run, answered by its code (the host is never called)."""
    body = {"type": "object", "properties": {
        "args": {"type": "object", "description": "The tool's inputs"},
        "state": {"type": "object", "description": "The agent's workspace files: name → text (null when absent)"},
        "now": {"type": "string", "description": "The time of the call, yyyy-MM-ddTHH:mm:ssZ"},
        "id": {"type": "string", "description": "A fresh guid for anything the call creates"}}}
    if spec.get("file_inputs"):
        body["properties"]["files"] = {"type": "object", "description": "The files the call names, read from the "
                                       "SharePoint library: for each file input, {path, content}, the content in base64 "
                                       "(null when there is no such file)"}
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


FILES_API = "shared_sharepointonline"
DEFAULT_FILES_FOLDER = "/Shared Documents"
FILES_SETTINGS = (("site", "RAPP Files Site", "The SharePoint site whose library holds the files RAPP agents read, "
                   "for example https://contoso.sharepoint.com/sites/team"),
                  ("folder", "RAPP Files Folder", "The folder in that site the agents' paths start from, for example "
                   "/Shared Documents"))


def files_parameters(schema_name, site="", folder=None):
    """Where agents that read files find them, as the flow parameters and environment variables that hold it: the
    site and the folder (a library, or a folder in one). One pair per publisher prefix, shared by its agents."""
    from .flows import _setting_param
    values = {"site": site or "", "folder": folder or DEFAULT_FILES_FOLDER}
    out = {}
    for key, display, description in FILES_SETTINGS:
        parameter, env_schema = _setting_param(schema_name, key, {"display": display})
        out[key] = {"parameter": parameter, "schemaName": env_schema, "displayName": display,
                    "description": description, "defaultValue": values[key]}
    return out


def compile_flow(spec, schema_name, display_name, connector_display, connector_logical, files_home=None):
    """The agent flow for a connector-code tool: read the workspace files (Dataverse notes) and the files the call
    names (SharePoint), run the code with the tool's inputs, write back the files it changed, return its output.
    `{{CONNECTOR:...}}` is the connector's internal id, filled in at deploy."""
    from .flows import _within_limits
    placeholder = CONNECTOR_PLACEHOLDER % connector_display
    files = list((spec.get("state") or {}).get("files") or [])
    file_inputs = list(spec.get("file_inputs") or [])
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
    # the files the call names: each read from the library when it is a path inside the folder; one that can't be
    # read (missing, or outside the folder) fails its read, and the code gets null for it, which it answers as a
    # missing file. The code is told {input: {path, content}}: a path can't be a key (setProperty refuses dots)
    settings = files_parameters(schema_name, **(files_home or {})) if file_inputs else {}
    sharepoint = {"apiId": f"/providers/Microsoft.PowerApps/apis/{FILES_API}", "connectionName": FILES_API}
    read_done = ["Succeeded", "Failed", "TimedOut"]
    named = {}
    for k in file_inputs:
        value = f"triggerBody()?['{k}']"
        slashed = f"replace(string({value}), '\\', '/')"
        get, guard = _action_name("Get_file", k), _action_name("Read_file", k)
        actions[guard] = {
            "type": "If", "runAfter": ({last: read_done if last.startswith("Read_file_") else ["Succeeded"]}
                                       if last else {}),
            "expression": {"and": [{"not": {"equals": [f"@empty({value})", "@true"]}},
                                   {"not": {"startsWith": [f"@{slashed}", "/"]}},
                                   {"not": {"contains": [f"@concat('/', {slashed}, '/')", "/../"]}}]},
            "actions": {get: {"type": "OpenApiConnection", "runAfter": {},
                              "inputs": {"host": {**sharepoint, "operationId": "GetFileContentByPath"},
                                         "parameters": {"dataset": f"@parameters('{settings['site']['parameter']}')",
                                                        "path": f"@concat(parameters('{settings['folder']['parameter']}'), '/', {value})",
                                                        "inferContentType": False,
                                                        "queryParametersSingleEncoded": True},
                                         "authentication": auth}}},
            "else": {"actions": {}}}
        # both branches of if() may be evaluated, and a skipped or failed read has no content: go through actions().
        # A read that worked has its bytes in body.$content, except an empty file's, which comes back with no body
        named[k] = {"path": f"@{value}",
                    "content": (f"@if(equals(actions('{get}')?['status'], 'Succeeded'), "
                                f"coalesce(actions('{get}')?['outputs']?['body']?['$content'], ''), null)")}
        last = guard
    args = {k: f"@triggerBody()?['{k}']" for k in spec["inputs"]}
    parameters = {"body/args": args, "body/state": state, "body/now": "@utcNow('yyyy-MM-ddTHH:mm:ssZ')",
                  "body/id": "@guid()"}
    if named:
        parameters["body/files"] = named
    actions["Run_the_agent"] = {"type": "OpenApiConnection",
                                "runAfter": ({last: read_done if last.startswith("Read_file_") else ["Succeeded"]}
                                             if last else {}),
                                "inputs": {"host": {"apiId": f"/providers/Microsoft.PowerApps/apis/{placeholder}",
                                                    "connectionName": "shared_rapp_code", "operationId": "Run"},
                                           "parameters": parameters,
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
    if file_inputs:
        refs[FILES_API] = {"api": {"name": FILES_API}, "runtimeSource": "embedded",
                           "connection": {"connectionReferenceLogicalName": f"{schema_name}.{FILES_API}"}}
    flow_parameters = {"$connections": {"defaultValue": {}, "type": "Object"},
                       "$authentication": {"defaultValue": {}, "type": "SecureObject"}}
    for v in settings.values():
        flow_parameters[v["parameter"]] = {"defaultValue": v["defaultValue"], "type": "String",
                                           "metadata": {"schemaName": v["schemaName"], "description": v["description"]}}
    return _within_limits({"properties": {"connectionReferences": refs, "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "parameters": flow_parameters,
        "triggers": {"manual": {"type": "Request", "kind": "Skills", "inputs": {"schema": {
            "type": "object", "properties": props, "required": list(spec.get("required", []))}}}},
        "actions": actions, "outputs": {}}, "templateName": ""}, "schemaVersion": "1.0.0.0"})


def fill_connectors(definition, internal_ids):
    """The flow with each connector placeholder replaced by the connector's internal id."""
    text = json.dumps(definition)
    for display, internal in internal_ids.items():
        text = text.replace(CONNECTOR_PLACEHOLDER % display, internal)
    return json.loads(text)
