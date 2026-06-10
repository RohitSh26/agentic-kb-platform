"""Azure Wiki connector skeleton.

Source identity: SourceRef.external_id carries the page id; source_version
carries the page revision (never concatenated, so the
(source_uri, source_version) index semantics stay clean).
"""

from typing import ClassVar

from agentic_kb_builder.connectors.source_connector import BaseConnector
from agentic_kb_builder.domain import SourceType


class AzureWikiConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "azure_wiki"
