"""The onboarding site (site/, published to GitHub Pages) tells people and their AI agents exactly what to run.
These tests keep it true:
- every command it gives parses with the real command line, and the skill and contract give the same ones;
- every deploy is planned first and is a Draft;
- the sample's answer and parity line are what the agent and the build really produce;
- every link resolves;
- the page works with and without JavaScript.

The browser checks need Playwright for Python with its Chromium (skipped otherwise):
    python3 -m pip install playwright && python3 -m playwright install chromium
"""
import html.parser
import importlib.util
import io
import json
import os
import re
import shlex
import struct
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
RUN = CONTRACT["run"] + " "
FREEZE = "../.venv/bin/python -m brainfreeze "
PLACEHOLDERS = {"<environment>": "https://org.crm.dynamics.com/", "<@publisher/id>": "@rapp/json_doctor",
                "<github-login>": "you", "<short-name>": "desk", "<id>": "json_doctor", "<Agent name>": "Desk",
                "<digest>": "0123456789ab"}


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
        elif isinstance(o, str) and o.startswith((RUN, FREEZE)):
            found.append(o)
    walk(CONTRACT["sources"])
    walk(CONTRACT["sign_in"])
    return found


def skill_blocks():
    return [block for block in re.findall(r"```bash\n(.*?)```", SKILL, re.S)]


def skill_commands():
    return [line.strip() for block in skill_blocks() for line in block.splitlines()
            if line.strip().startswith((RUN, FREEZE))]


def cli_argv(command):
    """The arguments brainfreeze studio's own parser gets from a command the site gives."""
    words = shlex.split(fill(command))
    if command.startswith(RUN):
        return words[len(shlex.split(RUN)):]
    raise AssertionError(f"not a brainfreeze studio command: {command!r}")


def parse(command):
    err = io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            return cli.parser().parse_args(cli_argv(command))
    except SystemExit:
        raise AssertionError(f"the command line refuses {command!r}: {err.getvalue().strip()}")


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "use":
            return
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.links.append(value)
            if name == "content" and value and value.startswith(SITE):
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
    def test_every_brainfreeze_studio_command_given_to_an_ai_is_one_the_command_line_takes(self):
        commands = [c for c in contract_commands() + skill_commands() if c.startswith(RUN)]
        self.assertGreaterEqual(len(commands), 12)
        for command in commands:
            with self.subTest(command=command):
                parse(command)

    def test_the_skill_and_the_contract_give_exactly_the_same_commands(self):
        self.assertEqual(sorted(set(skill_commands())), sorted(set(contract_commands())))

    def test_every_deploy_is_planned_first_and_is_a_draft(self):
        deploys = [c for c in contract_commands() + skill_commands()
                   if c.startswith(RUN) and (vars(parse(c)).get("cmd") == "deploy" or vars(parse(c)).get("deploy"))]
        self.assertGreaterEqual(len(deploys), 6)
        for command in deploys:
            with self.subTest(command=command):
                self.assertIs(parse(command).draft, True)
        for source in ("brainstem", "rapp_store_app", "sample"):
            with self.subTest(source=source):
                steps = CONTRACT["sources"][source]
                self.assertEqual(steps["deploy"], steps["plan"].replace(" --plan", " --expect <digest>"))
                self.assertIs(parse(steps["plan"]).plan, True)
                self.assertIs(parse(steps["deploy"]).plan, False)
                self.assertEqual(parse(steps["deploy"]).expect, PLACEHOLDERS["<digest>"])

    def test_the_plan_and_the_deploy_use_the_build_it_made_and_never_rebuild(self):
        for source in ("brainstem", "rapp_store_app", "sample"):
            with self.subTest(source=source):
                steps = CONTRACT["sources"][source]
                built = parse(steps["build"]).out
                for step in ("plan", "deploy"):
                    args = parse(steps[step])
                    self.assertEqual(args.cmd, "deploy", f"{step} must deploy the build, not build again")
                    self.assertEqual(args.workspace, built + "/workspace")

    def test_the_skill_shows_each_plan_before_its_deploy(self):
        for source in ("brainstem", "rapp_store_app", "sample"):
            with self.subTest(source=source):
                steps = CONTRACT["sources"][source]
                self.assertLess(SKILL.index(steps["plan"]), SKILL.index(steps["deploy"] + "\n"))

    def test_every_file_for_an_ai_deploys_exactly_the_planned_build_and_says_what_runs_where(self):
        for name, text in (("skill", SKILL), ("CLAUDE.md", CLAUDE), ("llms.txt", LLMS),
                           ("contract", json.dumps(CONTRACT))):
            with self.subTest(file=name):
                self.assertIn("--expect <digest>", text)
                self.assertIn("sandbox", text, "it must say the proof's Python isn't sandboxed")
                self.assertRegex(text, r"(haven't|has not) seen the reply", "it must never pass a link off as a reply")

    def test_nothing_it_builds_needs_node_unless_the_person_asks_for_the_code_app(self):
        for source in ("rapp_store_app", "sample"):
            with self.subTest(source=source):
                self.assertIs(parse(CONTRACT["sources"][source]["build"]).no_app, True)

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

    def test_the_skills_setup_blocks_are_the_contracts(self):
        blocks = "\n".join(skill_blocks())
        for line in CONTRACT["setup"] + CONTRACT["sources"]["brainstem"]["tools"] + [CONTRACT["sign_in"]["person_runs"]]:
            with self.subTest(line=line):
                self.assertIn(line, blocks)
        self.assertIn(f"`{CONTRACT['update']}`", SKILL)
        self.assertIn(CONTRACT["cwd"], SKILL)

    def test_windows_gets_its_own_spelling_of_everything(self):
        win = CONTRACT["windows"]
        for key in ("python3", "../.venv/bin/python"):
            with self.subTest(key=key):
                self.assertIn(f"`{win[key]}`", SKILL)
        self.assertIn(win["work_folder"], SKILL)
        self.assertIn("'@rapp/markdown_medic'", SKILL)
        for how in CONTRACT["open_link"].values():
            with self.subTest(open=how):
                self.assertIn(f"`{how}`", SKILL)


class Sample(unittest.TestCase):
    def test_the_answer_it_promises_is_the_agents_own(self):
        sample = CONTRACT["sources"]["sample"]
        self.assertEqual(sample["question"], "Route an invoice from Fabrikam for $18,750.")
        agent = EXAMPLE / "singleton" / "invoice_router_agent.py"
        (answer,) = run_python_agent(agent, [{"args": {"vendor": "Fabrikam", "amount": 18750}, "env": {}}])
        self.assertEqual(answer, sample["answer"])
        self.assertIn(f"`{sample['question']}`", SKILL)
        self.assertIn(f"`{sample['answer']}`", SKILL)

    def test_the_parity_line_it_quotes_is_what_the_sample_build_prints_without_node(self):
        argv = cli_argv(CONTRACT["sources"]["sample"]["build"])
        with tempfile.TemporaryDirectory() as d:
            argv[argv.index("--out") + 1] = d
            argv[argv.index("examples/rapplications/invoice_router")] = str(EXAMPLE)
            argv[argv.index("translations/")] = str(TRANSLATIONS)
            out = io.StringIO()
            from brainfreeze_studio import codeapp
            real = codeapp.host_bundle
            codeapp.host_bundle = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("needed the npm-built host"))
            try:
                with redirect_stdout(out):
                    code = cli.main(argv)
            finally:
                codeapp.host_bundle = real
            self.assertFalse(list(Path(d).glob("powerapps-flows/*.json")), "--no-app still wrote Power Apps flows")
        self.assertEqual(code, 0)
        printed = [line for line in out.getvalue().splitlines() if line.startswith("parity:")]
        self.assertEqual(printed, [CONTRACT["sources"]["sample"]["parity"]])
        self.assertIn(f"`{printed[0]}`", SKILL)
        self.assertIn(printed[0].split(None, 1)[1].replace("  ", " "), PAGE)

    def test_the_store_id_it_shows_is_one_the_catalog_resolves(self):
        index = json.loads((ROOT / "tests" / "fixtures" / "rapp-store-index.json").read_text(encoding="utf-8")) \
            if (ROOT / "tests" / "fixtures" / "rapp-store-index.json").is_file() else None
        example = "@rapp/markdown_medic"
        self.assertIn(f"`{example}`", SKILL)
        if index is None:
            self.skipTest("no RAPP Store index fixture")
        apps = index["rapplications"]
        self.assertIn(example, {f"{a['publisher']}/{a['id']}" for a in apps})


    def test_the_store_entries_it_says_to_skip_are_the_ones_the_command_line_refuses(self):
        from brainfreeze_studio import rapplication
        self.assertIn("`access` is `private`", SKILL)
        self.assertIn("no `singleton_url`", SKILL)
        entries = [{"id": "gated", "publisher": "@rapp", "access": "private", "singleton_url": "https://x/a.py"},
                   {"id": "whole_app", "publisher": "@rapp", "application_schema": "dock"}]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.json").write_text(json.dumps({"rapplications": entries}))
            for ref in ("@rapp/gated", "@rapp/whole_app"):
                with self.subTest(ref=ref):
                    with self.assertRaises(rapplication.RapplicationError):
                        rapplication.from_store(ref, store=d)


def external_links():
    """Every https link given to a person or an AI that isn't this site's own (those are checked as files)."""
    texts = [PAGE, SKILL, CLAUDE, LLMS, json.dumps(CONTRACT)]
    found = set()
    for text in texts:
        for url in re.findall(r"https://[^\s\"'<>()`\]]+", text):
            url = url.rstrip(".,;:\\")
            if "<" in url or url.startswith(SITE) or re.match(r"https://(fonts\.|org\.crm)", url):
                continue
            found.add(url)
    return sorted(found)


@unittest.skipUnless(os.environ.get("BFS_CHECK_LINKS") == "1", "set BFS_CHECK_LINKS=1 to fetch every external link")
class LiveLinks(unittest.TestCase):
    def test_every_external_link_answers(self):
        import urllib.request
        links = external_links()
        self.assertGreaterEqual(len(links), 6)
        for url in links:
            with self.subTest(url=url):
                req = urllib.request.Request(url, headers={"User-Agent": "brainfreeze-studio-site-check"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.assertEqual(r.status, 200, url)


class Links(unittest.TestCase):
    def test_every_link_on_the_page_resolves(self):
        parser = _Links()
        parser.feed(PAGE)
        self.assertGreaterEqual(len(parser.links), 12)
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

    def test_every_svg_reference_on_the_page_is_defined(self):
        for ref in re.findall(r'<use href="#([^"]+)"', PAGE):
            with self.subTest(ref=ref):
                self.assertIn(f'id="{ref}"', PAGE)

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
        self.assertEqual(discovery["schema"], CONTRACT["schema"])
        self.assertEqual(discovery["skill"], CONTRACT["skill"])
        for url in (CONTRACT["skill"], SITE + "brainfreeze-studio.json", SITE):
            self.assertIn(url, LLMS)


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} isn't a PNG"
    return struct.unpack(">II", data[16:24])


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

    def test_every_file_states_the_plan_and_draft_rules(self):
        front = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", SKILL)
        self.assertEqual(front.group(1), "brainfreeze-studio")
        self.assertTrue(front.group(2).strip())
        for name, text in (("page", PAGE), ("skill", SKILL), ("CLAUDE.md", CLAUDE), ("llms.txt", LLMS),
                           ("contract", json.dumps(CONTRACT))):
            with self.subTest(file=name):
                self.assertIn("--draft", text)
                self.assertIn("--plan", text)

    def test_it_never_says_every_agent_becomes_a_flow(self):
        for name, text in (("page", PAGE), ("skill", SKILL), ("llms.txt", LLMS)):
            with self.subTest(file=name):
                self.assertNotRegex(text, r"(?i)its logic becomes real power platform flows|every agent becomes a flow")
                self.assertIn("skill", text)

    def test_the_social_preview_and_icons_exist_at_their_sizes(self):
        self.assertEqual(png_size(SITE_DIR / "og.png"), (1200, 630))
        self.assertEqual(png_size(SITE_DIR / "apple-touch-icon.png"), (180, 180))
        for tag in ('property="og:image"', 'name="twitter:card"', 'rel="canonical"', 'name="color-scheme"'):
            self.assertIn(tag, PAGE)

    def test_a_missing_page_leads_back_home(self):
        missing = (SITE_DIR / "404.html").read_text(encoding="utf-8")
        self.assertIn(f'href="{SITE}"', missing)

    def test_the_pages_workflow_checks_then_publishes_this_folder(self):
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        self.assertIn("path: site", workflow)
        self.assertIn("tests/test_onboarding_site.py", workflow)
        self.assertIn("playwright install", workflow)
        self.assertLess(workflow.index("test_onboarding_site"), workflow.index("upload-pages-artifact"))


@unittest.skipUnless(sync_playwright, "needs Playwright for Python with its Chromium")
class PageInABrowser(unittest.TestCase):
    def open(self, pw, **context):
        browser = pw.chromium.launch()
        page = browser.new_context(**context).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", lambda route: route.abort() if not route.request.url.startswith("file:")
                   else route.continue_())
        page.goto((SITE_DIR / "index.html").as_uri())
        return browser, page, errors

    def test_the_pickers_change_the_line_to_paste_and_nothing_errors(self):
        with sync_playwright() as pw:
            browser, page, errors = self.open(pw)
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

    def test_without_javascript_it_still_gives_a_line_to_paste_and_the_way_in(self):
        with sync_playwright() as pw:
            browser, page, _ = self.open(pw, java_script_enabled=False)
            self.assertIn("Invoice Router sample", page.locator("#prompt").inner_text())
            self.assertTrue(page.locator("noscript").count())
            self.assertTrue(page.locator("#status").is_hidden(), "a no-JS visitor sees a check that never runs")
            self.assertIn("setup skill", page.inner_text("body"))
            browser.close()

    def test_it_asks_before_looking_for_a_brainstem_where_the_browser_would_prompt(self):
        with sync_playwright() as pw:
            browser, page, errors = self.open(pw)
            page.close()
            page = browser.new_context().new_page()
            page.add_init_script("""
              window.__probes = 0;
              const realFetch = window.fetch;
              window.fetch = (url, opts) => { if (String(url).includes('localhost:7071')) window.__probes++; return realFetch(url, opts); };
              navigator.permissions.query = async ({ name }) => {
                if (name === 'local-network-access') return { state: 'prompt' };
                throw new TypeError('unknown permission');
              };""")
            page.route("**/*", lambda route: route.abort() if not route.request.url.startswith("file:")
                       else route.continue_())
            page.goto((SITE_DIR / "index.html").as_uri())
            page.wait_for_timeout(300)
            self.assertEqual(page.evaluate("window.__probes"), 0, "looked for a Brainstem before being asked to")
            button = page.get_by_role("button", name="Check this computer")
            self.assertTrue(button.is_visible())
            button.click()
            page.wait_for_timeout(300)
            self.assertEqual(page.evaluate("window.__probes"), 1)
            browser.close()


if __name__ == "__main__":
    unittest.main()
