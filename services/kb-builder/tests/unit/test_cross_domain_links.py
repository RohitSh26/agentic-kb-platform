"""Deterministic cross-domain link rules (PR-26).

Pure, DB-free coverage of:
- reference parsing: every supported explicit form links; incidental numbers do NOT.
- commit → work-item implements (by external_id / title), with the exact matched
  substring as the evidence pointer.
- commit changed-file → code_file mentions.
- doc → work-item mentions, reusing the same explicit-reference guard.
- the ACL-intersection helper (no-leak / no-widen).
"""

import uuid

import pytest

from agentic_kb_builder.application.write_commit import DENY_ALL_ACL, commit_acl_intersection
from agentic_kb_builder.connectors.git_metadata import (
    CHANGED_FILES_HEADER,
    CommitRecord,
    parse_changed_files,
    render_commit,
)
from agentic_kb_builder.linker.cross_domain import (
    IMPLEMENTS_CONFIDENCE,
    MENTIONS_CONFIDENCE,
    find_cross_domain_links,
    find_doc_work_item_mentions,
    parse_sha_references,
    parse_work_item_references,
)
from agentic_kb_builder.linker.records import LinkableArtifact


def _artifact(
    artifact_type: str,
    *,
    title: str | None = None,
    body_text: str | None = None,
    source_type: str = "github_code",
    external_id: str | None = None,
    path: str | None = None,
    branch: str | None = None,
) -> LinkableArtifact:
    return LinkableArtifact(
        artifact_id=uuid.uuid4(),
        artifact_type=artifact_type,
        title=title,
        body_text=body_text,
        source_type=source_type,
        external_id=external_id,
        path=path,
        branch=branch,
    )


def _commit(body: str, *, branch: str | None = None) -> LinkableArtifact:
    return _artifact(
        "commit", body_text=body, source_type="git_metadata", external_id="a" * 40, branch=branch
    )


def _work_item(external_id: str, *, title: str = "Card") -> LinkableArtifact:
    return _artifact(
        "summary",
        title=title,
        body_text="card body",
        source_type="ado_card",
        external_id=external_id,
    )


# --------------------------------------------------------------------------
# reference parsing — positive
# --------------------------------------------------------------------------

POSITIVE_REFS = [
    ("AB#123 implement feature", None, "123", "AB#123"),
    ("fixes #123", None, "123", "#123"),
    ("closes GH-123", None, "123", "GH-123"),
    ("see PR #123 for context", None, "123", "PR #123"),
    ("done", "feature/AB-123-foo", "123", "AB-123"),
    ("done", "bugfix/123-thing", "123", "123"),
]


@pytest.mark.parametrize(("message", "branch", "key", "matched"), POSITIVE_REFS)
def test_reference_parsing_positive(
    message: str, branch: str | None, key: str, matched: str
) -> None:
    refs = parse_work_item_references(message, branch)
    keys = {r.key for r in refs}
    assert key in keys
    found = next(r for r in refs if r.key == key)
    assert found.matched == matched


# --------------------------------------------------------------------------
# reference parsing — negative (incidental numbers must NOT parse)
# --------------------------------------------------------------------------

NEGATIVE_MESSAGES = [
    "fixed 42 failing tests",  # bare number, no reference form
    "bump version to 1.2.3",  # version
    "released in 2026",  # year
    "refactor module abc123def into pieces",  # number inside a word
    "merge branch main",  # no digits at all
]


@pytest.mark.parametrize("message", NEGATIVE_MESSAGES)
def test_reference_parsing_negative(message: str) -> None:
    assert parse_work_item_references(message, None) == []


def test_branch_without_workitem_does_not_parse() -> None:
    assert parse_work_item_references("done", "feature/no-number-here") == []


# --------------------------------------------------------------------------
# commit → work-item implements, with evidence pointer
# --------------------------------------------------------------------------


def test_commit_implements_work_item_by_external_id() -> None:
    commit = _commit("AB#1234 wire up the thing")
    card = _work_item("1234")
    drafts = find_cross_domain_links([commit, card])
    implements = [d for d in drafts if d.edge_type == "implements"]
    assert len(implements) == 1
    draft = implements[0]
    assert draft.from_artifact_id == commit.artifact_id
    assert draft.to_artifact_id == card.artifact_id
    assert draft.confidence == IMPLEMENTS_CONFIDENCE
    assert draft.strategy == "deterministic"
    assert draft.evidence == {"kind": "work_item_ref", "matched": "AB#1234"}


def test_commit_implements_work_item_by_title_digits() -> None:
    commit = _commit("fixes #777")
    card = _work_item("WI-777", title="Story 777: payments")
    drafts = find_cross_domain_links([commit, card])
    assert any(d.edge_type == "implements" and d.to_artifact_id == card.artifact_id for d in drafts)


def test_no_false_implements_from_incidental_number() -> None:
    # commit mentions "42" only as a bare count; card 42 must NOT be linked.
    commit = _commit("fixed 42 failing tests")
    card = _work_item("42")
    drafts = find_cross_domain_links([commit, card])
    assert [d for d in drafts if d.edge_type == "implements"] == []


def test_commit_implements_from_branch_reference() -> None:
    commit = _commit("ship it", branch="feature/AB-321-foo")
    card = _work_item("321")
    drafts = find_cross_domain_links([commit, card])
    assert any(
        d.edge_type == "implements"
        and d.to_artifact_id == card.artifact_id
        and d.evidence == {"kind": "work_item_ref", "matched": "AB-321"}
        for d in drafts
    )


# --------------------------------------------------------------------------
# commit changed-file → code_file mentions
# --------------------------------------------------------------------------


def _commit_with_files(subject: str, files: list[str]) -> LinkableArtifact:
    body = "\n\n".join([subject, "\n".join([CHANGED_FILES_HEADER, *files])])
    return _artifact("commit", body_text=body, source_type="git_metadata", external_id="b" * 40)


def test_commit_mentions_changed_code_file() -> None:
    commit = _commit_with_files("touch service", ["src/app/service.py"])
    code_file = _artifact("code_file", title="src/app/service.py", path="src/app/service.py")
    drafts = find_cross_domain_links([commit, code_file])
    mentions = [d for d in drafts if d.edge_type == "mentions"]
    assert len(mentions) == 1
    draft = mentions[0]
    assert draft.from_artifact_id == commit.artifact_id
    assert draft.to_artifact_id == code_file.artifact_id
    assert draft.confidence == MENTIONS_CONFIDENCE
    assert draft.evidence == {"kind": "changed_file", "path": "src/app/service.py"}


def test_changed_file_with_no_code_artifact_creates_no_edge() -> None:
    commit = _commit_with_files("touch docs", ["docs/readme.md"])
    other = _artifact("code_file", title="src/other.py", path="src/other.py")
    drafts = find_cross_domain_links([commit, other])
    assert [d for d in drafts if d.edge_type == "mentions"] == []


# --------------------------------------------------------------------------
# doc → work-item mentions
# --------------------------------------------------------------------------


def test_doc_mentions_work_item_by_verbatim_id() -> None:
    doc = _artifact(
        "summary",
        title="design note",
        body_text="This follows AB#999 closely.",
        source_type="github_doc",
    )
    card = _work_item("999")
    drafts = find_doc_work_item_mentions([doc], [doc, card])
    assert len(drafts) == 1
    assert drafts[0].edge_type == "mentions"
    assert drafts[0].from_artifact_id == doc.artifact_id
    assert drafts[0].to_artifact_id == card.artifact_id
    assert drafts[0].evidence == {"kind": "work_item_ref", "matched": "AB#999"}


def test_doc_with_incidental_number_does_not_mention_work_item() -> None:
    doc = _artifact(
        "summary",
        title="note",
        body_text="We processed 999 records last night.",
        source_type="github_doc",
    )
    card = _work_item("999")
    assert find_doc_work_item_mentions([doc], [doc, card]) == []


# --------------------------------------------------------------------------
# SHA references
# --------------------------------------------------------------------------


def test_sha_reference_parsing() -> None:
    refs = parse_sha_references("reverts a1b2c3d4 and follows up")
    assert {r.key for r in refs} == {"a1b2c3d4"}


def test_non_hex_word_is_not_a_sha() -> None:
    # "deadbeefzz" is not pure hex; "zzzzzzz" is not hex.
    assert parse_sha_references("the zzzzzzz path") == []


def test_pure_decimal_run_is_not_a_sha() -> None:
    # A 7+ digit decimal (a year range, a row count, an issue id) is all valid
    # hex chars but must NOT be taken for a commit SHA — needs an a-f letter.
    assert parse_sha_references("added 1234567 rows in 2024010199") == []


def test_changed_files_header_in_commit_body_does_not_poison_roundtrip() -> None:
    # A commit whose message literally contains the header line must still
    # recover the REAL changed-file section (render appends it last).
    commit = CommitRecord(
        sha="a" * 40,
        subject="refactor",
        body=f"see the\n{CHANGED_FILES_HEADER}\nfake/decoy.py\nin the old log",
        changed_files=("real/one.py", "real/two.py"),
    )
    rendered = render_commit(commit)
    assert parse_changed_files(rendered) == ("real/one.py", "real/two.py")


# --------------------------------------------------------------------------
# ACL intersection helper — no leak, no widen
# --------------------------------------------------------------------------


def test_acl_intersection_only_teams_authorised_for_every_input() -> None:
    file_acls = {"a.py": ["platform", "payments"], "b.py": ["payments"]}
    result = commit_acl_intersection(["a.py", "b.py"], file_acls)
    assert result == ["payments"]


def test_acl_restricted_plus_public_keeps_restriction() -> None:
    # b.py is org-public (empty acl) → it imposes no constraint; the restricted
    # file's ACL still gates the commit (no widening to org-public).
    file_acls = {"a.py": ["secret"], "b.py": []}
    assert commit_acl_intersection(["a.py", "b.py"], file_acls) == ["secret"]


def test_acl_disjoint_restrictions_deny_all() -> None:
    # No team can see BOTH files. [] would mean org-public (everyone) at read, so
    # the commit must carry the deny-all sentinel — never widen to everyone.
    file_acls = {"a.py": ["team-a"], "b.py": ["team-b"]}
    assert commit_acl_intersection(["a.py", "b.py"], file_acls) == list(DENY_ALL_ACL)


def test_acl_unresolved_files_contribute_nothing() -> None:
    # a changed file with no source_item is absent from file_acls; it must not
    # widen visibility — the resolved restricted file still gates.
    file_acls = {"a.py": ["secret"]}
    assert commit_acl_intersection(["a.py", "unknown.py"], file_acls) == ["secret"]


def test_acl_zero_resolvable_inputs_deny_by_default() -> None:
    # Unknown provenance ⇒ deny by default (the sentinel, NOT [] = everyone).
    assert commit_acl_intersection(["x.py", "y.py"], {}) == list(DENY_ALL_ACL)


def test_acl_all_public_inputs_stay_public() -> None:
    file_acls = {"a.py": [], "b.py": []}
    assert commit_acl_intersection(["a.py", "b.py"], file_acls) == []
