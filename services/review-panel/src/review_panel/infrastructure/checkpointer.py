"""Durable graph state (LangGraph checkpointer) in the `review_panel` schema.

Crash-resume durability: a killed run's thread resumes instead of re-paying the
four reviewer LLM calls. Bootstrap is idempotent (the saver's own migration
table); the connection's search_path confines every table to `review_panel`.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from review_panel.infrastructure.postgres import REVIEW_PANEL_SCHEMA, review_panel_connection
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.checkpointer")


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncGenerator[AsyncPostgresSaver, None]:
    """Open an AsyncPostgresSaver whose tables live only in `review_panel`."""
    async with review_panel_connection(database_url) as conn:
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
        logger.info("event=checkpointer_ready schema=%s", REVIEW_PANEL_SCHEMA)
        yield saver
