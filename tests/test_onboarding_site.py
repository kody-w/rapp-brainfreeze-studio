"""The onboarding site (site/, published to GitHub Pages) tells people and their AI agents exactly what to run.
These tests keep it true: every command it gives parses with the real command line, the sample's answer and parity
line are what the agent and the build really produce, every link resolves, and every file states the same rules.

The page's browser check needs Playwright for Python with its Chromium (skipped otherwise):
    python3 -m pip install playwright && python3 -m playwright install chromium
"""
import html.parser
import importlib.util
import io
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from brainfreeze_studio import __main__ as cli  # noqa: E402
from test_rapplication import EXAMPLE, TRANSLATIONS, run_python_agent  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

CONTRACT = json.loads((SITE_DIR / "brainfreeze-studio.json").read_text(encoding="utf-8"))
SITE = CONTRACT["site"]
REPO = CONTRACT["repository"]
PAGE = (SITE_DIR / "index.html").read_text(encoding="utf-8")
SKILL = (SITE_DIR / "skills" / "brainfreeze-studio" / "SKILL.md").read_text(encoding="utf-8")
CLAUDE = (SITE_DIR / "CLAUDE.md").read_text(encoding="utf-8")
LLMS = (SITE_DIR / "llms.txt").read_text(encoding="utf-8")
PLACEHOLDERS = {"<environment>": "https://org.crm.dynamics.com/", "<@publisher/id>": "@rapp/json_doctor",
                "<github-login>": "you", "<short-name>": "desk", "<id>": "json_doctor", "<Agent name>": "Desk"}


def fill(command):
    for placeholder, value in PLACEHOLDERS.items():
        command = command.replace(placeholder, value)
    left = re.findall(r"<[^<>]+>", command)
    if left:
        raise AssertionError(f"unknown placeholder {left} in {command!r}")
    return command


def contract_commands():
    found = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and (o.startswith("bfs ") or o.startswith("../.venv/bin/python -m brainfreeze ")):
            found.append(o)
    walk(CONTRACT["sources"])
    walk(CONTRACT["sign_in"])
    return found


def skill_commands():
    blocks = re.findall(r"```bash\n(.*?)```", SKILL, re.S)
    return [line.strip() for block in blocks for line in block.splitlines()
            if line.strip().startswith(("bfs ", "../.venv/bin/python -m brainfreeze "))]


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.links.append(value)


def site_file(url):
    """The site file a link points at, or None when it points elsewhere."""
    if url.startswith(SITE):
        url = url[len(SITE):]
    elif re.match(r"^[a-z]+:", url) or url.startswith("#"):
        return None
    path = url.split("#")[0].split("?")[0]
    return SITE_DIR / (path or "index.html")


class Commands(unittest.TestCase):
    def assert_parses(self, command):
        argv = shlex.split(fill(command))[1:]
        err = io.StringIO()
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                cli.parser().parse_args(argv)
        except SystemExit:
            self.fail(f"the command line refuses {command!r}: {err.getvalue().strip()}")

    def test_every_brainfreeze_studio_command_given_to_an_ai_is_one_the_command_line_takes(self):
        commands = [c for c in contract_commands() + skill_commands() if c.startswith("bfs ")]
        self.assertGreaterEqual(len(commands), 9)
        for command in commands:
            with self.subTest(command=command):
                self.assert_parses(command)

    def test_the_skill_and_the_contract_give_the_same_commands(self):
        in_contract = set(contract_commands())
        for command in skill_commands():
            with self.subTest(command=command):
                self.assertIn(command, in_contract | {"bfs environments"})

    def test_every_deploy_is_a_draft(self):
        deploys = [c for c in contract_commands() + skill_commands() if " deploy " in f" {c} " or "--deploy" in c]
        self.assertGreaterEqual(len(deploys), 3)
        for command in deploys:
            with self.subTest(command=command):
                self.assertIn("--draft", command.split())

    def test_the_freeze_command_is_one_brainfreeze_takes(self):
        if importlib.util.find_spec("brainfreeze") is None:
            self.skipTest("brainfreeze isn't installed (pip install git+https://github.com/kody-w/rapp-brainfreeze.git)")
        freeze = CONTRACT["sources"]["brainstem"]["freeze"]
        self.assertIn(freeze, skill_commands())
        with tempfile.TemporaryDirectory() as d:
            argv = shlex.split(fill(freeze))[3:]
            argv[1] = str(Path(d) / "no-such-brainstem")
            argv[argv.index("--out") + 1] = d
            p = subprocess.run([sys.executable, "-m", "brainfreeze", *argv], capture_output=True, text=True)
        self.assertNotRegex(p.stderr, r"unrecognized arguments|the following arguments are required|invalid choice")
        self.assertNotEqual(p.returncode, 0, "a brainstem folder that doesn't exist must be refused")


class Sample(unittest.TestCase):
    def test_the_answer_it_promises_is_the_agents_own(self):
        sample = CONTRACT["sources"]["sample"]
        self.assertEqual(sample["question"], "Route an invoice from Fabrikam for $18,750.")
        agent = EXAMPLE / "singleton" / "invoice_router_agent.py"
        (answer,) = run_python_agent(agent, [{"args": {"vendor": "Fabrikam", "amount": 18750}, "env": {}}])
        self.assertEqual(answer, sample["answer"])
        self.assertIn(f"`{sample['question']}`", SKILL)
        self.assertIn(f"`{sample['answer']}`", SKILL)

    def test_the_parity_line_it_quotes_is_what_the_build_prints(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["rapplication", str(EXAMPLE), "--translations", str(TRANSLATIONS), "--out", d])
        self.assertEqual(code, 0)
        printed = [line for line in out.getvalue().splitlines() if line.startswith("parity:")]
        self.assertEqual(printed, [CONTRACT["sources"]["sample"]["parity"]])
        self.assertIn(f"`{printed[0]}`", SKILL)
        self.assertIn(printed[0].split(None, 1)[1].replace("  ", " "), PAGE)


class Links(unittest.TestCase):
    def test_every_link_on_the_page_resolves(self):
        parser = _Links()
        parser.feed(PAGE)
        self.assertGreaterEqual(len(parser.links), 10)
        for url in parser.links:
            with self.subTest(url=url):
                target = site_file(url)
                if url.startswith("#"):
                    self.assertIn(f'id="{url[1:]}"', PAGE, f"nothing on the page has the id {url[1:]}")
                elif target is not None:
                    self.assertTrue(target.is_file(), f"{url} -> {target} is missing")
                elif url.startswith(REPO + "/blob/main/"):
                    self.assertTrue((ROOT / url[len(REPO + "/blob/main/"):]).is_file(), url)
                elif url.startswith(REPO + "#"):
                    anchor = url.split("#", 1)[1]
                    headings = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
                                for h in re.findall(r"^#+ (.+)$", (ROOT / "README.md").read_text(), re.M)}
                    self.assertTrue(anchor == "readme" or anchor in headings, f"README has no #{anchor}")
                else:
                    self.assertRegex(url, r"^(https://(kody-w\.github\.io|github\.com/kody-w|fonts\.(googleapis|gstatic)\.com)"
                                          r"(/|$)|data:)")

    def test_the_markdown_files_link_only_to_what_exists(self):
        for name, text in (("SKILL.md", SKILL), ("CLAUDE.md", CLAUDE)):
            for url in re.findall(r"\]\(([^)]+)\)", text):
                with self.subTest(file=name, url=url):
                    base = SITE_DIR / "skills" / "brainfreeze-studio" if name == "SKILL.md" else SITE_DIR
                    self.assertTrue((base / url).is_file(), url)

    def test_the_contract_and_discovery_name_the_real_files(self):
        for key in ("skill", "claude_code", "llms"):
            with self.subTest(key=key):
                self.assertTrue(CONTRACT[key].startswith(SITE))
                self.assertTrue(site_file(CONTRACT[key]).is_file(), CONTRACT[key])
        discovery = json.loads(re.search(r'<script type="application/json" id="brainfreeze-studio-discovery">(.*?)'
                                         r"</script>", PAGE, re.S).group(1))
        self.assertEqual(discovery["manifest"], SITE + "brainfreeze-studio.json")
        self.assertEqual(discovery["skill"], CONTRACT["skill"])
        for url in (CONTRACT["skill"], SITE + "brainfreeze-studio.json", SITE):
            self.assertIn(url, LLMS)


class Page(unittest.TestCase):
    def test_every_line_to_paste_opens_this_site_and_asks_for_a_draft(self):
        script = PAGE.split('<script>')[-1]
        self.assertIn(f'const SITE = "{SITE}";', script)
        tail = re.search(r'const TAIL = "([^"]+)";', script).group(1)
        asks = dict(re.findall(r'\{ id: "(\w+)", label: "[^"]+", ask: "([^"]+)" \}', script))
        self.assertEqual(set(asks), {"brainstem", "store", "sample"})
        self.assertIn("keep it a Draft", tail)
        static = re.search(r'<span id="prompt">(.*?)</span>', PAGE, re.S).group(1)
        self.assertEqual(static, f"Open {SITE} and {asks['sample']}. {tail}")

    def test_the_skill_names_itself_and_every_file_states_the_draft_rule(self):
        front = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", SKILL)
        self.assertEqual(front.group(1), "brainfreeze-studio")
        self.assertTrue(front.group(2).strip())
        for name, text in (("page", PAGE), ("skill", SKILL), ("CLAUDE.md", CLAUDE), ("llms.txt", LLMS),
                           ("contract", json.dumps(CONTRACT))):
            with self.subTest(file=name):
                self.assertIn("--draft", text)

    def test_the_pages_workflow_checks_then_publishes_this_folder(self):
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        self.assertIn("path: site", workflow)
        self.assertIn("tests/test_onboarding_site.py", workflow)
        self.assertLess(workflow.index("test_onboarding_site"), workflow.index("upload-pages-artifact"))


@unittest.skipUnless(sync_playwright, "needs Playwright for Python with its Chromium")
class PageInABrowser(unittest.TestCase):
    def test_the_pickers_change_the_line_to_paste_and_nothing_errors(self):
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/*", lambda route: route.abort() if not route.request.url.startswith("file:")
                       else route.continue_())
            page.goto((SITE_DIR / "index.html").as_uri())
            prompt = page.locator("#prompt")
            self.assertIn("Invoice Router sample", prompt.inner_text())
            page.get_by_role("button", name="My Brainstem").click()
            self.assertIn("put my Brainstem in Copilot Studio", prompt.inner_text())
            self.assertEqual(page.get_by_role("button", name="My Brainstem").get_attribute("aria-pressed"), "true")
            page.get_by_role("button", name="Claude Code").click()
            self.assertEqual(page.locator("#paste-label").inner_text(), "Paste this into Claude Code")
            page.get_by_role("button", name="By hand").click()
            self.assertEqual(prompt.inner_text(), REPO + "#use-it")
            self.assertTrue(page.locator("#source-picker").is_hidden())
            browser.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
