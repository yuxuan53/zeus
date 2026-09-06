#!/usr/bin/env python3
# Created: 2026-09-05
# Last reused or audited: 2026-09-05
# Authority basis: window-A admission study 2026-09-05 — out-of-sample check of the veto
#   shipped in src/engine/day0_admission.py gate 10. Chain truth only: settlement and
#   exits are reduced from on-chain payouts and tx_hash-deduped fills, never from a
#   locally cached realized_pnl_usd.
"""Replay the Day0 held-ask repricing veto over settled day0 entries.

THE PREDICATE, recomputed here exactly as the live stamp computes it: for each FILLED
entry command, take the decision snapshot's ``captured_at`` as T and count the DISTINCT
``orderbook_top_ask`` values the HELD token showed in [T - 10 min, T). ``venue_commands.
token_id`` IS the held token (verified: NO for every buy_no, YES for every buy_yes).

The window EXCLUDES T. The study's SQL says ``BETWEEN start AND T`` — syntactically
closed — but it formats bounds without the '+00:00' the column stores, so the row at
exactly T compared FALSE and never entered the count; the measured window was half-open
in effect. Both variants were replayed against chain truth: [T-10min, T) reproduces the
published n=95 / -$382.53, while a genuinely closed [T-10min, T] gives n=114 / -$419.41.
Excluding T is also correct on its own terms — the sealed book AT T is the decision, not
evidence that the book moved before it.

The count is computed by CALLING the shipped stamp, so this script is a regression test
of the live predicate rather than a second implementation that could agree with the
numbers while production does something else.

P&L is chain truth per position, pro-rated onto its filled entry commands by cost share:
fully-exited positions net exit proceeds minus cost; still-held positions settle at the
on-chain payout ratio. Fills are deduped by tx_hash first — the 0x-placeholder/UUID pair
is one fill, and double-counting it corrupts every dollar figure downstream.

EXPECTED (window A, 2026-07-20..09-04, day0_nowcast_entry):
    removed n=95  net -$382.53      test W34-36 removed n=50  net -$179.12
The transfer check on forecast_qkernel_entry is printed too: the same cut there removes
a set that is POSITIVE out of sample, which is why gate 10 lives behind the
DAY0_EXTREME_UPDATED guard and is day0-only by construction.

READ-ONLY: ``mode=ro`` + ``PRAGMA query_only=ON``, explicit absolute DB paths (a worktree
resolves STATE_DIR to an empty directory, so nothing is inferred from it).
"""

from __future__ import annotations

import argparse
import collections
import math
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_TRADES_DB = "/Users/leofitz/zeus/state/zeus_trades.db"

WINDOW_A_END = "2026-09-04T07:46:00+00:00"
TEST_WEEKS = ("2026-W34", "2026-W35", "2026-W36")
DAY0_LANE = "day0_nowcast_entry"
TRANSFER_LANE = "forecast_qkernel_entry"


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


_STATE_PRIORITY = {"MATCHED": 0, "RETRYING": 0, "MINED": 1, "CONFIRMED": 2, "FAILED": -1}


def _dedup_fills(
    rows: list[sqlite3.Row], command_size: float | None = None
) -> tuple[float, float | None, str | None]:
    """Total shares, size-weighted price, earliest fill time — one row per on-chain fill.

    The venue_trade_facts dedup trap: ONE on-chain fill can appear under two different
    trade_id values (a placeholder equal to the tx_hash, then a real UUID) carrying the
    SAME tx_hash, plus an 'edli:' echo row with a NULL tx_hash. Fill identity is the
    tx_hash whenever there is a real one; a tx-less row is kept only when no tx-keyed
    row already claims its rounded SIZE, because the echo can restate the same price in
    a different float representation (0.5399999... vs 0.54), so price cannot gate the
    match. Summing the duplicates instead would inflate every dollar figure downstream.
    """

    groups: dict[tuple, tuple[int, float, float, str | None]] = {}
    tx_less: list[tuple[float, float, str | None, int]] = []
    for row in rows:
        try:
            size = float(row["filled_size"])
            price = float(row["fill_price"])
        except (TypeError, ValueError):
            continue
        stamp = row["venue_timestamp"] or row["observed_at"]
        priority = _STATE_PRIORITY.get(str(row["state"]), 0)
        tx_hash = row["tx_hash"]
        if tx_hash and len(str(tx_hash)) == 66:
            key = ("tx", str(tx_hash))
            prior = groups.get(key)
            if prior is None or priority >= prior[0]:
                groups[key] = (priority, size, price, stamp)
        else:
            tx_less.append((round(size, 6), price, stamp, priority))

    tx_sizes = {round(value[1], 6) for value in groups.values()}
    for size, price, stamp, priority in tx_less:
        if size in tx_sizes:
            continue
        key = ("notx", size, price)
        prior = groups.get(key)
        if prior is None or priority >= prior[0]:
            groups[key] = (priority, size, price, stamp)

    if not groups:
        return 0.0, None, None
    total = sum(value[1] for value in groups.values())
    notional = sum(value[1] * value[2] for value in groups.values())
    earliest = min((value[3] for value in groups.values() if value[3]), default=None)
    if command_size and total > 1.05 * command_size and len(groups) > 1:
        # Multiple groups summing over budget is a residual-duplication signal; a single
        # genuine fill above nominal size is left alone rather than silently rescaled.
        scale = (1.05 * command_size) / total
        total *= scale
        notional *= scale
    return total, (notional / total if total > 0 else None), earliest


def _load_entries(conn: sqlite3.Connection, *, start: str, end: str) -> list[dict]:
    """FILLED entry commands in the window, joined to lane/direction/settlement facts."""

    commands = conn.execute(
        """
        SELECT vc.command_id, vc.position_id, vc.token_id, vc.snapshot_id, vc.size,
               p.strategy_key, p.direction, p.condition_id,
               s.captured_at AS decision_captured_at
          FROM venue_commands vc
          JOIN position_current p ON p.position_id = vc.position_id
          JOIN executable_market_snapshots s ON s.snapshot_id = vc.snapshot_id
         WHERE vc.intent_kind = 'ENTRY' AND vc.side = 'BUY' AND vc.state = 'FILLED'
           AND vc.created_at >= ?
           AND p.strategy_key IN (?, ?)
         ORDER BY vc.created_at
        """,
        (start, DAY0_LANE, TRANSFER_LANE),
    ).fetchall()

    rows: list[dict] = []
    for cmd in commands:
        fills = conn.execute(
            """
            SELECT trade_id, state, filled_size, fill_price, tx_hash, observed_at,
                   venue_timestamp
              FROM venue_trade_facts WHERE command_id = ? ORDER BY local_sequence
            """,
            (cmd["command_id"],),
        ).fetchall()
        shares, price, fill_time = _dedup_fills(fills, cmd["size"])
        if shares <= 0 or price is None or fill_time is None:
            continue
        if str(fill_time) >= end:
            continue
        rows.append(
            {
                "command_id": cmd["command_id"],
                "position_id": cmd["position_id"],
                "token_id": cmd["token_id"],
                "lane": cmd["strategy_key"],
                "direction": cmd["direction"],
                "condition_id": cmd["condition_id"],
                "decision_captured_at": cmd["decision_captured_at"],
                "shares": shares,
                "fill_price": price,
                "cost_usd": shares * price,
                "iso_week": "{}-W{:02d}".format(*_parse(fill_time).isocalendar()[:2]),
            }
        )
    return rows


def _position_net(conn: sqlite3.Connection, rows: list[dict]) -> dict[str, dict]:
    """Chain-truth net per position: exits at their proceeds, the rest at on-chain payout."""

    by_position: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_position[row["position_id"]].append(row)

    out: dict[str, dict] = {}
    for position_id, entries in by_position.items():
        cost = sum(entry["cost_usd"] for entry in entries)
        shares = sum(entry["shares"] for entry in entries)
        # Dedup PER EXIT COMMAND, not over the pooled rows: two exit commands can
        # legitimately fill the same size at the same price, and pooling them would
        # collapse the tx-less pair into one and undercount the proceeds.
        exit_commands = conn.execute(
            """
            SELECT command_id, size FROM venue_commands
             WHERE position_id = ? AND intent_kind = 'EXIT' AND side = 'SELL'
               AND state = 'FILLED'
             ORDER BY created_at
            """,
            (position_id,),
        ).fetchall()
        exit_shares = 0.0
        exit_proceeds = 0.0
        for exit_command in exit_commands:
            fills = conn.execute(
                """
                SELECT trade_id, state, filled_size, fill_price, tx_hash, observed_at,
                       venue_timestamp
                  FROM venue_trade_facts WHERE command_id = ? ORDER BY local_sequence
                """,
                (exit_command["command_id"],),
            ).fetchall()
            size, price, _ = _dedup_fills(fills, exit_command["size"])
            if size <= 0 or price is None:
                continue
            exit_shares += size
            exit_proceeds += size * price
        remaining = shares - exit_shares

        if remaining <= max(1e-6, 0.01 * shares):
            out[position_id] = {"net": exit_proceeds - cost, "cost": cost, "resolved": True}
            continue
        direction = entries[0]["direction"]
        outcome_index = 0 if direction == "buy_yes" else 1 if direction == "buy_no" else None
        payout = None
        if outcome_index is not None:
            payout = conn.execute(
                """
                SELECT state, payout_numerator, payout_denominator
                  FROM payout_observations
                 WHERE condition_id = ? AND outcome_index = ? AND superseded_by IS NULL
                 ORDER BY observed_at DESC LIMIT 1
                """,
                (entries[0]["condition_id"], outcome_index),
            ).fetchone()
        if (
            payout is not None
            and str(payout["state"]) in {"RESOLVED_ZERO", "RESOLVED_NONZERO"}
            and payout["payout_denominator"]
        ):
            ratio = float(payout["payout_numerator"]) / float(payout["payout_denominator"])
            net = exit_proceeds + remaining * ratio - cost
            out[position_id] = {"net": net, "cost": cost, "resolved": True}
        else:
            out[position_id] = {"net": None, "cost": cost, "resolved": False}
    return out


def _distinct_ask_count(
    conn: sqlite3.Connection, *, token_id: str, window_end: datetime, minutes: int
) -> int | None:
    """Distinct asks in [T - minutes, T), computed by the SHIPPED stamp itself.

    This calls ``stamp_day0_held_ask_repricing`` rather than re-deriving the count
    from a parallel query, so the replay grades the live predicate and fails if the
    stamp ever drifts. A second implementation here could agree with the published
    numbers while the code that actually runs does something else.
    """

    from src.engine.event_reactor_adapter import (
        DAY0_ASK_DISTINCT_10MIN_KEY,
        stamp_day0_held_ask_repricing,
    )

    payload: dict[str, object] = {"event_type": "DAY0_EXTREME_UPDATED"}
    stamp_day0_held_ask_repricing(
        payload,
        held_token_id=token_id,
        book_captured_at=window_end,
        trade_conn=conn,
    )
    count = payload.get(DAY0_ASK_DISTINCT_10MIN_KEY)
    return int(count) if count is not None else None


def _stats(rows: list[dict]) -> tuple[int, float, float, float]:
    net = sum(row["net"] for row in rows)
    cost = sum(row["cost_usd"] for row in rows)
    return len(rows), net, cost, (net / cost if cost > 0 else math.nan)


def _lcb(rows: list[dict], draws: int = 2000, seed: int = 7) -> float:
    """5% cluster-bootstrap lower bound on total net, clustered by decision date.

    Entries sharing a decision day share the same book regime and the same weather
    surprise; treating them as independent would understate the interval.
    """

    if not rows:
        return math.nan
    clusters: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        clusters[str(row["decision_captured_at"])[:10]].append(row)
    keys = list(clusters)
    rng = random.Random(seed)
    totals = []
    for _ in range(draws):
        total = 0.0
        for _ in range(len(keys)):
            total += sum(r["net"] for r in clusters[keys[rng.randrange(len(keys))]])
        totals.append(total)
    totals.sort()
    return totals[int(0.05 * draws)]


def _report(label: str, rows: list[dict]) -> None:
    n, net, cost, ratio = _stats(rows)
    print(
        f"  {label:<26} n={n:>4}  net=${net:>+9.2f}  cost=${cost:>9.2f}  net/cost={ratio:>+7.3f}"
    )


def _weeks(rows: list[dict]) -> str:
    by_week: dict[str, float] = collections.defaultdict(float)
    for row in rows:
        by_week[row["iso_week"]] += row["net"]
    return "  ".join(
        f"{week[-3:]}:{'-' if value < 0 else '+'}" for week, value in sorted(by_week.items())
    )


def replay_lane(rows: list[dict], lane: str, *, threshold: int) -> None:
    lane_rows = [row for row in rows if row["lane"] == lane]
    removed = [r for r in lane_rows if r["ask_count"] is not None and r["ask_count"] >= threshold]
    kept = [r for r in lane_rows if not (r["ask_count"] is not None and r["ask_count"] >= threshold)]

    print(f"{lane}  (window A, settled, chain truth)")
    _report("ALL", lane_rows)
    _report(f"REMOVED (>= {threshold})", removed)
    _report("KEPT", kept)
    print(f"  removed 5% LCB           ${_lcb(removed):>+9.2f}")
    print(f"  removed per-week sign    {_weeks(removed)}")
    _, net_all, cost_all, ratio_all = _stats(lane_rows)
    _, _, _, ratio_kept = _stats(kept)
    print(f"  kept - all net/cost      {ratio_kept - ratio_all:>+7.3f}")

    test_rows = [r for r in lane_rows if r["iso_week"] in TEST_WEEKS]
    test_removed = [r for r in test_rows if r in removed]
    test_kept = [r for r in test_rows if r not in removed]
    print(f"  -- held out {TEST_WEEKS[0][-3:]}..{TEST_WEEKS[-1][-3:]} --")
    _report("test ALL", test_rows)
    _report(f"test REMOVED (>= {threshold})", test_removed)
    _report("test KEPT", test_kept)
    _, _, _, ratio_test_all = _stats(test_rows)
    _, _, _, ratio_test_kept = _stats(test_kept)
    print(f"  test kept - all net/cost {ratio_test_kept - ratio_test_all:>+7.3f}")
    print(f"  test removed week sign   {_weeks(test_removed)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades-db", default=DEFAULT_TRADES_DB)
    parser.add_argument("--start", default="2026-07-20")
    parser.add_argument("--end", default=WINDOW_A_END)
    # No --window-minutes knob: the window lives inside the shipped stamp this replay
    # calls, and a flag that silently failed to change it would be a lie.
    parser.add_argument("--threshold", type=int, default=None)
    args = parser.parse_args()

    from src.engine.day0_admission import (
        DAY0_ASK_REPRICING_MIN_DISTINCT,
        DAY0_ASK_REPRICING_WINDOW_MINUTES,
    )

    # Default to the SHIPPED constants so the replay grades the live rule, not a
    # parallel copy of it that could drift away from what the gate actually does.
    minutes = DAY0_ASK_REPRICING_WINDOW_MINUTES
    threshold = args.threshold or DAY0_ASK_REPRICING_MIN_DISTINCT

    conn = _ro(args.trades_db)
    try:
        entries = _load_entries(conn, start=args.start, end=args.end)
        settlements = _position_net(conn, entries)
        rows = []
        for entry in entries:
            position = settlements.get(entry["position_id"])
            if position is None or not position["resolved"] or position["cost"] <= 0:
                continue
            entry["net"] = position["net"] * (entry["cost_usd"] / position["cost"])
            entry["ask_count"] = _distinct_ask_count(
                conn,
                token_id=entry["token_id"],
                window_end=_parse(entry["decision_captured_at"]),
                minutes=minutes,
            )
            rows.append(entry)
    finally:
        conn.close()

    print(
        f"replay {args.start}..{args.end}  window={minutes}min  threshold=>={threshold}"
        f"  settled entries={len(rows)}"
    )
    print()
    replay_lane(rows, DAY0_LANE, threshold=threshold)
    print("TRANSFER CHECK — the cut is day0-only by construction, not by choice:")
    replay_lane(rows, TRANSFER_LANE, threshold=threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
