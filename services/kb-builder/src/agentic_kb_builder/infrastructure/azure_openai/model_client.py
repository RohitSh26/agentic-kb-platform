"""ModelClient interface: the only door to Azure OpenAI for wikify generation.

Builders depend on this Protocol, never on the SDK, so tests stay hermetic and
the model backend stays swappable (rule: python.md).
"""

from collections.abc import Sequence
from typing import Protocol

from agentic_kb_builder.domain import Chunk, WikifyGeneration


class ModelClient(Protocol):
    model_name: str
    model_params_hash: str

    async def generate_wikify(
        self, *, chunks: Sequence[Chunk], prompt_version: str
    ) -> WikifyGeneration:
        """Produce a summary, concepts, and source-backed facts for one source."""
        ...
