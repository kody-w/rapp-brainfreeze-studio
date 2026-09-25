// JSON Doctor (@rapp/json_doctor), perform() ported to custom connector code. The files it reads live in a SharePoint
// document library the user picked: the flow reads each named file there and hands the code its bytes (`files`), so
// `path` names a file in that library. Proven against the agent's Python on the same files by
// brainfreeze_studio.connector_code (translations/json_doctor.json).
public class Script : ScriptBase
{
    public override async Task<HttpResponseMessage> ExecuteAsync()
    {
        if (this.Context.OperationId != "Run")
            return await this.Context.SendAsync(this.Context.Request, this.CancellationToken).ConfigureAwait(false);
        var call = new Call(await this.Context.Request.Content.ReadAsStringAsync().ConfigureAwait(false));
        string output;
        try { output = JsonDoctor.Perform(call); }
        catch (Exception e) { output = Call.Raised(e); }
        return call.Reply(output);
    }
}

public static class JsonDoctor
{
    const long MaxBytes = 64L * 1024 * 1024;

    static JObject Obj(params object[] kv)
    {
        var o = new JObject();
        for (int i = 0; i < kv.Length; i += 2) o[(string)kv[i]] = kv[i + 1] as JToken ?? JToken.FromObject(kv[i + 1] ?? JValue.CreateNull());
        return o;
    }

    static JArray Strings(IEnumerable<string> items) { return new JArray(items.Select(x => (JToken)new JValue(x))); }

    static string TypeName(JToken v)
    {
        if (v == null || v.Type == JTokenType.Null) return "null";
        switch (v.Type)
        {
            case JTokenType.Boolean: return "bool";
            case JTokenType.Integer: case JTokenType.Raw: return "int";
            case JTokenType.Float: return "float";
            case JTokenType.String: return "string";
            case JTokenType.Array: return "array";
            case JTokenType.Object: return "object";
        }
        return v.Type.ToString();
    }

    static bool EndsWithAny(string path, params string[] suffixes) { return suffixes.Any(x => path.EndsWith(x, StringComparison.Ordinal)); }

    // _load(path) -> (records, mode)
    static JToken Load(Call call, string path, out string mode)
    {
        if (call.Bytes(path).Length > MaxBytes) throw new PyException("ValueError", "file larger than " + MaxBytes + " bytes");
        string text = call.ReadText(path);
        if (EndsWithAny(path, ".jsonl", ".ndjson"))
        {
            var recs = new JArray();
            int i = 0;
            foreach (var raw in Py.SplitLines(text))
            {
                i++;
                string line = Py.Strip(raw);
                if (line.Length == 0) continue;
                try { recs.Add(PyJson.Loads(line)); }
                catch (PyJsonDecodeError e) { throw new PyException("ValueError", "line " + i + ": " + e.Msg + " at column " + e.ColNo); }
            }
            mode = "jsonl";
            return recs;
        }
        mode = "json";
        return PyJson.Loads(text);
    }

    // v not in (None, "", [], {})
    static bool Blank(JToken v)
    {
        if (v == null || v.Type == JTokenType.Null) return true;
        if (v.Type == JTokenType.String) return ((string)v).Length == 0;
        if (v is JArray a) return a.Count == 0;
        if (v is JObject o) return o.Count == 0;
        return false;
    }

    class Field { public SortedSet<string> Types = new SortedSet<string>(StringComparer.Ordinal); public int PresentIn; public JToken Sample; }

    // _infer(records): field -> {types, present_in, coverage, sample[, optional]}
    static JObject Infer(JToken records)
    {
        if (records is JObject) records = new JArray(records);
        if (!(records is JArray list))
            return Obj("_root", Obj("types", Strings(new[] { TypeName(records) }), "present_in", 1, "coverage", "100%"));
        var objs = list.OfType<JObject>().ToList();
        int total = list.Count;
        if (objs.Count == 0)
        {
            var kinds = new SortedSet<string>(list.Select(TypeName), StringComparer.Ordinal);
            return Obj("_items", Obj("types", Strings(kinds), "present_in", total, "coverage", "100%"));
        }
        var fields = new Dictionary<string, Field>(StringComparer.Ordinal);
        var order = new List<string>();
        foreach (var r in objs)
        {
            foreach (var p in r.Properties())
            {
                Field f;
                if (!fields.TryGetValue(p.Name, out f)) { f = new Field(); fields[p.Name] = f; order.Add(p.Name); }
                f.Types.Add(TypeName(p.Value));
                f.PresentIn++;
                if (f.Sample == null && !Blank(p.Value))
                {
                    JToken sample = p.Value is JObject || p.Value is JArray ? new JValue(TypeName(p.Value)) : p.Value;
                    if (Py.IsStr(sample) && Py.Len((string)sample) > 60) sample = new JValue(Py.Slice((string)sample, 0, 60) + "…");
                    f.Sample = sample;
                }
            }
        }
        var output = new JObject();
        // sorted(..., key=-present_in) is stable: ties keep the order the keys were first seen
        foreach (var k in order.Select((k, i) => new { k, i }).OrderBy(x => -fields[x.k].PresentIn).ThenBy(x => x.i).Select(x => x.k))
        {
            var f = fields[k];
            double pct = 100.0 * f.PresentIn / Math.Max(1, objs.Count);
            var entry = Obj("types", Strings(f.Types), "present_in", f.PresentIn, "coverage", Py.Format0f(pct) + "%",
                            "sample", f.Sample ?? JValue.CreateNull());
            if (pct < 100) entry["optional"] = true;
            output[k] = entry;
        }
        return output;
    }

    // _walk(obj, path) -> (value, error)
    static JToken Walk(JToken obj, string path, out string error)
    {
        var cur = obj;
        foreach (var part in path.Split('.').Where(p => p.Length > 0))
        {
            if (cur is JArray a)
            {
                try { cur = Py.Index(a, Py.Int(new JValue(part))); continue; }
                catch (PyException e) when (e.PyType == "ValueError" || e.PyType == "IndexError")
                {
                    error = "no index " + Py.ReprString(part) + " in array of " + a.Count;
                    return null;
                }
            }
            if (cur is JObject o)
            {
                JToken next;
                if (!o.TryGetValue(part, out next))
                {
                    var keys = o.Properties().Select(p => p.Name).OrderBy(x => x, StringComparer.Ordinal).Take(8);
                    error = "no key " + Py.ReprString(part) + "; available: " + Py.Str(Strings(keys));
                    return null;
                }
                cur = next;
                continue;
            }
            error = "cannot descend into " + TypeName(cur) + " at " + Py.ReprString(part);
            return null;
        }
        error = null;
        return cur;
    }

    static int Records(JToken data) { return data is JArray a ? a.Count : 1; }

    static string Err(string message) { return PyJson.Dumps(Obj("status", "error", "message", message), 2); }

    public static string Perform(Call call)
    {
        var action = Py.Get(call.Args, "action");
        var pathArg = Py.Get(call.Args, "path");
        if (!Py.Truthy(pathArg) || !call.IsFile(pathArg)) return Err("file not found: " + Py.Str(pathArg));
        string path = (string)pathArg;
        try
        {
            if (Py.Eq(action, new JValue("validate")))
            {
                JToken data;
                string mode;
                try { data = Load(call, path, out mode); }
                catch (PyException e) when (e.PyType == "JSONDecodeError" || e.PyType == "ValueError" || e.PyType == "UnicodeDecodeError")
                {
                    var detail = Obj("status", "ok", "valid", false, "error", e.Message);
                    if (e is PyJsonDecodeError d)
                    {
                        string text = call.ReadText(path);
                        int lo = Math.Max(0, d.Pos - 60);
                        detail["line"] = d.LineNo;
                        detail["column"] = d.ColNo;
                        detail["context"] = Py.Slice(text, lo, d.Pos + 60);
                    }
                    return PyJson.Dumps(detail, 2);
                }
                return PyJson.Dumps(Obj("status", "ok", "valid", true, "mode", mode, "records", Records(data),
                                        "root_type", TypeName(data)), 2);
            }

            string m;
            var loaded = Load(call, path, out m);

            if (Py.Eq(action, new JValue("inspect")))
                return PyJson.Dumps(Obj("status", "ok", "mode", m, "root_type", TypeName(loaded), "records", Records(loaded),
                                        "bytes", call.Bytes(path).LongLength, "fields", Infer(loaded),
                                        "note", "coverage is the share of records containing the field; anything under 100% is marked optional"), 2);

            if (Py.Eq(action, new JValue("query")))
            {
                var keyArg = Py.Get(call.Args, "key");
                string key = Py.Truthy(keyArg) ? Py.AsStr(keyArg, "split") : "";
                string error;
                var value = Walk(loaded, key, out error);
                if (error != null) return PyJson.Dumps(Obj("status", "error", "path", key, "message", error), 2);
                return PyJson.Dumps(Obj("status", "ok", "path", key, "type", TypeName(value), "value", value ?? JValue.CreateNull()), 2);
            }

            if (Py.Eq(action, new JValue("diff")))
            {
                var other = Py.Get(call.Args, "other");
                if (!Py.Truthy(other) || !call.IsFile(other))
                    return Err("second file not found: " + Py.Str(other));
                string m2;
                var b = Load(call, (string)other, out m2);
                var fa = Infer(loaded);
                var fb = Infer(b);
                var ka = new HashSet<string>(fa.Properties().Select(p => p.Name), StringComparer.Ordinal);
                var kb = new HashSet<string>(fb.Properties().Select(p => p.Name), StringComparer.Ordinal);
                var added = kb.Where(k => !ka.Contains(k)).OrderBy(k => k, StringComparer.Ordinal).ToList();
                var removed = ka.Where(k => !kb.Contains(k)).OrderBy(k => k, StringComparer.Ordinal).ToList();
                var changed = new JArray();
                foreach (var k in ka.Where(kb.Contains).OrderBy(k => k, StringComparer.Ordinal))
                {
                    if (!Py.Eq(fa[k]["types"], fb[k]["types"]))
                        changed.Add(Obj("field", k, "from", fa[k]["types"], "to", fb[k]["types"]));
                    else if (!Py.Eq(fa[k]["coverage"], fb[k]["coverage"]))
                        changed.Add(Obj("field", k, "coverage", (string)fa[k]["coverage"] + " -> " + (string)fb[k]["coverage"]));
                }
                return PyJson.Dumps(Obj("status", "ok", "identical_shape", added.Count == 0 && removed.Count == 0 && changed.Count == 0,
                                        "fields_added", Strings(added), "fields_removed", Strings(removed), "fields_changed", changed,
                                        "records", Obj("a", Records(loaded), "b", Records(b))), 2);
            }

            return PyJson.Dumps(Obj("status", "error", "message", "unknown action " + Py.Repr(action),
                                    "valid", Strings(new[] { "inspect", "validate", "diff", "query" })), 2);
        }
        catch (Exception e)
        {
            return Err(Call.Raised(e));
        }
    }
}
