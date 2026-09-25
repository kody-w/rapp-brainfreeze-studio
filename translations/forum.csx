// ForumAgent (@kody-w/rapp_god_forum), perform() ported to custom connector code. Reading the forum is two GETs from
// the code itself: neighborhood.json on GitHub names the forum's cloud host, and the host lists the room's events.
// Posting needs the agent's signing key, which a connector doesn't hold: those actions give the agent's own answer
// for a machine without `cryptography`. Proven against the agent's Python on recorded responses by
// brainfreeze_studio.connector_code (translations/forum.json).
public class Script : ScriptBase
{
    public override async Task<HttpResponseMessage> ExecuteAsync()
    {
        if (this.Context.OperationId != "Run")
            return await this.Context.SendAsync(this.Context.Request, this.CancellationToken).ConfigureAwait(false);
        var call = new Call(await this.Context.Request.Content.ReadAsStringAsync().ConfigureAwait(false));
        string output;
        try { output = await Forum.Perform(call, this.Context, this.CancellationToken).ConfigureAwait(false); }
        catch (Exception e) { output = Call.Raised(e); }
        return call.Reply(output);
    }
}

public static class Forum
{
    const string Room = "rapp-god-forum";
    const string NeighborhoodUrl = "https://raw.githubusercontent.com/kody-w/rapp-god-forum/main/neighborhood.json";
    const string ProtocolUrl = "https://kody-w.github.io/rapp-god-forum/PROTOCOL.md";
    static readonly string[] Tags = { "brainstem", "kited-layer", "racon", "commons", "registry", "agents", "governance", "general" };

    static string EventId(JToken ev)
    {
        return PyBase64.UrlSafeNoPad(PyBase64.Sha256Utf8(PyJson.Canonical(ev))).Substring(0, 22);
    }

    // _cloud_base(): the first cloud host neighborhood.json lists, or null on any failure
    static async Task<string> CloudBase(IScriptContext context, CancellationToken cancel)
    {
        try
        {
            var n = await PyHttp.Json(context, cancel, "GET", NeighborhoodUrl).ConfigureAwait(false);
            var commons = Py.Get(n, "commons");
            var hosts = Py.Get(Py.Truthy(commons) ? commons : new JObject(), "cloud_hosts");
            if (!Py.Truthy(hosts)) hosts = new JArray();
            if (Py.Truthy(hosts))
            {
                JToken first = Index0(hosts);
                JToken url = first is JObject ? Py.Get(first, "url") : first;
                return Py.AsStr(url, "rstrip").TrimEnd('/');
            }
        }
        catch (Exception) { }
        return null;
    }

    static JToken Index0(JToken seq)
    {
        if (seq is JArray a) return a[0];
        if (Py.IsStr(seq)) return new JValue(Py.Slice((string)seq, 0, 1));
        if (seq is JObject o)
        {
            JToken v;
            if (o.TryGetValue("0", out v)) throw new PyException("KeyError", "0");       // a dict is indexed by key
            throw new PyException("KeyError", "0");
        }
        throw new PyException("TypeError", "'" + Py.TypeName(seq) + "' object is not subscriptable");
    }

    public static async Task<string> Perform(Call call, IScriptContext context, CancellationToken cancel)
    {
        var given = Py.Get(call.Args, "action");
        string action = Py.Lower(Py.AsStr(Py.Truthy(given) ? given : new JValue("help"), "lower"));

        if (action == "protocol")
            return "rapp-god forum — forum profile of rapp-commons-protocol/2.0\n"
                 + "  spec    : " + ProtocolUrl + "\n  room    : " + Room + "\n"
                 + "  kited   : well-known WebRTC id `rapp-god-forum-host`\n"
                 + "  kinds   : topic {title,text,tag} · reply {text,in_reply_to}\n"
                 + "  groups  : " + string.Join(", ", Tags) + "\n"
                 + "  identity: your rappid = your handle (the key is the account; open join).";

        if (action != "whoami" && action != "list" && action != "topic" && action != "reply")
            return "ForumAgent — the rapp-god forum from Python.\n"
                 + "  action=whoami                              your rappid (handle)\n"
                 + "  action=list                                the open topics\n"
                 + "  action=topic title='...' text='...' tag=kited-layer   start a thread\n"
                 + "  action=reply text='...' in_reply_to=<id>   reply to a thread\n"
                 + "  action=protocol                            the forum profile\n"
                 + "Spec: " + ProtocolUrl;

        if (action == "whoami")
            return "No local key — install `cryptography` to mint a rappid handle, or use the "
                 + "web forum which mints yours in the browser.";

        if (action == "list")
        {
            string cloud = await CloudBase(context, cancel).ConfigureAwait(false);
            if (string.IsNullOrEmpty(cloud))
                return "No cloud host listed yet — open the web forum at https://kody-w.github.io/rapp-god-forum/.";
            JToken evs;
            try
            {
                var reply = await PyHttp.Json(context, cancel, "GET", cloud + "/rooms/" + Room + "/events").ConfigureAwait(false);
                evs = Py.Get(reply, "events", new JArray());
            }
            catch (Exception e)
            {
                var py = e as PyException;
                return "Could not reach the forum host: " + (py != null ? py.Message : e.Message);
            }
            var all = Py.Iter(evs).ToList();
            var topics = all.Where(e => Py.Eq(Py.Get(e, "kind"), new JValue("topic"))).ToList();
            if (topics.Count == 0) return "No topics yet — start the first discussion (action=topic).";
            var lines = new List<string> { topics.Count + " topic(s) in the rapp-god forum:" };
            foreach (var t in topics)
            {
                int nrep = 0;
                foreach (var e in Py.Iter(evs))
                {
                    if (!Py.Eq(Py.Get(e, "kind"), new JValue("reply"))) continue;
                    var body = Py.Get(e, "body");
                    var target = Py.Get(Py.Truthy(body) ? body : new JObject(), "in_reply_to");
                    if (Py.Eq(target, new JValue(EventId(t)))) nrep++;
                }
                var tb = Py.Get(t, "body");
                var b = Py.Truthy(tb) ? tb : new JObject();
                JToken from;
                if (!((JObject)t).TryGetValue("from", out from)) throw new PyException("KeyError", "'from'");
                string who = Py.Slice(Py.AsStr(from, "replace").Replace("rappid:v3:", ""), 0, 12);
                lines.Add("  • [" + Py.Str(Py.Get(b, "tag", new JValue("general"))) + "] " + Py.Str(Py.Get(b, "title", new JValue("(untitled)")))
                          + "  — by " + who + " · " + nrep + " repl" + (nrep == 1 ? "y" : "ies") + " · id " + EventId(t));
            }
            return string.Join("\n", lines);
        }

        // topic / reply: signing needs a key
        return "This action needs a signing key. Install `cryptography` (pip install cryptography) "
             + "to mint a rappid and post, or use the web forum which signs in the browser.";
    }
}
