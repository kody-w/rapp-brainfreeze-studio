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

## How close is it?

[MAPPING.md](MAPPING.md) maps every brainstem and agent.py concept to its Copilot Studio harness counterpart,
with a status and evidence per row. Today: **15 of 28** proven or built, 5 approximated, 8 gaps. The largest gap:
an arbitrary agent.py doesn't execute in Copilot Studio yet. Hosting agents as MCP tools is the next step.

## Tests

```bash
python3 -m unittest discover -s tests -v
# with a copilot-harness-sdk checkout (+ node) and the grail's agents, the profile and SDK-parity tests run too:
HARNESS_SDK_DIR=../copilot-harness-sdk GRAIL_AGENTS_DIR=~/.brainstem/src/rapp_brainstem/agents python3 -m unittest discover -s tests -v
```

The parity test runs the SDK's own `scanWorkspace` and `expectedComponents` on a built workspace, and checks
that both compute the same agent-flow id.

## License

Apache-2.0. The RAPP reference implementation is vendored verbatim as `brainfreeze_studio/rapp1.py`;
`rapp1.vendor.json` records its source commit and checksum.
