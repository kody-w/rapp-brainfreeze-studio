"""Materialized translations: an agent flow that serves an agent's exact outputs, proven exhaustively.

Many agents are pure functions over bundled synthetic data: an ``operation`` enum, a few record
selectors, and deterministic formatting. For those, the most faithful Power Platform translation is
the agent's own behavior, materialized: run the real agent.py (sandboxed: network off, clock frozen)
over every input its contract declares, learn how each string input is matched, and lay a flow that
returns exactly what the Python returns.

    spec = materialize("prior_authorization_agent.py", "engine/agents/basic_agent.py")
    # spec["mode"] == "materialized"; build --translations lays it as an agent flow once its proof passes

How each string input is matched is learned by testing hypotheses against the real Python on many
probe strings (recognized values, case and whitespace variants, substrings, random text):

  exact     the value itself selects a record; anything else is "unknown"
  ci        the same, after lowercasing and trimming
  resolver  the library idiom ``q = s.lower().strip(); first key where key in q or q in name.lower()``,
            falling back to a default key; compiled to nested ``contains()`` checks
  echo      the value never selects anything; it is only printed back (a template substitution)
  ignored   the value never changes the output

Unknown values use sentinel templates: the output is computed once with a sentinel in place of the
value and the flow substitutes the real value back. That is only accepted when the substitution
reproduces the Python on every probe.

Inputs that drive computation (numbers that change the result, free text that is classified or
searched), a clock that changes the output, output that depends on call order, and hash-seed
nondeterminism are reported, never materialized: they need a hand translation (a translation spec
in expressions) or the MCP fallback.
"""
import ast
import base64
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path

ABSENT = "\u2205"            # ∅: the input was omitted
UNKNOWN = "\u27e8?\u27e9"    # ⟨?⟩: a value no record matches
EMPTY = "\u03b5"             # ε: an empty string, when the agent treats it unlike both omission and unknown
# Frozen clocks: the second checks that dates follow the clock; the third has a single-digit day and seconds,
# so a zero-padded day, an unpadded day and a printed time of day are told apart.
CLOCKS = ("2026-09-24T09:30:00", "2027-03-15T16:45:00", "2027-01-05T07:05:09")
MAX_CASES = 25000
NUMBER_PROBES = (0, 1, 2, 3, 5, 7, 10, 14, 30, 90, 365, 1000, 25000, 75000, -1, 2.5)
# Dates an agent prints from its clock: (how Python prints it, the same in Power Automate's formatDateTime)
DATE_FORMATS = (
    (lambda d: d.strftime("%Y-%m-%d %H:%M:%S"), "yyyy-MM-dd HH:mm:ss"),
    (lambda d: d.strftime("%Y-%m-%d %H:%M"), "yyyy-MM-dd HH:mm"),
    (lambda d: d.strftime("%Y-%m-%dT%H:%M:%S"), "yyyy-MM-ddTHH:mm:ss"),
    (lambda d: f"{d:%B} {d.day}, {d.year}", "MMMM d, yyyy"),
    (lambda d: f"{d:%b} {d.day}, {d.year}", "MMM d, yyyy"),
    (lambda d: d.strftime("%B %d, %Y"), "MMMM dd, yyyy"),
    (lambda d: d.strftime("%b %d, %Y"), "MMM dd, yyyy"),
    (lambda d: d.strftime("%m/%d/%Y"), "MM/dd/yyyy"),
    (lambda d: d.strftime("%Y-%m-%d"), "yyyy-MM-dd"),
)


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_MON = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# For each DATE_FORMATS entry: what it prints, how to read it back, and its precision (what an offset is counted in).
_DATE_SHAPES = (
    (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S", "seconds"),
    (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "%Y-%m-%d %H:%M", "minutes"),
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "%Y-%m-%dT%H:%M:%S", "seconds"),
    (rf"(?:{_MONTHS}) \d{{1,2}}, \d{{4}}", "%B %d, %Y", "days"),
    (rf"(?:{_MON}) \d{{1,2}}, \d{{4}}", "%b %d, %Y", "days"),
    (rf"(?:{_MONTHS}) \d{{2}}, \d{{4}}", "%B %d, %Y", "days"),
    (rf"(?:{_MON}) \d{{2}}, \d{{4}}", "%b %d, %Y", "days"),
    (r"\d{2}/\d{2}/\d{4}", "%m/%d/%Y", "days"),
    (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d", "days"),
)
# One scan finds every printed date; longer shapes first so a datetime is not read as a bare date.
_DATE_TOKEN = re.compile("(?<![0-9A-Za-z])(?:" + "|".join(_DATE_SHAPES[n][0] for n in (0, 2, 1, 3, 4, 7, 8))
                         + ")(?![0-9])")
# A clock that puts an offset date on a single-digit day in January: separates %d from an unpadded day and
# %B from %b, which the regular clocks cannot tell apart when their days are 10 or more.
_DISAMBIGUATION_DAY = "2027-01-05T09:30:00"


def clock_token(n, unit=None, k=0):
    if not k:
        return f"ZQCLOCK{n}QZ"
    return f"ZQCLOCK{n}{'D' if unit == 'days' else 'S'}{str(k).replace('-', 'M')}QZ"


def _split_dates(text):
    """(the text between printed dates, the printed dates)."""
    texts, dates, at = [], [], 0
    for m in _DATE_TOKEN.finditer(text):
        texts.append(text[at:m.start()])
        dates.append(m.group(0))
        at = m.end()
    texts.append(text[at:])
    return texts, dates


def _offset_fits(strings, clocks):
    """Every (format index, unit, offset, literal suffix) under which each clock prints its string as clock + offset:
    today + k days, now + k seconds, or today + k days at a fixed time of day (the time is kept as text)."""
    import datetime as _d
    fits = []
    for n, (rx, py, precision) in enumerate(_DATE_SHAPES):
        if not all(re.fullmatch(rx, s) for s in strings):
            continue
        try:
            parsed = [_d.datetime.strptime(s, py) for s in strings]
        except ValueError:
            continue
        tries = []
        if precision == "days":
            tries.append((n, "days", {(p.date() - c.date()).days for p, c in zip(parsed, clocks)}, ""))
        else:
            base = [c.replace(second=0, microsecond=0) if precision == "minutes" else c for c in clocks]
            tries.append((n, "seconds", {int((p - b).total_seconds()) for p, b in zip(parsed, base)}, ""))
            if len({s[10:] for s in strings}) == 1:     # a date that follows the clock, at a fixed time of day
                tries.append((8, "days", {(p.date() - c.date()).days for p, c in zip(parsed, clocks)}, strings[0][10:]))
        for m, unit, ks, suffix in tries:
            if len(ks) != 1:
                continue
            k = next(iter(ks))
            shift = _d.timedelta(**{unit: k})
            if all(DATE_FORMATS[m][0](c + shift) + suffix == s for c, s in zip(clocks, strings)):
                fits.append((m, unit, k, suffix))
    return fits


def _clock_template(runs, clocks=CLOCKS, rerun=None):
    """runs[c][j] is case j's output at clocks[c]. When the only differences between clocks are dates printed in
    known formats at a fixed offset from the clock (today, or today + k days), return (templates, formats) with
    ZQCLOCK… placeholders; else None. rerun(case indexes, clock) settles a format the clocks leave ambiguous."""
    import datetime as _d
    cl = [_d.datetime.fromisoformat(x) for x in clocks]
    pending = []                            # per case: its texts and, per date, a literal or its candidate fits
    for outs in zip(*runs):
        if all(o == outs[0] for o in outs[1:]):
            pending.append(outs[0])
            continue
        split = [_split_dates(o) for o in outs]
        if any(s[0] != split[0][0] or len(s[1]) != len(split[0][1]) for s in split[1:]):
            return None
        slots = []
        for i in range(len(split[0][1])):
            strings = [s[1][i] for s in split]
            if all(x == strings[0] for x in strings[1:]):
                slots.append(strings[0])
                continue
            fits = _offset_fits(strings, cl)
            if not fits:
                return None
            slots.append(fits)
        pending.append((split[0][0], slots))
    # a date the clocks cannot place in one format: run its case once more on a clock that tells them apart
    unsettled = {}
    for j, p in enumerate(pending):
        if isinstance(p, tuple):
            for fits in p[1]:
                if isinstance(fits, list) and len({(DATE_FORMATS[n][1], x) for n, _, _, x in fits}) > 1:
                    unsettled.setdefault((fits[0][1], fits[0][2]), []).append(j)
    for (unit, k), cases in unsettled.items():
        if rerun is None:
            return None
        extra = _d.datetime.fromisoformat(_DISAMBIGUATION_DAY) - _d.timedelta(**{unit: k})
        for j, out in zip(cases, rerun(cases, extra.isoformat())):
            texts, dates = _split_dates(out)
            if texts != pending[j][0] or len(dates) != len(pending[j][1]):
                return None
            for i, fits in enumerate(pending[j][1]):
                if isinstance(fits, list):
                    kept = [(n, u, kk, x) for n, u, kk, x in fits
                            if DATE_FORMATS[n][0](extra + _d.timedelta(**{u: kk})) + x == dates[i]]
                    if not kept:
                        return None
                    pending[j][1][i] = kept
    used, templates = {}, []
    for p in pending:
        if isinstance(p, str):
            templates.append(p)
            continue
        texts, slots = p
        out = texts[0]
        for i, fits in enumerate(slots):
            if isinstance(fits, list):
                n, unit, k, suffix = fits[0]
                token = clock_token(n, unit, k)
                used[token] = DATE_FORMATS[n][1] if not k else {"format": DATE_FORMATS[n][1], unit: k}
                out += token + suffix
            else:
                out += fits
            out += texts[i + 1]
        templates.append(out)
    return templates, used


_PLACEHOLDER = re.compile(r"ZQ[A-Z0-9]+QZ")
_NUMBER = re.compile(r"(?<![0-9A-Za-z])-?\d+(?:\.\d+)?(?:e[+-]\d+)?|(?<=[A-Za-z-])\d+(?:\.\d+)?")


def _number_spans(text):
    """Where the numbers are in text, leaving out the digits of printed dates and ZQ…QZ placeholders."""
    skip = [m.span() for m in _DATE_TOKEN.finditer(text)] + [m.span() for m in _PLACEHOLDER.finditer(text)]
    return [m.span() for m in _NUMBER.finditer(text) if not any(a <= m.start() < b for a, b in skip)]


def _between(text, spans):
    at, out = 0, []
    for a, b in spans:
        out.append(text[at:a])
        at = b
    return out + [text[at:]]


def _holes(example, marker):
    """Which numbers (indexes into _number_spans) differ between two runs that differ only in a number input;
    None when anything else differs."""
    se, sm = _number_spans(example), _number_spans(marker)
    if len(se) != len(sm) or _between(example, se) != _between(marker, sm):
        return None
    return [k for k, ((a, b), (c, d)) in enumerate(zip(se, sm)) if example[a:b] != marker[c:d]]


def _punch(template, holes, tokens):
    spans = _number_spans(template)
    for k, token in sorted(zip(holes, tokens), reverse=True):
        a, b = spans[k]
        template = template[:a] + token + template[b:]
    return template


class MaterializeError(RuntimeError):
    pass


def sentinel(name):
    """An ASCII placeholder that survives str(), repr() and json.dumps() unchanged."""
    return "ZQ" + re.sub(r"[^A-Za-z0-9]", "", name).upper() + "QZ"


def json_escaped(value):
    """How json.dumps prints a string between its quotes (ensure_ascii=True)."""
    return json.dumps(value)[1:-1]


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ── the sandboxed runner ─────────────────────────────────────────────────────

RUNNER = r'''
import datetime as _dt, importlib.util, json, os, socket, sys, time as _time, types

agent_file, basic_file, clock_iso, class_name = sys.argv[1:5]

def _blocked(*a, **k):
    raise OSError("network is off while this agent is materialized")
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

_FROZEN = _dt.datetime.fromisoformat(clock_iso)
_real_dt, _real_date = _dt.datetime, _dt.date
class _FrozenDateTime(_real_dt):
    @classmethod
    def now(cls, tz=None):
        base = _FROZEN if tz is None else _FROZEN.replace(tzinfo=_dt.timezone.utc).astimezone(tz)
        return cls(base.year, base.month, base.day, base.hour, base.minute, base.second, base.microsecond, base.tzinfo)
    @classmethod
    def utcnow(cls):
        return cls(*_FROZEN.timetuple()[:6])
    @classmethod
    def today(cls):
        return cls(*_FROZEN.timetuple()[:6])
class _FrozenDate(_real_date):
    @classmethod
    def today(cls):
        return cls(_FROZEN.year, _FROZEN.month, _FROZEN.day)
_dt.datetime, _dt.date = _FrozenDateTime, _FrozenDate
_epoch = _FROZEN.replace(tzinfo=_dt.timezone.utc).timestamp()
_time.time = lambda: _epoch

spec = importlib.util.spec_from_file_location("basic_agent", basic_file)
basic = importlib.util.module_from_spec(spec); spec.loader.exec_module(basic)
sys.modules["basic_agent"] = basic
pkg = types.ModuleType("agents"); pkg.__path__ = []; pkg.basic_agent = basic
sys.modules["agents"] = pkg; sys.modules["agents.basic_agent"] = basic
sys.path.insert(0, os.path.dirname(os.path.abspath(agent_file)))

spec = importlib.util.spec_from_file_location("agent_under_test", agent_file)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
classes = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, basic.BasicAgent)
           and v is not basic.BasicAgent and v.__module__ == "agent_under_test"]
if class_name:
    classes = [c for c in classes if c.__name__ == class_name]
if not classes:
    raise SystemExit("no agent class found")
agent = classes[0]()

def _dicts():
    out = {}
    for name, value in vars(mod).items():
        if name.startswith("__") or not isinstance(value, dict) or not value:
            continue
        if not all(isinstance(k, str) for k in value):
            continue
        out[name] = {"keys": list(value)[:200],
                     "names": {k: v.get("name") for k, v in list(value.items())[:200]
                               if isinstance(v, dict) and isinstance(v.get("name"), str)}}
    return out

for line in sys.stdin:
    req = json.loads(line)
    if req.get("op") == "contract":
        md = getattr(agent, "metadata", None) or {}
        print(json.dumps({"class": type(agent).__name__, "name": md.get("name") or getattr(agent, "name", None),
                          "description": md.get("description", ""), "parameters": md.get("parameters") or {},
                          "dicts": _dicts()}, default=str), flush=True)
        continue
    try:
        out = agent.perform(**req["args"])
        print(json.dumps({"ok": True, "out": out if isinstance(out, str) else json.dumps(out, default=str)}), flush=True)
    except Exception as e:
        print(json.dumps({"ok": False, "out": f"{type(e).__name__}: {e}"}), flush=True)
'''


def agent_python(version=None):
    """The interpreter to run agent code under. An agent's output can depend on the Python version (3.12 changed
    how sum() adds floats, which moves a rounded percentage), so a proof runs under the minor version its spec was
    materialized with: BFS_AGENT_PYTHON when set, else this interpreter if it matches, else pythonX.Y on PATH, else
    this interpreter. Materialize with the brainstem engine's Python (3.11 for the grail)."""
    override = os.environ.get("BFS_AGENT_PYTHON")
    if override:
        return override
    if version:
        minor = ".".join(str(version).split(".")[:2])
        if ".".join(map(str, sys.version_info[:2])) == minor:
            return sys.executable
        found = shutil.which(f"python{minor}")
        if found:
            return found
    return sys.executable


class Runner:
    """Runs one agent file's perform() in a sandboxed subprocess, a batch of cases at a time."""

    def __init__(self, agent_file, basic_file, class_name="", python=None, timeout=600):
        self.agent_file, self.basic_file = str(agent_file), str(basic_file)
        self.class_name, self.python, self.timeout = class_name or "", python or sys.executable, timeout
        self.calls = 0
        self._version = None

    def python_version(self):
        if self._version is None:
            self._version = subprocess.run([self.python, "-c", "import platform; print(platform.python_version())"],
                                           capture_output=True, text=True, timeout=60).stdout.strip()
        return self._version

    def _exchange(self, requests, clock=CLOCKS[0], hashseed="0"):
        env = {"PYTHONHASHSEED": str(hashseed), "PATH": "/usr/bin:/bin", "HOME": str(Path(self.agent_file).parent)}
        payload = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([self.python, "-c", RUNNER, self.agent_file, self.basic_file, clock, self.class_name],
                              input=payload, capture_output=True, text=True, timeout=self.timeout, env=env)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if proc.returncode != 0 or len(lines) != len(requests):
            raise MaterializeError(f"{Path(self.agent_file).name}: runner failed "
                                   f"({len(lines)}/{len(requests)} answers): {proc.stderr.strip()[-600:]}")
        self.calls += len(requests)
        return [json.loads(l) for l in lines]

    def contract(self):
        return self._exchange([{"op": "contract"}])[0]

    def run(self, cases, clock=CLOCKS[0], hashseed="0"):
        """cases: list of args dicts. Returns the list of output strings (errors included, prefixed)."""
        if not cases:
            return []
        answers = self._exchange([{"op": "perform", "args": c} for c in cases], clock, hashseed)
        return [a["out"] if a["ok"] else "\u26a0 " + a["out"] for a in answers]


# ── discovery ────────────────────────────────────────────────────────────────

def _string_constants(source, limit=64):
    seen, out = set(), []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if 1 <= len(s) <= limit and "\n" not in s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _random_strings(seed, n=6):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        out.append("".join(rng.choice(string.ascii_letters + " -_'") for _ in range(rng.randint(3, 14))).strip() or "q")
    return out + ["Zyqx Unmatched 42", "the 'quoted' one", "Ünïcødé ✓"]


def _variants(values):
    out = []
    for v in values:
        for w in (v.lower(), v.upper(), v.title(), f"  {v} ", f"\t{v}\n", f"the {v} one", v[: max(1, len(v) // 2)],
                  v[len(v) // 3:], v + "x"):
            if w not in out:
                out.append(w)
    return out


def _kind(schema):
    t = (schema or {}).get("type")
    if (schema or {}).get("enum"):
        return "string"
    if t == "boolean":
        return "boolean"
    if t in ("number", "integer"):
        return "number"
    if t in ("array", "object"):
        return "structured"
    return "string"


class _Resolver:
    """The library resolver idiom: empty -> a default key; else q = s.lower().strip() and the first key where
    ``key in q or q in name.lower()``; else ``miss`` (another key, or None for "unknown")."""

    def __init__(self, order, names, default, miss):
        self.order, self.names, self.default, self.miss = order, names, default, miss

    def __call__(self, raw):
        if raw is None or raw == "":
            return self.default
        q = raw.lower().strip()
        for k in self.order:
            if k in q or q in self.names.get(k, "").lower():
                return k
        return self.miss


def _norm(rule, raw):
    return raw.lower().strip() if rule == "ci" else raw


class Discovery:
    """What each input of one agent does, learned from the real Python."""

    def __init__(self, runner, source):
        self.r, self.source = runner, source
        self.contract = runner.contract()
        params = self.contract.get("parameters") or {}
        self.props = params.get("properties") or {}
        self.required = [n for n in (params.get("required") or []) if n in self.props]
        self.constants = _string_constants(source)
        self.inputs = {}
        self.capped = False

    def _contexts(self, cap=150):
        """Representative states of every keyed input learned so far (so rules are learned in context)."""
        ctx = [{}]
        for name, info in self.inputs.items():
            if info["rule"] not in ("exact", "ci", "resolver", "canonical") and not info.get("boolean"):
                continue
            reps = [v for st, v in _states(info) if st != UNKNOWN]
            ctx = [dict(c, **({name: v} if v is not None else {})) for c in ctx for v in reps]
            if len(ctx) > cap:
                self.capped = True
                return ctx[:cap]
        return ctx

    def _fill(self, name, contexts, values, table):
        cases, idx = [], []
        for ci, ctx in enumerate(contexts):
            for v in values:
                if (ci, v) in table:
                    continue
                args = dict(ctx)
                if v is not None:
                    args[name] = v
                cases.append(args)
                idx.append((ci, v))
        for key, out in zip(idx, self.r.run(cases)):
            table[key] = out

    def _check(self, name, contexts, table, probes, rule, canon_map=None, resolver=None, empty_is_absent=False,
               absent_is_default=True):
        """Does `rule` reproduce the Python on every probe (and on omission)?
        Returns (ok, counterexample, flags); flags["json"] means unknown values are echoed JSON-escaped."""
        s = sentinel(name)
        flags = {"json": False, "non_ascii": False}
        for ci in range(len(contexts)):
            absent, tmpl = table[(ci, None)], table[(ci, s)]
            for v in probes:
                got = table[(ci, v)]
                unknown = False
                if rule == "ignored":
                    want = absent
                elif rule == "resolver":
                    k = resolver(v)
                    if k is None:
                        unknown, want = True, tmpl.replace(s, v)
                    else:
                        rep = resolver.rep.get(k)
                        if rep is None:
                            return False, (contexts[ci], v), flags
                        want = table[(ci, rep)]
                elif v == "" and empty_is_absent in ("absent", "own", True):
                    want = absent if empty_is_absent in ("absent", True) else table[(ci, "")]
                else:
                    hit = (canon_map or {}).get(_norm(rule, v))
                    if hit is not None:
                        want = table[(ci, hit)]
                    else:
                        unknown, want = True, tmpl.replace(s, v)
                if got != want:
                    if unknown and got == tmpl.replace(s, json_escaped(v)):
                        flags["json"] = True
                        if any(ord(ch) > 127 for ch in v):
                            flags["non_ascii"] = True
                        continue
                    return False, (contexts[ci], v), flags
            if rule == "resolver" and absent_is_default and absent != table[(ci, resolver.rep[resolver.default])]:
                return False, (contexts[ci], None), flags
        return True, None, flags

    def _learn_string(self, name, contexts):
        schema = self.props.get(name) or {}
        s = sentinel(name)
        enum = [str(v) for v in schema.get("enum") or []]
        table = {}
        first = list(dict.fromkeys([None, s] + enum + self.constants + _random_strings(name) + ["", "   "]))
        self._fill(name, contexts, first, table)
        n = range(len(contexts))
        firsts = [v for v in first[2:] if any(table[(ci, v)] != table[(ci, s)].replace(s, v) for ci in n)]
        dict_words = []
        for d in self.contract.get("dicts", {}).values():
            if len(d["keys"]) <= 60:
                dict_words += [k for k in d["keys"] if isinstance(k, str)] + [v for v in d["names"].values() if v]
        probes = list(dict.fromkeys(first[2:] + _variants([v for v in firsts if v.strip()][:40])
                                    + _variants(dict_words[:40])))
        self._fill(name, contexts, probes, table)
        recognized = [v for v in probes if any(table[(ci, v)] != table[(ci, s)].replace(s, v) for ci in n)]
        if all(table[(ci, "")] == table[(ci, None)] for ci in n):
            empty_mode = "absent"
        elif all(table[(ci, "")] == table[(ci, s)].replace(s, "") for ci in n):
            empty_mode = "unknown"
        else:
            empty_mode = "own"
        empty_is_absent = empty_mode
        echoing = any(s in table[(ci, s)] for ci in n)
        info = {"name": name, "kind": "string", "echo": echoing, "empty_mode": empty_mode}
        if not recognized:
            rule = "ignored" if all(table[(ci, s)] == table[(ci, None)] for ci in n) and not echoing else "echo"
            ok, _, fl = self._check(name, contexts, table, probes, "ignored" if rule == "ignored" else "exact", {},
                                    empty_is_absent=empty_is_absent)
            if ok:
                info.update(rule=rule, json_echo=fl["json"])
                return info
        # canonical values come only from the agent's own constants and enum; variants are held out
        for rule in ("exact", "ci"):
            canon = {}
            for v in firsts:
                if v.strip():
                    canon.setdefault(_norm(rule, v), v)
            ok, cx, fl = self._check(name, contexts, table, probes, rule, canon, empty_is_absent=empty_is_absent)
            if ok:
                info.update(rule=rule, canon=sorted(canon), canon_map=canon, json_echo=fl["json"])
                return info
            info.setdefault("attempts", {})[rule] = cx
        for dname, d in self.contract.get("dicts", {}).items():
            order = [k for k in d["keys"] if isinstance(k, str)]
            if not order or len(order) > 60:
                continue
            names = {k: (d["names"].get(k) or "") for k in order}
            self._fill(name, contexts, order + [v for v in names.values() if v], table)
            for default in order:
                for miss in [None] + order:
                    res = _Resolver(order, names, default, miss)
                    res.rep = {}
                    for k in order:
                        for cand in (k, names[k]):
                            if cand and res(cand) == k:
                                res.rep[k] = cand
                                break
                    if default not in res.rep or (miss is not None and miss not in res.rep):
                        continue
                    for absent_is_default in (True, False):
                        ok, _, fl = self._check(name, contexts, table, probes, "resolver", resolver=res,
                                                absent_is_default=absent_is_default)
                        if ok:
                            info.update(rule="resolver", resolver=res, dict=dname,
                                        canon=[k for k in order if k in res.rep],
                                        absent_is_default=absent_is_default, json_echo=fl["json"])
                            return info
        # canonical mode (approximated): one representative per distinct behavior, from the agent's own values
        if firsts and not all(v.strip() == "" for v in firsts):
            enum = [str(v) for v in (self.props.get(name) or {}).get("enum") or []]
            keys = [k for d in self.contract.get("dicts", {}).values() for k in d["keys"] if isinstance(k, str)]
            groups = {}
            for v in firsts:
                if not v.strip():
                    continue
                sig = tuple(table[(ci, v)] for ci in n)
                groups.setdefault(sig, []).append(v)
            canon = {}
            for vals in groups.values():
                rep = next((v for v in vals if v in enum), None) or next((v for v in vals if v in keys), None) \
                    or min(vals, key=lambda v: (len(v), v))
                canon[rep] = rep
            if len(canon) <= 40:
                info.update(rule="canonical", canon=sorted(canon), canon_map=canon, approximated=True,
                            why_approx="fuzzy matching is left to the model: the tool lists the known values")
                return info
        info.update(rule="computational", recognized=recognized[:20],
                    why="this text is searched, classified or transformed; no match rule reproduces it")
        return info

    def _learn_number(self, name, contexts):
        """Numbers are probed jointly: the other number inputs take random values too, so a result that only
        appears when several numbers are set together (household size AND income) is not missed."""
        others = [n for n in self.props if n != name and _kind(self.props.get(n)) == "number"]
        rng = random.Random(f"num:{name}")
        pool = [None] + list(NUMBER_PROBES)
        cases, pairs = [], []
        for ci, ctx in enumerate(contexts):
            for _ in range(24 if others else 1):
                base = dict(ctx, **{o: v for o in others for v in [rng.choice(pool)] if v is not None})
                for v in (None,) + NUMBER_PROBES:
                    args = dict(base)
                    if v is not None:
                        args[name] = v
                    cases.append(args)
                pairs.append(len(NUMBER_PROBES) + 1)
        outs = self.r.run(cases)
        i = 0
        for width in pairs:
            chunk = outs[i:i + width]
            i += width
            if any(o != chunk[0] for o in chunk[1:]):
                return {"name": name, "kind": "number", "rule": "computational", "why": "the number changes the result"}
        return {"name": name, "kind": "number", "rule": "ignored"}

    def _learn_structured(self, name, contexts):
        empty = [] if (self.props.get(name) or {}).get("type") == "array" else {}
        outs = self.r.run([dict(c) for c in contexts] + [dict(c, **{name: empty}) for c in contexts])
        k = len(contexts)
        if all(outs[i] == outs[i + k] for i in range(k)):
            return {"name": name, "kind": "structured", "rule": "ignored"}
        return {"name": name, "kind": "structured", "rule": "computational", "why": "structured input changes the result"}

    def learn(self):
        order = [n for n in self.required if _kind(self.props.get(n)) == "string"] + list(self.props)
        for name in dict.fromkeys(order):
            kind = _kind(self.props.get(name))
            contexts = [{}] if name in self.required and kind == "string" else self._contexts()
            if kind == "string":
                self.inputs[name] = self._learn_string(name, contexts)
            elif kind == "number":
                self.inputs[name] = self._learn_number(name, contexts)
            elif kind == "boolean":
                self.inputs[name] = {"name": name, "kind": "boolean", "rule": "exact", "boolean": True,
                                     "canon": ["true", "false"], "canon_map": {"true": True, "false": False}}
            else:
                self.inputs[name] = self._learn_structured(name, contexts)
        return self


# ── materialization ──────────────────────────────────────────────────────────

def _states(info):
    """The states one keyed input can be in, each with the probe value that produces it."""
    rule = info["rule"]
    if rule == "resolver":
        res = info["resolver"]
        states = [(k, res.rep[k]) for k in res.order if k in res.rep]
        if res.miss is None:
            states.append((UNKNOWN, sentinel(info["name"])))
        return states + ([] if info.get("absent_is_default", True) else [(ABSENT, None)])
    if info.get("boolean"):
        return [("true", True), ("false", False), (ABSENT, None)]
    empty = [(EMPTY, "")] if info.get("empty_mode") == "own" else []
    if rule in ("exact", "ci", "canonical"):
        return ([(k, info["canon_map"][k]) for k in info["canon"]] + [(UNKNOWN, sentinel(info["name"])), (ABSENT, None)]
                + empty)
    if rule == "echo":
        return [(UNKNOWN, sentinel(info["name"])), (ABSENT, None)] + empty
    return []


def _key_inputs(disc):
    return [i for i in disc.inputs.values()
            if i["rule"] in ("exact", "ci", "canonical", "resolver", "echo") or i.get("boolean")]


def _sub(out, keyed, states, args):
    """Put the real unknown values back where the sentinels were."""
    for i, st in zip(keyed, states):
        if st == UNKNOWN:
            val = args.get(i["name"], "")
            out = out.replace(sentinel(i["name"]), json_escaped(val) if i.get("json_echo") else val)
    return out


def materialize(agent_file, basic_file, *, class_name="", flow_name=None, component=None, python=None,
                check_clock=True, check_order=True, samples=600, allow_hash_variation=False, hand=None):
    """Learn, run and verify one agent. Returns (spec, report); spec is None when it cannot be materialized.

    The table is per operation: for each state of the primary input (the required selector, usually
    ``operation``) only the inputs that change that operation's output are keyed. Relevance is found by
    varying one input at a time, then confirmed by random samples over the full input domain; an input that
    turns out to matter is added and the operation is rebuilt."""
    agent_file = Path(agent_file)
    source = agent_file.read_text(encoding="utf-8")
    source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    hand = hand or {}
    if hand.get("source_sha256") and hand["source_sha256"] != source_sha:
        raise MaterializeError(f"{agent_file.name}: its hand translation was written for a different source "
                               f"({hand['source_sha256'][:12]}, now {source_sha[:12]})")
    hand_ops = hand.get("operations") or {}
    runner = Runner(agent_file, basic_file, class_name, python)
    disc = Discovery(runner, source).learn()
    c = disc.contract
    report = {"agent": c.get("name"), "class": c.get("class"), "file": agent_file.name,
              "inputs": {n: {k: v for k, v in i.items() if k in ("kind", "rule", "why", "canon", "dict", "echo",
                                                              "approximated", "empty_mode")}
                         for n, i in disc.inputs.items()},
              "materialized": False, "reasons": []}
    keyed = _key_inputs(disc)
    computational = {n: i for n, i in disc.inputs.items() if i["rule"] == "computational"}
    primary = next((i for i in keyed if i["name"] in disc.required and i["rule"] in ("exact", "ci", "canonical")), None)
    rest = [i for i in keyed if i is not primary]
    pstates = _states(primary) if primary else [("*", None)]
    rng = random.Random(f"materialize:{agent_file.name}")
    blockers = []

    def args_for(pv, assignment):
        """assignment: {input name: (state, value)}; a value of None means the input is omitted."""
        a = {} if pv is None else {primary["name"]: pv}
        for name_, (st, v) in assignment.items():
            if v is not None:
                a[name_] = v
        return a

    def full_states(i):
        return _states(i)

    ops, cases_all, table_rows = {}, [], {}
    hand_keying, hand_holes, hand_vectors = {}, {}, []
    for pst, pv in pstates:
        # which inputs change this operation's output (one at a time, others omitted)
        base = runner.run([args_for(pv, {})])[0]
        relevant = []
        probe_cases, owners = [], []
        for i in rest:
            for st, v in full_states(i):
                probe_cases.append(args_for(pv, {i["name"]: (st, v)}))
                owners.append(i)
        if probe_cases:
            for i, out in zip(owners, runner.run(probe_cases)):
                if out != base and i not in relevant:
                    relevant.append(i)
        # numbers that change this operation block it (they need a hand translation); probed jointly
        num_blockers = []
        nums = [n for n, info in computational.items() if info["kind"] == "number"]
        if nums:
            trials = []
            for _ in range(48):
                trials.append(dict(args_for(pv, {}), **{n: v for n in nums for v in [rng.choice((None,) + NUMBER_PROBES)]
                                                        if v is not None}))
            if any(o != base for o in runner.run(trials)):
                for n in nums:
                    solo = [dict(t, **{n: v}) for t in trials[:12] for v in (1, 4, 65, 30000)]
                    ref = runner.run([{k: w for k, w in t.items() if k != n} for t in trials[:12] for _ in range(4)])
                    if runner.run(solo) != ref:
                        num_blockers.append(n)
                num_blockers = num_blockers or nums
        num_blockers += [n for n, info in computational.items() if info["kind"] != "number"]
        if num_blockers and pst in hand_ops:
            rows, cases, vectors = _hand_operation(runner, hand_ops[pst], pst, pv, relevant, rest, args_for,
                                                   hand_keying, hand_holes, rng)
            ops[pst] = {"relevant": [i["name"] for i in relevant] + [hand_ops[pst]["computed"]["name"]],
                        "blocked_by": [], "hand": True}
            table_rows.update(rows)
            cases_all.extend(cases)
            hand_vectors.extend(vectors)
            continue
        for attempt in range(4):
            combos = [([], {})]
            for i in relevant:
                combos = [(sts + [st], dict(asg, **{i["name"]: (st, v)})) for sts, asg in combos for st, v in full_states(i)]
            if len(combos) > MAX_CASES:
                blockers.append(f"{pst}: {len(combos)} combinations over {[i['name'] for i in relevant]}")
                break
            cases = [args_for(pv, asg) for _, asg in combos]
            outs = runner.run(cases)
            rows = {"|".join([pst] + sts): o for (sts, _), o in zip(combos, outs)}
            # verify on random samples over every keyed input (not just the relevant ones)
            sample_cases, sample_keys = [], []
            for _ in range(max(1, samples // max(1, len(pstates)))):
                asg, key_states, subst = {}, [], []
                for i in rest:
                    st, v = rng.choice(full_states(i))
                    if st == UNKNOWN:
                        v = rng.choice(["Zyqx Unmatched 42", "plain words here", "the 'quoted' one"])
                    asg[i["name"]] = (st, v)
                a = args_for(pv, asg)
                sample_cases.append(a)
                sample_keys.append(("|".join([pst] + [asg[i["name"]][0] for i in relevant]), a))
            got = runner.run(sample_cases)
            bad = None
            for (key, a), g in zip(sample_keys, got):
                want = rows.get(key)
                if want is None:
                    bad = (a, "no row")
                    break
                # substitute sentinels of relevant unknown inputs
                for i in relevant:
                    vi = a.get(i["name"])
                    if vi is not None and all(vi != v for st, v in full_states(i) if st != UNKNOWN):
                        want = want.replace(sentinel(i["name"]), json_escaped(vi) if i.get("json_echo") else vi)
                if g != want:
                    bad = (a, "mismatch")
                    break
            if bad is None:
                break
            # find the input that matters but was missed, add it, rebuild
            a, _ = bad
            first, again = runner.run([a, a])
            if first != again:
                blockers.append("the same call gives different answers (the agent keeps state between calls "
                                "or is nondeterministic)")
                break
            missed = None
            for i in rest:
                if i in relevant or i["name"] not in a:
                    continue
                trial = dict(a)
                trial.pop(i["name"])
                if runner.run([trial])[0] != runner.run([a])[0]:
                    missed = i
                    break
            if missed is None:
                blockers.append(f"{pst}: inputs interact in a way the table does not capture ({json.dumps(a)[:160]})")
                break
            relevant.append(missed)
        else:
            blockers.append(f"{pst}: relevance did not converge")
        ops[pst] = {"relevant": [i["name"] for i in relevant], "blocked_by": num_blockers}
        if num_blockers:
            continue
        table_rows.update(rows)
        cases_all.extend(cases)

    blocked_ops = {p: o["blocked_by"] for p, o in ops.items() if o["blocked_by"]}
    if blocked_ops and len(blocked_ops) == len(ops):
        blockers.append("every operation depends on " + ", ".join(sorted({n for v in blocked_ops.values() for n in v})))
    if blockers:
        report.update(reasons=list(dict.fromkeys(blockers)), runner_calls=runner.calls, operations=ops)
        return None, report

    caveats = []
    if check_order:
        if runner.run(list(reversed(cases_all)))[::-1] != runner.run(cases_all):
            report["reasons"] = ["output depends on call order (the agent keeps state between calls)"]
            return None, report
        seed1 = runner.run(cases_all, hashseed="1")
        if seed1 != runner.run(cases_all):
            varying = sorted({json.dumps(c.get(primary["name"]) if primary else "*") for c, a, b in
                              zip(cases_all, seed1, runner.run(cases_all)) if a != b})
            if not allow_hash_variation:
                report["reasons"] = ["output changes with PYTHONHASHSEED (set or hash ordering) for " + ", ".join(varying)]
                return None, report
            caveats.append("The Python's own output for " + ", ".join(varying) + " varies between brainstem restarts "
                           "(set iteration order); the flow serves the order a brainstem gets with PYTHONHASHSEED=0.")
    clock_formats = {}
    if check_clock:
        runs = [runner.run(cases_all, clock=clock) for clock in CLOCKS]
        if any(r != runs[0] for r in runs[1:]):
            templ = _clock_template(runs, CLOCKS,
                                    lambda idxs, clock: runner.run([cases_all[i] for i in idxs], clock=clock))
            if templ is None:
                diff = next(k for k, outs in enumerate(zip(*runs)) if any(o != outs[0] for o in outs))
                report["reasons"] = [f"output depends on the clock beyond printed dates (for example {json.dumps(cases_all[diff])})"]
                return None, report
            outs_by_case, clock_formats = templ
            # table_rows and cases_all were built together, one row per case in order
            table_rows = {key: t for key, t in zip(list(table_rows), outs_by_case)}
            caveats.append("Dates the agent prints from its clock (today, or today plus a fixed number of days) are "
                           "filled from the flow's clock (UTC); a brainstem prints its host's local date.")

    for key, (raw, marker, tokens) in hand_holes.items():
        holes = _holes(raw, marker)
        if holes is None or len(holes) != len(tokens):
            report["reasons"] = [f"{key}: the hand translation fills {len(tokens)} number(s), but its two runs differ "
                                 f"{'in more than numbers' if holes is None else f'in {len(holes)}'}"]
            return None, report
        if len(_number_spans(table_rows[key])) != len(_number_spans(raw)):
            report["reasons"] = [f"{key}: the clock template moved the numbers the hand translation fills"]
            return None, report
        table_rows[key] = _punch(table_rows[key], holes, tokens)
    for op, h in hand_ops.items():
        if ops.get(op, {}).get("hand"):
            caveats.append(f"`{op}` is a hand translation: {h.get('why', 'its numbers are computed in the flow')} "
                           "It is proven on its probe grid, not on every possible number.")

    keys, uniq = {}, {}
    for key, out in table_rows.items():
        digest = hashlib.sha256(out.encode("utf-8")).hexdigest()[:16]
        uniq.setdefault(digest, _b64(out))
        keys[key] = digest
    props = c.get("parameters", {}).get("properties", {})
    inputs = {}
    for n, schema in props.items():
        info = disc.inputs.get(n, {})
        desc = (schema or {}).get("description", "") or ""
        if info.get("rule") in ("exact", "ci", "canonical") and info.get("canon") and not info.get("boolean"):
            shown = [info["canon_map"][k] for k in info["canon"]]
            desc = (desc + " " if desc else "") + "Values: " + ", ".join(shown[:40]) + "."
        elif info.get("rule") == "resolver":
            desc = (desc + " " if desc else "") + "Known: " + ", ".join(info["canon"][:40]) + "."
        inputs[n] = {"type": "number" if info.get("kind") == "number" else "string", "description": desc[:500]}
    name = c.get("name") or c.get("class")
    keying = []
    for i in keyed:
        k = {"input": i["name"], "rule": i["rule"], "empty": i.get("empty_mode", "absent"),
             "states": [st for st, _ in _states(i)]}
        if i["rule"] in ("exact", "ci", "canonical") and not i.get("boolean"):
            k["values"] = info_values(i)
        if i.get("boolean"):
            k["boolean"] = True
        if i.get("approximated"):
            k["approximated"] = True
        if i.get("json_echo"):
            k["json_echo"] = True
        if i["rule"] == "resolver":
            r_ = i["resolver"]
            k["resolver"] = {"order": r_.order, "names": r_.names, "default": r_.default, "miss": r_.miss,
                             "reachable": sorted(r_.rep), "absent_is_default": i.get("absent_is_default", True)}
        keying.append(k)
    keying += list(hand_keying.values())
    merged = {"constants": {}, "derived": [], "fills": []}
    for op, h in hand_ops.items():
        if ops.get(op, {}).get("hand"):
            merged["constants"].update(h.get("constants") or {})
            merged["derived"] += [d for d in h.get("derived") or [] if d not in merged["derived"]]
            merged["fills"] += [f for f in h.get("fills") or [] if f not in merged["fills"]]
    spec = {
        "mode": "materialized", "agent": name, "class": c.get("class"), "agent_file": agent_file.name,
        "flow_name": flow_name or re.sub(r"(?:^|[^A-Za-z0-9]+)([A-Za-z0-9])", lambda m: m.group(1).upper(), name) + "Flow",
        "component": component or name, "description": (c.get("description") or name)[:1000],
        "inputs": inputs, "required": [n for n in disc.required if n in props],
        "primary": primary["name"] if primary else None, "keying": keying,
        "operations": {p: o["relevant"] for p, o in ops.items() if not o["blocked_by"]},
        "blocked_operations": blocked_ops,
        "ignored": [n for n, i in disc.inputs.items() if i["rule"] == "ignored"],
        "table_keys": keys, "table_outputs": uniq, "outputs": {"result": "(materialized)"},
        "vectors": cases_all + _probe_vectors(disc, keyed) + hand_vectors,
        "constants": merged["constants"], "derived": merged["derived"], "fills": merged["fills"],
        "hand_operations": {op: h.get("why", "") for op, h in hand_ops.items() if ops.get(op, {}).get("hand")},
        "clock_formats": clock_formats, "caveats": caveats,
        "source_sha256": source_sha,
        "materialized_with": {"clock": CLOCKS[0], "python": runner.python_version(),
                              "checked_clock": CLOCKS[1] if check_clock else None,
                              "checked_clocks": list(CLOCKS[1:]) if check_clock else [],
                              "cases": len(cases_all), "unique_outputs": len(uniq)},
    }
    report.update(materialized=True, cases=len(cases_all), unique_outputs=len(uniq), operations=ops,
                  blocked_operations=blocked_ops, bytes=sum(len(v) for v in uniq.values()), runner_calls=runner.calls)
    return spec, report


def _hand_operation(runner, h, pst, pv, relevant, rest, args_for, hand_keying, hand_holes, rng, cap=6000):
    """Rows for one operation whose numbers a hand translation computes in the flow.

    h["computed"] names a state the flow computes from the numbers (h["computed"]["expr"]) with one example per
    state; the real Python fills a row for every state of the operation's keyed inputs times every computed state.
    A state with a marker (the same state, other numbers) has number holes: the numbers that differ between the
    two runs become h["fills"] placeholders, in order. h["probes"] is the proof grid over the numbers."""
    comp = h["computed"]
    states = list(comp["examples"])
    hand_keying[comp["name"]] = {"input": comp["name"], "rule": "computed", "expr": comp["expr"], "states": states}
    combos = [([], {})]
    for i in relevant:
        combos = [(sts + [st], dict(asg, **{i["name"]: (st, v)})) for sts, asg in combos for st, v in _states(i)]
    cases, keys, markers = [], [], []
    for sts, asg in combos:
        for s_ in states:
            cases.append(dict(args_for(pv, asg), **comp["examples"][s_]))
            keys.append("|".join([pst] + sts + [s_]))
            m = (comp.get("markers") or {}).get(s_)
            markers.append(dict(args_for(pv, asg), **m) if m else None)
    outs = runner.run(cases)
    marker_outs = iter(runner.run([m for m in markers if m]))
    tokens = [f["token"] for f in h.get("fills") or []]
    for key, out, m in zip(keys, outs, markers):
        if m:
            hand_holes[key] = (out, next(marker_outs), tokens)
    grid = [{}]
    for name, values in (h.get("probes") or {}).items():
        grid = [dict(g, **({name: v} if v is not None else {})) for g in grid for v in values]
    vectors = []
    for sts, asg in combos:
        for g in grid:
            full = dict(asg)
            for i in rest:
                if i not in relevant:
                    full[i["name"]] = rng.choice(_states(i))
            vectors.append(dict(args_for(pv, full), **g))
    if len(vectors) > cap:
        vectors = rng.sample(vectors, cap)
    return dict(zip(keys, outs)), cases, vectors


def info_values(info):
    return [info["canon_map"][k] if info["rule"] in ("exact", "canonical") else k for k in info["canon"]]


def _probe_vectors(disc, keyed):
    """Extra proof vectors: variants and random strings for each keyed input, others omitted."""
    base = {}
    for n in disc.required:
        i = disc.inputs.get(n)
        if i and i["rule"] in ("exact", "ci", "canonical") and i.get("canon"):
            base[n] = i["canon_map"][i["canon"][0]]
        elif i and i["rule"] == "resolver":
            base[n] = i["resolver"].order[0]
    out = []
    for i in keyed:
        if i.get("boolean"):
            continue
        if i["rule"] in ("exact", "ci"):
            vals = _variants([i["canon_map"][k] for k in i["canon"][:6]])
        elif i["rule"] == "canonical":
            # approximated: exact only on the listed values (other text is left to the model to map)
            for v in [i["canon_map"][k] for k in i["canon"]]:
                out.append(dict(base, **{i["name"]: v}))
            continue
        elif i["rule"] == "resolver":
            vals = _variants(list(i["resolver"].names.values())[:6] + i["resolver"].order[:6])
        else:
            vals = []
        vals += _random_strings(i["name"] + "-proof", 4) + ["", "   "]
        if i.get("json_echo"):
            vals = [v for v in vals if all(ord(ch) < 128 for ch in v)]
        for v in vals:
            out.append(dict(base, **{i["name"]: v}))
    return out
