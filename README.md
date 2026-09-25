# brainfreeze-studio

Turn a frozen RAPP brainstem into a Copilot Studio agent, and a RAPP Store rapplication (an agent with its UI) into a
Copilot Studio agent plus a Power Apps code app.

[brainfreeze](https://github.com/kody-w/rapp-brainfreeze) freezes a brainstem into a rapp/1 organism egg: its soul,
agents and memory, plus a session egg for the conversation. brainfreeze-studio turns that egg into a **GitHub
Copilot harness workspace**, and [copilot-harness-sdk](https://github.com/kody-w/copilot-harness-sdk) deploys
that workspace to Copilot Studio as-is.

```
egg ──brainfreeze-studio build──▶ harness workspace ──copilot-harness-sdk deploy──▶ Copilot Studio agent
```

## Use it

```bash
git clone https://github.com/kody-w/copilot-harness-sdk.git   # for its proven infrastructure profiles
python3 -m brainfreeze_studio build https://raw.githubusercontent.com/kody-w/rapp-egg-hub/main/eggs/invoice-desk.egg \
    --name "Invoice Desk" --publisher-prefix rapp --sdk-dir copilot-harness-sdk --out build/

node copilot-harness-sdk/scripts/deploy-harness-agent.mjs --name "Invoice Desk" --publisher-prefix rapp \
    --schema-name rapp_InvoiceDesk --workspace-dir build/workspace --environment https://<org>.crm.dynamics.com/
```

The build is offline and deterministic: the same egg always gives the same workspace. The egg is verified
first, and its agents' contracts are read statically, so no code from the egg runs during a build.

| Output | What it is |
|---|---|
| `build/workspace/` | The harness workspace: `settings.mcs.yml` (the soul as instructions, `cliagent-1.0.0`), `behaviors/`, `capabilities/tools/`, `infrastructure/connections/`, `workflows/` |
| `build/proof.json` | The session egg's prompts, in the SDK's `prove-usecase.mjs` format (add `expect` regexes) |
| `build/reference-answers.json` | What the brainstem answered, for comparison |
| `build/memory-seed.json` | The egg's memories as normalized rows |
| `build/provenance.json` | Which egg (rappid + egg address) the agent grew from, how each agent was mapped, and the deploy command |

| Option | Needed for |
|---|---|
| `--sdk-dir` | the SDK's proven profiles (HackerNews → connector + flow; ManageMemory / ContextMemory → Dataverse) |
| `--environment` | the memory profiles (the Dataverse org URL) |
| `--hn-api-name` | the HackerNews profile (the environment's RAPP Hacker News connector) |
| `--session` | a session egg whose prompts become `proof.json` |
| `--model` | the model series (default `Sonnet46`) |

An agent that matches no proven profile, or lacks the input its profile needs, is laid as a **reasoning-only
skill**. That skill carries the agent.py for reference and must never claim the code ran. The build prints
which agents took which path, and why.

## Keep everything in Copilot Studio: translate, then prove

An agent.py doesn't run in Copilot Studio. Its logic can be **translated** into Power Platform parts, the way
the proven HackerNews and memory agents were, and nothing then lives outside Studio:

| What `perform()` does | Becomes |
|---|---|
| Rules or math | an **agent flow** (`WorkflowTool`) |
| Stored data | **Dataverse** tools |
| HTTP API calls | a **custom connector** (+ flow); its code can make the calls itself |
| Keeps state between calls (files it writes, the RAPP workspace contract) | **connector code** whose flow keeps each file as a Dataverse note |
| Reads the user's files | **connector code** whose flow reads each named file from a SharePoint folder |
| Only reasoning or writing | a **skill** |

A translation is a small spec (`translations/*.json`) written in Power Automate's own expression language.
`build --translations translations/` compiles it into the flow and **proves it**. It evaluates the compiled
flow's expressions offline, with .NET formatting semantics, and compares the result with the agent's real
Python on every test vector and every setting value. A translation that fails the proof is refused, and the
agent falls back to a reasoning-only skill with the reason recorded.

```bash
python3 -m brainfreeze_studio build desk.egg --name "Invoice Desk" --publisher-prefix rapp --translations translations/
#   InvoiceRouter      -> agent flow (translated, parity proven)
#   parity:      InvoiceRouter 56/56 PROVEN
```

The proof earns its keep. Written with plain `formatNumber`, the InvoiceRouter flow **fails** on exact
half-cent amounts, because .NET rounds midpoints away from zero and Python rounds them to even. The spec
uses `pyFormatNumber`, a macro that compiles to plain expressions reproducing Python's rounding, and passes
56/56. The agent's `.env` setting becomes a Power Platform environment variable the flow reads.

**Materialized translations, for whole agent libraries.** Many agents are deterministic formatters over
their own synthetic data: an `operation` plus record selectors in, markdown out. For those,
`brainfreeze_studio.materialize` writes the translation itself. It runs the real agent.py sandboxed (network
off, clock frozen), learns what each input does (an exact value, a case-insensitive value, the library's
name-resolver idiom, text it echoes, or ignored), tables every output, and compiles a lookup flow. The proof
runs every recorded case plus probes through the real Python and the compiled flow, on three frozen clocks
when the agent prints dates, so dates it prints relative to today follow the flow's clock. It runs agent code
under the brainstem engine's Python (3.11 for the grail) and records that version in the spec, because an agent's
output can depend on it: Python 3.12 changed how `sum()` adds floats, which moves a rounded percentage. An operation
computed from numbers gets a small hand translation (`materialize(..., hand=)`): a state the flow computes
from the numbers, plus the numbers it prints, proven on a grid. An agent that keeps state between calls is
refused. Every materialized tool's description ends with the values its selectors accept, because the
harness orchestrator reads a tool's description but not its input descriptions.

The AIBAST Copilot applied this to the 72 published agents of the AIBAST agents library: 71 materialized,
33,988/33,988 cases byte-exact, deployed as one harness agent with 72 flow tools. Live, four checked calls,
including hand-translated operations and dates from the flow's clock, returned the real Python's output byte
for byte. The workshop engine, which keeps state and runs `pac`, stays a reasoning-only skill.

**Connector code, for logic too heavy for flow expressions.** A port of the agent's logic to C#
(`translations/<agent>.csx`, with a spec whose `"mode"` is `"connector-code"`) runs as a custom connector's code,
built on `PyCompat` (Python's JSON, float formatting, rounding, string and error semantics, linked into every
script). The proof compiles the port locally with the .NET SDK and runs the same calls through it and through the
agent's real Python, under the engine's Python version, with the same clock, ids, workspace files, file bytes and
recorded HTTP responses. Any difference refuses it. The flow around the code does what the platform does best:

- **State** (Thoughtbox): read the agent's workspace files from Dataverse notes, run the code, create or update the
  notes it wrote. 40/40 offline; live, append, list, search and stats matched the Python, and the note was created and then updated.
- **Network** (the rapp-god forum): the code makes the agent's own HTTP calls. 29/29 on recorded responses; live, it
  called GitHub and the forum's host and matched the Python on the same endpoints.
- **Files** (JSON Doctor): each path the call names is read from the SharePoint folder in the RAPP Files Site and
  RAPP Files Folder environment variables (a relative path inside the folder; anything else is never read), and the code gets the bytes.
  60/60 offline; live, 11/11 flow runs matched the Python on the uploaded bytes, and the harness agent, asked in
  plain words, called the tool with the right arguments and returned the proven output.

Ports are written by hand today; the gate is what makes them safe to ship.

**Fallback for agents Power Platform can't express** (heavy compute, special libraries, a private
network): `python3 -m brainfreeze_studio serve <egg>` serves the egg's agents over MCP on their pinned
engine, running the real agent.py. It needs a host outside Copilot Studio, which is why it's the fallback,
not the default. Build with `--mcp-connector-id` to route untranslated agents to it.

## Rapplications: an agent with its UI

```bash
python3 -m brainfreeze_studio codeapp-host             # optional: builds the code app host now (needs node and npm)
python3 -m brainfreeze_studio rapplication @rapp/json_doctor --translations translations/ --out out/ \
    --environment https://yourorg.crm.dynamics.com/ --deploy     # signs in with az; omit --deploy to stay offline
```

A RAPP Store rapplication (`manifest.json`, `singleton/<id>_agent.py`, `ui/index.html`, each file checked against
the catalog's SHA-256), or a rapp/1 `rapplication` egg, builds like a one-agent brainstem, and its UI becomes a
**Power Apps code app**:

- **The agent** goes through the same build: a proven flow, connector code, or a reasoning skill.
- **The UI** runs unchanged in a same-origin frame inside a small host app. Its inline scripts and `on*=`
  attributes are rewritten to pass the code app content security policy (literal-argument handlers made at run
  time too, without `eval`), CDN files are vendored with their SHA-256, and what the policy will block anyway
  (remote `fetch`, `eval`, `document.write`) is reported per app.
- **Its calls to the agent** (`fetch('/chat')`, the `rapp-cartridge/1.0` messages): "Use the X tool with k=v" runs
  X's flow through a Power Apps copy of it (same actions, Power Apps trigger), so the app gets the proven output.
  Anything else goes to the Copilot Studio agent through a small chat flow on the agentic runtime
  (`ExecuteCopilotAsyncV2OnAgenticRuntime`), because the Copilot Studio connector's plain Execute Agent refuses
  GitHub Copilot harness agents.
- **Publishing** is pac-free, as the signed-in user: the Power Apps resource provider's storage, create or update
  (with a lease) and publish calls, with a token for `https://service.powerapps.com/`, a permission users can
  consent to themselves. The environment needs code apps turned on.

Live in a dev environment (25 Sep 2026): the Invoice Router example's UI got its flow's exact outputs in the Power
Apps player; AgentTeam and the Vibe Coding Loop rendered their agents' answers; BookFactory deployed through the
Azure Function as a background job; Thoughtbox, the rapp-god forum and JSON Doctor run their logic as connector
code. Headless, `tests/test_codeapp_browser.py` plays packaged apps in a stand-in player under the same policy,
and the JSON Doctor app runs its flow's own expressions and the real compiled C# against the Python.

## Deploy as the person signed in

`brainfreeze_studio.deploy` puts a built workspace into Copilot Studio with nothing but a Dataverse token. That
token is the user's own delegated one, so the agent lands with that person's rights, in any environment they can
make agents in. It needs no pac, az or Node, and no service account. Each step is a Dataverse Web API call, the
same calls `pac copilot push` and `publish` make. It is idempotent and refuses app-only tokens.

```python
from brainfreeze_studio.deploy import deploy
deploy("out/workspace", "https://yourorg.crm.dynamics.com/", get_token=lambda: user_token)
```

With a Power Apps token too (`get_powerapps_token`), a flow's Microsoft connector (SharePoint, Dataverse, ...) is
bound to a connection of the user's own. When they have none and an API Hub token is given
(`get_apihub_token`, `https://apihub.azure.com`), the deploy makes one with their sign-in alone, through the
connection's first-party login (API Hub exchanges the token on their behalf; no browser, no consent page). Agents
that read files find them in `files_site` / `files_folder`: by default the environment's RAPP Files Site, else the
build's, else the tenant's root site found through that connection, and `/Shared Documents`.

[`examples/azure-function`](examples/azure-function) runs build and deploy in an Azure Function. The user signs in
on a small page with their own account (device code, delegated Dataverse access only), picks one of their
environments (listed by the Dataverse Global Discovery Service) and an egg, and deploys.
The Function has no service account, and holds a user's sign-in only while their deploy runs. Deployed from a
laptop and from the Function as the same user, one egg gave identical agents. Agents too big for one HTTP
request, such as libraries of tens of flows, deploy as background jobs: `POST /api/jobs` returns at once, a queue
trigger runs the build and deploy for up to an hour, and `GET /api/jobs/{id}` shows the progress to the user who
started it. Dataverse checks the user's token before the Function does any work, and translations, which run the
egg's code there, need a list of the tenants it serves. A job can also deploy a RAPP Store rapplication
(`{rapplication: "@publisher/id"}`): its agent, the flows its app calls and its code app, published with the
user's own Power Apps token.

## How close is it?

[MAPPING.md](MAPPING.md) maps every brainstem, agent.py and rapplication concept to its Copilot Studio and Power
Platform counterpart, with a status and evidence per row. Today: **31 of 43** proven or built, 6 approximated,
6 gaps. The translated InvoiceRouter flow has run live in Copilot Studio with its proven outputs, including the
half-cent midpoint, and so have the materialized AIBAST flows, the connector-code ports (state, network, files)
and the rapplications' code apps.
Next: the side-by-side parity report (the same prompts to the brainstem and the Studio agent), parent + child
agents, memory seeding, and connectors that need an API key.

## Tests

```bash
python3 -m unittest discover -s tests -v
# with a copilot-harness-sdk checkout (+ node) and the grail's agents, the profile and SDK-parity tests run too:
HARNESS_SDK_DIR=../copilot-harness-sdk GRAIL_AGENTS_DIR=~/.brainstem/src/rapp_brainstem/agents python3 -m unittest discover -s tests -v
```

The parity test runs the SDK's own `scanWorkspace` and `expectedComponents` on a built workspace, and checks
that both compute the same agent-flow id. The connector-code proofs need the .NET SDK (`dotnet`); the RAPP Store
cases need a RAPP_Store checkout (`BFS_RAPP_STORE=~/src/RAPP_Store`); the code app tests need Playwright for Python
with its Chromium, plus node and npm. Each group skips when its tools are missing.

## License

MIT. The RAPP reference implementation is vendored verbatim as `brainfreeze_studio/rapp1.py`;
`rapp1.vendor.json` records its source commit and checksum.
