"""Offline proof protocol, coverage and child-environment regressions (Python 3.9 compatible)."""
import base64
import hashlib
import io
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brainfreeze_studio import __main__ as cli, connector_code as cc, flows, materialize as mat, rapp1

BASIC = """class BasicAgent:
    def __init__(self, name=None, metadata=None):
        self.name, self.metadata = name, metadata
"""
AGENT = """from agents.basic_agent import BasicAgent
class Echo(BasicAgent):
    def __init__(self):
        super().__init__(name="Echo", metadata={"name": "Echo", "parameters": {
            "type": "object", "properties": {"value": {"type": "string", "enum": ["first", "second"]}}}})
    def perform(self, value="first", **kwargs):
        return "same"
"""
FLOW_SPEC = {"agent": "Echo", "flow_name": "EchoFlow", "description": "A proof fixture.",
             "inputs": {"value": {"type": "string", "description": "Test case selector."}},
             "steps": [], "outputs": {"result": "same"}, "vectors": [{"value": "first"}, {"value": "second"}]}
MATERIALIZED_SPEC = dict(FLOW_SPEC, mode="materialized", **{
    "class": "Echo", "primary": None, "keying": [], "operations": {"*": []},
    "table_keys": {"*": "answer"}, "table_outputs": {"answer": base64.b64encode(b"same").decode()},
    "source_sha256": hashlib.sha256(AGENT.encode()).hexdigest()})
CONNECTOR_SPEC = {"agent": "Echo", "mode": "connector-code", "flow_name": "EchoFlow", "script": "echo.csx",
                  "sequences": [[{"args": v} for v in FLOW_SPEC["vectors"]]]}
CODE = """public class Script : ScriptBase {
    public override async Task<HttpResponseMessage> ExecuteAsync() {
        var body = JObject.Parse(await this.Context.Request.Content.ReadAsStringAsync());
        return new HttpResponseMessage(HttpStatusCode.OK) {
            Content = CreateJsonContent("{\\"output\\":\\"same\\",\\"state\\":{}}") };
    }
}"""
LEAK_KEYS = ("AZURE_ACCESS_TOKEN", "IDENTITY_HEADER", "IDENTITY_ENDPOINT", "AzureWebJobsStorage", "UNLISTED_SECRET")


def stream_runner(kind="connector-code", mutation=None):
    """An actual child process emitting controlled protocol faults, including output sent only after EOF."""
    return f"""import json, sys
kind, mutation = {kind!r}, {mutation!r}
last = None
for line in sys.stdin:
    case = json.loads(line)
    ident = case["case_id"]
    if mutation == "exit" and ident == 1:
        raise SystemExit(0)
    record = ({{"case_id": ident, "output": "same", "state": {{}}}} if kind == "connector-python" else
              {{"case_id": ident, "status": 200, "body": '{{"output":"same","state":{{}}}}'}})
    if ident == 1 and mutation == "duplicate":
        record["case_id"] = 0
    if ident == 1 and mutation == "no-id":
        del record["case_id"]
    if ident == 0 and mutation == "reordered":
        record["case_id"] = 1
    print("{{" if ident == 1 and mutation == "malformed" else json.dumps(record), flush=True)
    last = record
if mutation == "extra":
    last["case_id"] += 1
    print(json.dumps(last), flush=True)
elif mutation == "trailing-duplicate":
    print(json.dumps(last), flush=True)
elif mutation == "noise":
    print("stdout noise", flush=True)
elif mutation == "invalid-bytes":
    import os
    sys.stdout.flush()
    os.write(1, b"\\xff\\n")
elif mutation == "nonzero":
    raise SystemExit(3)
"""


class ProofFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bfs-proof-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.agent, self.basic, self.script = (self.work / n for n in ("agent.py", "basic_agent.py", "echo.csx"))
        self.agent.write_text(AGENT)
        self.basic.write_text(BASIC)
        self.script.write_text(CODE)

    @contextmanager
    def local_connector(self, code_mutation=None):
        # Exercise the real streaming machinery and real Python runner even on machines without .NET.
        session = cc._Session

        def start(cmd, *args, **kwargs):
            if cmd[0] == "dotnet":
                cmd = [sys.executable, "-B", "-c", stream_runner(mutation=code_mutation)]
            return session(cmd, *args, **kwargs)

        with mock.patch.object(cc, "compile_script", return_value=self.work / "unused.dll"), \
                mock.patch.object(cc, "_Session", side_effect=start):
            yield

    def connector(self, spec=None):
        return cc.prove(spec or CONNECTOR_SPEC, self.agent, self.basic, self.script, records=True)

    def assertRefused(self, report, reason, scheduled=2):
        self.assertFalse(report["parity"], report)
        self.assertEqual((report["cases"], report["scheduled"]), (scheduled, scheduled))
        self.assertIn(reason, report["reason"])


class AbruptExitTests(ProofFixture):
    def exit_code(self, held_pipe):
        if not held_pipe:
            return "            os._exit(0)\n"
        return ("            if os.fork() == 0:\n"
                "                time.sleep(12)\n"
                "                os._exit(0)\n"
                "            os._exit(0)\n")

    def echo_source(self, held_pipe):
        return "import os, time\n" + AGENT.replace(
            '        return "same"', '        if value == "second":\n' + self.exit_code(held_pipe) + '        return "same"')

    def test_flow_os_exit_is_refused_in_under_ten_seconds(self):
        original = (ROOT / "examples/invoice_router_agent.py").read_text()
        spec = json.loads((ROOT / "translations/invoice_router.json").read_text())
        for held_pipe in ((False, True) if hasattr(os, "fork") else (False,)):
            with self.subTest(inherited_pipe=held_pipe):
                source = "import time\n" + original.replace(
                    "        limit = float(",
                    '        if vendor == "Fabrikam":\n' + self.exit_code(held_pipe) + "        limit = float(")
                self.agent.write_text(source)
                started = time.monotonic()
                report = flows.prove(spec, self.agent, "rapp_Test")
                self.assertLess(time.monotonic() - started, 10, "dead runner was held open by inherited pipes")
                self.assertRefused(report, "missing results", scheduled=72)
                self.assertEqual(report["observed"], 1)

    def test_materialized_os_exit_is_refused_in_under_ten_seconds(self):
        for held_pipe in ((False, True) if hasattr(os, "fork") else (False,)):
            with self.subTest(inherited_pipe=held_pipe):
                self.agent.write_text(self.echo_source(held_pipe))
                started = time.monotonic()
                with self.assertRaisesRegex(mat.ProofProtocolError, r"scheduled=2, observed=1"):
                    mat.Runner(self.agent, self.basic).run(FLOW_SPEC["vectors"])
                self.assertLess(time.monotonic() - started, 10, "dead materialize runner waited for pipe EOF")
                started = time.monotonic()
                report = flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic)
                self.assertLess(time.monotonic() - started, 10, "dead materialized proof waited for pipe EOF")
                self.assertRefused(report, "missing results")
                self.assertEqual(report["observed"], 1)

    def test_connector_python_os_exit_is_refused_in_under_ten_seconds(self):
        for held_pipe in ((False, True) if hasattr(os, "fork") else (False,)):
            with self.subTest(inherited_pipe=held_pipe):
                self.agent.write_text(self.echo_source(held_pipe))
                started = time.monotonic()
                with self.local_connector():
                    report = self.connector()
                self.assertLess(time.monotonic() - started, 10, "dead connector runner waited for pipe EOF")
                self.assertRefused(report, "missing result for case_id 1")
                self.assertEqual((report["observed_python"], report["observed_code"]), (1, 1))


class BatchProofTests(ProofFixture):
    def test_invoice_control_still_proves_all_72_cases(self):
        spec = json.loads((ROOT / "translations/invoice_router.json").read_text())
        report = flows.prove(spec, ROOT / "examples/invoice_router_agent.py", "rapp_InvoiceDesk")
        self.assertEqual((report["passed"], report["cases"], report["scheduled"], report["observed"]),
                         (72, 72, 72, 72))
        self.assertTrue(report["parity"])
        self.assertEqual([r["case_id"] for r in report["_all"]], list(range(72)))

    def test_invoice_clean_early_exit_is_persisted_as_a_failed_72_case_proof(self):
        source = (ROOT / "examples/invoice_router_agent.py").read_text().replace(
            '        limit = float(', '        if vendor == "Fabrikam": raise SystemExit(0)\n        limit = float(')
        rid = "rappid:@example/proof-test:" + "d" * 64
        files = {"rappid.json": rapp1.canonical({"schema": "rapp/1", "rappid": rid}).encode(),
                 "soul.md": b"Invoice test.", "agents/invoice_router_agent.py": source.encode()}
        egg = self.work / "invoice.egg"
        egg.write_bytes(rapp1.pack_egg("organism", rid, "2026-09-27T12:00:00.000Z", files=files,
                                     payload={"engine": {"name": "rapp-brainstem", "version": "0.6.16"}}))
        out, printed = self.work / "out", io.StringIO()
        with redirect_stdout(printed):
            cli.main(["build", str(egg), "--out", str(out), "--name", "Invoice Test",
                      "--publisher-prefix", "rapp", "--translations", str(ROOT / "translations")])
        lines = [line.split() for line in printed.getvalue().splitlines() if line.startswith("parity:")]
        self.assertEqual(lines, [["parity:", "InvoiceRouter", "0/72", "FAILED"]])
        report = json.loads((out / "parity/InvoiceRouterFlow.json").read_text())
        self.assertRefused(report, "missing results", scheduled=72)
        self.assertEqual(report["observed"], 1)
        self.assertFalse(list((out / "workspace/workflows").glob("InvoiceRouterFlow-*")))

    def test_materialized_clean_early_exit_is_refused(self):
        self.agent.write_text(AGENT.replace('        return "same"',
                                          '        if value == "second": raise SystemExit(0)\n        return "same"'))
        report = flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic)
        self.assertRefused(report, "missing results")
        self.assertEqual(report["observed"], 1)

    def test_materialization_clean_early_exit_is_refused(self):
        self.agent.write_text(AGENT.replace('        return "same"',
                                          '        if value == "second": raise SystemExit(0)\n        return "same"'))
        with self.assertRaisesRegex(mat.MaterializeError, "missing results"):
            mat.materialize(self.agent, self.basic)

    def test_runner_clean_early_exit_is_refused(self):
        self.agent.write_text(AGENT.replace('        return "same"',
                                          '        if value == "second": raise SystemExit(0)\n        return "same"'))
        with self.assertRaisesRegex(mat.ProofProtocolError, r"scheduled=2, observed=1"):
            mat.Runner(self.agent, self.basic).run(FLOW_SPEC["vectors"])

    def test_batch_runners_refuse_extra_duplicate_malformed_and_unidentified_results(self):
        for materialized in (False, True):
            good = [{"case_id": i, "ok": True, "out": "same", **({"effects": []} if materialized else {})}
                    for i in range(2)]
            variants = {
                "extra": (good + [dict(good[1], case_id=2)], "unexpected case_id"),
                "duplicate": ([good[0], good[0]], "duplicate result"),
                "no-id": ([good[0], {k: v for k, v in good[1].items() if k != "case_id"}], "missing case_id"),
                "bad-type": ([good[0], dict(good[1], ok="true")], "invalid fields or types"),
                "boolean-id": ([good[0], dict(good[1], case_id=True)], "unexpected case_id"),
                "nonfinite-id": ([good[0], dict(good[1], case_id=float("nan"))], "malformed result"),
                "extra-field": ([good[0], dict(good[1], extra=True)], "invalid fields or types"),
            }
            lines = {name: ("\n".join(map(json.dumps, records)) + "\n", why)
                     for name, (records, why) in variants.items()}
            lines.update(malformed=(json.dumps(good[0]) + "\n{\n", "malformed result"),
                         noise=("\n".join(map(json.dumps, good)) + "\nstdout noise\n", "stdout noise"),
                         blank=("\n".join(map(json.dumps, good)) + "\n\n", "stdout noise"),
                         duplicate_field=('{"case_id":0,"case_id":1,"ok":true,"out":"same"}\n', "malformed result"))
            for name, (stdout, why) in lines.items():
                with self.subTest(materialized=materialized, fault=name):
                    stub = f"import sys; sys.stdin.read(); sys.stdout.write({stdout!r})"
                    module, attr = (mat, "RUNNER") if materialized else (flows, "_RUN_AGENT")
                    with mock.patch.object(module, attr, stub):
                        if materialized:
                            with self.assertRaisesRegex(mat.ProofProtocolError, why):
                                mat.Runner(self.agent, self.basic).run(FLOW_SPEC["vectors"])
                            report = flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic)
                        else:
                            report = flows.prove(FLOW_SPEC, self.agent, "rapp_Test")
                    self.assertRefused(report, why)
                    self.assertEqual(report["observed"], len(stdout.split("\n")) - 1)
            with mock.patch.object(mat if materialized else flows, "RUNNER" if materialized else "_RUN_AGENT",
                                   'import os; os.write(1, b"\\xff\\n")'):
                report = (flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic) if materialized else
                          flows.prove(FLOW_SPEC, self.agent, "rapp_Test"))
                self.assertRefused(report, "malformed result")

    def test_batch_results_with_ids_can_be_reordered_without_mispairing(self):
        records = [{"case_id": 1, "ok": True, "out": "second"}, {"case_id": 0, "ok": True, "out": "first"}]
        stdout = "".join(json.dumps(r) + "\n" for r in records)
        stub = f"import sys; sys.stdin.read(); sys.stdout.write({stdout!r})"
        spec = dict(FLOW_SPEC, outputs={"result": "@triggerBody()?['value']"})
        with mock.patch.object(flows, "_RUN_AGENT", stub):
            report = flows.prove(spec, self.agent, "rapp_Test")
        self.assertTrue(report["parity"], report)
        materialized = "".join(json.dumps(dict(r, effects=[])) + "\n" for r in records)
        with mock.patch.object(mat, "RUNNER", f"import sys; sys.stdin.read(); sys.stdout.write({materialized!r})"):
            self.assertEqual(mat.Runner(self.agent, self.basic).run(FLOW_SPEC["vectors"]), ["first", "second"])

    def test_contract_record_is_correlated_and_validated(self):
        record = {"case_id": 0, "class": "Echo", "name": "Echo", "description": "", "parameters": {}, "dicts": {}}
        for change in ({"case_id": 1}, {"parameters": []}, {"class": None}):
            with self.subTest(change=change):
                stdout = json.dumps(dict(record, **change)) + "\n"
                with mock.patch.object(mat, "RUNNER", f"print({stdout!r}, end='')"):
                    with self.assertRaises(mat.ProofProtocolError):
                        mat.Runner(self.agent, self.basic).contract()

    def test_clock_transposes_and_reruns_cannot_silently_truncate(self):
        with self.assertRaisesRegex(mat.MaterializeError, "unequal case/result counts"):
            mat._clock_template([["one", "two"], ["one"], ["one", "two"]])
        with self.assertRaisesRegex(mat.MaterializeError, "unequal case/result counts"):
            mat._zip_exact(["one", "two"], ["one"])
        runs = [["September 29, 2026"], ["March 20, 2027"], ["January 10, 2027"]]
        with self.assertRaisesRegex(mat.MaterializeError, "unequal case/result counts"):
            mat._clock_template(runs, rerun=lambda cases, clock: [])

    def test_batch_timeouts_preserve_scheduled_and_observed_counts(self):
        stdout = b'{"case_id":0,"ok":true,"out":"same"}\n'
        with mock.patch.object(flows, "run_proof_process", side_effect=subprocess.TimeoutExpired([], 120, stdout)):
            report = flows.prove(FLOW_SPEC, self.agent, "rapp_Test")
        self.assertRefused(report, "runner timed out")
        self.assertEqual(report["observed"], 1)
        with mock.patch.object(mat, "run_proof_process", side_effect=subprocess.TimeoutExpired([], 600, stdout)):
            with self.assertRaisesRegex(mat.ProofProtocolError, r"scheduled=2, observed=1"):
                mat.Runner(self.agent, self.basic).run(FLOW_SPEC["vectors"])

    def test_nonzero_exit_after_all_batch_results_is_still_refused(self):
        for materialized in (False, True):
            records = [{"case_id": i, "ok": True, "out": "same", **({"effects": []} if materialized else {})}
                       for i in range(2)]
            stdout = "".join(json.dumps(r) + "\n" for r in records)
            stub = f"import sys; sys.stdin.read(); sys.stdout.write({stdout!r}); raise SystemExit(3)"
            module, name = (mat, "RUNNER") if materialized else (flows, "_RUN_AGENT")
            with self.subTest(materialized=materialized), mock.patch.object(module, name, stub):
                report = (flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic) if materialized else
                          flows.prove(FLOW_SPEC, self.agent, "rapp_Test"))
                self.assertRefused(report, "runner exited with status 3")
                self.assertEqual(report["observed"], 2)


class StreamingProofTests(ProofFixture):
    def test_connector_python_clean_early_exit_is_refused(self):
        self.agent.write_text(AGENT.replace('        return "same"',
                                          '        if value == "second": raise SystemExit(0)\n        return "same"'))
        with self.local_connector():
            report = self.connector()
        self.assertRefused(report, "missing result for case_id 1")
        self.assertEqual((report["observed_python"], report["observed_code"], report["passed"]), (1, 1, 1))

    def test_both_connector_sides_refuse_stream_faults_including_trailing_output(self):
        faults = {"extra": "unexpected case_id", "duplicate": "duplicate result",
                  "trailing-duplicate": "duplicate result", "malformed": "malformed result",
                  "no-id": "missing case_id", "noise": "stdout noise", "reordered": "out-of-order result",
                  "invalid-bytes": "malformed result",
                  "exit": "missing result", "nonzero": "runner exited with status 3"}
        for side in ("python", "code"):
            for mutation, why in faults.items():
                with self.subTest(side=side, mutation=mutation):
                    with self.local_connector(mutation if side == "code" else None), \
                            mock.patch.object(cc, "PY_RUNNER", stream_runner("connector-python", mutation)
                                              if side == "python" else cc.PY_RUNNER):
                        report = self.connector()
                    self.assertRefused(report, why)
                    if mutation in ("extra", "trailing-duplicate", "noise", "invalid-bytes"):
                        self.assertEqual(report["observed_" + side], 3)

    def test_batch_connector_code_requires_exact_protocol_coverage(self):
        cases = [{"operationId": "Run", "body": {}} for _ in range(2)]
        record = {"case_id": 0, "status": 200, "body": "{}"}
        variants = [
            ([record], "missing results"),
            ([record, record], "duplicate result"),
            ([record, dict(record, case_id=1), dict(record, case_id=2)], "unexpected case_id"),
            ([record, {"status": 200, "body": "{}"}], "missing case_id"),
            ([record, dict(record, case_id=1, body=[])], "invalid fields or types"),
        ]
        for records, why in variants:
            with self.subTest(reason=why):
                proc = subprocess.CompletedProcess([], 0, "".join(json.dumps(r) + "\n" for r in records), "")
                with mock.patch.object(cc.subprocess, "run", return_value=proc):
                    with self.assertRaisesRegex(cc.ConnectorCodeError, why):
                        cc.run_script(self.work / "unused.dll", cases)
        for stdout in ('{"case_id":0,"status":200,"body":"{}"}\n{\n', "stdout noise\n"):
            with mock.patch.object(cc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                with self.assertRaisesRegex(cc.ConnectorCodeError, "malformed result"):
                    cc.run_script(self.work / "unused.dll", cases)

    def test_a_silent_stream_times_out_instead_of_hanging_the_build(self):
        session = cc._Session([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                              1, "connector-python", "Python runner", timeout=0.1,
                              env=mat.proof_environment(self.work), cwd=self.work)
        try:
            with self.assertRaisesRegex(mat.ProofProtocolError, "runner timed out"):
                session.ask({})
        finally:
            session.close()
        self.assertIsNotNone(session.p.returncode)

    def test_stream_write_also_has_a_timeout(self):
        session = cc._Session([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                              1, "connector-python", "Python runner", timeout=0.1,
                              env=mat.proof_environment(self.work), cwd=self.work)
        try:
            with self.assertRaisesRegex(mat.ProofProtocolError, "runner timed out sending case"):
                session.ask({"large_fixture": "x" * 2000000})
        finally:
            session.close()
        self.assertIsNotNone(session.p.returncode)

    @unittest.skipUnless(shutil.which("dotnet"), "needs the cached .NET SDK and Newtonsoft.Json")
    def test_real_csharp_control_and_clean_early_exit(self):
        report = self.connector()
        self.assertTrue(report["parity"], report)
        self.script.write_text(CODE.replace("        return new HttpResponseMessage", """
        if ((string)body["args"]["value"] == "second") System.Environment.Exit(0);
        return new HttpResponseMessage"""))
        self.assertRefused(self.connector(), "C# runner: missing result for case_id 1")
        dll = cc.compile_script(cc.linked(self.script.read_text()))
        cases = [{"operationId": "Run", "body": {"args": v}} for v in FLOW_SPEC["vectors"]]
        with self.assertRaisesRegex(cc.ConnectorCodeError, r"scheduled=2, observed=1"):
            cc.run_script(dll, cases)


class EnvironmentTests(ProofFixture):
    def environment_agent(self):
        marker = self.work.name + ".marker"
        operator_marker = Path.cwd() / marker
        self.assertFalse(operator_marker.exists())
        self.addCleanup(lambda: operator_marker.unlink() if operator_marker.exists() else None)
        prelude = f"""import os
from pathlib import Path
import proof_user_package, proof_path_package
IMPORT_CWD = os.getcwd()
IMPORT_HOME = os.environ["HOME"]
Path({marker!r}).write_text("relative import marker")
"""
        checks = f"""        assert not any(k in os.environ for k in {LEAK_KEYS!r})
        assert IMPORT_HOME == os.environ["USERPROFILE"] == os.environ["TMPDIR"] == os.environ["TMP"] == os.environ["TEMP"]
        assert IMPORT_CWD == IMPORT_HOME and IMPORT_CWD != {str(Path.cwd())!r}
        assert Path(IMPORT_CWD).name.startswith("bfs-")
        assert Path(IMPORT_CWD, {marker!r}).read_text() == "relative import marker"
        assert os.getcwd() == IMPORT_CWD or Path(os.getcwd()).parent == Path(IMPORT_CWD)
        assert proof_user_package.VALUE == proof_path_package.VALUE == "same"
        if kwargs.get("path"):
            assert Path(kwargs["path"]).read_text() == "same"
        return "same"
"""
        self.agent.write_text(prelude + AGENT.replace('        return "same"\n', checks))
        packages, imports = self.work / "user-packages", self.work / "path-packages"
        packages.mkdir()
        imports.mkdir()
        (packages / "proof_user_package.py").write_text('VALUE = "same"\n')
        (imports / "proof_path_package.py").write_text('VALUE = "same"\n')
        return operator_marker, packages, imports

    def test_all_python_proofs_remove_ambient_credentials_and_use_temporary_home_and_cwd(self):
        marker, packages, imports = self.environment_agent()
        with mock.patch.dict(os.environ, {k: "must-not-be-inherited" for k in LEAK_KEYS}), \
                mock.patch.object(site, "getusersitepackages", return_value=str(packages)), \
                mock.patch.object(sys, "path", sys.path + [str(imports)]):
            direct = flows.prove(FLOW_SPEC, self.agent, "rapp_Test")
            self.assertTrue(direct["parity"], direct)
            self.assertFalse(marker.exists())
            runner = mat.Runner(self.agent, self.basic)
            self.assertEqual(runner.run(FLOW_SPEC["vectors"]), ["same", "same"])
            self.assertFalse(marker.exists())
            materialized = flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic)
            self.assertTrue(materialized["parity"], materialized)
            self.assertFalse(marker.exists())
            with self.local_connector():
                connector = self.connector()
                self.assertTrue(connector["parity"], connector)
                files = dict(CONNECTOR_SPEC, file_inputs=["path"], fixtures={"data/input.txt": {"text": "same"}},
                             sequences=[[{"args": {"path": "data/input.txt"}}]])
                fixture = self.connector(files)
                self.assertTrue(fixture["parity"], fixture)
            self.assertFalse(marker.exists())
        self.assertFalse((self.work / "__pycache__").exists())

    def test_environment_is_allow_listed_and_cannot_override_its_temporary_home(self):
        with mock.patch.dict(os.environ, {k: "must-not-be-inherited" for k in LEAK_KEYS}):
            env = mat.proof_environment(self.work, {"HOME": "wrong", "USERPROFILE": "wrong", "MODE": "fixture"})
        self.assertTrue(set(LEAK_KEYS).isdisjoint(env))
        self.assertEqual(env["HOME"], str(self.work.resolve()))
        self.assertEqual(env["USERPROFILE"], env["HOME"])
        self.assertEqual((env["MODE"], env["PYTHONIOENCODING"], env["PYTHONHASHSEED"]), ("fixture", "utf-8", "0"))
        allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                   "HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONHASHSEED", "MODE"}
        self.assertTrue(set(env) <= allowed)

    def test_current_interpreter_version_needs_no_extra_proof_process(self):
        runner = mat.Runner(self.agent, self.basic)
        with mock.patch.object(mat.subprocess, "run", side_effect=AssertionError("unnecessary interpreter probe")):
            self.assertEqual(runner.python_version(), ".".join(map(str, sys.version_info[:3])))

    def test_print_and_native_stdout_cannot_forge_results_or_deadlock_a_stream(self):
        prelude = 'import os\nprint("import log" * 10000)\nos.write(1, b"native import log\\xff\\n")\n'
        body = ('        print("perform log" * 10000)\n'
                '        os.write(1, b\'{"case_id":0,"ok":true,"out":"forged"}\\n\')\n'
                '        return "same"')
        self.agent.write_text(prelude + AGENT.replace('        return "same"', body))
        self.assertTrue(flows.prove(FLOW_SPEC, self.agent, "rapp_Test")["parity"])
        self.assertTrue(flows.prove_materialized(MATERIALIZED_SPEC, self.agent, self.basic)["parity"])
        with self.local_connector():
            self.assertTrue(self.connector()["parity"])

    @unittest.skipUnless(os.name == "posix", "POSIX resource limits")
    def test_resource_limits_are_installed_before_agent_import(self):
        limits = """import resource, sys
assert 0 < resource.getrlimit(resource.RLIMIT_CPU)[0] <= 600
if sys.platform.startswith("linux"):
    assert 0 < resource.getrlimit(resource.RLIMIT_AS)[0] <= 4 * 1024 ** 3
"""
        self.agent.write_text(limits + AGENT)
        self.assertTrue(flows.prove(FLOW_SPEC, self.agent, "rapp_Test")["parity"])
        self.assertEqual(mat.Runner(self.agent, self.basic).run(FLOW_SPEC["vectors"]), ["same", "same"])
        with self.local_connector():
            self.assertTrue(self.connector()["parity"])

    @unittest.skipUnless(shutil.which("dotnet"), "needs the cached .NET SDK and Newtonsoft.Json")
    def test_real_csharp_also_isolates_credentials_and_captures_console_output(self):
        guard = """
        Console.WriteLine(new string('x', 200000));
        foreach (var key in new [] { "AZURE_ACCESS_TOKEN", "IDENTITY_HEADER", "IDENTITY_ENDPOINT",
                                    "AzureWebJobsStorage", "UNLISTED_SECRET" })
            if (System.Environment.GetEnvironmentVariable(key) != null)
                throw new InvalidOperationException("ambient variable leaked");
        var home = System.Environment.GetEnvironmentVariable("HOME");
        if (home != System.Environment.CurrentDirectory || home != System.Environment.GetEnvironmentVariable("USERPROFILE"))
            throw new InvalidOperationException("home/cwd is not isolated");
"""
        self.script.write_text(CODE.replace("        return new HttpResponseMessage", guard + "        return new HttpResponseMessage"))
        with mock.patch.dict(os.environ, {k: "must-not-be-inherited" for k in LEAK_KEYS}):
            proof = self.connector()
            self.assertTrue(proof["parity"], proof)
            dll = cc.compile_script(cc.linked(self.script.read_text()))
            results = cc.run_script(dll, [{"operationId": "Run", "body": {"args": v}} for v in FLOW_SPEC["vectors"]])
            self.assertEqual([r["status"] for r in results], [200, 200])

    def test_azure_function_job_builds_through_the_same_clean_runner(self):
        import brainfreeze_studio as bs
        from test_function_jobs import ENV, FakeStore, jobs, token
        source = (ROOT / "examples/invoice_router_agent.py").read_text()
        source = (f"import os\nassert not any(k in os.environ for k in {LEAK_KEYS!r})\n"
                  'assert os.getcwd() == os.environ["HOME"] == os.environ["USERPROFILE"]\n' + source)
        rid = "rappid:@example/function-proof-test:" + "d" * 64
        files = {"rappid.json": rapp1.canonical({"schema": "rapp/1", "rappid": rid}).encode(),
                 "soul.md": b"Invoice test.", "agents/invoice_router_agent.py": source.encode()}
        egg = rapp1.pack_egg("organism", rid, "2026-09-27T12:00:00.000Z", files=files,
                            payload={"engine": {"name": "rapp-brainstem", "version": "0.6.16"}})
        spec = json.loads((ROOT / "translations/invoice_router.json").read_text())
        store, reports = FakeStore(), []
        job = jobs.new_job(store, {"environment": ENV, "name": "Proof Test", "egg": base64.b64encode(egg).decode(),
                                  "translations": [spec]}, token(), "test-owner", "test-account")

        def build(*args, **kwargs):
            result = bs.build(*args, **kwargs)
            reports.append(json.loads((Path(args[1]) / "parity/InvoiceRouterFlow.json").read_text()))
            return result

        with mock.patch.dict(os.environ, {k: "must-not-be-inherited" for k in LEAK_KEYS}):
            status = jobs.run_job(job, store, build, lambda *a, **kw: {}, sdk_dir=self.work, allow_translations=True)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["parity"], reports[0])
        self.assertEqual((reports[0]["scheduled"], reports[0]["observed"]), (72, 72))


class RecordedCoverageTests(ProofFixture):
    def test_all_shipped_recorded_proofs_cover_the_specs_exact_cases(self):
        expected = {"JsonDoctor": 69, "Thoughtbox": 40, "ForumAgent": 29}
        basic_sha = hashlib.sha256((ROOT / "brainfreeze_studio/basic_agent.py").read_bytes()).hexdigest()
        for path in sorted((ROOT / "translations").glob("*.json")):
            spec = json.loads(path.read_text())
            if spec.get("mode") != "connector-code":
                continue
            spec["_dir"] = str(path.parent)
            with self.subTest(agent=spec["agent"]):
                script = (path.parent / spec["script"]).read_text()
                report = cc.recorded_proof(spec, spec["source_sha256"], basic_sha, script)
                self.assertIsNotNone(report)
                self.assertEqual(report["cases"], expected.pop(spec["agent"]))
                self.assertEqual(report["cases"], sum(len(s) for s in spec["sequences"]))
                self.assertEqual((report["passed"], report["scheduled"], report["observed"]), (report["cases"],) * 3)
        self.assertFalse(expected)

    def test_recorded_materialized_and_connector_counts_cannot_be_short_extra_or_noninteger(self):
        for materialized in (False, True):
            spec = dict(MATERIALIZED_SPEC if materialized else CONNECTOR_SPEC, _dir=str(self.work), _file="echo.json")
            report = {"parity": True, "cases": 2, "passed": 2,
                      "script_sha256": hashlib.sha256(cc.linked(CODE).encode()).hexdigest()}
            make = (lambda r: flows.record_materialized(spec, r, "basic", "today")) if materialized else (
                lambda r: cc.record(spec, r, "source", "basic", "today"))
            valid = make(report)
            path = flows.materialized_record_file(spec) if materialized else cc.proof_record_file(spec)
            read = (lambda: flows.recorded_materialized_proof(spec, spec["source_sha256"], "basic")) if materialized else (
                lambda: cc.recorded_proof(spec, "source", "basic", CODE))
            path.write_text(json.dumps(valid))
            self.assertIsNotNone(read())
            for field in ("cases", "passed", "scheduled", "observed"):
                for count in (0, 1, 3, True, "2", None):
                    with self.subTest(materialized=materialized, field=field, count=count):
                        path.write_text(json.dumps(dict(valid, **{field: count})))
                        self.assertIsNone(read())
                        with self.assertRaisesRegex((flows.StudioBuildError, cc.ConnectorCodeError),
                                                    "exactly 2 scheduled cases"):
                            make(dict(report, **{field: count}))
            path.write_text("{")
            self.assertIsNone(read())
            path.write_text("[]")
            self.assertIsNone(read())
            for field in ("cases", "passed", "parity"):
                with self.subTest(materialized=materialized, missing=field):
                    path.write_text(json.dumps({k: v for k, v in valid.items() if k != field}))
                    self.assertIsNone(read())

    def test_materialized_record_count_includes_every_clock_and_excludes_blocked_vectors(self):
        spec = dict(MATERIALIZED_SPEC, primary="value", blocked_operations={"second": ["amount"]},
                    clock_formats={"ZQCLOCK8QZ": "yyyy-MM-dd"})
        vectors, clocks = flows.materialized_schedule(spec)
        self.assertEqual((len(vectors), len(clocks)), (1, 3))
        with self.assertRaisesRegex(flows.StudioBuildError, "exactly 3 scheduled cases"):
            flows.record_materialized(spec, {"parity": True, "cases": 1, "passed": 1}, "basic", "today")
        record = flows.record_materialized(spec, {"parity": True, "cases": 3, "passed": 3}, "basic", "today")
        self.assertEqual((record["scheduled"], record["observed"]), (3, 3))


if __name__ == "__main__":
    unittest.main()
