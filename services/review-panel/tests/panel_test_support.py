"""Hermetic fakes for the draft engine: no LLM, no GitHub, no LangSmith creds.

TEST_DATABASE_URL follows the mcp-server convention: integration tests that
need Postgres read it (or DATABASE_URL) and skip when absent; everything else
runs on the in-memory checkpointer + draft store.

A sibling workstream rewords the agents/*.md manifests, so NOTHING here pins
manifest text: the fake model identifies the calling panelist by matching the
system prompt against the bodies the runtime loader itself returns.
"""

import asyncio
import functools
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import overload

from langchain_core.runnables import RunnableConfig

from review_panel.domain.draft import draft_key
from review_panel.domain.pr import PRContext
from review_panel.graph.nodes import PanelDependencies
from review_panel.graph.prompts import PanelPrompts, load_panel_prompts
from review_panel.graph.state import PanelState
from review_panel.infrastructure.draft_store import InMemoryDraftStore
from review_panel.infrastructure.model_client import ModelClient

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"

HEAD_SHA = "cafe1234" * 5  # 40 hex chars

DEFAULT_DIFF = """\
diff --git a/src/cache.py b/src/cache.py
--- a/src/cache.py
+++ b/src/cache.py
@@ -40,6 +40,8 @@ def write(key, value):
+    if key not in store:
+        store[key] = value
"""


@functools.cache
def real_prompts() -> PanelPrompts:
    return load_panel_prompts(AGENTS_DIR)


def lens_of(system: str) -> str:
    """Which panelist is calling — matched against the LOADED manifest bodies
    (structure, not wording: no manifest text is pinned here)."""
    prompts = real_prompts()
    for lens, body in prompts.reviewers.items():
        if body in system:
            return lens
    if prompts.synthesizer in system:
        return "synthesizer"
    raise AssertionError(f"system prompt matches no loaded manifest body: {system[:120]!r}")


def findings_json(
    verdict: str = "request_changes",
    findings: list[dict[str, object]] | None = None,
    open_questions: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "verdict": verdict,
            "findings": findings or [],
            "open_questions": open_questions or [],
        }
    )


#: Canned outputs chosen to exercise reconcile: bug + security report the SAME
#: race issue at different severities (duplicate + disagreement), security adds
#: a blocker, quality a note, test_coverage a major + an open question.
DEFAULT_RESPONSES: dict[str, str] = {
    "bug": findings_json(
        findings=[
            {
                "severity": "major",
                "finding": "Race condition in cache write path allows a double write",
                "evidence_ids": ["src/cache.py:42"],
            }
        ]
    ),
    "security": findings_json(
        findings=[
            {
                "severity": "blocker",
                "finding": "SQL injection in search query building",
                "evidence_ids": ["src/search.py:10"],
            },
            {
                "severity": "minor",
                "finding": "race condition in cache write path allows a double write",
                "evidence_ids": ["src/cache.py:40"],
            },
        ]
    ),
    "quality": findings_json(
        findings=[
            {
                "severity": "note",
                "finding": "Helper name write does not reveal its idempotency intent",
                "evidence_ids": ["src/cache.py:40"],
            }
        ]
    ),
    "test_coverage": findings_json(
        findings=[
            {
                "severity": "major",
                "finding": "New early-return branch in cache writer lacks a covering test",
                "evidence_ids": ["src/cache.py:41"],
            }
        ],
        open_questions=["Is the cache writer covered by an integration test elsewhere?"],
    ),
    "synthesizer": findings_json(
        findings=[
            {
                "severity": "blocker",
                "finding": "Panel agrees the injection and race issues must be fixed first",
                "evidence_ids": ["src/search.py:10", "src/cache.py:42"],
            }
        ]
    ),
}


class FakeModelClient:
    """Scriptable model; records prompts and tracks true fan-out concurrency."""

    def __init__(
        self,
        respond: Callable[[str, str], str] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._respond = respond or (lambda system, _user: DEFAULT_RESPONSES[lens_of(system)])
        self._delay = delay
        self._active = 0
        self.max_concurrent = 0

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            return self._respond(system, user)
        finally:
            self._active -= 1

    def reviewer_calls(self) -> list[tuple[str, str]]:
        return [(s, u) for s, u in self.calls if lens_of(s) != "synthesizer"]


class ScriptedModelClient:
    """Per-lens scripted responses, for proving the schema-repair retry
    (review_panel.graph.nodes._complete_with_schema_repair) end-to-end through the
    real graph: `scripts["bug"] = [bad_json, good_json]` fails once then recovers on
    lens "bug"; `[bad_json, bad_json]` proves the bound. A lens with no script (or
    one shorter than the calls it receives, past the last scripted entry) falls back
    to DEFAULT_RESPONSES so the rest of the panel completes normally."""

    def __init__(self, scripts: dict[str, list[str]] | None = None) -> None:
        self._scripts = scripts or {}
        self._counts: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        lens = lens_of(system)
        script = self._scripts.get(lens)
        if not script:
            return DEFAULT_RESPONSES[lens]
        count = self._counts.get(lens, 0)
        self._counts[lens] = count + 1
        return script[min(count, len(script) - 1)]

    def calls_for(self, lens: str) -> int:
        return sum(1 for system, _user in self.calls if lens_of(system) == lens)


class FakeGitHubClient:
    """In-memory READ-ONLY PR source. It deliberately has no write method at
    all — any escalation attempt would be an AttributeError, and `calls`
    records every capability that was exercised."""

    def __init__(self, pr: PRContext) -> None:
        self.pr = pr
        self.calls: list[str] = []

    async def get_pr(self, repo: str, number: int) -> PRContext:
        self.calls.append("get_pr")
        assert repo == self.pr.repo and number == self.pr.number
        return self.pr


class FakeKBSearch:
    def __init__(self, result: str = "") -> None:
        self.result = result
        self.queries: list[str] = []

    async def search(self, query: str) -> str:
        self.queries.append(query)
        return self.result


def make_pr(
    *,
    title: str = "Make cache writes idempotent",
    body: str = "Guards the cache writer against duplicate writes.",
    diff: str = DEFAULT_DIFF,
    head_sha: str = HEAD_SHA,
) -> PRContext:
    return PRContext(
        repo="acme/platform",
        number=7,
        head_sha=head_sha,
        title=title,
        body=body,
        author="dev",
        diff=diff,
    )


@overload
def make_deps(
    *,
    model: None = None,
    github: FakeGitHubClient | None = None,
    kb: FakeKBSearch | None = None,
    store: InMemoryDraftStore | None = None,
    pr: PRContext | None = None,
) -> tuple[PanelDependencies, FakeModelClient, FakeGitHubClient, InMemoryDraftStore]: ...


@overload
def make_deps(
    *,
    model: FakeModelClient,
    github: FakeGitHubClient | None = None,
    kb: FakeKBSearch | None = None,
    store: InMemoryDraftStore | None = None,
    pr: PRContext | None = None,
) -> tuple[PanelDependencies, FakeModelClient, FakeGitHubClient, InMemoryDraftStore]: ...


@overload
def make_deps(
    *,
    model: ModelClient,
    github: FakeGitHubClient | None = None,
    kb: FakeKBSearch | None = None,
    store: InMemoryDraftStore | None = None,
    pr: PRContext | None = None,
) -> tuple[PanelDependencies, ModelClient, FakeGitHubClient, InMemoryDraftStore]: ...


def make_deps(
    *,
    model: ModelClient | None = None,
    github: FakeGitHubClient | None = None,
    kb: FakeKBSearch | None = None,
    store: InMemoryDraftStore | None = None,
    pr: PRContext | None = None,
) -> tuple[PanelDependencies, ModelClient, FakeGitHubClient, InMemoryDraftStore]:
    """Build a full fake dependency set. `model` defaults to `FakeModelClient` (and the
    return type reflects that — see the overloads); passing any other `ModelClient`
    (e.g. `ScriptedModelClient`) is returned as-is, correctly typed, for tests that
    need a script the default fake can't express."""
    the_pr = pr or make_pr()
    the_model = model or FakeModelClient()
    the_github = github or FakeGitHubClient(the_pr)
    the_store = store or InMemoryDraftStore()
    deps = PanelDependencies(
        model=the_model,
        github=the_github,
        kb=kb or FakeKBSearch(),
        prompts=real_prompts(),
        store=the_store,
        model_label="fake:panel-test",
    )
    return deps, the_model, the_github, the_store


def panel_input(pr: PRContext) -> PanelState:
    return {"pr": pr}


def key_of(pr: PRContext) -> str:
    return draft_key(pr.repo, pr.number, pr.head_sha)


def thread_config(pr: PRContext) -> RunnableConfig:
    return {"configurable": {"thread_id": key_of(pr)}}
