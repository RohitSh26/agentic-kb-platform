"""Harness agent allowances stay within the documented token-budget ranges (EV-7).

The executor pins per-subject allowances that case scripts rely on; .claude/rules/
token-budgets.md is the source. This catches drift between the two without a database.
"""

from harness.executor import AGENT_ALLOWANCES

# (max_requests, min_tokens, max_tokens) per .claude/rules/token-budgets.md
RULE_RANGES = {
    "impl-agent": (2, 3000, 4000),
    "test-agent": (1, 1500, 2500),
    "review-agent": (1, 1500, 2500),
    "delivery-agent": (1, 1000, 1500),
    "pr-planner-agent": (1, 1000, 1500),
}


def test_harness_allowances_match_the_documented_ranges() -> None:
    assert set(AGENT_ALLOWANCES) == set(RULE_RANGES)
    for subject, (requests, low, high) in RULE_RANGES.items():
        allowance = AGENT_ALLOWANCES[subject]
        assert allowance.max_requests == requests, subject
        assert low <= allowance.max_tokens <= high, subject
