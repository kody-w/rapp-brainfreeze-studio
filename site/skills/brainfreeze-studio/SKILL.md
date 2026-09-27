---
name: brainfreeze-studio
description: Put a person's RAPP agent (their Brainstem, a RAPP Store app, or the built-in Invoice Router sample) into their own Copilot Studio environment with brainfreeze studio, prove its logic, and show it answering, without asking them to run commands.
---

# brainfreeze studio

brainfreeze studio turns a RAPP agent into a Copilot Studio agent. The agent's logic becomes agent flows (or
connector code), each proven against the agent's real Python before anything is deployed. What can't be proven
stays a reasoning-only skill, and the build says why. The deploy runs as the person, with their own sign-in, in an
environment they choose.

When someone asks you to put their agent, their Brainstem or a RAPP Store app in Copilot Studio, or points you at
https://kody-w.github.io/rapp-brainfreeze-studio/, do the whole job yourself. Don't hand commands back to them.
Their only jobs are to sign in, pick the environment, say yes, and say "publish it" if they want it published.

GitHub Copilot is the golden path. Claude Code, and any other AI agent that can run commands, follow this same file.

## Rules

1. **Say it, then do it.** Before you install anything, tell them in a few lines what you'll set up and where, and
   what you'll create in which environment. Wait for their yes.
2. **Their sign-in, their environment.** Sign in with the Azure CLI as the person. Deploy only to the environment
   they pick from their own list. Never use a service account, a client secret, an app-only token or anyone else's
   sign-in (brainfreeze studio refuses app-only tokens).
3. **Draft first.** Always deploy with `--draft`. Publish only when they ask, by running the same deploy again
   without `--draft`.
4. **Report the proof, don't invent one.** Quote the `parity:` lines the build prints. A translation that fails its
   proof is refused and that agent stays a skill: say so, with the reason. Never work around a refusal.
5. **Nothing destructive.** Don't delete agents, flows, connections or data, don't change environment settings,
   and don't deploy tenant-wide. A deploy creates what's missing and updates an agent of the same name in place
   (that's how a change ships), so a separate copy needs a new `--name`.
6. **No secrets in the open.** Never print, paste or save a token.

## 1. Set up the tools

Check `python3 --version` (3.9 or later), `git --version` and `az version`. If the Azure CLI is missing, install it
the usual way for their system, with their yes (macOS `brew install azure-cli`, Windows
`winget install -e --id Microsoft.AzureCLI`, Linux: Microsoft's package for their distribution). Node and npm are
needed only for a Power Apps code app; the .NET SDK isn't needed.

Everything lives in one work folder. Reuse it next time (`git -C rapp-brainfreeze-studio pull --ff-only`).

```bash
mkdir -p ~/brainfreeze-work && cd ~/brainfreeze-work
git clone https://github.com/kody-w/rapp-brainfreeze-studio.git
python3 -m venv .venv
cd rapp-brainfreeze-studio
```

Run every command below from inside `rapp-brainfreeze-studio`. `bfs` stands for the work folder's Python running
brainfreeze studio: `../.venv/bin/python -m brainfreeze_studio` (Windows: `..\.venv\Scripts\python.exe -m
brainfreeze_studio`).

## 2. Sign in and pick the environment

The person signs in with the account that has their Copilot Studio environment. Run this and let them finish in
the browser:

```bash
az login --allow-no-subscriptions
```

Add `--tenant <their tenant domain>` when the environment is in another tenant. If their usual Azure CLI sign-in is
a different account, keep a separate profile so you don't disturb it: set `AZURE_CONFIG_DIR=~/.azure-brainfreeze`
for every `az` and `bfs` command.

Then list their environments and ask which one to use:

```bash
bfs environments
```

It prints each environment's name, kind, region and URL. Use the chosen URL as `<environment>` below. If it lists
nothing, they signed in with an account that isn't in any environment: sign in again with the right one.

## 3. Build it (offline)

Ask what they're bringing, unless they already said.

**Their Brainstem** (it lives in `~/.brainstem/src/rapp_brainstem`). Freeze it into an egg, then build:

```bash
../.venv/bin/python -m pip install "git+https://github.com/kody-w/rapp-brainfreeze.git"
git -C .. clone https://github.com/kody-w/copilot-harness-sdk.git
../.venv/bin/python -m brainfreeze egg ~/.brainstem/src/rapp_brainstem --owner <github-login> --slug <short-name> --no-memory --out ../eggs
bfs build ../eggs/<github-login>--<short-name>.egg --name "<Agent name>" --publisher-prefix rapp --sdk-dir ../copilot-harness-sdk --translations translations/ --environment <environment> --out ../build/<short-name>
```

`<github-login>` is their lowercase GitHub login, `<short-name>` a lowercase-hyphen name, and the agent name 42
characters or fewer. Leave memory out (`--no-memory`) unless they ask for it: nothing deploys memory yet, so it would
only travel in the local egg. copilot-harness-sdk carries the proven profiles for the grail's HackerNews and memory
agents.

**A RAPP Store app.** Ask which one (the catalog is https://kody-w.github.io/RAPP_Store/) and use its
`@publisher/id`:

```bash
bfs rapplication <@publisher/id> --translations translations/ --out ../build/<id>
```

**Nothing of their own yet, or just trying it:** the Invoice Router sample.

```bash
bfs rapplication examples/rapplications/invoice_router --translations translations/ --out ../build/invoice-router
```

The build prints which agents became flows and which stayed skills, and a parity line per translated agent, for the
sample `parity:       InvoiceRouter 72/72 PROVEN`.

## 4. Show the plan and get the yes

Tell them, in a few lines: the environment's name, the agent's name, what becomes a flow (with its parity line) and
what stays a skill, that it's created as a Draft under their account, and that nothing is published. Wait for yes.

## 5. Deploy it as a Draft

**Their Brainstem:**

```bash
bfs deploy ../build/<short-name>/workspace --environment <environment> --draft
```

**A RAPP Store app or the sample:** run the same `rapplication` command again with the deploy options, for the
sample:

```bash
bfs rapplication examples/rapplications/invoice_router --translations translations/ --out ../build/invoice-router --environment <environment> --deploy --draft --no-app
```

Leave out `--no-app` only if they want the Power Apps code app too: that needs code apps turned on in the
environment, and node and npm here.

The deploy reads every component back from the environment and checks it's the agent it built. It prints
`status: Draft` and a `maker:` link, which opens the agent's test chat in Copilot Studio.

## 6. Show it answering

Open the `maker:` link. If you can drive a browser (Claude in Chrome, a browser MCP server), start a new chat there,
ask one question, wait for the reply (a new agent's first answer can take a minute) and show them the reply.
Otherwise give them the link and the question to ask.

- **The sample:** ask `Route an invoice from Fabrikam for $18,750.` Expand the agent's `Route an invoice` step: its
  Output is exactly what the Python returns, `Fabrikam $18,750.00: queue APPROVAL, needs AP manager sign-off (limit $10,000).`
  The reply below it puts that result in the agent's own words.
- **Their Brainstem:** ask a prompt from `../build/<short-name>/proof.json` when it has turns, and compare the reply
  with `reference-answers.json`; otherwise ask what it can help with.

A connector-code tool created a moment ago can answer 404 for a few minutes. Wait, then ask again before calling
anything a failure.

## 7. Publish, only when asked

Run the same deploy command again without `--draft`. It publishes the agent and prints when.

## 8. Report

Say where it is (the environment and agent name, with the `maker:` link), what became flows (with their parity lines)
and what stayed skills (with the build's reasons), and whether it's a Draft or published. The build stays in
`~/brainfreeze-work/build/` for next time; deploying it again updates the same agent.

## When something fails

- `bfs environments` fails with a sign-in error: sign in again (`az login --allow-no-subscriptions`, with `--tenant`
  when needed).
- The deploy answers HTTP 403: their account needs a maker role in that environment (Environment Maker or System
  Customizer). Their admin grants it; don't look for another account.
- `display name is N characters`: pick a name of 42 characters or fewer.
- `the live bot is not the harness agent this workspace describes`, or any other error: stop and report it as
  printed. Don't retry around it.

The README (https://github.com/kody-w/rapp-brainfreeze-studio) documents every command, and MAPPING.md shows how each
RAPP concept maps to Copilot Studio, with the evidence.
