"""Documents: an agent that writes files under RAPP_OUTPUT_DIR is materialized with their bytes, its flow creates them
in SharePoint, and the proof compares every document's bytes, not just the text. Writing anywhere else is refused."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brainfreeze_studio.flows import compile_materialized, prove_materialized  # noqa: E402
from brainfreeze_studio.materialize import DOC_MARK, materialize, split_documents  # noqa: E402

BASIC = "class BasicAgent:\n    def __init__(self, name=None, metadata=None):\n        self.name, self.metadata = name, metadata\n"
AGENT = '''import os
from basic_agent import BasicAgent

class DeskAgent(BasicAgent):
    def __init__(self):
        self.name = "Desk"
        self.metadata = {"name": "Desk", "description": "Notes and saved briefs.", "parameters": {"type": "object",
            "properties": {"operation": {"type": "string", "enum": ["note", "save_brief"]},
                           "account": {"type": "string", "enum": ["Northwind", "Fabrikam"]}},
            "required": ["operation"]}}
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, operation="note", account="Northwind", **kw):
        if account not in ("Northwind", "Fabrikam"):
            return "Unknown account."
        if operation == "save_brief":
            root = os.environ.get("RAPP_OUTPUT_DIR") or os.path.expanduser("~/Documents/RAPP Documents")
            os.makedirs(os.path.join(root, "Briefs"), exist_ok=True)
            with open(os.path.join(root, "Briefs", account + " brief.txt"), "wb") as f:
                f.write(("Brief for " + account + "\\n").encode())
            return "Saved Briefs/" + account + " brief.txt"
        return "Note for " + account
'''
OUTSIDE = AGENT.replace('root = os.environ.get("RAPP_OUTPUT_DIR") or ', 'root = ')


class DocumentTests(unittest.TestCase):
    def _files(self, source):
        d = Path(tempfile.mkdtemp())
        (d / "desk_agent.py").write_text(source)
        (d / "basic_agent.py").write_text(BASIC)
        return d / "desk_agent.py", d / "basic_agent.py"

    def test_documents_are_tabled_compiled_and_proven_by_their_bytes(self):
        agent, basic = self._files(AGENT)
        spec, rep = materialize(agent, basic)
        self.assertTrue(rep["materialized"], rep.get("reasons"))
        self.assertTrue(spec.get("documents"))
        saved = [split_documents(base64.b64decode(v).decode()) for v in spec["table_outputs"].values()]
        docs = [d for _, d in saved if d]
        self.assertEqual(sorted(d["documents"][0]["path"] for d in docs), ["Briefs/Fabrikam brief.txt", "Briefs/Northwind brief.txt"])
        flow = compile_materialized(spec, "rapp_Desk")
        acts = flow["properties"]["definition"]["actions"]
        self.assertEqual(acts["Save_documents"]["actions"]["Create_file"]["inputs"]["host"]["operationId"], "CreateFile")
        self.assertIn("shared_sharepointonline", flow["properties"]["connectionReferences"])
        self.assertTrue(prove_materialized(spec, agent, basic, "rapp_Desk")["parity"])
        # one changed byte inside a saved document fails the proof
        k = next(k for k, v in spec["table_outputs"].items() if DOC_MARK in base64.b64decode(v).decode())
        text, d = split_documents(base64.b64decode(spec["table_outputs"][k]).decode())
        d["data"][0] = base64.b64encode(base64.b64decode(d["data"][0]).replace(b"Brief", b"Brieg")).decode()
        spec["table_outputs"][k] = base64.b64encode((text + DOC_MARK + json.dumps(d, sort_keys=True, separators=(",", ":"))).encode()).decode()
        self.assertFalse(prove_materialized(spec, agent, basic, "rapp_Desk")["parity"])

    def test_writing_outside_the_output_folder_is_still_refused(self):
        agent, basic = self._files(OUTSIDE)
        spec, rep = materialize(agent, basic)
        self.assertFalse(rep["materialized"])
        self.assertIn("files", " ".join(rep["reasons"]))

    def test_specs_without_documents_compile_as_before(self):
        agent, basic = self._files(AGENT.replace('"enum": ["note", "save_brief"]', '"enum": ["note"]')
                                   .replace('if operation == "save_brief":', 'if False:'))
        spec, rep = materialize(agent, basic)
        self.assertTrue(rep["materialized"])
        self.assertNotIn("documents", spec)
        flow = compile_materialized(spec, "rapp_Desk")
        self.assertNotIn("Save_documents", flow["properties"]["definition"]["actions"])
        self.assertEqual(flow["properties"]["connectionReferences"], {})


if __name__ == "__main__":
    unittest.main()
