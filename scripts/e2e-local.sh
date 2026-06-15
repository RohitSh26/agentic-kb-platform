#!/usr/bin/env bash
# Hermetic local end-to-end demo (dev-guide 06).
#
# Runs the WHOLE platform on your machine with only Postgres + uv — NO Ollama,
# NO Azure, NO Entra. It: builds a tiny KB from this repo's git history (zero-LLM
# git_metadata commits), serves it through the MCP Context Broker on loopback with
# opt-in dev auth (ADR-0016), then drives the five broker tools as a real agent
# would and tears the server down. Exits non-zero if any stage fails.
#
#   ./scripts/e2e-local.sh            # full loop: build + serve + smoke
#   SKIP_BUILD=1 ./scripts/e2e-local.sh   # reuse the existing demo KB, just serve+smoke
#
# Override DEMO_DB / PGHOST / PGPORT / PORT via env if your Postgres differs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DB="${DEMO_DB:-agentic_kb_demo}"
PGHOST="${PGHOST:-localhost}"; PGPORT="${PGPORT:-5432}"; PGUSER="${PGUSER:-$USER}"
PORT="${PORT:-8765}"
INDEX_PATH="${INDEX_PATH:-/tmp/agentic-kb-demo-index.json}"
DB_URL="postgresql+asyncpg://${PGUSER}@${PGHOST}:${PGPORT}/${DEMO_DB}"
SOURCES="${REPO_ROOT}/scripts/demo-sources.yaml"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

command -v uv >/dev/null || { echo "uv not found — see dev-guide 06 §Prerequisites"; exit 1; }
pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null 2>&1 || {
  echo "Postgres not reachable at ${PGHOST}:${PGPORT}. Start it (e.g. brew services start postgresql) and retry."; exit 1; }
# Refuse to run next to a stale server on the port: otherwise our new server fails
# to bind while the smoke client silently talks to the OLD one (wrong KB/budget).
if lsof -ti ":${PORT}" >/dev/null 2>&1; then
  echo "Port ${PORT} is already in use (a previous server?). Free it: 'lsof -ti :${PORT} | xargs kill', or set PORT=<other>."; exit 1
fi

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  say "1/4  (Re)create the demo database  [$DEMO_DB]"
  dropdb --if-exists --force "$DEMO_DB" 2>/dev/null || true
  createdb "$DEMO_DB"

  say "2/4  Migrate the schema + build a KB from git history (zero-LLM)"
  ( cd "$REPO_ROOT/services/kb-builder"
    DATABASE_URL="$DB_URL" uv run alembic upgrade head >/dev/null
    # GITHUB_TOKEN is unused by the local-FS backend; the demo source matches no
    # files (see demo-sources.yaml), so the build is git_metadata-only and zero-LLM.
    GITHUB_TOKEN="local-demo-unused" DATABASE_URL="$DB_URL" \
      uv run python -m agentic_kb_builder.build \
        --workspace "$REPO_ROOT" --sources "$SOURCES" --index-path "$INDEX_PATH" )
else
  say "1-2/4  SKIP_BUILD=1 — reusing the existing KB in [$DEMO_DB]"
fi

say "3/4  Start the MCP server (loopback, dev auth) and wait for /health"
SERVER_LOG="$(mktemp -t agentic-mcp-demo.XXXX.log)"
( cd "$REPO_ROOT/services/mcp-server"
  DATABASE_URL="$DB_URL" \
  MCP_ENTRA_TENANT_ID="00000000-0000-0000-0000-000000000000" \
  MCP_ENTRA_AUDIENCE="api://agentic-kb-demo" \
  MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT="$PORT" \
  MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 20, "max_tokens": 50000}}' \
  uv run python -m agentic_mcp_server >"$SERVER_LOG" 2>&1 ) &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null)" = "200" ] && break
  sleep 1
done
if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null)" != "200" ]; then
  echo "server did not become healthy; log:"; tail -20 "$SERVER_LOG"; exit 1
fi
echo "  health: 200 OK  (http://127.0.0.1:${PORT}/)"

say "4/4  Drive the broker tools as an agent would"
( cd "$REPO_ROOT/services/mcp-server"
  MCP_URL="http://127.0.0.1:${PORT}/mcp/" uv run python "$REPO_ROOT/scripts/smoke_client.py" )

say "DONE — built a KB, served it, and exercised the full tool path locally."
echo "Server log: $SERVER_LOG   ·   Re-run server-only: SKIP_BUILD=1 ./scripts/e2e-local.sh"
