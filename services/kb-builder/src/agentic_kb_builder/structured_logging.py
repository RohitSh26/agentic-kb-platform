"""Structured logging for every build and retrieval path. No bare prints."""

import logging

__all__ = ["get_logger"]

_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits structured key=value records."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    return logger
