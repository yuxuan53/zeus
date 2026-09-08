# Lifecycle: created=2026-06-12; last_reviewed=2026-09-08; last_reused=2026-09-08
# Purpose: current executable snapshot/Gamma schedule is the taker fee authority.
# Authority basis: exact charged fees require current market and fee-regime identity;
# raw fee_rate_bps and local cost-basis residuals do not provide that identity.
"""Current executable snapshot fee-schedule authority.

The reconciler artifact remains diagnostic evidence. It cannot authorize a
taker fee because it lacks the current market and fee-regime identity.
"""
from __future__ import annotations

import math


def resolve_taker_fee_fraction(schedule_fraction: float) -> tuple[float, str]:
    """Validate and return the current executable snapshot fee schedule."""

    if isinstance(schedule_fraction, bool):
        raise ValueError("invalid current snapshot fee schedule")
    try:
        schedule = float(schedule_fraction)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid current snapshot fee schedule") from exc
    if not math.isfinite(schedule) or not 0.0 <= schedule <= 1.0:
        raise ValueError("invalid current snapshot fee schedule")
    return schedule, "current_snapshot_fee_schedule"
