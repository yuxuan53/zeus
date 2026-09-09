"""Shared Day0 window selection helpers."""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from src.signal.day0_extrema import RemainingMemberExtrema
from src.signal.ensemble_signal import select_hours_for_target_date
from src.types.metric_identity import HIGH_LOCALDAY_MAX, MetricIdentity


def _parse_forecast_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def condition_day0_hourly_members_on_current_state(
    members_hourly: np.ndarray,
    times: list[str],
    *,
    observation_time: datetime,
    current_temp: float,
    e_fold_hours: float,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Condition future hourly paths on the latest observed model residual.

    The correction is causal and transient: each model's residual at the
    latest elapsed hourly anchor decays exponentially through unseen hours.
    The elapsed anchor itself is replaced by the observation so the final
    sub-hour fallback cannot resurrect the model value.
    """

    values = np.asarray(members_hourly, dtype=float)
    if (
        values.ndim != 2
        or values.shape[1] != len(times)
        or not np.isfinite(values).all()
        or not np.isfinite(float(current_temp))
        or not np.isfinite(float(e_fold_hours))
        or float(e_fold_hours) <= 0.0
        or observation_time.tzinfo is None
    ):
        return None
    try:
        instants = [_parse_forecast_timestamp(value) for value in times]
    except (TypeError, ValueError):
        return None
    observed_utc = observation_time.astimezone(timezone.utc)
    elapsed = [index for index, instant in enumerate(instants) if instant <= observed_utc]
    if not elapsed:
        return None
    anchor_idx = max(elapsed, key=lambda index: instants[index])
    anchor_lag = observed_utc - instants[anchor_idx]
    if not timedelta(0) <= anchor_lag <= timedelta(hours=1):
        return None

    conditioned = values.copy()
    innovations = float(current_temp) - conditioned[:, anchor_idx]
    conditioned[:, anchor_idx] = float(current_temp)
    for index, instant in enumerate(instants):
        if instant <= observed_utc:
            continue
        lead_hours = (instant - observed_utc).total_seconds() / 3600.0
        conditioned[:, index] += innovations * np.exp(
            -lead_hours / float(e_fold_hours)
        )
    return conditioned, innovations


def remaining_member_extrema_for_day0(
    members_hourly: np.ndarray,
    times: list[str],
    timezone_name: str,
    target_d: date,
    *,
    now: datetime | None = None,
    temperature_metric: MetricIdentity = HIGH_LOCALDAY_MAX,
    causal_window_start: datetime | None = None,
) -> tuple[RemainingMemberExtrema | None, float]:
    """Select remaining target-date local hours for Day0 observation logic.

    Returns (RemainingMemberExtrema, hours_remaining). Returns (None, 0.0) when
    no remaining hours exist. HIGH sets maxes; LOW sets mins.

    Args:
        temperature_metric: MetricIdentity instance. Bare strings are rejected;
            callers holding a string must convert via MetricIdentity.from_raw().
        causal_window_start: Explicitly declares a complete hourly suffix from
            this aware boundary's local-day anchor through the target day's end.
            Requires aware ``now``; omission preserves the full-day input contract.
    """
    if isinstance(temperature_metric, str):
        raise TypeError(
            f"remaining_member_extrema_for_day0 requires a MetricIdentity instance "
            f"for temperature_metric, got str {temperature_metric!r}. "
            f"Convert via MetricIdentity.from_raw() at the caller seam."
        )

    tz = ZoneInfo(timezone_name)
    if causal_window_start is not None:
        return _causal_grid_member_extrema_for_day0(
            members_hourly,
            times,
            tz=tz,
            target_d=target_d,
            now=now,
            causal_window_start=causal_window_start,
            temperature_metric=temperature_metric,
        )

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_local = now_utc.astimezone(tz)
    try:
        target_day_idxs = select_hours_for_target_date(
            target_d,
            tz,
            times=times,
        )
    except ValueError:
        return None, 0.0

    remaining_idxs = [
        int(idx)
        for idx in target_day_idxs
        if _parse_forecast_timestamp(times[int(idx)]) > now_utc
    ]
    if not remaining_idxs and now_local.date() == target_d:
        day_end = datetime.combine(
            target_d + timedelta(days=1),
            datetime_time.min,
            tzinfo=tz,
        )
        elapsed = [
            int(idx)
            for idx in target_day_idxs
            if _parse_forecast_timestamp(times[int(idx)]) < now_utc
        ]
        if elapsed:
            anchor_idx = max(
                elapsed,
                key=lambda idx: _parse_forecast_timestamp(times[idx]),
            )
            anchor_time = _parse_forecast_timestamp(times[anchor_idx])
            day_end_utc = day_end.astimezone(timezone.utc)
            if (
                timedelta(0) < day_end_utc - now_utc <= timedelta(hours=1)
                and timedelta(0) <= now_utc - anchor_time <= timedelta(hours=1)
            ):
                remaining_idxs = [anchor_idx]
    if not remaining_idxs:
        return None, 0.0

    slice_data = members_hourly[:, remaining_idxs]
    if temperature_metric.is_low():
        arr = slice_data.min(axis=1)
    else:
        arr = slice_data.max(axis=1)
    return RemainingMemberExtrema.for_metric(arr, temperature_metric), float(len(remaining_idxs))


def _causal_grid_member_extrema_for_day0(
    members_hourly: np.ndarray,
    times: list[str],
    *,
    tz: ZoneInfo,
    target_d: date,
    now: datetime | None,
    causal_window_start: datetime,
    temperature_metric: MetricIdentity,
) -> tuple[RemainingMemberExtrema | None, float]:
    """Select strict-future extrema from one complete causal local-day suffix."""
    # SCOPE: this family and hourly cut. DRAIN: refresh its complete hourly suffix.
    # RESET: each valid subsequent grid is accepted without a latched block.
    if (
        not isinstance(now, datetime)
        or not isinstance(causal_window_start, datetime)
        or now.tzinfo is None
        or causal_window_start.tzinfo is None
        or not isinstance(times, (list, tuple))
    ):
        return None, 0.0
    now_utc = now.astimezone(timezone.utc)
    boundary_utc = causal_window_start.astimezone(timezone.utc)
    if (
        boundary_utc > now_utc
        or boundary_utc.astimezone(tz).date() != target_d
        or now_utc.astimezone(tz).date() != target_d
    ):
        return None, 0.0
    try:
        values = np.asarray(members_hourly, dtype=float)
    except (TypeError, ValueError):
        return None, 0.0
    if (
        values.ndim != 2
        or values.shape[0] == 0
        or values.shape[1] != len(times)
        or not np.isfinite(values).all()
    ):
        return None, 0.0

    local_start = datetime.combine(target_d, datetime_time.min, tzinfo=tz)
    local_end = datetime.combine(
        target_d + timedelta(days=1),
        datetime_time.min,
        tzinfo=tz,
    )
    target_grid: list[datetime] = []
    cursor = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    while cursor < end_utc:
        target_grid.append(cursor)
        cursor += timedelta(hours=1)
    anchors = [instant for instant in target_grid if instant <= boundary_utc]
    if not anchors:
        return None, 0.0
    anchor = anchors[-1]
    if boundary_utc - anchor > timedelta(hours=1):
        return None, 0.0
    expected = tuple(target_grid[target_grid.index(anchor) :])
    actual_values: list[datetime] = []
    for raw in times:
        if not isinstance(raw, str):
            return None, 0.0
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, 0.0
        if instant.tzinfo is None:
            return None, 0.0
        actual_values.append(instant.astimezone(timezone.utc))
    actual = tuple(actual_values)
    if actual != expected:
        return None, 0.0

    remaining_idxs = [
        index for index, instant in enumerate(actual) if instant > now_utc
    ]
    if not remaining_idxs:
        if (
            timedelta(0) < end_utc - now_utc <= timedelta(hours=1)
            and timedelta(0) <= now_utc - actual[-1] <= timedelta(hours=1)
        ):
            remaining_idxs = [len(actual) - 1]
        else:
            return None, 0.0
    slice_data = values[:, remaining_idxs]
    if temperature_metric.is_low():
        arr = slice_data.min(axis=1)
    else:
        arr = slice_data.max(axis=1)
    return RemainingMemberExtrema.for_metric(arr, temperature_metric), float(
        len(remaining_idxs)
    )


# Backward-compat alias `remaining_member_maxes_for_day0` REMOVED in Phase 7B.
# All production callers migrated to remaining_member_extrema_for_day0 in Phase 6.
# All test callers migrated in Phase 7B. Use the dataclass-returning entry point:
#     extrema, hours = remaining_member_extrema_for_day0(...)
#     arr = extrema.maxes  # HIGH — use .mins for LOW
