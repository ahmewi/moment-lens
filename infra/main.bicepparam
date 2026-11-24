using './main.bicep'

// Every value can be overridden with an environment variable; scripts/deploy.sh exports them from its flags.
// No secrets are involved: the deployment uses managed identities and Azure RBAC only.

param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'momentlens')
param location = readEnvironmentVariable('AZURE_LOCATION', 'westeurope')
param resourceGroupName = readEnvironmentVariable('AZURE_RESOURCE_GROUP', 'rg-momentlens')

// Object ID of the person running the deployment (az ad signed-in-user show --query id -o tsv). Empty skips the developer roles.
param principalId = readEnvironmentVariable('AZURE_PRINCIPAL_ID', '')
param principalType = readEnvironmentVariable('AZURE_PRINCIPAL_TYPE', 'User')

// Empty deploys placeholder images; scripts/deploy.sh builds the images and deploys again with their tag.
param imageTag = readEnvironmentVariable('IMAGE_TAG', '')

// Comma-separated IPv4/IPv6 addresses or CIDR ranges, for example "203.0.113.7,198.51.100.0/24". Empty = no restriction.
param allowedClientIps = filter(
  map(split(readEnvironmentVariable('ALLOWED_CLIENT_IPS', ''), ','), ip => trim(ip)),
  ip => !empty(ip)
)

// Comma-separated IPv4 addresses or CIDR ranges allowed to reach the storage account, e.g. a developer's address for local runs.
param storageAllowedIps = filter(
  map(split(readEnvironmentVariable('STORAGE_ALLOWED_IPS', ''), ','), ip => trim(ip)),
  ip => !empty(ip)
)

param appServicePlanSku = readEnvironmentVariable('APP_SERVICE_PLAN_SKU', 'P0v3')
param maxUploadMb = int(readEnvironmentVariable('MAX_UPLOAD_MB', '500'))
param maxUploadMinutes = int(readEnvironmentVariable('MAX_UPLOAD_MINUTES', '30'))
param searchSemanticTier = readEnvironmentVariable('SEARCH_SEMANTIC_TIER', 'free')
param promptContentModel = readEnvironmentVariable('PROMPT_CONTENT_MODEL', '')

param chatModelName = readEnvironmentVariable('CHAT_MODEL_NAME', 'gpt-4.1-mini')
param chatModelVersion = readEnvironmentVariable('CHAT_MODEL_VERSION', '2025-04-14')
param chatSkuName = readEnvironmentVariable('CHAT_SKU_NAME', 'GlobalStandard')
param chatCapacity = int(readEnvironmentVariable('CHAT_CAPACITY', '50'))
param chatReasoningEffort = readEnvironmentVariable('CHAT_REASONING_EFFORT', '')

param embeddingModelName = readEnvironmentVariable('EMBEDDING_MODEL_NAME', 'text-embedding-3-large')
param embeddingModelVersion = readEnvironmentVariable('EMBEDDING_MODEL_VERSION', '1')
param embeddingSkuName = readEnvironmentVariable('EMBEDDING_SKU_NAME', 'GlobalStandard')
param embeddingCapacity = int(readEnvironmentVariable('EMBEDDING_CAPACITY', '50'))

param judgeModelName = readEnvironmentVariable('JUDGE_MODEL_NAME', 'gpt-4.1')
param judgeModelVersion = readEnvironmentVariable('JUDGE_MODEL_VERSION', '2025-04-14')
param judgeSkuName = readEnvironmentVariable('JUDGE_SKU_NAME', 'GlobalStandard')
param judgeCapacity = int(readEnvironmentVariable('JUDGE_CAPACITY', '100'))
param judgeReasoningEffort = readEnvironmentVariable('JUDGE_REASONING_EFFORT', '')

param openAiApiVersion = readEnvironmentVariable('AZURE_OPENAI_API_VERSION', '2024-10-21')
