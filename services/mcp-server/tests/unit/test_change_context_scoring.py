"""Dependency-ranking for context.create_change_pack (pure, no DB).

The BUILD pack must include the imported INTERFACE/exception/client a change needs (so the
agent writes correct code) WITHOUT pulling every import (token bloat). These pin the ranking:
the file defining a contract type the target actually uses outranks an incidental import.
"""

from agentic_mcp_server.context_broker.change_context import _score_imported_file


def test_contract_type_used_by_target_scores_high() -> None:
    # http_client.py defines HttpFetchError + AsyncHttpClient, both used in the target's body
    target_body = "class GitHubRestBackend:\n    raise HttpFetchError(...)  # uses AsyncHttpClient"
    score, used = _score_imported_file(
        ["HttpFetchError", "AsyncHttpClient"], target_body.lower(), "add repo_is_accessible 404"
    )
    # 2 body hits (0.30*2) + 2 contract names (0.25*2) = 1.10
    assert score >= 1.0
    assert used == ["HttpFetchError", "AsyncHttpClient"]


def test_unused_import_scores_low_or_zero() -> None:
    # a logging helper that is imported but not referenced in the change's body
    score, used = _score_imported_file(["get_logger"], "class Backend: pass", "add a method")
    assert score == 0.0
    assert used == []


def test_name_in_task_is_rewarded() -> None:
    # callers pass an already-lowercased query (as create_change_pack does)
    score, used = _score_imported_file(["SourceRef"], "body without it", "return a sourceref list")
    assert score >= 0.40  # appears in the task


def test_contract_type_alone_still_ranks_some() -> None:
    # an Error/Client type that is imported but not in the body still gets the contract bump,
    # so a genuinely relevant exception isn't dropped to zero
    score, used = _score_imported_file(["TimeoutError"], "unrelated body", "do something")
    assert score == 0.25
    assert used == []
