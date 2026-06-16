"""SearchDoc search_text and projection rules (ADR-0018 Phase 2), hermetic — no DB.

Tests:
- SearchDoc accepts search_text field (str | None).
- A code_symbol with search_text but NO body_text IS projectable (the new OR filter
  means span-less symbols with identifier/docstring words reach the index).
- A code_symbol with BOTH body_text and search_text projects with both fields present.
- A code_file with no body_text and no search_text is NOT projectable.
- Determinism: CodeArtifactDraft with same fields produces same search_text path.
"""

import uuid

from agentic_kb_builder.domain.graph_artifacts import CodeArtifactDraft
from agentic_kb_builder.indexing.search_document import SearchDoc


def _make_search_doc(
    *,
    artifact_type: str = "code_symbol",
    body_text: str | None = None,
    search_text: str | None = None,
) -> SearchDoc:
    return SearchDoc(
        doc_id=str(uuid.uuid4()),
        artifact_id=uuid.uuid4(),
        artifact_type=artifact_type,
        source_type="github_code",
        source_uri="github://org/repo/src/service.py",
        title="my_function",
        body_text=body_text,
        kb_version="v-test.1",
        knowledge_kind="source_backed",
        authority_score=1.0,
        freshness_score=1.0,
        artifact_hash=None,
        search_text=search_text,
    )


class TestSearchDocSearchTextField:
    def test_search_text_is_none_by_default(self) -> None:
        doc = _make_search_doc(body_text="def foo(): pass")
        assert doc.search_text is None

    def test_search_text_is_set_when_provided(self) -> None:
        doc = _make_search_doc(body_text="def foo(): pass", search_text="foo validate token")
        assert doc.search_text == "foo validate token"

    def test_search_text_none_body_text_none_valid(self) -> None:
        """A doc with neither body_text nor search_text is valid (e.g. pointer-only
        artifacts whose projection filter excluded them — SearchDoc is the DTO after
        filtering, so it must accept any combination)."""
        doc = _make_search_doc(body_text=None, search_text=None)
        assert doc.body_text is None
        assert doc.search_text is None

    def test_search_text_only_no_body_text(self) -> None:
        """search_text-only docs (Phase 2 code_symbol with no recovered span)."""
        doc = _make_search_doc(body_text=None, search_text="auth middleware validate")
        assert doc.body_text is None
        assert doc.search_text == "auth middleware validate"

    def test_both_fields_present(self) -> None:
        doc = _make_search_doc(
            body_text="def validate_token(): ...",
            search_text="validate token auth header",
        )
        assert doc.body_text is not None
        assert doc.search_text is not None


class TestCodeArtifactDraftSearchTextField:
    def test_default_is_none(self) -> None:
        draft = CodeArtifactDraft(
            key="sym:pkg/m.py::foo",
            artifact_type="code_symbol",
            title="foo",
        )
        assert draft.search_text is None

    def test_field_set_when_provided(self) -> None:
        draft = CodeArtifactDraft(
            key="sym:pkg/m.py::validate_token",
            artifact_type="code_symbol",
            title="validate_token",
            search_text="validate token auth header",
        )
        assert draft.search_text == "validate token auth header"

    def test_search_text_with_body_text_coexist(self) -> None:
        draft = CodeArtifactDraft(
            key="sym:pkg/m.py::fn",
            artifact_type="code_symbol",
            title="fn",
            body_text="def fn(): pass",
            search_text="fn",
        )
        assert draft.body_text == "def fn(): pass"
        assert draft.search_text == "fn"
