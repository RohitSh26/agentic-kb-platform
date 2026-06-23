"""Per-agent allowances. Budgets are enforced here, server-side — never by prompts.

Allowances are keyed by the authenticated session subject (never by the
agent_name request field — that is what makes budgets unspoofable). The default
is the most conservative role allowance from
generous roles (e.g. implementation: 2 requests / 4k tokens) get an explicit
entry in the allowance map, supplied per deployment via MCP_AGENT_ALLOWANCES
(parsed by parse_agent_allowances below).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentAllowance:
    """Follow-up retrieval allowance for one agent within a run."""

    max_requests: int
    max_tokens: int


# fallback for a subject absent from the allowance map. NOT the smallest configured
# allowance — delivery/pr-planner deployments use 1/1500; deployments grant generous
# roles a larger allowance via MCP_AGENT_ALLOWANCES.
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


_ALLOWANCE_KEYS = {"max_requests", "max_tokens"}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"MCP_AGENT_ALLOWANCES: duplicate key {key!r}")
        result[key] = value
    return result


def parse_agent_allowances(raw: str | None) -> dict[str, AgentAllowance]:
    """Parse the MCP_AGENT_ALLOWANCES deployment value.

    Fail-fast by design: a typo in budget config must stop the boot, not
    silently hand every agent the conservative default. Unset / empty /
    whitespace ⇒ empty map (explicitly meaning "defaults for everyone").
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"MCP_AGENT_ALLOWANCES is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("MCP_AGENT_ALLOWANCES must be a JSON object of subject -> allowance")
    allowances: dict[str, AgentAllowance] = {}
    for subject, entry in parsed.items():
        # padded keys would silently never match a session subject
        if not subject.strip() or subject != subject.strip():
            raise RuntimeError(f"MCP_AGENT_ALLOWANCES: empty or padded subject key {subject!r}")
        if not isinstance(entry, dict) or set(entry) != _ALLOWANCE_KEYS:
            raise RuntimeError(
                f"MCP_AGENT_ALLOWANCES[{subject!r}] must be an object with exactly "
                f"max_requests and max_tokens"
            )
        for key in _ALLOWANCE_KEYS:
            value = entry[key]
            # bool is an int subclass; true/false must not pass as counts
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(
                    f"MCP_AGENT_ALLOWANCES[{subject!r}].{key} must be a non-negative integer"
                )
        allowances[subject] = AgentAllowance(
            max_requests=entry["max_requests"], max_tokens=entry["max_tokens"]
        )
    return allowances
