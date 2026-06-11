"""Config loading, env-var token resolution, filtering backend, factory (PR-14)."""

import logging
from pathlib import Path

import pytest

from agentic_kb_builder.connectors import (
    FilteredFetchBackend,
    SourceConfigError,
    connectors_from_config,
    load_source_config,
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
