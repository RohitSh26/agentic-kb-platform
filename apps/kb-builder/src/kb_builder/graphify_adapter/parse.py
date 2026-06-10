"""Validate raw Graphify parser output into a FileGraph (fail loudly, never guess)."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from common.logging import get_logger
from contracts.artifact_schemas import FileGraph

logger = get_logger("kb_builder.graphify_adapter.parse")


def parse_file_graph(raw: Mapping[str, Any]) -> FileGraph:
    try:
        return FileGraph.model_validate(raw)
    except ValidationError:
        logger.error("event=graphify_parse_failed path=%s", raw.get("path", "<missing>"))
        raise
