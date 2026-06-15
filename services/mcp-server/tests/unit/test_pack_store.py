"""PackStore eviction is least-recently-USED, not least-recently-created (#113)."""

import pytest

from agentic_mcp_server.context_broker.state import (
    EvidencePackState,
    PackStore,
    UnknownPackError,
)


def _pack(pack_id: str) -> EvidencePackState:
    return EvidencePackState(
        context_pack_id=pack_id,
        run_id="run",
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
