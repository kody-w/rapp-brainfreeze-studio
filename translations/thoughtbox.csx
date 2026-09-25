// Thoughtbox (@kody-w/thoughtbox), the agent's run() ported to custom connector code. The journal is the RAPP
// workspace file entries.json, which the flow keeps as a Dataverse note; the flow passes it in and writes back what
// this returns. Proven against the agent's Python by brainfreeze_studio.connector_code (translations/thoughtbox.json).
public class Script : ScriptBase
{
    public override async Task<HttpResponseMessage> ExecuteAsync()
    {
        if (this.Context.OperationId != "Run")
            return await this.Context.SendAsync(this.Context.Request, this.CancellationToken).ConfigureAwait(false);
        var call = new Call(await this.Context.Request.Content.ReadAsStringAsync().ConfigureAwait(false));
        string output;
        try { output = Thoughtbox.Run(call); }
        catch (Exception e) { output = Call.Raised(e); }
        return call.Reply(output);
    }
}

public static class Thoughtbox
{
    static JToken Fallback = new JArray();       // the agent's in-memory store, used when the workspace is unreadable

    static JArray Load(Call call)
    {
        string raw = call.WorkspaceRead("entries.json");
        if (!string.IsNullOrEmpty(raw))
        {
            try
            {
                var data = PyJson.Loads(raw);
                if (data is JArray list) return list;
            }
            catch (PyException) { }
        }
        return (JArray)Fallback;
    }

    static void Save(JArray entries, Call call) { call.WorkspaceWrite("entries.json", PyJson.Dumps(entries, 2)); }

    static JObject Obj(params object[] kv)
    {
        var o = new JObject();
        for (int i = 0; i < kv.Length; i += 2) o[(string)kv[i]] = kv[i + 1] as JToken ?? JToken.FromObject(kv[i + 1] ?? JValue.CreateNull());
        return o;
    }

    static JValue Null() { return JValue.CreateNull(); }

    static string Ts(JToken e) { return Py.AsStr(Py.Get(e, "ts", new JValue("")), "__lt__"); }

    // sorted(entries, key=ts, reverse=True): Python's sort is stable, and so is LINQ's
    static List<JToken> ByTsDesc(IEnumerable<JToken> entries)
    {
        return entries.OrderByDescending(Ts, StringComparer.Ordinal).ToList();
    }

    static string FormatEntries(List<JToken> entries, long limit)
    {
        if (entries.Count == 0) return "(no entries)";
        var rows = new List<string>();
        foreach (var e in entries.Take((int)Math.Min(limit, int.MaxValue)))
        {
            var ts = Py.Get(e, "ts", new JValue(""));
            var tags = Py.Get(e, "tags");
            if (!Py.Truthy(tags)) tags = new JArray();
            string tagStr = Py.Truthy(tags) ? " #" + string.Join(" #", Py.Iter(tags).Select(t => Py.AsStr(t, "join"))) : "";
            string id = Py.AsStr(Py.Get(e, "id", new JValue("")), "__getitem__");
            rows.Add("[" + Py.Str(ts) + "] " + Py.Str(Py.Get(e, "text", new JValue(""))) + tagStr + "  (" + Py.Slice(id, 0, 8) + ")");
        }
        return string.Join("\n", rows);
    }

    static JObject Append(JArray entries, Call call, JToken text, JToken tags)
    {
        string t = Py.Strip(Py.AsStr(Py.Truthy(text) ? text : new JValue(""), "strip"));
        if (t.Length == 0) return Obj("ok", false, "error", "text is required and non-empty");
        var kept = new JArray();
        foreach (var tag in Py.Iter(Py.Truthy(tags) ? tags : new JArray()))
            if (Py.Truthy(tag) && Py.Strip(Py.AsStr(tag, "strip")).Length > 0) kept.Add(Py.Strip((string)tag));
        var entry = Obj("id", call.NewId(), "text", t, "tags", kept, "ts", call.Now);
        entries.Add(entry);
        return Obj("ok", true, "entry", entry, "total", entries.Count);
    }

    static JObject List(JArray entries, long limit)
    {
        var sorted = ByTsDesc(entries);
        return Obj("ok", true, "total", entries.Count, "shown", Math.Min(entries.Count, limit),
                   "entries", new JArray(sorted.Take((int)Math.Min(limit, int.MaxValue))), "rendered", FormatEntries(sorted, limit));
    }

    static JObject Search(JArray entries, JToken query, long limit)
    {
        string q = Py.Lower(Py.Strip(Py.AsStr(Py.Truthy(query) ? query : new JValue(""), "strip")));
        if (q.Length == 0) return Obj("ok", false, "error", "query is required");
        var matches = ByTsDesc(entries.Where(e => Py.Lower(Py.AsStr(Py.Truthy(Py.Get(e, "text")) ? Py.Get(e, "text") : new JValue(""), "lower")).Contains(q)));
        return Obj("ok", true, "query", query, "total", matches.Count, "shown", Math.Min(matches.Count, limit),
                   "entries", new JArray(matches.Take((int)Math.Min(limit, int.MaxValue))), "rendered", FormatEntries(matches, limit));
    }

    static JObject Tag(JArray entries, JToken tag, long limit)
    {
        string t = Py.Lower(Py.Strip(Py.AsStr(Py.Truthy(tag) ? tag : new JValue(""), "strip")));
        if (t.Length == 0) return Obj("ok", false, "error", "tag is required");
        var matches = ByTsDesc(entries.Where(e =>
        {
            var tags = Py.Get(e, "tags");
            // the whole list is lowered first, as the comprehension does, so a bad tag raises even after a match
            return Py.Iter(Py.Truthy(tags) ? tags : new JArray()).Select(x => Py.Lower(Py.AsStr(x, "lower"))).ToList().Contains(t);
        }));
        return Obj("ok", true, "tag", tag, "total", matches.Count, "shown", Math.Min(matches.Count, limit),
                   "entries", new JArray(matches.Take((int)Math.Min(limit, int.MaxValue))), "rendered", FormatEntries(matches, limit));
    }

    static JObject Stats(JArray entries)
    {
        var counts = new List<KeyValuePair<string, long>>();       // a dict: first-seen order
        var index = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var e in entries)
        {
            var tags = Py.Get(e, "tags");
            foreach (var tg in Py.Iter(Py.Truthy(tags) ? tags : new JArray()))
            {
                string key = Py.AsStr(tg, "__hash__");
                int at;
                if (index.TryGetValue(key, out at)) counts[at] = new KeyValuePair<string, long>(key, counts[at].Value + 1);
                else { index[key] = counts.Count; counts.Add(new KeyValuePair<string, long>(key, 1)); }
            }
        }
        var stamps = entries.Select(e => Py.Get(e, "ts")).Where(Py.Truthy).Select(v => Py.AsStr(v, "__lt__")).ToList();
        JToken earliest = stamps.Count == 0 ? (JToken)Null() : new JValue(stamps.Aggregate((a, b) => Py.Compare(b, a) < 0 ? b : a));
        JToken latest = stamps.Count == 0 ? (JToken)Null() : new JValue(stamps.Aggregate((a, b) => Py.Compare(b, a) > 0 ? b : a));
        var tagCounts = new JObject();
        foreach (var kv in counts.OrderBy(kv => -kv.Value)) tagCounts[kv.Key] = kv.Value;
        return Obj("ok", true, "total", entries.Count, "earliest", earliest, "latest", latest, "tag_counts", tagCounts);
    }

    static JObject Export(JArray entries, Call call)
    {
        var blob = Obj("schema", "thoughtbox/1.0", "exported_at", call.Now, "count", entries.Count, "entries", entries);
        return Obj("ok", true, "json", PyJson.Dumps(blob, 2), "count", entries.Count);
    }

    static JObject Import(JArray entries, Call call, JToken blob)
    {
        if (!Py.Truthy(blob)) return Obj("ok", false, "error", "blob is required");
        JToken d;
        if (!Py.IsStr(blob))
            throw new PyException("TypeError", "the JSON object must be str, bytes or bytearray, not " + Py.TypeName(blob));
        try { d = PyJson.Loads((string)blob); }
        catch (PyException e) when (e.PyType == "JSONDecodeError") { return Obj("ok", false, "error", "invalid json: " + e.Message); }
        JToken incoming = d is JObject ? Py.Get(d, "entries") : d;
        if (!(incoming is JArray)) return Obj("ok", false, "error", "blob must contain a list of entries");
        var seen = new HashSet<string>(entries.Select(e => Py.Get(e, "id")).Where(Py.Truthy).Select(v => v.ToString(Newtonsoft.Json.Formatting.None)), StringComparer.Ordinal);
        int added = 0;
        foreach (var raw in (JArray)incoming)
        {
            if (!(raw is JObject)) continue;
            JToken eid = Py.Truthy(Py.Get(raw, "id")) ? Py.Get(raw, "id") : new JValue(call.NewId());
            string key = eid.ToString(Newtonsoft.Json.Formatting.None);
            if (seen.Contains(key)) continue;
            JToken text = Py.Truthy(Py.Get(raw, "text")) ? Py.Get(raw, "text") : Py.Truthy(Py.Get(raw, "body")) ? Py.Get(raw, "body") : new JValue("");
            if (!Py.Truthy(text)) continue;
            entries.Add(Obj("id", eid, "text", text,
                            "tags", Py.Truthy(Py.Get(raw, "tags")) ? Py.Get(raw, "tags") : new JArray(),
                            "ts", Py.Truthy(Py.Get(raw, "ts")) ? Py.Get(raw, "ts") : new JValue(call.Now)));
            seen.Add(key);
            added++;
        }
        return Obj("ok", true, "added", added, "total", entries.Count);
    }

    static JObject Delete(JArray entries, JToken entryId)
    {
        if (!Py.Truthy(entryId)) return Obj("ok", false, "error", "id is required");
        int before = entries.Count;
        var keep = entries.Where(e =>
        {
            var id = Py.Get(e, "id");
            if (Py.Eq(id, entryId)) return false;
            string idText = Py.AsStr(Py.Get(e, "id", new JValue("")), "startswith");
            return !idText.StartsWith(Py.AsStr(entryId, "startswith"), StringComparison.Ordinal);
        }).ToList();
        entries.Clear();
        foreach (var e in keep) entries.Add(e);
        return Obj("ok", true, "removed", before - entries.Count, "total", entries.Count);
    }

    // the transport stringifies lists (a Power Apps trigger carries text): take a list back
    static JToken ListArg(JToken v)
    {
        if (Py.IsStr(v))
        {
            string s = ((string)v).Trim();
            if (s.StartsWith("[")) { try { var parsed = PyJson.Loads(s); if (parsed is JArray) return parsed; } catch (PyException) { } }
        }
        return v;
    }

    public static string Run(Call call)
    {
        var a = call.Args;
        string action = Py.Strip(Py.AsStr(Py.Truthy(Py.Get(a, "action")) ? Py.Get(a, "action") : new JValue(""), "strip"));
        if (action.Length == 0) return PyJson.Dumps(Obj("ok", false, "error", "action is required"), 2);
        long limit = Py.Int(Py.Truthy(Py.Get(a, "limit")) ? Py.Get(a, "limit") : new JValue(50));
        limit = Math.Max(1, Math.Min(limit, 1000));
        var entries = new JArray(Load(call).Select(e => e.DeepClone()));
        JObject result;
        switch (action)
        {
            case "append": result = Append(entries, call, Py.Get(a, "text"), ListArg(Py.Get(a, "tags"))); break;
            case "list": result = List(entries, limit); break;
            case "search": result = Search(entries, Py.Get(a, "query"), limit); break;
            case "tag": result = Tag(entries, Py.Get(a, "tag"), limit); break;
            case "stats": result = Stats(entries); break;
            case "export": result = Export(entries, call); break;
            case "import_json": result = Import(entries, call, Py.Get(a, "blob")); break;
            case "delete": result = Delete(entries, Py.Get(a, "id")); break;
            default: result = Obj("ok", false, "error", "unknown action: " + Py.ReprString(action)); break;
        }
        if (Py.Truthy(result["ok"]) && (action == "append" || action == "import_json" || action == "delete")) Save(entries, call);
        return PyJson.Dumps(result, 2);
    }
}
