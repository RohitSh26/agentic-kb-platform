"""Shared constants for the Context Broker tools.

Single home for values that were independently re-declared across the tool modules
(graph/verify/expand) and call sites (pack/change_context), so they cannot drift.
"""

#: run_id placeholder for a retrieval_event written OUTSIDE a pack run (e.g. a standalone
#: graph/verify/expand call). The ledger column is non-null; this marks "no owning run".
NO_RUN_SENTINEL = "-"

#: User-facing error when no KB build has been activated yet. Raised as a ToolError by the
#: tools and surfaced as a note by change_context; one wording everywhere.
MSG_NO_ACTIVE_VERSION = "no active kb_version; the knowledge base has not been built yet"
