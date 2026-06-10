"""Budget arithmetic: enforced by the broker, so it must be exact."""

from agentic_mcp_server.domain.token_budget import TokenBudget


def test_remaining_tokens() -> None:
    budget = TokenBudget(max_tokens=100, used_tokens=40)
    assert budget.remaining_tokens == 60


def test_can_spend_up_to_the_remainder() -> None:
    budget = TokenBudget(max_tokens=100, used_tokens=40)
    assert budget.can_spend(60)
    assert not budget.can_spend(61)


def test_overdrawn_budget_clamps_to_zero() -> None:
    budget = TokenBudget(max_tokens=10, used_tokens=25)
    assert budget.remaining_tokens == 0
    assert not budget.can_spend(1)
