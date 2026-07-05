"""CheckResult/TierResult status aggregation + exit-code contract (no I/O)."""

from harness.tier_result import CheckResult, TierResult, exit_code, overall_status


def _check(name: str = "c", status: str = "pass", **overrides: object) -> CheckResult:
    base: dict[str, object] = {"name": name, "status": status}
    base.update(overrides)
    return CheckResult(**base)  # type: ignore[arg-type]


def test_tier_with_no_checks_is_a_skip() -> None:
    tier = TierResult("T9", "empty tier", ())
    assert tier.status == "skip"


def test_tier_passes_when_every_check_passes() -> None:
    tier = TierResult("T1", "t", (_check(status="pass"), _check(status="pass")))
    assert tier.status == "pass"


def test_tier_fails_if_any_check_fails() -> None:
    tier = TierResult("T1", "t", (_check(status="pass"), _check(status="fail")))
    assert tier.status == "fail"


def test_tier_skips_only_when_every_check_skips() -> None:
    tier = TierResult("T1", "t", (_check(status="skip"), _check(status="skip")))
    assert tier.status == "skip"


def test_a_mix_of_pass_and_skip_is_a_pass_not_a_skip() -> None:
    # e.g. run.py passed but the pytest sub-check was skipped for some other reason —
    # the tier as a whole did real, successful work, so it must not read as "skip".
    tier = TierResult("T1", "t", (_check(status="pass"), _check(status="skip")))
    assert tier.status == "pass"


def test_fail_beats_skip_in_the_same_tier() -> None:
    tier = TierResult("T1", "t", (_check(status="fail"), _check(status="skip")))
    assert tier.status == "fail"


def test_duration_sums_across_checks() -> None:
    tier = TierResult("T1", "t", (_check(duration_seconds=1.5), _check(duration_seconds=2.5)))
    assert tier.duration_seconds == 4.0


def test_overall_status_fails_if_any_tier_fails() -> None:
    tiers = [
        TierResult("T1", "t", (_check(status="pass"),)),
        TierResult("T2", "t", (_check(status="fail"),)),
    ]
    assert overall_status(tiers) == "fail"
    assert exit_code(tiers) == 1


def test_overall_status_is_skip_only_when_every_tier_skips() -> None:
    tiers = [TierResult("T1", "t", ()), TierResult("T2", "t", (_check(status="skip"),))]
    assert overall_status(tiers) == "skip"
    assert exit_code(tiers) == 0


def test_overall_status_passes_with_a_mix_of_pass_and_skip() -> None:
    tiers = [TierResult("T1", "t", (_check(status="pass"),)), TierResult("T2", "t", ())]
    assert overall_status(tiers) == "pass"
    assert exit_code(tiers) == 0


def test_check_result_metrics_default_to_an_empty_dict_and_are_independent_per_instance() -> None:
    first = CheckResult(name="a", status="pass")
    second = CheckResult(name="b", status="pass")
    first.metrics["x"] = "1"
    assert second.metrics == {}
