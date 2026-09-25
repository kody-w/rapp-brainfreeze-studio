/* The Power Apps code app host for a RAPP rapplication.
 *
 * The rapplication's own UI runs unchanged in a same-origin iframe (rapp/ui.html). This page answers it the way a
 * brainstem does, with the parts the build laid in Power Platform:
 *
 *   - an explicit tool call ("Use the X tool with k=v", rapp:invoke, thoughtbox's {rapp:"invoke"}) runs X's Power
 *     Apps flow when the build proved one, so the answer is the agent's exact output; otherwise it goes to the agent;
 *   - anything else (rapp:chat, a free-form /chat message) goes to the rapplication's Copilot Studio agent, one
 *     conversation per session, through the app's chat flow (the agent is a GitHub Copilot harness agent, which
 *     the flow reaches on the agentic runtime);
 *   - the cartridge (rapp-cartridge/1.0) carries the catalog entry and the signed-in user from getContext().
 *
 * Per app, only rapp-config.js (window.RAPP_CONFIG) and rapp/ change; this file is built once.
 */
import { getContext } from "@microsoft/power-apps/app";
import { getClient } from "@microsoft/power-apps/data";

const cfg = window.RAPP_CONFIG;
const client = getClient(cfg.dataSourcesInfo);
const frame = document.getElementById("rapp");
const note = document.getElementById("note");
const sessionId = "pa-" + Math.random().toString(36).slice(2, 12);
let context = null;
let conversationId;

const contextReady = getContext().then((c) => { context = c; return c; }).catch(() => null);

function show(message) {
  note.textContent = message;
  note.style.display = message ? "block" : "none";
  if (message) setTimeout(() => { if (note.textContent === message) show(""); }, 12000);
}

function operationData(result, what) {
  if (!result || result.success === false) {
    const e = result && result.error;
    throw new Error(`${what}: ${e ? e.message || JSON.stringify(e) : "no result"}`);
  }
  return result.data;
}

function toolFor(name) {
  if (!name) return null;
  const key = String(name).toLowerCase();
  return Object.keys(cfg.tools).find((t) => t.toLowerCase() === key
    || (cfg.tools[t].aliases || []).some((a) => a.toLowerCase() === key)) || null;
}

function flowInput(args) {
  const input = {};
  for (const [k, v] of Object.entries(args || {})) {
    if (v === undefined || v === null) continue;
    input[k] = typeof v === "string" ? v : typeof v === "object" ? JSON.stringify(v) : String(v);
  }
  return input;
}

async function callFlow(flow, input, what) {
  return operationData(await client.executeAsync({
    connectorOperation: { tableName: flow.dataSource, operationName: "Run",
                          parameters: { input, "api-version": "2015-02-01-preview" } },
  }), what);
}

async function runFlow(tool, args) {
  const out = await callFlow(cfg.tools[tool].flow, flowInput(args), `${tool} flow`);
  if (typeof out === "string") return out;
  if (out && typeof out === "object") {
    const keys = Object.keys(out);
    if (keys.length === 1) return typeof out[keys[0]] === "string" ? out[keys[0]] : JSON.stringify(out[keys[0]]);
  }
  return JSON.stringify(out);
}

// The Copilot Studio agent, through the app's chat flow (a harness agent is reached on the agentic runtime; the
// connector's plain Execute Agent actions don't serve harness agents). One conversation per session.
async function askAgent(message) {
  if (!cfg.agent.flow) throw new Error("this app has no chat flow to its agent; every tool it uses runs as a flow");
  const data = await callFlow(cfg.agent.flow, { message, conversation_id: conversationId || "" }, "Copilot Studio agent");
  conversationId = (data && data.conversation_id) || conversationId;
  return (data && data.reply) || "";
}

// What a tool call asked of the agent returns: the output alone, without a code fence around it.
function toolOutput(reply) {
  const m = /^\s*```[\w-]*\s*\n([\s\S]*?)\n?```\s*$/.exec(String(reply || ""));
  return m ? m[1] : String(reply || "").trim();
}

function describe(args) {
  return Object.entries(args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ");
}

// A tool call: the exact flow when the build proved one, else the agent is told to run that tool.
async function invokeTool(tool, args) {
  const name = toolFor(tool) || (Object.keys(cfg.tools).length === 1 ? Object.keys(cfg.tools)[0] : tool);
  if (cfg.tools[name] && cfg.tools[name].flow) return { output: await runFlow(name, args), via: "flow" };
  const reply = await askAgent(`Use the ${name} tool with ${describe(args)}. Reply with the tool's output only.`);
  return { output: toolOutput(reply), via: "agent" };
}

// "Use the X tool with a=1 b="two" ..." — how a rapplication's UI asks its brainstem for one exact tool call.
const TOOL_CALL = /^\s*use the ([\w@./-]+) tool with (.*?)(?:\.\s*(?:reply|return|respond)\b[\s\S]*)?\s*$/is;
const ARG = /([A-Za-z_][\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\s]+))/g;

function explicitCall(text) {
  const m = TOOL_CALL.exec(String(text || ""));
  if (!m || !toolFor(m[1])) return null;
  const args = {};
  for (const a of m[2].matchAll(ARG)) {
    const raw = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4];
    args[a[1]] = a[2] !== undefined || a[3] !== undefined ? raw.replace(/\\(.)/g, "$1") : raw.replace(/[.,;]$/, "");
  }
  return { tool: toolFor(m[1]), args };
}

// The brainstem HTTP surface a rapplication UI calls (via rapp/rapp-bridge.js, since code apps allow no network).
async function brainstem(path, method, body) {
  if (/\/chat\/?$/.test(path) && method === "POST") {
    const text = body && (body.user_input ?? body.message ?? body.prompt);
    const call = explicitCall(text);
    if (call) {
      // agent_logs as a brainstem writes them: one "[Tool] output" line per tool call
      const r = await invokeTool(call.tool, call.args);
      return { status: 200, body: { response: r.via === "flow" ? "DONE" : r.output, agent_logs: `[${call.tool}] ${r.output}`,
                                    session_id: sessionId, via: r.via } };
    }
    return { status: 200, body: { response: await askAgent(String(text || "")), agent_logs: "", session_id: sessionId,
                                  via: "agent" } };
  }
  if (/\/api\/binder\/agent\/?$/.test(path) && method === "POST") {
    const r = await invokeTool(body && (body.name || body.agent), body && (body.args || body.kwargs || {}));
    return { status: 200, body: { result: r.output, via: r.via } };
  }
  if (/\/health\/?$/.test(path)) {
    return { status: 200, body: { status: "ok", runtime: "power-apps-code-app", agents: Object.keys(cfg.tools) } };
  }
  return { status: 404, body: { error: `${path} isn't available inside Power Apps` } };
}

function cartridge() {
  const user = context && context.user ? { login: context.user.userPrincipalName, name: context.user.fullName,
                                           avatar_url: null } : null;
  return {
    type: "rapp:cartridge", schema: "rapp-cartridge/1.0", rapp: cfg.rapp,
    context: { user, tether: { active: false, base: null }, session: { id: sessionId, conversation_history: [] },
               origin: { vbrainstem: null, catalog_source: cfg.rapp.catalog_source || "kody-w/RAPP_Store",
                         host: "power-apps-code-app" } },
    capabilities: { can_invoke_agent: true, can_proxy_fetch: false, can_post_chat: Boolean(cfg.agent.flow) },
  };
}

async function answer(m, reply) {
  try {
    reply(await m.work());
  } catch (e) {
    show(e.message);
    reply(null, e.message);
  }
}

window.addEventListener("message", (ev) => {
  if (ev.source !== frame.contentWindow) return;
  const m = ev.data || {};
  const post = (msg) => frame.contentWindow.postMessage(msg, "*");
  if (m.type === "rapp:get_cartridge") {
    contextReady.then(() => post(cartridge()));
  } else if (m.type === "rapp:invoke") {
    answer({ work: async () => (await invokeTool(m.tool || m.agent, m.args || {})).output },
           (result, error) => post({ type: "rapp:invoke:result", id: m.id, ...(error ? { error } : { result }) }));
  } else if (m.type === "rapp:chat") {
    answer({ work: () => askAgent(String(m.message || "")) },
           (reply, error) => post({ type: "rapp:chat:result", id: m.id, ...(error ? { error } : { reply }) }));
  } else if (m.type === "rapp:fetch") {
    post({ type: "rapp:fetch:result", id: m.id, status: 0,
           error: "code apps allow no network access (connect-src 'none'); this fetch isn't available here" });
  } else if (m.rapp === "invoke") {
    answer({ work: async () => (await invokeTool(null, { action: m.action, ...(m.args || {}) })).output },
           (result, error) => post({ rapp: "result", id: m.id, result: error ? JSON.stringify({ ok: false, error }) : result }));
  } else if (m.type === "rapp:bridge") {
    brainstem(m.path, m.method || "GET", m.body)
      .then((r) => post({ type: "rapp:bridge:result", id: m.id, status: r.status, body: r.body }))
      .catch((e) => { show(e.message); post({ type: "rapp:bridge:result", id: m.id, status: 502, body: { error: e.message } }); });
  }
});

frame.addEventListener("load", () => contextReady.then(() => frame.contentWindow.postMessage(cartridge(), "*")));
