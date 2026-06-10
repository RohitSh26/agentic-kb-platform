"""GitHub code connector skeleton. source_version is the commit SHA.

Code is normalized conservatively (line endings only): evidence must remain an
exact snippet at a source version, and file_content_hash feeds the graphify
cache key.
"""

from typing import ClassVar

from common.hashing import normalize_code
from contracts.artifact_schemas import SourceType
from kb_builder.connectors.base import BaseConnector


class GitHubCodeConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "github_code"

    def _normalize(self, raw: str) -> str:
        return normalize_code(raw)
