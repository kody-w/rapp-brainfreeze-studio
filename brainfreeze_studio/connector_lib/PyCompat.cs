// PyCompat: Python's own behavior, for agent logic ported from Python into custom connector code. Every port links
// this file in (brainfreeze_studio.connector_code puts it after the port), so the deployed script stays one
// self-contained file.
//
//   PyJson.Dumps(value, indent)   json.dumps(value, indent=indent)  (ensure_ascii, Python's separators and float repr)
//   PyJson.Loads(text)            json.loads(text), raising PyException("JSONDecodeError", ...) with Python 3.11's
//                                 messages (the grail engine's Python)
//   Py.Str / Py.Truthy / Py.Strip / Py.Lower / Py.Int / Py.Repr / Py.Slice / Py.Get   str(), truthiness, strip(),
//                                 lower(), int(), repr(), s[a:b] on code points, dict.get()
//
// Values are Newtonsoft JTokens: JObject keeps insertion order like a dict, JArray is a list, JValue holds the scalars.

public class PyException : Exception
{
    public string PyType { get; }
    public PyException(string pyType, string message) : base(message) { PyType = pyType; }
}

public static class Py
{
    // str.isspace(), for the characters strip() removes when called with no argument
    public static bool IsSpace(char c)
    {
        if (c >= '\t' && c <= '\r') return true;
        if (c >= '\x1c' && c <= ' ') return true;
        switch (c)
        {
            case '\x85': case '\xa0': case '\u1680': case '\u2028': case '\u2029': case '\u202f': case '\u205f':
            case '\u3000': return true;
        }
        return c >= '\u2000' && c <= '\u200a';
    }

    public static string Strip(string s)
    {
        if (s == null) return null;
        int a = 0, b = s.Length;
        while (a < b && IsSpace(s[a])) a++;
        while (b > a && IsSpace(s[b - 1])) b--;
        return s.Substring(a, b - a);
    }

    public static string Lower(string s) { return s == null ? null : s.ToLowerInvariant(); }

    public static bool Truthy(JToken v)
    {
        if (v == null) return false;
        switch (v.Type)
        {
            case JTokenType.Null: case JTokenType.Undefined: return false;
            case JTokenType.Boolean: return (bool)v;
            case JTokenType.Integer: return Convert.ToInt64(((JValue)v).Value, System.Globalization.CultureInfo.InvariantCulture) != 0;
            case JTokenType.Raw: return ((JRaw)v).ToString().TrimStart('-').Trim('0').Length > 0;       // a big int
            case JTokenType.Float: return (double)v != 0.0;
            case JTokenType.String: return ((string)v).Length > 0;
            case JTokenType.Array: return ((JArray)v).Count > 0;
            case JTokenType.Object: return ((JObject)v).Count > 0;
        }
        return true;
    }

    public static string TypeName(JToken v)
    {
        if (v == null || v.Type == JTokenType.Null) return "NoneType";
        switch (v.Type)
        {
            case JTokenType.Boolean: return "bool";
            case JTokenType.Integer: case JTokenType.Raw: return "int";
            case JTokenType.Float: return "float";
            case JTokenType.String: return "str";
            case JTokenType.Array: return "list";
            case JTokenType.Object: return "dict";
        }
        return "object";
    }

    public static bool IsStr(JToken v) { return v != null && v.Type == JTokenType.String; }

    // the str a method call on this value needs, or Python's AttributeError
    public static string AsStr(JToken v, string method)
    {
        if (IsStr(v)) return (string)v;
        throw new PyException("AttributeError", "'" + TypeName(v) + "' object has no attribute '" + method + "'");
    }

    // str(value), as print() and f-strings show it
    public static string Str(JToken v)
    {
        if (v == null || v.Type == JTokenType.Null) return "None";
        switch (v.Type)
        {
            case JTokenType.String: return (string)v;
            case JTokenType.Boolean: return (bool)v ? "True" : "False";
            case JTokenType.Integer: case JTokenType.Raw: return IntText(v);
            case JTokenType.Float: return PyJson.FloatRepr((double)v, true);
            case JTokenType.Array: return "[" + string.Join(", ", ((JArray)v).Select(Repr)) + "]";
            case JTokenType.Object:
                return "{" + string.Join(", ", ((JObject)v).Properties().Select(p => ReprString(p.Name) + ": " + Repr(p.Value))) + "}";
        }
        return v.ToString();
    }

    public static string IntText(JToken v)
    {
        if (v.Type == JTokenType.Raw) return ((JRaw)v).ToString();
        return Convert.ToInt64(((JValue)v).Value, System.Globalization.CultureInfo.InvariantCulture).ToString(System.Globalization.CultureInfo.InvariantCulture);
    }

    public static string Repr(JToken v) { return IsStr(v) ? ReprString((string)v) : Str(v); }

    public static string ReprString(string s)
    {
        char quote = s.Contains('\'') && !s.Contains('"') ? '"' : '\'';
        var sb = new StringBuilder();
        sb.Append(quote);
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c == quote || c == '\\') { sb.Append('\\').Append(c); continue; }
            if (c == '\t') { sb.Append("\\t"); continue; }
            if (c == '\n') { sb.Append("\\n"); continue; }
            if (c == '\r') { sb.Append("\\r"); continue; }
            if (c < ' ' || (c >= '\x7f' && c < '\xa0')) { sb.Append("\\x").Append(((int)c).ToString("x2")); continue; }
            if (char.IsHighSurrogate(c) && i + 1 < s.Length && char.IsLowSurrogate(s[i + 1])) { sb.Append(c).Append(s[++i]); continue; }
            var cat = char.GetUnicodeCategory(c);
            bool printable = !(cat == System.Globalization.UnicodeCategory.Control || cat == System.Globalization.UnicodeCategory.Format ||
                               cat == System.Globalization.UnicodeCategory.Surrogate || cat == System.Globalization.UnicodeCategory.PrivateUse ||
                               cat == System.Globalization.UnicodeCategory.OtherNotAssigned || cat == System.Globalization.UnicodeCategory.LineSeparator ||
                               cat == System.Globalization.UnicodeCategory.ParagraphSeparator ||
                               (cat == System.Globalization.UnicodeCategory.SpaceSeparator && c != ' '));
            if (!printable) { sb.Append(c <= '\xff' ? "\\x" + ((int)c).ToString("x2") : "\\u" + ((int)c).ToString("x4")); continue; }
            sb.Append(c);
        }
        sb.Append(quote);
        return sb.ToString();
    }

    // int(value), for the values a tool call carries
    public static long Int(JToken v)
    {
        if (v == null || v.Type == JTokenType.Null)
            throw new PyException("TypeError", "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'");
        switch (v.Type)
        {
            case JTokenType.Boolean: return (bool)v ? 1 : 0;
            case JTokenType.Integer: return (long)v;
            case JTokenType.Float: return (long)Math.Truncate((double)v);
            case JTokenType.String:
                // Python accepts any Unicode decimal digit (int("٣") is 3), and single underscores between digits
                var s = Strip((string)v);
                if (Regex.IsMatch(s, @"^[+-]?\d(?:_?\d)*$"))
                {
                    long n = 0;
                    foreach (char c in s) if (char.IsDigit(c)) n = checked(n * 10 + (long)char.GetNumericValue(c));
                    return s[0] == '-' ? -n : n;
                }
                throw new PyException("ValueError", "invalid literal for int() with base 10: " + ReprString((string)v));
        }
        throw new PyException("TypeError", "int() argument must be a string, a bytes-like object or a real number, not '" + TypeName(v) + "'");
    }

    static List<string> Points(string s)
    {
        var points = new List<string>();
        for (int i = 0; i < s.Length; i++)
        {
            if (char.IsHighSurrogate(s[i]) && i + 1 < s.Length && char.IsLowSurrogate(s[i + 1])) { points.Add(s.Substring(i, 2)); i++; }
            else points.Add(s[i].ToString());
        }
        return points;
    }

    // s[start:end] by code points (Python's str counts characters, not UTF-16 units)
    public static string Slice(string s, int start, int end)
    {
        var p = Points(s);
        start = Math.Max(0, Math.Min(start, p.Count));
        end = Math.Max(start, Math.Min(end, p.Count));
        return string.Concat(p.Skip(start).Take(end - start));
    }

    // an iterable's items: a list's elements, a str's characters, a dict's keys
    public static IEnumerable<JToken> Iter(JToken v)
    {
        if (v == null || v.Type == JTokenType.Null) throw new PyException("TypeError", "'NoneType' object is not iterable");
        if (v.Type == JTokenType.Array) return ((JArray)v).ToList();
        if (v.Type == JTokenType.String) return Points((string)v).Select(c => (JToken)new JValue(c));
        if (v.Type == JTokenType.Object) return ((JObject)v).Properties().Select(p => (JToken)new JValue(p.Name));
        throw new PyException("TypeError", "'" + TypeName(v) + "' object is not iterable");
    }

    // dict.get(key, default)
    public static JToken Get(JToken obj, string key, JToken fallback = null)
    {
        var o = obj as JObject;
        if (o == null) throw new PyException("AttributeError", "'" + TypeName(obj) + "' object has no attribute 'get'");
        JToken v;
        return o.TryGetValue(key, out v) ? v : (fallback ?? JValue.CreateNull());
    }

    // == between two Python values from JSON
    public static bool Eq(JToken a, JToken b) { return JToken.DeepEquals(a ?? JValue.CreateNull(), b ?? JValue.CreateNull()); }

    // Python's ordering of two str values: by code point
    public static int Compare(string a, string b) { return string.CompareOrdinal(a, b); }
}

public static class PyJson
{
    // ── json.dumps(value, indent=n): separators (', ', ': ') or (',', ': ') with indent, ensure_ascii ──
    public static string Dumps(JToken v, int? indent = null)
    {
        var sb = new StringBuilder();
        Write(sb, v, indent, 0);
        return sb.ToString();
    }

    static void Write(StringBuilder sb, JToken v, int? indent, int level)
    {
        if (v == null || v.Type == JTokenType.Null || v.Type == JTokenType.Undefined) { sb.Append("null"); return; }
        switch (v.Type)
        {
            case JTokenType.Boolean: sb.Append((bool)v ? "true" : "false"); return;
            case JTokenType.Integer: case JTokenType.Raw: sb.Append(Py.IntText(v)); return;
            case JTokenType.Float: sb.Append(FloatRepr((double)v, false)); return;
            case JTokenType.String: WriteString(sb, (string)v); return;
            case JTokenType.Array:
                var a = (JArray)v;
                if (a.Count == 0) { sb.Append("[]"); return; }
                sb.Append('[');
                for (int i = 0; i < a.Count; i++)
                {
                    if (i > 0) sb.Append(indent == null ? ", " : ",");
                    Newline(sb, indent, level + 1);
                    Write(sb, a[i], indent, level + 1);
                }
                Newline(sb, indent, level);
                sb.Append(']');
                return;
            case JTokenType.Object:
                var o = (JObject)v;
                if (o.Count == 0) { sb.Append("{}"); return; }
                sb.Append('{');
                bool first = true;
                foreach (var p in o.Properties())
                {
                    if (!first) sb.Append(indent == null ? ", " : ",");
                    first = false;
                    Newline(sb, indent, level + 1);
                    WriteString(sb, p.Name);
                    sb.Append(": ");
                    Write(sb, p.Value, indent, level + 1);
                }
                Newline(sb, indent, level);
                sb.Append('}');
                return;
        }
        WriteString(sb, v.ToString());
    }

    // json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    public static string Canonical(JToken v)
    {
        var sb = new StringBuilder();
        WriteCanonical(sb, v);
        return sb.ToString();
    }

    static void WriteCanonical(StringBuilder sb, JToken v)
    {
        if (v is JObject o)
        {
            sb.Append('{');
            bool first = true;
            foreach (var p in o.Properties().OrderBy(p => p.Name, StringComparer.Ordinal))
            {
                if (!first) sb.Append(',');
                first = false;
                WriteString(sb, p.Name, false);
                sb.Append(':');
                WriteCanonical(sb, p.Value);
            }
            sb.Append('}');
            return;
        }
        if (v is JArray a)
        {
            sb.Append('[');
            for (int i = 0; i < a.Count; i++) { if (i > 0) sb.Append(','); WriteCanonical(sb, a[i]); }
            sb.Append(']');
            return;
        }
        if (v != null && v.Type == JTokenType.String) { WriteString(sb, (string)v, false); return; }
        Write(sb, v, null, 0);
    }

    static void Newline(StringBuilder sb, int? indent, int level)
    {
        if (indent == null) return;
        sb.Append('\n').Append(' ', indent.Value * level);
    }

    public static void WriteString(StringBuilder sb, string s) { WriteString(sb, s, true); }

    // ensure_ascii=False keeps every character but '"', '\\' and the controls, which it escapes
    public static void WriteString(StringBuilder sb, string s, bool asciiOnly)
    {
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                default:
                    if (c < ' ' || (asciiOnly && c > '~')) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }

    // repr(float): the shortest round-trip digits; fixed notation from 1e-4 up to 1e16, else d.ddde+XX. json.dumps
    // writes inf and nan as Infinity and NaN, str() as inf and nan.
    public static string FloatRepr(double d, bool asStr)
    {
        if (double.IsNaN(d)) return asStr ? "nan" : "NaN";
        if (double.IsPositiveInfinity(d)) return asStr ? "inf" : "Infinity";
        if (double.IsNegativeInfinity(d)) return asStr ? "-inf" : "-Infinity";
        if (d == 0) return (1 / d < 0) ? "-0.0" : "0.0";
        string r = d.ToString("E16", System.Globalization.CultureInfo.InvariantCulture);
        for (int p = 0; p <= 16; p++)
        {
            string t = d.ToString("E" + p, System.Globalization.CultureInfo.InvariantCulture);
            if (double.Parse(t, System.Globalization.CultureInfo.InvariantCulture) == d) { r = t; break; }
        }
        bool neg = r[0] == '-';
        if (neg) r = r.Substring(1);
        int e = r.IndexOf('E');
        string digits = r.Substring(0, e).Replace(".", "").TrimEnd('0');
        if (digits.Length == 0) digits = "0";
        int exp = int.Parse(r.Substring(e + 1), System.Globalization.CultureInfo.InvariantCulture);
        string body;
        if (exp >= -4 && exp < 16)
        {
            if (exp >= 0)
            {
                string ip = digits.Length > exp + 1 ? digits.Substring(0, exp + 1) : digits + new string('0', exp + 1 - digits.Length);
                string fp = digits.Length > exp + 1 ? digits.Substring(exp + 1) : "0";
                body = ip + "." + fp;
            }
            else body = "0." + new string('0', -exp - 1) + digits;
        }
        else
        {
            string m = digits.Length > 1 ? digits.Substring(0, 1) + "." + digits.Substring(1) : digits;
            body = m + "e" + (exp < 0 ? "-" : "+") + Math.Abs(exp).ToString("00", System.Globalization.CultureInfo.InvariantCulture);
        }
        return (neg ? "-" : "") + body;
    }

    // ── json.loads(text): Python's grammar (NaN, Infinity, big ints) and its error messages ──
    public static JToken Loads(string s)
    {
        var p = new Parser(s);
        int i = p.SkipWs(0);
        int end;
        JToken v = p.Value(i, out end);
        end = p.SkipWs(end);
        if (end != s.Length) throw p.Error("Extra data", end);
        return v;
    }

    class Parser
    {
        readonly string s;
        public Parser(string text) { s = text; }

        public int SkipWs(int i)
        {
            while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
            return i;
        }

        public PyException Error(string msg, int pos)
        {
            int line = 1, last = -1;
            for (int k = 0; k < pos && k < s.Length; k++) if (s[k] == '\n') { line++; last = k; }
            return new PyException("JSONDecodeError", msg + ": line " + line + " column " + (pos - last) + " (char " + pos + ")");
        }

        bool Match(int i, string word) { return i + word.Length <= s.Length && string.CompareOrdinal(s, i, word, 0, word.Length) == 0; }

        public JToken Value(int i, out int end)
        {
            if (i >= s.Length) throw Error("Expecting value", i);
            char c = s[i];
            if (c == '"') { var str = Str(i + 1, out end); return new JValue(str); }
            if (c == '{') return Obj(i + 1, out end);
            if (c == '[') return Arr(i + 1, out end);
            if (c == 'n' && Match(i, "null")) { end = i + 4; return JValue.CreateNull(); }
            if (c == 't' && Match(i, "true")) { end = i + 4; return new JValue(true); }
            if (c == 'f' && Match(i, "false")) { end = i + 5; return new JValue(false); }
            if (c == 'N' && Match(i, "NaN")) { end = i + 3; return new JValue(double.NaN); }
            if (c == 'I' && Match(i, "Infinity")) { end = i + 8; return new JValue(double.PositiveInfinity); }
            if (c == '-' && Match(i, "-Infinity")) { end = i + 9; return new JValue(double.NegativeInfinity); }
            var m = Regex.Match(s.Substring(i), @"^(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?");
            if (m.Success && m.Length > 0)
            {
                end = i + m.Length;
                if (m.Groups[2].Success || m.Groups[3].Success)
                    return new JValue(double.Parse(m.Value, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture));
                long l;
                if (long.TryParse(m.Value, System.Globalization.NumberStyles.AllowLeadingSign, System.Globalization.CultureInfo.InvariantCulture, out l)) return new JValue(l);
                return new JRaw(m.Value);                  // beyond a long: Python keeps every digit, and so does this
            }
            throw Error("Expecting value", i);
        }

        string Str(int i, out int end)
        {
            int begin = i - 1;
            var sb = new StringBuilder();
            while (true)
            {
                if (i >= s.Length) throw Error("Unterminated string starting at", begin);
                char c = s[i];
                if (c == '"') { end = i + 1; return sb.ToString(); }
                if (c == '\\')
                {
                    if (i + 1 >= s.Length) throw Error("Unterminated string starting at", begin);
                    switch (s[i + 1])
                    {
                        case '"': sb.Append('"'); i += 2; continue;
                        case '\\': sb.Append('\\'); i += 2; continue;
                        case '/': sb.Append('/'); i += 2; continue;
                        case 'b': sb.Append('\b'); i += 2; continue;
                        case 'f': sb.Append('\f'); i += 2; continue;
                        case 'n': sb.Append('\n'); i += 2; continue;
                        case 'r': sb.Append('\r'); i += 2; continue;
                        case 't': sb.Append('\t'); i += 2; continue;
                        case 'u':
                            int code = Hex4(i + 2, i + 1);
                            i += 6;
                            if (code >= 0xd800 && code <= 0xdbff && i + 1 < s.Length && s[i] == '\\' && s[i + 1] == 'u')
                            {
                                int low = Hex4(i + 2, i + 1);
                                if (low >= 0xdc00 && low <= 0xdfff) { sb.Append((char)code).Append((char)low); i += 6; continue; }
                            }
                            sb.Append((char)code);
                            continue;
                        default: throw Error("Invalid \\escape", i);
                    }
                }
                if (c < ' ') throw Error("Invalid control character at", i);
                sb.Append(c);
                i++;
            }
        }

        int Hex4(int i, int escapeAt)
        {
            if (i + 4 > s.Length || !Regex.IsMatch(s.Substring(i, 4), "^[0-9a-fA-F]{4}$")) throw Error("Invalid \\uXXXX escape", escapeAt);
            return Convert.ToInt32(s.Substring(i, 4), 16);
        }

        JObject Obj(int i, out int end)
        {
            var o = new JObject();
            i = SkipWs(i);
            if (i < s.Length && s[i] == '}') { end = i + 1; return o; }
            while (true)
            {
                if (i >= s.Length || s[i] != '"') throw Error("Expecting property name enclosed in double quotes", i);
                string key = Str(i + 1, out i);
                i = SkipWs(i);
                if (i >= s.Length || s[i] != ':') throw Error("Expecting ':' delimiter", i);
                i = SkipWs(i + 1);
                var v = Value(i, out i);
                o[key] = v;                           // a repeated key keeps its first place, with the last value
                i = SkipWs(i);
                if (i < s.Length && s[i] == '}') { end = i + 1; return o; }
                if (i >= s.Length || s[i] != ',') throw Error("Expecting ',' delimiter", i);
                i = SkipWs(i + 1);            // Python 3.11, the grail engine's: no special message for a trailing comma
            }
        }

        JArray Arr(int i, out int end)
        {
            var a = new JArray();
            i = SkipWs(i);
            if (i < s.Length && s[i] == ']') { end = i + 1; return a; }
            while (true)
            {
                a.Add(Value(i, out i));
                i = SkipWs(i);
                if (i < s.Length && s[i] == ']') { end = i + 1; return a; }
                if (i >= s.Length || s[i] != ',') throw Error("Expecting ',' delimiter", i);
                i = SkipWs(i + 1);
            }
        }
    }
}

// urllib.request.urlopen as agents use it: a status of 400 or more raises HTTPError ("HTTP Error 403: Forbidden").
public static class PyHttp
{
    public static async Task<string> Open(IScriptContext context, CancellationToken cancel, string method, string url,
                                          string jsonBody = null)
    {
        var request = new HttpRequestMessage(new HttpMethod(method), url);
        if (jsonBody != null) request.Content = new StringContent(jsonBody, Encoding.UTF8, "application/json");
        HttpResponseMessage response;
        try { response = await context.SendAsync(request, cancel).ConfigureAwait(false); }
        catch (HttpRequestException e) { throw new PyException("URLError", "<urlopen error " + e.Message + ">"); }
        int code = (int)response.StatusCode;
        if (code >= 400)
            throw new PyException("HTTPError", "HTTP Error " + code + ": " + (response.ReasonPhrase ?? ""));
        var bytes = response.Content == null ? new byte[0] : await response.Content.ReadAsByteArrayAsync().ConfigureAwait(false);
        return new UTF8Encoding(false).GetString(bytes);
    }

    // json.loads(urlopen(...).read())
    public static async Task<JToken> Json(IScriptContext context, CancellationToken cancel, string method, string url,
                                          string jsonBody = null)
    {
        return PyJson.Loads(await Open(context, cancel, method, url, jsonBody).ConfigureAwait(false));
    }
}

public static class PyBase64
{
    // base64.urlsafe_b64encode(data).decode().rstrip("=")
    public static string UrlSafeNoPad(byte[] data)
    {
        return Convert.ToBase64String(data).Replace('+', '-').Replace('/', '_').TrimEnd('=');
    }

    public static byte[] Sha256Utf8(string text)
    {
        using (var sha = System.Security.Cryptography.SHA256.Create()) return sha.ComputeHash(new UTF8Encoding(false).GetBytes(text));
    }
}

// The request a flow sends and what it reads back (see brainfreeze_studio.connector_code).
public class Call
{
    public JObject Args { get; }
    public JObject State { get; }
    public JObject Written { get; } = new JObject();
    public string Now { get; }
    readonly string id;
    int minted;

    // JSON as Python reads it: dates stay strings (Newtonsoft would turn "2026-09-25T10:00:00Z" into a DateTime)
    public static JToken ParseJson(string text)
    {
        using (var reader = new JsonTextReader(new System.IO.StringReader(text)) { DateParseHandling = DateParseHandling.None,
                                                                         FloatParseHandling = FloatParseHandling.Double })
            return JToken.ReadFrom(reader);
    }

    public Call(string requestBody) : this((JObject)ParseJson(requestBody)) { }

    public Call(JObject request)
    {
        Args = (request["args"] as JObject) ?? new JObject();
        // an input the caller didn't give reaches the flow as null: the agent sees it as not passed
        foreach (var p in Args.Properties().Where(p => p.Value.Type == JTokenType.Null).ToList()) p.Remove();
        State = (request["state"] as JObject) ?? new JObject();
        Now = (string)request["now"] ?? DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", System.Globalization.CultureInfo.InvariantCulture);
        id = ((string)request["id"] ?? Guid.NewGuid().ToString()).ToLowerInvariant();
    }

    // uuid4(): the flow's guid() first, then the same guid counting up in its last 12 hex digits
    public string NewId()
    {
        int k = minted++;
        if (k == 0) return id;
        string head = id.Substring(0, id.Length - 12);
        long tail = Convert.ToInt64(id.Substring(id.Length - 12), 16);
        return head + ((tail + k) % (1L << 48)).ToString("x12");
    }

    // the RAPP workspace contract
    public string WorkspaceRead(string key)
    {
        var v = State[key];
        return v == null || v.Type == JTokenType.Null ? null : (string)v;
    }

    public void WorkspaceWrite(string key, string text) { State[key] = text; Written[key] = text; }

    // what perform() raised, as the brainstem reports it
    public static string Raised(Exception e)
    {
        var py = e as PyException;
        return py != null ? py.PyType + ": " + py.Message : e.GetType().Name + ": " + e.Message;
    }

    // the reply the flow reads: the output and the files written
    public HttpResponseMessage Reply(string output)
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK);
        response.Content = ScriptBase.CreateJsonContent(new JObject { ["output"] = output, ["state"] = Written }.ToString(Newtonsoft.Json.Formatting.None));
        return response;
    }
}
