# brainfreeze-studio in an Azure Function

A person signs in with **their own account** and deploys a brainstem egg into a Copilot Studio environment they
can make agents in. The Function holds no service account and stores no tokens. Every change lands with the
signed-in user's rights.

```
browser page ──device-code sign-in (the user)──▶ Entra ID ──▶ delegated Dataverse token (kept in the page)
     │
     └──POST /api/deploy, Bearer <user token>──▶ Function: brainfreeze_studio.build(egg) → workspace
                                                            brainfreeze_studio.deploy(workspace, token)
                                                                 └──▶ Dataverse Web API of the user's environment
```

`brainfreeze_studio.deploy` needs no `pac`, `az` or Node. Each step is a Dataverse Web API call, the same calls
`pac copilot push` and `publish` make: rows in `bots` and `botcomponents`, plus the `PvaPublish` message.

## What the user needs

- A Copilot Studio maker role in the target environment. The Environment Maker security role works.
- The connections the agent's tools use, in that environment. A Dataverse connection covers the memory tools.
  The deploy binds to one of the user's existing connections and says which one is missing. Creating a connection
  needs the user's consent in Power Apps; no API can create one for them.

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
   - `BFS_ALLOW_TRANSLATIONS`: optional; see the note below.

3. Publish: `./publish.sh <function app name> [path to copilot-harness-sdk]`. This bundles brainfreeze_studio, the
   SDK's `tutorial/` assets and `azure-functions`, then runs `func azure functionapp publish`.

Open `https://<app>.azurewebsites.net/api/page`, sign in, pick an egg and deploy.

## Endpoints

| Route | Does |
|---|---|
| `GET /api/page` | The page: sign in, pick an egg, deploy. |
| `POST /api/signin` `{environment}` | Starts the user's device-code sign-in for that environment. |
| `POST /api/signin/poll` `{device_code}` | Returns `pending`, or the user's token (to the page, never stored). |
| `POST /api/deploy` | `Authorization: Bearer <user token>`, `{environment, name, egg (base64) \| eggUrl, schemaName?, hnApiName?, translations?}` → the deploy summary, the maker URL and the log. |

The deploy accepts only a delegated user token whose audience is the environment being deployed to. An app-only
token is refused.

## Notes

- **Translations run the egg's code.** Their parity proof runs the agent in a subprocess, so they are off unless
  `BFS_ALLOW_TRANSLATIONS=true`. Turn them on only where you trust the eggs, or build in an isolated job. Without
  translations, a build reads agent code without running it.
- **Keep deploys small.** An HTTP request is cut off at 230 seconds. A small agent deploys in about two minutes;
  a large library (tens of flows) needs a queue-triggered deploy.
- **Private storage works.** In a subscription whose policy forces private storage and no shared keys, run the app
  with VNet integration, storage private endpoints (blob, queue, table) and managed-identity storage
  (`AzureWebJobsStorage__accountName`).
