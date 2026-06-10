"""Token budget primitives. Budgets are enforced by the Context Broker, never by prompts."""

from dataclasses import dataclass

__all__ = ["TokenBudget"]


@dataclass(frozen=True)
class TokenBudget:
    """A per-run or per-agent token allowance."""

    max_tokens: int
    used_tokens: int = 0

    @property
    def remaining_tokens(self) -> int:
        return max(self.max_tokens - self.used_tokens, 0)

    def can_spend(self, tokens: int) -> bool:
        return tokens <= self.remaining_tokens
