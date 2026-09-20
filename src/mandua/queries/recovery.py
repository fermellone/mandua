"""Recover bounded local Git history without changing it by default."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field, replace

from mandua.bounds import bound_text
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import (
    GitOperationBudget,
    GitRepositoryAuthority,
    _RepositoryAuthorityLayout,
)
from mandua.models import Claim, Evidence, MemoryResult, PlannedChange, QueryLimits
from mandua.repository import RepositoryInspector

_OBJECT_ID = rb"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"
_UNREACHABLE_COMMIT = re.compile(rb"^unreachable commit (" + _OBJECT_ID + rb")$")
_MISSING_FSCK_OBJECT = re.compile(rb"^missing (?:blob|tree|commit|tag) (" + _OBJECT_ID + rb")$")
_ABBREVIATION = re.compile(r"^[0-9a-fA-F]{4,64}$")
_EMERGENCY_REF_TIMEOUT_SECONDS = 1.0
_RECONCILED_REF_WARNING = "The recovery branch was reconciled after an interrupted Git result."


@dataclass(slots=True)
class _Candidate:
    oid: str
    subject: str = ""
    refs: list[bytes] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Discovery:
    candidates: tuple[_Candidate, ...]
    truncated: bool
    missing_objects: tuple[str, ...]
    incomplete: bool = False


def recover_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    *,
    query: str | None,
    create_branch: str | None,
    apply: bool = False,
) -> MemoryResult:
    """Select exactly one bounded local candidate and optionally create a new branch for it."""
    _validate_request(query, create_branch, apply, limits)
    deadline = _deadline(limits.timeout_seconds)
    inspector.validate(timeout_seconds=_remaining_timeout(deadline))
    discovery = _discover(inspector, limits, deadline)
    selected = _select(inspector, discovery, query, deadline) if query is not None else None
    branch_ref = None
    mutation_warning = None
    if create_branch is not None:
        branch_ref = inspector.recovery_branch_ref(
            create_branch, timeout_seconds=_remaining_timeout(deadline)
        )
        if inspector.recovery_ref_exists(branch_ref, timeout_seconds=_remaining_timeout(deadline)):
            raise ManduaError(ErrorCode.CONFLICT, "The recovery branch already exists.")

    if apply:
        # Candidate and object/ref preconditions are recomputed immediately before mutation.
        discovery = _discover(inspector, limits, deadline)
        selected = _select(inspector, discovery, query, deadline)
        branch_ref = inspector.recovery_branch_ref(
            create_branch,
            timeout_seconds=_remaining_timeout(deadline),  # type: ignore[arg-type]
        )
        if inspector.recovery_ref_exists(branch_ref, timeout_seconds=_remaining_timeout(deadline)):
            raise ManduaError(ErrorCode.CONFLICT, "The recovery branch already exists.")
        scope = _recovery_scope(inspector, discovery, selected, deadline)
        authority = inspector.open_recovery_authority()
        layout = authority.layout
        inspector._replace_recovery_authority(authority)
        try:
            try:
                creation = inspector.create_recovery_ref(
                    branch_ref,
                    selected.oid,
                    timeout_seconds=_remaining_timeout(deadline),
                )
            except BaseException as failure:  # noqa: BLE001 - mutation may precede any exception
                creation, mutation_warning = _reconcile_recovery_ref_exception(
                    inspector,
                    limits,
                    branch_ref,
                    selected.oid,
                    failure,
                    layout,
                )
        except BaseException as operation_error:
            inspector._replace_recovery_authority(None)
            _close_recovery_authority(authority, original=operation_error)
            raise
        inspector._replace_recovery_authority(None)
        try:
            authority.cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup follows mutation
            creation, _ = _reconcile_recovery_ref_exception(
                inspector,
                limits,
                branch_ref,
                selected.oid,
                cleanup_error,
                layout,
            )
            mutation_warning = (
                "The recovery branch was created, but private Git authority cleanup was "
                f"uncertain; inspect {branch_ref} before another recovery attempt."
            )
        if creation == "conflict":
            # A failed zero-old update must not be retried: a concurrent creator wins.
            raise ManduaError(ErrorCode.CONFLICT, "The recovery branch changed concurrently.")
    else:
        scope = _recovery_scope(inspector, discovery, selected, deadline)
    return _result(
        discovery,
        scope,
        selected,
        branch_ref,
        apply,
        limits,
        mutation_warning=mutation_warning,
    )


def _reconcile_recovery_ref_exception(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    branch_ref: str,
    requested_oid: str,
    original: BaseException,
    repository_layout: _RepositoryAuthorityLayout,
) -> tuple[str, str | None]:
    interruption: BaseException | None = None
    for _ in range(2):
        try:
            actual_oid = _read_recovery_ref_emergency(
                inspector,
                limits,
                branch_ref,
                requested_oid,
                repository_layout,
            )
        except BaseException as error:  # noqa: BLE001 - control-flow identity is classified
            if error is original:
                if interruption is not None:
                    raise _uncertain_recovery_ref(branch_ref) from original
                interruption = error
                continue
            if isinstance(error, ManduaError):
                uncertainty = _uncertain_recovery_ref(branch_ref)
                if interruption is not None:
                    raise uncertainty from interruption
                raise uncertainty from original
            if interruption is not None:
                raise _uncertain_recovery_ref(branch_ref) from interruption
            interruption = error
            continue
        if actual_oid not in {None, requested_oid}:
            cause = interruption if interruption is not None else original
            raise _uncertain_recovery_ref(branch_ref) from cause
        if interruption is not None:
            raise interruption
        if actual_oid is None or not isinstance(original, ManduaError):
            raise original
        return "created", _RECONCILED_REF_WARNING
    raise _uncertain_recovery_ref(branch_ref) from interruption


def _read_recovery_ref_emergency(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    branch_ref: str,
    requested_oid: str,
    repository_layout: _RepositoryAuthorityLayout,
) -> str | None:
    timeout = min(float(limits.timeout_seconds), _EMERGENCY_REF_TIMEOUT_SECONDS)
    emergency_limits = replace(limits, timeout_seconds=timeout)
    private_limits = inspector.plan_recovery_ref_emergency_budget(
        repository_layout,
        branch_ref,
        object_id_length=len(requested_oid),
    )
    budget = GitOperationBudget(emergency_limits, budget_limits=private_limits)
    inspector._replace_operation_budget(budget, private_limits)
    authority = inspector.open_recovery_authority(expected_layout=repository_layout)
    try:
        result = inspector.recovery_ref_oid(
            branch_ref,
            object_id_length=len(requested_oid),
            timeout_seconds=budget.remaining_timeout(),
            repository_authority=authority,
        )
    except BaseException as operation_error:
        _close_recovery_authority(authority, original=operation_error)
        raise
    authority.cleanup()
    return result


def _close_recovery_authority(
    authority: GitRepositoryAuthority,
    *,
    original: BaseException,
) -> None:
    """Close descriptors without allowing cleanup uncertainty to mask control flow."""
    try:
        authority.cleanup()
    except BaseException as cleanup_error:  # noqa: BLE001 - descriptor closure is mandatory
        original.add_note(
            f"Recovery repository-authority cleanup also failed: {type(cleanup_error).__name__}."
        )


def _uncertain_recovery_ref(branch_ref: str) -> ManduaError:
    return ManduaError(
        ErrorCode.GIT_FAILURE,
        "The recovery branch command was interrupted and the mutation state is uncertain.",
        recovery=f"Inspect {branch_ref} before retrying recovery.",
    )


def _discover(inspector: RepositoryInspector, limits: QueryLimits, deadline: float) -> _Discovery:
    candidates: dict[str, _Candidate] = {}
    missing_objects: list[str] = []
    truncated = False
    incomplete = False
    remaining_output = limits.max_output_bytes
    for source in ("refs", "reflog", "fsck"):
        if remaining_output < 1:
            truncated = True
            break
        try:
            output = inspector.recovery_source(
                source,
                max_output_bytes=remaining_output,
                timeout_seconds=_remaining_timeout(deadline),
            )
        except ManduaError as error:
            if error.code is ErrorCode.LIMIT_EXCEEDED:
                truncated = True
                break
            raise
        remaining_output -= len(output.stdout) + len(output.stderr)
        if source == "fsck":
            missing_objects.extend(_missing_fsck_oids(output.stdout))
        if output.returncode != 0:
            incomplete = True
            if source == "fsck" and missing_objects:
                continue
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not read a recovery source.")
        if source == "refs":
            records = _ref_records(output.stdout)
        elif source == "reflog":
            records = _reflog_records(output.stdout)
        else:
            records = _fsck_records(output.stdout)
        for oid, subject, ref in records:
            key = oid.lower()
            candidate = candidates.get(key)
            if candidate is None:
                if len(candidates) >= limits.max_commits:
                    truncated = True
                    continue
                candidate = _Candidate(oid=key)
                candidates[key] = candidate
            if source not in candidate.sources:
                candidate.sources.append(source)
            if subject and not candidate.subject:
                candidate.subject = subject
            if ref and ref not in candidate.refs:
                if len(candidate.refs) >= limits.max_commits:
                    truncated = True
                else:
                    candidate.refs.append(ref)
    return _Discovery(
        candidates=tuple(candidates.values()),
        truncated=truncated,
        missing_objects=_unique(missing_objects),
        incomplete=incomplete,
    )


def _ref_records(payload: bytes) -> tuple[tuple[str, str, bytes | None], ...]:
    records: list[tuple[str, str, bytes | None]] = []
    for oid, object_type, peeled_oid, peeled_type, ref, subject in _nul_records(
        payload, field_count=6
    ):
        if re.fullmatch(_OBJECT_ID, oid) is None or not object_type or not ref:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid recovery ref record.")
        if object_type == b"commit":
            commit_oid = oid
        elif (
            object_type == b"tag"
            and peeled_type == b"commit"
            and re.fullmatch(_OBJECT_ID, peeled_oid) is not None
        ):
            commit_oid = peeled_oid
        else:
            continue
        records.append(
            (
                commit_oid.decode("ascii").lower(),
                _search_text(subject),
                ref,
            )
        )
    return tuple(records)


def _reflog_records(payload: bytes) -> tuple[tuple[str, str, bytes | None], ...]:
    records: list[tuple[str, str, bytes | None]] = []
    for oid, subject in _nul_records(payload, field_count=2):
        if re.fullmatch(_OBJECT_ID, oid) is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid recovery reflog record."
            )
        records.append((oid.decode("ascii").lower(), _search_text(subject), None))
    return tuple(records)


def _fsck_records(payload: bytes) -> tuple[tuple[str, str, bytes | None], ...]:
    records: list[tuple[str, str, bytes | None]] = []
    for raw_line in payload.splitlines():
        match = _UNREACHABLE_COMMIT.fullmatch(raw_line)
        if match is not None:
            records.append((match.group(1).decode("ascii").lower(), "", None))
    return tuple(records)


def _select(
    inspector: RepositoryInspector, discovery: _Discovery, query: str, deadline: float
) -> _Candidate:
    candidates = discovery.candidates
    object_id_length = inspector.recovery_object_id_length(
        timeout_seconds=_remaining_timeout(deadline)
    )
    lowered = query.casefold()
    exact_oid = [candidate for candidate in candidates if candidate.oid == query.lower()]
    query_ref = _query_ref_bytes(query)
    exact_ref = [candidate for candidate in candidates if query_ref in candidate.refs]
    if exact_oid:
        matches = exact_oid
    elif exact_ref:
        matches = exact_ref
    elif _ABBREVIATION.fullmatch(query):
        matches = [candidate for candidate in candidates if candidate.oid.startswith(query.lower())]
    else:
        matches = [candidate for candidate in candidates if lowered in candidate.subject.casefold()]
    if not matches and _is_full_oid(query, object_id_length):
        _ensure_available(inspector, query.lower(), deadline)
        return _Candidate(oid=query.lower(), sources=["direct"])
    if (discovery.truncated or discovery.incomplete) and not exact_oid and not exact_ref:
        raise ManduaError(
            ErrorCode.INCOMPLETE_HISTORY,
            "Recovery candidates were incomplete before the query could be resolved uniquely.",
        )
    if not matches:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "No local recovery candidate matches the query."
        )
    if len(matches) != 1:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery query is ambiguous.")
    selected = matches[0]
    _ensure_available(inspector, selected.oid, deadline)
    return selected


def _ensure_available(inspector: RepositoryInspector, oid: str, deadline: float) -> None:
    status = inspector.recovery_commit_available(oid, timeout_seconds=_remaining_timeout(deadline))
    if status.kind == "commit":
        return
    if status.kind == "missing" and status.missing_objects:
        missing_oid = status.missing_objects[0]
        raise ManduaError(
            ErrorCode.MISSING_OBJECT,
            "The selected recovery commit is unavailable.",
            evidence=(
                Evidence(
                    id=f"missing-object:{missing_oid}",
                    kind="missing-object",
                    oid=missing_oid,
                    details={"object_oid": missing_oid},
                ),
            ),
        )
    if status.kind == "non_commit":
        raise ManduaError(ErrorCode.INVALID_REVISION, "The recovery object is not a commit.")
    raise ManduaError(
        ErrorCode.INCOMPLETE_HISTORY,
        "The selected recovery commit could not be verified locally.",
    )


def _recovery_scope(
    inspector: RepositoryInspector,
    discovery: _Discovery,
    selected: _Candidate | None,
    deadline: float,
):
    scope = inspector.history_scope(timeout_seconds=_remaining_timeout(deadline))
    candidate_oids = {candidate.oid for candidate in discovery.candidates}
    missing_objects = _unique(
        (
            *(item for item in scope.missing_objects if item not in candidate_oids),
            *discovery.missing_objects,
        )
    )
    return replace(
        scope,
        start_oid=selected.oid if selected is not None else scope.start_oid,
        end_oid=selected.oid if selected is not None else scope.end_oid,
        commit_count=len(discovery.candidates),
        truncated=scope.truncated or discovery.truncated,
        missing_objects=missing_objects,
    )


def _result(
    discovery: _Discovery,
    scope,
    selected: _Candidate | None,
    branch_ref: str | None,
    apply: bool,
    limits: QueryLimits,
    *,
    mutation_warning: str | None = None,
) -> MemoryResult:
    candidates = discovery.candidates
    evidence_truncated = False
    if selected is not None and all(candidate.oid != selected.oid for candidate in candidates):
        if len(candidates) >= limits.max_commits:
            candidates = (selected, *candidates[: limits.max_commits - 1])
            evidence_truncated = True
        else:
            candidates = (*candidates, selected)
    evidence = _candidate_evidence(candidates, limits)
    warnings: list[str] = []
    if discovery.truncated:
        warnings.append("Recovery candidates are limited by the configured commit or output bound.")
    if scope.shallow:
        warnings.append("History is shallow; earlier recovery candidates may be unavailable.")
    if scope.missing_objects:
        warnings.append("Some history objects are unavailable.")
    if discovery.incomplete:
        warnings.append("Local recovery discovery was incomplete.")
    if evidence_truncated:
        scope = replace(scope, truncated=True)
        warnings.append("Recovery candidate evidence is limited by the configured commit bound.")
    if mutation_warning is not None:
        warnings.append(mutation_warning)
    change = (
        ()
        if branch_ref is None or selected is None
        else (
            PlannedChange(
                action="create-ref", target=branch_ref, before_oid=None, after_oid=selected.oid
            ),
        )
    )
    if selected is None:
        answer = (
            "Bounded local recovery candidates were inspected without changing repository refs."
        )
        observed = (Claim(f"Found {len(discovery.candidates)} local recovery candidates."),)
    else:
        answer = (
            "A recovery branch was created from a local history candidate."
            if apply
            else (
                "A local history recovery branch was planned without changing repository refs."
                if branch_ref is not None
                else "A local history recovery candidate was selected without changing repository refs."
            )
        )
        observed = (
            Claim(
                text=f"Selected local commit {selected.oid} for recovery.",
                evidence=(f"recovery-candidate:{selected.oid}",),
            ),
        )
    return MemoryResult(
        operation="recover",
        answer=answer,
        observed=observed,
        evidence=evidence,
        history_scope=scope,
        warnings=tuple(warnings),
        changes=change,
        applied=apply,
    )


def _candidate_evidence(
    candidates: tuple[_Candidate, ...], limits: QueryLimits
) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(
            id=f"recovery-candidate:{candidate.oid}",
            kind="recovery-candidate",
            oid=candidate.oid,
            ref=_bounded_text(candidate.refs[0], limits) if candidate.refs else None,
            excerpt=_safe_text(candidate.subject, limits.max_excerpt_chars),
            details={
                "sources": tuple(candidate.sources),
                "refs": tuple(_bounded_text(ref, limits) for ref in candidate.refs),
            },
        )
        for candidate in candidates
    )


def _validate_request(
    query: str | None, create_branch: str | None, apply: bool, limits: QueryLimits
) -> None:
    for value, message, optional in (
        (query, "The recovery query is invalid.", True),
        (create_branch, "The recovery branch is invalid.", True),
    ):
        if value is None and optional:
            continue
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, message)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, message) from None
    if not isinstance(apply, bool):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery apply flag is invalid.")
    if create_branch is not None and query is None:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED,
            "A recovery branch requires a selected recovery query.",
        )
    if apply and (query is None or create_branch is None):
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED,
            "Applying recovery requires both a query and a new branch name.",
        )


def _bounded_text(value: bytes, limits: QueryLimits) -> str:
    return _safe_text(value.decode("utf-8", errors="replace"), limits.max_excerpt_chars)


def _search_text(value: bytes) -> str:
    """Render already output-bounded subject bytes without applying a public display bound."""
    return _render_safe_text(value.decode("utf-8", errors="replace"))


def _safe_text(value: str, limit: int) -> str:
    return bound_text(_render_safe_text(value), limit)


def _render_safe_text(value: str) -> str:
    rendered: list[str] = []
    for character in value:
        if character.isprintable():
            rendered.append(character)
        elif ord(character) <= 0xFFFF:
            rendered.append(f"\\u{ord(character):04x}")
        else:
            rendered.append(f"\\U{ord(character):08x}")
    return "".join(rendered)


def _nul_records(payload: bytes, *, field_count: int) -> tuple[tuple[bytes, ...], ...]:
    """Parse an exactly framed sequence of NUL fields, allowing only Git's record newline."""
    records: list[tuple[bytes, ...]] = []
    cursor = 0
    while cursor < len(payload):
        if payload[cursor : cursor + 1] == b"\n":
            cursor += 1
            if cursor == len(payload):
                break
        if payload[cursor : cursor + 1] != b"\0":
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid recovery record.")
        cursor += 1
        fields: list[bytes] = []
        for _ in range(field_count):
            end = payload.find(b"\0", cursor)
            if end < 0:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an incomplete recovery record."
                )
            fields.append(payload[cursor:end])
            cursor = end + 1
        records.append(tuple(fields))
    return tuple(records)


def _query_ref_bytes(query: str) -> bytes:
    try:
        return query.encode("utf-8")
    except UnicodeEncodeError:
        return b"\xff"


def _missing_fsck_oids(value: bytes) -> tuple[str, ...]:
    """Extract only full object IDs from fsck records that explicitly report missing data."""
    return _unique(
        match.group(1).decode("ascii").lower()
        for line in value.splitlines()
        for match in (_MISSING_FSCK_OBJECT.fullmatch(line),)
        if match is not None
    )


def _is_full_oid(value: str, object_id_length: int) -> bool:
    return len(value) == object_id_length and re.fullmatch(r"[0-9a-fA-F]+", value) is not None


def _unique(values: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))  # type: ignore[arg-type]


def _deadline(timeout_seconds: float) -> float:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "The recovery timeout must be finite and positive."
        )
    return time.monotonic() + float(timeout_seconds)


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
        raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Recovery exceeded the configured time limit.")
    return remaining
