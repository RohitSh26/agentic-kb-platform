"""Pure, DB-free unit tests for the PR-33 temporal semantics module.

Covers deterministic source-kind derivation, current/superseded state, per-intent
weighting (incl. determinism: same inputs ⇒ same weight), the stale-doc signal,
and that the weighting is logged (event=temporal_weight*).
"""

import logging
import uuid

import pytest

from agentic_mcp_server.context_broker.temporal import (
    TemporalSignals,
    compute_weight,
    derive_source_kind,
    derive_state,
    is_stale_doc_for_intent,
    referenced_symbols,
)


def _signals(
    *,
    source_type: str | None = "github_code",
    artifact_type: str = "code_symbol",
    invalidated_at_seq: int | None = None,
    source_is_deleted: bool = False,
) -> TemporalSignals:
    return TemporalSignals(
        source_type=source_type,
        artifact_type=artifact_type,
        invalidated_at_seq=invalidated_at_seq,
        source_is_deleted=source_is_deleted,
    )


@pytest.mark.parametrize(
    ("source_type", "artifact_type", "expected"),
    [
        ("github_code", "code_symbol", "code"),
        ("github_code", "code_file", "code"),
        (None, "code_symbol", "code"),  # structural type wins regardless of source
        ("github_doc", "doc_chunk", "doc"),
        ("azure_wiki", "chunk", "doc"),
        ("ado_card", "card", "card"),
        ("github_doc", "adr", "adr"),
        ("git_metadata", "commit", "pr"),
        ("github_doc", "pull_request", "pr"),
        ("mystery", "mystery", "other"),
    ],
)
def test_derive_source_kind(source_type: str | None, artifact_type: str, expected: str) -> None:
    assert derive_source_kind(source_type, artifact_type) == expected


def test_derive_state_current_vs_superseded() -> None:
    assert derive_state(_signals()) == "current"
    assert derive_state(_signals(invalidated_at_seq=5)) == "superseded"
    assert derive_state(_signals(source_is_deleted=True)) == "superseded"


def test_how_intent_lifts_current_code_over_doc() -> None:
    aid = uuid.uuid4()
    code = compute_weight(
        artifact_id=aid,
        intent="how_does_x_work",
        signals=_signals(source_type="github_code", artifact_type="code_symbol"),
        stale_for_intent=False,
    )
    doc = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="how_does_x_work",
        signals=_signals(source_type="github_doc", artifact_type="doc_chunk"),
        stale_for_intent=False,
    )
    assert code.weight > doc.weight


def test_why_intent_lifts_cards_and_prs_over_code() -> None:
    card = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="why_was_x_changed",
        signals=_signals(source_type="ado_card", artifact_type="card"),
        stale_for_intent=False,
    )
    pr = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="why_was_x_changed",
        signals=_signals(source_type="git_metadata", artifact_type="commit"),
        stale_for_intent=False,
    )
    code = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="why_was_x_changed",
        signals=_signals(source_type="github_code", artifact_type="code_symbol"),
        stale_for_intent=False,
    )
    assert card.weight > code.weight
    assert pr.weight > code.weight


def test_superseded_is_downranked_but_not_removed() -> None:
    current = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="how_does_x_work",
        signals=_signals(),
        stale_for_intent=False,
    )
    superseded = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="how_does_x_work",
        signals=_signals(invalidated_at_seq=9),
        stale_for_intent=False,
    )
    assert superseded.weight < current.weight
    assert superseded.weight > 0.0  # weighted, never removed


def test_neutral_intent_is_kind_independent() -> None:
    code = compute_weight(
        artifact_id=uuid.uuid4(),
        intent=None,
        signals=_signals(source_type="github_code", artifact_type="code_symbol"),
        stale_for_intent=False,
    )
    doc = compute_weight(
        artifact_id=uuid.uuid4(),
        intent=None,
        signals=_signals(source_type="github_doc", artifact_type="doc_chunk"),
        stale_for_intent=False,
    )
    assert code.weight == doc.weight == 1.0


def test_weight_is_deterministic() -> None:
    aid = uuid.uuid4()
    first = compute_weight(
        artifact_id=aid,
        intent="how_does_x_work",
        signals=_signals(),
        stale_for_intent=False,
    )
    second = compute_weight(
        artifact_id=aid,
        intent="how_does_x_work",
        signals=_signals(),
        stale_for_intent=False,
    )
    assert first == second


def test_weight_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="agentic_mcp_server.context_broker.temporal"):
        compute_weight(
            artifact_id=uuid.uuid4(),
            intent="how_does_x_work",
            signals=_signals(),
            stale_for_intent=False,
        )
    assert any("event=temporal_weight" in record.getMessage() for record in caplog.records)


def test_referenced_symbols_extracts_backticked_identifiers() -> None:
    refs = referenced_symbols("The `helper` calls `pkg.util.compute` and `EvidenceCard`.")
    assert "helper" in refs
    assert "EvidenceCard" in refs
    assert "pkg.util.compute" in refs
    assert "compute" in refs  # trailing segment of a dotted name


def test_stale_doc_flagged_for_how_when_symbol_removed() -> None:
    current_symbols = frozenset({"helper", "compute"})
    stale = is_stale_doc_for_intent(
        intent="how_does_x_work",
        source_kind="doc",
        body_text="See `removed_symbol` for the legacy path.",
        title=None,
        current_symbols=current_symbols,
    )
    assert stale is True


def test_stale_doc_not_flagged_when_symbol_current() -> None:
    current_symbols = frozenset({"helper"})
    stale = is_stale_doc_for_intent(
        intent="how_does_x_work",
        source_kind="doc",
        body_text="See `helper` for details.",
        title=None,
        current_symbols=current_symbols,
    )
    assert stale is False


def test_stale_signal_does_not_apply_to_why_intent() -> None:
    # A doc referencing a removed symbol is exactly the history "why" wants.
    stale = is_stale_doc_for_intent(
        intent="why_was_x_changed",
        source_kind="doc",
        body_text="See `removed_symbol`.",
        title=None,
        current_symbols=frozenset(),
    )
    assert stale is False


def test_stale_signal_only_applies_to_docs() -> None:
    stale = is_stale_doc_for_intent(
        intent="how_does_x_work",
        source_kind="code",
        body_text="`removed_symbol`",
        title=None,
        current_symbols=frozenset(),
    )
    assert stale is False


def test_stale_doc_pushed_below_non_stale_via_weight() -> None:
    stale = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="how_does_x_work",
        signals=_signals(source_type="github_doc", artifact_type="doc_chunk"),
        stale_for_intent=True,
    )
    fresh = compute_weight(
        artifact_id=uuid.uuid4(),
        intent="how_does_x_work",
        signals=_signals(source_type="github_doc", artifact_type="doc_chunk"),
        stale_for_intent=False,
    )
    assert stale.weight < fresh.weight
