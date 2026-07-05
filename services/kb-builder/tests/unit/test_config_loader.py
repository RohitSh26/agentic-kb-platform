"""Config loading, env-var token resolution, filtering backend, factory (PR-14)."""

import logging
from pathlib import Path

import pytest

from agentic_kb_builder.connectors import (
    FilteredFetchBackend,
    SourceConfigError,
    connectors_from_config,
    load_source_config,
    resolve_git_metadata_repo,
    resolve_token,
)
from agentic_kb_builder.connectors.source_connector import FetchBackend
from agentic_kb_builder.domain import PathFilter, SourceConfig, SourceRef, SourceSpec

SECRET = "ghp_super_secret_value"


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(body, encoding="utf-8")
    return path


VALID_YAML = """
version: 1
defaults:
  acl_teams: []
sources:
  - name: code
    type: github_code
    repo: o/r
    include: ["src/**"]
    auth:
      token_env: TEST_GITHUB_TOKEN
  - name: docs
    type: github_doc
    repo: o/r
  - name: disabled-wiki
    type: azure_wiki
    enabled: false
    organization: org
    project: proj
    wiki: w
"""


class FakeBackend:
    def __init__(self, sources: list[SourceRef]) -> None:
        self._sources = sources
        self.fetched: list[str] = []

    async def list_sources(self) -> list[SourceRef]:
        return self._sources

    async def fetch_text(self, source: SourceRef) -> str:
        self.fetched.append(source.source_uri)
        return "text"


def _ref(path: str | None, uri: str | None = None) -> SourceRef:
    return SourceRef(
        source_type="github_code",
        source_uri=uri or f"https://github.com/o/r/blob/sha/{path}",
        source_version="sha",
        repo="o/r",
        path=path,
    )


class TestLoadSourceConfig:
    def test_valid_file_loads(self, tmp_path: Path) -> None:
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        assert [spec.name for spec in config.sources] == ["code", "docs", "disabled-wiki"]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SourceConfigError, match="cannot read"):
            load_source_config(tmp_path / "nope.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SourceConfigError, match="invalid YAML"):
            load_source_config(_write_yaml(tmp_path, "sources: [unclosed"))

    def test_non_mapping_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SourceConfigError, match="top level must be a mapping"):
            load_source_config(_write_yaml(tmp_path, "- just\n- a list\n"))

    def test_validation_error_names_the_source(self, tmp_path: Path) -> None:
        bad = VALID_YAML.replace("repo: o/r\n    include", "repo: bad repo\n    include")
        with pytest.raises(SourceConfigError, match="source 'code'"):
            load_source_config(_write_yaml(tmp_path, bad))

    def test_load_logs_counts_not_tokens(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            load_source_config(_write_yaml(tmp_path, VALID_YAML))
        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "event=source_config_loaded" in joined
        assert "sources=3" in joined and "enabled=2" in joined
        assert "github_code=1" in joined and "azure_wiki=1" in joined


def _spec(config: SourceConfig, name: str) -> SourceSpec:
    return next(spec for spec in config.sources if spec.name == name)


class TestResolveToken:
    def test_present_env_var_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_GITHUB_TOKEN", SECRET)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        assert resolve_token(_spec(config, "code")) == SECRET

    def test_absent_env_var_raises_without_leaking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_GITHUB_TOKEN", raising=False)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        with pytest.raises(SourceConfigError, match="TEST_GITHUB_TOKEN is not set"):
            resolve_token(_spec(config, "code"))

    def test_no_auth_resolves_to_none(self, tmp_path: Path) -> None:
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        assert resolve_token(_spec(config, "docs")) is None

    def test_token_value_never_on_models_or_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("TEST_GITHUB_TOKEN", SECRET)
        with caplog.at_level(logging.DEBUG):
            config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
            connectors_from_config(config, lambda spec, token: FakeBackend([]))
        assert SECRET not in repr(config)
        assert SECRET not in config.model_dump_json()
        assert all(SECRET not in record.getMessage() for record in caplog.records)


class TestFilteredFetchBackend:
    async def test_excluded_paths_never_listed_and_acl_stamped(self) -> None:
        inner = FakeBackend(
            [_ref("src/keep.py"), _ref("src/tests/drop.py"), _ref(None, uri="card:1")]
        )
        backend = FilteredFetchBackend(
            inner,
            PathFilter(["src/**"], ["**/tests/**"]),
            ["team-a"],
            source_name="code",
        )
        kept = await backend.list_sources()
        assert [ref.path for ref in kept] == ["src/keep.py", None]
        assert all(ref.acl_teams == ["team-a"] for ref in kept)

    async def test_fetch_text_delegates(self) -> None:
        inner = FakeBackend([])
        backend = FilteredFetchBackend(inner, PathFilter(), [], source_name="code")
        assert await backend.fetch_text(_ref("a.py")) == "text"
        assert inner.fetched


class TestConnectorsFromConfig:
    def test_factory_receives_spec_and_token_and_disabled_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_GITHUB_TOKEN", SECRET)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        seen: list[tuple[str, str | None]] = []

        def factory(spec: SourceSpec, token: str | None) -> FetchBackend:
            seen.append((spec.name, token))
            return FakeBackend([])

        connectors = connectors_from_config(config, factory)
        assert seen == [("code", SECRET), ("docs", None)]  # disabled-wiki never built
        assert [connector.source_type for connector in connectors] == [
            "github_code",
            "github_doc",
        ]

    async def test_connector_lists_only_included_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_GITHUB_TOKEN", SECRET)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        backend = FakeBackend([_ref("src/a.py"), _ref("docs/readme.md")])
        connectors = connectors_from_config(config, lambda spec, token: backend)
        listed = await connectors[0].list_sources()
        assert [ref.path for ref in listed] == ["src/a.py"]

    def test_missing_token_aborts_before_any_connector_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_GITHUB_TOKEN", raising=False)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        with pytest.raises(SourceConfigError):
            connectors_from_config(config, lambda spec, token: FakeBackend([]))

    def test_authenticates_true_is_the_default_and_still_hard_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Production must keep hard-failing fast on a missing token_env — this is the
        # exact same case as above, asserted again with `authenticates=True` spelled
        # out explicitly so a future default change cannot silently weaken it.
        monkeypatch.delenv("TEST_GITHUB_TOKEN", raising=False)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        with pytest.raises(SourceConfigError, match="TEST_GITHUB_TOKEN is not set"):
            connectors_from_config(config, lambda spec, token: FakeBackend([]), authenticates=True)

    def test_authenticates_false_never_resolves_tokens_and_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The local backend reads workspace files only and never authenticates, so an
        # unset token_env (e.g. GITHUB_TOKEN/ADO_PAT never exported for a local build)
        # must not abort connector construction.
        monkeypatch.delenv("TEST_GITHUB_TOKEN", raising=False)
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        seen: list[tuple[str, str | None]] = []

        def factory(spec: SourceSpec, token: str | None) -> FetchBackend:
            seen.append((spec.name, token))
            return FakeBackend([])

        connectors = connectors_from_config(config, factory, authenticates=False)
        assert seen == [("code", None), ("docs", None)]
        assert [connector.source_type for connector in connectors] == [
            "github_code",
            "github_doc",
        ]

    def test_authenticates_false_also_skips_not_locally_fetchable_source_tokens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `authenticates` and `locally_fetchable_only` are independent flags. In
        # isolation (no `locally_fetchable_only=True` requested), `authenticates=False`
        # alone must still defer token resolution for every enabled source — including
        # azure_wiki/ado_card — without requiring their auth.token_env. The real
        # `--backend local` build path also passes `locally_fetchable_only=True` (see
        # TestLocallyFetchableOnly below), so azure_wiki/ado_card never reach this
        # point there.
        monkeypatch.delenv("TEST_ADO_PAT", raising=False)
        yaml_body = """
version: 1
sources:
  - name: wiki
    type: azure_wiki
    organization: org
    project: proj
    wiki: w
    auth:
      token_env: TEST_ADO_PAT
  - name: cards
    type: ado_card
    organization: org
    project: proj
    auth:
      token_env: TEST_ADO_PAT
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        connectors = connectors_from_config(
            config, lambda spec, token: FakeBackend([]), authenticates=False
        )
        assert [connector.source_type for connector in connectors] == ["azure_wiki", "ado_card"]


MIXED_TYPES_YAML = """
version: 1
sources:
  - name: code
    type: github_code
    repo: o/r
    include: ["src/**"]
  - name: docs
    type: github_doc
    repo: o/r
  - name: wiki
    type: azure_wiki
    organization: org
    project: proj
    wiki: w
  - name: cards
    type: ado_card
    organization: org
    project: proj
"""


class TestLocallyFetchableOnly:
    """`locally_fetchable_only` (default False): the local-filesystem backend can only
    genuinely serve github_code/github_doc — azure_wiki/ado_card have no local
    representation, so its default include-everything PathFilter would otherwise treat
    every workspace file as though it were a wiki page or card (the bug this fixes)."""

    def test_default_false_constructs_every_enabled_type(self, tmp_path: Path) -> None:
        config = load_source_config(_write_yaml(tmp_path, MIXED_TYPES_YAML))
        connectors = connectors_from_config(
            config, lambda spec, token: FakeBackend([]), authenticates=False
        )
        assert [connector.source_type for connector in connectors] == [
            "github_code",
            "github_doc",
            "azure_wiki",
            "ado_card",
        ]

    def test_true_filters_out_non_locally_fetchable_types_and_logs_why(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = load_source_config(_write_yaml(tmp_path, MIXED_TYPES_YAML))
        with caplog.at_level(logging.WARNING):
            connectors = connectors_from_config(
                config,
                lambda spec, token: FakeBackend([]),
                authenticates=False,
                locally_fetchable_only=True,
            )
        assert [connector.source_type for connector in connectors] == [
            "github_code",
            "github_doc",
        ]
        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "event=source_skipped_not_locally_fetchable" in joined
        assert "source=wiki" in joined and "type=azure_wiki" in joined
        assert "source=cards" in joined and "type=ado_card" in joined

    def test_true_never_calls_the_backend_factory_for_a_filtered_source(
        self, tmp_path: Path
    ) -> None:
        config = load_source_config(_write_yaml(tmp_path, MIXED_TYPES_YAML))
        seen: list[str] = []

        def factory(spec: SourceSpec, token: str | None) -> FetchBackend:
            seen.append(spec.name)
            return FakeBackend([])

        connectors_from_config(config, factory, authenticates=False, locally_fetchable_only=True)
        assert seen == ["code", "docs"]  # wiki/cards never reach the backend factory

    def test_true_does_not_require_the_filtered_sources_token_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_ADO_PAT", raising=False)
        yaml_body = """
version: 1
sources:
  - name: code
    type: github_code
    repo: o/r
    include: ["src/**"]
  - name: docs
    type: github_doc
    repo: o/r
  - name: wiki
    type: azure_wiki
    organization: org
    project: proj
    wiki: w
    auth:
      token_env: TEST_ADO_PAT
  - name: cards
    type: ado_card
    organization: org
    project: proj
    auth:
      token_env: TEST_ADO_PAT
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        connectors = connectors_from_config(
            config,
            lambda spec, token: FakeBackend([]),
            authenticates=False,
            locally_fetchable_only=True,
        )
        assert [connector.source_type for connector in connectors] == [
            "github_code",
            "github_doc",
        ]

    def test_disabled_source_is_skipped_before_the_fetchability_check(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        yaml_body = MIXED_TYPES_YAML.replace(
            "  - name: wiki\n    type: azure_wiki\n",
            "  - name: wiki\n    type: azure_wiki\n    enabled: false\n",
        )
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        with caplog.at_level(logging.INFO):
            connectors_from_config(
                config,
                lambda spec, token: FakeBackend([]),
                authenticates=False,
                locally_fetchable_only=True,
            )
        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "event=source_skipped_disabled source=wiki" in joined
        assert "event=source_skipped_not_locally_fetchable source=wiki" not in joined


class TestResolveGitMetadataRepo:
    def test_single_shared_repo_is_derived_automatically(self, tmp_path: Path) -> None:
        # VALID_YAML's "code" and "docs" sources both name repo o/r.
        config = load_source_config(_write_yaml(tmp_path, VALID_YAML))
        assert resolve_git_metadata_repo(config) == "o/r"

    def test_no_github_sources_resolves_to_none(self, tmp_path: Path) -> None:
        yaml_body = """
version: 1
sources:
  - name: wiki
    type: azure_wiki
    organization: org
    project: proj
    wiki: w
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        assert resolve_git_metadata_repo(config) is None

    def test_mixed_repos_with_no_override_resolves_to_none_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        yaml_body = """
version: 1
sources:
  - name: code-a
    type: github_code
    repo: org/repo-a
    include: ["a/**"]
  - name: code-b
    type: github_code
    repo: org/repo-b
    include: ["b/**"]
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        with caplog.at_level(logging.WARNING):
            assert resolve_git_metadata_repo(config) is None
        assert any(
            "event=git_metadata_repo_ambiguous" in record.getMessage() for record in caplog.records
        )

    def test_explicit_override_wins_even_with_a_single_shared_repo(self, tmp_path: Path) -> None:
        yaml_body = """
version: 1
git_metadata:
  repo: org/override
sources:
  - name: code
    type: github_code
    repo: o/r
    include: ["src/**"]
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        assert resolve_git_metadata_repo(config) == "org/override"

    def test_explicit_override_disambiguates_mixed_repos(self, tmp_path: Path) -> None:
        yaml_body = """
version: 1
git_metadata:
  repo: org/repo-a
sources:
  - name: code-a
    type: github_code
    repo: org/repo-a
    include: ["a/**"]
  - name: code-b
    type: github_code
    repo: org/repo-b
    include: ["b/**"]
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        assert resolve_git_metadata_repo(config) == "org/repo-a"

    def test_disabled_sources_do_not_count_toward_derivation(self, tmp_path: Path) -> None:
        yaml_body = """
version: 1
sources:
  - name: code-a
    type: github_code
    repo: org/repo-a
    include: ["a/**"]
  - name: code-b
    type: github_code
    repo: org/repo-b
    include: ["b/**"]
    enabled: false
"""
        config = load_source_config(_write_yaml(tmp_path, yaml_body))
        assert resolve_git_metadata_repo(config) == "org/repo-a"
