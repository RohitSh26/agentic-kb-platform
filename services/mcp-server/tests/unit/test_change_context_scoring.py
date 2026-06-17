"""Dependency-ranking for context.create_change_pack (pure, no DB).

The BUILD pack must include the imported INTERFACE/exception/client a change needs (so the
agent writes correct code) WITHOUT pulling every import (token bloat). These pin the ranking:
the file defining a contract type the target actually uses outranks an incidental import, and a
ubiquitous logging/telemetry utility is de-prioritised below a genuinely-used domain client.
"""

from agentic_mcp_server.context_broker.change_context import _score_imported_file


def test_contract_type_used_by_target_scores_high() -> None:
    # http_client.py defines HttpFetchError + AsyncHttpClient, both used in the target's body
    target_body = "class GitHubRestBackend:\n    raise HttpFetchError(...)  # uses AsyncHttpClient"
    score, used = _score_imported_file(
        ["HttpFetchError", "AsyncHttpClient"],
        target_body.lower(),
        "add repo_is_accessible 404",
        "src/pkg/http_client.py",
    )
    # 2 body hits (0.45*2) + 2 contract names (0.25*2) = 1.40
    assert score >= 1.0
    assert used == ["HttpFetchError", "AsyncHttpClient"]


def test_unused_import_scores_low_or_zero() -> None:
    # a logging helper that is imported but not referenced in the change's body
    score, used = _score_imported_file(
        ["get_logger"], "class Backend: pass", "add a method", "src/pkg/helpers.py"
    )
    assert score == 0.0
    assert used == []


def test_name_in_task_is_rewarded() -> None:
    # callers pass an already-lowercased query (as create_change_pack does)
    score, _used = _score_imported_file(
        ["SourceRef"], "body without it", "return a sourceref list", "src/pkg/models.py"
    )
    assert score >= 0.40  # appears in the task


def test_contract_type_alone_still_ranks_some() -> None:
    # an Error/Client type that is imported but not in the body still gets the contract bump,
    # so a genuinely relevant exception isn't dropped to zero
    score, used = _score_imported_file(
        ["TimeoutError"], "unrelated body", "do something", "src/pkg/errors.py"
    )
    assert score == 0.25
    assert used == []


def test_used_client_outranks_ubiquitous_logger() -> None:
    """The demonstrated BUILD-lane failure: a target that USES both AsyncHttpClient and a logger
    must rank the http client ABOVE structured_logging, so the implementer sees the real client
    API rather than guessing a non-existent ``client.get(...)``."""
    body = (
        "class GitHubRestBackend:\n"
        "    def repo_is_accessible(self, client: AsyncHttpClient):\n"
        "        get_logger(__name__).info('x')\n"
        "        return client.request('GET', '/repos')"
    ).lower()
    task = "add repo_is_accessible that does a get /repos and returns true on 200, false on 404"

    client_score, _ = _score_imported_file(
        ["AsyncHttpClient"], body, task, "src/pkg/http_client.py"
    )
    logger_score, _ = _score_imported_file(
        ["get_logger"], body, task, "src/pkg/structured_logging.py"
    )
    # the used client (+0.45 used, +0.25 Client kind = 0.70) beats the down-weighted logger
    # (+0.45 used - 0.50 ubiquity = -0.05) by a clear margin.
    assert client_score > logger_score
    assert logger_score < 0  # the ubiquitous utility is filtered out (score <= 0) as a dependency


def test_ubiquity_penalty_lifts_when_the_task_targets_logging() -> None:
    """The down-weight is by ubiquity, not a blanket ban: a task that explicitly names the
    logging module keeps it in contention so logging changes still resolve their dependency."""
    body = "logger = get_logger(__name__)"
    task = "refactor structured_logging to emit json"
    score, _ = _score_imported_file(["get_logger"], body, task, "src/pkg/structured_logging.py")
    assert score > 0  # no ubiquity penalty when the task itself names the module
