"""configure_logging wires the package logger so build INFO lines emit.

KB-2 (#23): build loggers logged at INFO to a handler whose logger had no level,
so the effective level fell back to root (WARNING) and records were dropped.
configure_logging() attaches one handler at INFO to the agentic_kb_builder package
logger so every child inherits it. These tests snapshot and restore global logging
state so they cannot leak propagate=False into the rest of the suite.
"""

import logging
from collections.abc import Iterator

import pytest

from agentic_kb_builder.structured_logging import PACKAGE_LOGGER, configure_logging, get_logger


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

    get_logger("agentic_kb_builder.application.build_runner").info("event=build_run_started")

    assert any("event=build_run_started" in record.getMessage() for record in captured)


def test_configure_logging_honors_log_level(
    package_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    configure_logging()
    assert package_logger.level == logging.WARNING


def test_configure_logging_is_idempotent(package_logger: logging.Logger) -> None:
    configure_logging()
    configure_logging()
    structured = [h for h in package_logger.handlers if h.name == "agentic_kb_builder.structured"]
    assert len(structured) == 1


def test_get_logger_adds_no_handler_of_its_own() -> None:
    assert get_logger("agentic_kb_builder.tests.fresh_logger").handlers == []
