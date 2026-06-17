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

from agentic_kb_builder.structured_logging import (
    PACKAGE_LOGGER,
    JsonFormatter,
    TimelineFormatter,
    _select_formatter,
    _stage_for,
    configure_logging,
    get_logger,
)


def _record(name: str, message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )


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


# --- timeline formatter: human rendering must preserve the greppable structured tail ---


def test_timeline_keeps_the_full_event_and_key_value_tail() -> None:
    # The whole point: the human prefix is additive — every existing event=/key=value
    # token a test greps for must still appear verbatim in the rendered line.
    message = "event=docify_started source_uri=file:///x.md path=x.md model=ollama:llama3.1"
    line = TimelineFormatter().format(_record("agentic_kb_builder.docify.extractor", message))
    assert message in line
    assert "event=docify_started" in line
    assert "model=ollama:llama3.1" in line


def test_timeline_leads_with_clock_stage_and_headline() -> None:
    line = TimelineFormatter().format(
        _record("agentic_kb_builder.docify.extractor", "event=docify_started path=x.md")
    )
    # HH:MM:SS.mmm clock, an elapsed delta, the DOCIFY stage, and the human headline.
    import re

    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s+\+", line)
    assert "DOCIFY" in line
    assert "extracting doc" in line  # the headline for docify_started


def test_timeline_surfaces_level_for_warnings_and_errors() -> None:
    line = TimelineFormatter().format(
        _record(
            "agentic_kb_builder.application.publish_gates",
            "event=publish_gate_failed gate=x",
            level=logging.ERROR,
        )
    )
    assert "ERROR" in line
    assert "event=publish_gate_failed" in line


def test_stage_derivation_covers_the_build_pipeline() -> None:
    assert _stage_for("agentic_kb_builder.connectors.local_fs") == "FETCH"
    assert _stage_for("agentic_kb_builder.docify.extractor") == "DOCIFY"
    assert _stage_for("agentic_kb_builder.graphify.write") == "GRAPHIFY"
    assert _stage_for("agentic_kb_builder.linker.judge") == "JUDGE"
    assert _stage_for("agentic_kb_builder.linker.run") == "LINKER"
    assert _stage_for("agentic_kb_builder.indexing.upsert") == "INDEX"
    assert _stage_for("agentic_kb_builder.application.publish_gates") == "GATE"
    assert _stage_for("agentic_kb_builder.application.active_version") == "ACTIVATE"
    assert _stage_for("agentic_kb_builder.application.build_runner") == "BUILD"


def test_json_formatter_emits_one_object_with_the_message_preserved() -> None:
    import json

    line = JsonFormatter().format(
        _record("agentic_kb_builder.application.build_runner", "event=build_run_started x=1")
    )
    payload = json.loads(line)
    assert payload["stage"] == "BUILD"
    assert payload["msg"] == "event=build_run_started x=1"
    assert payload["level"] == "INFO"


def test_select_formatter_honors_explicit_format() -> None:
    assert isinstance(_select_formatter("timeline"), TimelineFormatter)
    assert isinstance(_select_formatter("json"), JsonFormatter)
    raw = _select_formatter("raw")
    assert not isinstance(raw, (TimelineFormatter, JsonFormatter))


def test_select_formatter_reads_log_format_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert isinstance(_select_formatter(None), JsonFormatter)


def test_select_formatter_defaults_to_raw_off_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI / a piped nightly build is not a TTY -> the original parseable line, so machine
    # consumers are unaffected by the human default.
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    fmt = _select_formatter(None)
    assert not isinstance(fmt, (TimelineFormatter, JsonFormatter))


def test_configure_logging_accepts_a_format_override(package_logger: logging.Logger) -> None:
    configure_logging(log_format="timeline")
    handler = next(h for h in package_logger.handlers if h.name == "agentic_kb_builder.structured")
    assert isinstance(handler.formatter, TimelineFormatter)
