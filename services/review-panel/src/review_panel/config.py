"""Panel configuration, read from the environment.

Identifiers and references only — secret VALUES arrive via env at runtime
(a local .env or the caller's shell) and are never logged. Model settings are
deliberately NOT loaded here: returning a stored draft must work with no LLM
credentials, so the CLI loads them only when it actually has to compute.
"""

import os
from dataclasses import dataclass
from pathlib import Path

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _default_agents_dir() -> Path:
    # services/review-panel/src/review_panel/config.py -> repo checkout root /agents
    return Path(__file__).resolve().parents[4] / "agents"


@dataclass(frozen=True)
class PanelConfig:
    #: optional READ-ONLY token (private repos / rate limits); never a write credential
    github_token: str
    github_api_url: str
    agents_dir: Path
    database_url: str | None
    mcp_url: str | None
    mcp_token: str | None
    langsmith_tracing: bool


def load_config() -> PanelConfig:
    agents_dir = os.environ.get("REVIEW_PANEL_AGENTS_DIR", "")
    return PanelConfig(
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        agents_dir=Path(agents_dir) if agents_dir else _default_agents_dir(),
        database_url=os.environ.get("REVIEW_PANEL_DATABASE_URL") or None,
        mcp_url=os.environ.get("REVIEW_PANEL_MCP_URL") or None,
        mcp_token=os.environ.get("REVIEW_PANEL_MCP_TOKEN") or None,
        # LangGraph/LangSmith pick the env up natively; surfaced here for the boot log only
        langsmith_tracing=env_flag("LANGSMITH_TRACING") or env_flag("LANGCHAIN_TRACING_V2"),
    )
