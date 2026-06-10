"""ADO card connector skeleton. source_version is the card revision.

Cards mutate, so the backend's fetch_text must return a deterministic
normalized rendering of the card fields at that revision (snapshot policy).
"""

from typing import ClassVar

from contracts.artifact_schemas import SourceType
from kb_builder.connectors.base import BaseConnector


class AdoCardConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "ado_card"
