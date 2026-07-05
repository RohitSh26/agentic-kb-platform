"""Markdown rendering for the consolidated report (evals/run_all.py --out).

Pure function over `TierResult`s — no I/O, unit-testable with synthetic data. A failing check's
`detail` (verbatim captured output/exception text) is rendered in a fenced code block, never
folded into prose: see docs/architecture/evaluation-system.md "Generate-and-test loops" for why a
failure must stay objective, machine-parseable data rather than a summary.
"""

from datetime import UTC, datetime

from harness.tier_result import Status, TierResult, overall_status

_STATUS_LABEL: dict[Status, str] = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}


def render_markdown(tiers: list[TierResult], *, git_sha: str | None) -> str:
    lines: list[str] = ["# Consolidated evaluation report", ""]
    lines.append(f"- Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"- git sha: `{git_sha or 'n/a'}`")
    lines.append("")
    lines.append("| Tier | Status | Duration (s) | Checks |")
    lines.append("|---|---|---|---|")
    for tier in tiers:
        lines.append(
            f"| {tier.tier} | {_STATUS_LABEL[tier.status]} | {tier.duration_seconds:.1f} | "
            f"{len(tier.checks)} |"
        )
    lines.append("")

    for tier in tiers:
        lines.append(f"## {tier.tier} — {tier.title}")
        lines.append("")
        for check in tier.checks:
            duration_note = f" ({check.duration_seconds:.1f}s)" if check.duration_seconds else ""
            lines.append(f"- **{check.name}**: {_STATUS_LABEL[check.status]}{duration_note}")
            if check.reason:
                lines.append(f"  - reason: {check.reason}")
            for key, value in check.metrics.items():
                lines.append(f"  - {key}: {value}")
            if check.detail:
                lines.append("  - detail (verbatim, last lines):")
                lines.append("    ```")
                for detail_line in check.detail.splitlines():
                    lines.append(f"    {detail_line}")
                lines.append("    ```")
        lines.append("")

    overall = overall_status(tiers)
    lines.append(f"**Overall: {_STATUS_LABEL[overall]}**")
    lines.append("")
    lines.append(
        "Every metric above comes from this run's own subprocess/tool output — no number here is "
        "invented. Skips carry their own reason and are not failures (see 'Degradation over "
        "failure', docs/architecture/evaluation-system.md). Failures carry the verbatim captured "
        "output above, not a paraphrase, so a human or a bounded runtime retry loop has objective "
        "data to act on — see 'Generate-and-test loops' in the same doc for what may and may not "
        "loop against these results."
    )
    return "\n".join(lines)
