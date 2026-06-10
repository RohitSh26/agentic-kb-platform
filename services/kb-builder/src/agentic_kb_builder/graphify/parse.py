"""Validate raw Graphify parser output into a FileGraph (fail loudly, never guess)."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agentic_kb_builder.domain import FileGraph
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


def parse_file_graph(raw: Mapping[str, Any]) -> FileGraph:
    try:
        return FileGraph.model_validate(raw)
    except ValidationError:
        logger.error("event=graphify_parse_failed path=%s", raw.get("path", "<missing>"))
        raise
