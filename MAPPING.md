# Mapping: RAPP brainstem and agent.py → Copilot Studio GitHub Copilot harness

This is the parity scorecard for turning a frozen brainstem (a rapp/1 organism egg) into a Copilot Studio
agent through [copilot-harness-sdk](https://github.com/kody-w/copilot-harness-sdk). Every row says where a
brainstem concept lands, and how far that is proven.

| Status | Meaning |
|---|---|
| **proven** | Works in a live Copilot Studio environment, with evidence (the SDK's ledger or tutorial proof) |
| **built** | brainfreeze-studio produces it; checked offline, including against the SDK's own workspace scanner |
| **approximated** | Mapped, but the behavior is not the same; the row says how it differs |
| **gap** | Not mapped yet; the row says what it would take |

Evidence sources: [SDK capability ledger](https://github.com/kody-w/copilot-harness-sdk/blob/main/docs/harness-capability-ledger.md)
(live proof, 7–10 Sep 2026), the SDK's RAR tutorial proof (10 Sep 2026), this repo's tests
(`tests/test_build.py`), and a live run of this tool's own Invoice Desk build in a dev environment's Copilot Studio
Preview (24 Sep 2026).

## Score

| | proven + built | approximated | gap |
|---|---|---|---|
| Brainstem runtime (16 rows) | 10 | 4 | 2 |
| agent.py (10 rows) | 6 | 1 | 3 |
| Frozen-brainstem extras (6 rows) | 4 | 0 | 2 |
| **Total (32 rows)** | **20** | **5** | **7** |

**How agents keep working inside Copilot Studio:** an agent.py doesn't run in Studio, but its logic can be
**translated** into Power Platform parts, the way the proven HackerNews and memory agents were: a custom
connector for API calls, an agent flow for rules, Dataverse for storage, a skill for pure reasoning. Every
translation is **proven** against the agent's real Python before it's deployed, and a failed proof is refused.
An outside MCP host is only the fallback, for agents Power Platform can't express (agent.py rows 4–7).

## Brainstem runtime

| # | Brainstem | Copilot Studio harness | Status | Notes |
|---|---|---|---|---|
| 1 | The engine: `brainstem.py` (Flask + the GitHub Copilot API) | A GitHub Copilot harness agent: `template: cliagent-1.0.0`, `CLICopilotRecognizer` | **proven** / **built** | SDK: 21 agents created, never classic. brainfreeze-studio writes this template (`test_soul_becomes_harness_instructions`). |
| 2 | `soul.md` | `agentSettings.instructions` (a static segment) | **proven** / **built** | The soul is the instructions. With a proven profile, the SDK's routing text follows the soul, trimmed to the tools actually deployed. |
| 3 | Model choice (`.brainstem_model`, Copilot model ids) | `agentSettings.model.series` (for example `Sonnet46`) | **approximated** | `--model` sets the series. No automatic mapping from a Copilot model id to a Studio series. |
| 4 | `POST /chat` `{user_input, conversation_history, session_id}` → `response` | Agentic Runtime `/3p` route: `HarnessClient` `copilot-studio-3p` | **proven** | SDK proof: 10 agents × 5 turns over `/3p`. Studio keeps the conversation on the server, so the client does not resend history. |
| 5 | `POST /chat/stream` (SSE deltas) | `/3p` streaming, normalized to `text.delta` / `text.final` | **proven** | Unit-tested in the SDK; the route was verified live from the playground. |
| 6 | Agent tool loop (up to 3 rounds per message) | The harness plans its own multi-step tool use | **approximated** | Studio has no 3-round cap to match. Answers that relied on the cap may differ. |
| 7 | Hot-load: drop an `agent.py`, live on the next request | Deploy + publish (`deploy-harness-agent.mjs`) | **approximated** | It works, but takes minutes, not a request. There's no parity by design. |
| 8 | Sign-in: GitHub Copilot device login | Entra: `authenticationMode: Integrated`, delegated `CopilotStudio.Copilots.Invoke` | **proven** | A different identity model. Callers need an Entra app with that permission. |
| 9 | Loopback-only routes + per-install secret for other machines | Access control policy, security groups, sharing | **proven** | SDK: `setAccessControl`, `shareAgent` (GrantAccess 204). |
| 10 | Local memory store (`.brainstem_data`) via ManageMemory / ContextMemory | Dataverse `annotations` rows via the memory profile (ConnectorTools + skills) | **proven** / **built** | The tutorial proved write + recall live. brainfreeze-studio lays the same files (`test_real_agents_match_the_proven_profiles`). |
| 11 | Agents' `system_context()`: text added to the system prompt on every request | No per-request prompt hook; only static instructions | **approximated** | The memory profile's "automatic context on every turn" instruction stands in for ContextMemory's preload. Other agents' `system_context()` is lost. |
| 12 | Settings in `.env` (read by agents, `requires_env`) | Environment variables, read by the translated flow | **built** | A translated agent's setting becomes a flow parameter bound to an environment variable (for example `rapp_InvoiceApprovalLimit`), and the parity proof covers several values. `provenance.json` lists the variables to create with the SDK's `upsertEnvironmentVariable`. Live (24 Sep 2026), the flow ran with the default limit; a changed value hasn't been tested live. |
| 13 | Voice mode (`\|\|\|VOICE\|\|\|` split) | — | **gap** | No harness equivalent is mapped. Teams and M365 channels handle speech themselves. |
| 14 | Channels: the web UI, any `/chat` client | Teams and Microsoft 365 Copilot (`setChannels` + publish) | **proven** | Declared and published. The portal builds the Teams app package on first publish. |
| 15 | Health and introspection (`/health`) | `assertHarnessAgent` + `listComponents` readback | **proven** | The SDK reads back template, instructions, published state and every component. |
| 16 | Runs anywhere Python runs; the user owns their instance | A tenant-owned agent in a Power Platform environment | **gap** | This is the tier change itself, not a bug. Needs pac + Entra + an environment. |

## agent.py

| # | agent.py | Copilot Studio harness | Status | Notes |
|---|---|---|---|---|
| 1 | Contract: `metadata` `name` / `description` / `parameters` | Skill frontmatter and input contract; tool descriptions | **built** | Read statically (the egg's code never runs during a build): `test_contract_is_read_without_running_egg_code`. |
| 2 | HackerNews agent (`perform` calls the HN API) | Custom connector + agent flow (`WorkflowTool`) + fetch-hacker-news skill | **proven** / **built** | The tutorial proved live stories through the flow. Needs `--hn-api-name` (the environment's connector). Flow ids match the SDK's (`test_the_sdk_reads_the_workspace_and_agrees_on_flow_ids`). This tool's own build fetched live stories through the flow on 24 Sep 2026. Parity note: the SDK's output contract prints the summary's closing sentence ("When presenting these to the user, render the titles as clickable markdown links exactly as written above."), which a brainstem's model follows instead of printing. |
| 3 | ManageMemory / ContextMemory agents | Dataverse Add row / List rows `ConnectorTool`s + manage-memory / recall-memory skills | **proven** / **built** | Needs `--environment` (the org URL). Without it the agent falls back to reasoning-only, and the build says so. This tool's own build wrote a memory as a Dataverse `annotations` row and recalled it in a fresh conversation on 24 Sep 2026. |
| 4 | **Rules or math in `perform()`** (for example InvoiceRouter) | An **agent flow** translated from a spec (`translations/*.json`), laid as a `WorkflowTool` | **proven** / **built** | Translate-then-prove: the compiled flow's own expressions are evaluated and compared with the real Python on every test vector and setting value. InvoiceRouter: **56/56**. A wrong rule, or plain `formatNumber` (.NET rounds midpoints away from zero, Python to even), fails the gate (`tests/test_translate.py`). The SDK reads the flow tool (`test_the_sdk_reads_the_flow_tool`). Live on 24 Sep 2026, the Studio agent called the flow and got the proven outputs byte-for-byte, including the exact midpoint (`0.125` → `$0.12`, Python's rounding), so the offline evaluator's prediction held live. |
| 5 | Calls an HTTP API in `perform()` | A custom connector (OpenAPI + `script.csx`) and an agent flow, the HackerNews pattern | **gap** | Proven by hand for HackerNews. Translation specs for connectors aren't built yet. |
| 6 | Any agent.py with no translation yet | `InlineAgentSkill` that carries the source as reference and must never claim it ran | **approximated** | Studio reasons about the code but doesn't execute it. This is the fallback until a translation exists. |
| 7 | Agents Power Platform can't express (heavy compute, special libraries, private network) | `McpTool` → `brainfreeze-studio serve`: the egg's agents on their pinned engine, over MCP | **built** | Needs a host outside Copilot Studio, so it's the fallback only. Served live locally: `tools/call` ran the real InvoiceRouter on the grail engine. Studio → connector → server isn't proven live yet. |
| 8 | An agent that calls another agent | `ConnectedAgentTool` (a child harness agent) | **gap** | Proven in the SDK use cases, but brainfreeze-studio doesn't lay multi-agent brainstems as parent + child yet. |
| 9 | Agents that call external APIs with keys in `.env` | One custom connector + connection per API | **gap** | Connection consent is portal-only; the SDK can reference connections but not create them. |
| 10 | **Deterministic agents over their own data** (for example the 72 agents of the AIBAST agents library) | A **materialized** agent flow per agent (`"mode": "materialized"` specs from `brainfreeze_studio.materialize`), laid as a `WorkflowTool` | **proven** / **built** | The real agent runs sandboxed (network off, clock frozen); each input's rule is learned, every output is tabled, and the flow looks the answer up and puts echoed text and dates back. The proof runs every case plus probes through the real Python and the compiled flow, on three clocks for agents that print dates: AIBAST **71 of 72** agents, **33,988/33,988** cases (`tests/test_materialize.py`). Operations computed from numbers get a hand translation proven on a grid (AskHR `submit_time_off`, utility `assistance_programs`, FS `certification_tracker`). An agent that keeps state between calls is refused (the AIBAST workshop engine stays a reasoning-only skill). Live on 24 Sep 2026, in a dev environment's Copilot Studio Preview, four checked calls matched the real Python byte for byte, including two hand translations and dates from the flow's clock. Finding: the harness orchestrator reads a tool's description but not its input descriptions, so a materialized tool's description ends with the values its selectors accept; before that, the model guessed operation names. |

## Frozen-brainstem extras (what an egg adds)

| # | Egg | Copilot Studio harness | Status | Notes |
|---|---|---|---|---|
| 1 | Egg verification (rapp/1 §9) | Refused before anything is built | **built** | `test_tampered_or_wrong_eggs_are_refused`. |
| 2 | Lineage: `rappid` + egg address | `provenance.json` beside the workspace | **built** | Not written onto the agent record yet. |
| 3 | Session egg (the conversation) | `proof.json` for `prove-usecase.mjs` + `reference-answers.json` | **built** | The prompts are carried. `expect` regexes start empty: you add the checks, and the reference answers show what the brainstem said. |
| 4 | Memory in the egg | `memory-seed.json` (normalized rows) | **gap** | Exported, but not yet written into Dataverse. Seeding would use the same Add-row shape as the memory profile. |
| 5 | Proof of parity (brainstem vs Studio, same prompts) | brainfreeze `replay` + `prove-usecase.mjs` side by side | **gap** | Both halves exist; the combined report doesn't. Running it needs an Entra app with `CopilotStudio.Copilots.Invoke`. |
| 6 | Per-agent parity (translation vs real Python) | `parity/<Flow>.json`: every case, both outputs | **built** | The gate for every translation: `build --translations` lays a flow only when its proof passes. |

## What would move the score

In order of how much parity each one buys:

1. **Connector translations** (agent.py row 5), so API-calling agents follow the HackerNews pattern
   automatically.
2. **The side-by-side parity report** (extras row 5): the same prompts to the thawed brainstem and the Studio
   agent.
3. **Memory seeding** (extras row 4) and **parent + child agents** (agent.py row 8).
