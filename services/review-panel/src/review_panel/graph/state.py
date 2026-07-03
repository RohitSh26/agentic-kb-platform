"""LangGraph state for one draft run. Checkpointed per thread <repo>#<pr>@<head_sha>."""

import operator
from typing import Annotated, Required, TypedDict

from review_panel.domain.draft import ReviewDraft
from review_panel.domain.findings import PanelistReview, ReviewFindingsV1
from review_panel.domain.pr import PRContext
from review_panel.domain.reconcile import ReconciledReview


class PanelState(TypedDict, total=False):
    # input (the only key present at invoke time; fetched read-only by the caller)
    pr: Required[PRContext]
    # load_pr
    kb_context: str
    # fan-out: each reviewer node appends exactly one entry (reducer joins branches)
    panelist_reviews: Annotated[list[PanelistReview], operator.add]
    # reconcile
    reconciled: ReconciledReview
    synthesis: ReviewFindingsV1
    # store_draft returns the STORED draft (the winner if a racing run stored first)
    draft: ReviewDraft


class PanelUpdate(TypedDict, total=False):
    """A node's partial state update (nodes never return the whole state)."""

    pr: PRContext
    kb_context: str
    panelist_reviews: list[PanelistReview]
    reconciled: ReconciledReview
    synthesis: ReviewFindingsV1
    draft: ReviewDraft
