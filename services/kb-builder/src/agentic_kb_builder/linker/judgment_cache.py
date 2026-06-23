"""Relationship-judgment cache key + gate.

Lives in the linker (not application) so the judge step has no dependency on the
application package — keeping it free of the build_runner import cycle. It mirrors
the other cache gates (cache_gates.py): a hit MUST prevent the LLM judge call
(architecture invariant 4), and inserts are idempotent so rebuilds never duplicate
or overwrite a cached verdict.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain import RelationshipJudgment
from agentic_kb_builder.infrastructure.postgres.models import RelationshipJudgmentCache
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# (hash_a, hash_b, relation_schema_version, prompt_version, model_version)
JudgmentCacheKey = tuple[str, str, int, str, str]


def relationship_judgment_cache_parts(
    *,
    hash_a: str,
    hash_b: str,
    relation_schema_version: int,
    prompt_version: str,
    model_version: str,
) -> JudgmentCacheKey:
    """The relationship-judgment cache key.

    Keyed by the two endpoints' content hashes + the relation schema version + the
    judge prompt version + the model version. ``hash_a``/``hash_b`` are SORTED so the
    key is direction-independent: the same unordered pair under a fixed
    schema/prompt/model resolves to ONE cache row. A change to any part is a new
    key ⇒ a miss ⇒ a fresh judge call (re-judges the pair)."""
    low, high = sorted((hash_a, hash_b))
    return (low, high, relation_schema_version, prompt_version, model_version)


class RelationshipJudgmentCacheGate:
    """Lookup/record gate over relationship_judgment_cache; a hit MUST prevent the
    LLM judge call, exactly like generation_cache / embedding_cache."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup(self, key: JudgmentCacheKey) -> RelationshipJudgmentCache | None:
        hit = await self._session.get(RelationshipJudgmentCache, key)
        logger.info(
            "event=judge_cache_lookup hash_a=%s hash_b=%s relation_schema_version=%d "
            "prompt_version=%s model_version=%s hit=%s",
            key[0],
            key[1],
            key[2],
            key[3],
            key[4],
            hit is not None,
        )
        return hit

    async def record(self, key: JudgmentCacheKey, *, judgment: RelationshipJudgment) -> None:
        """Idempotent insert: on-conflict-do-nothing on the composite PK so a rebuild
        never duplicates a cache row and never overwrites a prior verdict."""
        statement = (
            insert(RelationshipJudgmentCache)
            .values(
                hash_a=key[0],
                hash_b=key[1],
                relation_schema_version=key[2],
                prompt_version=key[3],
                model_version=key[4],
                relation_type=judgment.relation_type,
                trust_bucket=judgment.trust_bucket,
                supporting_quote=judgment.supporting_quote,
                reason=judgment.reason,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "hash_a",
                    "hash_b",
                    "relation_schema_version",
                    "prompt_version",
                    "model_version",
                ]
            )
        )
        await self._session.execute(statement)
        logger.info(
            "event=judge_cache_record hash_a=%s hash_b=%s trust_bucket=%s",
            key[0],
            key[1],
            judgment.trust_bucket,
        )


__all__ = [
    "JudgmentCacheKey",
    "RelationshipJudgmentCacheGate",
    "relationship_judgment_cache_parts",
]
