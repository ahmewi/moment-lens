// MomentLens - subscription-scope entry point: creates the resource group and deploys everything into it.
//
//   az deployment sub create --location <region> --parameters infra/main.bicepparam
//
// scripts/deploy.sh wraps this (deploy, build the images, deploy again with the image tag).
targetScope = 'subscription'

// ---------------------------------------------------------------------------
// General
// ---------------------------------------------------------------------------
@description('Short name of the environment. It seeds the unique resource-name token (use a different name for each environment in a subscription and region) and is stored in the "environment" tag.')
@minLength(1)
@maxLength(32)
param environmentName string = 'momentlens'

@description('Azure region for all resources. It must offer Video Indexer, Azure OpenAI, AI Search and Container Apps.')
param location string = 'westeurope'

@description('Name of the resource group the template creates.')
@minLength(1)
@maxLength(90)
param resourceGroupName string = 'rg-momentlens'

@description('Object ID of the person or service principal running the deployment. When set, it gets the data-plane roles needed to run the API locally with "az login". Leave empty to skip these role assignments.')
param principalId string = ''

@description('Principal type of principalId.')
@allowed(['User', 'Group', 'ServicePrincipal'])
param principalType string = 'User'

@description('Tag of the momentlens/api and momentlens/vi-poller images in the container registry created by this template. Empty deploys public placeholder images, which is what the first deployment does before the images are built.')
@maxLength(128)
param imageTag string = ''

@description('IPv4/IPv6 addresses or CIDR ranges allowed to reach the web app, for example ["203.0.113.7", "198.51.100.0/24"]. Empty leaves the site open to the internet.')
param allowedClientIps array = []

@description('IPv4 addresses or CIDR ranges allowed to reach the storage account from the internet, for example the public address of a developer running the API locally. Use a plain address instead of /32 and /31 ranges. Empty (the default) allows only the private endpoints and Video Indexer.')
param storageAllowedIps array = []

@description('SKU of the Linux App Service plan. The API runs a single worker, so the plan always has one instance.')
@allowed(['P0v3', 'P1v3', 'P2v3', 'P3v3'])
param appServicePlanSku string = 'P0v3'

@description('Maximum size of an uploaded video in MB.')
@minValue(1)
param maxUploadMb int = 500

@description('Maximum duration of an uploaded video in minutes.')
@minValue(1)
param maxUploadMinutes int = 30

@description('Billing plan of the AI Search semantic ranker. "free" is capped at a monthly request quota; "standard" bills beyond the free quota.')
@allowed(['disabled', 'free', 'standard'])
param searchSemanticTier string = 'free'

@description('Model Video Indexer uses for its generated segment summaries. Empty lets Video Indexer choose.')
param promptContentModel string = ''

// ---------------------------------------------------------------------------
// Azure OpenAI. Availability and quota differ per subscription and region: check them first (see docs/deployment.md)
// and override these values when needed. Model versions are retired on a published schedule.
// ---------------------------------------------------------------------------
@description('Chat model: scene enrichment and query understanding. A classic chat model, or a reasoning model together with chatReasoningEffort.')
@minLength(1)
param chatModelName string = 'gpt-4.1-mini'

@description('Version of the chat model.')
@minLength(1)
param chatModelVersion string = '2025-04-14'

@description('SKU of the chat deployment. Use DataZoneStandard to keep processing inside a data zone.')
@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param chatSkuName string = 'GlobalStandard'

@description('Capacity of the chat deployment in units of 1,000 tokens per minute.')
@minValue(1)
param chatCapacity int = 50

@description('Reasoning effort for the chat model, for example low, medium or high. Leave empty for classic chat models. A reasoning model (gpt-5 family and later) needs a value here and an openAiApiVersion that supports reasoning_effort.')
param chatReasoningEffort string = ''

@description('Embedding model. The search index uses 3072-dimensional vectors, so this must be text-embedding-3-large.')
@minLength(1)
param embeddingModelName string = 'text-embedding-3-large'

@description('Version of the embedding model.')
@minLength(1)
param embeddingModelVersion string = '1'

@description('SKU of the embedding deployment.')
@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param embeddingSkuName string = 'GlobalStandard'

@description('Capacity of the embedding deployment in units of 1,000 tokens per minute.')
@minValue(1)
param embeddingCapacity int = 50

@description('Judge model: reads video frames to confirm moments. It must accept image input.')
@minLength(1)
param judgeModelName string = 'gpt-4.1'

@description('Version of the judge model.')
@minLength(1)
param judgeModelVersion string = '2025-04-14'

@description('SKU of the judge deployment.')
@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param judgeSkuName string = 'GlobalStandard'

@description('Capacity of the judge deployment in units of 1,000 tokens per minute. Frame-based judging is token hungry; raise it for faster indexing.')
@minValue(1)
param judgeCapacity int = 100

@description('Reasoning effort for the judge model, for example low, medium or high. Leave empty for classic chat models. Reasoning models (gpt-5 family and later) need a value here and an openAiApiVersion that supports reasoning_effort.')
param judgeReasoningEffort string = ''

@description('Azure OpenAI API version used by the application.')
param openAiApiVersion string = '2024-10-21'

// ---------------------------------------------------------------------------
var tags = {
  project: 'MomentLens'
  environment: environmentName
}

// Keeps globally unique names (storage, registry, web app, ...) unique per subscription, environment and region.
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'modules/resources.bicep' = {
  name: 'resources-${resourceToken}'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    principalId: principalId
    principalType: principalType
    imageTag: imageTag
    allowedClientIps: allowedClientIps
    storageAllowedIps: storageAllowedIps
    appServicePlanSku: appServicePlanSku
    maxUploadMb: maxUploadMb
    maxUploadMinutes: maxUploadMinutes
    searchSemanticTier: searchSemanticTier
    promptContentModel: promptContentModel
    openAiApiVersion: openAiApiVersion
    chatModel: {
      name: chatModelName
      version: chatModelVersion
      sku: chatSkuName
      capacity: chatCapacity
    }
    embeddingModel: {
      name: embeddingModelName
      version: embeddingModelVersion
      sku: embeddingSkuName
      capacity: embeddingCapacity
    }
    judgeModel: {
      name: judgeModelName
      version: judgeModelVersion
      sku: judgeSkuName
      capacity: judgeCapacity
    }
    chatReasoningEffort: chatReasoningEffort
    judgeReasoningEffort: judgeReasoningEffort
  }
}

// ---------------------------------------------------------------------------
// Outputs: identifiers only, never secrets.
// ---------------------------------------------------------------------------
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_TENANT_ID string = subscription().tenantId

output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer

output WEB_APP_NAME string = resources.outputs.webAppName
output WEB_APP_URL string = resources.outputs.webAppUrl
output MANAGED_IDENTITY_CLIENT_ID string = resources.outputs.managedIdentityClientId

output STORAGE_ACCOUNT_NAME string = resources.outputs.storageAccountName
output SEARCH_ENDPOINT string = resources.outputs.searchEndpoint
output SEARCH_INDEX_NAME string = resources.outputs.searchIndexName
output OPENAI_ENDPOINT string = resources.outputs.openAiEndpoint

output VIDEO_INDEXER_ACCOUNT_NAME string = resources.outputs.videoIndexerAccountName
output VIDEO_INDEXER_ACCOUNT_ID string = resources.outputs.videoIndexerAccountId
output VI_POLLER_JOB_NAME string = resources.outputs.viPollerJobName

output LOG_ANALYTICS_WORKSPACE_NAME string = resources.outputs.logAnalyticsWorkspaceName
output APPLICATION_INSIGHTS_NAME string = resources.outputs.applicationInsightsName
