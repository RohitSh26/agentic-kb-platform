"""Budget policy unit tests: allowance resolution is keyed by session subject."""

import pytest

from agentic_mcp_server.context_broker.budgets import (
    DEFAULT_AGENT_ALLOWANCE,
    AgentAllowance,
    BudgetPolicy,
    parse_agent_allowances,
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


def test_parse_agent_allowances_builds_the_subject_map() -> None:
    allowances = parse_agent_allowances(
        '{"impl-client-id": {"max_requests": 2, "max_tokens": 4000},'
        ' "auditor-client-id": {"max_requests": 1, "max_tokens": 1500}}'
    )
    assert allowances == {
        "impl-client-id": AgentAllowance(max_requests=2, max_tokens=4000),
        "auditor-client-id": AgentAllowance(max_requests=1, max_tokens=1500),
    }
    policy = BudgetPolicy(allowances=allowances)
    assert policy.allowance_for("impl-client-id").max_tokens == 4000
    assert policy.allowance_for("unlisted") == DEFAULT_AGENT_ALLOWANCE


def test_parse_agent_allowances_unset_or_blank_means_defaults_for_everyone() -> None:
    assert parse_agent_allowances(None) == {}
    assert parse_agent_allowances("") == {}
    assert parse_agent_allowances("   \n") == {}


def test_parse_agent_allowances_fails_fast_on_bad_config() -> None:
    """A typo in budget config must stop the boot, never silently default."""
    bad_values = [
        "{not json",
        '["impl"]',
        '{"impl": {"max_requests": 2}}',  # missing max_tokens
        '{"impl": {"max_requests": 2, "max_tokens": 4000, "extra": 1}}',
        '{"impl": {"max_requests": -1, "max_tokens": 4000}}',
        '{"impl": {"max_requests": "2", "max_tokens": 4000}}',
        '{"impl": {"max_requests": true, "max_tokens": 4000}}',
        '{"impl": {"max_requests": 2.5, "max_tokens": 4000}}',
        '{" ": {"max_requests": 2, "max_tokens": 4000}}',
        '{"impl ": {"max_requests": 2, "max_tokens": 4000}}',  # padded never matches a subject
        '{"impl": {"max_requests": 1, "max_tokens": 1000},'
        ' "impl": {"max_requests": 2, "max_tokens": 4000}}',  # duplicate must not last-win
    ]
    for raw in bad_values:
        with pytest.raises(RuntimeError, match="MCP_AGENT_ALLOWANCES"):
            parse_agent_allowances(raw)
