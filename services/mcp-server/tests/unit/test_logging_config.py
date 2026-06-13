"""configure_logging wires the package logger so broker/audit INFO lines emit.

MCP-1 (#29): the broker and audit loggers logged at INFO to an unconfigured root
(default WARNING) and were silently dropped in production. configure_logging()
attaches one handler at INFO to the agentic_mcp_server package logger so every child
inherits it. These tests snapshot and restore global logging state so they cannot
leak propagate=False into the rest of the suite.
"""

import logging
from collections.abc import Iterator

import pytest

from agentic_mcp_server.structured_logging import PACKAGE_LOGGER, configure_logging, get_logger


@pytest.fixture
def package_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger(PACKAGE_LOGGER)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    try:
        yield logger
    finally:
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logger.handlers[:] = saved_handlers


def test_configure_logging_makes_a_child_emit_at_info(package_logger: logging.Logger) -> None:
    configure_logging()
    assert package_logger.level == logging.INFO
    assert package_logger.propagate is False

    captured: list[logging.LogRecord] = []
    sink = logging.Handler()
    sink.emit = captured.append  # type: ignore[method-assign]
    package_logger.addHandler(sink)

    # a broker/audit-style child logger that adds no handler of its own
    get_logger("agentic_mcp_server.audit").info("event=audit_line subject=alice")

    assert any("event=audit_line" in record.getMessage() for record in captured)


def test_configure_logging_is_idempotent(package_logger: logging.Logger) -> None:
    configure_logging()
    configure_logging()
    structured = [h for h in package_logger.handlers if h.name == "agentic_mcp_server.structured"]
    assert len(structured) == 1


def test_get_logger_adds_no_handler_of_its_own() -> None:
    # children inherit the package handler; a per-child handler would double-emit
    assert get_logger("agentic_mcp_server.tests.fresh_logger").handlers == []
