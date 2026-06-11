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

# subjects with a manifest allowance in the executor; a typo'd agent would
# otherwise fall back silently to the broker's smallest default allowance
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


ScriptStep = RequestMoreStep | OpenEvidenceStep


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
        for step in self.script:
            if isinstance(step, OpenEvidenceStep):
                referenced.add(step.evidence)
            else:
                referenced |= set(step.already_checked)
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


def load_cases(directory: Path) -> list[EvalCase]:
    cases = [load_case(path) for path in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
    return cases
