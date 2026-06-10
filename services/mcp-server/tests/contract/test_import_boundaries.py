"""mcp-server is self-contained: no cross-service or legacy root-package imports.

The two services share contracts only through docs/contracts/*.md. Any import
of the other service, of the old workspace packages, or of migration tooling
is a boundary violation and must fail here, not at deploy time.
"""

import ast
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]
SRC = SERVICE_ROOT / "src" / "agentic_mcp_server"

FORBIDDEN_PREFIXES = (
    "agentic_kb_builder",  # the other service
    "kb_builder",  # its pre-refactor name
    "mcp_server",  # this service's pre-refactor name
    "common",  # deleted root workspace packages
    "contracts",
    "db",
    "alembic",  # kb-builder owns the schema; mcp-server never runs migrations
    "azure",  # no direct SDK use; a SearchClient interface arrives with PR-10
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_source_never_crosses_the_service_boundary() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for module in sorted(_imported_modules(path)):
            root = module.split(".")[0]
            if root in FORBIDDEN_PREFIXES:
                violations.append(f"{path.relative_to(SERVICE_ROOT)}: imports {module}")
    assert not violations, "boundary violations:\n" + "\n".join(violations)
