#!/usr/bin/env python3
# Created: 2026-09-04
# Last reused or audited: 2026-09-04
# Authority basis: diurnal-residual study 2026-09-04 (REPORT.md §5) — out-of-sample
#   check of the veto shipped in src/engine/day0_admission.py gate 9. Fit once at
#   --fit-date, replay strictly after it, so every certificate is graded by an artifact
#   that could not have seen its own day.
"""Replay the Day0 diurnal-residual veto over settled day0 decisions.

TWO POPULATIONS, both read-only:

  (a) Day0 ActionableTradeCertificates (zeus-world.db decision_certificates, payload
      ``_edli_day0_q_mode == 'remaining_day'``). These are COUNTERFACTUAL — all of them
      carry ``submitted=False`` — so their P&L is what a $1 unit on the held token would
      have returned at the certificate's own decision-time price p0, graded against
      VERIFIED settlement. It measures the RULE, not realized capital.

  (b) Live ``day0_nowcast_entry`` positions (zeus_trades.db position_current), graded at
      their actual ``entry_price`` against the same settlement truth. Real capital.

P&L convention, $1 unit on the token the candidate would HOLD:
    won  -> 1 - p0
    lost ->   - p0
The veto set must be NEGATIVE and the kept set must be no worse than the full set.

READ-ONLY: ``mode=ro`` + ``PRAGMA query_only=ON``, explicit DB paths (a worktree
resolves STATE_DIR to an empty directory, so nothing is inferred from it).
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import re
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from src.calibration.day0_diurnal_residual import DiurnalResidualNowcast  # noqa: E402

DEFAULT_WORLD_DB = "/Users/leofitz/zeus/state/zeus-world.db"
DEFAULT_FORECAST_DB = "/Users/leofitz/zeus/state/zeus-forecasts.db"
DEFAULT_TRADES_DB = "/Users/leofitz/zeus/state/zeus_trades.db"


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


def _settlements(forecast_db: str, start: str, end: str) -> dict:
    return {
        (row["city"], row["target_date"], row["temperature_metric"]): float(
            row["settlement_value"]
        )
        for row in _query(
            forecast_db,
            """
            SELECT city, target_date, temperature_metric, settlement_value
            FROM settlement_outcomes
            WHERE authority = 'VERIFIED' AND settlement_value IS NOT NULL
              AND target_date BETWEEN ? AND ?
            """,
            (start, end),
        )
    }


def _held_price(payload: dict) -> float | None:
    """p0 — the same anchor src/engine/event_reactor_adapter.py's gate reads."""

    economics = payload.get("qkernel_execution_economics") or {}
    correction = economics.get("market_anchored_correction") or {}
    if correction.get("applied") is True and correction.get("p0") is not None:
        return float(correction["p0"])
    raw = economics.get("global_expected_fill_price_before_fee")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _carrier_gap(payload: dict, metric: str, running: float, unit: str) -> float | None:
    authority = payload.get("day0_probability_authority") or {}
    for block in (authority, authority.get("global_current_observation_payload") or {}):
        members = block.get("remaining_carrier_future_extremes_c") or block.get(
            "_edli_day0_remaining_carrier_future_extremes_c"
        )
        if not isinstance(members, (list, tuple)) or not members:
            continue
        values = sorted(float(value) for value in members)
        middle = len(values) // 2
        center_c = (
            values[middle]
            if len(values) % 2
            else 0.5 * (values[middle - 1] + values[middle])
        )
        center = center_c * 9.0 / 5.0 + 32.0 if unit == "F" else center_c
        return center - running if metric == "high" else running - center
    return None


def _parse_bin(label: str) -> tuple[float | None, float | None]:
    from src.data.market_scanner import _parse_temp_range

    return _parse_temp_range(label or "")


def _bin_pays(low: float | None, high: float | None, settled: float) -> bool:
    if low is not None and settled < low - 1e-9:
        return False
    if high is not None and settled > high + 1e-9:
        return False
    return not (low is None and high is None)


def _report(name: str, rows: list[dict]) -> None:
    if not rows:
        print(f"  {name:<34} n=0")
        return
    total = sum(row["pnl"] for row in rows)
    wins = sum(1 for row in rows if row["won"])
    cost = sum(row["p0"] for row in rows)
    print(
        f"  {name:<34} n={len(rows):>5}  win={wins / len(rows):>6.3f}  "
        f"mean_p0={cost / len(rows):>6.3f}  PnL/unit={total / len(rows):>+8.4f}  "
        f"total=${total:>+9.2f}"
    )


def _bootstrap(rows: list[dict], draws: int = 500, seed: int = 7) -> tuple[float, float]:
    """Cluster bootstrap by city-day — slices within one city-day are not independent."""

    if not rows:
        return math.nan, math.nan
    clusters = collections.defaultdict(list)
    for row in rows:
        clusters[(row["city"], row["target_date"])].append(row)
    keys = list(clusters)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = []
        for _ in range(len(keys)):
            sample.extend(clusters[keys[rng.randrange(len(keys))]])
        means.append(sum(row["pnl"] for row in sample) / len(sample))
    means.sort()
    return means[int(0.025 * draws)], means[int(0.975 * draws)]


def replay_certificates(
    *,
    nowcast: DiurnalResidualNowcast,
    world_db: str,
    forecast_db: str,
    start: str,
    end: str,
    cities: dict,
) -> None:
    settlements = _settlements(forecast_db, start, end)
    rows = _query(
        world_db,
        """
        SELECT decision_time, payload_json
        FROM decision_certificates
        WHERE certificate_type = 'ActionableTradeCertificate'
          AND decision_time >= ? AND decision_time < datetime(?, '+1 day')
        ORDER BY decision_time
        """,
        (start, end),
    )
    counts = collections.Counter()
    evaluable: list[dict] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("_edli_day0_q_mode") != "remaining_day":
            continue
        counts["day0_certificates"] += 1
        city_name = str(payload.get("city") or "")
        target_date = str(payload.get("target_date") or "")
        metric = str(payload.get("temperature_metric") or payload.get("metric") or "")
        direction = str(payload.get("direction") or "")
        city = cities.get(city_name)
        settled = settlements.get((city_name, target_date, metric))
        if city is None or settled is None:
            counts["no_settlement"] += 1
            continue
        p0 = _held_price(payload)
        running = payload.get("high_so_far" if metric == "high" else "low_so_far")
        low, high = _parse_bin(str(payload.get("bin_label") or ""))
        if p0 is None or running is None or (low is None and high is None):
            counts["incomplete"] += 1
            continue
        unit = str(getattr(city, "settlement_unit", "") or "").strip().upper()
        decision = datetime.fromisoformat(row["decision_time"])
        local = decision.astimezone(ZoneInfo(str(getattr(city, "timezone", "UTC"))))
        verdict = nowcast.held_probability(
            city=city_name,
            metric=metric,
            direction=direction,
            local_hour=local.hour + local.minute / 60.0,
            running_extreme=float(running),
            bin_low=low,
            bin_high=high,
            gap=_carrier_gap(payload, metric, float(running), unit),
        )
        if verdict is None:
            counts["nowcast_unavailable"] += 1
            continue
        yes_pays = _bin_pays(low, high, settled)
        won = yes_pays if direction == "buy_yes" else not yes_pays
        evaluable.append(
            {
                "city": city_name,
                "target_date": target_date,
                "metric": metric,
                "direction": direction,
                "p0": p0,
                "won": won,
                "pnl": (1.0 - p0) if won else -p0,
                "q_nc": verdict.q_held,
                "basis": verdict.basis,
                "vetoed": p0 >= verdict.q_held,
            }
        )
    print("=" * 104)
    print(f"(a) DAY0 ACTIONABLE CERTIFICATES {start}..{end} — $1/unit on the held token")
    print("=" * 104)
    for key, value in counts.most_common():
        print(f"  {key}: {value}")
    print(f"  evaluable: {len(evaluable)}")
    print()
    vetoed = [row for row in evaluable if row["vetoed"]]
    kept = [row for row in evaluable if not row["vetoed"]]
    _report("FULL (no veto)", evaluable)
    _report("KEPT (p0 < q_nowcast)", kept)
    _report("VETOED (p0 >= q_nowcast)", vetoed)
    for name, subset in (("FULL", evaluable), ("KEPT", kept), ("VETOED", vetoed)):
        low_ci, high_ci = _bootstrap(subset)
        print(f"    {name:<8} PnL/unit 95% CI [{low_ci:+.4f}, {high_ci:+.4f}]")
    print()
    print("  by metric:")
    for metric in ("high", "low"):
        _report(
            f"  {metric} VETOED",
            [row for row in vetoed if row["metric"] == metric],
        )
        _report(f"  {metric} KEPT", [row for row in kept if row["metric"] == metric])
    print()
    print("  by direction:")
    for direction in ("buy_yes", "buy_no"):
        _report(
            f"  {direction} VETOED",
            [row for row in vetoed if row["direction"] == direction],
        )
    print()
    print("  shrink basis of the vetoed set:")
    for basis, n in collections.Counter(row["basis"] for row in vetoed).most_common():
        print(f"    {basis}: {n}")
    print()
    full_mean = sum(r["pnl"] for r in evaluable) / len(evaluable) if evaluable else 0.0
    kept_mean = sum(r["pnl"] for r in kept) / len(kept) if kept else 0.0
    veto_mean = sum(r["pnl"] for r in vetoed) / len(vetoed) if vetoed else 0.0
    print(f"  VERDICT: vetoed {veto_mean:+.4f}/unit (must be < 0); "
          f"kept {kept_mean:+.4f} vs full {full_mean:+.4f} "
          f"(kept must be >= full): "
          f"{'PASS' if veto_mean < 0 and kept_mean >= full_mean else 'FAIL'}")


def _running_extremes_at(world_db: str, keys: set) -> dict:
    """{(city, date): [(utc_timestamp, running_max, running_min)]} for the pinned ledgers.

    The entry's running extreme is not stored on the position row, so it is read back
    from the same hourly ledger the fitter uses, taking the last observation at or
    before the fill. This is a REPLAY-only read; the live path never does it.
    """

    if not keys:
        return {}
    cities = sorted({city for city, _ in keys})
    rows = _query(
        world_db,
        """
        SELECT city, target_date, utc_timestamp, running_max, running_min, temp_current
        FROM observation_instants
        WHERE city IN (%s) AND utc_timestamp IS NOT NULL
          AND source IN ('wu_icao_history', 'hko_hourly_accumulator',
                         'ogimet_metar_llbg', 'ogimet_metar_ltfm', 'ogimet_metar_uuww')
        ORDER BY city, target_date, utc_timestamp
        """
        % ",".join("?" * len(cities)),
        tuple(cities),
    )
    table = collections.defaultdict(list)
    for row in rows:
        key = (row["city"], row["target_date"])
        if key not in keys:
            continue
        high = row["running_max"] if row["running_max"] is not None else row["temp_current"]
        low = row["running_min"] if row["running_min"] is not None else row["temp_current"]
        if high is None and low is None:
            continue
        table[key].append((str(row["utc_timestamp"]), high, low))
    return table


def _running_extreme_before(series: list, when: str, metric: str) -> float | None:
    running = None
    for timestamp, high, low in series:
        if timestamp > when:
            break
        value = high if metric == "high" else low
        if value is None:
            continue
        running = (
            value
            if running is None
            else (max(running, value) if metric == "high" else min(running, value))
        )
    return None if running is None else float(running)


def replay_positions(
    *,
    nowcast: DiurnalResidualNowcast,
    trades_db: str,
    forecast_db: str,
    world_db: str,
    cities: dict,
) -> None:
    positions = _query(
        trades_db,
        """
        SELECT p.city, p.target_date, p.temperature_metric, p.direction, p.bin_label,
               p.entry_price, p.cost_basis_usd, p.shares, p.phase, p.unit,
               p.settlement_price, p.realized_pnl_usd,
               (SELECT MIN(e.occurred_at) FROM position_events e
                 WHERE e.position_id = p.position_id
                   AND e.event_type IN ('ENTRY_ORDER_FILLED', 'CHAIN_SYNCED',
                                        'ENTRY_ORDER_POSTED')) AS entry_at
        FROM position_current p
        WHERE p.strategy_key = 'day0_nowcast_entry'
        ORDER BY p.target_date
        """,
    )
    dates = [row["target_date"] for row in positions if row["target_date"]]
    settlements = _settlements(forecast_db, min(dates), max(dates)) if dates else {}
    ledger = _running_extremes_at(
        world_db,
        {
            (row["city"], row["target_date"])
            for row in positions
            if float(row["cost_basis_usd"] or 0.0) > 0.0
        },
    )
    counts = collections.Counter()
    evaluable: list[dict] = []
    for row in positions:
        counts["positions"] += 1
        # A `voided` position never took capital (cost_basis_usd == 0): the order was
        # cancelled or the market resolved away before a fill. Grading them as if they
        # had traded inflates the unit P&L of whichever set they fall into and measures
        # nothing about the veto, so only positions that actually risked money count.
        cost = float(row["cost_basis_usd"] or 0.0)
        if str(row["phase"] or "") == "voided" or cost <= 0.0:
            counts["never_filled"] += 1
            continue
        city_name = str(row["city"] or "")
        metric = str(row["temperature_metric"] or "")
        city = cities.get(city_name)
        settled = settlements.get((city_name, row["target_date"], metric))
        entry = row["entry_price"]
        if city is None or settled is None or entry in (None, ""):
            counts["no_settlement_or_price"] += 1
            continue
        low, high = _parse_bin(str(row["bin_label"] or ""))
        if low is None and high is None:
            counts["unparseable_bin"] += 1
            continue
        entry_at = row["entry_at"]
        if not entry_at:
            counts["no_entry_time"] += 1
            continue
        entry_utc = datetime.fromisoformat(str(entry_at))
        running = _running_extreme_before(
            ledger.get((city_name, row["target_date"]), []),
            entry_utc.isoformat(),
            metric,
        )
        if running is None:
            counts["no_running_extreme"] += 1
            continue
        local = entry_utc.astimezone(ZoneInfo(str(getattr(city, "timezone", "UTC"))))
        verdict = nowcast.held_probability(
            city=city_name,
            metric=metric,
            direction=str(row["direction"] or ""),
            local_hour=local.hour + local.minute / 60.0,
            running_extreme=running,
            bin_low=low,
            bin_high=high,
        )
        if verdict is None:
            counts["nowcast_unavailable"] += 1
            continue
        # Grade from the position's OWN recorded settlement where it exists (the money
        # path's truth), falling back to the bin-vs-settlement reconstruction otherwise.
        settlement_price = row["settlement_price"]
        if settlement_price is not None:
            won = float(settlement_price) > 0.5
        else:
            yes_pays = _bin_pays(low, high, settled)
            won = yes_pays if row["direction"] == "buy_yes" else not yes_pays
        p0 = float(entry)
        shares = float(row["shares"] or 0.0)
        evaluable.append(
            {
                "city": city_name,
                "target_date": row["target_date"],
                "metric": metric,
                "direction": row["direction"],
                "p0": p0,
                "won": won,
                "pnl": (1.0 - p0) if won else -p0,
                "cost_usd": cost,
                "pnl_usd": (
                    float(row["realized_pnl_usd"])
                    if row["realized_pnl_usd"] is not None
                    else ((shares - cost) if won else -cost)
                ),
                "vetoed": p0 >= verdict.q_held,
            }
        )
    print()
    print("=" * 104)
    print("(b) LIVE day0_nowcast_entry POSITIONS — real capital, graded at entry_price")
    print("=" * 104)
    for key, value in counts.most_common():
        print(f"  {key}: {value}")
    print(f"  evaluable: {len(evaluable)}")
    print()
    vetoed = [row for row in evaluable if row["vetoed"]]
    kept = [row for row in evaluable if not row["vetoed"]]
    for name, subset in (
        ("FULL (no veto)", evaluable),
        ("KEPT (p0 < q_nowcast)", kept),
        ("DROPPED (p0 >= q_nowcast)", vetoed),
    ):
        _report(name, subset)
        if subset:
            cost = sum(row["cost_usd"] for row in subset)
            realized = sum(row["pnl_usd"] for row in subset)
            print(
                f"      real capital: cost=${cost:>9,.0f}  PnL=${realized:>+9,.0f}  "
                f"ROI={realized / max(cost, 1e-9):>+7.1%}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--world-db", default=DEFAULT_WORLD_DB)
    parser.add_argument("--forecast-db", default=DEFAULT_FORECAST_DB)
    parser.add_argument("--trades-db", default=DEFAULT_TRADES_DB)
    parser.add_argument("--start", default="2026-08-05")
    parser.add_argument("--end", default="2026-09-03")
    args = parser.parse_args()

    from src.config import runtime_cities_by_name

    cities = runtime_cities_by_name()
    with open(args.artifact, "r", encoding="utf-8") as handle:
        nowcast = DiurnalResidualNowcast(json.load(handle))
    print(f"artifact fit_date={nowcast.fit_date} replay window {args.start}..{args.end}")
    print()
    replay_certificates(
        nowcast=nowcast,
        world_db=args.world_db,
        forecast_db=args.forecast_db,
        start=args.start,
        end=args.end,
        cities=cities,
    )
    replay_positions(
        nowcast=nowcast,
        trades_db=args.trades_db,
        forecast_db=args.forecast_db,
        world_db=args.world_db,
        cities=cities,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
