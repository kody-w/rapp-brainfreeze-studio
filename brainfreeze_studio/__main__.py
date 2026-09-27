"""python3 -m brainfreeze_studio build <egg> --name "..." --publisher-prefix rapp [--sdk-dir ...] [--out build/]
python3 -m brainfreeze_studio environments                      (the environments your az login can reach)
python3 -m brainfreeze_studio deploy build/workspace --environment https://<org>.crm.dynamics.com/ [--draft]
python3 -m brainfreeze_studio rapplication @kody-w/agent_team --out out/ [--environment https://<org>.crm.dynamics.com/ --deploy]
python3 -m brainfreeze_studio codeapp-host
python3 -m brainfreeze_studio managed-app spec.json --out out/ [--deploy --sdk-dir ../copilot-harness-sdk --tenant <id>]"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext, redirect_stdout
from pathlib import Path

from . import StudioBuildError, build


def parser():
    p = argparse.ArgumentParser(prog="brainfreeze-studio",
                                description="Turn a frozen RAPP brainstem (organism egg) into a Copilot Studio agent.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write a GitHub Copilot harness workspace from an egg (offline)")
    b.add_argument("egg", help="organism egg: a file path or an https URL")
    b.add_argument("--name", required=True, help="agent display name (42 characters max)")
    b.add_argument("--publisher-prefix", required=True, help="solution publisher prefix, e.g. rapp")
    b.add_argument("--schema-name", help="default: <prefix>_<Name without spaces>")
    b.add_argument("--sdk-dir", help="a copilot-harness-sdk checkout (for its proven infrastructure profiles)")
    b.add_argument("--environment", help="https://<org>.crm.dynamics.com/ (needed by the memory profiles)")
    b.add_argument("--hn-api-name", help="the environment's RAPP Hacker News connector (needed by that profile)")
    b.add_argument("--session", help="a session egg: its prompts become proof.json")
    b.add_argument("--model", default="Sonnet46", help="model series written to the agent (default Sonnet46)")
    b.add_argument("--translations", help="folder of translation specs: agents that prove parity become agent flows")
    b.add_argument("--mcp-connector-id", help="fallback: route untranslated agents to an MCP server's custom connector")
    b.add_argument("--mcp-host", help="with --mcp-connector-id: the MCP server's host, for the connector files")
    b.add_argument("--files-site", help="the SharePoint site agents that read files find them in "
                   "(https://<tenant>.sharepoint.com/sites/<site>; default: filled in at deploy)")
    b.add_argument("--files-folder", help="the folder in that site their paths start from (default /Shared Documents)")
    b.add_argument("--out", default="build", help="output folder (default build/)")
    b.add_argument("--json", action="store_true", help="print the summary as JSON")
    dp = sub.add_parser("deploy", help="deploy a built workspace to Copilot Studio as you, with your Azure CLI sign-in "
                        "(az login)")
    dp.add_argument("workspace", help="the workspace a build wrote, for example build/workspace")
    dp.add_argument("--environment", required=True, help="https://<org>.crm.dynamics.com/ (see: environments)")
    dp.add_argument("--draft", action="store_true", help="leave the agent a Draft: don't publish it")
    dp.add_argument("--plan", action="store_true", help="preview the deploy with GETs only; do not deploy or publish")
    dp.add_argument("--expect", help="deploy only the workspace digest shown by the approved plan")
    dp.add_argument("--keep-extra", action="store_true", help="keep the agent's components that are not in the workspace")
    dp.add_argument("--use-shared-connection", action="store_true",
                    help="allow an environment connection that may belong to someone else")
    dp.add_argument("--files-site", help="the SharePoint site agents that read files find them in (default: the "
                    "build's, else the environment's RAPP Files Site, else your tenant's root site)")
    dp.add_argument("--files-folder", help="the folder in that site their paths start from (default /Shared Documents)")
    dp.add_argument("--json", action="store_true", help="print the result as JSON")
    en = sub.add_parser("environments", help="the Power Platform environments your Azure CLI sign-in (az login) can "
                        "reach, from the Global Discovery Service")
    en.add_argument("--json", action="store_true", help="print them as JSON")
    sv = sub.add_parser("serve", help="fallback: serve an egg's agents as MCP tools (runs the real agent.py)")
    sv.add_argument("egg")
    sv.add_argument("--engine-dir", help="a grail checkout to use instead of cloning the egg's pinned engine")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=7700)
    sv.add_argument("--api-key", default=os.getenv("BRAINFREEZE_MCP_KEY"), help="required beyond loopback")
    ra = sub.add_parser("rapplication", help="a RAPP Store rapplication (agent.py + UI) → Copilot Studio agent + "
                                              "Power Apps code app")
    ra.add_argument("ref", help="a store ref (@publisher/id or id), a bundle folder or zip, or a rapplication egg")
    ra.add_argument("--store", default=None, help="the catalog root: an https URL or a RAPP_Store checkout "
                                                  "(default: RAPP_Store main on GitHub)")
    ra.add_argument("--out", default="rapplication", help="output folder (default rapplication/)")
    ra.add_argument("--name", help="agent and app display name (default: the rapplication's name, 42 characters max)")
    ra.add_argument("--publisher-prefix", default="rapp", help="solution publisher prefix (default rapp)")
    ra.add_argument("--schema-name", help="default: <prefix>_<Name without spaces>")
    ra.add_argument("--translations", help="folder of translation specs (agents that prove parity become flows)")
    ra.add_argument("--sdk-dir", help="a copilot-harness-sdk checkout (for its proven infrastructure profiles)")
    ra.add_argument("--rappid", help="the rappid to give the egg (default: minted once, then kept in --out)")
    ra.add_argument("--environment", help="https://<org>.crm.dynamics.com/: where --deploy puts it")
    ra.add_argument("--deploy", action="store_true", help="deploy as you: the agent, its flows and the code app, "
                                                          "with your Azure CLI sign-in (az login)")
    ra.add_argument("--plan", action="store_true", help="with --deploy: preview with GETs only; do not deploy or publish")
    ra.add_argument("--expect", help="with --deploy: require the workspace digest shown by the approved plan")
    ra.add_argument("--keep-extra", action="store_true", help="with --deploy: keep components not in the workspace")
    ra.add_argument("--use-shared-connection", action="store_true",
                    help="with --deploy: allow an environment connection that may belong to someone else")
    ra.add_argument("--no-app", action="store_true", help="leave out the code app and its Power Apps flows; no Node or npm")
    ra.add_argument("--draft", action="store_true", help="with --deploy: leave the agent a Draft: don't publish it")
    ra.add_argument("--files-site", help="the SharePoint site agents that read files find them in (default: the "
                    "environment's RAPP Files Site, else your tenant's root site)")
    ra.add_argument("--files-folder", help="the folder in that site their paths start from (default /Shared Documents)")
    ra.add_argument("--json", action="store_true", help="print the summary as JSON")
    pr = sub.add_parser("record-proof", help="prove a connector-code port or a materialized spec against its agent.py "
                        "and record the proof beside the spec, so builds that can't run it (no .NET SDK, no pinned "
                        "data, or no agent code allowed, as in a hosted service) can lay it")
    pr.add_argument("spec", help="the port's spec, for example translations/json_doctor.json")
    pr.add_argument("agent", help="the agent.py it ports")
    pr.add_argument("--basic", help="the BasicAgent it runs beside (default: this package's, as rapplication builds use)")
    ch = sub.add_parser("codeapp-host", help="build the code app host once (needs node and npm)")
    ch.add_argument("--build-dir", help="default: ~/.cache/brainfreeze-studio/codeapp-host-build")
    ma = sub.add_parser("managed-app", help="a managed app (Microsoft Copilot Managed Runtime) from a spec, of kind "
                        "sharepoint-media, people-directory, calendar-dashboard, sharepoint-list or task-tracker")
    ma.add_argument("spec", help="the spec JSON file: {kind, name, title?, ...the kind's settings} (README: Managed "
                    "apps)")
    ma.add_argument("--out", default="out", help="the project goes to <out>/managed-app (default: out)")
    ma.add_argument("--deploy", action="store_true", help="register, bind, build, push and deploy it as the user "
                    "signed in to the ms CLI (ms auth login); needs --sdk-dir and --tenant")
    ma.add_argument("--no-deploy", action="store_true", help="with --deploy: stop after the local build and commit")
    ma.add_argument("--sdk-dir", help="a copilot-harness-sdk checkout (its scripts/managed-apps.mjs)")
    ma.add_argument("--tenant", help="the Entra tenant id of the account that owns the app (for the git push)")
    ma.add_argument("--login-hint", help="that account's user name, to pick it at sign-in")
    ma.add_argument("--environment-id", help="the Power Platform environment; omit to let the CLI choose")
    ma.add_argument("--git-cache", help="an MSAL cache file for the git push (later pushes are silent)")
    ma.add_argument("--create-table", action="store_true", help="task-tracker: create its Dataverse table and "
                    "columns where missing (a schema change; uses az for the token)")
    ma.add_argument("--dataverse-url", help="task-tracker: the environment URL for --create-table, if the spec has "
                    "no dataverseUrl")
    ma.add_argument("--json", action="store_true", help="print the result as JSON")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if a.cmd == "environments":
        return _environments(a)
    if a.cmd == "deploy":
        return _deploy(a)
    if a.cmd == "managed-app":
        return _managed_app(a)
    if a.cmd == "codeapp-host":
        from .codeapp import build_host
        try:
            print(f"host: {build_host(a.build_dir)}")
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"brainfreeze-studio: the host build failed: {e}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "rapplication":
        return _rapplication(a)
    if a.cmd == "record-proof":
        return _record_proof(a)
    if a.cmd == "serve":
        from .mcp import McpApp, host_agents, make_server
        try:
            agents, info = host_agents(a.egg, a.engine_dir)
            server = make_server(McpApp(agents, info), a.host, a.port, a.api_key)
        except (StudioBuildError, ModuleNotFoundError) as e:
            print(f"brainfreeze-studio: {e}" + ("  (run with a Python that has the engine's requirements, e.g. "
                  "~/.brainstem/venv/bin/python)" if isinstance(e, ModuleNotFoundError) else ""), file=sys.stderr)
            return 1
        print(f"MCP: http://{a.host}:{a.port}/mcp  tools: {', '.join(sorted(agents))}  egg: {info['rappid']}")
        server.serve_forever()
        return 0
    try:
        r = build(a.egg, a.out, a.name, a.publisher_prefix, schema_name=a.schema_name, sdk_dir=a.sdk_dir,
                  environment=a.environment, session=a.session, model=a.model, hn_api_name=a.hn_api_name,
                  translations=a.translations, mcp_connector_id=a.mcp_connector_id, mcp_host=a.mcp_host,
                  files_home=_files_home(a))
    except StudioBuildError as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({k: str(v) if k == "workspace" else v for k, v in r.items()}, indent=2))
        return 0
    print(f"workspace:   {r['workspace']}")
    print(f"agent:       {r['schema_name']}")
    _show_agents(r["agents"])
    print(f"proof turns: {r['proof_turns']}   memories: {r['memories']}")
    with open(os.path.join(a.out, "provenance.json"), encoding="utf-8") as f:
        prov = json.load(f)
    for agent, p in prov.get("parity", {}).items():
        print(f"parity:      {agent} {p['passed']}/{p['cases']} {'PROVEN' if p['parity'] else 'FAILED'}")
    workspace = shlex.quote(str(r["workspace"]))
    environment = shlex.quote(a.environment) if a.environment else "https://<org>.crm.dynamics.com/"
    print(f"next:        python3 -m brainfreeze_studio deploy {workspace} "
          f"--environment {environment} --draft --plan")
    print("             then the same without --plan")
    return 0


def _show_agents(agents):
    for ag in agents:
        print(f"  {ag['name']:<18} -> {ag['as']}" + (f"  ({ag['note']})" if ag.get("note") else ""))


def az_token(resource):
    """A token for `resource` from the Azure CLI sign-in (az login; AZURE_CONFIG_DIR is honored), cached until close
    to its expiry. The deploy runs as whoever that is."""
    cached = _TOKENS.get(resource)
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    try:
        out = subprocess.run([shutil.which("az") or "az", "account", "get-access-token", "--resource", resource,
                              "-o", "json"], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise SystemExit("brainfreeze-studio: this signs in with the Azure CLI; install it and run az login")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"brainfreeze-studio: az couldn't get a token for {resource}: {e.stderr.strip()[:400]}")
    body = json.loads(out)
    _TOKENS[resource] = (body["accessToken"], float(body.get("expires_on") or time.time() + 1800))
    return body["accessToken"]


_TOKENS = {}
APIHUB = "https://apihub.azure.com"


def _files_home(a):
    home = {k: v for k, v in (("site", a.files_site), ("folder", a.files_folder)) if v}
    return home or None


def _environments(a):
    from .discovery import DISCOVERY, DiscoveryError, environments
    try:
        found = environments(az_token(DISCOVERY))
    except DiscoveryError as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(found, indent=2))
        return 0
    if not found:
        print("brainfreeze-studio: this sign-in belongs to no enabled environment; az login with the account that "
              "has your Copilot Studio environment", file=sys.stderr)
        return 1
    width = max(len(e["name"]) for e in found)
    for e in found:
        print(f"{e['name']:<{width}}  {e['kind'] or '-':<10}  {e['region'] or '-':<14}  {e['url']}")
    return 0


def _deploy(a):
    from .codeapp_publish import AUDIENCE, PublishError
    from .deploy import DeployError, deploy, plan
    env = a.environment.rstrip("/") + "/"
    options = dict(expect=a.expect, keep_extra_components=a.keep_extra, use_shared_connection=a.use_shared_connection,
                   get_powerapps_token=lambda: az_token(AUDIENCE), get_apihub_token=lambda: az_token(APIHUB),
                   files_site=a.files_site, files_folder=a.files_folder)
    try:
        if a.plan:
            return _show_plan(plan(a.workspace, env, lambda: az_token(env.rstrip("/")), **options), a.json)
        r = deploy(a.workspace, env, lambda: az_token(env.rstrip("/")), do_publish=not a.draft,
                   log=(lambda m: None) if a.json else print, **options)
    except (DeployError, PublishError, OSError, ValueError, KeyError) as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    if "status" not in r:
        print(f"agent:       {r.get('displayName')}  ({r.get('schemaName')})")
        if a.draft:
            print("status:      Draft: not published")
    if not a.draft:
        print(f"status:      published {r.get('published', {}).get('publishedon')}")
    print(f"maker:       {r.get('makerUrl')}")
    return 0


def _show_plan(result, as_json=False):
    agent = result["agent"]
    refused = agent["operation"] == "refuse"
    if as_json:
        print(json.dumps(result, indent=2))
        return 1 if refused else 0
    print(f"digest:      {result['digest']}")
    print(f"agent:       {agent['displayName']}  ({agent['schemaName']})")
    if refused:
        print(f"plan:        refuse: {agent['reason']}")
    else:
        existing = json.dumps(agent.get("existingDisplayName") or agent["displayName"], ensure_ascii=False)
        text = {"create": "create: no agent with this schema name exists",
                "update": f"update: an agent with this schema name exists ({existing}), components below change",
                "unchanged": f"unchanged: agent settings match ({existing}); component and flow changes are listed below"}
        print(f"plan:        {text[agent['operation']]}")
        print(f"status:      {result['status']}")
        c = result["components"]
        if any(c.values()):
            print(f"tools:       adds {len(c['add'])}, updates {len(c['update'])}, removes {len(c['remove'])}, "
                  f"keeps {len(c['keep'])}")
        print("remove:      " + (", ".join(c["remove"]) or "none"))
        if result.get("keepExtraComponents"):
            print("keep:        " + (", ".join(result["keptExtraComponents"]) or "none") + " (--keep-extra)")
        f = result["flows"]
        if any(f.values()):
            print(f"flows:       creates {len(f['create'])}, updates {len(f['update'])}")
        variables, references = len(result["environmentVariables"]["create"]), len(result["connectionReferences"]["create"])
        if variables or references:
            print(f"settings:    creates {variables} environment variables, {references} connection references")
        for logical, source in result["connectionReferences"]["sources"].items():
            print(f"connection:  {logical}: {source}")
        app = result.get("codeapp")
        if app:
            if app["operation"] == "unchecked":
                print(f"code app:     not checked: {app['reason']}")
            else:
                verb = "creates" if app["operation"] == "create" else "updates"
                print(f"code app:     {verb} {json.dumps(app['displayName'], ensure_ascii=False)}"
                      + (f" ({app['appId']}) in place" if app.get("appId") else ""))
        elif result.get("codeapp_skipped"):
            print("code app:     " + ("not published (--draft); run again without --draft to publish it"
                  if result["codeapp_skipped"] == "--draft" else "not built (--no-app)"))
    print("plan only:   nothing was changed")
    return 1 if refused else 0


def _record_proof(a):
    import hashlib
    from pathlib import Path
    from . import connector_code as cc
    from .materialize import agent_python
    spec_file = Path(a.spec)
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    spec["_dir"] = str(spec_file.parent)
    basic = Path(a.basic) if a.basic else Path(__file__).with_name("basic_agent.py")
    source = Path(a.agent).read_bytes().decode("utf-8", errors="replace")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if spec.get("source_sha256") and spec["source_sha256"] != digest:
        print(f"brainfreeze-studio: the spec is for other code (sha256 {spec['source_sha256'][:12]}, "
              f"{a.agent} has {digest[:12]})", file=sys.stderr)
        return 1
    spec["_file"] = spec_file.name
    if spec.get("mode") == "materialized":
        # proven on its pinned data, beside the agent's siblings (an agent may load them)
        from .flows import StudioBuildError, materialized_record_file, prove_materialized, record_materialized
        report = prove_materialized(spec, a.agent, basic)
        try:
            rec = record_materialized(spec, report, hashlib.sha256(basic.read_bytes()).hexdigest(), time.strftime("%Y-%m-%d"))
        except StudioBuildError as e:
            print(f"brainfreeze-studio: {e}" + (f": {report['reason']}" if report.get("reason") else ""), file=sys.stderr)
            return 1
        target = materialized_record_file(spec)
    else:
        try:
            report = cc.prove(spec, a.agent, basic, spec_file.parent / spec["script"], python=agent_python(spec.get("python")))
            rec = cc.record(spec, report, digest, hashlib.sha256(basic.read_bytes()).hexdigest(), time.strftime("%Y-%m-%d"))
        except cc.ConnectorCodeError as e:
            print(f"brainfreeze-studio: {e}", file=sys.stderr)
            return 1
        target = cc.proof_record_file(spec)
    target.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    print(f"{spec['agent']}: {report['passed']}/{report['cases']} proven; recorded in {target}")
    return 0


def _managed_app(a):
    from . import managed_app as ma
    try:
        spec = json.loads(open(a.spec, encoding="utf-8").read())
        made = ma.scaffold(spec, a.out)
    except (OSError, ValueError, ma.ManagedAppError) as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    result = {"dir": str(made["dir"]), "report": made["report"]}
    if a.deploy:
        if not a.sdk_dir or not a.tenant:
            print("brainfreeze-studio: --deploy needs --sdk-dir <copilot-harness-sdk> and --tenant <tenant id>",
                  file=sys.stderr)
            return 1
        try:
            result["deployed"] = ma.lifecycle(made["dir"], display_name=made["spec"]["name"], sdk_dir=a.sdk_dir,
                                              tenant_id=a.tenant, environment_id=a.environment_id,
                                              login_hint=a.login_hint, deploy=not a.no_deploy,
                                              git_cache=a.git_cache, create_table=a.create_table,
                                              dataverse_url=a.dataverse_url,
                                              log=(lambda m: None) if a.json else print)
        except ma.ManagedAppError as e:
            print(f"brainfreeze-studio: {e}", file=sys.stderr)
            return 1
    if a.json:
        print(json.dumps(result, indent=2))
        return 0
    r = made["report"]
    print(f"managed app:  {r['app']}  ({made['dir']})")
    print(f"{r['kind']}:{' ' * max(1, 13 - len(r['kind']))}{r['summary']}")
    for d in r["dataSources"]:
        table = f" {d['table']}" if d.get("table") else ""
        print(f"connector:    {d['connector']} ({d['as']}{table}): {', '.join(d['allowedActions'])}")
    if result.get("deployed"):
        dep = result["deployed"]
        print(f"app id:       {dep.get('appId')}")
        if dep.get("playUrl"):
            print(f"play:         {dep['playUrl']}  (commit {str(dep.get('commit'))[:7]})")
    else:
        print("next:         --deploy --sdk-dir ../copilot-harness-sdk --tenant <tenant id>   (or: cd there; ms app init; …)")
    return 0


def _rapplication(a):
    from . import rapplication as rp
    from .codeapp_publish import AUDIENCE, PublishError
    from .deploy import DeployError
    if a.plan and not a.deploy:
        print("brainfreeze-studio: --plan needs --deploy and --environment https://<org>.crm.dynamics.com/",
              file=sys.stderr)
        return 1
    if (a.expect or a.keep_extra or a.use_shared_connection) and not a.deploy:
        print("brainfreeze-studio: --expect, --keep-extra and --use-shared-connection need --deploy", file=sys.stderr)
        return 1
    if a.deploy and not a.environment:
        print("brainfreeze-studio: --deploy needs --environment https://<org>.crm.dynamics.com/", file=sys.stderr)
        return 1
    try:
        with redirect_stdout(sys.stderr) if a.json else nullcontext():
            s = rp.prepare(a.ref, a.out, name=a.name, publisher_prefix=a.publisher_prefix, schema_name=a.schema_name,
                           store=a.store or rp.STORE, translations=a.translations, sdk_dir=a.sdk_dir, rappid=a.rappid,
                           environment=a.environment, files_home=_files_home(a), app=not a.no_app)
    except (StudioBuildError, rp.RapplicationError, FileNotFoundError) as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    deployed = None
    if a.deploy:
        env = a.environment.rstrip("/") + "/"
        try:
            if a.plan:
                return _show_plan(rp.plan(a.out, env, lambda: az_token(env.rstrip("/")), lambda: az_token(AUDIENCE),
                                          app=not a.no_app, files_site=a.files_site, files_folder=a.files_folder,
                                          get_apihub_token=lambda: az_token(APIHUB), expect=a.expect,
                                          keep_extra_components=a.keep_extra,
                                          use_shared_connection=a.use_shared_connection,
                                          publish_agent=not a.draft), a.json)
            deployed = rp.deploy(a.out, env, lambda: az_token(env.rstrip("/")), lambda: az_token(AUDIENCE),
                                 log=(lambda m: None) if a.json else print, app=not a.no_app,
                                 get_apihub_token=lambda: az_token(APIHUB), files_site=a.files_site,
                                 files_folder=a.files_folder, publish_agent=not a.draft, expect=a.expect,
                                 keep_extra_components=a.keep_extra, use_shared_connection=a.use_shared_connection)
        except (DeployError, PublishError, OSError, ValueError, KeyError) as e:
            print(f"brainfreeze-studio: {e}", file=sys.stderr)
            return 1
        with open(os.path.join(a.out, "rapplication.json"), encoding="utf-8") as f:
            s = json.load(f)
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    print(f"rapplication: {s['rapp']['publisher']}/{s['rapp']['id']} v{s['rapp']['version']}  ({s['rappid']})")
    print(f"agent:        {s['agent']['schemaName']}  ({a.out}/workspace)")
    _show_agents(s["agent"].get("agents", []))
    for t in s["tools"]:
        print(f"  {t['name']:<22} -> " + ("agent tool" if a.no_app else
              f"flow for the app: {t['flow']['displayName']}" if t["flow"] else "the agent answers the app"))
    try:
        with open(os.path.join(a.out, "provenance.json"), encoding="utf-8") as f:
            prov = json.load(f)
    except (OSError, ValueError):
        prov = {}
    for agent, p in prov.get("parity", {}).items():
        print(f"parity:       {agent} {p['passed']}/{p['cases']} {'PROVEN' if p['parity'] else 'FAILED'}")
    app = s.get("codeapp")
    if (s.get("deployed") or {}).get("codeapp_skipped") == "--draft":
        print("code app:     not published (--draft); run again without --draft to publish it")
    elif app:
        risks = app["report"].get("risks") or []
        print(f"code app:     {app['displayName']}  ({a.out}/codeapp)" + (f"  {len(risks)} warning(s):" if risks else ""))
        for r in risks:
            print(f"  ! {r}")
    elif a.no_app or s.get("codeapp_skipped"):
        print("code app:     not built (--no-app)")
    else:
        print("code app:     none (the rapplication ships no UI)")
    if deployed:
        d = s["deployed"]
        if a.draft and "status" not in d:
            print("status:      Draft: not published")
        elif not a.draft:
            print(f"status:      published {deployed['agent'].get('published', {}).get('publishedon')}")
        print(f"maker:        {d.get('makerUrl')}")
        if d.get("codeapp"):
            print(f"play:         {d['codeapp']['playUrl']}")
    else:
        workspace = shlex.quote(str(Path(a.out).expanduser() / "workspace"))
        environment = shlex.quote(a.environment) if a.environment else "https://<org>.crm.dynamics.com/"
        print(f"next:         python3 -m brainfreeze_studio deploy {workspace} "
              f"--environment {environment} --draft --plan")
        print("             then the same without --plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
