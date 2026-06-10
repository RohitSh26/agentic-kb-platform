"""WikifyGenerator: chunk -> ModelClient.generate_wikify -> artifact drafts.

Implements the build runner's Wikifier protocol. The generation-cache gate
lives in the runner, so this module is only reached on a cache miss; it makes
exactly one model call per changed source.
"""

from collections.abc import Sequence

from common.logging import get_logger
from contracts.artifact_schemas import NormalizedContent, WikifyArtifactDraft
from contracts.versions import PROMPT_VERSION
from kb_builder.wikify.chunker import chunk_text
from kb_builder.wikify.model_client import ModelClient

logger = get_logger("kb_builder.wikify.generate")

# V1 seed scores (tune from retrieval logs; structural changes need an ADR).
# Interpreted knowledge (summary/concept) sits strictly below source-backed
# artifacts so the broker can never rank a summary as final truth (§5 risk).
CHUNK_AUTHORITY = 1.0
FACT_AUTHORITY = 0.8
CONCEPT_AUTHORITY = 0.6
SUMMARY_AUTHORITY = 0.5
# Freshly generated from the current source version; runtime decay is broker policy.
BUILD_TIME_FRESHNESS = 1.0


class WikifyGenerator:
    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    @property
    def model_name(self) -> str:
        return self._model_client.model_name

    @property
    def model_params_hash(self) -> str:
        return self._model_client.model_params_hash

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        chunks = chunk_text(content.text)
        generation = await self._model_client.generate_wikify(
            chunks=chunks, prompt_version=PROMPT_VERSION
        )

        drafts: list[WikifyArtifactDraft] = [
            WikifyArtifactDraft(
                artifact_type="chunk",
                knowledge_kind="source_backed",
                title=None,
                body_text=chunk.text,
                authority_score=CHUNK_AUTHORITY,
                freshness_score=BUILD_TIME_FRESHNESS,
            )
            for chunk in chunks
        ]
        drafts.append(
            WikifyArtifactDraft(
                artifact_type="summary",
                knowledge_kind="interpreted",
                title=None,
                body_text=generation.summary,
                authority_score=SUMMARY_AUTHORITY,
                freshness_score=BUILD_TIME_FRESHNESS,
            )
        )
        drafts.extend(
            WikifyArtifactDraft(
                artifact_type="concept",
                knowledge_kind="interpreted",
                title=concept.name,
                body_text=concept.description,
                authority_score=CONCEPT_AUTHORITY,
                freshness_score=BUILD_TIME_FRESHNESS,
            )
            for concept in generation.concepts
        )
        dropped_facts = 0
        for fact in generation.facts:
            if fact.quote not in content.text:
                # A "source-backed" fact whose quote is not in the source is an
                # invention (invariant 7) — drop it loudly, never store it.
                dropped_facts += 1
                logger.warning(
                    "event=wikify_fact_dropped reason=quote_not_in_source source_uri=%s",
                    content.source.source_uri,
                )
                continue
            drafts.append(
                WikifyArtifactDraft(
                    artifact_type="source_backed_fact",
                    knowledge_kind="source_backed",
                    title=None,
                    body_text=f"{fact.statement}\n\nSource quote:\n{fact.quote}",
                    authority_score=FACT_AUTHORITY,
                    freshness_score=BUILD_TIME_FRESHNESS,
                )
            )

        logger.info(
            "event=wikify_generated source_uri=%s chunks=%d concepts=%d facts=%d "
            "dropped_facts=%d drafts=%d",
            content.source.source_uri,
            len(chunks),
            len(generation.concepts),
            len(generation.facts) - dropped_facts,
            dropped_facts,
            len(drafts),
        )
        return drafts
