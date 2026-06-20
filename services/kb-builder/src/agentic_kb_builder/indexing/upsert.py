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

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.indexing.projection import load_doc_hashes, load_search_docs
from agentic_kb_builder.indexing.search_document import SearchDoc
from agentic_kb_builder.infrastructure.azure_search.search_client import SearchClient
from agentic_kb_builder.infrastructure.postgres.models import EmbeddingCache
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


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
        if upserted != len(docs):
            # A silently missing doc would wedge consistency validation on every
            # later build (the orphan sweep repairs orphans, nothing repairs
            # missing); failing the run keeps the source unchanged-skip from
            # committing, so the next build retries the upsert.
            raise RuntimeError(
                f"search upsert incomplete: {upserted}/{len(docs)} documents accepted"
            )
        await self._record_doc_ids(docs)
        logger.info(
            "event=indexer_docs_upserted requested=%d upserted=%d", len(artifact_ids), upserted
        )
        return upserted

    async def delete_orphaned(self) -> int:
        return await delete_orphaned_docs(self._session, self._client)

    async def reconcile_missing(self) -> int:
        """Back-fill every current member the index lacks or holds at a stale hash.

        ``delete_orphaned`` removes index docs whose artifact left the registry; this
        is the opposite direction — registry members the index is MISSING or holds at
        a stale ``artifact_hash``. The database persists across builds while the index
        may not (it was in-memory and vanished when a prior build's process exited, or
        it is a fresh/reset file), so the index can lag an already-populated registry.
        The incremental path upserts only changed sources and so cannot close that gap;
        without this, the consistency gate permanently blocks activation.

        Documents are reprojected from Postgres — no LLM and no re-embedding
        (invariant 4 governs generation, not the projection) — so the index is a
        genuinely self-healing, rebuildable projection of the registry (invariant 1),
        and the post-build consistency gate becomes an invariant every build
        satisfies rather than an assumption about how the index was populated.
        """
        expected = await load_doc_hashes(self._session)
        actual = (await self._client.fetch_index_state()).docs
        stale_doc_ids = [
            doc_id
            for doc_id, artifact_hash in expected.items()
            if doc_id not in actual or actual[doc_id] != artifact_hash
        ]
        if not stale_doc_ids:
            return 0
        docs = await load_search_docs(
            self._session, artifact_ids=[uuid.UUID(doc_id) for doc_id in stale_doc_ids]
        )
        upserted = await self._client.upsert_docs(docs)
        if upserted != len(docs):
            # Same contract as upsert_documents: a partial Azure write must fail loudly,
            # not stamp every doc as indexed — otherwise _record_doc_ids marks rejected
            # docs as present and the index stays silently incomplete past the gate.
            raise RuntimeError(
                f"search reconcile incomplete: {upserted}/{len(docs)} documents accepted"
            )
        await self._record_doc_ids(docs)
        logger.info(
            "event=indexer_reconciled_missing missing_or_stale=%d upserted=%d",
            len(stale_doc_ids),
            upserted,
        )
        return upserted

    async def _record_doc_ids(self, docs: Sequence[SearchDoc]) -> None:
        """Stamp azure_search_doc_id on the embedding row behind each doc."""
        for doc in docs:
            if doc.embedding is None:
                continue
            # body_text may be None for search_text-only code_symbol projections;
            # the embedding was keyed on search_text in that case (see projection.py).
            embed_text = doc.body_text if doc.body_text is not None else doc.search_text
            if embed_text is None:
                continue
            await self._session.execute(
                update(EmbeddingCache)
                .where(
                    EmbeddingCache.artifact_id == doc.artifact_id,
                    EmbeddingCache.text_hash == content_hash(embed_text),
                    EmbeddingCache.embedding_model == doc.embedding_model,
                )
                .values(azure_search_doc_id=doc.doc_id)
            )


async def delete_orphaned_docs(session: AsyncSession, client: SearchClient) -> int:
    """Remove index documents whose artifact is gone from the registry."""
    expected = set(await load_doc_hashes(session))
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
