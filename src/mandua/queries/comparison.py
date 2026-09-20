"""Compare bounded Git histories without treating patch similarity as identity."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import replace
from pathlib import PurePosixPath
from time import monotonic

from mandua.bounds import bound_text, normalize_timeout_seconds
from mandua.errors import ErrorCode, ManduaError
from mandua.models import Claim, Confidence, Evidence, MemoryResult, QueryLimits
from mandua.repository import ComparisonNameStatus, RepositoryInspector


def stable_patch_id(
    inspector: RepositoryInspector,
    commit: str,
    *,
    path: PurePosixPath | None = None,
    timeout_seconds: float | None = None,
) -> str | None:
    """Return a stable patch ID for one already resolved and optionally scoped commit."""
    return inspector.stable_patch_id(commit, path=path, timeout_seconds=timeout_seconds)


def compare_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    left: str,
    right: str,
    *,
    path: PurePosixPath | None = None,
    limit: int | None = 100,
) -> MemoryResult:
    """Compare two resolved hypotheses through merge-base-exclusive bounded evidence."""
    result_limit = _result_limit(limit, limits)
    deadline = _deadline(limits.timeout_seconds)
    if path is not None:
        inspector.validate_path(path)
    left_oid = inspector.resolve_commit(left, timeout_seconds=_remaining_timeout(deadline))
    right_oid = inspector.resolve_commit(right, timeout_seconds=_remaining_timeout(deadline))
    baseline_scope = inspector.comparison_history_scope(
        left_oid, right_oid, timeout_seconds=_remaining_timeout(deadline)
    )
    try:
        merge_base = inspector.merge_base_or_none(
            left_oid, right_oid, timeout_seconds=_remaining_timeout(deadline)
        )
    except ManduaError as error:
        if error.code is ErrorCode.GIT_FAILURE:
            _raise_incomplete_comparison_history(baseline_scope, limits)
        raise
    if merge_base is None:
        _raise_incomplete_comparison_history(baseline_scope, limits)
        raise ManduaError(ErrorCode.CONFLICT, "The revisions have no merge base.")

    left_records = inspector.exclusive_commit_oids(
        merge_base,
        left_oid,
        path=path,
        limit=result_limit + 1,
        timeout_seconds=_remaining_timeout(deadline),
    )
    right_records = inspector.exclusive_commit_oids(
        merge_base,
        right_oid,
        path=path,
        limit=result_limit + 1,
        timeout_seconds=_remaining_timeout(deadline),
    )
    left_commits, left_truncated = _truncate_commits(left_records, result_limit)
    right_commits, right_truncated = _truncate_commits(right_records, result_limit)
    state = inspector.comparison_state(
        left_oid, right_oid, path=path, timeout_seconds=_remaining_timeout(deadline)
    )
    left_patch_ids = {
        commit: stable_patch_id(
            inspector, commit, path=path, timeout_seconds=_remaining_timeout(deadline)
        )
        for commit in left_commits
    }
    right_patch_ids = {
        commit: stable_patch_id(
            inspector, commit, path=path, timeout_seconds=_remaining_timeout(deadline)
        )
        for commit in right_commits
    }
    correspondences, ambiguous_correspondences = _patch_correspondences(
        left_patch_ids, right_patch_ids
    )

    evidence: list[Evidence] = [
        Evidence(
            id=f"merge-base:{merge_base}",
            kind="merge-base",
            oid=merge_base,
            details={"left_oid": left_oid, "right_oid": right_oid},
        )
    ]
    evidence.extend(
        _commit_evidence("left-only", commit, left_patch_ids[commit])
        for commit in reversed(left_commits)
    )
    evidence.extend(
        _commit_evidence("right-only", commit, right_patch_ids[commit])
        for commit in reversed(right_commits)
    )
    if state.stat:
        evidence.append(
            Evidence(
                id=f"state-stat:{_digest(state.stat)}",
                kind="state-stat",
                excerpt=_truncate(state.stat, limits),
                details={"left_oid": left_oid, "right_oid": right_oid},
            )
        )
    evidence.extend(
        _name_status_evidence(item, index, limits) for index, item in enumerate(state.names)
    )
    correspondence_evidence = tuple(
        Evidence(
            id=f"patch-correspondence:{left_commit}:{right_commit}",
            kind="patch-correspondence",
            details={"left_oid": left_commit, "right_oid": right_commit},
        )
        for left_commit, right_commit in correspondences
    )
    evidence.extend(correspondence_evidence)
    ambiguous_evidence = tuple(
        Evidence(
            id=f"patch-correspondence-ambiguous:{patch_id}",
            kind="patch-correspondence-ambiguous",
            details={
                "stable_patch_id": patch_id,
                "left_oids": left_oids,
                "right_oids": right_oids,
                "left_count": len(left_oids),
                "right_count": len(right_oids),
            },
        )
        for patch_id, left_oids, right_oids in ambiguous_correspondences
    )
    evidence.extend(ambiguous_evidence)

    truncated = left_truncated or right_truncated
    scope = inspector.bound_history_scope(
        replace(
            baseline_scope,
            start_oid=merge_base,
            end_oid=right_oid,
            refs=(left_oid, right_oid),
            commit_count=len(left_commits) + len(right_commits),
            truncated=baseline_scope.truncated or truncated,
        )
    )
    warnings: list[str] = []
    truncation_source = _truncation_source(limit, limits)
    for side, side_truncated in (("left", left_truncated), ("right", right_truncated)):
        if not side_truncated:
            continue
        evidence.append(
            Evidence(
                id=f"comparison-history-truncation:{side}:{result_limit}",
                kind="comparison-history-truncation",
                details={
                    "side": side,
                    "effective_limit": result_limit,
                    "source": truncation_source,
                },
            )
        )
        if truncation_source == "caller":
            warnings.append(
                f"{side.title()} comparison history is limited by the caller result limit of {result_limit}."
            )
    if truncated and truncation_source == "configured":
        warnings.append("Comparison histories are limited by the configured commit bound.")
    if baseline_scope.shallow:
        warnings.append("History is shallow; earlier commits may be unavailable.")
    if baseline_scope.truncated:
        warnings.append(
            "Repository history scope is limited by configured commit or display bounds."
        )
    if baseline_scope.missing_objects:
        warnings.append("Some history objects are unavailable.")
    if correspondence_evidence:
        warnings.append(
            "Patch correspondence is inferred from stable patch IDs; it does not establish commit identity or intent."
        )
    if ambiguous_evidence:
        warnings.append("Some stable patch IDs are ambiguous and were not paired.")
    gaps: list[str] = []
    if not left_commits and not right_commits:
        gaps.append("No exclusive commits were found between the compared revisions.")
    if not state.names:
        gaps.append("No state differences were found between the compared revisions.")
    inferred: list[Claim] = []
    if correspondence_evidence:
        inferred.append(
            Claim(
                text=(
                    "Matching stable patch IDs indicate inferred patch correspondence; "
                    "this does not establish commit identity or intent."
                ),
                evidence=tuple(item.id for item in correspondence_evidence),
            )
        )
    if ambiguous_evidence:
        inferred.append(
            Claim(
                text="Matching stable patch IDs have ambiguous candidate sets and were not paired.",
                evidence=tuple(item.id for item in ambiguous_evidence),
            )
        )
    return MemoryResult(
        operation="compare",
        answer="Repository hypotheses were compared from immutable commit object IDs.",
        observed=(
            Claim(
                text=(
                    f"Compared {len(left_commits)} left-only and {len(right_commits)} right-only "
                    "commit records."
                ),
                evidence=tuple(
                    item.id for item in evidence if not item.kind.startswith("patch-correspondence")
                ),
            ),
        ),
        inferred=tuple(inferred),
        evidence=tuple(evidence),
        history_scope=scope,
        confidence=Confidence.MEDIUM if inferred else Confidence.HIGH,
        gaps=tuple(gaps),
        warnings=tuple(warnings),
    )


def _result_limit(limit: int | None, limits: QueryLimits) -> int:
    if limit is None:
        return limits.max_commits
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit limit must be positive.")
    return min(limit, limits.max_commits)


def _truncate_commits(commits: tuple[str, ...], limit: int) -> tuple[tuple[str, ...], bool]:
    return commits[:limit], len(commits) > limit


def _commit_evidence(kind: str, oid: str, patch_id: str | None) -> Evidence:
    details = {} if patch_id is None else {"stable_patch_id": patch_id}
    return Evidence(id=f"{kind}:{oid}", kind=kind, oid=oid, details=details)


def _name_status_evidence(
    record: ComparisonNameStatus, index: int, limits: QueryLimits
) -> Evidence:
    details: dict[str, object] = {"status": record.status}
    _add_path_details(details, "old_path", record.old_path, limits)
    _add_path_details(details, "new_path", record.new_path, limits)
    path = _bounded_path(record.new_path or record.old_path, limits)
    return Evidence(
        id=f"state-name-status:{index}:{_digest(record.status + (path or ''))}",
        kind="state-name-status",
        path=path,
        details=details,
    )


def _patch_correspondences(
    left_patch_ids: dict[str, str | None], right_patch_ids: dict[str, str | None]
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]]:
    left_by_patch: dict[str, list[str]] = {}
    right_by_patch: dict[str, list[str]] = {}
    for commit, patch_id in left_patch_ids.items():
        if patch_id is not None:
            left_by_patch.setdefault(patch_id, []).append(commit)
    for commit, patch_id in right_patch_ids.items():
        if patch_id is not None:
            right_by_patch.setdefault(patch_id, []).append(commit)
    pairs: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for patch_id in sorted(set(left_by_patch) & set(right_by_patch)):
        left_oids = tuple(left_by_patch[patch_id])
        right_oids = tuple(right_by_patch[patch_id])
        if len(left_oids) == 1 and len(right_oids) == 1:
            pairs.append((left_oids[0], right_oids[0]))
        else:
            ambiguous.append((patch_id, left_oids, right_oids))
    return tuple(pairs), tuple(ambiguous)


def _add_path_details(
    details: dict[str, object], name: str, path: bytes | None, limits: QueryLimits
) -> None:
    if path is None:
        details[name] = None
        details[f"{name}_bytes"] = None
        details[f"{name}_bytes_truncated"] = False
        return
    details[name] = _bounded_path(path, limits)
    path_hex = path.hex()
    if len(path_hex) <= limits.max_excerpt_chars:
        details[f"{name}_bytes"] = path_hex
        details[f"{name}_bytes_truncated"] = False
        return
    prefix_length = limits.max_excerpt_chars - (limits.max_excerpt_chars % 2)
    details[f"{name}_bytes_prefix"] = path_hex[:prefix_length]
    details[f"{name}_bytes_sha256"] = hashlib.sha256(path).hexdigest()
    details[f"{name}_bytes_truncated"] = True


def _bounded_path(path: bytes | None, limits: QueryLimits) -> str | None:
    return None if path is None else _truncate(_render_path(path), limits)


def _render_path(path: bytes) -> str:
    rendered: list[str] = []
    for character in os.fsdecode(path):
        codepoint = ord(character)
        if character == "\\":
            rendered.append("\\\\")
        elif character == "\n":
            rendered.append("\\n")
        elif character == "\r":
            rendered.append("\\r")
        elif character == "\t":
            rendered.append("\\t")
        elif 0xDC80 <= codepoint <= 0xDCFF:
            rendered.append(f"\\x{codepoint - 0xDC00:02x}")
        elif not character.isprintable():
            rendered.append(_escaped_codepoint(codepoint))
        else:
            rendered.append(character)
    return "".join(rendered)


def _escaped_codepoint(codepoint: int) -> str:
    if codepoint <= 0xFF:
        return f"\\x{codepoint:02x}"
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def _raise_incomplete_comparison_history(scope, limits: QueryLimits) -> None:
    if scope.missing_objects:
        missing_oid = scope.missing_objects[0]
        raise ManduaError(
            ErrorCode.MISSING_OBJECT,
            "Comparison requires a missing Git object.",
            evidence=(
                Evidence(
                    id=f"missing-object:{missing_oid}",
                    kind="missing-object",
                    oid=missing_oid,
                    details={"object_oid": missing_oid},
                ),
            ),
            recovery="Restore the required object before comparing revisions.",
        )
    if scope.shallow:
        raise ManduaError(
            ErrorCode.INCOMPLETE_HISTORY,
            "Comparison cannot determine a merge base from shallow history.",
            evidence=(
                Evidence(
                    id=f"comparison-history-scope:{scope.end_oid or 'none'}",
                    kind="comparison-history-scope",
                    oid=scope.end_oid,
                    details={
                        "shallow": True,
                        "commit_count": min(scope.commit_count, limits.max_commits),
                        "truncated": scope.truncated,
                    },
                ),
            ),
            recovery="Fetch complete history before comparing revisions.",
        )


def _truncation_source(limit: int | None, limits: QueryLimits) -> str:
    return "caller" if limit is not None and limit < limits.max_commits else "configured"


def _deadline(timeout_seconds: float) -> float:
    normalized_timeout = normalize_timeout_seconds(
        timeout_seconds,
        message="The comparison timeout must be finite and positive.",
    )
    deadline = monotonic() + normalized_timeout
    if not math.isfinite(deadline):
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "The comparison timeout must be finite and positive."
        )
    return deadline


def _truncate(value: str, limits: QueryLimits) -> str:
    return bound_text(value, limits.max_excerpt_chars)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - monotonic()
    if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
        raise ManduaError(
            ErrorCode.LIMIT_EXCEEDED, "Comparison exceeded the configured time limit."
        )
    return remaining
