# brainfreeze-studio

Turn a frozen RAPP brainstem into a Copilot Studio agent.

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
| HTTP API calls | a **custom connector** (+ flow) |
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
when the agent prints dates, so dates it prints relative to today follow the flow's clock. An operation
computed from numbers gets a small hand translation (`materialize(..., hand=)`): a state the flow computes
from the numbers, plus the numbers it prints, proven on a grid. An agent that keeps state between calls is
refused. Every materialized tool's description ends with the values its selectors accept, because the
harness orchestrator reads a tool's description but not its input descriptions.

The AIBAST Copilot applied this to the 72 published agents of the AIBAST agents library: 71 materialized,
33,988/33,988 cases byte-exact, deployed as one harness agent with 72 flow tools. Live, four checked calls,
including hand-translated operations and dates from the flow's clock, returned the real Python's output byte
for byte. The workshop engine, which keeps state and runs `pac`, stays a reasoning-only skill.

**Fallback for agents Power Platform can't express** (heavy compute, special libraries, a private
network): `python3 -m brainfreeze_studio serve <egg>` serves the egg's agents over MCP on their pinned
engine, running the real agent.py. It needs a host outside Copilot Studio, which is why it's the fallback,
not the default. Build with `--mcp-connector-id` to route untranslated agents to it.

## Deploy as the person signed in

`brainfreeze_studio.deploy` puts a built workspace into Copilot Studio with nothing but a Dataverse token. That
token is the user's own delegated one, so the agent lands with that person's rights, in any environment they can
make agents in. It needs no pac, az or Node, and no service account. Each step is a Dataverse Web API call, the
same calls `pac copilot push` and `publish` make. It is idempotent and refuses app-only tokens.

```python
from brainfreeze_studio.deploy import deploy
deploy("out/workspace", "https://yourorg.crm.dynamics.com/", get_token=lambda: user_token)
```

[`examples/azure-function`](examples/azure-function) runs build and deploy in an Azure Function. The user signs in
on a small page with their own account (device code, delegated Dataverse access only), picks an egg and deploys.
The Function stores no tokens. Deployed from a laptop and from the Function as the same user, one egg gave
identical agents.

## How close is it?

[MAPPING.md](MAPPING.md) maps every brainstem and agent.py concept to its Copilot Studio harness counterpart,
with a status and evidence per row. Today: **21 of 33** proven or built, 5 approximated, 7 gaps. The translated
InvoiceRouter flow has run live in Copilot Studio with its proven outputs, including the half-cent midpoint, and
so have the materialized AIBAST flows.
Next: connector translations for API-calling agents.

## Tests

```bash
python3 -m unittest discover -s tests -v
# with a copilot-harness-sdk checkout (+ node) and the grail's agents, the profile and SDK-parity tests run too:
HARNESS_SDK_DIR=../copilot-harness-sdk GRAIL_AGENTS_DIR=~/.brainstem/src/rapp_brainstem/agents python3 -m unittest discover -s tests -v
```

The parity test runs the SDK's own `scanWorkspace` and `expectedComponents` on a built workspace, and checks
that both compute the same agent-flow id.

## License

MIT. The RAPP reference implementation is vendored verbatim as `brainfreeze_studio/rapp1.py`;
`rapp1.vendor.json` records its source commit and checksum.
