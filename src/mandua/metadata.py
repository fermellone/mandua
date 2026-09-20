"""Parse and construct bounded Git commit metadata."""

from __future__ import annotations

import re

from mandua.errors import ErrorCode, ManduaError
from mandua.models import QueryLimits

_TRAILER = re.compile(r"([A-Za-z0-9-]+):[ \t]*(.*)")
_RECORDED_REASON = re.compile(r"Reason:[ \t]*(\S(?:.*\S)?)$")
_STRICT_GENERATED_TRAILER = re.compile(r"([A-Za-z0-9-]+): (.*)")
_GENERATED_TRAILER_KEYS = frozenset(
    {"Memory-Type", "Scope", "Task-ID", "Decision-ID", "Agent-ID", "Corrects"}
)
_TRAILER_ORDER = (
    ("Memory-Type", "memory_type"),
    ("Scope", "scope"),
    ("Task-ID", "task_id"),
    ("Decision-ID", "decision_id"),
    ("Agent-ID", "agent_id"),
    ("Corrects", "corrects"),
)
_EXTRA_TRAILER_KEY = re.compile(r"[A-Za-z0-9-]+")
_MAX_EXTRA_TRAILERS = 32


def parse_trailers(message: str) -> tuple[tuple[str, str], ...]:
    """Return every trailer from the final complete non-empty paragraph only."""
    if not isinstance(message, str):
        return ()
    lines = message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ()
    paragraph_start = len(lines)
    while paragraph_start and lines[paragraph_start - 1].strip():
        paragraph_start -= 1
    trailers: list[tuple[str, str]] = []
    for line in lines[paragraph_start:]:
        match = _TRAILER.fullmatch(line)
        if match is None:
            return ()
        trailers.append((match.group(1), match.group(2)))
    return tuple(trailers)


def parse_recorded_reason(body: str, *, limits: QueryLimits | None = None) -> str | None:
    """Return only the generated standalone Reason paragraph before final trailers."""
    if not isinstance(body, str) or (limits is not None and not isinstance(limits, QueryLimits)):
        return None
    configured_limits = limits if limits is not None else QueryLimits()
    paragraphs = _nonempty_paragraphs(body)
    trailers = _parse_generated_trailers(paragraphs[-1]) if len(paragraphs) == 2 else ()
    if not _is_generated_trailer_block(trailers, configured_limits):
        return None
    match = _RECORDED_REASON.fullmatch(paragraphs[-2])
    if match is None:
        return None
    reason = match.group(1)
    if paragraphs[-2] != f"Reason: {reason}" or not _is_exact_valid_line(reason, configured_limits):
        return None
    return reason


def build_commit_message(
    subject: str,
    *,
    reason: str | None = None,
    memory_type: str | None = None,
    scope: str | None = None,
    task_id: str | None = None,
    decision_id: str | None = None,
    agent_id: str | None = None,
    corrects: str | None = None,
    extra_trailers: tuple[tuple[str, str], ...] = (),
    limits: QueryLimits | None = None,
) -> str:
    """Build one structurally valid message with a contiguous canonical trailer block."""
    configured_limits = limits or QueryLimits()
    subject = _validated_line(subject, configured_limits, "The commit subject is invalid.")
    sections = [subject]
    if reason is not None:
        sections.append(
            "Reason: "
            + _validated_line(reason, configured_limits, "The recorded reason is invalid.")
        )
    values = {
        "memory_type": memory_type,
        "scope": scope,
        "task_id": task_id,
        "decision_id": decision_id,
        "agent_id": agent_id,
        "corrects": corrects,
    }
    canonical_trailers = [
        f"{key}: {_validated_line(values[name], configured_limits, 'The trailer value is invalid.')}"
        for key, name in _TRAILER_ORDER
        if values[name] is not None
    ]
    trailers = canonical_trailers + _validated_extra_trailers(extra_trailers, configured_limits)
    if reason is not None and not canonical_trailers:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED,
            "A recorded reason requires a final trailer block.",
        )
    if trailers:
        sections.append("\n".join(trailers))
    message = "\n\n".join(sections)
    try:
        message_bytes = message.encode("utf-8")
    except UnicodeEncodeError:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "The generated commit message is invalid."
        ) from None
    if len(message_bytes) > configured_limits.max_output_bytes:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "The generated commit message exceeds the byte limit."
        )
    return message


def _validated_line(value: object, limits: QueryLimits, message: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value) > limits.max_input_chars
    ):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, message)
    return value.strip()


def _validated_extra_trailers(value: object, limits: QueryLimits) -> list[str]:
    if not isinstance(value, tuple) or len(value) > _MAX_EXTRA_TRAILERS:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "Extra trailers must be a bounded tuple of pairs."
        )
    reserved = {key.casefold() for key, _ in _TRAILER_ORDER} | {"reason"}
    seen: set[str] = set()
    trailers: list[str] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "Extra trailers must be a bounded tuple of pairs."
            )
        key, raw_value = item
        if (
            not isinstance(key, str)
            or _EXTRA_TRAILER_KEY.fullmatch(key) is None
            or len(key) > limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The extra trailer key is invalid.")
        try:
            key.encode("utf-8")
        except UnicodeEncodeError:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The extra trailer key is invalid."
            ) from None
        folded = key.casefold()
        if folded in reserved or folded in seen:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The extra trailer key conflicts with metadata."
            )
        trailers.append(
            f"{key}: {_validated_line(raw_value, limits, 'The extra trailer value is invalid.')}"
        )
        seen.add(folded)
    return trailers


def _is_generated_trailer_block(trailers: tuple[tuple[str, str], ...], limits: QueryLimits) -> bool:
    if not trailers or len(trailers) > len(_TRAILER_ORDER) + _MAX_EXTRA_TRAILERS:
        return False
    canonical_positions = {key: position for position, (key, _) in enumerate(_TRAILER_ORDER)}
    canonical_folded = {key.casefold() for key in canonical_positions}
    previous_position = -1
    canonical_seen = False
    extras_started = False
    extra_seen: set[str] = set()
    extra_count = 0
    for key, value in trailers:
        if not isinstance(key, str) or not isinstance(value, str):
            return False
        if key in canonical_positions:
            if extras_started or canonical_positions[key] <= previous_position:
                return False
            previous_position = canonical_positions[key]
            canonical_seen = True
            if not _is_exact_valid_line(value, limits):
                return False
            continue
        extras_started = True
        folded = key.casefold()
        extra_count += 1
        if (
            _EXTRA_TRAILER_KEY.fullmatch(key) is None
            or folded in canonical_folded
            or folded == "reason"
            or folded in extra_seen
            or extra_count > _MAX_EXTRA_TRAILERS
            or not _is_exact_valid_line(value, limits)
        ):
            return False
        extra_seen.add(folded)
    return canonical_seen


def _valid_line(value: str, limits: QueryLimits) -> bool:
    try:
        _validated_line(value, limits, "invalid")
    except ManduaError:
        return False
    return True


def _is_exact_valid_line(value: str, limits: QueryLimits) -> bool:
    try:
        return _validated_line(value, limits, "invalid") == value
    except ManduaError:
        return False


def _parse_generated_trailers(paragraph: str) -> tuple[tuple[str, str], ...]:
    trailers: list[tuple[str, str]] = []
    for line in paragraph.splitlines():
        match = _STRICT_GENERATED_TRAILER.fullmatch(line)
        if match is None:
            return ()
        trailers.append((match.group(1), match.group(2)))
    return tuple(trailers)


def _nonempty_paragraphs(value: str) -> tuple[str, ...]:
    paragraphs: list[str] = []
    lines: list[str] = []
    for line in value.splitlines():
        if line.strip():
            lines.append(line)
        elif lines:
            paragraphs.append("\n".join(lines))
            lines = []
    if lines:
        paragraphs.append("\n".join(lines))
    return tuple(paragraphs)
