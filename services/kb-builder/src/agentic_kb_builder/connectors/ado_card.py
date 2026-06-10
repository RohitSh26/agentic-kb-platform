"""ADO card connector skeleton. source_version is the card revision.

Cards mutate, so the backend's fetch_text must return a deterministic
normalized rendering of the card fields at that revision (snapshot policy).
"""

from typing import ClassVar

from agentic_kb_builder.connectors.source_connector import BaseConnector
from agentic_kb_builder.domain import SourceType


class AdoCardConnector(BaseConnector):
    source_type: ClassVar[SourceType] = "ado_card"
