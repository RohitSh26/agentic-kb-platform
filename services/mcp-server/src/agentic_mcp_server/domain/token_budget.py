"""Token estimation. Budgets themselves are enforced by the Context Broker
(context_broker/state.py + budgets.py), never by prompts; this module only
provides the deterministic char->token estimate those paths charge against."""

__all__ = ["CHARS_PER_TOKEN", "estimate_tokens"]

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Deterministic ~4-chars-per-token estimate; budgets need consistency, not exactness."""
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
