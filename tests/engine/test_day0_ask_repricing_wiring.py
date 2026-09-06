# Created: 2026-09-05
# Authority basis: window-A chain-truth measurement of the day0_nowcast_entry lane
#   (2026-07-20..09-04, tx_hash-deduped fills). Entries whose HELD token's ask took
#   >= 2 distinct values in the prior 10 minutes are n=95 / net -$382.53 (net/cost
#   -0.563, 7/7 ISO weeks negative) against n=220 / -$106.57 for the rest.
"""Wiring contracts for the Day0 held-ask repricing stamp.

The predicate itself is unit-tested in tests/test_day0_admission.py. What is proved
HERE is the half that decides whether the predicate ever sees the truth: which token's
book is counted, which rows fall inside the window, what counts as a distinct price,
and that every fault leaves the payload unstamped so the gate stays inert.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.engine.event_reactor_adapter import (
    DAY0_ASK_DISTINCT_10MIN_KEY,
    DAY0_ASK_WINDOW_END_KEY,
    DAY0_ASK_WINDOW_START_KEY,
    stamp_day0_held_ask_repricing,
)
from src.state.snapshot_repo import init_snapshot_schema

T = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)
YES_TOKEN = "111000111"
NO_TOKEN = "222000222"


@pytest.fixture()
def trade_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    # Real DDL, not a hand-copied fixture: a schema that drifts from production is a
    # green test pinning a phantom surface.
    init_snapshot_schema(conn)
    yield conn
    conn.close()


def _insert(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    token: str,
    captured_at: datetime,
    ask: str,
) -> None:
    conn.execute(
        """
        INSERT INTO executable_market_snapshots (
            snapshot_id, gamma_market_id, event_id, condition_id, question_id,
            yes_token_id, no_token_id, selected_outcome_token_id, outcome_label,
            enable_orderbook, active, closed, min_tick_size, min_order_size,
            fee_details_json, token_map_json, neg_risk,
            orderbook_top_bid, orderbook_top_ask, orderbook_depth_json,
            raw_gamma_payload_hash, raw_clob_market_info_hash, raw_orderbook_hash,
            authority_tier, captured_at, freshness_deadline
        ) VALUES (?, 'gm', 'ev', 'cond', 'q', ?, ?, ?, ?, 1, 1, 0, '0.01', '5',
                  '{}', '{}', 0, '0.40', ?, '{}', 'h1', 'h2', 'h3', 'CLOB', ?, ?)
        """,
        (
            snapshot_id,
            YES_TOKEN,
            NO_TOKEN,
            token,
            "YES" if token == YES_TOKEN else "NO",
            ask,
            captured_at.isoformat(),
            (captured_at + timedelta(minutes=5)).isoformat(),
        ),
    )
    conn.commit()


def _payload(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_type": "DAY0_EXTREME_UPDATED",
        "direction": "buy_no",
        "token_id": NO_TOKEN,
    }
    base.update(kw)
    return base


def test_two_distinct_asks_in_window_are_counted(trade_conn) -> None:
    # Three snapshots, two distinct prices: the count is of PRICE LEVELS, not rows,
    # so a book re-polled at an unchanged ask must not look like a reprice.
    _insert(trade_conn, snapshot_id="s1", token=NO_TOKEN, captured_at=T - timedelta(minutes=8), ask="0.55")
    _insert(trade_conn, snapshot_id="s2", token=NO_TOKEN, captured_at=T - timedelta(minutes=5), ask="0.55")
    _insert(trade_conn, snapshot_id="s3", token=NO_TOKEN, captured_at=T - timedelta(minutes=2), ask="0.61")
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    assert payload[DAY0_ASK_DISTINCT_10MIN_KEY] == 2
    assert payload[DAY0_ASK_WINDOW_END_KEY] == T.isoformat()
    assert payload[DAY0_ASK_WINDOW_START_KEY] == (T - timedelta(minutes=10)).isoformat()


def test_an_unmoved_book_counts_one(trade_conn) -> None:
    for i, minutes in enumerate((9, 6, 3, 1)):
        _insert(
            trade_conn,
            snapshot_id=f"q{i}",
            token=NO_TOKEN,
            captured_at=T - timedelta(minutes=minutes),
            ask="0.48",
        )
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    assert payload[DAY0_ASK_DISTINCT_10MIN_KEY] == 1


def test_rows_outside_the_window_are_ignored(trade_conn) -> None:
    # Older than T-10min, and the decision instant itself. The sealed book AT T is
    # the decision, not evidence that the book moved before it — counting it would
    # score every candidate one level higher and veto quiet books.
    _insert(trade_conn, snapshot_id="old", token=NO_TOKEN, captured_at=T - timedelta(minutes=11), ask="0.20")
    _insert(trade_conn, snapshot_id="inw", token=NO_TOKEN, captured_at=T - timedelta(minutes=4), ask="0.50")
    _insert(trade_conn, snapshot_id="atT", token=NO_TOKEN, captured_at=T, ask="0.90")
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    assert payload[DAY0_ASK_DISTINCT_10MIN_KEY] == 1


def test_the_decision_snapshot_at_T_is_excluded_from_its_own_window(trade_conn) -> None:
    # Pinned deliberately. The study's SQL reads BETWEEN start AND T (closed), but it
    # formatted bounds without the '+00:00' the column stores, so the row at exactly T
    # compared FALSE and never entered the count — the measured window was half-open.
    # Replayed both ways on chain truth: [T-10min, T) reproduces n=95 / -$382.53, a
    # genuinely closed window gives n=114 / -$419.41. A future "fix" that closes this
    # bound would silently re-grade the lane, so the boundary is a contract.
    _insert(trade_conn, snapshot_id="quiet", token=NO_TOKEN, captured_at=T - timedelta(minutes=4), ask="0.50")
    _insert(trade_conn, snapshot_id="jit", token=NO_TOKEN, captured_at=T, ask="0.77")
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    # Two rows, two prices — but one of them IS the decision, so the book was quiet.
    assert payload[DAY0_ASK_DISTINCT_10MIN_KEY] == 1


def test_absent_asks_are_not_distinct_prices(trade_conn) -> None:
    # 'ABSENT' is a missing quote (8430 of the last 30k live rows), not a price
    # level; treating it as one would veto on a book that never actually repriced.
    _insert(trade_conn, snapshot_id="a1", token=NO_TOKEN, captured_at=T - timedelta(minutes=7), ask="ABSENT")
    _insert(trade_conn, snapshot_id="a2", token=NO_TOKEN, captured_at=T - timedelta(minutes=3), ask="0.52")
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    assert payload[DAY0_ASK_DISTINCT_10MIN_KEY] == 1


def test_buy_no_counts_the_no_token_not_the_yes_token(trade_conn) -> None:
    # The token we HOLD is the one whose ask we lift. Counting the YES book for a
    # buy_no would score an unrelated instrument.
    _insert(trade_conn, snapshot_id="y1", token=YES_TOKEN, captured_at=T - timedelta(minutes=8), ask="0.10")
    _insert(trade_conn, snapshot_id="y2", token=YES_TOKEN, captured_at=T - timedelta(minutes=6), ask="0.30")
    _insert(trade_conn, snapshot_id="y3", token=YES_TOKEN, captured_at=T - timedelta(minutes=4), ask="0.70")
    _insert(trade_conn, snapshot_id="n1", token=NO_TOKEN, captured_at=T - timedelta(minutes=5), ask="0.62")
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    # The NO book was quiet even though the YES book moved three times.
    assert payload[DAY0_ASK_DISTINCT_10MIN_KEY] == 1


def test_no_snapshots_leaves_the_gate_inert(trade_conn) -> None:
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    assert DAY0_ASK_DISTINCT_10MIN_KEY not in payload


def test_missing_token_or_capture_time_leaves_the_gate_inert(trade_conn) -> None:
    _insert(trade_conn, snapshot_id="s1", token=NO_TOKEN, captured_at=T - timedelta(minutes=5), ask="0.55")
    _insert(trade_conn, snapshot_id="s2", token=NO_TOKEN, captured_at=T - timedelta(minutes=2), ask="0.61")

    no_token = _payload()
    stamp_day0_held_ask_repricing(
        no_token, held_token_id=None, book_captured_at=T, trade_conn=trade_conn
    )
    assert DAY0_ASK_DISTINCT_10MIN_KEY not in no_token

    no_time = _payload()
    stamp_day0_held_ask_repricing(
        no_time, held_token_id=NO_TOKEN, book_captured_at=None, trade_conn=trade_conn
    )
    assert DAY0_ASK_DISTINCT_10MIN_KEY not in no_time


def test_a_database_fault_fails_open(trade_conn) -> None:
    trade_conn.execute("DROP TABLE executable_market_snapshots")
    trade_conn.commit()
    payload = _payload()

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    # A broken read must never block a submit — same contract as the nowcast stamp.
    assert DAY0_ASK_DISTINCT_10MIN_KEY not in payload


def test_non_day0_candidate_is_never_stamped(trade_conn) -> None:
    # The cut does not transfer to forecast_qkernel_entry (its removed set is
    # positive out of sample), so the stamp must refuse to score one.
    _insert(trade_conn, snapshot_id="s1", token=NO_TOKEN, captured_at=T - timedelta(minutes=5), ask="0.55")
    _insert(trade_conn, snapshot_id="s2", token=NO_TOKEN, captured_at=T - timedelta(minutes=2), ask="0.61")
    payload = _payload(event_type="FORECAST_SNAPSHOT_READY")

    stamp_day0_held_ask_repricing(
        payload, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )

    assert DAY0_ASK_DISTINCT_10MIN_KEY not in payload


def test_stamped_count_drives_the_admission_gate(trade_conn) -> None:
    # End-to-end on the two halves of the split: the stamp is the only thing the
    # predicate needs, and it must produce a veto on a repriced book and silence
    # on a quiet one.
    from src.engine.day0_admission import (
        Day0AdmissionContext,
        day0_live_admission_rejection_reason,
    )

    def _reason(payload: dict[str, object]) -> str | None:
        return day0_live_admission_rejection_reason(
            Day0AdmissionContext(
                event_type="DAY0_EXTREME_UPDATED",
                metric="high",
                settlement_source_type="wu_icao",
                fast_obs_supported=True,
                source_health_state="OK_FAST_AND_WU",
                execution_mode="maker",
                quote_time_utc=T,
                latest_observation_available_at_utc=T - timedelta(minutes=5),
                in_final_localday_noentry_window=False,
                selected_bin_edge_distance_quanta=3.0,
                edge_survives_one_bin_stress=True,
                held_ask_distinct_count_10min=payload.get(  # type: ignore[arg-type]
                    DAY0_ASK_DISTINCT_10MIN_KEY
                ),
            )
        )

    _insert(trade_conn, snapshot_id="r1", token=NO_TOKEN, captured_at=T - timedelta(minutes=6), ask="0.55")
    _insert(trade_conn, snapshot_id="r2", token=NO_TOKEN, captured_at=T - timedelta(minutes=2), ask="0.61")
    repriced = _payload()
    stamp_day0_held_ask_repricing(
        repriced, held_token_id=NO_TOKEN, book_captured_at=T, trade_conn=trade_conn
    )
    assert _reason(repriced) == "DAY0_ASK_REPRICING_VETO"

    quiet_token = "333000333"
    _insert(trade_conn, snapshot_id="k1", token=quiet_token, captured_at=T - timedelta(minutes=6), ask="0.44")
    _insert(trade_conn, snapshot_id="k2", token=quiet_token, captured_at=T - timedelta(minutes=2), ask="0.44")
    quiet = _payload(token_id=quiet_token)
    stamp_day0_held_ask_repricing(
        quiet, held_token_id=quiet_token, book_captured_at=T, trade_conn=trade_conn
    )
    assert _reason(quiet) is None
