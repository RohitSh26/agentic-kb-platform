"""Source connectors for the nightly incremental build."""

from agentic_kb_builder.connectors.ado_card import AdoCardConnector
from agentic_kb_builder.connectors.ado_wiki_backend import AdoWikiBackend
from agentic_kb_builder.connectors.ado_work_item_backend import AdoWorkItemBackend
from agentic_kb_builder.connectors.azure_wiki import AzureWikiConnector
from agentic_kb_builder.connectors.config_loader import (
    SOURCE_CONFIG_PATH_ENV,
    BackendFactory,
    FilteredFetchBackend,
    SourceConfigError,
    connectors_from_config,
    load_source_config,
    resolve_git_metadata_repo,
    resolve_token,
)
from agentic_kb_builder.connectors.git_metadata import GitMetadataConnector
from agentic_kb_builder.connectors.github_code import GitHubCodeConnector
from agentic_kb_builder.connectors.github_doc import GitHubDocConnector
from agentic_kb_builder.connectors.github_rest import GitHubRestBackend
from agentic_kb_builder.connectors.http_client import AsyncHttpClient, HttpFetchError
from agentic_kb_builder.connectors.production_factory import production_backend_factory
from agentic_kb_builder.connectors.source_connector import BaseConnector, Connector, FetchBackend

__all__ = [
    "SOURCE_CONFIG_PATH_ENV",
    "AdoCardConnector",
    "AdoWikiBackend",
    "AdoWorkItemBackend",
    "AsyncHttpClient",
    "AzureWikiConnector",
    "BackendFactory",
    "BaseConnector",
    "Connector",
    "FetchBackend",
    "FilteredFetchBackend",
    "GitHubCodeConnector",
    "GitHubDocConnector",
    "GitHubRestBackend",
    "GitMetadataConnector",
    "HttpFetchError",
    "SourceConfigError",
    "connectors_from_config",
    "load_source_config",
    "production_backend_factory",
    "resolve_git_metadata_repo",
    "resolve_token",
]
