# Lifecycle: created=2026-09-05
# Purpose: Pin that the day0_hourly_vectors retention prune is index-served, never a table scan
#   under the forecasts LIVE flock.
# Reuse: Run after changing day0_hourly_vectors schema or persist_day0_hourly_vectors pruning.
"""The prune predicate in persist_day0_hourly_vectors filters on captured_at alone."""
from __future__ import annotations

import sqlite3

from src.data.day0_hourly_vectors import _ensure_schema


def test_prune_delete_uses_captured_at_index() -> None:
    conn = sqlite3.connect(":memory:")
    _ensure_schema(conn)
    plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM day0_hourly_vectors WHERE captured_at < ?",
            ("2026-09-01T00:00:00+00:00",),
        ).fetchall()
    )
    assert "idx_day0_hourly_vectors_captured_at" in plan, plan
    assert "SCAN day0_hourly_vectors" not in plan, plan
