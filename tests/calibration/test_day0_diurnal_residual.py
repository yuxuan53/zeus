# Created: 2026-09-04
# Last reused or audited: 2026-09-04
# Authority basis: diurnal-residual study 2026-09-04 (REPORT.md §5) — the estimator's
#   arithmetic is the deliverable, so the three shrink stages are checked against
#   hand-computed values rather than against the implementation's own output.
"""Contract tests for the Day0 diurnal-residual estimator and its fail-open loader."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.calibration import day0_diurnal_residual as mod
from src.calibration.day0_diurnal_residual import (
    GAP_MIN_ROWS,
    J_MAX,
    PRIOR_WEIGHT,
    SCHEMA_VERSION,
    DiurnalResidualNowcast,
    gap_band_index,
    load_day0_diurnal_residual_nowcast,
)

FIT_DATE = "2026-08-04"
NOW = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)


def _counts(**by_j: int) -> list[int]:
    counts = [0] * (J_MAX + 1)
    for key, value in by_j.items():
        counts[int(key[1:])] = value
    return counts


def _artifact(**overrides: object) -> dict:
    base = {
        "schema_version": SCHEMA_VERSION,
        "fit_date": FIT_DATE,
        # Peak 12 => at local hour 10, k = 2.
        "peak_hours": {"Testville": 12.0},
        "trough_hours": {"Testville": 4.0},
        "unit": {"Testville": "C"},
        # pooled (high, k=2): 60 at j=0, 30 at j=1, 10 at j=2 -> n=100.
        "pooled": {"high|2": _counts(j0=60, j1=30, j2=10)},
        "gap": {},
        "city": {},
    }
    base.update(overrides)
    return base


def _pooled_base() -> list[float]:
    """Hand-computed pooled stage: (count + 0.5) / (100 + 0.5 * 13)."""

    denominator = 100 + 0.5 * (J_MAX + 1)
    raw = [60, 30, 10] + [0] * (J_MAX - 2)
    return [(count + 0.5) / denominator for count in raw]


def test_pooled_only_cell_matches_hand_computed_laplace() -> None:
    nowcast = DiurnalResidualNowcast(_artifact())

    result = nowcast.pmf(city="Testville", metric="high", local_hour=10.0)

    assert result is not None
    pmf, basis = result
    assert basis == "pooled"
    expected = _pooled_base()
    total = sum(expected)
    for actual, want in zip(pmf, expected):
        assert actual == pytest.approx(want / total, abs=1e-12)
    # +0.5 Laplace: an unobserved residual keeps strictly positive mass.
    assert pmf[J_MAX] > 0.0
    assert sum(pmf) == pytest.approx(1.0, abs=1e-12)


def test_pooled_plus_gap_cell_matches_hand_computed_prior_25() -> None:
    # gap band for +1.0 is [0.5, 1.5) -> index 2; 40 rows, over GAP_MIN_ROWS.
    assert gap_band_index(1.0) == 2
    gap_counts = _counts(j0=10, j1=20, j2=10)
    assert sum(gap_counts) == 40 >= GAP_MIN_ROWS
    nowcast = DiurnalResidualNowcast(_artifact(gap={"high|2|2": gap_counts}))

    result = nowcast.pmf(city="Testville", metric="high", local_hour=10.0, gap=1.0)

    assert result is not None
    pmf, basis = result
    assert basis == "gap"
    base = _pooled_base()
    expected = [
        (gap_counts[j] + PRIOR_WEIGHT * base[j]) / (40 + PRIOR_WEIGHT)
        for j in range(J_MAX + 1)
    ]
    total = sum(expected)
    for actual, want in zip(pmf, expected):
        assert actual == pytest.approx(want / total, abs=1e-12)


def test_pooled_plus_gap_plus_city_matches_hand_computed_two_stage_shrink() -> None:
    gap_counts = _counts(j0=10, j1=20, j2=10)
    city_counts = _counts(j0=5, j1=5)
    nowcast = DiurnalResidualNowcast(
        _artifact(
            gap={"high|2|2": gap_counts},
            city={"high|Testville|2": city_counts},
        )
    )

    result = nowcast.pmf(city="Testville", metric="high", local_hour=10.0, gap=1.0)

    assert result is not None
    pmf, basis = result
    assert basis == "city"
    base = _pooled_base()
    tilted = [
        (gap_counts[j] + PRIOR_WEIGHT * base[j]) / (40 + PRIOR_WEIGHT)
        for j in range(J_MAX + 1)
    ]
    expected = [
        (city_counts[j] + PRIOR_WEIGHT * tilted[j]) / (10 + PRIOR_WEIGHT)
        for j in range(J_MAX + 1)
    ]
    total = sum(expected)
    for actual, want in zip(pmf, expected):
        assert actual == pytest.approx(want / total, abs=1e-12)


def test_thin_gap_cell_below_minimum_does_not_tilt() -> None:
    thin = _counts(j0=1, j1=1)
    assert sum(thin) < GAP_MIN_ROWS
    nowcast = DiurnalResidualNowcast(_artifact(gap={"high|2|2": thin}))

    with_gap = nowcast.pmf(city="Testville", metric="high", local_hour=10.0, gap=1.0)
    without = nowcast.pmf(city="Testville", metric="high", local_hour=10.0)

    assert with_gap is not None and without is not None
    assert with_gap[1] == "pooled"
    assert with_gap[0] == pytest.approx(without[0], abs=1e-12)


def test_point_bin_probability_reads_the_offset_cell() -> None:
    nowcast = DiurnalResidualNowcast(_artifact())
    pmf, _ = nowcast.pmf(city="Testville", metric="high", local_hour=10.0)

    # running 30.4 rounds to 30; the 32 degC point bin is rel=+2.
    result = nowcast.bin_probability(
        city="Testville",
        metric="high",
        local_hour=10.0,
        running_extreme=30.4,
        bin_low=32.0,
        bin_high=32.0,
    )

    assert result is not None
    assert result[0] == pytest.approx(pmf[2], abs=1e-12)


def test_range_bin_sums_its_cells_and_open_top_sums_the_tail() -> None:
    nowcast = DiurnalResidualNowcast(_artifact())
    pmf, _ = nowcast.pmf(city="Testville", metric="high", local_hour=10.0)

    ranged = nowcast.bin_probability(
        city="Testville",
        metric="high",
        local_hour=10.0,
        running_extreme=30.0,
        bin_low=31.0,
        bin_high=32.0,
    )
    open_top = nowcast.bin_probability(
        city="Testville",
        metric="high",
        local_hour=10.0,
        running_extreme=30.0,
        bin_low=33.0,
        bin_high=None,
    )

    assert ranged is not None and open_top is not None
    assert ranged[0] == pytest.approx(pmf[1] + pmf[2], abs=1e-12)
    assert open_top[0] == pytest.approx(sum(pmf[3:]), abs=1e-12)


def test_bin_already_passed_by_the_absorbing_direction_gets_zero() -> None:
    nowcast = DiurnalResidualNowcast(_artifact())

    # HIGH cannot settle BELOW its running maximum.
    result = nowcast.bin_probability(
        city="Testville",
        metric="high",
        local_hour=10.0,
        running_extreme=30.0,
        bin_low=28.0,
        bin_high=28.0,
    )

    assert result is not None
    assert result[0] == 0.0


def test_low_metric_reverses_the_offset_direction() -> None:
    artifact = _artifact(pooled={"low|2": _counts(j0=60, j1=30, j2=10)})
    nowcast = DiurnalResidualNowcast(artifact)
    # trough 4 => local hour 2 gives k = 2.
    pmf, _ = nowcast.pmf(city="Testville", metric="low", local_hour=2.0)

    below = nowcast.bin_probability(
        city="Testville",
        metric="low",
        local_hour=2.0,
        running_extreme=10.0,
        bin_low=8.0,
        bin_high=8.0,
    )
    above = nowcast.bin_probability(
        city="Testville",
        metric="low",
        local_hour=2.0,
        running_extreme=10.0,
        bin_low=12.0,
        bin_high=12.0,
    )

    assert below is not None and above is not None
    assert below[0] == pytest.approx(pmf[2], abs=1e-12)
    assert above[0] == 0.0


def test_buy_no_held_probability_is_the_complement() -> None:
    nowcast = DiurnalResidualNowcast(_artifact())

    yes = nowcast.held_probability(
        city="Testville",
        metric="high",
        direction="buy_yes",
        local_hour=10.0,
        running_extreme=30.0,
        bin_low=30.0,
        bin_high=30.0,
    )
    no = nowcast.held_probability(
        city="Testville",
        metric="high",
        direction="buy_no",
        local_hour=10.0,
        running_extreme=30.0,
        bin_low=30.0,
        bin_high=30.0,
    )

    assert yes is not None and no is not None
    assert yes.q_held + no.q_held == pytest.approx(1.0, abs=1e-12)
    assert yes.fit_date == FIT_DATE
    assert yes.basis == "pooled"


def test_unknown_city_and_empty_cell_return_none() -> None:
    nowcast = DiurnalResidualNowcast(_artifact())

    assert nowcast.pmf(city="Nowhere", metric="high", local_hour=10.0) is None
    # k = 12 - 3 = 9 has no pooled cell in this artifact.
    assert nowcast.pmf(city="Testville", metric="high", local_hour=3.0) is None


# ----------------------------- loader contract ------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    mod.reset_cache()
    yield
    mod.reset_cache()


def _install(tmp_path, monkeypatch, artifact: object) -> None:
    path = tmp_path / mod.ARTIFACT_FILENAME
    if isinstance(artifact, str):
        path.write_text(artifact, encoding="utf-8")
    else:
        path.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setattr(mod, "artifact_path", lambda: path)


def test_loader_returns_none_when_artifact_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "artifact_path", lambda: tmp_path / "absent.json")

    assert load_day0_diurnal_residual_nowcast(now=NOW) is None


def test_loader_returns_none_on_malformed_artifact(tmp_path, monkeypatch) -> None:
    _install(tmp_path, monkeypatch, "{not json")

    assert load_day0_diurnal_residual_nowcast(now=NOW) is None


def test_loader_returns_none_on_schema_mismatch(tmp_path, monkeypatch) -> None:
    _install(tmp_path, monkeypatch, _artifact(schema_version=SCHEMA_VERSION + 1))

    assert load_day0_diurnal_residual_nowcast(now=NOW) is None


def test_loader_serves_a_fresh_artifact(tmp_path, monkeypatch) -> None:
    _install(tmp_path, monkeypatch, _artifact())

    nowcast = load_day0_diurnal_residual_nowcast(now=NOW)

    assert nowcast is not None
    assert nowcast.fit_date == FIT_DATE


def test_loader_returns_none_past_the_freshness_horizon(tmp_path, monkeypatch) -> None:
    _install(tmp_path, monkeypatch, _artifact())
    stale = NOW + timedelta(days=mod.MAX_ARTIFACT_AGE_DAYS + 1)

    assert load_day0_diurnal_residual_nowcast(now=stale) is None
    # The boundary itself still serves — the horizon is inclusive.
    boundary = datetime.fromisoformat(FIT_DATE).replace(tzinfo=timezone.utc) + timedelta(
        days=mod.MAX_ARTIFACT_AGE_DAYS
    )
    assert load_day0_diurnal_residual_nowcast(now=boundary) is not None


def test_loader_picks_up_a_rewritten_artifact_without_restart(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / mod.ARTIFACT_FILENAME
    monkeypatch.setattr(mod, "artifact_path", lambda: path)
    path.write_text(json.dumps(_artifact()), encoding="utf-8")
    first = load_day0_diurnal_residual_nowcast(now=NOW)
    assert first is not None and first.fit_date == FIT_DATE

    refit = _artifact(fit_date="2026-08-05")
    path.write_text(json.dumps(refit), encoding="utf-8")
    # Force a distinct mtime so the cache key genuinely changes.
    import os

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    second = load_day0_diurnal_residual_nowcast(now=NOW + timedelta(days=1))
    assert second is not None and second.fit_date == "2026-08-05"
