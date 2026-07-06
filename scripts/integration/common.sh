#!/usr/bin/env bash
# Shared helpers for the host integration harness
# (docs/runbooks/host-integration-test-plan.md §4).
#
# Sourced by preflight.sh / run_copilot.sh / run_opencode.sh / t5_governance.sh.
# Never prints secret values: .env is loaded in-process (load_secrets) and the
# values are only ever passed through the environment of child processes.

set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${COMMON_DIR}/../.." && pwd)"
export REPO_ROOT

# One evidence tree per run; run_all.sh exports EVIDENCE_DIR so all phases share it.
EVIDENCE_DIR="${EVIDENCE_DIR:-${TMPDIR:-/tmp}/host-integration-evidence-$(date +%Y%m%d-%H%M%S)}"
export EVIDENCE_DIR
mkdir -p "${EVIDENCE_DIR}/cases" "${EVIDENCE_DIR}/server-logs" "${EVIDENCE_DIR}/backups"

DB_NAME="${DB_NAME:-agentic_kb_full}"
export DB_NAME
DATABASE_URL_ASYNC="postgresql+asyncpg://${USER}@localhost:5432/${DB_NAME}"
export DATABASE_URL_ASYNC

MCP_PORT="${MCP_PORT:-8765}"
MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp/"
export MCP_URL
HEALTH_URL="http://127.0.0.1:${MCP_PORT}/health"
PID_FILE="/tmp/mcp-it.pid"
SERVER_PY="${REPO_ROOT}/services/mcp-server/.venv/bin/python"

CASE_TIMEOUT="${CASE_TIMEOUT:-300}"

log() { printf 'event=%s %s\n' "$1" "${2:-}" >&2; }

load_secrets() {
    # Load repo-root .env into this process only. Values are NEVER echoed.
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env"
    set +a
}

# ---------------------------------------------------------------- server ----

port_is_free() {
    ! lsof -nP -iTCP:"${MCP_PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

start_server() { # $1=local-dev subject  $2=MCP_AGENT_ALLOWANCES json  $3=phase label
    local subject="$1" allowances="$2" phase="$3"
    if [ -f "${PID_FILE}" ]; then
        log server_start_refused "reason=pid_file_exists pid_file=${PID_FILE}"
        return 1
    fi
    if ! port_is_free; then
        log server_start_refused "reason=port_busy port=${MCP_PORT}"
        return 1
    fi
    local log_file="${EVIDENCE_DIR}/server-logs/server-${phase}.log"
    (
        cd "${REPO_ROOT}/services/mcp-server"
        MCP_LOCAL_DEV_AUTH=1 \
        MCP_HTTP_HOST=127.0.0.1 \
        MCP_HTTP_PORT="${MCP_PORT}" \
        MCP_ENTRA_TENANT_ID=local-dev \
        MCP_ENTRA_AUDIENCE=api://local \
        MCP_LOCAL_DEV_TEAMS=platform \
        MCP_LOCAL_DEV_SUBJECT="${subject}" \
        MCP_AGENT_ALLOWANCES="${allowances}" \
        DATABASE_URL="${DATABASE_URL_ASYNC}" \
            nohup "${SERVER_PY}" -m agentic_mcp_server >"${log_file}" 2>&1 &
        echo $! >"${PID_FILE}"
    )
    log server_started "phase=${phase} subject=${subject} pid=$(cat "${PID_FILE}") log=${log_file}"
    wait_for_health
}

wait_for_health() {
    local i body
    for i in $(seq 1 60); do
        body="$(curl -fsS "${HEALTH_URL}" 2>/dev/null || true)"
        if [ -n "${body}" ] && printf '%s' "${body}" | grep -q '"status":"ok"'; then
            log server_healthy "health=${body}"
            return 0
        fi
        sleep 0.5
    done
    log server_unhealthy "waited_s=30 last=${body:-none}"
    return 1
}

stop_server() {
    if [ ! -f "${PID_FILE}" ]; then
        log server_stop_noop "reason=no_pid_file"
        return 0
    fi
    local pid
    pid="$(cat "${PID_FILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
        local i
        # shellcheck disable=SC2034  # bounded-wait counter only
        for i in $(seq 1 20); do
            if ! kill -0 "${pid}" 2>/dev/null; then break; fi
            sleep 0.5
        done
        if kill -0 "${pid}" 2>/dev/null; then
            kill -9 "${pid}" 2>/dev/null || true
        fi
    fi
    rm -f "${PID_FILE}"
    log server_stopped "pid=${pid}"
}

# --------------------------------------------------------- ledger snapshots ----

db_now() { psql -d "${DB_NAME}" -Atc "SELECT now()"; }

ledger_delta() { # $1=since timestamptz — retrieval_event rows written after it
    psql -d "${DB_NAME}" -Atc "
        SELECT coalesce(json_agg(row_to_json(t)), '[]') FROM (
            SELECT agent_name, tool_name, status, tokens_returned, latency_ms,
                   kb_version, run_id, details, created_at
            FROM retrieval_event WHERE created_at > '$1' ORDER BY created_at
        ) t"
}

spans_delta() { # $1=since timestamptz — trace_span rows started after it
    psql -d "${DB_NAME}" -Atc "
        SELECT coalesce(json_agg(row_to_json(t)), '[]') FROM (
            SELECT name, service, status, trace_id, started_at, ended_at
            FROM trace_span WHERE started_at > '$1' ORDER BY started_at
        ) t"
}

# ------------------------------------------------------------- case runner ----

run_with_timeout() { # $1=seconds, rest = command; 124 on timeout
    local secs="$1"
    shift
    "$@" &
    local cmd_pid=$!
    (
        sleep "${secs}"
        if kill -0 "${cmd_pid}" 2>/dev/null; then
            kill -TERM "${cmd_pid}" 2>/dev/null || true
            sleep 5
            kill -KILL "${cmd_pid}" 2>/dev/null || true
        fi
    ) &
    local watchdog_pid=$!
    local rc=0
    wait "${cmd_pid}" || rc=$?
    kill "${watchdog_pid}" 2>/dev/null || true
    wait "${watchdog_pid}" 2>/dev/null || true
    if [ "${rc}" -eq 143 ] || [ "${rc}" -eq 137 ]; then rc=124; fi
    return "${rc}"
}

# Model-flake signatures (evaluation-system.md §2: bounded retry against a
# machine-checkable provider error, never against a graded expectation).
# 'Failed to call a function'/failed_generation is Groq's malformed-tool-call
# 400; 'Tool call validation failed' is the hallucinated-absent-tool 400;
# 'validation error for call' is the MCP boundary's pydantic reject of
# malformed tool arguments — all machine-checkable validator errors.
is_flaky_transcript() { # $1=transcript file
    grep -qiE 'rate.?limit|429|AI_APICallError|tool_use_failed|overloaded|internal server error|503 Service|"status": ?(429|5[0-9][0-9])|Failed to call a function|invalid_request_error|failed_generation|Tool call validation failed|validation error for call' "$1"
}

write_meta() { # dir host case_id kind exit_code attempts flake_detected prompt
    jq -n \
        --arg host "$2" --arg case_id "$3" --arg kind "$4" \
        --argjson exit_code "$5" --argjson attempts "$6" \
        --arg flake_detected "$7" --arg prompt "$8" \
        --arg subject "${SUBJECT:-}" \
        --arg started_at "${CASE_STARTED_AT:-}" \
        --arg ended_at "$(date -u +%FT%TZ)" \
        '{host: $host, case_id: $case_id, kind: $kind, exit_code: $exit_code,
          attempts: $attempts, flake_detected: ($flake_detected == "yes"),
          prompt: $prompt, subject: $subject,
          started_at: $started_at, ended_at: $ended_at}' \
        >"$1/meta.json"
}

begin_case() { # $1=case dir name → sets CASE_DIR, CASE_BEFORE_TS, CASE_STARTED_AT
    CASE_DIR="${EVIDENCE_DIR}/cases/$1"
    mkdir -p "${CASE_DIR}"
    CASE_BEFORE_TS="$(db_now)"
    CASE_STARTED_AT="$(date -u +%FT%TZ)"
    log case_started "case=$1"
}

approved_rows_since() { # $1=since ts → count of approved rows (retry guards)
    psql -d "${DB_NAME}" -Atc \
        "SELECT count(*) FROM retrieval_event WHERE created_at > '$1' AND status = 'approved'"
}

# ------------------------------------------------------- phase bookkeeping ----
# Each runner invocation records the [start, end] interval + subject it drove,
# so T5 can bound the graded ledger window to exactly the case runs that are
# still present in cases/ — a re-run never orphans or double-counts rows.

phase_begin() { # $1=host — full runs reset the host's cases + intervals
    PHASE_HOST="$1"
    PHASE_START_TS="$(db_now)"
    mkdir -p "${EVIDENCE_DIR}/phases"
    if [ "${#CASE_SELECTION[@]}" -eq 0 ]; then
        rm -f "${EVIDENCE_DIR}/phases/${PHASE_HOST}"-*.json
        if compgen -G "${EVIDENCE_DIR}/cases/${PHASE_HOST}-*" >/dev/null; then
            local stash
            stash="${EVIDENCE_DIR}/archived/${PHASE_HOST}-$(date +%s)"
            mkdir -p "${stash}"
            mv "${EVIDENCE_DIR}/cases/${PHASE_HOST}"-* "${stash}/"
            log phase_previous_cases_archived "host=${PHASE_HOST} to=${stash}"
        fi
    fi
}

phase_end() { # uses PHASE_HOST/PHASE_START_TS/SUBJECT
    jq -n --arg subject "${SUBJECT}" --arg start "${PHASE_START_TS}" \
        --arg end "$(db_now)" \
        '{subject: $subject, start: $start, end: $end}' \
        >"${EVIDENCE_DIR}/phases/${PHASE_HOST}-$(date +%s).json"
}

# Optional targeted re-run: `run_<host>.sh case-id [case-id...]` runs only the
# named cases (fix-loop tool; full runs pass no args).
CASE_SELECTION=("$@")

case_selected() { # $1=case id
    if [ "${#CASE_SELECTION[@]}" -eq 0 ]; then return 0; fi
    local wanted
    for wanted in "${CASE_SELECTION[@]}"; do
        if [ "${wanted}" = "$1" ]; then return 0; fi
    done
    return 1
}

snapshot_case_deltas() { # uses CASE_DIR / CASE_BEFORE_TS
    ledger_delta "${CASE_BEFORE_TS}" >"${CASE_DIR}/ledger_delta.json"
    spans_delta "${CASE_BEFORE_TS}" >"${CASE_DIR}/spans_delta.json"
}
