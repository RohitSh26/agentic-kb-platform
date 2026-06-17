"""Pre-flight config validation: catch auth/token/path mistakes before any fetch."""

from pathlib import Path

from agentic_kb_builder.connectors.config_validator import (
    Severity,
    has_errors,
    validate_source_config,
)
from agentic_kb_builder.domain.source_config import (
    AdoCardSourceSpec,
    AuthRef,
    GithubCodeSourceSpec,
    GithubDocSourceSpec,
    SourceConfig,
)


def _config(*specs: object) -> SourceConfig:
    return SourceConfig.model_validate({"version": 1, "sources": list(specs)})


def _github_code(name: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": name,
        "type": "github_code",
        "repo": "owner/repo",
        "branch": "main",
        "include": ["services/**/*.py"],
    }
    base.update(over)
    return base


# --- production: auth + tokens -------------------------------------------------


def test_production_authless_source_is_an_error() -> None:
    config = _config(_github_code("docs", include=["docs/**/*.md"]))  # no auth, not public
    issues = validate_source_config(config, backend="production", environ={})
    assert has_errors(issues)
    assert any("no auth.token_env" in i.message and i.source == "docs" for i in issues)


def test_production_authless_but_public_is_allowed() -> None:
    config = _config(_github_code("public-docs", include=["docs/**/*.md"], public=True))
    issues = validate_source_config(config, backend="production", environ={})
    assert not has_errors(issues)


def test_production_missing_token_env_is_an_error() -> None:
    config = _config(_github_code("code", auth={"token_env": "GITHUB_TOKEN"}))
    issues = validate_source_config(config, backend="production", environ={})  # var unset
    assert has_errors(issues)
    assert any("GITHUB_TOKEN is not set" in i.message for i in issues)


def test_production_token_present_is_clean() -> None:
    config = _config(_github_code("code", auth={"token_env": "GITHUB_TOKEN"}))
    issues = validate_source_config(config, backend="production", environ={"GITHUB_TOKEN": "ghp_x"})
    assert issues == []


def test_production_public_with_auth_warns() -> None:
    config = _config(_github_code("code", auth={"token_env": "GITHUB_TOKEN"}, public=True))
    issues = validate_source_config(config, backend="production", environ={"GITHUB_TOKEN": "ghp_x"})
    assert not has_errors(issues)
    assert any(i.severity is Severity.WARNING and "public" in i.message for i in issues)


def test_production_reports_all_issues_at_once() -> None:
    config = _config(
        _github_code("a", include=["docs/**/*.md"]),  # authless error
        _github_code("b", auth={"token_env": "MISSING_TOKEN"}),  # missing-token error
    )
    issues = validate_source_config(config, backend="production", environ={})
    error_sources = {i.source for i in issues if i.severity is Severity.ERROR}
    assert error_sources == {"a", "b"}  # both surfaced, not just the first


# --- local: workspace + paths + unsupported types ------------------------------


def test_local_missing_workspace_is_an_error() -> None:
    config = _config(_github_code("code"))
    issues = validate_source_config(
        config, backend="local", environ={}, workspace=Path("/no/such/dir")
    )
    assert has_errors(issues)
    assert any("workspace path does not exist" in i.message for i in issues)


def test_local_ignores_missing_tokens(tmp_path: Path) -> None:
    # auth-less in local mode is fine — tokens are not used by the local backend
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "m.py").write_text("x = 1\n")
    config = _config(_github_code("code"))
    issues = validate_source_config(config, backend="local", environ={}, workspace=tmp_path)
    assert not has_errors(issues)


def test_local_flags_unsupported_source_type(tmp_path: Path) -> None:
    cards = {
        "name": "cards",
        "type": "ado_card",
        "organization": "contoso",
        "project": "platform",
        "auth": {"token_env": "ADO_PAT"},
    }
    config = _config(cards)
    issues = validate_source_config(config, backend="local", environ={}, workspace=tmp_path)
    assert any(
        i.severity is Severity.WARNING and "not fetchable by --backend local" in i.message
        for i in issues
    )


def test_local_warns_when_globs_match_no_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hi\n")  # exists, but no .py under services/
    config = _config(_github_code("code", include=["services/**/*.py"]))
    issues = validate_source_config(config, backend="local", environ={}, workspace=tmp_path)
    assert any(i.severity is Severity.WARNING and "match no files" in i.message for i in issues)


def test_local_clean_when_globs_match(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "m.py").write_text("x = 1\n")
    config = _config(_github_code("code", include=["services/**/*.py"]))
    issues = validate_source_config(config, backend="local", environ={}, workspace=tmp_path)
    assert issues == []


def test_disabled_source_is_skipped() -> None:
    config = _config(_github_code("off", include=["docs/**/*.md"], enabled=False))
    issues = validate_source_config(config, backend="production", environ={})
    assert issues == []  # no auth error for a disabled source


# keep the unused imports meaningful: build a doc + ado spec via the models too
def test_specs_construct() -> None:
    assert GithubDocSourceSpec(name="d", type="github_doc", repo="o/r").auth is None
    assert (
        AdoCardSourceSpec(
            name="c",
            type="ado_card",
            organization="o",
            project="p",
            auth=AuthRef(token_env="ADO_PAT"),
        ).public
        is False
    )
    assert GithubCodeSourceSpec(name="x", type="github_code", repo="o/r").public is False
