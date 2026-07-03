"""review-panel is self-contained: no cross-service or registry-store imports.

Services share contracts only through docs/contracts/*.md (ADR-0008). Beyond
the standard boundary set, this service also forbids sqlalchemy/asyncpg — its
ONLY database surface is the LangGraph checkpointer + draft store in the
review_panel schema (psycopg), so registry access is statically impossible,
not just untested.
"""

import ast
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]
SRC = SERVICE_ROOT / "src" / "review_panel"

FORBIDDEN_PREFIXES = (
    "agentic_kb_builder",  # the build-plane service
    "agentic_mcp_server",  # the runtime-plane service
    "kb_builder",
    "mcp_server",
    "common",  # deleted root workspace packages
    "contracts",
    "db",
    "alembic",  # kb-builder owns the schema; this service never migrates it
    "azure",  # no direct Azure SDK use anywhere outside the interfaces
    "redis",  # V1 exclusion (CLAUDE.md); Postgres checkpointer + CLI is the whole runtime
    "sqlalchemy",  # registry access is impossible by construction
    "asyncpg",
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
