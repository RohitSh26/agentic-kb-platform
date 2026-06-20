"""estimate_tokens: the deterministic ~4-chars-per-token arithmetic budgets charge against."""

from agentic_mcp_server.domain.token_budget import CHARS_PER_TOKEN, estimate_tokens


def test_estimate_tokens_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * CHARS_PER_TOKEN) == 1
    assert estimate_tokens("a" * (CHARS_PER_TOKEN + 1)) == 2


def test_estimate_tokens_exact_value() -> None:
    # 25 chars / 4 chars-per-token, rounded up -> 7 (a real oracle, not x == x)
    assert estimate_tokens("the quick brown fox jumps") == 7
