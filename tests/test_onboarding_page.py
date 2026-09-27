"""Onboarding page contracts and browser behavior, without live network access.

Browser tests skip when Playwright is absent, unless BFS_REQUIRE_BROWSER=1.
Run with: python3 -m unittest tests.test_onboarding_page -v
"""
import html.parser
import json
import os
import re
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
PAGE = (SITE_DIR / "index.html").read_text(encoding="utf-8")
CONTRACT = json.loads((SITE_DIR / "brainfreeze-studio.json").read_text(encoding="utf-8"))
SITE = CONTRACT["site"]
REPO = CONTRACT["repository"]
SKILL = (SITE_DIR / "skills" / "brainfreeze-studio" / "SKILL.md").read_text(encoding="utf-8")
CLAUDE = (SITE_DIR / "CLAUDE.md").read_text(encoding="utf-8")
LLMS = (SITE_DIR / "llms.txt").read_text(encoding="utf-8")
PROBE = "http://localhost:7071/health/public"
ORIGIN = "https://onboarding.test/"
SCOPE = "Agents with a proven translation become tools; the rest become skills, and the build says why."


class Markup(html.parser.HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.nodes, self.stack = [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                       "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data

    def by_id(self, ident):
        return next(n for n in self.nodes if n["attrs"].get("id") == ident)

    def text(self, ident):
        return " ".join(self.by_id(ident)["text"].split())


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "{} isn't a PNG".format(path.name)
    return struct.unpack(">II", data[16:24])


def require_browser():
    if sync_playwright is None:
        if os.environ.get("BFS_REQUIRE_BROWSER") == "1":
            raise AssertionError("BFS_REQUIRE_BROWSER=1 requires Playwright and Chromium; browser tests cannot skip")
        raise unittest.SkipTest("needs Playwright for Python with its Chromium")


class Page(unittest.TestCase):
    def test_every_line_to_paste_opens_this_site_and_asks_for_a_draft(self):
        script = PAGE.split("<script>")[-1]
        self.assertIn('const SITE = "{}";'.format(SITE), script)
        tail = re.search(r'const TAIL = "([^"]+)";', script).group(1)
        asks = dict(re.findall(r'\{ id: "(\w+)", label: "[^"]+", ask: "([^"]+)" \}', script))
        self.assertEqual(set(asks), {"brainstem", "store", "sample"})
        self.assertIn("keep it a Draft", tail)
        self.assertIn("deploy only after my yes", tail)
        self.assertEqual(Markup(PAGE).text("prompt"), "Open {} and {}. {}".format(SITE, asks["sample"], tail))

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

    def test_tool_coverage_requires_a_shipped_spec_or_a_reviewed_source_profile(self):
        doc = Markup(PAGE)
        self.assertIn(SCOPE, doc.text("coverage-promise"))
        coverage = doc.text("coverage")
        for phrase in ("shipped", "translations/", "reviewed", "copilot-harness-sdk", "digest",
                       "not just its name", "not re-proven against your file"):
            self.assertIn(phrase, coverage)
        table = Markup(re.search(r'<table id="conversion-table">.*?</table>', PAGE, re.S).group())
        self.assertIn("Requires", [n["text"].strip() for n in table.nodes if n["tag"] == "th"])
        for example, requirement in (("InvoiceRouter", "shipped translation spec"),
                                     ("JsonDoctor", "shipped translation spec"),
                                     ("Thoughtbox", "shipped translation spec"),
                                     ("ForumAgent", "shipped translation spec"),
                                     ("ManageMemory", "reviewed source digest"),
                                     ("ContextMemory", "reviewed source digest"),
                                     ("HackerNews", "provisioned connector")):
            with self.subTest(example=example):
                row = next(n["text"] for n in table.nodes if n["tag"] == "tr" and example in n["text"])
                self.assertIn(requirement, row)
        self.assertIn("fallback is a reasoning-only skill", doc.text("conversion-table"))
        self.assertIn("Translations: Python or recorded proofs", doc.text("proof-chips"))
        self.assertIn("Profiles: reviewed SDK capabilities", doc.text("proof-chips"))
        self.assertNotIn("Flows proven against your Python", PAGE)

    def test_writing_new_translations_is_separate_work(self):
        authoring = Markup(PAGE).text("translation-authoring")
        for phrase in ("materialize", "C#", "separate work", "this setup does not"):
            self.assertIn(phrase, authoring)

    def test_python_proof_is_explicitly_not_a_sandbox(self):
        proof = Markup(PAGE).text("proof-safety")
        for phrase in ("on your computer", "separate process", "clean environment", "temporary home",
                       "working folder", "not a sandbox", "only code you trust"):
            self.assertIn(phrase, proof)

    def test_the_plan_digest_draft_connections_and_redeploy_contract_is_explicit(self):
        doc = Markup(PAGE)
        for ident, phrases in (
            ("plan-rules", ("read-only plan", "create or update", "removed", "already published",
                            "which connection each flow uses", "only after you say yes")),
            ("deploy-rules", ("exactly the build", "digest", "content", "flows", "links and bindings",
                              "stops on any difference")),
            ("draft-rules", ("Draft", "nothing is published", "already published",
                             "flow changes reach it at once", "plan says so first")),
            ("identity-rules", ("delegated sign-in", "app-only tokens", "your own connections",
                                "unless you explicitly allow")),
            ("redeploy-rules", ("removes components", "added by hand", "plan lists them", "--keep-extra")),
        ):
            for phrase in phrases:
                with self.subTest(section=ident, phrase=phrase):
                    self.assertIn(phrase, doc.text(ident))

    def test_a_browser_reply_is_conditional_in_the_page_and_the_pasted_line(self):
        doc = Markup(PAGE)
        tail = re.search(r'const TAIL = "([^"]+)";', PAGE).group(1)
        for phrase in ("If you can drive my signed-in browser", "otherwise", "maker link and question to ask"):
            self.assertIn(phrase, tail)
        for ident in ("browser-note", "test-step", "ai-instructions"):
            with self.subTest(section=ident):
                text = doc.text(ident)
                self.assertIn("signed-in browser", text)
                self.assertRegex(text, r"[Oo]therwise")
                self.assertIn("maker link", text)
                self.assertIn("question", text)
        self.assertIn("not a verified reply", doc.text("browser-note"))
        self.assertNotIn("show me it answering in Copilot Studio", PAGE)

    def test_probe_discloses_the_exact_read_and_never_promises_absence(self):
        doc = Markup(PAGE)
        self.assertEqual(doc.text("probe"), "Check this computer for a Brainstem")
        disclosure = doc.text("probe-disclosure")
        for phrase in ("one GET", PROBE, "whether a Brainstem answers", "its version",
                       "nothing is sent", "Setup works without granting"):
            self.assertIn(phrase, disclosure)
        self.assertNotRegex(PAGE, r"No Brainstem (?:on|running on) this computer")

    def test_the_social_preview_and_icons_exist_at_their_sizes(self):
        self.assertEqual(png_size(SITE_DIR / "og.png"), (1200, 630))
        self.assertEqual(png_size(SITE_DIR / "apple-touch-icon.png"), (180, 180))
        nodes = Markup(PAGE).nodes
        meta = {n["attrs"].get("property") or n["attrs"].get("name"): n["attrs"].get("content")
                for n in nodes if n["tag"] == "meta"}
        self.assertEqual(meta["og:url"], SITE)
        self.assertEqual(meta["og:image"], SITE + "og.png")
        self.assertEqual((int(meta["og:image:width"]), int(meta["og:image:height"])), png_size(SITE_DIR / "og.png"))
        self.assertEqual(meta["twitter:card"], "summary_large_image")
        self.assertEqual(meta["color-scheme"], "light dark")
        self.assertEqual(meta["og:description"], meta["description"])
        for phrase in ("shipped translations", "reviewed", "reasoning-only skills"):
            self.assertIn(phrase, meta["og:description"])
        themes = {n["attrs"].get("media"): n["attrs"].get("content") for n in nodes
                  if n["attrs"].get("name") == "theme-color"}
        self.assertEqual(themes, {"(prefers-color-scheme: light)": "#f2f6f9",
                                  "(prefers-color-scheme: dark)": "#0d1419"})
        self.assertTrue(any(n["attrs"].get("rel") == "canonical" and n["attrs"].get("href") == SITE for n in nodes))
        self.assertTrue(any(n["attrs"].get("rel") == "icon" and
                            n["attrs"].get("href", "").startswith("data:image/svg+xml,") for n in nodes))

    def test_no_external_fonts_scripts_or_analytics_are_required(self):
        for node in Markup(PAGE).nodes:
            if node["tag"] == "script":
                self.assertNotIn("src", node["attrs"])
            if node["tag"] == "link" and node["attrs"].get("rel") in ("stylesheet", "preconnect"):
                self.assertNotRegex(node["attrs"].get("href", ""), r"^(https?:)?//")
        self.assertNotIn("innerHTML", PAGE)
        self.assertLess(len(PAGE.encode("utf-8")), 30000, "keep this a lean, self-contained page")

    def test_a_missing_page_leads_back_home(self):
        missing = (SITE_DIR / "404.html").read_text(encoding="utf-8")
        self.assertIn('href="{}"'.format(SITE), missing)

    def test_the_pages_workflow_checks_then_publishes_this_folder(self):
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        self.assertIn("path: site", workflow)
        self.assertIn("tests/test_onboarding_site.py", workflow)
        self.assertIn("playwright install", workflow)
        self.assertLess(workflow.index("test_onboarding_site"), workflow.index("upload-pages-artifact"))

    def test_missing_playwright_skips_only_when_it_is_not_required(self):
        with patch(__name__ + ".sync_playwright", None):
            for value in ("", "0", "true"):
                with self.subTest(value=value), patch.dict(os.environ, {"BFS_REQUIRE_BROWSER": value}):
                    with self.assertRaises(unittest.SkipTest):
                        require_browser()
            with patch.dict(os.environ, {"BFS_REQUIRE_BROWSER": "1"}):
                with self.assertRaisesRegex(AssertionError, "BFS_REQUIRE_BROWSER=1"):
                    require_browser()


CONTRAST = r"""() => {
  const color = value => {
    const match = value.match(/^rgba?\(([\d.]+), ([\d.]+), ([\d.]+)(?:, ([\d.]+))?\)$/);
    if (!match) throw new Error('Cannot measure color: ' + value);
    return [+match[1], +match[2], +match[3], match[4] === undefined ? 1 : +match[4]];
  };
  const over = (fg, bg) => {
    const alpha = fg[3] + bg[3] * (1 - fg[3]);
    return [0, 1, 2].map(i => (fg[i] * fg[3] + bg[i] * bg[3] * (1 - fg[3])) / (alpha || 1)).concat(alpha);
  };
  const luminance = rgba => rgba.slice(0, 3).map(v => {
    v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
  }).reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
  const visible = el => {
    if (!el.getClientRects().length) return false;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === 'none' || s.visibility !== 'visible' || +s.opacity === 0) return false;
    }
    return true;
  };
  const checks = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el)) continue;
    const ancestors = [];
    for (let p = el; p; p = p.parentElement) ancestors.unshift(p);
    let background = [0, 0, 0, 0];
    for (const p of ancestors) {
      const s = getComputedStyle(p);
      if (s.backgroundImage !== 'none') throw new Error('Unmeasured background image');
      background = over(color(s.backgroundColor), background);
    }
    for (const pseudo of [null, '::before', '::after', '::marker']) {
      const s = getComputedStyle(el, pseudo);
      let text = pseudo ? s.content : [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => n.textContent).join('').trim();
      if (!pseudo && /^(TEXTAREA|INPUT)$/.test(el.tagName)) text = el.value;
      if (!text || (pseudo && ['none', 'normal', '""'].includes(text)) || s.display === 'none') continue;
      const bg = pseudo ? over(color(s.backgroundColor), background) : background;
      if (bg[3] !== 1) throw new Error('Text has no measurable opaque background: ' + text);
      const fg = over(color(s.color), bg);
      const values = [luminance(fg), luminance(bg)].sort((a, b) => a - b);
      const ratio = (values[1] + .05) / (values[0] + .05);
      const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight, 10) >= 700;
      checks.push({element: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (pseudo || ''),
        text: text.slice(0, 75), ratio, required: size >= 24 || (bold && size >= 18.6667) ? 3 : 4.5,
        foreground: s.color, background: bg, generated: !!pseudo});
    }
  }
  return checks;
}"""


class PageInABrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_browser()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def open(self, permission="throws", probe="found", version="1.2.3", saved=None, clipboard="success",
             exec_copy=False, **options):
        context = self.browser.new_context(**options)
        self.addCleanup(context.close)
        page = context.new_page()
        page.errors, page.probes, page.pending, page.external = [], [], [], []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.add_init_script("""
          window.__permission = %s; window.__permissionQueries = [];
          const query = async ({name}) => {
            window.__permissionQueries.push(name);
            const state = typeof window.__permission === 'object' ? window.__permission[name] : window.__permission;
            if (!['prompt', 'granted', 'denied'].includes(state)) throw new TypeError('Unknown permission');
            return {state};
          };
          Object.defineProperty(navigator, 'permissions', {value: window.__permission === 'missing' ? undefined : {query}});
          window.__writes = []; window.__execCalls = 0;
          Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {
            window.__writes.push(text);
            if (%s !== 'success') throw new Error('Clipboard refused');
          }}});
          document.execCommand = () => { window.__execCalls++; return %s; };
          const saved = %s;
          if (saved) for (const [key, value] of Object.entries(saved)) localStorage.setItem(key, value);
        """ % (json.dumps(permission), json.dumps(clipboard), json.dumps(exec_copy), json.dumps(saved)))

        def site_route(route):
            if route.request.url == ORIGIN:
                route.fulfill(status=200, content_type="text/html", body=PAGE)
            else:
                page.external.append(route.request.url)
                route.abort()

        def local_route(route):
            page.probes.append({"url": route.request.url, "method": route.request.method,
                                "body": route.request.post_data, "headers": route.request.headers})
            if probe == "pending":
                page.pending.append(route)
            elif probe == "aborted":
                route.abort("aborted")
            elif probe == "invalid":
                route.fulfill(status=200, content_type="application/json", body='{"status":"different app"}')
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"status": "ok", "version": version}))

        page.route("**/*", site_route)
        page.route("http://localhost:7071/**", local_route)
        page.goto(ORIGIN, wait_until="domcontentloaded")
        if options.get("java_script_enabled", True):
            page.wait_for_function("!document.getElementById('status').hidden")
        return page

    def wait_text(self, page, ident, text):
        page.wait_for_function("([id, text]) => document.getElementById(id).textContent.includes(text)",
                               arg=[ident, text])

    def test_all_twelve_ai_source_combinations_keep_one_explicit_choice(self):
        page = self.open()
        self.assertIn("Invoice Router sample", page.locator("#prompt").inner_text())
        for ai in ("GitHub Copilot", "Claude Code", "Another AI", "By hand"):
            page.get_by_role("button", name=ai, exact=True).click()
            for source, ask in (("My Brainstem", "put my Brainstem in Copilot Studio"),
                                ("A RAPP Store app", "ask me which one"),
                                ("Just trying it", "Invoice Router sample")):
                with self.subTest(ai=ai, source=source):
                    self.assertTrue(page.locator("#source-picker").is_visible())
                    page.get_by_role("button", name=source, exact=True).click()
                    text = page.locator("#prompt").inner_text()
                    if ai == "By hand":
                        self.assertEqual(text, REPO + "#use-it")
                    else:
                        self.assertIn(ask, text)
                        self.assertIn("keep it a Draft", text)
                        self.assertIn("otherwise give me the maker link and question to ask", text)
                    for group in ("ai", "source"):
                        self.assertEqual(page.locator('#{}-picker [aria-pressed="true"]'.format(group)).count(), 1)
        page.get_by_role("button", name="Claude Code", exact=True).click()
        self.assertEqual(page.locator("#paste-label").inner_text(), "Paste this into Claude Code")
        self.assertEqual(page.errors, [])

    def test_without_javascript_static_defaults_are_selectable_and_no_dead_controls_appear(self):
        page = self.open(java_script_enabled=False)
        prompt = page.locator("#prompt")
        self.assertTrue(prompt.is_visible())
        self.assertIn("Invoice Router sample", prompt.inner_text())
        self.assertIn("keep it a Draft", prompt.inner_text())
        for selector in ("#copy", "#customize", "#status", "#manual-copy"):
            self.assertTrue(page.locator(selector).is_hidden(), selector)
        self.assertNotIn("Checking for", page.inner_text("body"))
        self.assertIn("setup skill", page.locator("noscript").inner_text())
        self.assertEqual(prompt.locator("xpath=ancestor::button").count(), 0)
        self.assertNotEqual(prompt.evaluate("e => getComputedStyle(e).userSelect"), "none")
        self.assertEqual(page.probes, [])
        self.assertEqual(page.external, [])

    def test_only_a_granted_permission_can_trigger_an_automatic_local_request(self):
        for permission in ("prompt", "throws", "missing"):
            with self.subTest(permission=permission):
                page = self.open(permission=permission)
                page.wait_for_timeout(100)
                self.assertEqual(page.probes, [])
                self.assertTrue(page.locator("#probe").is_visible())
                page.locator("#probe").click()
                self.wait_text(page, "status-label", "v1.2.3")
                self.assertEqual(len(page.probes), 1)
                self.assertEqual(page.errors, [])
        page = self.open(permission="granted")
        self.wait_text(page, "status-label", "v1.2.3")
        self.assertEqual(len(page.probes), 1)
        self.assertEqual(page.probes[0]["url"], PROBE)
        self.assertEqual(page.probes[0]["method"], "GET")
        self.assertIsNone(page.probes[0]["body"])
        for header in ("authorization", "cookie", "referer"):
            self.assertNotIn(header, page.probes[0]["headers"])
        self.assertEqual(page.locator('#source-picker [aria-pressed="true"]').inner_text(), "My Brainstem")

    def test_permission_aliases_are_queried_without_treating_unknown_names_as_granted(self):
        for name in ("local-network-access", "loopback-network", "local-network"):
            with self.subTest(name=name):
                page = self.open(permission={name: "granted"})
                self.wait_text(page, "status-label", "v1.2.3")
                self.assertEqual(len(page.probes), 1)
        page = self.open(permission={"local-network-access": "unsupported"})
        page.locator("#probe").wait_for(state="visible")
        self.assertEqual(page.probes, [])

    def test_denied_permission_is_not_a_missing_brainstem_and_setup_still_works(self):
        page = self.open(permission="denied")
        self.wait_text(page, "status-label", "Your browser blocked the check")
        self.assertIn("setup still works", page.locator("#status-label").inner_text())
        self.assertIn("tell your AI to use your Brainstem", page.locator("#status-label").inner_text())
        self.assertEqual(page.probes, [])
        page.get_by_role("button", name="My Brainstem", exact=True).click()
        self.assertIn("put my Brainstem", page.locator("#prompt").inner_text())
        self.assertTrue(page.locator("#copy").is_enabled())

    def test_a_ten_second_browser_prompt_does_not_abort_a_clicked_check(self):
        page = self.open(permission="prompt", probe="pending")
        page.locator("#probe").click()
        self.wait_text(page, "status-label", "Waiting for your browser")
        page.wait_for_timeout(10000)
        self.assertEqual(len(page.pending), 1)
        waiting = page.locator("#status-label").inner_text()
        page.pending[0].fulfill(status=200, content_type="application/json",
                                body='{"status":"ok","version":"slow"}')
        self.assertIn("Waiting for your browser", waiting)
        self.wait_text(page, "status-label", "vslow")
        self.assertEqual(page.errors, [])

    def test_aborted_and_non_brainstem_responses_never_claim_the_computer_has_no_brainstem(self):
        for result in ("aborted", "invalid"):
            with self.subTest(result=result):
                page = self.open(permission="prompt", probe=result)
                page.locator("#probe").click()
                self.wait_text(page, "status-label", "Nothing answered at localhost:7071")
                self.assertIn("it may not be running, or your browser blocked the check",
                              page.locator("#status-label").inner_text())
                self.assertTrue(page.locator("#probe").is_visible())
                self.assertEqual(page.locator('#source-picker [aria-pressed="true"]').inner_text(), "Just trying it")

    def test_permission_denied_during_the_prompt_has_its_own_result(self):
        page = self.open(permission="prompt", probe="pending")
        page.locator("#probe").click()
        page.evaluate("window.__permission = 'denied'")
        page.pending[0].abort("accessdenied")
        self.wait_text(page, "status-label", "Your browser blocked the check")
        self.assertNotIn("Nothing answered", page.locator("#status-label").inner_text())

    def test_saved_or_in_flight_explicit_source_choices_win_over_detection(self):
        for saved in ("sample", "store"):
            with self.subTest(saved=saved):
                page = self.open(permission="granted", saved={"brainfreeze-source": saved})
                self.wait_text(page, "status-label", "v1.2.3")
                self.assertEqual(page.locator('#source-picker [aria-pressed="true"]').get_attribute("data-id"), saved)
        page = self.open(permission="granted", probe="pending")
        page.get_by_role("button", name="A RAPP Store app", exact=True).click()
        page.pending[0].fulfill(status=200, content_type="application/json", body='{"status":"ok","version":"late"}')
        self.wait_text(page, "status-label", "vlate")
        self.assertEqual(page.locator('#source-picker [aria-pressed="true"]').get_attribute("data-id"), "store")

    def test_a_version_is_rendered_as_text_never_markup(self):
        version = '<img src=x onerror="window.injected=true">'
        page = self.open(permission="granted", version=version)
        self.wait_text(page, "status-label", version)
        self.assertEqual(page.locator("#status img").count(), 0)
        self.assertIsNone(page.evaluate("window.injected"))

    def test_copy_reports_success_only_when_a_clipboard_method_succeeds(self):
        for clipboard, legacy in (("success", False), ("reject", True)):
            with self.subTest(clipboard=clipboard, legacy=legacy):
                page = self.open(clipboard=clipboard, exec_copy=legacy)
                page.locator("#copy").click()
                self.wait_text(page, "copied", "Copied.")
                self.assertEqual(page.evaluate("window.__writes"), [page.locator("#prompt").inner_text()])
                self.assertEqual(page.evaluate("window.__execCalls"), 0 if clipboard == "success" else 1)
                self.assertTrue(page.locator("#success-icon").is_visible())
                self.assertTrue(page.locator("#manual-copy").is_hidden())

    def test_copy_failure_leaves_an_accessible_focused_selected_text_field(self):
        page = self.open(clipboard="reject", exec_copy=False)
        page.locator("#copy").click()
        page.wait_for_function("window.__execCalls === 1")
        self.assertIn("Couldn't copy automatically", page.locator("#copied").inner_text())
        self.assertIn("press ⌘C or Ctrl+C", page.locator("#copied").inner_text())
        self.assertTrue(page.locator("#copied").is_visible())
        self.assertTrue(page.locator("#icon").is_visible())
        self.assertTrue(page.locator("#success-icon").is_hidden())
        field = page.get_by_role("textbox", name="Setup line for manual copy")
        self.assertTrue(field.is_visible())
        self.assertEqual(field.input_value(), page.locator("#prompt").inner_text())
        self.assertEqual(field.evaluate("(e) => [e === document.activeElement, e.selectionStart, e.selectionEnd]"),
                         [True, 0, len(field.input_value())])
        self.assertEqual(page.evaluate("window.__execCalls"), 1)

    def test_two_successful_copy_clicks_restore_the_original_icon(self):
        page = self.open()
        icon = page.locator("#icon").inner_html()
        page.locator("#copy").click()
        page.wait_for_timeout(80)
        page.locator("#copy").click()
        page.wait_for_timeout(1800)
        self.assertEqual(len(page.evaluate("window.__writes")), 2)
        self.assertTrue(page.locator("#icon").is_visible())
        self.assertTrue(page.locator("#success-icon").is_hidden())
        self.assertEqual(page.locator("#icon").inner_html(), icon)
        self.assertEqual(page.locator("#copied").inner_text(), "")

    def test_a_second_copy_restarts_the_feedback_timer(self):
        page = self.open()
        page.locator("#copy").click()
        page.wait_for_timeout(1000)
        page.locator("#copy").click()
        page.wait_for_timeout(600)
        self.assertEqual(page.locator("#copied").inner_text(), "Copied.")
        self.assertTrue(page.locator("#success-icon").is_visible())
        page.wait_for_timeout(1000)
        self.assertEqual(page.locator("#copied").inner_text(), "")
        self.assertTrue(page.locator("#icon").is_visible())

    def test_primary_copy_action_and_explicit_source_choice_fit_the_phone_viewport(self):
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                page = self.open(viewport={"width": 390, "height": 844}, color_scheme=scheme)
                box = page.locator("#copy").bounding_box()
                self.assertGreaterEqual(box["y"], 0)
                self.assertLessEqual(box["y"] + box["height"], 844, box)
                self.assertTrue(page.locator("#source-picker").is_visible())
                source = page.locator("#source-picker").bounding_box()
                self.assertLessEqual(source["y"] + source["height"], 844, source)
                self.assertTrue(page.get_by_role("button", name="Just trying it", exact=True).is_visible())
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_all_visible_text_including_generated_step_numbers_has_wcag_contrast(self):
        for scheme in ("light", "dark"):
            for panels in (False, True):
                with self.subTest(scheme=scheme, panels=panels):
                    page = self.open(viewport={"width": 390, "height": 844}, color_scheme=scheme)
                    page.evaluate("(open) => document.querySelectorAll('details').forEach(d => d.open = open)", panels)
                    checks = page.evaluate(CONTRAST)
                    self.assertGreater(len(checks), 35)
                    generated = [c for c in checks if c["generated"]]
                    self.assertEqual(len([c for c in generated if re.fullmatch(r"li(?:#[\w-]+)?::before",
                                                                              c["element"])]), 3, generated)
                    self.assertEqual([c for c in checks if c["ratio"] + .001 < c["required"]], [])

    def test_keyboard_starts_with_copy_and_native_details_can_be_toggled(self):
        page = self.open()
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate("document.activeElement.id"), "copy")
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate("document.activeElement.textContent"), "GitHub Copilot")
        summary = page.locator("summary").first
        summary.focus()
        page.keyboard.press("Enter")
        self.assertTrue(summary.locator("..").evaluate("e => e.open"))
        page.keyboard.press("Enter")
        self.assertFalse(summary.locator("..").evaluate("e => e.open"))

    def test_page_boot_makes_no_external_requests_or_unapproved_local_requests(self):
        page = self.open(permission="throws")
        page.locator("#probe").wait_for(state="visible")
        self.assertEqual(page.external, [])
        self.assertEqual(page.probes, [])
        self.assertEqual(page.errors, [])


if __name__ == "__main__":
    unittest.main()
