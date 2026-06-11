"""Incremental build runner — the 8-step algorithm from docs/architecture §7.

Unchanged content_hash => chunk/wikify/graphify/embed/index are all skipped.
Wikify (kb_builder.wikify), the graphify adapter (kb_builder.graphify_adapter),
and the search indexer (kb_builder.indexer) are real; the embedding backend
remains a Protocol until the Azure OpenAI client lands. Every model-shaped call is
gated by a cache lookup so a hit never reaches the model. Generation-cache rows
are recorded in the same transaction as their output artifacts, after the
artifacts are persisted, so a crash between generate and cache can never strand
a cache row without output.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.application.cache_gates import (
    EmbeddingCacheGate,
    GenerationCacheGate,
    chunk_summary_cache_key,
    code_graph_cache_key,
)
from agentic_kb_builder.connectors import Connector
from agentic_kb_builder.domain import (
    CodeEdgeDraft,
    GraphifyResult,
    NormalizedContent,
    WikifyArtifactDraft,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.schema_versions import (
    CHUNKER_VERSION,
    GRAPHIFY_VERSION,
    OUTPUT_SCHEMA_VERSION,
    PARSER_CONFIG_VERSION,
    PROMPT_VERSION,
)
from agentic_kb_builder.graphify.write import write_code_artifacts, write_code_edges
from agentic_kb_builder.infrastructure.postgres.models import (
    KbBuildRun,
    KnowledgeArtifact,
    SourceItem,
)
from agentic_kb_builder.linker.run import run_linker
from agentic_kb_builder.linker.semantic import SimilarityProvider
from agentic_kb_builder.structured_logging import get_logger
from agentic_kb_builder.wikify.write import write_wikify_artifacts

logger = get_logger(__name__)


class Wikifier(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def model_params_hash(self) -> str: ...

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]: ...


class Graphifier(Protocol):
    async def graphify(self, content: NormalizedContent) -> GraphifyResult: ...


@dataclass(frozen=True)
class EmbeddingResult:
    """The vector is persisted in embedding_cache so the Search index stays
    rebuildable from Postgres without re-embedding (invariant 1/4)."""

    embedding_hash: str
    vector: list[float]


class Embedder(Protocol):
    embedding_model: str

    async def embed(self, text: str) -> EmbeddingResult: ...


class SearchIndexer(Protocol):
    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int: ...

    async def delete_orphaned(self) -> int:
        """Remove index docs whose artifact left the registry; returns count."""
        ...


@dataclass(frozen=True)
class _PendingEdges:
    """Edge drafts from one graphified file, held until the end of the run."""

    repo: str
    drafts: tuple[CodeEdgeDraft, ...]


@dataclass
class _Counters:
    sources_seen: int = 0
    sources_changed: int = 0
    artifacts_created: int = 0
    llm_calls: int = 0
    embedding_calls: int = 0
    search_docs_upserted: int = 0


class BuildRunner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        kb_version: str,
        wikifier: Wikifier,
        graphifier: Graphifier,
        embedder: Embedder,
        indexer: SearchIndexer,
        similarity: SimilarityProvider | None = None,
    ) -> None:
        self._session = session
        self._kb_version = kb_version
        self._wikifier = wikifier
        self._graphifier = graphifier
        self._embedder = embedder
        self._indexer = indexer
        self._similarity = similarity
        self._generation_gate = GenerationCacheGate(session)
        self._embedding_gate = EmbeddingCacheGate(session)

    async def run(self, connectors: Sequence[Connector]) -> KbBuildRun:
        """Execute one build. The runner owns the session's transactions: the run
        row is committed up front so the audit record survives a failed build;
        per-source work is committed only when the whole build succeeds."""
        run = KbBuildRun(
            kb_version=self._kb_version,
            status="running",
            sources_seen=0,
            sources_changed=0,
            artifacts_created=0,
            artifacts_updated=0,
            artifacts_deleted=0,
            llm_calls=0,
            embedding_calls=0,
            search_docs_upserted=0,
        )
        self._session.add(run)
        await self._session.flush()
        build_id = run.build_id
        await self._session.commit()
        logger.info("event=build_run_started build_id=%s kb_version=%s", build_id, self._kb_version)
        counters = _Counters()
        code_key_map: dict[tuple[str, str], uuid.UUID] = {}
        pending_edges: list[_PendingEdges] = []
        try:
            for connector in connectors:
                for ref in await connector.list_sources():
                    fetched = await connector.fetch(ref)
                    counters.sources_seen += 1
                    if await self._is_unchanged(fetched):
                        await self._touch_last_seen(fetched)
                        logger.info(
                            "event=build_skip_unchanged source_uri=%s content_hash=%s",
                            ref.source_uri,
                            fetched.content_hash,
                        )
                        continue
                    counters.sources_changed += 1
                    await self._process_changed_source(
                        counters, fetched, code_key_map, pending_edges
                    )
            await self._write_pending_edges(code_key_map, pending_edges)
            await run_linker(
                self._session, kb_version=self._kb_version, similarity=self._similarity
            )
            # reconcile the index before validation can run against it, so an
            # orphaned doc can never permanently block activation (invariant 5)
            orphans_removed = await self._indexer.delete_orphaned()
            if orphans_removed:
                logger.info(
                    "event=build_index_orphans_removed build_id=%s count=%d",
                    build_id,
                    orphans_removed,
                )
            await self._finish_run(build_id, counters, status="completed")
            await self._session.commit()
        except Exception as error:
            # discard partial work, then record the failure in a fresh transaction
            # so the audit row is never lost (no silent failures).
            await self._session.rollback()
            error_summary = f"{type(error).__name__}: {error}"
            await self._finish_run(build_id, counters, status="failed", error_summary=error_summary)
            await self._session.commit()
            logger.error("event=build_run_failed build_id=%s error=%s", build_id, error_summary)
            raise
        final = (
            await self._session.execute(select(KbBuildRun).where(KbBuildRun.build_id == build_id))
        ).scalar_one()
        logger.info(
            "event=build_run_completed build_id=%s sources_seen=%d sources_changed=%d "
            "llm_calls=%d embedding_calls=%d search_docs_upserted=%d",
            build_id,
            counters.sources_seen,
            counters.sources_changed,
            counters.llm_calls,
            counters.embedding_calls,
            counters.search_docs_upserted,
        )
        return final

    async def _finish_run(
        self,
        build_id: uuid.UUID,
        counters: "_Counters",
        *,
        status: str,
        error_summary: str | None = None,
    ) -> None:
        await self._session.execute(
            update(KbBuildRun)
            .where(KbBuildRun.build_id == build_id)
            .values(
                status=status,
                error_summary=error_summary,
                completed_at=func.now(),
                sources_seen=counters.sources_seen,
                sources_changed=counters.sources_changed,
                artifacts_created=counters.artifacts_created,
                llm_calls=counters.llm_calls,
                embedding_calls=counters.embedding_calls,
                search_docs_upserted=counters.search_docs_upserted,
            )
        )

    async def _touch_last_seen(self, fetched: NormalizedContent) -> None:
        # acl_teams rides along: an ACL-only config change (an access
        # revocation) must land even when content_hash is unchanged
        await self._session.execute(
            update(SourceItem)
            .where(
                SourceItem.source_type == fetched.source.source_type,
                SourceItem.source_uri == fetched.source.source_uri,
            )
            .values(last_seen_at=func.now(), acl_teams=list(fetched.source.acl_teams))
        )

    async def _is_unchanged(self, fetched: NormalizedContent) -> bool:
        existing = (
            await self._session.execute(
                select(SourceItem.content_hash).where(
                    SourceItem.source_type == fetched.source.source_type,
                    SourceItem.source_uri == fetched.source.source_uri,
                )
            )
        ).scalar_one_or_none()
        return existing == fetched.content_hash

    async def _upsert_source_item(self, fetched: NormalizedContent) -> uuid.UUID:
        ref = fetched.source
        statement = (
            insert(SourceItem)
            .values(
                source_type=ref.source_type,
                source_uri=ref.source_uri,
                source_version=ref.source_version,
                repo=ref.repo,
                branch=ref.branch,
                path=ref.path,
                external_id=ref.external_id,
                acl_teams=list(ref.acl_teams),
                content_hash=fetched.content_hash,
                last_seen_at=text("now()"),
                is_deleted=False,
            )
            .on_conflict_do_update(
                constraint="uq_source_item_source_type_source_uri",
                set_={
                    "source_version": ref.source_version,
                    "acl_teams": list(ref.acl_teams),
                    "content_hash": fetched.content_hash,
                    "last_seen_at": text("now()"),
                    "is_deleted": False,
                },
            )
            .returning(SourceItem.source_id)
        )
        source_id = (await self._session.execute(statement)).scalar_one()
        logger.info(
            "event=source_item_upserted source_id=%s source_uri=%s content_hash=%s",
            source_id,
            ref.source_uri,
            fetched.content_hash,
        )
        return source_id

    async def _process_changed_source(
        self,
        counters: _Counters,
        fetched: NormalizedContent,
        code_key_map: dict[tuple[str, str], uuid.UUID],
        pending_edges: list[_PendingEdges],
    ) -> None:
        source_id = await self._upsert_source_item(fetched)
        artifact_ids = await self._wikify_gated(counters, fetched, source_id)
        if fetched.source.source_type == "github_code":
            artifact_ids += await self._graphify_gated(
                counters, fetched, source_id, code_key_map, pending_edges
            )
        for artifact_id in artifact_ids:
            await self._embed_gated(counters, artifact_id)
        if artifact_ids:
            counters.search_docs_upserted += await self._indexer.upsert_documents(artifact_ids)

    async def _wikify_gated(
        self, counters: _Counters, fetched: NormalizedContent, source_id: uuid.UUID
    ) -> list[uuid.UUID]:
        cache_key = chunk_summary_cache_key(
            source_content_hash=fetched.content_hash,
            chunker_version=CHUNKER_VERSION,
            wikify_prompt_version=PROMPT_VERSION,
            model_name=self._wikifier.model_name,
            model_params_hash=self._wikifier.model_params_hash,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
        )
        hit = await self._generation_gate.lookup(cache_key)
        if hit is not None:
            artifact_ids = await self._generation_gate.lookup_artifact_ids(cache_key)
            if not artifact_ids:
                # Every known wikifier emits >= 1 draft, so an empty mapping on a
                # hit almost certainly means a corrupt/unbackfilled cache row;
                # surface it rather than silently dropping artifacts.
                logger.warning(
                    "event=wikify_cache_hit_empty_mapping cache_key=%s source_uri=%s",
                    cache_key,
                    fetched.source.source_uri,
                )
            return artifact_ids
        drafts = await self._wikifier.wikify(fetched)
        counters.llm_calls += 1
        # write_wikify_artifacts flushes BEFORE the cache row is recorded (same
        # transaction) so a cache row can never exist without its output artifacts.
        artifact_ids = await write_wikify_artifacts(
            self._session, source_id=source_id, kb_version=self._kb_version, drafts=drafts
        )
        counters.artifacts_created += len(artifact_ids)
        await self._generation_gate.record(
            cache_key=cache_key,
            input_hash=fetched.content_hash,
            prompt_version=PROMPT_VERSION,
            model_name=self._wikifier.model_name,
            model_params_hash=self._wikifier.model_params_hash,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            output_artifact_ids=artifact_ids,
        )
        return artifact_ids

    async def _graphify_gated(
        self,
        counters: _Counters,
        fetched: NormalizedContent,
        source_id: uuid.UUID,
        code_key_map: dict[tuple[str, str], uuid.UUID],
        pending_edges: list[_PendingEdges],
    ) -> list[uuid.UUID]:
        """Graphify one changed code file; returns its code artifact ids so they
        reach embed/index on hits and misses alike (graphify itself is a
        deterministic parser, not an LLM call — llm_calls stays untouched).
        Edges are only collected here; they are resolved and written once at the
        end of the run, after every changed file's artifacts exist."""
        ref = fetched.source
        cache_key = code_graph_cache_key(
            repo=ref.repo or "",
            commit_sha=ref.source_version,
            file_path=ref.path or "",
            file_content_hash=fetched.content_hash,
            graphify_version=GRAPHIFY_VERSION,
            parser_config_version=PARSER_CONFIG_VERSION,
        )
        if await self._generation_gate.lookup(cache_key) is not None:
            artifact_ids = await self._generation_gate.lookup_artifact_ids(cache_key)
            if not artifact_ids:
                # Every file graph yields at least its code_file artifact, so an
                # empty mapping on a hit means a corrupt/unbackfilled cache row.
                logger.warning(
                    "event=graphify_cache_hit_empty_mapping cache_key=%s source_uri=%s",
                    cache_key,
                    ref.source_uri,
                )
            return artifact_ids
        result = await self._graphifier.graphify(fetched)
        key_to_id = await write_code_artifacts(
            self._session,
            source_id=source_id,
            kb_version=self._kb_version,
            drafts=result.artifacts,
        )
        counters.artifacts_created += len(key_to_id)
        repo = ref.repo or ""
        code_key_map.update({(repo, key): artifact_id for key, artifact_id in key_to_id.items()})
        if result.edges:
            pending_edges.append(_PendingEdges(repo=repo, drafts=result.edges))
        artifact_ids = list(key_to_id.values())
        await self._generation_gate.record(
            cache_key=cache_key,
            input_hash=fetched.content_hash,
            prompt_version=GRAPHIFY_VERSION,
            model_name="graphify",
            model_params_hash=PARSER_CONFIG_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            output_artifact_ids=artifact_ids,
        )
        return artifact_ids

    async def _write_pending_edges(
        self, code_key_map: dict[tuple[str, str], uuid.UUID], pending_edges: list[_PendingEdges]
    ) -> None:
        """Single end-of-run resolution pass: every changed file's artifacts are
        already flushed, so cross-file targets within this build resolve to this
        build's artifacts regardless of connector iteration order."""
        for pending in pending_edges:
            await write_code_edges(
                self._session,
                kb_version=self._kb_version,
                repo=pending.repo,
                drafts=pending.drafts,
                key_to_id=code_key_map,
            )

    async def _embed_gated(self, counters: _Counters, artifact_id: uuid.UUID) -> None:
        artifact = await self._session.get(KnowledgeArtifact, artifact_id)
        if artifact is None or artifact.body_text is None:
            return
        text_hash = content_hash(artifact.body_text)
        hit = await self._embedding_gate.lookup(
            artifact_id=artifact_id,
            text_hash=text_hash,
            embedding_model=self._embedder.embedding_model,
        )
        if hit is not None:
            return
        result = await self._embedder.embed(artifact.body_text)
        counters.embedding_calls += 1
        await self._embedding_gate.record(
            artifact_id=artifact_id,
            text_hash=text_hash,
            embedding_model=self._embedder.embedding_model,
            embedding_hash=result.embedding_hash,
            embedding=result.vector,
        )
