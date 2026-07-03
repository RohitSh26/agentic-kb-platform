"""Postgres isolation + durability: checkpointer AND draft store live in the
`review_panel` schema ONLY; the Knowledge Registry is untouched; crash-resume
through Postgres never re-pays the reviewer LLM calls and lands exactly one
draft row.

Follows the mcp-server convention: requires an external TEST_DATABASE_URL and
skips otherwise (the durability pair also runs hermetically on the in-memory
saver/store in test_crash_resume.py / test_idempotency.py).
"""

import dataclasses
import uuid

import pytest
from panel_test_support import (
    TEST_DATABASE_URL,
    key_of,
    make_deps,
    make_pr,
    panel_input,
    thread_config,
)
from psycopg import AsyncConnection

from review_panel.application import compute_or_get_draft
from review_panel.domain.draft import ReviewDraft
from review_panel.graph.build import STORE_NODE, build_panel_graph
from review_panel.infrastructure.checkpointer import postgres_checkpointer
from review_panel.infrastructure.draft_store import postgres_draft_store
from review_panel.infrastructure.postgres import REVIEW_PANEL_SCHEMA, to_psycopg_url

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

#: Canonical Knowledge Registry tables (docs/contracts/postgres-knowledge-registry.md).
REGISTRY_TABLES = (
    "source_item",
    "knowledge_artifact",
    "knowledge_edge",
    "generation_cache",
    "embedding_cache",
    "kb_build_run",
    "retrieval_event",
)


async def _public_tables(conn: AsyncConnection) -> set[str]:
    cursor = await conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {row[0] for row in await cursor.fetchall()}


async def _registry_row_counts(conn: AsyncConnection, tables: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in REGISTRY_TABLES:
        if table in tables:
            # table names come from the fixed REGISTRY_TABLES tuple, never user input
            cursor = await conn.execute(f"SELECT count(*) FROM public.{table}")
            row = await cursor.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


async def test_crash_resume_through_postgres_confined_to_review_panel_schema() -> None:
    assert TEST_DATABASE_URL is not None
    url = to_psycopg_url(TEST_DATABASE_URL)
    pr = make_pr(head_sha=uuid.uuid4().hex + uuid.uuid4().hex[:8])
    deps, model, _, _ = make_deps(pr=pr)

    async with await AsyncConnection.connect(url) as conn:
        tables_before = await _public_tables(conn)
        registry_before = await _registry_row_counts(conn, tables_before)

    # crash-resume THROUGH POSTGRES: first process dies at the breakpoint...
    async with (
        postgres_checkpointer(TEST_DATABASE_URL) as saver,
        postgres_draft_store(TEST_DATABASE_URL) as store,
    ):
        deps_pg = dataclasses.replace(deps, store=store)
        crashed = build_panel_graph(deps_pg, saver, interrupt_before=[STORE_NODE])
        await crashed.ainvoke(panel_input(pr), thread_config(pr))
        assert await store.get(key_of(pr)) is None
    assert len(model.calls) == 5

    # ...a NEW process (new connections, new saver) resumes and stores exactly once
    async with (
        postgres_checkpointer(TEST_DATABASE_URL) as saver,
        postgres_draft_store(TEST_DATABASE_URL) as store,
    ):
        deps_pg = dataclasses.replace(deps, store=store)
        outcome = await compute_or_get_draft(deps_pg, saver, pr.repo, pr.number)
    assert outcome.source == "resumed"
    assert len(model.calls) == 5  # the reviewer LLM calls were NOT re-executed

    async with await AsyncConnection.connect(url) as conn:
        cursor = await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (REVIEW_PANEL_SCHEMA,),
        )
        panel_tables = {row[0] for row in await cursor.fetchall()}
        cursor = await conn.execute(
            f"SELECT repo, head_sha, draft FROM {REVIEW_PANEL_SCHEMA}.review_draft "
            "WHERE draft_key = %s",
            (key_of(pr),),
        )
        rows = await cursor.fetchall()
        tables_after = await _public_tables(conn)
        registry_after = await _registry_row_counts(conn, tables_after)

    # exactly one draft row, whose jsonb round-trips to the returned draft
    assert len(rows) == 1
    repo, head_sha, draft_doc = rows[0]
    assert repo == pr.repo
    assert head_sha == pr.head_sha
    assert ReviewDraft.model_validate(draft_doc) == outcome.draft

    # checkpointer + draft store live in review_panel ONLY; registry untouched
    assert {"checkpoints", "checkpoint_migrations", "review_draft"} <= panel_tables
    assert tables_after == tables_before
    assert registry_after == registry_before


async def test_same_sha_rerun_through_postgres_is_a_store_hit() -> None:
    assert TEST_DATABASE_URL is not None
    pr = make_pr(head_sha=uuid.uuid4().hex + uuid.uuid4().hex[:8])
    deps, model, _, _ = make_deps(pr=pr)

    async with (
        postgres_checkpointer(TEST_DATABASE_URL) as saver,
        postgres_draft_store(TEST_DATABASE_URL) as store,
    ):
        first = await compute_or_get_draft(
            dataclasses.replace(deps, store=store), saver, pr.repo, pr.number
        )
        assert first.source == "computed"
        assert len(model.calls) == 5

    # a completely new process: the stored row, not the checkpoint, is the guard
    async with (
        postgres_checkpointer(TEST_DATABASE_URL) as saver,
        postgres_draft_store(TEST_DATABASE_URL) as store,
    ):
        second = await compute_or_get_draft(
            dataclasses.replace(deps, store=store), saver, pr.repo, pr.number
        )
    assert second.source == "stored"
    assert second.draft == first.draft
    assert len(model.calls) == 5


async def test_bootstrap_is_idempotent() -> None:
    assert TEST_DATABASE_URL is not None
    async with (
        postgres_checkpointer(TEST_DATABASE_URL),
        postgres_draft_store(TEST_DATABASE_URL),
    ):
        pass
    # second setup: no error, no duplicate tables
    async with (
        postgres_checkpointer(TEST_DATABASE_URL),
        postgres_draft_store(TEST_DATABASE_URL),
    ):
        pass
