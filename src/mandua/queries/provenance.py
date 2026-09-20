"""Ground line explanations and decisions in bounded Git records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from mandua.bounds import bound_text
from mandua.errors import ErrorCode, ManduaError
from mandua.metadata import parse_recorded_reason
from mandua.models import Claim, Confidence, Evidence, MemoryResult, QueryLimits
from mandua.repository import CommitRecord, RepositoryInspector

_FULL_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_PATCH_RECORD = re.compile(rb"\0\0([0-9a-f]{40}|[0-9a-f]{64})\0")


@dataclass(slots=True)
class _PatchFile:
    """Raw old/new paths and changed hunk payloads for one patch file."""

    old_path: bytes | None = None
    new_path: bytes | None = None
    old_hunks: list[bytearray] = field(default_factory=list)
    new_hunks: list[bytearray] = field(default_factory=list)


def why_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    *,
    path: PurePosixPath,
    line: int,
    revision: str = "HEAD",
) -> MemoryResult:
    """Explain one line from its blame attribution without inferring motivation."""
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The line number must be positive.")
    inspector.validate_path(path)
    revision_oid = inspector.resolve_commit(revision)
    blamed = inspector.blame_line(revision_oid, path, line)
    record = _commit_record(inspector, blamed.oid)
    bounded_path = _truncate(path.as_posix(), limits)
    line_evidence = Evidence(
        id=_line_evidence_id(blamed.oid, path, line),
        kind="line-blame",
        oid=blamed.oid,
        path=bounded_path,
        line=line,
        excerpt=_truncate(blamed.content, limits),
        details={"revision": revision_oid, "signature_verified": False},
    )
    observed = [
        Claim(
            text=f"Line {line} in {bounded_path} was last changed by commit {blamed.oid}.",
            evidence=(line_evidence.id,),
        )
    ]
    evidence: list[Evidence] = [line_evidence]
    motivations = _recorded_motivations(record, limits)
    for index, (kind, text) in enumerate(motivations):
        metadata_evidence = _metadata_evidence(record, kind, text, index, limits)
        evidence.append(metadata_evidence)
        observed.append(
            Claim(
                text=f"Recorded {kind}: {_truncate(text, limits)}",
                evidence=(metadata_evidence.id,),
            )
        )

    note_status = inspector.review_note_status(blamed.oid)
    if note_status.content is not None:
        note_evidence = Evidence(
            id=f"review-note:{blamed.oid}",
            kind="review-note",
            oid=blamed.oid,
            ref=_truncate(inspector.notes_ref, limits),
            excerpt=_truncate(note_status.content, limits),
            details={"signature_verified": False},
        )
        evidence.append(note_evidence)
        observed.append(
            Claim(
                text=f"Recorded review note: {_truncate(note_status.content, limits)}",
                evidence=(note_evidence.id,),
            )
        )

    recorded = bool(motivations) or note_status.content is not None
    warnings: list[str] = list(note_status.warnings)
    gaps: tuple[str, ...]
    if note_status.lookup_failed:
        warnings.append("Review note lookup failed; recorded motivation may be unavailable.")
        gaps = (
            ()
            if recorded
            else ("Recorded motivation could not be checked because review note lookup failed.",)
        )
    elif not note_status.ref_available:
        warnings.append(
            "Review notes are unavailable in this repository; a review may exist elsewhere. "
            "Fetch the configured notes ref explicitly to inspect it."
        )
        gaps = (
            ()
            if recorded
            else ("Recorded motivation could not be checked because review notes are unavailable.",)
        )
    else:
        gaps = () if recorded else ("No reason was recorded for this change.",)
    return MemoryResult(
        operation="why",
        answer=f"Line provenance was read for {bounded_path} line {line}.",
        observed=tuple(observed),
        evidence=tuple(evidence),
        history_scope=inspector.history_scope(revision_oid),
        confidence=Confidence.HIGH
        if recorded
        else (Confidence.LOW if warnings else Confidence.MEDIUM),
        gaps=gaps,
        warnings=tuple(warnings),
    )


def decision_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    decision_id: str,
    *,
    limit: int | None = 500,
) -> MemoryResult:
    """Find exact Decision-ID trailers and later recorded correction links."""
    _validate_decision_id(decision_id, limits)
    bounded_decision_id = _truncate(decision_id, limits)
    result_limit = _record_limit(limit, limits)
    scanned = inspector.log(("--all",), limit=limits.max_commits + 1)
    records = scanned[: limits.max_commits]
    scan_truncated = len(scanned) > limits.max_commits
    matches = tuple(
        record for record in records if _trailer_values(record, "Decision-ID").count(decision_id)
    )
    selected = matches[:result_limit]
    correction_candidates, verified_pairs, causal_warnings, causal_gaps = _causal_corrections(
        inspector,
        records,
        selected,
        limits,
        result_limit,
    )
    selected_oids = {record.oid for record in selected}
    selected_correction_oids = selected_oids.intersection(verified_pairs)
    selected_decisions = tuple(
        record for record in selected if record.oid not in selected_correction_oids
    )
    external_correction_candidates = tuple(
        record for record in correction_candidates if record.oid not in selected_oids
    )
    remaining_budget = result_limit - len(selected)
    external_corrections = external_correction_candidates[:remaining_budget]
    correction_oids = selected_correction_oids | {record.oid for record in external_corrections}
    corrections = tuple(record for record in records if record.oid in correction_oids)
    results_truncated = len(matches) > len(selected) or len(external_correction_candidates) > len(
        external_corrections
    )
    baseline_scope = inspector.history_scope()
    scope = inspector.bound_history_scope(
        replace(
            baseline_scope,
            start_oid=records[-1].oid if records else None,
            end_oid=records[0].oid if records else None,
            refs=("--all",),
            commit_count=len(records),
            truncated=baseline_scope.truncated or scan_truncated,
        )
    )
    warnings: list[str] = []
    if scan_truncated:
        warnings.append("Decision history is limited by the configured commit bound.")
    if results_truncated:
        warnings.append("Decision results are limited by the requested result bound.")
    warnings.extend(causal_warnings)
    evidence, note_warnings = _decision_evidence(
        inspector,
        limits,
        selected_decisions,
        corrections,
        verified_pairs,
        notes_available=scope.notes_available,
        evidence_limit=result_limit,
    )
    warnings.extend(note_warnings)
    gaps = (() if selected else (f"No commits were found for decision {bounded_decision_id}.",)) + (
        causal_gaps
    )
    noun = "commit" if len(matches) == 1 else "commits"
    result = MemoryResult(
        operation="decision",
        answer=f"Decision history was searched for {bounded_decision_id}.",
        observed=(Claim(f"Found {len(matches)} {noun} for decision {bounded_decision_id}."),),
        evidence=evidence,
        history_scope=scope,
        gaps=gaps,
        warnings=tuple(warnings),
    )
    return _bounded_decision_result(result, limits)


def _decision_evidence(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    selected: tuple[CommitRecord, ...],
    corrections: tuple[CommitRecord, ...],
    verified_pairs: dict[str, tuple[str, ...]],
    *,
    notes_available: bool,
    evidence_limit: int,
) -> tuple[tuple[Evidence, ...], tuple[str, ...]]:
    """Interleave bounded note evidence after each relevant chronological commit record."""
    deadline = time.monotonic() + limits.timeout_seconds if notes_available else None
    records = (
        *((record, "decision-commit") for record in reversed(selected)),
        *((record, "decision-correction") for record in reversed(corrections)),
    )
    primary_evidence = tuple(
        (
            record,
            _commit_evidence(inspector, record, kind, limits)
            if record.oid not in verified_pairs
            else _commit_evidence(
                inspector,
                record,
                kind,
                limits,
                verified_corrects=verified_pairs[record.oid],
            ),
        )
        for record, kind in records
    )
    note_slots = max(0, evidence_limit - len(records))
    note_payload_remaining = limits.max_output_bytes
    note_evidence: dict[int, Evidence] = {}
    warnings: list[str] = []
    ref_missing = not notes_available
    lookup_failed = False
    note_bound_reached = notes_available and bool(records) and note_slots == 0

    for index, (record, _primary) in enumerate(primary_evidence):
        if ref_missing:
            continue
        if len(note_evidence) >= note_slots or note_payload_remaining < 1:
            note_bound_reached = True
            break
        assert deadline is not None
        try:
            note_status = inspector.review_note_status(
                record.oid,
                timeout_seconds=_remaining_query_timeout(deadline),
                max_output_bytes=note_payload_remaining,
            )
        except ManduaError as error:
            if error.code is not ErrorCode.LIMIT_EXCEEDED:
                raise
            note_bound_reached = True
            break
        warnings.extend(warning for warning in note_status.warnings if warning not in warnings)
        if not note_status.ref_available:
            ref_missing = True
            continue
        if note_status.lookup_failed:
            lookup_failed = True
            continue
        if note_status.content is not None:
            content_size = len(note_status.content.encode("utf-8"))
            if content_size > note_payload_remaining:
                note_bound_reached = True
                break
            note_payload_remaining -= content_size
            note_evidence[index] = Evidence(
                id=f"review-note:{record.oid}",
                kind="review-note",
                oid=record.oid,
                ref=_truncate(inspector.notes_ref, limits),
                excerpt=_truncate(note_status.content, limits),
                details={"signature_verified": False},
            )

    if ref_missing:
        warnings.append(
            "Review notes are unavailable in this repository; a review may exist elsewhere. "
            "Fetch the configured notes ref explicitly to inspect it."
        )
    elif lookup_failed:
        warnings.append(
            "One or more review note lookups failed; review evidence may be incomplete."
        )
    if note_bound_reached:
        warnings.append(
            "Review note evidence was limited by the aggregate result, byte, or time bound."
        )
    evidence = tuple(
        item
        for index, (_record, primary) in enumerate(primary_evidence)
        for item in (
            primary,
            *((note_evidence[index],) if index in note_evidence else ()),
        )
    )
    return tuple(evidence), tuple(dict.fromkeys(warnings))


def _bounded_decision_result(result: MemoryResult, limits: QueryLimits) -> MemoryResult:
    """Keep the exact serialized decision result within the caller's byte budget."""
    if _result_size(result) <= limits.max_output_bytes:
        return result
    evidence = list(result.evidence)
    warning = "Review note evidence was limited by the aggregate serialized-result byte bound."
    warnings = tuple(dict.fromkeys((*result.warnings, warning)))
    for index in range(len(evidence) - 1, -1, -1):
        if evidence[index].kind != "review-note":
            continue
        del evidence[index]
        bounded = replace(result, evidence=tuple(evidence), warnings=warnings)
        if _result_size(bounded) <= limits.max_output_bytes:
            return bounded
    raise ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Decision evidence exceeded the configured serialized-result byte limit.",
    )


def _result_size(result: MemoryResult) -> int:
    return len(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))


def origin_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    text: str,
    *,
    path: PurePosixPath | None = None,
    limit: int | None = 100,
) -> MemoryResult:
    """Find exact added and removed content with bounded fixed-string pickaxe evidence."""
    _validate_origin_text(text, limits)
    if path is not None:
        inspector.validate_path(path)
    result_limit = _record_limit(limit, limits)
    records = _patch_records(
        inspector.patch_log(
            text,
            context_lines=_origin_context_lines(text),
            path=path,
            limit=result_limit + 1,
        )
    )
    selected = records[:result_limit]
    scan_truncated = len(records) > result_limit
    evidence = tuple(
        evidence
        for oid, patch in reversed(selected)
        for evidence in _origin_evidence(oid, patch, text, limits)
    )
    scope = _all_refs_scope(inspector, selected, scan_truncated)
    warnings = (
        ("Content history is limited by the configured commit bound.",) if scan_truncated else ()
    )
    bounded_text = _truncate(text, limits)
    return MemoryResult(
        operation="origin",
        answer=f"Content origin was searched for {bounded_text}.",
        observed=(Claim(f"Found {len(evidence)} exact content change(s)."),),
        evidence=evidence,
        history_scope=scope,
        gaps=() if evidence else (f"No exact content changes were found for {bounded_text}.",),
        warnings=warnings,
    )


def evolution_result(
    inspector: RepositoryInspector,
    limits: QueryLimits,
    path: PurePosixPath,
    *,
    limit: int | None = 100,
) -> MemoryResult:
    """Follow one validated path through bounded Git name-status history."""
    inspector.validate_path(path)
    result_limit = _record_limit(limit, limits)
    records = _name_status_records(inspector.follow_path_log(path, limit=result_limit + 1))
    selected = records[:result_limit]
    scan_truncated = len(records) > result_limit
    evidence = tuple(
        evidence
        for oid, statuses in reversed(selected)
        for evidence in _evolution_evidence(oid, statuses, limits)
    )
    baseline_scope = inspector.history_scope(inspector.resolve_commit("HEAD"))
    scope = inspector.bound_history_scope(
        replace(
            baseline_scope,
            start_oid=selected[-1][0] if selected else None,
            refs=("HEAD",),
            commit_count=len(selected),
            truncated=baseline_scope.truncated or scan_truncated,
        )
    )
    warnings: list[str] = []
    gaps: list[str] = []
    if scan_truncated:
        warnings.append("Path history is limited by the configured commit bound.")
    if any(item.details["status"] == "D" for item in evidence):
        gaps.append("A deletion is a path-history boundary; the path did not exist after it.")
    if not evidence:
        gaps.append(f"No commits were found for path {_truncate(path.as_posix(), limits)}.")
    return MemoryResult(
        operation="evolution",
        answer=f"Path evolution was read for {_truncate(path.as_posix(), limits)}.",
        observed=(Claim(f"Found {len(evidence)} path change(s)."),),
        evidence=evidence,
        history_scope=scope,
        gaps=tuple(gaps),
        warnings=tuple(warnings),
    )


def _commit_record(inspector: RepositoryInspector, oid: str) -> CommitRecord:
    records = inspector.log((oid,), limit=1)
    if not records or records[0].oid != oid:
        raise ManduaError(ErrorCode.MISSING_OBJECT, "The blamed commit record is unavailable.")
    return records[0]


def _patch_records(payload: bytes) -> tuple[tuple[str, bytes], ...]:
    records: list[tuple[str, bytes]] = []
    markers = tuple(_PATCH_RECORD.finditer(payload))
    for index, marker in enumerate(markers):
        next_start = markers[index + 1].start() if index + 1 < len(markers) else len(payload)
        records.append((marker.group(1).decode("ascii"), payload[marker.end() : next_start]))
    return tuple(records)


def _origin_evidence(
    oid: str, patch: bytes, text: str, limits: QueryLimits
) -> tuple[Evidence, ...]:
    expected = _origin_bytes(text)
    evidence: list[Evidence] = []
    for patch_file in _patch_files(patch):
        old_count = sum(bytes(hunk).count(expected) for hunk in patch_file.old_hunks)
        new_count = sum(bytes(hunk).count(expected) for hunk in patch_file.new_hunks)
        net_count = new_count - old_count
        if net_count == 0:
            continue
        if net_count > 0:
            evidence.append(
                _content_evidence(
                    "content-added",
                    oid,
                    patch_file.new_path,
                    text,
                    len(evidence),
                    old_count,
                    new_count,
                    limits,
                )
            )
        else:
            evidence.append(
                _content_evidence(
                    "content-removed",
                    oid,
                    patch_file.old_path,
                    text,
                    len(evidence),
                    old_count,
                    new_count,
                    limits,
                )
            )
    return tuple(evidence)


def _patch_files(patch: bytes) -> tuple[_PatchFile, ...]:
    files: list[_PatchFile] = []
    current: _PatchFile | None = None
    in_hunk = False
    last_hunks: tuple[bytearray, ...] = ()
    for line, ending in _patch_lines(patch):
        if line.startswith(b"diff --git "):
            if current is not None:
                files.append(current)
            current = _PatchFile()
            in_hunk = False
            last_hunks = ()
            continue
        if current is None:
            continue
        if not in_hunk and line.startswith(b"--- "):
            current.old_path = _patch_path(line[4:])
            continue
        if not in_hunk and line.startswith(b"+++ "):
            current.new_path = _patch_path(line[4:])
            continue
        if line.startswith(b"@@ "):
            in_hunk = True
            current.old_hunks.append(bytearray())
            current.new_hunks.append(bytearray())
            last_hunks = ()
            continue
        if not in_hunk:
            continue
        if line.startswith(b"+"):
            current.new_hunks[-1].extend(line[1:] + ending)
            last_hunks = (current.new_hunks[-1],)
        elif line.startswith(b"-"):
            current.old_hunks[-1].extend(line[1:] + ending)
            last_hunks = (current.old_hunks[-1],)
        elif line.startswith(b" "):
            current.old_hunks[-1].extend(line[1:] + ending)
            current.new_hunks[-1].extend(line[1:] + ending)
            last_hunks = (current.old_hunks[-1], current.new_hunks[-1])
        elif line == b"\\ No newline at end of file":
            for last_hunk in last_hunks:
                del last_hunk[-1:]
            last_hunks = ()
    if current is not None:
        files.append(current)
    return tuple(files)


def _patch_lines(payload: bytes) -> tuple[tuple[bytes, bytes], ...]:
    lines: list[tuple[bytes, bytes]] = []
    start = 0
    while start < len(payload):
        end = payload.find(b"\n", start)
        if end < 0:
            lines.append((payload[start:], b""))
            break
        lines.append((payload[start:end], b"\n"))
        start = end + 1
    return tuple(lines)


def _patch_path(value: bytes) -> bytes | None:
    raw_path = _unquote_patch_path(value)
    if raw_path == b"/dev/null":
        return None
    if raw_path.startswith((b"a/", b"b/")):
        raw_path = raw_path[2:]
    return raw_path


def _unquote_patch_path(value: bytes) -> bytes:
    if not value.startswith(b'"'):
        return value.split(b"\t", 1)[0]
    decoded = bytearray()
    index = 1
    escapes = {
        ord(b"a"): b"\a",
        ord(b"b"): b"\b",
        ord(b"f"): b"\f",
        ord(b"n"): b"\n",
        ord(b"r"): b"\r",
        ord(b"t"): b"\t",
        ord(b"v"): b"\v",
    }
    while index < len(value):
        character = value[index]
        index += 1
        if character == ord(b'"'):
            return bytes(decoded)
        if character != ord(b"\\") or index >= len(value):
            decoded.append(character)
            continue
        escaped = value[index]
        index += 1
        if ord(b"0") <= escaped <= ord(b"7"):
            digits = bytes([escaped])
            while index < len(value) and len(digits) < 3 and ord(b"0") <= value[index] <= ord(b"7"):
                digits += bytes([value[index]])
                index += 1
            decoded.append(int(digits, 8))
        elif escaped in escapes:
            decoded.extend(escapes[escaped])
        else:
            decoded.append(escaped)
    return value


def _content_evidence(
    kind: str,
    oid: str,
    path: bytes | None,
    text: str,
    index: int,
    old_count: int,
    new_count: int,
    limits: QueryLimits,
) -> Evidence:
    details: dict[str, object] = {
        "occurrence_count": abs(new_count - old_count),
        "net_hunk_occurrences": new_count - old_count,
        "old_hunk_occurrences": old_count,
        "new_hunk_occurrences": new_count,
        "signature_verified": False,
    }
    if path is not None:
        details.update(_path_identity_details("path", path, limits))
    return Evidence(
        id=f"{kind}:{oid}:{index}",
        kind=kind,
        oid=oid,
        path=None if path is None else _bounded_path(path, limits),
        excerpt=_truncate(text, limits),
        details=details,
    )


def _name_status_records(
    payload: bytes,
) -> tuple[tuple[str, tuple[tuple[str, bytes, bytes | None], ...]], ...]:
    records: list[tuple[str, tuple[tuple[str, bytes, bytes | None], ...]]] = []
    for oid, raw_statuses in _patch_records(payload):
        fields = raw_statuses.lstrip(b"\0\n").split(b"\0")
        statuses: list[tuple[str, bytes, bytes | None]] = []
        index = 0
        while index < len(fields):
            raw_status = fields[index]
            if not raw_status:
                break
            status = raw_status.decode("ascii", errors="replace")
            index += 1
            if status[0:1] in {"R", "C"}:
                if index + 1 >= len(fields):
                    break
                old_path = fields[index]
                new_path = fields[index + 1]
                statuses.append((status, old_path, new_path))
                index += 2
                continue
            if status[0:1] not in {"A", "M", "D", "T"} or index >= len(fields):
                break
            statuses.append((status, fields[index], None))
            index += 1
        records.append((oid, tuple(statuses)))
    return tuple(records)


def _evolution_evidence(
    oid: str, statuses: tuple[tuple[str, bytes, bytes | None], ...], limits: QueryLimits
) -> tuple[Evidence, ...]:
    evidence: list[Evidence] = []
    for index, (status, old_path, new_path) in enumerate(statuses):
        code = status[0]
        kind = {
            "A": "path-added",
            "M": "path-modified",
            "D": "path-deleted",
            "R": "path-renamed",
            "C": "path-copied",
            "T": "path-type-changed",
        }[code]
        details: dict[str, object] = {"status": status, "signature_verified": False}
        if new_path is not None:
            details.update(_path_details("old_path", old_path, limits))
            details.update(_path_details("new_path", new_path, limits))
        elif code == "A":
            details["old_path"] = None
            details["old_path_bytes"] = None
            details.update(_path_details("new_path", old_path, limits))
        elif code == "D":
            details.update(_path_details("old_path", old_path, limits))
            details["new_path"] = None
            details["new_path_bytes"] = None
        else:
            details.update(_path_details("old_path", old_path, limits))
            details.update(_path_details("new_path", old_path, limits))
        evidence.append(
            Evidence(
                id=f"{kind}:{oid}:{index}",
                kind=kind,
                oid=oid,
                path=_bounded_path(old_path, limits),
                details=details,
            )
        )
    return tuple(evidence)


def _all_refs_scope(
    inspector: RepositoryInspector,
    records: tuple[tuple[str, bytes], ...],
    scan_truncated: bool,
):
    baseline_scope = inspector.history_scope()
    return inspector.bound_history_scope(
        replace(
            baseline_scope,
            start_oid=records[-1][0] if records else None,
            end_oid=records[0][0] if records else None,
            refs=("--all",),
            commit_count=len(records),
            truncated=baseline_scope.truncated or scan_truncated,
        )
    )


def _origin_bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The origin text is invalid.") from None


def _origin_context_lines(text: str) -> int:
    return _origin_bytes(text).count(b"\n")


def _bounded_path(path: bytes, limits: QueryLimits) -> str:
    return _truncate(_render_path(path), limits)


def _path_details(name: str, path: bytes, limits: QueryLimits) -> dict[str, object]:
    return {name: _bounded_path(path, limits), **_path_identity_details(name, path, limits)}


def _path_identity_details(name: str, path: bytes, limits: QueryLimits) -> dict[str, object]:
    path_hex = path.hex()
    if len(path_hex) <= limits.max_excerpt_chars:
        return {f"{name}_bytes": path_hex, f"{name}_bytes_truncated": False}
    prefix_length = limits.max_excerpt_chars - (limits.max_excerpt_chars % 2)
    return {
        f"{name}_bytes_prefix": path_hex[:prefix_length],
        f"{name}_bytes_sha256": hashlib.sha256(path).hexdigest(),
        f"{name}_bytes_truncated": True,
    }


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


def _recorded_motivations(record: CommitRecord, limits: QueryLimits) -> tuple[tuple[str, str], ...]:
    reason = parse_recorded_reason(record.body, limits=limits)
    reasons = [reason] if reason is not None else []
    decisions = [value for value in _trailer_values(record, "Decision-ID") if value]
    return tuple(("reason", reason) for reason in dict.fromkeys(reasons)) + tuple(
        ("Decision-ID", decision) for decision in dict.fromkeys(decisions)
    )


def _commit_evidence(
    inspector: RepositoryInspector,
    record: CommitRecord,
    kind: str,
    limits: QueryLimits,
    *,
    verified_corrects: tuple[str, ...] = (),
) -> Evidence:
    details: dict[str, object] = {
        "parents": record.parents,
        "author_time": record.author_time,
        "changed_paths": tuple(_truncate(path, limits) for path in record.paths),
    }
    for key in ("Memory-Type", "Scope", "Task-ID", "Decision-ID", "Agent-ID", "Corrects"):
        values = _trailer_values(record, key)
        if values:
            details[key.lower().replace("-", "_")] = tuple(
                _truncate(value, limits) for value in values
            )
    details["signature_verified"] = (
        inspector.signature_status(
            record.oid,
            timeout_seconds=inspector.remaining_operation_timeout(),
        )
        if _trailer_values(record, "Agent-ID")
        else False
    )
    if verified_corrects:
        details["verified_corrects"] = tuple(
            _truncate(value, limits) for value in verified_corrects
        )
    return Evidence(
        id=f"{kind}:{record.oid}",
        kind=kind,
        oid=record.oid,
        excerpt=_truncate(record.subject, limits),
        details=details,
    )


def _metadata_evidence(
    record: CommitRecord, kind: str, value: str, index: int, limits: QueryLimits
) -> Evidence:
    field = "Reason" if kind == "reason" else kind
    return Evidence(
        id=f"commit-metadata:{record.oid}:{field}:{index}",
        kind="commit-metadata",
        oid=record.oid,
        excerpt=_truncate(f"{field}: {value}", limits),
        details={"field": field, "value": _truncate(value, limits)},
    )


def _causal_corrections(
    inspector: RepositoryInspector,
    records: tuple[CommitRecord, ...],
    selected: tuple[CommitRecord, ...],
    limits: QueryLimits,
    result_limit: int,
) -> tuple[tuple[CommitRecord, ...], dict[str, tuple[str, ...]], tuple[str, ...], tuple[str, ...]]:
    selected_oids = {record.oid for record in selected}
    selected_pairs: list[tuple[str, CommitRecord]] = []
    external_pairs: list[tuple[str, CommitRecord]] = []
    seen_pairs: set[tuple[str, str]] = set()
    warnings: list[str] = []
    for record in records:
        record_is_selected = record.oid in selected_oids
        for value in _trailer_values(record, "Corrects"):
            corrected_oid = _validated_corrects_oid(value)
            if record_is_selected and (
                corrected_oid is None
                or corrected_oid not in selected_oids
                or corrected_oid == record.oid
            ):
                continue
            if corrected_oid is None:
                warnings.append("An invalid Corrects link was omitted.")
                continue
            pair = (corrected_oid, record.oid)
            if corrected_oid in selected_oids and pair not in seen_pairs:
                seen_pairs.add(pair)
                target = selected_pairs if record_is_selected else external_pairs
                target.append((corrected_oid, record))

    selected_first, selected_deferred = _first_correction_pairs(selected_pairs)
    external_first, external_deferred = _first_correction_pairs(external_pairs)
    pairs = (
        *selected_first,
        *external_first,
        *selected_deferred,
        *external_deferred,
    )
    selected_candidate_count = len(selected_first)
    max_checks = min(
        len(pairs),
        limits.max_commits,
        max(0, result_limit - len(selected)) + selected_candidate_count,
    )
    deadline = time.monotonic() + limits.timeout_seconds
    causal_oids: set[str] = set()
    verified_pairs: dict[str, list[str]] = {}
    skipped = False
    for checked, (corrected_oid, record) in enumerate(pairs):
        remaining_seconds = deadline - time.monotonic()
        if checked >= max_checks or remaining_seconds <= 0:
            skipped = True
            break
        try:
            causal = inspector.is_ancestor_oids(
                corrected_oid,
                record.oid,
                timeout_seconds=remaining_seconds,
            )
        except ManduaError as error:
            if error.code is not ErrorCode.LIMIT_EXCEEDED:
                raise
            skipped = True
            break
        if causal:
            causal_oids.add(record.oid)
            verified_pairs.setdefault(record.oid, []).append(corrected_oid)
        else:
            warnings.append("A Corrects link was not causally later and was omitted.")
    gaps: list[str] = []
    if skipped:
        warnings.append(
            "Some Corrects links were not checked because the ancestry-check budget was exhausted."
        )
        gaps.append(
            "Some Corrects links could not be verified as causal within the ancestry-check budget."
        )
    corrections = tuple(record for record in records if record.oid in causal_oids)
    return (
        tuple(corrections),
        {oid: tuple(values) for oid, values in verified_pairs.items()},
        tuple(dict.fromkeys(warnings)),
        tuple(gaps),
    )


def _first_correction_pairs(
    pairs: list[tuple[str, CommitRecord]],
) -> tuple[tuple[tuple[str, CommitRecord], ...], tuple[tuple[str, CommitRecord], ...]]:
    """Schedule each correction record's first pair before its remaining pairs."""
    first: list[tuple[str, CommitRecord]] = []
    deferred: list[tuple[str, CommitRecord]] = []
    seen_records: set[str] = set()
    for pair in pairs:
        record_oid = pair[1].oid
        target = deferred if record_oid in seen_records else first
        target.append(pair)
        seen_records.add(record_oid)
    return tuple(first), tuple(deferred)


def _validated_corrects_oid(value: str) -> str | None:
    if not _FULL_OBJECT_ID.fullmatch(value):
        return None
    return value.lower()


def _trailer_values(record: CommitRecord, key: str) -> tuple[str, ...]:
    return tuple(value for trailer_key, value in record.trailers if trailer_key == key)


def _validate_decision_id(value: str, limits: QueryLimits) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value) > limits.max_input_chars
    ):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The decision ID is invalid.")


def _validate_origin_text(value: str, limits: QueryLimits) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The origin text is invalid.")
    if len(value) > limits.max_input_chars:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The origin text exceeds the input limit.")
    _origin_bytes(value)


def _record_limit(limit: int | None, limits: QueryLimits) -> int:
    if limit is None:
        return limits.max_commits
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit limit must be positive.")
    return min(limit, limits.max_commits)


def _line_evidence_id(oid: str, path: PurePosixPath, line: int) -> str:
    digest = hashlib.sha256(path.as_posix().encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"line-blame:{oid}:{digest}:{line}"


def _truncate(value: str, limits: QueryLimits) -> str:
    return bound_text(value, limits.max_excerpt_chars)


def _remaining_query_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
        raise ManduaError(
            ErrorCode.LIMIT_EXCEEDED,
            "Review-note lookup exceeded the configured time limit.",
        )
    return remaining
