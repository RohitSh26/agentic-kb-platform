"""DraftStore semantics (in-memory adapter): first writer wins, get after put."""

from panel_test_support import make_pr

from review_panel.domain.draft import build_draft
from review_panel.domain.findings import ReviewFindingsV1
from review_panel.domain.reconcile import ReconciledReview
from review_panel.infrastructure.draft_store import InMemoryDraftStore


def _draft(model: str):
    return build_draft(
        pr=make_pr(),
        reconciled=ReconciledReview(lens_verdicts={"bug": "approve"}),
        synthesis=ReviewFindingsV1(verdict="approve"),
        model=model,
        kb_used=False,
    )


async def test_get_returns_none_until_a_draft_is_stored() -> None:
    store = InMemoryDraftStore()
    draft = _draft("fake:a")
    assert await store.get(draft.draft_key) is None
    stored = await store.put_if_absent(draft)
    assert stored == draft
    assert await store.get(draft.draft_key) == draft


async def test_put_if_absent_keeps_the_first_writer_and_returns_it() -> None:
    store = InMemoryDraftStore()
    first = _draft("fake:first")
    rival = _draft("fake:rival")  # same PR, same head_sha => same key
    assert first.draft_key == rival.draft_key

    assert await store.put_if_absent(first) == first
    winner = await store.put_if_absent(rival)

    assert winner == first  # the loser receives the stored row, not its own
    assert await store.get(first.draft_key) == first
