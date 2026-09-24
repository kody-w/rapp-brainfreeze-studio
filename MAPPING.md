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
(live proof, 7–10 Sep 2026), the SDK's RAR tutorial proof (10 Sep 2026), and this repo's tests
(`tests/test_build.py`).

## Score

| | proven + built | approximated | gap |
|---|---|---|---|
| Brainstem runtime (16 rows) | 9 | 4 | 3 |
| agent.py (7 rows) | 3 | 1 | 3 |
| Frozen-brainstem extras (5 rows) | 3 | 0 | 2 |
| **Total (28 rows)** | **15** | **5** | **8** |

The biggest gap is the one that matters most: **an arbitrary agent.py does not run in Copilot Studio**. It is
carried as a reasoning-only skill. Real functional parity for custom agents needs the agent hosted as an
MCP server (see agent.py rows 4 and 5).

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
| 12 | Settings in `.env` (read by agents, `requires_env`) | Environment variables (`upsertEnvironmentVariable`) | **gap** | The SDK proves the operation. brainfreeze-studio records the setting *names* but doesn't create the variables yet. |
| 13 | Voice mode (`\|\|\|VOICE\|\|\|` split) | — | **gap** | No harness equivalent is mapped. Teams and M365 channels handle speech themselves. |
| 14 | Channels: the web UI, any `/chat` client | Teams and Microsoft 365 Copilot (`setChannels` + publish) | **proven** | Declared and published. The portal builds the Teams app package on first publish. |
| 15 | Health and introspection (`/health`) | `assertHarnessAgent` + `listComponents` readback | **proven** | The SDK reads back template, instructions, published state and every component. |
| 16 | Runs anywhere Python runs; the user owns their instance | A tenant-owned agent in a Power Platform environment | **gap** | This is the tier change itself, not a bug. Needs pac + Entra + an environment. |

## agent.py

| # | agent.py | Copilot Studio harness | Status | Notes |
|---|---|---|---|---|
| 1 | Contract: `metadata` `name` / `description` / `parameters` | Skill frontmatter and input contract; tool descriptions | **built** | Read statically (the egg's code never runs during a build): `test_contract_is_read_without_running_egg_code`. |
| 2 | HackerNews agent (`perform` calls the HN API) | Custom connector + agent flow (`WorkflowTool`) + fetch-hacker-news skill | **proven** / **built** | The tutorial proved live stories through the flow. Needs `--hn-api-name` (the environment's connector). Flow ids match the SDK's (`test_the_sdk_reads_the_workspace_and_agrees_on_flow_ids`). |
| 3 | ManageMemory / ContextMemory agents | Dataverse Add row / List rows `ConnectorTool`s + manage-memory / recall-memory skills | **proven** / **built** | Needs `--environment` (the org URL). Without it the agent falls back to reasoning-only, and the build says so. |
| 4 | **Any other agent.py** (custom Python in `perform`) | `InlineAgentSkill` that carries the source as reference and must never claim it ran | **approximated** | Studio reasons about the code but doesn't execute it. There's no functional parity: results are explanations, not computed outputs. |
| 5 | agent.py executed for real | `McpTool` → the agent hosted as an MCP server (for example the Tier 2 Azure Function) | **gap** | McpTool is proven in the SDK use cases. Hosting arbitrary agent.py files behind MCP isn't built. **This is the step that closes row 4.** |
| 6 | An agent that calls another agent | `ConnectedAgentTool` (a child harness agent) | **gap** | Proven in the SDK use cases, but brainfreeze-studio doesn't lay multi-agent brainstems as parent + child yet. |
| 7 | Agents that call external APIs (with keys in `.env`) | One custom connector + connection per API | **gap** | Connection consent is portal-only; the SDK can reference connections but not create them. |

## Frozen-brainstem extras (what an egg adds)

| # | Egg | Copilot Studio harness | Status | Notes |
|---|---|---|---|---|
| 1 | Egg verification (rapp/1 §9) | Refused before anything is built | **built** | `test_tampered_or_wrong_eggs_are_refused`. |
| 2 | Lineage: `rappid` + egg address | `provenance.json` beside the workspace | **built** | Not written onto the agent record yet. |
| 3 | Session egg (the conversation) | `proof.json` for `prove-usecase.mjs` + `reference-answers.json` | **built** | The prompts are carried. `expect` regexes start empty: you add the checks, and the reference answers show what the brainstem said. |
| 4 | Memory in the egg | `memory-seed.json` (normalized rows) | **gap** | Exported, but not yet written into Dataverse. Seeding would use the same Add-row shape as the memory profile. |
| 5 | Proof of parity (brainstem vs Studio, same prompts) | brainfreeze `replay` + `prove-usecase.mjs` side by side | **gap** | Both halves exist; the combined report doesn't. Running it needs an Entra app with `CopilotStudio.Copilots.Invoke`. |

## What would move the score

In order of how much parity each one buys:

1. **agent.py as MCP** (agent.py rows 4 and 5). Host the egg's agents behind an MCP server and lay each as an
   `McpTool`. This turns every reasoning-only skill into a tool that really runs.
2. **The side-by-side parity report** (extras row 5). The same prompts go to the thawed brainstem and the Studio
   agent, and the answers are compared.
3. **Memory seeding** (extras row 4) and **environment variables** (runtime row 12). Both use operations the SDK
   already proves.
4. **Parent + child agents** (agent.py row 6), for brainstems whose agents call one another.
