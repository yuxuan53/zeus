#!/usr/bin/env python3
# Created: 2026-09-04
# Last reused or audited: 2026-09-04
# Authority basis: diurnal-residual study 2026-09-04 (scratchpad/diurnal/REPORT.md §5).
#   Row construction mirrors the study's build_clim.py / build_clim2.py / merge_clim.py;
#   the histogram cells and shrink constants live with the server in
#   src/calibration/day0_diurnal_residual.py so fit and serve can never disagree.
"""Fit the Day0 diurnal-residual artifact ``state/day0_diurnal_residual.json``.

WHAT IS FITTED. The empirical distribution of D = final_extreme - running_extreme by
(metric, k = hours-to-peak, NWP-gap band), as raw COUNTS per cell. The artifact stores
counts, not the 2.2M source records, so the loader reads it in milliseconds; the
Empirical-Bayes shrink is applied at serve time from those counts.

ROW SOURCE, per city, one hourly ledger family:
  * ``observation_instants`` source ``wu_icao_history`` for the 50 WU cities;
  * ``ogimet_metar_*`` for Tel Aviv / Istanbul / Moscow (WU does not cover them);
  * ``hko_hourly_accumulator`` for Hong Kong -- the openmeteo grid archive carries a
    -0.8 degC median bias against the HKO settlement station, so it is never used.
The cumulative extreme is RECOMPUTED per day from the per-hour running_max/running_min
rather than trusted as stored, and ``final`` is the VERIFIED ``settlement_outcomes``
value when one exists in the same unit, else the day's own cumulative extreme.

WALK-FORWARD. Every record whose date is >= ``fit_date`` is DROPPED. That is the whole
walk-forward guarantee: the server does no date filtering, so an artifact that contained
the target day's own records would leak the outcome into the decision that trades it.
``--fit-date`` therefore also names the first day the artifact may legitimately serve.

READ-ONLY w.r.t. the live databases (``mode=ro`` + ``PRAGMA query_only``); writes only
the JSON artifact.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sqlite3
import statistics
import sys
from datetime import date, datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from src.calibration.day0_diurnal_residual import (  # noqa: E402
    GAP_BAND_EDGES,
    J_MAX,
    SCHEMA_VERSION,
    gap_band_index,
)

DEFAULT_WORLD_DB = os.path.join(REPO, "state", "zeus-world.db")
DEFAULT_FORECAST_DB = os.path.join(REPO, "state", "zeus-forecasts.db")
DEFAULT_OUT = os.path.join(REPO, "state", "day0_diurnal_residual.json")

HISTORY_START = "2024-01-01"
# A WU day needs near-complete hourly coverage before its cumulative curve is
# trustworthy; the ogimet/HKO ledgers are sparser, so they carry their own floor.
MIN_HOURS_WU = 22
MIN_HOURS_ALT = 20
WU_SOURCE = "wu_icao_history"
# Cities WU does not cover, pinned to the hourly ledger that agrees with their
# settlement station (study build_clim2.py).
ALT_SOURCE_CITIES = {
    "Hong Kong": "hko_hourly_accumulator",
    "Tel Aviv": "ogimet_metar_llbg",
    "Istanbul": "ogimet_metar_ltfm",
    "Moscow": "ogimet_metar_uuww",
}


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _query(path: str, sql: str, args: tuple = ()) -> list[dict]:
    conn = _ro(path)
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _hourly_days(world_db: str) -> tuple[dict, dict]:
    """{(city, date): {hour: (hi, lo)}} and {city: unit} from the pinned ledgers."""

    rows = _query(
        world_db,
        """
        SELECT city, target_date, source, local_hour, running_max, running_min,
               temp_current, temp_unit
        FROM observation_instants
        WHERE target_date >= ? AND local_hour IS NOT NULL
          AND (source = ? OR (city IN (%s) AND source IN (%s)))
        ORDER BY city, target_date, local_hour
        """
        % (
            ",".join("?" * len(ALT_SOURCE_CITIES)),
            ",".join("?" * len(ALT_SOURCE_CITIES)),
        ),
        (HISTORY_START, WU_SOURCE, *ALT_SOURCE_CITIES.keys(), *ALT_SOURCE_CITIES.values()),
    )
    days: dict = collections.defaultdict(dict)
    unit: dict = {}
    for row in rows:
        city = row["city"]
        source = row["source"]
        if source != WU_SOURCE and ALT_SOURCE_CITIES.get(city) != source:
            continue
        if city in ALT_SOURCE_CITIES and source == WU_SOURCE:
            # A pinned city uses only its pinned ledger, never a mixed envelope.
            continue
        try:
            hour = int(round(float(row["local_hour"])))
        except (TypeError, ValueError):
            continue
        if not 0 <= hour <= 23:
            continue
        high = row["running_max"]
        low = row["running_min"]
        if high is None:
            high = row["temp_current"]
        if low is None:
            low = row["temp_current"]
        if high is None:
            continue
        if low is None:
            low = high
        key = (city, row["target_date"])
        bucket = days[key]
        if hour in bucket:
            # Duplicate hour: keep the extreme envelope, as the study does.
            bucket[hour] = (max(bucket[hour][0], high), min(bucket[hour][1], low))
        else:
            bucket[hour] = (high, low)
        unit[city] = str(row["temp_unit"] or "").strip().upper()
    kept = {}
    for (city, day), bucket in days.items():
        floor = MIN_HOURS_ALT if city in ALT_SOURCE_CITIES else MIN_HOURS_WU
        if len(bucket) >= floor:
            kept[(city, day)] = bucket
    return kept, unit


def _verified_settlements(forecast_db: str) -> dict:
    """{(city, date, metric): (value, unit)} for VERIFIED settlements."""

    out = {}
    for row in _query(
        forecast_db,
        """
        SELECT city, target_date, temperature_metric, settlement_value, settlement_unit
        FROM settlement_outcomes
        WHERE authority = 'VERIFIED' AND settlement_value IS NOT NULL
          AND temperature_metric IN ('high', 'low') AND target_date >= ?
        """,
        (HISTORY_START,),
    ):
        out[(row["city"], row["target_date"], row["temperature_metric"])] = (
            float(row["settlement_value"]),
            str(row["settlement_unit"] or "").strip().upper(),
        )
    return out


def _nwp_centers(forecast_db: str) -> dict:
    """{metric: {(city, date): center_c}} — median over models of each model's latest
    ``single_runs`` cycle at lead_days <= 1, i.e. the day-of NWP daily-extreme center."""

    centers: dict = {}
    for metric in ("high", "low"):
        latest: dict = collections.defaultdict(dict)
        for row in _query(
            forecast_db,
            """
            SELECT city, target_date, model, forecast_value_c, source_cycle_time
            FROM raw_model_forecasts
            WHERE metric = ? AND endpoint = 'single_runs' AND lead_days <= 1
              AND target_date >= ? AND forecast_value_c IS NOT NULL
            """,
            (metric, HISTORY_START),
        ):
            key = (row["city"], row["target_date"])
            model = row["model"]
            cycle = row["source_cycle_time"]
            current = latest[key].get(model)
            if current is None or cycle > current[0]:
                latest[key][model] = (cycle, float(row["forecast_value_c"]))
        centers[metric] = {
            key: statistics.median([value for _, value in models.values()])
            for key, models in latest.items()
            if models
        }
    return centers


def _anchor_hours(records: list[dict], metric: str) -> dict:
    """Median first-attainment hour of the day's extreme, per city."""

    by_day: dict = collections.defaultdict(dict)
    for record in records:
        if record["metric"] != metric:
            continue
        by_day[(record["city"], record["date"])][record["h"]] = record["cum"]
    hours: dict = collections.defaultdict(list)
    for (city, _day), curve in by_day.items():
        final = max(curve.values()) if metric == "high" else min(curve.values())
        for hour in sorted(curve):
            attained = (
                curve[hour] >= final - 1e-9
                if metric == "high"
                else curve[hour] <= final + 1e-9
            )
            if attained:
                hours[city].append(hour)
                break
    return {city: statistics.median(values) for city, values in hours.items() if values}


def build_records(world_db: str, forecast_db: str) -> tuple[list[dict], dict]:
    """Station-hour residual records and the per-city settlement unit."""

    days, unit = _hourly_days(world_db)
    settlements = _verified_settlements(forecast_db)
    records: list[dict] = []
    for (city, day), bucket in days.items():
        city_unit = unit.get(city, "")
        hours = sorted(bucket)
        running_high = -math.inf
        running_low = math.inf
        cum_high: dict = {}
        cum_low: dict = {}
        for hour in hours:
            high, low = bucket[hour]
            running_high = max(running_high, high)
            running_low = min(running_low, low)
            cum_high[hour] = running_high
            cum_low[hour] = running_low
        for metric, cumulative, day_extreme in (
            ("high", cum_high, running_high),
            ("low", cum_low, running_low),
        ):
            final = day_extreme
            settled = settlements.get((city, day, metric))
            if settled is not None and settled[1] == city_unit:
                final = settled[0]
            for hour in hours:
                residual = (
                    final - cumulative[hour]
                    if metric == "high"
                    else cumulative[hour] - final
                )
                records.append(
                    {
                        "city": city,
                        "date": day,
                        "metric": metric,
                        "h": hour,
                        "cum": cumulative[hour],
                        "D": residual,
                        "unit": city_unit,
                    }
                )
    return records, unit


def build_artifact(
    records: list[dict],
    *,
    unit: dict,
    nwp: dict,
    fit_date: str,
) -> dict:
    """Histogram counts per cell, from records strictly before ``fit_date``."""

    peak = _anchor_hours(records, "high")
    trough = _anchor_hours(records, "low")
    training = [record for record in records if record["date"] < fit_date]
    pooled: dict = collections.defaultdict(lambda: [0] * (J_MAX + 1))
    gap: dict = collections.defaultdict(lambda: [0] * (J_MAX + 1))
    city_cells: dict = collections.defaultdict(lambda: [0] * (J_MAX + 1))
    for record in training:
        metric = record["metric"]
        city = record["city"]
        anchor = peak.get(city) if metric == "high" else trough.get(city)
        if anchor is None:
            continue
        k = int(round(anchor - record["h"]))
        j = min(J_MAX, max(0, int(round(record["D"]))))
        pooled[f"{metric}|{k}"][j] += 1
        city_cells[f"{metric}|{city}|{k}"][j] += 1
        center = nwp[metric].get((city, record["date"]))
        if center is None:
            continue
        if record["unit"] == "F":
            center = center * 9.0 / 5.0 + 32.0
        offset = (
            center - record["cum"] if metric == "high" else record["cum"] - center
        )
        band = gap_band_index(offset)
        if band is not None:
            gap[f"{metric}|{k}|{band}"][j] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "fit_date": fit_date,
        "fitted_at_utc": datetime.now(timezone.utc).isoformat(),
        "history_start": HISTORY_START,
        "j_max": J_MAX,
        "gap_band_edges": [
            [None if math.isinf(low) else low, None if math.isinf(high) else high]
            for low, high in GAP_BAND_EDGES
        ],
        "record_counts": {
            "total": len(records),
            "training": len(training),
            "cities": len(unit),
            "pooled_cells": len(pooled),
            "gap_cells": len(gap),
            "city_cells": len(city_cells),
        },
        "peak_hours": peak,
        "trough_hours": trough,
        "unit": unit,
        "pooled": dict(pooled),
        "gap": dict(gap),
        "city": dict(city_cells),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world-db", default=DEFAULT_WORLD_DB)
    parser.add_argument("--forecast-db", default=DEFAULT_FORECAST_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--fit-date",
        default=None,
        help="Exclude records on/after this date (default: today UTC). Also the "
        "first date the artifact may serve.",
    )
    args = parser.parse_args()
    fit_date = args.fit_date or datetime.now(timezone.utc).date().isoformat()
    date.fromisoformat(fit_date)

    records, unit = build_records(args.world_db, args.forecast_db)
    nwp = _nwp_centers(args.forecast_db)
    artifact = build_artifact(records, unit=unit, nwp=nwp, fit_date=fit_date)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, separators=(",", ":"), sort_keys=True)
    counts = artifact["record_counts"]
    print(
        f"wrote {args.out} fit_date={fit_date} "
        f"records={counts['total']} training={counts['training']} "
        f"cities={counts['cities']} pooled={counts['pooled_cells']} "
        f"gap={counts['gap_cells']} city={counts['city_cells']} "
        f"bytes={os.path.getsize(args.out)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
