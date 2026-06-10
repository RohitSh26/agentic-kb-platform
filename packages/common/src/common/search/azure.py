"""Azure AI Search implementation of the SearchClient Protocol (ADR-0006).

The only module allowed to import the azure-search-documents SDK. Everything
else — build indexer, future MCP tools — depends on common.search.client's
Protocol, which keeps the projection swappable and tests hermetic. Credential
is injected (managed identity preferred per the security rules); no secrets
are read here.

The SDK ships partially-typed responses, so the unknown-type strict checks
are relaxed for this boundary module only.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from collections.abc import Sequence
from typing import Any

from azure.search.documents.aio import SearchClient as _AzureSdkSearchClient

from common.logging import get_logger
from contracts.search_schemas import IndexState, SearchDoc

logger = get_logger("common.search.azure")


class AzureSearchClient:
    """SearchClient backed by one Azure AI Search hybrid index."""

    def __init__(self, *, endpoint: str, index_name: str, credential: Any) -> None:
        self._index_name = index_name
        self._client = _AzureSdkSearchClient(
            endpoint=endpoint, index_name=index_name, credential=credential
        )

    async def upsert_docs(self, docs: Sequence[SearchDoc]) -> int:
        if not docs:
            return 0
        payload = [_to_index_document(doc) for doc in docs]
        results = await self._client.merge_or_upload_documents(documents=payload)
        succeeded = sum(1 for r in results if r.succeeded)
        if succeeded != len(docs):
            failed = [r.key for r in results if not r.succeeded]
            logger.error(
                "event=azure_search_upsert_partial index=%s requested=%d ok=%d failed_keys=%s",
                self._index_name,
                len(docs),
                succeeded,
                failed[:20],
            )
        else:
            logger.info(
                "event=azure_search_upserted index=%s count=%d", self._index_name, succeeded
            )
        return succeeded

    async def delete_docs(self, doc_ids: Sequence[str]) -> int:
        if not doc_ids:
            return 0
        results = await self._client.delete_documents(
            documents=[{"doc_id": doc_id} for doc_id in doc_ids]
        )
        removed = sum(1 for r in results if r.succeeded)
        logger.info(
            "event=azure_search_deleted index=%s requested=%d removed=%d",
            self._index_name,
            len(doc_ids),
            removed,
        )
        return removed

    async def fetch_index_state(self) -> IndexState:
        docs: dict[str, str | None] = {}
        results = await self._client.search(search_text="*", select=["doc_id", "artifact_hash"])
        async for result in results:
            docs[str(result["doc_id"])] = result.get("artifact_hash")
        logger.info(
            "event=azure_search_state_fetched index=%s docs=%d", self._index_name, len(docs)
        )
        return IndexState(docs=docs)

    async def close(self) -> None:
        await self._client.close()


def _to_index_document(doc: SearchDoc) -> dict[str, Any]:
    payload = doc.model_dump(mode="json")
    # the embedding vector field must be a plain list for the SDK
    if payload.get("embedding") is not None:
        payload["embedding"] = list(payload["embedding"])
    return payload
