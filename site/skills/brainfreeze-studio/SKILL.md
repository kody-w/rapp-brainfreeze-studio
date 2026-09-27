---
name: brainfreeze-studio
description: Put a person's RAPP agent (their Brainstem, a RAPP Store app, or the built-in Invoice Router sample) into their own Copilot Studio environment with brainfreeze studio, prove what can be proven, and show it answering, without asking them to run commands.
---

# brainfreeze studio

brainfreeze studio turns a RAPP agent into a Copilot Studio agent in the person's own environment. An agent with a
translation that brainfreeze studio ships (in its `translations/` folder) becomes an agent flow or connector code,
and the build runs it beside the agent's real Python on every test case and refuses any difference. The Brainstem's
own memory agents become Copilot Studio memory tools when their code is exactly the reviewed original. Every other
agent becomes a skill that works from its instructions, and the build says which and why. Writing new translations
is separate work; this setup doesn't do it. The deploy runs as the person, with their own sign-in.

When someone asks you to put their agent, their Brainstem or a RAPP Store app in Copilot Studio, or points you at
https://kody-w.github.io/rapp-brainfreeze-studio/, do the whole job yourself. Don't hand commands back to them.
Their only jobs are to sign in, pick the environment, approve the plan, and say "publish it" if they want it
published.

GitHub Copilot is the golden path. Claude Code, and any other AI agent that can run commands, follow this same file.

## Rules

1. **Say it, then do it.** Before installing anything, tell them in a few lines what you'll set up and where, and
   wait for their yes. Say that the build runs the agents' Python on this computer to prove each translation, in a
   separate process with a clean environment and a temporary home and working folder. That isn't a sandbox, so build
   only code they trust: their own Brainstem, the sample, or a Store app they chose.
2. **Their sign-in, their environment.** Sign in with the Azure CLI as the person. Deploy only to the environment
   they pick from their own list. Prefer a Developer or Sandbox environment. If they pick one whose kind is
   Production, say so and deploy there only if they confirm. Never use a service account, a client secret, an
   app-only token or anyone else's sign-in (brainfreeze studio refuses app-only tokens).
3. **Plan first, then deploy exactly what was planned.** Build once. Then run the deploy with `--plan`: it only reads
   the environment, changes nothing, and prints what the deploy would create, update or remove, with the build's
   `digest:`. Show that to the person and wait for their yes. Then deploy that same build with `--expect <digest>`,
   which refuses if the build changed after the plan. Never rebuild between the plan and the deploy.
4. **Draft first.** Deploy with `--draft`, so nothing is published. If the plan says the agent is already published,
   tell them before deploying: its flows change the moment you deploy, and the published agent uses them at once.
   Publish only when they ask.
5. **Never take over someone else's agent.** The plan names the agent's schema name, for example `rapp_InvoiceDesk`,
   and says whether the deploy creates it or updates it. Names that differ only in spaces or punctuation are the same
   agent. An update makes the agent's tools and skills the build's, and removes every one the build doesn't have,
   including any added by hand in Copilot Studio; the plan lists those under `remove:`. If the plan would update an
   agent the person didn't make with brainfreeze studio, pick a new `--name` and build again. If it would remove
   something they want to keep, add `--keep-extra` to both the plan and the deploy.
6. **Report the proof, don't invent one.** Quote the `parity:` lines the build prints. An agent that has no
   translation, or whose translation failed its proof, stays a skill. Say so, with the build's reason. Never work
   around a refusal.
7. **Nothing else, nothing secret.** Beyond what the plan lists, don't delete or change agents, flows, connections or
   data. Don't change environment settings or deploy tenant-wide. Never print, paste or save a token.

## 1. Set up the tools

Check `python3 --version` (3.9 or later), `git --version` and `az version`. Install whatever is missing the usual way
for their system, with their yes:
- macOS: `xcode-select --install` gives Python and git; `brew install azure-cli` gives the Azure CLI. If they don't
  have Homebrew, ask before installing it from https://brew.sh.
- Windows: `winget install -e --id Python.Python.3.12` (it brings the `py` launcher), `winget install -e --id Git.Git`
  and `winget install -e --id Microsoft.AzureCLI`. Open a new terminal afterwards so they're on the path.
- Linux: the distribution's `python3` and `git` packages, and Microsoft's Azure CLI package for the distribution.

Nothing else is needed for the sample or a RAPP Store app's agent. Node and npm are needed only if they also want a
RAPP Store app's Power Apps code app.

Everything lives in one work folder:

```bash
mkdir -p ~/brainfreeze-work && cd ~/brainfreeze-work
git clone https://github.com/kody-w/rapp-brainfreeze-studio.git
```

If `~/brainfreeze-work/rapp-brainfreeze-studio` is already there from an earlier run, update it instead of cloning:
`git -C ~/brainfreeze-work/rapp-brainfreeze-studio pull --ff-only`. If that fails, stop and tell them why. Never
delete or reset the folder.

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

The person signs in with the account that has their Copilot Studio environment. If their usual Azure CLI sign-in is a
different account, first give this sign-in a separate profile so you don't disturb theirs: set
`AZURE_CONFIG_DIR=~/.azure-brainfreeze` (in PowerShell, `$env:AZURE_CONFIG_DIR = "$HOME\.azure-brainfreeze"`) for
this and every later `az` and `python3 -m brainfreeze_studio` command. Then run this and let them finish in the
browser:

```bash
az login --allow-no-subscriptions
```

Add `--tenant <their tenant domain>` when the environment is in another tenant.

Then list their environments and ask which one to use:

```bash
python3 -m brainfreeze_studio environments
```

It prints each environment's name, kind, region and URL, usually within half a minute. Use the chosen URL as
`<environment>` below. If it lists nothing, they signed in with an account that isn't in any environment: sign in
again with the right one.

## 3. Build it (offline)

Ask what they're bringing, unless they already said.

**Their Brainstem** (it lives in `~/.brainstem/src/rapp_brainstem`). Get the freezer and copilot-harness-sdk once. If
`../.venv` and `../copilot-harness-sdk` are already there from an earlier run, skip these three commands and update
the SDK with `git -C ../copilot-harness-sdk pull --ff-only`. copilot-harness-sdk carries the reviewed profiles that
turn the Brainstem's own memory agents into Copilot Studio memory tools, when their code is exactly the reviewed
original. Its HackerNews agent stays a skill here: that profile also needs the environment's RAPP Hacker News
connector, which this setup doesn't create.

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

**A RAPP Store app.** Ask which one. The catalog is https://kody-w.github.io/RAPP_Store/. Its page loads the list
with JavaScript, so read the index instead: https://raw.githubusercontent.com/kody-w/RAPP_Store/main/index.json
lists each app under `rapplications`, with its `name`, `publisher` and `id`. Find the app by name.
- If two entries match, ask which publisher.
- Skip entries whose `access` is `private`, and complete applications with no `singleton_url`. brainfreeze studio
  can't build those; offer one it can.
The app's id for the command is `<publisher>/<id>`: the index's `id`, with its underscores, not its
`manifest_name`. For Markdown Medic that's `@rapp/markdown_medic`.

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

Plan the build you just made. The plan signs in and reads the environment, changes nothing, and prints what the
deploy would do, ending with `nothing was changed`.

**Their Brainstem:**

```bash
python3 -m brainfreeze_studio deploy ../build/<short-name>/workspace --environment <environment> --draft --plan
```

**A RAPP Store app:**

```bash
python3 -m brainfreeze_studio deploy ../build/<id>/workspace --environment <environment> --draft --plan
```

**The sample:**

```bash
python3 -m brainfreeze_studio deploy ../build/invoice-router/workspace --environment <environment> --draft --plan
```

Tell them, in a few lines:
- the environment's name and kind, and the agent's name and schema name;
- whether the plan creates a new agent or updates an existing one, anything it would remove, and whether the agent
  is already published;
- which connection each flow uses: theirs, or one it would make with their sign-in;
- what became a flow (with its parity line) and what stayed a skill, with the build's reason;
- that it's a Draft under their account and nothing is published.

If the plan refuses, or would update or remove something that isn't theirs, follow rule 5 and plan again. Wait for
their yes.

## 5. Deploy it as a Draft

Deploy the same build, with the `digest:` the plan printed as `<digest>`.

**Their Brainstem:**

```bash
python3 -m brainfreeze_studio deploy ../build/<short-name>/workspace --environment <environment> --draft --expect <digest>
```

**A RAPP Store app:**

```bash
python3 -m brainfreeze_studio deploy ../build/<id>/workspace --environment <environment> --draft --expect <digest>
```

**The sample:**

```bash
python3 -m brainfreeze_studio deploy ../build/invoice-router/workspace --environment <environment> --draft --expect <digest>
```

The deploy reads back what it wrote: the components and their content, the flows, their links and their
connections. It stops on any difference. It prints a `status:` line saying it's a Draft, and a `maker:` link, which
opens the agent's test chat in Copilot Studio.

## 6. Show it answering

Open the `maker:` link for them: `open "<link>"` on macOS, `start "" "<link>"` in the Windows command prompt (or
`Start-Process "<link>"` in PowerShell), `xdg-open "<link>"` on Linux. A new agent's test chat can take a minute to
load.

If you can drive their signed-in browser (for example Claude in Chrome), start a new chat there, ask the question
below, wait for the reply, and show them the reply you saw. Otherwise give them the question to type and what to look
for, and say plainly that you haven't seen the reply yourself. A link alone is not a verified answer.

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

Plan it again without `--draft`, and show them the plan: it says the agent will be published. Then run that deploy
without `--draft` and with the new plan's `--expect <digest>`. It publishes the agent and prints when.

## 8. Report

Say where it is: the environment and the agent's name, with the `maker:` link. Say what became flows (with their
parity lines) and what stayed skills (with the build's reasons), and whether it's a Draft or published.

The build stays in `~/brainfreeze-work/build/` for next time. Deploying again updates the same agent: build, plan,
then deploy.

## When something fails

- `environments` fails with a sign-in error: sign in again (`az login --allow-no-subscriptions`, with `--tenant` when
  needed).
- The plan or deploy answers HTTP 403: their account needs a maker role in that environment (Environment Maker or
  System Customizer). Their admin grants it; don't look for another account.
- `display name is N characters`: pick a name of 42 characters or fewer.
- `a classic agent` has the name: pick a new `--name`.
- `the build changed since the plan`: something rebuilt it after the plan. Plan again and show them the new plan.
- `an app-only token`: sign in as the person with `az login`, never with a service principal.
- `no connection for ...`: a flow needs a connection the person doesn't have. They create it in Power Apps
  (Connections) as themselves; then plan again. Never borrow someone else's.
- `the live bot is not the harness agent this workspace describes`, or any other error: stop and report it as printed.
  Don't retry around it.

The README (https://github.com/kody-w/rapp-brainfreeze-studio) documents every command, and MAPPING.md shows how each
RAPP concept maps to Copilot Studio, with the evidence.
