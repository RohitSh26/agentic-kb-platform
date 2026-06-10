# Rule: Python style + stack (all src/)

- Python 3.12, managed with uv. Format/lint with ruff; type-check with pyright (strict on each
  service's domain, infrastructure, and tool-schema packages).
- Async-first: asyncpg + SQLAlchemy 2.x async, fastmcp async tools, pytest-asyncio for tests.
- External systems sit behind interfaces: SearchClient (Azure AI Search) and ModelClient (Azure
  OpenAI). Tools and builders depend on the interface, never the SDK directly — this keeps Search a
  swappable projection and makes tests hermetic.
- Structured logging (key=value or JSON) on every build and retrieval path. No bare prints.
- No secrets in code, fixtures, or logs. Prefer managed identity; Key Vault for the remainder.
- Keep modules small and single-purpose; respect the service boundary (services never import each
  other or root packages — docs/contracts/ markdown is the only shared interface; duplicate small
  DTOs instead of sharing code).
