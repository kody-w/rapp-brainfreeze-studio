"""The onboarding site (site/, published to GitHub Pages) tells people and their AI agents exactly what to run.
These tests keep it true:
- every command it gives parses with the real command line, and the skill and contract give the same ones;
- every deploy is planned first and is a Draft;
- the sample's answer and parity line are what the agent and the build really produce;
- every link resolves.

Page rendering and browser checks live in tests.test_onboarding_page.
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
                self.assertEqual(steps["plan"], steps["deploy"] + " --plan")
                self.assertIs(parse(steps["plan"]).plan, True)
                self.assertIs(parse(steps["deploy"]).plan, False)

    def test_the_skill_shows_each_plan_before_its_deploy(self):
        for source in ("brainstem", "rapp_store_app", "sample"):
            with self.subTest(source=source):
                steps = CONTRACT["sources"][source]
                self.assertLess(SKILL.index(steps["plan"]), SKILL.index(steps["deploy"] + "\n"))

    def test_nothing_it_builds_needs_node_unless_the_person_asks_for_the_code_app(self):
        for source in ("rapp_store_app", "sample"):
            for step in ("build", "plan", "deploy"):
                with self.subTest(source=source, step=step):
                    self.assertIs(parse(CONTRACT["sources"][source][step]).no_app, True)

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
        self.assertEqual(discovery["skill"], CONTRACT["skill"])
        for url in (CONTRACT["skill"], SITE + "brainfreeze-studio.json", SITE):
            self.assertIn(url, LLMS)


if __name__ == "__main__":
    unittest.main()
