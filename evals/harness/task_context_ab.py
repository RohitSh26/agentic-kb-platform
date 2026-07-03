"""get_task_context A/B suite: case schema, loader, and registry seeder (PR-39).

`agent_task_cases/task_context_ab_v1.yaml` is a distinct suite from the broker
EvalCase files (excluded from `harness.cases.load_cases`, the alias_* precedent):
each case is one realistic dev task with hand-written expected files and a small
hermetic fixture KB. The hermetic consumer (tests/test_task_context_ab.py) seeds
the fixtures and asserts the real tool's one-call output covers the expected
file set; the live consumer (scripts/eval_task_context.py) reuses only
task/hints/expected_files against the active local KB.
"""

import json
import uuid
from pathlib import Path
from typing import Self, cast

import yaml
from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from harness.cases import CaseModel
from harness.fixtures import KB_VERSION

SUITE = "task_context_ab_v1"

# derived source_uri prefix: readable_path(github://<owner>/<repo>/<path>) == <path>,
# so a fixture's `path` is exactly what the tool reports and coverage compares
_SOURCE_URI_PREFIX = "github://local/agentic-kb-platform/"

_EDGE_TYPES = ("calls", "imports", "tests", "defined_in")


class AbArtifact(CaseModel):
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body_text: str = Field(min_length=1)
    artifact_type: str = "code_file"
    # repo-relative source path; the seeded source_uri (and therefore the path the
    # tool reports) is derived from it
    path: str = Field(min_length=1)
    # alias_reference rows only: fixture keys this alias resolves to. The seeder
    # replaces body_text with a generated alias_reference_v1 payload
    # (docs/contracts/alias-reference.md) carrying the minted target ids.
    alias_targets: list[str] = Field(default_factory=list)


class AbSearchSeed(CaseModel):
    keyword: str = Field(min_length=1)
    hits: list[str] = Field(min_length=1)  # fixture keys, best match first


class AbEdge(CaseModel):
    from_key: str = Field(alias="from", min_length=1)
    to_key: str = Field(alias="to", min_length=1)
    edge_type: str = Field(min_length=1)

    @model_validator(mode="after")
    def _known_edge_type(self) -> Self:
        if self.edge_type not in _EDGE_TYPES:
            raise ValueError(f"unknown edge_type {self.edge_type!r} (expected {_EDGE_TYPES})")
        return self


class AbHints(CaseModel):
    file_paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)


class AbFixtures(CaseModel):
    artifacts: list[AbArtifact] = Field(min_length=1)
    search_seeds: list[AbSearchSeed] = Field(min_length=1)
    edges: list[AbEdge] = Field(default_factory=list[AbEdge])


class TaskContextAbCase(CaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    task: str = Field(min_length=1)
    hints: AbHints | None = None
    # hand-written expert references (written before any run); the hermetic
    # coverage gate and the live arms both score against this set
    expected_files: list[str] = Field(min_length=1)
    fixtures: AbFixtures

    @model_validator(mode="after")
    def _referenced_keys_exist(self) -> Self:
        keys = {artifact.key for artifact in self.fixtures.artifacts}
        if len(keys) != len(self.fixtures.artifacts):
            raise ValueError(f"{self.id}: duplicate fixture artifact keys")
        referenced = {hit for seed in self.fixtures.search_seeds for hit in seed.hits}
        referenced |= {key for a in self.fixtures.artifacts for key in a.alias_targets}
        referenced |= {edge.from_key for edge in self.fixtures.edges}
        referenced |= {edge.to_key for edge in self.fixtures.edges}
        unknown = referenced - keys
        if unknown:
            raise ValueError(f"{self.id}: unknown fixture keys referenced: {sorted(unknown)}")
        return self

    @model_validator(mode="after")
    def _expected_files_are_seedable(self) -> Self:
        # hermetic coverage is only provable if every expected file exists as a
        # fixture artifact path (the live arms use the real KB instead)
        fixture_paths = {artifact.path for artifact in self.fixtures.artifacts}
        missing = set(self.expected_files) - fixture_paths
        if missing:
            raise ValueError(f"{self.id}: expected_files not seeded as fixtures: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def _search_seeds_are_reachable(self) -> Self:
        # FakeSearchClient matches whole normalized single tokens; a seed keyword
        # the tool never queries (task text, hint symbol, or hint filename) is dead
        tokens = set(normalize_query(self.task).split())
        if self.hints is not None:
            for symbol in self.hints.symbols:
                tokens |= set(normalize_query(symbol).split())
            for path in self.hints.file_paths:
                tokens |= set(normalize_query(path.rsplit("/", 1)[-1]).split())
        dead = [
            seed.keyword
            for seed in self.fixtures.search_seeds
            if normalize_query(seed.keyword) not in tokens
        ]
        if dead:
            raise ValueError(f"{self.id}: search seed keywords never queried: {dead}")
        return self


def load_ab_cases(path: Path) -> list[TaskContextAbCase]:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping with suite: {SUITE}")
    document = cast(dict[str, object], raw)
    if document.get("suite") != SUITE:
        raise ValueError(f"{path}: expected a YAML mapping with suite: {SUITE}")
    entries = document.get("cases", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: `cases` must be a list")
    cases = [TaskContextAbCase.model_validate(entry) for entry in cast(list[object], entries)]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
    return cases


def _alias_body(alias: AbArtifact, targets: list[tuple[str, uuid.UUID]]) -> str:
    """An alias_reference_v1 payload (docs/contracts/alias-reference.md) with real ids."""
    return json.dumps(
        {
            "schema": "alias_reference_v1",
            "alias": alias.title,
            "variants": [],
            "confidence_tier": "interpreted",
            "confirmation_count": len(targets),
            "targets": [
                {"path": path, "artifact_id": str(artifact_id), "count": 1}
                for path, artifact_id in targets
            ],
            "evidence": [],
        }
    )


async def seed_ab_case(
    session: AsyncSession, case: TaskContextAbCase, search: FakeSearchClient
) -> dict[str, uuid.UUID]:
    """Seed one case's fixture KB (active build run, artifacts, alias bodies, edges)
    and the fake search. Returns fixture key -> artifact_id."""
    await session.execute(
        text(
            "INSERT INTO kb_build_run (kb_version, build_seq, status)"
            " VALUES (:kb_version, 1, 'active')"
        ),
        {"kb_version": KB_VERSION},
    )
    ids: dict[str, uuid.UUID] = {a.key: uuid.uuid4() for a in case.fixtures.artifacts}
    by_key = {a.key: a for a in case.fixtures.artifacts}
    for artifact in case.fixtures.artifacts:
        artifact_id = ids[artifact.key]
        source_id = uuid.uuid4()
        body_text = (
            _alias_body(
                artifact, [(by_key[k].path, ids[k]) for k in artifact.alias_targets]
            )
            if artifact.alias_targets
            else artifact.body_text
        )
        await session.execute(
            text(
                "INSERT INTO source_item (source_id, source_type, source_uri, source_version,"
                " path, content_hash) VALUES (CAST(:source_id AS uuid), 'github_code',"
                " :source_uri, 'rev-1', :path, :content_hash)"
            ),
            {
                "source_id": str(source_id),
                "source_uri": f"{_SOURCE_URI_PREFIX}{artifact.path}",
                "path": artifact.path,
                "content_hash": f"hash-{artifact_id}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_artifact (artifact_id, artifact_type, source_id, title,"
                " body_text, kb_version, knowledge_kind, authority_score, acl_teams) VALUES"
                " (CAST(:artifact_id AS uuid), :artifact_type, CAST(:source_id AS uuid),"
                " :title, :body_text, :kb_version, :knowledge_kind, 0.8,"
                " CAST(:acl_teams AS text[]))"
            ),
            {
                "artifact_id": str(artifact_id),
                "artifact_type": artifact.artifact_type,
                "source_id": str(source_id),
                "title": artifact.title,
                "body_text": body_text,
                "kb_version": KB_VERSION,
                "knowledge_kind": (
                    "interpreted" if artifact.alias_targets else "source_backed"
                ),
                "acl_teams": [],
            },
        )
    for edge in case.fixtures.edges:
        await session.execute(
            text(
                "INSERT INTO knowledge_edge (from_artifact_id, to_artifact_id, edge_type,"
                " confidence, source, kb_version, trust_class) VALUES"
                " (CAST(:from_id AS uuid), CAST(:to_id AS uuid), :edge_type, 1.0,"
                " 'ab-fixture', :kb_version, 'EXTRACTED')"
            ),
            {
                "from_id": str(ids[edge.from_key]),
                "to_id": str(ids[edge.to_key]),
                "edge_type": edge.edge_type,
                "kb_version": KB_VERSION,
            },
        )
    await session.commit()

    for seed in case.fixtures.search_seeds:
        search.seed(
            seed.keyword,
            [
                SearchHit(artifact_id=ids[key], score=float(len(seed.hits) - position))
                for position, key in enumerate(seed.hits)
            ],
        )
    return ids
