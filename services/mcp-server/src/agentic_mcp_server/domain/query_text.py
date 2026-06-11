"""Query text normalization shared by dedupe and the search backends."""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_query(query: str) -> str:
    return _NON_ALNUM.sub(" ", query.lower()).strip()
