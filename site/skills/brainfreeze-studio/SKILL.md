---
name: brainfreeze-studio
description: Put a person's RAPP agent (their Brainstem, a RAPP Store app, or the built-in Invoice Router sample) into their own Copilot Studio environment with brainfreeze studio, prove what can be proven, and show it answering, without asking them to run commands.
---

# brainfreeze studio

brainfreeze studio turns a RAPP agent into a Copilot Studio agent in the person's own environment. Wherever an agent
has a translation (a proven profile, a spec written for it, or one materialized from its recorded outputs), the
agent's logic becomes an agent flow or connector code, and the build runs it beside the agent's real Python on every
test case and refuses any difference. Every other agent becomes a skill that works from its instructions, and the
build says which and why. The deploy runs as the person, with their own sign-in.

When someone asks you to put their agent, their Brainstem or a RAPP Store app in Copilot Studio, or points you at
https://kody-w.github.io/rapp-brainfreeze-studio/, do the whole job yourself. Don't hand commands back to them.
Their only jobs are to sign in, pick the environment, approve the plan, and say "publish it" if they want it
published.

GitHub Copilot is the golden path. Claude Code, and any other AI agent that can run commands, follow this same file.

## Rules

1. **Say it, then do it.** Before installing anything, tell them in a few lines what you'll set up and where, and
   wait for their yes.
2. **Their sign-in, their environment.** Sign in with the Azure CLI as the person. Deploy only to the environment
   they pick from their own list. Prefer a Developer or Sandbox environment. If they pick one whose kind is
   Production, say so and deploy there only if they confirm. Never use a service account, a client secret, an
   app-only token or anyone else's sign-in (brainfreeze studio refuses app-only tokens).
3. **Plan first.** Before every deploy, run the same command with `--plan`. It only reads the environment, changes
   nothing, and prints what the deploy would create, update or remove. Show that to the person and wait for their yes.
4. **Draft first.** Deploy with `--draft`. Publish only when they ask.
5. **Never take over someone else's agent.** A deploy updates an agent of the same name in place, including removing
   tools it doesn't have. If the plan says it would update an agent the person didn't make with brainfreeze studio,
   or remove something they want to keep, pick a new `--name` instead.
6. **Report the proof, don't invent one.** Quote the `parity:` lines the build prints. An agent that has no
   translation, or whose translation failed its proof, stays a skill. Say so, with the build's reason. Never work
   around a refusal.
7. **Nothing destructive, nothing secret.** Don't delete agents, flows, connections or data. Don't change environment
   settings or deploy tenant-wide. Never print, paste or save a token.

## 1. Set up the tools

Check `python3 --version` (3.9 or later), `git --version` and `az version`. Install whatever is missing the usual way
for their system, with their yes:
- macOS: `xcode-select --install` gives Python and git; `brew install azure-cli` gives the Azure CLI.
- Windows: `winget install -e --id Python.Python.3.12`, `winget install -e --id Git.Git` and
  `winget install -e --id Microsoft.AzureCLI`.
- Linux: the distribution's `python3` and `git` packages, and Microsoft's Azure CLI package for the distribution.

Nothing else is needed for the sample or a RAPP Store app's agent. Node and npm are needed only if they also want a
RAPP Store app's Power Apps code app.

Everything lives in one work folder:

```bash
mkdir -p ~/brainfreeze-work && cd ~/brainfreeze-work
git clone https://github.com/kody-w/rapp-brainfreeze-studio.git
```

If the folder is already there from an earlier run, update it instead: `git -C rapp-brainfreeze-studio pull --ff-only`.

Run every command below from `~/brainfreeze-work/rapp-brainfreeze-studio`. If your shell doesn't keep its folder
between commands, start each one with `cd ~/brainfreeze-work/rapp-brainfreeze-studio &&`. brainfreeze studio has no
dependencies and needs no install: `python3 -m brainfreeze_studio` runs it from there.

**On Windows (PowerShell),** change every command below in these ways:
- Use `py -3` wherever it says `python3`, and `..\.venv\Scripts\python.exe` wherever it says `../.venv/bin/python`.
- Use `$HOME\...` for `~/...`. Make the work folder with
  `New-Item -ItemType Directory -Force "$HOME\brainfreeze-work"; Set-Location "$HOME\brainfreeze-work"`.
- Quote a RAPP Store id, as in `'@rapp/markdown_medic'`. An unquoted argument that starts with `@` is PowerShell
  syntax.
- Windows PowerShell 5.1 has no `&&`: run the commands one at a time, or join them with `;`.

## 2. Sign in and pick the environment

The person signs in with the account that has their Copilot Studio environment. Run this and let them finish in the
browser:

```bash
az login --allow-no-subscriptions
```

Add `--tenant <their tenant domain>` when the environment is in another tenant. If their usual Azure CLI sign-in is a
different account, keep a separate profile so you don't disturb it: set `AZURE_CONFIG_DIR=~/.azure-brainfreeze` for
every `az` and `python3 -m brainfreeze_studio` command.

Then list their environments and ask which one to use:

```bash
python3 -m brainfreeze_studio environments
```

It prints each environment's name, kind, region and URL, usually within half a minute. Use the chosen URL as
`<environment>` below. If it lists nothing, they signed in with an account that isn't in any environment: sign in
again with the right one.

## 3. Build it (offline)

Ask what they're bringing, unless they already said.

**Their Brainstem** (it lives in `~/.brainstem/src/rapp_brainstem`). Get the freezer and copilot-harness-sdk once
(next time, `git -C ../copilot-harness-sdk pull --ff-only`). copilot-harness-sdk carries the proven profiles for the
Brainstem's own HackerNews and memory agents.

```bash
python3 -m venv ../.venv
../.venv/bin/python -m pip install "git+https://github.com/kody-w/rapp-brainfreeze.git"
git -C .. clone https://github.com/kody-w/copilot-harness-sdk.git
```

On Debian or Ubuntu, `python3 -m venv` needs the `python3-venv` package: install it with their yes.

Freeze their Brainstem into an egg, then build it. Installing the freezer and freezing each take about half a
minute; the build takes seconds.

```bash
../.venv/bin/python -m brainfreeze egg ~/.brainstem/src/rapp_brainstem --owner <github-login> --slug <short-name> --no-memory --out ../eggs
python3 -m brainfreeze_studio build ../eggs/<github-login>--<short-name>.egg --name "<Agent name>" --publisher-prefix rapp --sdk-dir ../copilot-harness-sdk --translations translations/ --environment <environment> --out ../build/<short-name>
```

- `<github-login>`: their GitHub login, in lowercase.
- `<short-name>`: a lowercase-hyphen name.
- The agent name: 42 characters or fewer.

Leave memory out (`--no-memory`) unless they ask for it. Nothing deploys memory yet, so it would only travel in the
local egg.

**A RAPP Store app.** Ask which one. The catalog is https://kody-w.github.io/RAPP_Store/, and its index,
https://raw.githubusercontent.com/kody-w/RAPP_Store/main/index.json, lists each app under `rapplications` with its
`name`, `publisher` and `id`.
The app's id for the command is `<publisher>/<id>` (the index's `id`, with its underscores, not its
`manifest_name`); for Markdown Medic that's `@rapp/markdown_medic`.

```bash
python3 -m brainfreeze_studio rapplication <@publisher/id> --translations translations/ --no-app --out ../build/<id>
```

Leave out `--no-app` (here and in the deploy commands) only if they also want the app's screen as a Power Apps code
app. That needs Node and npm here, and code apps turned on in the environment.

**Nothing of their own yet, or just trying it:** the Invoice Router sample.

```bash
python3 -m brainfreeze_studio rapplication examples/rapplications/invoice_router --translations translations/ --no-app --out ../build/invoice-router
```

Add `--name "<Agent name>"` to either command when they want a different name from the app's own.

The build prints which agents became flows and which stayed skills, and a parity line per translated agent. For the
sample: `parity:       InvoiceRouter 72/72 PROVEN`.

## 4. Show the plan and get the yes

Run the deploy command you'll use in step 5 with `--plan` added. It signs in and reads the environment, changes
nothing, and prints what the deploy would do, ending with `nothing was changed`.

**Their Brainstem:**

```bash
python3 -m brainfreeze_studio deploy ../build/<short-name>/workspace --environment <environment> --draft --plan
```

**A RAPP Store app:**

```bash
python3 -m brainfreeze_studio rapplication <@publisher/id> --translations translations/ --no-app --out ../build/<id> --environment <environment> --deploy --draft --plan
```

**The sample:**

```bash
python3 -m brainfreeze_studio rapplication examples/rapplications/invoice_router --translations translations/ --no-app --out ../build/invoice-router --environment <environment> --deploy --draft --plan
```

If you added `--name` to the build, add it to these commands too.

Tell them, in a few lines:
- the environment's name and kind, and the agent's name;
- whether the plan creates a new agent or updates an existing one, and anything it would remove;
- what became a flow (with its parity line) and what stayed a skill, with the build's reason;
- that it's created as a Draft under their account and nothing is published.

If the plan refuses, or would update or remove something that isn't theirs, choose a new `--name` and plan again
(rule 5). Wait for their yes.

## 5. Deploy it as a Draft

Run the same command without `--plan`.

**Their Brainstem:**

```bash
python3 -m brainfreeze_studio deploy ../build/<short-name>/workspace --environment <environment> --draft
```

**A RAPP Store app:**

```bash
python3 -m brainfreeze_studio rapplication <@publisher/id> --translations translations/ --no-app --out ../build/<id> --environment <environment> --deploy --draft
```

**The sample:**

```bash
python3 -m brainfreeze_studio rapplication examples/rapplications/invoice_router --translations translations/ --no-app --out ../build/invoice-router --environment <environment> --deploy --draft
```

The deploy reads every component back from the environment and checks it's the agent it built. It prints
`status: Draft` and a `maker:` link, which opens the agent's test chat in Copilot Studio.

## 6. Show it answering

Open the `maker:` link for them: `open "<link>"` on macOS, `start "" "<link>"` in the Windows command prompt (or
`Start-Process "<link>"` in PowerShell), `xdg-open "<link>"` on Linux. A new agent's test chat can take a minute to
load.

If you can drive a browser (Claude in Chrome, a browser MCP server), start a new chat there, ask the question below,
wait for the reply, and show it to them. Otherwise, tell them the question to type and what to look for.

- **The sample:** ask `Route an invoice from Fabrikam for $18,750.` Expand the agent's `Route an invoice` step: its
  Output is exactly what the Python returns, `Fabrikam $18,750.00: queue APPROVAL, needs AP manager sign-off (limit $10,000).`
  The reply below it puts that result in the agent's own words.
- **Their Brainstem:** ask a prompt from `../build/<short-name>/proof.json` when it has turns, and compare the reply
  with `reference-answers.json`. Otherwise ask `What can you help me with?` That's a smoke test, not a proof: the
  reply should describe what its agents do, and a skill must never claim it ran code, fetched live data or remembered
  anything, because it works from its instructions.
- **A RAPP Store app:** ask what it can help with, or the kind of question its catalog entry describes.

A connector-code tool created a moment ago can answer 404 for a few minutes. Wait, then ask again before calling
anything a failure.

## 7. Publish, only when asked

Run the deploy command again without `--draft`. It publishes the agent and prints when.

## 8. Report

Say where it is: the environment and the agent's name, with the `maker:` link. Say what became flows (with their
parity lines) and what stayed skills (with the build's reasons), and whether it's a Draft or published.

The build stays in `~/brainfreeze-work/build/` for next time. Deploying it again updates the same agent: plan first.

## When something fails

- `environments` fails with a sign-in error: sign in again (`az login --allow-no-subscriptions`, with `--tenant` when
  needed).
- The plan or deploy answers HTTP 403: their account needs a maker role in that environment (Environment Maker or
  System Customizer). Their admin grants it; don't look for another account.
- `display name is N characters`: pick a name of 42 characters or fewer.
- `a classic agent` has the name: pick a new `--name`.
- `the live bot is not the harness agent this workspace describes`, or any other error: stop and report it as printed.
  Don't retry around it.

The README (https://github.com/kody-w/rapp-brainfreeze-studio) documents every command, and MAPPING.md shows how each
RAPP concept maps to Copilot Studio, with the evidence.
