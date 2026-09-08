# Lifecycle: created=2026-06-12; last_reviewed=2026-09-08; last_reused=2026-09-08
# Purpose: antibody that current executable snapshot/Gamma fee schedule is the
#   only taker fee authority; historical fee artifacts remain diagnostic.
"""Tests for src/contracts/fee_authority.py + the reconciler artifact contract."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import src.contracts.fee_authority as fa


@pytest.fixture()
def artifact_path(tmp_path, monkeypatch):
    p = tmp_path / "fee_reconciliation.json"
    monkeypatch.setattr(fa, "ARTIFACT_PATH", p, raising=False)
    monkeypatch.setattr(fa, "_cache", {"mtime": None, "artifact": None}, raising=False)
    return p


def _write(p: Path, **kw):
    base = {
        "schema": "fee_reconciliation",
        "fitted_at": "2026-06-12T23:00:00+00:00",
        "n_fills": 42,
        "observed_max_fee_fraction": 0.0,
    }
    base.update(kw)
    p.write_text(json.dumps(base))


def test_fresh_zero_artifact_does_not_override_current_schedule(artifact_path):
    _write(artifact_path, n_fills=41221)
    fraction, source = fa.resolve_taker_fee_fraction(0.05)
    assert fraction == 0.05
    assert source == "current_snapshot_fee_schedule"


def test_no_artifact_falls_back_to_schedule(artifact_path):
    fraction, source = fa.resolve_taker_fee_fraction(0.10)
    assert fraction == 0.10
    assert source == "current_snapshot_fee_schedule"


def test_thin_evidence_falls_back_to_schedule(artifact_path):
    _write(artifact_path, n_fills=3)
    fraction, source = fa.resolve_taker_fee_fraction(0.10)
    assert fraction == 0.10
    assert source == "current_snapshot_fee_schedule"


def test_positive_or_large_artifact_cannot_lower_current_schedule(artifact_path):
    _write(artifact_path, observed_max_fee_fraction=0.02)
    fraction, source = fa.resolve_taker_fee_fraction(0.10)
    assert fraction == 0.10
    assert source == "current_snapshot_fee_schedule"
    _write(artifact_path, observed_max_fee_fraction=0.50)
    fraction2, _ = fa.resolve_taker_fee_fraction(0.10)
    assert fraction2 == 0.10


def test_stale_artifact_is_ignored(artifact_path):
    _write(artifact_path)
    old = time.time() - 35 * 86400
    os.utime(artifact_path, (old, old))
    fraction, source = fa.resolve_taker_fee_fraction(0.10)
    assert fraction == 0.10
    assert source == "current_snapshot_fee_schedule"


@pytest.mark.parametrize("schedule", [0.0, 0.02, 1.0])
def test_valid_current_snapshot_schedule_is_returned_unchanged(schedule):
    fraction, source = fa.resolve_taker_fee_fraction(schedule)
    assert fraction == float(schedule)
    assert source == "current_snapshot_fee_schedule"


@pytest.mark.parametrize(
    "schedule",
    [float("nan"), float("inf"), float("-inf"), -0.01, 1.01, "invalid", True, False],
    ids=["nan", "positive-inf", "negative-inf", "negative", "above-one", "parse", "true", "false"],
)
def test_invalid_current_snapshot_schedule_fails_closed(schedule):
    with pytest.raises(ValueError, match="invalid current snapshot fee schedule"):
        fa.resolve_taker_fee_fraction(schedule)


def test_malformed_artifact_is_ignored(artifact_path):
    artifact_path.write_text("not-json")
    fraction, source = fa.resolve_taker_fee_fraction(0.10)
    assert fraction == 0.10
    assert source == "current_snapshot_fee_schedule"


def test_refit_writes_diagnostic_artifact_without_fee_authority(tmp_path, monkeypatch):
    """The extracted refit() core (imported by the daemon scheduler) must produce the
    same artifact shape the CLI writes, from a plain fixture DB -- no live zeus_trades.db
    dependency."""
    import sqlite3

    import scripts.reconcile_realized_fees as recon

    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE venue_order_facts (venue_order_id TEXT, state TEXT, "
        "matched_size TEXT, observed_at TEXT, raw_payload_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE position_current (city TEXT, target_date TEXT, "
        "entry_price REAL, shares REAL, cost_basis_usd REAL)"
    )
    payload = json.dumps({"trade_fact_proof": {"trade": {"fee_rate_bps": "0"}}})
    for i in range(12):
        conn.execute(
            "INSERT INTO venue_order_facts VALUES (?,?,?,?,?)",
            (f"o{i}", "MATCHED", "5.0", "2026-08-01T00:00:00Z", payload),
        )
    conn.commit()
    conn.close()

    out_path = tmp_path / "fee_reconciliation.json"
    artifact = recon.refit(out_path=str(out_path), db_path=str(db_path))

    assert artifact["n_fills"] == 12
    assert artifact["observed_max_fee_fraction"] == 0.0
    written = json.loads(out_path.read_text())
    assert written == artifact
    monkeypatch.setattr(fa, "ARTIFACT_PATH", out_path, raising=False)
    assert fa.resolve_taker_fee_fraction(0.05) == (0.05, "current_snapshot_fee_schedule")
    # No leftover tmp file from the atomic write.
    assert not (tmp_path / "fee_reconciliation.json.tmp").exists()


def test_reconciler_excludes_schedule_envelope_fields():
    """The reconciler must read TRADE-level fee fields only — fee_details.* is the
    venue schedule CAP, the exact confusion this artifact exists to kill."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "recon", Path(__file__).resolve().parent.parent / "scripts" / "reconcile_realized_fees.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    payload = {
        "trade_fact_proof": {"trade": {"fee_rate_bps": "0"}},
        "submit_result": {"_venue_submission_envelope": {"fee_details": {"fee_rate_bps": 1000.0}}},
    }
    fields = mod._scan_fee_fields(payload)
    realized = {k: v for k, v in fields.items() if "fee_details" not in k}
    assert list(realized.values()) == ["0"]
