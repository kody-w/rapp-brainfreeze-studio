"""Name lookups the AIBAST library writes two ways: keyed dicts whose display name lives in a field other than
"name" (account, title, ...), and keys compared upper-cased with a second pass over names. A loose name the agent
accepts ("Northwind", "foods", "cust-003") must reach the same record in the compiled flow."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brainfreeze_studio.flows import compile_materialized, run_flow  # noqa: E402
from brainfreeze_studio.materialize import Runner, materialize  # noqa: E402

BASIC = "class BasicAgent:\n    def __init__(self, name=None, metadata=None):\n        self.name, self.metadata = name, metadata\n"
HEAD = '''from basic_agent import BasicAgent
class LookupAgent(BasicAgent):
    def __init__(self):
        self.name = "Lookup"
        self.metadata = {"name": "Lookup", "description": "Looks a record up.", "parameters": {"type": "object",
            "properties": {"who": {"type": "string"}}, "required": []}}
        super().__init__(name=self.name, metadata=self.metadata)
    def perform(self, who="", **kw):
        k = _resolve(who)
        return "Unknown." if k is None else "Record " + k + ": " + _DATA[k]["%s"]
'''
ACCOUNT = '''_DATA = {"northwind": {"account": "Northwind Energy", "tier": "gold"},
         "fabrikam": {"account": "Fabrikam Foods", "tier": "silver"}}
def _resolve(query):
    if not query:
        return "northwind"
    q = query.lower().strip()
    for key in _DATA:
        if key in q or q in _DATA[key]["account"].lower():
            return key
    return None
''' + HEAD % "account"
UPPER = '''_DATA = {"CUST-001": {"name": "Northwind Energy", "tier": "gold"},
         "CUST-002": {"name": "Fabrikam Foods", "tier": "silver"}}
def _resolve(query):
    if not query:
        return "CUST-001"
    q = query.upper().strip()
    for key in _DATA:
        if key in q:
            return key
    q_lower = query.lower()
    for key, c in _DATA.items():
        if q_lower in c["name"].lower():
            return key
    return None
''' + HEAD % "name"


class ResolverFieldTests(unittest.TestCase):
    def _same_records(self, source, names):
        d = Path(tempfile.mkdtemp())
        (d / "lookup_agent.py").write_text(source)
        (d / "basic_agent.py").write_text(BASIC)
        spec, rep = materialize(d / "lookup_agent.py", d / "basic_agent.py")
        self.assertTrue(rep["materialized"], rep.get("reasons"))
        k = next(x for x in spec["keying"] if x["input"] == "who")
        self.assertEqual(k["rule"], "resolver")
        flow = compile_materialized(spec, "rapp_Lookup")
        py = Runner(d / "lookup_agent.py", d / "basic_agent.py").run([{"who": n} for n in names])
        for n, want in zip(names, py):
            self.assertEqual(run_flow(flow, {"who": n})["result"], want, n)
        return k

    def test_display_name_in_another_field(self):
        self._same_records(ACCOUNT, ["Northwind", "northwind energy", "Foods", "the Fabrikam Foods bid", "", "xyz"])

    def test_upper_cased_keys_then_names(self):
        k = self._same_records(UPPER, ["CUST-002", "cust-002", "Fabrikam", "foods", "   ", "", "xyz"])
        self.assertTrue(k["resolver"].get("ci_key") and k["resolver"].get("name_raw"))


if __name__ == "__main__":
    unittest.main()
