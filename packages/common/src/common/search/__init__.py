"""Search projection clients: Protocol + fake here, Azure impl in .azure."""

from common.search.client import FakeSearchClient, SearchClient

__all__ = ["FakeSearchClient", "SearchClient"]
