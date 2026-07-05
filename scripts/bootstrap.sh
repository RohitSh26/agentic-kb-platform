#!/usr/bin/env bash
# scripts/bootstrap.sh — fresh-machine onboarding (docs/dev-guide/00-quickstart.md).
#
# One command, ~2-3 minutes: fresh clone -> synced dependencies -> a migrated Postgres
# database -> an ACTIVE, queryable knowledge base -> a real zero-LLM retrieval check ->
# printed next steps to serve it and connect a host. No cloud accounts, no API keys, no
# tokens on the default path. Idempotent and safely re-runnable (skips what already exists,
# and the build itself is incremental — see .claude/rules/connectors.md).
#
# Usage:
#   ./scripts/bootstrap.sh                                # default database `agentic_kb`
#   ./scripts/bootstrap.sh --db-name agentic_kb_dev        # a different database name
#   DB_NAME=agentic_kb_dev ./scripts/bootstrap.sh          # same, via env var
#   ./scripts/bootstrap.sh --with-docs                     # + doc summaries (needs an LLM key)
#
# Env overrides (all optional): DB_NAME, PGHOST, PGPORT, PGUSER.
#
# What it does NOT do: touch services/ source code, read or print any secret, or require
# network access / credentials on the default path. See docs/dev-guide/00-quickstart.md for
# the narrated walkthrough and troubleshooting table.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="${DB_NAME:-agentic_kb}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-$USER}"
WITH_DOCS=0
MCP_PORT="${MCP_PORT:-8765}"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --db-name)
      [ $# -ge 2 ] || { echo "ERROR: --db-name needs a value" >&2; exit 2; }
      DB_NAME="$2"
      shift 2
      ;;
    --db-name=*)
      DB_NAME="${1#--db-name=}"
      shift
      ;;
    --with-docs)
      WITH_DOCS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '$1' (see --help)" >&2
      exit 2
      ;;
  esac
done

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

DB_URL="postgresql+asyncpg://${PGUSER}@${PGHOST}:${PGPORT}/${DB_NAME}"

# ---------------------------------------------------------------------------
say "1/5  Preflight checks"
# ---------------------------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git not found. Install it (Xcode Command Line Tools on macOS: xcode-select --install), then re-run."
info "git       — OK ($(git --version))"

command -v uv >/dev/null 2>&1 || die "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh — then open a NEW terminal (the installer edits your shell profile) and re-run."
info "uv        — OK ($(uv --version))"

if uv python find 3.12 >/dev/null 2>&1; then
  info "python3.12 — OK (already available to uv)"
else
  info "python3.12 — not found yet; uv will download it automatically during 'uv sync' below (one-time, needs network)."
fi

command -v psql >/dev/null 2>&1 || die "psql not found — the Postgres client tools aren't on your PATH. macOS: brew install postgresql@16."
info "psql      — OK ($(psql --version))"

pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null 2>&1 \
  || die "Postgres isn't reachable at ${PGHOST}:${PGPORT}. Start it (brew services start postgresql@16, or: docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16) and re-run. Override the host/port with PGHOST/PGPORT if yours differs."
info "postgres  — OK (reachable at ${PGHOST}:${PGPORT})"

# ---------------------------------------------------------------------------
say "2/5  Install dependencies (uv sync — kb-builder, mcp-server, review-panel, evals)"
# ---------------------------------------------------------------------------
for proj in services/kb-builder services/mcp-server services/review-panel evals; do
  info "-- ${proj}"
  ( cd "$REPO_ROOT/$proj" && uv sync ) || die "'uv sync' failed in ${proj} — see the error above. A clean retry: rm -rf ${proj}/.venv && ./scripts/bootstrap.sh"
done

# ---------------------------------------------------------------------------
say "3/5  Create + migrate the database [$DB_NAME]"
# ---------------------------------------------------------------------------
if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -lqt 2>/dev/null | cut -d'|' -f1 | tr -d ' ' | grep -qx "$DB_NAME"; then
  info "database '${DB_NAME}' already exists — skipping createdb"
else
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$DB_NAME" \
    || die "createdb failed. If your Postgres role isn't '${PGUSER}', pass it via PGUSER=<role>, e.g.: PGUSER=postgres ./scripts/bootstrap.sh"
  info "created database '${DB_NAME}'"
fi

( cd "$REPO_ROOT/services/kb-builder" && DATABASE_URL="$DB_URL" uv run alembic upgrade head ) \
  || die "'alembic upgrade head' failed — see the output above. Migrations are idempotent, so a clean retry is just: ./scripts/bootstrap.sh --db-name ${DB_NAME}"
info "schema is at head"

# ---------------------------------------------------------------------------
say "4/5  Build the knowledge base (code + commits + aliases — zero LLM, ~1 minute)"
# ---------------------------------------------------------------------------
BUILD_LOG="$(mktemp -t agentic-kb-bootstrap-build.XXXX.log)"
if ( cd "$REPO_ROOT/services/kb-builder" && DATABASE_URL="$DB_URL" uv run python -m agentic_kb_builder.build \
       --backend local --workspace "$REPO_ROOT" \
       --sources "$REPO_ROOT/scripts/local-code-sources.yaml" ) >"$BUILD_LOG" 2>&1
then
  tail -n 5 "$BUILD_LOG" | sed 's/^/  /'
else
  info "build did not complete — tail of the log ($BUILD_LOG):"
  tail -n 30 "$BUILD_LOG" | sed 's/^/  /'
  die "the local build failed. Common cause: the database wasn't migrated (re-run this script) — full log at $BUILD_LOG"
fi
grep -q "^active version: local" "$BUILD_LOG" \
  || die "build finished but did not activate a kb_version — full log at $BUILD_LOG"

# ---------------------------------------------------------------------------
say "5/5  Smoke-verify: an active kb_version + a real, zero-LLM retrieval check"
# ---------------------------------------------------------------------------
ACTIVE_KB="$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DB_NAME" -tAc \
  "select kb_version from kb_build_run where status = 'active' order by build_seq desc limit 1;")"
[ -n "$ACTIVE_KB" ] || die "no active kb_version found in '${DB_NAME}' after the build — full log at $BUILD_LOG"
info "active kb_version: ${ACTIVE_KB}"

info "running the alias-resolution retrieval check (real kb_search-path code, zero LLM)..."
( cd "$REPO_ROOT/services/kb-builder" && DATABASE_URL="$DB_URL" uv run python "$REPO_ROOT/scripts/eval_alias_resolution.py" ) \
  || die "the KB built and activated, but the alias-resolution retrieval check failed — see output above. This exercises the same alias_reference table the MCP server's kb_search/get_task_context path reads (services/mcp-server/.../task_context_nodes.py)."

# ---------------------------------------------------------------------------
if [ "$WITH_DOCS" = "1" ]; then
  say "Optional: full build with doc summaries (--with-docs)"
  if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
  fi
  if [ -n "${LLM_API_KEY:-}" ] && [ -n "${LLM_PROVIDER:-}" ]; then
    info "LLM credentials found (provider=${LLM_PROVIDER}) — running the full build (code + commits + aliases + doc summaries)..."
    DOCS_LOG="$(mktemp -t agentic-kb-bootstrap-docs-build.XXXX.log)"
    if ( cd "$REPO_ROOT/services/kb-builder" && DATABASE_URL="$DB_URL" uv run python -m agentic_kb_builder.build \
           --backend local --workspace "$REPO_ROOT" \
           --sources "$REPO_ROOT/scripts/local-code-and-docs-sources.yaml" ) >"$DOCS_LOG" 2>&1
    then
      tail -n 5 "$DOCS_LOG" | sed 's/^/  /'
      info "full build activated — the KB now includes doc summaries."
    else
      info "the docs build did not activate — tail of its log ($DOCS_LOG):"
      tail -n 30 "$DOCS_LOG" | sed 's/^/  /'
      info "the zero-LLM KB from step 4/5 is still active and fully queryable; only this optional pass didn't land."
    fi
  else
    info "--with-docs requested, but no LLM credentials were found (need LLM_PROVIDER + LLM_API_KEY, e.g. in .env)."
    info "add them to ${REPO_ROOT}/.env (LLM_PROVIDER=groq, LLM_API_KEY=..., LLM_MODEL=...) and re-run with --with-docs."
  fi
fi

# ---------------------------------------------------------------------------
say "Done — a knowledge base is built, active, and verified in '${DB_NAME}'"
# ---------------------------------------------------------------------------
cat <<EOF

Next steps:

  1. Start the MCP Context Broker (serves the KB you just built; leave it running):

       cd services/mcp-server
       MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=${MCP_PORT} \\
       MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \\
       MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}' \\
       DATABASE_URL="${DB_URL}" \\
         uv run python -m agentic_mcp_server

     Verify: curl -s http://127.0.0.1:${MCP_PORT}/health   (expect {"status":"ok", ...})

  2. Connect a host to it:
       - VS Code + GitHub Copilot: already wired via .vscode/mcp.json (points at
         http://127.0.0.1:${MCP_PORT}/mcp/) — open the folder, start the "context-broker"
         connection, switch Copilot Chat to Agent mode. See docs/dev-guide/00-getting-started.md
         Part 6-7 for the click-by-click version.
       - OpenCode: copy .opencode/ to your project root, set the broker URL in
         opencode.json to http://127.0.0.1:${MCP_PORT}/mcp/ and export CONTEXT_BROKER_TOKEN
         (any non-empty value in local-dev mode). See .opencode/README.md.
       - GitHub Copilot CLI / cloud agent: use .copilot/mcp/repository-settings.json (repo
         settings) or .copilot/mcp/vscode-mcp.json (already = .vscode/mcp.json). See
         .copilot/README.md and docs/dev-guide/09-copilot-cli-end-to-end.md.

  3. Run the eval suite:  make eval-all
     (T1/T2 need TEST_DATABASE_URL / DATABASE_URL — see docs/dev-guide/03-local-testing.md;
     tiers that need creds you don't have just SKIP with a stated reason, they don't fail.)

  4. Want doc/wiki/ticket summaries too, not just code? That needs a chat model (a Groq key
     is cheap/fast): add LLM_PROVIDER=groq, LLM_API_KEY=..., LLM_MODEL=... to a repo-root
     .env (never committed — see .gitignore), then re-run:  ./scripts/bootstrap.sh --with-docs
     (this script never reads or prints the key's value — only whether it's present.)

Full narrated walkthrough + troubleshooting: docs/dev-guide/00-quickstart.md
Deeper manual walkthrough (understand each moving part): docs/dev-guide/00-getting-started.md
EOF
