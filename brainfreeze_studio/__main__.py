"""python3 -m brainfreeze_studio build <egg> --name "..." --publisher-prefix rapp [--sdk-dir ...] [--out build/]"""
import argparse
import json
import os
import sys

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
    b.add_argument("--out", default="build", help="output folder (default build/)")
    b.add_argument("--json", action="store_true", help="print the summary as JSON")
    sv = sub.add_parser("serve", help="fallback: serve an egg's agents as MCP tools (runs the real agent.py)")
    sv.add_argument("egg")
    sv.add_argument("--engine-dir", help="a grail checkout to use instead of cloning the egg's pinned engine")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=7700)
    sv.add_argument("--api-key", default=os.getenv("BRAINFREEZE_MCP_KEY"), help="required beyond loopback")
    a = p.parse_args(argv)
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
                  translations=a.translations, mcp_connector_id=a.mcp_connector_id, mcp_host=a.mcp_host)
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


if __name__ == "__main__":
    sys.exit(main())
