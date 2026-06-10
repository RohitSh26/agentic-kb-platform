"""Azure AI Search adapter: SearchClient interface + fake here, Azure impl alongside."""

from agentic_kb_builder.infrastructure.azure_search.search_client import (
    FakeSearchClient,
    SearchClient,
)

__all__ = ["FakeSearchClient", "SearchClient"]
