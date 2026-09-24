"""Translate an agent.py into a Copilot Studio agent flow, and prove the two agree before deploy.

A translation spec (JSON) says what the flow does, in Power Automate's own expression language:

    {
      "agent": "InvoiceRouter",
      "flow_name": "InvoiceRouterFlow",
      "description": "Routes one invoice ...",
      "inputs":   {"vendor": {"type": "string", "description": "..."},
                   "amount": {"type": "number", "description": "..."}},
      "settings": {"INVOICE_APPROVAL_LIMIT": {"display": "Invoice approval limit", "default": "10000"}},
      "steps":    [["limit", "@float(setting('INVOICE_APPROVAL_LIMIT'))"], ...],
      "outputs":  {"result": "@if(greater(...), concat(...), concat(...))"},
      "vectors":  [{"vendor": "Fabrikam", "amount": 18750}, ...]
    }

``compile_flow`` turns it into the flow.json shape Copilot Studio calls (the same shape as the
proven HackerNews flow: a Skills request trigger, Compose steps, a Skills response).
``prove`` runs the agent's real Python and evaluates the *compiled flow's* expressions for every
test vector and every setting value, and reports each mismatch. A translation is only laid into
a workspace when its proof passes.

The evaluator implements the subset of the Workflow Definition Language the specs use, with
.NET formatting semantics (for example, ``formatNumber`` rounds exact midpoints away from zero,
where Python rounds them to even). Where the evaluator's semantics are an assumption about
Power Automate, the proof report says the result still needs one live confirmation.
"""
import json
import math
import re
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import StudioBuildError

# ── a small Workflow Definition Language evaluator ──────────────────────────

_TOKEN = re.compile(r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>'(?:[^']|'')*')|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    r"|(?P<op>\?\[|\[|\]|\(|\)|,))")


def _tokens(src):
    pos, out = 0, []
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            if src[pos:].strip() == "":
                break
            raise StudioBuildError(f"cannot parse expression at: {src[pos:pos + 30]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, src):
        self.toks, self.i = _tokens(src), 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise StudioBuildError(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def expr(self):
        kind, val = self.take()
        if kind == "num":
            node = ("lit", float(val) if "." in val else int(val))
        elif kind == "str":
            node = ("lit", val[1:-1].replace("''", "'"))
        elif kind == "name":
            if val in ("null", "true", "false") and self.peek()[1] != "(":
                node = ("lit", {"null": None, "true": True, "false": False}[val])
            else:
                self.take("(")
                args = []
                if self.peek()[1] != ")":
                    args.append(self.expr())
                    while self.peek()[1] == ",":
                        self.take(",")
                        args.append(self.expr())
                self.take(")")
                node = ("call", val, args)
        else:
            raise StudioBuildError(f"unexpected token {val!r}")
        while self.peek()[1] in ("[", "?["):
            safe = self.take()[1] == "?["
            key = self.expr()
            self.take("]")
            node = ("index", node, key, safe)
        return node


def _format_number(value, fmt, locale="en-US"):
    """.NET 'N<d>' / 'F<d>' formatting for en-US, midpoints rounded away from zero."""
    if locale not in ("en-US", None):
        raise StudioBuildError(f"formatNumber locale {locale!r} is not modeled")
    m = re.fullmatch(r"([NnFf])(\d*)", fmt or "")
    if not m:
        raise StudioBuildError(f"formatNumber format {fmt!r} is not modeled")
    digits = int(m.group(2) or 2)
    q = Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f"{q:,.{digits}f}" if m.group(1) in "Nn" else f"{q:.{digits}f}"


def _to_float(v):
    if isinstance(v, bool) or v is None:
        raise StudioBuildError(f"float() of {v!r}")
    return float(v)


def _str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


FUNCTIONS = {
    "concat": lambda *a: "".join(_str(x) for x in a),
    "if": None,                                    # lazy; handled in _eval
    "greater": lambda a, b: a > b, "greaterOrEquals": lambda a, b: a >= b,
    "less": lambda a, b: a < b, "lessOrEquals": lambda a, b: a <= b,
    "equals": lambda a, b: a == b, "not": lambda a: not a,
    "and": lambda *a: all(a), "or": lambda *a: any(a),
    "float": _to_float, "int": lambda v: int(float(v)), "string": _str,
    "add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b,
    "div": lambda a, b: (a // b if isinstance(a, int) and isinstance(b, int) else a / b),
    "toUpper": lambda s: _str(s).upper(), "toLower": lambda s: _str(s).lower(), "trim": lambda s: _str(s).strip(),
    "empty": lambda v: v in (None, "", [], {}), "coalesce": lambda *a: next((x for x in a if x is not None), None),
    "formatNumber": lambda v, f, loc="en-US": _format_number(v, f, loc),
    "mod": lambda a, b: math.fmod(a, b) if isinstance(a, float) or isinstance(b, float) else int(math.fmod(a, b)),
}


# ── serializing and macros (translation helpers that compile to plain expressions) ──

def _serialize(node):
    kind = node[0]
    if kind == "lit":
        v = node[1]
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        if isinstance(v, float):
            text = f"{v:.15f}".rstrip("0")
            return text + "0" if text.endswith(".") else text
        return str(v)
    if kind == "index":
        return f"{_serialize(node[1])}{'?[' if node[3] else '['}{_serialize(node[2])}]"
    return f"{node[1]}({', '.join(_serialize(a) for a in node[2])})"


# pyFormatNumber(x, 'N<d>'): Python's f"{x:,.<d>f}" (round half to even) in plain expressions.
# At an exact midpoint whose truncated digit is even, nudge x toward zero so formatNumber's
# away-from-zero rounding lands where Python's half-to-even does.
_PY_FORMAT = ("formatNumber(if(and(equals(mod(mul(<X>, <S>), 1), 0.5), equals(mod(sub(mul(<X>, <S>), 0.5), 2), 0)), "
              "sub(<X>, <E>), if(and(equals(mod(mul(<X>, <S>), 1), -0.5), equals(mod(add(mul(<X>, <S>), 0.5), 2), 0)), "
              "add(<X>, <E>), <X>)), <F>, 'en-US')")


def _macro(node):
    if node[0] == "index":
        return ("index", _macro(node[1]), _macro(node[2]), node[3])
    if node[0] != "call":
        return node
    args = [_macro(a) for a in node[2]]
    if node[1] == "pyFormatNumber":
        fmt = args[1][1] if args[1][0] == "lit" else None
        m = re.fullmatch(r"[Nn](\d+)", fmt or "")
        if not m:
            raise StudioBuildError("pyFormatNumber needs a literal 'N<digits>' format")
        d = int(m.group(1))
        text = (_PY_FORMAT.replace("<S>", str(10 ** d)).replace("<E>", f"{10.0 ** -(d + 3):.{d + 3}f}")
                .replace("<F>", _serialize(args[1])).replace("<X>", _serialize(args[0])))
        return _Parser(text).expr()
    return ("call", node[1], args)


def _eval(node, ctx):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "index":
        base = _eval(node[1], ctx)
        key = _eval(node[2], ctx)
        if base is None and node[3]:
            return None
        try:
            return base[key]
        except (KeyError, IndexError, TypeError):
            if node[3]:
                return None
            raise StudioBuildError(f"no {key!r} in {base!r}")
    name, args = node[1], node[2]
    if name == "if":
        return _eval(args[1], ctx) if _eval(args[0], ctx) else _eval(args[2], ctx)
    if name == "triggerBody":
        return ctx["trigger"]
    if name == "outputs":
        return ctx["outputs"][_eval(args[0], ctx)]
    if name == "parameters":
        return ctx["parameters"][_eval(args[0], ctx)]
    fn = FUNCTIONS.get(name)
    if fn is None:
        raise StudioBuildError(f"function {name}() is not modeled by the evaluator")
    return fn(*[_eval(a, ctx) for a in args])


def evaluate(expression, ctx):
    if not isinstance(expression, str) or not expression.startswith("@"):
        return expression
    return _eval(_Parser(expression[1:]).expr(), ctx)


# ── spec → flow.json ─────────────────────────────────────────────────────────

def _setting_param(schema_name, key, meta):
    env_schema = f"{schema_name.split('_')[0]}_{re.sub(r'[^A-Za-z0-9]', '', meta['display'].title())}"
    return f"{meta['display']} ({env_schema})", env_schema


def _expand(expr, spec, schema_name):
    """Spec shorthands → real flow expressions: input('x'), setting('X'), step('y')."""
    expr = re.sub(r"input\('([A-Za-z0-9_]+)'\)", r"triggerBody()?['\1']", expr)
    expr = re.sub(r"step\('([A-Za-z0-9_]+)'\)", r"outputs('\1')", expr)

    def setting(m):
        return "parameters('" + _setting_param(schema_name, m.group(1), spec["settings"][m.group(1)])[0].replace("'", "''") + "')"
    expr = re.sub(r"setting\('([A-Za-z0-9_]+)'\)", setting, expr)
    if not expr.startswith("@"):
        return expr
    return "@" + _serialize(_macro(_Parser(expr[1:]).expr()))


def compile_flow(spec, schema_name):
    """The flow.json Copilot Studio calls, in the proven HackerNews flow's shape."""
    props = {}
    for name, meta in spec["inputs"].items():
        p = {"title": name, "type": "string", "description": meta.get("description", ""),
             "x-ms-dynamically-added": True}
        if meta.get("type") == "number":
            p["x-ms-content-hint"] = "NUMBER"
        props[name] = p
    parameters = {"$connections": {"defaultValue": {}, "type": "Object"},
                  "$authentication": {"defaultValue": {}, "type": "SecureObject"}}
    for key, meta in spec.get("settings", {}).items():
        pname, env_schema = _setting_param(schema_name, key, meta)
        parameters[pname] = {"defaultValue": str(meta["default"]), "type": "String",
                             "metadata": {"schemaName": env_schema, "description": f"From the agent's {key} setting"}}
    actions, prev = {}, None
    for name, expr in spec.get("steps", []):
        actions[name] = {"type": "Compose", "inputs": _expand(expr, spec, schema_name),
                         "runAfter": {prev: ["Succeeded"]} if prev else {}}
        prev = name
    actions["Respond_to_agent"] = {
        "runAfter": {prev: ["Succeeded"]} if prev else {}, "type": "Response", "kind": "Skills",
        "inputs": {"statusCode": 200,
                   "body": {k: _expand(v, spec, schema_name) for k, v in spec["outputs"].items()},
                   "schema": {"type": "object", "properties": {k: {"type": "string"} for k in spec["outputs"]}}}}
    return {"properties": {"connectionReferences": {}, "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0", "parameters": parameters,
        "triggers": {"manual": {"type": "Request", "kind": "Skills",
                                "inputs": {"schema": {"type": "object", "properties": props,
                                                      "required": list(spec.get("required", spec["inputs"]))}}}},
        "actions": actions, "outputs": {}}, "templateName": ""}, "schemaVersion": "1.0.0.0"}


def tool_yaml(spec, workflow_id):
    outs = "".join(f"  - name: {k}\n" for k in spec["outputs"])
    ins = "".join(f"  - name: {k}\n    displayName: {k}\n    description: {json.dumps(m.get('description', k))}\n"
                  for k, m in spec["inputs"].items())
    return (f"mcs.metadata:\n  componentName: {json.dumps(spec['description'][:80] if spec.get('component') is None else spec['component'])}\n"
            f"  description: {json.dumps(spec['description'][:300])}\nkind: WorkflowTool\nworkflowId: {workflow_id}\n"
            f"toolOutputs:\n{outs}toolInputs:\n{ins}")


def run_flow(flow, trigger, parameter_values=None):
    """Evaluate a compiled flow.json for one trigger body. Returns the response body."""
    d = flow["properties"]["definition"]
    params = {k: v.get("defaultValue") for k, v in d["parameters"].items()}
    params.update(parameter_values or {})
    ctx = {"trigger": {k: (None if v is None else _str(v)) for k, v in trigger.items()},
           "outputs": {}, "parameters": params}
    for name, action in d["actions"].items():
        if action["type"] == "Compose":
            ctx["outputs"][name] = evaluate(action["inputs"], ctx)
        elif action["type"] == "Response":
            return {k: evaluate(v, ctx) for k, v in action["inputs"]["body"].items()}
    raise StudioBuildError("flow has no Response action")


# ── the parity proof ─────────────────────────────────────────────────────────

_RUN_AGENT = r"""
import importlib.util, json, os, sys, types
class BasicAgent:
    def __init__(self, name=None, metadata=None, *a, **k):
        self.name, self.metadata = name, metadata
pkg = types.ModuleType('agents'); sub = types.ModuleType('agents.basic_agent'); sub.BasicAgent = BasicAgent
pkg.basic_agent = sub; sys.modules['agents'] = pkg; sys.modules['agents.basic_agent'] = sub
spec = importlib.util.spec_from_file_location('agent_under_test', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cls = next(v for v in vars(m).values() if isinstance(v, type) and issubclass(v, BasicAgent) and v is not BasicAgent)
agent = cls()
for line in sys.stdin:
    case = json.loads(line)
    os.environ.update(case['env'])
    try:
        out = agent.perform(**case['args'])
        print(json.dumps({'ok': True, 'out': str(out)}), flush=True)
    except Exception as e:
        print(json.dumps({'ok': False, 'out': f'{type(e).__name__}: {e}'}), flush=True)
"""


def prove(spec, agent_file, schema_name, compare_output="result"):
    """Run the real agent.py and the compiled flow on every vector × setting value; report every mismatch."""
    flow = compile_flow(spec, schema_name)
    d = flow["properties"]["definition"]
    setting_cases = [{}]
    for key, meta in spec.get("settings", {}).items():
        values = [str(meta["default"])] + [str(v) for v in meta.get("test_values", [])]
        setting_cases = [dict(c, **{key: v}) for c in setting_cases for v in values]
    cases = [(vec, env) for env in setting_cases for vec in spec["vectors"]]
    lines = "".join(json.dumps({"args": vec, "env": {k: v for k, v in env.items()}}) + "\n" for vec, env in cases)
    proc = subprocess.run([sys.executable, "-c", _RUN_AGENT, str(agent_file)], input=lines,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise StudioBuildError(f"the agent could not be run for the proof: {proc.stderr.strip()[-400:]}")
    python_out = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    results = []
    for (vec, env), py in zip(cases, python_out):
        pvals = {}
        for key, v in env.items():
            pname, _ = _setting_param(schema_name, key, spec["settings"][key])
            pvals[pname] = v
        try:
            flow_out = _str(run_flow(flow, vec, pvals).get(compare_output))
        except StudioBuildError as e:
            flow_out = f"FLOW ERROR: {e}"
        results.append({"args": vec, "settings": env, "python": py["out"], "flow": flow_out,
                        "match": py["ok"] and py["out"] == flow_out})
    passed = sum(r["match"] for r in results)
    return {"agent": spec["agent"], "flow": spec["flow_name"], "cases": len(results), "passed": passed,
            "parity": passed == len(results) and len(results) > 0,
            "mismatches": [r for r in results if not r["match"]],
            "evaluator_note": "Flow side evaluated offline with .NET formatting semantics; confirm one case live.",
            "flow_json": flow, "_all": results}
