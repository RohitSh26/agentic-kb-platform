"""Use-case layer: compute or return the stored draft for one pull request.

Idempotency + durability policy lives here, not in tool wiring:
- a stored draft for the PR's current head SHA is returned without any model call;
- a thread left mid-run by a crash is RESUMED (input None), so completed
  reviewer nodes — and their LLM spend — are never re-executed;
- otherwise the graph runs fresh and its terminal node persists the draft.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from review_panel.domain.draft import ReviewDraft, draft_key
from review_panel.domain.pr import PRContext
from review_panel.graph.build import build_panel_graph
from review_panel.graph.nodes import PanelDependencies
from review_panel.infrastructure.draft_store import DraftStore
from review_panel.infrastructure.github_client import GitHubClient
from review_panel.infrastructure.trace_sink import Span, SpanStatus, emit_span
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.application")

DraftSource = Literal["stored", "computed", "resumed"]


@dataclass(frozen=True)
class DraftOutcome:
    draft: ReviewDraft
    source: DraftSource


async def get_stored_draft(
    github: GitHubClient, store: DraftStore, repo: str, pr_number: int
) -> tuple[PRContext, ReviewDraft | None]:
    """Fetch the PR (read-only) and look up a draft for its current head SHA.

    Needs no model credentials — this is the cheap fetch path the developer's
    in-session agent hits when a draft was already prepared.
    """
    pr = await github.get_pr(repo, pr_number)
    key = draft_key(pr.repo, pr.number, pr.head_sha)
    stored = await store.get(key)
    logger.info(
        "event=draft_lookup draft_key=%s hit=%s", key, "true" if stored is not None else "false"
    )
    return pr, stored


async def compute_draft(
    deps: PanelDependencies,
    checkpointer: BaseCheckpointSaver[str] | None,
    pr: PRContext,
) -> DraftOutcome:
    """Run (or resume) the panel graph for `pr` and return the stored draft."""
    key = draft_key(pr.repo, pr.number, pr.head_sha)
    graph = build_panel_graph(deps, checkpointer)
    config: RunnableConfig = {"configurable": {"thread_id": key}}
    resuming = False
    if checkpointer is not None:
        snapshot = await graph.aget_state(config)
        if snapshot.next:  # pending nodes => a crashed run to resume, not re-pay
            resuming = True
            logger.info("event=panel_resume thread_id=%s next=%s", key, ",".join(snapshot.next))
        elif snapshot.values:
            # A COMPLETED thread whose stored draft row is gone (deleted
            # out-of-band): invoking fresh input on this thread would MERGE into
            # the checkpointed reducer state — operator.add appends four MORE
            # panelist_reviews, feeding eight into reconcile. Clear the thread so
            # the recompute starts from clean state on the same thread_id.
            await checkpointer.adelete_thread(key)
            logger.info(
                "event=panel_thread_cleared thread_id=%s reason=completed_without_stored_draft",
                key,
            )
    span_started = datetime.now(UTC)
    status: SpanStatus = "ok"
    try:
        result = await graph.ainvoke(None if resuming else {"pr": pr}, config)
    except Exception:
        status = "error"
        raise
    finally:
        # Root span for this ONE draft-run attempt (ADR-0032). Its own span_id doubles
        # as the parent every node span in this attempt points at
        # (PanelDependencies.trace_root_span_id) — never checkpointed state.
        await emit_span(
            deps.trace_sink,
            Span(
                trace_id=key,
                span_id=deps.trace_root_span_id,
                parent_span_id=None,
                name="review_panel.draft_run",
                service="review-panel",
                started_at=span_started,
                ended_at=datetime.now(UTC),
                status=status,
                attributes={"resuming": resuming},
            ),
        )
    draft = cast(ReviewDraft, result["draft"])
    return DraftOutcome(draft=draft, source="resumed" if resuming else "computed")


async def compute_or_get_draft(
    deps: PanelDependencies,
    checkpointer: BaseCheckpointSaver[str] | None,
    repo: str,
    pr_number: int,
) -> DraftOutcome:
    """The full idempotent path: stored draft if one exists, else compute/resume."""
    pr, stored = await get_stored_draft(deps.github, deps.store, repo, pr_number)
    if stored is not None:
        return DraftOutcome(draft=stored, source="stored")
    return await compute_draft(deps, checkpointer, pr)
