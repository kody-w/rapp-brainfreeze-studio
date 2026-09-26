"""Managed apps (Microsoft Copilot Managed Runtime) from a small spec.

    from brainfreeze_studio import managed_app
    app = managed_app.scaffold({"kind": "sharepoint-media", "name": "Film Library",
                                "site": "https://contoso.sharepoint.com/sites/films", "folder": "/Shared Documents/Films"},
                               "out/")
    # out/managed-app/: a React + Vite project in the shape of Microsoft's template (microsoft/managed-apps
    # templates/vite8), with the app's screens in src/ and report.json (the connectors and actions it needs)

A managed app runs in Microsoft's App Player and reaches data only through Power Platform connectors, with typed
services the managed apps CLI (`ms`, @microsoft/managed-apps-cli) generates under generated/. The build is offline
and deterministic, like the rest of this tool: it writes the project and the least-privilege action list its code
calls. `lifecycle()` then does what the microsoft-managed-apps plugin skills do (create-app, add-data-source and the
add-* connector skills, deploy): `ms app init`, `ms app add data-source`, `npm run build`, a push to the app's
platform repository and `ms app deploy`, driving copilot-harness-sdk's `scripts/managed-apps.mjs` for the parts the
skills leave to a person (the shared-connection action policy, and git credentials without Git Credential Manager's
interactive flow).

Kinds (the connector decision guide's common app patterns, one per connector skill):
  sharepoint-media    a folder of videos, images or audio in a SharePoint document library, played in the app
                      (add-sharepoint, actions). Media load as data: URLs: the deployed player's content security
                      policy allows `media-src 'self' data:` and blocks blob: URLs (seen live, 26 Sep 2026).
  people-directory    you, your manager and your direct reports, and a directory search, with profile photos
                      (add-office365-users; photos as data: URLs, as that skill says)
  calendar-dashboard  the coming days of your Outlook calendar, by day, with a few totals (add-office365; the
                      calendar id is discovered with CalendarGetTables, never assumed)
  sharepoint-list     a SharePoint list as a searchable table, read-only (add-sharepoint, table mode: verb "get")
  task-tracker        tasks in a Dataverse table: add, complete, delete (add-dataverse; verbs get, post, patch, delete)

Each app's code is a template in managed_app_templates/<kind>/ that is valid TypeScript as it stands. The spec's values
go to src/config.ts, and src/bound.ts is the one file that names generated code: predicted when the project is
written, rewritten from generated/services (each service's dataSourceName) once `ms app add data-source` has run, so
a CLI that names a service differently is followed, and one whose signatures differ fails the type check.
"""
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# The versions managed apps were built, deployed and played with (26 Sep 2026); the template ranges resolve to them.
VERSIONS = {"@microsoft/managed-apps": "0.5.17", "react": "19.3.0", "react-dom": "19.3.0",
            "@microsoft/managed-apps-vite-plugin": "0.3.28", "@vitejs/plugin-react": "6.1.1", "vite": "8.3.0",
            "typescript": "6.0.3", "@types/react": "19.3.0", "@types/react-dom": "19.3.0", "@types/node": "24.13.6"}
TEMPLATES = Path(__file__).with_name("managed_app_templates")
SHAREPOINT = "sharepointonline"
USERS = "office365users"
OUTLOOK = "office365"
DATAVERSE = "commondataserviceforapps"   # `--connector dataverse` is ambiguous in ms 0.25.1; this id is not
MEDIA = {"video": ("mp4", "m4v", "webm", "mov"), "image": ("png", "jpg", "jpeg", "gif", "webp", "svg"),
         "audio": ("mp3", "m4a", "wav", "ogg")}
MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "png": "image/png",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg"}
DEFAULT_MAX_BYTES = 200 * 1000 * 1000
# What each template calls on its connector: its least privilege. A shared connection must declare exactly what the
# app may do (allowed-actions.md), and scaffold() refuses a template that calls anything else.
SHAREPOINT_MEDIA_ACTIONS = ("GetFileContentByPath", "GetFolderMetadataByPath", "ListFolder")
PEOPLE_ACTIONS = ("DirectReports_V2", "Manager_V2", "MyProfile_V2", "SearchUserV2", "UserPhotoMetadata", "UserPhoto_V2")
CALENDAR_ACTIONS = ("CalendarGetTables", "GetEventsCalendarViewV2")
LIST_METHODS = ("getAll",)
TASK_METHODS = ("CreateRecord", "DeleteRecord", "ListRecords", "UpdateRecord")
# A table's allowedActions are four verbs, not operation ids (allowed-actions.md); a generated table service's
# methods map onto them the way copilot-harness-sdk's inferAllowedActions maps them.
TABLE_VERBS = ("get", "post", "patch", "delete")
_VERB_OF = ((r"^(get|list|read|query|search|find|lookup)", "get"), (r"^(create|post|insert|add)", "post"),
            (r"^(update|patch|set)", "patch"), (r"^(delete|remove)", "delete"))
_SITE = re.compile(r"https://[a-z0-9-]+\.sharepoint\.(com|us|cn|de)/(sites|teams)/[A-Za-z0-9._~%-]+", re.I)
_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ManagedAppError(Exception):
    pass


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "managed-app"


def verbs_for(methods):
    """The table verbs a generated table service's methods need, in allowed-actions order."""
    found = {verb for m in methods for rx, verb in _VERB_OF if re.match(rx, m, re.I)}
    return [v for v in TABLE_VERBS if v in found]


# ── specs ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _int(spec, key, default, low, high):
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ManagedAppError(f"{key} must be a whole number from {low} to {high}")
    return value


def _site(spec):
    site = str(spec.get("site") or "").rstrip("/")
    if not _SITE.fullmatch(site):
        raise ManagedAppError("site must be a SharePoint site URL: https://<tenant>.sharepoint.com/sites/<site>")
    return site


def _check_media(spec):
    site = _site(spec)
    folder = "/" + str(spec.get("folder") or "").strip().strip("/")
    parts = folder.split("/")[1:]
    if not parts or parts == [""] or any(p in ("", ".", "..") for p in parts) or re.search(r"[\\:*?\"<>|#]", folder):
        raise ManagedAppError("folder must be a path inside the site's libraries, e.g. /Shared Documents/Films")
    media = spec.get("media") or ["video"]
    if not isinstance(media, list) or not media or any(not isinstance(m, str) or m not in MEDIA for m in media):
        raise ManagedAppError(f"media must list some of {', '.join(MEDIA)}")
    max_bytes = spec.get("maxBytes") or DEFAULT_MAX_BYTES
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ManagedAppError("maxBytes must be positive")
    media = sorted(set(media))
    return {"site": site, "folder": folder, "media": media, "maxBytes": max_bytes,
            "description": f"Plays {', '.join(media)} from a SharePoint folder."}


def _check_people(spec):
    return {"searchTop": _int(spec, "searchTop", 12, 1, 100),
            "description": "You, your manager and your direct reports, and a directory search."}


def _check_calendar(spec):
    calendar = str(spec.get("calendar") or "Calendar").strip()
    if not calendar or len(calendar) > 100:
        raise ManagedAppError("calendar must be a calendar's name (at most 100 characters)")
    days = _int(spec, "days", 14, 1, 31)
    return {"days": days, "calendar": calendar, "description": f"The next {days} days of your calendar."}


def _check_list(spec):
    site = _site(spec)
    name = spec.get("list")
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 255 \
            or re.search(r"[\x00-\x1f]", name):
        raise ManagedAppError("list must be the SharePoint list's name, as the site shows it")
    columns = spec.get("columns") or []
    if not isinstance(columns, list) or any(not isinstance(c, str) or not _COLUMN.fullmatch(c) for c in columns):
        raise ManagedAppError("columns must list column internal names, e.g. [\"Title\", \"Status\"]")
    group = spec.get("groupBy") or ""
    if group and (not isinstance(group, str) or not _COLUMN.fullmatch(group)):
        raise ManagedAppError("groupBy must be a column internal name")
    return {"site": site, "list": name, "columns": list(dict.fromkeys(columns)), "groupBy": group,
            "top": _int(spec, "top", 500, 1, 5000), "description": f"The SharePoint list {name}, searchable."}


def _check_tasks(spec):
    table = spec.get("table")
    m = re.fullmatch(r"([a-z][a-z0-9]{1,7})_([a-z0-9][a-z0-9_]*)", table) if isinstance(table, str) else None
    if not m or len(table) > 50:
        raise ManagedAppError("table must be a Dataverse table's logical name, <publisher prefix>_<name>, "
                              "e.g. cr123_task")
    entity_set = spec.get("entitySet") or f"{table}s"
    if not isinstance(entity_set, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", entity_set):
        raise ManagedAppError("entitySet must be the table's entity set name, e.g. cr123_tasks")
    url = str(spec.get("dataverseUrl") or "").rstrip("/")
    if url and not re.fullmatch(r"https://[a-z0-9-]+(\.[a-z0-9-]+)*\.dynamics\.(com|us|cn)", url, re.I):
        raise ManagedAppError("dataverseUrl must be the environment URL, e.g. https://contoso.crm.dynamics.com")
    label = str(spec.get("tableLabel") or m.group(2).replace("_", " ").title()).strip()[:100]
    prefix = m.group(1)
    return {"table": table, "entitySet": entity_set, "tableLabel": label, "dataverseUrl": url,
            "fields": {"id": f"{table}id", "name": f"{prefix}_name", "done": f"{prefix}_done", "due": f"{prefix}_due",
                       "notes": f"{prefix}_notes"},
            "description": f"Tasks in the Dataverse table {table}."}


# ── kinds: what each template needs ─────────────────────────────────────────────────────────────────────────────
# A data source: the alias the app's code calls; the connector and how it binds (action, or a table in a dataset);
# the generated service it is predicted to be (file stem, class, named or default export); and what the code calls.

def _action(alias, connector, predicted, calls):
    return {"alias": alias, "connector": connector, "as": "action", "predict": (predicted, predicted, "named"),
            "calls": tuple(sorted(calls))}


def _media_sources(s):
    return [_action("SharePointService", SHAREPOINT, "SharePointService", SHAREPOINT_MEDIA_ACTIONS)]


def _people_sources(s):
    return [_action("UsersService", USERS, "Office365UsersService", PEOPLE_ACTIONS)]


def _calendar_sources(s):
    return [_action("OutlookService", OUTLOOK, "Office365OutlookService", CALENDAR_ACTIONS)]


def _list_sources(s):
    stem = re.sub(r"[^A-Za-z0-9]", "", s["list"]) + "Service"
    return [{"alias": "ListService", "connector": SHAREPOINT, "as": "table", "dataset": s["site"], "table": s["list"],
             "predict": (stem, stem, "named"), "calls": LIST_METHODS}]


def _task_sources(s):
    stem = s["entitySet"][:1].upper() + s["entitySet"][1:] + "Service"
    return [{"alias": "TaskService", "connector": DATAVERSE, "as": "table", "table": s["table"], "dataverse": True,
             "predict": (stem, stem, "default"), "calls": TASK_METHODS}]


KINDS = {
    "sharepoint-media": {
        "check": _check_media, "sources": _media_sources, "files": ("App.tsx", "media.ts"),
        "config": lambda s: {"title": s["title"], "site": s["site"], "folder": s["folder"], "media": s["media"],
                             "maxBytes": s["maxBytes"],
                             "mime": {e: MIME[e] for k in s["media"] for e in MEDIA[k]}},
        "config_type": "{ title: string; site: string; folder: string; media: string[]; maxBytes: number; "
                       "mime: Record<string, string> }",
        "summary": lambda s: f"plays {', '.join(s['media'])} from the SharePoint folder {s['folder']}"},
    "people-directory": {
        "check": _check_people, "sources": _people_sources, "files": ("App.tsx",),
        "config": lambda s: {"title": s["title"], "searchTop": s["searchTop"]},
        "config_type": "{ title: string; searchTop: number }",
        "summary": lambda s: "shows you, your manager and your direct reports, and searches the directory, with "
                             "profile photos"},
    "calendar-dashboard": {
        "check": _check_calendar, "sources": _calendar_sources, "files": ("App.tsx", "calendar.ts"),
        "config": lambda s: {"title": s["title"], "days": s["days"], "calendar": s["calendar"]},
        "config_type": "{ title: string; days: number; calendar: string }",
        "summary": lambda s: f"shows the next {s['days']} days of the Outlook calendar {s['calendar']}"},
    "sharepoint-list": {
        "check": _check_list, "sources": _list_sources, "files": ("App.tsx", "rows.ts"),
        "config": lambda s: {"title": s["title"], "site": s["site"], "list": s["list"], "columns": s["columns"],
                             "groupBy": s["groupBy"], "top": s["top"]},
        "config_type": "{ title: string; site: string; list: string; columns: string[]; groupBy: string; "
                       "top: number }",
        "summary": lambda s: f"shows the SharePoint list {s['list']} as a searchable table, read-only"},
    "task-tracker": {
        "check": _check_tasks, "sources": _task_sources, "files": ("App.tsx",),
        "config": lambda s: {"title": s["title"], "table": s["table"], "fields": s["fields"]},
        "config_type": "{ title: string; table: string; fields: { id: string; name: string; done: string; "
                       "due: string; notes: string } }",
        "summary": lambda s: f"tracks tasks in the Dataverse table {s['table']}: add, complete, delete"},
}


def check_spec(spec):
    """Normalized spec, or ManagedAppError naming what is wrong."""
    if not isinstance(spec, dict):
        raise ManagedAppError("the spec must be a JSON object")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ManagedAppError(f"unknown kind {kind!r}; known: {', '.join(KINDS)}")
    name = str(spec.get("name") or "").strip()
    if not name or len(name) > 60:
        raise ManagedAppError("name is required (at most 60 characters)")
    checked = KINDS[kind]["check"](spec)
    description = str(spec.get("description") or checked.pop("description"))[:300]
    checked.pop("description", None)
    return {"kind": kind, "name": name, "title": str(spec.get("title") or name), **checked, "description": description}


def sources_for(spec):
    s = check_spec(spec)
    return KINDS[s["kind"]]["sources"](s)


# ── the project ─────────────────────────────────────────────────────────────────────────────────────────────────

def _package_json(name):
    deps = ["@microsoft/managed-apps", "react", "react-dom"]
    dev = ["@microsoft/managed-apps-vite-plugin", "@types/node", "@types/react", "@types/react-dom",
           "@vitejs/plugin-react", "typescript", "vite"]
    return {"name": slug(name), "private": True, "version": "0.0.0", "type": "module",
            "scripts": {"dev": "vite", "build": "tsc -b && vite build", "preview": "vite preview"},
            "dependencies": {d: f"^{VERSIONS[d]}" for d in deps},
            "devDependencies": {d: f"^{VERSIONS[d]}" if d != "typescript" else f"~{VERSIONS[d]}" for d in dev}}


_TS_COMMON = {"target": "es2023", "module": "esnext", "skipLibCheck": True, "moduleResolution": "bundler",
              "allowImportingTsExtensions": True, "verbatimModuleSyntax": True, "moduleDetection": "force",
              "noEmit": True, "noUnusedLocals": True, "noUnusedParameters": True, "erasableSyntaxOnly": True,
              "noFallthroughCasesInSwitch": True}

VITE_CONFIG = """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { managedApps } from '@microsoft/managed-apps-vite-plugin'

export default defineConfig({
  plugins: [react(), managedApps()],
})
"""

MAIN_TSX = """import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
"""

GITIGNORE = "node_modules\ndist\ndist-ssr\n*.local\n.DS_Store\n.ms/packed/\n"


def _template(*parts):
    return (TEMPLATES.joinpath(*parts)).read_text(encoding="utf-8")


def config_ts(spec):
    s = check_spec(spec)
    kind = KINDS[s["kind"]]
    body = json.dumps(kind["config"](s), indent=2, ensure_ascii=False)
    return ("// The app's settings, written by brainfreeze-studio from managed-app.spec.json: change the spec and "
            "rebuild,\n// not this file.\n"
            f"export const CONFIG: {kind['config_type']} = {body}\n")


def bound_ts(sources, resolved=None):
    """src/bound.ts: the generated services the app calls, under the names its code uses."""
    lines = ["// The generated connector services this app calls, under the names its code uses. brainfreeze-studio",
             "// writes this file: predicted when the project is written, then read from generated/services (each",
             "// service's dataSourceName) after `ms app add data-source`, so the app follows what the CLI generated.",
             ""]
    for src in sources:
        stem, cls, style = (resolved or {}).get(src["alias"]) or src["predict"]
        if style == "default":
            lines.append(f"export {{ default as {src['alias']} }} from '../generated/services/{stem}'")
        elif cls == src["alias"]:
            lines.append(f"export {{ {cls} }} from '../generated/services/{stem}'")
        else:
            lines.append(f"export {{ {cls} as {src['alias']} }} from '../generated/services/{stem}'")
    return "\n".join(lines) + "\n"


def _least_privilege(src, methods):
    return list(methods) if src["as"] == "action" else verbs_for(methods)


def files_for(spec):
    """{relative path: text} of the managed-app project for a checked spec."""
    s = check_spec(spec)
    kind = KINDS[s["kind"]]
    sources = kind["sources"](s)
    tsconfig_app = {"compilerOptions": {"tsBuildInfoFile": "./node_modules/.tmp/tsconfig.app.tsbuildinfo",
                                        "lib": ["ES2023", "DOM"], "types": ["vite/client"], "jsx": "react-jsx",
                                        **_TS_COMMON}, "include": ["src"]}
    tsconfig_node = {"compilerOptions": {"tsBuildInfoFile": "./node_modules/.tmp/tsconfig.node.tsbuildinfo",
                                         "lib": ["ES2023"], "types": ["node"], **_TS_COMMON},
                     "include": ["vite.config.ts"]}
    uses = "\n".join(f"- {src['connector']} ({src['as']}{': ' + src['table'] if src.get('table') else ''}): "
                     f"{', '.join(_least_privilege(src, src['calls']))}" for src in sources)
    files = {
        "package.json": json.dumps(_package_json(s["name"]), indent=2) + "\n",
        "vite.config.ts": VITE_CONFIG,
        "tsconfig.json": json.dumps({"files": [], "references": [{"path": "./tsconfig.app.json"},
                                                                 {"path": "./tsconfig.node.json"}]}, indent=2) + "\n",
        "tsconfig.app.json": json.dumps(tsconfig_app, indent=2) + "\n",
        "tsconfig.node.json": json.dumps(tsconfig_node, indent=2) + "\n",
        ".gitignore": GITIGNORE,
        "src/main.tsx": MAIN_TSX,
        "src/index.css": _template("common", "index.css"),
        "src/App.css": _template("common", "App.css"),
        "src/connector.ts": _template("common", "connector.ts"),
        "src/config.ts": config_ts(s),
        "src/bound.ts": bound_ts(sources),
        "README.md": (f"# {s['title']}\n\nA managed app (Microsoft Copilot Managed Runtime) generated by "
                      f"brainfreeze-studio (kind `{s['kind']}`): it {kind['summary'](s)}.\n\n"
                      f"Connectors, and what it calls on them (its least privilege):\n\n{uses}\n\n"
                      "Run it locally with `ms app dev`; deploy with `ms app deploy` from a pushed commit.\n"),
    }
    for name in kind["files"]:
        files[f"src/{name}"] = _template(s["kind"], name)
    title = html.escape(s["title"])
    files["index.html"] = ("<!doctype html>\n<html lang=\"en\">\n  <head>\n    <meta charset=\"UTF-8\" />\n"
                           "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
                           f"    <meta name=\"brainfreeze-build\" content=\"{build_id(files)}\" />\n"
                           f"    <title>{title}</title>\n  </head>\n  <body>\n    <div id=\"root\"></div>\n"
                           "    <script type=\"module\" src=\"/src/main.tsx\"></script>\n  </body>\n</html>\n")
    return files


def build_id(files):
    """What a build is, as 16 hex digits: its spec, versions and templates (every file but index.html, which carries
    the id, and src/bound.ts, which follows what the CLI generated). The app's page carries it as
    <meta name="brainfreeze-build">, so a test or an agent can confirm the player runs this build and not a cached one."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        if rel not in ("index.html", "src/bound.ts"):
            digest.update(rel.encode("utf-8") + b"\0" + files[rel].encode("utf-8") + b"\0")
    return digest.hexdigest()[:16]


def calls_in(app_dir, service="SharePointService"):
    """Every `<service>.<Method>(` the app's src/ calls: what it needs (least privilege)."""
    found = set()
    for path in sorted(Path(app_dir, "src").rglob("*")):
        if path.suffix in (".ts", ".tsx", ".js", ".jsx"):
            found.update(re.findall(rf"\b{service}\.([A-Za-z0-9_]+)\s*\(", path.read_text(encoding="utf-8")))
    return sorted(found)


def _next_steps(s, sources):
    steps = ["ms app init --display-name <name> --repo native [--environment-id <id>]"]
    for src in sources:
        steps.append(" ".join(_bind_args(src, "<environment id>")))
    return steps + ["npm install && npm run build", "git commit; copilot-harness-sdk managed-apps push; deploy"]


def scaffold(spec, out_dir):
    """Write out_dir/managed-app/ (the project) and its report.json. Offline and deterministic."""
    s = check_spec(spec)
    sources = KINDS[s["kind"]]["sources"](s)
    out = Path(out_dir).expanduser() / "managed-app"
    keep = {"node_modules", "generated", ".git", "ms.config.json", ".ms"}   # what `ms` and npm own
    if out.exists():
        for child in out.iterdir():
            if child.name in keep:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    files = files_for(s)
    for rel, text in files.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    data_sources = []
    for src in sources:
        methods = calls_in(out, src["alias"])
        if tuple(methods) != src["calls"]:
            raise ManagedAppError(f"the {s['kind']} app calls {methods} on {src['alias']}, not its declared "
                                  f"{list(src['calls'])}")
        entry = {"connector": src["connector"], "as": src["as"], "service": src["alias"]}
        if src["as"] == "table":
            entry.update({k: src[k] for k in ("dataset", "table") if src.get(k)})
            entry["calls"] = methods
        entry["allowedActions"] = _least_privilege(src, methods)
        data_sources.append(entry)
    extra = {k: s[k] for k in ("site", "folder", "media", "maxBytes", "list", "table", "days", "calendar") if k in s}
    report = {"app": s["name"], "kind": s["kind"], "build": build_id(files), "summary": KINDS[s["kind"]]["summary"](s),
              **extra,
              "dataSources": data_sources, "player": {"mediaSrc": "'self' data:", "binaryAs": "data: URLs"},
              "versions": VERSIONS, "next": _next_steps(s, sources)}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "managed-app.spec.json").write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    return {"dir": out, "spec": s, "report": report}


# ── binding: ms.config.json and generated/ after `ms app add data-source` ───────────────────────────────────────

def _refs_for(config, connector):
    api = f"/apis/shared_{connector}"
    return [(k, r) for k, r in (config.get("connectionReferences") or {}).items() if str(r.get("id", "")).endswith(api)]


def _same_site(a, b):
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def table_key(config, src):
    """(reference, dataset, data source key) of a bound table data source, or None."""
    for name, ref in _refs_for(config, src["connector"]):
        for dataset, block in (ref.get("dataSets") or {}).items():
            if src.get("dataset") and not _same_site(dataset, src["dataset"]):
                continue
            for key, table in (block.get("dataSources") or {}).items():
                if key == src["table"] or table.get("tableName") == src["table"]:
                    return name, dataset, key
    return None


def is_bound(config, src):
    if src["as"] == "table":
        return table_key(config, src) is not None
    return any(src["connector"] in (r.get("dataSources") or []) for _, r in _refs_for(config, src["connector"]))


def _bind_args(src, environment_id):
    args = ["ms", "app", "add", "data-source", "--connector", src["connector"], "--as", src["as"]]
    if src.get("dataset"):
        args += ["--dataset", src["dataset"]]
    if src["as"] == "table":
        args += ["--table", src["table"]]
    if src.get("dataverse"):
        args += ["--dataverse-environment-id", environment_id]
    return args + ["--use-sso"]


def generated_services(app_dir):
    """{dataSourceName: (file stem, class, 'named' | 'default')} for generated/services/*.ts."""
    found = {}
    for path in sorted(Path(app_dir, "generated", "services").glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        key = re.search(r"\bdataSourceName\s*=\s*'([^']+)'", text)
        cls = re.search(r"^export\s+(default\s+)?class\s+([A-Za-z0-9_$]+)", text, re.M)
        if key and cls:
            found[key.group(1)] = (path.stem, cls.group(2), "default" if cls.group(1) else "named")
    return found


def resolve_services(app_dir, sources, config):
    """{alias: (stem, class, style)} of the generated service behind each data source, or ManagedAppError."""
    services = generated_services(app_dir)
    resolved = {}
    for src in sources:
        key = src["connector"] if src["as"] == "action" else (table_key(config, src) or (None, None, None))[2]
        if key not in services:
            raise ManagedAppError(f"no generated service for {src['connector']} "
                                  f"{src.get('table') or ''} (dataSourceName {key!r}) under generated/services")
        resolved[src["alias"]] = services[key]
    return resolved


# ── Dataverse: the task tracker's table (the add-dataverse skill's table-management reference) ─────────────────

def _label(text):
    return {"@odata.type": "Microsoft.Dynamics.CRM.Label",
            "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": text,
                                 "LanguageCode": 1033}]}


def _az_token(resource):
    r = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o",
                        "tsv"], capture_output=True, text=True, env=_base_env())
    if r.returncode != 0 or not r.stdout.strip():
        raise ManagedAppError(f"az could not get a Dataverse token for {resource} (az login?): {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def _dataverse_caller(base, token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json",
               "OData-Version": "4.0", "OData-MaxVersion": "4.0"}

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    return call


def ensure_dataverse_table(org_url, spec, *, call=None, log=print):
    """Create the task tracker's table and its columns where missing, the way the add-dataverse skill's
    table-management reference does: the prefix is the CDS Default Publisher's, a table or column that exists is left
    alone. Returns {"created": bool, "added": [columns], "entitySet": name}."""
    s = check_spec(spec)
    if s["kind"] != "task-tracker":
        raise ManagedAppError("only a task-tracker has a table to create")
    call = call or _dataverse_caller(org_url.rstrip("/") + "/api/data/v9.2", _az_token(org_url.rstrip("/")))
    table, f = s["table"], s["fields"]
    prefix = table.split("_", 1)[0]
    status, pubs = call("GET", "/publishers?$filter=friendlyname%20eq%20%27CDS%20Default%20Publisher%27"
                               "&$select=customizationprefix")
    have = (((pubs if isinstance(pubs, dict) else {}).get("value") or [{}])[0]).get("customizationprefix")
    if status != 200 or not have:
        raise ManagedAppError(f"could not read the environment's default publisher ({status}): {str(pubs)[:300]}")
    if have != prefix:
        raise ManagedAppError(f"this environment's default publisher prefix is {have!r}: name the table "
                              f"{have}_{table.split('_', 1)[1]}")
    status, body = call("GET", f"/EntityDefinitions(LogicalName='{table}')?$select=LogicalName")
    created = False
    if status == 404:
        status, body = call("POST", "/EntityDefinitions", {
            "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata", "SchemaName": table,
            "DisplayName": _label(s["tableLabel"]), "DisplayCollectionName": _label(s["tableLabel"] + "s"),
            "Description": _label(f"Tasks for the {s['name']} managed app (brainfreeze-studio task-tracker)."),
            "OwnershipType": "UserOwned", "HasNotes": False, "HasActivities": False,
            "PrimaryNameAttribute": f["name"],
            "Attributes": [{"@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata", "SchemaName": f["name"],
                            "AttributeType": "String", "FormatName": {"Value": "Text"}, "MaxLength": 200,
                            "DisplayName": _label("Name"), "IsPrimaryName": True}]})
        if status not in (200, 201, 204):
            raise ManagedAppError(f"creating the table {table} failed ({status}): {str(body)[:400]}")
        created = True
        log(f"created the Dataverse table {table}")
    elif status != 200:
        raise ManagedAppError(f"reading the table {table} failed ({status}): {str(body)[:400]}")
    columns = [(f["done"], "Done", {"@odata.type": "Microsoft.Dynamics.CRM.BooleanAttributeMetadata",
                                    "AttributeType": "Boolean",
                                    "OptionSet": {"TrueOption": {"Value": 1, "Label": _label("Yes")},
                                                  "FalseOption": {"Value": 0, "Label": _label("No")}}}),
               (f["due"], "Due", {"@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
                                  "AttributeType": "DateTime", "Format": "DateAndTime"}),
               (f["notes"], "Notes", {"@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
                                      "AttributeType": "Memo", "MaxLength": 2000})]
    added = []
    for schema, display, shape in columns:
        status, body = call("GET", f"/EntityDefinitions(LogicalName='{table}')/Attributes(LogicalName='{schema}')"
                                   "?$select=LogicalName")
        if status == 404:
            status, body = call("POST", f"/EntityDefinitions(LogicalName='{table}')/Attributes",
                                {**shape, "SchemaName": schema, "DisplayName": _label(display)})
            if status not in (200, 201, 204):
                raise ManagedAppError(f"adding the column {schema} failed ({status}): {str(body)[:400]}")
            added.append(schema)
            log(f"added the column {schema}")
        elif status != 200:
            raise ManagedAppError(f"reading the column {schema} failed ({status}): {str(body)[:400]}")
    status, meta = call("GET", f"/EntityDefinitions(LogicalName='{table}')?$select=EntitySetName,LogicalName")
    entity_set = meta.get("EntitySetName") if status == 200 and isinstance(meta, dict) else None
    if entity_set and entity_set != s["entitySet"]:
        raise ManagedAppError(f"the table's entity set is {entity_set!r}, not {s['entitySet']!r}: put "
                              f"\"entitySet\": \"{entity_set}\" in the spec")
    return {"created": created, "added": added, "entitySet": entity_set}


# ── lifecycle: the managed-apps skills, driven through ms and copilot-harness-sdk ─────────────────────────────────

def _base_env():
    """os.environ, with Git Credential Manager on PATH when it was installed as a .NET tool (its documented
    `dotnet tool install -g git-credential-manager` route puts it in ~/.dotnet/tools, often not on PATH)."""
    env = dict(os.environ)
    tools = Path.home() / ".dotnet" / "tools"
    if not shutil.which("git-credential-manager") and (tools / "git-credential-manager").exists():
        env["PATH"] = f"{tools}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run(cmd, cwd, log, env=None, check=True):
    log(f"$ {' '.join(cmd[:4])}{' …' if len(cmd) > 4 else ''}")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env={**_base_env(), **(env or {})})
    if check and r.returncode != 0:
        raise ManagedAppError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({r.returncode}): "
                              f"{(r.stderr or r.stdout).strip()[-800:]}")
    return r


def _ms(args, cwd, log, want_json=True):
    r = _run(["ms", *args, "--non-interactive", "--json"], cwd, log, check=False,
             env={"MS_CLI_ORIGIN": os.environ.get("MS_CLI_ORIGIN", "sdk/brainfreeze-studio")})
    out = r.stdout or ""
    try:
        body = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        # some verbs (add data-source) print text even with --json; a clean exit is their success
        if r.returncode == 0 and not want_json:
            return {"text": out.strip()}
        raise ManagedAppError(f"ms {' '.join(args)} returned no JSON ({r.returncode}): {(out + r.stderr)[-600:]}")
    if body.get("success") is False:
        raise ManagedAppError(f"ms {' '.join(args)}: {body.get('errorMessage') or body}")
    return body


def _config(app):
    return json.loads((app / "ms.config.json").read_text(encoding="utf-8"))


def _shared(ref):
    return bool(str(ref.get("sharedConnectionId") or "").strip())


def policy_targets(config, sources, calls):
    """The shared-connection policies an app needs (allowed-actions.md): for each data source on a shared reference, the
    reference that holds it and what to allow there. `calls(alias)` lists the methods the app's code calls on a service.
    Non-shared references get nothing: the rule is not to add allowedActions to them."""
    targets = []
    for src in sources:
        if src["as"] == "table":
            found = table_key(config, src)
            if not found:
                raise ManagedAppError(f"no {src['connector']} table {src['table']} in ms.config.json")
            name, dataset, key = found
            if _shared(config["connectionReferences"][name]):
                targets.append({"command": "allow-table", "connector": src["connector"], "reference": name,
                                "dataset": dataset, "table": key, "allow": verbs_for(calls(src["alias"]))})
            continue
        owners = [(k, r) for k, r in _refs_for(config, src["connector"]) if src["connector"] in (r.get("dataSources") or [])]
        if not owners:
            raise ManagedAppError(f"no {src['connector']} action data source in ms.config.json")
        for name, ref in owners:
            if _shared(ref):
                targets.append({"command": "allow", "connector": src["connector"], "reference": name,
                                "allow": list(calls(src["alias"]))})
    return targets


def lifecycle(app_dir, *, display_name, sdk_dir, tenant_id, environment_id=None, login_hint=None, deploy=True,
              git_cache=None, create_table=False, dataverse_url=None, log=print):
    """Register, bind, build, push and deploy a scaffolded managed app as the signed-in user (`ms auth login`).
    Returns {"appId", "commit", "playUrl"}. Needs node, npm, git, the ms CLI, Git Credential Manager (the CLI checks
    it is installed for platform repositories) and a copilot-harness-sdk checkout (sdk_dir). The first push signs in
    in a browser; `git_cache` (an MSAL cache file) makes later pushes silent. For a task-tracker, create_table makes
    its Dataverse table where missing (with az, at dataverse_url or the spec's dataverseUrl)."""
    # every path absolute: the commands below run in the app's directory, so a relative one would point elsewhere
    app = Path(app_dir).expanduser().resolve()
    s = check_spec(json.loads((app / "managed-app.spec.json").read_text(encoding="utf-8")))
    sources = KINDS[s["kind"]]["sources"](s)
    sdk_script = Path(sdk_dir).expanduser().resolve() / "scripts" / "managed-apps.mjs"
    git_cache = Path(git_cache).expanduser().resolve() if git_cache else None
    if not sdk_script.is_file():
        raise ManagedAppError(f"no copilot-harness-sdk managed-apps script at {sdk_script}")
    if create_table:
        url = dataverse_url or s.get("dataverseUrl")
        if s["kind"] != "task-tracker" or not url:
            raise ManagedAppError("create_table is for a task-tracker, with its environment URL (dataverseUrl)")
        ensure_dataverse_table(url, s, log=log)
    if not (app / ".git").exists():
        _run(["git", "init", "-q", "-b", "main"], app, log)
    # the CLI requires Git Credential Manager as the repository's credential helper for a platform repository; set it
    # for this repository only (pushes go through copilot-harness-sdk with a token, never GCM's interactive flow)
    helpers = _run(["git", "config", "--get-all", "credential.helper"], app, log, check=False).stdout.split()
    if "manager" not in helpers:
        _run(["git", "config", "--local", "credential.helper", ""], app, log)
        _run(["git", "config", "--local", "--add", "credential.helper", "manager"], app, log)
    if not (app / "ms.config.json").exists():
        args = ["app", "init", "--display-name", display_name, "--repo", "native"]
        if environment_id:
            args += ["--environment-id", environment_id]
        init = _ms(args, app, log)
        log(f"registered: {init.get('appId')}")
    for src in sources:
        config = _config(app)
        if not is_bound(config, src):
            env = config.get("environmentId") or environment_id
            if src.get("dataverse") and not env:
                raise ManagedAppError("binding a Dataverse table needs the app's environment id (ms.config.json has none)")
            _ms(_bind_args(src, env or "")[1:], app, log, want_json=False)
            if not is_bound(_config(app), src):
                raise ManagedAppError(f"ms app add data-source ran, but ms.config.json has no {src['connector']} "
                                      f"{src.get('table') or ''} data source")
    config = _config(app)
    bound = bound_ts(sources, resolve_services(app, sources, config))
    if (app / "src" / "bound.ts").read_text(encoding="utf-8") != bound:
        log("src/bound.ts: following the services the CLI generated")
        (app / "src" / "bound.ts").write_text(bound, encoding="utf-8")
    for t in policy_targets(config, sources, lambda alias: calls_in(app, alias)):
        cmd = ["node", str(sdk_script), t["command"], str(app), t["connector"]]
        if t["command"] == "allow-table":
            cmd += [t["table"], ",".join(t["allow"]), "--dataset", t["dataset"]]
        else:
            cmd += [",".join(t["allow"])]
        _run(cmd + ["--reference", t["reference"]], app, log)
    _run(["node", str(sdk_script), "check", str(app)], app, log)
    _run(["npm", "install", "--no-audit", "--no-fund"], app, log)
    _run(["npm", "run", "build"], app, log)
    _run(["git", "add", "-A"], app, log)
    if _run(["git", "status", "--porcelain"], app, log).stdout.strip():
        _run(["git", "commit", "-q", "-m", f"{display_name}: generated by brainfreeze-studio"], app, log)
    result = {"appId": _config(app).get("appId")}
    if not deploy:
        return result
    push = ["node", str(sdk_script), "push", str(app), "--tenant", tenant_id]
    if login_hint:
        push += ["--login-hint", login_hint]
    if git_cache:
        push += ["--cache", str(git_cache)]
    _run(push, app, log)
    body = json.loads(_run(["node", str(sdk_script), "deploy", str(app)], app, log).stdout)
    result.update(commit=body.get("commitHash"), playUrl=body.get("appPlayUri"))
    return result
