"""Code apps, headless: a packaged rapplication runs under the default code app content security policy, inside a
stand-in Power Apps player, with a stand-in SDK (tests/codeapp_harness.py).

Needs Playwright for Python (with its Chromium) plus Node and npm, which build the host; skipped otherwise:
    python3 -m pip install playwright && python3 -m playwright install chromium
    python3 -m unittest tests.test_codeapp_browser -v
The RAPP Store cases also need a RAPP_Store checkout: BFS_RAPP_STORE=~/src/RAPP_Store.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from brainfreeze_studio import codeapp, rapplication  # noqa: E402
from test_rapplication import EXAMPLE, TRANSLATIONS, run_python_agent  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
STORE = os.path.expanduser(os.getenv("BFS_RAPP_STORE", ""))
HAVE_STORE = bool(STORE) and Path(STORE, "index.json").is_file()
READY = sync_playwright is not None and shutil.which("node") and shutil.which("npm")


def twin_definitions(out):
    return {codeapp.data_source_name(d["name"]): d["definition"]
            for d in (json.loads(f.read_text()) for f in sorted((out / "powerapps-flows").glob("*.json")))}


@unittest.skipUnless(READY, "needs Playwright for Python, node and npm")
class CodeAppBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import codeapp_harness as harness
        cls.h = harness
        cls.tmp = Path(tempfile.mkdtemp(prefix="bfs-browser-test-"))
        cls.host = harness.build_test_host(cls.tmp / "host-test.js")
        cls.pw = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def open(self, where, copilot=None, **kw):
        out = self.tmp / re.sub(r"[^a-z0-9]+", "-", str(where).lower()).strip("-")[-40:]
        summary = rapplication.prepare(where, out, host_js=self.host, **kw)
        player = self.h.Player(out / "codeapp" / "dist", twin_definitions(out), copilot=copilot)
        base = player.start()
        self.addCleanup(player.stop)
        browser, page, log = self.h.open_player(self.pw, base)
        self.addCleanup(browser.close)
        ui = page.frame_locator("#app").frame_locator("#rapp")
        return summary, player, page, ui, log

    def assertClean(self, log):
        self.assertEqual(log["violations"], [], "content security policy violations")
        self.assertEqual(log["errors"], [], "uncaught errors")

    # Proof A: the UI's /chat call runs the agent's Power Apps flow, and the answer is the Python's, exactly
    def test_the_invoice_router_ui_gets_the_agents_exact_output_through_its_flow(self):
        summary, player, page, ui, log = self.open(EXAMPLE, translations=str(TRANSLATIONS), fetch_vendor=None)
        cases = [("Northwind Traders", "4200"), ("Litware", "10000.01"), ("Contoso & Sons", "10000"), ("Fabrikam", "18750.005")]
        want = run_python_agent(EXAMPLE / "singleton" / "invoice_router_agent.py",
                                [{"args": {"vendor": v, "amount": a}, "env": {}} for v, a in cases])
        for (vendor, amount), python in zip(cases, want):
            ui.locator("#vendor").fill(vendor)
            ui.locator("#amount").fill(amount)
            ui.locator("#go").click()
            ui.locator("#result", has_text=vendor).wait_for(timeout=15000)
            self.assertEqual(ui.locator("#result").inner_text(), python)
        runs = [c for c in player.calls if c["operation"] == "Run"]
        self.assertEqual(len(runs), len(cases))
        self.assertEqual({c["table"] for c in runs}, {codeapp.data_source_name(summary["powerapps_flows"][0]["name"])})
        self.assertEqual(runs[1]["parameters"]["input"], {"vendor": "Litware", "amount": "10000.01"})
        self.assertFalse([c for c in player.calls if c["operation"] != "Run"], "nothing went to the agent")
        self.assertClean(log)

    @unittest.skipUnless(HAVE_STORE, "set BFS_RAPP_STORE to a RAPP_Store checkout")
    def test_json_doctor_asks_its_agent_for_the_exact_tool_call(self):
        reply = json.dumps({"status": "success", "action": "inspect", "records": 3, "root_type": "array", "bytes": 120,
                            "fields": {"id": {"types": ["int"], "coverage": "100%", "optional": False}}, "note": "stand-in"})
        summary, player, page, ui, log = self.open("@rapp/json_doctor", copilot=lambda m: reply, store=STORE)
        ui.locator("#path").fill("data.jsonl")
        ui.locator("#go").click()
        ui.locator("#out table").wait_for(timeout=15000)
        self.assertIn("3 record(s)", ui.locator("#out").inner_text())
        call = player.calls[-1]
        self.assertEqual(call["table"], codeapp.data_source_name(summary["chat"]["name"]))
        self.assertEqual(call["parameters"]["input"], {"message": 'Use the JsonDoctor tool with action="inspect" '
                                                                  'path="data.jsonl". Reply with the tool\'s output only.',
                                                       "conversation_id": ""})
        self.assertClean(log)

    @unittest.skipUnless(HAVE_STORE, "set BFS_RAPP_STORE to a RAPP_Store checkout")
    def test_agent_team_gets_its_cartridge_and_its_invoke_answered(self):
        envelope = {"outcome_frame": {"success_metric": "A signed pilot", "definition_of_done": ["demo"], "kpis": ["1"]},
                    "persona_route": [{"order": 1, "persona": "Architect", "why": "shapes it"}],
                    "issue_body": "## Pilot", "needs_you_questions": ["Who signs?"]}
        fenced = "```json\n" + json.dumps(envelope, indent=2) + "\n```"          # models often fence their JSON
        _, player, page, ui, log = self.open("@kody-w/agent_team", copilot=lambda m: fenced, store=STORE)
        ui.locator("#ctxBanner", has_text="@ada@example.com").wait_for(timeout=15000)
        ui.locator("#goal").fill("Land a pilot")
        ui.locator("#runBtn").click()
        ui.locator("#output .persona").wait_for(timeout=15000)
        self.assertIn("Architect", ui.locator("#output").inner_text())
        self.assertIn('goal="Land a pilot"', player.calls[-1]["parameters"]["input"]["message"])
        self.assertClean(log)

    @unittest.skipUnless(HAVE_STORE, "set BFS_RAPP_STORE to a RAPP_Store checkout")
    def test_thoughtbox_invokes_round_trip(self):
        entries = []

        def agent(message):
            action = re.search(r'action="(\w+)"', message).group(1)
            if action == "append":
                entries.append({"id": f"e{len(entries) + 1}", "ts": "2026-09-25T10:00:00Z",
                                "text": re.search(r'text="((?:[^"\\]|\\.)*)"', message).group(1), "tags": []})
                return json.dumps({"ok": True, "entry": entries[-1]})
            if action == "stats":
                return json.dumps({"ok": True, "total": len(entries), "tag_counts": {}})
            return json.dumps({"ok": True, "entries": entries, "total": len(entries)})

        _, player, page, ui, log = self.open("@kody-w/thoughtbox", copilot=agent, store=STORE)
        ui.locator("#input").fill("remember the milk")
        ui.locator("#send").click()
        ui.locator("#entries .entry .text", has_text="remember the milk").wait_for(timeout=15000)
        self.assertClean(log)

    @unittest.skipUnless(HAVE_STORE, "set BFS_RAPP_STORE to a RAPP_Store checkout")
    def test_project_tracker_markup_built_at_run_time_keeps_its_handlers(self):
        _, player, page, ui, log = self.open("@kody-w/project_tracker", store=STORE)
        ui.locator("button.nav-tab", has_text="Projects").click()
        ui.locator("button", has_text="+ New Project").click()
        ui.locator("#customer-name").fill("Contoso")
        ui.locator("#mvp-use-case").fill("Contract bot")
        ui.locator("#project-form button[type=submit]").click()
        item = ui.locator(".project-item", has_text="Contoso")
        item.wait_for(timeout=15000)
        item.click()                                    # onclick="showProjectDetails('<id>')", built by the page
        ui.locator("#project-details", has_text="Contoso").wait_for(state="visible", timeout=15000)
        self.assertClean(log)

    @unittest.skipUnless(HAVE_STORE, "set BFS_RAPP_STORE to a RAPP_Store checkout")
    def test_vibe_coding_loop_runs_unchanged(self):
        _, player, page, ui, log = self.open("@kody-w/vibe-coding-loop", store=STORE)
        ui.locator('button.action-btn[data-action="ideate"]').click()
        ui.locator("#ideate-domain").fill("weather sims")
        ui.locator('button.run-btn[data-run="ideate"]').click()
        ui.locator("pre", has_text='"action": "ideate"').first.wait_for(timeout=15000)
        self.assertIn("weather sims", ui.locator("pre", has_text='"action": "ideate"').first.inner_text())
        self.assertClean(log)


if __name__ == "__main__":
    unittest.main()
