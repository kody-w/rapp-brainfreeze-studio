"""Materialized translation tests. Offline; each test runs a small fixture agent in the sandboxed runner."""
import copy
import hashlib
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brainfreeze_studio import flows  # noqa: E402
from brainfreeze_studio.flows import compile_flow, prove_materialized, run_flow  # noqa: E402
from brainfreeze_studio.materialize import CLOCKS, MaterializeError, materialize  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="bf-materialize-test-"))
BASIC = TMP / "basic_agent.py"
BASIC.write_text('''class BasicAgent:
    def __init__(self, name=None, metadata=None):
        self.name, self.metadata = name, metadata
''')

DESK = '''from datetime import date, timedelta
from basic_agent import BasicAgent

_ACCOUNTS = {
    "contoso": {"name": "Contoso Ltd", "tier": "Gold", "credit": 1200},
    "fabrikam": {"name": "Fabrikam Inc", "tier": "Silver", "credit": 300},
}


def _resolve(query):
    if not query:
        return "contoso"
    q = query.lower().strip()
    for key in _ACCOUNTS:
        if key in q or q in _ACCOUNTS[key]["name"].lower():
            return key
    return "contoso"


class DeskAgent(BasicAgent):
    def __init__(self):
        self.name = "DeskAgent"
        self.metadata = {"name": "DeskAgent", "description": "A synthetic account desk.",
                         "parameters": {"type": "object", "properties": {
                             "operation": {"type": "string",
                                           "enum": ["summary", "note", "renewal", "credit_check", "seats"]},
                             "account": {"type": "string"}, "note": {"type": "string"},
                             "amount": {"type": "number"}, "seats": {"type": "number"}},
                             "required": ["operation"]}}
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        op = kwargs.get("operation", "summary")
        acct = _ACCOUNTS[_resolve(kwargs.get("account", ""))]
        if op == "summary":
            return f"# {acct['name']}\\nTier: {acct['tier']}"
        if op == "note":
            return f"Note for {acct['name']}: {kwargs.get('note', '(none)')}"
        if op == "renewal":
            today = date.today()
            return (f"Renewal review for {acct['name']} on {today.isoformat()}; "
                    f"next check {(today + timedelta(days=7)).isoformat()}")
        if op == "credit_check":
            amount = kwargs.get("amount")
            if amount is None:
                return "Give an amount."
            return f"{acct['name']}: {'within' if amount <= acct['credit'] else 'over'} the credit line"
        if op == "seats":
            seats = kwargs.get("seats", 10)
            try:
                seats = float(seats)
            except (TypeError, ValueError):
                seats = 10
            return f"{acct['name']}: {seats:g} seats, {acct['credit'] - seats:g} credit left"
        return f"Unknown operation: {op}"
'''

STATEFUL = '''from basic_agent import BasicAgent

class CounterAgent(BasicAgent):
    def __init__(self):
        self.calls = 0
        self.metadata = {"name": "Counter", "description": "Counts calls.",
                         "parameters": {"type": "object", "properties": {
                             "operation": {"type": "string", "enum": ["tick", "show"]}}, "required": ["operation"]}}
        super().__init__(name="Counter", metadata=self.metadata)

    def perform(self, **kwargs):
        self.calls += 1
        return f"{kwargs.get('operation')} #{self.calls}"
'''

AGENT = TMP / "desk_agent.py"
AGENT.write_text(DESK)
HAND = {
    "source_sha256": hashlib.sha256(DESK.encode("utf-8")).hexdigest(),
    "operations": {"seats": {
        "why": "prints the seats and the credit left after them.",
        "constants": {"Credit": {"contoso": 1200, "fabrikam": 300, "\u27e8?\u27e9": 1200}},
        "derived": [["D_Valid", "isFloat(coalesce(triggerBody()?['seats'], ''))"],
                    ["D_Seats", "float(if(outputs('D_Valid'), triggerBody()?['seats'], '10'))"]],
        "computed": {"name": "seat_count", "expr": "if(outputs('D_Valid'), 'given', 'default')",
                     "examples": {"default": {}, "given": {"seats": 7.3125}},
                     "markers": {"given": {"seats": 2.5625}}},
        "fills": [{"token": "ZQDSEATSQZ", "expr": "formatNumber(outputs('D_Seats'), 'g6')"},
                  {"token": "ZQDCREDITQZ",
                   "expr": "formatNumber(sub(outputs('Const_Credit')?[outputs('State_account')], outputs('D_Seats')), 'g6')"}],
        "probes": {"seats": [None, 0, 1, 2.5, 10, 99, 1200, 5000]},
    }},
}
_SPEC = {}


def desk_spec():
    if not _SPEC:
        spec, report = materialize(AGENT, BASIC, hand=copy.deepcopy(HAND))
        _SPEC.update(spec=spec, report=report)
    return _SPEC["spec"], _SPEC["report"]


def answer(trigger, now=CLOCKS[0]):
    spec, _ = desk_spec()
    return run_flow(compile_flow(spec, "rapp_Test"), trigger, now=now)["result"]


class MaterializeTests(unittest.TestCase):
    def test_flow_matches_the_python_on_every_vector_and_clock(self):
        spec, report = desk_spec()
        self.assertIsNotNone(spec, report.get("reasons"))
        proof = prove_materialized(spec, AGENT, BASIC)
        self.assertTrue(proof["parity"], proof["mismatches"][:2])
        self.assertEqual(proof["clocks"], list(CLOCKS))

    def test_inputs_are_learned_as_the_library_idioms(self):
        _, report = desk_spec()
        rules = {n: i["rule"] for n, i in report["inputs"].items()}
        self.assertEqual(rules["operation"], "exact")
        self.assertEqual(rules["account"], "resolver")
        self.assertEqual(rules["note"], "echo")
        self.assertEqual((rules["amount"], rules["seats"]), ("computational", "computational"))
        self.assertEqual(answer({"operation": "summary", "account": "fab"}), "# Fabrikam Inc\nTier: Silver")
        self.assertEqual(answer({"operation": "note", "account": "Contoso", "note": "call back Tuesday"}),
                         "Note for Contoso Ltd: call back Tuesday")
        self.assertEqual(answer({"operation": "audit"}), "Unknown operation: audit")

    def test_dates_follow_the_flow_clock_including_offsets(self):
        self.assertEqual(answer({"operation": "renewal", "account": "fabrikam"}, now="2027-01-05T07:05:09"),
                         "Renewal review for Fabrikam Inc on 2027-01-05; next check 2027-01-12")

    def test_an_operation_computed_from_numbers_is_refused_without_a_hand_translation(self):
        spec, _ = desk_spec()
        self.assertEqual(spec["blocked_operations"], {"credit_check": ["amount"]})
        self.assertIn("Not available in this deployment", answer({"operation": "credit_check", "amount": "5"}))

    def test_a_hand_translation_computes_the_numbers_in_the_flow(self):
        spec, _ = desk_spec()
        self.assertIn("seats", spec["hand_operations"])
        self.assertEqual(answer({"operation": "seats", "account": "fabrikam", "seats": "12.5"}),
                         "Fabrikam Inc: 12.5 seats, 287.5 credit left")
        self.assertEqual(answer({"operation": "seats", "account": "Contoso"}), "Contoso Ltd: 10 seats, 1190 credit left")
        self.assertEqual(answer({"operation": "seats", "seats": "lots"}), "Contoso Ltd: 10 seats, 1190 credit left")

    def test_a_hand_translation_for_other_source_is_refused(self):
        stale = dict(copy.deepcopy(HAND), source_sha256="0" * 64)
        with self.assertRaises(MaterializeError):
            materialize(AGENT, BASIC, hand=stale)

    def test_an_agent_that_keeps_state_between_calls_is_refused(self):
        path = TMP / "counter_agent.py"
        path.write_text(STATEFUL)
        spec, report = materialize(path, BASIC)
        self.assertIsNone(spec)
        self.assertTrue(any("keeps state" in r for r in report["reasons"]), report["reasons"])

    def test_the_tool_description_carries_the_values_the_orchestrator_must_pass(self):
        spec, _ = desk_spec()
        desc = flows.tool_description(dict(spec, description="A synthetic account desk. " * 60))
        self.assertLessEqual(len(desc), flows.TOOL_DESCRIPTION_LIMIT)
        self.assertIn("Set `operation` to exactly one of: credit_check, note, renewal, seats, summary.", desc)
        self.assertIn(f"workflowId: wf-1", flows.tool_yaml(spec, "wf-1"))

    def test_proofs_run_agent_code_under_the_python_the_spec_was_made_with(self):
        import os
        from unittest import mock
        from brainfreeze_studio.materialize import agent_python
        spec, _ = desk_spec()
        here = ".".join(map(str, sys.version_info[:2]))
        self.assertTrue(spec["materialized_with"]["python"].startswith(here + "."))
        self.assertEqual(agent_python(here + ".0"), sys.executable)
        self.assertEqual(agent_python(None), sys.executable)
        with mock.patch("shutil.which", return_value="/opt/python3.9") as which:
            self.assertEqual(agent_python("3.9.1") if here != "3.9" else "/opt/python3.9", "/opt/python3.9")
        with mock.patch.dict(os.environ, {"BFS_AGENT_PYTHON": "/custom/python"}):
            self.assertEqual(agent_python("3.9.1"), "/custom/python")

    def test_expressions_over_the_power_automate_limit_are_refused(self):
        spec, _ = desk_spec()
        flow = compile_flow(spec, "rapp_Test")
        flow["properties"]["definition"]["actions"]["Text"]["inputs"] = "@concat('" + "x" * 9000 + "')"
        with self.assertRaises(ValueError):
            flows._within_limits(flow)


# An agent over a dataset it loads by path (the shape of an agent that reads its site's exports): the SKUs come from
# the data, not the code, and each answer is also written back into the data folder as a report.
CATALOG = '''import csv, os
from basic_agent import BasicAgent
DATA = os.environ.get("CATALOG_DIR", "/nonexistent")


class CatalogAgent(BasicAgent):
    def __init__(self):
        self.metadata = {"name": "Catalog", "description": "Looks up a SKU in the catalog export.",
                         "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": []}}
        super().__init__(name="Catalog", metadata=self.metadata)

    def perform(self, sku="", **kw):
        rows = {r["sku"]: r for r in csv.DictReader(open(os.path.join(DATA, "catalog.csv")))}
        if not sku:
            text = f"{len(rows)} SKUs in the {os.environ.get('CATALOG_MODE', '?')} catalog"
        elif sku in rows:
            text = f"{sku}: {rows[sku]['name']} at ${float(rows[sku]['price']):,.2f}"
        else:
            text = f"no SKU {sku}"
        os.makedirs(os.path.join(DATA, "reports"), exist_ok=True)
        open(os.path.join(DATA, "reports", "last.txt"), "w").write(text)
        return text
'''
CATALOG_AGENT = TMP / "catalog_agent.py"
CATALOG_AGENT.write_text(CATALOG)


def catalog_folder(rows=(("CAB-288", "Fiber cable, 288 count", "4.10"), ("HH-1730", "Handhole 17x30", "212.00"))):
    folder = Path(tempfile.mkdtemp(prefix="bf-catalog-", dir=TMP))
    (folder / "catalog.csv").write_text("sku,name,price\n" + "".join(",".join(r) + "\n" for r in rows))
    return folder


class PinnedDataTests(unittest.TestCase):
    def test_an_agent_that_reads_files_is_refused_unless_its_data_is_pinned(self):
        folder = catalog_folder()
        spec, report = materialize(CATALOG_AGENT, BASIC, env={"CATALOG_DIR": str(folder)})
        self.assertIsNone(spec)
        self.assertIn("files", report["effects"])

    def test_pinned_data_is_digested_copied_and_proven_on_that_exact_data(self):
        folder = catalog_folder()
        before = sorted(p.name for p in folder.rglob("*"))
        spec, report = materialize(CATALOG_AGENT, BASIC, env={"CATALOG_MODE": "export"},
                                   data={"CATALOG_DIR": folder}, values={"sku": ["CAB-288", "HH-1730"]})
        self.assertIsNotNone(spec, report.get("reasons"))
        self.assertEqual(sorted(p.name for p in folder.rglob("*")), before)          # it wrote into its copy only
        self.assertEqual(spec["data"]["CATALOG_DIR"]["files"], 1)
        self.assertEqual(spec["env"], {"CATALOG_MODE": "export"})
        keying = {k["input"]: k for k in spec["keying"]}
        self.assertEqual(sorted(keying["sku"]["values"]), ["CAB-288", "HH-1730"])
        proof = prove_materialized(spec, CATALOG_AGENT, BASIC)
        self.assertTrue(proof["parity"], proof["mismatches"][:2])
        flow = compile_flow(spec, "rapp_Test")
        self.assertEqual(run_flow(flow, {"sku": "HH-1730"})["result"], "HH-1730: Handhole 17x30 at $212.00")
        self.assertEqual(run_flow(flow, {})["result"], "2 SKUs in the export catalog")
        self.assertEqual(run_flow(flow, {"sku": "XYZ"})["result"], "no SKU XYZ")

    def test_changed_or_missing_data_is_refused_and_an_identical_copy_elsewhere_is_accepted(self):
        folder = catalog_folder()
        spec, _ = materialize(CATALOG_AGENT, BASIC, data={"CATALOG_DIR": folder}, values={"sku": ["CAB-288", "HH-1730"]})
        moved = catalog_folder()                                  # the same bytes in another place
        with unittest.mock.patch.dict("os.environ", {"BFS_DATA_CATALOG_DIR": str(moved)}):
            self.assertTrue(prove_materialized(spec, CATALOG_AGENT, BASIC)["parity"])
        (folder / "catalog.csv").write_text((folder / "catalog.csv").read_text().replace("212.00", "199.00"))
        refused = prove_materialized(spec, CATALOG_AGENT, BASIC)
        self.assertFalse(refused["parity"])
        self.assertIn("changed since it was materialized", refused["reason"])
        gone = dict(spec, data={"CATALOG_DIR": dict(spec["data"]["CATALOG_DIR"], path=str(TMP / "nowhere"))})
        self.assertIn("isn't here", prove_materialized(gone, CATALOG_AGENT, BASIC)["reason"])

    def test_an_agent_with_no_inputs_is_not_sampled_over_and_over(self):
        folder = catalog_folder()
        agent = TMP / "catalog_count_agent.py"
        agent.write_text(CATALOG.replace('"properties": {"sku": {"type": "string"}}', '"properties": {}'))
        spec, report = materialize(agent, BASIC, data={"CATALOG_DIR": folder})
        self.assertIsNotNone(spec, report.get("reasons"))
        self.assertEqual(spec["materialized_with"]["unique_outputs"], 1)
        self.assertLess(report["runner_calls"], 60)          # one sample, not 600 copies of the same call

    def test_without_its_values_an_id_from_the_data_would_only_look_unknown(self):
        folder = catalog_folder()
        spec, _ = materialize(CATALOG_AGENT, BASIC, data={"CATALOG_DIR": folder})
        keying = {k["input"]: k for k in spec["keying"]}
        self.assertNotIn("HH-1730", keying["sku"].get("values") or [])  # why values= exists: name them from the data


class RecordedMaterializedProofTests(unittest.TestCase):
    """A hosted build (the Azure Function) holds no one's dataset and may run no agent code: it lays a materialized
    flow only on a proof recorded, where the data is, for the exact agent, BasicAgent and spec."""

    def setUp(self):
        import json
        import shutil
        from brainfreeze_studio import rapp1
        from brainfreeze_studio.flows import record_materialized
        self.work = Path(tempfile.mkdtemp(prefix="bf-recorded-", dir=TMP))
        self.folder = catalog_folder()
        spec, _ = materialize(CATALOG_AGENT, BASIC, data={"CATALOG_DIR": self.folder},
                              values={"sku": ["CAB-288", "HH-1730"]})
        self.translations = self.work / "translations"
        self.translations.mkdir()
        (self.translations / "catalog.json").write_text(json.dumps(spec))
        spec["_dir"], spec["_file"] = str(self.translations), "catalog.json"
        proof = prove_materialized(spec, CATALOG_AGENT, BASIC)
        record = record_materialized(spec, proof, hashlib.sha256(BASIC.read_bytes()).hexdigest(), "2026-09-25")
        (self.translations / "catalog.proof.json").write_text(json.dumps(record))
        rid = "rappid:@example/catalog-desk:" + "e" * 64
        files = {"rappid.json": rapp1.canonical({"schema": "rapp/1", "rappid": rid}).encode(),
                 "soul.md": b"You are the catalog desk.\n", "agents/catalog_agent.py": CATALOG.encode(),
                 "agents/basic_agent.py": BASIC.read_bytes()}
        self.egg = self.work / "catalog.egg"
        self.egg.write_bytes(rapp1.pack_egg("organism", rid, "2026-09-25T12:00:00.000Z", files=files,
                                            payload={"engine": {"name": "rapp-brainstem", "version": "0.6.16"}}))
        shutil.rmtree(self.folder)                              # as in the service: the dataset isn't there

    def build(self, **kw):
        import brainfreeze_studio as bs
        out = Path(tempfile.mkdtemp(prefix="bf-recorded-out-", dir=self.work))
        r = bs.build(self.egg, out, "Catalog Desk", "rapp", translations=self.translations, **kw)
        return r["agents"][0], out

    def test_without_its_data_the_flow_is_laid_on_the_recorded_proof(self):
        import json
        agent, out = self.build()
        self.assertEqual(agent["as"], "agent flow (materialized, parity proven)", agent.get("note"))
        parity = json.loads((out / "parity" / "CatalogFlow.json").read_text())
        self.assertEqual((parity["recorded"], parity["passed"], parity["cases"] > 0), ("2026-09-25", parity["cases"], True))
        self.assertEqual(self.build(run_proofs=False)[0]["as"], "agent flow (materialized, parity proven)")

    def test_a_record_for_other_bytes_or_no_record_is_refused(self):
        import json
        spec = json.loads((self.translations / "catalog.json").read_text())
        spec["description"] += " Edited."
        (self.translations / "catalog.json").write_text(json.dumps(spec))
        agent, _ = self.build()
        self.assertIn("no proof was recorded for these exact bytes (catalog.proof.json)", agent["note"])
        self.assertIn("isn't here", agent["note"])
        (self.translations / "catalog.proof.json").unlink()
        self.assertIn("can't be proven here (no agent code may run here)", self.build(run_proofs=False)[0]["note"])


if __name__ == "__main__":
    unittest.main()
