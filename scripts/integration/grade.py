#!/usr/bin/env python3
"""Shared grader for the host integration harness.

Reads the evidence directory the runners populate (see common.sh: one
directory per case with meta.json + transcript(s) + ledger/span deltas) and
emits a markdown report with a PASS/FAIL/SKIP(reason) verdict per case,
verbatim failure output, and a flake count — the same reporting discipline as
docs/architecture/evaluation-system.md (verbatim detail, skip-with-reason,
flakes counted separately, never folded into failures).

Usage: python3 grade.py --evidence <dir> --repo <repo-root> [--out <report.md>]
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_TOOLS = {"kb_search", "get_task_context"}
FORBIDDEN_TOOLS = {
    "context_create_pack",
    "context_create_change_pack",
    "context_expand",
    "context_open_evidence",
    "context_platform_trust",
    "context_read_pack",
    "context_request_more",
    "context_verify_answer",
    "graph_get_neighbors",
    "ledger_list_retrievals",
}
TASK_CONTEXT_NODES = {"resolve_scope", "blast_radius", "conventions", "similar_prior_changes"}
EXPECTED_SUBJECTS = {"copilot-cli", "opencode-cli"}
CRASH_MARKERS = ("Traceback (most recent call last)", "panic:", "UnhandledPromiseRejection", "FATAL")
FLAKE_RE = re.compile(
    r"rate.?limit|429|AI_APICallError|tool_use_failed|overloaded"
    r"|internal server error|503 Service"
    r"|Failed to call a function|invalid_request_error|failed_generation",
    re.IGNORECASE,
)
REPO_PATH_RE = re.compile(
    r"(?:services|docs|scripts|agents|evals|infra)/[\w./-]+\.(?:py|md|json|yaml|toml|sh)"
)

# Ordered tool-call extraction. Both hosts' transcripts carry tool names in
# chronological order; these patterns capture a normalized tool label.
# Prefix mandatory: bare "kb_search" also occurs inside retrieved file contents
# echoed in tool outputs, which must not register as calls.
BROKER_TOOL_RE = re.compile(r"context[-_]broker[-_/](kb_search|get_task_context)")
NATIVE_TOOL_HINTS = ("read", "grep", "glob", "list", "view", "search", "bash", "edit")
# Formats pinned from real probe transcripts (2026-07-06):
# - opencode --format json: {"type":"tool_use",...,"part":{"tool":"context-broker_kb_search",...}}
# - copilot --share session.md: tool calls are h3 headers like `### ✅ `context-broker-kb_search``
GENERIC_TOOL_RE = re.compile(
    r'"tool"\s*:\s*"([^"]+)"'
    r"|^###\s+\S+\s+`([^`\n]+)`",
    re.MULTILINE,
)


@dataclass
class CaseResult:
    case_id: str
    host: str
    kind: str
    verdict: str  # PASS | FAIL | SKIP
    checks: list[str] = field(default_factory=list)  # "ok: ..." / "FAIL: ..."
    detail: str = ""  # verbatim failure tail
    attempts: int = 1
    flake_detected: bool = False
    reason: str = ""  # skip reason


def read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def transcript_of(case_dir: Path) -> str:
    for name in ("transcript.json", "transcript.txt"):
        text = read_text(case_dir / name)
        if text:
            return text
    return ""


def full_capture(case_dir: Path) -> str:
    """Everything the case captured — transcripts, session shares, run logs."""
    parts: list[str] = []
    for p in sorted(case_dir.glob("*")):
        if p.is_file() and p.suffix in {".txt", ".json", ".md", ".log"}:
            parts.append(read_text(p))
    return "\n".join(parts)


def ordered_tool_calls(case_dir: Path) -> list[str]:
    """Chronological, normalized tool labels ('broker:kb_search' / 'native:read')."""
    text = ""
    for name in ("transcript.json", "session.md", "transcript.txt"):
        candidate = read_text(case_dir / name)
        if candidate:
            text = candidate
            break
    if not text:
        return []
    events: list[tuple[int, str]] = []
    for m in GENERIC_TOOL_RE.finditer(text):
        name = next(g for g in m.groups() if g)
        events.append((m.start(), name))
    # broker tool names sometimes appear only in their namespaced form
    for m in BROKER_TOOL_RE.finditer(text):
        events.append((m.start(), m.group(1)))
    events.sort()
    calls: list[str] = []
    for _, name in events:
        low = name.lower()
        if "kb_search" in low or "get_task_context" in low:
            label = f"broker:{'kb_search' if 'kb_search' in low else 'get_task_context'}"
        elif any(h in low for h in NATIVE_TOOL_HINTS):
            label = f"native:{low}"
        else:
            continue
        if not calls or calls[-1] != label:
            calls.append(label)
    return calls


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.splitlines()[-lines:])


def ledger_rows(case_dir: Path, name: str = "ledger_delta.json") -> list[dict[str, object]]:
    data = read_json(case_dir / name)
    return data if isinstance(data, list) else []


def check_subject(rows: list[dict[str, object]], subject: str, checks: list[str]) -> bool:
    bad = [r for r in rows if r.get("agent_name") != subject]
    if bad:
        checks.append(f"FAIL: {len(bad)} ledger row(s) not attributed to subject '{subject}'")
        return False
    if rows:
        checks.append(f"ok: all {len(rows)} ledger row(s) attributed to '{subject}'")
    return True


def grade_case(case_dir: Path, repo: Path) -> CaseResult:
    meta = read_json(case_dir / "meta.json")
    if not isinstance(meta, dict):
        return CaseResult(case_dir.name, "?", "?", "SKIP", reason="no meta.json — case did not run")
    result = CaseResult(
        case_id=str(meta.get("case_id", case_dir.name)),
        host=str(meta.get("host", "?")),
        kind=str(meta.get("kind", "?")),
        verdict="PASS",
        attempts=int(meta.get("attempts", 1) or 1),
        flake_detected=bool(meta.get("flake_detected", False)),
    )
    checks = result.checks
    subject = str(meta.get("subject", ""))
    exit_code = int(meta.get("exit_code", 0) or 0)
    rows = ledger_rows(case_dir)
    transcript = transcript_of(case_dir)
    kind = result.kind

    def fail(msg: str) -> None:
        checks.append(f"FAIL: {msg}")
        result.verdict = "FAIL"

    if kind not in {"fallback", "mcp_config"} and exit_code != 0:
        fail(f"host exited {exit_code}")

    if kind == "mcp_config":
        if result.host == "copilot":
            get_txt = read_text(case_dir / "mcp_get.txt")
            if "get_task_context" in get_txt and "kb_search" in get_txt:
                checks.append("ok: mcp get shows both allowlisted tools")
            else:
                fail("mcp get does not show the committed two-tool allowlist")
            leaked = sorted(t for t in FORBIDDEN_TOOLS if t in get_txt)
            if leaked:
                fail(f"non-allowlisted tools visible in mcp config: {leaked}")
            else:
                checks.append("ok: no non-allowlisted broker tool in mcp config")
        else:  # opencode: resolved config parity vs the committed file
            resolved = read_json(case_dir / "resolved_config.json")
            committed = read_json(repo / ".opencode" / "opencode.json")
            if not isinstance(resolved, dict) or not isinstance(committed, dict):
                fail("resolved or committed opencode config unreadable")
            else:
                mcp = resolved.get("mcp", {}).get("context-broker", {})
                if str(mcp.get("url", "")).startswith("http://127.0.0.1"):
                    checks.append(f"ok: broker url resolved to {mcp.get('url')}")
                else:
                    fail(f"broker url not the local broker: {mcp.get('url')}")
                tools = resolved.get("tools", {})
                if tools.get("context-broker_*") is False:
                    checks.append("ok: global broker namespace denied (context-broker_*: false)")
                else:
                    fail("global 'context-broker_*': false missing from resolved config")
                mismatches: list[str] = []
                for agent, spec in committed.get("agent", {}).items():
                    want = spec.get("tools", {})
                    got = resolved.get("agent", {}).get(agent, {}).get("tools", {})
                    for tool_name, enabled in want.items():
                        if got.get(tool_name) != enabled:
                            mismatches.append(f"{agent}.{tool_name}")
                if mismatches:
                    fail(f"per-agent grants diverge from committed config: {mismatches}")
                else:
                    checks.append(
                        f"ok: all per-agent grants match committed config "
                        f"({len(committed.get('agent', {}))} agents)"
                    )

    elif kind == "discovery":
        for tool_name in sorted(ALLOWED_TOOLS):
            if tool_name in transcript:
                checks.append(f"ok: model reports {tool_name}")
            else:
                fail(f"model does not report allowlisted tool {tool_name}")
        leaked = sorted(t for t in FORBIDDEN_TOOLS if t in transcript)
        if leaked:
            fail(f"model reports non-allowlisted broker tools: {leaked}")
        else:
            checks.append("ok: no non-allowlisted broker tool reported")
        if rows:
            checks.append(f"note: discovery made {len(rows)} tool call(s) despite 'do not call'")

    elif kind == "forced_kb_search":
        # The delta spans every attempt (a retried attempt's calls are real
        # calls); the bar is one approved kb_search per attempt, nothing else.
        kb_rows = [r for r in rows if r.get("tool_name") == "kb_search"]
        if (
            1 <= len(rows) <= result.attempts
            and len(kb_rows) == len(rows)
            and all(r.get("status") == "approved" for r in kb_rows)
        ):
            checks.append(
                f"ok: {len(kb_rows)} approved kb_search row(s) across "
                f"{result.attempts} attempt(s), nothing else"
            )
        else:
            fail(
                f"expected one approved kb_search row per attempt, got "
                f"{[(r.get('tool_name'), r.get('status')) for r in rows]}"
            )
        check_subject(rows, subject, checks)
        if "budget" in transcript.lower():
            checks.append("ok: transcript reports budget_remaining/notice")
        else:
            fail("transcript does not report the response's budget field")
        real = [p for p in REPO_PATH_RE.findall(transcript) if (repo / p).exists()]
        if real:
            checks.append(f"ok: cited source_uri resolves to a real repo file ({real[0]})")
        else:
            fail("no reported source_uri resolves to a file in this repo")

    elif kind == "forced_task_context":
        tc_rows = [r for r in rows if r.get("tool_name") == "get_task_context"]
        if (
            1 <= len(rows) <= result.attempts
            and len(tc_rows) == len(rows)
            and all(r.get("status") == "approved" for r in tc_rows)
        ):
            checks.append(
                f"ok: {len(tc_rows)} approved get_task_context row(s) across "
                f"{result.attempts} attempt(s), nothing else"
            )
        else:
            fail(
                f"expected one approved get_task_context row per attempt, got "
                f"{[(r.get('tool_name'), r.get('status')) for r in rows]}"
            )
        check_subject(rows, subject, checks)
        spans = read_json(case_dir / "spans_delta.json")
        span_names = {s.get("name") for s in spans} if isinstance(spans, list) else set()
        missing = TASK_CONTEXT_NODES - span_names
        if not missing:
            checks.append(f"ok: all four node spans present ({sorted(TASK_CONTEXT_NODES)})")
        else:
            fail(f"missing trace spans for nodes: {sorted(missing)}")
        for term in ("scope", "blast"):
            if term in transcript.lower():
                checks.append(f"ok: transcript reports resolved {term}")
            else:
                fail(f"transcript missing the tool's '{term}' content")

    elif kind == "explain":
        approved = [
            r
            for r in rows
            if r.get("status") == "approved" and r.get("tool_name") in ALLOWED_TOOLS
        ]
        if approved:
            checks.append(f"ok: {len(approved)} approved platform tool call(s) in the ledger")
        else:
            fail("no approved platform tool call in the ledger — KB was not consulted")
        check_subject(rows, subject, checks)
        calls = ordered_tool_calls(case_dir)
        if calls:
            first = calls[0]
            if first.startswith("broker:"):
                checks.append(f"ok: first tool call is {first} (KB before files)")
            else:
                fail(f"first tool call is {first}, not a platform tool (order: {calls[:6]})")
        else:
            checks.append("note: tool order not machine-parseable from transcript — review manually")
        if REPO_PATH_RE.search(transcript):
            checks.append("ok: answer cites at least one repo source path")
        else:
            fail("answer carries no citation (no repo source path found)")

    elif kind == "build":
        platform_rows = [r for r in rows if r.get("tool_name") in ALLOWED_TOOLS]
        if platform_rows and platform_rows[0].get("tool_name") == "get_task_context":
            checks.append("ok: first platform call in the ledger is get_task_context")
        else:
            fail(
                f"first platform ledger call is not get_task_context: "
                f"{[r.get('tool_name') for r in platform_rows]}"
            )
        check_subject(rows, subject, checks)
        if REPO_PATH_RE.search(transcript):
            checks.append("ok: plan cites repo source paths")
        else:
            fail("plan carries no citation")

    elif kind == "fallback":
        turn1_rows = ledger_rows(case_dir, "ledger_delta_turn1.json")
        turn2_rows = ledger_rows(case_dir, "ledger_delta_turn2.json")
        if any(r.get("status") == "approved" for r in turn1_rows):
            checks.append("ok: turn 1 (server up) produced an approved ledger row")
        else:
            fail("turn 1 produced no approved ledger row")
        rc2 = int(meta.get("exit_code", 1) or 0)
        turn2 = read_text(case_dir / "transcript_turn2.txt") or read_text(
            case_dir / "transcript_turn2.json"
        )
        if rc2 == 0:
            checks.append("ok: turn 2 (server killed) exited 0 — no visible crash")
        else:
            fail(f"turn 2 exited {rc2} after the server was killed")
        crashes = [m for m in CRASH_MARKERS if m in turn2]
        if crashes:
            fail(f"crash markers in turn-2 transcript: {crashes}")
        else:
            checks.append("ok: no crash markers in turn-2 transcript")
        if len(turn2.strip()) > 200:
            checks.append("ok: turn 2 produced a substantive answer from native reads")
        else:
            fail("turn 2 answer is empty/trivial — the agent did not answer completely")
        checks.append(
            f"note: turn-2 ledger delta = {len(turn2_rows)} row(s) "
            "(server was down; no server process existed to write an error row)"
        )

    elif kind == "budget":
        # The load-bearing evidence is the denial + a complete answer anyway.
        # (Approved-under-cap behavior is separately proven by every generous-
        # cap case; a zero cap makes the denial deterministic.)
        approved = [r for r in rows if r.get("status") == "approved"]
        denied = [r for r in rows if r.get("status") == "denied"]
        checks.append(f"note: {len(approved)} approved / {len(denied)} denied under the tiny cap")
        if denied:
            checks.append(f"ok: {len(denied)} denied call(s) — the budget notice fired")
        else:
            fail("no denied ledger row — the cap was never hit")
        check_subject(rows, subject, checks)
        low = transcript.lower()
        for term in ("generation_cache", "retrieval_event"):
            if term in low:
                checks.append(f"ok: answer covers {term}")
            else:
                fail(f"answer does not cover {term} — completion degraded too far")

    else:
        result.verdict = "SKIP"
        result.reason = f"unknown case kind '{kind}'"

    if result.verdict == "FAIL":
        result.detail = tail(full_capture(case_dir))
    return result


def grade_t5(evidence: Path, cases: list[CaseResult]) -> list[CaseResult]:
    out: list[CaseResult] = []
    t5 = evidence / "t5"

    ledger = read_json(t5 / "ledger_window.json")
    r = CaseResult("t5-ledger-completeness", "both", "governance", "PASS")
    if not isinstance(ledger, list):
        r.verdict = "SKIP"
        r.reason = "t5/ledger_window.json missing — t5_governance.sh did not run"
    else:
        # Host-subject rows are the graded universe. Rows under any OTHER
        # subject are reported verbatim (a dev registry can carry probe/eval
        # traffic); a HOST call misattributed to another subject cannot hide
        # here — every case's own check_subject pins its rows to its subject.
        host_rows = [x for x in ledger if x.get("agent_name") in EXPECTED_SUBJECTS]
        other_subjects: dict[str, int] = {}
        for x in ledger:
            name = str(x.get("agent_name"))
            if name not in EXPECTED_SUBJECTS:
                other_subjects[name] = other_subjects.get(name, 0) + 1
        if other_subjects:
            r.checks.append(f"note: non-host subjects also in the window: {other_subjects}")
        bad_status = [
            x for x in host_rows if x.get("status") not in {"approved", "denied", "error"}
        ]
        if bad_status:
            r.verdict = "FAIL"
            r.checks.append(f"FAIL: unexpected statuses: {[x.get('status') for x in bad_status]}")
        else:
            r.checks.append(f"ok: {len(host_rows)} host rows, all status ∈ approved/denied/error")
        bad_tool = [x for x in host_rows if x.get("tool_name") not in ALLOWED_TOOLS]
        if bad_tool:
            r.verdict = "FAIL"
            r.checks.append(
                f"FAIL: non-allowlisted tools in ledger: "
                f"{sorted({str(x.get('tool_name')) for x in bad_tool})}"
            )
        else:
            r.checks.append("ok: only kb_search/get_task_context rows")
        per_case = 0
        for case_dir in sorted((evidence / "cases").iterdir()):
            per_case += len(ledger_rows(case_dir))
            per_case += len(ledger_rows(case_dir, "ledger_delta_turn1.json"))
            per_case += len(ledger_rows(case_dir, "ledger_delta_turn2.json"))
        if per_case == len(host_rows):
            r.checks.append(f"ok: per-case deltas sum to the window total ({per_case}) — zero gaps")
        else:
            r.verdict = "FAIL"
            r.checks.append(
                f"FAIL: per-case deltas sum to {per_case} but the window holds "
                f"{len(host_rows)} host rows"
            )
    out.append(r)

    scan = read_json(t5 / "secret_scan.json")
    r = CaseResult("t5-secret-scan", "both", "governance", "PASS")
    if not isinstance(scan, dict):
        r.verdict = "SKIP"
        r.reason = "t5/secret_scan.json missing"
    else:
        hits = {
            f"{group}.{name}": count
            for group, entries in scan.items()
            for name, count in entries.items()
            if count
        }
        if hits:
            r.verdict = "FAIL"
            r.checks.append(f"FAIL: secret matches found (zero tolerance): {hits}")
        else:
            r.checks.append("ok: zero secret-value and zero secret-pattern matches")
    out.append(r)

    dash = read_json(t5 / "dashboard.json")
    r = CaseResult("t5-dashboard", "both", "governance", "PASS")
    if not isinstance(dash, dict):
        r.verdict = "SKIP"
        r.reason = "t5/dashboard.json missing"
    elif dash.get("exit_code") == 0 and dash.get("rendered_html") and dash.get("rendered_md"):
        r.checks.append("ok: make dashboard exited 0 and rendered dashboard.html + dashboard.md")
    else:
        r.verdict = "FAIL"
        r.checks.append(f"FAIL: dashboard render: {dash}")
        r.detail = tail(read_text(t5 / "dashboard_render.log"))
    out.append(r)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    evidence: Path = args.evidence
    results: list[CaseResult] = []

    preflight = read_json(evidence / "preflight" / "baseline.json")
    r = CaseResult("t1-preflight", "both", "preflight", "PASS")
    if isinstance(preflight, dict) and preflight.get("active_kb_version"):
        r.checks.append(
            f"ok: active kb_version={preflight['active_kb_version']}, baseline "
            f"retrieval_event={preflight['retrieval_event_count']}, "
            f"trace_span={preflight['trace_span_count']}"
        )
    else:
        r.verdict = "FAIL"
        r.checks.append("FAIL: preflight baseline missing or no active KB")
    results.append(r)

    cases_dir = evidence / "cases"
    if cases_dir.is_dir():
        for case_dir in sorted(cases_dir.iterdir()):
            if case_dir.is_dir():
                results.append(grade_case(case_dir, args.repo))

    results.extend(grade_t5(evidence, [c for c in results if c.kind != "governance"]))

    flake_retries = sum(1 for c in results if c.attempts > 1)
    flake_signature_cases = []
    if cases_dir.is_dir():
        for case_dir in sorted(cases_dir.iterdir()):
            if case_dir.is_dir() and FLAKE_RE.search(full_capture(case_dir)):
                flake_signature_cases.append(case_dir.name)

    failed = [c for c in results if c.verdict == "FAIL"]
    skipped = [c for c in results if c.verdict == "SKIP"]
    gate = "PASS" if not failed else "FAIL"

    lines: list[str] = []
    lines.append("# Host integration grading report")
    lines.append("")
    versions = read_text(evidence / "preflight" / "versions.txt").strip()
    if versions:
        lines.append("## Environment")
        lines.append("```")
        lines.append(versions)
        lines.append("```")
        lines.append("")
    lines.append("## Matrix")
    lines.append("")
    lines.append("| Case | Host | Kind | Verdict | Attempts |")
    lines.append("|---|---|---|---|---|")
    for c in results:
        verdict = c.verdict if not c.reason else f"{c.verdict} ({c.reason})"
        lines.append(f"| {c.case_id} | {c.host} | {c.kind} | {verdict} | {c.attempts} |")
    lines.append("")
    lines.append(
        f"**Flakes:** {flake_retries} case(s) retried once on a provider-error signature; "
        f"flake signatures present in: {flake_signature_cases or 'none'}"
    )
    lines.append("")
    lines.append("## Per-case checks")
    for c in results:
        lines.append("")
        lines.append(f"### {c.case_id} — {c.verdict}")
        if c.reason:
            lines.append(f"- skip reason: {c.reason}")
        for chk in c.checks:
            lines.append(f"- {chk}")
        if c.detail:
            lines.append("")
            lines.append("Verbatim failure detail (transcript tail):")
            lines.append("```")
            lines.append(c.detail)
            lines.append("```")
    lines.append("")
    lines.append(f"**GATE VERDICT: {gate}** — {len(failed)} failed, {len(skipped)} skipped, "
                 f"{len(results) - len(failed) - len(skipped)} passed.")
    report = "\n".join(lines)

    out_path = args.out or (evidence / "report.md")
    out_path.write_text(report)
    print(report)
    return 0 if gate == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
