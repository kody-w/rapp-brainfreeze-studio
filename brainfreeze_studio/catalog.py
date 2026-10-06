"""From a catalog egg to a Copilot Studio agent in one command, gated by a gauntlet.

    brainfreeze-studio from-catalog <egg-url> --environment https://<org>.crm.dynamics.com/ [--name ...]
    brainfreeze-studio catalog-check [--catalog rar | <index.json URL> | <egg URL>...] [--out status.json]

The gauntlet runs the egg exactly as a person would, beside (never inside) their own brainstem: fetch it over https,
verify it as a rapp/1 organism egg, hatch it in a throwaway brainstem on the grail engine, require every agent to
load with none quarantined, and ask it what it can do. Only a green gauntlet goes on to build, plan, deploy (exactly
the planned workspace) and a live proof over /3p. Agents with a translation spec are proven equal to their Python
(the build's parity table); every other agent is deployed as a reasoning skill, and the summary says so.

catalog-check runs the gauntlet alone over a whole catalog (default: the eggs in kody-w/RAR) and writes one status
file, for a nightly job. Each throwaway is torn down whatever happens.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RAR_TREE = "https://api.github.com/repos/kody-w/RAR/git/trees/main?recursive=1"
RAR_RAW = "https://raw.githubusercontent.com/kody-w/RAR/main/"
QUESTION = "In two short sentences, what can you do for me?"
GITHUB = Path.home() / "Documents" / "GitHub"


class CatalogError(Exception):
    pass


def _brainfreeze():
    """The brainfreeze package (throwaway brainstems), from BRAINFREEZE_DIR or the usual checkout."""
    for root in (os.getenv("BRAINFREEZE_DIR"), str(GITHUB / "brainfreeze"), str(GITHUB / "rapp-brainfreeze")):
        if root and (Path(root) / "brainfreeze" / "__init__.py").is_file() and root not in sys.path:
            sys.path.insert(0, root)
    try:
        import brainfreeze
    except ImportError:
        raise CatalogError("the gauntlet needs brainfreeze (kody-w/rapp-brainfreeze); set BRAINFREEZE_DIR to its checkout")
    return brainfreeze


def _get(url, limit=64 * 1024 * 1024):
    if not url.startswith("https://"):
        raise CatalogError(f"refusing a non-https egg URL: {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise CatalogError(f"{url} is larger than {limit // (1024 * 1024)} MB")
    return data


def rar_eggs():
    """Egg URLs in the RAR catalog."""
    with urllib.request.urlopen(RAR_TREE, timeout=60) as r:
        tree = json.load(r)
    return [RAR_RAW + t["path"] for t in tree.get("tree", []) if t.get("path", "").endswith(".egg")]


def gauntlet(url, workdir, question=QUESTION, timeout=240):
    """Run one egg through the gauntlet. Returns a report; report['green'] says whether it passed."""
    bf = _brainfreeze()
    from brainfreeze import rapp1
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    rep = {"egg": url, "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": [], "green": False}

    def step(name, ok, detail=""):
        rep["steps"].append({"step": name, "ok": bool(ok), "detail": detail})
        print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)
        return ok

    try:
        blob = _get(url)
    except Exception as e:
        step("fetch", False, str(e))
        return rep
    rep["sha256"] = hashlib.sha256(blob).hexdigest()
    step("fetch", True, f"{len(blob):,} bytes, sha256 {rep['sha256'][:16]}")
    egg = workdir / (url.rstrip("/").rsplit("/", 1)[-1] or "egg.egg")
    egg.write_bytes(blob)
    ok, where, why = rapp1.verify_egg(blob)
    if not step("verify rapp/1", ok, "" if ok else f"{where}: {why}"):
        return rep
    manifest, files = rapp1.read_egg(blob)
    rep["rappid"] = manifest["rappid"]
    if not step("organism egg", manifest["variant"] == "organism", manifest["variant"]):
        return rep
    agent_files = sorted(f for f in files if f.startswith("agents/") and f.endswith("_agent.py")
                         and f.count("/") == 1 and f != "agents/basic_agent.py")
    rep["agents"] = agent_files
    bs = None
    try:
        bs = bf.Throwaway.hatch(str(egg)).up()
        h = bs.health()
        loaded, quarantined = h.get("agents", []), h.get("quarantined", [])
        rep["loaded"], rep["quarantined"] = loaded, quarantined
        step("hatch", True, f"{bs.url}, engine {h.get('version')}")
        if not step("every agent loads", len(loaded) >= len(agent_files) and not quarantined,
                    f"{len(loaded)} loaded of {len(agent_files)} agent files"
                    + (f"; quarantined: {', '.join(map(str, quarantined))}" if quarantined else "")):
            return rep
        t0 = time.time()
        r = bs.chat(question)
        answer = (r.response or "").strip()
        rep["answer"] = answer
        if not step("answers", bool(re.search(r"\w{3,}", answer)), f"{time.time() - t0:.1f}s: {answer[:140]}"):
            return rep
        rep["green"] = True
    except Exception as e:                                   # a stranger's egg: any failure is a red gauntlet
        step("hatch and talk", False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        if bs is not None:
            try:
                bs.down()
            except Exception:
                pass
    return rep


def _run(argv, **kw):
    print("  $ " + " ".join(str(a) for a in argv[:6]) + (" ..." if len(argv) > 6 else ""), flush=True)
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)


def ship(url, environment, out, name=None, publisher_prefix="rapp", translations=None, sdk_dir=None,
         proof_client_id=None, tenant_id=None):
    """Gauntlet → build → plan → deploy → live proof. Returns a summary dict; raises CatalogError at a red step."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    print("gauntlet", flush=True)
    rep = gauntlet(url, out / "gauntlet")
    (out / "gauntlet.json").write_text(json.dumps(rep, indent=2))
    if not rep["green"]:
        raise CatalogError(f"the gauntlet is red, so nothing was built or deployed (see {out / 'gauntlet.json'})")
    egg = out / "gauntlet" / (url.rstrip("/").rsplit("/", 1)[-1])
    slug = rep["rappid"].split("/", 1)[1].split(":", 1)[0]
    name = name or slug.replace("-", " ").title()

    print("build", flush=True)
    from . import build
    sdk_dir = sdk_dir or (GITHUB / "copilot-harness-sdk")
    b = build(str(egg), out / "build", name, publisher_prefix, sdk_dir=str(sdk_dir) if Path(sdk_dir).is_dir() else None,
              environment=environment, translations=translations)
    prov = json.loads((out / "build" / "provenance.json").read_text())
    parity = prov.get("parity", {})
    for ag in b["agents"]:
        p = parity.get(ag["name"])
        print(f"  {ag['name']:<28} -> {ag['as']}" + (f"  parity {p['passed']}/{p['cases']}" if p else "  (not proven: reasoning skill)"
                                                     if "skill" in str(ag["as"]).lower() else ""), flush=True)
    failed = [a for a, p in parity.items() if not p.get("parity")]
    if failed:
        raise CatalogError(f"parity failed for {', '.join(failed)}; nothing was deployed")

    print("plan", flush=True)
    me = [sys.executable, "-m", "brainfreeze_studio", "deploy", b["workspace"], "--environment", environment]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent),
                                                                     os.environ.get("PYTHONPATH")])))
    r = _run(me + ["--plan"], env=env)
    digest = (re.search(r"^digest:\s+(\S+)", r.stdout, re.M) or [None, None])[1]
    if r.returncode != 0 or not digest:
        raise CatalogError(f"the deploy plan failed:\n{(r.stdout + r.stderr)[-1500:]}")
    print(f"  digest {digest}", flush=True)

    print("deploy", flush=True)
    r = _run(me + ["--expect", digest], env=env)
    (out / "deploy.log").write_text(r.stdout + r.stderr)
    maker = (re.search(r"^maker:\s+(\S+)", r.stdout, re.M) or [None, None])[1]
    if r.returncode != 0 or not maker:
        raise CatalogError(f"the deploy failed (log: {out / 'deploy.log'}):\n{(r.stdout + r.stderr)[-1500:]}")
    env_id = (re.search(r"/environments/([0-9a-f-]{36})/", maker) or [None, None])[1]
    print(f"  {maker}", flush=True)

    summary = {"egg": url, "rappid": rep["rappid"], "agent": b["schema_name"], "maker": maker,
               "parity": parity, "skills": [a["name"] for a in b["agents"] if "skill" in str(a["as"]).lower()],
               "live": None}
    client, tenant = proof_client_id or os.getenv("ENTRA_CLIENT_ID"), tenant_id or os.getenv("ENTRA_TENANT_ID")
    if client and tenant and env_id:
        print("prove live", flush=True)
        spec = {"schemaName": b["schema_name"], "environmentId": env_id, "turnTimeoutMs": 240000,
                "turns": [{"component": "gauntlet question", "prompt": QUESTION, "expect": [r"\w{3,}"]}]}
        (out / "proof.json").write_text(json.dumps(spec, indent=2))
        r = _run(["node", "scripts/prove-usecase.mjs", "--spec", out / "proof.json", "--environment-url", environment,
                  "--out", out / "proof.results.json"], cwd=str(sdk_dir),
                 env=dict(os.environ, ENTRA_CLIENT_ID=client, ENTRA_TENANT_ID=tenant, COPILOT_ENVIRONMENT_ID=env_id),
                 timeout=900)
        (out / "proof.log").write_text(r.stdout + r.stderr)
        summary["live"] = r.returncode == 0
        last = [ln for ln in r.stdout.splitlines() if "turns passed" in ln or ln.strip().startswith("[")]
        print("\n".join("  " + ln.strip()[:200] for ln in last), flush=True)
        if r.returncode != 0:
            raise CatalogError(f"deployed, but the live proof failed (log: {out / 'proof.log'})")
    else:
        print("prove live: skipped (set ENTRA_CLIENT_ID and ENTRA_TENANT_ID for the /3p proof app)", flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def check_catalog(urls, out):
    """The gauntlet over many eggs; writes one status file. Returns the list of reports."""
    reports = []
    with tempfile.TemporaryDirectory(prefix="bfs-catalog-") as tmp:
        for i, url in enumerate(urls):
            print(f"[{i + 1}/{len(urls)}] {url}", flush=True)
            reports.append(gauntlet(url, Path(tmp) / str(i)))
    status = {"checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "green": sum(r["green"] for r in reports), "total": len(reports),
              "eggs": [{k: r.get(k) for k in ("egg", "sha256", "rappid", "green", "steps")} for r in reports]}
    Path(out).write_text(json.dumps(status, indent=2))
    return status
