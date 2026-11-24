"""Shared Azure SDK clients (singletons)."""
from __future__ import annotations

from functools import lru_cache

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

from app.config import settings


@lru_cache
def credential() -> DefaultAzureCredential:
    # Use UAMI client id when running on App Service; falls back to az login locally.
    if settings.azure_client_id:
        return DefaultAzureCredential(managed_identity_client_id=settings.azure_client_id)
    return DefaultAzureCredential()


@lru_cache
def blob_service() -> BlobServiceClient:
    account_url = f"https://{settings.azure_storage_account}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=credential())


@lru_cache
def table_service() -> TableServiceClient:
    """Singleton TableServiceClient for the same storage account.

    Uses AAD via the same UAMI — caller must have `Storage Table Data Contributor`
    on the storage account (granted in infra/modules/resources.bicep).
    """
    endpoint = f"https://{settings.azure_storage_account}.table.core.windows.net"
    return TableServiceClient(endpoint=endpoint, credential=credential())


@lru_cache
def search_index_client() -> SearchIndexClient:
    return SearchIndexClient(endpoint=settings.azure_search_endpoint, credential=credential())


def search_client(index_name: str) -> SearchClient:
    return SearchClient(
        endpoint=settings.azure_search_endpoint,
        index_name=index_name,
        credential=credential(),
    )


@lru_cache
def _cognitive_token_provider():
    from azure.identity import get_bearer_token_provider

    return get_bearer_token_provider(credential(), "https://cognitiveservices.azure.com/.default")


@lru_cache
def openai_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_ad_token_provider=_cognitive_token_provider(),
        api_version=settings.azure_openai_api_version,
    )


@lru_cache
def judge_client() -> AzureOpenAI:
    """Client for a rerank judge that lives on another endpoint (for example a Foundry project's
    account) or needs a different API version than the primary OpenAI client. Callers only use it
    when `AZURE_OPENAI_JUDGE_ENDPOINT` / `AZURE_OPENAI_JUDGE_API_VERSION` is set.
    """
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_judge_endpoint or settings.azure_openai_endpoint,
        azure_ad_token_provider=_cognitive_token_provider(),
        api_version=settings.azure_openai_judge_api_version or settings.azure_openai_api_version,
    )


def aad_token(scope: str) -> str:
    """Get a bearer token for the given AAD scope."""
    return credential().get_token(scope).token
