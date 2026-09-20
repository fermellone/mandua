"""Shared bounds for untrusted text entering Mandu'a's public contract."""

from __future__ import annotations

import math
from dataclasses import replace

from mandua.errors import ErrorCode, ManduaError
from mandua.models import HistoryScope, QueryLimits

_INTEGER_LIMIT_MAXIMUMS = (
    ("max_commits", 1_000_000),
    ("max_output_bytes", 64 * 1_048_576),
    ("max_excerpt_chars", 1_048_576),
    ("max_input_chars", 1_048_576),
)


def normalize_timeout_seconds(value: object, *, message: str) -> float:
    """Return one built-in finite positive timeout or a stable validation error."""
    if type(value) not in (int, float):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, message)
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, message) from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, message)
    return normalized


def validate_query_limits(value: object) -> QueryLimits:
    """Return one complete public limit model or raise a stable validation error."""
    if not isinstance(value, QueryLimits):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The query limits object is invalid.")
    for field, maximum in _INTEGER_LIMIT_MAXIMUMS:
        configured = getattr(value, field)
        if type(configured) is not int or configured < 1:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                f"The {field} query limit must be a positive integer.",
            )
        if configured > maximum:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                f"The {field} query limit exceeds the supported maximum.",
            )
    normalized_timeout = normalize_timeout_seconds(
        value.timeout_seconds,
        message="The timeout_seconds query limit must be finite and positive.",
    )
    if type(value.timeout_seconds) is float:
        return value
    return replace(value, timeout_seconds=normalized_timeout)


def bound_text(value: str, limit: int) -> str:
    """Shorten text with a marker that cannot be mistaken for a Git object or ref."""
    if len(value) <= limit:
        return value
    if limit <= 0:
        return ""
    marker = "." * min(3, limit)
    return f"{value[: limit - len(marker)]}{marker}"


def bound_history_scope(scope: HistoryScope, limit: int) -> HistoryScope:
    """Bound every displayed ref and disclose when any ref identity was shortened."""
    refs = tuple(bound_text(ref, limit) for ref in scope.refs)
    refs_truncated = refs != scope.refs
    return replace(scope, refs=refs, truncated=scope.truncated or refs_truncated)
