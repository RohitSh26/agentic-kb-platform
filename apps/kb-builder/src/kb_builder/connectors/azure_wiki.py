"""Azure Wiki connector skeleton.

Source identity: SourceRef.external_id carries the page id; source_version
carries the page revision (never concatenated, so the
(source_uri, source_version) index semantics stay clean).
"""

from typing import ClassVar

from contracts.artifact_schemas import SourceType
from kb_builder.connectors.base import BaseConnector


class AzureWikiConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "azure_wiki"
