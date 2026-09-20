"""Bounded, non-shell execution of Git commands."""

from __future__ import annotations

import hashlib
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Generic, TypeVar

from mandua.bounds import bound_text, normalize_timeout_seconds, validate_query_limits
from mandua.errors import ErrorCode, ManduaError
from mandua.isolated_objects import (
    PublicationRejected,
    isolated_command,
    isolated_objects,
    produces_objects,
)
from mandua.models import Evidence, QueryLimits

TextOrBytes = TypeVar("TextOrBytes", str, bytes)
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_DIAGNOSTIC_OBJECT_ID = re.compile(
    rb"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{40})(?![0-9a-f])",
    re.IGNORECASE,
)
_CONTROLLED_GIT_CONFIG = (
    # Background maintenance can outlive Git and race private object publication/cleanup.
    "maintenance.auto=false",
    "gc.auto=0",
    "core.fsmonitor=false",
    "core.pager=cat",
    "commit.gpgSign=false",
    f"gpg.openpgp.program={os.devnull}",
    f"gpg.program={os.devnull}",
    f"gpg.ssh.allowedSignersFile={os.devnull}",
    "gpg.ssh.defaultKeyCommand=",
    f"gpg.ssh.program={os.devnull}",
    f"gpg.ssh.revocationFile={os.devnull}",
    f"gpg.x509.program={os.devnull}",
    "gpg.format=openpgp",
    "gpg.minTrustLevel=fully",
    "log.showSignature=false",
    "merge.verifySignatures=false",
    "credential.interactive=false",
)
_REMOVED_GIT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_ATTR_SOURCE",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIFF_OPTS",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXTERNAL_DIFF",
    "GIT_INDEX_FILE",
    "GIT_GLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_LITERAL_PATHSPECS",
    "GIT_NAMESPACE",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_WORK_TREE",
}
_DIFF_PRODUCING_COMMANDS = frozenset({"blame", "diff", "log", "show"})
_UNSAFE_HELPER_OPTIONS = ("--ext-diff", "--filters", "--textconv")
_DEFAULT_MAX_GIT_PROCESSES = 512
_DEFAULT_MAX_GIT_OUTPUT_BYTES = 1_048_576
_DEFAULT_MAX_GIT_INPUT_BYTES = 1_048_576
_MAX_AUTHORITY_DIRECTORY_ENTRIES = 64
_INDEX_MUTATING_COMMANDS = frozenset({"read-tree", "update-index", "write-tree"})
_monotonic = time.monotonic


@dataclass(frozen=True, slots=True)
class GitOutput(Generic[TextOrBytes]):
    """Captured, bounded output from a single Git invocation."""

    stdout: TextOrBytes
    stderr: TextOrBytes
    returncode: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _GitProcessEvent:
    """Private lifecycle evidence for one exact Git process attempt."""

    attempt: object
    phase: str
    command: tuple[str, ...]
    arguments: tuple[str, ...]
    cwd: Path
    returncode: int | None


_GitProcessObserver = Callable[[_GitProcessEvent], None]
_GIT_PROCESS_OBSERVER: ContextVar[_GitProcessObserver | None] = ContextVar(
    "mandua_git_process_observer", default=None
)


@contextmanager
def _observe_git_processes(observer: _GitProcessObserver) -> Iterator[None]:
    """Scope a private process observer to this context without changing public execution."""
    if not callable(observer):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git process observer is invalid.")
    token = _GIT_PROCESS_OBSERVER.set(observer)
    try:
        yield
    finally:
        _GIT_PROCESS_OBSERVER.reset(token)


def _observation_failure_recovery(existing: str | None = None) -> str:
    notice = "Git process observation also failed; treat its operation audit as incomplete."
    return f"{existing} {notice}" if existing else notice


def _notify_git_process(
    event: _GitProcessEvent, *, original_error: BaseException | None = None
) -> None:
    observer = _GIT_PROCESS_OBSERVER.get()
    if observer is None:
        return
    try:
        observer(event)
    except BaseException as observer_error:
        if original_error is not None and not isinstance(observer_error, Exception):
            raise observer_error from original_error
        if original_error is not None:
            if isinstance(original_error, ManduaError):
                original_error.recovery = _observation_failure_recovery(original_error.recovery)
            else:
                original_error.add_note(_observation_failure_recovery())
            raise original_error from observer_error
        if not isinstance(observer_error, Exception):
            # Interpreter-level and other direct BaseException control flow is not a Git
            # diagnostic.  Preserve the exact object so the transaction owner can perform
            # bounded mutation classification before deciding whether it is safe to re-raise.
            raise
        if event.phase == "considered" and isinstance(observer_error, ManduaError):
            raise
        phase = "before launch" if event.phase == "considered" else "at completion"
        evidence = (
            (
                Evidence(
                    id="git_observation_failure",
                    kind="GitObservationFailure",
                    details={
                        "exit_code": event.returncode,
                        "process_phase": event.phase,
                    },
                ),
            )
            if event.phase != "considered"
            else ()
        )
        raise ManduaError(
            ErrorCode.GIT_FAILURE,
            f"Git process observation failed {phase}.",
            evidence=evidence,
            recovery="No unrecorded Git process may be trusted; repair the observer before retrying.",
        ) from observer_error


@dataclass(frozen=True, slots=True)
class GitCapabilities:
    """Capabilities probed from the Git executable available at runtime."""

    version: str
    merge_tree_write_tree: bool


@dataclass(frozen=True, slots=True)
class GitIndexFile:
    """Opaque authority for one runner-owned temporary Git index."""

    path: Path
    _authority: object


@dataclass(frozen=True, slots=True)
class _GitBudgetLimits:
    """Private aggregate limits for one bounded family of Git processes."""

    max_processes: int = _DEFAULT_MAX_GIT_PROCESSES
    max_output_bytes: int = _DEFAULT_MAX_GIT_OUTPUT_BYTES
    max_input_bytes: int = _DEFAULT_MAX_GIT_INPUT_BYTES


@dataclass(frozen=True, slots=True)
class _AuthorityEntry:
    """One descriptor-bound administrative entry captured without following links."""

    name: str
    descriptor: int | None
    device: int | None
    inode: int | None
    file_type: int | None
    digest: bytes | None = None
    contents: bytes | None = None


@dataclass(frozen=True, slots=True)
class _AuthorityPath:
    """One absolute directory path bound to a no-follow open descriptor."""

    label: str
    path: Path
    descriptor: int
    device: int
    inode: int
    file_type: int


@dataclass(frozen=True, slots=True)
class _AuthorityPathIdentity:
    """Stable physical identity of one captured repository authority root."""

    device: int
    inode: int
    file_type: int


@dataclass(frozen=True, slots=True)
class _RepositoryAuthorityLayout:
    """Immutable physical repository layout used for bounded authority reopening."""

    worktree: Path
    worktree_identity: _AuthorityPathIdentity
    git_directory: Path
    git_directory_identity: _AuthorityPathIdentity
    common_directory: Path
    common_directory_identity: _AuthorityPathIdentity
    object_directory: Path
    object_directory_identity: _AuthorityPathIdentity
    object_format: str
    locator_device: int
    locator_inode: int
    locator_file_type: int
    locator_digest: bytes | None
    maximum_administrative_file_bytes: int


@dataclass(frozen=True, slots=True)
class _PrivateAuthorityEntry:
    """One exact trust-bearing child inside the private common-directory projection."""

    name: str
    kind: str
    descriptor: int | None
    device: int
    inode: int
    file_type: int
    digest: bytes | None = None
    link_target: str | None = None
    child_names: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class _IndexSnapshot:
    """One bounded exact state of a runner-owned temporary index."""

    present: bool
    device: int | None = None
    inode: int | None = None
    file_type: int | None = None
    size: int | None = None
    digest: bytes | None = None
    contents: bytes | None = None


@dataclass(slots=True)
class _TemporaryIndexAuthority:
    """Descriptor-bound parent and current exact snapshot for one temporary index."""

    directory: _PrivateDirectory
    parent: _AuthorityPath
    path: Path
    snapshot: _IndexSnapshot


class _PrivateDirectory:
    """Descriptor-owned temporary directory with finite non-recursive cleanup."""

    def __init__(self, *, prefix: str) -> None:
        created = Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=True)
        self.name = os.fspath(created)
        self._active = True
        self._manifest: frozenset[str] = frozenset()
        self._parent_path = created.parent
        self._basename = created.name
        self._parent_descriptor = -1
        self._root_descriptor = -1
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._parent_descriptor = os.open(self._parent_path, flags)
            self._root_descriptor = os.open(self._basename, flags, dir_fd=self._parent_descriptor)
            metadata = os.fstat(self._root_descriptor)
            path_metadata = os.stat(
                self._basename,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or not stat.S_ISDIR(path_metadata.st_mode)
                or metadata.st_dev != path_metadata.st_dev
                or metadata.st_ino != path_metadata.st_ino
            ):
                raise OSError("temporary directory identity changed during creation")
            self._device = metadata.st_dev
            self._inode = metadata.st_ino
        except BaseException:
            self._close_descriptors()
            self._active = False
            raise

    def set_cleanup_manifest(self, names: frozenset[str]) -> None:
        """Record the complete finite set of children owned by this resource."""
        if not self._active or any(
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\x00" in name
            for name in names
        ):
            raise OSError("invalid private-directory cleanup manifest")
        self._manifest = frozenset(names)

    def cleanup(self) -> None:
        """Detach and remove only the captured finite manifest through open descriptors."""
        if not self._active:
            return
        container_path: Path | None = None
        container_descriptor = -1
        detached = False
        try:
            self._validate_root_path()
            if self._directory_names(self._root_descriptor) != self._manifest:
                raise OSError("private directory namespace changed before cleanup")
            container_path = Path(
                tempfile.mkdtemp(
                    prefix=f".{self._basename}-cleanup-",
                    dir=self._parent_path,
                )
            )
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            container_descriptor = os.open(container_path, flags)
            os.rename(
                self._basename,
                "captured",
                src_dir_fd=self._parent_descriptor,
                dst_dir_fd=container_descriptor,
            )
            detached = True
            captured = os.stat("captured", dir_fd=container_descriptor, follow_symlinks=False)
            if (
                captured.st_dev != self._device
                or captured.st_ino != self._inode
                or not stat.S_ISDIR(captured.st_mode)
            ):
                self._restore_detached_replacement(container_descriptor)
                detached = False
                raise OSError("private directory replacement reached cleanup")
            try:
                os.stat(
                    self._basename,
                    dir_fd=self._parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise OSError("private directory path was replaced during cleanup")

            for name in sorted(self._manifest):
                self._remove_manifest_entry(name)
            if self._directory_names(self._root_descriptor):
                raise OSError("private directory was not empty after manifest cleanup")
            captured_after = os.stat("captured", dir_fd=container_descriptor, follow_symlinks=False)
            if (
                captured_after.st_dev != self._device
                or captured_after.st_ino != self._inode
                or not stat.S_ISDIR(captured_after.st_mode)
            ):
                raise OSError("private cleanup quarantine changed concurrently")
            os.rmdir("captured", dir_fd=container_descriptor)
            detached = False
            try:
                os.stat(
                    self._basename,
                    dir_fd=self._parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise OSError("private directory path was replaced during cleanup")
            container_metadata = os.fstat(container_descriptor)
            path_metadata = os.stat(container_path, follow_symlinks=False)
            if (
                container_metadata.st_dev != path_metadata.st_dev
                or container_metadata.st_ino != path_metadata.st_ino
                or not stat.S_ISDIR(path_metadata.st_mode)
                or self._directory_names(container_descriptor)
            ):
                raise OSError("private cleanup quarantine changed concurrently")
            os.close(container_descriptor)
            container_descriptor = -1
            os.rmdir(container_path.name, dir_fd=self._parent_descriptor)
            container_path = None
        except BaseException:
            if detached:
                # The captured resource remains quarantined; never recurse through a name whose
                # identity cannot be atomically coupled to removal on all supported platforms.
                pass
            raise
        finally:
            if container_descriptor >= 0:
                try:
                    os.close(container_descriptor)
                except OSError:
                    pass
            self._active = False
            self._close_descriptors()

    def abandon(self) -> None:
        """Revoke cleanup when the original directory no longer owns its path."""
        self._active = False
        self._close_descriptors()

    def _validate_root_path(self) -> None:
        metadata = os.fstat(self._root_descriptor)
        path_metadata = os.stat(
            self._basename,
            dir_fd=self._parent_descriptor,
            follow_symlinks=False,
        )
        if (
            metadata.st_dev != self._device
            or metadata.st_ino != self._inode
            or path_metadata.st_dev != self._device
            or path_metadata.st_ino != self._inode
            or not stat.S_ISDIR(metadata.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
        ):
            raise OSError("private directory path changed before cleanup")

    def _restore_detached_replacement(self, container_descriptor: int) -> None:
        try:
            os.stat(
                self._basename,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            os.rename(
                "captured",
                self._basename,
                src_dir_fd=container_descriptor,
                dst_dir_fd=self._parent_descriptor,
            )

    def _remove_manifest_entry(self, name: str) -> None:
        metadata = os.stat(name, dir_fd=self._root_descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(name, flags, dir_fd=self._root_descriptor)
            try:
                observed = os.fstat(descriptor)
                if (
                    observed.st_dev != metadata.st_dev
                    or observed.st_ino != metadata.st_ino
                    or self._directory_names(descriptor)
                ):
                    raise OSError("private manifest directory changed during cleanup")
            finally:
                os.close(descriptor)
            os.rmdir(name, dir_fd=self._root_descriptor)
            return
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise OSError("private manifest entry has an unsafe type")
        os.unlink(name, dir_fd=self._root_descriptor)

    @staticmethod
    def _directory_names(descriptor: int) -> frozenset[str]:
        names: set[str] = set()
        scan_descriptor = os.open(
            ".",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            with os.scandir(scan_descriptor) as entries:
                for entry in entries:
                    names.add(entry.name)
                    if len(names) > _MAX_AUTHORITY_DIRECTORY_ENTRIES:
                        raise OSError("private directory cleanup manifest exceeded its bound")
        finally:
            os.close(scan_descriptor)
        return frozenset(names)

    def _close_descriptors(self) -> None:
        for name in ("_root_descriptor", "_parent_descriptor"):
            descriptor = getattr(self, name, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, -1)


class GitRepositoryAuthority:
    """Opaque runner-owned projection with closed config and attribute authority."""

    def __init__(
        self,
        runner: GitRunner,
        directory: _PrivateDirectory,
        *,
        git_directory: Path,
        common_directory: Path,
        object_directory: Path,
        worktree: Path,
        attribute_source: str | None,
        git_directory_entries: tuple[_AuthorityEntry, ...],
        source_parent_descriptor: int,
        authority_paths: tuple[_AuthorityPath, ...],
        private_common_path: _AuthorityPath,
        private_names: frozenset[str],
        private_entries: tuple[_PrivateAuthorityEntry, ...],
        source_entries: tuple[_AuthorityEntry, ...],
        worktree_locator: _AuthorityEntry,
        layout: _RepositoryAuthorityLayout,
        object_path: _AuthorityPath,
        file_descriptors: tuple[int, ...],
    ) -> None:
        self._runner = runner
        self._directory = directory
        self.git_directory = git_directory
        self.common_directory = common_directory
        self.object_directory = object_directory
        self.worktree = worktree
        self.attribute_source = attribute_source
        self._git_directory_entries = git_directory_entries
        self._source_parent_descriptor = source_parent_descriptor
        self._authority_paths = authority_paths
        self._private_common_path = private_common_path
        self._private_names = private_names
        self._private_entries = private_entries
        self._source_entries = source_entries
        self._worktree_locator = worktree_locator
        self.layout = layout
        self._object_path = object_path
        self.file_descriptors = file_descriptors

    def cleanup(self) -> None:
        """Close the authority and verify removal of its private directory."""
        self._runner._close_repository_authority(self)


class GitOperationBudget:
    """One thread-safe aggregate allowance shared by every runner in an operation."""

    def __init__(
        self, limits: QueryLimits, *, budget_limits: _GitBudgetLimits | None = None
    ) -> None:
        configured_limits = validate_query_limits(limits)
        private_limits = budget_limits or _GitBudgetLimits()
        if not isinstance(private_limits, _GitBudgetLimits):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The aggregate Git limits are invalid.")
        self._validate_positive_integer(private_limits.max_processes, "process")
        self._validate_positive_integer(private_limits.max_output_bytes, "output")
        self._validate_positive_integer(private_limits.max_input_bytes, "input")
        normalized_timeout = GitRunner._validate_timeout_value(configured_limits.timeout_seconds)
        self.deadline = _monotonic() + normalized_timeout
        self.limits = private_limits
        self._processes_remaining = private_limits.max_processes
        self._output_remaining = private_limits.max_output_bytes
        self._input_remaining = private_limits.max_input_bytes
        self._lock = threading.Lock()

    @staticmethod
    def _validate_positive_integer(value: object, label: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                f"The aggregate Git {label} limit must be a positive integer.",
            )

    def remaining_timeout(self) -> float:
        """Return the finite remaining wall-clock allowance."""
        remaining = self.deadline - _monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The aggregate Git operation time limit was exceeded.",
            )
        return remaining

    def reserve_process(self, input_bytes: int) -> int:
        """Reserve one process and its complete argv/stdin bytes before starting Git."""
        self.remaining_timeout()
        with self._lock:
            if self._processes_remaining < 1:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The aggregate Git process limit was exceeded.",
                )
            if input_bytes > self._input_remaining:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The aggregate Git input byte limit was exceeded.",
                )
            if self._output_remaining < 1:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The aggregate Git output byte limit was exceeded.",
                )
            self._processes_remaining -= 1
            self._input_remaining -= input_bytes
            return self._output_remaining

    def consume_output(self, output_bytes: int) -> None:
        """Charge stdout and stderr bytes to the shared operation."""
        with self._lock:
            if output_bytes > self._output_remaining:
                self._output_remaining = 0
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The aggregate Git output byte limit was exceeded.",
                )
            self._output_remaining -= output_bytes


class GitRunner:
    """Execute Git through bounded argument arrays in one repository."""

    def __init__(
        self,
        repository: Path,
        *,
        limits: QueryLimits | None = None,
        max_output_bytes: int | None = None,
        timeout_seconds: float | None = None,
        operation_budget: GitOperationBudget | None = None,
        _budget_limits: _GitBudgetLimits | None = None,
    ) -> None:
        configured_limits = validate_query_limits(QueryLimits() if limits is None else limits)
        self._limits = validate_query_limits(
            QueryLimits(
                max_commits=configured_limits.max_commits,
                max_output_bytes=(
                    max_output_bytes
                    if max_output_bytes is not None
                    else configured_limits.max_output_bytes
                ),
                max_excerpt_chars=configured_limits.max_excerpt_chars,
                max_input_chars=configured_limits.max_input_chars,
                timeout_seconds=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else configured_limits.timeout_seconds
                ),
            )
        )
        self._repository = Path(repository)
        if operation_budget is not None and not isinstance(operation_budget, GitOperationBudget):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git operation budget is invalid.")
        if _budget_limits is not None and not isinstance(_budget_limits, _GitBudgetLimits):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The private Git limits are invalid.")
        if operation_budget is not None and _budget_limits not in {
            None,
            operation_budget.limits,
        }:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The Git runner and operation limits differ."
            )
        self._operation_budget = operation_budget
        self._git_limits = (
            operation_budget.limits
            if operation_budget is not None
            else (_budget_limits or _GitBudgetLimits())
        )
        self._capabilities: GitCapabilities | None = None
        self._index_authority = object()
        self._active_indexes: set[Path] = set()
        self._index_states: dict[Path, _TemporaryIndexAuthority] = {}
        self._active_repository_authorities: set[GitRepositoryAuthority] = set()

    @property
    def capabilities(self) -> GitCapabilities:
        """Return Git features discovered by executing capability probes."""
        if self._capabilities is None:
            deadline = _monotonic() + float(self._limits.timeout_seconds)
            version = self.run_text(
                ["--version"], timeout_seconds=self._remaining_timeout(deadline)
            ).stdout.strip()
            probe = self.run(
                ["merge-tree", "--write-tree"],
                check=False,
                timeout_seconds=self._remaining_timeout(deadline),
            )
            probe_text = self._decode(probe.stdout + probe.stderr)[0].lower()
            self._capabilities = GitCapabilities(
                version=version,
                merge_tree_write_tree=(
                    "unknown option" not in probe_text and "unrecognized option" not in probe_text
                ),
            )
        return self._capabilities

    def run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
        input_bytes: bytes = b"",
        max_output_bytes: int | None = None,
        index_file: GitIndexFile | None = None,
        literal_pathspecs: bool = False,
        isolated_configuration: bool = False,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> GitOutput[bytes]:
        """Run Git and return binary output after enforcing execution limits."""
        repository = self._validated_repository()
        self._validate_arguments(arguments)
        self._validate_input(input_bytes)
        timeout = self._effective_timeout(timeout_seconds)
        output_limit = self._effective_output_limit(max_output_bytes)
        validated_authority = self._validated_repository_authority(repository_authority)
        validated_index = self._validated_index_authority(index_file)
        if (
            validated_authority is None
            and validated_index is not None
            and produces_objects(arguments)
        ):
            try:
                authority = self.open_repository_authority(None)
            except BaseException:
                self._require_index_snapshot(validated_index, validated_index.snapshot)
                raise
            original: BaseException | None = None
            try:
                return self.run(
                    arguments,
                    check=check,
                    timeout_seconds=timeout_seconds,
                    input_bytes=input_bytes,
                    max_output_bytes=max_output_bytes,
                    index_file=index_file,
                    literal_pathspecs=literal_pathspecs,
                    isolated_configuration=isolated_configuration,
                    repository_authority=authority,
                )
            except BaseException as error:
                original = error
                raise
            finally:
                try:
                    authority.cleanup()
                except BaseException as cleanup_error:
                    if original is None:
                        raise
                    original.add_note(
                        f"Temporary index repository authority cleanup was uncertain: {cleanup_error}"
                    )
        mutates_index = validated_index is not None and arguments[0] in _INDEX_MUTATING_COMMANDS
        controlled_environment = self._environment(
            index_file=(validated_index.path if validated_index is not None else None),
            literal_pathspecs=literal_pathspecs,
            isolated_configuration=isolated_configuration,
            repository_authority=validated_authority,
        )
        command = self._command(
            self._safe_arguments(arguments),
            isolated_configuration=isolated_configuration,
        )
        private_object_write = validated_authority is not None and produces_objects(arguments)
        if self._operation_budget is not None and not private_object_write:
            timeout = min(timeout, self._operation_budget.remaining_timeout())
            output_limit = min(
                output_limit,
                self._operation_budget.reserve_process(
                    len(input_bytes) + self._argument_bytes(command)
                ),
            )

        try:
            process_options = {
                "arguments": arguments,
                "command": command,
                "cwd": repository,
                "environment": controlled_environment,
                "pass_fds": (validated_authority.file_descriptors if validated_authority else ()),
                "check": check,
                "input_bytes": input_bytes,
                "timeout_seconds": timeout,
                "max_output_bytes": output_limit,
            }
            if private_object_write:
                assert validated_authority is not None
                with isolated_objects(
                    self, validated_authority, arguments, validated_index
                ) as execution:
                    process_options.update(
                        command=execution.command(command),
                        environment=execution.environment(controlled_environment),
                        pass_fds=(execution.root,),
                        before_notify=execution.publish,
                    )
                    execution.refresh_index(process_options)
                    if self._operation_budget is not None:
                        process_options["timeout_seconds"] = min(
                            timeout, self._operation_budget.remaining_timeout()
                        )
                        process_options["max_output_bytes"] = min(
                            output_limit,
                            self._operation_budget.reserve_process(
                                len(input_bytes) + self._argument_bytes(process_options["command"])
                            ),
                        )
                    output = self._run_prepared_process(**process_options)
            else:
                output = self._run_prepared_process(**process_options)
        except BaseException as command_error:
            if validated_authority is not None:
                try:
                    self._validate_authority_sources(validated_authority)
                except ManduaError as authority_error:
                    raise authority_error from command_error
            if validated_index is not None:
                try:
                    process_started = getattr(command_error, "_mandua_git_process_started", True)
                    if mutates_index and process_started:
                        validated_index.snapshot = self._capture_index_snapshot(validated_index)
                    else:
                        self._require_index_snapshot(validated_index, validated_index.snapshot)
                except ManduaError as index_error:
                    raise index_error from command_error
            if isinstance(command_error, OSError):
                recovery = (
                    _observation_failure_recovery()
                    if getattr(command_error, "_mandua_observation_failed", False)
                    else None
                )
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    (
                        "Git publication failed after the process started."
                        if getattr(command_error, "_mandua_git_process_started", False)
                        else "Git could not be started."
                    ),
                    evidence=(
                        Evidence(
                            id="git_failure",
                            kind="GitFailure",
                            details={"exit_code": None, "subcommand": arguments[0]},
                        ),
                    ),
                    recovery=recovery,
                ) from command_error
            raise
        if validated_authority is not None:
            self._validate_authority_sources(validated_authority)
        if validated_index is not None:
            if mutates_index:
                validated_index.snapshot = self._capture_index_snapshot(validated_index)
            else:
                self._require_index_snapshot(validated_index, validated_index.snapshot)

        return output

    def _run_prepared_process(
        self,
        *,
        arguments: list[str],
        command: list[str] | tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        pass_fds: tuple[int, ...] = (),
        check: bool,
        input_bytes: bytes,
        timeout_seconds: float,
        max_output_bytes: int,
        before_notify: Callable[[int], None] | None = None,
    ) -> GitOutput[bytes]:
        """Run one already-validated command through the sole bounded process primitive."""
        attempt = object()
        command_tuple = tuple(command)
        arguments_tuple = tuple(arguments)

        def notify(
            phase: str,
            returncode: int | None = None,
            *,
            original_error: BaseException | None = None,
        ) -> None:
            _notify_git_process(
                _GitProcessEvent(
                    attempt=attempt,
                    phase=phase,
                    command=command_tuple,
                    arguments=arguments_tuple,
                    cwd=cwd,
                    returncode=returncode,
                ),
                original_error=original_error,
            )

        try:
            notify("considered")
        except BaseException as error:
            error._mandua_git_process_started = False
            raise
        try:
            process = subprocess.Popen(
                command_tuple,
                cwd=cwd,
                env=environment,
                pass_fds=pass_fds,
                shell=False,
                start_new_session=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except BaseException as error:
            error._mandua_git_process_started = False
            try:
                notify("failed", original_error=error)
            except BaseException as terminal_error:
                if terminal_error is error and terminal_error.__cause__ is not None:
                    error._mandua_observation_failed = True
                raise
            raise
        try:
            stdout, stderr = self._capture_bounded_process(
                process,
                input_bytes=input_bytes,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        except BaseException as error:
            error._mandua_git_process_started = True
            notify("failed", process.returncode, original_error=error)
            raise

        output = GitOutput(stdout=stdout, stderr=stderr, returncode=process.returncode)
        if before_notify is not None:
            try:
                before_notify(output.returncode)
            except PublicationRejected as error:
                output = GitOutput(stdout=b"", stderr=str(error).encode(), returncode=128)
            except BaseException as error:
                error._mandua_git_process_started = True
                notify("failed", output.returncode, original_error=error)
                raise
        process_error = (
            self._git_failure(arguments[0], output.returncode, output.stderr)
            if check and output.returncode != 0
            else None
        )
        notify(
            "completed" if output.returncode == 0 else "failed",
            output.returncode,
            original_error=process_error,
        )
        if check and process_error is not None:
            raise process_error
        return output

    def _capture_bounded_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        input_bytes: bytes,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> tuple[bytes, bytes]:
        """Drain one child incrementally while enforcing combined output and time bounds."""
        selector: selectors.BaseSelector | None = None
        streams: tuple[BinaryIO, ...] = ()
        result: tuple[bytes, bytes] | None = None
        original_error: BaseException | None = None
        original_cause: BaseException | None = None
        try:
            streams = tuple(
                stream
                for stream in (process.stdin, process.stdout, process.stderr)
                if stream is not None
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git process pipes are unavailable.")
            deadline = _monotonic() + timeout_seconds
            cleanup_deadline: float | None = None
            stdout = bytearray()
            stderr = bytearray()
            output_size = 0
            input_offset = 0
            failure: ManduaError | None = None
            selector = selectors.DefaultSelector()
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if input_bytes:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()

            while selector.get_map():
                now = _monotonic()
                if failure is None and now >= deadline:
                    failure = ManduaError(
                        ErrorCode.LIMIT_EXCEEDED,
                        "Git command exceeded the configured time limit.",
                    )
                    cleanup_deadline = now + 1.0
                    self._close_registered_input(selector, process)
                    self._kill_process_group(process)
                if cleanup_deadline is not None and now >= cleanup_deadline:
                    break
                wait_until = cleanup_deadline if failure is not None else deadline
                events = selector.select(timeout=max(0.0, min(0.05, wait_until - now)))
                for key, _ in events:
                    stream = key.fileobj
                    if key.data == "stdin":
                        try:
                            written = os.write(stream.fileno(), input_bytes[input_offset:])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            written = 0
                        if written:
                            input_offset += written
                        if written == 0 or input_offset == len(input_bytes):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        chunk = os.read(stream.fileno(), 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    output_size += len(chunk)
                    if self._operation_budget is not None:
                        try:
                            self._operation_budget.consume_output(len(chunk))
                        except ManduaError as error:
                            if failure is None:
                                failure = error
                                cleanup_deadline = _monotonic() + 1.0
                                self._close_registered_input(selector, process)
                                self._kill_process_group(process)
                    if output_size > max_output_bytes and failure is None:
                        failure = ManduaError(
                            ErrorCode.LIMIT_EXCEEDED,
                            "Git command output exceeded the configured byte limit.",
                        )
                        cleanup_deadline = _monotonic() + 1.0
                        self._close_registered_input(selector, process)
                        self._kill_process_group(process)
                    if failure is None:
                        (stdout if key.data == "stdout" else stderr).extend(chunk)
            if failure is not None:
                raise failure
            result = bytes(stdout), bytes(stderr)
        except OSError as error:
            original_error = ManduaError(
                ErrorCode.GIT_FAILURE, "Git process output could not be captured."
            )
            original_cause = error
        except BaseException as error:  # noqa: BLE001 - cleanup covers every post-launch abort
            original_error = error

        cleanup_error = self._cleanup_captured_process(process, selector, streams)
        if cleanup_error is not None:
            if original_error is None:
                raise cleanup_error
            self._disclose_process_cleanup_uncertainty(original_error)
        if original_error is not None:
            if original_cause is not None:
                raise original_error from original_cause
            raise original_error
        assert result is not None
        return result

    def _cleanup_captured_process(
        self,
        process: subprocess.Popen[bytes],
        selector: selectors.BaseSelector | None,
        streams: tuple[BinaryIO, ...],
    ) -> ManduaError | None:
        """Close every available resource and reap the child despite earlier cleanup failures."""
        cleanup_failed = False
        if selector is not None:
            try:
                selector.close()
            except BaseException:  # noqa: BLE001 - later process cleanup must still run
                cleanup_failed = True
        for stream in streams:
            try:
                if not stream.closed:
                    stream.close()
            except BaseException:  # noqa: BLE001 - later process cleanup must still run
                cleanup_failed = True
        try:
            if process.poll() is None:
                self._kill_process_group(process)
        except BaseException:  # noqa: BLE001 - bounded reap remains mandatory
            cleanup_failed = True
        try:
            self._wait_for_process(process)
        except BaseException:  # noqa: BLE001 - report cleanup uncertainty uniformly
            cleanup_failed = True
        if not cleanup_failed:
            return None
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "Git process cleanup could not be verified.",
            recovery="Treat the child process state as uncertain before retrying.",
        )

    @staticmethod
    def _disclose_process_cleanup_uncertainty(error: BaseException) -> None:
        notice = "Git process cleanup also failed; the child process state is uncertain."
        if isinstance(error, ManduaError):
            error.recovery = f"{error.recovery} {notice}" if error.recovery else notice
        else:
            error.add_note(notice)

    @staticmethod
    def _close_registered_input(
        selector: selectors.BaseSelector, process: subprocess.Popen[bytes]
    ) -> None:
        if process.stdin is None or process.stdin.closed:
            return
        try:
            selector.unregister(process.stdin)
        except KeyError:
            pass
        process.stdin.close()

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass

    def _wait_for_process(self, process: subprocess.Popen[bytes]) -> None:
        """Reap one child within two finite kill-and-wait windows."""
        for _ in range(2):
            try:
                process.wait(timeout=1.0)
                return
            except subprocess.TimeoutExpired:
                self._kill_process_group(process)
        raise ManduaError(
            ErrorCode.GIT_FAILURE,
            "Git process cleanup exceeded the configured time limit.",
            recovery="Terminate the lingering Git process before retrying.",
        )

    def run_text(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
        input_bytes: bytes = b"",
        max_output_bytes: int | None = None,
        index_file: GitIndexFile | None = None,
        literal_pathspecs: bool = False,
        isolated_configuration: bool = False,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> GitOutput[str]:
        """Run Git and decode its bounded output as UTF-8 with replacement."""
        output = self.run(
            arguments,
            check=check,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            max_output_bytes=max_output_bytes,
            index_file=index_file,
            literal_pathspecs=literal_pathspecs,
            isolated_configuration=isolated_configuration,
            repository_authority=repository_authority,
        )
        stdout, stdout_replaced = self._decode(output.stdout)
        stderr, stderr_replaced = self._decode(output.stderr)
        warnings: list[str] = []
        if stdout_replaced or stderr_replaced:
            warnings.append("Git output contained invalid UTF-8 and was decoded with replacement.")
        return GitOutput(
            stdout=stdout,
            stderr=stderr,
            returncode=output.returncode,
            warnings=tuple(warnings),
        )

    @contextmanager
    def temporary_index(self) -> Iterator[GitIndexFile]:
        """Yield one private index authority that is valid only inside this context."""
        directory: _PrivateDirectory | None = None
        parent: _AuthorityPath | None = None
        state: _TemporaryIndexAuthority | None = None
        path: Path | None = None
        try:
            directory = _PrivateDirectory(prefix="mandua-git-index-")
            os.chmod(directory.name, 0o700)
            root = Path(directory.name).resolve(strict=True)
            parent = self._capture_authority_path(root, "temporary Git index directory")
            path = root / "index"
            state = _TemporaryIndexAuthority(
                directory=directory,
                parent=parent,
                path=path,
                snapshot=_IndexSnapshot(present=False),
            )
            self._active_indexes.add(path)
            self._index_states[path] = state
        except BaseException as setup_failure:
            if path is not None:
                self._active_indexes.discard(path)
                self._index_states.pop(path, None)
            cleanup_failed = False
            if directory is not None:
                try:
                    if state is not None:
                        cleanup_failed = not self._cleanup_temporary_index(state)
                    elif parent is not None:
                        self._validate_authority_path(parent)
                        self._set_private_manifest(directory, frozenset())
                        directory.cleanup()
                    else:
                        self._set_private_manifest(directory, frozenset())
                        directory.cleanup()
                except BaseException:  # noqa: BLE001 - cleanup uncertainty overrides every abort
                    cleanup_failed = True
            if parent is not None:
                cleanup_failed = not self._close_descriptors([parent.descriptor]) or cleanup_failed
            if cleanup_failed:
                raise self._temporary_index_cleanup_error() from None
            if isinstance(setup_failure, OSError):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "The temporary Git index could not be created."
                ) from None
            raise

        assert path is not None
        assert state is not None
        try:
            yield GitIndexFile(path=path, _authority=self._index_authority)
        finally:
            self._active_indexes.discard(path)
            self._index_states.pop(path, None)
            cleanup_failed = False
            try:
                cleanup_failed = not self._cleanup_temporary_index(state)
            except BaseException:  # noqa: BLE001 - cleanup uncertainty overrides every abort
                cleanup_failed = True
            finally:
                cleanup_failed = (
                    not self._close_descriptors([state.parent.descriptor]) or cleanup_failed
                )
            if cleanup_failed:
                raise self._temporary_index_cleanup_error() from None

    def seed_temporary_index(self, value: GitIndexFile, contents: bytes) -> None:
        """Create one exact runner-owned index from bounded trusted bytes."""
        self._validate_input(contents)
        state = self._validated_index_authority(value)
        if state is None or state.snapshot.present:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git index path is invalid.")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open("index", flags, 0o600, dir_fd=state.parent.descriptor)
            try:
                offset = 0
                while offset < len(contents):
                    written = os.write(descriptor, contents[offset:])
                    if written < 1:
                        raise OSError("short temporary index write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The temporary Git index could not be seeded."
            ) from None
        state.snapshot = self._capture_index_snapshot(state)
        if state.snapshot.contents != contents:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The temporary Git index changed while being seeded."
            )

    def read_temporary_index(self, value: GitIndexFile) -> bytes:
        """Return the exact bounded contents of one stable runner-owned index."""
        state = self._validated_index_authority(value)
        if state is None or not state.snapshot.present or state.snapshot.contents is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The temporary Git index is unavailable.")
        return state.snapshot.contents

    def _cleanup_temporary_index(self, state: _TemporaryIndexAuthority) -> bool:
        try:
            self._require_index_snapshot(state, state.snapshot)
        except BaseException:  # noqa: BLE001 - never remove an untrusted replacement
            state.directory.abandon()
            return False
        try:
            self._set_private_manifest(
                state.directory,
                frozenset({"index"}) if state.snapshot.present else frozenset(),
            )
            state.directory.cleanup()
        except BaseException:  # noqa: BLE001 - cleanup uncertainty is material
            state.directory.abandon()
            return False
        return True

    @staticmethod
    def _set_private_manifest(directory: object, names: frozenset[str]) -> None:
        setter = getattr(directory, "set_cleanup_manifest", None)
        if setter is not None:
            setter(names)

    def plan_repository_reopen_budget(
        self,
        layout: _RepositoryAuthorityLayout,
        planned_arguments: tuple[tuple[str, ...], ...],
        *,
        expected_output_bytes: int,
    ) -> _GitBudgetLimits:
        """Size one finite emergency reopen from captured metadata and exact Git argv."""
        if (
            not isinstance(layout, _RepositoryAuthorityLayout)
            or not planned_arguments
            or len(planned_arguments) > _DEFAULT_MAX_GIT_PROCESSES
            or not isinstance(expected_output_bytes, int)
            or isinstance(expected_output_bytes, bool)
            or expected_output_bytes < 1
            or not isinstance(layout.maximum_administrative_file_bytes, int)
            or isinstance(layout.maximum_administrative_file_bytes, bool)
            or layout.maximum_administrative_file_bytes < 1
            or layout.maximum_administrative_file_bytes > self._limits.max_output_bytes
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The emergency Git authority budget plan is invalid.",
            )
        cumulative_input = 0
        for planned in planned_arguments:
            arguments = list(planned)
            self._validate_arguments(arguments)
            command = self._command(
                self._safe_arguments(arguments),
                isolated_configuration=True,
            )
            if produces_objects(arguments):
                command = isolated_command(command, 2_147_483_647)
            cumulative_input += self._argument_bytes(command)
        return _GitBudgetLimits(
            max_processes=len(planned_arguments),
            max_output_bytes=max(
                layout.maximum_administrative_file_bytes,
                expected_output_bytes,
            ),
            max_input_bytes=max(1, cumulative_input),
        )

    def open_repository_authority(
        self,
        attribute_source: str | None,
        *,
        expected_layout: _RepositoryAuthorityLayout | None = None,
        bind_index: bool = False,
    ) -> GitRepositoryAuthority:
        """Create a private common-dir projection from one bound physical layout."""
        if attribute_source is not None and (
            not isinstance(attribute_source, str) or _OBJECT_ID.fullmatch(attribute_source) is None
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git attribute source is invalid.")
        if not isinstance(bind_index, bool):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git index binding is invalid.")
        if expected_layout is not None and not isinstance(
            expected_layout, _RepositoryAuthorityLayout
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The expected Git authority layout is invalid."
            )
        repository = self._validated_repository()
        directory: _PrivateDirectory | None = None
        authority: GitRepositoryAuthority | None = None
        private_common_path: _AuthorityPath | None = None
        private_names: frozenset[str] = frozenset()
        private_entries: tuple[_PrivateAuthorityEntry, ...] = ()
        created_private_names: set[str] = set()
        descriptors: list[int] = []
        try:
            worktree_path = self._capture_authority_path(repository, "worktree")
            descriptors.append(worktree_path.descriptor)
            worktree_locator = self._capture_worktree_locator(worktree_path.descriptor)
            if worktree_locator.descriptor is not None:
                descriptors.append(worktree_locator.descriptor)

            git_directory = self._git_directory_from_locator(repository, worktree_locator)
            git_directory_path = self._capture_authority_path(
                git_directory, "worktree Git directory"
            )
            descriptors.append(git_directory_path.descriptor)
            git_directory_entries = [
                self._capture_authority_entry(
                    git_directory_path.descriptor,
                    "commondir",
                    kind="file",
                    required=False,
                )
            ]
            if bind_index:
                git_directory_entries.append(
                    self._capture_authority_entry(
                        git_directory_path.descriptor,
                        "index",
                        kind="file",
                        required=True,
                    )
                )
            for entry in git_directory_entries:
                if entry.descriptor is not None:
                    descriptors.append(entry.descriptor)
            bound_git_entries = tuple(git_directory_entries)
            common_directory = self._common_directory_from_entry(
                git_directory, bound_git_entries[0]
            )
            common_directory_path = self._capture_authority_path(
                common_directory, "common Git directory"
            )
            descriptors.append(common_directory_path.descriptor)
            object_directory = common_directory / "objects"
            object_directory_path = self._capture_authority_path(
                object_directory, "object directory"
            )
            descriptors.append(object_directory_path.descriptor)
            source_paths = (
                worktree_path,
                git_directory_path,
                common_directory_path,
                object_directory_path,
            )

            if expected_layout is None:
                discovered_git_directory = self._strict_repository_directory(
                    self.run_text(
                        ["rev-parse", "--absolute-git-dir"],
                        check=False,
                        isolated_configuration=True,
                    ),
                    repository,
                    "Git returned an invalid worktree administrative directory.",
                )
                self._validate_bound_repository_inputs(
                    source_paths, worktree_locator, bound_git_entries
                )
                if discovered_git_directory != git_directory:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Git returned an inconsistent worktree administrative directory.",
                    )
                discovered_common_directory = self._strict_repository_directory(
                    self.run_text(
                        ["rev-parse", "--git-common-dir"],
                        check=False,
                        isolated_configuration=True,
                    ),
                    repository,
                    "Git returned an invalid common administrative directory.",
                )
                self._validate_bound_repository_inputs(
                    source_paths, worktree_locator, bound_git_entries
                )
                if discovered_common_directory != common_directory:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Git returned an inconsistent common administrative directory.",
                    )
                object_format = self.run_text(
                    ["rev-parse", "--show-object-format"],
                    check=False,
                    isolated_configuration=True,
                )
                self._validate_bound_repository_inputs(
                    source_paths, worktree_locator, bound_git_entries
                )
                if object_format.returncode != 0 or object_format.stderr:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git returned an invalid object format."
                    )
                format_name = object_format.stdout.removesuffix("\n")
                if object_format.stdout != f"{format_name}\n" or format_name not in {
                    "sha1",
                    "sha256",
                }:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git returned an invalid object format."
                    )
            else:
                self._require_expected_layout(repository, worktree_locator, expected_layout)
                if (
                    git_directory != expected_layout.git_directory
                    or common_directory != expected_layout.common_directory
                    or object_directory != expected_layout.object_directory
                ):
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "The captured Git administrative layout changed before reopening.",
                    )
                self._require_expected_root_identities(source_paths, expected_layout)
                format_name = expected_layout.object_format

            expected_width = 40 if format_name == "sha1" else 64
            if attribute_source is not None and len(attribute_source) != expected_width:
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED,
                    "The Git attribute source has the wrong object format.",
                )
            common_descriptor = source_paths[2].descriptor
            captured_entries: list[_AuthorityEntry] = []
            for name, kind, required in (
                ("objects", "directory", True),
                ("refs", "directory", True),
                ("logs", "directory", False),
                ("worktrees", "directory", False),
                ("packed-refs", "file", False),
                ("shallow", "file", False),
            ):
                entry = self._capture_authority_entry(
                    common_descriptor,
                    name,
                    kind=kind,
                    required=required,
                )
                captured_entries.append(entry)
                if entry.descriptor is not None:
                    descriptors.append(entry.descriptor)
            entries = tuple(captured_entries)
            entry_by_name = {entry.name: entry for entry in entries}
            directory = _PrivateDirectory(prefix="mandua-git-authority-")
            os.chmod(directory.name, 0o700)
            common_projection = Path(directory.name).resolve(strict=True)
            private_common_path = self._capture_authority_path(
                common_projection, "private common Git directory"
            )
            descriptors.append(private_common_path.descriptor)
            (common_projection / "info").mkdir(mode=0o700)
            created_private_names.add("info")
            for name in ("refs", "objects", "logs", "worktrees"):
                entry = entry_by_name[name]
                if entry.descriptor is None:
                    continue
                os.symlink(
                    common_directory / name,
                    common_projection / name,
                    target_is_directory=True,
                )
                created_private_names.add(name)
            for name in ("packed-refs", "shallow"):
                entry = entry_by_name[name]
                if entry.descriptor is not None:
                    self._write_private_file(
                        common_projection / name,
                        entry.contents if entry.contents is not None else b"",
                    )
                    created_private_names.add(name)
            config = self._closed_repository_config(
                format_name,
                log_all_ref_updates=entry_by_name["logs"].descriptor is not None,
            )
            self._write_private_file(common_projection / "config", config)
            created_private_names.add("config")
            expected_private_names = {"config", "info"}
            expected_private_names.update(
                name
                for name in ("refs", "objects", "logs", "worktrees", "packed-refs", "shallow")
                if entry_by_name[name].descriptor is not None
            )
            private_names, private_entries = self._capture_private_projection(
                private_common_path.descriptor,
                expected_names=frozenset(expected_private_names),
                common_directory=common_directory,
            )
            descriptors.extend(
                entry.descriptor for entry in private_entries if entry.descriptor is not None
            )
            self._set_private_manifest(directory, private_names)
            object_entry = entry_by_name["objects"]
            assert object_entry.descriptor is not None
            layout = _RepositoryAuthorityLayout(
                worktree=repository,
                worktree_identity=self._authority_path_identity(source_paths[0]),
                git_directory=git_directory,
                git_directory_identity=self._authority_path_identity(source_paths[1]),
                common_directory=common_directory,
                common_directory_identity=self._authority_path_identity(source_paths[2]),
                object_directory=object_directory,
                object_directory_identity=self._authority_path_identity(source_paths[3]),
                object_format=format_name,
                locator_device=worktree_locator.device,
                locator_inode=worktree_locator.inode,
                locator_file_type=worktree_locator.file_type,
                locator_digest=worktree_locator.digest,
                maximum_administrative_file_bytes=max(
                    (
                        len(config),
                        *(
                            len(entry.contents)
                            for entry in (worktree_locator, *bound_git_entries, *entries)
                            if entry.contents is not None
                        ),
                    ),
                ),
            )
            authority = GitRepositoryAuthority(
                self,
                directory,
                git_directory=git_directory,
                common_directory=common_projection,
                object_directory=common_directory / "objects",
                worktree=repository,
                attribute_source=attribute_source,
                git_directory_entries=bound_git_entries,
                source_parent_descriptor=common_descriptor,
                authority_paths=(*source_paths, private_common_path),
                private_common_path=private_common_path,
                private_names=private_names,
                private_entries=private_entries,
                source_entries=entries,
                worktree_locator=worktree_locator,
                layout=layout,
                object_path=source_paths[3],
                file_descriptors=tuple(descriptors),
            )
            self._active_repository_authorities.add(authority)
            self._validate_authority_sources(authority)
            return authority
        except BaseException as setup_error:
            if authority is not None:
                self._active_repository_authorities.discard(authority)
            cleanup_failed = False
            try:
                if directory is not None:
                    self._set_private_manifest(
                        directory,
                        private_names or frozenset(created_private_names),
                    )
                cleanup_failed = directory is not None and not self._cleanup_authority_directory(
                    directory, private_common_path
                )
            except BaseException:  # noqa: BLE001 - descriptors must close on every cleanup path
                cleanup_failed = True
            finally:
                cleanup_failed = not self._close_descriptors(descriptors) or cleanup_failed
            if cleanup_failed:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "The private Git authority cleanup could not be verified.",
                    recovery="Inspect the system temporary directory before retrying.",
                ) from None
            if isinstance(setup_error, ManduaError):
                raise
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The private Git authority could not be created."
            ) from None

    @staticmethod
    def _capture_authority_path(path: Path, label: str) -> _AuthorityPath:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The repository {label} authority path is indirect or invalid.",
            ) from None
        try:
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = os.stat(path, follow_symlinks=False)
            expected_type = stat.S_IFDIR
            if (
                stat.S_IFMT(descriptor_metadata.st_mode) != expected_type
                or stat.S_IFMT(path_metadata.st_mode) != expected_type
                or descriptor_metadata.st_dev != path_metadata.st_dev
                or descriptor_metadata.st_ino != path_metadata.st_ino
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The repository {label} authority path changed during inspection.",
                )
            return _AuthorityPath(
                label=label,
                path=path,
                descriptor=descriptor,
                device=descriptor_metadata.st_dev,
                inode=descriptor_metadata.st_ino,
                file_type=expected_type,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _capture_worktree_locator(self, parent_descriptor: int) -> _AuthorityEntry:
        try:
            metadata = os.stat(".git", dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The worktree .git locator is unavailable."
            ) from None
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
        else:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The worktree .git locator has an invalid type."
            )
        return self._capture_authority_entry(
            parent_descriptor,
            ".git",
            kind=kind,
            required=True,
        )

    @classmethod
    def _git_directory_from_locator(cls, repository: Path, locator: _AuthorityEntry) -> Path:
        if locator.file_type == stat.S_IFDIR:
            candidate = repository / ".git"
        elif locator.file_type == stat.S_IFREG and locator.contents is not None:
            candidate = cls._administrative_locator_path(
                repository,
                locator.contents,
                prefix="gitdir: ",
                label="worktree .git locator",
            )
        else:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The worktree .git locator is invalid.")
        try:
            return candidate.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The worktree Git directory is unavailable."
            ) from None

    @classmethod
    def _common_directory_from_entry(cls, git_directory: Path, commondir: _AuthorityEntry) -> Path:
        if commondir.descriptor is None:
            return git_directory
        if commondir.file_type != stat.S_IFREG or commondir.contents is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The worktree commondir locator is invalid.")
        candidate = cls._administrative_locator_path(
            git_directory,
            commondir.contents,
            prefix="",
            label="worktree commondir locator",
        )
        try:
            return candidate.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The common Git directory is unavailable."
            ) from None

    @staticmethod
    def _administrative_locator_path(
        base: Path,
        contents: bytes,
        *,
        prefix: str,
        label: str,
    ) -> Path:
        try:
            decoded = contents.decode("utf-8")
        except UnicodeDecodeError:
            raise ManduaError(ErrorCode.GIT_FAILURE, f"The {label} is invalid.") from None
        value = decoded.removeprefix(prefix).removesuffix("\n")
        if (
            not decoded.startswith(prefix)
            or decoded != f"{prefix}{value}\n"
            or not value
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, f"The {label} is invalid.")
        candidate = Path(value)
        return candidate if candidate.is_absolute() else base / candidate

    def _validate_bound_repository_inputs(
        self,
        source_paths: tuple[_AuthorityPath, ...],
        locator: _AuthorityEntry,
        git_directory_entries: tuple[_AuthorityEntry, ...],
    ) -> None:
        for source in source_paths:
            self._validate_authority_path(source)
        self._validate_authority_entry(
            source_paths[0].descriptor,
            locator,
            label="worktree .git locator",
        )
        for entry in git_directory_entries:
            self._validate_authority_entry(
                source_paths[1].descriptor,
                entry,
                label=f"worktree {entry.name} administrative entry",
            )

    def _require_expected_layout(
        self,
        repository: Path,
        locator: _AuthorityEntry,
        expected: _RepositoryAuthorityLayout,
    ) -> None:
        if (
            repository != expected.worktree
            or locator.device != expected.locator_device
            or locator.inode != expected.locator_inode
            or locator.file_type != expected.locator_file_type
            or locator.digest != expected.locator_digest
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The worktree .git locator changed before authority reopening.",
            )
        if (
            not expected.git_directory.is_absolute()
            or not expected.common_directory.is_absolute()
            or not expected.object_directory.is_absolute()
            or expected.object_directory != expected.common_directory / "objects"
            or expected.object_format not in {"sha1", "sha256"}
            or not isinstance(expected.maximum_administrative_file_bytes, int)
            or isinstance(expected.maximum_administrative_file_bytes, bool)
            or expected.maximum_administrative_file_bytes < 1
            or expected.maximum_administrative_file_bytes > self._git_limits.max_output_bytes
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The captured Git authority layout is invalid."
            )

    @staticmethod
    def _authority_path_identity(source: _AuthorityPath) -> _AuthorityPathIdentity:
        return _AuthorityPathIdentity(
            device=source.device,
            inode=source.inode,
            file_type=source.file_type,
        )

    @classmethod
    def _require_expected_root_identities(
        cls,
        sources: tuple[_AuthorityPath, ...],
        expected: _RepositoryAuthorityLayout,
    ) -> None:
        expected_identities = (
            expected.worktree_identity,
            expected.git_directory_identity,
            expected.common_directory_identity,
            expected.object_directory_identity,
        )
        for source, expected_identity in zip(sources, expected_identities, strict=True):
            if cls._authority_path_identity(source) != expected_identity:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The repository {source.label} authority path changed before authority "
                    "reopening.",
                )

    def _capture_private_projection(
        self,
        parent_descriptor: int,
        *,
        expected_names: frozenset[str],
        common_directory: Path,
    ) -> tuple[frozenset[str], tuple[_PrivateAuthorityEntry, ...]]:
        names = self._bounded_directory_names(
            parent_descriptor,
            label="private Git authority",
            limit=_MAX_AUTHORITY_DIRECTORY_ENTRIES,
        )
        if names != expected_names:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The private Git authority namespace changed during creation.",
            )
        entries: list[_PrivateAuthorityEntry] = []
        try:
            for name in sorted(names):
                if name in {"refs", "objects", "logs", "worktrees"}:
                    entries.append(
                        self._capture_private_symlink(
                            parent_descriptor,
                            name,
                            expected_target=os.fspath(common_directory / name),
                        )
                    )
                elif name == "info":
                    entries.append(self._capture_private_directory(parent_descriptor, name))
                else:
                    entries.append(self._capture_private_file(parent_descriptor, name))
        except BaseException as error:
            descriptors_closed = self._close_descriptors(
                [entry.descriptor for entry in entries if entry.descriptor is not None]
            )
            if not descriptors_closed:
                error.add_note(
                    "Private Git authority descriptor cleanup was uncertain during capture."
                )
            raise
        return names, tuple(entries)

    def _capture_private_file(self, parent_descriptor: int, name: str) -> _PrivateAuthorityEntry:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The private Git authority {name} entry is indirect or invalid.",
            ) from None
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not stat.S_ISREG(path_metadata.st_mode)
                or metadata.st_dev != path_metadata.st_dev
                or metadata.st_ino != path_metadata.st_ino
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {name} entry changed during capture.",
                )
            contents = self._read_authority_file(descriptor, f"private {name}")
            observed = os.fstat(descriptor)
            if (
                observed.st_dev != metadata.st_dev
                or observed.st_ino != metadata.st_ino
                or observed.st_size != metadata.st_size
                or len(contents) != observed.st_size
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {name} entry changed during capture.",
                )
            return _PrivateAuthorityEntry(
                name=name,
                kind="file",
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                file_type=stat.S_IFREG,
                digest=hashlib.sha256(contents).digest(),
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _capture_private_directory(
        self, parent_descriptor: int, name: str
    ) -> _PrivateAuthorityEntry:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The private Git authority {name} directory is indirect or invalid.",
            ) from None
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or not stat.S_ISDIR(path_metadata.st_mode)
                or metadata.st_dev != path_metadata.st_dev
                or metadata.st_ino != path_metadata.st_ino
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {name} directory changed during capture.",
                )
            child_names = self._bounded_directory_names(
                descriptor,
                label=f"private Git authority {name} directory",
                limit=_MAX_AUTHORITY_DIRECTORY_ENTRIES,
            )
            if child_names:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {name} directory is not empty.",
                )
            return _PrivateAuthorityEntry(
                name=name,
                kind="directory",
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                file_type=stat.S_IFDIR,
                child_names=child_names,
            )
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _capture_private_symlink(
        parent_descriptor: int, name: str, *, expected_target: str
    ) -> _PrivateAuthorityEntry:
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            target = os.readlink(name, dir_fd=parent_descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The private Git authority {name} link is unavailable.",
            ) from None
        if not stat.S_ISLNK(metadata.st_mode) or target != expected_target:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The private Git authority {name} link is invalid.",
            )
        return _PrivateAuthorityEntry(
            name=name,
            kind="symlink",
            descriptor=None,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            file_type=stat.S_IFLNK,
            link_target=target,
        )

    def set_repository_attribute_source(
        self, authority: GitRepositoryAuthority, attribute_source: str
    ) -> None:
        """Pin an active private authority to another exact immutable tree-ish."""
        self._validated_repository_authority(authority)
        if (
            not isinstance(attribute_source, str)
            or _OBJECT_ID.fullmatch(attribute_source) is None
            or len(attribute_source) != (40 if authority.layout.object_format == "sha1" else 64)
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git attribute source is invalid.")
        authority.attribute_source = attribute_source

    def primary_object_directory(self, authority: GitRepositoryAuthority) -> Path:
        """Return the captured object directory after a no-follow alternates inspection."""
        validated = self._validated_repository_authority(authority)
        assert validated is not None
        object_descriptor = validated._object_path.descriptor
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            info_descriptor = os.open("info", flags, dir_fd=object_descriptor)
        except FileNotFoundError:
            self._validate_authority_sources(validated)
            try:
                os.stat("info", dir_fd=object_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return validated.object_directory
            except OSError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Repository alternate metadata could not be inspected.",
                ) from None
            raise ManduaError(
                ErrorCode.INCOMPLETE_HISTORY,
                "Repository alternate metadata appeared concurrently.",
                recovery="Use a complete self-contained local object store before retrying.",
            )
        except OSError:
            raise ManduaError(
                ErrorCode.INCOMPLETE_HISTORY,
                "Mandu'a rejects indirect repository alternate metadata.",
                recovery="Use a complete self-contained local object store before retrying.",
            ) from None
        try:
            before = os.fstat(info_descriptor)
            path_metadata = os.stat("info", dir_fd=object_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(path_metadata.st_mode)
                or before.st_dev != path_metadata.st_dev
                or before.st_ino != path_metadata.st_ino
            ):
                raise ManduaError(
                    ErrorCode.INCOMPLETE_HISTORY,
                    "Mandu'a rejects indirect repository alternate metadata.",
                    recovery="Use a complete self-contained local object store before retrying.",
                )
            for name in ("alternates", "http-alternates"):
                try:
                    os.stat(name, dir_fd=info_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Repository alternate metadata could not be inspected.",
                    ) from None
                raise ManduaError(
                    ErrorCode.INCOMPLETE_HISTORY,
                    "Mandu'a rejects repository alternate object stores.",
                    recovery="Use a complete self-contained local object store before retrying.",
                )
            after = os.fstat(info_descriptor)
            final_path = os.stat("info", dir_fd=object_descriptor, follow_symlinks=False)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or final_path.st_dev != before.st_dev
                or final_path.st_ino != before.st_ino
            ):
                raise ManduaError(
                    ErrorCode.INCOMPLETE_HISTORY,
                    "Repository alternate metadata changed concurrently.",
                    recovery="Use a complete self-contained local object store before retrying.",
                )
        finally:
            os.close(info_descriptor)
        self._validate_authority_sources(validated)
        return validated.object_directory

    def repository_index_snapshot(self, authority: GitRepositoryAuthority) -> bytes:
        """Return exact descriptor-bound index bytes from an active index authority."""
        validated = self._validated_repository_authority(authority)
        assert validated is not None
        matches = tuple(
            entry for entry in validated._git_directory_entries if entry.name == "index"
        )
        if (
            len(matches) != 1
            or matches[0].descriptor is None
            or matches[0].contents is None
            or matches[0].file_type != stat.S_IFREG
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The repository index authority is unavailable.",
            )
        self._validate_authority_sources(validated)
        return matches[0].contents

    def validate_repository_authority(self, authority: GitRepositoryAuthority) -> None:
        """Revalidate every physical source bound to an active repository authority."""
        self._validated_repository_authority(authority)

    def read_repository_administrative_file(
        self,
        authority: GitRepositoryAuthority,
        name: str,
        *,
        maximum: int,
    ) -> bytes:
        """Read one stable regular file relative to the bound per-worktree Git directory."""
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\x00" in name
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < 1
            or maximum > self._limits.max_output_bytes
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The administrative file request is invalid.",
            )
        validated = self._validated_repository_authority(authority)
        assert validated is not None
        parent_descriptor = validated._authority_paths[1].descriptor
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            before = os.fstat(descriptor)
            path_before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or not stat.S_ISREG(path_before.st_mode)
                or before.st_dev != path_before.st_dev
                or before.st_ino != path_before.st_ino
            ):
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "The administrative message path must name a regular file.",
                )
            if before.st_size > maximum:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The administrative message file exceeds the byte limit.",
                )
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            contents = b"".join(chunks)
            if len(contents) > maximum:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The administrative message file exceeds the byte limit.",
                )
            after = os.fstat(descriptor)
            path_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                len(contents) != after.st_size
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
                or after.st_dev != path_after.st_dev
                or after.st_ino != path_after.st_ino
                or not stat.S_ISREG(path_after.st_mode)
            ):
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "The administrative message file changed while it was read.",
                )
            self._validate_authority_sources(validated)
            return contents
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The administrative message file cannot be opened safely.",
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _close_repository_authority(self, authority: GitRepositoryAuthority) -> None:
        if authority not in self._active_repository_authorities or authority._runner is not self:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The private Git authority is invalid.")
        self._active_repository_authorities.discard(authority)
        cleanup_failed = False
        try:
            try:
                self._validate_authority_sources(authority)
            except BaseException:  # noqa: BLE001 - never remove a changed private namespace
                authority._directory.abandon()
                cleanup_failed = True
            else:
                self._set_private_manifest(authority._directory, authority._private_names)
                cleanup_failed = not self._cleanup_authority_directory(
                    authority._directory, authority._private_common_path
                )
        except BaseException:  # noqa: BLE001 - descriptors must close on every cleanup path
            cleanup_failed = True
        finally:
            cleanup_failed = (
                not self._close_descriptors(list(authority.file_descriptors)) or cleanup_failed
            )
        if cleanup_failed:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The private Git authority cleanup could not be verified.",
                recovery="Inspect the system temporary directory before retrying.",
            ) from None

    def _cleanup_authority_directory(
        self,
        directory: _PrivateDirectory,
        private_common_path: _AuthorityPath | None,
    ) -> bool:
        if private_common_path is None:
            directory.abandon()
            return False
        try:
            self._validate_authority_path(private_common_path)
        except BaseException:  # noqa: BLE001 - never remove an untrusted replacement
            directory.abandon()
            return False
        try:
            directory.cleanup()
        except BaseException:  # noqa: BLE001 - cleanup uncertainty is material
            directory.abandon()
            return False
        return True

    def _capture_authority_entry(
        self,
        parent_descriptor: int,
        name: str,
        *,
        kind: str,
        required: bool,
    ) -> _AuthorityEntry:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if kind == "directory":
            flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not required:
                return _AuthorityEntry(name, None, None, None, None)
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The repository {name} administrative entry is unavailable.",
            ) from None
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The repository {name} administrative entry is indirect or invalid.",
            ) from None
        try:
            metadata = os.fstat(descriptor)
            expected_type = stat.S_IFDIR if kind == "directory" else stat.S_IFREG
            if stat.S_IFMT(metadata.st_mode) != expected_type:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The repository {name} administrative entry has an invalid type.",
                )
            path_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                path_metadata.st_dev != metadata.st_dev
                or path_metadata.st_ino != metadata.st_ino
                or stat.S_IFMT(path_metadata.st_mode) != expected_type
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The repository {name} administrative entry changed during inspection.",
                )
            contents = self._read_authority_file(descriptor, name) if kind == "file" else None
            observed = os.fstat(descriptor)
            if (
                observed.st_dev != metadata.st_dev
                or observed.st_ino != metadata.st_ino
                or observed.st_size != metadata.st_size
                or observed.st_mtime_ns != metadata.st_mtime_ns
                or observed.st_ctime_ns != metadata.st_ctime_ns
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The repository {name} administrative entry changed during inspection.",
                )
            digest = hashlib.sha256(contents).digest() if contents is not None else None
            return _AuthorityEntry(
                name=name,
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                file_type=expected_type,
                digest=digest,
                contents=contents,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _read_authority_file(self, descriptor: int, name: str) -> bytes:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            contents = bytearray()
            while len(contents) <= self._git_limits.max_output_bytes:
                chunk = os.read(
                    descriptor,
                    min(65_536, self._git_limits.max_output_bytes + 1 - len(contents)),
                )
                if not chunk:
                    break
                contents.extend(chunk)
            if len(contents) > self._git_limits.max_output_bytes:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    f"The repository {name} administrative entry exceeded the byte limit.",
                )
            return bytes(contents)
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The repository {name} administrative entry could not be read.",
            ) from None
        finally:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
            except OSError:
                pass

    @staticmethod
    def _write_private_file(path: Path, contents: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(contents):
                written = os.write(descriptor, contents[offset:])
                if written < 1:
                    raise OSError("short private authority write")
                offset += written
        finally:
            os.close(descriptor)

    @staticmethod
    def _close_descriptors(descriptors: list[int]) -> bool:
        cleanup_failed = False
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except BaseException:  # noqa: BLE001 - every remaining descriptor must be attempted
                cleanup_failed = True
        return not cleanup_failed

    @staticmethod
    def _closed_repository_config(object_format: str, *, log_all_ref_updates: bool) -> bytes:
        version = "1" if object_format == "sha256" else "0"
        extension = "[extensions]\n\tobjectFormat = sha256\n" if object_format == "sha256" else ""
        return (
            "[core]\n"
            f"\trepositoryformatversion = {version}\n"
            "\tbare = false\n"
            f"\tlogallrefupdates = {'true' if log_all_ref_updates else 'false'}\n"
            "\tfsmonitor = false\n"
            "[commit]\n"
            "\tgpgSign = false\n"
            "\tcleanup = verbatim\n"
            "[merge]\n"
            "\tverifySignatures = false\n"
            "[credential]\n"
            "\tinteractive = false\n"
            "[user]\n"
            "\tname = Mandu'a\n"
            "\temail = mandua@localhost\n"
            f"{extension}"
        ).encode()

    @staticmethod
    def _strict_repository_directory(
        output: GitOutput[str], repository: Path, message: str
    ) -> Path:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n" or "\x00" in lines[0]:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        candidate = Path(lines[0])
        if not candidate.is_absolute():
            candidate = repository / candidate
        try:
            resolved = candidate.resolve(strict=True)
            metadata = os.lstat(resolved)
        except OSError:
            raise ManduaError(ErrorCode.GIT_FAILURE, message) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        return resolved

    @staticmethod
    def _temporary_index_cleanup_error() -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "The temporary Git index cleanup could not be verified.",
            recovery="Inspect the system temporary directory before retrying.",
        )

    def resolve_commit(self, revision: str, *, timeout_seconds: float | None = None) -> str:
        """Resolve a revision to a full commit object ID."""
        if (
            not revision
            or revision.startswith("-")
            or "\x00" in revision
            or len(revision) > self._limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The revision is invalid.")
        try:
            return self.run_text(
                ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
                timeout_seconds=timeout_seconds,
            ).stdout.strip()
        except ManduaError as error:
            if error.code is ErrorCode.GIT_FAILURE:
                raise ManduaError(ErrorCode.INVALID_REVISION, "The revision is invalid.") from None
            raise

    def show_blob(
        self,
        object_id: str,
        path: PurePosixPath,
        *,
        timeout_seconds: float | None = None,
        isolated_configuration: bool = False,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> GitOutput[bytes]:
        """Return the blob at *path* in a full Git object ID."""
        if not _OBJECT_ID.fullmatch(object_id):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        self._validate_repository_path(path)
        deadline = _monotonic() + self._effective_timeout(timeout_seconds)
        output = self.run(
            ["show", f"{object_id}:{path.as_posix()}"],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
            isolated_configuration=isolated_configuration,
            repository_authority=repository_authority,
        )
        if output.returncode == 0:
            return output

        listing = self.run(
            ["ls-tree", "-z", object_id, "--", path.as_posix()],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
            literal_pathspecs=True,
            isolated_configuration=isolated_configuration,
            repository_authority=repository_authority,
        )
        if listing.returncode == 0 and not listing.stderr:
            blob_oid = self._single_tree_blob_oid(listing.stdout, path)
            if blob_oid is not None:
                raise self._missing_object_error(blob_oid, output.stderr)
        elif self._diagnostic_names_object(listing.stderr, object_id):
            raise self._missing_object_error(object_id.lower(), listing.stderr)
        raise self._git_failure("show", output.returncode, output.stderr)

    def stable_patch_id(
        self,
        commit: str,
        path: PurePosixPath | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> str | None:
        """Return Git's stable patch ID for one resolved commit and optional path scope."""
        if not _OBJECT_ID.fullmatch(commit):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        if path is not None:
            self._validate_repository_path(path)
        show_arguments = [
            "show",
            "--pretty=format:",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--end-of-options",
            commit,
        ]
        if path is not None:
            show_arguments.extend(("--", path.as_posix()))
        deadline = _monotonic() + self._effective_timeout(timeout_seconds)
        patch = self.run(
            show_arguments,
            timeout_seconds=self._remaining_timeout(deadline),
            literal_pathspecs=path is not None,
        ).stdout
        output = self.run_text(
            ["patch-id", "--stable"],
            input_bytes=patch,
            timeout_seconds=self._remaining_timeout(deadline),
        ).stdout.strip()
        if not output:
            return None
        patch_id = output.split(maxsplit=1)[0]
        if not _OBJECT_ID.fullmatch(patch_id):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git patch-id returned an invalid record.")
        return patch_id.lower()

    def _validated_repository(self) -> Path:
        try:
            repository = self._repository.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.INVALID_REPOSITORY, "The repository directory is unavailable."
            ) from None
        if not repository.is_dir() or not (repository / ".git").exists():
            raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")
        return repository

    def _validate_arguments(self, arguments: list[str]) -> None:
        if not arguments:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "Git requires at least one argument.")
        for argument in arguments:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "Git arguments must be valid text.")
        if arguments[0].startswith("-") and arguments[0] != "--version":
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "Git commands must not override controlled global options.",
            )
        for argument in arguments:
            if len(argument) > self._limits.max_input_chars:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED, "A Git argument exceeded the input limit."
                )

    def _validate_input(self, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "Git input must be bytes.")
        if len(value) > self._git_limits.max_input_bytes:
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Git input exceeded the byte limit.")

    @staticmethod
    def _argument_bytes(arguments: list[str]) -> int:
        """Measure the complete NUL-separated process argv using OS path encoding."""
        try:
            return sum(len(os.fsencode(argument)) + 1 for argument in arguments)
        except UnicodeEncodeError:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "Git arguments must be valid text."
            ) from None

    def _effective_timeout(self, requested: float | None) -> float:
        configured = self._validate_timeout_value(self._limits.timeout_seconds)
        if requested is None:
            return configured
        normalized_request = self._validate_timeout_value(requested)
        return min(normalized_request, configured)

    def _effective_output_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._limits.max_output_bytes
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or requested < 1
            or requested > self._limits.max_output_bytes
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git output limit is invalid.")
        return requested

    @staticmethod
    def _validate_timeout_value(value: object) -> float:
        return normalize_timeout_seconds(
            value,
            message="The Git timeout must be finite and positive.",
        )

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - _monotonic()
        if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED, "Git command exceeded the configured time limit."
            )
        return remaining

    def _command(self, arguments: list[str], *, isolated_configuration: bool = False) -> list[str]:
        if not isinstance(isolated_configuration, bool):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The isolated Git configuration setting is invalid.",
            )
        return [
            "git",
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            *(item for config in _CONTROLLED_GIT_CONFIG for item in ("-c", config)),
            *arguments,
        ]

    def _safe_arguments(self, arguments: list[str]) -> list[str]:
        subcommand, *remaining = arguments
        for argument in remaining:
            if argument == "--":
                break
            if any(
                argument == option or argument.startswith(f"{option}=")
                for option in _UNSAFE_HELPER_OPTIONS
            ):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED,
                    "Git commands must not enable external helpers or text conversion.",
                )
        if subcommand in _DIFF_PRODUCING_COMMANDS:
            return [subcommand, "--no-ext-diff", "--no-textconv", *remaining]
        return arguments

    def _environment(
        self,
        *,
        index_file: Path | None = None,
        literal_pathspecs: bool = False,
        isolated_configuration: bool = False,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> dict[str, str]:
        if not isinstance(literal_pathspecs, bool):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The literal pathspec setting is invalid."
            )
        if not isinstance(isolated_configuration, bool):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The isolated Git configuration setting is invalid.",
            )
        environment = os.environ.copy()
        for key in tuple(environment):
            if (
                key in _REMOVED_GIT_ENVIRONMENT
                or key == "GIT_CONFIG_COUNT"
                or key.startswith(("GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            ):
                environment.pop(key, None)
        environment.update(
            {
                "GIT_ASKPASS": os.devnull,
                "GIT_EDITOR": "true",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_SEQUENCE_EDITOR": "true",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
                "SSH_ASKPASS": os.devnull,
            }
        )
        if isolated_configuration or repository_authority is not None:
            environment.update(
                {
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_SYSTEM": os.devnull,
                }
            )
        if repository_authority is not None:
            environment.update(
                {
                    "GIT_COMMON_DIR": os.fspath(repository_authority.common_directory),
                    "GIT_DIR": os.fspath(repository_authority.git_directory),
                    "GIT_OBJECT_DIRECTORY": os.fspath(repository_authority.object_directory),
                    "GIT_WORK_TREE": os.fspath(repository_authority.worktree),
                }
            )
            if repository_authority.attribute_source is not None:
                environment["GIT_ATTR_SOURCE"] = repository_authority.attribute_source
        if index_file is not None:
            environment["GIT_INDEX_FILE"] = os.fspath(index_file)
        if literal_pathspecs:
            environment["GIT_LITERAL_PATHSPECS"] = "1"
        return environment

    def _validated_repository_authority(
        self, value: GitRepositoryAuthority | None
    ) -> GitRepositoryAuthority | None:
        if value is None:
            return None
        if value._runner is not self or value not in self._active_repository_authorities:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The private Git authority is invalid.")
        self._validate_authority_sources(value)
        return value

    def _validate_authority_sources(self, authority: GitRepositoryAuthority) -> None:
        """Recheck descriptor-bound administrative inputs without following replacements."""
        for source in authority._authority_paths:
            self._validate_authority_path(source)
        self._validate_authority_entry(
            authority._authority_paths[0].descriptor,
            authority._worktree_locator,
            label="worktree .git locator",
        )
        for entry in authority._git_directory_entries:
            self._validate_authority_entry(
                authority._authority_paths[1].descriptor,
                entry,
                label=f"worktree {entry.name} administrative entry",
            )
        for entry in authority._source_entries:
            self._validate_authority_entry(
                authority._source_parent_descriptor,
                entry,
                label=f"repository {entry.name} administrative entry",
            )
        self._validate_private_projection(authority)

    def _validate_authority_entry(
        self,
        parent_descriptor: int,
        entry: _AuthorityEntry,
        *,
        label: str,
    ) -> None:
        """Recheck one descriptor-relative entry and any immutable file contents."""
        try:
            path_metadata = os.stat(
                entry.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if entry.descriptor is None:
                return
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The {label} disappeared.",
            ) from None
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The {label} could not be rechecked.",
            ) from None
        if entry.descriptor is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The {label} appeared concurrently.",
            )
        try:
            descriptor_metadata = os.fstat(entry.descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The {label} descriptor is unavailable.",
            ) from None
        if (
            path_metadata.st_dev != entry.device
            or path_metadata.st_ino != entry.inode
            or descriptor_metadata.st_dev != entry.device
            or descriptor_metadata.st_ino != entry.inode
            or stat.S_IFMT(path_metadata.st_mode) != entry.file_type
            or stat.S_IFMT(descriptor_metadata.st_mode) != entry.file_type
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The {label} changed concurrently.",
            )
        if (
            entry.digest is not None
            and hashlib.sha256(self._read_authority_file(entry.descriptor, entry.name)).digest()
            != entry.digest
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The {label} changed concurrently.",
            )

    def _validate_private_projection(self, authority: GitRepositoryAuthority) -> None:
        names = self._bounded_directory_names(
            authority._private_common_path.descriptor,
            label="private Git authority",
            limit=_MAX_AUTHORITY_DIRECTORY_ENTRIES,
        )
        if names != authority._private_names:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The private Git authority namespace changed concurrently.",
            )
        for entry in authority._private_entries:
            try:
                path_metadata = os.stat(
                    entry.name,
                    dir_fd=authority._private_common_path.descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {entry.name} entry is unavailable.",
                ) from None
            if (
                path_metadata.st_dev != entry.device
                or path_metadata.st_ino != entry.inode
                or stat.S_IFMT(path_metadata.st_mode) != entry.file_type
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {entry.name} entry changed concurrently.",
                )
            if entry.kind == "symlink":
                try:
                    target = os.readlink(
                        entry.name, dir_fd=authority._private_common_path.descriptor
                    )
                except OSError:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        f"The private Git authority {entry.name} link is unavailable.",
                    ) from None
                if target != entry.link_target:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        f"The private Git authority {entry.name} link changed concurrently.",
                    )
                continue
            if entry.descriptor is None:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {entry.name} descriptor is unavailable.",
                )
            try:
                descriptor_metadata = os.fstat(entry.descriptor)
            except OSError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {entry.name} descriptor is unavailable.",
                ) from None
            if (
                descriptor_metadata.st_dev != entry.device
                or descriptor_metadata.st_ino != entry.inode
                or stat.S_IFMT(descriptor_metadata.st_mode) != entry.file_type
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    f"The private Git authority {entry.name} entry changed concurrently.",
                )
            if entry.kind == "file":
                digest = hashlib.sha256(
                    self._read_authority_file(entry.descriptor, f"private {entry.name}")
                ).digest()
                if digest != entry.digest:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        f"The private Git authority {entry.name} contents changed concurrently.",
                    )
            elif entry.kind == "directory":
                children = self._bounded_directory_names(
                    entry.descriptor,
                    label=f"private Git authority {entry.name} directory",
                    limit=_MAX_AUTHORITY_DIRECTORY_ENTRIES,
                )
                if children != entry.child_names:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        f"The private Git authority {entry.name} namespace changed concurrently.",
                    )

    @staticmethod
    def _validate_authority_path(source: _AuthorityPath) -> None:
        try:
            path_metadata = os.stat(source.path, follow_symlinks=False)
            descriptor_metadata = os.fstat(source.descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The repository {source.label} authority path is unavailable.",
            ) from None
        if (
            path_metadata.st_dev != source.device
            or path_metadata.st_ino != source.inode
            or descriptor_metadata.st_dev != source.device
            or descriptor_metadata.st_ino != source.inode
            or stat.S_IFMT(path_metadata.st_mode) != source.file_type
            or stat.S_IFMT(descriptor_metadata.st_mode) != source.file_type
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                f"The repository {source.label} authority path changed concurrently.",
            )

    def _validated_index_authority(
        self, value: GitIndexFile | None
    ) -> _TemporaryIndexAuthority | None:
        if value is None:
            return None
        if (
            not isinstance(value, GitIndexFile)
            or value._authority is not self._index_authority
            or value.path not in self._active_indexes
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git index path is invalid.")
        state = self._index_states.get(value.path)
        if state is None or state.path != value.path or not value.path.is_absolute():
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git index path is invalid.")
        self._require_index_snapshot(state, state.snapshot)
        return state

    def _capture_index_snapshot(self, state: _TemporaryIndexAuthority) -> _IndexSnapshot:
        self._validate_authority_path(state.parent)
        names = self._bounded_directory_names(
            state.parent.descriptor,
            label="temporary Git index directory",
            limit=_MAX_AUTHORITY_DIRECTORY_ENTRIES,
        )
        if names not in {frozenset(), frozenset({"index"})}:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The temporary Git index directory namespace changed concurrently.",
            )
        if not names:
            return _IndexSnapshot(present=False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open("index", flags, dir_fd=state.parent.descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The temporary Git index is indirect or unavailable."
            ) from None
        try:
            before = os.fstat(descriptor)
            path_metadata = os.stat("index", dir_fd=state.parent.descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or not stat.S_ISREG(path_metadata.st_mode)
                or before.st_dev != path_metadata.st_dev
                or before.st_ino != path_metadata.st_ino
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "The temporary Git index is indirect or invalid."
                )
            contents = self._read_authority_file(descriptor, "temporary Git index")
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or len(contents) != after.st_size
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "The temporary Git index changed while inspected."
                )
            return _IndexSnapshot(
                present=True,
                device=after.st_dev,
                inode=after.st_ino,
                file_type=stat.S_IFREG,
                size=after.st_size,
                digest=hashlib.sha256(contents).digest(),
                contents=contents,
            )
        finally:
            os.close(descriptor)

    def _require_index_snapshot(
        self, state: _TemporaryIndexAuthority, expected: _IndexSnapshot
    ) -> None:
        observed = self._capture_index_snapshot(state)
        if observed != expected:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The temporary Git index changed concurrently."
            )

    @staticmethod
    def _bounded_directory_names(descriptor: int, *, label: str, limit: int) -> frozenset[str]:
        names: set[str] = set()
        scan_descriptor = -1
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            scan_descriptor = os.open(".", flags, dir_fd=descriptor)
            with os.scandir(scan_descriptor) as entries:
                for entry in entries:
                    if not isinstance(entry.name, str):
                        raise ManduaError(
                            ErrorCode.GIT_FAILURE, f"The {label} contains an invalid entry."
                        )
                    names.add(entry.name)
                    if len(names) > limit:
                        raise ManduaError(
                            ErrorCode.LIMIT_EXCEEDED,
                            f"The {label} contains too many entries.",
                        )
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, f"The {label} could not be inspected."
            ) from None
        finally:
            if scan_descriptor >= 0:
                try:
                    os.close(scan_descriptor)
                except OSError:
                    pass
        return frozenset(names)

    def _git_failure(self, subcommand: str, exit_code: int, stderr: bytes) -> ManduaError:
        excerpt, _ = self._decode(stderr)
        evidence = Evidence(
            id="git_failure",
            kind="GitFailure",
            excerpt=bound_text(excerpt, self._limits.max_excerpt_chars),
            details={"exit_code": exit_code, "subcommand": subcommand},
        )
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "Git command failed.",
            evidence=(evidence,),
        )

    @staticmethod
    def _decode(value: bytes) -> tuple[str, bool]:
        decoded = value.decode("utf-8", errors="replace")
        return decoded, "\ufffd" in decoded

    def _missing_object_error(self, oid: str, stderr: bytes) -> ManduaError:
        excerpt, _ = self._decode(stderr)
        bounded_excerpt = bound_text(excerpt, self._limits.max_excerpt_chars)
        return ManduaError(
            ErrorCode.MISSING_OBJECT,
            f"Git object {oid} is missing or corrupt.",
            evidence=(
                Evidence(
                    id=f"missing-object:{oid}",
                    kind="missing-object",
                    oid=oid,
                    excerpt=bounded_excerpt or None,
                    details={"object_oid": oid},
                ),
            ),
            recovery="Restore the required Git object before retrying.",
        )

    def _single_tree_blob_oid(self, payload: bytes, path: PurePosixPath) -> str | None:
        if not payload:
            return None
        if not payload.endswith(b"\x00") or payload.count(b"\x00") != 1:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid tree record.")
        record = payload[:-1]
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if (
            separator != b"\t"
            or len(fields) != 3
            or fields[1] != b"blob"
            or _OBJECT_ID.fullmatch(fields[2].decode("ascii", errors="ignore")) is None
            or raw_path != os.fsencode(path.as_posix())
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid tree record.")
        return fields[2].decode("ascii").lower()

    @staticmethod
    def _diagnostic_names_object(stderr: bytes, oid: str) -> bool:
        lowered = stderr.lower()
        parsed_oids = {match.group(0).lower() for match in _DIAGNOSTIC_OBJECT_ID.finditer(stderr)}
        return oid.lower().encode("ascii") in parsed_oids and any(
            marker in lowered
            for marker in (b"bad object", b"corrupt", b"missing", b"unable to read")
        )

    def _validate_repository_path(self, path: PurePosixPath) -> None:
        rendered = path.as_posix() if isinstance(path, PurePosixPath) else ""
        if (
            not isinstance(path, PurePosixPath)
            or path.is_absolute()
            or not path.parts
            or len(rendered) > self._limits.max_input_chars
            or "\x00" in rendered
            or rendered.startswith(":")
            or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in path.parts)
        ):
            raise ManduaError(ErrorCode.INVALID_PATH, "The repository path is invalid.")
