"""brainfreeze-studio build tests. Offline; no Copilot Studio environment needed.

Core tests build their own eggs. Profile and SDK-parity tests also need:
  HARNESS_SDK_DIR   a copilot-harness-sdk checkout (profiles, and node for its own workspace scanner)
  GRAIL_AGENTS_DIR  the grail brainstem's agents/ folder (the real HackerNews / memory agents)
and are skipped when those are not present.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brainfreeze_studio as bs  # noqa: E402
from brainfreeze_studio import rapp1  # noqa: E402

SDK = Path(os.path.expanduser(os.getenv("HARNESS_SDK_DIR", "~/Documents/GitHub/copilot-harness-sdk")))
GRAIL = Path(os.path.expanduser(os.getenv("GRAIL_AGENTS_DIR", "~/.brainstem/src/rapp_brainstem/agents")))
HAVE_SDK = (SDK / "tutorial" / "profiles").is_dir()
HAVE_GRAIL = all((GRAIL / f).is_file() for f in ("hacker_news_agent.py", "manage_memory_agent.py", "context_memory_agent.py"))
HAVE_NODE = shutil.which("node") is not None
TMP = Path(tempfile.mkdtemp(prefix="bf-studio-test-"))
UTC = "2026-09-24T12:00:00.000Z"
RID = "rappid:@example/test-desk:" + "c" * 64
ENV = "https://example.crm.dynamics.com/"

ROUTER = '''from agents.basic_agent import BasicAgent
TOOL = "InvoiceRouter"
class InvoiceRouterAgent(BasicAgent):
    def __init__(self):
        self.name = TOOL
        self.metadata = {"name": TOOL, "description": "Routes one invoice to a queue.",
                         "parameters": {"type": "object", "properties": {"amount": {"type": "number"}},
                                        "required": ["amount"]}}
        super().__init__(name=self.name, metadata=self.metadata)
    def perform(self, amount=0, **kw):
        raise SystemExit("egg code must never run during a build")
'''


def egg(files, name="organism.egg"):
    base = {"rappid.json": rapp1.canonical({"schema": "rapp/1", "rappid": RID}).encode(),
            "soul.md": b"You are Invoice Desk, a brisk accounts-payable assistant.\n"}
    base.update(files)
    path = TMP / name
    path.write_bytes(rapp1.pack_egg("organism", RID, UTC, files=base,
                                    payload={"engine": {"name": "rapp-brainstem", "version": "0.6.16"}}))
    return path


def session(turns):
    path = TMP / "s.session.egg"
    path.write_bytes(rapp1.pack_egg("session", RID, UTC, payload={"runtime": "rapp-brainstem/0.6.16",
                                                                   "transcript": turns}))
    return path


def build(e, **kw):
    out = Path(tempfile.mkdtemp(dir=TMP))
    args = dict(name="Invoice Desk", publisher_prefix="rapp")
    args.update(kw)
    return bs.build(e, out, **args), out


class CoreTests(unittest.TestCase):
    def test_soul_becomes_harness_instructions(self):
        r, out = build(egg({"agents/invoice_router_agent.py": ROUTER.encode()}))
        settings = (r["workspace"] / "settings.mcs.yml").read_text()
        self.assertIn("template: cliagent-1.0.0", settings)
        self.assertIn("kind: CLICopilotRecognizer", settings)
        self.assertIn("You are Invoice Desk, a brisk accounts-payable assistant.", settings)
        self.assertIn("schemaName: rapp_InvoiceDesk", settings)
        self.assertNotIn("manage-memory", settings)          # nothing about skills that aren't deployed
        self.assertNotIn("Hacker News", settings)

    def test_unmatched_agent_becomes_reasoning_only_skill_carrying_its_code(self):
        r, _ = build(egg({"agents/invoice_router_agent.py": ROUTER.encode()}))
        skill = (r["workspace"] / "behaviors" / "rapp_invoice-router.mcs.yml").read_text()
        self.assertIn("kind: InlineAgentSkill", skill)
        self.assertIn("Never claim that the code ran", skill)
        self.assertIn("class InvoiceRouterAgent(BasicAgent):", skill)
        self.assertIn('"required":["amount"]', skill)
        self.assertEqual(r["reasoning_only"], ["invoice-router"])

    def test_contract_is_read_without_running_egg_code(self):
        c = bs.read_contract(ROUTER)                          # perform() would SystemExit if it ran
        self.assertEqual((c["name"], c["description"]), ("InvoiceRouter", "Routes one invoice to a queue."))

    def test_session_becomes_proof_and_reference_answers(self):
        s = session([{"role": "user", "content": "Route Fabrikam $18,750."},
                     {"role": "assistant", "content": "APPROVAL queue."}])
        r, out = build(egg({}), session=s)
        proof = json.loads((out / "proof.json").read_text())
        self.assertEqual(proof, {"schemaName": "rapp_InvoiceDesk", "turns": [
            {"prompt": "Route Fabrikam $18,750.", "expect": [], "component": "conversation"}]})
        self.assertEqual(json.loads((out / "reference-answers.json").read_text())[0]["original_answer"],
                         "APPROVAL queue.")

    def test_memories_are_exported_for_seeding(self):
        mem = {"a1": {"message": "Approval limit is $10,000", "theme": "fact", "importance": 4, "tags": ["ap"]}}
        r, out = build(egg({".brainstem_data/shared_memories/memory.json": json.dumps(mem).encode()}))
        rows = json.loads((out / "memory-seed.json").read_text())
        self.assertEqual(rows[0]["content"], "Approval limit is $10,000")
        self.assertEqual((rows[0]["scope"], rows[0]["memory_type"], rows[0]["importance"]), ("shared", "fact", 4))

    def test_provenance_records_lineage(self):
        e = egg({})
        r, out = build(e)
        prov = json.loads((out / "provenance.json").read_text())
        manifest, _ = rapp1.read_egg(e.read_bytes())
        self.assertEqual(prov["egg"]["rappid"], RID)
        self.assertEqual(prov["egg"]["address"], rapp1.egg_address(manifest))

    def test_tampered_or_wrong_eggs_are_refused(self):
        blob = bytearray(egg({}).read_bytes())
        i = blob.find(b"brisk")
        blob[i] ^= 1
        bad = TMP / "bad.egg"
        bad.write_bytes(bytes(blob))
        with self.assertRaises(bs.StudioBuildError):
            build(bad)
        with self.assertRaises(bs.StudioBuildError):
            build(egg({}), session=egg({}, "not-a-session.egg"))

    def test_guards_on_names(self):
        with self.assertRaises(bs.StudioBuildError):
            build(egg({}), name="x" * 43)
        with self.assertRaises(bs.StudioBuildError):
            build(egg({}), publisher_prefix="Bad-Prefix")

    def test_deterministic(self):
        e = egg({"agents/invoice_router_agent.py": ROUTER.encode()})
        a, _ = build(e)
        b, _ = build(e)
        read = lambda r: {f: (r["workspace"] / f).read_bytes() for f in r["files"]}
        self.assertEqual(read(a), read(b))


@unittest.skipUnless(HAVE_SDK and HAVE_GRAIL, "needs HARNESS_SDK_DIR and GRAIL_AGENTS_DIR")
class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.egg = egg({f"agents/{f}": (GRAIL / f).read_bytes() for f in
                       ("hacker_news_agent.py", "manage_memory_agent.py", "context_memory_agent.py")}
                      | {"agents/invoice_router_agent.py": ROUTER.encode()}, "grail.egg")

    def test_real_agents_match_the_proven_profiles(self):
        r, _ = build(self.egg, sdk_dir=SDK, environment=ENV, hn_api_name="shared_rapp-20hacker-20news-5fexample")
        self.assertEqual(sorted(r["routing"]), ["hackernews", "memory-recall", "memory-write"])
        self.assertEqual(r["reasoning_only"], ["invoice-router"])
        files = set(r["files"])
        for f in ("capabilities/tools/HackerNewsWorkflow.mcs.yml", "behaviors/rapp_fetch-hacker-news.mcs.yml",
                  "behaviors/rapp_manage-memory.mcs.yml", "capabilities/tools/rapp_dataverse-add-memory.mcs.yml",
                  "behaviors/rapp_recall-memory.mcs.yml", "capabilities/tools/rapp_dataverse-list-memories.mcs.yml",
                  "infrastructure/connections/rapp_InvoiceDesk.cr.shared_rapp_hn.sync.yaml"):
            self.assertIn(f, files)
        text = "".join((r["workspace"] / f).read_text() for f in r["files"])
        self.assertNotIn("{{", text)                                        # every placeholder filled
        settings = (r["workspace"] / "settings.mcs.yml").read_text()
        self.assertIn(ENV, settings)
        self.assertIn("You are Invoice Desk, a brisk accounts-payable assistant.", settings)

    def test_profiles_need_their_inputs_or_fall_back_honestly(self):
        r, _ = build(self.egg, sdk_dir=SDK)                                  # no environment, no connector
        self.assertEqual(r["routing"], [])
        notes = {a["name"]: a.get("note", "") for a in r["agents"]}
        self.assertIn("--environment", notes["ManageMemory"])
        self.assertIn("--hn-api-name", notes["HackerNews"])

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_the_sdk_reads_the_workspace_and_agrees_on_flow_ids(self):
        r, _ = build(self.egg, sdk_dir=SDK, environment=ENV, hn_api_name="shared_rapp-20hacker-20news-5fexample")
        script = (f"import {{ scanWorkspace, expectedComponents, workflowIdFor }} from '{SDK}/src/harness-provision.js';"
                  f"const s = scanWorkspace({json.dumps(str(r['workspace']))});"
                  f"console.log(JSON.stringify({{ tools: s.tools.map(t => [t.name, t.kind]),"
                  f" components: expectedComponents({json.dumps(str(r['workspace']))}, 'rapp_InvoiceDesk').length,"
                  f" wf: workflowIdFor('rapp_InvoiceDesk', 'RAPPHackerNewsWorkflow') }}));")
        res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=SDK)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout.strip().splitlines()[-1])
        kinds = dict(got["tools"])
        self.assertEqual(kinds.get("HackerNewsWorkflow"), "WorkflowTool")
        self.assertEqual(got["wf"], bs.workflow_id_for("rapp_InvoiceDesk", "RAPPHackerNewsWorkflow"))
        self.assertGreaterEqual(got["components"], 3)


class VendorTests(unittest.TestCase):
    def test_reference_implementation_is_verbatim(self):
        import hashlib
        pkg = Path(bs.__file__).parent
        meta = json.loads((pkg / "rapp1.vendor.json").read_text())
        self.assertEqual(hashlib.sha256((pkg / "rapp1.py").read_bytes()).hexdigest(), meta["sha256"])


if __name__ == "__main__":
    unittest.main()
