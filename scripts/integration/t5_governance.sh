#!/usr/bin/env bash
# T5 — Governance evidence (host-integration-test-plan.md §3).
# 1) Dumps every retrieval_event / trace_span row written since the T1 baseline
#    (the session ledger the grader checks for completeness + attribution).
# 2) Secret scan over ALL captured evidence (transcripts, session shares, server
#    logs): the real values of the .env credentials and the live gh token are
#    grepped -F; only COUNTS are ever written, never values. Zero tolerance.
# 3) Renders the operator dashboard against the same registry (make dashboard).

set -euo pipefail
# shellcheck source=common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

OUT="${EVIDENCE_DIR}/t5"
mkdir -p "${OUT}"

# ---- 1. session ledger window ------------------------------------------
# The graded window is the union of the phase intervals the runners recorded
# (phases/<host>-*.json: subject + [start, end]), each bound to its subject —
# so the window covers exactly the case runs still present in cases/, even
# after a fix-loop re-run, and never counts probe or third-party traffic.
LEDGER_WHERE="$(python3 - "${EVIDENCE_DIR}" <<'PY'
import json, pathlib, sys
phases = sorted(pathlib.Path(sys.argv[1], "phases").glob("*.json"))
clauses = []
for p in phases:
    ph = json.loads(p.read_text())
    clauses.append(
        f"(agent_name = '{ph['subject']}' AND created_at > '{ph['start']}' "
        f"AND created_at <= '{ph['end']}')"
    )
print(" OR ".join(clauses) if clauses else "false")
PY
)"
SPANS_WHERE="$(python3 - "${EVIDENCE_DIR}" <<'PY'
import json, pathlib, sys
phases = sorted(pathlib.Path(sys.argv[1], "phases").glob("*.json"))
clauses = []
for p in phases:
    ph = json.loads(p.read_text())
    clauses.append(f"(started_at > '{ph['start']}' AND started_at <= '{ph['end']}')")
print(" OR ".join(clauses) if clauses else "false")
PY
)"

psql -d "${DB_NAME}" -Atc "
    SELECT coalesce(json_agg(row_to_json(t)), '[]') FROM (
        SELECT agent_name, tool_name, status, tokens_returned, latency_ms,
               kb_version, run_id, details, created_at
        FROM retrieval_event WHERE ${LEDGER_WHERE} ORDER BY created_at
    ) t" >"${OUT}/ledger_window.json"
psql -d "${DB_NAME}" -Atc "
    SELECT coalesce(json_agg(row_to_json(t)), '[]') FROM (
        SELECT name, service, status, trace_id, started_at, ended_at
        FROM trace_span WHERE ${SPANS_WHERE} ORDER BY started_at
    ) t" >"${OUT}/spans_window.json"
log t5_ledger_dumped "rows=$(jq 'length' "${OUT}/ledger_window.json") spans=$(jq 'length' "${OUT}/spans_window.json")"

# ---- 2. secret scan (counts only — values never leave this process) -----
load_secrets
GH_LIVE_TOKEN="$(gh auth token 2>/dev/null || true)"

count_value() { # $1=value → match count across the evidence tree (0 if unset)
    local value="$1"
    if [ -z "${value}" ]; then echo 0; return; fi
    grep -rF "${value}" "${EVIDENCE_DIR}" --exclude-dir=t5 2>/dev/null | wc -l | tr -d ' '
}

count_pattern() { # $1=regex → match count across the evidence tree
    grep -rE "$1" "${EVIDENCE_DIR}" --exclude-dir=t5 2>/dev/null | wc -l | tr -d ' '
}

jq -n \
    --argjson llm_api_key "$(count_value "${LLM_API_KEY:-}")" \
    --argjson github_token "$(count_value "${GITHUB_TOKEN:-}")" \
    --argjson ado_pat "$(count_value "${ADO_PAT:-}")" \
    --argjson gh_live_token "$(count_value "${GH_LIVE_TOKEN}")" \
    --argjson pat_gsk "$(count_pattern 'gsk_[A-Za-z0-9]{20,}')" \
    --argjson pat_ghp "$(count_pattern 'gh[pousr]_[A-Za-z0-9]{20,}')" \
    --argjson pat_github_pat "$(count_pattern 'github_pat_[A-Za-z0-9_]{20,}')" \
    --argjson pat_sk "$(count_pattern 'sk-[A-Za-z0-9-]{24,}')" \
    '{value_matches: {LLM_API_KEY: $llm_api_key, GITHUB_TOKEN: $github_token,
                      ADO_PAT: $ado_pat, GH_AUTH_TOKEN: $gh_live_token},
      pattern_matches: {gsk_: $pat_gsk, gh_token_prefixes: $pat_ghp,
                        github_pat_: $pat_github_pat, "sk-": $pat_sk}}' \
    >"${OUT}/secret_scan.json"
log t5_secret_scan_done "out=${OUT}/secret_scan.json"

# ---- 3. dashboard renders against the same registry ---------------------
dash_rc=0
(cd "${REPO_ROOT}" && DATABASE_URL="${DATABASE_URL_ASYNC}" make dashboard) \
    >"${OUT}/dashboard_render.log" 2>&1 || dash_rc=$?
for f in dashboard.html dashboard.md; do
    if [ -f "${REPO_ROOT}/evals/${f}" ]; then
        cp "${REPO_ROOT}/evals/${f}" "${OUT}/${f}"
        rm -f "${REPO_ROOT}/evals/${f}"   # keep the repo checkout pristine
    fi
done
jq -n --argjson exit_code "${dash_rc}" \
    --arg html "$([ -f "${OUT}/dashboard.html" ] && echo yes || echo no)" \
    --arg md "$([ -f "${OUT}/dashboard.md" ] && echo yes || echo no)" \
    '{exit_code: $exit_code, rendered_html: ($html == "yes"), rendered_md: ($md == "yes")}' \
    >"${OUT}/dashboard.json"
log t5_dashboard_done "exit=${dash_rc}"
