"""Translate-then-prove: agent.py → Power Platform agent flow, gated by parity with the real Python. Offline."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import brainfreeze_studio as bs  # noqa: E402
from brainfreeze_studio import flows, rapp1  # noqa: E402
from brainfreeze_studio.mcp import McpApp, make_server  # noqa: E402

AGENT = ROOT / "examples" / "invoice_router_agent.py"
SPEC = json.loads((ROOT / "translations" / "invoice_router.json").read_text())
TMP = Path(tempfile.mkdtemp(prefix="bf-translate-"))


def egg():
    rid = "rappid:@example/invoice-desk:" + "d" * 64
    files = {"rappid.json": rapp1.canonical({"schema": "rapp/1", "rappid": rid}).encode(),
             "soul.md": b"You are Invoice Desk.\n", "agents/invoice_router_agent.py": AGENT.read_bytes()}
    p = TMP / "desk.egg"
    p.write_bytes(rapp1.pack_egg("organism", rid, "2026-09-24T12:00:00.000Z", files=files,
                                 payload={"engine": {"name": "rapp-brainstem", "version": "0.6.16"}}))
    return p


class ParityTests(unittest.TestCase):
    def test_translation_matches_the_real_python_everywhere(self):
        r = flows.prove(SPEC, AGENT, "rapp_InvoiceDesk")
        self.assertTrue(r["parity"], r["mismatches"][:3])
        self.assertEqual(r["cases"], len(SPEC["vectors"]) * 4)            # every vector × every limit setting

    def test_plain_formatNumber_would_diverge_on_exact_ties(self):
        """The gate catches .NET's away-from-zero rounding against Python's half-to-even, which differ only where
        the double is exactly halfway (0.125 at two decimals)."""
        spec = json.loads(json.dumps(SPEC).replace("pyFormatNumber(step('amount'), 'N2')",
                                                   "formatNumber(step('amount'), 'N2', 'en-US')"))
        r = flows.prove(spec, AGENT, "rapp_InvoiceDesk")
        self.assertFalse(r["parity"])
        self.assertEqual({m["args"]["amount"] for m in r["mismatches"]}, {0.125, -0.125})

    def test_a_tie_test_on_the_rounded_product_would_diverge_just_above_ties(self):
        """11.005 is 11.00500000000000078… in binary, so Python prints 11.01; 11.005 * 100 still rounds to exactly
        1100.5, so treating that product's .5 fraction as a tie rounds it to even, 11.00. (Found by the code app
        test, which typed 18750.005 into the Invoice Router UI.)"""
        x = "step('amount')"
        naive = (f"formatNumber(if(and(equals(mod(mul({x}, 100), 1), 0.5), equals(mod(sub(mul({x}, 100), 0.5), 2), 0)), "
                 f"sub({x}, 0.00001), if(and(equals(mod(mul({x}, 100), 1), -0.5), equals(mod(add(mul({x}, 100), 0.5), 2), 0)), "
                 f"add({x}, 0.00001), {x})), 'N2', 'en-US')")
        spec = json.loads(json.dumps(SPEC).replace("pyFormatNumber(step('amount'), 'N2')", naive.replace("'", "\u0027")))
        r = flows.prove(spec, AGENT, "rapp_InvoiceDesk")
        self.assertFalse(r["parity"])
        self.assertEqual({m["args"]["amount"] for m in r["mismatches"]}, {11.005, 18750.005})

    def test_pyFormatNumber_is_python_formatting_on_every_kind_of_value(self):
        import random
        rnd = random.Random(7)
        values = ([float(f"{n}.005") for n in range(0, 2000)] + [n / 8 for n in range(-200, 200)] + [-0.5, -0.0, 2.5]
                  + [rnd.uniform(-1e6, 1e6) for _ in range(500)] + [round(rnd.uniform(-1e5, 1e5), 3) for _ in range(500)])
        ctx = {"trigger": {}, "outputs": {}, "parameters": {}, "now": "2026-09-24T00:00:00"}
        for d in (0, 1, 2, 3):
            for v in values:
                node = flows._macro(flows._Parser(f"pyFormatNumber({v!r}, 'N{d}')").expr())
                self.assertEqual(flows._eval(node, ctx), f"{v:,.{d}f}", (v, d))

    def test_a_wrong_rule_fails_the_gate(self):
        spec = copy.deepcopy(SPEC)
        spec["outputs"]["result"] = spec["outputs"]["result"].replace("greater(", "greaterOrEquals(")
        r = flows.prove(spec, AGENT, "rapp_InvoiceDesk")
        self.assertFalse(r["parity"])
        self.assertTrue(all(m["args"]["amount"] in (10000, 5000, 25000) or m["settings"] for m in r["mismatches"]))

    def test_compiled_flow_has_the_proven_agent_flow_shape(self):
        f = flows.compile_flow(SPEC, "rapp_InvoiceDesk")["properties"]["definition"]
        self.assertEqual((f["triggers"]["manual"]["type"], f["triggers"]["manual"]["kind"]), ("Request", "Skills"))
        resp = f["actions"]["Respond_to_agent"]
        self.assertEqual((resp["type"], resp["kind"]), ("Response", "Skills"))
        self.assertIn("Invoice approval limit (rapp_InvoiceApprovalLimit)", f["parameters"])
        for a in f["actions"].values():                                   # macros compiled away
            self.assertNotIn("pyFormatNumber", json.dumps(a))


class BuildTests(unittest.TestCase):
    def test_proven_translation_becomes_an_agent_flow(self):
        out = TMP / "b1"
        r = bs.build(egg(), out, "Invoice Desk", "rapp", translations=ROOT / "translations")
        self.assertEqual(r["live"], ["InvoiceRouter"])
        self.assertEqual(r["reasoning_only"], [])
        self.assertIn("capabilities/tools/InvoiceRouterFlow.mcs.yml", r["files"])
        prov = json.loads((out / "provenance.json").read_text())
        self.assertTrue(prov["parity"]["InvoiceRouter"]["parity"])
        self.assertEqual(prov["environment_variables"][0]["schemaName"], "rapp_InvoiceApprovalLimit")

    def test_failed_translation_is_refused_and_falls_back(self):
        bad = TMP / "bad-translations"
        bad.mkdir(exist_ok=True)
        spec = copy.deepcopy(SPEC)
        spec["outputs"]["result"] = spec["outputs"]["result"].replace("greater(", "greaterOrEquals(")
        (bad / "invoice_router.json").write_text(json.dumps(spec))
        out = TMP / "b2"
        r = bs.build(egg(), out, "Invoice Desk", "rapp", translations=bad)
        self.assertEqual(r["live"], [])
        self.assertEqual(r["reasoning_only"], ["invoice-router"])
        self.assertIn("failed parity", r["agents"][0]["note"])
        self.assertFalse(any("InvoiceRouterFlow" in f for f in r["files"]))

    @unittest.skipUnless(shutil.which("node") and Path(bs.__file__).exists(), "needs node")
    def test_the_sdk_reads_the_flow_tool(self):
        import os
        sdk = Path(os.path.expanduser(os.getenv("HARNESS_SDK_DIR", "~/Documents/GitHub/copilot-harness-sdk")))
        if not (sdk / "src" / "harness-provision.js").exists():
            self.skipTest("needs HARNESS_SDK_DIR")
        out = TMP / "b3"
        r = bs.build(egg(), out, "Invoice Desk", "rapp", translations=ROOT / "translations")
        script = (f"import {{ scanWorkspace, workflowIdFor }} from '{sdk}/src/harness-provision.js';"
                  f"const s = scanWorkspace({json.dumps(str(r['workspace']))});"
                  "console.log(JSON.stringify({ tools: s.tools.map(t => [t.kind, t.workflowId]),"
                  " folders: s.workflows.map(w => w.id), id: workflowIdFor('rapp_InvoiceDesk', 'InvoiceRouterFlow') }));")
        res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=sdk)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(got["tools"], [["WorkflowTool", got["id"]]])
        self.assertEqual(got["folders"], [got["id"]])


class _Echo:
    name = "Echo"

    def to_tool(self):
        return {"function": {"name": "Echo", "description": "Echo back.",
                             "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}

    def perform(self, text=""):
        if text == "boom":
            raise ValueError("bad input")
        return f"echo: {text}"


class McpFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(McpApp({"Echo": _Echo()}), "127.0.0.1", 0, api_key="k3y")
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/mcp"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, body, key="k3y"):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": key})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_protocol_round_trip(self):
        status, init = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual((status, init["result"]["protocolVersion"]), (200, "2025-06-18"))
        self.assertEqual(self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        tools = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]
        self.assertEqual(tools[0]["name"], "Echo")
        out = self.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "Echo", "arguments": {"text": "hi"}}})[1]["result"]
        self.assertEqual(out, {"content": [{"type": "text", "text": "echo: hi"}], "isError": False})
        err = self.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "Echo", "arguments": {"text": "boom"}}})[1]["result"]
        self.assertTrue(err["isError"])

    def test_api_key_required(self):
        self.assertEqual(self.call({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="wrong")[0], 401)

    def test_refuses_public_bind_without_key(self):
        with self.assertRaises(bs.StudioBuildError):
            make_server(McpApp({}), "0.0.0.0", 0, api_key=None)


if __name__ == "__main__":
    unittest.main()
