"""kb_search dual-cap budget logic (ADR-0025 §4, PR-37).

The one enforced restriction is a call-count cap AND a token cap — BOTH
required, each closing the budget independently (kb_agent.py's proven
``_kb_budget_open`` shape). One axis without the other is a bug.
"""

from agentic_mcp_server.context_broker.budgets import (
    AgentAllowance,
    AgentUsage,
    KbSearchBudgetStore,
    kb_budget_open,
)

ALLOWANCE = AgentAllowance(max_requests=2, max_tokens=1000)


def test_budget_open_only_while_both_caps_remain() -> None:
    assert kb_budget_open(ALLOWANCE, AgentUsage(requests=0, tokens=0)) is True
    assert kb_budget_open(ALLOWANCE, AgentUsage(requests=1, tokens=999)) is True


def test_call_cap_closes_the_budget_even_with_tokens_left() -> None:
    """Call axis alone: all requests spent, plenty of tokens remaining."""
    assert kb_budget_open(ALLOWANCE, AgentUsage(requests=2, tokens=0)) is False


def test_token_cap_closes_the_budget_even_with_calls_left() -> None:
    """Token axis alone: tokens spent (or overdrawn), calls remaining."""
    assert kb_budget_open(ALLOWANCE, AgentUsage(requests=0, tokens=1000)) is False
    # tokens are charged after the answer, so the final call may overdraw
    assert kb_budget_open(ALLOWANCE, AgentUsage(requests=1, tokens=4321)) is False


def test_zero_allowance_means_never_open() -> None:
    """max_requests: 0 is valid config (mcp-tools-contract.md) — the subject may never search."""
    assert kb_budget_open(AgentAllowance(max_requests=0, max_tokens=1000), AgentUsage()) is False
    assert kb_budget_open(AgentAllowance(max_requests=5, max_tokens=0), AgentUsage()) is False


def test_window_is_shared_within_a_session_and_isolated_across_them() -> None:
    store = KbSearchBudgetStore()
    window = store.window_for("session-1", "impl-agent")
    window.usage.requests += 1

    assert store.window_for("session-1", "impl-agent") is window
    assert store.window_for("session-1", "impl-agent").usage.requests == 1
    # a new session (fresh task) or another subject starts a fresh window
    assert store.window_for("session-2", "impl-agent").usage.requests == 0
    assert store.window_for("session-1", "test-agent").usage.requests == 0


def test_store_is_lru_bounded_and_touch_preserves_active_windows() -> None:
    store = KbSearchBudgetStore(max_windows=2)
    first = store.window_for("s1", "a")
    store.window_for("s2", "a")
    # touching s1 makes s2 the least-recently-used; inserting s3 evicts s2, not s1
    assert store.window_for("s1", "a") is first
    store.window_for("s3", "a")
    assert store.window_for("s1", "a") is first
    assert ("s2", "a") not in store.windows
