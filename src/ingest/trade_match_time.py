# Created: 2026-09-08
# Last reused/audited: 2026-09-08
# Authority basis: Polymarket REST/WS native match-time contract; seconds are
#   venue epoch units and all native aliases must agree before persistence.
"""Pure normalization of Polymarket's source-native trade match time."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


_NATIVE_ALIASES = ("match_time", "matchtime", "matchTime")
_NUMERIC_RE = re.compile(
    r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?$"
)
_ISO_FRACTION_RE = re.compile(r"[.,](\d+)")
_FRACTIONAL_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}(?::?\d{2})?[.,]")
_MAX_TEXT_LENGTH = 128
_MIN_EPOCH_SECONDS = Decimal("-62135596800")
_MAX_EPOCH_SECONDS = Decimal("253402300800")


def _parse_epoch_seconds(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        text = str(value)
        if len(text) > _MAX_TEXT_LENGTH:
            return None
        decimal_value = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal_value.is_finite() or decimal_value < _MIN_EPOCH_SECONDS:
        return None
    if decimal_value >= _MAX_EPOCH_SECONDS:
        return None

    # Reject giant exponents before any Decimal operation can ask for an
    # unbounded intermediate. Zero is safe at every exponent.
    if not decimal_value.is_zero() and not -6 <= decimal_value.adjusted() <= 11:
        return None
    try:
        numerator, denominator = decimal_value.as_integer_ratio()
        microseconds, remainder = divmod(numerator * 1_000_000, denominator)
        if remainder:
            return None
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=microseconds,
        )
    except (OverflowError, TypeError, ValueError):
        return None


def _parse_native_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value.astimezone(timezone.utc)
        except (OverflowError, TypeError, ValueError):
            return None
    if isinstance(value, (int, float, Decimal)):
        return _parse_epoch_seconds(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_TEXT_LENGTH:
        return None
    if _NUMERIC_RE.fullmatch(text):
        return _parse_epoch_seconds(text)
    if any(len(fraction) > 6 for fraction in _ISO_FRACTION_RE.findall(text)):
        return None
    # datetime.fromisoformat can discard fractional offsets near UTC.
    if _FRACTIONAL_OFFSET_RE.search(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def trade_match_time(raw: Mapping[str, Any]) -> datetime | None:
    """Normalize agreeing native clocks to exact UTC, or return None.

    Epoch values are seconds. ISO text with fractional UTC offsets is unsupported.
    """

    # SCOPE: one raw trade's optional match clock. DRAIN: retain the raw fill.
    # RESET: a later valid native source revision supplies its own clock.
    if not isinstance(raw, Mapping):
        return None
    parsed_values: list[datetime] = []
    for alias in _NATIVE_ALIASES:
        value = raw.get(alias)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        parsed = _parse_native_value(value)
        if parsed is None:
            return None
        parsed_values.append(parsed)
    if not parsed_values or any(parsed != parsed_values[0] for parsed in parsed_values[1:]):
        return None
    return parsed_values[0]
