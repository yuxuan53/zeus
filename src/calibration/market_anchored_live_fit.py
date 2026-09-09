# Created: 2026-08-27
# Last reused or audited: 2026-09-08
# Authority basis: docs/operations/current/plans/reversal_plan_tier0_2026-08-24.md
#   item 9 ("Market-anchored walk-forward calibrator") — live wiring. Row
#   extraction mirrors scripts/calibrator_walkforward_report.py (load_rows /
#   build_walk_forward_rows); the calibrator math is imported, never restated.
"""In-process fit provider for the market-anchored residual calibrator.

Fits ONE artifact from settled history and caches it in module state behind a
TTL. There is deliberately no artifact FILE: a written artifact plus a separate
refitter is a known failure class here — the refitter stops, the file goes
stale, and the live path keeps acting on frozen parameters while every
freshness check it has still passes. An in-process cache cannot outlive the
process that fitted it, so staleness is bounded by the TTL by construction.

Walk-forward law: ``training_cutoff`` is the fit instant, and only rows whose
settlement is strictly before it and whose CURRENT attribution version was
graded at or before it are trained on. A late grade or a later regrade cannot
reach back into an artifact already fitted, so covered historical versions are
conservatively absent rather than reconstructed from supersession history.

Fail-open is the whole contract. Too few rows, an unreadable database, a lead
outside day0/day1/day2, a non-finite probability — every one of these returns
None, and the caller keeps the raw q it already had. This module never raises
into the decision path and never degrades an unfittable case into a guess.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import math
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.calibration.market_anchored_residual import (
    LAMBDA_GRID,
    MIN_TRAIN_ROWS,
    FitRow,
    ResidualCalibratorArtifact,
    LEAD_CALENDAR_REVISION,
    UNBOUND_LEAD_CALENDAR_REVISION,
    apply_artifact,
    fit,
    lead_bucket_of,
)

# One fit serves this long before a refit is attempted. Six hours matches the
# forecast cycle interval (00/06/12/18Z): settled rows arrive in bursts tied to
# market resolution, so refitting faster re-reads the same table to recompute
# the same parameters, and refitting slower lets a full cycle of settled
# evidence sit unused.
DEFAULT_TTL = timedelta(hours=6)

# Lambda for the live fit. The walk-forward report selects lambda on an early
# tuning fold; live has no such fold (it fits once over all settled history),
# so it takes the grid's most-regularized value. Under-regularizing a live
# acting probability manufactures edge; over-regularizing shrinks toward the
# market price, which is the plan's explicit safe direction.
LIVE_LAMBDA = max(LAMBDA_GRID)

_FIT_TABLE_BY_ALIAS = {
    "main": "settlement_attribution",
    "world": "world.settlement_attribution",
}
ArtifactCacheKey = tuple[object, ...]


def _canonical_db_identity(
    conn: sqlite3.Connection,
    *,
    schema_alias: str,
) -> tuple[str, int, int] | None:
    """Return the physical identity of one attached canonical database."""

    if schema_alias not in _FIT_TABLE_BY_ALIAS:
        return None
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        raw_path = next(
            str(row[2] or "")
            for row in rows
            if len(row) > 2 and str(row[1]) == schema_alias
        )
        if not raw_path:
            return None
        path = Path(raw_path).resolve(strict=False)
        stat = path.stat()
        return str(path), int(stat.st_dev), int(stat.st_ino)
    except (OSError, StopIteration, TypeError, ValueError, sqlite3.Error):
        return None


class MarketAnchoredArtifactCache:
    """Thread-safe cache of immutable fit artifacts, never database handles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[
            ArtifactCacheKey,
            tuple[ResidualCalibratorArtifact, datetime],
        ] = {}

    def get_or_fit(
        self,
        key: ArtifactCacheKey,
        *,
        now: datetime,
        ttl: timedelta,
        fit_current: Callable[[], ResidualCalibratorArtifact | None],
        deadline_monotonic: float | None = None,
    ) -> tuple[ResidualCalibratorArtifact | None, datetime | None]:
        """Serve a live artifact or fit once using only the current connection."""

        if deadline_monotonic is None:
            acquired = self._lock.acquire()
        else:
            remaining = float(deadline_monotonic) - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                return None, now
            acquired = self._lock.acquire(timeout=remaining)
        if not acquired:
            return None, now
        try:
            if (
                deadline_monotonic is not None
                and time.monotonic() >= float(deadline_monotonic)
            ):
                return None, now
            cached = self._entries.get(key)
            if cached is not None:
                artifact, fitted_at = cached
                age = now - fitted_at
                if timedelta(0) <= age < ttl:
                    if (
                        deadline_monotonic is not None
                        and time.monotonic() >= float(deadline_monotonic)
                    ):
                        return None, now
                    return artifact, fitted_at
            artifact = fit_current()
            if (
                artifact is not None
                and (
                    deadline_monotonic is None
                    or time.monotonic() < float(deadline_monotonic)
                )
            ):
                if cached is None or now >= cached[1]:
                    self._entries[key] = (artifact, now)
                    return artifact, now
                # This was a causal backfill earlier than a newer shared
                # artifact.  Serve the backfill to this caller without
                # letting its provider-local cache hide the newer artifact.
                return artifact, None
            # A failed current connection must not poison a different provider's
            # cache entry. The caller may still locally cache None for its own TTL.
            return None, now
        finally:
            self._lock.release()


_SHARED_ARTIFACT_CACHE = MarketAnchoredArtifactCache()


def get_shared_artifact_cache() -> MarketAnchoredArtifactCache:
    """Return the process-local artifact cache; no connection is retained."""

    return _SHARED_ARTIFACT_CACHE


@contextmanager
def _sqlite_fit_deadline(
    conn: sqlite3.Connection,
    deadline_monotonic: float | None,
):
    """Bound one borrowed SQLite read without replacing outer progress hooks."""

    if deadline_monotonic is None:
        yield conn
        return
    remaining = float(deadline_monotonic) - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("market anchored SQLite fit deadline expired")

    previous_busy_timeout: int | None = None
    timer: threading.Timer | None = None
    try:
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            previous_busy_timeout = int(row[0]) if row else None
            if previous_busy_timeout is not None:
                remaining_ms = max(
                    1,
                    int(max(0.0, deadline_monotonic - time.monotonic()) * 1000.0),
                )
                conn.execute(
                    f"PRAGMA busy_timeout = {min(previous_busy_timeout, remaining_ms)}"
                )
        except Exception:  # noqa: BLE001 - the timer still bounds supported handles
            previous_busy_timeout = None

        remaining = float(deadline_monotonic) - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("market anchored SQLite fit deadline expired")
        timer = threading.Timer(
            remaining,
            lambda: _interrupt_connection(conn),
        )
        timer.daemon = True
        timer.start()
        yield conn
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if previous_busy_timeout is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout = {previous_busy_timeout}")
            except Exception:  # noqa: BLE001 - borrowed connection may be closing
                pass


def _interrupt_connection(conn: sqlite3.Connection) -> None:
    try:
        conn.interrupt()
    except Exception:  # noqa: BLE001 - interruption is best effort
        pass


def _validated_city_timezone_snapshot(
    city_timezones: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...] | None:
    """Copy exact city names and validated ZoneInfo keys immutably."""

    if city_timezones is None:
        return ()
    if not isinstance(city_timezones, Mapping):
        return None
    if not city_timezones:
        return None
    snapshot: list[tuple[str, str]] = []
    for city, zone_name in city_timezones.items():
        if not isinstance(city, str) or not city or not isinstance(zone_name, str):
            continue
        try:
            zone = ZoneInfo(zone_name)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            continue
        snapshot.append((city, zone.key))
    return tuple(sorted(snapshot)) if snapshot else None


def _city_local_target_date(
    instant: datetime,
    city: str,
    city_timezone_snapshot: tuple[tuple[str, str], ...],
) -> date | None:
    """Derive a target-date lead anchor from one aware instant and city."""

    if (
        not isinstance(instant, datetime)
        or instant.tzinfo is None
        or instant.utcoffset() is None
        or not isinstance(city, str)
        or not city
    ):
        return None
    zones = dict(city_timezone_snapshot)
    zone_name = zones.get(city)
    if zone_name is None:
        return None
    try:
        return instant.astimezone(ZoneInfo(zone_name)).date()
    except (TypeError, ValueError, ZoneInfoNotFoundError, OverflowError):
        return None


def _snapshot_is_valid(snapshot: object) -> bool:
    if not isinstance(snapshot, tuple) or not snapshot:
        return False
    cities: set[str] = set()
    for item in snapshot:
        if not isinstance(item, tuple) or len(item) != 2:
            return False
        city, zone_name = item
        if not isinstance(city, str) or not city or city in cities:
            return False
        if not isinstance(zone_name, str) or not zone_name:
            return False
        try:
            ZoneInfo(zone_name)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return False
        cities.add(city)
    return tuple(sorted(snapshot)) == snapshot


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO8601 timestamp to tz-aware UTC, or None (never raises)."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def load_fit_rows(
    conn: sqlite3.Connection, *, training_cutoff: datetime,
        city_timezone_snapshot: tuple[tuple[str, str], ...] | None = None,
        schema_alias: str = "main",
) -> list[FitRow]:
    """Extract settled training rows whose outcome preceded ``training_cutoff``.

    Predicates mirror ``load_rows`` in scripts/calibrator_walkforward_report.py
    (q_in_bin / market_in_bin_prob / settled_in_bin / direction all NOT NULL).
    Both the strict ``settled_at < training_cutoff`` condition and the
    ``graded_at <= training_cutoff`` condition are applied after parsing. A
    missing ``settled_at`` may use a valid grade time as its conservative
    fallback; an invalid or missing grade can never make a row eligible.

    A claim is (city, target_date, temperature_metric, traded_bin_label,
    direction). The live table carries roughly one row per claim, but the
    certificate surface underneath it is re-certified many times over (a
    claim re-certifying does not produce a new independent outcome), so an
    unweighted fit over-counts whichever claims happen to re-certify most.
    Each returned row is weighted 1/(claim count within this same
    cutoff-filtered set) so a claim contributes exactly one row's worth of
    evidence to the fit regardless of how many certified rows it produced.
    """

    table = _FIT_TABLE_BY_ALIAS.get(schema_alias)
    if table is None:
        raise ValueError(f"unsupported calibration schema alias: {schema_alias!r}")
    rows = conn.execute(
        f"""
        SELECT q_in_bin, market_in_bin_prob, settled_in_bin,
               decision_posterior_computed_at, target_date, settled_at, graded_at,
               city, temperature_metric, traded_bin_label, direction
        FROM {table}
        WHERE q_in_bin IS NOT NULL
          AND market_in_bin_prob IS NOT NULL
          AND settled_in_bin IS NOT NULL
          AND direction IS NOT NULL
        """
    ).fetchall()

    valid: list[tuple[dict, str, tuple, int]] = []
    for row in rows:
        record = dict(row) if not isinstance(row, dict) else row
        graded_at = _parse_ts(record.get("graded_at"))
        if graded_at is None or graded_at > training_cutoff:
            continue
        if record.get("settled_at") is None:
            settled_at = graded_at
        else:
            settled_at = _parse_ts(record.get("settled_at"))
        if settled_at is None or settled_at >= training_cutoff:
            continue
        decision_at = _parse_ts(record.get("decision_posterior_computed_at"))
        target_date = _parse_date(record.get("target_date"))
        if decision_at is None or target_date is None:
            continue
        decision_date = (
            _city_local_target_date(
                decision_at,
                record.get("city"),
                city_timezone_snapshot,
            )
            if city_timezone_snapshot is not None
            else None
        )
        if decision_date is None:
            continue
        lead_bucket = lead_bucket_of(decision_date, target_date)
        if lead_bucket is None:
            continue
        try:
            outcome = int(record["settled_in_bin"])
        except (KeyError, TypeError, ValueError):
            continue
        claim_key = (
            record.get("city"),
            target_date.isoformat(),
            record.get("temperature_metric"),
            record.get("traded_bin_label"),
            record.get("direction"),
        )
        valid.append((record, lead_bucket, claim_key, outcome))

    claim_counts: dict[tuple, int] = {}
    for _record, _lead_bucket, claim_key, _outcome in valid:
        claim_counts[claim_key] = claim_counts.get(claim_key, 0) + 1

    fit_rows: list[FitRow] = []
    for record, lead_bucket, claim_key, outcome in valid:
        fit_rows.append(
            FitRow(
                p0=record.get("market_in_bin_prob"),
                q_raw=record.get("q_in_bin"),
                lead_bucket=lead_bucket,
                y=outcome,
                w=1.0 / claim_counts[claim_key],
            )
        )
    return fit_rows


class MarketAnchoredFitProvider:
    """TTL-cached artifact source for one borrowed DB connection factory.

    The provider never stores or closes a connection.  Only successful,
    immutable artifacts enter the shared cache; a failed current connection
    cannot poison a later provider that has a live connection.
    """

    def __init__(
        self,
        connect,
        *,
        ttl: timedelta = DEFAULT_TTL,
        min_train_rows: int = MIN_TRAIN_ROWS,
        lambda_: float = LIVE_LAMBDA,
        city_timezones: Mapping[str, str] | None,
        schema_alias: str = "main",
        cache: MarketAnchoredArtifactCache | None = None,
        db_identity: tuple[object, ...] | None = None,
        cache_only: bool = False,
    ) -> None:
        if schema_alias not in _FIT_TABLE_BY_ALIAS:
            raise ValueError(f"unsupported calibration schema alias: {schema_alias!r}")
        self._connect = connect
        self._schema_alias = schema_alias
        self._ttl = ttl
        self._min_train_rows = min_train_rows
        self._lambda = lambda_
        self._city_timezone_snapshot = _validated_city_timezone_snapshot(city_timezones)
        self._lead_calendar_revision = (
            LEAD_CALENDAR_REVISION if city_timezones is not None else UNBOUND_LEAD_CALENDAR_REVISION
        )
        self._cache = cache if cache is not None else get_shared_artifact_cache()
        self._db_identity = db_identity
        self._cache_only = bool(cache_only)
        self._lock = threading.Lock()
        self._artifact: ResidualCalibratorArtifact | None = None
        self._fitted_at: datetime | None = None

    def _cache_key(self, db_identity: tuple[str, int, int]) -> ArtifactCacheKey:
        return (
            db_identity,
            self._city_timezone_snapshot,
            self._lead_calendar_revision,
            float(self._lambda),
            int(self._min_train_rows),
            self._ttl.total_seconds(),
        )

    def artifact(
        self,
        *,
        now: datetime,
        deadline_monotonic: float | None = None,
    ) -> ResidualCalibratorArtifact | None:
        """The current artifact, refitting when the cached one has aged out."""

        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return None
        now_utc = now.astimezone(timezone.utc)
        if deadline_monotonic is not None:
            if not math.isfinite(float(deadline_monotonic)) or time.monotonic() >= float(deadline_monotonic):
                return None
        if deadline_monotonic is None:
            acquired = self._lock.acquire()
        else:
            remaining = float(deadline_monotonic) - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                return None
            acquired = self._lock.acquire(timeout=remaining)
        if not acquired:
            return None
        try:
            if (
                deadline_monotonic is not None
                and time.monotonic() >= float(deadline_monotonic)
            ):
                return None
            if self._fitted_at is not None:
                age = now_utc - self._fitted_at
                if age < timedelta(0):
                    # A backward request is independently fit at its causal
                    # cutoff, without downgrading the newest cached result.
                    return (
                        None
                        if self._cache_only
                        else self._fit(
                            now_utc,
                            deadline_monotonic=deadline_monotonic,
                        )
                    )
                if age < self._ttl:
                    if (
                        deadline_monotonic is not None
                        and time.monotonic() >= float(deadline_monotonic)
                    ):
                        return None
                    return self._artifact
            if self._cache_only:
                return None
            artifact, fitted_at = self._fit_cached(
                now_utc,
                deadline_monotonic=deadline_monotonic,
            )
            if fitted_at is None:
                return artifact
            # A deadline miss is transient work-budget exhaustion.  Preserve
            # the previous local state so a later caller may retry; in
            # particular, a monitor warm-up must not turn a late fit into a
            # six-hour cached ``None``.
            if (
                artifact is None
                and deadline_monotonic is not None
                and time.monotonic() >= float(deadline_monotonic)
            ):
                return None
            self._artifact = artifact
            self._fitted_at = fitted_at
            return self._artifact
        finally:
            self._lock.release()

    def warm(
        self,
        *,
        now: datetime,
        deadline_monotonic: float | None,
    ) -> ResidualCalibratorArtifact | None:
        """Fit/refresh once before entering a monitor's position loop."""

        previous_cache_only = self._cache_only
        self._cache_only = False
        try:
            return self.artifact(
                now=now,
                deadline_monotonic=deadline_monotonic,
            )
        finally:
            self._cache_only = previous_cache_only

    def _fit_connection(
        self,
        conn: sqlite3.Connection,
        training_cutoff: datetime,
        *,
        deadline_monotonic: float | None = None,
    ) -> ResidualCalibratorArtifact | None:
        try:
            if self._city_timezone_snapshot is None:
                return None
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return None
            with _sqlite_fit_deadline(conn, deadline_monotonic):
                rows = load_fit_rows(
                    conn,
                    training_cutoff=training_cutoff,
                    city_timezone_snapshot=(
                        self._city_timezone_snapshot
                        if self._lead_calendar_revision == LEAD_CALENDAR_REVISION
                        else None
                    ),
                    schema_alias=self._schema_alias,
                )
        except Exception:  # noqa: BLE001 - an unavailable fit must never block serving
            return None
        # Each row is weighted 1/(claim count), so the training-row floor is
        # measured in claim-equivalent weight (sum(w)), not raw row count —
        # a claim re-certified many times must not look like many claims.
        if sum(row.w for row in rows) < self._min_train_rows:
            return None
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            return None
        cutoff_iso = training_cutoff.isoformat().replace("+00:00", "Z")
        try:
            artifact = fit(
                rows,
                lambda_=self._lambda,
                training_cutoff=cutoff_iso,
                lead_calendar_revision=self._lead_calendar_revision,
                city_timezone_snapshot=self._city_timezone_snapshot,
            )
            if (
                deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                return None
            return artifact
        except Exception:  # noqa: BLE001 - a failed fit degrades to raw q, never raises
            return None

    def _fit(
        self,
        training_cutoff: datetime,
        *,
        deadline_monotonic: float | None = None,
    ) -> ResidualCalibratorArtifact | None:
        try:
            conn = self._connect()
            if conn is None:
                return None
        except Exception:  # noqa: BLE001 - an unavailable fit must never block serving
            return None
        return self._fit_connection(
            conn,
            training_cutoff,
            deadline_monotonic=deadline_monotonic,
        )

    def _fit_cached(
        self,
        training_cutoff: datetime,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[ResidualCalibratorArtifact | None, datetime | None]:
        try:
            conn = self._connect()
            if conn is None:
                return None, training_cutoff
            identity = self._db_identity or _canonical_db_identity(
                conn,
                schema_alias=self._schema_alias,
            )
        except Exception:  # noqa: BLE001 - an unavailable fit must never block serving
            return None, training_cutoff
        if identity is None:
            return (
                self._fit_connection(
                    conn,
                    training_cutoff,
                    deadline_monotonic=deadline_monotonic,
                ),
                training_cutoff,
            )
        return self._cache.get_or_fit(
            self._cache_key(identity),
            now=training_cutoff,
            ttl=self._ttl,
            fit_current=lambda: self._fit_connection(
                conn,
                training_cutoff,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )


# The active provider is monitor-scope state only.  Entry selection uses its
# batch-local provider and never registers it here.
_active_provider: ContextVar[MarketAnchoredFitProvider | None] = ContextVar(
    "market_anchored_active_provider",
    default=None,
)


def register_active_provider(
    provider: MarketAnchoredFitProvider | None,
) -> Token:
    """Set the current monitor-scope provider and return its reset token."""

    return _active_provider.set(provider)


def reset_active_provider(token: Token) -> None:
    """Restore the provider scope represented by ``token``."""

    _active_provider.reset(token)


@contextmanager
def active_provider_scope(provider: MarketAnchoredFitProvider | None):
    token = register_active_provider(provider)
    try:
        yield provider
    finally:
        reset_active_provider(token)


def get_active_provider() -> MarketAnchoredFitProvider | None:
    """The registered active provider, or None when unset."""

    return _active_provider.get()


# side vocabulary accepted by corrected_probability. Both the candidate.side
# ("YES"/"NO") and direction ("buy_yes"/"buy_no") spellings are in live use
# across the codebase, so both are recognized; anything else raises rather
# than silently defaulting to buy_yes.
_YES_SIDE_VALUES = frozenset({"YES", "buy_yes"})
_NO_SIDE_VALUES = frozenset({"NO", "buy_no"})


def _is_no_side(side: str) -> bool:
    if side in _NO_SIDE_VALUES:
        return True
    if side in _YES_SIDE_VALUES:
        return False
    raise ValueError(f"corrected_probability: unrecognized side {side!r}")


def corrected_probability(
    artifact: ResidualCalibratorArtifact | None,
    *,
    p0: float,
    q_raw: float,
    target_date: date,
    side: str,
    city: str | None = None,
    decision_at: datetime | None = None,
) -> tuple[float, str, float] | None:
    """Apply ``artifact`` to one candidate, or None when it cannot be applied.

    The artifact is fit in in-bin (YES-event) space: p0 and q_raw there are
    ``market_in_bin_prob``/``q_in_bin``, i.e. probabilities of the YES event.
    ``p0``/``q_raw`` passed in here are in HELD-TOKEN space instead (the price
    and payoff probability of whichever token the candidate holds). For a
    buy_no candidate, held-token space is the in-bin space's complement
    (q_NO = 1 - q_in, p_NO = 1 - p_in), so this complements both inputs into
    in-bin space, applies the unchanged artifact, and complements the result
    back: ``1 - apply_artifact(artifact, 1 - p0, 1 - q_raw, lead_bucket)``.
    buy_yes needs no transform since held-token space already is in-bin space.

    Returns ``(corrected_q, lead_bucket, alpha_lead)`` where ``alpha_lead`` is
    the EFFECTIVE signed intercept applied in held-token space (the fitted
    alpha for buy_yes, its negation for buy_no), so certificates record what
    was actually applied. None means every fail-open case at once — no
    artifact, an unmodeled lead, a non-finite input — because each has the
    identical consequence for the caller: keep the raw q.
    """

    if artifact is None:
        return None
    artifact_revision = getattr(artifact, "lead_calendar_revision", UNBOUND_LEAD_CALENDAR_REVISION)
    if artifact_revision == LEAD_CALENDAR_REVISION:
        if (
            not isinstance(decision_at, datetime)
            or decision_at.tzinfo is None
            or decision_at.utcoffset() is None
            or not city
        ):
            return None
        snapshot = getattr(artifact, "city_timezone_snapshot", None)
        if not _snapshot_is_valid(snapshot):
            return None
        decision_date = _city_local_target_date(
            decision_at, city, snapshot
        )
        if decision_date is None:
            return None
        training_cutoff = _parse_ts(getattr(artifact, "training_cutoff", None))
        decision_at_utc = decision_at.astimezone(timezone.utc)
        if training_cutoff is None or training_cutoff > decision_at_utc:
            return None
    else:
        return None
    lead_bucket = lead_bucket_of(decision_date, target_date)
    if lead_bucket is None:
        return None
    is_no = _is_no_side(side)
    if is_no:
        corrected = apply_artifact(artifact, 1.0 - p0, 1.0 - q_raw, lead_bucket)
        if corrected is None:
            return None
        corrected = 1.0 - corrected
    else:
        corrected = apply_artifact(artifact, p0, q_raw, lead_bucket)
        if corrected is None:
            return None
    alpha_lead = float(artifact.alpha[lead_bucket])
    if is_no:
        alpha_lead = -alpha_lead
    return corrected, lead_bucket, alpha_lead
