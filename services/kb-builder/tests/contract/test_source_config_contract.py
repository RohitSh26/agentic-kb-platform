"""Pins sources.example.yaml to the schema in docs/contracts/source-config.md.

If the example, the contract document, or the pydantic models drift apart,
this test fails — the example can never rot.
"""

from pathlib import Path

from agentic_kb_builder.connectors import load_source_config
from agentic_kb_builder.domain import (
    AdoCardSourceSpec,
    AzureWikiSourceSpec,
    GithubCodeSourceSpec,
    GithubDocSourceSpec,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = SERVICE_ROOT / "sources.example.yaml"


def test_example_yaml_validates_against_the_schema() -> None:
    config = load_source_config(EXAMPLE)
    assert config.version == 1
    assert config.defaults.acl_teams == []
    assert config.git_metadata is not None
    assert config.git_metadata.repo == "RohitSh26/agentic-kb-platform"
    assert [spec.name for spec in config.sources] == [
        "platform-code",
        "platform-docs",
        "platform-wiki",
        "roadmap-cards",
    ]


def test_example_covers_every_source_type_documented_in_the_contract() -> None:
    config = load_source_config(EXAMPLE)
    types = {type(spec) for spec in config.sources}
    assert types == {
        GithubCodeSourceSpec,
        GithubDocSourceSpec,
        AzureWikiSourceSpec,
        AdoCardSourceSpec,
    }


def test_example_pins_documented_field_semantics() -> None:
    config = load_source_config(EXAMPLE)
    by_name = {spec.name: spec for spec in config.sources}

    code = by_name["platform-code"]
    assert isinstance(code, GithubCodeSourceSpec)
    assert code.repo == "RohitSh26/agentic-kb-platform"
    assert code.branch == "main"
    assert code.include == ["services/**/*.py"]
    assert code.exclude == ["**/tests/**"]
    assert code.auth is not None and code.auth.token_env == "GITHUB_TOKEN"

    docs = by_name["platform-docs"]
    assert isinstance(docs, GithubDocSourceSpec)
    # The example points at a PRIVATE repo, so every github source must send a token —
    # an auth-less source 404s on a private repo. (auth-optional is representable in the
    # schema and is covered by the loader unit tests, not by this private-repo example.)
    assert docs.auth is not None and docs.auth.token_env == "GITHUB_TOKEN"
    assert docs.exclude == []  # default: exclude nothing

    wiki = by_name["platform-wiki"]
    assert isinstance(wiki, AzureWikiSourceSpec)
    assert (wiki.organization, wiki.project, wiki.wiki) == (
        "contoso",
        "platform",
        "platform.wiki",
    )
    assert wiki.acl_teams == ["platform-eng"]

    cards = by_name["roadmap-cards"]
    assert isinstance(cards, AdoCardSourceSpec)
    assert cards.area_path == "Platform\\KB"
    assert cards.work_item_types == ["User Story", "Bug"]
    assert cards.states == ["Active", "Resolved", "Closed"]
    assert cards.tags == []


def test_example_never_contains_a_token_value() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    # only env-var NAMES may appear; common PAT shapes must not
    for marker in ("ghp_", "github_pat_", "Bearer ", "AZDO", "secret"):
        assert marker not in text


def test_contract_document_exists_and_declares_version_1() -> None:
    contract = SERVICE_ROOT.parents[1] / "docs" / "contracts" / "source-config.md"
    text = contract.read_text(encoding="utf-8")
    assert "version: 1" in text
    assert "token_env" in text
