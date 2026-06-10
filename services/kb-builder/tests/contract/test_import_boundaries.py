"""kb-builder is self-contained: no cross-service or legacy root-package imports.

The two services share contracts only through docs/contracts/*.md. Any import
of the other service, of the old workspace packages, or of MCP runtime
libraries is a boundary violation and must fail here, not at deploy time.
"""

import ast
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]
SRC = SERVICE_ROOT / "src" / "agentic_kb_builder"
MIGRATIONS = SERVICE_ROOT / "migrations"

FORBIDDEN_PREFIXES = (
    "agentic_mcp_server",  # the other service
    "mcp_server",  # its pre-refactor name
    "kb_builder",  # this service's pre-refactor name
    "common",  # deleted root workspace packages
    "contracts",
    "db",
    "fastmcp",  # MCP runtime libraries: build plane never serves MCP
    "mcp",
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


def test_source_and_migrations_never_cross_the_service_boundary() -> None:
    violations: list[str] = []
    for path in sorted([*SRC.rglob("*.py"), *MIGRATIONS.rglob("*.py")]):
        for module in sorted(_imported_modules(path)):
            root = module.split(".")[0]
            if root in FORBIDDEN_PREFIXES:
                violations.append(f"{path.relative_to(SERVICE_ROOT)}: imports {module}")
    assert not violations, "boundary violations:\n" + "\n".join(violations)
