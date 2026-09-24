"""python3 -m brainfreeze_studio build <egg> --name "..." --publisher-prefix rapp [--sdk-dir ...] [--out build/]"""
import argparse
import json
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
    b.add_argument("--out", default="build", help="output folder (default build/)")
    b.add_argument("--json", action="store_true", help="print the summary as JSON")
    a = p.parse_args(argv)
    try:
        r = build(a.egg, a.out, a.name, a.publisher_prefix, schema_name=a.schema_name, sdk_dir=a.sdk_dir,
                  environment=a.environment, session=a.session, model=a.model, hn_api_name=a.hn_api_name)
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
    print(f"next:        deploy with copilot-harness-sdk: see {a.out}/provenance.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
