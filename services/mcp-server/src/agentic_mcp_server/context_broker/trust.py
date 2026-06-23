"""Trust-bucket vocabulary + read-time admission.

The broker enforces trust at read time. Producers assign a bucket at build
time; this module decides which buckets a traversal may surface given a
``trust_floor`` and the ``include_inferred`` gate. The rules (verbatim from the
contract) are:

- Ordering: ``REJECTED < AMBIGUOUS < INFERRED_LOW < INFERRED_HIGH < EXTRACTED``.
- ``trust_floor=X`` admits buckets ``>= X`` in that order.
- ``AMBIGUOUS`` and ``REJECTED`` are NEVER returned (excluded regardless of
  floor) — they cannot route or support a claim.
- ``INFERRED_*`` is admitted only when ``include_inferred=True``, and always
  labelled as a routing hint that cannot support a cited claim.
- An unknown / banned bucket is treated as ``AMBIGUOUS`` (excluded).

This module is the single place that knows the bucket order, so callers never
compare bucket strings directly.
"""

from typing import Literal

# Strictly increasing trust. Index 0 = least trusted.
_TRUST_ORDER: tuple[str, ...] = (
    "REJECTED",
    "AMBIGUOUS",
    "INFERRED_LOW",
    "INFERRED_HIGH",
    "EXTRACTED",
)
_RANK: dict[str, int] = {bucket: i for i, bucket in enumerate(_TRUST_ORDER)}

# Buckets that may never be returned by a traversal, whatever the floor.
_NEVER_RETURNED: frozenset[str] = frozenset({"REJECTED", "AMBIGUOUS"})

# Buckets gated behind include_inferred and labelled as routing hints only.
_INFERRED: frozenset[str] = frozenset({"INFERRED_LOW", "INFERRED_HIGH"})

DEFAULT_TRUST_FLOOR = "EXTRACTED"

#: Only EXTRACTED edges may support a cited claim. INFERRED_* are routing hints.
CLAIM_SUPPORTING = "EXTRACTED"

TrustFloor = Literal["EXTRACTED", "INFERRED_HIGH", "INFERRED_LOW"]


def _rank(trust_class: str) -> int:
    # Unknown / banned bucket ⇒ treated as AMBIGUOUS (excluded from default
    # traversal), per relation-ontology.md.
    return _RANK.get(trust_class, _RANK["AMBIGUOUS"])


def admits(trust_class: str, *, trust_floor: str, include_inferred: bool) -> bool:
    """Return True if an edge of ``trust_class`` may be surfaced.

    - ``AMBIGUOUS`` / ``REJECTED`` (and any unknown bucket, treated as
      ``AMBIGUOUS``) are NEVER admitted, whatever the floor or flag.
    - ``INFERRED_*`` are admitted only when ``include_inferred`` is set. The
      ``include_inferred`` flag is the gate that opens the inferred buckets as
      labelled routing hints; the default ``trust_floor=EXTRACTED`` does not
      suppress them once the flag is on. A higher ``trust_floor`` (e.g.
      ``INFERRED_HIGH``) still narrows which inferred tier qualifies.
    - ``EXTRACTED`` is always at the top of the ordering: admitted whenever the
      floor is at or below it (i.e. always), independent of ``include_inferred``.
    """
    normalized = trust_class if trust_class in _RANK else "AMBIGUOUS"
    if normalized in _NEVER_RETURNED:
        return False
    if normalized in _INFERRED:
        # Gate first; among inferred tiers, an inferred-level floor selects the
        # tier (INFERRED_HIGH floor excludes INFERRED_LOW).
        if not include_inferred:
            return False
        floor_for_inferred = trust_floor if trust_floor in _INFERRED else _TRUST_ORDER[0]
        return _rank(normalized) >= _rank(floor_for_inferred)
    # EXTRACTED.
    return _rank(normalized) >= _rank(trust_floor)


def is_claim_supporting(trust_class: str) -> bool:
    """Only EXTRACTED edges may support a cited claim; INFERRED_* cannot."""
    return trust_class == CLAIM_SUPPORTING
