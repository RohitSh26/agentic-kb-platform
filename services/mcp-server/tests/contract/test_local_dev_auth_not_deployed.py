"""ADR-0016: the opt-in local-dev auth flag must never ship in a deployment artifact.

MCP_LOCAL_DEV_AUTH swaps the Entra verifier for a fixed loopback-only dev identity.
The verifier guardrails already fail closed on a real tenant or a non-loopback bind,
but defense in depth (ADR-0016 Option B, point 5) requires the flag to be ABSENT from
every shipped deployment manifest so it can never be set in an image or infra
definition. This contract test enforces that promise.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FLAG = b"MCP_LOCAL_DEV_AUTH"

_DEPLOYMENT_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "services" / "mcp-server" / "Dockerfile",
]


def _infra_files() -> list[Path]:
    infra = REPO_ROOT / "infra"
    return [p for p in infra.rglob("*") if p.is_file()] if infra.is_dir() else []


@pytest.mark.parametrize("path", _DEPLOYMENT_FILES + _infra_files(), ids=str)
def test_local_dev_auth_flag_absent_from_deployment_artifact(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"{path} not present in this checkout")
    # read_bytes so an unexpected binary under infra/ never trips a decode error.
    assert FLAG not in path.read_bytes(), (
        f"{FLAG.decode()} must not appear in deployment artifact {path} (ADR-0016): "
        "local-dev auth is loopback-only and must never ship in an image or infra def."
    )
