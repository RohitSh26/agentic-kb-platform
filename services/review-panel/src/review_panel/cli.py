"""CLI entrypoint: compute or return the review DRAFT for one pull request.

    uv run review-panel draft <owner/repo> <pr-number>

Prints the stored/computed review_draft_v1 JSON on stdout (logs go to stderr).
Exit codes: 0 = draft printed; 1 = failure (structured log carries the reason).
The panel never publishes anything (ADR-0031) — the developer's in-session
agent reads this output, the developer edits, and publication happens only on
the developer's ask under the developer's own authorization.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from review_panel.application import DraftOutcome, compute_draft, get_stored_draft
from review_panel.config import PanelConfig, load_config
from review_panel.domain.errors import ReviewPanelError
from review_panel.domain.pr import PRContext
from review_panel.graph.nodes import PanelDependencies
from review_panel.graph.prompts import load_panel_prompts
from review_panel.infrastructure.checkpointer import postgres_checkpointer
from review_panel.infrastructure.draft_store import (
    DraftStore,
    InMemoryDraftStore,
    postgres_draft_store,
)
from review_panel.infrastructure.github_client import HttpxGitHubClient
from review_panel.infrastructure.kb_search import KBSearchClient, McpHttpKBSearch, NullKBSearch
from review_panel.infrastructure.model_client import create_model_client, load_model_settings
from review_panel.infrastructure.postgres_trace_sink import postgres_trace_sink
from review_panel.infrastructure.trace_sink import NullTraceSink, TraceSink
from review_panel.structured_logging import configure_logging, get_logger

logger = get_logger("review_panel.cli")


def _kb_client(config: PanelConfig) -> KBSearchClient:
    if config.mcp_url:
        return McpHttpKBSearch(config.mcp_url, config.mcp_token)
    return NullKBSearch()


async def _compute(
    config: PanelConfig,
    github: HttpxGitHubClient,
    store: DraftStore,
    checkpointer: BaseCheckpointSaver[str],
    trace_sink: TraceSink,
    pr: PRContext,
) -> DraftOutcome:
    # model settings are loaded ONLY on the compute path — fetching a stored
    # draft must work without LLM credentials
    settings = load_model_settings()
    deps = PanelDependencies(
        model=create_model_client(settings),
        github=github,
        kb=_kb_client(config),
        prompts=load_panel_prompts(config.agents_dir),
        store=store,
        model_label=f"{settings.provider}:{settings.model}",
        trace_sink=trace_sink,
    )
    logger.info(
        "event=panel_start repo=%s pr=%s head_sha=%s provider=%s model=%s langsmith_tracing=%s",
        pr.repo,
        pr.number,
        pr.head_sha,
        settings.provider,
        settings.model,
        config.langsmith_tracing,
    )
    return await compute_draft(deps, checkpointer, pr)


async def run_draft(repo: str, pr_number: int) -> int:
    config = load_config()
    github = HttpxGitHubClient(config.github_token, config.github_api_url)
    async with AsyncExitStack() as stack:
        checkpointer: BaseCheckpointSaver[str]
        store: DraftStore
        if config.database_url:
            checkpointer = await stack.enter_async_context(
                postgres_checkpointer(config.database_url)
            )
            store = await stack.enter_async_context(postgres_draft_store(config.database_url))
            logger.info("event=persistence kind=postgres durable=true")
        else:
            # single-process durability only: a killed run re-pays the reviewer
            # calls and nothing outlives this process. Postgres is the durable
            # path (REVIEW_PANEL_DATABASE_URL).
            logger.warning("event=persistence_fallback kind=memory durable=false")
            checkpointer = InMemorySaver()
            store = InMemoryDraftStore()

        trace_sink: TraceSink
        if config.trace_sink not in ("", "postgres", "none"):
            raise RuntimeError(
                f"invalid TRACE_SINK={config.trace_sink!r}; expected 'postgres' or 'none'"
            )
        if config.trace_sink != "none" and config.database_url:
            trace_sink = await stack.enter_async_context(postgres_trace_sink(config.database_url))
        else:
            trace_sink = NullTraceSink()

        pr, stored = await get_stored_draft(github, store, repo, pr_number)
        if stored is not None:
            outcome = DraftOutcome(draft=stored, source="stored")
        else:
            outcome = await _compute(config, github, store, checkpointer, trace_sink, pr)
        logger.info(
            "event=draft_done draft_key=%s source=%s findings=%s",
            outcome.draft.draft_key,
            outcome.source,
            len(outcome.draft.findings),
        )
        # stdout carries ONLY the draft document (contract); logs go to stderr
        print(outcome.draft.model_dump_json(indent=2))
    return 0


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(
        prog="review-panel",
        description="Review-panel draft engine (ADR-0031): drafts only, never publishes.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    draft = subcommands.add_parser(
        "draft", help="compute or return the stored review draft for a pull request"
    )
    draft.add_argument("repo", help="owner/name")
    draft.add_argument("pr", type=int, help="pull request number")
    args = parser.parse_args()
    try:
        return asyncio.run(run_draft(args.repo, args.pr))
    except (ReviewPanelError, RuntimeError) as exc:
        logger.error("event=draft_failed error=%s detail=%s", type(exc).__name__, exc)
        return 1
