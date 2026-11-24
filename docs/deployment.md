# Deploying MomentLens to Azure

`infra/` (Bicep) and `scripts/deploy.sh` create the whole solution in one resource group of your subscription. Nothing
outside the template is referenced, and no key, password, connection string or SAS token is committed to the repository
or returned by the template: the web app, the job and Video Indexer use one user-assigned managed identity with Azure
RBAC, and storage shared keys, AI Search API keys and Azure OpenAI keys are disabled.

Two values are still handed over by Azure itself while the deployment runs, and never written to a file or an output:
the Log Analytics workspace key, given to the Container Apps environment for its logs, and the Application Insights
connection string, set as an application setting of the web app for telemetry. At run time the API also mints
short-lived, read-only SAS links with a user delegation key (no account key exists) for Video Indexer and `ffmpeg`.

## What gets created

| Resource | Notes |
|---|---|
| Virtual network `10.40.0.0/16` | Subnets for App Service integration, Container Apps and private endpoints. |
| Storage account | Data Lake (hierarchical namespace), LRS, Entra ID only. Containers `videos`, `insights`, `thumbnails`; the API creates the job-state table on first use. Reached privately by the web app and the job. The public endpoint is closed to the internet; only trusted Azure services (Video Indexer) and any `STORAGE_ALLOWED_IPS` get through. |
| Private endpoints and private DNS zones | Blob and table. |
| Azure AI Search (Basic) | Index `moments` is created by the API. Semantic ranker enabled. |
| Azure OpenAI (S0) | Deployments `chat`, `embedding` and `judge`. |
| Azure AI Video Indexer | ARM account connected to the storage account through the managed identity. |
| Container Registry (Basic) | Admin user disabled; images are pulled with the managed identity. |
| App Service plan (Linux, P0v3) and web app | One container: FastAPI plus the React UI. One instance only, because the API runs a single worker. |
| Container Apps environment and job `vi-poller` | Manual trigger, started by the API; waits for Video Indexer to finish a video. |
| Log Analytics, Application Insights, workbook | The workbook charts runs, latency, estimated cost and the job log. |
| User-assigned managed identity and role assignments | Storage, Search, OpenAI, Video Indexer, registry pull and job start. |

Main cost drivers: the App Service plan and the Search service (billed while they exist), Video Indexer (per minute of
video indexed), Azure OpenAI (per token; the judge model reads video frames), Container Apps (only while a job
runs), private endpoints, the registry, storage and log ingestion. See the Azure pricing pages for current rates.

## Prerequisites

* An Azure subscription where you are **Owner**, or **Contributor** plus **User Access Administrator**. The template
  creates role assignments.
* The [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) 2.60 or later, signed in with `az login`.
  Bicep is installed on first use; run `az bicep upgrade` if compilation reports unknown functions.
* A shell with Bash: Azure Cloud Shell, Linux, macOS or WSL (not Windows PowerShell). Docker is optional
  (`USE_LOCAL_DOCKER=1`).
* These resource providers: `Microsoft.VideoIndexer`, `Microsoft.CognitiveServices`, `Microsoft.Search`,
  `Microsoft.App`, `Microsoft.Web`, `Microsoft.Storage`, `Microsoft.Network`, `Microsoft.ContainerRegistry`,
  `Microsoft.OperationalInsights`, `Microsoft.Insights`, `Microsoft.ManagedIdentity`. `scripts/deploy.sh` registers any
  that are missing (skip with `--no-register`); to do it by hand use `az provider register --namespace <name>`.
* **Region.** The default is `westeurope`. The region must offer all of the services above.
* **Azure OpenAI quota.** The defaults are `gpt-4.1-mini` (chat, 50), `text-embedding-3-large` (embedding, 50) and
  `gpt-4.1` (judge, 100), all `GlobalStandard`; capacity is in thousands of tokens per minute. Check that your
  subscription has the quota and that the models are offered in the region (see "Troubleshooting").
* **App Service quota.** Premium v3 plans need quota in the region; some new subscription types start without it.

## Deploy

```bash
az login
./scripts/deploy.sh                      # defaults: environment "momentlens", westeurope, resource group rg-momentlens
```

Cloud Shell works the same way (`git clone` the repository, run the script). The script:

1. checks the login and the subscription, and registers the resource providers;
2. runs `az deployment sub create` with placeholder images (the registry has to exist before images can be built);
3. copies the `node:20-alpine` and `python:3.12-slim` base images into the registry as `cache/...` (best effort; this
   avoids Docker Hub anonymous pull limits, and the builds fall back to the public images if the copy fails);
4. builds `momentlens/api` and `momentlens/vi-poller` with `az acr build` under an immutable tag
   (`<git sha>-<UTC timestamp>`), or with local Docker and `docker push` when `USE_LOCAL_DOCKER=1`;
5. deploys again with that tag and prints the site URL.

The first run takes a while (15 minutes or more); later runs are incremental. Re-running the script keeps the site
online (it skips the placeholder step once the registry exists) and deploys a new image tag, so it is also how you
publish a new version of the application. `./scripts/deploy.sh --help` lists every option, for example
`--what-if`, `--infra-only`, `--skip-build --image-tag <tag>` and `--no-user-roles`.

Without the script: `az deployment sub create --location <region> --parameters infra/main.bicepparam` (placeholder
images), build and push the two images, then run it again with `IMAGE_TAG=<tag>` exported. The deployment name used by
the script is `momentlens-<environment>-<region>`; `az deployment sub show --name <that name> --query properties.outputs`
lists the endpoints and names.

### Parameters

`infra/main.bicepparam` reads environment variables; unset means the default. The script has flags for the common
ones (`--help`) and exports the rest as they are.

| Variable | Default | Meaning |
|---|---|---|
| `AZURE_ENV_NAME` | `momentlens` | Seeds the resource-name token (`ml-<kind>-<token>`) and the `environment` tag. Use another name for a second environment in the same subscription and region. |
| `AZURE_LOCATION` | `westeurope` | Region. A resource group cannot change region. |
| `AZURE_RESOURCE_GROUP` | `rg-momentlens` | Created if missing. |
| `AZURE_SUBSCRIPTION_ID` | current | Subscription id or name (script only). |
| `ALLOWED_CLIENT_IPS` | empty (open) | Comma-separated IPs or CIDRs allowed to reach the site. |
| `STORAGE_ALLOWED_IPS` | empty | IPv4 addresses or CIDRs allowed to reach storage directly (local development). |
| `AZURE_PRINCIPAL_ID`, `AZURE_PRINCIPAL_TYPE` | signed-in user, `User` | Who gets data-plane roles for local development. Set `AZURE_PRINCIPAL_ID=` (empty) or pass `--no-user-roles` to skip. |
| `IMAGE_TAG` | empty | Empty deploys placeholder images. |
| `APP_SERVICE_PLAN_SKU` | `P0v3` | `P0v3`, `P1v3`, `P2v3` or `P3v3`. |
| `MAX_UPLOAD_MB`, `MAX_UPLOAD_MINUTES` | `500`, `30` | Upload limits enforced by the API. |
| `SEARCH_SEMANTIC_TIER` | `free` | `disabled`, `free` (capped monthly quota) or `standard` (billed beyond the free quota). |
| `CHAT_MODEL_NAME`, `_VERSION`, `CHAT_SKU_NAME`, `CHAT_CAPACITY` | `gpt-4.1-mini`, `2025-04-14`, `GlobalStandard`, `50` | Scene summaries and query rewriting. |
| `CHAT_REASONING_EFFORT` | empty | For a reasoning model as `chat`, for example `low`; empty for classic chat models (the API then sends `temperature` and `max_tokens`). |
| `EMBEDDING_MODEL_NAME`, `_VERSION`, `EMBEDDING_SKU_NAME`, `EMBEDDING_CAPACITY` | `text-embedding-3-large`, `1`, `GlobalStandard`, `50` | Must produce 3072-dimension vectors (the index schema). |
| `JUDGE_MODEL_NAME`, `_VERSION`, `JUDGE_SKU_NAME`, `JUDGE_CAPACITY` | `gpt-4.1`, `2025-04-14`, `GlobalStandard`, `100` | Must accept images. |
| `JUDGE_REASONING_EFFORT` | empty | For a reasoning model, for example `low`, `medium` or `high`; empty for classic chat models. |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | API version the application uses. Reasoning parameters (`reasoning_effort`, `max_completion_tokens`) need a newer one, for example `2025-04-01-preview` or later. |
| `PROMPT_CONTENT_MODEL` | empty | Model for Video Indexer's segment summaries; empty lets Video Indexer choose. |

The deployment names `chat`, `embedding` and `judge` are fixed because the API addresses them by name. Models are
retired on a schedule Microsoft publishes (see "Azure OpenAI model retirements" in the Microsoft Learn docs): check it
before you deploy and override the model variables when a version is retired or not available to you. The defaults
(`gpt-4.1` and `gpt-4.1-mini`) are scheduled for deprecation in 2027, after which new deployments of them are no
longer possible. Moving to a newer vision-capable family only takes variables, for example (check the names, versions
and regions in your subscription first):

```bash
CHAT_MODEL_NAME=gpt-5.4-mini CHAT_MODEL_VERSION=2026-03-17 CHAT_REASONING_EFFORT=low \
JUDGE_MODEL_NAME=gpt-5.4 JUDGE_MODEL_VERSION=2026-03-05 JUDGE_REASONING_EFFORT=low \
AZURE_OPENAI_API_VERSION=2025-04-01-preview ./scripts/deploy.sh
```

Use `DataZoneStandard` instead of `GlobalStandard` to keep inference inside a data zone.

## Protect the site

**The site is open to anyone who has the URL**: there is no sign-in in the application, and anyone can upload videos
and run searches at your expense. Do at least one of the following before you share the URL.

1. **IP allow-list.** `ALLOWED_CLIENT_IPS="203.0.113.7,198.51.100.0/24" ./scripts/deploy.sh` adds one allow rule per
   entry to the web app (the same rules apply to its Kudu/SCM site) and denies everything else. Remove the variable
   to open the site again.
2. **Microsoft Entra authentication** (App Service Authentication, "Easy Auth"), configured in the Azure portal; the
   template does not manage it, so redeployments keep it:
   * Open the web app, then **Settings > Authentication > Add identity provider**.
   * Choose **Microsoft**, tenant type **Workforce configuration (current tenant)**, let the portal create a new app
     registration, and keep **Require authentication** with the redirect option for unauthenticated requests.
   * To limit sign-in to specific people, open the app in **Microsoft Entra ID > Enterprise applications**, set
     **Properties > Assignment required** to **Yes**, and add the users or groups under **Users and groups**.

   After enabling it, check that the web app still reports healthy under **Monitoring > Health check**. Scripts that call
   the HTTP API without signing in (such as `scripts/smoke_test.py`) can no longer reach it.

Both options can be combined. Azure OpenAI, AI Search and the registry have public endpoints that accept only Entra
ID tokens (no keys exist); the storage account is closed to the internet.

## Verify

```bash
curl https://<site>/api/health                        # {"status":"ok", ...}
python scripts/smoke_test.py --base-url https://<site> # health and library; add --video to run the whole flow
```

The first start pulls the image and can take a few minutes. A page titled "Your web app is running and waiting for
your content" means the placeholder image is still deployed: run the full script. In the portal, **Application
Insights > Workbooks** has the "MomentLens - Pipeline cost and latency" workbook. Console output of the `vi-poller`
job is in the Log Analytics table `ContainerAppConsoleLogs_CL` (filter `ContainerGroupName_s startswith 'ml-vi-poller'`).
The workbook shows those rows once the job has run; application logs are in Application Insights (`traces`).

## Run the API locally

The script grants your signed-in user the Storage, Search, OpenAI and Video Indexer roles (`AZURE_PRINCIPAL_ID`).
Storage is closed to the internet, so also pass your public IPv4 address:
`STORAGE_ALLOWED_IPS=<your ip> ./scripts/deploy.sh` (adding it by hand with `az storage account network-rule add`
works too, but the next deployment removes rules the template does not list). Then copy `api/.env.example` to
`api/.env`, fill it from the deployment outputs and sign in with `az login`.

| Output | `api/.env` |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | `AZURE_SUBSCRIPTION_ID` |
| `AZURE_RESOURCE_GROUP` | `AZURE_VIDEO_INDEXER_RESOURCE_GROUP`, `VI_POLLER_RESOURCE_GROUP` |
| `STORAGE_ACCOUNT_NAME` | `AZURE_STORAGE_ACCOUNT` |
| `SEARCH_ENDPOINT`, `SEARCH_INDEX_NAME` | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX` |
| `OPENAI_ENDPOINT` | `AZURE_OPENAI_ENDPOINT` |
| `VIDEO_INDEXER_ACCOUNT_ID`, `VIDEO_INDEXER_ACCOUNT_NAME` | `AZURE_VIDEO_INDEXER_ACCOUNT_ID`, `AZURE_VIDEO_INDEXER_ACCOUNT_NAME` |
| `VI_POLLER_JOB_NAME` | `VI_POLLER_JOB_NAME` (or `VI_POLLER_USE_JOB=false` to poll in-process) |

## Troubleshooting

* **The deployment fails on a model deployment** (`InsufficientQuota`, `SkuNotSupported`, "model not available",
  a retired version). Quota and availability are per subscription and region. Check them, then override the variables:
  ```bash
  az cognitiveservices usage list --location westeurope \
    --query "[?starts_with(name.value, 'OpenAI.GlobalStandard.')].{quota:name.value, used:currentValue, limit:limit}" -o table
  az cognitiveservices model list --location westeurope \
    --query "[?model.format=='OpenAI' && contains(model.name, 'gpt-4.1')].{model:model.name, version:model.version, skus:model.skus[].name}" -o table
  CHAT_CAPACITY=30 JUDGE_CAPACITY=30 ./scripts/deploy.sh                          # smaller deployments
  JUDGE_MODEL_NAME=gpt-4.1-mini JUDGE_MODEL_VERSION=2025-04-14 ./scripts/deploy.sh # another vision-capable model
  ```
  Quota can be requested in the Azure portal (**Quotas**). A reasoning model as `chat` or `judge` also needs
  `CHAT_REASONING_EFFORT` or `JUDGE_REASONING_EFFORT` and a newer `AZURE_OPENAI_API_VERSION`.
* **Search stops answering semantic queries.** The free semantic ranker plan has a monthly request quota; set
  `SEARCH_SEMANTIC_TIER=standard`.
* **`MissingSubscriptionRegistration`.** Register the provider named in the error (see Prerequisites) and run again.
* **`az acr build` is refused** (`TasksOperationsNotAllowed`; some subscription types such as free trials cannot use
  ACR Tasks). Build locally: `USE_LOCAL_DOCKER=1 ./scripts/deploy.sh`.
* **`InvalidResourceGroupLocation`.** The resource group exists in another region. Use that region or another group.
* **Role assignment errors.** Your account needs Owner or User Access Administrator. If you deleted only the managed
  identity, remove the orphaned role assignments ("Identity not found") and deploy again.
* **Video Indexer or the first requests fail right after deployment.** Role assignments can take a few minutes to
  propagate; run the script again.
* **Video Indexer fails with a storage error** (the job status shows a failed `analyze` stage). Video Indexer reads
  the video and writes its results to the storage account as the managed identity, and is admitted through the storage
  firewall by resource ID. Check that the identity still has **Storage Blob Data Owner** on the account, that the
  account's **Networking > Resource instances** lists the Video Indexer account, and that no Azure Policy changes the
  account's network or key settings. The template turns shared-key access off; if your Video Indexer region or version
  turns out to require it, set `allowSharedKeyAccess` to `true` in `infra/modules/resources.bicep` and deploy again.
* **Storage "AuthorizationFailure" or "This request is not authorized" from your machine.** The firewall is closed to the
  internet; see "Run the API locally". Storage keys do not exist, so Storage Explorer and other tools must sign in
  with Entra ID.
* **Changes made in the portal disappear.** App settings, IP rules and the like are owned by the template; a
  deployment resets them. Change the variables above, or edit `infra/`.

## Tear down

```bash
az group delete --name rg-momentlens --yes --no-wait
```

This deletes everything in the group, including the indexed data. Azure OpenAI accounts are soft-deleted: before you
create the same environment again, purge the old account (its name is `ml-openai-<token>`; the token is the suffix
of every resource name):

```bash
az cognitiveservices account list-deleted -o table
az cognitiveservices account purge --name <account> --resource-group rg-momentlens --location westeurope
```
