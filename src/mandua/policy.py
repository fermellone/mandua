"""Central policy checks for Mandu'a mutable operations and proposed trees."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from mandua.config import load_tree_policy, load_worktree_policy, parse_repository_policy
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRepositoryAuthority, GitRunner
from mandua.metadata import build_commit_message, parse_trailers
from mandua.models import QueryLimits, RepositoryPolicy, UniqueJsonFieldInvariant

_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_PRIVATE_KEY = re.compile(rb"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")
_AWS_ACCESS_KEY = re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_ASSIGNED_SECRET = re.compile(
    r'(?i)\b(?:api[_-]?key|token|secret)\b\s*[:=]\s*(?:"[^"\r\n]{20,}"|\'[^\'\r\n]{20,}\'|[^\s#;]{20,})'
)
_REGULAR_MODES = frozenset({"100644", "100755"})
_INDEX_MODE_TYPES = {
    "100644": "blob",
    "100755": "blob",
    "120000": "blob",
    "160000": "commit",
}
_CONFIG_PATH = PurePosixPath(".mandua.toml")


def _authority_arguments(
    authority: GitRepositoryAuthority | None,
) -> dict[str, object]:
    if authority is None:
        return {}
    return {"isolated_configuration": True, "repository_authority": authority}


class Policy:
    """Apply one immutable repository policy to worktree and tree content."""

    def __init__(
        self,
        repository: Path,
        runner: GitRunner,
        repository_policy: RepositoryPolicy,
        limits: QueryLimits,
        *,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self.repository_policy = repository_policy
        self._limits = limits
        self._repository_authority = repository_authority

    @classmethod
    def open(
        cls,
        repository: Path,
        *,
        limits: QueryLimits | None = None,
        runner: GitRunner | None = None,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> Policy:
        """Open a worktree policy using only bounded configuration and Git access."""
        configured_limits = limits or QueryLimits()
        try:
            root = Path(repository).resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.INVALID_REPOSITORY, "The repository directory is unavailable."
            ) from None
        if not root.is_dir():
            raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")
        proof_runner = runner or GitRunner(root, limits=configured_limits)
        try:
            top_level = proof_runner.run_text(
                ["rev-parse", "--show-toplevel"],
                **_authority_arguments(repository_authority),
            ).stdout.strip()
            resolved_top_level = Path(top_level).resolve(strict=True)
        except ManduaError as error:
            if repository_authority is not None or error.code is ErrorCode.LIMIT_EXCEEDED:
                raise
            raise ManduaError(
                ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid."
            ) from None
        except OSError:
            raise ManduaError(
                ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid."
            ) from None
        if resolved_top_level != root:
            raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")
        policy = load_worktree_policy(root, configured_limits)
        if repository_authority is not None:
            proof_runner.validate_repository_authority(repository_authority)
        policy_runner = runner or GitRunner(root, limits=configured_limits)
        return cls(
            root,
            policy_runner,
            policy,
            configured_limits,
            repository_authority=repository_authority,
        )

    def validate_path(self, path: PurePosixPath) -> PurePosixPath:
        """Return a safe path only when it is component-contained by a knowledge root."""
        checked = _checked_path(path, self._limits, ErrorCode.POLICY_VIOLATION)
        if not any(_is_within(checked, root) for root in self.repository_policy.knowledge_roots):
            raise _policy_error("The path is outside configured knowledge roots.")
        return checked

    def validate_message(
        self,
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
    ) -> str:
        """Build a bounded semantic commit message through the shared metadata builder."""
        if (
            not isinstance(subject, str)
            or _contains_surrogate(subject)
            or not subject.strip()
            or "\x00" in subject
            or "\n" in subject
            or "\r" in subject
            or len(subject) > 72
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The commit subject must be one non-empty line of at most 72 characters.",
            )
        if any(
            _contains_surrogate(value)
            for value in (reason, memory_type, scope, task_id, decision_id, agent_id, corrects)
            if isinstance(value, str)
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "Commit metadata must be valid UTF-8.")
        message = build_commit_message(
            subject,
            reason=reason,
            memory_type=memory_type,
            scope=scope,
            task_id=task_id,
            decision_id=decision_id,
            agent_id=agent_id,
            corrects=corrects,
            extra_trailers=extra_trailers,
            limits=self._limits,
        )
        trailers = parse_trailers(message)
        if any("\n" in key or "\n" in value or "\x00" in value for key, value in trailers):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The generated commit trailers are invalid."
            )
        return message

    def validate_selected_files(self, paths: Iterable[PurePosixPath]) -> None:
        """Validate selected worktree files; missing paths are left unread for future deletions."""
        total = 0
        deadline = time.monotonic() + self._limits.timeout_seconds
        for path in paths:
            _remaining_timeout(deadline)
            checked = self.validate_path(path)
            entry = self._lstat_selected_file(checked)
            if entry is None:
                continue
            metadata = entry
            if metadata.st_size > self.repository_policy.max_file_bytes:
                raise _policy_error("A selected file exceeds the configured size limit.")
            total += metadata.st_size
            if total > self._limits.max_output_bytes:
                raise _policy_error("Selected files exceed the configured aggregate byte limit.")
            contents = _read_checked_regular_file(
                self._repository, checked, metadata, self.repository_policy.max_file_bytes
            )
            _reject_secret_material(contents)
            _remaining_timeout(deadline)

    def validate_selected_contents(self, entries: Iterable[tuple[PurePosixPath, bytes]]) -> None:
        """Validate already captured path bytes without reopening worktree files."""
        total = 0
        deadline = time.monotonic() + self._limits.timeout_seconds
        for path, contents in entries:
            _remaining_timeout(deadline)
            self.validate_path(path)
            if not isinstance(contents, bytes):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED, "Selected content must be captured bytes."
                )
            if len(contents) > self.repository_policy.max_file_bytes:
                raise _policy_error("A selected file exceeds the configured size limit.")
            total += len(contents)
            if total > self._limits.max_output_bytes:
                raise _policy_error("Selected files exceed the configured aggregate byte limit.")
            _reject_secret_material(contents)
            _remaining_timeout(deadline)

    def validate_staged_index(self, index_contents: bytes) -> None:
        """Validate the exact stage-zero index without writing a tree or reopening worktree data."""
        deadline = time.monotonic() + self._limits.timeout_seconds
        width = _object_id_width(
            self._runner,
            deadline,
            repository_authority=self._repository_authority,
        )
        raw_records = _parse_index_records(
            self._runner.run(
                ["ls-files", "--stage", "-z", "--"],
                timeout_seconds=_remaining_timeout(deadline),
                **_authority_arguments(self._repository_authority),
            ).stdout,
            self._limits,
            width,
        )
        _validate_index_file(index_contents, width, len(raw_records), self._limits)
        records = _validate_index_objects(
            self._runner,
            raw_records,
            deadline,
            repository_authority=self._repository_authority,
        )
        config_record = next((record for record in records if record[3] == _CONFIG_PATH), None)
        if config_record is None:
            proposed_policy = parse_repository_policy(b"", self._limits)
        else:
            mode, object_type, object_id, _path = config_record
            if mode != "100644" or object_type != "blob":
                raise _policy_error("The staged configuration entry is invalid.")
            config_size = _blob_size(
                self._runner,
                object_id,
                timeout_seconds=_remaining_timeout(deadline),
                repository_authority=self._repository_authority,
            )
            if config_size > self._limits.max_output_bytes:
                raise _policy_error("The staged configuration exceeds the byte limit.")
            config_contents = _read_blob_object(
                self._runner,
                object_id,
                config_size,
                timeout_seconds=_remaining_timeout(deadline),
                repository_authority=self._repository_authority,
            )
            proposed_policy = parse_repository_policy(config_contents, self._limits)
        self._validate_proposed_records(
            proposed_policy,
            records,
            deadline,
            lambda object_id, _path, size, timeout: _read_blob_object(
                self._runner,
                object_id,
                size,
                timeout_seconds=timeout,
                repository_authority=self._repository_authority,
            ),
        )

    def validate_tree(
        self,
        tree: str,
        *,
        invariant_error_code: ErrorCode = ErrorCode.POLICY_VIOLATION,
    ) -> None:
        """Validate all knowledge blobs and declarative invariants in exactly one proposed tree."""
        if not isinstance(invariant_error_code, ErrorCode):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The invariant error category is invalid."
            )
        if not isinstance(tree, str) or _OBJECT_ID.fullmatch(tree) is None:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The proposed tree ID is invalid.")
        deadline = time.monotonic() + self._limits.timeout_seconds
        self._verify_exact_tree(tree, deadline)
        proposed_policy = load_tree_policy(
            self._runner,
            tree,
            self._limits,
            deadline=deadline,
            repository_authority=self._repository_authority,
        )
        records = _parse_tree_records(
            self._runner.run(
                ["ls-tree", "-r", "-z", tree],
                timeout_seconds=_remaining_timeout(deadline),
                **_authority_arguments(self._repository_authority),
            ).stdout,
            self._limits,
        )
        _remaining_timeout(deadline)
        self._validate_proposed_records(
            proposed_policy,
            records,
            deadline,
            lambda _object_id, path, _size, timeout: (
                self._runner.show_blob(
                    tree,
                    path,
                    timeout_seconds=timeout,
                    **_authority_arguments(self._repository_authority),
                ).stdout
            ),
            invariant_error_code=invariant_error_code,
        )

    def _validate_proposed_records(
        self,
        proposed_policy: RepositoryPolicy,
        records: tuple[tuple[str, str, str, PurePosixPath], ...],
        deadline: float,
        read_blob: Callable[[str, PurePosixPath, int, float], bytes],
        *,
        invariant_error_code: ErrorCode = ErrorCode.POLICY_VIOLATION,
    ) -> None:
        contents: dict[PurePosixPath, bytes] = {}
        total = 0
        for mode, object_type, object_id, path in records:
            _remaining_timeout(deadline)
            if not any(_is_within(path, root) for root in proposed_policy.knowledge_roots):
                continue
            if mode not in _REGULAR_MODES or object_type != "blob":
                raise _policy_error(
                    "Knowledge content in the proposed snapshot must be a regular blob."
                )
            size = _blob_size(
                self._runner,
                object_id,
                timeout_seconds=_remaining_timeout(deadline),
                repository_authority=self._repository_authority,
            )
            if size > proposed_policy.max_file_bytes:
                raise _policy_error("A proposed knowledge file exceeds the configured size limit.")
            total += size
            if total > self._limits.max_output_bytes:
                raise _policy_error("Proposed knowledge content exceeds the aggregate byte limit.")
            blob = read_blob(
                object_id,
                path,
                size,
                _remaining_timeout(deadline),
            )
            if len(blob) != size:
                raise _policy_error("A proposed knowledge blob changed while it was read.")
            _reject_secret_material(blob)
            _remaining_timeout(deadline)
            contents[path] = blob
        for invariant in proposed_policy.invariants:
            _remaining_timeout(deadline)
            try:
                _validate_unique_json_field(invariant, contents, deadline)
            except ManduaError as error:
                if error.code is not ErrorCode.POLICY_VIOLATION:
                    raise
                raise ManduaError(
                    invariant_error_code,
                    error.message,
                    evidence=error.evidence,
                    recovery=error.recovery,
                ) from None
        _remaining_timeout(deadline)

    def _verify_exact_tree(self, tree: str, deadline: float) -> None:
        expected_width = _object_id_width(
            self._runner,
            deadline,
            repository_authority=self._repository_authority,
        )
        if len(tree) != expected_width:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The proposed tree ID has the wrong object format."
            )
        result = self._runner.run_text(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input_bytes=f"{tree}\n".encode("ascii"),
            timeout_seconds=_remaining_timeout(deadline),
            check=False,
            **_authority_arguments(self._repository_authority),
        )
        if result.returncode != 0 or result.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object probe.")
        output = result.stdout
        lines = output.splitlines()
        if len(lines) != 1:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object probe.")
        fields = lines[0].split(" ")
        if len(fields) == 2 and fields[0].casefold() == tree.casefold() and fields[1] == "missing":
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The proposed tree object is unavailable."
            )
        if len(fields) != 2 or fields[0].casefold() != tree.casefold():
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object probe.")
        if fields[1] != "tree":
            if fields[1] in {"blob", "commit", "tag"}:
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "The proposed object is not a tree.")
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object probe.")

    def _lstat_selected_file(self, path: PurePosixPath) -> os.stat_result | None:
        current = self._repository
        for index, component in enumerate(path.parts):
            current = current / component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                return None
            except OSError:
                raise _policy_error("A selected path cannot be inspected safely.") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise _policy_error("Selected paths must not traverse symbolic links.")
            if index < len(path.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise _policy_error("A selected path has a non-directory parent.")
        if not stat.S_ISREG(metadata.st_mode):
            raise _policy_error("Selected paths must name regular files.")
        return metadata


def _checked_path(path: object, limits: QueryLimits, code: ErrorCode) -> PurePosixPath:
    if not isinstance(path, PurePosixPath) or path.is_absolute() or not path.parts:
        raise ManduaError(code, "The repository path is invalid.")
    if (
        len(path.as_posix()) > limits.max_input_chars
        or _contains_surrogate(path.as_posix())
        or "\x00" in path.as_posix()
        or path.as_posix().startswith(":")
        or any(
            component in {"", ".", ".."} or component.casefold() == ".git"
            for component in path.parts
        )
    ):
        raise ManduaError(code, "The repository path is invalid.")
    return path


def _is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path.parts[: len(root.parts)] == root.parts


def _read_checked_regular_file(
    root: Path, path: PurePosixPath, before: os.stat_result, maximum: int
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError:
        raise _policy_error("The repository root cannot be opened safely.") from None
    try:
        parent_descriptor = root_descriptor
        for component in path.parts[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=parent_descriptor)
            if parent_descriptor != root_descriptor:
                os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        descriptor = os.open(path.parts[-1], flags, dir_fd=parent_descriptor)
    except OSError:
        raise _policy_error("A selected file cannot be opened safely.") from None
    finally:
        if "parent_descriptor" in locals() and parent_descriptor != root_descriptor:
            os.close(parent_descriptor)
        os.close(root_descriptor)
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or _fingerprint(before) != _fingerprint(after)
            or after.st_size > maximum
        ):
            raise _policy_error("A selected file changed while it was read.")
        contents = _read_descriptor(descriptor, maximum)
        completed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(contents) > maximum
        or len(contents) != after.st_size
        or _fingerprint(before) != _fingerprint(completed)
    ):
        raise _policy_error("A selected file changed while it was read.")
    if len(contents) > maximum:
        raise _policy_error("A selected file exceeds the configured size limit.")
    return contents


def _read_descriptor(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _reject_secret_material(contents: bytes) -> None:
    decoded = contents.decode("utf-8", errors="replace")
    if (
        _PRIVATE_KEY.search(contents)
        or _AWS_ACCESS_KEY.search(contents)
        or _ASSIGNED_SECRET.search(decoded)
    ):
        raise _policy_error("Secret-like material is not allowed in knowledge content.")


def _parse_tree_records(
    value: bytes, limits: QueryLimits
) -> tuple[tuple[str, str, str, PurePosixPath], ...]:
    if not value:
        return ()
    if not value.endswith(b"\x00"):
        raise _policy_error("Git returned malformed tree records.")
    records: list[tuple[str, str, str, PurePosixPath]] = []
    seen_paths: set[PurePosixPath] = set()
    for record in value[:-1].split(b"\x00"):
        if not record or record.count(b"\t") != 1:
            raise _policy_error("Git returned malformed tree records.")
        header, raw_path = record.split(b"\t", 1)
        try:
            mode, object_type, object_id = header.decode("ascii").split(" ")
            path_text = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise _policy_error("Git returned malformed tree records.") from None
        if _OBJECT_ID.fullmatch(object_id) is None:
            raise _policy_error("Git returned malformed tree records.")
        path = _checked_path(
            path_text_to_path(path_text, limits), limits, ErrorCode.POLICY_VIOLATION
        )
        if path in seen_paths:
            raise _policy_error("Git returned duplicate tree paths.")
        seen_paths.add(path)
        records.append((mode, object_type, object_id, path))
    return tuple(records)


def _parse_index_records(
    value: bytes,
    limits: QueryLimits,
    object_id_width: int,
) -> tuple[tuple[str, str, PurePosixPath], ...]:
    if not value:
        return ()
    if not value.endswith(b"\x00"):
        raise _policy_error("Git returned malformed index records.")
    records: list[tuple[str, str, PurePosixPath]] = []
    seen_paths: set[PurePosixPath] = set()
    for record in value[:-1].split(b"\x00"):
        if not record or record.count(b"\t") != 1:
            raise _policy_error("Git returned malformed index records.")
        header, raw_path = record.split(b"\t", 1)
        try:
            mode, object_id, stage = header.decode("ascii").split(" ")
            path_text = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise _policy_error("Git returned malformed index records.") from None
        if (
            stage != "0"
            or len(object_id) != object_id_width
            or _OBJECT_ID.fullmatch(object_id) is None
        ):
            raise _policy_error("Git returned malformed index records.")
        path = _checked_path(
            path_text_to_path(path_text, limits), limits, ErrorCode.POLICY_VIOLATION
        )
        if path in seen_paths:
            raise _policy_error("Git returned duplicate index paths.")
        seen_paths.add(path)
        records.append((mode, object_id, path))
    return tuple(records)


def _validate_index_file(
    value: bytes,
    object_id_width: int,
    expected_entries: int,
    limits: QueryLimits,
) -> None:
    """Reject unsupported or intent-to-add entries in one exact Git index snapshot."""
    digest_size = object_id_width // 2
    if (
        not isinstance(value, bytes)
        or len(value) > limits.max_output_bytes
        or len(value) < 12 + digest_size
        or value[:4] != b"DIRC"
    ):
        raise _policy_error("The repository index is invalid.")
    version = int.from_bytes(value[4:8], "big")
    entry_count = int.from_bytes(value[8:12], "big")
    if version not in {2, 3, 4} or entry_count != expected_entries:
        raise _policy_error("The repository index is invalid.")
    payload_end = len(value) - digest_size
    hasher = hashlib.sha1 if object_id_width == 40 else hashlib.sha256
    if not hmac.compare_digest(hasher(value[:payload_end]).digest(), value[payload_end:]):
        raise _policy_error("The repository index is invalid.")

    cursor = 12
    previous_path = b""
    for _ in range(entry_count):
        entry_start = cursor
        fixed_end = cursor + 40 + digest_size + 2
        if fixed_end > payload_end:
            raise _policy_error("The repository index is invalid.")
        flags = int.from_bytes(value[fixed_end - 2 : fixed_end], "big")
        cursor = fixed_end
        if flags & 0x4000:
            if version < 3 or cursor + 2 > payload_end:
                raise _policy_error("The repository index is invalid.")
            extended_flags = int.from_bytes(value[cursor : cursor + 2], "big")
            cursor += 2
            if extended_flags & 0x2000:
                raise _policy_error("Intent-to-add index entries are not allowed.")
            if extended_flags & ~0x6000:
                raise _policy_error("The repository index uses unsupported entry flags.")

        if version == 4:
            removed, cursor = _decode_index_v4_removed_path(value, cursor, payload_end)
            if removed > len(previous_path):
                raise _policy_error("The repository index is invalid.")
            terminator = value.find(b"\x00", cursor, payload_end)
            if terminator < 0:
                raise _policy_error("The repository index is invalid.")
            path = previous_path[: len(previous_path) - removed] + value[cursor:terminator]
            cursor = terminator + 1
            previous_path = path
        else:
            terminator = value.find(b"\x00", cursor, payload_end)
            if terminator < 0:
                raise _policy_error("The repository index is invalid.")
            path = value[cursor:terminator]
            cursor = terminator + 1
            padding = (-(cursor - entry_start)) % 8
            if (
                cursor + padding > payload_end
                or value[cursor : cursor + padding] != b"\x00" * padding
            ):
                raise _policy_error("The repository index is invalid.")
            cursor += padding
        if not path or (flags & 0x0FFF) not in {0x0FFF, len(path)}:
            raise _policy_error("The repository index is invalid.")

    while cursor < payload_end:
        if cursor + 8 > payload_end:
            raise _policy_error("The repository index is invalid.")
        signature = value[cursor : cursor + 4]
        extension_size = int.from_bytes(value[cursor + 4 : cursor + 8], "big")
        cursor += 8
        if cursor + extension_size > payload_end:
            raise _policy_error("The repository index is invalid.")
        if signature == b"link" or not signature.isalpha():
            raise _policy_error("The repository index uses an unsupported extension.")
        cursor += extension_size
    if cursor != payload_end:
        raise _policy_error("The repository index is invalid.")


def _decode_index_v4_removed_path(value: bytes, cursor: int, payload_end: int) -> tuple[int, int]:
    if cursor >= payload_end:
        raise _policy_error("The repository index is invalid.")
    result = 0
    for _ in range(10):
        if cursor >= payload_end:
            raise _policy_error("The repository index is invalid.")
        byte = value[cursor]
        cursor += 1
        result = (result << 7) + (byte & 0x7F)
        if not byte & 0x80:
            return result, cursor
        result += 1
    raise _policy_error("The repository index is invalid.")


def _validate_index_objects(
    runner: GitRunner,
    records: tuple[tuple[str, str, PurePosixPath], ...],
    deadline: float,
    *,
    repository_authority: GitRepositoryAuthority | None = None,
) -> tuple[tuple[str, str, str, PurePosixPath], ...]:
    """Prove every staged entry's mode, object availability, and actual object type."""
    expected_types: list[str] = []
    unique_ids: list[str] = []
    for mode, object_id, _path in records:
        expected_type = _INDEX_MODE_TYPES.get(mode)
        if expected_type is None:
            raise _policy_error("The repository index contains an unsupported file mode.")
        expected_types.append(expected_type)
        if object_id not in unique_ids:
            unique_ids.append(object_id)
    if not unique_ids:
        return ()
    result = runner.run_text(
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input_bytes="".join(f"{object_id}\n" for object_id in unique_ids).encode("ascii"),
        timeout_seconds=_remaining_timeout(deadline),
        check=False,
        **_authority_arguments(repository_authority),
    )
    if result.returncode != 0 or result.stderr:
        raise _policy_error("Git returned an invalid staged-object probe.")
    lines = result.stdout.splitlines()
    if len(lines) != len(unique_ids) or result.stdout != "".join(f"{line}\n" for line in lines):
        raise _policy_error("Git returned an invalid staged-object probe.")
    actual_types: dict[str, str] = {}
    for requested, line in zip(unique_ids, lines, strict=True):
        fields = line.split(" ")
        if (
            len(fields) == 2
            and fields[0].casefold() == requested.casefold()
            and fields[1] == "missing"
        ):
            raise _policy_error("A staged object is unavailable.")
        if (
            len(fields) != 3
            or fields[0].casefold() != requested.casefold()
            or fields[1] not in {"blob", "commit", "tree", "tag"}
            or not fields[2].isascii()
            or not fields[2].isdecimal()
        ):
            raise _policy_error("Git returned an invalid staged-object probe.")
        actual_types[requested] = fields[1]
    validated: list[tuple[str, str, str, PurePosixPath]] = []
    for (mode, object_id, path), expected_type in zip(records, expected_types, strict=True):
        actual_type = actual_types[object_id]
        if actual_type != expected_type:
            raise _policy_error("A staged entry's mode does not match its object type.")
        validated.append((mode, actual_type, object_id, path))
    return tuple(validated)


def path_text_to_path(value: str, limits: QueryLimits) -> PurePosixPath:
    """Convert strict Git tree path text without allowing empty or traversal components."""
    if (
        not value
        or len(value) > limits.max_input_chars
        or _contains_surrogate(value)
        or "\x00" in value
        or value.startswith(("/", ":"))
        or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in value.split("/"))
    ):
        raise _policy_error("Git returned malformed tree records.")
    return PurePosixPath(value)


def _blob_size(
    runner: GitRunner,
    object_id: str,
    *,
    timeout_seconds: float | None = None,
    repository_authority: GitRepositoryAuthority | None = None,
) -> int:
    output = runner.run_text(
        ["cat-file", "-s", object_id],
        timeout_seconds=timeout_seconds,
        **_authority_arguments(repository_authority),
    ).stdout.strip()
    if not output.isascii() or not output.isdecimal():
        raise _policy_error("Git returned an invalid blob size.")
    return int(output)


def _read_blob_object(
    runner: GitRunner,
    object_id: str,
    expected_size: int,
    *,
    timeout_seconds: float,
    repository_authority: GitRepositoryAuthority | None = None,
) -> bytes:
    result = runner.run(
        ["cat-file", "blob", object_id],
        timeout_seconds=timeout_seconds,
        check=False,
        **_authority_arguments(repository_authority),
    )
    if result.returncode != 0 or result.stderr or len(result.stdout) != expected_size:
        raise _policy_error("Git returned an invalid staged blob.")
    return result.stdout


def _object_id_width(
    runner: GitRunner,
    deadline: float,
    *,
    repository_authority: GitRepositoryAuthority | None = None,
) -> int:
    result = runner.run_text(
        ["rev-parse", "--show-object-format"],
        timeout_seconds=_remaining_timeout(deadline),
        check=False,
        **_authority_arguments(repository_authority),
    )
    if result.returncode != 0 or result.stderr:
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
    width = {"sha1\n": 40, "sha256\n": 64}.get(result.stdout)
    if width is None:
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
    return width


def _validate_unique_json_field(
    invariant: UniqueJsonFieldInvariant, contents: dict[PurePosixPath, bytes], deadline: float
) -> None:
    document = contents.get(invariant.path)
    if document is None:
        raise _policy_error("A required invariant JSON file is missing from the proposed tree.")
    try:
        _remaining_timeout(deadline)
        decoded = document.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
        _remaining_timeout(deadline)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _policy_error("An invariant JSON file is malformed.") from None
    if not isinstance(value, list):
        raise _policy_error("An invariant JSON file must contain a top-level array.")
    seen: set[tuple[str, str]] = set()
    for item in value:
        _remaining_timeout(deadline)
        if not isinstance(item, dict):
            raise _policy_error("An invariant JSON array must contain objects.")
        if invariant.field not in item or not _is_nonempty_scalar(item[invariant.field]):
            raise _policy_error("An invariant JSON object has a missing or empty required field.")
        identifier = _scalar_key(item[invariant.field])
        if identifier in seen:
            label = _safe_scalar_label(item[invariant.field])
            raise _policy_error(f"The unique JSON field has a duplicate value {label}.")
        seen.add(identifier)
    _remaining_timeout(deadline)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON object key.")
        value[key] = item
    return value


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number.")
    return parsed


def _is_nonempty_scalar(value: object) -> bool:
    if value is None or isinstance(value, (list, dict)):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (bool, int, float))


def _scalar_key(value: object) -> tuple[str, str]:
    return type(value).__name__, json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _safe_scalar_label(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"IRR-[A-Z0-9]{1,24}", value):
        return value
    return "redacted"


def _contains_surrogate(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ManduaError(
            ErrorCode.LIMIT_EXCEEDED, "Policy validation exceeded the configured time limit."
        )
    return remaining


def _policy_error(message: str) -> ManduaError:
    return ManduaError(ErrorCode.POLICY_VIOLATION, message)
