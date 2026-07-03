"""Confidence tiering for get_task_context (PR-39, proposal §3).

Pins the 2026-07-02 Graphify-audit rule in isolation: a `calls` edge may read
`deterministic` ONLY with independent structural corroboration (same defining
file, or an `imports` edge from the caller's file to the target's file). The
name-collision fixtures model the audit's exact failure shape — a free function
and a same-named method, where a syntactic match resolves to a single,
confidently-labelled, WRONG target — so none of them may surface without a
caveat. Alias body parsing is pinned fail-soft: garbage degrades to "no
targets" (search fallback), never a crash.
"""

import json
import uuid

from agentic_mcp_server.context_broker.task_context_nodes import (
    _alias_targets,
    _alias_tier,
    admits_floor,
    calls_edge_tier,
)
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow

CALLER_FILE = uuid.uuid4()  # caller.py — defines handle()
FREE_FN_FILE = uuid.uuid4()  # module_a.py — defines the free function resolve()
METHOD_FILE = uuid.uuid4()  # module_b.py — defines a same-named method resolve()

NO_IMPORTS: frozenset[tuple[uuid.UUID, uuid.UUID]] = frozenset()


# --------------------------------------------------------- calls-edge corroboration rule


def test_collision_without_import_corroboration_is_interpreted_with_caveat() -> None:
    """Collision fixture 1: handle() calls resolve(); module_a.py defines the free
    function, module_b.py a same-named method; the caller imports NEITHER."""
    tier, caveat = calls_edge_tier(
        caller_file=CALLER_FILE,
        target_file=FREE_FN_FILE,
        import_pairs=NO_IMPORTS,
        target_path="services/x/module_a.py",
    )
    assert tier == "interpreted"
    assert caveat is not None and "module_a.py" in caveat
    assert "same-named" in caveat


def test_collision_with_import_of_the_wrong_module_is_still_interpreted() -> None:
    """Collision fixture 2: the caller imports module_b.py (the same-named METHOD's
    module) — an import edge exists, but not to the labelled target's module, so
    the label is uncorroborated and must not read deterministic."""
    tier, caveat = calls_edge_tier(
        caller_file=CALLER_FILE,
        target_file=FREE_FN_FILE,
        import_pairs=frozenset({(CALLER_FILE, METHOD_FILE)}),
        target_path="services/x/module_a.py",
    )
    assert tier == "interpreted"
    assert caveat is not None and "module_a.py" in caveat


def test_collision_with_unresolvable_target_module_is_interpreted() -> None:
    """Collision fixture 3: the target has no `defined_in` edge at all — its
    module identity cannot be checked, so the edge is flagged, never trusted."""
    tier, caveat = calls_edge_tier(
        caller_file=CALLER_FILE,
        target_file=None,
        import_pairs=NO_IMPORTS,
        target_path=None,
    )
    assert tier == "interpreted"
    assert caveat is not None and "could not be resolved" in caveat


def test_unresolvable_caller_module_is_interpreted() -> None:
    # corroboration needs BOTH sides' modules: a missing caller `defined_in` is
    # the same "cannot check" shape as a missing target one
    tier, caveat = calls_edge_tier(
        caller_file=None,
        target_file=FREE_FN_FILE,
        import_pairs=NO_IMPORTS,
        target_path="services/x/module_a.py",
    )
    assert tier == "interpreted"
    assert caveat is not None


def test_import_corroborated_call_is_deterministic_without_caveat() -> None:
    tier, caveat = calls_edge_tier(
        caller_file=CALLER_FILE,
        target_file=FREE_FN_FILE,
        import_pairs=frozenset({(CALLER_FILE, FREE_FN_FILE)}),
        target_path="services/x/module_a.py",
    )
    assert tier == "deterministic"
    assert caveat is None


def test_same_file_call_is_deterministic_without_caveat() -> None:
    tier, caveat = calls_edge_tier(
        caller_file=CALLER_FILE,
        target_file=CALLER_FILE,
        import_pairs=NO_IMPORTS,
        target_path="services/x/caller.py",
    )
    assert tier == "deterministic"
    assert caveat is None


# ----------------------------------------------------------------- confidence_floor rule


def test_floor_admits_same_or_stronger_tiers_only() -> None:
    assert admits_floor("interpreted", "interpreted")
    assert admits_floor("deterministic", "interpreted")
    assert admits_floor("ground_truth", "interpreted")
    assert not admits_floor("interpreted", "deterministic")
    assert admits_floor("deterministic", "deterministic")
    assert not admits_floor("deterministic", "ground_truth")
    assert admits_floor("ground_truth", "ground_truth")


# ------------------------------------------------------------- alias body parsing (PR-38)


def _alias_row(body_text: str | None) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=uuid.uuid4(),
        artifact_type="alias_reference",
        title="payment validation",
        body_text=body_text,
        knowledge_kind="interpreted",
        authority_score=0.5,
        source_uri="github://org/repo/x",
    )


def test_alias_targets_reads_the_pr38_ranked_targets_shape() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    body = json.dumps(
        {
            "schema": "alias_reference_v1",
            "alias": "payment validation",
            "confidence_tier": "interpreted",
            "targets": [
                {"path": "a.py", "artifact_id": str(first), "count": 2},
                {"path": "b.py", "artifact_id": str(second), "count": 1},
                {"path": "unresolved.py", "artifact_id": None, "count": 1},
            ],
        }
    )
    # rank order preserved; the PR-38 "unresolved path" (artifact_id null) is skipped
    assert _alias_targets(_alias_row(body)) == [first, second]


def test_alias_targets_reads_the_proposal_target_entity_ids_shape() -> None:
    target = uuid.uuid4()
    body = json.dumps({"target_entity_ids": [str(target), str(target)]})
    assert _alias_targets(_alias_row(body)) == [target]  # deduped


def test_alias_targets_degrades_to_empty_on_garbage_never_raises() -> None:
    for body in (None, "", "not json {", '"a bare string"', "[1, 2]", '{"targets": "x"}'):
        assert _alias_targets(_alias_row(body)) == []


def test_alias_tier_honors_a_known_tier_and_defaults_to_interpreted() -> None:
    assert _alias_tier(_alias_row(json.dumps({"confidence_tier": "deterministic"})))\
        == "deterministic"
    assert _alias_tier(_alias_row(json.dumps({"confidence_tier": "certain"}))) == "interpreted"
    assert _alias_tier(_alias_row("not json")) == "interpreted"
