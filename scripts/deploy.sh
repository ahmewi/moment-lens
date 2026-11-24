#!/usr/bin/env bash
# Deploys MomentLens to Azure: infrastructure, container images, and the final configuration.
#
#   az login
#   ./scripts/deploy.sh [options]
#
# Needs only the Azure CLI (az). Docker is optional (USE_LOCAL_DOCKER=1). Works in Azure Cloud Shell,
# Linux, macOS and WSL. No secrets are read, written or printed: everything uses Azure RBAC.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [options]

Deploys the MomentLens infrastructure, builds the two container images in the new registry and
points the web app and the vi-poller job at them. Safe to run again: it updates what changed.

Options (each has an environment variable, shown in brackets):
  -n, --env-name NAME        environment name, seeds resource names     [AZURE_ENV_NAME, momentlens]
  -l, --location REGION      Azure region                               [AZURE_LOCATION, westeurope]
  -g, --resource-group NAME  resource group to create or update         [AZURE_RESOURCE_GROUP, rg-momentlens]
  -s, --subscription ID      subscription id or name to use             [AZURE_SUBSCRIPTION_ID, current one]
      --allowed-ips LIST     comma-separated IPs/CIDRs allowed to reach the site (default: open)
                                                                        [ALLOWED_CLIENT_IPS]
      --image-tag TAG        tag for the images (default <git sha>-<UTC timestamp>)  [IMAGE_TAG]
      --skip-build           do not build images; deploy the images already tagged --image-tag
                             (also how to apply infrastructure changes without rebuilding)
      --infra-only           only create the infrastructure (placeholder images), then stop
      --what-if              show what the infrastructure deployment would change, then stop
      --local-docker         build with local Docker and push, instead of "az acr build"
                                                                        [USE_LOCAL_DOCKER=1]
      --no-import            do not copy the public base images into the registry
      --no-user-roles        do not grant the signed-in user data-plane roles (local development)
      --no-register          do not register the required resource providers
  -h, --help                 show this help

Other environment variables read by infra/main.bicepparam (unset or empty = default, except where empty
is a valid value such as JUDGE_REASONING_EFFORT):
  AZURE_PRINCIPAL_ID, AZURE_PRINCIPAL_TYPE   who gets the local-development roles (default: signed-in user)
  STORAGE_ALLOWED_IPS                        comma-separated IPv4 addresses that may reach the storage
                                             account directly (local development; default: none)
  APP_SERVICE_PLAN_SKU (P0v3), MAX_UPLOAD_MB (500), MAX_UPLOAD_MINUTES (30)
  SEARCH_SEMANTIC_TIER (free), PROMPT_CONTENT_MODEL (empty)
  CHAT_MODEL_NAME, CHAT_MODEL_VERSION, CHAT_SKU_NAME, CHAT_CAPACITY, CHAT_REASONING_EFFORT
  EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_VERSION, EMBEDDING_SKU_NAME, EMBEDDING_CAPACITY
  JUDGE_MODEL_NAME, JUDGE_MODEL_VERSION, JUDGE_SKU_NAME, JUDGE_CAPACITY, JUDGE_REASONING_EFFORT
  AZURE_OPENAI_API_VERSION
Base images for the builds (official Docker Hub images, copied to the registry as cache/<name>:<tag>):
  NODE_IMAGE (node:20-alpine), PYTHON_IMAGE (python:3.12-slim)

Examples:
  ./scripts/deploy.sh
  ./scripts/deploy.sh --env-name demo --location swedencentral --allowed-ips "203.0.113.7"
  JUDGE_MODEL_NAME=gpt-4.1-mini JUDGE_MODEL_VERSION=2025-04-14 ./scripts/deploy.sh
  USE_LOCAL_DOCKER=1 ./scripts/deploy.sh

Details: docs/deployment.md
EOF
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
STEP=""

log() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { STEP=""; printf 'ERROR: %s\n' "$*" >&2; exit 1; }

on_exit() {
    local rc=$?
    if [ "${rc}" -ne 0 ] && [ -n "${STEP}" ]; then
        printf '\nFailed during: %s (exit code %s). Fix the cause and run the script again.\n' "${STEP}" "${rc}" >&2
    fi
}
trap on_exit EXIT

# Azure CLI prints "None" for null values in tsv output.
clean() {
    case "$1" in
        None | null) printf '' ;;
        *) printf '%s' "$1" ;;
    esac
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
ENV_NAME="${AZURE_ENV_NAME:-momentlens}"
LOCATION="${AZURE_LOCATION:-westeurope}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-rg-momentlens}"
SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-}"
ALLOWED_IPS="${ALLOWED_CLIENT_IPS:-}"
TAG="${IMAGE_TAG:-}"
LOCAL_DOCKER="${USE_LOCAL_DOCKER:-0}"
NODE_IMAGE="${NODE_IMAGE:-node:20-alpine}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-slim}"
SKIP_BUILD=0
INFRA_ONLY=0
WHAT_IF=0
IMPORT_BASE_IMAGES=1
USER_ROLES=1
REGISTER_PROVIDERS=1

while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help) usage; exit 0 ;;
        -n | --env-name) [ $# -ge 2 ] || die "$1 needs a value"; ENV_NAME="$2"; shift 2 ;;
        -l | --location) [ $# -ge 2 ] || die "$1 needs a value"; LOCATION="$2"; shift 2 ;;
        -g | --resource-group) [ $# -ge 2 ] || die "$1 needs a value"; RESOURCE_GROUP="$2"; shift 2 ;;
        -s | --subscription) [ $# -ge 2 ] || die "$1 needs a value"; SUBSCRIPTION="$2"; shift 2 ;;
        --allowed-ips) [ $# -ge 2 ] || die "$1 needs a value"; ALLOWED_IPS="$2"; shift 2 ;;
        --image-tag) [ $# -ge 2 ] || die "$1 needs a value"; TAG="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --infra-only) INFRA_ONLY=1; shift ;;
        --what-if) WHAT_IF=1; shift ;;
        --local-docker) LOCAL_DOCKER=1; shift ;;
        --no-import) IMPORT_BASE_IMAGES=0; shift ;;
        --no-user-roles) USER_ROLES=0; shift ;;
        --no-register) REGISTER_PROVIDERS=0; shift ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

case "${LOCAL_DOCKER}" in
    1 | true | TRUE | yes | YES) LOCAL_DOCKER=1 ;;
    *) LOCAL_DOCKER=0 ;;
esac

if [ "${SKIP_BUILD}" -eq 1 ] && [ -z "${TAG}" ]; then
    die "--skip-build needs --image-tag (or IMAGE_TAG) naming images that already exist in the registry"
fi

# "West Europe" and "westeurope" must give the same resource names.
LOCATION="$(printf '%s' "${LOCATION}" | tr -d ' ' | tr '[:upper:]' '[:lower:]')"
DEPLOYMENT_NAME="momentlens-${ENV_NAME}-${LOCATION}"

# Blank values would otherwise override the defaults in infra/main.bicepparam.
for var in APP_SERVICE_PLAN_SKU MAX_UPLOAD_MB MAX_UPLOAD_MINUTES SEARCH_SEMANTIC_TIER \
    CHAT_MODEL_NAME CHAT_MODEL_VERSION CHAT_SKU_NAME CHAT_CAPACITY \
    EMBEDDING_MODEL_NAME EMBEDDING_MODEL_VERSION EMBEDDING_SKU_NAME EMBEDDING_CAPACITY \
    JUDGE_MODEL_NAME JUDGE_MODEL_VERSION JUDGE_SKU_NAME JUDGE_CAPACITY \
    AZURE_OPENAI_API_VERSION AZURE_PRINCIPAL_TYPE; do
    if [ -z "$(printenv "${var}" || true)" ]; then
        unset "${var}"
    fi
done

# ---------------------------------------------------------------------------
# 1. Azure CLI, login, subscription
# ---------------------------------------------------------------------------
STEP="checking the Azure CLI login"
[ -f api/Dockerfile ] && [ -f vi-poller/Dockerfile ] && [ -f infra/main.bicepparam ] ||
    die "run this script from a complete checkout of the repository (api/, vi-poller/ and infra/ are required)"
command -v az >/dev/null 2>&1 || die "the Azure CLI (az) is not installed: https://learn.microsoft.com/cli/azure/install-azure-cli"

if ! az account show >/dev/null 2>&1; then
    die "you are not logged in. Run 'az login' (add --use-device-code on a machine without a browser) and try again."
fi
if [ -n "${SUBSCRIPTION}" ]; then
    az account set --subscription "${SUBSCRIPTION}"
fi
SUBSCRIPTION_ID="$(az account show --query id --output tsv)"
SUBSCRIPTION_NAME="$(az account show --query name --output tsv)"

log "Deploying MomentLens"
note "subscription:   ${SUBSCRIPTION_NAME} (${SUBSCRIPTION_ID})"
note "environment:    ${ENV_NAME}"
note "location:       ${LOCATION}"
note "resource group: ${RESOURCE_GROUP}"
if [ -n "${ALLOWED_IPS}" ]; then
    note "site access:    restricted to ${ALLOWED_IPS}"
else
    note "site access:    open to the internet (see docs/deployment.md to restrict it)"
fi

# A resource group cannot change region; fail early with a clear message.
STEP="checking the resource group"
EXISTING_RG_LOCATION="$(clean "$(az group show --name "${RESOURCE_GROUP}" --query location --output tsv 2>/dev/null || true)")"
if [ -n "${EXISTING_RG_LOCATION}" ] && [ "$(lower "${EXISTING_RG_LOCATION}")" != "$(lower "${LOCATION}")" ]; then
    die "resource group '${RESOURCE_GROUP}' already exists in '${EXISTING_RG_LOCATION}'. Use --location ${EXISTING_RG_LOCATION} or another --resource-group."
fi

# ---------------------------------------------------------------------------
# 2. Resource providers
# ---------------------------------------------------------------------------
PROVIDERS="Microsoft.VideoIndexer Microsoft.CognitiveServices Microsoft.Search Microsoft.App Microsoft.Web \
Microsoft.Storage Microsoft.Network Microsoft.ContainerRegistry Microsoft.OperationalInsights Microsoft.Insights \
Microsoft.ManagedIdentity"

if [ "${REGISTER_PROVIDERS}" -eq 1 ]; then
    STEP="registering resource providers"
    log "Checking resource providers"
    PENDING=""
    for ns in ${PROVIDERS}; do
        state="$(clean "$(az provider show --namespace "${ns}" --query registrationState --output tsv 2>/dev/null || true)")"
        if [ "${state}" != "Registered" ]; then
            note "registering ${ns} (state: ${state:-unknown})"
            if az provider register --namespace "${ns}" >/dev/null 2>&1; then
                PENDING="${PENDING} ${ns}"
            else
                warn "could not register ${ns}; the deployment registers it automatically if your role allows it"
            fi
        fi
    done
    ATTEMPTS=0
    while [ -n "${PENDING}" ] && [ "${ATTEMPTS}" -lt 60 ]; do
        STILL=""
        for ns in ${PENDING}; do
            state="$(clean "$(az provider show --namespace "${ns}" --query registrationState --output tsv 2>/dev/null || true)")"
            if [ "${state}" != "Registered" ]; then
                STILL="${STILL} ${ns}"
            fi
        done
        PENDING="${STILL}"
        if [ -n "${PENDING}" ]; then
            note "waiting for:${PENDING}"
            sleep 10
            ATTEMPTS=$((ATTEMPTS + 1))
        fi
    done
    if [ -n "${PENDING}" ]; then
        warn "still registering:${PENDING}. Continuing; the deployment waits for them."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Parameters for infra/main.bicepparam (it reads environment variables)
# ---------------------------------------------------------------------------
STEP="preparing parameters"
PRINCIPAL_ID=""
PRINCIPAL_TYPE="${AZURE_PRINCIPAL_TYPE:-User}"
if [ "${USER_ROLES}" -eq 1 ]; then
    if [ -n "${AZURE_PRINCIPAL_ID+x}" ]; then
        PRINCIPAL_ID="${AZURE_PRINCIPAL_ID}"
    else
        ACCOUNT_TYPE="$(clean "$(az account show --query user.type --output tsv 2>/dev/null || true)")"
        ACCOUNT_NAME="$(clean "$(az account show --query user.name --output tsv 2>/dev/null || true)")"
        case "${ACCOUNT_TYPE}" in
            user)
                PRINCIPAL_ID="$(clean "$(az ad signed-in-user show --query id --output tsv 2>/dev/null || true)")"
                PRINCIPAL_TYPE="User"
                ;;
            servicePrincipal)
                PRINCIPAL_ID="$(clean "$(az ad sp show --id "${ACCOUNT_NAME}" --query id --output tsv 2>/dev/null || true)")"
                PRINCIPAL_TYPE="ServicePrincipal"
                ;;
        esac
        if ! [[ "${PRINCIPAL_ID}" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]]; then
            PRINCIPAL_ID=""
            warn "could not determine your object id; no local-development roles will be granted (set AZURE_PRINCIPAL_ID to grant them)"
        fi
    fi
fi

export AZURE_ENV_NAME="${ENV_NAME}"
export AZURE_LOCATION="${LOCATION}"
export AZURE_RESOURCE_GROUP="${RESOURCE_GROUP}"
export AZURE_PRINCIPAL_ID="${PRINCIPAL_ID}"
export AZURE_PRINCIPAL_TYPE="${PRINCIPAL_TYPE}"
export ALLOWED_CLIENT_IPS="${ALLOWED_IPS}"
PARAMETERS_FILE="${REPO_ROOT}/infra/main.bicepparam"

deploy_infra() {
    # $1 = image tag; empty deploys the public placeholder images.
    IMAGE_TAG="$1" az deployment sub create \
        --name "${DEPLOYMENT_NAME}" \
        --location "${LOCATION}" \
        --parameters "${PARAMETERS_FILE}" \
        --only-show-errors \
        --output none
}

output_value() {
    clean "$(az deployment sub show --name "${DEPLOYMENT_NAME}" --query "properties.outputs.$1.value" --output tsv --only-show-errors)"
}

if [ "${WHAT_IF}" -eq 1 ]; then
    STEP="what-if"
    log "What-if (no changes are made)"
    IMAGE_TAG="${TAG}" az deployment sub what-if \
        --name "${DEPLOYMENT_NAME}" \
        --location "${LOCATION}" \
        --parameters "${PARAMETERS_FILE}" \
        --only-show-errors
    STEP=""
    exit 0
fi

# ---------------------------------------------------------------------------
# 4. Infrastructure with placeholder images (creates the registry the images are built in)
# ---------------------------------------------------------------------------
# Name of the registry created by an earlier run of this environment (same name and region), if it still exists.
existing_registry() {
    local name
    name="$(clean "$(az deployment sub show --name "${DEPLOYMENT_NAME}" --query properties.outputs.AZURE_CONTAINER_REGISTRY_NAME.value --output tsv --only-show-errors 2>/dev/null || true)")"
    if [ -n "${name}" ] && az acr show --name "${name}" --resource-group "${RESOURCE_GROUP}" >/dev/null 2>&1; then
        printf '%s' "${name}"
    fi
}

# The first deployment uses placeholder images because the images can only be built once the registry exists.
# When the registry already exists (re-run), the placeholder deployment is skipped so the site keeps running.
STEP="locating the container registry"
ACR_NAME="$(existing_registry)"
BOOTSTRAPPED=0
if [ "${SKIP_BUILD}" -eq 1 ]; then
    [ -n "${ACR_NAME}" ] || die "this environment has no container registry yet, so no images exist. Run without --skip-build."
elif [ -z "${ACR_NAME}" ]; then
    STEP="deploying the infrastructure (placeholder images)"
    log "Deploying the infrastructure (the first deployment can take 15 minutes or more)"
    deploy_infra ""
    BOOTSTRAPPED=1
else
    note "registry ${ACR_NAME} already exists: skipping the placeholder deployment"
fi

if [ "${INFRA_ONLY}" -eq 1 ]; then
    STEP=""
    log "Infrastructure is in place"
    note "Next: ./scripts/deploy.sh --env-name ${ENV_NAME} --location ${LOCATION} --resource-group ${RESOURCE_GROUP}"
    exit 0
fi

STEP="reading deployment outputs"
if [ "${BOOTSTRAPPED}" -eq 1 ]; then
    ACR_NAME="$(output_value AZURE_CONTAINER_REGISTRY_NAME)"
fi
[ -n "${ACR_NAME}" ] || die "could not read the registry name from the deployment outputs"
LOGIN_SERVER="$(clean "$(az acr show --name "${ACR_NAME}" --query loginServer --output tsv)")"
[ -n "${LOGIN_SERVER}" ] || die "could not read the login server of registry ${ACR_NAME}"

# ---------------------------------------------------------------------------
# 5. Images
# ---------------------------------------------------------------------------
if [ "${SKIP_BUILD}" -eq 0 ]; then
    if [ -z "${TAG}" ]; then
        SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
        TAG="${SHA:-local}-$(date -u +%Y%m%dT%H%M%SZ)"
    fi

    # Docker Hub limits anonymous pulls, which breaks builds on shared build agents, so the two base images
    # are copied into the registry first. If the copy fails the builds fall back to the public images.
    NODE_BUILD_IMAGE="${NODE_IMAGE}"
    PYTHON_BUILD_IMAGE="${PYTHON_IMAGE}"
    if [ "${IMPORT_BASE_IMAGES}" -eq 1 ]; then
        STEP="importing base images"
        log "Copying base images into ${ACR_NAME} (best effort)"
        if az acr import --name "${ACR_NAME}" --source "docker.io/library/${NODE_IMAGE}" --image "cache/${NODE_IMAGE}" --force --only-show-errors >/dev/null 2>&1; then
            NODE_BUILD_IMAGE="${LOGIN_SERVER}/cache/${NODE_IMAGE}"
            note "cache/${NODE_IMAGE}: ok"
        else
            warn "could not import ${NODE_IMAGE}; the build pulls it from Docker Hub"
        fi
        if az acr import --name "${ACR_NAME}" --source "docker.io/library/${PYTHON_IMAGE}" --image "cache/${PYTHON_IMAGE}" --force --only-show-errors >/dev/null 2>&1; then
            PYTHON_BUILD_IMAGE="${LOGIN_SERVER}/cache/${PYTHON_IMAGE}"
            note "cache/${PYTHON_IMAGE}: ok"
        else
            warn "could not import ${PYTHON_IMAGE}; the build pulls it from Docker Hub"
        fi
    fi

    if [ "${LOCAL_DOCKER}" -eq 1 ]; then
        STEP="building the images with local Docker"
        command -v docker >/dev/null 2>&1 || die "USE_LOCAL_DOCKER=1 needs Docker, which was not found"
        docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
        log "Building images ${TAG} with local Docker"
        az acr login --name "${ACR_NAME}" --only-show-errors >/dev/null
        docker build --platform linux/amd64 --file api/Dockerfile \
            --build-arg "NODE_IMAGE=${NODE_BUILD_IMAGE}" --build-arg "PYTHON_IMAGE=${PYTHON_BUILD_IMAGE}" \
            --tag "${LOGIN_SERVER}/momentlens/api:${TAG}" .
        docker push "${LOGIN_SERVER}/momentlens/api:${TAG}"
        docker build --platform linux/amd64 --file vi-poller/Dockerfile \
            --build-arg "PYTHON_IMAGE=${PYTHON_BUILD_IMAGE}" \
            --tag "${LOGIN_SERVER}/momentlens/vi-poller:${TAG}" .
        docker push "${LOGIN_SERVER}/momentlens/vi-poller:${TAG}"
    else
        STEP="building the api image in the registry"
        log "Building images ${TAG} in ${ACR_NAME} (az acr build)"
        if ! az acr build --registry "${ACR_NAME}" --image "momentlens/api:${TAG}" --file api/Dockerfile \
            --build-arg "NODE_IMAGE=${NODE_BUILD_IMAGE}" --build-arg "PYTHON_IMAGE=${PYTHON_BUILD_IMAGE}" .; then
            warn "az acr build failed. Some subscription types (for example free trials) cannot use ACR Tasks: retry with USE_LOCAL_DOCKER=1."
            exit 1
        fi
        STEP="building the vi-poller image in the registry"
        az acr build --registry "${ACR_NAME}" --image "momentlens/vi-poller:${TAG}" --file vi-poller/Dockerfile \
            --build-arg "PYTHON_IMAGE=${PYTHON_BUILD_IMAGE}" .
    fi
fi

# ---------------------------------------------------------------------------
# 6. Final deployment with the real images
# ---------------------------------------------------------------------------
STEP="deploying with image tag ${TAG}"
log "Deploying with image tag ${TAG}"
deploy_infra "${TAG}"

STEP="reading deployment outputs"
WEB_URL="$(output_value WEB_APP_URL)"
WEB_NAME="$(output_value WEB_APP_NAME)"

# ---------------------------------------------------------------------------
# 7. Health probe (informational) and summary
# ---------------------------------------------------------------------------
STEP=""
if command -v curl >/dev/null 2>&1 && [ -n "${WEB_URL}" ]; then
    log "Waiting for ${WEB_URL}/api/health (the first start pulls the image and can take a few minutes)"
    HEALTH="not reachable yet"
    for _ in $(seq 1 30); do
        CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${WEB_URL}/api/health" 2>/dev/null || true)"
        if [ "${CODE}" = "200" ]; then
            HEALTH="healthy"
            break
        elif [ "${CODE}" = "403" ]; then
            HEALTH="blocked by the IP restriction (expected unless your address is in the allow-list)"
            break
        fi
        sleep 10
    done
    note "health: ${HEALTH}"
fi

printf '\nMomentLens is deployed.\n'
printf '  Site:            %s\n' "${WEB_URL}"
printf '  Web app:         %s\n' "${WEB_NAME}"
printf '  Resource group:  %s\n' "${RESOURCE_GROUP}"
printf '  Image tag:       %s\n' "${TAG}"
printf '\n'
if [ -z "${ALLOWED_IPS}" ]; then
    printf 'The site is OPEN: anyone who knows the URL can use it and spend your Azure credits.\n'
    printf 'Restrict it with ALLOWED_CLIENT_IPS or Microsoft Entra authentication (docs/deployment.md, "Protect the site").\n\n'
fi
printf 'Tear down everything:\n'
printf '  az group delete --name %s --yes --no-wait\n' "${RESOURCE_GROUP}"
printf 'Azure OpenAI accounts are soft-deleted: purge the account before re-creating the same environment (docs/deployment.md).\n'
