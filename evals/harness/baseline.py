"""Baseline load/save and delta + verdict computation (docs/contracts/evals-report.md)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from harness.metrics import HIGHER_IS_BETTER, LOWER_IS_BETTER, MetricValue

BASELINE_SCHEMA_VERSION = "1.0.0"
REGRESSION_THRESHOLD = 0.05  # 5% relative

Verdict = Literal["improved", "flat", "regressed", "no_baseline"]


@dataclass(frozen=True)
class Delta:
    old: float
    new: float
    relative: float


@dataclass(frozen=True)
class BaselineComparison:
    present: bool
    deltas: dict[str, Delta]
    verdict: Verdict
    biggest_mover: str | None


def load_baseline(path: Path) -> dict[str, MetricValue] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    if version != BASELINE_SCHEMA_VERSION:
        raise ValueError(
            f"baseline schema_version {version!r} != {BASELINE_SCHEMA_VERSION!r}; "
            "regenerate with run.py --update-baseline"
        )
    return {
        name: MetricValue(value=entry["value"], status=entry["status"])
        for name, entry in raw["metrics"].items()
    }


def write_baseline(path: Path, metrics: dict[str, MetricValue], git_sha: str | None) -> None:
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "metrics": {name: metric.as_dict() for name, metric in metrics.items()},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare(
    baseline: dict[str, MetricValue] | None, current: dict[str, MetricValue]
) -> BaselineComparison:
    if baseline is None:
        return BaselineComparison(
            present=False, deltas={}, verdict="no_baseline", biggest_mover=None
        )

    deltas: dict[str, Delta] = {}
    for name, metric in current.items():
        old = baseline.get(name)
        # compare only metrics measured (numeric) in both runs
        if old is None or old.value is None or metric.value is None:
            continue
        deltas[name] = Delta(
            old=old.value, new=metric.value, relative=_relative(old.value, metric.value)
        )

    regressed = any(_is_worse(name, delta) for name, delta in deltas.items())
    improved = any(_is_better(name, delta) for name, delta in deltas.items())
    verdict: Verdict = "regressed" if regressed else "improved" if improved else "flat"
    biggest = max(deltas, key=lambda name: abs(deltas[name].relative), default=None)
    return BaselineComparison(present=True, deltas=deltas, verdict=verdict, biggest_mover=biggest)


def _relative(old: float, new: float) -> float:
    if old == 0:
        return 0.0 if new == 0 else float("inf") if new > 0 else float("-inf")
    return (new - old) / abs(old)


def _is_worse(name: str, delta: Delta) -> bool:
    if name in LOWER_IS_BETTER:
        return delta.relative > REGRESSION_THRESHOLD
    if name in HIGHER_IS_BETTER:
        return delta.relative < -REGRESSION_THRESHOLD
    return False


def _is_better(name: str, delta: Delta) -> bool:
    if name in LOWER_IS_BETTER:
        return delta.relative < -REGRESSION_THRESHOLD
    if name in HIGHER_IS_BETTER:
        return delta.relative > REGRESSION_THRESHOLD
    return False
