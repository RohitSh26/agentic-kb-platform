"""Changed-docs-only upsert + orphan reconciliation (architecture §7 step 6).

SearchDocUpserter implements the build runner's SearchIndexer protocol: the
runner hands it only the artifacts of *changed* sources, so an unchanged night
upserts nothing (invariant 4's cost discipline extends to the index).
delete_orphaned_docs is the other half of keeping the projection honest:
documents whose artifact disappeared from the registry (source deleted,
artifact superseded) are removed before validation, so a stale index entry
can never block activation forever or serve dead evidence.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from common.search.client import SearchClient
from contracts.search_schemas import SearchDoc
from db.models import EmbeddingCache
from kb_builder.indexer.projection import load_search_docs

logger = get_logger("kb_builder.indexer.upsert")


class SearchDocUpserter:
    """SearchIndexer implementation backed by a SearchClient."""

    def __init__(self, session: AsyncSession, client: SearchClient) -> None:
        self._session = session
        self._client = client

    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        docs = await load_search_docs(self._session, artifact_ids=artifact_ids)
        if not docs:
            logger.info("event=indexer_upsert_skipped reason=no_projectable_artifacts")
            return 0
        upserted = await self._client.upsert_docs(docs)
        await self._record_doc_ids(docs)
        logger.info(
            "event=indexer_docs_upserted requested=%d upserted=%d", len(artifact_ids), upserted
        )
        return upserted

    async def delete_orphaned(self) -> int:
        return await delete_orphaned_docs(self._session, self._client)

    async def _record_doc_ids(self, docs: Sequence[SearchDoc]) -> None:
        """Stamp azure_search_doc_id on the embedding rows behind each doc."""
        for doc in docs:
            if doc.embedding is None:
                continue
            await self._session.execute(
                update(EmbeddingCache)
                .where(
                    EmbeddingCache.artifact_id == doc.artifact_id,
                    EmbeddingCache.azure_search_doc_id.is_(None),
                )
                .values(azure_search_doc_id=doc.doc_id)
            )


async def delete_orphaned_docs(session: AsyncSession, client: SearchClient) -> int:
    """Remove index documents whose artifact is gone from the registry."""
    expected = {doc.doc_id for doc in await load_search_docs(session)}
    state = await client.fetch_index_state()
    orphaned = sorted(set(state.docs) - expected)
    if not orphaned:
        return 0
    removed = await client.delete_docs(orphaned)
    logger.warning(
        "event=indexer_orphans_deleted reason=artifact_gone count=%d doc_ids=%s",
        removed,
        orphaned[:20],
    )
    return removed
