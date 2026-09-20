"""Translate repository state into the stable result contract."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import PurePosixPath

from mandua.bounds import bound_text
from mandua.errors import ErrorCode, ManduaError
from mandua.models import Claim, Evidence, HistoryScope, MemoryResult, QueryLimits
from mandua.repository import CommitRecord, RepositoryInspector


def status_result(inspector: RepositoryInspector, limits: QueryLimits) -> MemoryResult:
    """Return an honest bounded snapshot of index, worktree, and history state."""
    repository_status = inspector.status()
    evidence = tuple(
        _evidence(item.path, item.state, item.previous_path, item.copy_from, limits)
        for item in repository_status.entries
    )
    gaps: list[str] = []
    warnings: list[str] = []
    if repository_status.history_scope.shallow:
        warnings.append("History is shallow; earlier commits may be unavailable.")
    if repository_status.history_scope.truncated:
        warnings.append("History scope is limited by configured commit or display bounds.")
    if repository_status.history_scope.missing_objects:
        warnings.append("Some history objects are unavailable.")
    if repository_status.history_access_failed:
        warnings.append("History access failed; the reported scope may be incomplete.")
        gaps.append("History traversal failed before a complete scope could be established.")
    if not repository_status.history_scope.notes_available:
        warnings.append("Review notes are unavailable.")
    observed = (
        Claim(
            text="Repository state was read without refreshing the index or contacting a remote.",
            evidence=tuple(item.id for item in evidence),
        ),
    )
    return MemoryResult(
        operation="status",
        answer="Repository status and reachable history scope were inspected.",
        observed=observed,
        evidence=evidence,
        history_scope=repository_status.history_scope,
        gaps=tuple(gaps),
        warnings=tuple(warnings),
    )


def context_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    *,
    task_id: str | None = None,
    branch: str | None = None,
    limit: int | None = None,
    canonical_branch: str = "main",
) -> MemoryResult:
    """Reconstruct context from exact task trailers, a branch delta, or current state."""
    if task_id is not None and branch is not None:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED,
            "Context accepts either a task ID or a branch, not both.",
        )
    record_limit = _record_limit(limit, limits)
    if task_id is not None:
        _validate_text(task_id, limits, "The task ID is invalid.")
        scan_records = inspector.log(("--all",), limit=limits.max_commits + 1)
        records, scan_truncated = _truncate_records(scan_records, limits.max_commits)
        matches = tuple(
            record
            for record in records
            if any(key == "Task-ID" and value == task_id for key, value in record.trailers)
        )
        selected, matches_truncated = _truncate_records(matches, record_limit)
        scope = _query_scope(inspector, records, ("--all",), scan_truncated)
        evidence = tuple(
            _commit_evidence(inspector, record, "task-commit", None, limits)
            for record in _chronological(selected)
        )
        bounded_task_id = _truncate(task_id, limits.max_excerpt_chars)
        gaps = () if selected else (f"No commits were found for task {bounded_task_id}.",)
        warnings = (
            ("Task matches are limited by the requested result bound.",)
            if matches_truncated
            else ()
        )
        noun = "commit" if len(matches) == 1 else "commits"
        return MemoryResult(
            operation="context",
            answer=f"Context was reconstructed for task {bounded_task_id}.",
            observed=(Claim(f"Found {len(matches)} {noun} for task {bounded_task_id}."),),
            evidence=evidence,
            history_scope=scope,
            gaps=gaps,
            warnings=warnings,
        )

    if branch is not None:
        branch_ref = _branch_ref(branch, limits)
        canonical_ref = _branch_ref(canonical_branch, limits)
        bounded_branch = _truncate(branch, limits.max_excerpt_chars)
        canonical_oid = inspector.resolve_commit(canonical_ref)
        branch_oid = inspector.resolve_commit(branch_ref)
        merge_base = inspector.merge_base(canonical_oid, branch_oid)
        records = inspector.log((f"{merge_base}..{branch_oid}",), limit=record_limit + 1)
        selected, truncated = _truncate_records(records, record_limit)
        scope = _query_scope(
            inspector,
            selected,
            (canonical_ref, branch_ref),
            truncated,
            end_oid=branch_oid,
        )
        noun = "commit" if len(selected) == 1 else "commits"
        return MemoryResult(
            operation="context",
            answer=f"Context was reconstructed for branch {bounded_branch}.",
            observed=(
                Claim(f"Found {len(selected)} branch-only {noun} for branch {bounded_branch}."),
            ),
            evidence=tuple(
                _commit_evidence(inspector, record, "branch-commit", None, limits)
                for record in _chronological(selected)
            ),
            history_scope=scope,
            gaps=(
                ()
                if selected
                else (f"No branch-only commits were found for branch {bounded_branch}.",)
            ),
        )

    repository_status = inspector.status()
    records = (
        inspector.log((repository_status.head_oid,), limit=record_limit + 1)
        if repository_status.head_oid is not None
        else ()
    )
    selected, truncated = _truncate_records(records, record_limit)
    refs = ("HEAD",)
    if repository_status.branch is not None:
        refs += (f"refs/heads/{repository_status.branch}",)
    scope = inspector.bound_history_scope(
        replace(
            repository_status.history_scope,
            start_oid=selected[-1].oid if selected else None,
            commit_count=len(selected),
            truncated=repository_status.history_scope.truncated or truncated,
            refs=refs,
        )
    )
    status_evidence = tuple(
        _evidence(item.path, item.state, item.previous_path, item.copy_from, limits)
        for item in repository_status.entries
    )
    return MemoryResult(
        operation="context",
        answer="Context was reconstructed from the current branch and working tree.",
        observed=(
            Claim(
                f"Current branch is {_truncate(repository_status.branch, limits.max_excerpt_chars) if repository_status.branch else 'detached'}.",
                evidence=tuple(item.id for item in status_evidence),
            ),
        ),
        evidence=tuple(
            _commit_evidence(inspector, record, "context-commit", None, limits)
            for record in _chronological(selected)
        )
        + status_evidence,
        history_scope=scope,
        gaps=(
            ("No current commit is available for context reconstruction.",)
            if repository_status.head_oid is None
            else ()
        ),
    )


def timeline_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    *,
    path: PurePosixPath | None = None,
    start: str | None = None,
    end: str | None = "HEAD",
    limit: int | None = 100,
) -> MemoryResult:
    """Return a chronological, bounded timeline for a repository range and optional path."""
    record_limit = _record_limit(limit, limits)
    if path is not None:
        inspector.validate_path(path)
    start_oid = inspector.resolve_commit(start) if start is not None else None
    end_oid = inspector.resolve_commit(end or "HEAD")
    revision = f"{start_oid}..{end_oid}" if start_oid is not None else end_oid
    records = inspector.log((revision,), path=path, limit=record_limit + 1)
    selected, truncated = _truncate_records(records, record_limit)
    scope = _query_scope(
        inspector,
        selected,
        (revision,),
        truncated,
        end_oid=end_oid,
    )
    return MemoryResult(
        operation="timeline",
        answer="Timeline was reconstructed from repository history.",
        observed=(Claim(f"Found {len(selected)} timeline commit records."),),
        evidence=tuple(
            _commit_evidence(inspector, record, "timeline-commit", path, limits)
            for record in _chronological(selected)
        ),
        history_scope=scope,
    )


def _evidence(
    path: str,
    state: str,
    previous_path: str | None,
    copy_from: str | None,
    limits: QueryLimits,
) -> Evidence:
    """Bound untrusted path data before returning it through the public contract."""
    bounded_path = _truncate(path, limits.max_excerpt_chars)
    details: dict[str, str] = {"state": state}
    if previous_path is not None:
        details["previous_path"] = _truncate(previous_path, limits.max_excerpt_chars)
    if copy_from is not None:
        details["copy_from"] = _truncate(copy_from, limits.max_excerpt_chars)
    path_digest = hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()[:16]
    return Evidence(
        id=f"status:{state}:{path_digest}:{bounded_path}",
        kind="RepositoryStatus",
        path=bounded_path,
        excerpt=_truncate(f"{state}: {path}", limits.max_excerpt_chars),
        details=details,
    )


def _record_limit(limit: int | None, limits: QueryLimits) -> int:
    if limit is None:
        return limits.max_commits
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit limit must be positive.")
    return min(limit, limits.max_commits)


def _truncate_records(
    records: tuple[CommitRecord, ...], limit: int
) -> tuple[tuple[CommitRecord, ...], bool]:
    return records[:limit], len(records) > limit


def _chronological(records: tuple[CommitRecord, ...]) -> tuple[CommitRecord, ...]:
    """Present a newest-first Git window from oldest to newest after it is bounded."""
    return tuple(reversed(records))


def _query_scope(
    inspector: RepositoryInspector,
    records: tuple[CommitRecord, ...],
    refs: tuple[str, ...],
    truncated: bool,
    *,
    end_oid: str | None = None,
) -> HistoryScope:
    baseline = inspector.history_scope(end_oid)
    return inspector.bound_history_scope(
        replace(
            baseline,
            start_oid=records[-1].oid if records else None,
            end_oid=end_oid or (records[0].oid if records else None),
            refs=refs,
            commit_count=len(records),
            truncated=baseline.truncated or truncated,
        )
    )


def _commit_evidence(
    inspector: RepositoryInspector,
    record: CommitRecord,
    kind: str,
    selected_path: PurePosixPath | None,
    limits: QueryLimits,
) -> Evidence:
    details: dict[str, object] = {
        "parents": record.parents,
        "author_time": record.author_time,
        "changed_paths": tuple(_truncate(item, limits.max_excerpt_chars) for item in record.paths),
    }
    declared_agent = False
    for key, value in record.trailers:
        detail_key = key.lower().replace("-", "_")
        if detail_key in {"memory_type", "scope", "task_id", "decision_id", "agent_id"}:
            details.setdefault(detail_key, _truncate(value, limits.max_excerpt_chars))
            declared_agent = declared_agent or detail_key == "agent_id"
    details["signature_verified"] = (
        inspector.signature_status(
            record.oid,
            timeout_seconds=inspector.remaining_operation_timeout(),
        )
        if declared_agent
        else False
    )
    path = (
        _truncate(selected_path.as_posix(), limits.max_excerpt_chars)
        if selected_path is not None
        else None
    )
    return Evidence(
        id=f"{kind}:{record.oid}",
        kind=kind,
        oid=record.oid,
        path=path,
        excerpt=_truncate(record.subject, limits.max_excerpt_chars),
        details=details,
    )


def _validate_text(value: str, limits: QueryLimits, message: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or len(value) > limits.max_input_chars
    ):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, message)


def _branch_ref(branch: str, limits: QueryLimits) -> str:
    _validate_text(branch, limits, "The branch is invalid.")
    if branch.startswith("refs/heads/"):
        return branch
    if branch.startswith("refs/"):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The branch is invalid.")
    return f"refs/heads/{branch}"


def _truncate(value: str, limit: int) -> str:
    return bound_text(value, limit)
