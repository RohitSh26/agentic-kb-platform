"""Dedupe unit tests: normalization, token-cosine similarity, query history."""

import pytest

from agentic_mcp_server.context_broker.dedupe import QueryHistory, normalize_query, similarity


def test_normalize_query_lowercases_and_strips_punctuation() -> None:
    assert normalize_query("  How does PAYMENT-validation work?! ") == (
        "how does payment validation work"
    )


def test_similarity_identical_queries_is_one() -> None:
    assert similarity("payment validation rules", "payment validation rules") == pytest.approx(1.0)


def test_similarity_disjoint_queries_is_zero() -> None:
    assert similarity("payment validation", "graph traversal depth") == 0.0


def test_similarity_empty_query_is_zero() -> None:
    assert similarity("", "payment validation") == 0.0


def test_similarity_near_duplicate_clears_default_threshold() -> None:
    a = normalize_query("how does payment validation work in checkout")
    b = normalize_query("how does payment validation work in checkout service")
    assert similarity(a, b) >= 0.90


def test_similarity_loosely_related_query_stays_below_threshold() -> None:
    a = normalize_query("payment validation rules")
    b = normalize_query("payment refund email notifications")
    assert similarity(a, b) < 0.88


def test_history_find_exact_only_matches_identical_normalized_text() -> None:
    history = QueryHistory()
    history.record("payment validation rules", ["ev-1"])
    assert history.find_exact("payment validation rules") is not None
    assert history.find_exact("payment validation rule") is None


def test_history_find_semantic_returns_best_match_over_threshold() -> None:
    history = QueryHistory()
    history.record("how does payment validation work in checkout", ["ev-1"])
    history.record("how does payment validation work in checkout today", ["ev-2"])
    match = history.find_semantic(
        "how does payment validation work in checkout today please", threshold=0.85
    )
    assert match is not None
    assert match.evidence_ids == ("ev-2",)


def test_history_find_semantic_respects_threshold() -> None:
    history = QueryHistory()
    history.record("payment validation rules", ["ev-1"])
    assert history.find_semantic("graph traversal depth", threshold=0.88) is None
