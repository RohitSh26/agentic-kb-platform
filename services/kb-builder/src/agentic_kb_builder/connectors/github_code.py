"""GitHub code connector skeleton. source_version is the commit SHA.

Code is normalized conservatively (line endings only): evidence must remain an
exact snippet at a source version, and file_content_hash feeds the graphify
cache key.
"""

from typing import ClassVar

from agentic_kb_builder.connectors.source_connector import BaseConnector
from agentic_kb_builder.domain import SourceType
from agentic_kb_builder.domain.content_hasher import normalize_code


class GitHubCodeConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "github_code"

    def _normalize(self, raw: str) -> str:
        return normalize_code(raw)
