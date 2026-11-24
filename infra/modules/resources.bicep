// MomentLens - every resource that lives in the resource group.
//
// A single user-assigned managed identity (UAMI) is used by the web app, the Container Apps job and Video Indexer.
// The template creates no keys, passwords, connection strings or SAS tokens: shared-key access on storage, API keys
// on AI Search and keys on Azure OpenAI are disabled, and every data-plane call is authorized with Azure RBAC.
targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters (defaults live in ../main.bicep)
// ---------------------------------------------------------------------------
@description('Azure region for all resources.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('Stable per-environment token that keeps globally unique resource names unique.')
param resourceToken string

@description('Object ID of the person or service principal running the deployment. When set, it gets the data-plane roles needed to run the API locally. Empty skips these role assignments.')
param principalId string

@description('Principal type of principalId.')
@allowed(['User', 'Group', 'ServicePrincipal'])
param principalType string

@description('Tag of the momentlens/api and momentlens/vi-poller images in the container registry. Empty deploys public placeholder images (used for the first deployment, before the images are built).')
param imageTag string

@description('IPv4/IPv6 addresses or CIDR ranges allowed to reach the web app. Empty means no restriction.')
param allowedClientIps array

@description('IPv4 addresses or CIDR ranges allowed to reach the storage account from the internet (local development). Empty allows only the private endpoints and Video Indexer.')
param storageAllowedIps array

@description('SKU of the Linux App Service plan.')
param appServicePlanSku string

@description('Maximum size of an uploaded video in MB.')
param maxUploadMb int

@description('Maximum duration of an uploaded video in minutes.')
param maxUploadMinutes int

@description('Billing plan of the Azure AI Search semantic ranker.')
param searchSemanticTier string

@description('API version the application uses for Azure OpenAI calls.')
param openAiApiVersion string

@description('Chat model deployment named "chat".')
param chatModel modelDeployment

@description('Embedding model deployment named "embedding".')
param embeddingModel modelDeployment

@description('Vision-capable model deployment named "judge".')
param judgeModel modelDeployment

@description('Reasoning effort for the chat model. Empty uses the classic chat parameters (temperature, max_tokens).')
param chatReasoningEffort string

@description('Reasoning effort for the judge model. Empty uses the classic chat parameters (temperature, max_tokens).')
param judgeReasoningEffort string

@description('Model Video Indexer uses for its generated segment summaries. Empty lets Video Indexer choose.')
param promptContentModel string

@description('A model deployment in the Azure OpenAI account.')
type modelDeployment = {
  @description('Model name, for example gpt-4.1.')
  name: string
  @description('Model version.')
  version: string
  @description('Deployment SKU, for example GlobalStandard.')
  sku: string
  @description('Capacity in units of 1,000 tokens per minute.')
  capacity: int
}

// ---------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------
var prefix = 'ml'
var storageAccountName = '${prefix}st${resourceToken}'
var searchName = '${prefix}-search-${resourceToken}'
var openAiName = '${prefix}-openai-${resourceToken}'
var videoIndexerName = '${prefix}-vi-${resourceToken}'
var uamiName = '${prefix}-id-${resourceToken}'
var vnetName = '${prefix}-vnet-${resourceToken}'
var logAnalyticsName = '${prefix}-log-${resourceToken}'
var appInsightsName = '${prefix}-appi-${resourceToken}'
var acrName = '${prefix}acr${resourceToken}'
var planName = '${prefix}-plan-${resourceToken}'
var webAppName = '${prefix}-app-${resourceToken}'
var containerEnvName = '${prefix}-cae-${resourceToken}'
var viPollerJobName = '${prefix}-vi-poller-${resourceToken}'

// Names the API and the vi-poller job rely on.
var videosContainerName = 'videos'
var insightsContainerName = 'insights'
var thumbnailsContainerName = 'thumbnails'
var searchIndexName = 'moments'
var jobsTableName = 'momentlensjobs'
var chatDeploymentName = 'chat'
var embeddingDeploymentName = 'embedding'
var judgeDeploymentName = 'judge'

// Video Indexer ARM API version the application and the vi-poller use to mint access tokens.
var videoIndexerApiVersion = '2024-01-01'

// Built-in Azure role definitions.
var roleIds = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  cognitiveServicesOpenAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDelegator: 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

// ---------------------------------------------------------------------------
// Managed identity
// ---------------------------------------------------------------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: uamiName
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Network: virtual network, subnets and private DNS zones (see network.bicep)
// ---------------------------------------------------------------------------
module network 'network.bicep' = {
  name: 'network-${resourceToken}'
  params: {
    location: location
    tags: tags
    vnetName: vnetName
  }
}

// ---------------------------------------------------------------------------
// Storage: videos, Video Indexer insights, thumbnails and the job-state table
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    isHnsEnabled: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
    // The web app and the job use the private endpoints below. The public endpoint stays reachable only for trusted
    // Azure services: Video Indexer downloads the uploaded video from here, so it is admitted by resource ID.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      ipRules: [for ip in storageAllowedIps: { value: ip, action: 'Allow' }]
      virtualNetworkRules: []
      resourceAccessRules: [
        {
          tenantId: subscription().tenantId
          resourceId: resourceId('Microsoft.VideoIndexer/accounts', videoIndexerName)
        }
      ]
    }
  }
}

// No CORS rules: the browser never talks to storage. Uploads go through the API (block by block) and videos,
// frames and thumbnails are streamed back through the API as well.
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: storage
  name: 'default'
}

resource videosContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: videosContainerName
  properties: { publicAccess: 'None' }
}

resource insightsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: insightsContainerName
  properties: { publicAccess: 'None' }
}

resource thumbnailsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: thumbnailsContainerName
  properties: { publicAccess: 'None' }
}

resource peBlob 'Microsoft.Network/privateEndpoints@2025-07-01' = {
  name: '${prefix}-pe-blob-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: { id: network.outputs.privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'conn-blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource peBlobDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-07-01' = {
  parent: peBlob
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'blob', properties: { privateDnsZoneId: network.outputs.blobDnsZoneId } }
    ]
  }
}

resource peTable 'Microsoft.Network/privateEndpoints@2025-07-01' = {
  name: '${prefix}-pe-table-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: { id: network.outputs.privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'conn-table'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['table']
        }
      }
    ]
  }
}

resource peTableDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-07-01' = {
  parent: peTable
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'table', properties: { privateDnsZoneId: network.outputs.tableDnsZoneId } }
    ]
  }
}

// ---------------------------------------------------------------------------
// Azure AI Search: hybrid (keyword + vector) retrieval with the semantic ranker. RBAC only, no API keys.
// ---------------------------------------------------------------------------
resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchName
  location: location
  tags: tags
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    semanticSearch: searchSemanticTier
  }
}

// ---------------------------------------------------------------------------
// Azure OpenAI: chat, embeddings and the vision "judge". Entra ID only, no API keys.
// ---------------------------------------------------------------------------
resource openAi 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: openAiName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

var openAiDeployments = [
  { name: chatDeploymentName, model: chatModel }
  { name: embeddingDeploymentName, model: embeddingModel }
  { name: judgeDeploymentName, model: judgeModel }
]

// Deployments of one account are created one at a time; parallel requests make the service reject them.
@batchSize(1)
resource openAiModelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [
  for deployment in openAiDeployments: {
    parent: openAi
    name: deployment.name
    sku: {
      name: deployment.model.sku
      capacity: deployment.model.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: deployment.model.name
        version: deployment.model.version
      }
    }
  }
]

// ---------------------------------------------------------------------------
// Azure AI Video Indexer (ARM account) with the managed identity as its storage identity.
// Accounts that connect storage are billed pay-as-you-go; there is no SKU to choose.
// The identity needs "Storage Blob Data Owner" before the account is created, hence the dependsOn.
// ---------------------------------------------------------------------------
resource videoIndexer 'Microsoft.VideoIndexer/accounts@2025-04-01' = {
  name: videoIndexerName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    storageServices: {
      resourceId: storage.id
      userAssignedIdentity: uami.id
    }
  }
  dependsOn: [
    uamiStorageRoleAssignments
  ]
}

// ---------------------------------------------------------------------------
// Monitoring: Log Analytics, workspace-based Application Insights and the pipeline workbook
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// Application Insights keeps its instrumentation-key (local) authentication: the API exports telemetry with the
// connection string and does not sign requests with Entra ID.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// The workbook queries the custom metrics in Application Insights and the job logs in Log Analytics.
// The workspace resource ID is injected here so the JSON file contains no resource IDs.
resource pipelineWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(appInsights.id, 'momentlens-pipeline')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: 'MomentLens - Pipeline cost and latency'
    category: 'workbook'
    sourceId: appInsights.id
    serializedData: replace(
      loadTextContent('../workbooks/momentlens-pipeline.json'),
      '__LOG_ANALYTICS_WORKSPACE_ID__',
      logAnalytics.id
    )
    version: '1.0'
  }
}

// ---------------------------------------------------------------------------
// Container registry: images are pulled with the managed identity, the admin user is disabled
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: acrName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Web app: FastAPI + React in one container on App Service for Linux.
// The API runs a single uvicorn worker, so the plan stays at one instance (no autoscale).
// ---------------------------------------------------------------------------
resource plan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: appServicePlanSku
    tier: appServicePlanSku == 'P0v3' ? 'Premium0V3' : 'PremiumV3'
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

// The placeholder image listens on port 80; the real image on 8000.
var bootstrapImages = empty(imageTag)
var webImage = bootstrapImages
  ? 'mcr.microsoft.com/appsvc/staticsite:latest'
  : '${acr.properties.loginServer}/momentlens/api:${imageTag}'
var jobImage = bootstrapImages
  ? 'mcr.microsoft.com/k8se/quickstart-jobs:latest'
  : '${acr.properties.loginServer}/momentlens/vi-poller:${imageTag}'

// One Allow rule per entry. Once a rule exists, everything else is denied.
var ipRestrictions = [
  for (ip, i) in allowedClientIps: {
    name: 'allow-client-${i + 1}'
    description: 'Allowed client address'
    action: 'Allow'
    priority: 100 + i
    tag: 'Default'
    ipAddress: contains(ip, '/') ? ip : (contains(ip, ':') ? '${ip}/128' : '${ip}/32')
  }
]

resource webApp 'Microsoft.Web/sites@2025-03-01' = {
  name: webAppName
  location: location
  tags: tags
  kind: 'app,linux,container'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    publicNetworkAccess: 'Enabled'
    // All outbound traffic leaves through snet-app, so the storage host names resolve to the private endpoints.
    virtualNetworkSubnetId: network.outputs.appSubnetId
    siteConfig: {
      linuxFxVersion: 'DOCKER|${webImage}'
      alwaysOn: true
      http20Enabled: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      healthCheckPath: '/api/health'
      vnetRouteAllEnabled: true
      acrUseManagedIdentityCreds: true
      acrUserManagedIdentityID: uami.properties.clientId
      ipSecurityRestrictions: ipRestrictions
      ipSecurityRestrictionsDefaultAction: empty(allowedClientIps) ? 'Allow' : 'Deny'
      scmIpSecurityRestrictionsUseMain: true
      appSettings: [
        { name: 'WEBSITES_PORT', value: bootstrapImages ? '80' : '8000' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'AZURE_LOCATION', value: location }
        { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
        { name: 'AZURE_STORAGE_VIDEOS_CONTAINER', value: videosContainerName }
        { name: 'AZURE_STORAGE_INSIGHTS_CONTAINER', value: insightsContainerName }
        { name: 'AZURE_STORAGE_THUMBNAILS_CONTAINER', value: thumbnailsContainerName }
        { name: 'AZURE_SEARCH_ENDPOINT', value: search.properties.endpoint }
        { name: 'AZURE_SEARCH_INDEX', value: searchIndexName }
        { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
        { name: 'AZURE_OPENAI_API_VERSION', value: openAiApiVersion }
        { name: 'AZURE_OPENAI_CHAT_DEPLOYMENT', value: chatDeploymentName }
        { name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT', value: embeddingDeploymentName }
        { name: 'AZURE_OPENAI_CHAT_REASONING_EFFORT', value: chatReasoningEffort }
        { name: 'AZURE_OPENAI_JUDGE_DEPLOYMENT', value: judgeDeploymentName }
        { name: 'AZURE_OPENAI_JUDGE_REASONING_EFFORT', value: judgeReasoningEffort }
        { name: 'AZURE_VIDEO_INDEXER_ACCOUNT_ID', value: videoIndexer.properties.accountId }
        { name: 'AZURE_VIDEO_INDEXER_ACCOUNT_NAME', value: videoIndexer.name }
        { name: 'AZURE_VIDEO_INDEXER_LOCATION', value: location }
        { name: 'AZURE_VIDEO_INDEXER_RESOURCE_GROUP', value: resourceGroup().name }
        { name: 'AZURE_VIDEO_INDEXER_API_VERSION', value: videoIndexerApiVersion }
        { name: 'VI_POLLER_USE_JOB', value: 'true' }
        { name: 'VI_POLLER_RESOURCE_GROUP', value: resourceGroup().name }
        { name: 'VI_POLLER_JOB_NAME', value: viPollerJob.name }
        { name: 'VI_POLLER_TIMEOUT_MINUTES', value: '240' }
        { name: 'MAX_UPLOAD_MB', value: string(maxUploadMb) }
        { name: 'MAX_UPLOAD_MINUTES', value: string(maxUploadMinutes) }
        { name: 'JOBS_TABLE_ENABLED', value: 'true' }
        { name: 'JOBS_TABLE_NAME', value: jobsTableName }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
      ]
    }
  }
  dependsOn: [
    acrPullRoleAssignment
  ]
}

// Deployments use container images and Entra ID, so basic-auth publishing credentials are switched off.
resource scmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: webApp
  name: 'scm'
  properties: { allow: false }
}

resource ftpBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: webApp
  name: 'ftp'
  properties: { allow: false }
}

// ---------------------------------------------------------------------------
// Container Apps: the vi-poller job polls Video Indexer until a video is indexed (outlives web app restarts).
// The API starts executions through ARM and overrides VIDEO_ID / VI_VIDEO_ID; every variable it can
// override has to be declared in the template.
// ---------------------------------------------------------------------------
resource containerEnv 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: containerEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    // Attached to snet-cae so the job reaches storage through the private endpoints.
    // The VNet configuration cannot be changed after creation.
    vnetConfiguration: {
      infrastructureSubnetId: network.outputs.containerEnvSubnetId
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource viPollerJob 'Microsoft.App/jobs@2025-07-01' = {
  name: viPollerJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    environmentId: containerEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      // Video Indexer needs about as long as the video lasts; four hours leave room for throttling.
      replicaTimeout: 14400
      replicaRetryLimit: 0
      manualTriggerConfig: {
        replicaCompletionCount: 1
        parallelism: 1
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'vi-poller'
          image: jobImage
          // The loop mostly waits for HTTP responses.
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          // An empty PROMPT_CONTENT_MODEL is left out (the poller then lets Video Indexer choose); the API copies these
          // variables into every execution it starts, so none of them should be empty.
          env: concat(
            [
              { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
              { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
              { name: 'STORAGE_ACCOUNT', value: storage.name }
              { name: 'VIDEOS_CONTAINER', value: videosContainerName }
              { name: 'INSIGHTS_CONTAINER', value: insightsContainerName }
              { name: 'VI_LOCATION', value: location }
              { name: 'VI_ACCOUNT_ID', value: videoIndexer.properties.accountId }
              { name: 'VI_ACCOUNT_NAME', value: videoIndexer.name }
              { name: 'VI_RESOURCE_GROUP', value: resourceGroup().name }
              { name: 'VI_API_VERSION', value: videoIndexerApiVersion }
              // Placeholders, overridden when the API starts an execution.
              { name: 'VIDEO_ID', value: 'unset' }
              { name: 'VI_VIDEO_ID', value: 'unset' }
              { name: 'POLL_TIMEOUT_MINUTES', value: '240' }
              { name: 'POLL_INTERVAL_SECONDS', value: '30' }
              { name: 'PROMPT_CONTENT_ENABLED', value: 'true' }
              { name: 'PROMPT_CONTENT_TIMEOUT_MINUTES', value: '10' }
            ],
            empty(promptContentModel) ? [] : [{ name: 'PROMPT_CONTENT_MODEL', value: promptContentModel }]
          )
        }
      ]
    }
  }
  dependsOn: [
    acrPullRoleAssignment
  ]
}

// ---------------------------------------------------------------------------
// RBAC for the managed identity
// ---------------------------------------------------------------------------
var uamiStorageRoleIds = [
  roleIds.storageBlobDataOwner // also needed by Video Indexer, which accesses the account through this identity
  roleIds.storageBlobDataContributor
  roleIds.storageBlobDelegator
  roleIds.storageTableDataContributor
]

resource uamiStorageRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in uamiStorageRoleIds: {
    name: guid(storage.id, uami.id, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: uami.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

var searchRoleIds = [
  roleIds.searchIndexDataContributor
  roleIds.searchServiceContributor
]

resource uamiSearchRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in searchRoleIds: {
    name: guid(search.id, uami.id, roleId)
    scope: search
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: uami.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource uamiOpenAiRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, uami.id, roleIds.cognitiveServicesOpenAiUser)
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roleIds.cognitiveServicesOpenAiUser
    )
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Contributor on the Video Indexer account lets the API and the job call generateAccessToken.
resource uamiVideoIndexerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(videoIndexer.id, uami.id, roleIds.contributor)
  scope: videoIndexer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.contributor)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, roleIds.acrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPull)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Contributor on the job lets the API start executions (POST .../jobs/<name>/start) and read their status.
resource uamiJobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(viPollerJob.id, uami.id, roleIds.contributor)
  scope: viPollerJob
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.contributor)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// RBAC for the deploying user, so the API can run locally with "az login"
// ---------------------------------------------------------------------------
var userStorageRoleIds = empty(principalId)
  ? []
  : [
      roleIds.storageBlobDataContributor // includes the user-delegation-key action used to mint read SAS URLs
      roleIds.storageTableDataContributor
    ]

resource userStorageRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in userStorageRoleIds: {
    name: guid(storage.id, principalId, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: principalId
      principalType: principalType
    }
  }
]

var userSearchRoleIds = empty(principalId) ? [] : searchRoleIds

resource userSearchRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in userSearchRoleIds: {
    name: guid(search.id, principalId, roleId)
    scope: search
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: principalId
      principalType: principalType
    }
  }
]

resource userOpenAiRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(openAi.id, principalId, roleIds.cognitiveServicesOpenAiUser)
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roleIds.cognitiveServicesOpenAiUser
    )
    principalId: principalId
    principalType: principalType
  }
}

resource userVideoIndexerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(videoIndexer.id, principalId, roleIds.contributor)
  scope: videoIndexer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.contributor)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------
// Outputs (no secrets)
// ---------------------------------------------------------------------------
output managedIdentityClientId string = uami.properties.clientId
output storageAccountName string = storage.name
output searchEndpoint string = search.properties.endpoint
output searchIndexName string = searchIndexName
output openAiEndpoint string = openAi.properties.endpoint
output videoIndexerAccountName string = videoIndexer.name
output videoIndexerAccountId string = videoIndexer.properties.accountId
output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output registryName string = acr.name
output registryLoginServer string = acr.properties.loginServer
output viPollerJobName string = viPollerJob.name
output logAnalyticsWorkspaceName string = logAnalytics.name
output applicationInsightsName string = appInsights.name
