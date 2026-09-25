# brainfreeze-studio in an Azure Function

A person signs in with **their own account**, picks one of their Copilot Studio environments, and deploys a
brainstem egg into it, or a RAPP Store rapplication (its agent plus its UI as a Power Apps code app). The Function
has no service account, and holds a user's sign-in only while their deploy runs. Every change lands with the
signed-in user's rights.

```
browser page ──device-code sign-in (the user)──▶ Entra ID ──▶ delegated token + refresh token (kept in the page)
     │
     ├──GET /api/environments──▶ Global Discovery: the environments this user belongs to
     ├──POST /api/signin/environment (the one picked)──▶ a token for that environment + the user's rights there
     │
     └──POST /api/jobs, Bearer <user token>──▶ Function: brainfreeze_studio.build(egg) → workspace
                                                         brainfreeze_studio.deploy(workspace, token)
                                                              └──▶ Dataverse Web API of the user's environment
```

`brainfreeze_studio.deploy` needs no `pac`, `az` or Node. Each step is a Dataverse Web API call, the same calls
`pac copilot push` and `publish` make: rows in `bots` and `botcomponents`, plus the `PvaPublish` message.

## What the user needs

- A Copilot Studio maker role in the target environment. The Environment Maker security role works.
- The connections the agent's tools use, in that environment. A Dataverse connection covers the memory tools.
  The deploy binds to one of the user's existing connections and says which one is missing. It makes a connection
  itself only when no key or consent page is involved: for a connector that runs its own code, and for SharePoint
  when the job has the user's API Hub token (see Set up).
- For a rapplication's code app: code apps turned on in the environment, and the user's consent to PowerApps
  Service's `User` permission for this app registration (users can give it themselves; the page asks).

## Set up

1. Register a public client app with a delegated Dynamics CRM `user_impersonation` permission. Multi-tenant
   lets anyone sign in from their own tenant; users consent on first sign-in.

   ```bash
   az ad app create --display-name "brainfreeze-studio cloud deploy" --sign-in-audience AzureADMultipleOrgs \
     --is-fallback-public-client true \
     --required-resource-accesses '[{"resourceAppId":"00000007-0000-0000-c000-000000000000","resourceAccess":[{"id":"78ce3f0f-a1ce-49c2-8cde-64b5c0896db5","type":"Scope"}]}]'
   ```

2. Create a Python 3.11 Function App (Flex Consumption works), then set these app settings:
   - `BFS_CLIENT_ID`: the app registration's appId.
   - `BFS_TENANT`: `organizations`, or one tenant id.
   - `BFS_ALLOWED_TENANTS`: the tenant ids this deployment serves, comma-separated. Callers from any other tenant
     are refused. Leave it empty to serve every tenant, without translations.
   - `BFS_ALLOW_TRANSLATIONS`: optional, and only with `BFS_ALLOWED_TENANTS`; see the note below.

3. Publish: `./publish.sh <function app name> [path to copilot-harness-sdk]`. This bundles brainfreeze_studio, the
   SDK's `tutorial/` assets, the code app host (built with npm, which needs Node 18+) and `azure-functions`, then
   runs `func azure functionapp publish`.

Publishing a rapplication's code app uses PowerApps Service's delegated `User` permission, asked for at sign-in
(`POST /api/signin {purpose: "powerapps"}`), so it needn't be in the registration. Optional: for jobs to make a
user's SharePoint connection when they have none (agents that read files), add a delegated permission to Azure
API Hub (`https://apihub.azure.com`) to the registration. Without it, a job uses a SharePoint connection the user
already has. This path hasn't been run with a person's sign-in yet.

Open `https://<app>.azurewebsites.net/api/page`, sign in, pick an environment and an egg, and deploy.

## Signing in and picking an environment

1. **Sign in.** The page shows a device code; the user enters it at Microsoft's sign-in page with their own
   account. The sign-in is for the Dataverse Global Discovery Service, with a refresh token.
2. **Pick an environment.** The page lists the environments the user belongs to (Global Discovery, called with
   their own token): name, type and address, disabled ones left out. An environment that isn't listed can be
   entered by URL.
3. **The Function checks the pick.** It trades the refresh token for the user's token to that environment, has
   Dataverse accept it (`WhoAmI`), and reads whether the user's roles let them create what a deploy creates:
   agents, their tools and skills, flows, connection references and environment variables. When they can't, the
   page says what's missing (the Environment Maker role covers it).

One sign-in covers every environment in the user's tenant. To Microsoft Entra they are all the same Dataverse
resource, so the refresh token gets a token for whichever one is picked.

## Endpoints

| Route | Does |
|---|---|
| `GET /api/page` | The page: sign in, pick an environment and an egg, deploy. |
| `POST /api/signin` `{environment?}` | Starts the user's device-code sign-in: for Global Discovery, or for the environment named. |
| `POST /api/signin/poll` `{device_code}` | Returns `pending`, or the user's token and refresh token (to the page, never stored). |
| `GET /api/environments` | `Authorization: Bearer <discovery token>` → the environments the user belongs to: `{name, url, kind, region, id}`. |
| `POST /api/signin/environment` | `{environment, refreshToken}` → the user's token for that environment (and a new refresh token), `canMakeAgents` and what's `missing`. Nothing is stored. |
| `POST /api/deploy` | `Authorization: Bearer <user token>`, `{environment, name, egg (base64) \| eggUrl, schemaName?, hnApiName?, translations?}` → the deploy summary, the maker URL and the log. |
| `POST /api/jobs` | The same body, plus `translationsZip`, `workspaceZip` and `refreshToken` → `202 {job}`. A background deploy of any size. Or `{environment, rapplication: "@publisher/id", refreshToken, powerAppsToken?, filesSite?, filesFolder?, schemaName?, publisherPrefix?, store?}`: a RAPP Store rapplication. |
| `GET /api/rapplications` | The RAPP Store catalog: the rapplications a job can deploy (cached for ten minutes). |
| `GET /api/jobs/{job}` | `Authorization: Bearer <user token>` → the job's state (`queued`, `running`, `deploying`, `succeeded`, `failed`), its log and its result. Only the user who queued the job can read it. |

Every route that does work first has Dataverse check the caller's token (`WhoAmI` in the target environment), so
the Function acts only on a token Dataverse accepts. It must be a signed-in user's delegated token whose audience
is the environment being deployed to, from an allowed tenant. A made-up, app-only, expired or wrong-tenant token
gets a 401 before anything is built, fetched or queued (`auth.py`).

## Background jobs (big agents)

An HTTP request is cut off at 230 seconds. A library of tens of agents (the AIBAST Copilot has 72 flows) takes
longer, so `POST /api/jobs` returns at once and a queue trigger does the work, for up to an hour (`host.json`).
The page always uses jobs.

- **The user's sign-in is held only while the job runs.** It sits in the job's request blob, which only the
  Function's managed identity can reach (private storage, no shared keys). The blob is deleted when the job ends,
  and a lifecycle rule on the `bfs-deploy-jobs/` container deletes anything older than a day that a crashed job
  left behind.
- **Long jobs need a refresh token.** With one (the page sends the one from its sign-in), a job outlasts the
  access token, which lives about an hour. Without one, the job must finish before that token expires.
- **A stopped job resumes when run again.** Deploys are idempotent, so the second run only does what's left.
- **Failed jobs aren't retried.** Each job runs once (`maxDequeueCount: 1`), one at a time per instance, and the
  outcome is in the job's status. A job whose host stopped it (the time limit, or a restart) reads as failed once
  it is past the time limit, so the page stops waiting for it.

Storage is reached over its REST API with the managed identity's token, so `jobs.py`, like the rest of the app,
needs nothing beyond the standard library and `azure-functions`.

## Rapplications (an agent with its UI)

A rapplication job prepares everything first (`brainfreeze_studio.rapplication.prepare`): the rapplication egg,
the agent's workspace, Power Apps copies of the flows its UI calls, a chat flow when the UI talks to the agent
itself, and the code app. Then it deploys the agent and those flows, and publishes the code app with the user's own
Power Apps token, which it gets with their refresh token.

- **No consent yet:** the agent and its flows deploy, and the job's result says the app is waiting. After the
  sign-in that asks for the permission, running the job again publishes it.
- **Agents that read files**, when their connector-code port is among the job's translations, read them from
  SharePoint: `filesSite` and `filesFolder` set where (default: the environment's RAPP Files Site, else the
  tenant's root site, and `/Shared Documents`).
- **Translations:** a job without its own uses the repo's (`translations/`, bundled by `publish.sh`). Proofs that
  run the rapplication's code run only with `BFS_ALLOW_TRANSLATIONS`, as for eggs. The Function has no .NET SDK, so
  a connector-code port is laid on the proof recorded for its exact bytes (`<port>.proof.json`), which runs
  nothing. The job asks for the user's Power Apps token when the agent runs connector code, which needs a
  connection.

On 25 Sep 2026 a job deployed BookFactory this way in 54 seconds (agent, flows and a published code app), and the
app ran in the Power Apps player. Another job deployed JSON Doctor in 148 seconds, with its connector code on the
recorded proof and the user's existing SharePoint connection; 7/7 runs of its flow equal the Python. A connector
made for the first time took about two minutes before its code answered (404 until then).

## Notes

- **Translations run the egg's code.** Their parity proof runs the agent in a subprocess of the Function, which
  also holds the sign-ins of other users' running jobs. So translations are on only when
  `BFS_ALLOW_TRANSLATIONS=true` and `BFS_ALLOWED_TENANTS` names the tenants whose eggs you trust. Otherwise they
  are refused. Without translations, a build reads agent code without running it.
- **Use `/api/deploy` for small agents only.** It answers in one request, which suits an agent that deploys within
  about two minutes. Use `/api/jobs` for anything bigger.
- **Private storage works.** In a subscription whose policy forces private storage and no shared keys, run the app
  with VNet integration, storage private endpoints (blob, queue, table) and managed-identity storage
  (`AzureWebJobsStorage__accountName`).
