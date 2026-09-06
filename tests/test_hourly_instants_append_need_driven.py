# Created: 2026-09-05
# Purpose: Regression tests for the round-3 quota fix in
#   src/data/hourly_instants_append.py: hourly_tick's unconditional 3-day-every-hour
#   re-pull (54 cities x 3 days x 24 ticks/day) is replaced with a need-driven pull
#   that reads the data_coverage ledger's written_fetch_times.
"""TDD for hourly_tick's need-driven date selection.

The change rate of archive-hourly readings is NOT measurable read-only from this
worktree (observation_instants keeps one row per (city, date, hour) with
INSERT OR REPLACE; imported_at is overwritten on every re-pull, so there is no
history to diff). _LATE_PROMOTION_HOT_DAYS=2 and
_LATE_PROMOTION_RECHECK_INTERVAL_HOURS=6.0 are therefore a stated ASSUMPTION, not a
measured cadence: a local day's hourly readings may still be revised for up to 2
days after that day ends, and re-checking more than once every 6h within that
window adds no value. These tests assert the mechanism (need-driven, ledger-backed,
FAILED/partial rows always stay needed), not the specific constant values.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from zoneinfo import ZoneInfo

from src.config import cities_by_name
from src.data import hourly_instants_append
from src.state.db import init_schema


def _memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def _synthetic_rows(city, start_date: date, end_date: date) -> list[dict]:
    """One full day (24 hours) of rows per date in [start_date, end_date]."""
    tz = ZoneInfo(city.timezone)
    rows = []
    d = start_date
    while d <= end_date:
        for hour in range(24):
            local_dt = datetime(d.year, d.month, d.day, hour, tzinfo=tz)
            rows.append({
                "city": city.name,
                "target_date": d.isoformat(),
                "source": hourly_instants_append.SOURCE,
                "timezone_name": city.timezone,
                "local_hour": float(hour),
                "local_timestamp": local_dt.isoformat(),
                "utc_timestamp": local_dt.astimezone(UTC).isoformat(),
                "utc_offset_minutes": 0,
                "dst_active": 0,
                "is_ambiguous_local_hour": 0,
                "is_missing_local_hour": 0,
                "temp_current": 20.0,
                "temp_unit": city.settlement_unit,
                "local_dt": local_dt,
            })
        d += timedelta(days=1)
    return rows


def test_hourly_tick_is_need_driven_against_the_coverage_ledger(monkeypatch) -> None:
    conn = _memdb()
    city = cities_by_name["NYC"]
    calls: list[tuple[str, str]] = []

    def _fetch(city_arg, start_d, end_d):
        calls.append((start_d.isoformat(), end_d.isoformat()))
        return _synthetic_rows(city_arg, start_d, end_d), None

    monkeypatch.setattr(hourly_instants_append, "_fetch_with_retry", _fetch)

    # record_written stamps fetched_at from the real wall clock, not an injected
    # "now" -- anchor boot_now to the real clock so the recheck-interval deltas
    # below are comparable to what _dates_needing_fetch actually measures.
    boot_now = datetime.now(UTC)

    # Boot: nothing written yet -> one call covering the full 3-day window.
    hourly_instants_append.hourly_tick(
        conn, now_utc=boot_now, cities=[city], days_window=3,
    )
    assert len(calls) == 1, f"expected one fetch at boot, got {calls}"

    # Within the recheck interval (1h later): everything already WRITTEN and
    # fresh -- zero new calls.
    hourly_instants_append.hourly_tick(
        conn, now_utc=boot_now + timedelta(hours=1), cities=[city], days_window=3,
    )
    assert len(calls) == 1, f"expected no re-fetch within the recheck interval, got {calls}"

    # After the recheck interval (7h later): only the hot-window dates (the two
    # most recent local days) are re-checked; the oldest date, once WRITTEN and
    # outside the hot window, is never re-pulled again.
    later = boot_now + timedelta(hours=7)
    hourly_instants_append.hourly_tick(
        conn, now_utc=later, cities=[city], days_window=3,
    )
    assert len(calls) == 2, f"expected exactly one re-fetch after 7h, got {calls}"
    refetch_start, refetch_end = calls[1]
    end_d = hourly_instants_append._city_yesterday_local(city, later)
    hot_floor = end_d - timedelta(days=hourly_instants_append._LATE_PROMOTION_HOT_DAYS - 1)
    assert date.fromisoformat(refetch_start) == hot_floor
    assert date.fromisoformat(refetch_end) == end_d


def test_hourly_tick_always_needs_a_failed_or_partial_write(monkeypatch) -> None:
    """A FAILED date, or a partial write (fewer rows than _expected_hours), must
    never be treated as final -- only a full WRITTEN row satisfies the ledger."""
    conn = _memdb()
    city = cities_by_name["NYC"]
    call_count = {"n": 0}

    def _fetch_partial(city_arg, start_d, end_d):
        call_count["n"] += 1
        rows = _synthetic_rows(city_arg, start_d, end_d)
        # Drop half of the newest date's hours so it never reaches _expected_hours
        # and is therefore never marked WRITTEN.
        newest = end_d.isoformat()
        kept = [r for r in rows if r["target_date"] != newest] + [
            r for r in rows if r["target_date"] == newest
        ][:12]
        return kept, None

    monkeypatch.setattr(hourly_instants_append, "_fetch_with_retry", _fetch_partial)

    now = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
    hourly_instants_append.hourly_tick(conn, now_utc=now, cities=[city], days_window=3)
    assert call_count["n"] == 1

    # Even seconds later (well within the recheck interval), the partially
    # written newest date must still be "needed" -- it was never marked WRITTEN.
    hourly_instants_append.hourly_tick(
        conn, now_utc=now + timedelta(seconds=1), cities=[city], days_window=3,
    )
    assert call_count["n"] == 2, "a partial write must stay needed, not be treated as done"


def test_dates_needing_fetch_reads_the_coverage_ledger_not_the_data_table() -> None:
    """The need-driven decision must consult data_coverage (written_fetch_times),
    not observation_instants directly -- observation_instants uses INSERT OR
    REPLACE and its imported_at is silently overwritten on every re-pull, so it
    cannot answer "was this already written and how long ago"."""
    conn = _memdb()
    city = cities_by_name["NYC"]
    end_d = date(2026, 9, 4)
    start_d = end_d - timedelta(days=2)

    from src.state.data_coverage import DataTable, record_written

    # Mark only the OLDEST date WRITTEN via the coverage ledger, with no rows at
    # all in observation_instants -- the ledger alone must drive the decision.
    record_written(
        conn,
        data_table=DataTable.OBSERVATION_INSTANTS,
        city=city.name,
        data_source=hourly_instants_append.SOURCE,
        target_date=start_d,
    )
    conn.commit()

    needed = hourly_instants_append._dates_needing_fetch(
        conn, city, start_d, end_d, now_utc=datetime(2026, 9, 5, 6, 0, tzinfo=UTC),
    )
    assert start_d not in needed, "a ledger-WRITTEN date outside the hot window must be skipped"
    assert end_d in needed
    assert (end_d - timedelta(days=1)) in needed
