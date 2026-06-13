"""Structured logging for every build path. No bare prints.

`configure_logging()` is the boot-time setup the build entrypoint calls: it attaches
one structured handler to the `agentic_kb_builder` package logger so every
`agentic_kb_builder.*` logger (connectors, wikify, graphify, linker, indexing, the
build runner) inherits it at INFO (or `$LOG_LEVEL`). Without it, build records at INFO
reached a handler whose logger had no level set, so the effective level fell back to
the root default (WARNING) and they were silently dropped — the suite only saw them
because pytest's caplog installs its own INFO handler.

There is no build CLI yet (the nightly pipeline is a recorded follow-up — see
`docker-compose.yml`); when it lands it calls this at start, exactly as the
mcp-server's `create_app()` does. Do NOT call it from library or test code: it sets
`propagate=False`, which would stop caplog (root-anchored) from seeing build logs.
"""

import logging
import os

__all__ = ["PACKAGE_LOGGER", "configure_logging", "get_logger"]

PACKAGE_LOGGER = "agentic_kb_builder"
_HANDLER_NAME = "agentic_kb_builder.structured"
_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def configure_logging(level: int | str | None = None) -> None:
    """Attach the structured stderr handler to the package logger.

    Level defaults to `$LOG_LEVEL` or INFO. Idempotent; sets `propagate=False` so
    records emit exactly once and do not also reach the unconfigured root logger.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level if level is not None else os.environ.get("LOG_LEVEL", "INFO"))
    logger.propagate = False
    if any(handler.name == _HANDLER_NAME for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the `agentic_kb_builder` tree.

    Handler and level come from `configure_logging()` on the package logger; child
    loggers deliberately add no handler of their own (that would double-emit).
    """
    return logging.getLogger(name)
