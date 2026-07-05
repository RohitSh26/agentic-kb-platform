"""Backend-driven connector selection at build construction time.

Bug fix: under `--backend local`, `config_validator` warned that `ado_card`/`azure_wiki`
sources "will be skipped", but nothing filtered them at runtime — `connectors_from_config`
still built a connector for them, and the local-filesystem backend's default (matches
"**") `PathFilter` then matched every workspace file, so an `ado_card` source docified
arbitrary files as fake "cards".

These tests reproduce the exact composition `run_build` performs BEFORE any DB write —
`connectors_from_config` plus the conditional `GitMetadataConnector` append — against the
real `sources.example.yaml`, so a `--backend local` build genuinely constructs only the
source types it can serve, and a `--backend production` build is unaffected.
"""

import logging
from pathlib import Path

import pytest

from agentic_kb_builder.connectors import (
    Connector,
    GitMetadataConnector,
    connectors_from_config,
    load_source_config,
    resolve_git_metadata_repo,
)
from agentic_kb_builder.connectors.local_fs import local_fs_backend_factory
from agentic_kb_builder.connectors.production_factory import production_backend_factory

SERVICE_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = SERVICE_ROOT / "sources.example.yaml"


def _connector_types(connectors: list[Connector]) -> list[str]:
    return [connector.source_type for connector in connectors]


class TestLocalBackendConnectorSelection:
    def test_only_locally_fetchable_types_are_constructed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = load_source_config(EXAMPLE)
        factory = local_fs_backend_factory(tmp_path, version="local")
        with caplog.at_level(logging.WARNING):
            connectors = connectors_from_config(
                config, factory, authenticates=False, locally_fetchable_only=True
            )
        # platform-wiki (azure_wiki) and roadmap-cards (ado_card) are absent.
        assert _connector_types(connectors) == ["github_code", "github_doc"]
        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "event=source_skipped_not_locally_fetchable" in joined
        assert "source=platform-wiki" in joined and "type=azure_wiki" in joined
        assert "source=roadmap-cards" in joined and "type=ado_card" in joined

    def test_no_token_env_required_for_the_skipped_sources(self, tmp_path: Path) -> None:
        # platform-wiki/roadmap-cards configure auth.token_env: ADO_PAT — filtered out
        # before token resolution, so an unset ADO_PAT must not abort construction.
        config = load_source_config(EXAMPLE)
        factory = local_fs_backend_factory(tmp_path, version="local")
        connectors = connectors_from_config(
            config, factory, authenticates=False, locally_fetchable_only=True
        )
        assert len(connectors) == 2

    def test_git_metadata_connector_is_still_constructible_alongside(self, tmp_path: Path) -> None:
        # git_metadata has no `sources:` entry — build.py appends it unconditionally,
        # for both backends — so this proves the local build still gets a real
        # git_metadata connector alongside the two fetchable github connectors.
        config = load_source_config(EXAMPLE)
        repo = resolve_git_metadata_repo(config)
        assert repo == "RohitSh26/agentic-kb-platform"
        connector = GitMetadataConnector(tmp_path, repo=repo)
        assert connector.source_type == "git_metadata"


class TestProductionBackendConnectorSelection:
    def test_all_four_source_types_are_constructed_when_tokens_are_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.setenv("ADO_PAT", "pat_x")
        config = load_source_config(EXAMPLE)
        factory = production_backend_factory()
        connectors = connectors_from_config(
            config, factory, authenticates=True, locally_fetchable_only=False
        )
        assert _connector_types(connectors) == [
            "github_code",
            "github_doc",
            "azure_wiki",
            "ado_card",
        ]


class TestRegressionAdoCardNeverProducesWorkspaceFiles:
    def test_ado_card_under_local_backend_is_never_constructed(self, tmp_path: Path) -> None:
        # The exact failure shape this fixes: LocalFsBackend enumerates EVERY workspace
        # file for ANY spec (it has no notion of "card" vs "code"), and an ado_card spec
        # carries no include/exclude filter at all — unfixed, its connector would list
        # every one of these files as a fake "card" SourceRef.
        for name in ("a.py", "b.md", "c.txt"):
            (tmp_path / name).write_text("content", encoding="utf-8")
        sources_path = tmp_path / "sources.yaml"
        sources_path.write_text(
            "version: 1\n"
            "sources:\n"
            "  - name: cards\n"
            "    type: ado_card\n"
            "    organization: contoso\n"
            "    project: platform\n",
            encoding="utf-8",
        )
        config = load_source_config(sources_path)
        factory = local_fs_backend_factory(tmp_path, version="local")

        connectors = connectors_from_config(
            config, factory, authenticates=False, locally_fetchable_only=True
        )

        assert connectors == []  # never constructed => zero SourceRefs, guaranteed
