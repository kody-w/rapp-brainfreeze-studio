"""Managed app tests: the spec check for every kind, the generated project (Microsoft's template shape, pinned
versions), each app's least-privilege connector calls, the App Player's media rules, binding (ms.config.json and
generated/ after `ms app add data-source`) and the task tracker's Dataverse table. Offline. With MANAGED_APP_BUILD=1
it also compiles every kind against a real registered app (MANAGED_APP_FIXTURE: its generated/, ms.config.json and
node_modules) using specs that match what that app bound (MANAGED_APP_FIXTURE_SPECS: {kind: spec})."""
import copy
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brainfreeze_studio import managed_app as ma  # noqa: E402

SPEC = {"kind": "sharepoint-media", "name": "Film Library", "site": "https://contoso.sharepoint.com/sites/films",
        "folder": "/Shared Documents/Films"}
SPECS = {
    "sharepoint-media": SPEC,
    "people-directory": {"kind": "people-directory", "name": "People"},
    "calendar-dashboard": {"kind": "calendar-dashboard", "name": "My Week", "days": 7},
    "sharepoint-list": {"kind": "sharepoint-list", "name": "Rules", "site": "https://contoso.sharepoint.com/sites/ops",
                        "list": "Contoso Rules", "groupBy": "Category"},
    "task-tracker": {"kind": "task-tracker", "name": "Tasks", "table": "cr123_task"},
}
# What each kind may do on its connector, and nothing else.
LEAST = {
    "sharepoint-media": ("SharePointService", "sharepointonline", "action",
                         ["GetFileContentByPath", "GetFolderMetadataByPath", "ListFolder"]),
    "people-directory": ("UsersService", "office365users", "action",
                         ["DirectReports_V2", "Manager_V2", "MyProfile_V2", "SearchUserV2", "UserPhotoMetadata",
                          "UserPhoto_V2"]),
    "calendar-dashboard": ("OutlookService", "office365", "action", ["CalendarGetTables", "GetEventsCalendarViewV2"]),
    "sharepoint-list": ("ListService", "sharepointonline", "table", ["get"]),
    "task-tracker": ("TaskService", "commondataserviceforapps", "table", ["get", "post", "patch", "delete"]),
}


def tmpdir(test):
    d = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, d)
    return d


class SpecTests(unittest.TestCase):
    def test_a_good_media_spec_is_normalized(self):
        s = ma.check_spec({**SPEC, "site": SPEC["site"] + "/", "folder": "Shared Documents/Films/",
                           "media": ["video", "image", "video"]})
        self.assertEqual(s["site"], "https://contoso.sharepoint.com/sites/films")
        self.assertEqual(s["folder"], "/Shared Documents/Films")
        self.assertEqual(s["media"], ["image", "video"])
        self.assertEqual(s["maxBytes"], ma.DEFAULT_MAX_BYTES)

    def test_every_kind_has_defaults_and_a_normalized_spec_checks_to_itself(self):
        people = ma.check_spec(SPECS["people-directory"])
        self.assertEqual((people["searchTop"], people["title"]), (12, "People"))
        calendar = ma.check_spec({"kind": "calendar-dashboard", "name": "C"})
        self.assertEqual((calendar["days"], calendar["calendar"]), (14, "Calendar"))
        lst = ma.check_spec(SPECS["sharepoint-list"])
        self.assertEqual((lst["columns"], lst["groupBy"], lst["top"]), ([], "Category", 500))
        tasks = ma.check_spec(SPECS["task-tracker"])
        self.assertEqual(tasks["entitySet"], "cr123_tasks")
        self.assertEqual(tasks["fields"], {"id": "cr123_taskid", "name": "cr123_name", "done": "cr123_done",
                                           "due": "cr123_due", "notes": "cr123_notes"})
        for kind, spec in SPECS.items():
            with self.subTest(kind=kind):
                once = ma.check_spec(spec)
                self.assertEqual(ma.check_spec(once), once, "lifecycle re-checks the spec scaffold saved")
        self.assertEqual(set(SPECS), set(ma.KINDS), "every kind is tested")

    def test_bad_specs_are_refused_with_the_reason(self):
        lst, tasks = SPECS["sharepoint-list"], SPECS["task-tracker"]
        for bad, why in [({**SPEC, "kind": "rapplication"}, "unknown kind"),
                         ({**SPEC, "name": ""}, "name is required"),
                         ({**SPEC, "name": "x" * 61}, "name is required"),
                         ({**SPEC, "site": "https://contoso.example.com/sites/x"}, "SharePoint site URL"),
                         ({**SPEC, "site": "http://contoso.sharepoint.com/sites/x"}, "SharePoint site URL"),
                         ({**SPEC, "folder": "/Shared Documents/../secret"}, "folder must be"),
                         ({**SPEC, "folder": "/"}, "folder must be"),
                         ({**SPEC, "folder": "/Docs/a:b"}, "folder must be"),
                         ({**SPEC, "media": ["pdf"]}, "media must list"),
                         ({**SPEC, "media": [{}]}, "media must list"),
                         ({**SPEC, "maxBytes": -1}, "maxBytes"),
                         ({**SPEC, "maxBytes": "9"}, "maxBytes"),
                         ({"kind": "people-directory", "name": "P", "searchTop": 0}, "searchTop"),
                         ({"kind": "people-directory", "name": "P", "searchTop": True}, "searchTop"),
                         ({"kind": "calendar-dashboard", "name": "C", "days": 32}, "days"),
                         ({"kind": "calendar-dashboard", "name": "C", "calendar": "x" * 101}, "calendar must be"),
                         ({**lst, "site": "https://contoso.sharepoint.com"}, "SharePoint site URL"),
                         ({**lst, "list": ""}, "list must be"),
                         ({**lst, "list": " Padded "}, "list must be"),
                         ({**lst, "columns": ["Title", "Bad Name"]}, "columns must list"),
                         ({**lst, "groupBy": "a-b"}, "groupBy must be"),
                         ({**lst, "top": 5001}, "top must be"),
                         ({**tasks, "table": "task"}, "logical name"),
                         ({**tasks, "table": "Cr123_Task"}, "logical name"),
                         ({**tasks, "table": "c_task"}, "logical name"),
                         ({**tasks, "entitySet": "Tasks!"}, "entitySet"),
                         ({**tasks, "dataverseUrl": "https://evil.example.com"}, "dataverseUrl"),
                         ("nope", "JSON object")]:
            with self.subTest(why=why, bad=bad), self.assertRaisesRegex(ma.ManagedAppError, why):
                ma.check_spec(bad)


class ScaffoldTests(unittest.TestCase):
    def test_the_project_has_the_template_shape_with_pinned_versions(self):
        d = ma.scaffold(SPEC, tmpdir(self))["dir"]
        for f in ["package.json", "vite.config.ts", "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json",
                  "index.html", ".gitignore", "src/main.tsx", "src/App.tsx", "src/App.css", "src/index.css",
                  "src/media.ts", "src/connector.ts", "src/config.ts", "src/bound.ts", "report.json",
                  "managed-app.spec.json"]:
            self.assertTrue((d / f).is_file(), f)
        pkg = json.loads((d / "package.json").read_text())
        self.assertEqual(pkg["scripts"]["build"], "tsc -b && vite build")
        self.assertEqual(pkg["dependencies"]["@microsoft/managed-apps"], "^0.5.17")
        self.assertEqual(pkg["devDependencies"]["@microsoft/managed-apps-vite-plugin"], "^0.3.28")
        self.assertIn("managedApps()", (d / "vite.config.ts").read_text())
        self.assertFalse((d / "ms.config.json").exists(), "ms app init writes ms.config.json, not the build")

    def test_the_build_is_deterministic_and_keeps_what_ms_and_npm_own(self):
        for kind, spec in SPECS.items():
            with self.subTest(kind=kind):
                out = tmpdir(self)
                a = ma.scaffold(spec, out)["dir"]
                first = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
                for owned in ["generated/services/X.ts", "ms.config.json", "node_modules/x/index.js"]:
                    (a / owned).parent.mkdir(parents=True, exist_ok=True)
                    (a / owned).write_text("kept")
                (a / "src" / "stale.ts").write_text("export const x = 1")
                ma.scaffold(spec, out)
                again = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()
                         and not str(p.relative_to(a)).startswith(("generated", "node_modules", "ms.config"))}
                self.assertEqual(first, again, "same spec, same bytes; stale files removed")
                for owned in ["generated/services/X.ts", "ms.config.json", "node_modules/x/index.js"]:
                    self.assertEqual((a / owned).read_text(), "kept", owned)

    def test_each_app_calls_only_its_least_privilege_and_the_report_says_so(self):
        for kind, (alias, connector, how, allowed) in LEAST.items():
            with self.subTest(kind=kind):
                made = ma.scaffold(SPECS[kind], tmpdir(self))
                (entry,) = made["report"]["dataSources"]
                self.assertEqual((entry["service"], entry["connector"], entry["as"]), (alias, connector, how))
                self.assertEqual(entry["allowedActions"], allowed)
                calls = ma.calls_in(made["dir"], alias)
                self.assertEqual(calls if how == "action" else ma.verbs_for(calls), allowed)
                self.assertTrue(made["report"]["summary"])
        self.assertEqual(ma.scaffold(SPECS["sharepoint-list"], tmpdir(self))["report"]["dataSources"][0]["table"],
                         "Contoso Rules")

    def test_a_template_that_calls_more_than_it_declares_is_refused(self):
        kinds = copy.deepcopy(ma.KINDS)
        self.addCleanup(lambda: ma.KINDS.update(kinds))
        narrow = lambda s: [{**src, "calls": ("CalendarGetTables",)} for src in ma._calendar_sources(s)]
        ma.KINDS["calendar-dashboard"] = {**ma.KINDS["calendar-dashboard"], "sources": narrow}
        with self.assertRaisesRegex(ma.ManagedAppError, "GetEventsCalendarViewV2"):
            ma.scaffold(SPECS["calendar-dashboard"], tmpdir(self))

    def test_no_app_reaches_the_network_or_generated_code_except_through_bound_ts(self):
        for kind, spec in SPECS.items():
            with self.subTest(kind=kind):
                d = ma.scaffold(spec, tmpdir(self))["dir"]
                sources = {p.name: p.read_text() for p in (d / "src").rglob("*.ts*")}
                code = "\n".join(sources.values())
                # the connector-first rule: no direct HTTP from a managed app (the sandbox refuses it); and no blob:
                for banned in (r"\bfetch\s*\(", r"XMLHttpRequest", r"\baxios\b", r"graph\.microsoft\.com",
                               r"WebSocket", r"createObjectURL", r"\b(confirm|alert|prompt)\s*\("):
                    self.assertNotRegex(code, banned)
                naming = sorted(n for n, t in sources.items() if "generated/" in t)
                self.assertEqual(naming, ["bound.ts"], "only bound.ts names generated code")

    def test_binary_content_is_shown_as_data_urls_because_the_player_blocks_blob_urls(self):
        d = ma.scaffold({**SPEC, "media": ["video", "image", "audio"]}, tmpdir(self))["dir"]
        app, connector = (d / "src" / "App.tsx").read_text(), (d / "src" / "connector.ts").read_text()
        self.assertIn("dataUrl(bytes, typeOf(name, CONFIG.mime))", app)
        self.assertIn("data:${type};base64,", connector)
        self.assertIn("0x8000", connector, "base64 in chunks, so a large file doesn't overflow the stack")
        mime = json.loads((d / "src" / "config.ts").read_text().split(" = ", 1)[1])["mime"]
        self.assertEqual((mime["mp4"], mime["mp3"], mime["png"]), ("video/mp4", "audio/mpeg", "image/png"))
        only_video = ma.scaffold(SPEC, tmpdir(self))["dir"]
        self.assertNotIn("png", json.loads((only_video / "src" / "config.ts").read_text().split(" = ", 1)[1])["mime"])
        people = ma.scaffold(SPECS["people-directory"], tmpdir(self))["dir"]
        self.assertIn("imageUrl(photo as unknown, meta.ContentType", (people / "src" / "App.tsx").read_text())
        self.assertRegex(connector, r"if \(/\^https\?:/i\.test\(data\)\) throw", "an http photo URL is refused")

    def test_spec_values_are_data_in_config_ts_and_the_screens_are_the_templates_verbatim(self):
        hostile = {**SPEC, "title": "Films <b>{x}</b> & `${more}` </script>",
                   "folder": "/Shared Documents/Kody's Films \u2014 2026"}
        for kind, spec in {**SPECS, "sharepoint-media": hostile}.items():
            with self.subTest(kind=kind):
                d = ma.scaffold(spec, tmpdir(self))["dir"]
                self.assertEqual((d / "src" / "App.tsx").read_text(), ma._template(kind, "App.tsx"))
                config = (d / "src" / "config.ts").read_text(encoding="utf-8")
                self.assertRegex(config, r"export const CONFIG: \{ title: string;")
                self.assertEqual(json.loads(config.split(" = ", 1)[1])["title"], ma.check_spec(spec)["title"])
        d = ma.scaffold(hostile, tmpdir(self))["dir"]
        index = (d / "index.html").read_text()
        self.assertIn("<title>Films &lt;b&gt;{x}&lt;/b&gt; &amp; `${more}` &lt;/script&gt;</title>", index)
        self.assertEqual(json.loads((d / "src" / "config.ts").read_text().split(" = ", 1)[1])["folder"],
                         "/Shared Documents/Kody's Films \u2014 2026")

    def test_the_page_carries_a_build_id_that_changes_exactly_when_the_build_does(self):
        made = ma.scaffold(SPEC, tmpdir(self))
        bid = made["report"]["build"]
        self.assertRegex(bid, r"^[0-9a-f]{16}$")
        self.assertIn(f'<meta name="brainfreeze-build" content="{bid}" />', (made["dir"] / "index.html").read_text())
        self.assertEqual(ma.scaffold(SPEC, tmpdir(self))["report"]["build"], bid, "same spec, same build")
        self.assertNotEqual(ma.scaffold({**SPEC, "maxBytes": 5}, tmpdir(self))["report"]["build"], bid, "spec change")
        files = ma.files_for(SPEC)
        self.assertEqual(ma.build_id({**files, "src/bound.ts": "rewritten by the lifecycle"}), bid,
                         "bound.ts follows the CLI's names, so it doesn't change the build")
        self.assertNotEqual(ma.build_id({**files, "src/App.tsx": files["src/App.tsx"] + "\n"}), bid, "template change")
        kinds = {ma.scaffold(spec, tmpdir(self))["report"]["build"] for spec in SPECS.values()}
        self.assertEqual(len(kinds), len(SPECS), "every kind builds differently")

    def test_every_template_file_ships_in_the_package(self):
        data = re.search(r"^brainfreeze_studio = \[(.*?)\]", (ROOT / "pyproject.toml").read_text(), re.S | re.M)
        globs = re.findall(r'"([^"]+)"', data.group(1))
        files = [p.relative_to(ma.TEMPLATES.parent).as_posix() for p in ma.TEMPLATES.rglob("*") if p.is_file()]
        self.assertTrue(files)
        for f in files:
            self.assertTrue(any(fnmatch.fnmatch(f, g) for g in globs), f)
        for kind, k in ma.KINDS.items():
            for name in k["files"]:
                self.assertTrue((ma.TEMPLATES / kind / name).is_file(), f"{kind}/{name}")


def app_with(test, spec, config, services):
    """A scaffolded app with a given ms.config.json and generated/services/{stem}.ts files."""
    d = ma.scaffold(spec, tmpdir(test))["dir"]
    (d / "ms.config.json").write_text(json.dumps(config))
    (d / "generated" / "services").mkdir(parents=True)
    for stem, text in services.items():
        (d / "generated" / "services" / f"{stem}.ts").write_text(text)
    return d


SP_REF = "/providers/Microsoft.PowerApps/apis/shared_sharepointonline"
DV_REF = "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps"


class BindingTests(unittest.TestCase):
    def test_bind_arguments_are_the_working_cli_forms(self):
        env = "00000000-0000-0000-0000-000000000000"
        args = {k: ma._bind_args(ma.sources_for(s)[0], env) for k, s in SPECS.items()}
        self.assertEqual(args["sharepoint-media"], ["ms", "app", "add", "data-source", "--connector",
                                                    "sharepointonline", "--as", "action", "--use-sso"])
        self.assertEqual(args["people-directory"][5:], ["office365users", "--as", "action", "--use-sso"])
        self.assertEqual(args["calendar-dashboard"][5:], ["office365", "--as", "action", "--use-sso"])
        self.assertEqual(args["sharepoint-list"][5:], ["sharepointonline", "--as", "table", "--dataset",
                                                       "https://contoso.sharepoint.com/sites/ops", "--table",
                                                       "Contoso Rules", "--use-sso"])
        # `--connector dataverse` is ambiguous in ms 0.25.1; the connector id is not
        self.assertEqual(args["task-tracker"][5:], ["commondataserviceforapps", "--as", "table", "--table",
                                                    "cr123_task", "--dataverse-environment-id", env, "--use-sso"])

    def test_bound_tables_are_found_by_site_and_name_or_key(self):
        src = ma.sources_for(SPECS["sharepoint-list"])[0]
        config = {"connectionReferences": {"r": {"id": SP_REF, "dataSources": ["contosorules"], "dataSets": {
            "https://CONTOSO.sharepoint.com/sites/ops/": {"dataSources": {"contosorules": {"tableName": "Contoso Rules"}}},
            "https://contoso.sharepoint.com/sites/hr": {"dataSources": {"other": {"tableName": "Other"}}}}}}}
        self.assertEqual(ma.table_key(config, src), ("r", "https://CONTOSO.sharepoint.com/sites/ops/", "contosorules"))
        self.assertFalse(ma.is_bound(config, {**src, "dataset": "https://contoso.sharepoint.com/sites/hr"}))
        self.assertFalse(ma.is_bound(config, ma.sources_for(SPEC)[0]), "a table is not the action data source")
        config["connectionReferences"]["r"]["dataSources"].append("sharepointonline")
        self.assertTrue(ma.is_bound(config, ma.sources_for(SPEC)[0]))
        task = ma.sources_for(SPECS["task-tracker"])[0]
        dv = {"connectionReferences": {"d": {"id": DV_REF, "dataSets": {
            "https://contoso.api.crm.dynamics.com": {"dataSources": {"cr123_task": {"tableName": "cr123_tasks"}}}}}}}
        self.assertEqual(ma.table_key(dv, task)[2], "cr123_task")

    def test_bound_ts_follows_what_the_cli_generated(self):
        spec = SPECS["sharepoint-list"]
        config = {"connectionReferences": {"r": {"id": SP_REF, "dataSets": {spec["site"]: {"dataSources": {
            "contosorules1": {"tableName": "Contoso Rules"}}}}}}}
        d = app_with(self, spec, config, {
            "ContosoRules1Service": "export class ContosoRules1Service {\n  private static readonly dataSourceName = 'contosorules1';\n}",
            "SharePointService": "export class SharePointService {\n  private static readonly dataSourceName = 'sharepointonline';\n}"})
        resolved = ma.resolve_services(d, ma.sources_for(spec), config)
        self.assertEqual(resolved, {"ListService": ("ContosoRules1Service", "ContosoRules1Service", "named")})
        self.assertIn("export { ContosoRules1Service as ListService } from '../generated/services/ContosoRules1Service'",
                      ma.bound_ts(ma.sources_for(spec), resolved))
        self.assertNotEqual(ma.bound_ts(ma.sources_for(spec), resolved), (d / "src" / "bound.ts").read_text(),
                            "the prediction (ContosoRulesService) is replaced")
        task = SPECS["task-tracker"]
        dv = {"connectionReferences": {"d": {"id": DV_REF, "dataSets": {"https://x.api.crm.dynamics.com": {
            "dataSources": {"cr123_task": {"tableName": "cr123_tasks"}}}}}}}
        t = app_with(self, task, dv, {"Cr123_tasksService": "export default class Cr123_tasksService {\n"
                                                            "  public static readonly dataSourceName = 'cr123_task';\n}"})
        bound = ma.bound_ts(ma.sources_for(task), ma.resolve_services(t, ma.sources_for(task), dv))
        self.assertIn("export { default as TaskService } from '../generated/services/Cr123_tasksService'", bound)
        self.assertEqual(bound, (t / "src" / "bound.ts").read_text(), "predicted right, nothing to rewrite")

    def test_a_data_source_without_a_generated_service_is_an_error(self):
        spec = SPECS["people-directory"]
        d = app_with(self, spec, {"connectionReferences": {}}, {
            "SharePointService": "export class SharePointService { static readonly dataSourceName = 'sharepointonline' }"})
        with self.assertRaisesRegex(ma.ManagedAppError, "no generated service for office365users"):
            ma.resolve_services(d, ma.sources_for(spec), {"connectionReferences": {}})


class FakeDataverse:
    """The Web API calls ensure_dataverse_table makes, answered from a dict of what exists."""
    def __init__(self, prefix="cr123", table=False, columns=(), entity_set="cr123_tasks"):
        self.prefix, self.table, self.columns, self.entity_set, self.calls = prefix, table, set(columns), entity_set, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/publishers"):
            return 200, {"value": [{"customizationprefix": self.prefix}]}
        m = re.fullmatch(r"/EntityDefinitions\(LogicalName='(\w+)'\)(/Attributes(\(LogicalName='(\w+)'\))?)?(\?.*)?", path)
        if method == "POST" and path == "/EntityDefinitions":
            self.table = True
            return 204, {}
        if method == "POST" and m and m.group(2):
            self.columns.add(body["SchemaName"])
            return 204, {}
        if m and m.group(4):
            return (200, {"LogicalName": m.group(4)}) if m.group(4) in self.columns else (404, "not found")
        if m:
            return (200, {"LogicalName": m.group(1), "EntitySetName": self.entity_set}) if self.table else (404, "nf")
        raise AssertionError(path)


class DataverseTableTests(unittest.TestCase):
    def test_a_missing_table_is_created_with_the_skill_column_shapes(self):
        dv = FakeDataverse()
        r = ma.ensure_dataverse_table("https://contoso.crm.dynamics.com", SPECS["task-tracker"], call=dv, log=lambda m: None)
        self.assertEqual(r, {"created": True, "added": ["cr123_done", "cr123_due", "cr123_notes"], "entitySet": "cr123_tasks"})
        posts = [(p, b) for m, p, b in dv.calls if m == "POST"]
        table = posts[0][1]
        self.assertEqual((table["SchemaName"], table["PrimaryNameAttribute"], table["OwnershipType"]),
                         ("cr123_task", "cr123_name", "UserOwned"))
        self.assertEqual(table["Attributes"][0]["IsPrimaryName"], True)
        kinds = {b["SchemaName"]: b["AttributeType"] for _, b in posts[1:]}
        self.assertEqual(kinds, {"cr123_done": "Boolean", "cr123_due": "DateTime", "cr123_notes": "Memo"})
        self.assertIn("OptionSet", posts[1][1], "a Boolean column needs its option set")

    def test_what_exists_is_left_alone(self):
        dv = FakeDataverse(table=True, columns=("cr123_done", "cr123_due", "cr123_notes"))
        r = ma.ensure_dataverse_table("https://contoso.crm.dynamics.com", SPECS["task-tracker"], call=dv, log=lambda m: None)
        self.assertEqual(r, {"created": False, "added": [], "entitySet": "cr123_tasks"})
        self.assertEqual([m for m, _, _ in dv.calls], ["GET"] * len(dv.calls))

    def test_a_prefix_or_entity_set_that_does_not_match_the_environment_is_refused(self):
        dv = FakeDataverse(prefix="abc12")
        with self.assertRaisesRegex(ma.ManagedAppError, "prefix is 'abc12': name the table abc12_task"):
            ma.ensure_dataverse_table("https://contoso.crm.dynamics.com", SPECS["task-tracker"], call=dv, log=lambda m: None)
        self.assertNotIn("POST", [m for m, _, _ in dv.calls])
        odd = FakeDataverse(table=True, columns=("cr123_done", "cr123_due", "cr123_notes"), entity_set="cr123_taskses")
        with self.assertRaisesRegex(ma.ManagedAppError, '"entitySet": "cr123_taskses"'):
            ma.ensure_dataverse_table("https://contoso.crm.dynamics.com", SPECS["task-tracker"], call=odd, log=lambda m: None)
        with self.assertRaisesRegex(ma.ManagedAppError, "only a task-tracker"):
            ma.ensure_dataverse_table("https://contoso.crm.dynamics.com", SPEC, call=FakeDataverse(), log=lambda m: None)


class PolicyTargetTests(unittest.TestCase):
    calls = staticmethod(lambda alias: {"ListService": ["getAll"], "SharePointService": ["ListFolder", "GetFileContentByPath"],
                                        "TaskService": ["CreateRecord", "ListRecords"]}[alias])

    def test_a_policy_goes_to_the_shared_reference_that_holds_the_data_source(self):
        media = ma.sources_for(SPEC)
        # a non-shared SharePoint reference holding only a table, listed first; the shared one holds the actions
        config = {"connectionReferences": {
            "tables": {"id": SP_REF, "dataSources": ["contosorules"], "dataSets": {SPEC["site"]: {"dataSources": {
                "contosorules": {"tableName": "Contoso Rules"}}}}},
            "actions": {"id": SP_REF, "dataSources": ["sharepointonline"], "sharedConnectionId": "s1"}}}
        self.assertEqual(ma.policy_targets(config, media, self.calls),
                         [{"command": "allow", "connector": "sharepointonline", "reference": "actions",
                           "allow": ["ListFolder", "GetFileContentByPath"]}])
        lst = ma.sources_for({**SPECS["sharepoint-list"], "site": SPEC["site"]})
        self.assertEqual(ma.policy_targets(config, lst, self.calls), [], "the table's reference isn't shared")
        config["connectionReferences"]["tables"]["sharedConnectionId"] = "s2"
        self.assertEqual(ma.policy_targets(config, lst, self.calls),
                         [{"command": "allow-table", "connector": "sharepointonline", "reference": "tables",
                           "dataset": SPEC["site"], "table": "contosorules", "allow": ["get"]}])

    def test_non_shared_references_get_no_policy_and_a_missing_source_is_an_error(self):
        task = ma.sources_for(SPECS["task-tracker"])
        dv = {"connectionReferences": {"d": {"id": DV_REF, "sharedConnectionId": "  ", "dataSets": {
            "https://contoso.api.crm.dynamics.com": {"dataSources": {"cr123_task": {"tableName": "cr123_tasks"}}}}}}}
        self.assertEqual(ma.policy_targets(dv, task, self.calls), [], "a blank sharedConnectionId is not shared")
        dv["connectionReferences"]["d"]["sharedConnectionId"] = "d1"
        self.assertEqual(ma.policy_targets(dv, task, self.calls)[0]["allow"], ["get", "post"])
        with self.assertRaisesRegex(ma.ManagedAppError, "no sharepointonline action data source"):
            ma.policy_targets({"connectionReferences": {}}, ma.sources_for(SPEC), self.calls)


FAKE_MS = r'''
import json, os, re, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(["ms", os.getcwd(), *args]) + "\n")
opt = lambda name: args[args.index(name) + 1] if name in args else None
config = Path("ms.config.json")
if args[:2] == ["app", "init"]:
    env = opt("--environment-id") or "env-default"
    config.write_text(json.dumps({"appId": "app-1", "environmentId": env, "repoType": "native", "connectionReferences": {}}))
    print("Creating the app...\n" + json.dumps({"success": True, "appId": "app-1", "environmentId": env}))
    sys.exit(0)
if args[:3] == ["app", "add", "data-source"]:
    if os.environ.get("FAKE_BIND_NOOP"):
        print("Data source added."); sys.exit(0)
    cfg = json.loads(config.read_text())
    connector, how, suffix = opt("--connector"), opt("--as"), os.environ.get("FAKE_TABLE_SUFFIX", "")
    ref = cfg["connectionReferences"].setdefault("ref-" + connector, {
        "id": "/providers/Microsoft.PowerApps/apis/shared_" + connector, "displayName": connector, "dataSources": [], "dataSets": {}})
    if os.environ.get("FAKE_SHARED"):
        ref["sharedConnectionId"] = "shared-1"
    services = Path("generated/services"); services.mkdir(parents=True, exist_ok=True)
    if how == "action":
        ref["dataSources"].append(connector)
        name = {"sharepointonline": "SharePointService", "office365users": "Office365UsersService",
                "office365": "Office365OutlookService"}[connector]
        (services / f"{name}.ts").write_text(f"export class {name} {{\n  private static readonly dataSourceName = '{connector}';\n}}\n")
    else:
        table = opt("--table"); key = re.sub(r"[^a-z0-9_]", "", table.lower()) + suffix
        dataverse = connector == "commondataserviceforapps"
        ref["dataSources"].append(key)
        ref["dataSets"].setdefault(opt("--dataset") or "https://contoso.api.crm.dynamics.com", {"dataSources": {}})[
            "dataSources"][key] = {"tableName": table + "s" if dataverse else table}
        if dataverse:
            name = table[:1].upper() + table[1:] + "sService"
            (services / f"{name}.ts").write_text(f"export default class {name} {{\n  public static readonly dataSourceName = '{key}';\n}}\n")
        else:
            name = re.sub(r"[^A-Za-z0-9]", "", table) + suffix + "Service"
            (services / f"{name}.ts").write_text(f"export class {name} {{\n  private static readonly dataSourceName = '{key}';\n}}\n")
    config.write_text(json.dumps(cfg))
    print("Data source added.")
    sys.exit(0)
print(json.dumps({"success": False, "errorMessage": "fake ms: unexpected " + " ".join(args)}))
sys.exit(1)
'''

# copilot-harness-sdk's scripts/managed-apps.mjs, as far as lifecycle() can tell: it needs a real app directory and
# script path (so a relative path resolved from the wrong directory fails here as it would there)
FAKE_NODE = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(["node", os.getcwd(), *args]) + "\n")
if not os.path.isfile(args[0]) or not os.path.isfile(os.path.join(args[2], "ms.config.json")):
    print(f"fake node: no script {args[0]} or no app at {args[2]}", file=sys.stderr); sys.exit(2)
if args[1] == "deploy":
    print(json.dumps({"success": True, "commitHash": "abc1234", "appPlayUri": "https://play.example.test/apps/app-1"}))
else:
    print("OK")
'''

FAKE_NPM = r'''
import json, os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(["npm", os.getcwd(), *sys.argv[1:]]) + "\n")
'''


@unittest.skipIf(os.name == "nt", "the fake ms, node and npm are extensionless scripts")
class LifecycleTests(unittest.TestCase):
    """lifecycle() end to end against stand-ins for the ms CLI, node (copilot-harness-sdk) and npm, with real git."""

    def setUp(self):
        self.root = tmpdir(self)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("ms", FAKE_MS), ("node", FAKE_NODE), ("npm", FAKE_NPM)):
            (bin_dir / name).write_text(f"#!{sys.executable}\n{body}")
            (bin_dir / name).chmod(0o755)
        (self.root / "sdk" / "scripts").mkdir(parents=True)
        (self.root / "sdk" / "scripts" / "managed-apps.mjs").write_text("// stand-in\n")
        (self.root / "gitconfig").write_text("")
        self.log = self.root / "calls.jsonl"
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_LOG": str(self.log),
               "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
               "GIT_COMMITTER_EMAIL": "test@example.com"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for flag in ("FAKE_SHARED", "FAKE_TABLE_SUFFIX", "FAKE_BIND_NOOP"):
            os.environ.pop(flag, None)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)   # the paths given to lifecycle() below are relative to here

    def calls(self, tool=None, since=0):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [r for r in rows[since:] if tool is None or r[0] == tool]

    def run_lifecycle(self, spec, **kw):
        ma.scaffold(spec, "out")
        options = {"display_name": spec["name"], "sdk_dir": "sdk", "tenant_id": "tenant-1", "log": lambda m: None}
        return ma.lifecycle("out/managed-app", **{**options, **kw})

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root / "out" / "managed-app"), *args], capture_output=True,
                              text=True, check=True).stdout

    def test_a_first_run_registers_binds_follows_the_generated_names_builds_commits_pushes_and_deploys(self):
        os.environ.update(FAKE_SHARED="1", FAKE_TABLE_SUFFIX="1")
        result = self.run_lifecycle(SPECS["sharepoint-list"], environment_id="env-1", login_hint="user@contoso.com",
                                    git_cache="cache.json")
        self.assertEqual(result, {"appId": "app-1", "commit": "abc1234", "playUrl": "https://play.example.test/apps/app-1"})
        app = str((self.root / "out" / "managed-app").resolve())
        self.assertEqual({r[1] for r in self.calls()}, {app}, "every command runs in the app's directory")
        self.assertEqual([r[2:] for r in self.calls("ms")], [
            ["app", "init", "--display-name", "Rules", "--repo", "native", "--environment-id", "env-1",
             "--non-interactive", "--json"],
            ["app", "add", "data-source", "--connector", "sharepointonline", "--as", "table", "--dataset",
             "https://contoso.sharepoint.com/sites/ops", "--table", "Contoso Rules", "--use-sso", "--non-interactive",
             "--json"]])
        script = str((self.root / "sdk" / "scripts" / "managed-apps.mjs").resolve())
        node = self.calls("node")
        self.assertEqual({r[2] for r in node}, {script})
        self.assertEqual([r[3:] for r in node], [
            ["allow-table", app, "sharepointonline", "contosorules1", "get", "--dataset",
             "https://contoso.sharepoint.com/sites/ops", "--reference", "ref-sharepointonline"],
            ["check", app],
            ["push", app, "--tenant", "tenant-1", "--login-hint", "user@contoso.com", "--cache",
             str((self.root / "cache.json").resolve())],
            ["deploy", app]])
        self.assertEqual([r[2:] for r in self.calls("npm")], [["install", "--no-audit", "--no-fund"], ["run", "build"]])
        self.assertEqual(self.git("log", "--format=%s").strip(), "Rules: generated by brainfreeze-studio")
        self.assertIn("export { ContosoRules1Service as ListService } from '../generated/services/ContosoRules1Service'",
                      self.git("show", "HEAD:src/bound.ts"), "the commit has bound.ts as rewritten from generated/")
        self.assertEqual(self.git("config", "--local", "--get-all", "credential.helper").split("\n")[:2], ["", "manager"])

    def test_a_second_run_registers_and_binds_nothing_and_commits_nothing_new(self):
        self.run_lifecycle(SPECS["people-directory"])
        head, since = self.git("rev-parse", "HEAD"), len(self.calls())
        self.assertEqual(ma.lifecycle("out/managed-app", display_name="People", sdk_dir="sdk", tenant_id="tenant-1",
                                      log=lambda m: None)["playUrl"], "https://play.example.test/apps/app-1")
        self.assertEqual(self.calls("ms", since), [], "already registered and bound")
        self.assertEqual([r[3] for r in self.calls("node", since)], ["check", "push", "deploy"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_actions_on_a_shared_connection_allow_exactly_what_the_code_calls_on_its_reference(self):
        os.environ["FAKE_SHARED"] = "1"
        self.run_lifecycle(SPECS["people-directory"])
        allow = [r[3:] for r in self.calls("node") if r[3] == "allow"]
        self.assertEqual(allow, [["allow", str((self.root / "out" / "managed-app").resolve()), "office365users",
                                  ",".join(LEAST["people-directory"][3]), "--reference", "ref-office365users"]])

    def test_a_dataverse_table_binds_in_the_apps_environment_and_needs_no_policy_when_not_shared(self):
        result = self.run_lifecycle(SPECS["task-tracker"], deploy=False)
        self.assertEqual(result, {"appId": "app-1"})
        bind = self.calls("ms")[1][2:]
        self.assertEqual(bind[bind.index("--dataverse-environment-id") + 1], "env-default", "the environment ms chose")
        self.assertEqual([r[3] for r in self.calls("node")], ["check"], "no policy, no push, no deploy")
        self.assertIn("export { default as TaskService } from '../generated/services/Cr123_tasksService'",
                      self.git("show", "HEAD:src/bound.ts"))

    def test_a_bind_that_binds_nothing_stops_before_the_build(self):
        os.environ["FAKE_BIND_NOOP"] = "1"
        with self.assertRaisesRegex(ma.ManagedAppError, "has no office365users"):
            self.run_lifecycle(SPECS["people-directory"])
        self.assertEqual((self.calls("npm"), self.calls("node")), ([], []))

    def test_create_table_is_only_for_a_task_tracker_and_is_checked_before_anything_runs(self):
        with self.assertRaisesRegex(ma.ManagedAppError, "create_table is for a task-tracker"):
            self.run_lifecycle(SPECS["people-directory"], create_table=True)
        with self.assertRaisesRegex(ma.ManagedAppError, "create_table is for a task-tracker"):
            self.run_lifecycle(SPECS["task-tracker"], create_table=True)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "out" / "managed-app" / ".git").exists())


@unittest.skipUnless(os.environ.get("MANAGED_APP_BUILD") == "1", "set MANAGED_APP_BUILD=1 (with MANAGED_APP_FIXTURE and "
                     "MANAGED_APP_FIXTURE_SPECS) to compile every kind against real generated services")
class CompileTests(unittest.TestCase):
    def test_every_kind_compiles_against_real_generated_services(self):
        # opted in: a missing fixture is a failure, not a skip
        fixture = Path(os.environ.get("MANAGED_APP_FIXTURE", ""))
        specs_file = Path(os.environ.get("MANAGED_APP_FIXTURE_SPECS", ""))
        for need in (fixture / "generated", fixture / "ms.config.json", fixture / "node_modules", specs_file):
            self.assertTrue(need.exists(), f"MANAGED_APP_BUILD=1 needs {need}")
        specs = json.loads(specs_file.read_text())
        self.assertEqual(set(specs), set(ma.KINDS), "a spec for every kind")
        for kind, spec in specs.items():
            with self.subTest(kind=kind):
                d = ma.scaffold(spec, tmpdir(self))["dir"]
                shutil.copytree(fixture / "generated", d / "generated")
                shutil.copy(fixture / "ms.config.json", d / "ms.config.json")
                (d / "node_modules").symlink_to((fixture / "node_modules").resolve())
                sources = ma.sources_for(spec)
                config = json.loads((d / "ms.config.json").read_text())
                (d / "src" / "bound.ts").write_text(ma.bound_ts(sources, ma.resolve_services(d, sources, config)))
                r = subprocess.run(["npm", "run", "build"], cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
                self.assertTrue((d / "dist" / "index.html").is_file())


class CliTests(unittest.TestCase):
    def run_cli(self, spec, *args):
        tmp = tmpdir(self)
        path = tmp / "spec.json"
        path.write_text(json.dumps(spec))
        return subprocess.run([sys.executable, "-m", "brainfreeze_studio", "managed-app", str(path), "--out",
                               str(tmp / "out"), *args], cwd=ROOT, capture_output=True, text=True)

    def test_managed_app_command_writes_every_kind_and_refuses_deploy_without_the_sdk(self):
        for kind, spec in SPECS.items():
            with self.subTest(kind=kind):
                r = self.run_cli(spec, "--json")
                self.assertEqual(r.returncode, 0, r.stderr)
                out = json.loads(r.stdout)
                self.assertTrue(out["dir"].endswith("managed-app"))
                self.assertEqual(out["report"]["dataSources"][0]["allowedActions"], LEAST[kind][3])
                text = self.run_cli(spec)
                self.assertIn(f"{kind}:", text.stdout)
        r = self.run_cli(SPEC, "--deploy")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--sdk-dir", r.stderr)
        r = self.run_cli({**SPEC, "kind": "nope"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown kind", r.stderr)


if __name__ == "__main__":
    unittest.main()
