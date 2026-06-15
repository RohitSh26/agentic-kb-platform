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

Rendering
---------
A human watching a build wants a readable, real-time TIMELINE — which source/file is
being processed, which step, every model call, each publish gate, activation — not a
wall of bare `event=key=value` lines. `TimelineFormatter` renders each record as:

    14:02:31.412  +1.8s  WIKIFY   summarizing README.md  event=wikify_started ...

i.e. a wall-clock time + an elapsed-since-build-start delta + a short STAGE label, then
a short human message, then the FULL structured `event=... key=value` tail so logs stay
greppable AND machine-parseable. The tail is the record's own message verbatim, so every
existing `event=`/`key=value` assertion keeps passing — only the *rendering* changed.

`$LOG_FORMAT` selects the renderer:
- `timeline` — the human timeline above (the default when stderr is a TTY),
- `raw`      — the original `ts=... level=... logger=... <message>` line (the default
  when stderr is NOT a TTY, e.g. CI / a piped nightly build, so machine parsers are
  unaffected),
- `json`     — one JSON object per record (`{"ts","level","logger","stage","msg"}`).
"""

import json
import logging
import os
import re
import sys
import time

__all__ = ["PACKAGE_LOGGER", "configure_logging", "get_logger"]

PACKAGE_LOGGER = "agentic_kb_builder"
_HANDLER_NAME = "agentic_kb_builder.structured"
# The original machine line; still the default off a TTY and the `raw` format.
_RAW_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"

# Wall-clock at the first formatted record — the "+Xs" delta is measured from here so a
# reader sees how far into the build each line lands. Process-global (one build per
# process) and lazily initialised so importing the module does not start the clock.
_BUILD_START_MONOTONIC: float | None = None

# Map a logger-name fragment to a short, fixed-width STAGE label. First match wins, so
# order most-specific first. Anything unmatched falls back to BUILD (the runner/CLI).
_STAGE_RULES: tuple[tuple[str, str], ...] = (
    ("connectors", "FETCH"),
    ("wikify", "WIKIFY"),
    ("graphify", "GRAPHIFY"),
    ("linker.judge", "JUDGE"),
    ("linker.judgment_cache", "JUDGE"),
    ("linker.candidates", "LINKER"),
    ("linker.run_candidates", "LINKER"),
    ("linker.cross_domain", "LINKER"),
    ("linker", "LINKER"),
    ("indexing", "INDEX"),
    ("local_search", "INDEX"),
    ("azure_search", "INDEX"),
    ("azure_openai", "MODEL"),
    ("invalidation", "INVALID"),
    ("publish_gates", "GATE"),
    ("active_version", "ACTIVATE"),
    ("export_obsidian", "EXPORT"),
    ("cache_gates", "CACHE"),
)
_STAGE_WIDTH = 8

# event= name -> short human verb shown before the structured tail. Only a hint for the
# reader; the authoritative data is always the event=/key=value tail that follows.
_EVENT_HEADLINES: dict[str, str] = {
    "build_run_started": "build started",
    "build_run_completed": "build completed",
    "build_run_failed": "build FAILED",
    "build_finished": "build finished",
    "build_summary": "build summary",
    "build_activation": "activation decision",
    "build_skip_unchanged": "unchanged, skipping",
    "build_source_started": "processing source",
    "build_file_wikify": "entering wikify",
    "build_file_graphify": "entering graphify",
    "source_item_upserted": "recorded source",
    "connector_fetch": "fetched source",
    "model_call": "model call",
    "model_call_failed": "model call FAILED",
    "wikify_started": "summarizing",
    "wikify_generated": "summarized",
    "wikify_artifacts_written": "wrote wikify artifacts",
    "graphify_started": "parsing code",
    "graphify_artifacts_written": "wrote code artifacts",
    "graphify_edges_written": "wrote code edges",
    "graphify_extraction_failed": "extraction FAILED",
    "generation_cache_lookup": "generation cache",
    "generation_cache_record": "generation cached",
    "embedding_cache_lookup": "embedding cache",
    "embedding_cache_record": "embedding cached",
    "embedding_computed": "embedded",
    "linker_deterministic_matched": "deterministic links",
    "linker_semantic_matched": "semantic links",
    "linker_cross_domain_matched": "cross-domain links",
    "linker_edges_written": "wrote linker edges",
    "candidate_generated": "candidates generated",
    "candidate_written": "candidates written",
    "judge_completed": "judge completed",
    "judge_edge_written": "wrote judged edge",
    "invalidation_pass_completed": "invalidation pass",
    "indexer_docs_upserted": "index upsert",
    "build_index_backfilled": "index back-fill",
    "build_index_orphans_removed": "index orphans removed",
    "index_consistency_validated": "index consistency",
    "publish_gate": "publish gate",
    "publish_gate_failed": "publish gate FAILED",
    "publish_gate_skipped": "publish gate skipped",
    "publish_gates_passed": "all publish gates passed",
    "kb_version_activated": "version ACTIVATED",
    "kb_version_validation_failed": "validation FAILED",
}

_EVENT_RE = re.compile(r"event=([a-zA-Z0-9_]+)")


def _stage_for(logger_name: str) -> str:
    for fragment, label in _STAGE_RULES:
        if fragment in logger_name:
            return label
    return "BUILD"


def _headline(message: str) -> str:
    match = _EVENT_RE.search(message)
    if match is None:
        return ""
    return _EVENT_HEADLINES.get(match.group(1), "")


class TimelineFormatter(logging.Formatter):
    """Human, real-time build timeline. Keeps the full structured tail.

    The emitted line is `<HH:MM:SS.mmm>  +<elapsed>  <STAGE>  [<headline>]  <message>`,
    where `<message>` is the record's own `event=... key=value` text verbatim — so the
    line stays greppable on every existing `event=`/`key=value` token while leading with
    information a human reads at a glance.
    """

    def format(self, record: logging.LogRecord) -> str:
        global _BUILD_START_MONOTONIC
        now = time.monotonic()
        if _BUILD_START_MONOTONIC is None:
            _BUILD_START_MONOTONIC = now
        elapsed = now - _BUILD_START_MONOTONIC
        clock = time.strftime("%H:%M:%S", time.localtime(record.created))
        clock = f"{clock}.{int(record.msecs):03d}"
        stage = _stage_for(record.name).ljust(_STAGE_WIDTH)
        message = record.getMessage()
        headline = _headline(message)
        level = "" if record.levelno <= logging.INFO else f"{record.levelname} "
        lead = f"{clock}  +{elapsed:5.1f}s  {stage} {level}"
        body = f"{headline} :: {message}" if headline else message
        line = f"{lead}{body}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per record; the structured tail is preserved in `msg`."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "stage": _stage_for(record.name),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def _select_formatter(log_format: str | None) -> logging.Formatter:
    """Resolve the renderer from `$LOG_FORMAT` (or the explicit override).

    Default: `timeline` when stderr is a TTY (a human is watching), else `raw` (a
    machine — CI / a piped nightly build — keeps the original parseable line)."""
    choice = (log_format or os.environ.get("LOG_FORMAT") or "").strip().lower()
    if not choice:
        choice = "timeline" if sys.stderr.isatty() else "raw"
    if choice == "json":
        return JsonFormatter()
    if choice == "timeline":
        return TimelineFormatter()
    return logging.Formatter(_RAW_FORMAT)


def configure_logging(level: int | str | None = None, *, log_format: str | None = None) -> None:
    """Attach the structured stderr handler to the package logger.

    Level defaults to `$LOG_LEVEL` or INFO. `log_format` (or `$LOG_FORMAT`) selects the
    renderer — `timeline` (human, the TTY default), `raw` (the original machine line, the
    non-TTY default), or `json`. Idempotent; sets `propagate=False` so records emit
    exactly once and do not also reach the unconfigured root logger.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level if level is not None else os.environ.get("LOG_LEVEL", "INFO"))
    logger.propagate = False
    if any(handler.name == _HANDLER_NAME for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(_select_formatter(log_format))
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the `agentic_kb_builder` tree.

    Handler and level come from `configure_logging()` on the package logger; child
    loggers deliberately add no handler of their own (that would double-emit).
    """
    return logging.getLogger(name)
