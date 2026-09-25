"""Rapplications: a RAPP agent with its front end, as the RAPP Store ships them, into a rapp/1 rapplication egg.

    from brainfreeze_studio import rapplication
    r = rapplication.load("@kody-w/agent_team")          # a store ref, a bundle folder or zip, or a .egg
    egg = rapplication.pack(r)                           # rapp/1 rapplication egg: rappid.json, agent.py, ui.html
    build(egg_path, "out", r.name, "rapp")                # the brainstem build takes it as it is

A RAPP Store bundle is `manifest.json` (rapp-application/1.0), `singleton/<id>_agent.py` and `ui/index.html`. RAPP/1
names the same unit a `rapplication` egg (SPEC §9: rappid.json, exactly one root agent.py, optionally one ui.html).
The egg is the unit brainfreeze-studio builds from: its agent.py goes through the same build as a brainstem's agents,
and its ui.html becomes a Power Apps code app (brainfreeze_studio.codeapp).

Nothing here runs the rapplication's code. Files fetched from the store are checked against the catalog's SHA-256
when the catalog lists one, and the result records what was checked.
"""
import hashlib
import io
import json
import re
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import rapp1

STORE = "https://raw.githubusercontent.com/kody-w/RAPP_Store/main/"
MAX_FILE = 4 * 1024 * 1024
_REF = re.compile(r"^(?:@(?P<publisher>[A-Za-z0-9][A-Za-z0-9-]*)/)?(?P<id>[a-z][a-z0-9_-]*)$")
BASIC_AGENT = (Path(__file__).with_name("basic_agent.py")).read_bytes()


class RapplicationError(ValueError):
    pass


@dataclass
class Rapplication:
    id: str
    name: str
    version: str
    publisher: str
    summary: str
    agent_filename: str
    agent: bytes
    ui: bytes = None
    tagline: str = ""
    category: str = ""
    manifest: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    egg: dict = None            # when loaded from an egg: its rappid, address and created_utc

    @property
    def slug(self):
        return re.sub(r"[^a-z0-9]+", "-", self.id.lower()).strip("-")

    @property
    def owner(self):
        return re.sub(r"[^a-z0-9]+", "-", self.publisher.lstrip("@").lower()).strip("-") or "rapp"


def _get(url, limit=MAX_FILE):
    if url.startswith("file://"):
        return Path(url[7:]).read_bytes()
    if not url.startswith("https://"):
        raise RapplicationError(f"only https URLs are fetched, not {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RapplicationError(f"{url} is larger than {limit // 2**20} MB")
    return data


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _checked(data, expected, what, checks):
    digest = _sha(data)
    if expected:
        if digest != expected:
            raise RapplicationError(f"{what} does not match the catalog's sha256 ({expected[:12]}…, got {digest[:12]}…)")
        checks[what] = "sha256 matches the catalog"
    else:
        checks[what] = "the catalog lists no sha256 for it"
    return digest


def _from_manifest(manifest, agent, ui, source, agent_filename=None):
    for key in ("id", "name", "version"):
        if not manifest.get(key):
            raise RapplicationError(f"manifest.json has no {key}")
    return Rapplication(id=manifest["id"], name=manifest["name"], version=str(manifest["version"]),
                        publisher=manifest.get("publisher") or "@rapp", summary=manifest.get("summary") or "",
                        tagline=manifest.get("tagline") or "", category=manifest.get("category") or "",
                        agent_filename=agent_filename or manifest.get("singleton_filename")
                        or Path(manifest.get("agent") or f"{manifest['id']}_agent.py").name,
                        agent=agent, ui=ui, manifest=manifest, source=source)


def _bundle_files(files, where):
    """{relative path: bytes} of one bundle (tolerating one wrapper folder) → Rapplication."""
    roots = {p.split("/", 1)[0] for p in files if "/" in p}
    if "manifest.json" not in files and len(roots) == 1:
        root = roots.pop() + "/"
        files = {p[len(root):]: v for p, v in files.items() if p.startswith(root)}
    if "manifest.json" not in files:
        raise RapplicationError(f"{where}: no manifest.json (a RAPP Store bundle has manifest.json, singleton/, ui/)")
    manifest = json.loads(files["manifest.json"])
    agent_rel = manifest.get("agent") or f"singleton/{manifest.get('singleton_filename') or manifest.get('id', '') + '_agent.py'}"
    if agent_rel not in files:
        found = [p for p in files if p.startswith("singleton/") and p.endswith("_agent.py")]
        if len(found) != 1:
            raise RapplicationError(f"{where}: can't find the singleton agent ({agent_rel})")
        agent_rel = found[0]
    ui_rel = manifest.get("ui") or "ui/index.html"
    ui = files.get(ui_rel) or files.get("ui/index.html")
    return _from_manifest(manifest, files[agent_rel], ui,
                          {"kind": "bundle", "where": str(where), "agent_sha256": _sha(files[agent_rel]),
                           "ui_sha256": _sha(ui) if ui else None}, Path(agent_rel).name)


def _catalog(store):
    blob = Path(store, "index.json").read_bytes() if Path(store).is_dir() else _get(store.rstrip("/") + "/index.json")
    return json.loads(blob)


def _store_path(url):
    """apps/@publisher/id/... from a catalog URL (raw.githubusercontent.com/<owner>/<repo>/<ref>/apps/...)."""
    m = re.search(r"/(apps/@[^/]+/[^/]+/.+)$", url or "")
    return m.group(1) if m else None


def from_store(ref, store=STORE):
    """A catalog entry by `@publisher/id` or `id`. `store` is the catalog root: an https URL (default: RAPP_Store main
    on GitHub) or a local checkout. Files come from the same root, checked against the catalog's sha256."""
    m = _REF.match(ref.strip())
    if not m:
        raise RapplicationError(f"not a store reference: {ref!r} (use @publisher/id or id)")
    entries = [e for e in _catalog(store).get("rapplications", []) if e.get("id") == m.group("id")]
    if m.group("publisher"):
        entries = [e for e in entries if (e.get("publisher") or "").lstrip("@").lower() == m.group("publisher").lower()]
    if len(entries) != 1:
        raise RapplicationError(f"{ref}: {'no' if not entries else len(entries)} catalog entries match")
    entry = entries[0]
    if entry.get("access") == "private":
        raise RapplicationError(f"{ref} is a gated rapplication (its files are in a private repo)")
    if not entry.get("singleton_url"):
        raise RapplicationError(f"{ref} ships no single-file agent (a complete application: "
                                f"{entry.get('application_schema') or 'see its install notes'})")
    local = Path(store).is_dir()
    base = str(Path(store).resolve()) if local else store.rstrip("/")

    def fetch(url):
        rel = _store_path(url)
        if rel:
            return Path(base, rel).read_bytes() if local else _get(f"{base}/{rel}")
        return _get(url)            # a federated entry: its own repo, pinned to a commit in the URL

    checks = {}
    agent = fetch(entry["singleton_url"])
    agent_sha = _checked(agent, entry.get("singleton_sha256"), "agent", checks)
    ui = None
    if entry.get("ui_url"):
        ui = fetch(entry["ui_url"])
        _checked(ui, entry.get("ui_sha256"), "ui", checks)
    folder = (_store_path(entry["singleton_url"]) or "").split("/singleton/")[0] or None
    manifest = {}
    if folder:
        try:
            manifest = json.loads(Path(base, folder, "manifest.json").read_bytes() if local
                                  else _get(f"{base}/{folder}/manifest.json"))
        except Exception:  # noqa: BLE001 - the catalog entry carries the same fields
            manifest = {}
    manifest = {**{k: entry.get(k) for k in ("id", "name", "version", "publisher", "summary", "tagline", "category")
                   if entry.get(k) is not None}, **manifest}
    return _from_manifest(manifest, agent, ui,
                          {"kind": "store", "store": base if local else store, "ref": ref,
                           "folder": folder or entry["singleton_url"],
                           "agent_sha256": agent_sha, "ui_sha256": _sha(ui) if ui else None, "checks": checks},
                          entry.get("singleton_filename"))


def load(where, store=STORE):
    """A rapplication from a RAPP Store ref, a bundle folder, a bundle zip, or a rapp/1 rapplication egg."""
    p = Path(str(where)).expanduser()
    if p.is_dir():
        files = {str(f.relative_to(p)).replace("\\", "/"): f.read_bytes() for f in p.rglob("*") if f.is_file()}
        return _bundle_files(files, p)
    if p.is_file():
        blob = p.read_bytes()
        if p.suffix == ".egg":
            return from_egg(blob, str(p))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return _bundle_files({i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}, p)
    return from_store(str(where), store)


def from_egg(blob, where="egg"):
    ok, step, why = rapp1.verify_egg(blob)
    if not ok:
        raise RapplicationError(f"{where}: fails RAPP egg verification at {step}: {why}")
    manifest, files = rapp1.read_egg(blob)
    if manifest["variant"] != "rapplication":
        raise RapplicationError(f"{where}: expected a rapplication egg, got {manifest['variant']}")
    meta = (manifest.get("payload") or {}).get("rapplication") or {}
    parts = rapp1.rappid_parts(manifest["rappid"])
    meta = {"id": parts["slug"].replace("-", "_"), "name": parts["slug"], "version": "0.0.0",
            "publisher": "@" + parts["owner"], **meta}
    r = _from_manifest(meta, files["agent.py"], files.get("ui.html"), dict(meta.get("source") or {"kind": "egg"}),
                       meta.get("agent_filename"))
    r.egg = {"where": where, "rappid": manifest["rappid"], "address": rapp1.egg_address(manifest),
             "created_utc": manifest["created_utc"]}
    return r


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def pack(r, rappid=None, created_utc=None):
    """The rapplication as a rapp/1 rapplication egg (bytes). `rappid` is minted once (keyless) when not given."""
    rappid = rappid or (r.egg or {}).get("rappid") or rapp1.mint_rappid(r.owner, r.slug)
    created_utc = created_utc or (r.egg or {}).get("created_utc")
    files = {"rappid.json": rapp1.canonical({"schema": "rapp/1", "rappid": rappid}).encode(), "agent.py": r.agent}
    if r.ui:
        files["ui.html"] = r.ui
    payload = {"rapplication": {"id": r.id, "name": r.name, "version": r.version, "publisher": r.publisher,
                                "summary": r.summary, "tagline": r.tagline, "category": r.category,
                                "agent_filename": r.agent_filename,
                                "source": {k: v for k, v in r.source.items() if k in ("kind", "ref", "folder", "store",
                                                                                     "agent_sha256", "ui_sha256")}}}
    return rapp1.pack_egg("rapplication", rappid, created_utc or utc_now(), files=files, payload=payload)


def soul(meta):
    """The harness instructions for a rapplication's agent, from its manifest."""
    lines = [f"# {meta.get('name') or meta.get('id')}", ""]
    for key in ("summary", "tagline"):
        if meta.get(key):
            lines += [meta[key].strip(), ""]
    lines += [f"You are {meta.get('name') or meta.get('id')}, a RAPP rapplication ({meta.get('publisher', '@rapp')}/"
              f"{meta.get('id')} v{meta.get('version', '0.0.0')}) running in Copilot Studio. Its tool does the work: "
              "call it for anything it covers, pass the values the user gives, and show its output as it is.", ""]
    return "\n".join(lines)


def organism_files(manifest, files):
    """A rapplication egg's files laid out the way the brainstem build reads an organism egg: its agent under agents/,
    the grail's BasicAgent beside it, and a soul written from the rapplication's manifest."""
    meta = (manifest.get("payload") or {}).get("rapplication") or {}
    if not meta.get("id"):
        parts = rapp1.rappid_parts(manifest["rappid"])
        meta = {"id": parts["slug"].replace("-", "_"), "publisher": "@" + parts["owner"], **meta}
    agent_name = Path(meta.get("agent_filename") or f"{meta['id']}_agent.py").name
    if not agent_name.endswith("_agent.py"):
        agent_name = re.sub(r"\.py$", "", agent_name) + "_agent.py"
    out = {"rappid.json": files["rappid.json"], "soul.md": soul(meta).encode(),
           f"agents/{agent_name}": files["agent.py"], "agents/basic_agent.py": BASIC_AGENT}
    if "ui.html" in files:
        out["ui.html"] = files["ui.html"]
    return out, meta


# ── a whole rapplication: its agent, the flows its app calls, and the code app ───────────────────────────────────

def twins(workspace, schema_name, display_name):
    """Power Apps twins of the workspace's agent flows: the same actions behind the Power Apps trigger, which is the
    only trigger a code app can call. Their ids are fixed per agent and flow, so the app can name them before they
    exist. Returns [{"id", "name", "description", "definition", "of"}]."""
    from . import workflow_id_for
    from .codeapp import powerapps_twin
    from .deploy import read_workspace
    out = []
    for wf in read_workspace(workspace)["workflows"]:
        flow_name = re.sub(r"-[0-9a-f-]{36}$", "", wf["folder"], flags=re.I)
        out.append({"id": workflow_id_for(schema_name, f"{flow_name}PowerApps"),
                    "name": f"{display_name} {flow_name} (Power Apps)",
                    "description": f"The {flow_name} agent flow behind the Power Apps trigger, for the {display_name} "
                                   "code app: the same actions, so the same proven output.",
                    "definition": powerapps_twin(wf["definition"]), "of": {"id": wf["id"], "flow": flow_name}})
    return out


def app_tools(r, agents, twin_flows):
    """What the code app host needs per tool: the names a UI may call it by, and its Power Apps flow when it has one."""
    by_flow = {t["of"]["flow"]: t for t in twin_flows}
    tools = []
    for a in agents:
        stem = Path(a["file"]).name[:-len("_agent.py")] if a["file"].endswith("_agent.py") else Path(a["file"]).stem
        aliases = [x for x in dict.fromkeys([a.get("class"), r.id, stem, (a.get("class") or "").removesuffix("Agent")])
                   if x and x != a["name"]]
        twin = by_flow.get((a.get("flow") or {}).get("name"))
        tools.append({"name": a["name"], "aliases": aliases,
                      "flow": {"workflowId": twin["id"], "displayName": twin["name"]} if twin else None})
    return tools


def prepare(where, out_dir, *, name=None, publisher_prefix="rapp", schema_name=None, store=STORE, translations=None,
            sdk_dir=None, environment=None, rappid=None, created_utc=None, fetch_vendor=None, host_js=None,
            files_home=None, run_proofs=True):
    """Everything offline, nothing deployed: the rapplication egg, the Copilot Studio workspace built from it, Power
    Apps twins of the flows the build proved, and the code app. Writes out_dir/rapplication.json and returns it.
    A rappid minted by an earlier run in the same out_dir is kept, so re-running updates the same organism."""
    from . import build
    from . import codeapp
    r = load(where, store)
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / "rapplication.json"
    if not rappid and summary_path.is_file():
        before = json.loads(summary_path.read_text())
        if (before.get("rapp") or {}).get("id") == r.id and (before.get("rapp") or {}).get("publisher") == r.publisher:
            rappid = before.get("rappid")
    egg = pack(r, rappid, created_utc)
    manifest, _ = rapp1.read_egg(egg)
    egg_path = out / f"{r.slug}.egg"
    egg_path.write_bytes(egg)
    display = (name or r.name)[:42]
    built = build(str(egg_path), out, display, publisher_prefix, schema_name=schema_name, sdk_dir=sdk_dir,
                  environment=environment, translations=translations, files_home=files_home, run_proofs=run_proofs)
    schema = built["schema_name"]
    twin_flows = twins(built["workspace"], schema, display)
    tools = app_tools(r, built["agents"], twin_flows)
    chat = None
    if r.ui and codeapp.ui_calls_agent(r.ui) and any(not t["flow"] for t in tools):
        # tools without a flow of their own, and free-form chat, go to the agent through a broker flow
        from . import workflow_id_for
        chat = {"id": workflow_id_for(schema, "ChatBrokerPowerApps"), "name": f"{display} Chat (Power Apps)",
                "description": f"Lets the {display} code app talk to its Copilot Studio agent (a GitHub Copilot "
                               "harness agent, reached on the agentic runtime).",
                "definition": codeapp.chat_broker(schema, f"{schema}.shared_microsoftcopilotstudio"),
                "of": {"id": None, "flow": "ChatBroker"}}
        twin_flows.append(chat)
    flows_dir = out / "powerapps-flows"
    if flows_dir.exists():
        for f in flows_dir.glob("*.json"):
            f.unlink()
    flows_dir.mkdir(exist_ok=True)
    for t in twin_flows:
        (flows_dir / f"{t['of']['flow']}PowerApps.json").write_text(json.dumps(t, indent=2) + "\n")
    app = None
    if r.ui:
        kw = {"fetch_vendor": fetch_vendor} if fetch_vendor is not None else {}
        example = next((a["ui_example"] for a in built["agents"] if a.get("ui_example")), None)
        app = codeapp.package(r, out, schema_name=schema, display_name=display, tools=tools, host_js=host_js,
                              chat={"workflowId": chat["id"], "displayName": chat["name"]} if chat else None,
                              example=example, **kw)
    summary = {
        "kind": "brainfreeze-studio-rapplication", "rappid": manifest["rappid"], "egg": egg_path.name,
        "egg_address": rapp1.egg_address(manifest),
        "rapp": {"id": r.id, "name": r.name, "version": r.version, "publisher": r.publisher, "source": r.source},
        "agent": {"schemaName": schema, "displayName": display, "workspace": "workspace",
                  "agents": built["agents"]},
        "tools": tools,
        "powerapps_flows": [{"id": t["id"], "name": t["name"], "of": t["of"]} for t in twin_flows],
        "chat": {"id": chat["id"], "name": chat["name"]} if chat else None,
        "codeapp": ({"dir": "codeapp", "displayName": display, "report": app["report"]} if app else None),
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def deploy(out_dir, environment, get_dataverse_token, get_powerapps_token=None, *, log=print, publish_agent=True,
           dataverse=None, opener=None, get_apihub_token=None, files_site=None, files_folder=None, app=True):
    """Deploy what prepare() wrote, as the signed-in user: the agent (brainfreeze_studio.deploy), the Power Apps
    flows, then the code app (brainfreeze_studio.codeapp_publish) when there is one, a Power Apps token and app is
    true. get_apihub_token lets the deploy make the user's own SharePoint (or other Microsoft) connection when they
    have none; files_site/files_folder say where agents that read files find them."""
    from . import deploy as dep
    from .codeapp_publish import publish
    out = Path(out_dir).expanduser()
    summary = json.loads((out / "rapplication.json").read_text())
    dv = dataverse or dep.Dataverse(environment, get_dataverse_token)
    agent = dep.deploy(out / "workspace", environment, get_dataverse_token, log=log, do_publish=publish_agent,
                       dataverse=dv, get_powerapps_token=get_powerapps_token, powerapps_opener=opener,
                       get_apihub_token=get_apihub_token, files_site=files_site, files_folder=files_folder)
    from .connector_code import fill_connectors
    code_ids = {c["displayName"]: c["internalId"] for c in agent.get("connectors") or []}
    log("flows for the code app")
    flows = []
    for f in sorted((out / "powerapps-flows").glob("*.json")):
        t = json.loads(f.read_text())
        t["definition"] = fill_connectors(t["definition"], code_ids)
        if agent.get("files_home"):
            dep.apply_files_home(t["definition"], agent["files_home"])
        for api, ref in ((t["definition"].get("properties") or {}).get("connectionReferences") or {}).items():
            logical = ((ref or {}).get("connection") or {}).get("connectionReferenceLogicalName")
            if logical:
                connector = f"/providers/Microsoft.PowerApps/apis/{((ref.get('api') or {}).get('name')) or api}"
                r = dep.ensure_connection_reference(dv, logical, connector, f"{summary['agent']['displayName']} - {api}")
                log(f"   {logical}: {r['operation']}")
        flows.append(dep.ensure_workflow(dv, t))
        log(f"   {t['name']}: {flows[-1]['operation']}")
    result = {"agent": agent, "powerapps_flows": flows, "codeapp": None}
    if summary.get("codeapp") and get_powerapps_token and app:
        env_id = dep.environment_id(dv)
        if not env_id:
            raise dep.DeployError("couldn't read the environment id from Dataverse")
        log("code app")
        result["codeapp"] = publish(out / "codeapp", env_id, get_powerapps_token, log=log, opener=opener)
    summary["deployed"] = {"environment": dv.environment, "botId": agent.get("botId"), "makerUrl": agent.get("makerUrl"),
                           "powerapps_flows": flows, "codeapp": result["codeapp"], "at": utc_now()}
    (out / "rapplication.json").write_text(json.dumps(summary, indent=2) + "\n")
    return result
