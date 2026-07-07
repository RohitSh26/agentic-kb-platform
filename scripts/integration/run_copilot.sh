#!/usr/bin/env bash
# Copilot CLI runner — T2/T3/T4 of docs/runbooks/host-integration-test-plan.md.
#
# Policy source: the COMMITTED .copilot/mcp/repository-settings.json (the tool
# allowlist). The Copilot CLI cannot consume that file in place, so — exactly as
# docs/dev-guide/how-to/connect-copilot-cli.md documents — its native ~/.copilot/mcp-config.json is GENERATED
# from the committed file, changing only the URL (local broker) and the bearer
# (any non-empty value under ADR-0016 local-dev auth). The pre-existing user
# config is backed up and restored on exit.
#
# Copilot custom agents: the committed renderings .copilot/agents/*.agent.md are
# installed to ~/.copilot/agents/ (the user-level CLI discovery location named in
# .copilot/README.md), and restored on exit.
#
# Session flags: --allow-all-tools is required for non-interactive mode
# (copilot --help, v1.0.63); write/shell are explicitly denied so no run can
# edit the repo or exfiltrate env values into transcripts. The three servers
# from .mcp.json (Claude Code build tooling that Copilot also reads as
# workspace config) and the builtin GitHub MCP are disabled per session so the
# visible surface is the committed broker policy — a session-scoped exclusion,
# not a config edit.

set -euo pipefail
# shellcheck source=common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SUBJECT="copilot-cli"
export SUBJECT
GENEROUS='{"copilot-cli": {"max_requests": 50, "max_tokens": 50000}}'
# max_requests 0: EVERY kb_search is denied with the budget notice — the
# deterministic form of "tiny cap", same rationale as run_opencode.sh (a cap
# of 1 only produces a denial if the model happens to call twice in one
# session; run 2's copilot budget case proved Haiku often doesn't).
# Approved-under-cap behavior is separately proven by every generous-cap case.
TINY='{"copilot-cli": {"max_requests": 0, "max_tokens": 50000}}'

COPILOT_USER_CONFIG="${HOME}/.copilot/mcp-config.json"
COPILOT_USER_AGENTS="${HOME}/.copilot/agents"
BACKUP_DIR="${EVIDENCE_DIR}/backups"
INSTALLED_AGENTS=0

install_host_config() {
    mkdir -p "${HOME}/.copilot"
    if [ -f "${COPILOT_USER_CONFIG}" ]; then
        cp "${COPILOT_USER_CONFIG}" "${BACKUP_DIR}/copilot-mcp-config.json.orig"
    fi
    python3 - "${REPO_ROOT}" "${MCP_URL}" "${COPILOT_USER_CONFIG}" <<'PY'
import json, sys
root, url, out = sys.argv[1], sys.argv[2], sys.argv[3]
committed = json.load(open(f"{root}/.copilot/mcp/repository-settings.json"))
entry = committed["mcpServers"]["context-broker"]
entry["url"] = url                                      # local broker
entry["headers"]["Authorization"] = "Bearer local-dev-token"  # ADR-0016 local-dev
with open(out, "w") as f:
    json.dump({"mcpServers": {"context-broker": entry}}, f, indent=2)
PY
    log copilot_config_generated "from=.copilot/mcp/repository-settings.json to=${COPILOT_USER_CONFIG}"

    if [ -d "${COPILOT_USER_AGENTS}" ]; then
        mv "${COPILOT_USER_AGENTS}" "${BACKUP_DIR}/copilot-agents.orig"
    fi
    mkdir -p "${COPILOT_USER_AGENTS}"
    # Generated from the committed renderings with ONE transform: the frontmatter
    # `description:` value is YAML-quoted. Copilot CLI 1.0.63 drops any agent
    # whose plain-scalar description contains ": " (a YAML ScannerError) — today
    # that is exactly orchestrator.agent.md. Finding recorded in the report; the
    # committed file itself is not touched.
    python3 - "${REPO_ROOT}/.copilot/agents" "${COPILOT_USER_AGENTS}" <<'PY'
import json, pathlib, re, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
for f in sorted(src.glob("*.agent.md")):
    text = f.read_text()
    text = re.sub(
        r"^description: (?!['\"])(.*)$",
        lambda m: f"description: {json.dumps(m.group(1))}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    (dst / f.name).write_text(text)
PY
    INSTALLED_AGENTS=1
    log copilot_agents_installed "to=${COPILOT_USER_AGENTS} transform=yaml-quote-description"
}

restore_host_config() {
    if [ -f "${BACKUP_DIR}/copilot-mcp-config.json.orig" ]; then
        cp "${BACKUP_DIR}/copilot-mcp-config.json.orig" "${COPILOT_USER_CONFIG}"
    else
        rm -f "${COPILOT_USER_CONFIG}"
    fi
    if [ "${INSTALLED_AGENTS}" -eq 1 ]; then
        rm -rf "${COPILOT_USER_AGENTS}"
        if [ -d "${BACKUP_DIR}/copilot-agents.orig" ]; then
            mv "${BACKUP_DIR}/copilot-agents.orig" "${COPILOT_USER_AGENTS}"
        fi
    fi
    log copilot_config_restored ""
}

cleanup() {
    stop_server || true
    restore_host_config || true
}
trap cleanup EXIT

copilot_run() { # $1=case dir  $2=prompt  [extra copilot flags...]
    local case_dir="$1" prompt="$2"
    shift 2
    (
        cd "${REPO_ROOT}"
        run_with_timeout "${CASE_TIMEOUT}" copilot -p "${prompt}" \
            --allow-all-tools --deny-tool 'write' --deny-tool 'shell' \
            --disable-builtin-mcps \
            --disable-mcp-server postgres-dev \
            --disable-mcp-server github \
            --disable-mcp-server agentic-kb \
            --no-color --no-ask-user \
            --log-level debug --log-dir "${case_dir}/logs" \
            --share "${case_dir}/session.md" \
            "$@"
    )
}

copilot_case() { # $1=case id  $2=kind  $3=prompt  [extra copilot flags...]
    local case_id="$1" kind="$2" prompt="$3"
    shift 3
    case_selected "${case_id}" || return 0
    begin_case "${case_id}"
    local rc=0 attempts=1 flake=no
    copilot_run "${CASE_DIR}" "${prompt}" "$@" \
        >"${CASE_DIR}/transcript.txt" 2>&1 || rc=$?
    # Bounded retry (once) on a machine-checkable provider/validator error:
    # non-zero exit with a flake signature, or an MCP-boundary schema reject
    # ('validation error for call') that left the case with ZERO approved
    # rows — never on a graded outcome the model merely got wrong.
    local schema_reject=no
    if [ -f "${CASE_DIR}/session.md" ] \
            && grep -q 'validation error for call' "${CASE_DIR}/session.md" \
            && [ "$(approved_rows_since "${CASE_BEFORE_TS}")" -eq 0 ]; then
        schema_reject=yes
    fi
    if { [ "${rc}" -ne 0 ] || [ "${schema_reject}" = yes ]; } \
            && { is_flaky_transcript "${CASE_DIR}/transcript.txt" \
                || { [ -f "${CASE_DIR}/session.md" ] && is_flaky_transcript "${CASE_DIR}/session.md"; }; }; then
        flake=yes attempts=2
        mv "${CASE_DIR}/transcript.txt" "${CASE_DIR}/transcript.attempt1.txt"
        [ -f "${CASE_DIR}/session.md" ] && mv "${CASE_DIR}/session.md" "${CASE_DIR}/session.attempt1.md"
        rc=0
        copilot_run "${CASE_DIR}" "${prompt}" "$@" \
            >"${CASE_DIR}/transcript.txt" 2>&1 || rc=$?
    fi
    snapshot_case_deltas
    write_meta "${CASE_DIR}" copilot "${case_id}" "${kind}" "${rc}" "${attempts}" "${flake}" "${prompt}"
    log case_finished "case=${case_id} exit=${rc} attempts=${attempts}"
}

main() {
    # No load_secrets here: Copilot needs no .env value. The keyring OAuth token
    # is fetched with GITHUB_TOKEN/GH_TOKEN masked so a classic PAT from the
    # calling environment can never shadow it (Copilot rejects ghp_ tokens).
    GH_TOKEN="$(env -u GITHUB_TOKEN -u GH_TOKEN gh auth token)"
    export GH_TOKEN

    install_host_config
    phase_begin copilot
    start_server "${SUBJECT}" "${GENEROUS}" copilot-main

    # ---- T2: handshake & discovery -------------------------------------
    if case_selected copilot-t2-mcp-config; then
        begin_case copilot-t2-mcp-config
        { copilot mcp list 2>&1 || true; } >"${CASE_DIR}/mcp_list.txt"
        { copilot mcp get context-broker 2>&1 || true; } >"${CASE_DIR}/mcp_get.txt"
        snapshot_case_deltas
        write_meta "${CASE_DIR}" copilot copilot-t2-mcp-config mcp_config 0 1 no \
            "copilot mcp list + copilot mcp get context-broker (mechanical)"
    fi

    copilot_case copilot-t2-discovery discovery \
        "Do not call any tools. List the exact names of every tool you can use from the MCP server named 'context-broker', one name per line, and nothing else."

    # ---- T3: single-tool correctness ------------------------------------
    copilot_case copilot-t3-kb-search forced_kb_search \
        "Call the context-broker kb_search tool exactly once with the query: how does the incremental build skip unchanged sources without calling the LLM? Then report verbatim: each result's title, source_uri and confidence_tier, plus the budget_remaining and any notice from the tool response. Do not read any files; do not call any other tool; call kb_search exactly once."

    copilot_case copilot-t3-task-context forced_task_context \
        "Call the context-broker get_task_context tool exactly once, with task_description: add input validation to the GitHub connector. Then report: the resolved scope (files and symbols), the blast radius (callers, callees, tests), the applicable conventions, and similar prior changes — with the evidence ids and confidence tiers the tool returned. Do not read any files; do not call any other tool; call get_task_context exactly once."

    # ---- T4: agent discipline (orchestrator rendering) ------------------
    copilot_case copilot-t4-explain-1 explain \
        "How does the durable model-output cache survive crashes during a KB build?" \
        --agent orchestrator
    copilot_case copilot-t4-explain-2 explain \
        "How does the KB build decide it can skip calling the LLM for an unchanged document?" \
        --agent orchestrator
    copilot_case copilot-t4-explain-3 explain \
        "Where is the per-agent kb_search budget enforced, and what happens when a session spends it?" \
        --agent orchestrator
    copilot_case copilot-t4-explain-4 explain \
        "What has to happen before a kb_version is marked active, and what serves it afterwards?" \
        --agent orchestrator
    copilot_case copilot-t4-explain-5 explain \
        "How does the nightly build keep one failing source from aborting the whole build?" \
        --agent orchestrator

    copilot_case copilot-t4-build-1 build \
        "I approve this task — proceed: add input validation to the GitHub connector's configuration. Gather the task context first, then present the implementation plan with citations. Do NOT edit any files; stop after presenting the plan." \
        --agent orchestrator
    copilot_case copilot-t4-build-2 build \
        "I approve this task — proceed: add retry-with-backoff to the ADO connector's HTTP calls. Gather the task context first, then present the implementation plan with citations. Do NOT edit any files; stop after presenting the plan." \
        --agent orchestrator

    # ---- T4: fallback (answer-completely rule) --------------------------
    if ! case_selected copilot-t4-fallback; then
        stop_server
    else
    local sid
    sid="$(uuidgen | tr '[:upper:]' '[:lower:]')"
    begin_case copilot-t4-fallback
    local rc1=0 rc2=0
    copilot_run "${CASE_DIR}" \
        "How does the KB build decide it can skip calling the LLM for an unchanged document? Short answer with sources." \
        --agent orchestrator --session-id "${sid}" \
        >"${CASE_DIR}/transcript_turn1.txt" 2>&1 || rc1=$?
    ledger_delta "${CASE_BEFORE_TS}" >"${CASE_DIR}/ledger_delta_turn1.json"
    stop_server
    local turn2_ts
    turn2_ts="$(db_now)"
    (
        cd "${REPO_ROOT}"
        run_with_timeout "${CASE_TIMEOUT}" copilot -p \
            "Now also explain: where does the alias/reference miner store its artifacts, and how is their ACL derived? Answer completely even if the knowledge tools are unavailable — read the specific files you need." \
            --resume="${sid}" \
            --allow-all-tools --deny-tool 'write' --deny-tool 'shell' \
            --disable-builtin-mcps \
            --disable-mcp-server postgres-dev \
            --disable-mcp-server github \
            --disable-mcp-server agentic-kb \
            --no-color --no-ask-user \
            --log-level debug --log-dir "${CASE_DIR}/logs" \
            --share "${CASE_DIR}/session_turn2.md"
    ) >"${CASE_DIR}/transcript_turn2.txt" 2>&1 || rc2=$?
    ledger_delta "${turn2_ts}" >"${CASE_DIR}/ledger_delta_turn2.json"
    spans_delta "${CASE_BEFORE_TS}" >"${CASE_DIR}/spans_delta.json"
    jq -n --argjson rc1 "${rc1}" --argjson rc2 "${rc2}" --arg sid "${sid}" \
        --arg subject "${SUBJECT}" --arg started_at "${CASE_STARTED_AT}" \
        --arg ended_at "$(date -u +%FT%TZ)" \
        '{host: "copilot", case_id: "copilot-t4-fallback", kind: "fallback",
          exit_code: $rc2, turn1_exit_code: $rc1, attempts: 1, flake_detected: false,
          prompt: "turn1: skip-LLM question (server up) / turn2: alias miner ACL question (server killed mid-session)",
          session_id: $sid, subject: $subject, started_at: $started_at, ended_at: $ended_at}' \
        >"${CASE_DIR}/meta.json"
    log case_finished "case=copilot-t4-fallback exit_turn1=${rc1} exit_turn2=${rc2}"
    fi

    # ---- T4: budget exhaustion (tiny cap) --------------------------------
    if case_selected copilot-t4-budget; then
        start_server "${SUBJECT}" "${TINY}" copilot-budget
        copilot_case copilot-t4-budget budget \
            "Answer BOTH questions. Use the context-broker kb_search tool for each question separately — one call per question, two calls total: (1) What is stored in the generation_cache table and when is it read? (2) What does a retrieval_event row record for a denied call? If the tool reports its budget is spent, continue with your native file tools and answer both questions completely." \
            --agent orchestrator
        stop_server
    fi

    phase_end
    log copilot_runner_done "evidence=${EVIDENCE_DIR}"
}

main "$@"
