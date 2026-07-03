"""Structured logging for every panel path. No bare prints.

Deliberately duplicated across services (ADR-0008); kept in sync through
docs/contracts, never by import.
"""

import logging

__all__ = ["PACKAGE_LOGGER", "configure_logging", "get_logger"]

PACKAGE_LOGGER = "review_panel"
_HANDLER_NAME = "review_panel.structured"
_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Attach the structured stderr handler at `level` to the package logger. Idempotent."""
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level)
    logger.propagate = False
    if any(handler.name == _HANDLER_NAME for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the `review_panel` tree."""
    return logging.getLogger(name)
