"""EntailmentClient seam for the L3 verifier.

The broker depends only on the ``EntailmentClient`` Protocol, never an SDK — so
the model backend (local Ollama by default, Azure OpenAI / Groq / OpenAI by env)
stays swappable and tests stay hermetic with ``FakeEntailmentClient``. Services
never share code: this is mcp-server's own copy of the Ollama-first chat pattern
that kb-builder uses for extraction and judging (duplicate small clients).
"""

from agentic_mcp_server.infrastructure.entailment.client import (
    EntailmentClient,
    EntailmentVerdict,
)
from agentic_mcp_server.infrastructure.entailment.fake import FakeEntailmentClient

__all__ = [
    "EntailmentClient",
    "EntailmentVerdict",
    "FakeEntailmentClient",
]
