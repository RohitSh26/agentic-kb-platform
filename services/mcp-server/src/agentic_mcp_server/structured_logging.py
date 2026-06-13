"""Structured logging for every retrieval path. No bare prints.

`configure_logging()` is the boot-time setup: it attaches one structured handler at
INFO to the `agentic_mcp_server` package logger, so every `agentic_mcp_server.*`
logger (broker, audit, health, telemetry) inherits it. Without it, those records at
INFO propagate to an unconfigured root logger (default level WARNING) and are silently
dropped in production — the unit suite only saw them because pytest's caplog installs
its own INFO handler. Call it once at process start (see `mcp.server.create_app`).
"""

import logging

__all__ = ["PACKAGE_LOGGER", "configure_logging", "get_logger"]

PACKAGE_LOGGER = "agentic_mcp_server"
_HANDLER_NAME = "agentic_mcp_server.structured"
_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Attach the structured stderr handler at `level` to the package logger.

    Idempotent. Sets `propagate=False` so records are emitted exactly once and do
    not also reach the root logger (which is left unconfigured in this service).
    """
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
    """Return a logger under the `agentic_mcp_server` tree.

    Handler and level come from `configure_logging()` on the package logger; child
    loggers deliberately add no handler of their own (that would double-emit).
    """
    return logging.getLogger(name)
