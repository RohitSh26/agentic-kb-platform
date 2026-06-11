"""The root docker-compose.yml stays honest about ownership and the V1 footprint.

Compose is the local spin-up of the whole system (PR-17). These pins encode
what the architecture demands of it: only the three sanctioned containers
(no V1-excluded resource can sneak in as a service), kb-builder as the sole
migration runner (ADR-0008), mcp-server starting strictly after the schema
exists, async drivers everywhere, and no credential beyond the documented
compose-internal local default.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

EXPECTED_SERVICES = {"postgres", "kb-builder", "mcp-server"}
# resources excluded from V1 (CLAUDE.md) that a compose service could smuggle in
EXCLUDED_IMAGE_MARKERS = ("redis", "azurite", "neo4j", "rabbitmq", "eventhub", "servicebus")

pytestmark = pytest.mark.skipif(
    not COMPOSE_PATH.is_file(), reason="docker-compose.yml not present in this checkout"
)


def _compose() -> dict[str, object]:
    loaded = yaml.safe_load(COMPOSE_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _services() -> dict[str, dict[str, object]]:
    services = _compose()["services"]
    assert isinstance(services, dict)
    return services


def test_compose_runs_exactly_the_three_sanctioned_containers() -> None:
    services = _services()
    assert set(services) == EXPECTED_SERVICES, "compose service set drifted"
    for name, service in services.items():
        image = str(service.get("image", ""))
        for marker in EXCLUDED_IMAGE_MARKERS:
            assert marker not in image.lower(), f"{name}: V1-excluded resource {marker!r}"


def test_postgres_is_the_only_image_and_the_services_build_from_their_dirs() -> None:
    services = _services()
    assert str(services["postgres"]["image"]).startswith("postgres:16")
    assert services["kb-builder"]["build"] == "./services/kb-builder"
    assert services["mcp-server"]["build"] == "./services/mcp-server"
    assert "image" not in services["kb-builder"] and "image" not in services["mcp-server"]


def test_kb_builder_is_the_only_migration_runner() -> None:
    services = _services()
    # mcp-server: no alembic anywhere — not in compose, not in its image
    assert "alembic" not in str(services["mcp-server"]).lower()
    mcp_dockerfile = (REPO_ROOT / "services" / "mcp-server" / "Dockerfile").read_text()
    assert "alembic" not in mcp_dockerfile.lower(), "mcp-server image must never migrate"
    # kb-builder: the image's default command applies migrations; compose must
    # not override it with something else
    kb_dockerfile = (REPO_ROOT / "services" / "kb-builder" / "Dockerfile").read_text()
    assert '"alembic", "upgrade", "head"' in kb_dockerfile
    command = services["kb-builder"].get("command")
    assert command is None or "alembic" in str(command), "kb-builder command must migrate"
    assert services["kb-builder"].get("restart") == "no", "migration job must be one-shot"


def test_mcp_server_starts_only_after_migrations_complete() -> None:
    depends = _services()["mcp-server"]["depends_on"]
    assert isinstance(depends, dict)
    assert depends["kb-builder"] == {"condition": "service_completed_successfully"}
    assert depends["postgres"] == {"condition": "service_healthy"}


def test_database_urls_use_the_async_driver() -> None:
    for name in ("kb-builder", "mcp-server"):
        env = _services()[name]["environment"]
        assert isinstance(env, dict)
        url = str(env["DATABASE_URL"])
        assert url.startswith("postgresql+asyncpg://"), f"{name}: must use the asyncpg driver"


def test_no_credential_beyond_the_documented_local_default() -> None:
    text = COMPOSE_PATH.read_text()
    # Entra values are identifiers supplied by variable, never inlined tokens
    assert "${MCP_ENTRA_TENANT_ID" in text and "${MCP_ENTRA_AUDIENCE" in text
    for marker in ("ghp_", "github_pat_", "client_secret", "Bearer "):
        assert marker not in text, f"credential marker {marker!r} in compose"
    # the only password is the compose-internal local default (same as CI)
    for line in text.splitlines():
        if "PASSWORD" in line.upper():
            value = line.split(":", 1)[1].split("#", 1)[0].strip()
            assert value == "postgres", f"unexpected credential line: {line.strip()}"
