"""Incremental build runner — the 8-step algorithm from docs/architecture §7.

Unchanged content_hash => chunk/wikify/graphify/embed/index are all skipped.
Wikify/Graphify/Embed/Index are Protocols stubbed in this PR (real pipelines
arrive in PR-05/06/08); every model-shaped call is gated by a cache lookup so a
hit never reaches the stub. Generation-cache rows are recorded in the same
transaction as their output artifacts, after the artifacts are persisted, so a
crash between generate and cache can never strand a cache row without output.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from common.hashing import content_hash
from common.logging import get_logger
from contracts.artifact_schemas import NormalizedContent
from contracts.versions import (
    CHUNKER_VERSION,
    GRAPHIFY_VERSION,
    OUTPUT_SCHEMA_VERSION,
    PARSER_CONFIG_VERSION,
    PROMPT_VERSION,
)
from db.models import KbBuildRun, KnowledgeArtifact, KnowledgeEdge, SourceItem
from kb_builder.build.cache import (
    EmbeddingCacheGate,
    GenerationCacheGate,
    chunk_summary_cache_key,
    code_graph_cache_key,
)
from kb_builder.connectors import Connector
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger("kb_builder.build.runner")


@dataclass(frozen=True)
class ArtifactDraft:
    """Placeholder wikify output; the real artifact contract lands in PR-05."""

    artifact_type: str
    title: str | None
    body_text: str


@dataclass(frozen=True)
class EdgeDraft:
    """Placeholder graphify output; the real edge contract lands in PR-06."""

    from_artifact_id: uuid.UUID
    to_artifact_id: uuid.UUID
    edge_type: str
    confidence: float | None = None
    source: str | None = None


class Wikifier(Protocol):
    model_name: str
    model_params_hash: str

    async def wikify(self, content: NormalizedContent) -> Sequence[ArtifactDraft]: ...


class Graphifier(Protocol):
    async def graphify(
        self, content: NormalizedContent, artifact_ids: Sequence[uuid.UUID]
    ) -> Sequence[EdgeDraft]: ...


class Embedder(Protocol):
    embedding_model: str

    async def embed(self, text: str) -> str:
        """Return the embedding_hash for the text."""
        ...


class SearchIndexer(Protocol):
    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int: ...


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
    ) -> None:
        self._session = session
        self._kb_version = kb_version
        self._wikifier = wikifier
        self._graphifier = graphifier
        self._embedder = embedder
        self._indexer = indexer
        self._generation_gate = GenerationCacheGate(session)
        self._embedding_gate = EmbeddingCacheGate(session)

    async def run(self, connectors: Sequence[Connector]) -> KbBuildRun:
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
        logger.info(
            "event=build_run_started build_id=%s kb_version=%s", run.build_id, run.kb_version
        )
        try:
            for connector in connectors:
                for ref in await connector.list_sources():
                    fetched = await connector.fetch(ref)
                    run.sources_seen += 1
                    if await self._is_unchanged(fetched):
                        logger.info(
                            "event=build_skip_unchanged source_uri=%s content_hash=%s",
                            ref.source_uri,
                            fetched.content_hash,
                        )
                        continue
                    run.sources_changed += 1
                    await self._process_changed_source(run, fetched)
        except Exception as error:
            run.status = "failed"
            run.error_summary = f"{type(error).__name__}: {error}"
            run.completed_at = await self._now()
            await self._session.flush()
            logger.error(
                "event=build_run_failed build_id=%s error=%s", run.build_id, run.error_summary
            )
            raise
        run.status = "completed"
        run.completed_at = await self._now()
        await self._session.flush()
        logger.info(
            "event=build_run_completed build_id=%s sources_seen=%d sources_changed=%d "
            "llm_calls=%d embedding_calls=%d search_docs_upserted=%d",
            run.build_id,
            run.sources_seen,
            run.sources_changed,
            run.llm_calls,
            run.embedding_calls,
            run.search_docs_upserted,
        )
        return run

    async def _now(self) -> datetime:
        return (await self._session.execute(select(func.now()))).scalar_one()

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
                content_hash=fetched.content_hash,
                last_seen_at=text("now()"),
                is_deleted=False,
            )
            .on_conflict_do_update(
                constraint="uq_source_item_source_type_source_uri",
                set_={
                    "source_version": ref.source_version,
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

    async def _process_changed_source(self, run: KbBuildRun, fetched: NormalizedContent) -> None:
        source_id = await self._upsert_source_item(fetched)
        artifact_ids = await self._wikify_gated(run, fetched, source_id)
        if fetched.source.source_type == "github_code":
            await self._graphify_gated(run, fetched, artifact_ids)
        for artifact_id in artifact_ids:
            await self._embed_gated(run, artifact_id)
        if artifact_ids:
            run.search_docs_upserted += await self._indexer.upsert_documents(artifact_ids)

    async def _wikify_gated(
        self, run: KbBuildRun, fetched: NormalizedContent, source_id: uuid.UUID
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
            return [hit.output_artifact_id] if hit.output_artifact_id is not None else []
        drafts = await self._wikifier.wikify(fetched)
        run.llm_calls += 1
        artifacts: list[KnowledgeArtifact] = []
        for draft in drafts:
            artifact = KnowledgeArtifact(
                artifact_type=draft.artifact_type,
                source_id=source_id,
                title=draft.title,
                body_text=draft.body_text,
                content_hash=content_hash(draft.body_text),
                kb_version=self._kb_version,
            )
            self._session.add(artifact)
            artifacts.append(artifact)
            run.artifacts_created += 1
        # flush artifacts BEFORE recording the cache row (same transaction) so a
        # cache row can never exist without its output artifact.
        await self._session.flush()
        artifact_ids = [artifact.artifact_id for artifact in artifacts]
        await self._generation_gate.record(
            cache_key=cache_key,
            input_hash=fetched.content_hash,
            prompt_version=PROMPT_VERSION,
            model_name=self._wikifier.model_name,
            model_params_hash=self._wikifier.model_params_hash,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            output_artifact_id=artifact_ids[0] if artifact_ids else None,
        )
        return artifact_ids

    async def _graphify_gated(
        self, run: KbBuildRun, fetched: NormalizedContent, artifact_ids: Sequence[uuid.UUID]
    ) -> None:
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
            return
        edges = await self._graphifier.graphify(fetched, artifact_ids)
        for edge in edges:
            self._session.add(
                KnowledgeEdge(
                    from_artifact_id=edge.from_artifact_id,
                    to_artifact_id=edge.to_artifact_id,
                    edge_type=edge.edge_type,
                    confidence=edge.confidence,
                    source=edge.source,
                    kb_version=self._kb_version,
                )
            )
        await self._session.flush()
        await self._generation_gate.record(
            cache_key=cache_key,
            input_hash=fetched.content_hash,
            prompt_version=GRAPHIFY_VERSION,
            model_name="graphify",
            model_params_hash=PARSER_CONFIG_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            output_artifact_id=None,
        )

    async def _embed_gated(self, run: KbBuildRun, artifact_id: uuid.UUID) -> None:
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
        embedding_hash = await self._embedder.embed(artifact.body_text)
        run.embedding_calls += 1
        await self._embedding_gate.record(
            artifact_id=artifact_id,
            text_hash=text_hash,
            embedding_model=self._embedder.embedding_model,
            embedding_hash=embedding_hash,
        )
