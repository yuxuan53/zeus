# Created: 2026-09-04
# Last reused or audited: 2026-09-08
# Authority basis: docs/operations/current/plans/reversal_plan_tier0_2026-08-24.md
#   (market-anchored calibrator, item 9) + this task's fix — the exit stop was
#   comparing against the RAW posterior-predictive point (measured +0.170
#   over-biased on filled positions), while entries already act on the
#   market-anchored CORRECTED probability. src/state/portfolio.py
#   Position._exit_q_mean_and_source / Position.evaluate_exit;
#   src/calibration/market_anchored_live_fit.py register_active_provider /
#   get_active_provider / corrected_probability.
"""Proof that Position.evaluate_exit's stop acts on the market-anchored
corrected probability when a provider is registered, and fails open to the
raw point (identical to pre-fix behavior) whenever the correction cannot be
applied — never opening a DB connection of its own in the process.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from src.calibration.market_anchored_live_fit import (
    MarketAnchoredFitProvider,
    get_active_provider,
    register_active_provider,
)
from src.calibration.market_anchored_residual import LEAD_BUCKETS, ResidualCalibratorArtifact
from src.state.portfolio import ExitContext, Position


@pytest.fixture(autouse=True)
def _clear_active_provider():
    """The active provider is process-global state; never let one test's
    registration leak into another."""
    assert get_active_provider() is None, "a prior test left a provider registered"
    yield
    register_active_provider(None)


def _stub_artifact(*, alpha_day0: float = 0.09, beta: float = 0.0) -> ResidualCalibratorArtifact:
    return ResidualCalibratorArtifact(
        alpha={"day0": alpha_day0, "day1": 0.0, "day2": 0.0},
        beta=beta,
        lambda_=1.0,
        clip_d=3.0,
        p_clip=(0.005, 0.995),
        lead_buckets=LEAD_BUCKETS,
        training_cutoff="2026-09-04T00:00:00Z",
        n_train=100,
        n_excluded=0,
        excluded_reasons={},
        param_hash="stub",
        lead_calendar_revision="city_local_target_date_v1",
        city_timezone_snapshot=(("Warsaw", "Europe/Warsaw"),),
    )


class _StubProvider:
    """A pre-warmed provider that never touches a database — used to prove
    the exit path only needs `.artifact(now=...)`, never a connection."""

    def __init__(self, artifact: ResidualCalibratorArtifact | None) -> None:
        self._artifact = artifact

    def artifact(self, *, now: datetime) -> ResidualCalibratorArtifact | None:
        return self._artifact


def _held_position(direction: str = "buy_yes", *, target_date: str = "2026-09-04") -> Position:
    return Position(
        trade_id="pos-market-anchored-exit",
        market_id="mkt-market-anchored-exit",
        city="Warsaw",
        cluster="europe",
        target_date=target_date,
        bin_label="20-21C",
        direction=direction,
        entry_price=0.30,
        size_usd=20.0,
        shares=40.0,
        cost_basis_usd=20.0,
        p_posterior=0.50,
    )


def _exit_context(
    *,
    fresh_prob: float,
    current_market_price: float,
    best_bid: float,
    best_ask: float | None = None,
) -> ExitContext:
    return ExitContext(
        exit_reason="",
        fresh_prob=fresh_prob,
        fresh_prob_is_fresh=True,
        current_market_price=current_market_price,
        current_market_price_is_fresh=True,
        best_bid=best_bid,
        best_ask=best_ask if best_ask is not None else best_bid + 0.02,
        market_vig=1.0,
        hours_to_settlement=12.0,
        position_state="holding",
        day0_active=False,
        whale_toxicity=False,
        divergence_score=0.0,
        market_velocity_1h=0.0,
        current_ci=(0.05, 0.95),
        belief_available=True,
    )


# --- (a) buy_yes: market-anchored correction replaces the raw point ---------


def test_buy_yes_exit_uses_market_anchored_corrected_q():
    register_active_provider(_StubProvider(_stub_artifact(alpha_day0=0.09)))
    # decision_date == target_date (both "today" UTC) => lead_bucket day0.
    today = datetime.now(timezone.utc).date().isoformat()
    pos = _held_position(direction="buy_yes", target_date=today)
    ctx = _exit_context(fresh_prob=0.50, current_market_price=0.20, best_bid=0.15)

    q_mean, evidence_ok, source = pos._exit_q_mean_and_source(ctx)

    assert evidence_ok is True
    assert source == "market_anchored"
    expected = 1.0 / (1.0 + math.exp(-(math.log(0.2 / 0.8) + 0.09)))
    assert float(q_mean) == pytest.approx(expected, abs=1e-9)
    assert float(q_mean) == pytest.approx(0.2148, abs=1e-3)

    decision = pos.evaluate_exit(ctx)
    assert "exit_q:market_anchored" in decision.applied_validations
    assert "exit_q:raw" not in decision.applied_validations


# --- (b) buy_no: complement law applied --------------------------------------


def test_buy_no_exit_applies_the_complement_law():
    register_active_provider(_StubProvider(_stub_artifact(alpha_day0=0.09)))
    today = datetime.now(timezone.utc).date().isoformat()
    pos = _held_position(direction="buy_no", target_date=today)
    ctx = _exit_context(fresh_prob=0.50, current_market_price=0.20, best_bid=0.15)

    q_mean, evidence_ok, source = pos._exit_q_mean_and_source(ctx)

    assert evidence_ok is True
    assert source == "market_anchored"
    expected = 1.0 - 1.0 / (1.0 + math.exp(-(math.log(0.8 / 0.2) + 0.09)))
    assert float(q_mean) == pytest.approx(expected, abs=1e-9)
    assert float(q_mean) == pytest.approx(0.1860, abs=1e-3)

    decision = pos.evaluate_exit(ctx)
    assert "exit_q:market_anchored" in decision.applied_validations


# --- (c) provider unavailable -> fail open to the raw point ------------------


def test_no_provider_registered_falls_back_to_raw_q():
    assert get_active_provider() is None
    today = datetime.now(timezone.utc).date().isoformat()
    pos = _held_position(direction="buy_yes", target_date=today)
    ctx = _exit_context(fresh_prob=0.50, current_market_price=0.20, best_bid=0.15)

    q_mean, evidence_ok, source = pos._exit_q_mean_and_source(ctx)

    assert evidence_ok is True
    assert source == "raw"
    assert float(q_mean) == pytest.approx(0.50, abs=1e-9)

    decision = pos.evaluate_exit(ctx)
    assert "exit_q:raw" in decision.applied_validations
    assert "exit_q:market_anchored" not in decision.applied_validations


def test_provider_returning_none_artifact_falls_back_to_raw_q():
    register_active_provider(_StubProvider(None))
    today = datetime.now(timezone.utc).date().isoformat()
    pos = _held_position(direction="buy_yes", target_date=today)
    ctx = _exit_context(fresh_prob=0.50, current_market_price=0.20, best_bid=0.15)

    q_mean, evidence_ok, source = pos._exit_q_mean_and_source(ctx)

    assert evidence_ok is True
    assert source == "raw"
    assert float(q_mean) == pytest.approx(0.50, abs=1e-9)


# --- (d) evaluate_exit never opens its own DB connection ---------------------


def _seed_conn_for_real_provider() -> sqlite3.Connection:
    """A real, in-process sqlite connection with enough DISTINCT claims (one
    per row: unique city/bin) to clear MIN_TRAIN_ROWS=20 under the claim-count
    weighting from the sibling fix, fitted BEFORE sqlite3.connect is
    monkeypatched — proving the exit path only ever reuses this
    already-open connection (or the provider's TTL cache), never dials a
    fresh one itself.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE settlement_attribution (
            attribution_id TEXT, q_in_bin REAL, market_in_bin_prob REAL,
            settled_in_bin INTEGER, direction TEXT,
            decision_posterior_computed_at TEXT, target_date TEXT,
            settled_at TEXT, graded_at TEXT, city TEXT,
            temperature_metric TEXT, traded_bin_label TEXT
        )
        """
    )
    decision_day = date(2026, 8, 20)
    target_day = decision_day + timedelta(days=1)
    settled_at = datetime.combine(
        target_day, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=6)
    rows = [
        {
            "attribution_id": f"row-{i}",
            "q_in_bin": 0.6,
            "market_in_bin_prob": 0.35,
            "settled_in_bin": i % 2,
            "direction": "buy_yes",
            "decision_posterior_computed_at": datetime.combine(
                decision_day, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat(),
            "target_date": target_day.isoformat(),
            "settled_at": settled_at.isoformat(),
            "graded_at": None,
            "city": f"city-{i}",
            "temperature_metric": "high",
            "traded_bin_label": f"bin-{i}",
        }
        for i in range(30)
    ]
    conn.executemany(
        """
        INSERT INTO settlement_attribution (
            attribution_id, q_in_bin, market_in_bin_prob, settled_in_bin,
            direction, decision_posterior_computed_at, target_date,
            settled_at, graded_at, city, temperature_metric, traded_bin_label
        ) VALUES (
            :attribution_id, :q_in_bin, :market_in_bin_prob, :settled_in_bin,
            :direction, :decision_posterior_computed_at, :target_date,
            :settled_at, :graded_at, :city, :temperature_metric, :traded_bin_label
        )
        """,
        rows,
    )
    conn.commit()
    return conn


def test_evaluate_exit_never_opens_its_own_db_connection(monkeypatch):
    conn = _seed_conn_for_real_provider()
    provider = MarketAnchoredFitProvider(
        lambda: conn,
        min_train_rows=20,
        city_timezones={f"city-{i}": "UTC" for i in range(30)},
    )
    # Warm the TTL cache now (real wall-clock "now"), BEFORE sqlite3.connect
    # is sabotaged below — the exit path's own `now` a moment later is well
    # inside the 6h TTL, so it must never call `_connect()` again.
    warm_artifact = provider.artifact(now=datetime.now(timezone.utc))
    assert warm_artifact is not None
    register_active_provider(provider)

    def _explode(*_args, **_kwargs):
        raise AssertionError("evaluate_exit must never open its own DB connection")

    monkeypatch.setattr(sqlite3, "connect", _explode)

    pos = _held_position(direction="buy_yes", target_date="2026-08-21")
    ctx = _exit_context(fresh_prob=0.50, current_market_price=0.20, best_bid=0.15)

    # Must not raise: no new connection, cache still warm (within its 6h TTL
    # relative to `now=datetime.now(timezone.utc)` inside the exit path, or a
    # cache miss that fails open to "raw" rather than dialing sqlite).
    decision = pos.evaluate_exit(ctx)
    assert decision.trigger in {"HOLD", "SELL_REVERSAL", "EVIDENCE_UNAVAILABLE"}
    applied = set(decision.applied_validations)
    assert "exit_q:market_anchored" in applied or "exit_q:raw" in applied
