"""Eval case models + YAML loader.

Each case seeds its own registry fixtures, scripts broker calls, and lists the
expected docs/files/symbols/tests/open questions (docs/pr-briefs/PR-12). Search
seeds are validated at load time: the FakeSearchClient matches whole normalized
tokens, so a keyword that never appears in the task or a scripted question
would silently return nothing.
"""

from pathlib import Path
from typing import Literal, Self

import yaml
from agentic_mcp_server.domain.query_text import normalize_query
from pydantic import BaseModel, ConfigDict, Field, model_validator

BENCHMARK_TASK_TYPES = (
    "plan_new_endpoint",
    "find_auth_validation_rules",
    "identify_embeddings_change_impact",
    "find_tests_for_similar_endpoint",
    "find_release_monitoring_guidance",
    "detect_conflicting_evidence",
)

TaskType = Literal[
    "plan_new_endpoint",
    "find_auth_validation_rules",
    "identify_embeddings_change_impact",
    "find_tests_for_similar_endpoint",
    "find_release_monitoring_guidance",
    "detect_conflicting_evidence",
    "retrieval_recall",
]

# scripted unsupported claims cite handles with this prefix; they resolve to
# nothing and must fail validate_evidence_references
UNKNOWN_EVIDENCE_PREFIX = "unknown:"

# subjects with a manifest allowance in the executor. The Literal guards scripted
# names against typos: an unknown subject falls back to DEFAULT_AGENT_ALLOWANCE
# (1/2500), which is LARGER than the delivery/pr-planner allowances (1/1500) — so a
# silent typo would widen the budget, not shrink it. Keep this list in sync with the
# executor's allowance map.
AgentName = Literal[
    "impl-agent",
    "test-agent",
    "review-agent",
    "delivery-agent",
    "pr-planner-agent",
]


class CaseModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FixtureArtifact(CaseModel):
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body_text: str = Field(min_length=1)
    knowledge_kind: Literal["source_backed", "interpreted"] = "source_backed"
    authority_score: float = Field(default=0.8, ge=0.0, le=1.0)
    artifact_type: str = "doc_chunk"
    # team_acl_v1: empty ⇒ org-public; a non-empty list restricts the artifact to
    # requesters sharing one of these teams (rbac.py). A team-less requester is
    # filtered out of a restricted artifact — the F7 must_not_leak negative.
    acl_teams: list[str] = Field(default_factory=list)


class SearchSeed(CaseModel):
    keyword: str = Field(min_length=1)
    hits: list[str] = Field(min_length=1)  # fixture keys, best match first


class Fixtures(CaseModel):
    artifacts: list[FixtureArtifact] = Field(min_length=1)
    search_seeds: list[SearchSeed] = Field(min_length=1)


class RequestMoreStep(CaseModel):
    tool: Literal["context.request_more"]
    agent: AgentName
    question: str = Field(min_length=1)
    why_needed: str = Field(min_length=1)
    decision_needed: str = Field(min_length=1)
    already_checked: list[str] = Field(default_factory=list)  # fixture keys
    max_tokens: int = Field(default=1500, ge=1)


class OpenEvidenceStep(CaseModel):
    tool: Literal["context.open_evidence"]
    agent: AgentName
    evidence: str = Field(min_length=1)  # fixture key
    max_tokens: int = Field(default=800, ge=1)


# verifier levels available to a scripted verify_answer step (the schema defaults
# to ["L0"]; cases stay on the mandatory L0 floor unless they request more).
VerifierLevel = Literal["L0", "L1", "L2", "L3"]


class VerifyAnswerStep(CaseModel):
    """Script a context.verify_answer call: build a single-claim answer that cites
    the listed fixture keys, run the broker verifier, and assert its overall verdict.

    The expected verdict makes a verification path a first-class, asserted trust
    case — a passing receipt (L0 satisfied) or a contractual `failed`/`partial`
    (e.g. evidence the requester never retrieved, the F1 L0_in_requester_ledger
    negative). The step records its result into the case for the executor to assert.
    """

    tool: Literal["context.verify_answer"]
    agent: AgentName
    answer_id: str = Field(min_length=1, max_length=256)
    claim: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)  # fixture keys the claim cites
    # fixture keys to mark as retrieved by this agent BEFORE verifying, so L0's
    # in_requester_ledger check can pass; omit a key here to script the F1 negative.
    retrieved: list[str] = Field(default_factory=list)
    verifier_levels: list[VerifierLevel] = Field(default_factory=lambda: ["L0"])
    expect_overall: Literal["passed", "failed", "partial"]


class PlatformTrustStep(CaseModel):
    """Script the context.platform_trust gate (ADR-0011 §6). A verification_required
    client is platform-trusted ONLY with a valid, client-matched, passing receipt;
    without one the gate returns a STRUCTURED `denied` (never a silent pass). This
    step drives evaluate_platform_trust directly and asserts the status — the F2
    trusted vs denied(no-receipt) pair."""

    tool: Literal["context.platform_trust"]
    agent: AgentName
    verification_required: bool
    # when true, first run verify_answer over `evidence` (signed receipt) and feed
    # it to the gate; when false, present NO receipt (the denied negative).
    present_receipt: bool = True
    answer_id: str = Field(default="platform-trust-answer", min_length=1, max_length=256)
    claim: str = Field(default="the gate is asserted", min_length=1)
    evidence: list[str] = Field(default_factory=list)  # fixture keys for the receipt
    retrieved: list[str] = Field(default_factory=list)
    expect_status: Literal["trusted", "denied", "not_required"]


ScriptStep = RequestMoreStep | OpenEvidenceStep | VerifyAnswerStep | PlatformTrustStep


class ScriptedClaim(CaseModel):
    claim: str = Field(min_length=1)
    # fixture keys, or "unknown:<x>" to script an unsupported claim
    evidence: list[str] = Field(min_length=1)


class ScriptedOutput(CaseModel):
    claims: list[ScriptedClaim] = Field(default_factory=list[ScriptedClaim])
    open_questions: list[str] = Field(default_factory=list)


class Expected(CaseModel):
    docs: list[str] = Field(min_length=1)  # fixture keys; recall must be 1.0
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class EvalCase(CaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    task_type: TaskType
    task: str = Field(min_length=1)
    approved_context_plan: str = Field(min_length=1)
    budget_tokens: int = Field(default=8000, ge=1)
    # the orchestrator/requester's team memberships (team_acl_v1). Empty ⇒ a
    # team-less requester, which can only see org-public artifacts — the F7
    # must_not_leak setup. Threaded into every broker call the executor makes.
    requester_teams: list[str] = Field(default_factory=list)
    # fixture keys that MUST NOT appear in broker output for this case (e.g. a
    # team-restricted artifact a team-less requester asks for). The executor
    # asserts these were filtered out; a leak fails the case.
    must_not_leak: list[str] = Field(default_factory=list)
    fixtures: Fixtures
    script: list[ScriptStep] = Field(default_factory=list[ScriptStep])
    agent_output: ScriptedOutput | None = None
    expected: Expected

    @model_validator(mode="after")
    def _referenced_keys_exist(self) -> Self:
        keys = {artifact.key for artifact in self.fixtures.artifacts}
        if len(keys) != len(self.fixtures.artifacts):
            raise ValueError(f"{self.id}: duplicate fixture artifact keys")
        unknown = self._referenced_fixture_keys() - keys
        if unknown:
            raise ValueError(f"{self.id}: unknown fixture keys referenced: {sorted(unknown)}")
        return self

    @model_validator(mode="after")
    def _search_seeds_are_reachable(self) -> Self:
        queries = [f"{self.task} {self.approved_context_plan}"]
        queries += [step.question for step in self.script if isinstance(step, RequestMoreStep)]
        tokens = {token for query in queries for token in normalize_query(query).split()}
        # verify_answer / platform_trust steps don't search — they cite already-seeded
        # fixtures — so they add no reachable query tokens (handled below).
        dead = [
            seed.keyword
            for seed in self.fixtures.search_seeds
            if normalize_query(seed.keyword) not in tokens
        ]
        if dead:
            raise ValueError(
                f"{self.id}: search seed keywords never queried (FakeSearchClient matches "
                f"whole normalized tokens): {dead}"
            )
        return self

    def _referenced_fixture_keys(self) -> set[str]:
        referenced = {hit for seed in self.fixtures.search_seeds for hit in seed.hits}
        referenced |= set(self.expected.docs)
        referenced |= set(self.must_not_leak)
        for step in self.script:
            if isinstance(step, OpenEvidenceStep):
                referenced.add(step.evidence)
            elif isinstance(step, RequestMoreStep):
                referenced |= set(step.already_checked)
            else:
                # verify_answer / platform_trust steps cite + pre-retrieve fixtures
                referenced |= set(step.evidence) | set(step.retrieved)
        if self.agent_output is not None:
            for claim in self.agent_output.claims:
                referenced |= {
                    handle
                    for handle in claim.evidence
                    if not handle.startswith(UNKNOWN_EVIDENCE_PREFIX)
                }
        return referenced


def load_case(path: Path) -> EvalCase:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return EvalCase.model_validate(raw)


# alias_*.yaml (e.g. retrieval_cases/alias_golden_v1.yaml, PR-38) is a DIFFERENT
# suite schema (harness.alias.AliasCase — a resolver golden set, not a broker
# EvalCase) that happens to live alongside the broker retrieval cases; it is
# scored by scripts/eval_alias_resolution.py, not evals/run.py, so it is
# deliberately excluded from this glob.
_EXCLUDED_GLOBS = ("alias_*.yaml",)


def load_cases(directory: Path) -> list[EvalCase]:
    paths = [
        path
        for path in sorted(directory.glob("*.yaml"))
        if not any(path.match(pattern) for pattern in _EXCLUDED_GLOBS)
    ]
    cases = [load_case(path) for path in paths]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
    return cases
