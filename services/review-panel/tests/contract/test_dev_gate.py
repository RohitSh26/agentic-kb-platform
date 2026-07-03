"""The dev gate (ADR-0031 Decision §1) as a static contract: this service can
DRAFT but can never PUBLISH. No GitHub write capability exists anywhere in the
source tree — not disabled, absent."""

import ast
import re
from pathlib import Path

from review_panel.infrastructure.github_client import GitHubClient, HttpxGitHubClient

SERVICE_ROOT = Path(__file__).resolve().parents[2]
SRC = SERVICE_ROOT / "src" / "review_panel"

#: Method names that would constitute a publish path on any GitHub surface.
FORBIDDEN_METHODS = (
    "post_review",
    "submit_review",
    "create_review",
    "post_comment",
    "create_comment",
    "approve",
    "request_changes",
    "merge",
)


def test_github_port_exposes_only_the_read_capability() -> None:
    protocol_members = {
        name for name in vars(GitHubClient) if not name.startswith("_") and name != "mro"
    }
    assert protocol_members == {"get_pr"}
    for method in FORBIDDEN_METHODS:
        assert not hasattr(HttpxGitHubClient, method)


def test_github_adapter_never_issues_a_write_request() -> None:
    """The adapter source contains GET calls only — no POST/PUT/PATCH/DELETE,
    no httpx .post/.put/.patch/.delete, no request(method=...) escape hatch."""
    source = (SRC / "infrastructure" / "github_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("post", "put", "patch", "delete", "request"), (
                f"github adapter makes a non-GET/write-capable call: .{node.func.attr}(...)"
            )
    for verb in ('"POST"', "'POST'", '"PUT"', "'PUT'", '"PATCH"', "'PATCH'", '"DELETE"'):
        assert verb not in source


def test_no_publish_method_name_anywhere_in_the_source_tree() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for method in FORBIDDEN_METHODS:
            if re.search(rf"def {method}\b|\.{method}\(", source):
                violations.append(f"{path.relative_to(SERVICE_ROOT)}: {method}")
    assert not violations, "publish-capable code found:\n" + "\n".join(violations)


def test_github_reviews_endpoint_is_never_referenced() -> None:
    """The POST /pulls/{n}/reviews endpoint (the old posting path) is gone."""
    for path in sorted(SRC.rglob("*.py")):
        assert "/reviews" not in path.read_text(encoding="utf-8"), path
