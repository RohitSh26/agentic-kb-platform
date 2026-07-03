"""End-to-end draft run with a fake model: parallel fan-out, reconciled draft,
one stored row — and NO publish node anywhere in the graph (ADR-0031)."""

from langgraph.checkpoint.memory import InMemorySaver
from panel_test_support import (
    FakeKBSearch,
    FakeModelClient,
    key_of,
    make_deps,
    make_pr,
    panel_input,
    thread_config,
)

from review_panel.domain.render import DRAFT_DISCLAIMER
from review_panel.domain.untrusted import UNTRUSTED_BEGIN
from review_panel.graph.build import PANEL_NODES, STORE_NODE, build_panel_graph

#: Words that would indicate a publication capability. No node name may carry one.
PUBLISH_WORDS = ("post", "publish", "approve", "merge", "comment", "submit")


async def test_panel_drafts_and_stores_exactly_one_draft() -> None:
    pr = make_pr()
    deps, model, github, store = make_deps(
        pr=pr, model=FakeModelClient(delay=0.05), kb=FakeKBSearch("KB: cache module notes")
    )
    graph = build_panel_graph(deps, InMemorySaver())
    result = await graph.ainvoke(panel_input(pr), thread_config(pr))

    # 4 specialists + 1 synthesizer, nothing more
    assert len(model.calls) == 5
    assert len(model.reviewer_calls()) == 4
    # fan-out is genuinely parallel: all four reviewer calls overlap in time
    assert model.max_concurrent == 4

    draft = result["draft"]
    assert draft.draft_key == key_of(pr)
    assert await store.get(key_of(pr)) == draft  # persisted, not just returned
    assert draft.advisory_verdict == "request_changes"

    # reconcile merged the duplicate race finding (bug=major, security=minor) into ONE entry
    race = [f for f in draft.findings if "race condition" in f.finding.lower()]
    assert len(race) == 1
    assert race[0].lenses == ["bug", "security"]
    assert race[0].severity == "major"  # highest kept
    assert race[0].disagreement is not None and "severity disputed" in race[0].disagreement
    assert "bug=major" in race[0].disagreement and "security=minor" in race[0].disagreement

    # ranked by severity: the blocker (SQL injection) comes first
    assert draft.findings[0].finding == "SQL injection in search query building"
    assert draft.findings[0].severity == "blocker"
    # nothing dropped: every lens's finding text is present
    all_texts = [f.finding for f in draft.findings]
    assert "New early-return branch in cache writer lacks a covering test" in all_texts
    assert "Helper name write does not reveal its idempotency intent" in all_texts
    # open questions merged from panel + synthesizer
    assert "Is the cache writer covered by an integration test elsewhere?" in draft.open_questions

    # developer-editable renderings, produced by our code, draft-labelled
    assert all(f.suggested_comment.startswith(f"**[{f.severity}]**") for f in draft.findings)
    assert DRAFT_DISCLAIMER in draft.summary_markdown
    assert draft.provenance.engine == "review-panel"
    assert draft.provenance.model == "fake:panel-test"
    assert draft.provenance.kb_used is True

    # the engine never wrote to GitHub: the read-only fake saw no calls here
    # (the PR was injected as graph input) and has no write capability at all
    assert github.calls == []
    assert not hasattr(github, "post_review")


async def test_graph_node_set_contains_no_posting_node() -> None:
    """The dev gate (ADR-0031): the terminal node is store_draft; nothing can publish."""
    deps, _, _, _ = make_deps()
    graph = build_panel_graph(deps, InMemorySaver())
    node_names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert node_names == set(PANEL_NODES)
    assert STORE_NODE in node_names
    for name in node_names:
        for word in PUBLISH_WORDS:
            assert word not in name.lower(), f"node {name!r} looks like a publish path"


async def test_kb_context_reaches_every_reviewer_fenced() -> None:
    pr = make_pr()
    kb = FakeKBSearch("KB: cache module owned by platform")
    deps, model, _, _ = make_deps(pr=pr, kb=kb)
    graph = build_panel_graph(deps, InMemorySaver())
    await graph.ainvoke(panel_input(pr), thread_config(pr))

    assert kb.queries  # one shared search, derived from title + changed paths
    assert "src/cache.py" in kb.queries[0]
    for _system, user in model.reviewer_calls():
        assert f"{UNTRUSTED_BEGIN} kb_results" in user
        assert "KB: cache module owned by platform" in user


async def test_without_kb_configured_no_kb_block_appears() -> None:
    pr = make_pr()
    deps, model, _, store = make_deps(pr=pr)
    graph = build_panel_graph(deps, InMemorySaver())
    await graph.ainvoke(panel_input(pr), thread_config(pr))
    for _system, user in model.reviewer_calls():
        assert "kb_results" not in user
    stored = await store.get(key_of(pr))
    assert stored is not None
    assert stored.provenance.kb_used is False


async def test_long_diff_is_truncated_before_prompting() -> None:
    pr = make_pr(diff="+++ b/src/big.py\n" + ("x" * 100_000))
    deps, model, _, _ = make_deps(pr=pr)
    graph = build_panel_graph(deps, InMemorySaver())
    await graph.ainvoke(panel_input(pr), thread_config(pr))
    for _system, user in model.reviewer_calls():
        assert "[diff truncated by the review panel at 60000 chars]" in user
