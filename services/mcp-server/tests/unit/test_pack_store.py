"""PackStore eviction is least-recently-USED, not least-recently-created (#113)."""

import pytest

from agentic_mcp_server.context_broker.state import (
    EvidencePackState,
    PackStore,
    UnknownPackError,
)


def _pack(pack_id: str, run_id: str = "run") -> EvidencePackState:
    return EvidencePackState(
        context_pack_id=pack_id,
        run_id=run_id,
        kb_version="kb",
        build_seq=1,
        retrieval_profile="default",
        summary="s",
        budget_tokens=1000,
    )


def test_eviction_drops_least_recently_used_not_least_recently_created() -> None:
    store = PackStore(max_packs=2)
    store.create(_pack("a"))
    store.create(_pack("b"))
    # Read "a": it becomes most-recently-used, so "b" is now the eviction target.
    assert store.get("a").context_pack_id == "a"

    store.create(_pack("c"))  # over cap ⇒ evict the LRU, which is "b" not "a"

    assert store.get("a").context_pack_id == "a"
    assert store.get("c").context_pack_id == "c"
    with pytest.raises(UnknownPackError):
        store.get("b")


def test_create_without_reads_evicts_oldest() -> None:
    store = PackStore(max_packs=2)
    store.create(_pack("a"))
    store.create(_pack("b"))
    store.create(_pack("c"))  # no reads ⇒ oldest-created "a" is evicted

    with pytest.raises(UnknownPackError):
        store.get("a")
    assert store.get("b").context_pack_id == "b"
    assert store.get("c").context_pack_id == "c"


def test_run_usage_is_pruned_when_a_runs_last_pack_is_evicted() -> None:
    # run_usage must stay bounded: when a run's LAST live pack is evicted, drop its
    # shared usage meter so a long-lived instance does not leak one entry per run.
    store = PackStore(max_packs=2)
    store.create(_pack("a", run_id="run-1"))
    store.usage_for_run("run-1")  # materialize the per-run meter (as create_pack does)
    store.create(_pack("b", run_id="run-2"))
    store.usage_for_run("run-2")
    assert set(store.run_usage) == {"run-1", "run-2"}

    store.create(_pack("c", run_id="run-3"))  # evicts "a"; run-1 has no live pack left
    assert "run-1" not in store.run_usage
    assert "run-2" in store.run_usage  # still has a live pack ("b")


def test_run_usage_survives_while_the_run_has_a_live_pack() -> None:
    # Re-packing within a live run must NOT reset its meter (the create_pack ceiling
    # bypass stays closed): both packs share run-1, so eviction of one keeps the meter.
    store = PackStore(max_packs=2)
    store.create(_pack("a", run_id="run-1"))
    store.usage_for_run("run-1")
    store.create(_pack("b", run_id="run-1"))
    store.create(_pack("c", run_id="run-2"))  # evicts "a", but "b" keeps run-1 alive

    assert "run-1" in store.run_usage
