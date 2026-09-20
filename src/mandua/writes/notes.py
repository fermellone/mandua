"""Attach bounded review metamemory through one configured Git notes ref."""

from __future__ import annotations

import math
import re
import secrets
import time
import unicodedata
from bisect import bisect_left
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path

from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import (
    GitIndexFile,
    GitOperationBudget,
    GitOutput,
    GitRepositoryAuthority,
    GitRunner,
    _RepositoryAuthorityLayout,
)
from mandua.models import (
    AnnotationRequest,
    Claim,
    Evidence,
    HistoryScope,
    MemoryResult,
    PlannedChange,
    QueryLimits,
)
from mandua.policy import Policy

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_EMERGENCY_RECONCILIATION_SECONDS = 1.0
_MAX_PUBLICATION_ATTEMPTS = 4
_NOTE_COMMIT_MESSAGE = b"Record Mandu'a review annotation\n\nTransaction-ID: "
_NOTE_MERGE_COMMIT_MESSAGE = b"Repair Mandu'a review annotation\n\nTransaction-ID: "


@dataclass(frozen=True, slots=True)
class _PreparedAnnotation:
    request: AnnotationRequest
    target_oid: str
    notes_ref: str
    notes_ref_oid: str | None
    note_path: bytes
    existing_note: bytes | None
    note_body: bytes


@dataclass(frozen=True, slots=True)
class _NoteTreeEntry:
    mode: bytes
    object_type: bytes
    object_id: str
    path: bytes
    logical_oid: bytes | None


@dataclass(slots=True)
class _MergeBudget:
    remaining: int

    def output_limit(self) -> int:
        if self.remaining < 1:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Review-note tree repair exceeded the configured byte limit.",
            )
        return self.remaining

    def consume(self, size: int) -> None:
        if size < 0 or size > self.remaining:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Review-note tree repair exceeded the configured byte limit.",
            )
        self.remaining -= size


class NoteWriter:
    """Preview and apply one review note without changing repository content state."""

    def __init__(self, repository: Path, limits: QueryLimits) -> None:
        self._repository = Path(repository)
        self._limits = limits
        self._runner = GitRunner(self._repository, limits=limits)
        self._deadline = 0.0
        self._object_width: int | None = None
        self._repository_authority: GitRepositoryAuthority | None = None
        self._repository_layout: _RepositoryAuthorityLayout | None = None

    def annotate(self, request: AnnotationRequest, *, apply: bool = False) -> MemoryResult:
        """Return a non-mutating preview or append the requested review annotation."""
        self._deadline = time.monotonic() + self._validated_timeout()
        self._validate_request(request, apply)
        first = self._prepare(request)
        if not apply:
            return self._result(first, after_oid=None, applied=False)

        authority = self._runner.open_repository_authority(None)
        self._repository_authority = authority
        self._repository_layout = authority.layout
        try:
            second = self._prepare(request)
            self._require_same_snapshot(first, second)
            after_oid, warning = self._append(second)
        except BaseException as operation_error:
            self._repository_authority = None
            self._close_repository_authority(authority, original=operation_error)
            raise
        self._repository_authority = None
        try:
            authority.cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup follows publication
            after_oid, _ = self._reconcile_interrupted_publication(
                second,
                after_oid,
                cleanup_error,
            )
            warning = (
                "The annotation was applied, but private Git authority cleanup was uncertain; "
                f"inspect {second.notes_ref} before another annotation."
            )
        return self._result(
            second,
            after_oid=after_oid,
            applied=True,
            warnings=(warning,) if warning is not None else (),
        )

    def _prepare(self, request: AnnotationRequest) -> _PreparedAnnotation:
        policy = Policy.open(
            self._repository,
            limits=replace(self._limits, timeout_seconds=self._remaining_timeout()),
            runner=self._runner,
            repository_authority=self._repository_authority,
        )
        self._remaining_timeout()
        notes_ref = policy.repository_policy.notes_ref
        target_oid = self._resolve_target(request.revision)
        notes_ref_oid = self._read_notes_ref(notes_ref, deadline=self._deadline)
        existing_note, note_path = (
            (None, target_oid.encode("ascii"))
            if notes_ref_oid is None
            else self._read_note_snapshot(notes_ref_oid, target_oid, deadline=self._deadline)
        )
        note_body = self._note_body(request)
        existing_size = len(existing_note) if existing_note is not None else 0
        separator_size = 1 if existing_note else 0
        if existing_size + len(note_body) + separator_size > self._limits.max_output_bytes:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The appended review note would exceed the configured byte limit.",
            )
        self._remaining_timeout()
        return _PreparedAnnotation(
            request=request,
            target_oid=target_oid,
            notes_ref=notes_ref,
            notes_ref_oid=notes_ref_oid,
            note_path=note_path,
            existing_note=existing_note,
            note_body=note_body,
        )

    def _append(self, prepared: _PreparedAnnotation) -> tuple[str, str | None]:
        current = prepared
        candidate_oid = self._build_candidate(current)
        for attempt in range(_MAX_PUBLICATION_ATTEMPTS):
            published = self._publish_candidate(current, candidate_oid)
            if published is not None:
                return published
            if attempt + 1 == _MAX_PUBLICATION_ATTEMPTS:
                raise self._race_error(current)
            refreshed = self._prepare(current.request)
            self._require_same_annotation(current, refreshed)
            if refreshed.notes_ref_oid is not None and refreshed.notes_ref_oid != candidate_oid:
                candidate_oid = self._merge_candidate(candidate_oid, refreshed)
            current = refreshed
        raise self._race_error(current)

    def _build_candidate(self, prepared: _PreparedAnnotation) -> str:
        note_contents = (
            prepared.note_body
            if not prepared.existing_note
            else prepared.existing_note + b"\n" + prepared.note_body
        )
        if len(note_contents) > self._limits.max_output_bytes:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The appended review note would exceed the configured byte limit.",
            )
        blob = self._run_text(
            ["hash-object", "-w", "--stdin"],
            input_bytes=note_contents,
            timeout_seconds=self._remaining_timeout(),
        )
        blob_oid = self._strict_oid(blob, "Git returned an invalid review-note blob ID.")
        with self._runner.temporary_index() as index_file:
            seed = self._run_index(
                (
                    ["read-tree", prepared.notes_ref_oid]
                    if prepared.notes_ref_oid is not None
                    else ["read-tree", "--empty"]
                ),
                index_file=index_file,
            )
            self._require_silent_success(seed, "Git returned invalid isolated review-tree output.")
            update = self._run_index(
                ["update-index", "-z", "--index-info"],
                index_file=index_file,
                input_bytes=(
                    b"100644 " + blob_oid.encode("ascii") + b"\t" + prepared.note_path + b"\x00"
                ),
            )
            self._require_silent_success(
                update, "Git returned invalid isolated review-index output."
            )
            tree = self._run_index(["write-tree"], index_file=index_file)
            tree_oid = self._strict_oid(tree, "Git returned an invalid isolated review-tree ID.")

        transaction_id = secrets.token_hex(16).encode("ascii")
        arguments = ["commit-tree", tree_oid]
        if prepared.notes_ref_oid is not None:
            arguments.extend(("-p", prepared.notes_ref_oid))
        arguments.extend(("-F", "-"))
        commit = self._run_text(
            arguments,
            input_bytes=_NOTE_COMMIT_MESSAGE + transaction_id + b"\n",
            timeout_seconds=self._remaining_timeout(),
        )
        return self._strict_oid(commit, "Git returned an invalid review-note commit ID.")

    def _merge_candidate(self, candidate_oid: str, current: _PreparedAnnotation) -> str:
        current_oid = current.notes_ref_oid
        if current_oid is None or current_oid == candidate_oid:
            return candidate_oid
        budget = _MergeBudget(self._limits.max_output_bytes)
        contents: dict[str, bytes] = {}
        base_oid = self._merge_base(candidate_oid, current_oid)
        candidate_entries = self._normalized_note_tree(
            candidate_oid, budget=budget, contents=contents, prepared=current
        )
        current_entries = self._normalized_note_tree(
            current_oid, budget=budget, contents=contents, prepared=current
        )
        if base_oid is None:
            base_entries = {}
        elif base_oid == candidate_oid:
            base_entries = candidate_entries
        elif base_oid == current_oid:
            base_entries = current_entries
        else:
            base_entries = self._normalized_note_tree(
                base_oid, budget=budget, contents=contents, prepared=current
            )
        merged = self._merge_note_entries(
            candidate_entries,
            current_entries,
            base_entries,
            budget=budget,
            contents=contents,
            prepared=current,
        )
        tree_oid = self._write_merged_tree(merged, prepared=current)
        transaction_id = secrets.token_hex(16).encode("ascii")
        commit = self._run_text(
            ["commit-tree", tree_oid, "-p", current_oid, "-p", candidate_oid, "-F", "-"],
            input_bytes=_NOTE_MERGE_COMMIT_MESSAGE + transaction_id + b"\n",
            timeout_seconds=self._remaining_timeout(),
        )
        return self._strict_oid(commit, "Git returned an invalid repaired review-note commit ID.")

    def _merge_base(self, candidate_oid: str, current_oid: str) -> str | None:
        output = self._run_text(
            ["merge-base", "--all", "--end-of-options", candidate_oid, current_oid],
            check=False,
            max_output_bytes=min(self._limits.max_output_bytes, (self._object_id_width() + 1) * 2),
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode == 1 and not output.stdout and not output.stderr:
            return None
        if output.returncode != 0 or output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git could not identify a review-note merge base.",
            )
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The review-note histories have an ambiguous merge base.",
            )
        object_id = lines[0]
        if _OBJECT_ID.fullmatch(object_id) is None or len(object_id) != self._object_id_width():
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git returned an invalid review-note merge base.",
            )
        return object_id

    def _normalized_note_tree(
        self,
        commit_oid: str,
        *,
        budget: _MergeBudget,
        contents: dict[str, bytes],
        prepared: _PreparedAnnotation,
    ) -> dict[tuple[bool, bytes], _NoteTreeEntry]:
        entries = self._read_merge_tree(commit_oid, budget=budget)
        grouped: dict[tuple[bool, bytes], list[_NoteTreeEntry]] = {}
        for entry in entries:
            key = (
                (True, entry.logical_oid) if entry.logical_oid is not None else (False, entry.path)
            )
            grouped.setdefault(key, []).append(entry)

        normalized: dict[tuple[bool, bytes], _NoteTreeEntry] = {}
        for key, group in grouped.items():
            ordered = sorted(group, key=lambda entry: entry.path)
            if key[0]:
                if any(
                    entry.mode != b"100644" or entry.object_type != b"blob" for entry in ordered
                ):
                    raise self._unmergeable_note_tree(prepared)
                path = self._preferred_note_path(tuple(entry.path for entry in ordered))
                distinct_oids = tuple(dict.fromkeys(entry.object_id for entry in ordered))
                if len(distinct_oids) == 1:
                    normalized[key] = replace(ordered[0], path=path)
                    continue
                combined: bytes | None = None
                seen_contents: set[bytes] = set()
                for entry in ordered:
                    note = self._read_merge_blob(entry, budget=budget, contents=contents)
                    if note in seen_contents:
                        continue
                    seen_contents.add(note)
                    combined = note if combined is None else self._join_note_bytes(combined, note)
                assert combined is not None
                normalized[key] = self._write_merged_blob(combined, path=path)
                continue

            if len(ordered) != 1:
                raise self._unmergeable_note_tree(prepared)
            normalized[key] = ordered[0]
        return normalized

    def _read_merge_tree(
        self, commit_oid: str, *, budget: _MergeBudget
    ) -> tuple[_NoteTreeEntry, ...]:
        return self._read_validated_note_tree(
            commit_oid,
            budget=budget,
            deadline=self._deadline,
        )

    def _read_validated_note_tree(
        self,
        commit_oid: str,
        *,
        budget: _MergeBudget,
        deadline: float,
    ) -> tuple[_NoteTreeEntry, ...]:
        output = self._run(
            ["ls-tree", "-r", "-t", "-z", "--full-tree", commit_oid],
            check=False,
            max_output_bytes=budget.output_limit(),
            timeout_seconds=self._remaining(deadline),
        )
        budget.consume(len(output.stdout) + len(output.stderr))
        if (
            output.returncode != 0
            or output.stderr
            or (output.stdout and not output.stdout.endswith(b"\x00"))
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid review-note tree.")
        parsed: list[_NoteTreeEntry] = []
        records = () if not output.stdout else output.stdout[:-1].split(b"\x00")
        for record in records:
            metadata, separator, path = record.partition(b"\t")
            fields = metadata.split(b" ")
            if (
                not separator
                or not path
                or path.startswith(b"/")
                or path.endswith(b"/")
                or b"//" in path
                or len(fields) != 3
                or fields[0] not in {b"040000", b"100644", b"100755", b"120000", b"160000"}
                or fields[1]
                != (
                    b"tree"
                    if fields[0] == b"040000"
                    else b"commit"
                    if fields[0] == b"160000"
                    else b"blob"
                )
                or self._validated_oid_bytes(fields[2]) is None
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an invalid review-note tree."
                )
            flattened = path.replace(b"/", b"")
            logical_oid = flattened if self._validated_oid_bytes(flattened) is not None else None
            parsed.append(
                _NoteTreeEntry(
                    mode=fields[0],
                    object_type=fields[1],
                    object_id=fields[2].decode("ascii"),
                    path=path,
                    logical_oid=logical_oid,
                )
            )
        validated = self._validate_note_tree_objects(
            tuple(parsed),
            budget=budget,
            deadline=deadline,
        )
        return self._representable_note_tree_entries(validated)

    def _validate_note_tree_objects(
        self,
        entries: tuple[_NoteTreeEntry, ...],
        *,
        budget: _MergeBudget,
        deadline: float,
    ) -> tuple[_NoteTreeEntry, ...]:
        object_ids = tuple(dict.fromkeys(entry.object_id for entry in entries))
        if not object_ids:
            return entries
        output = self._run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            check=False,
            input_bytes=b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids),
            max_output_bytes=budget.output_limit(),
            timeout_seconds=self._remaining(deadline),
        )
        budget.consume(len(output.stdout) + len(output.stderr))
        if output.returncode != 0 or output.stderr or not output.stdout.endswith(b"\n"):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git returned an invalid review-note object probe.",
            )
        records = output.stdout[:-1].split(b"\n")
        if len(records) != len(object_ids):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git returned an invalid review-note object probe.",
            )

        actual_types: dict[str, bytes] = {}
        for expected_oid, record in zip(object_ids, records, strict=True):
            returned_oid, separator, actual_type = record.partition(b" ")
            if (
                not separator
                or returned_oid != expected_oid.encode("ascii")
                or actual_type not in {b"blob", b"commit", b"tree", b"tag", b"missing"}
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git returned an invalid review-note object probe.",
                )
            actual_types[expected_oid] = actual_type

        validated: list[_NoteTreeEntry] = []
        for entry in entries:
            actual_type = actual_types[entry.object_id]
            if entry.mode == b"040000":
                valid = actual_type == b"tree"
            elif entry.mode == b"160000":
                valid = actual_type in {b"commit", b"missing"}
            else:
                valid = actual_type == b"blob"
            if not valid:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "The review-note tree references an invalid object type.",
                )
            validated.append(replace(entry, object_type=actual_type))
        return tuple(validated)

    @staticmethod
    def _representable_note_tree_entries(
        entries: tuple[_NoteTreeEntry, ...],
    ) -> tuple[_NoteTreeEntry, ...]:
        leaves = tuple(entry for entry in entries if entry.mode != b"040000")
        leaf_paths = sorted(entry.path for entry in leaves)
        for tree in (entry for entry in entries if entry.mode == b"040000"):
            prefix = tree.path + b"/"
            position = bisect_left(leaf_paths, prefix)
            if position == len(leaf_paths) or not leaf_paths[position].startswith(prefix):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "The review-note tree contains an unsupported empty subtree.",
                    recovery=(
                        "Remove explicit empty subtrees from the configured review notes ref "
                        "before retrying."
                    ),
                )
        return leaves

    def _merge_note_entries(
        self,
        candidate: dict[tuple[bool, bytes], _NoteTreeEntry],
        current: dict[tuple[bool, bytes], _NoteTreeEntry],
        base: dict[tuple[bool, bytes], _NoteTreeEntry],
        *,
        budget: _MergeBudget,
        contents: dict[str, bytes],
        prepared: _PreparedAnnotation,
    ) -> tuple[_NoteTreeEntry, ...]:
        merged: list[_NoteTreeEntry] = []
        for key in sorted(candidate.keys() | current.keys()):
            candidate_entry = candidate.get(key)
            current_entry = current.get(key)
            if candidate_entry is None:
                assert current_entry is not None
                merged.append(current_entry)
                continue
            if current_entry is None:
                merged.append(candidate_entry)
                continue
            if self._same_entry(candidate_entry, current_entry):
                path = (
                    self._preferred_note_path((candidate_entry.path, current_entry.path))
                    if key[0]
                    else candidate_entry.path
                )
                merged.append(replace(candidate_entry, path=path))
                continue
            base_entry = base.get(key)
            if not key[0]:
                if base_entry is not None and self._same_entry(candidate_entry, base_entry):
                    merged.append(current_entry)
                    continue
                if base_entry is not None and self._same_entry(current_entry, base_entry):
                    merged.append(candidate_entry)
                    continue
                raise self._unmergeable_note_tree(prepared)
            if (
                candidate_entry.mode != current_entry.mode
                or candidate_entry.object_type != current_entry.object_type
            ):
                raise self._unmergeable_note_tree(prepared)
            candidate_note = self._read_merge_blob(
                candidate_entry, budget=budget, contents=contents
            )
            current_note = self._read_merge_blob(current_entry, budget=budget, contents=contents)
            base_note = (
                None
                if base_entry is None
                else self._read_merge_blob(base_entry, budget=budget, contents=contents)
            )
            note = self._combine_note_bytes(candidate_note, current_note, base_note)
            path = self._preferred_note_path((candidate_entry.path, current_entry.path))
            merged.append(self._write_merged_blob(note, path=path))
        self._validate_merged_paths(tuple(entry.path for entry in merged), prepared=prepared)
        return tuple(merged)

    def _read_merge_blob(
        self,
        entry: _NoteTreeEntry,
        *,
        budget: _MergeBudget,
        contents: dict[str, bytes],
    ) -> bytes:
        if entry.mode != b"100644" or entry.object_type != b"blob":
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "A review-note blob could not be merged safely.",
            )
        cached = contents.get(entry.object_id)
        if cached is not None:
            return cached
        output = self._run(
            ["cat-file", "blob", entry.object_id],
            check=False,
            max_output_bytes=budget.output_limit(),
            timeout_seconds=self._remaining_timeout(),
        )
        budget.consume(len(output.stdout) + len(output.stderr))
        if output.returncode != 0 or output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git could not inspect a review-note blob for repair.",
            )
        contents[entry.object_id] = output.stdout
        return output.stdout

    def _write_merged_blob(self, contents: bytes, *, path: bytes) -> _NoteTreeEntry:
        if len(contents) > self._limits.max_output_bytes:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "A repaired review note would exceed the configured byte limit.",
            )
        output = self._run_text(
            ["hash-object", "-w", "--stdin"],
            input_bytes=contents,
            timeout_seconds=self._remaining_timeout(),
        )
        object_id = self._strict_oid(
            output, "Git returned an invalid repaired review-note blob ID."
        )
        return _NoteTreeEntry(
            mode=b"100644",
            object_type=b"blob",
            object_id=object_id,
            path=path,
            logical_oid=path.replace(b"/", b""),
        )

    def _write_merged_tree(
        self, entries: tuple[_NoteTreeEntry, ...], *, prepared: _PreparedAnnotation
    ) -> str:
        index_info = b"".join(
            entry.mode + b" " + entry.object_id.encode("ascii") + b"\t" + entry.path + b"\x00"
            for entry in sorted(entries, key=lambda item: item.path)
        )
        if len(index_info) > self._limits.max_output_bytes:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Review-note tree repair exceeded the configured byte limit.",
            )
        with self._runner.temporary_index() as index_file:
            seed = self._run_index(["read-tree", "--empty"], index_file=index_file)
            self._require_silent_success(seed, "Git returned invalid repaired review-tree output.")
            if index_info:
                update = self._run_index(
                    ["update-index", "-z", "--index-info"],
                    index_file=index_file,
                    input_bytes=index_info,
                )
                if update.returncode != 0 or update.stdout or update.stderr:
                    raise self._unmergeable_note_tree(prepared)
            tree = self._run_index(["write-tree"], index_file=index_file)
            return self._strict_oid(tree, "Git returned an invalid repaired review-tree ID.")

    @staticmethod
    def _same_entry(left: _NoteTreeEntry, right: _NoteTreeEntry) -> bool:
        return (
            left.mode == right.mode
            and left.object_type == right.object_type
            and left.object_id == right.object_id
        )

    @staticmethod
    def _preferred_note_path(paths: tuple[bytes, ...]) -> bytes:
        return min(paths, key=lambda path: (-path.count(b"/"), path))

    @staticmethod
    def _join_note_bytes(left: bytes, right: bytes) -> bytes:
        return left + (b"\n" if left and right else b"") + right

    @classmethod
    def _combine_note_bytes(cls, candidate: bytes, current: bytes, base: bytes | None) -> bytes:
        if candidate == current:
            return candidate
        if base is not None:
            if candidate == base:
                return current
            if current == base:
                return candidate
            candidate_suffix = cls._appended_note_suffix(base, candidate)
            current_suffix = cls._appended_note_suffix(base, current)
            if candidate_suffix is not None and current_suffix is not None:
                combined = base
                if candidate_suffix:
                    combined = cls._join_note_bytes(combined, candidate_suffix)
                if current_suffix:
                    combined = cls._join_note_bytes(combined, current_suffix)
                return combined
        return cls._join_note_bytes(candidate, current)

    @staticmethod
    def _appended_note_suffix(base: bytes, changed: bytes) -> bytes | None:
        if not base:
            return changed
        prefix = base + b"\n"
        return changed[len(prefix) :] if changed.startswith(prefix) else None

    @staticmethod
    def _validate_merged_paths(paths: tuple[bytes, ...], *, prepared: _PreparedAnnotation) -> None:
        ordered = sorted(paths)
        for previous, current in pairwise(ordered):
            if previous == current or current.startswith(previous + b"/"):
                raise NoteWriter._unmergeable_note_tree(prepared)

    @staticmethod
    def _unmergeable_note_tree(prepared: _PreparedAnnotation) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "Concurrent review-note trees could not be merged without losing data.",
            recovery=f"Inspect {prepared.notes_ref} and resolve the note-tree conflict before retrying.",
        )

    def _read_note_snapshot(
        self, notes_ref_oid: str, target_oid: str, *, deadline: float
    ) -> tuple[bytes | None, bytes]:
        target = target_oid.encode("ascii")
        budget = _MergeBudget(self._limits.max_output_bytes)
        entries = self._read_validated_note_tree(
            notes_ref_oid,
            budget=budget,
            deadline=deadline,
        )
        matches: list[_NoteTreeEntry] = []
        for entry in entries:
            if entry.logical_oid == target:
                if entry.mode != b"100644" or entry.object_type != b"blob":
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "The target review-note entry is invalid."
                    )
                matches.append(entry)
        if len(matches) > 1:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The review-note snapshot is inconsistent.")
        if not matches:
            return None, target
        match = matches[0]
        note = self._run(
            ["cat-file", "blob", match.object_id],
            check=False,
            max_output_bytes=budget.output_limit(),
            timeout_seconds=self._remaining(deadline),
        )
        budget.consume(len(note.stdout) + len(note.stderr))
        if note.returncode != 0 or note.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git could not inspect the target review note."
            )
        return note.stdout, match.path

    def _publish_candidate(
        self, prepared: _PreparedAnnotation, candidate_oid: str
    ) -> tuple[str, str | None] | None:
        expected = prepared.notes_ref_oid or "0" * self._object_id_width()
        try:
            output = self._run_text(
                [
                    "update-ref",
                    "--no-deref",
                    prepared.notes_ref,
                    candidate_oid,
                    expected,
                ],
                check=False,
                timeout_seconds=self._remaining_timeout(),
            )
        except BaseException as failure:  # noqa: BLE001 - mutation may precede any exception
            return self._reconcile_interrupted_publication(prepared, candidate_oid, failure)

        if output.returncode == 0 and not output.stdout and not output.stderr:
            try:
                return self._verify_publication(prepared, candidate_oid)
            except BaseException as failure:  # noqa: BLE001 - publication needs classification
                return self._reconcile_interrupted_publication(prepared, candidate_oid, failure)
        if output.returncode != 0:
            try:
                return self._classify_rejected_publication(prepared)
            except BaseException as failure:  # noqa: BLE001 - publication needs classification
                return self._reconcile_interrupted_publication(prepared, candidate_oid, failure)
        failure = ManduaError(
            ErrorCode.GIT_FAILURE,
            "Git returned an invalid review-note ref-update result.",
        )
        return self._reconcile_interrupted_publication(prepared, candidate_oid, failure)

    def _verify_publication(
        self, prepared: _PreparedAnnotation, candidate_oid: str
    ) -> tuple[str, str | None] | None:
        after_oid = self._read_notes_ref(prepared.notes_ref, deadline=self._deadline)
        if after_oid is not None and self._candidate_is_ancestor(
            candidate_oid, after_oid, deadline=self._deadline
        ):
            return after_oid, None
        return None

    def _classify_rejected_publication(
        self, prepared: _PreparedAnnotation
    ) -> tuple[str, str | None] | None:
        after_oid = self._read_notes_ref(prepared.notes_ref, deadline=self._deadline)
        if after_oid == prepared.notes_ref_oid:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not update the review notes ref.")
        return None

    def _reconcile_interrupted_publication(
        self,
        prepared: _PreparedAnnotation,
        candidate_oid: str,
        failure: BaseException,
    ) -> tuple[str, str | None]:
        interruption: BaseException | None = None
        for _ in range(2):
            try:
                with self._emergency_publication_resources(prepared, candidate_oid) as deadline:
                    published_oid = self._classify_interrupted_publication(
                        prepared,
                        candidate_oid,
                        deadline=deadline,
                    )
            except BaseException as error:  # noqa: BLE001 - emergency control is classified
                if error is failure:
                    if interruption is not None:
                        raise self._uncertain_mutation_error(prepared) from failure
                    interruption = error
                    continue
                if isinstance(error, ManduaError):
                    uncertainty = self._uncertain_mutation_error(prepared)
                    if interruption is not None:
                        raise uncertainty from interruption
                    raise uncertainty from failure
                if interruption is not None:
                    raise self._uncertain_mutation_error(prepared) from interruption
                interruption = error
                continue
            if interruption is not None:
                raise interruption
            if published_oid is None:
                raise failure
            if not isinstance(failure, ManduaError):
                raise failure
            return (
                published_oid,
                "The annotation was reconciled after an interrupted Git result.",
            )
        raise self._uncertain_mutation_error(prepared) from interruption

    @contextmanager
    def _emergency_publication_resources(
        self,
        prepared: _PreparedAnnotation,
        candidate_oid: str,
    ) -> Iterator[float]:
        timeout = min(float(self._limits.timeout_seconds), _EMERGENCY_RECONCILIATION_SECONDS)
        limits = replace(self._limits, timeout_seconds=timeout)
        if self._repository_layout is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The captured review-note repository authority is unavailable.",
            )
        object_width = self._object_id_width()
        private_limits = self._runner.plan_repository_reopen_budget(
            self._repository_layout,
            (
                (
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "--end-of-options",
                    prepared.notes_ref,
                ),
                ("cat-file", "-t", candidate_oid),
                (
                    "merge-base",
                    "--is-ancestor",
                    "--end-of-options",
                    candidate_oid,
                    candidate_oid,
                ),
            ),
            expected_output_bytes=(object_width + 1) + len("commit\n"),
        )
        budget = GitOperationBudget(limits, budget_limits=private_limits)
        runner = GitRunner(
            self._repository,
            limits=limits,
            operation_budget=budget,
            _budget_limits=private_limits,
        )
        authority = runner.open_repository_authority(
            None,
            expected_layout=self._repository_layout,
        )
        original_runner = self._runner
        original_authority = self._repository_authority
        self._runner = runner
        self._repository_authority = authority
        try:
            yield budget.deadline
        finally:
            try:
                authority.cleanup()
            finally:
                self._runner = original_runner
                self._repository_authority = original_authority

    def _classify_interrupted_publication(
        self,
        prepared: _PreparedAnnotation,
        candidate_oid: str,
        *,
        deadline: float,
    ) -> str | None:
        after_oid = self._read_notes_ref(prepared.notes_ref, deadline=deadline)
        if after_oid == prepared.notes_ref_oid:
            return None
        if after_oid is not None and self._candidate_is_ancestor(
            candidate_oid, after_oid, deadline=deadline
        ):
            return after_oid
        raise self._uncertain_mutation_error(prepared)

    def _candidate_is_ancestor(
        self, candidate_oid: str, after_oid: str, *, deadline: float
    ) -> bool:
        if candidate_oid == after_oid:
            return True
        output = self._run_text(
            ["merge-base", "--is-ancestor", "--end-of-options", candidate_oid, after_oid],
            check=False,
            timeout_seconds=self._remaining(deadline),
        )
        if output.returncode in {0, 1} and not output.stdout and not output.stderr:
            return output.returncode == 0
        raise ManduaError(
            ErrorCode.GIT_FAILURE,
            "Git could not verify the review-note publication ancestry.",
        )

    def _run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
        input_bytes: bytes = b"",
        max_output_bytes: int | None = None,
        index_file: GitIndexFile | None = None,
        literal_pathspecs: bool = False,
    ) -> GitOutput[bytes]:
        authority = self._repository_authority
        return self._runner.run(
            arguments,
            check=check,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            max_output_bytes=max_output_bytes,
            index_file=index_file,
            literal_pathspecs=literal_pathspecs,
            isolated_configuration=authority is not None,
            repository_authority=authority,
        )

    def _run_text(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
        input_bytes: bytes = b"",
        max_output_bytes: int | None = None,
        index_file: GitIndexFile | None = None,
        literal_pathspecs: bool = False,
    ) -> GitOutput[str]:
        authority = self._repository_authority
        return self._runner.run_text(
            arguments,
            check=check,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            max_output_bytes=max_output_bytes,
            index_file=index_file,
            literal_pathspecs=literal_pathspecs,
            isolated_configuration=authority is not None,
            repository_authority=authority,
        )

    def _run_index(
        self,
        arguments: list[str],
        *,
        index_file: GitIndexFile,
        input_bytes: bytes = b"",
    ) -> GitOutput[str]:
        return self._run_text(
            arguments,
            input_bytes=input_bytes,
            index_file=index_file,
            timeout_seconds=self._remaining_timeout(),
        )

    @staticmethod
    def _require_silent_success(output: GitOutput[str], message: str) -> None:
        if output.returncode != 0 or output.stdout or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)

    def _resolve_target(self, revision: str) -> str:
        output = self._run_text(
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0:
            raise ManduaError(ErrorCode.INVALID_REVISION, "The revision is invalid.")
        return self._strict_oid(output, "Git returned an invalid annotation target.")

    def _read_notes_ref(self, notes_ref: str, *, deadline: float) -> str | None:
        output = self._run_text(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", notes_ref],
            check=False,
            timeout_seconds=self._remaining(deadline),
        )
        if output.returncode == 1 and not output.stdout and not output.stderr:
            return None
        if output.returncode != 0:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not inspect the review notes ref.")
        object_id = self._strict_oid(output, "Git returned an invalid review notes ref.")
        object_type = self._run_text(
            ["cat-file", "-t", object_id],
            check=False,
            timeout_seconds=self._remaining(deadline),
        )
        if object_type.returncode != 0 or object_type.stdout != "commit\n" or object_type.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The review notes ref is not a commit.")
        return object_id

    @staticmethod
    def _close_repository_authority(
        authority: GitRepositoryAuthority,
        *,
        original: BaseException,
    ) -> None:
        """Close authority descriptors without masking original control flow."""
        try:
            authority.cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001 - closure is mandatory
            original.add_note(
                "Review-note repository-authority cleanup also failed: "
                f"{type(cleanup_error).__name__}."
            )

    def _object_id_width(self) -> int:
        if self._object_width is None:
            output = self._run_text(
                ["rev-parse", "--show-object-format"],
                check=False,
                timeout_seconds=self._remaining_timeout(),
            )
            if output.returncode != 0 or output.stderr:
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
            width = {"sha1\n": 40, "sha256\n": 64}.get(output.stdout)
            if width is None:
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
            self._object_width = width
        return self._object_width

    def _strict_oid(self, output: GitOutput[str], message: str) -> str:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        object_id = lines[0]
        if _OBJECT_ID.fullmatch(object_id) is None or len(object_id) != self._object_id_width():
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        return object_id

    def _validated_oid_bytes(self, value: bytes) -> bytes | None:
        try:
            object_id = value.decode("ascii")
        except UnicodeDecodeError:
            return None
        if _OBJECT_ID.fullmatch(object_id) is None or len(object_id) != self._object_id_width():
            return None
        return value

    def _note_body(self, request: AnnotationRequest) -> bytes:
        message = self._validated_line(request.message, "The review message is invalid.")
        agent_id = self._validated_line(request.agent_id, "The review Agent-ID is invalid.")
        try:
            body = f"{message}\n\nAgent-ID: {agent_id}\n".encode()
        except UnicodeEncodeError:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The review annotation must be valid UTF-8."
            ) from None
        if len(body) > self._limits.max_output_bytes:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The review annotation exceeds the configured byte limit.",
            )
        return body

    def _validated_line(self, value: object, message: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
            or "\x00" in value
            or "\n" in value
            or "\r" in value
            or any(
                unicodedata.category(character).startswith("C")
                or unicodedata.category(character) in {"Zl", "Zp"}
                for character in value
            )
            or len(value) > self._limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, message)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, message) from None
        return value

    def _validate_request(self, request: object, apply: object) -> None:
        self._remaining_timeout()
        if not isinstance(apply, bool):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The apply setting is invalid.")
        if not isinstance(request, AnnotationRequest):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The annotation request is invalid.")
        if (
            not isinstance(request.revision, str)
            or not request.revision
            or "\x00" in request.revision
            or len(request.revision) > self._limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The revision is invalid.")
        try:
            request.revision.encode("utf-8")
        except UnicodeEncodeError:
            raise ManduaError(ErrorCode.INVALID_REVISION, "The revision is invalid.") from None
        self._validated_line(request.message, "The review message is invalid.")
        self._validated_line(request.agent_id, "The review Agent-ID is invalid.")

    def _validated_timeout(self) -> float:
        value = self._limits.timeout_seconds
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The annotation timeout must be finite and positive.",
            )
        return float(value)

    def _remaining_timeout(self) -> float:
        return self._remaining(self._deadline)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Review annotation exceeded the configured time limit.",
            )
        return remaining

    @staticmethod
    def _require_same_snapshot(expected: _PreparedAnnotation, actual: _PreparedAnnotation) -> None:
        if (
            expected.target_oid != actual.target_oid
            or expected.notes_ref != actual.notes_ref
            or expected.notes_ref_oid != actual.notes_ref_oid
            or expected.note_path != actual.note_path
            or expected.existing_note != actual.existing_note
            or expected.note_body != actual.note_body
        ):
            raise NoteWriter._race_error(actual)

    @staticmethod
    def _require_same_annotation(
        expected: _PreparedAnnotation, actual: _PreparedAnnotation
    ) -> None:
        if (
            expected.target_oid != actual.target_oid
            or expected.notes_ref != actual.notes_ref
            or expected.note_body != actual.note_body
        ):
            raise NoteWriter._race_error(actual)

    @staticmethod
    def _race_error(prepared: _PreparedAnnotation) -> ManduaError:
        return ManduaError(
            ErrorCode.POLICY_VIOLATION,
            "Repository review state changed while the annotation was being prepared.",
            recovery=f"Inspect {prepared.notes_ref} and retry the annotation.",
        )

    @staticmethod
    def _uncertain_mutation_error(prepared: _PreparedAnnotation) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "The review-note command was interrupted and the mutation state is uncertain.",
            recovery=f"Inspect {prepared.notes_ref} for commit {prepared.target_oid} before retrying.",
        )

    def _result(
        self,
        prepared: _PreparedAnnotation,
        *,
        after_oid: str | None,
        applied: bool,
        warnings: tuple[str, ...] = (),
    ) -> MemoryResult:
        evidence_id = f"annotation:{prepared.target_oid}"
        return MemoryResult(
            operation="annotate",
            answer=(
                "The review annotation was applied."
                if applied
                else "The review annotation was validated and previewed."
            ),
            observed=(
                Claim(
                    "The annotation targets an exact local commit.",
                    evidence=(evidence_id,),
                ),
            ),
            evidence=(
                Evidence(
                    id=evidence_id,
                    kind="annotation-preview",
                    oid=prepared.target_oid,
                    ref=prepared.notes_ref,
                    excerpt=prepared.request.message[: self._limits.max_excerpt_chars],
                    details={
                        "agent_id": prepared.request.agent_id,
                        "notes_ref_oid": after_oid or prepared.notes_ref_oid,
                        "signature_verified": False,
                    },
                ),
            ),
            history_scope=HistoryScope(
                end_oid=prepared.target_oid,
                refs=(prepared.notes_ref,),
                notes_available=(after_oid or prepared.notes_ref_oid) is not None,
            ),
            warnings=warnings,
            changes=(
                PlannedChange(
                    action="append-note",
                    target=prepared.notes_ref,
                    before_oid=prepared.notes_ref_oid,
                    after_oid=after_oid,
                ),
            ),
            applied=applied,
        )


def annotate_result(
    repository: Path,
    limits: QueryLimits,
    request: AnnotationRequest,
    *,
    apply: bool = False,
) -> MemoryResult:
    """Build one annotation result through an operation-scoped writer."""
    return NoteWriter(repository, limits).annotate(request, apply=apply)
