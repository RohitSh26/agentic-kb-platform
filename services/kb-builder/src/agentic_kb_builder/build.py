"""`build` — the one product entry point for a local KB build (ADR-0010).

Wires connectors + a local-filesystem fetch backend + the real extractors (Graphify for
code, wikify via the LLM client) + a local embedder + the local Search indexer into the
existing BuildRunner, and runs one incremental build into Postgres — no cloud, no spend.

Usage (from services/kb-builder, with a migrated DATABASE_URL set):

    uv run python -m agentic_kb_builder.build --workspace ../.. --sources ./sources.example.yaml

Env: DATABASE_URL (asyncpg URL, schema already migrated via `alembic upgrade head`),
plus the wikify model vars (LLM_PROVIDER/LLM_MODEL ... default local Ollama). A build only
goes active after the consistency validator passes (invariant 5); pass --no-activate to skip.
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.application.active_version import activate_kb_version, get_active_kb_version
from agentic_kb_builder.application.build_runner import (
    BuildRunner,
    Embedder,
    Graphifier,
    SearchIndexer,
    Wikifier,
)
from agentic_kb_builder.application.publish_gates import make_publish_gate_validator
from agentic_kb_builder.connectors import (
    GitMetadataConnector,
    connectors_from_config,
    load_source_config,
)
from agentic_kb_builder.connectors.config_loader import BackendFactory
from agentic_kb_builder.connectors.local_fs import local_fs_backend_factory
from agentic_kb_builder.connectors.production_factory import production_backend_factory
from agentic_kb_builder.embeddings import LocalHashEmbedder
from agentic_kb_builder.graphify import GraphifyGraphifier
from agentic_kb_builder.indexing import SearchDocUpserter, make_consistency_validator
from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import ChatModelClient
from agentic_kb_builder.infrastructure.azure_search.search_client import SearchClient
from agentic_kb_builder.infrastructure.local_search import LocalFileSearchClient
from agentic_kb_builder.infrastructure.postgres.models import KbBuildRun
from agentic_kb_builder.infrastructure.postgres.session import create_engine, create_session_factory
from agentic_kb_builder.structured_logging import get_logger
from agentic_kb_builder.wikify.generate import WikifyGenerator

logger = get_logger(__name__)


@dataclass(frozen=True)
class Collaborators:
    """The injectable build collaborators — defaulted for local runs, faked in tests."""

    wikifier: Wikifier
    graphifier: Graphifier
    embedder: Embedder
    indexer: SearchIndexer
    search_client: SearchClient  # backs the activation consistency validator


def default_collaborators(session: AsyncSession, *, index_path: Path) -> Collaborators:
    """Real, no-cloud collaborators: LLM wikify (local Ollama by default), Graphify code
    extraction, deterministic local embeddings, and a PERSISTENT local Search projection.

    The projection is file-backed (ADR-0017) so it survives across build invocations the
    same way the Azure index does — an incremental rebuild that upserts nothing still
    validates against the carried-forward membership."""
    client = LocalFileSearchClient(index_path)
    return Collaborators(
        wikifier=WikifyGenerator(ChatModelClient.from_env()),
        graphifier=GraphifyGraphifier(),
        embedder=LocalHashEmbedder(),
        indexer=SearchDocUpserter(session, client),
        search_client=client,
    )


async def run_build(
    session: AsyncSession,
    *,
    sources_path: str,
    workspace: str,
    kb_version: str,
    version: str,
    collaborators: Collaborators,
    activate: bool,
    allow_large_delta: bool = False,
    git_metadata: bool = True,
    backend: str = "local",
) -> KbBuildRun:
    """Run one build into Postgres; activate the new kb_version if every publish
    gate passes (docs/contracts/publish-gates.md). allow_large_delta overrides the
    symbol-count-delta gate only (recorded on kb_build_run, logged). `backend`
    selects the fetch backend: `local` (workspace files) or `production` (real
    GitHub/ADO sources via the production factory, ADR-0015)."""
    config = load_source_config(sources_path)
    factory: BackendFactory
    if backend == "production":
        factory = production_backend_factory()
    else:
        factory = local_fs_backend_factory(Path(workspace), version=version)
    connectors = connectors_from_config(config, factory)
    if git_metadata:
        # Cross-domain phase 2 (PR-26): deterministic, zero-LLM commit artifacts
        # from the local workspace git history. Appended last so its commit
        # artifacts can resolve changed-file → code edges against code artifacts
        # produced earlier in the same build.
        connectors.append(GitMetadataConnector(Path(workspace)))
    runner = BuildRunner(
        session,
        kb_version=kb_version,
        wikifier=collaborators.wikifier,
        graphifier=collaborators.graphifier,
        embedder=collaborators.embedder,
        indexer=collaborators.indexer,
    )
    run = await runner.run(connectors)
    logger.info(
        "event=build_finished kb_version=%s status=%s build_id=%s",
        kb_version,
        run.status,
        run.build_id,
    )
    if activate and run.status == "completed":
        # Record the override BEFORE the gates read it, so the symbol-count-delta
        # gate sees allow_large_delta on this build's kb_build_run row.
        run.allow_large_delta = allow_large_delta
        await session.flush()
        consistency = make_consistency_validator(collaborators.search_client)
        validator = make_publish_gate_validator(consistency)
        activated = await activate_kb_version(session, run.build_id, validator)
        await session.commit()
        logger.info(
            "event=build_activation kb_version=%s activated=%s allow_large_delta=%s",
            kb_version,
            activated,
            allow_large_delta,
        )
    return run


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build", description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("--sources", required=True, help="path to a sources.yaml")
    parser.add_argument(
        "--workspace", required=True, help="workspace root the local-FS backend reads"
    )
    parser.add_argument(
        "--kb-version",
        default=f"local.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        help="kb_version label for this build (default: local.<timestamp>)",
    )
    parser.add_argument("--version", default="local", help="source_version stamp (e.g. a git SHA)")
    parser.add_argument(
        "--backend",
        choices=("local", "production"),
        default="local",
        help="fetch backend: 'local' reads --workspace files (default); 'production' fetches "
        "real GitHub/ADO sources via the production factory (ADR-0015)",
    )
    parser.add_argument(
        "--index-path",
        default=None,
        help="persistent local search index file (default: $KB_LOCAL_INDEX_PATH or "
        "./.kb-local-search-index.json). A rebuildable projection of Postgres — delete it "
        "(or recreate the database) to force a clean reprojection on the next build",
    )
    parser.add_argument("--no-activate", action="store_true", help="build but do not mark active")
    parser.add_argument(
        "--no-git-metadata",
        action="store_true",
        help="skip the git-metadata connector (commit artifacts + cross-domain links)",
    )
    parser.add_argument(
        "--allow-large-delta",
        action="store_true",
        help="override the symbol-count-delta publish gate for an intentional large change "
        "(recorded in kb_build_run and logged); no other gate is overridable",
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    index_path = Path(
        args.index_path or os.environ.get("KB_LOCAL_INDEX_PATH") or ".kb-local-search-index.json"
    )
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            run = await run_build(
                session,
                sources_path=args.sources,
                workspace=args.workspace,
                kb_version=args.kb_version,
                version=args.version,
                collaborators=default_collaborators(session, index_path=index_path),
                activate=not args.no_activate,
                allow_large_delta=args.allow_large_delta,
                git_metadata=not args.no_git_metadata,
                backend=args.backend,
            )
            active = await get_active_kb_version(session)
    finally:
        await engine.dispose()
    # Structured log on the build path (rule: no silent build paths); the prints
    # below are the CLI's human-readable summary to stdout, not the audit record.
    logger.info(
        "event=build_summary status=%s kb_version=%s active_version=%s index_path=%s",
        run.status,
        run.kb_version,
        active,
        index_path,
    )
    print(f"build status : {run.status}")
    print(f"kb_version   : {run.kb_version}")
    print(f"active version: {active}")
    print(f"search index : {index_path}")
    return 0 if run.status in {"completed", "active"} else 1


def main() -> int:
    return asyncio.run(_main(_parse_args(sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
