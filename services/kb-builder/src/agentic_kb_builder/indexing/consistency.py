"""Index-vs-registry drift validation (architecture §14, invariant 5).

The consistency check is the ValidationHook that gates kb_version activation:
a build whose index does not faithfully mirror the registry is marked
validation_failed and the previous active version keeps serving. Three drift
classes, each logged with a structured event so a failure is diagnosable from
logs alone: missing (in registry, not index), orphaned (in index, not
registry — delete_orphaned_docs should have removed these), and drifted
(artifact_hash mismatch, including hash-vs-None).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.application.active_version import ValidationHook
from agentic_kb_builder.indexing.projection import load_doc_hashes
from agentic_kb_builder.infrastructure.azure_search.search_client import SearchClient
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


async def validate_index_consistency(
    session: AsyncSession, kb_version: str, *, client: SearchClient
) -> bool:
    expected = await load_doc_hashes(session)
    actual = (await client.fetch_index_state()).docs

    missing = sorted(set(expected) - set(actual))
    orphaned = sorted(set(actual) - set(expected))
    drifted = sorted(
        doc_id for doc_id in set(expected) & set(actual) if expected[doc_id] != actual[doc_id]
    )

    consistent = not (missing or orphaned or drifted)
    if missing:
        logger.error(
            "event=index_drift class=missing kb_version=%s count=%d doc_ids=%s",
            kb_version,
            len(missing),
            missing[:20],
        )
    if orphaned:
        logger.error(
            "event=index_drift class=orphaned kb_version=%s count=%d doc_ids=%s",
            kb_version,
            len(orphaned),
            orphaned[:20],
        )
    if drifted:
        logger.error(
            "event=index_drift class=drifted kb_version=%s count=%d doc_ids=%s",
            kb_version,
            len(drifted),
            drifted[:20],
        )
    logger.info(
        "event=index_consistency_validated kb_version=%s expected=%d actual=%d consistent=%s",
        kb_version,
        len(expected),
        len(actual),
        consistent,
    )
    return consistent


def make_consistency_validator(client: SearchClient) -> ValidationHook:
    """Bind a SearchClient into the (session, kb_version) hook shape."""

    async def _validate(session: AsyncSession, kb_version: str) -> bool:
        return await validate_index_consistency(session, kb_version, client=client)

    return _validate
