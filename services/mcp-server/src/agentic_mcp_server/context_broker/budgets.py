"""Per-agent allowances. Budgets are enforced here, server-side — never by prompts.

Allowances are keyed by the authenticated session subject (never by the
agent_name request field). The default is the most conservative role allowance
from .claude/rules/token-budgets.md; generous roles (e.g. implementation:
2 requests / 4k tokens) get an explicit entry in the allowance map. Role
manifests (PR-11) will supply that map per deployment.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentAllowance:
    """Follow-up retrieval allowance for one agent within a run."""

    max_requests: int
    max_tokens: int


# conservative default: the smallest role allowance (delivery planner)
DEFAULT_AGENT_ALLOWANCE = AgentAllowance(max_requests=1, max_tokens=2500)


@dataclass
class AgentUsage:
    requests: int = 0
    tokens: int = 0


@dataclass(frozen=True)
class BudgetPolicy:
    allowances: Mapping[str, AgentAllowance] = field(default_factory=dict)
    default_allowance: AgentAllowance = DEFAULT_AGENT_ALLOWANCE

    def allowance_for(self, subject: str) -> AgentAllowance:
        return self.allowances.get(subject, self.default_allowance)
