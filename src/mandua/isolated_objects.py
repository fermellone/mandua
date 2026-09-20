"""Run object-producing Git commands privately, then publish through held directories.

Git never receives a writable path into the source object store. The child enters its
private working directory with fchdir before exec; all its writable Git paths are relative
to that directory. The source store is only a read-only Git alternate. Publication validates
Git object identities and uses descriptor-relative, no-follow filesystem operations.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import sys
import time
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from mandua.errors import ErrorCode, ManduaError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mandua.git_runner import GitRepositoryAuthority, GitRunner, _TemporaryIndexAuthority

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CHILD = (
    "import os,sys; os.fchdir(int(sys.argv[1])); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
)
_MAX_ENTRIES = 4096


class PublicationRejected(Exception):
    """The requested ref update was rejected before any ref or log write."""


def produces_objects(arguments: list[str]) -> bool:
    return arguments[0] in {
        "commit-tree",
        "merge-tree",
        "read-tree",
        "update-index",
        "write-tree",
        "update-ref",
        "merge",
        "commit",
    } or (arguments[0] == "hash-object" and "-w" in arguments)


def isolated_command(git_command: list[str], descriptor: int) -> list[str]:
    return [sys.executable, "-I", "-S", "-c", _CHILD, str(descriptor), *git_command]


def isolated_git_arguments(command: list[str] | tuple[str, ...]) -> tuple[str, ...] | None:
    """Recognize recorded launcher structure without executing report contents."""
    if not command or any(not isinstance(part, str) for part in command):
        return None
    if command[0] == "git":
        return tuple(command[1:])
    if (
        len(command) > 6
        and Path(command[0]).is_absolute()
        and re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", Path(command[0]).name)
        and list(command[1:5]) == ["-I", "-S", "-c", _CHILD]
        and re.fullmatch(r"[1-9][0-9]{0,9}", command[5])
        and command[6] == "git"
    ):
        return tuple(command[7:])
    return None


def _write(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("short private object write")
        view = view[count:]


def _read(parent: int, name: str, limit: int) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Private Git object exceeds its bound.")
        result = bytearray()
        while len(result) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not part:
                return bytes(result)
            result.extend(part)
        raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Private Git object exceeds its bound.")
    finally:
        os.close(descriptor)


def _names(descriptor: int) -> list[str]:
    names = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > _MAX_ENTRIES:
                raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Private Git namespace is too large.")
    return names


class ObjectExecution:
    def __init__(
        self,
        runner: GitRunner,
        authority: GitRepositoryAuthority,
        arguments: list[str],
        index: _TemporaryIndexAuthority | None,
    ) -> None:
        from mandua.git_runner import _PrivateDirectory

        self.runner = runner
        self.authority = authority
        self.directory = _PrivateDirectory(prefix="mandua-object-execution-")
        self.root = self.directory._root_descriptor
        self.byte_limit = runner._git_limits.max_input_bytes
        self.arguments = arguments
        self.index = index
        self.index_parent = (
            index.parent.descriptor
            if index is not None
            else authority._authority_paths[1].descriptor
        )
        self.index_before = None
        self.private_index_identity = None
        self.high_level = arguments[0] in {"merge", "commit"}
        self.writes_index = self.high_level or arguments[0] in {
            "read-tree",
            "update-index",
            "write-tree",
        }
        self.ref_name: str | None = None
        self.ref_before: bytes | None = None
        self.ref_snapshots: dict[str, bytes] = {}
        self.log_snapshots: dict[str, bytes] = {}
        self.refs_root = next(
            entry.descriptor for entry in authority._source_entries if entry.name == "refs"
        )
        self.worktree_before = {}
        self.administrative_before = {}
        self.head_before = b""
        self.shared_index_lock = None
        self.shared_ref_lock = None
        self.publication_journal = []
        self.worktree_directories = {"": authority._authority_paths[0].descriptor}
        self.created_worktree_directories = []
        self.log_locations = None
        self.owned_log_roots = []

    @contextmanager
    def _worktree_parent(self, path: str, *, capture: bool = False):
        parts = path.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Invalid worktree publication path.")
        prefix = ""
        parent = self.worktree_directories[prefix]
        for part in parts[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix not in self.worktree_directories:
                if not capture:
                    # An unknown existing directory must not become a new write authority.
                    os.mkdir(part, mode=0o755, dir_fd=parent)
                    self.created_worktree_directories.append((parent, part))
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                self.worktree_directories[prefix] = child
            child = self.worktree_directories[prefix]
            actual = os.stat(part, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(child)
            if (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino):
                raise ManduaError(ErrorCode.GIT_FAILURE, "Captured worktree directory changed.")
            parent = child
        yield parent, parts[-1]

    def prepare(self) -> None:
        for name in ("objects", "refs"):
            os.mkdir(name, mode=0o700, dir_fd=self.root)
        for name, contents in (
            ("HEAD", b"ref: refs/heads/main\n"),
            (
                "config",
                self.runner._closed_repository_config(
                    self.authority.layout.object_format,
                    log_all_ref_updates=(arguments_create_logs(self.arguments, self.authority)),
                ),
            ),
        ):
            if name == "config" and self.high_level:
                contents = contents.replace(
                    b"\tbare = false\n", b"\tbare = false\n\tworktree = worktree\n"
                )
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.root,
            )
            try:
                _write(descriptor, contents)
            finally:
                os.close(descriptor)
        if self.writes_index:
            self.index_before = self._index_snapshot()
            if self.index_before is not None:
                self._private_file("index", self.index_before[1])
                metadata = os.stat("index", dir_fd=self.root, follow_symlinks=False)
                self.private_index_identity = (metadata.st_dev, metadata.st_ino)
        self._prepare_ref()
        if self.high_level:
            self._prepare_worktree()

    @contextmanager
    def _parent(self, root: int, path: str, *, create: bool = False):
        components = path.split("/")
        if any(not part or part in {".", ".."} for part in components):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Invalid private publication path.")
        descriptor = os.dup(root)
        try:
            for component in components[:-1]:
                if create:
                    try:
                        os.mkdir(component, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor, components[-1]
        finally:
            os.close(descriptor)

    def _optional_file(self, root: int, path: str) -> bytes | None:
        try:
            with self._parent(root, path) as (parent, name):
                return _read(parent, name, self.byte_limit)
        except FileNotFoundError:
            return None

    def _nested_private_file(self, path: str, data: bytes) -> None:
        with self._parent(self.root, path, create=True) as (parent, name):
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            try:
                _write(descriptor, data)
            finally:
                os.close(descriptor)

    def _prepare_ref(self) -> None:
        if self.arguments[0] == "update-ref":
            values = self.arguments[1:]
            if values[:1] == ["--no-deref"]:
                values = values[1:]
            if len(values) not in {2, 3} or not values[0].startswith("refs/"):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED,
                    "Isolated ref writes require one explicit ref update.",
                )
            self.ref_name = values[0][5:]
        remaining = self.byte_limit
        entry_count = 0

        def copy_refs(descriptor: int, prefix: str = "", depth: int = 0):
            nonlocal remaining, entry_count
            if depth > 64:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED, "Ref namespace exceeds its depth bound."
                )
            for name in _names(descriptor):
                entry_count += 1
                if entry_count > _MAX_ENTRIES:
                    raise ManduaError(
                        ErrorCode.LIMIT_EXCEEDED, "Ref namespace exceeds its entry bound."
                    )
                path = f"{prefix}/{name}" if prefix else name
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        copy_refs(child, path, depth + 1)
                    finally:
                        os.close(child)
                else:
                    data = _read(descriptor, name, remaining)
                    remaining -= len(data)
                    self.ref_snapshots[path] = data
                    self._nested_private_file(f"refs/{path}", data)

        copy_refs(self.refs_root)
        self.ref_before = self.ref_snapshots.get(self.ref_name) if self.ref_name else None
        for entry in self.authority._source_entries:
            if entry.name in {"packed-refs", "shallow"} and entry.contents is not None:
                self._private_file(entry.name, entry.contents)
        head = _read(self.authority._authority_paths[1].descriptor, "HEAD", self.byte_limit)
        self.head_before = head
        os.unlink("HEAD", dir_fd=self.root)
        self._private_file("HEAD", head)
        if self.high_level:
            if not head.startswith(b"ref: refs/") or not head.endswith(b"\n"):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED, "Isolated integration requires a symbolic HEAD."
                )
            self.ref_name = os.fsdecode(head[len(b"ref: refs/") : -1])
            self.ref_before = self.ref_snapshots.get(self.ref_name)
        for path, parent, relative in self._log_locations() if self.ref_name else ():
            data = self._optional_file(parent, relative)
            if data is not None:
                self.log_snapshots[path] = data
                self._nested_private_file(path, data)

    def _log_locations(self):
        assert self.ref_name is not None
        if self.log_locations is None:
            common_logs = next(
                entry.descriptor for entry in self.authority._source_entries if entry.name == "logs"
            )
            if common_logs is None:
                self.log_locations = ()
            else:
                admin = self.authority._authority_paths[1].descriptor
                common = self.authority._source_parent_descriptor
                admin_stat, common_stat = os.fstat(admin), os.fstat(common)
                if (admin_stat.st_dev, admin_stat.st_ino) == (
                    common_stat.st_dev,
                    common_stat.st_ino,
                ):
                    head_logs = common_logs
                else:
                    head_logs = os.open("logs", _DIRECTORY_FLAGS, dir_fd=admin)
                    self.owned_log_roots.append((admin, head_logs))
                self.log_locations = (
                    (f"logs/refs/{self.ref_name}", common_logs, f"refs/{self.ref_name}"),
                    ("logs/HEAD", head_logs, "HEAD"),
                )
        return self.log_locations

    def _publish_ref(self) -> None:
        assert self.ref_name is not None
        next_ref = self._optional_file(self.root, f"refs/{self.ref_name}")
        if self.high_level and next_ref == self.ref_before:
            return
        if next_ref is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git did not produce the requested ref.")
        if not self.high_level:
            values = self.arguments[1:]
            if values[:1] == ["--no-deref"]:
                values = values[1:]
            if next_ref != os.fsencode(values[1]) + b"\n":
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git produced an unexpected ref value.")
        self.runner.validate_repository_authority(self.authority)
        with self._parent(self.refs_root, self.ref_name, create=True) as (parent, name):
            lock_name = name + ".lock"
            descriptor = (
                self.shared_ref_lock
                if self.shared_ref_lock is not None
                else self._ref_lock(parent, lock_name)
            )
            locked = os.fstat(descriptor)
            try:
                if self._optional_file(self.refs_root, self.ref_name) != self.ref_before:
                    if not self.high_level:
                        raise PublicationRejected("Ref changed before isolated publication.")
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Ref changed before isolated publication."
                    )
                _write(descriptor, next_ref)
                os.fsync(descriptor)
                pending_logs = []
                for path, log_root, relative in self._log_locations():
                    updated = self._optional_file(self.root, path)
                    original = self.log_snapshots.get(path)
                    if updated is None or updated == original:
                        continue
                    if (
                        not updated.startswith(original or b"")
                        or self._optional_file(log_root, relative) != original
                    ):
                        raise ManduaError(
                            ErrorCode.GIT_FAILURE, "Reflog changed before isolated publication."
                        )
                    pending_logs.append((relative, log_root, updated[len(original or b"") :]))
                self.runner.validate_repository_authority(self.authority)
                for path, log_root, appended in pending_logs:
                    with self._parent(log_root, path, create=True) as (log_parent, log_name):
                        log_descriptor = os.open(
                            log_name,
                            os.O_WRONLY | os.O_NONBLOCK | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=log_parent,
                        )
                        try:
                            _write(log_descriptor, appended)
                            os.fsync(log_descriptor)
                        finally:
                            os.close(log_descriptor)
                current = os.stat(lock_name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino):
                    raise ManduaError(ErrorCode.GIT_FAILURE, "Ref publication lock changed.")
                os.replace(lock_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                if self.shared_ref_lock is None:
                    os.close(descriptor)
                try:
                    current = os.stat(lock_name, dir_fd=parent, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino):
                        os.unlink(lock_name, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _ref_lock(self, parent: int, name: str) -> int:
        deadline = time.monotonic() + 1.0
        if self.runner._operation_budget is not None:
            deadline = min(
                deadline, time.monotonic() + self.runner._operation_budget.remaining_timeout()
            )
        while True:
            try:
                return os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise PublicationRejected(
                        "Ref publication lock is held by another writer."
                    ) from None
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def _entry(self, root: int, path: str):
        try:
            with self._parent(root, path) as (parent, name):
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    return "symlink", os.fsencode(os.readlink(name, dir_fd=parent)), 0o777
                if not stat.S_ISREG(metadata.st_mode):
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Tracked worktree entry is not a regular file or symlink.",
                    )
                return "file", _read(parent, name, self.byte_limit), stat.S_IMODE(metadata.st_mode)
        except FileNotFoundError:
            return None

    def _prepare_worktree(self) -> None:
        os.mkdir("worktree", mode=0o700, dir_fd=self.root)
        listing = self.runner.run(
            ["ls-files", "--cached", "-z"],
            repository_authority=self.authority,
        ).stdout
        if listing and not listing.endswith(b"\0"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid tracked worktree paths.")
        paths = set(listing[:-1].split(b"\0")) if listing else set()
        if len(paths) > _MAX_ENTRIES:
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Tracked worktree exceeds its path bound.")
        remaining = self.byte_limit
        source = self.authority._authority_paths[0].descriptor
        for encoded in sorted(paths):
            path = os.fsdecode(encoded)
            if any(component.casefold() == ".git" for component in path.split("/")):
                raise ManduaError(ErrorCode.GIT_FAILURE, "Invalid tracked administrative path.")
            value = self._entry(source, path)
            if value is not None:
                with self._worktree_parent(path, capture=True):
                    pass
            self.worktree_before[path] = value
            if value is None:
                continue
            kind, data, mode = value
            remaining -= len(data)
            if remaining < 0:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED, "Tracked worktree exceeds its byte bound."
                )
            private_path = f"worktree/{path}"
            if kind == "file":
                self._nested_private_file(private_path, data)
                with self._parent(self.root, private_path) as (parent, name):
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        os.fchmod(descriptor, mode)
                    finally:
                        os.close(descriptor)
            else:
                with self._parent(self.root, private_path, create=True) as (parent, name):
                    os.symlink(os.fsdecode(data), name, dir_fd=parent)
        for name in (
            "MERGE_HEAD",
            "MERGE_MSG",
            "MERGE_MODE",
            "AUTO_MERGE",
            "MERGE_AUTOSTASH",
            "ORIG_HEAD",
            "COMMIT_EDITMSG",
        ):
            value = self._entry(self.authority._authority_paths[1].descriptor, name)
            self.administrative_before[name] = value
            if value is not None:
                if value[0] != "file":
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Merge administrative entry is not a regular file."
                    )
                self._private_file(name, value[1])
        if self._index_snapshot() != self.index_before:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Index changed while capturing the worktree.")

    def _worktree_after(self):
        entries = {}
        remaining = self.byte_limit
        entry_count = 0

        def visit(descriptor: int, prefix: str = "", depth: int = 0):
            nonlocal remaining, entry_count
            if depth > 64 or len(entries) > _MAX_ENTRIES:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED, "Private worktree exceeds its path bound."
                )
            for name in _names(descriptor):
                entry_count += 1
                if entry_count > _MAX_ENTRIES:
                    raise ManduaError(
                        ErrorCode.LIMIT_EXCEEDED, "Private worktree exceeds its entry bound."
                    )
                path = f"{prefix}/{name}" if prefix else name
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        visit(child, path, depth + 1)
                    finally:
                        os.close(child)
                else:
                    value = self._entry(descriptor, name)
                    remaining -= len(value[1])
                    if remaining < 0:
                        raise ManduaError(
                            ErrorCode.LIMIT_EXCEEDED, "Private worktree exceeds its byte bound."
                        )
                    entries[path] = value

        worktree = os.open("worktree", _DIRECTORY_FLAGS, dir_fd=self.root)
        try:
            visit(worktree)
        finally:
            os.close(worktree)
        return entries

    def _replace_entry(self, root: int, path: str, value, *, expected) -> None:
        context = (
            self._worktree_parent(path)
            if root == self.authority._authority_paths[0].descriptor
            else self._parent(root, path, create=value is not None)
        )
        with context as (parent, name):
            temporary = f".mandua-publish-{uuid.uuid4().hex}"
            saved = f".mandua-save-{uuid.uuid4().hex}"
            detached = False
            detach_attempted = False
            reserved = None
            try:
                if value is not None:
                    kind, data, mode = value
                    if kind == "symlink":
                        os.symlink(os.fsdecode(data), temporary, dir_fd=parent)
                    else:
                        descriptor = os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            mode,
                            dir_fd=parent,
                        )
                        try:
                            _write(descriptor, data)
                            os.fchmod(descriptor, mode)
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                if expected is not None:
                    reservation = os.open(
                        saved,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    reserved = os.fstat(reservation)
                    os.close(reservation)
                    detach_attempted = True
                    os.replace(name, saved, src_dir_fd=parent, dst_dir_fd=parent)
                    detached = True
                    if self._entry(parent, saved) != expected:
                        raise ManduaError(
                            ErrorCode.GIT_FAILURE,
                            "Concurrent file change detected during publication.",
                        )
                if value is not None:
                    os.link(
                        temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                    )
                elif expected is None and self._entry(parent, name) is not None:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Concurrent file appeared during publication."
                    )
                # A writer holding the old inode can change it even after rename.
                # Keep its recovery name through installation and check it again
                # before discarding it; a detected edit must remain reachable.
                if detached and self._entry(parent, saved) != expected:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Concurrent file change detected after publication installation.",
                    )
            except BaseException as error:
                if detach_attempted and not detached:
                    try:
                        current = os.stat(saved, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
                            os.unlink(saved, dir_fd=parent)
                        else:
                            # Classify even when interruption follows a successful rename.
                            detached = True
                    except BaseException as classification_error:  # noqa: BLE001 - preserve uncertain original data
                        error.add_note(
                            f"Inspect {path} and {saved}; detach state is uncertain: {classification_error}"
                        )
                if detached:
                    try:
                        os.link(
                            saved, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                        )
                        os.unlink(saved, dir_fd=parent)
                        detached = False
                    except BaseException as restore_error:  # noqa: BLE001 - retain the original publication failure
                        error.add_note(
                            f"Concurrent data preserved at {path} and {saved}; inspect both: {restore_error}"
                        )
                raise
            else:
                if detached:
                    os.unlink(saved, dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _publish_worktree(self) -> None:
        after = self._worktree_after()
        source = self.authority._authority_paths[0].descriptor
        changed = [
            path
            for path in sorted(set(after) | set(self.worktree_before))
            if after.get(path) != self.worktree_before.get(path)
        ]
        for path in changed:
            if self._entry(source, path) != self.worktree_before.get(path):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Worktree changed before isolated publication."
                )
        for path in changed:
            self.publication_journal.append(
                (source, path, self.worktree_before.get(path), after.get(path))
            )
            self._replace_entry(
                source, path, after.get(path), expected=self.worktree_before.get(path)
            )

    def _publish_administrative(self) -> None:
        source = self.authority._authority_paths[1].descriptor
        pending = []
        for name, old in self.administrative_before.items():
            data = self._optional_file(self.root, name)
            new = ("file", data, old[2] if old is not None else 0o600) if data is not None else None
            if old == new:
                continue
            if self._entry(source, name) != old:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Merge state changed before isolated publication."
                )
            pending.append((name, new))
        for name, new in pending:
            old = self.administrative_before[name]
            self.publication_journal.append(
                (
                    source,
                    name,
                    old,
                    new,
                )
            )
            self._replace_entry(
                source,
                name,
                new,
                expected=self.publication_journal[-1][2],
            )

    def _private_file(self, name: str, contents: bytes) -> None:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.root
        )
        try:
            _write(descriptor, contents)
        finally:
            os.close(descriptor)

    def _index_snapshot(self):
        try:
            before = os.stat("index", dir_fd=self.index_parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        data = _read(self.index_parent, "index", self.byte_limit)
        after = os.stat("index", dir_fd=self.index_parent, follow_symlinks=False)
        identity = lambda s: (
            s.st_dev,
            s.st_ino,
            s.st_mode,
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Index changed while preparing isolated Git.")
        return identity(before), data

    def _publish_index(self) -> None:
        try:
            next_index = _read(self.root, "index", self.byte_limit)
        except FileNotFoundError:
            return
        metadata = os.stat("index", dir_fd=self.root, follow_symlinks=False)
        if (
            self.index_before is not None
            and next_index == self.index_before[1]
            and (metadata.st_dev, metadata.st_ino) == self.private_index_identity
        ):
            return
        self.runner.validate_repository_authority(self.authority)
        if self.index is not None:
            self.runner._require_index_snapshot(self.index, self.index.snapshot)
        descriptor = (
            self.shared_index_lock
            if self.shared_index_lock is not None
            else os.open(
                "index.lock",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.index_parent,
            )
        )
        locked = os.fstat(descriptor)
        try:
            _write(descriptor, next_index)
            os.fsync(descriptor)
            if self._index_snapshot() != self.index_before:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Index changed before isolated publication."
                )
            current = os.stat("index.lock", dir_fd=self.index_parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino):
                raise ManduaError(ErrorCode.GIT_FAILURE, "Index publication lock changed.")
            if self.high_level:
                old = self.index_before
                self.publication_journal.append(
                    (
                        self.index_parent,
                        "index",
                        ("file", old[1], stat.S_IMODE(old[0][2])) if old is not None else None,
                        ("file", next_index, 0o600),
                    )
                )
            os.replace(
                "index.lock", "index", src_dir_fd=self.index_parent, dst_dir_fd=self.index_parent
            )
        finally:
            if self.shared_index_lock is None:
                os.close(descriptor)
            try:
                current = os.stat("index.lock", dir_fd=self.index_parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino):
                    os.unlink("index.lock", dir_fd=self.index_parent)
            except FileNotFoundError:
                pass

    def environment(self, original: dict[str, str]) -> dict[str, str]:
        environment = original.copy()
        environment.update(
            GIT_DIR=".",
            GIT_COMMON_DIR=".",
            GIT_OBJECT_DIRECTORY="objects",
            GIT_WORK_TREE=".",
            GIT_INDEX_FILE="index",
            GIT_ALTERNATE_OBJECT_DIRECTORIES=os.fspath(self.authority.object_directory),
        )
        if self.high_level:
            for name in (
                "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
            ):
                environment.pop(name, None)
        return environment

    def command(self, git_command: list[str]) -> list[str]:
        return isolated_command(git_command, self.root)

    def refresh_index(self, options: dict) -> None:
        """Refresh copied stat data without staging changes to file contents."""
        if not self.high_level:
            return
        arguments = ["update-index", "--refresh"]
        command = self.command(self.runner._command(arguments, isolated_configuration=True))
        refresh_options = {
            **options,
            "arguments": arguments,
            "command": command,
            "input_bytes": b"",
            "check": False,
            "before_notify": None,
        }
        budget = self.runner._operation_budget
        if budget is not None:
            refresh_options["timeout_seconds"] = min(
                options["timeout_seconds"], budget.remaining_timeout()
            )
            refresh_options["max_output_bytes"] = min(
                options["max_output_bytes"],
                budget.reserve_process(self.runner._argument_bytes(command)),
            )
        output = self.runner._run_prepared_process(**refresh_options)
        if output.returncode not in {0, 1}:
            raise self.runner._git_failure(arguments[0], output.returncode, output.stderr)

    def publish(self, returncode: int) -> None:
        if self.high_level and returncode == 0:
            with self._publication_locks():
                try:
                    self._publish_success(returncode)
                except BaseException as error:
                    self._restore_unpublished(error)
                    raise
        else:
            self._publish_success(returncode)

    def _restore_unpublished(self, original: BaseException) -> None:
        try:
            current_ref = self._optional_file(self.refs_root, self.ref_name)
        except BaseException as error:  # noqa: BLE001 - preserve original control flow
            original.add_note(
                f"Publication state is uncertain; inspect the ref and worktree: {error}"
            )
            return
        if current_ref != self.ref_before:
            original.add_note(
                "The publication ref changed; inspect the applied commit, index and merge state before retrying."
            )
            return
        for root, path, before, after in reversed(self.publication_journal):
            try:
                current = self._entry(root, path)
                if current == before:
                    continue
                if current != after:
                    original.add_note(
                        f"Concurrent data at {path} was preserved; publication rollback is uncertain."
                    )
                    continue
                self._replace_entry(root, path, before, expected=after)
            except BaseException as error:  # noqa: BLE001 - continue bounded rollback
                original.add_note(f"Publication rollback for {path} was uncertain: {error}")

    @contextmanager
    def _publication_locks(self):
        from contextlib import ExitStack

        locks = []
        with ExitStack() as parents:
            try:
                self.runner.validate_repository_authority(self.authority)
                ref_parent, ref_name = parents.enter_context(
                    self._parent(self.refs_root, self.ref_name, create=True)
                )
                for parent, name, kind in (
                    (self.index_parent, "index.lock", "index"),
                    (self.authority._authority_paths[1].descriptor, "HEAD.lock", "head"),
                    (ref_parent, ref_name + ".lock", "ref"),
                    (self.authority._source_parent_descriptor, "packed-refs.lock", "packed"),
                ):
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    metadata = os.fstat(descriptor)
                    locks.append((parent, name, descriptor, metadata))
                    if kind == "index":
                        self.shared_index_lock = descriptor
                    elif kind == "ref":
                        self.shared_ref_lock = descriptor
                if self._index_snapshot() != self.index_before:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Index changed before integration publication."
                    )
                if self._optional_file(self.refs_root, self.ref_name) != self.ref_before:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Ref changed before integration publication."
                    )
                yield
            finally:
                original = sys.exception()
                cleanup_errors = []
                self.shared_index_lock = None
                self.shared_ref_lock = None
                for parent, name, descriptor, metadata in reversed(locks):
                    try:
                        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
                            os.unlink(name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
                    except BaseException as error:  # noqa: BLE001 - attempt every cleanup
                        cleanup_errors.append(error)
                    finally:
                        try:
                            os.close(descriptor)
                        except BaseException as error:  # noqa: BLE001 - attempt every cleanup
                            cleanup_errors.append(error)
                if cleanup_errors:
                    if original is not None:
                        for error in cleanup_errors:
                            original.add_note(f"Publication lock cleanup was uncertain: {error}")
                    else:
                        raise ManduaError(
                            ErrorCode.GIT_FAILURE, "Publication lock cleanup was uncertain."
                        ) from cleanup_errors[0]

    def _publish_success(self, returncode: int) -> None:
        if returncode != 0:
            return
        self.directory._validate_root_path()
        self.runner.validate_repository_authority(self.authority)
        if (
            self.high_level
            and _read(self.authority._authority_paths[1].descriptor, "HEAD", self.byte_limit)
            != self.head_before
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, "HEAD changed before isolated publication.")
        objects = os.open("objects", _DIRECTORY_FLAGS, dir_fd=self.root)
        remaining = self.byte_limit
        raw_remaining = self.byte_limit
        object_count = 0
        try:
            for prefix in _names(objects):
                if prefix in {"info", "pack"}:
                    continue
                if re.fullmatch(r"[0-9a-f]{2}", prefix) is None:
                    raise ManduaError(ErrorCode.GIT_FAILURE, "Unexpected private object namespace.")
                bucket = os.open(prefix, _DIRECTORY_FLAGS, dir_fd=objects)
                try:
                    for suffix in _names(bucket):
                        object_count += 1
                        if object_count > _MAX_ENTRIES:
                            raise ManduaError(
                                ErrorCode.LIMIT_EXCEEDED, "Private object count exceeds its bound."
                            )
                        width = 38 if self.authority.layout.object_format == "sha1" else 62
                        if re.fullmatch(rf"[0-9a-f]{{{width}}}", suffix) is None:
                            raise ManduaError(
                                ErrorCode.GIT_FAILURE, "Unexpected private object name."
                            )
                        compressed = _read(bucket, suffix, remaining)
                        remaining -= len(compressed)
                        inflater = zlib.decompressobj()
                        raw = inflater.decompress(compressed, raw_remaining + 1)
                        if len(raw) > raw_remaining or not inflater.eof or inflater.unused_data:
                            raise ManduaError(ErrorCode.GIT_FAILURE, "Invalid private Git object.")
                        raw_remaining -= len(raw)
                        digest = hashlib.new(self.authority.layout.object_format, raw).hexdigest()
                        if digest != prefix + suffix:
                            raise ManduaError(
                                ErrorCode.GIT_FAILURE, "Private Git object identity changed."
                            )
                        self._publish_object(prefix, suffix, compressed)
                finally:
                    os.close(bucket)
        finally:
            os.close(objects)
        if self.high_level:
            self._publish_worktree()
        if self.writes_index:
            self._publish_index()
        if self.ref_name is not None:
            self._publish_ref()
        if self.high_level:
            self._publish_administrative()
        self.runner.validate_repository_authority(self.authority)
        for admin, descriptor in self.owned_log_roots:
            current = os.stat("logs", dir_fd=admin, follow_symlinks=False)
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise ManduaError(ErrorCode.GIT_FAILURE, "Captured HEAD log directory changed.")

    def _publish_object(self, prefix: str, suffix: str, contents: bytes) -> None:
        root = self.authority._object_path.descriptor
        self.runner.validate_repository_authority(self.authority)
        try:
            os.mkdir(prefix, mode=0o755, dir_fd=root)
        except FileExistsError:
            pass
        parent = os.open(prefix, _DIRECTORY_FLAGS, dir_fd=root)
        temporary = f"tmp_mandua_{uuid.uuid4().hex}"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o444,
                dir_fd=parent,
            )
            try:
                _write(descriptor, contents)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(
                    temporary, suffix, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                )
            except FileExistsError:
                if _read(parent, suffix, self.byte_limit) != contents:
                    # Git may use a different zlib encoding for the same immutable object.
                    existing = zlib.decompressobj()
                    raw = existing.decompress(
                        _read(parent, suffix, self.byte_limit), self.byte_limit + 1
                    )
                    if (
                        len(raw) > self.byte_limit
                        or not existing.eof
                        or hashlib.new(self.authority.layout.object_format, raw).hexdigest()
                        != prefix + suffix
                    ):
                        raise ManduaError(
                            ErrorCode.GIT_FAILURE, "Existing Git object is inconsistent."
                        )
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent)

    def cleanup(self) -> None:
        self.directory._validate_root_path()
        entry_count = 0

        def remove_children(descriptor: int, depth: int = 0) -> None:
            nonlocal entry_count
            if depth > 68:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Unexpected private object directory depth."
                )
            for name in _names(descriptor):
                entry_count += 1
                if entry_count > _MAX_ENTRIES * 4:
                    raise ManduaError(
                        ErrorCode.LIMIT_EXCEEDED, "Private cleanup exceeds its entry bound."
                    )
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        remove_children(child, depth + 1)
                    finally:
                        os.close(child)
                    os.rmdir(name, dir_fd=descriptor)
                elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    os.unlink(name, dir_fd=descriptor)
                else:
                    raise ManduaError(ErrorCode.GIT_FAILURE, "Unexpected private object entry.")

        remove_children(self.root)
        self.directory.set_cleanup_manifest(frozenset())
        self.directory.cleanup()


@contextmanager
def isolated_objects(
    runner: GitRunner,
    authority: GitRepositoryAuthority,
    arguments: list[str],
    index: _TemporaryIndexAuthority | None,
) -> Iterator[ObjectExecution]:
    execution = ObjectExecution(runner, authority, arguments, index)
    original: BaseException | None = None
    try:
        execution.prepare()
        yield execution
    except BaseException as error:
        original = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        for parent, name in reversed(execution.created_worktree_directories):
            try:
                os.rmdir(name, dir_fd=parent)
            except OSError as error:
                # Published/concurrent files legitimately keep their directories.
                if error.errno not in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                    cleanup_errors.append(error)
            except BaseException as error:  # noqa: BLE001 - attempt every owned cleanup
                cleanup_errors.append(error)
        for path, descriptor in execution.worktree_directories.items():
            if path:
                try:
                    os.close(descriptor)
                except BaseException as error:  # noqa: BLE001 - attempt every owned cleanup
                    cleanup_errors.append(error)
        for _, descriptor in execution.owned_log_roots:
            try:
                os.close(descriptor)
            except BaseException as error:  # noqa: BLE001 - attempt every owned cleanup
                cleanup_errors.append(error)
        try:
            execution.cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001 - aggregate cleanup without masking control flow
            cleanup_errors.append(cleanup_error)
            try:
                execution.directory.abandon()
            except BaseException as error:  # noqa: BLE001 - preserve original and cleanup failures
                cleanup_errors.append(error)
        if cleanup_errors:
            if original is not None:
                for error in cleanup_errors:
                    original.add_note(f"Private object execution cleanup was uncertain: {error}")
            else:
                failure = ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Private object execution cleanup was uncertain.",
                    recovery="Inspect cleanup diagnostics and any retained private execution directory before retrying.",
                )
                for error in cleanup_errors:
                    failure.add_note(str(error))
                raise failure from cleanup_errors[0]


def arguments_create_logs(arguments: list[str], authority: GitRepositoryAuthority) -> bool:
    return arguments[0] in {"update-ref", "merge", "commit"} and any(
        entry.name == "logs" and entry.descriptor is not None for entry in authority._source_entries
    )
