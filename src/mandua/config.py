"""Strict, bounded loading of Mandu'a repository policy configuration."""

from __future__ import annotations

import math
import os
import re
import stat
import time
import tomllib
from pathlib import Path, PurePosixPath

from mandua.bounds import normalize_timeout_seconds
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRepositoryAuthority, GitRunner
from mandua.models import QueryLimits, RepositoryPolicy, UniqueJsonFieldInvariant

_CONFIG_PATH = PurePosixPath(".mandua.toml")
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_TOP_LEVEL_KEYS = frozenset({"repository", "invariants"})
_REPOSITORY_KEYS = frozenset({"canonical_branch", "notes_ref", "knowledge_roots", "max_file_bytes"})
_INVARIANT_KEYS = frozenset({"kind", "path", "field"})


def _authority_arguments(
    authority: GitRepositoryAuthority | None,
) -> dict[str, object]:
    if authority is None:
        return {}
    return {"isolated_configuration": True, "repository_authority": authority}


def load_worktree_policy(repository: Path, limits: QueryLimits) -> RepositoryPolicy:
    """Load .mandua.toml from one worktree without following an external symlink."""
    root = _validated_repository(repository)
    path = root / _CONFIG_PATH.as_posix()
    try:
        contents = _read_regular_file(path, limits.max_output_bytes)
    except FileNotFoundError:
        return parse_repository_policy(b"", limits)
    return parse_repository_policy(contents, limits)


def load_tree_policy(
    runner: GitRunner,
    tree: str,
    limits: QueryLimits,
    *,
    deadline: float | None = None,
    timeout_seconds: float | None = None,
    repository_authority: GitRepositoryAuthority | None = None,
) -> RepositoryPolicy:
    """Load .mandua.toml from exactly *tree*, never from the current worktree."""
    _validate_tree_id(tree)
    if deadline is None:
        configured_timeout = (
            timeout_seconds if timeout_seconds is not None else limits.timeout_seconds
        )
        configured_deadline = time.monotonic() + _validated_timeout(configured_timeout)
    else:
        configured_deadline = deadline
    listing = runner.run(
        ["ls-tree", "-z", tree, "--", _CONFIG_PATH.as_posix()],
        timeout_seconds=_remaining_timeout(configured_deadline),
        **_authority_arguments(repository_authority),
    ).stdout
    if not listing:
        policy = parse_repository_policy(b"", limits)
        _remaining_timeout(configured_deadline)
        return policy
    mode, object_type, object_id, path = _one_tree_record(listing, limits)
    if (
        path != _CONFIG_PATH
        or mode != "100644"
        or object_type != "blob"
        or _OBJECT_ID.fullmatch(object_id) is None
    ):
        raise _configuration_error("The proposed configuration entry is invalid.")
    size = _blob_size(
        runner,
        object_id,
        timeout_seconds=_remaining_timeout(configured_deadline),
        repository_authority=repository_authority,
    )
    if size > limits.max_output_bytes:
        raise _configuration_error("The proposed configuration exceeds the byte limit.")
    contents = runner.show_blob(
        tree,
        _CONFIG_PATH,
        timeout_seconds=_remaining_timeout(configured_deadline),
        **_authority_arguments(repository_authority),
    ).stdout
    if len(contents) != size:
        raise _configuration_error("The proposed configuration changed while it was read.")
    policy = parse_repository_policy(contents, limits)
    _remaining_timeout(configured_deadline)
    return policy


def parse_repository_policy(contents: bytes, limits: QueryLimits) -> RepositoryPolicy:
    """Parse one bounded TOML document into the immutable public policy model."""
    if not isinstance(contents, bytes) or len(contents) > limits.max_output_bytes:
        raise _configuration_error("The repository configuration exceeds the byte limit.")
    try:
        source = contents.decode("utf-8")
    except UnicodeDecodeError:
        raise _configuration_error("The repository configuration must be valid UTF-8.") from None
    try:
        document = tomllib.loads(source)
    except tomllib.TOMLDecodeError:
        raise _configuration_error("The repository configuration is invalid TOML.") from None
    if not isinstance(document, dict) or set(document) - _TOP_LEVEL_KEYS:
        raise _configuration_error(
            "The repository configuration contains an unknown top-level key."
        )

    repository = document.get("repository", {})
    if not isinstance(repository, dict) or set(repository) - _REPOSITORY_KEYS:
        raise _configuration_error(
            "The repository configuration contains an unknown repository key."
        )
    canonical_branch = _branch(repository.get("canonical_branch", "main"), limits)
    notes_ref = _notes_ref(repository.get("notes_ref", "refs/notes/review"), limits)
    knowledge_roots = _knowledge_roots(repository.get("knowledge_roots", ["knowledge"]), limits)
    max_file_bytes = _max_file_bytes(
        repository.get("max_file_bytes", min(1_048_576, limits.max_output_bytes)), limits
    )
    invariants = _invariants(document.get("invariants", []), knowledge_roots, limits)
    return RepositoryPolicy(
        canonical_branch=canonical_branch,
        notes_ref=notes_ref,
        knowledge_roots=knowledge_roots,
        max_file_bytes=max_file_bytes,
        invariants=invariants,
    )


def _validated_repository(repository: Path) -> Path:
    try:
        root = Path(repository).resolve(strict=True)
    except OSError:
        raise _configuration_error("The repository directory is unavailable.") from None
    if not root.is_dir():
        raise _configuration_error("The repository directory is invalid.")
    return root


def _read_regular_file(path: Path, maximum: int) -> bytes:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError:
        raise _configuration_error("The repository configuration cannot be inspected.") from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _configuration_error("The repository configuration must be a regular file.")
    if before.st_size > maximum:
        raise _configuration_error("The repository configuration exceeds the byte limit.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _configuration_error(
            "The repository configuration cannot be opened safely."
        ) from None
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or _fingerprint(before) != _fingerprint(after)
            or after.st_size > maximum
        ):
            raise _configuration_error("The repository configuration changed while it was read.")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        completed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(contents) > maximum
        or len(contents) != after.st_size
        or _fingerprint(before) != _fingerprint(completed)
    ):
        raise _configuration_error("The repository configuration changed while it was read.")
    if len(contents) > maximum:
        raise _configuration_error("The repository configuration exceeds the byte limit.")
    return contents


def _branch(value: object, limits: QueryLimits) -> str:
    if not _valid_text(value, limits) or value.startswith(("refs/", "-")):
        raise _configuration_error("The canonical branch is invalid.")
    _validate_ref_components(value, limits, "The canonical branch is invalid.")
    return value


def _notes_ref(value: object, limits: QueryLimits) -> str:
    if not _valid_text(value, limits) or not value.startswith("refs/notes/"):
        raise _configuration_error("The notes ref is invalid.")
    _validate_ref_components(value, limits, "The notes ref is invalid.")
    return value


def _validate_ref_components(value: str, limits: QueryLimits, message: str) -> None:
    if (
        not _valid_text(value, limits)
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            or character in "~^:?*[\\"
            for character in value
        )
        or value.endswith(".")
        or ".." in value
        or "@{" in value
    ):
        raise _configuration_error(message)
    parts = value.split("/")
    if any(
        not part or part in {".", "..", "@"} or part.startswith(".") or part.endswith(".lock")
        for part in parts
    ):
        raise _configuration_error(message)


def _knowledge_roots(value: object, limits: QueryLimits) -> tuple[PurePosixPath, ...]:
    if not isinstance(value, list) or not value:
        raise _configuration_error("Knowledge roots must be a non-empty array.")
    roots: list[PurePosixPath] = []
    for item in value:
        path = _relative_path(item, limits, "The knowledge root is invalid.")
        if path not in roots:
            roots.append(path)
    return tuple(roots)


def _max_file_bytes(value: object, limits: QueryLimits) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > limits.max_output_bytes
    ):
        raise _configuration_error("The maximum file size is invalid.")
    return value


def _invariants(
    value: object, roots: tuple[PurePosixPath, ...], limits: QueryLimits
) -> tuple[UniqueJsonFieldInvariant, ...]:
    if not isinstance(value, list):
        raise _configuration_error("Invariants must be an array.")
    invariants: list[UniqueJsonFieldInvariant] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != _INVARIANT_KEYS:
            raise _configuration_error("An invariant has an invalid schema.")
        if entry["kind"] != "unique-json-field":
            raise _configuration_error("The invariant kind is unsupported.")
        path = _relative_path(entry["path"], limits, "The invariant path is invalid.")
        if not any(_is_within(path, root) for root in roots):
            raise _configuration_error("The invariant path is outside knowledge roots.")
        field = entry["field"]
        if (
            not _valid_text(field, limits)
            or not field.strip()
            or "\n" in field
            or "\r" in field
            or len(field) > limits.max_input_chars
        ):
            raise _configuration_error("The invariant field is invalid.")
        invariant = UniqueJsonFieldInvariant(path=path, field=field)
        if invariant not in invariants:
            invariants.append(invariant)
    return tuple(invariants)


def _relative_path(value: object, limits: QueryLimits, message: str) -> PurePosixPath:
    if not _valid_text(value, limits) or not value:
        raise _configuration_error(message)
    components = value.split("/")
    if (
        "\x00" in value
        or value.startswith("/")
        or any(
            component in {"", ".", ".."} or component.casefold() == ".git"
            for component in components
        )
    ):
        raise _configuration_error(message)
    return PurePosixPath(value)


def _is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path.parts[: len(root.parts)] == root.parts


def _validate_tree_id(tree: object) -> None:
    if not isinstance(tree, str) or _OBJECT_ID.fullmatch(tree) is None:
        raise _configuration_error("The proposed tree ID is invalid.")


def _one_tree_record(value: bytes, limits: QueryLimits) -> tuple[str, str, str, PurePosixPath]:
    records = _parse_tree_records(value, limits)
    if len(records) != 1:
        raise _configuration_error("The proposed configuration record is invalid.")
    return records[0]


def _parse_tree_records(
    value: bytes, limits: QueryLimits
) -> tuple[tuple[str, str, str, PurePosixPath], ...]:
    if not value.endswith(b"\x00"):
        raise _configuration_error("Git returned malformed tree records.")
    records: list[tuple[str, str, str, PurePosixPath]] = []
    for record in value[:-1].split(b"\x00"):
        if not record or record.count(b"\t") != 1:
            raise _configuration_error("Git returned malformed tree records.")
        header, raw_path = record.split(b"\t", 1)
        try:
            mode, object_type, object_id = header.decode("ascii").split(" ")
            path_text = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise _configuration_error("Git returned malformed tree records.") from None
        if _OBJECT_ID.fullmatch(object_id) is None:
            raise _configuration_error("Git returned malformed tree records.")
        path = _relative_path(path_text, limits, "Git returned malformed tree records.")
        records.append((mode, object_type, object_id, path))
    return tuple(records)


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
        raise _configuration_error("Git returned an invalid blob size.")
    return int(output)


def _valid_text(value: object, limits: QueryLimits) -> bool:
    if not isinstance(value, str) or len(value) > limits.max_input_chars or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
        raise ManduaError(
            ErrorCode.LIMIT_EXCEEDED, "Policy validation exceeded the configured time limit."
        )
    return remaining


def _validated_timeout(value: object) -> float:
    return normalize_timeout_seconds(
        value,
        message="The policy timeout must be finite and positive.",
    )


def _configuration_error(message: str) -> ManduaError:
    return ManduaError(ErrorCode.VALIDATION_FAILED, message)
