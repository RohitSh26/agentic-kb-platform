"""GitHub docs connector skeleton. source_version is the commit SHA."""

from typing import ClassVar

from contracts.artifact_schemas import SourceType
from kb_builder.connectors.base import BaseConnector


class GitHubDocConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "github_doc"
