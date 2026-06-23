#!/usr/bin/env bash
# Generate (or refresh) the pristine, customer-facing `prod` branch from a source branch.
#
# `prod` is a GENERATED release branch: it is reset from the source branch every run, dev
# scaffolding is removed, internal references are scrubbed, and the curated customer docs are
# dropped in. Because it is regenerated, refreshing it force-updates the branch — prod carries
# no independent history. Run from anywhere in the repo:
#
#     release/make_prod.sh [source-branch]        # default source: main
#
# Review the result, then push with:  git push -f origin prod
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

SRC_BRANCH="${1:-main}"

# Ignore local-only files that prod removes anyway (e.g. local editor/agent settings).
if [ -n "$(git status --porcelain | grep -v '\.claude/settings\.json$')" ]; then
  echo "error: working tree not clean — commit or stash first." >&2
  exit 1
fi
START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Stage the release assets (they live only on the source branch) outside the tree so they
# survive the branch switch and the deletion of release/.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R release/docs "$STAGE/docs"
cp release/README.prod.md "$STAGE/README.md"
cp release/gitignore.prod "$STAGE/gitignore"
cp release/scrub.py "$STAGE/scrub.py"

git checkout -B prod "$SRC_BRANCH" >/dev/null

# --- 1. delete dev scaffolding (anything that reveals the build process) ---
DEV_PATHS=(
  .claude CLAUDE.md .copilot .opencode .mcp.json .vscode .github
  docs evals vault scripts release docker-compose.yml
  build-run.md build-run-2.md build-run-coretrio.md build-run-embed.md
  build-run-final.md build-run-judge.md build-run-scratch.md
  .kb-buildlane-index.json .kb-local-search-index.json
)
for p in "${DEV_PATHS[@]}"; do
  git rm -rq --ignore-unmatch "$p" >/dev/null 2>&1 || true
done
# Internal test suites: densest source of internal references; the product ships src only.
git rm -rq --ignore-unmatch services/kb-builder/tests services/mcp-server/tests >/dev/null 2>&1 || true

# --- 2. drop in the curated customer docs + clean root README ---
mkdir -p docs
cp -R "$STAGE/docs/." docs/
cp "$STAGE/README.md" README.md
cp "$STAGE/gitignore" .gitignore

# --- 3. scrub internal references from every remaining file ---
python3 "$STAGE/scrub.py" .

# safety gate: the scrub must never break Python syntax.
if ! python3 -m compileall -q services >/dev/null 2>&1; then
  echo "error: scrub produced invalid Python — aborting (prod not committed)." >&2
  python3 -m compileall -q services 2>&1 | grep -i error | head >&2
  git checkout -fq "$START_BRANCH"
  exit 1
fi

# --- 4. commit the generated release ---
# Stage tracked changes (scrub edits + the dev-path deletions) and the curated files only.
# NEVER `git add -A`: that would sweep in untracked local cruft (index files, agent state) that
# the source branch's .gitignore was keeping out.
git add -u
git add docs README.md .gitignore
git commit -q -m "prod: pristine customer release (generated from ${SRC_BRANCH})"
git checkout -q "$START_BRANCH"

echo "prod branch regenerated from ${SRC_BRANCH}. Review it, then: git push -f origin prod"
