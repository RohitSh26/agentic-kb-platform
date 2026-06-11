"""Security audit log: every context expansion and source access (PR-13).

Operator-facing structured stdout, distinct from the Postgres retrieval_event
ledger on purpose: audit lines carry data agents must never see — ACL-
suppressed artifact ids, requester team sets, injection detections. Lines
carry ids and metadata only, never body_text or other retrieved content.
"""

import logging
import re
import uuid
from collections.abc import Sequence

from agentic_mcp_server.auth.rbac import Requester

logger = logging.getLogger("agentic_mcp_server.audit")

# subject and team values come from IdP claims (external input): constrain
# the charset so claim values cannot forge key=value audit fields
_UNSAFE = re.compile(r"[^\w.@-]")


def _safe(value: str) -> str:
    return _UNSAFE.sub("_", value) or "-"


def _ids(artifact_ids: Sequence[uuid.UUID]) -> str:
    return ",".join(str(artifact_id) for artifact_id in artifact_ids) or "-"


def audit_context_access(
    *,
    tool: str,
    requester: Requester,
    kb_version: str,
    artifact_ids: Sequence[uuid.UUID],
    suppressed_artifact_ids: Sequence[uuid.UUID] = (),
    injection_flagged_ids: Sequence[uuid.UUID] = (),
) -> None:
    logger.info(
        "audit.context_access tool=%s subject=%s teams=%s kb_version=%s "
        "artifact_ids=%s suppressed_artifact_ids=%s injection_flagged_ids=%s",
        tool,
        _safe(requester.subject),
        ",".join(_safe(team) for team in sorted(requester.teams)) or "-",
        kb_version,
        _ids(artifact_ids),
        _ids(suppressed_artifact_ids),
        _ids(injection_flagged_ids),
    )
