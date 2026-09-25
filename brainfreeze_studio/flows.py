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
import base64
import hashlib
import json
import math
import re
import subprocess
import sys
import urllib.parse
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
    g = re.fullmatch(r"([Gg])(\d+)", fmt or "")
    if g:                                       # .NET G<d>: Python's .<d>g, with the exponent letter in fmt's case
        text = f"{float(value):.{int(g.group(2))}g}"
        return text.replace("e", "E") if g.group(1) == "G" else text
    if fmt == "0":                              # custom format: a whole number, midpoints away from zero
        return str(int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
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


def _contains(collection, value):
    if isinstance(collection, str):
        return _str(value) in collection
    if isinstance(collection, dict):
        return value in collection
    if isinstance(collection, list):
        return value in collection
    raise StudioBuildError(f"contains() on {type(collection).__name__}")


def _format_datetime(ts, fmt):
    import datetime as _d
    dt = _d.datetime.fromisoformat(_str(ts).replace("Z", "+00:00"))
    tokens = [("yyyy", "%Y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("dddd", "%A"), ("dd", "%d"),
              ("HH", "%H"), ("mm", "%M"), ("ss", "%S")]
    out, i = "", 0
    while i < len(fmt):
        for tok, py in tokens:
            if fmt.startswith(tok, i):
                out += dt.strftime(py)
                i += len(tok)
                break
        else:
            if fmt[i] == "d":
                out += str(dt.day)
            else:
                out += fmt[i]
            i += 1
    return out


def _shift(ts, fmt=None, **delta):
    import datetime as _d
    if not isinstance(next(iter(delta.values())), int):
        raise StudioBuildError("addDays/addSeconds need a whole number")
    moved = _d.datetime.fromisoformat(_str(ts).replace("Z", "+00:00")) + _d.timedelta(**delta)
    return moved.isoformat() if fmt is None else _format_datetime(moved.isoformat(), fmt)


_FLOAT_TEXT = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*")


def _is_float(value, locale=None):
    # Only plain decimal text is modeled; a proof never relies on thousands separators or other locales.
    return isinstance(value, str) and bool(_FLOAT_TEXT.fullmatch(value))


def _substring(text, start, length=None):
    text, start = _str(text), int(start)
    if start < 0 or start > len(text) or (length is not None and start + int(length) > len(text)):
        raise StudioBuildError(f"substring({start}, {length}) is out of range for length {len(text)}")
    return text[start:] if length is None else text[start:start + int(length)]


FUNCTIONS = {
    "concat": lambda *a: "".join(_str(x) for x in a),
    "createArray": lambda *a: list(a),
    "contains": _contains,
    "replace": lambda text, old, new: _str(text).replace(_str(old), _str(new)),
    "base64ToString": lambda v: base64.b64decode(_str(v)).decode("utf-8"),
    "substring": _substring,
    "decodeUriComponent": lambda v: urllib.parse.unquote(_str(v)),
    "formatDateTime": _format_datetime,
    "isFloat": _is_float,
    "addDays": lambda ts, n, fmt=None: _shift(ts, days=n, fmt=fmt),
    "addSeconds": lambda ts, n, fmt=None: _shift(ts, seconds=n, fmt=fmt),
    "if": None,                                    # handled in _eval: both branches are evaluated
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


# pyFormatNumber(x, 'N<d>'): Python's f"{x:,.<d>f}" in plain expressions. Both round the double's exact binary
# value, so they differ only on an exact tie, where formatNumber rounds away from zero and Python to even. A double
# is an exact tie at <d> decimals only when x * 2^(d+1) is an odd integer (x = 0.125 at two decimals), and that
# product is exact because the factor is a power of two. Testing x * 10^d for a .5 fraction is not enough: 11.005 is
# 11.00500000000000078… in binary, so Python prints 11.01, yet 11.005 * 100 rounds to exactly 1100.5. At a true tie
# whose truncated digit is even, the flow formats the truncated value instead (x * 10^d is exact there too), keeping
# the sign of a negative value that truncates to zero, as Python prints -0.
_PY_FORMAT = ("formatNumber(if(and(equals(mod(mul(<X>, <P>), 2), 1.0), equals(mod(sub(mul(<X>, <S>), 0.5), 2), 0)), "
              "div(sub(mul(<X>, <S>), 0.5), <S>), if(and(equals(mod(mul(<X>, <P>), 2), -1.0), "
              "equals(mod(add(mul(<X>, <S>), 0.5), 2), 0)), mul(div(sub(mul(<X>, -<S>), 0.5), <S>), -1.0), <X>)), <F>, 'en-US')")


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
        text = (_PY_FORMAT.replace("<S>", f"{10 ** d}.0").replace("<P>", str(2 ** (d + 1)))
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
        # Power Automate evaluates every function argument, both branches included: evaluate both here too,
        # so a branch that would fail at runtime fails the proof instead of hiding behind the condition.
        cond, yes, no = _eval(args[0], ctx), _eval(args[1], ctx), _eval(args[2], ctx)
        return yes if cond else no
    if name == "triggerBody":
        return ctx["trigger"]
    if name == "utcNow":
        return ctx.get("now", "2026-09-24T09:30:00")
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


def _q(text):
    return "'" + str(text).replace("'", "''") + "'"


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n, width):
    out = ""
    for _ in range(width):
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out


def _state_expr(k):
    """Power Automate expression for the state one input is in (see materialize._states)."""
    from .materialize import ABSENT, EMPTY, UNKNOWN
    raw = f"triggerBody()?[{_q(k['input'])}]"
    empty = {"absent": ABSENT, "own": EMPTY, "unknown": UNKNOWN}.get(k.get("empty", "absent"), ABSENT)
    if k.get("boolean"):
        return f"if(equals({raw}, null), {_q(ABSENT)}, if(equals(toLower(string({raw})), 'true'), 'true', 'false'))"
    rule = k["rule"]
    if rule in ("exact", "canonical", "ci"):
        val = f"toLower(trim({raw}))" if rule == "ci" else raw
        arr = "createArray(" + ", ".join(_q(v) for v in k["values"]) + ")" if k["values"] else "createArray()"
        return (f"if(equals({raw}, null), {_q(ABSENT)}, if(equals({raw}, ''), {_q(empty)}, "
                f"if(contains({arr}, {val}), {val}, {_q(UNKNOWN)})))")
    if rule == "echo":
        return f"if(equals({raw}, null), {_q(ABSENT)}, if(equals({raw}, ''), {_q(empty)}, {_q(UNKNOWN)}))"
    if rule == "resolver":
        r = k["resolver"]
        if sorted(r["reachable"]) != sorted(r["order"]):
            raise StudioBuildError(f"{k['input']}: resolver keys {sorted(set(r['order']) - set(r['reachable']))} "
                                   "have no probe that reaches them")
        miss = _q(r["miss"] if r["miss"] is not None else UNKNOWN)
        q = f"toLower(trim({raw}))"
        chain = miss
        for key in reversed(r["order"]):
            name = (r["names"].get(key) or "").lower()
            chain = f"if(or(contains({q}, {_q(key)}), contains({_q(name)}, {q})), {_q(key)}, {chain})"
        absent = _q(r["default"] if r.get("absent_is_default", True) else ABSENT)
        return f"if(equals({raw}, null), {absent}, if(equals({raw}, ''), {_q(r['default'])}, {chain}))"
    if rule == "computed":
        return k["expr"]
    raise StudioBuildError(f"{k['input']}: rule {rule} is not compiled")


def _json_escape_expr(value):
    lf, cr, tab = "decodeUriComponent('%0A')", "decodeUriComponent('%0D')", "decodeUriComponent('%09')"
    v = f"replace(replace({value}, '\\', '\\\\'), '\"', '\\\"')"
    return f"replace(replace(replace({v}, {lf}, '\\n'), {cr}, '\\r'), {tab}, '\\t')"


FILL_STAGE_LIMIT = 6000


def _fill_expr(source, fills):
    expr = source
    for token, value in fills:
        expr = f"replace({expr}, {_q(token)}, {value})"
    return expr


def compile_materialized(spec, schema_name=None):
    """A materialized spec -> flow.json: per-input state expressions, a mixed-radix position per operation,
    a fixed-width index string of output ids, the outputs (base64, so nothing is read as an expression), and the
    unknown values substituted back into the output."""
    from .materialize import sentinel
    keying = {k["input"]: k for k in spec["keying"]}
    table_outputs = dict(spec["table_outputs"])
    table_keys = dict(spec["table_keys"])
    operations = dict(spec["operations"])
    for op, needs in (spec.get("blocked_operations") or {}).items():
        msg = (f"**Not available in this deployment:** `{op}` computes its answer from "
               f"{', '.join('`' + n + '`' for n in needs)}, and that calculation has no translation yet. "
               "No action was taken.")
        digest = "blocked-" + hashlib.sha256(msg.encode()).hexdigest()[:10]
        table_outputs[digest] = base64.b64encode(msg.encode("utf-8")).decode("ascii")
        table_keys[op] = digest
        operations[op] = []
    ids = sorted(table_outputs)
    number = {d: n for n, d in enumerate(ids)}
    width = 1
    while 36 ** width < len(ids) + 1:
        width += 1
    maps = {name: {st: n for n, st in enumerate(k["states"])} for name, k in keying.items()}
    ops = list(operations.items())
    primary = spec.get("primary")
    index, branches, offset = [], [], 0
    for op, relevant in ops:
        sizes = [len(keying[r]["states"]) for r in relevant]
        count = 1
        for n in sizes:
            count *= n
        for c in range(count):
            states, stride = [], 1
            for r, n in zip(relevant, sizes):
                states.append(keying[r]["states"][(c // stride) % n])
                stride *= n
            digest = table_keys.get("|".join([op] + states))
            if digest is None:
                raise StudioBuildError(f"{spec['agent']}: no output for {op}|{'|'.join(states)}")
            index.append(_b36(number[digest], width))
        terms, stride = [str(offset)], 1
        for r, n in zip(relevant, sizes):
            idx = f"int(coalesce(outputs('Maps')?[{_q(r)}]?[outputs({_q('State_' + r)})], 0))"
            terms.append(idx if stride == 1 else f"mul({idx}, {stride})")
            stride *= n
        expr = terms[0]
        for t in terms[1:]:
            expr = f"add({expr}, {t})"
        branches.append((op, expr))
        offset += count
    position = "0"
    for op, expr in reversed(branches):
        position = expr if (primary is None and op == "*") else f"if(equals(outputs({_q('State_' + primary)}), {_q(op)}), {expr}, {position})"
    text = (f"base64ToString(outputs('Outputs')?[substring(outputs('Index'), mul(outputs('Position'), {width}), {width})])")
    fills = []                                          # (placeholder in the output, what replaces it)
    for token, fmt in (spec.get("clock_formats") or {}).items():
        if isinstance(fmt, str):
            when, pa_format = "utcNow()", fmt
        else:
            pa_format = fmt["format"]
            when = (f"addDays(utcNow(), {fmt['days']})" if "days" in fmt
                    else f"addSeconds(utcNow(), {fmt['seconds']})")
        fills.append((token, f"formatDateTime({when}, {_q(pa_format)})"))
    for fill in spec.get("fills") or []:
        fills.append((fill["token"], fill["expr"]))
    for name, k in keying.items():
        if "\u27e8?\u27e9" in k["states"]:
            value = f"coalesce(triggerBody()?[{_q(name)}], '')"
            if k.get("json_echo"):
                value = _json_escape_expr(value)
            fills.append((sentinel(name), value))
    # one expression while it is short; a chain of Compose steps once it would near Power Automate's limit
    stages, current, source = [], [], "outputs('Text')"
    for fill in fills:
        if current and len(_fill_expr(source, current + [fill])) > FILL_STAGE_LIMIT:
            stages.append(current)
            current, source = [], f"outputs('Fill_{len(stages)}')"
        current.append(fill)
    stages.append(current)
    actions, prev = {}, None

    def add(name, inputs):
        nonlocal prev
        actions[name] = {"type": "Compose", "inputs": inputs, "runAfter": {prev: ["Succeeded"]} if prev else {}}
        prev = name
    for name, value in (spec.get("constants") or {}).items():
        add("Const_" + name, value)
    for name, expr in spec.get("derived") or []:
        add(name, "@" + expr)
    for name, k in keying.items():
        add("State_" + name, "@" + _state_expr(k))
    add("Maps", maps)
    add("Index", "".join(index))
    add("Outputs", {_b36(number[d], width): table_outputs[d] for d in ids})
    add("Position", "@" + position)
    add("Text", "@" + text)
    source = "outputs('Text')"
    for n, stage in enumerate(stages[:-1], 1):
        add(f"Fill_{n}", "@" + _fill_expr(source, stage))
        source = f"outputs('Fill_{n}')"
    result = _fill_expr(source, stages[-1])
    props = {}
    for name, meta in spec["inputs"].items():
        p_ = {"title": name, "type": "string", "description": meta.get("description", ""), "x-ms-dynamically-added": True}
        if meta.get("type") == "number":
            p_["x-ms-content-hint"] = "NUMBER"
        props[name] = p_
    actions["Respond_to_agent"] = {
        "runAfter": {prev: ["Succeeded"]}, "type": "Response", "kind": "Skills",
        "inputs": {"statusCode": 200, "body": {"result": "@" + result},
                   "schema": {"type": "object", "properties": {"result": {"type": "string"}}}}}
    return {"properties": {"connectionReferences": {}, "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                       "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
        "triggers": {"manual": {"type": "Request", "kind": "Skills",
                                "inputs": {"schema": {"type": "object", "properties": props,
                                                      "required": list(spec.get("required", []))}}}},
        "actions": actions, "outputs": {}}, "templateName": ""}, "schemaVersion": "1.0.0.0"}


EXPRESSION_LIMIT = 8192  # Power Automate rejects a longer expression


def _within_limits(flow):
    def walk(o, at):
        if isinstance(o, str):
            if o.startswith("@") and not o.startswith("@@") and len(o) > EXPRESSION_LIMIT:
                raise ValueError(f"{at} is a {len(o)}-character expression; Power Automate allows {EXPRESSION_LIMIT}")
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{at}/{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{at}[{i}]")
    walk(flow["properties"]["definition"]["actions"], "actions")
    return flow


def compile_flow(spec, schema_name):
    """The flow.json Copilot Studio calls, in the proven HackerNews flow's shape."""
    if spec.get("mode") == "materialized":
        return _within_limits(compile_materialized(spec, schema_name))
    return _within_limits(_compile_translated(spec, schema_name))


def _compile_translated(spec, schema_name):
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


TOOL_DESCRIPTION_LIMIT = 1000


def tool_description(spec):
    """What the orchestrator reads to choose and call the tool. A live Copilot Studio harness agent sees the tool
    description but not its input descriptions (24 Sep 2026, in a dev environment: the model guessed `triage`, `intake_review`, …
    for an operation whose input description began with its values), so a materialized tool's description ends with
    the values its selectors accept, and the prose is shortened to make room."""
    desc = (spec.get("description") or "").strip()
    if spec.get("mode") != "materialized":
        return desc[:TOOL_DESCRIPTION_LIMIT]
    keying = {k["input"]: k for k in spec.get("keying", [])}
    primary = spec.get("primary")
    order = ([primary] if primary in keying else []) + [n for n in keying if n != primary]
    parts = []
    for name in order:
        k = keying[name]
        if k.get("rule") in ("exact", "ci", "canonical") and k.get("values") and not k.get("boolean"):
            values = [str(v) for v in k["values"]]
            parts.append(f"Set `{name}` to exactly one of: {', '.join(values)}." if name == primary
                         else f"`{name}` values: {', '.join(values[:15])}{', …' if len(values) > 15 else ''}.")
    suffix = ""
    for part in parts:                     # the operation list first; later lists only while they fit
        if len(suffix) + len(part) + 1 <= TOOL_DESCRIPTION_LIMIT // 2 or not suffix:
            suffix += " " + part
    # the agent's own guidance on which operation answers what (its input description, minus the value list)
    guide = re.sub(r"\s*Values: .*$", "", ((spec.get("inputs") or {}).get(primary) or {}).get("description", "")).strip()
    guide = _clip(guide, min(350, TOOL_DESCRIPTION_LIMIT - len(suffix) - 250)) if guide else ""
    desc = _clip(desc, TOOL_DESCRIPTION_LIMIT - len(suffix) - (len(guide) + 1 if guide else 0))
    return " ".join(p for p in (desc, guide) if p) + suffix


def _clip(text, room):
    """At most room characters, cut at a sentence end when there is one."""
    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    cut = text[:room]
    return cut[:cut.rfind(". ") + 1] if ". " in cut else cut[:room - 1] + "…"


def tool_yaml(spec, workflow_id):
    outs = "".join(f"  - name: {k}\n" for k in spec["outputs"])
    ins = "".join(f"  - name: {k}\n    displayName: {k}\n    description: {json.dumps(m.get('description', k))}\n"
                  for k, m in spec["inputs"].items())
    return (f"mcs.metadata:\n  componentName: {json.dumps(spec['description'][:80] if spec.get('component') is None else spec['component'])}\n"
            f"  description: {json.dumps(tool_description(spec))}\nkind: WorkflowTool\nworkflowId: {workflow_id}\n"
            f"toolOutputs:\n{outs}toolInputs:\n{ins}")


def run_flow(flow, trigger, parameter_values=None, now=None):
    """Evaluate a compiled flow.json for one trigger body. Returns the response body."""
    d = flow["properties"]["definition"]
    params = {k: v.get("defaultValue") for k, v in d["parameters"].items()}
    params.update(parameter_values or {})
    ctx = {"trigger": {k: (None if v is None else _str(v)) for k, v in trigger.items()},
           "outputs": {}, "parameters": params, "now": now or "2026-09-24T09:30:00"}
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


def prove_materialized(spec, agent_file, basic_file, schema_name=None):
    """Every vector through the real agent.py (sandboxed, same frozen clock) and through the compiled flow."""
    from .materialize import CLOCKS, Runner, agent_python
    flow = compile_materialized(spec, schema_name)
    blocked = set(spec.get("blocked_operations") or {})
    primary = spec.get("primary")
    vectors = [v for v in spec["vectors"] if not (primary and v.get(primary) in blocked)]
    # a flow that fills dates from its clock is proven on every frozen clock, not just the one it was built on
    first = (spec.get("materialized_with") or {}).get("clock") or CLOCKS[0]
    clocks = [first] + ([c for c in CLOCKS if c != first] if spec.get("clock_formats") else [])
    runner = Runner(agent_file, basic_file, spec.get("class") or "",
                    python=agent_python((spec.get("materialized_with") or {}).get("python")))
    results = []
    for clock in clocks:
        for vec, py in zip(vectors, runner.run(vectors, clock=clock)):
            try:
                flow_out = _str(run_flow(flow, vec, now=clock).get("result"))
            except StudioBuildError as e:
                flow_out = f"FLOW ERROR: {e}"
            results.append({"args": vec, "clock": clock, "python": py, "flow": flow_out, "match": py == flow_out})
    passed = sum(r["match"] for r in results)
    return {"agent": spec["agent"], "flow": spec["flow_name"], "cases": len(results), "passed": passed,
            "clocks": clocks, "python": runner.python_version(),
            "parity": passed == len(results) and len(results) > 0, "mode": "materialized",
            "blocked_operations": spec.get("blocked_operations") or {},
            "approximated_inputs": [k["input"] for k in spec["keying"] if k.get("approximated")],
            "mismatches": [r for r in results if not r["match"]][:25],
            "evaluator_note": "Flow side evaluated offline; every declared input combination plus probes.",
            "flow_json": flow}


def prove(spec, agent_file, schema_name, compare_output="result", basic_file=None):
    """Run the real agent.py and the compiled flow on every vector × setting value; report every mismatch."""
    if spec.get("mode") == "materialized":
        if basic_file is None:
            raise StudioBuildError("a materialized proof needs the engine's basic_agent.py")
        return prove_materialized(spec, agent_file, basic_file, schema_name)
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
