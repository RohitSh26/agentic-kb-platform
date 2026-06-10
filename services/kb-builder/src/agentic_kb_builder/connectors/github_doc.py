"""GitHub docs connector skeleton. source_version is the commit SHA."""

from typing import ClassVar

from agentic_kb_builder.connectors.source_connector import BaseConnector
from agentic_kb_builder.domain import SourceType


class GitHubDocConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "github_doc"
