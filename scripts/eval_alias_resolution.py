"""eval_alias_resolution — full alias golden-set run against a locally built KB (PR-38).

Resolves every case in `evals/retrieval_cases/alias_golden_v1.yaml` against the LIVE
`alias_reference` index of a real Postgres registry (a `build` run — see
docs/dev-guide/22-testing-and-builds.md "Running an end-to-end build locally"), and prints
per-case hit/miss plus top-1 accuracy. Target: >= 80% (docs/contracts/alias-reference.md).

Imports `agentic_kb_builder.alias` directly (the resolution algorithm kb-builder owns —
duplicating it here would violate DRY) plus evals' lightweight, dependency-free
`harness.alias` loader/scorer (added to sys.path; it needs only pydantic + pyyaml, both
already kb-builder dependencies) — so this script is one thing:

    cd services/kb-builder
    export DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb   # a BUILT kb
    uv run python ../../scripts/eval_alias_resolution.py

Writes nothing; the caller is expected to record a real run's output into
docs/reports/alias-accuracy-<date>.md (never fabricated — an empty index exits non-zero
with a clear reason instead of printing invented numbers).
"""

import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "evals"))  # harness.alias (pure loader/scorer, no DB)

from agentic_kb_builder.alias.resolve import resolve  # noqa: E402
from agentic_kb_builder.alias.run import load_alias_entries  # noqa: E402
from harness.alias import AliasResult, aggregate, load_alias_suite  # noqa: E402

GOLDEN_PATH = _REPO_ROOT / "evals" / "retrieval_cases" / "alias_golden_v1.yaml"
TOP1_ACCURACY_TARGET = 0.80


async def _resolve_all(database_url: str) -> list[AliasResult]:
    suite = load_alias_suite(GOLDEN_PATH)
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            entries = await load_alias_entries(session)
    finally:
        await engine.dispose()
    if not entries:
        raise RuntimeError(
            "no live alias_reference rows found in this registry — build a local KB first "
            "(docs/dev-guide/22-testing-and-builds.md 'Running an end-to-end build locally')"
        )
    results: list[AliasResult] = []
    for case in suite.cases:
        resolution = resolve(case.query, entries)
        targets = resolution.targets if resolution is not None else ()
        results.append(AliasResult(case=case, resolved_targets=targets))
    return results


def _print_report(results: list[AliasResult]) -> bool:
    from harness.alias import top1_hit

    for result in results:
        outcome = "HIT " if top1_hit(result) else "MISS"
        top1 = result.resolved_targets[0] if result.resolved_targets else "(no match)"
        print(f"{outcome}  {result.case.id:45s} query={result.case.query!r:55s} top1={top1}")

    report = aggregate(results)
    print(f"\n{report.hits}/{report.cases} top-1 hits = {report.top1_accuracy:.1%}")
    if report.misses:
        print(f"misses ({len(report.misses)}): {', '.join(report.misses)}")
    passed = report.top1_accuracy >= TOP1_ACCURACY_TARGET
    verdict = "PASS" if passed else "FAIL"
    print(f"{verdict} (target >= {TOP1_ACCURACY_TARGET:.0%})")
    return passed


async def _main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "DATABASE_URL is not set — point it at a locally built KB registry "
            "(docs/dev-guide/22-testing-and-builds.md).",
            file=sys.stderr,
        )
        return 2
    try:
        results = await _resolve_all(database_url)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0 if _print_report(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
