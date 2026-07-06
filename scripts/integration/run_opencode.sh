#!/usr/bin/env bash
# OpenCode runner — T2/T3/T4 of docs/runbooks/host-integration-test-plan.md.
#
# Policy source: the COMMITTED .opencode/opencode.json + .opencode/agents/*
# (auto-discovered by OpenCode at the repo root — verified on 1.17.13). The one
# thing the committed file cannot carry is the broker URL (it ships the
# placeholder https://<your-broker-host>/mcp/), and a root-level opencode.json
# does NOT override it (the .opencode/ project config wins that merge). The
# supported merge-last mechanism is OPENCODE_CONFIG_CONTENT, so the runner
# GENERATES that value from the committed file, substituting ONLY the URL. The
# bearer stays the committed {env:CONTEXT_BROKER_TOKEN} reference, satisfied by
# exporting CONTEXT_BROKER_TOKEN=local-dev-token (ADR-0016 local-dev auth).
#
# Provider: Groq, keyed by GROQ_API_KEY taken in-process from repo-root .env's
# LLM_API_KEY (never printed). GROQ_API_KEY is exported only inside the
# opencode-run subshells so `opencode debug config` evidence can never resolve
# a real key into a captured file.

set -euo pipefail
# shellcheck source=common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SUBJECT="opencode-cli"
export SUBJECT
GENEROUS='{"opencode-cli": {"max_requests": 50, "max_tokens": 50000}}'
# max_requests 0: EVERY kb_search is denied with the budget notice — the
# deterministic form of "tiny cap". (A cap of 1 only produces a denial if the
# model happens to call twice in one session; run 2 proved it often doesn't.)
TINY='{"opencode-cli": {"max_requests": 0, "max_tokens": 50000}}'

cleanup() {
    stop_server || true
}
trap cleanup EXIT

opencode_run() { # $1=case dir  $2=agent  $3=prompt  [extra opencode flags...]
    local case_dir="$1" agent="$2" prompt="$3"
    shift 3
    (
        cd "${REPO_ROOT}"
        # shellcheck disable=SC2030  # subshell-local ON PURPOSE: the key must
        # exist only inside opencode-run children, never in evidence captures.
        export GROQ_API_KEY="${LLM_API_KEY}"
        run_with_timeout "${CASE_TIMEOUT}" opencode run \
            --agent "${agent}" -m "${OC_MODEL}" \
            --format json --print-logs --log-level INFO \
            "$@" \
            "${prompt}" \
            >"${case_dir}/transcript.json" 2>"${case_dir}/run.log"
    )
}

opencode_case() { # $1=case id  $2=kind  $3=prompt  [extra opencode flags...]
    local case_id="$1" kind="$2" prompt="$3"
    shift 3
    case_selected "${case_id}" || return 0
    begin_case "${case_id}"
    local rc=0 attempts=1 flake=no
    opencode_run "${CASE_DIR}" orchestrator "${prompt}" "$@" || rc=$?
    # Retry only on a provider-error event (opencode can exit 0 after a stream
    # error), never on a graded outcome the model simply got wrong.
    if { [ "${rc}" -ne 0 ] || grep -q '"type":"error"' "${CASE_DIR}/transcript.json"; } \
            && { is_flaky_transcript "${CASE_DIR}/transcript.json" \
                || is_flaky_transcript "${CASE_DIR}/run.log"; }; then
        flake=yes attempts=2
        mv "${CASE_DIR}/transcript.json" "${CASE_DIR}/transcript.attempt1.json"
        mv "${CASE_DIR}/run.log" "${CASE_DIR}/run.attempt1.log"
        rc=0
        opencode_run "${CASE_DIR}" orchestrator "${prompt}" "$@" || rc=$?
    fi
    # T3 forcing-device miss: the prompt's whole job is to force one broker
    # call; if the model made NO broker call at all (and no provider error),
    # the measurement never happened — re-seat the probe once, recorded.
    if [ "${attempts}" -eq 1 ] && [ "${rc}" -eq 0 ] \
            && [[ "${kind}" == forced_* ]] \
            && ! grep -q '"tool":"context-broker_' "${CASE_DIR}/transcript.json"; then
        attempts=2
        mv "${CASE_DIR}/transcript.json" "${CASE_DIR}/transcript.attempt1-forcing-miss.json"
        mv "${CASE_DIR}/run.log" "${CASE_DIR}/run.attempt1-forcing-miss.log"
        opencode_run "${CASE_DIR}" orchestrator "${prompt}" "$@" || rc=$?
        log forcing_miss_retry "case=${case_id}"
    fi
    snapshot_case_deltas
    write_meta "${CASE_DIR}" opencode "${case_id}" "${kind}" "${rc}" "${attempts}" "${flake}" "${prompt}"
    log case_finished "case=${case_id} exit=${rc} attempts=${attempts}"
}

main() {
    load_secrets
    # NOT .env's LLM_MODEL: that is the build-plane docify model (a small
    # instant model). Agent runs use the platform's documented agent default
    # (scripts/kb_agent.py: llama-3.3-70b-versatile) unless overridden.
    OC_MODEL="groq/${OPENCODE_MODEL:-llama-3.3-70b-versatile}"
    export CONTEXT_BROKER_TOKEN="local-dev-token"

    OPENCODE_CONFIG_CONTENT="$(python3 - "${REPO_ROOT}" "${MCP_URL}" <<'PY'
import json, sys
root, url = sys.argv[1], sys.argv[2]
committed = json.load(open(f"{root}/.opencode/opencode.json"))
entry = dict(committed["mcp"]["context-broker"])
entry["url"] = url  # the ONLY substitution; header stays {env:CONTEXT_BROKER_TOKEN}
print(json.dumps({"mcp": {"context-broker": entry}}))
PY
)"
    export OPENCODE_CONFIG_CONTENT
    log opencode_config_generated "from=.opencode/opencode.json mechanism=OPENCODE_CONFIG_CONTENT url=${MCP_URL}"

    phase_begin opencode
    start_server "${SUBJECT}" "${GENEROUS}" opencode-main

    # ---- T2: handshake & discovery (mechanical: resolved config parity) --
    if case_selected opencode-t2-config; then
        begin_case opencode-t2-config
        (cd "${REPO_ROOT}" && opencode debug config) >"${CASE_DIR}/resolved_config.json" 2>"${CASE_DIR}/debug_config.log" || true
        (cd "${REPO_ROOT}" && opencode debug agent orchestrator) >"${CASE_DIR}/debug_agent_orchestrator.txt" 2>&1 || true
        (cd "${REPO_ROOT}" && opencode debug skill) >"${CASE_DIR}/debug_skills.txt" 2>&1 || true
        snapshot_case_deltas
        write_meta "${CASE_DIR}" opencode opencode-t2-config mcp_config 0 1 no \
            "opencode debug config + debug agent orchestrator (mechanical parity vs committed .opencode/opencode.json)"
    fi

    opencode_case opencode-t2-discovery discovery \
        "Do not call any tools. List the exact names of every tool you can use from the MCP server named 'context-broker', one name per line, and nothing else."

    # ---- T3: single-tool correctness ------------------------------------
    opencode_case opencode-t3-kb-search forced_kb_search \
        "Call the context-broker kb_search tool exactly once with the query: how does the incremental build skip unchanged sources without calling the LLM? Then report verbatim: each result's title, source_uri and confidence_tier, plus the budget_remaining and any notice from the tool response. Do not read any files; do not call any other tool; call kb_search exactly once."

    opencode_case opencode-t3-task-context forced_task_context \
        "Call the context-broker get_task_context tool exactly once, with task_description: add input validation to the GitHub connector. Then report: the resolved scope (files and symbols), the blast radius (callers, callees, tests), the applicable conventions, and similar prior changes — with the evidence ids and confidence tiers the tool returned. Do not read any files; do not call any other tool; call get_task_context exactly once."

    # ---- T4: agent discipline -------------------------------------------
    opencode_case opencode-t4-explain-1 explain \
        "How does the durable model-output cache survive crashes during a KB build?"
    opencode_case opencode-t4-explain-2 explain \
        "How does the KB build decide it can skip calling the LLM for an unchanged document?"
    opencode_case opencode-t4-explain-3 explain \
        "Where is the per-agent kb_search budget enforced, and what happens when a session spends it?"
    opencode_case opencode-t4-explain-4 explain \
        "What has to happen before a kb_version is marked active, and what serves it afterwards?"
    opencode_case opencode-t4-explain-5 explain \
        "How does the nightly build keep one failing source from aborting the whole build?"

    opencode_case opencode-t4-build-1 build \
        "I approve this task — proceed: add input validation to the GitHub connector's configuration. Gather the task context first, then present the implementation plan with citations. Do NOT edit any files; stop after presenting the plan."
    opencode_case opencode-t4-build-2 build \
        "I approve this task — proceed: add retry-with-backoff to the ADO connector's HTTP calls. Gather the task context first, then present the implementation plan with citations. Do NOT edit any files; stop after presenting the plan."

    # ---- T4: fallback (answer-completely rule) --------------------------
    if ! case_selected opencode-t4-fallback; then
        stop_server
    else
    begin_case opencode-t4-fallback
    local rc1=0 rc2=0 turn2_attempts=1
    (
        cd "${REPO_ROOT}"
        # shellcheck disable=SC2030,SC2031  # subshell-local on purpose (see above)
        export GROQ_API_KEY="${LLM_API_KEY}"
        run_with_timeout "${CASE_TIMEOUT}" opencode run \
            --agent orchestrator -m "${OC_MODEL}" \
            --format json --print-logs --log-level INFO \
            "How does the KB build decide it can skip calling the LLM for an unchanged document? Short answer with sources." \
            >"${CASE_DIR}/transcript_turn1.json" 2>"${CASE_DIR}/run_turn1.log"
    ) || rc1=$?
    ledger_delta "${CASE_BEFORE_TS}" >"${CASE_DIR}/ledger_delta_turn1.json"
    stop_server
    local turn2_ts try
    turn2_ts="$(db_now)"
    # Turn 2 gets the same bounded retry as everything else: a session that
    # hallucinates the now-absent broker tool draws a provider 400 ('Tool call
    # validation failed') — the adopted machine-checkable retry class.
    for try in 1 2; do
        rc2=0
        (
            cd "${REPO_ROOT}"
            # shellcheck disable=SC2030,SC2031  # subshell-local on purpose (see above)
            export GROQ_API_KEY="${LLM_API_KEY}"
            run_with_timeout "${CASE_TIMEOUT}" opencode run -c \
                --agent orchestrator -m "${OC_MODEL}" \
                --format json --print-logs --log-level INFO \
                "Now also explain: where does the alias/reference miner store its artifacts, and how is their ACL derived? Answer completely even if the knowledge tools are unavailable — read the specific files you need." \
                >"${CASE_DIR}/transcript_turn2.json" 2>"${CASE_DIR}/run_turn2.log"
        ) || rc2=$?
        if [ "${try}" -eq 1 ] && [ "${rc2}" -ne 0 ] \
                && { is_flaky_transcript "${CASE_DIR}/transcript_turn2.json" \
                    || is_flaky_transcript "${CASE_DIR}/run_turn2.log"; }; then
            turn2_attempts=2
            mv "${CASE_DIR}/transcript_turn2.json" "${CASE_DIR}/transcript_turn2.attempt1.json"
            mv "${CASE_DIR}/run_turn2.log" "${CASE_DIR}/run_turn2.attempt1.log"
            continue
        fi
        break
    done
    ledger_delta "${turn2_ts}" >"${CASE_DIR}/ledger_delta_turn2.json"
    spans_delta "${CASE_BEFORE_TS}" >"${CASE_DIR}/spans_delta.json"
    jq -n --argjson rc1 "${rc1}" --argjson rc2 "${rc2}" \
        --argjson attempts "${turn2_attempts}" \
        --arg subject "${SUBJECT}" --arg started_at "${CASE_STARTED_AT}" \
        --arg ended_at "$(date -u +%FT%TZ)" \
        '{host: "opencode", case_id: "opencode-t4-fallback", kind: "fallback",
          exit_code: $rc2, turn1_exit_code: $rc1, attempts: $attempts,
          flake_detected: ($attempts > 1),
          prompt: "turn1: skip-LLM question (server up) / turn2: alias miner ACL question (server killed mid-session, -c continues the session)",
          subject: $subject, started_at: $started_at, ended_at: $ended_at}' \
        >"${CASE_DIR}/meta.json"
    log case_finished "case=opencode-t4-fallback exit_turn1=${rc1} exit_turn2=${rc2}"
    fi

    # ---- T4: budget exhaustion (deterministic zero cap) ------------------
    if case_selected opencode-t4-budget; then
        start_server "${SUBJECT}" "${TINY}" opencode-budget
        opencode_case opencode-t4-budget budget \
            "Answer BOTH questions. Use the context-broker kb_search tool for each question separately — one call per question, two calls total: (1) What is stored in the generation_cache table and when is it read? (2) What does a retrieval_event row record for a denied call? If the tool reports its budget is spent, continue with your native file tools and answer both questions completely."
        stop_server
    fi

    phase_end
    log opencode_runner_done "evidence=${EVIDENCE_DIR}"
}

main "$@"
