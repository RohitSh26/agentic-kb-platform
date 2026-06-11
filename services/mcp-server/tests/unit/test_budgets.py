"""Budget policy unit tests: allowance resolution is keyed by session subject."""

from agentic_mcp_server.context_broker.budgets import (
    DEFAULT_AGENT_ALLOWANCE,
    AgentAllowance,
    BudgetPolicy,
)
from agentic_mcp_server.domain.token_budget import estimate_tokens


def test_unknown_subject_gets_the_conservative_default() -> None:
    policy = BudgetPolicy()
    assert policy.allowance_for("anyone") == DEFAULT_AGENT_ALLOWANCE
    assert DEFAULT_AGENT_ALLOWANCE.max_requests == 1
    assert DEFAULT_AGENT_ALLOWANCE.max_tokens == 2500


def test_explicit_allowance_overrides_the_default() -> None:
    impl = AgentAllowance(max_requests=2, max_tokens=4000)
    policy = BudgetPolicy(allowances={"impl-agent": impl})
    assert policy.allowance_for("impl-agent") == impl
    assert policy.allowance_for("test-agent") == DEFAULT_AGENT_ALLOWANCE


def test_estimate_tokens_rounds_up_at_four_chars_per_token() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
