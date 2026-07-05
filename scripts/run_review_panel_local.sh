#!/usr/bin/env bash
# Compute or fetch the review-panel DRAFT for a pull request (ADR-0031: the
# panel never publishes — the developer reads, edits, and publishes from their
# own session).
#
#   scripts/run_review_panel_local.sh <owner/repo> <pr-number>
#
# Wraps `uv run review-panel draft`. LLM creds (LLM_PROVIDER/LLM_MODEL/
# LLM_API_KEY) are needed only when no stored draft exists for the current
# head SHA. Optional: GITHUB_TOKEN (read-only PR fetch for private repos),
# REVIEW_PANEL_DATABASE_URL (durable checkpoints + draft store),
# REVIEW_PANEL_MCP_URL for kb_search, TRACE_SINK=postgres for tracing (ADR-0032).
# Secrets stay in env / .env — never on the command line. The draft JSON is
# printed on stdout; logs go to stderr.
set -euo pipefail

REPO="${1:?usage: run_review_panel_local.sh <owner/repo> <pr-number>}"
PR="${2:?usage: run_review_panel_local.sh <owner/repo> <pr-number>}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# repo-root .env (shell env wins)
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

export REVIEW_PANEL_AGENTS_DIR="${REVIEW_PANEL_AGENTS_DIR:-$ROOT/agents}"

cd "$ROOT/services/review-panel"
exec uv run review-panel draft "$REPO" "$PR"
