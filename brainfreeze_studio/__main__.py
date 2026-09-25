"""python3 -m brainfreeze_studio build <egg> --name "..." --publisher-prefix rapp [--sdk-dir ...] [--out build/]
python3 -m brainfreeze_studio rapplication @kody-w/agent_team --out out/ [--environment https://<org>.crm.dynamics.com/ --deploy]
python3 -m brainfreeze_studio codeapp-host"""
import argparse
import json
import os
import subprocess
import sys
import time

from . import StudioBuildError, build


def main(argv=None):
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
    ra.add_argument("--no-app", action="store_true", help="with --deploy: leave the code app out")
    ra.add_argument("--files-site", help="the SharePoint site agents that read files find them in (default: the "
                    "environment's RAPP Files Site, else your tenant's root site)")
    ra.add_argument("--files-folder", help="the folder in that site their paths start from (default /Shared Documents)")
    ra.add_argument("--json", action="store_true", help="print the summary as JSON")
    pr = sub.add_parser("record-proof", help="prove a connector-code port against its agent.py and record the proof "
                        "beside its spec, so builds without the .NET SDK (or that run no agent code) can lay it")
    pr.add_argument("spec", help="the port's spec, for example translations/json_doctor.json")
    pr.add_argument("agent", help="the agent.py it ports")
    pr.add_argument("--basic", help="the BasicAgent it runs beside (default: this package's, as rapplication builds use)")
    ch = sub.add_parser("codeapp-host", help="build the code app host once (needs node and npm)")
    ch.add_argument("--build-dir", help="default: ~/.cache/brainfreeze-studio/codeapp-host-build")
    a = p.parse_args(argv)
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
    for ag in r["agents"]:
        print(f"  {ag['name']:<18} -> {ag['as']}" + (f"  ({ag['note']})" if ag.get("note") else ""))
    print(f"proof turns: {r['proof_turns']}   memories: {r['memories']}")
    prov = json.load(open(os.path.join(a.out, "provenance.json")))
    for agent, p in prov.get("parity", {}).items():
        print(f"parity:      {agent} {p['passed']}/{p['cases']} {'PROVEN' if p['parity'] else 'FAILED'}")
    print(f"next:        deploy with copilot-harness-sdk: see {a.out}/provenance.json")
    return 0


def az_token(resource):
    """A token for `resource` from the Azure CLI sign-in (az login; AZURE_CONFIG_DIR is honored), cached until close
    to its expiry. The deploy runs as whoever that is."""
    cached = _TOKENS.get(resource)
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    try:
        out = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
                             capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise SystemExit("brainfreeze-studio: --deploy signs in with the Azure CLI; install it and run az login")
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
    try:
        report = cc.prove(spec, a.agent, basic, spec_file.parent / spec["script"], python=agent_python(spec.get("python")))
        rec = cc.record(spec, report, digest, hashlib.sha256(basic.read_bytes()).hexdigest(), time.strftime("%Y-%m-%d"))
    except cc.ConnectorCodeError as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    cc.proof_record_file(spec).write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    print(f"{spec['agent']}: {report['passed']}/{report['cases']} proven; recorded in {cc.proof_record_file(spec)}")
    return 0


def _rapplication(a):
    from . import rapplication as rp
    from .codeapp_publish import AUDIENCE, PublishError
    from .deploy import DeployError
    if a.deploy and not a.environment:
        print("brainfreeze-studio: --deploy needs --environment https://<org>.crm.dynamics.com/", file=sys.stderr)
        return 1
    try:
        s = rp.prepare(a.ref, a.out, name=a.name, publisher_prefix=a.publisher_prefix, schema_name=a.schema_name,
                       store=a.store or rp.STORE, translations=a.translations, sdk_dir=a.sdk_dir, rappid=a.rappid,
                       environment=a.environment, files_home=_files_home(a))
    except (StudioBuildError, rp.RapplicationError, FileNotFoundError) as e:
        print(f"brainfreeze-studio: {e}", file=sys.stderr)
        return 1
    deployed = None
    if a.deploy:
        env = a.environment.rstrip("/") + "/"
        try:
            deployed = rp.deploy(a.out, env, lambda: az_token(env.rstrip("/")), lambda: az_token(AUDIENCE),
                                 log=print, app=not a.no_app, get_apihub_token=lambda: az_token(APIHUB),
                                 files_site=a.files_site, files_folder=a.files_folder)
        except (DeployError, PublishError) as e:
            print(f"brainfreeze-studio: {e}", file=sys.stderr)
            return 1
        s = json.loads(open(os.path.join(a.out, "rapplication.json")).read())
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    print(f"rapplication: {s['rapp']['publisher']}/{s['rapp']['id']} v{s['rapp']['version']}  ({s['rappid']})")
    print(f"agent:        {s['agent']['schemaName']}  ({a.out}/workspace)")
    for t in s["tools"]:
        print(f"  {t['name']:<22} -> " + (f"flow for the app: {t['flow']['displayName']}" if t["flow"]
                                          else "the agent answers the app"))
    app = s.get("codeapp")
    if app:
        risks = app["report"].get("risks") or []
        print(f"code app:     {app['displayName']}  ({a.out}/codeapp)" + (f"  {len(risks)} warning(s):" if risks else ""))
        for r in risks:
            print(f"  ! {r}")
    else:
        print("code app:     none (the rapplication ships no UI)")
    if deployed:
        d = s["deployed"]
        print(f"maker:        {d.get('makerUrl')}")
        if d.get("codeapp"):
            print(f"play:         {d['codeapp']['playUrl']}")
    else:
        print(f"next:         --environment https://<org>.crm.dynamics.com/ --deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
