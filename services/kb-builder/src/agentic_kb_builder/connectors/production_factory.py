"""Production BackendFactory: dispatch a SourceSpec to its real backend.

Same shape as `local_fs_backend_factory` so `connectors_from_config` is unchanged:
the token (already resolved from `auth.token_env` by `resolve_token`) is handed in as
a local value. Dispatch:
  github_code / github_doc -> GitHubRestBackend (api.github.com, pinned to a commit SHA)
  azure_wiki               -> AdoWikiBackend (dev.azure.com wiki REST, pinned to the wiki git head)
  ado_card                 -> AdoWorkItemBackend (dev.azure.com WIQL query + work-item batch fetch)
All three are real, implemented backends (see their modules + tests). Any other type raises
SourceConfigError.
"""

from typing import Any

from agentic_kb_builder.connectors.ado_wiki_backend import AdoWikiBackend
from agentic_kb_builder.connectors.ado_work_item_backend import AdoWorkItemBackend
from agentic_kb_builder.connectors.config_loader import BackendFactory, SourceConfigError
from agentic_kb_builder.connectors.github_rest import GitHubRestBackend
from agentic_kb_builder.connectors.source_connector import FetchBackend
from agentic_kb_builder.domain.source_config import (
    AdoCardSourceSpec,
    AzureWikiSourceSpec,
    GithubCodeSourceSpec,
    GithubDocSourceSpec,
    SourceSpec,
)


def production_backend_factory(*, client_transport: Any | None = None) -> BackendFactory:
    """A BackendFactory that builds the real (or stubbed) backend for each source.

    `client_transport` (e.g. `httpx.MockTransport`) is threaded into every backend so
    integration tests can drive the whole config -> connector path hermetically.
    """

    def factory(spec: SourceSpec, token: str | None) -> FetchBackend:
        if isinstance(spec, GithubCodeSourceSpec | GithubDocSourceSpec):
            return GitHubRestBackend(spec, token, client_transport=client_transport)
        if isinstance(spec, AzureWikiSourceSpec):
            return AdoWikiBackend(spec, token, client_transport=client_transport)
        if isinstance(spec, AdoCardSourceSpec):
            return AdoWorkItemBackend(spec, token, client_transport=client_transport)
        raise SourceConfigError(f"unsupported source type for production backend: {spec.type!r}")

    return factory


__all__ = ["production_backend_factory"]
