"""Schema validation matrix + glob semantics for sources.yaml (PR-14)."""

import pytest
from pydantic import ValidationError

from agentic_kb_builder.domain import GlobError, PathFilter, SourceConfig
from agentic_kb_builder.domain.source_config import glob_to_regex


def _config(sources: list[dict[str, object]], **top: object) -> dict[str, object]:
    return {"version": 1, "sources": sources, **top}


def _github(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": "code",
        "type": "github_code",
        "repo": "o/r",
    }
    spec.update(overrides)
    return spec


class TestValidationMatrix:
    def test_valid_config_loads_typed_specs(self) -> None:
        config = SourceConfig.model_validate(
            _config(
                [
                    _github(include=["src/**"], exclude=["**/tests/**"]),
                    {
                        "name": "cards",
                        "type": "ado_card",
                        "organization": "org",
                        "project": "proj",
                        "tags": ["kb"],
                    },
                ]
            )
        )
        assert config.sources[0].type == "github_code"
        assert config.sources[0].branch == "main"
        assert config.sources[1].type == "ado_card"
        assert config.sources[1].enabled is True

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(type="gitlab_code")]))

    def test_duplicate_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate source name"):
            SourceConfig.model_validate(_config([_github(), _github()]))

    def test_bad_repo_format_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(repo="not-owner-slash-name")]))

    @pytest.mark.parametrize(
        "overrides",
        [
            {"organization": "bad/org"},  # slash → path reshape
            {"organization": "evil.com"},  # dot not allowed in org
            {"project": "proj/../other"},  # slash/traversal
            {"project": "has?query"},  # url metachar
            {"area_path": "a/b"},  # forward slash not an ADO area separator
            {"area_path": "Area'; DROP"},  # quote/metachars (defense beyond WIQL escaping)
        ],
    )
    def test_bad_ado_fields_rejected(self, overrides: dict[str, object]) -> None:
        spec: dict[str, object] = {
            "name": "cards",
            "type": "ado_card",
            "organization": "org",
            "project": "proj",
        }
        spec.update(overrides)
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([spec]))

    def test_lowercase_token_env_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(auth={"token_env": "github_token"})]))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(includ=["src/**"])]))

    def test_token_value_shape_is_unrepresentable(self) -> None:
        # a PAT-looking string never matches the env-var-name pattern
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(auth={"token_env": "ghp_abc123secret"})]))

    def test_wrong_version_rejected(self) -> None:
        config = _config([_github()])
        config["version"] = 2
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(config)

    def test_empty_sources_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([]))

    def test_bad_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(name="Bad Name!")]))

    def test_invalid_glob_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github(include=["a**b"])]))

    def test_defaults_acl_teams_inherited_unless_overridden(self) -> None:
        config = SourceConfig.model_validate(
            _config(
                [
                    _github(name="inherits"),
                    _github(name="overrides", acl_teams=["secure-team"]),
                    _github(name="explicit-public", acl_teams=[]),
                ],
                defaults={"acl_teams": ["org-team"]},
            )
        )
        by_name = {spec.name: spec.acl_teams for spec in config.sources}
        assert by_name == {
            "inherits": ["org-team"],
            "overrides": ["secure-team"],
            "explicit-public": [],
        }

    def test_specs_are_frozen(self) -> None:
        config = SourceConfig.model_validate(_config([_github()]))
        with pytest.raises(ValidationError):
            config.sources[0].repo = "x/y"  # type: ignore[misc]

    def test_git_metadata_repo_defaults_to_none(self) -> None:
        config = SourceConfig.model_validate(_config([_github()]))
        assert config.git_metadata is None

    def test_git_metadata_repo_parses(self) -> None:
        config = SourceConfig.model_validate(
            _config([_github()], git_metadata={"repo": "o/r"})
        )
        assert config.git_metadata is not None
        assert config.git_metadata.repo == "o/r"

    def test_git_metadata_bad_repo_format_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(
                _config([_github()], git_metadata={"repo": "not-owner-slash-name"})
            )

    def test_git_metadata_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(_config([_github()], git_metadata={"branch": "main"}))


GLOB_CASES = [
    # (include, exclude, path, expected)
    (["**"], [], "anything/at/all.txt", True),  # default include matches everything
    (["services/**/*.py"], [], "services/a.py", True),  # ** spans zero segments
    (["services/**/*.py"], [], "services/x/y/a.py", True),  # ** spans many segments
    (["services/**/*.py"], [], "docs/x.py", False),
    (["services/**/*.py"], [], "services/a.pyc", False),  # literal suffix is exact
    (["*.md"], [], "README.md", True),  # * stays within one segment
    (["*.md"], [], "docs/intro.md", False),
    (["services/**"], [], "services", True),  # trailing ** includes the base itself
    (["services/**"], [], "services/a/b", True),
    (["services/**"], [], "service/a", False),
    (["**/tests/**"], [], "tests/x.py", True),  # leading ** spans zero segments
    (["**/tests/**"], [], "a/b/tests/x.py", True),
    (["**/tests/**"], [], "a/latests/x.py", False),  # segment boundary respected
    (["a/?.py"], [], "a/b.py", True),  # ? is exactly one non-/ char
    (["a/?.py"], [], "a/bc.py", False),
    (["a/?.py"], [], "a/.py", False),
    (["src/**", "docs/**"], [], "docs/x.md", True),  # any include suffices
    (["**/**"], [], "a", True),  # consecutive ** collapses to one
    (["**/**"], [], "a/b/c", True),
    (["a/**/**"], [], "a", True),
    (["a/**/**/b"], [], "a/b", True),
    (["a/**/**/b"], [], "a/x/y/b", True),
    (["**"], ["**/tests/**"], "a/tests/x.py", False),  # exclude wins
    (["**/tests/**"], ["**/tests/**"], "tests/x.py", False),  # exclude beats include
    (["services/**/*.py"], ["**/tests/**"], "services/a/tests/b.py", False),
]


@pytest.mark.parametrize(("include", "exclude", "path", "expected"), GLOB_CASES)
def test_glob_semantics(include: list[str], exclude: list[str], path: str, expected: bool) -> None:
    assert PathFilter(include, exclude).matches(path) is expected


@pytest.mark.parametrize("pattern", ["", "/leading/slash", "a//b", "trailing/", "a**b", "x/a**/b"])
def test_malformed_globs_raise(pattern: str) -> None:
    with pytest.raises(GlobError):
        glob_to_regex(pattern)
