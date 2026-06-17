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
)
from agentic_kb_builder.application.invalidation import run_invalidation_pass
from agentic_kb_builder.application.write_commit import write_commit_artifact
from agentic_kb_builder.connectors import Connector
from agentic_kb_builder.connectors.git_metadata import parse_changed_files
from agentic_kb_builder.domain import (
    NormalizedContent,
    WikifyArtifactDraft,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.schema_versions import (
    CHUNKER_VERSION,
    OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
)
from agentic_kb_builder.graphify.graphify_backend import graphify_tree
from agentic_kb_builder.graphify.write import write_code_artifacts, write_code_edges
from agentic_kb_builder.infrastructure.postgres.models import (
    KbBuildRun,
    KnowledgeArtifact,
    SourceItem,
)
from agentic_kb_builder.linker.judge import RelationshipJudge, run_judge
from agentic_kb_builder.linker.run import run_linker
from agentic_kb_builder.linker.run_candidates import run_candidate_generator
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

    async def reconcile_missing(self) -> int:
        """Back-fill index docs the registry has but the index lacks/has stale."""
        ...


@dataclass(frozen=True)
class _CodeUnit:
    """One current code file collected for the whole-tree graphify pass."""

    path: str
    text: str
    source_id: uuid.UUID
    acl: list[str]
    repo: str


def _path_of_code_key(key: str) -> str:
    """Repo-relative file path a code artifact key belongs to (``file:p`` / ``sym:p::name``)."""
    if key.startswith("file:"):
        return key[len("file:") :]
    if key.startswith("sym:"):
        return key[len("sym:") :].split("::", 1)[0]
    return ""


@dataclass
class _Counters:
    sources_seen: int = 0
    sources_changed: int = 0
    artifacts_created: int = 0
    llm_calls: int = 0
    embedding_calls: int = 0
    search_docs_upserted: int = 0
    # code files whose AST extraction raised; backs the extractor-error-rate
    # publish gate (docs/contracts/publish-gates.md). A failed file is skipped,
    # not fatal — one unparsable file must not abort an otherwise-good build.
    extractor_failures: int = 0


class BuildRunner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        kb_version: str,
        wikifier: Wikifier,
        embedder: Embedder,
        indexer: SearchIndexer,
        similarity: SimilarityProvider | None = None,
        judge: RelationshipJudge | None = None,
    ) -> None:
        self._session = session
        self._kb_version = kb_version
        # Assigned once at run start from the kb_build_seq SEQUENCE (set in run()).
        self._build_seq: int = 0
        self._wikifier = wikifier
        self._embedder = embedder
        self._indexer = indexer
        self._similarity = similarity
        # The phase-3B relationship judge (PR-29). None ⇒ no judging this build
        # (candidate generation still runs so the audit set stays current).
        self._judge = judge
        self._generation_gate = GenerationCacheGate(session)
        self._embedding_gate = EmbeddingCacheGate(session)

    async def run(self, connectors: Sequence[Connector]) -> KbBuildRun:
        """Execute one build. The runner owns the session's transactions: the run
        row is committed up front so the audit record survives a failed build;
        per-source work is committed only when the whole build succeeds."""
        build_id = await self._start_run()
        # Connector plan up front so a watcher sees the build's shape before the first
        # fetch (count + the source types in play). Additive; cheap — just the configured
        # connectors, not a full source listing.
        connector_types = ",".join(sorted({str(c.source_type) for c in connectors})) or "none"
        logger.info(
            "event=build_connectors_planned build_id=%s connectors=%d source_types=%s",
            build_id,
            len(connectors),
            connector_types,
        )
        counters = _Counters()
        try:
            seen_source_ids, changed_source_ids = await self._process_sources(connectors, counters)
            await self._finalize_graph(seen_source_ids, changed_source_ids)
            await self._reconcile_index(build_id, counters)
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

    async def _start_run(self) -> uuid.UUID:
        """Allocate the build_seq, persist the running kb_build_run row, and commit
        it up front so the audit record survives a failed build. Returns build_id."""
        # Monotonic build_seq, assigned once at run start (ADR-0013): the active
        # build's build_seq is the served interval-membership cutoff. nextval is
        # safe under concurrent builds (the SEQUENCE serialises allocation).
        self._build_seq = (
            await self._session.execute(select(func.nextval("kb_build_seq")))
        ).scalar_one()
        run = KbBuildRun(
            kb_version=self._kb_version,
            build_seq=self._build_seq,
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
        logger.info(
            "event=build_run_started build_id=%s kb_version=%s build_seq=%d",
            build_id,
            self._kb_version,
            self._build_seq,
        )
        return build_id

    async def _process_sources(
        self, connectors: Sequence[Connector], counters: _Counters
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Fetch every source; skip unchanged ones, process changed ones. Returns
        (seen_source_ids, changed_source_ids) for the invalidation pass.

        seen_source_ids: every source_item observed this build (by source_id); the
        deletion sweep retires every live source NOT in this set.
        changed_source_ids: sources whose CONTENT changed (cache miss ⇒ new
        artifacts at this build_seq); the supersession sweep retires their PRIOR
        generation so the new version does not serve both."""
        code_key_map: dict[tuple[str, str], uuid.UUID] = {}
        # Code (github_code) is graphified as ONE whole tree (Graphify resolves cross-file
        # imports/calls/uses only when it sees all files together — ADR-0012), so every current
        # code file is collected here and the whole graph is (re)built after the loop. Docs and
        # commits keep their incremental per-file path.
        code_units: list[_CodeUnit] = []
        any_code_changed = False
        seen_source_ids: set[uuid.UUID] = set()
        changed_source_ids: set[uuid.UUID] = set()
        for connector in connectors:
            for ref in await connector.list_sources():
                fetched = await connector.fetch(ref)
                counters.sources_seen += 1
                if fetched.source.source_type == "github_code" and ref.path:
                    # Code is graphified as ONE whole tree (Graphify resolves cross-file edges
                    # only when it sees all files), so a single changed file means the whole
                    # graph is rebuilt; if NOTHING changed the prior graph stands (idempotent).
                    changed = not await self._is_unchanged(fetched)
                    source_id = (
                        await self._upsert_source_item(fetched)
                        if changed
                        else await self._touch_last_seen(fetched)
                    )
                    if source_id is None:
                        continue
                    seen_source_ids.add(source_id)
                    code_units.append(
                        _CodeUnit(
                            path=ref.path,
                            text=fetched.text,
                            source_id=source_id,
                            acl=list(fetched.source.acl_teams),
                            repo=ref.repo or "",
                        )
                    )
                    if changed:
                        any_code_changed = True
                    continue
                if await self._is_unchanged(fetched):
                    source_id = await self._touch_last_seen(fetched)
                    if source_id is not None:
                        seen_source_ids.add(source_id)
                    logger.info(
                        "event=build_skip_unchanged source_uri=%s content_hash=%s",
                        ref.source_uri,
                        fetched.content_hash,
                    )
                    continue
                counters.sources_changed += 1
                logger.info(
                    "event=build_source_started connector=%s source_uri=%s "
                    "source_version=%s decision=changed",
                    fetched.source.source_type,
                    ref.source_uri,
                    fetched.source.source_version,
                )
                changed_id = await self._process_changed_source(counters, fetched)
                seen_source_ids.add(changed_id)
                changed_source_ids.add(changed_id)
        if code_units and any_code_changed:
            # the whole code graph is regenerated: mark every code source changed so the
            # supersession sweep retires the prior generation (no duplicate served edges).
            for unit in code_units:
                changed_source_ids.add(unit.source_id)
            counters.sources_changed += len(code_units)
            await self._graphify_code_tree(counters, code_units, code_key_map)
        return seen_source_ids, changed_source_ids

    async def _finalize_graph(
        self, seen_source_ids: set[uuid.UUID], changed_source_ids: set[uuid.UUID]
    ) -> None:
        """Post-source graph work: deterministic linker, cross-domain candidate
        generation + judge, then the identity invalidation pass. Runs AFTER all
        source writes and BEFORE index reconciliation + activation."""
        await run_linker(
            self._session,
            kb_version=self._kb_version,
            valid_from_seq=self._build_seq,
            similarity=self._similarity,
        )
        # Phase 3A/3B (ADR-0010): the cheap, zero-LLM candidate generator emits
        # cross-domain candidate pairs (audit only), then the LLM judge (if
        # configured) rules on them and writes INFERRED_*/AMBIGUOUS edges. Both
        # run AFTER the deterministic linker (so deterministic facts are excluded
        # from candidates) and BEFORE invalidation + activation.
        await run_candidate_generator(
            self._session,
            kb_version=self._kb_version,
            similarity=self._similarity,
        )
        if self._judge is not None:
            await run_judge(
                self._session,
                kb_version=self._kb_version,
                valid_from_seq=self._build_seq,
                judge=self._judge,
            )
        # Invalidation pass (ADR-0013) runs AFTER all writes and the linker, but
        # BEFORE activation: deletion sweep, rename detection, ACL propagation.
        # Version-scoped — it only flips invalidated_at_seq / acl_teams, never
        # physically deletes a live row a prior version still serves.
        await run_invalidation_pass(
            self._session,
            build_seq=self._build_seq,
            seen_source_ids=seen_source_ids,
            changed_source_ids=changed_source_ids,
        )

    async def _reconcile_index(self, build_id: uuid.UUID, counters: _Counters) -> None:
        """Reconcile the search index in BOTH directions before validation, so
        neither an orphaned doc nor a missing member can permanently block
        activation (invariant 5). delete_orphaned removes index-extras;
        reconcile_missing back-fills members the registry has but the index lacks.
        The DB persists across builds while the index may not (it was in-memory and
        gone after a prior build's process exited, or a fresh/reset file); an
        incremental build upserts only changed sources, so without this the index
        can never catch up to the registry. Back-fill reprojects from Postgres — no
        LLM, no re-embed."""
        orphans_removed = await self._indexer.delete_orphaned()
        if orphans_removed:
            logger.info(
                "event=build_index_orphans_removed build_id=%s count=%d",
                build_id,
                orphans_removed,
            )
        backfilled = await self._indexer.reconcile_missing()
        if backfilled:
            counters.search_docs_upserted += backfilled
            logger.info(
                "event=build_index_backfilled build_id=%s count=%d",
                build_id,
                backfilled,
            )

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
                extractor_failures=counters.extractor_failures,
            )
        )

    async def _touch_last_seen(self, fetched: NormalizedContent) -> uuid.UUID | None:
        # acl_teams rides along: an ACL-only config change (an access
        # revocation) must land even when content_hash is unchanged. The returned
        # source_id marks the source SEEN this build (excludes it from the deletion
        # sweep) and feeds ACL propagation onto its live artifacts.
        source_id = (
            await self._session.execute(
                update(SourceItem)
                .where(
                    SourceItem.source_type == fetched.source.source_type,
                    SourceItem.source_uri == fetched.source.source_uri,
                )
                .values(last_seen_at=func.now(), acl_teams=list(fetched.source.acl_teams))
                .returning(SourceItem.source_id)
            )
        ).scalar_one_or_none()
        return source_id

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
        self, counters: _Counters, fetched: NormalizedContent
    ) -> uuid.UUID:
        """Process one changed NON-code source; return its source_id (seen this build).

        Routing (ADR-0018): git_metadata -> one deterministic commit artifact;
        github_doc / azure_wiki / ado_card -> wikify (the LLM is reserved for prose).
        github_code is handled separately as a whole tree (see _graphify_code_tree).
        """
        if fetched.source.source_type == "git_metadata":
            return await self._process_commit_source(counters, fetched)
        source_id = await self._upsert_source_item(fetched)
        # Prose sources (github_doc / azure_wiki / ado_card) go through wikify.
        artifact_ids = await self._wikify_gated(counters, fetched, source_id)
        for artifact_id in artifact_ids:
            await self._embed_gated(counters, artifact_id)
        if artifact_ids:
            counters.search_docs_upserted += await self._indexer.upsert_documents(artifact_ids)
        return source_id

    async def _graphify_code_tree(
        self,
        counters: _Counters,
        code_units: list[_CodeUnit],
        code_key_map: dict[tuple[str, str], uuid.UUID],
    ) -> None:
        """Build the WHOLE code graph in one Graphify pass (ADR-0012, the way the library is
        meant to be used): run Graphify over every current code file together so it resolves
        cross-file imports/calls/uses, then write the artifacts (grouped by their file's source)
        and the edges in one consistent key space. Zero-LLM; embeddings stay body-hash cached."""
        # Run Graphify ONCE PER REPO. Symbolic keys are repo-relative, so two repos that
        # share a path (both have src/utils.py) would collide in a single shared temp tree
        # and cross-bind their edges. A per-repo pass keeps each repo's tree — and therefore
        # its cross-file import/call resolution — isolated to its own files.
        units_by_repo: dict[str, list[_CodeUnit]] = {}
        for unit in code_units:
            units_by_repo.setdefault(unit.repo, []).append(unit)
        for repo, units in sorted(units_by_repo.items()):
            await self._graphify_one_repo(counters, repo, units, code_key_map)

    async def _graphify_one_repo(
        self,
        counters: _Counters,
        repo: str,
        units: list[_CodeUnit],
        code_key_map: dict[tuple[str, str], uuid.UUID],
    ) -> None:
        try:
            result = graphify_tree([(u.path, u.text) for u in units])
            logger.info(
                "event=code_graph_built repo=%s units=%d artifacts=%d edges=%d",
                repo, len(units), len(result.artifacts), len(result.edges),
            )
        except Exception as error:
            counters.extractor_failures += 1
            logger.error(
                "event=graphify_tree_failed repo=%s files=%d error=%s",
                repo,
                len(units),
                f"{type(error).__name__}: {error}",
            )
            return
        unit_by_path = {u.path: u for u in units}
        drafts_by_path: dict[str, list] = {}
        for draft in result.artifacts:
            drafts_by_path.setdefault(_path_of_code_key(draft.key), []).append(draft)
        artifact_ids: list[uuid.UUID] = []
        for path, drafts in drafts_by_path.items():
            unit = unit_by_path.get(path)
            if unit is None:
                continue  # an artifact for a path we did not collect (defensive)
            key_to_id = await write_code_artifacts(
                self._session,
                source_id=unit.source_id,
                kb_version=self._kb_version,
                valid_from_seq=self._build_seq,
                acl_teams=unit.acl,
                drafts=drafts,
            )
            counters.artifacts_created += len(key_to_id)
            code_key_map.update({(repo, k): aid for k, aid in key_to_id.items()})
            artifact_ids.extend(key_to_id.values())
        for artifact_id in artifact_ids:
            await self._embed_gated(counters, artifact_id)
        if artifact_ids:
            counters.search_docs_upserted += await self._indexer.upsert_documents(artifact_ids)
        # Both endpoints resolve against this build's code artifacts for this repo
        # (write_code_edges drops any edge whose endpoint is not present in key_to_id).
        inserted, dropped = await write_code_edges(
            self._session,
            kb_version=self._kb_version,
            valid_from_seq=self._build_seq,
            repo=repo,
            drafts=tuple(result.edges),
            key_to_id=code_key_map,
        )
        logger.info(
            "event=code_graph_edges_written repo=%s inserted=%d dropped=%d",
            repo,
            inserted,
            dropped,
        )

    async def _process_commit_source(
        self, counters: _Counters, fetched: NormalizedContent
    ) -> uuid.UUID:
        """git_metadata path: ONE deterministic commit artifact, zero LLM.

        No wikify, no graphify, no generation-cache row, no llm_calls increment —
        the rendering is fully deterministic from git, so the content_hash skip
        (above) handles incrementality. The artifact is still embedded and
        indexed via the shared deterministic paths so it is retrievable.
        """
        source_id = await self._upsert_source_item(fetched)
        changed_files = parse_changed_files(fetched.text)
        sha = fetched.source.source_version
        title = sha[:12] or sha
        artifact_id = await write_commit_artifact(
            self._session,
            source_id=source_id,
            kb_version=self._kb_version,
            valid_from_seq=self._build_seq,
            title=title,
            body_text=fetched.text,
            changed_files=changed_files,
            repo=fetched.source.repo,
        )
        counters.artifacts_created += 1
        await self._embed_gated(counters, artifact_id)
        counters.search_docs_upserted += await self._indexer.upsert_documents([artifact_id])
        return source_id

    async def _cache_hit_artifact_ids(
        self, cache_key: str, *, empty_event: str, source_uri: str
    ) -> list[uuid.UUID]:
        """Resolve a generation-cache hit to its output artifact ids. An empty
        mapping on a hit means a corrupt/unbackfilled cache row; surface it rather
        than silently dropping artifacts (no silent failures)."""
        artifact_ids = await self._generation_gate.lookup_artifact_ids(cache_key)
        if not artifact_ids:
            logger.warning(
                "event=%s cache_key=%s source_uri=%s",
                empty_event,
                cache_key,
                source_uri,
            )
        return artifact_ids

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
        # Per-file headline as this source ENTERS wikify (path + the model that will
        # generate on a miss). Additive; fires before the cache lookup so the reader sees
        # the file even on a hit (where no model call follows).
        logger.info(
            "event=build_file_wikify source_uri=%s path=%s model=%s",
            fetched.source.source_uri,
            fetched.source.path or "",
            self._wikifier.model_name,
        )
        hit = await self._generation_gate.lookup(cache_key)
        if hit is not None:
            # Every known wikifier emits >= 1 draft, so an empty mapping on a hit
            # almost certainly means a corrupt/unbackfilled cache row.
            return await self._cache_hit_artifact_ids(
                cache_key,
                empty_event="wikify_cache_hit_empty_mapping",
                source_uri=fetched.source.source_uri,
            )
        logger.info(
            "event=wikify_started source_uri=%s path=%s model=%s",
            fetched.source.source_uri,
            fetched.source.path or "",
            self._wikifier.model_name,
        )
        drafts = await self._wikifier.wikify(fetched)
        counters.llm_calls += 1
        # write_wikify_artifacts flushes BEFORE the cache row is recorded (same
        # transaction) so a cache row can never exist without its output artifacts.
        artifact_ids = await write_wikify_artifacts(
            self._session,
            source_id=source_id,
            kb_version=self._kb_version,
            valid_from_seq=self._build_seq,
            acl_teams=list(fetched.source.acl_teams),
            drafts=drafts,
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
