#!/usr/bin/env bash
# T1 — Preflight (host-integration-test-plan.md §3).
# Pins binaries + versions, confirms the KB registry is active, snapshots the
# retrieval_event / trace_span baseline counts into the evidence directory.

set -euo pipefail
# shellcheck source=common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

OUT="${EVIDENCE_DIR}/preflight"
mkdir -p "${OUT}"

fail=0

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log preflight_missing_binary "binary=$1"
        fail=1
        return 1
    fi
}

{
    echo "date_utc=$(date -u +%FT%TZ)"
    echo "os=$(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)"
    require_cmd copilot && echo "copilot=$(copilot --version 2>&1 | head -1)"
    require_cmd opencode && echo "opencode=$(opencode --version 2>&1 | head -1)"
    require_cmd gh && echo "gh=$(gh --version | head -1)"
    require_cmd psql && echo "psql=$(psql --version)"
    require_cmd uv && echo "uv=$(uv --version)"
    require_cmd jq && echo "jq=$(jq --version)"
    require_cmd node && echo "node=$(node --version)"
    echo "server_python=$("${SERVER_PY}" --version 2>&1)"
    echo "db=${DB_NAME}"
} >"${OUT}/versions.txt"

if ! gh auth status >/dev/null 2>&1; then
    log preflight_gh_not_authed ""
    fail=1
fi

active_kb="$(psql -d "${DB_NAME}" -Atc \
    "SELECT kb_version FROM kb_build_run WHERE status='active'" 2>/dev/null || true)"
if [ -z "${active_kb}" ]; then
    log preflight_no_active_kb "db=${DB_NAME}"
    fail=1
fi

retrieval_count="$(psql -d "${DB_NAME}" -Atc "SELECT count(*) FROM retrieval_event")"
span_count="$(psql -d "${DB_NAME}" -Atc "SELECT count(*) FROM trace_span")"
baseline_ts="$(db_now)"

jq -n \
    --arg active_kb "${active_kb}" \
    --argjson retrieval_count "${retrieval_count}" \
    --argjson span_count "${span_count}" \
    --arg baseline_ts "${baseline_ts}" \
    --arg db "${DB_NAME}" \
    '{active_kb_version: $active_kb, retrieval_event_count: $retrieval_count,
      trace_span_count: $span_count, baseline_ts: $baseline_ts, db: $db}' \
    >"${OUT}/baseline.json"

if ! port_is_free; then
    log preflight_port_busy "port=${MCP_PORT}"
    fail=1
fi
if [ -f "${PID_FILE}" ]; then
    log preflight_stale_pid_file "pid_file=${PID_FILE}"
    fail=1
fi

if [ "${fail}" -ne 0 ]; then
    log preflight_failed "see=${OUT}"
    exit 1
fi
log preflight_ok "active_kb=${active_kb} retrieval_event=${retrieval_count} trace_span=${span_count}"
