"""The durable doc-extraction cache stores the serialized DocExtractionResult and re-maps
it on a hit (ADR-0027). The crash-recovery guarantee depends on that JSON round-trip being
lossless, so lock it here (no DB needed). The DB-backed crash test lives in
tests/integration/test_durable_cache_crash.py.
"""

from agentic_kb_builder.domain import DocArtifactDraft, DocExtractionResult


def _result() -> DocExtractionResult:
    return DocExtractionResult(
        artifacts=(
            DocArtifactDraft(
                artifact_type="summary",
                knowledge_kind="interpreted",
                title="Doc summary",
                body_text="A summary of the document.",
                authority_score=0.5,
                freshness_score=1.0,
            ),
            DocArtifactDraft(
                artifact_type="source_backed_fact",
                knowledge_kind="source_backed",
                title="User embeddings",
                body_text="docs/guide.md#L10",
                authority_score=0.9,
                freshness_score=1.0,
            ),
        )
    )


def test_doc_extraction_result_survives_json_roundtrip() -> None:
    original = _result()
    # exactly what PostgresDurableOutputCache stores and reloads
    stored = original.model_dump(mode="json")
    restored = DocExtractionResult.model_validate(stored)
    assert restored == original
    assert len(restored.artifacts) == 2
    assert restored.artifacts[0].artifact_type == "summary"
    assert restored.artifacts[1].body_text == "docs/guide.md#L10"


def test_stored_form_is_plain_jsonable() -> None:
    import json

    stored = _result().model_dump(mode="json")
    # must survive a real JSON encode/decode (JSONB column) with no custom types
    assert DocExtractionResult.model_validate(json.loads(json.dumps(stored))) == _result()


def test_empty_extraction_roundtrips() -> None:
    empty = DocExtractionResult(artifacts=())
    assert DocExtractionResult.model_validate(empty.model_dump(mode="json")) == empty
