"""Create semantic commits from an isolated or explicitly selected Git index."""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path, PurePosixPath

from mandua.config import load_tree_policy
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
    CheckpointRequest,
    Claim,
    CommitMetadata,
    Evidence,
    HistoryScope,
    MemoryResult,
    PlannedChange,
    QueryLimits,
    RepositoryPolicy,
)
from mandua.policy import Policy

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REGULAR_TREE_MODES = frozenset({"100644", "100755"})
_MAX_EXPLICIT_PATHS = 256
_MAX_PATH_ARGUMENT_BYTES = 65_536
_EMERGENCY_REF_TIMEOUT_SECONDS = 1.0


class _CheckpointTransactionPhase(Enum):
    IDLE = auto()
    VALIDATED = auto()
    AUTHORITY_BOUND = auto()
    FINAL_PREPARED = auto()
    LOCKS_HELD = auto()
    REF_ATTEMPTED_UNCLASSIFIED = auto()
    REF_REJECTED_PROVEN_OLD = auto()
    REF_DIVERGED_PROVEN_OTHER = auto()
    REF_UPDATED_PROVEN_NEXT = auto()
    INDEX_PROVEN_NEXT = auto()
    CLEANUP_PROVEN = auto()


class _CheckpointReconciliationState(Enum):
    OLD = auto()
    NEXT = auto()


_CHECKPOINT_PHASES_REQUIRING_RECONCILIATION = frozenset(
    {
        _CheckpointTransactionPhase.REF_ATTEMPTED_UNCLASSIFIED,
        _CheckpointTransactionPhase.REF_UPDATED_PROVEN_NEXT,
        _CheckpointTransactionPhase.INDEX_PROVEN_NEXT,
    }
)


@dataclass(frozen=True, slots=True)
class _WorktreeSnapshot:
    path: PurePosixPath
    mode: str | None
    contents: bytes | None


@dataclass(frozen=True, slots=True)
class _SelectedIndexEntry:
    path: PurePosixPath
    mode: str | None
    object_id: str | None


@dataclass(frozen=True, slots=True)
class _IndexState:
    exists: bool
    fingerprint: tuple[int, int, int, int, int, int] | None
    digest: str | None


@dataclass(frozen=True, slots=True)
class _GitControlPaths:
    git_dir: Path
    index: Path
    head: Path


@dataclass(slots=True)
class _StateLocks:
    directory_fd: int
    index_fd: int | None
    head_fd: int | None = None
    head_owned: bool = False
    index_temp_fd: int | None = None
    index_temp_name: str | None = None
    index_temp_consumed: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedCheckpoint:
    request: CheckpointRequest
    branch_ref: str
    branch: str
    parent_oid: str
    tree_oid: str
    diff_summary: str
    message: str
    policy: RepositoryPolicy
    index_state: _IndexState
    selected_entries: tuple[_SelectedIndexEntry, ...] = ()


class CheckpointWriter:
    """Prepare and apply one checkpoint without broad worktree staging."""

    def __init__(self, repository: Path, limits: QueryLimits) -> None:
        self._repository = repository
        self._limits = limits
        self._runner = GitRunner(repository, limits=limits)
        self._deadline = 0.0
        self._object_width: int | None = None
        self._control_paths: _GitControlPaths | None = None
        self._repository_authority: GitRepositoryAuthority | None = None
        self._repository_layout: _RepositoryAuthorityLayout | None = None
        self._transaction_phase = _CheckpointTransactionPhase.IDLE

    def checkpoint(self, request: CheckpointRequest, *, apply: bool = False) -> MemoryResult:
        """Return a validated preview or atomically advance the captured branch."""
        self._transaction_phase = _CheckpointTransactionPhase.IDLE
        self._deadline = time.monotonic() + self._limits.timeout_seconds
        self._validate_request(request, apply)
        self._transaction_phase = _CheckpointTransactionPhase.VALIDATED
        first = self._prepare(request)
        if not apply:
            return self._result(first, commit_oid=None, applied=False)

        authority = self._runner.open_repository_authority(None)
        self._repository_authority = authority
        self._repository_layout = authority.layout
        self._transaction_phase = _CheckpointTransactionPhase.AUTHORITY_BOUND
        try:
            second = self._prepare(request)
            self._require_same_snapshot(first, second)
            commit_oid = self._commit_tree(second)
            self._transaction_phase = _CheckpointTransactionPhase.FINAL_PREPARED
            warning = self._apply_locked(second, commit_oid, authority=authority)
        except BaseException as failure:
            if self._repository_authority is authority:
                self._repository_authority = None
                self._close_repository_authority(authority, original=failure)
            raise
        return self._result(
            second,
            commit_oid=commit_oid,
            applied=True,
            warnings=(warning,) if warning is not None else (),
        )

    def _prepare(self, request: CheckpointRequest) -> _PreparedCheckpoint:
        index_before, index_contents = self._read_real_index()
        branch_ref, branch, parent_oid = self._capture_head()
        policy = self._open_policy()
        message = policy.validate_message(
            request.subject,
            reason=request.metadata.reason,
            memory_type=request.metadata.memory_type,
            scope=request.metadata.scope,
            task_id=request.metadata.task_id,
            decision_id=request.metadata.decision_id,
            agent_id=request.metadata.agent_id,
            extra_trailers=request.metadata.extra_trailers,
        )
        self._remaining_timeout()
        selected_entries: tuple[_SelectedIndexEntry, ...] = ()
        if request.staged:
            tree_oid = self._write_staged_tree(parent_oid, index_contents)
        else:
            selected_policy = self._policy_for_remaining(policy.repository_policy)
            for path in request.paths:
                self._remaining_timeout()
                selected_policy.validate_path(path)
            snapshots = self._snapshot_explicit_selection(
                policy.repository_policy, parent_oid, request.paths
            )
            selected_policy.validate_selected_contents(
                (snapshot.path, snapshot.contents)
                for snapshot in snapshots
                if snapshot.contents is not None
            )
            self._reject_unsafe_attributes(request.paths)
            tree_oid, selected_entries = self._write_isolated_tree(parent_oid, snapshots)
        self._validate_oid(tree_oid)
        proposed_policy = load_tree_policy(
            self._runner,
            tree_oid,
            self._limits,
            timeout_seconds=self._remaining_timeout(),
            repository_authority=self._repository_authority,
        )
        if not request.staged and proposed_policy != policy.repository_policy:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The worktree policy must match the policy in the proposed checkpoint tree.",
            )
        self._policy_for_remaining(policy.repository_policy).validate_tree(tree_oid)
        self._remaining_timeout()
        if tree_oid == self._commit_tree_oid(parent_oid):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The checkpoint selection does not contain a change.",
            )
        diff_summary = self._diff_summary(parent_oid, tree_oid)
        index_after, _ = self._read_real_index()
        if index_after != index_before:
            raise self._race_error()
        return _PreparedCheckpoint(
            request=request,
            branch_ref=branch_ref,
            branch=branch,
            parent_oid=parent_oid,
            tree_oid=tree_oid,
            diff_summary=diff_summary,
            message=message,
            policy=policy.repository_policy,
            index_state=index_before,
            selected_entries=selected_entries,
        )

    def _write_isolated_tree(
        self, parent_oid: str, snapshots: tuple[_WorktreeSnapshot, ...]
    ) -> tuple[str, tuple[_SelectedIndexEntry, ...]]:
        try:
            with self._runner.temporary_index() as index_file:
                read_tree = self._run_index(["read-tree", parent_oid], index_file=index_file)
                self._require_silent_success(
                    read_tree, "Git returned invalid isolated read-tree output."
                )
                entries = self._hash_snapshots(snapshots)
                update = self._run_index(
                    ["update-index", "-z", "--index-info"],
                    index_file=index_file,
                    input_bytes=self._index_info(entries),
                )
                self._require_silent_success(
                    update, "Git returned invalid isolated index-update output."
                )
                written = self._run_index(["write-tree"], index_file=index_file)
                tree_oid = self._strict_stdout_oid(
                    written, "Git returned an invalid isolated tree ID."
                )
                return tree_oid, entries
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The isolated Git index could not be created."
            ) from None

    def _write_staged_tree(self, parent_oid: str, index_contents: bytes | None) -> str:
        try:
            with self._runner.temporary_index() as index_file:
                if index_contents is None:
                    read_tree = self._run_index(["read-tree", parent_oid], index_file=index_file)
                    self._require_silent_success(
                        read_tree, "Git returned invalid staged-index seed output."
                    )
                else:
                    self._seed_private_index(index_file, index_contents)
                output = self._run_index(["write-tree"], index_file=index_file)
                return self._strict_stdout_oid(output, "Git returned an invalid staged tree ID.")
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The staged Git index could not be inspected."
            ) from None

    def _hash_snapshots(
        self, snapshots: tuple[_WorktreeSnapshot, ...]
    ) -> tuple[_SelectedIndexEntry, ...]:
        entries: list[_SelectedIndexEntry] = []
        for snapshot in snapshots:
            self._remaining_timeout()
            if snapshot.contents is None:
                entries.append(
                    _SelectedIndexEntry(
                        path=snapshot.path,
                        mode=None,
                        object_id=None,
                    )
                )
                continue
            output = self._run_text(
                ["hash-object", "-w", "--stdin"],
                input_bytes=snapshot.contents,
                timeout_seconds=self._remaining_timeout(),
            )
            object_id = self._strict_stdout_oid(output, "Git returned an invalid selected blob ID.")
            entries.append(
                _SelectedIndexEntry(
                    path=snapshot.path,
                    mode=snapshot.mode,
                    object_id=object_id,
                )
            )
        return tuple(entries)

    def _index_info(self, entries: tuple[_SelectedIndexEntry, ...]) -> bytes:
        records: list[bytes] = []
        zero_oid = b"0" * self._object_id_width()
        for entry in entries:
            self._remaining_timeout()
            raw_path = entry.path.as_posix().encode("utf-8")
            if entry.mode is None:
                records.append(b"0 " + zero_oid + b"\t" + raw_path + b"\x00")
                continue
            if entry.mode not in _REGULAR_TREE_MODES or entry.object_id is None:
                raise ManduaError(ErrorCode.GIT_FAILURE, "The selected index entry is invalid.")
            object_id = self._validate_oid(entry.object_id).encode("ascii")
            records.append(
                entry.mode.encode("ascii") + b" " + object_id + b"\t" + raw_path + b"\x00"
            )
        payload = b"".join(records)
        if len(payload) > self._limits.max_output_bytes:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED, "Selected index records exceeded the byte limit."
            )
        return payload

    def _commit_tree(self, prepared: _PreparedCheckpoint) -> str:
        message = prepared.message.encode("utf-8")
        output = self._run_text(
            [
                "commit-tree",
                prepared.tree_oid,
                "-p",
                prepared.parent_oid,
                "-F",
                "-",
            ],
            input_bytes=message,
            timeout_seconds=self._remaining_timeout(),
        )
        return self._strict_stdout_oid(output, "Git returned an invalid checkpoint commit ID.")

    def _apply_locked(
        self,
        prepared: _PreparedCheckpoint,
        commit_oid: str,
        *,
        authority: GitRepositoryAuthority,
    ) -> str | None:
        current: _PreparedCheckpoint | None = None
        old_index: bytes | None = None
        next_index: bytes | None = None
        try:
            with self._locked_repository_state() as locks:
                self._transaction_phase = _CheckpointTransactionPhase.LOCKS_HELD
                current = self._prepare(prepared.request)
                self._require_same_snapshot(prepared, current)
                index_state, old_index = self._read_real_index()
                if index_state != current.index_state:
                    raise self._race_error()
                if not prepared.request.staged:
                    next_index = self._build_next_index(current, old_index)
                    self._stage_locked_index(locks, next_index)
                self._require_locked_state(current)
                self._transaction_phase = _CheckpointTransactionPhase.REF_ATTEMPTED_UNCLASSIFIED
                self._update_ref(current, commit_oid)
                self._acquire_head_lock(locks)
                self._require_post_ref_state(current, commit_oid)
                self._transaction_phase = _CheckpointTransactionPhase.REF_UPDATED_PROVEN_NEXT
                if next_index is not None:
                    self._install_locked_index(locks, next_index)
                self._transaction_phase = _CheckpointTransactionPhase.INDEX_PROVEN_NEXT
            self._transaction_phase = _CheckpointTransactionPhase.CLEANUP_PROVEN
        except BaseException as failure:
            if (
                current is not None
                and self._transaction_phase in _CHECKPOINT_PHASES_REQUIRING_RECONCILIATION
            ):
                try:
                    self._reconcile_ref_exception(
                        current,
                        commit_oid,
                        failure,
                        old_index=old_index,
                        next_index=next_index,
                    )
                except BaseException as reconciled_error:
                    self._repository_authority = None
                    self._close_repository_authority(authority, original=reconciled_error)
                    raise
            self._repository_authority = None
            self._close_repository_authority(authority, original=failure)
            raise

        self._repository_authority = None
        try:
            authority.cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup follows mutation
            assert current is not None
            reconciled = self._reconcile_ref_exception(
                current,
                commit_oid,
                cleanup_error,
                old_index=old_index,
                next_index=next_index,
                allow_verified_applied_cleanup=True,
            )
            if reconciled is _CheckpointReconciliationState.NEXT:
                return (
                    "The checkpoint was applied, but private Git authority cleanup was "
                    f"uncertain; inspect {current.branch_ref} and the index before another "
                    "checkpoint."
                )
        return None

    def _update_ref(self, prepared: _PreparedCheckpoint, commit_oid: str) -> None:
        output = self._run_text(
            [
                "update-ref",
                "--no-deref",
                prepared.branch_ref,
                commit_oid,
                prepared.parent_oid,
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode == 0 and not output.stdout and not output.stderr:
            return
        if output.returncode == 0:
            try:
                actual_oid = self._read_ref_oid(
                    prepared.branch_ref, timeout_seconds=self._remaining_timeout()
                )
            except ManduaError:
                raise self._uncertain_ref_failure(prepared) from None
            if actual_oid not in (commit_oid, prepared.parent_oid):
                self._transaction_phase = _CheckpointTransactionPhase.REF_DIVERGED_PROVEN_OTHER
                raise self._race_error()
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid ref-update output.")
        try:
            actual_oid = self._read_ref_oid(
                prepared.branch_ref, timeout_seconds=self._remaining_timeout()
            )
        except ManduaError:
            raise self._uncertain_ref_failure(prepared) from None
        if actual_oid == commit_oid:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned invalid ref-update failure output."
            )
        if actual_oid != prepared.parent_oid:
            self._transaction_phase = _CheckpointTransactionPhase.REF_DIVERGED_PROVEN_OTHER
            raise self._race_error()
        try:
            branch_ref, _, head_oid = self._capture_head()
        except ManduaError:
            raise self._race_error() from None
        if branch_ref != prepared.branch_ref or head_oid != prepared.parent_oid:
            raise self._race_error()
        index_state, _ = self._read_real_index()
        if index_state != prepared.index_state:
            raise self._uncertain_ref_failure(prepared)
        self._transaction_phase = _CheckpointTransactionPhase.REF_REJECTED_PROVEN_OLD
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not update the checkpoint branch.")

    def _reconcile_ref_exception(
        self,
        prepared: _PreparedCheckpoint,
        commit_oid: str,
        original: BaseException,
        *,
        old_index: bytes | None,
        next_index: bytes | None,
        allow_verified_applied_cleanup: bool = False,
    ) -> _CheckpointReconciliationState:
        interruption: BaseException | None = None
        for _ in range(2):
            try:
                with self._emergency_ref_resources(prepared, commit_oid) as deadline:
                    reconciled = self._reconcile_interrupted_ref_update(
                        prepared,
                        commit_oid,
                        old_index=old_index,
                        next_index=next_index,
                        emergency_deadline=deadline,
                    )
            except BaseException as error:  # noqa: BLE001 - emergency control is classified
                if error is original:
                    if interruption is not None:
                        raise self._uncertain_ref_failure(prepared) from original
                    interruption = error
                    continue
                if isinstance(error, ManduaError):
                    uncertainty = self._uncertain_ref_failure(prepared)
                    if interruption is not None:
                        raise uncertainty from interruption
                    raise uncertainty from original
                if interruption is not None:
                    raise self._uncertain_ref_failure(prepared) from interruption
                interruption = error
                continue
            if interruption is not None:
                raise interruption
            if (
                allow_verified_applied_cleanup
                and reconciled is _CheckpointReconciliationState.NEXT
                and isinstance(original, ManduaError)
            ):
                return reconciled
            raise original
        raise self._uncertain_ref_failure(prepared) from interruption

    @contextmanager
    def _emergency_ref_resources(
        self,
        prepared: _PreparedCheckpoint,
        commit_oid: str,
    ) -> Iterator[float]:
        timeout = min(float(self._limits.timeout_seconds), _EMERGENCY_REF_TIMEOUT_SECONDS)
        limits = replace(self._limits, timeout_seconds=timeout)
        if self._repository_layout is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The captured checkpoint repository authority is unavailable.",
            )
        read_arguments = (
            "rev-parse",
            "--verify",
            "--end-of-options",
            prepared.branch_ref,
        )
        private_limits = self._runner.plan_repository_reopen_budget(
            self._repository_layout,
            (
                read_arguments,
                (
                    "update-ref",
                    "--no-deref",
                    prepared.branch_ref,
                    prepared.parent_oid,
                    commit_oid,
                ),
                read_arguments,
            ),
            expected_output_bytes=2 * (self._object_id_width() + 1),
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

    def _reconcile_interrupted_ref_update(
        self,
        prepared: _PreparedCheckpoint,
        commit_oid: str,
        *,
        old_index: bytes | None,
        next_index: bytes | None,
        emergency_deadline: float,
    ) -> _CheckpointReconciliationState:
        # Recovery outlives the original transaction locks. Exclude cooperative
        # index writers throughout classification and any compensating ref update.
        with self._locked_repository_state():
            actual_oid = self._read_ref_oid(
                prepared.branch_ref,
                timeout_seconds=self._remaining_emergency_timeout(emergency_deadline),
            )
            _, actual_index = self._read_real_index(emergency_deadline=emergency_deadline)
            expected_next_index = old_index if next_index is None else next_index
            index_is_next = actual_index == expected_next_index
            index_is_old = actual_index == old_index
            if actual_oid == prepared.parent_oid and index_is_old:
                return _CheckpointReconciliationState.OLD
            if actual_oid != commit_oid:
                raise self._uncertain_ref_failure(prepared)
            if index_is_next:
                return _CheckpointReconciliationState.NEXT
            if not index_is_old:
                raise self._uncertain_ref_failure(prepared)
            self._rollback_ref(
                prepared,
                commit_oid,
                emergency_deadline=emergency_deadline,
            )
            _, confirmed_index = self._read_real_index(emergency_deadline=emergency_deadline)
            if confirmed_index != old_index:
                raise self._uncertain_ref_failure(prepared)
            return _CheckpointReconciliationState.OLD

    def _rollback_ref(
        self,
        prepared: _PreparedCheckpoint,
        commit_oid: str,
        *,
        emergency_deadline: float | None = None,
    ) -> None:
        deadline = (
            self._emergency_ref_deadline() if emergency_deadline is None else emergency_deadline
        )
        try:
            output = self._run_text(
                [
                    "update-ref",
                    "--no-deref",
                    prepared.branch_ref,
                    prepared.parent_oid,
                    commit_oid,
                ],
                check=False,
                timeout_seconds=self._remaining_emergency_timeout(deadline),
            )
        except ManduaError:
            raise self._rollback_failure(prepared) from None
        if output.returncode != 0 or output.stdout or output.stderr:
            raise self._rollback_failure(prepared)
        try:
            actual_oid = self._read_ref_oid(
                prepared.branch_ref,
                timeout_seconds=self._remaining_emergency_timeout(deadline),
            )
        except ManduaError:
            raise self._rollback_failure(prepared) from None
        if actual_oid != prepared.parent_oid:
            raise self._rollback_failure(prepared)

    def _emergency_ref_deadline(self) -> float:
        return time.monotonic() + min(
            float(self._limits.timeout_seconds), _EMERGENCY_REF_TIMEOUT_SECONDS
        )

    @staticmethod
    def _remaining_emergency_timeout(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Checkpoint ref recovery exceeded the emergency time limit.",
            )
        return remaining

    @staticmethod
    def _close_repository_authority(
        authority: GitRepositoryAuthority,
        *,
        original: BaseException,
    ) -> None:
        """Close authority descriptors without masking the reconciled failure."""
        try:
            authority.cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001 - closure is mandatory
            original.add_note(
                "Checkpoint repository-authority cleanup also failed: "
                f"{type(cleanup_error).__name__}."
            )

    def _read_ref_oid(self, branch_ref: str, *, timeout_seconds: float) -> str:
        output = self._run_text(
            ["rev-parse", "--verify", "--end-of-options", branch_ref],
            check=False,
            timeout_seconds=timeout_seconds,
        )
        return self._strict_stdout_oid(output, "Git returned an invalid branch ref value.")

    def _require_locked_state(self, prepared: _PreparedCheckpoint) -> None:
        branch_ref, _, head_oid = self._capture_head()
        index_state, _ = self._read_real_index()
        if (
            branch_ref != prepared.branch_ref
            or head_oid != prepared.parent_oid
            or index_state != prepared.index_state
        ):
            raise self._race_error()

    def _require_post_ref_state(self, prepared: _PreparedCheckpoint, commit_oid: str) -> None:
        branch_ref, _, head_oid = self._capture_head()
        index_state, _ = self._read_real_index()
        if (
            branch_ref != prepared.branch_ref
            or head_oid != commit_oid
            or index_state != prepared.index_state
        ):
            raise self._race_error()

    def _build_next_index(
        self, prepared: _PreparedCheckpoint, current_contents: bytes | None
    ) -> bytes:
        try:
            with self._runner.temporary_index() as index_file:
                if current_contents is None:
                    read_tree = self._run_index(
                        ["read-tree", prepared.parent_oid], index_file=index_file
                    )
                    self._require_silent_success(
                        read_tree, "Git returned invalid real-index seed output."
                    )
                else:
                    self._seed_private_index(index_file, current_contents)
                update = self._run_index(
                    ["update-index", "-z", "--index-info"],
                    index_file=index_file,
                    input_bytes=self._index_info(prepared.selected_entries),
                )
                self._require_silent_success(
                    update, "Git returned invalid selected-index synchronization output."
                )
                return self._read_private_index(index_file)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The next Git index could not be prepared."
            ) from None

    def _seed_private_index(self, index_file: GitIndexFile, contents: bytes) -> None:
        self._runner.seed_temporary_index(index_file, contents)

    def _read_private_index(self, index_file: GitIndexFile) -> bytes:
        return self._runner.read_temporary_index(index_file)

    def _stage_locked_index(self, locks: _StateLocks, contents: bytes) -> None:
        if locks.index_fd is None or locks.index_temp_fd is not None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The Git index lock is unavailable.")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            for _ in range(8):
                self._remaining_timeout()
                name = f".mandua-index-{secrets.token_hex(16)}.tmp"
                try:
                    descriptor = os.open(name, flags, 0o600, dir_fd=locks.directory_fd)
                except FileExistsError:
                    continue
                locks.index_temp_fd = descriptor
                locks.index_temp_name = name
                break
            else:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "A private next-index file could not be allocated."
                )
            self._write_descriptor(locks.index_temp_fd, contents)
            os.fsync(locks.index_temp_fd)
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The next Git index could not be staged atomically."
            ) from None

    def _install_locked_index(self, locks: _StateLocks, contents: bytes) -> None:
        self._remaining_timeout()
        if locks.index_fd is None or locks.index_temp_fd is None or locks.index_temp_name is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The Git index lock is unavailable.")
        try:
            before = os.fstat(locks.index_temp_fd)
            os.lseek(locks.index_temp_fd, 0, os.SEEK_SET)
            captured = self._read_descriptor(
                locks.index_temp_fd, self._limits.max_output_bytes, policy_error=False
            )
            after = os.fstat(locks.index_temp_fd)
            if (
                self._file_fingerprint(before) != self._file_fingerprint(after)
                or len(captured) != after.st_size
                or captured != contents
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "The staged Git index changed before installation."
                )
            os.close(locks.index_temp_fd)
            locks.index_temp_fd = None
            os.replace(
                locks.index_temp_name,
                "index",
                src_dir_fd=locks.directory_fd,
                dst_dir_fd=locks.directory_fd,
            )
            locks.index_temp_consumed = True
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The checkpoint ref moved but the prepared Git index could not be installed.",
                recovery="Inspect the branch and index before retrying the checkpoint.",
            ) from None

    @contextmanager
    def _locked_repository_state(self) -> Iterator[_StateLocks]:
        paths = self._git_control_paths()
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        lock_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = -1
        index_fd: int | None = None
        owns_index = False
        locks: _StateLocks | None = None
        body_error: BaseException | None = None
        try:
            directory_fd = os.open(paths.git_dir, directory_flags)
            self._require_bound_directory_identity(directory_fd, worktree=False)
            try:
                index_fd = os.open("index.lock", lock_flags, 0o600, dir_fd=directory_fd)
                owns_index = True
            except FileExistsError:
                raise self._race_error() from None
            locks = _StateLocks(
                directory_fd=directory_fd,
                index_fd=index_fd,
            )
            try:
                yield locks
                self._validate_bound_repository_authority()
            except BaseException as error:
                body_error = error
                raise
        except ManduaError:
            raise
        except OSError:
            if body_error is not None:
                raise
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The repository state locks could not be acquired."
            ) from None
        finally:
            cleanup_errors: list[BaseException] = []
            head_fd = locks.head_fd if locks is not None else None
            temp_fd = locks.index_temp_fd if locks is not None else None
            if locks is not None:
                index_fd = locks.index_fd
            for descriptor in (head_fd, temp_fd, index_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException as error:  # noqa: BLE001 - try every owned cleanup
                        cleanup_errors.append(error)
            if directory_fd >= 0:
                if locks is not None and locks.head_owned:
                    self._record_lock_unlink_cleanup(
                        directory_fd,
                        "HEAD.lock",
                        cleanup_errors,
                    )
                if (
                    locks is not None
                    and locks.index_temp_name is not None
                    and not locks.index_temp_consumed
                ):
                    self._record_lock_unlink_cleanup(
                        directory_fd,
                        locks.index_temp_name,
                        cleanup_errors,
                    )
                if owns_index:
                    self._record_lock_unlink_cleanup(
                        directory_fd,
                        "index.lock",
                        cleanup_errors,
                    )
                try:
                    os.close(directory_fd)
                except BaseException as error:  # noqa: BLE001 - cleanup evidence is retained
                    cleanup_errors.append(error)
            if cleanup_errors:
                cleanup_failure = ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Repository transaction locks could not be cleaned up.",
                    recovery="Inspect the worktree Git lock files before retrying.",
                )
                if body_error is not None:
                    body_error.add_note(
                        "Checkpoint transaction cleanup is uncertain after lock cleanup failed; "
                        "inspect the worktree Git lock files before retrying or changing refs."
                    )
                else:
                    raise cleanup_failure from cleanup_errors[0]

    def _acquire_head_lock(self, locks: _StateLocks) -> None:
        if locks.head_fd is not None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "The HEAD lock is already active.")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            locks.head_fd = os.open("HEAD.lock", flags, 0o600, dir_fd=locks.directory_fd)
            locks.head_owned = True
        except FileExistsError:
            raise self._race_error() from None
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The per-worktree HEAD lock could not be acquired."
            ) from None

    def _release_head_lock(self, locks: _StateLocks) -> None:
        if locks.head_fd is None:
            return
        try:
            os.close(locks.head_fd)
            locks.head_fd = None
            os.unlink("HEAD.lock", dir_fd=locks.directory_fd)
            locks.head_owned = False
        except OSError:
            locks.head_fd = None
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The checkpoint ref moved but the HEAD lock could not be released.",
                recovery="Inspect the branch and worktree Git locks before retrying.",
            ) from None

    @staticmethod
    def _unlink_owned_lock(directory_fd: int, name: str) -> bool:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _record_lock_unlink_cleanup(
        self,
        directory_fd: int,
        name: str,
        cleanup_errors: list[BaseException],
    ) -> None:
        try:
            if not self._unlink_owned_lock(directory_fd, name):
                cleanup_errors.append(OSError(f"{name} cleanup was not confirmed"))
        except BaseException as error:  # noqa: BLE001 - later lock cleanup must still run
            cleanup_errors.append(error)

    def _git_control_paths(self) -> _GitControlPaths:
        self._validate_bound_repository_authority()
        if self._control_paths is not None:
            if (
                self._repository_authority is not None
                and self._control_paths.git_dir != self._repository_authority.git_directory
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "The bound per-worktree Git directory changed during checkpointing.",
                )
            return self._control_paths
        if self._repository_authority is not None:
            git_dir = self._repository_authority.git_directory
            self._control_paths = _GitControlPaths(
                git_dir=git_dir,
                index=git_dir / "index",
                head=git_dir / "HEAD",
            )
            return self._control_paths
        git_dir = self._strict_absolute_git_path(
            self._run_text(
                ["rev-parse", "--absolute-git-dir"],
                check=False,
                timeout_seconds=self._remaining_timeout(),
            ),
            "Git returned an invalid per-worktree directory.",
        )
        try:
            resolved_git_dir = git_dir.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The per-worktree Git directory is unavailable."
            ) from None
        if not resolved_git_dir.is_dir():
            raise ManduaError(ErrorCode.GIT_FAILURE, "The per-worktree Git directory is invalid.")
        index = self._resolve_control_path(resolved_git_dir, "index")
        head = self._resolve_control_path(resolved_git_dir, "HEAD")
        self._control_paths = _GitControlPaths(
            git_dir=resolved_git_dir,
            index=index,
            head=head,
        )
        return self._control_paths

    def _validate_bound_repository_authority(self) -> None:
        if self._repository_authority is not None:
            self._runner.validate_repository_authority(self._repository_authority)

    def _require_bound_directory_identity(
        self,
        descriptor: int,
        *,
        worktree: bool,
    ) -> None:
        authority = self._repository_authority
        if authority is None:
            return
        self._runner.validate_repository_authority(authority)
        expected = (
            authority.layout.worktree_identity
            if worktree
            else authority.layout.git_directory_identity
        )
        try:
            observed = os.fstat(descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "A bound repository directory could not be verified.",
            ) from None
        if (
            observed.st_dev != expected.device
            or observed.st_ino != expected.inode
            or stat.S_IFMT(observed.st_mode) != expected.file_type
        ):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "A bound repository directory changed during checkpointing.",
            )

    def _resolve_control_path(self, git_dir: Path, name: str) -> Path:
        path = self._strict_absolute_git_path(
            self._run_text(
                ["rev-parse", "--path-format=absolute", "--git-path", name],
                check=False,
                timeout_seconds=self._remaining_timeout(),
            ),
            "Git returned an invalid per-worktree control path.",
        )
        try:
            parent = path.parent.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "A per-worktree Git control path is unavailable."
            ) from None
        if path.name != name or parent != git_dir or path.is_symlink():
            raise ManduaError(ErrorCode.GIT_FAILURE, "A per-worktree Git control path is invalid.")
        return git_dir / name

    @staticmethod
    def _strict_absolute_git_path(output: GitOutput[str], message: str) -> Path:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n" or "\x00" in lines[0]:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        path = Path(lines[0])
        if not path.is_absolute():
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        return path

    def _read_real_index(
        self, *, emergency_deadline: float | None = None
    ) -> tuple[_IndexState, bytes | None]:
        paths = self._git_control_paths()
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = -1
        descriptor: int | None = None
        try:
            directory_fd = os.open(paths.git_dir, directory_flags)
            self._require_bound_directory_identity(directory_fd, worktree=False)
            try:
                descriptor = os.open("index", read_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                self._validate_bound_repository_authority()
                return _IndexState(exists=False, fingerprint=None, digest=None), None
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ManduaError(ErrorCode.GIT_FAILURE, "The real Git index is invalid.")
            contents = self._read_descriptor(
                descriptor,
                self._limits.max_output_bytes,
                policy_error=False,
                emergency_deadline=emergency_deadline,
            )
            after = os.fstat(descriptor)
            self._validate_bound_repository_authority()
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The real Git index could not be read safely."
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory_fd >= 0:
                os.close(directory_fd)
        if (
            self._file_fingerprint(before) != self._file_fingerprint(after)
            or len(contents) != after.st_size
        ):
            raise self._race_error()
        state = _IndexState(
            exists=True,
            fingerprint=self._file_fingerprint(after),
            digest=hashlib.sha256(contents).hexdigest(),
        )
        return state, contents

    def _read_descriptor(
        self,
        descriptor: int,
        maximum: int,
        *,
        policy_error: bool,
        emergency_deadline: float | None = None,
    ) -> bytes:
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            if emergency_deadline is None:
                self._remaining_timeout()
            else:
                self._remaining_emergency_timeout(emergency_deadline)
            try:
                chunk = os.read(descriptor, min(65_536, remaining))
            except OSError:
                code = ErrorCode.POLICY_VIOLATION if policy_error else ErrorCode.GIT_FAILURE
                raise ManduaError(code, "A bounded file read failed.") from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > maximum:
            if policy_error:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "A selected file exceeds the configured byte limit.",
                )
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED, "The Git index exceeded the configured byte limit."
            )
        return contents

    def _write_descriptor(self, descriptor: int, contents: bytes) -> None:
        offset = 0
        while offset < len(contents):
            self._remaining_timeout()
            written = os.write(descriptor, contents[offset : offset + 65_536])
            if written <= 0:
                raise ManduaError(ErrorCode.GIT_FAILURE, "A bounded file write failed.")
            offset += written

    @staticmethod
    def _file_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def _capture_head(self) -> tuple[str, str, str]:
        first = self._run_text(
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if first.returncode == 1:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "A checkpoint requires HEAD to name a local branch.",
            )
        branch_ref = self._strict_branch_ref(first)
        head = self._run_text(
            ["rev-parse", "--verify", "--end-of-options", branch_ref],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if head.returncode != 0:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "A checkpoint requires a branch with an existing commit.",
            )
        parent_oid = self._strict_stdout_oid(head, "Git returned an invalid branch tip.")
        self._require_commit_object(parent_oid)
        second = self._run_text(
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if second.returncode != 0 or self._strict_branch_ref(second) != branch_ref:
            raise self._race_error()
        return branch_ref, branch_ref.removeprefix("refs/heads/"), parent_oid

    def _require_commit_object(self, object_id: str) -> None:
        output = self._run_text(
            ["cat-file", "-t", object_id],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode == 0 and not output.stderr and output.stdout == "commit\n":
            return
        if (
            output.returncode == 0
            and not output.stderr
            and output.stdout
            in {
                "blob\n",
                "tag\n",
                "tree\n",
            }
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "A checkpoint branch must point directly to a commit.",
            )
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify the branch tip object.")

    def _open_policy(self) -> Policy:
        return Policy.open(
            self._repository,
            limits=replace(self._limits, timeout_seconds=self._remaining_timeout()),
            runner=self._runner,
            repository_authority=self._repository_authority,
        )

    def _snapshot_explicit_selection(
        self,
        repository_policy: RepositoryPolicy,
        parent_oid: str,
        paths: tuple[PurePosixPath, ...],
    ) -> tuple[_WorktreeSnapshot, ...]:
        snapshots: list[_WorktreeSnapshot] = []
        total = 0
        for path in paths:
            self._remaining_timeout()
            entry = self._tree_entry(parent_oid, path)
            contents = self._read_selected_file(
                path,
                maximum=repository_policy.max_file_bytes,
                aggregate_remaining=self._limits.max_output_bytes - total,
            )
            if contents is None:
                if not self._is_regular_tree_entry(entry):
                    raise ManduaError(
                        ErrorCode.VALIDATION_FAILED,
                        "A missing checkpoint path must name one tracked regular file.",
                    )
                snapshots.append(_WorktreeSnapshot(path=path, mode=None, contents=None))
                continue
            if entry is not None and not self._is_regular_tree_entry(entry):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED,
                    "A checkpoint file cannot replace a tracked non-file entry.",
                )
            mode, raw_contents = contents
            total += len(raw_contents)
            if total > self._limits.max_output_bytes:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Selected files exceed the configured aggregate byte limit.",
                )
            snapshots.append(_WorktreeSnapshot(path=path, mode=mode, contents=raw_contents))
        return tuple(snapshots)

    @staticmethod
    def _is_regular_tree_entry(entry: tuple[str, str, str] | None) -> bool:
        return entry is not None and entry[0] in _REGULAR_TREE_MODES and entry[1] == "blob"

    def _read_selected_file(
        self, path: PurePosixPath, *, maximum: int, aggregate_remaining: int
    ) -> tuple[str, bytes] | None:
        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = read_flags | getattr(os, "O_DIRECTORY", 0)
        descriptors: list[int] = []
        try:
            descriptors.append(os.open(self._repository, directory_flags))
            self._require_bound_directory_identity(descriptors[0], worktree=True)
            for component in path.parts[:-1]:
                self._remaining_timeout()
                try:
                    descriptors.append(os.open(component, directory_flags, dir_fd=descriptors[-1]))
                except FileNotFoundError:
                    return None
                except OSError:
                    raise ManduaError(
                        ErrorCode.POLICY_VIOLATION,
                        "A checkpoint path cannot be inspected safely.",
                    ) from None
            try:
                descriptor = os.open(path.parts[-1], read_flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                return None
            except OSError:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "A checkpoint file cannot be opened safely.",
                ) from None
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Checkpoint paths must name regular files.",
                )
            if before.st_size > maximum:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "A selected file exceeds the configured size limit.",
                )
            if before.st_size > aggregate_remaining:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Selected files exceed the configured aggregate byte limit.",
                )
            contents = self._read_descriptor(
                descriptor, min(maximum, aggregate_remaining), policy_error=True
            )
            after = os.fstat(descriptor)
            if (
                self._file_fingerprint(before) != self._file_fingerprint(after)
                or len(contents) != after.st_size
            ):
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "A selected file changed while it was read.",
                )
            self._validate_bound_repository_authority()
            mode = "100755" if before.st_mode & 0o111 else "100644"
            return mode, contents
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "A checkpoint path cannot be inspected safely.",
            ) from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _tree_entry(self, treeish: str, path: PurePosixPath) -> tuple[str, str, str] | None:
        output = self._run(
            ["ls-tree", "-z", treeish, "--", path.as_posix()],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            literal_pathspecs=True,
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not inspect a checkpoint path.")
        if not output.stdout:
            return None
        if not output.stdout.endswith(b"\x00"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid tree entry.")
        records = output.stdout[:-1].split(b"\x00")
        if len(records) != 1 or records[0].count(b"\t") != 1:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid tree entry.")
        header, raw_path = records[0].split(b"\t", 1)
        try:
            mode, object_type, object_id = header.decode("ascii").split(" ")
            expected_path = path.as_posix().encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid tree entry."
            ) from None
        if raw_path != expected_path or _OBJECT_ID.fullmatch(object_id) is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid tree entry.")
        self._validate_oid(object_id)
        return mode, object_type, object_id

    def _reject_unsafe_attributes(self, paths: tuple[PurePosixPath, ...]) -> None:
        output = self._run(
            ["check-attr", "-z", "--all", "--", *(path.as_posix() for path in paths)],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            literal_pathspecs=True,
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not inspect checkpoint attributes.")
        if not output.stdout:
            return
        if not output.stdout.endswith(b"\x00"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid attribute records.")
        fields = output.stdout[:-1].split(b"\x00")
        if len(fields) % 3:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid attribute records.")
        selected = {path.as_posix().encode("utf-8") for path in paths}
        for index in range(0, len(fields), 3):
            raw_path, raw_attribute, raw_value = fields[index : index + 3]
            try:
                attribute = raw_attribute.decode("ascii")
                value = raw_value.decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned invalid attribute records."
                ) from None
            if raw_path not in selected or not attribute or "\x00" in value:
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid attribute records.")
            if attribute in {"filter", "working-tree-encoding"}:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Checkpoint paths must not use content filters or working-tree encoding.",
                )

    @staticmethod
    def _rollback_failure(prepared: _PreparedCheckpoint) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "The checkpoint ref moved but index synchronization and rollback both failed.",
            recovery=f"Inspect {prepared.branch_ref} and the index before retrying the checkpoint.",
        )

    @staticmethod
    def _uncertain_ref_failure(prepared: _PreparedCheckpoint) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "Git returned an invalid ref-update result and the branch state is uncertain.",
            recovery=f"Inspect {prepared.branch_ref} and the index before retrying the checkpoint.",
        )

    def _commit_tree_oid(self, commit_oid: str) -> str:
        output = self._run_text(
            ["rev-parse", "--verify", "--end-of-options", f"{commit_oid}^{{tree}}"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        return self._strict_stdout_oid(output, "Git returned an invalid parent tree ID.")

    def _policy_for_remaining(self, repository_policy: RepositoryPolicy) -> Policy:
        limits = replace(self._limits, timeout_seconds=self._remaining_timeout())
        return Policy(
            self._repository,
            self._runner,
            repository_policy,
            limits,
            repository_authority=self._repository_authority,
        )

    def _diff_summary(self, parent_oid: str, tree_oid: str) -> str:
        result = self._run_text(
            [
                "diff",
                f"--stat=80,80,{self._limits.max_commits}",
                "--no-ext-diff",
                "--no-textconv",
                parent_oid,
                tree_oid,
            ],
            timeout_seconds=self._remaining_timeout(),
        )
        if result.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid diff output.")
        output = result.stdout
        return output[: self._limits.max_excerpt_chars]

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
        literal_pathspecs: bool = False,
    ) -> GitOutput[str]:
        return self._run_text(
            arguments,
            index_file=index_file,
            input_bytes=input_bytes,
            literal_pathspecs=literal_pathspecs,
            timeout_seconds=self._remaining_timeout(),
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

    def _validate_oid(self, value: str) -> str:
        if (
            not isinstance(value, str)
            or _OBJECT_ID.fullmatch(value) is None
            or len(value) != self._object_id_width()
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object ID.")
        return value

    def _strict_stdout_oid(self, output: GitOutput[str], message: str) -> str:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        try:
            return self._validate_oid(lines[0])
        except ManduaError:
            raise ManduaError(ErrorCode.GIT_FAILURE, message) from None

    @staticmethod
    def _require_silent_success(output: GitOutput[str], message: str) -> None:
        if output.returncode != 0 or output.stdout or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)

    def _strict_branch_ref(self, output: GitOutput[str]) -> str:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid current branch.")
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid current branch.")
        branch_ref = lines[0]
        if (
            not branch_ref.startswith("refs/heads/")
            or branch_ref == "refs/heads/"
            or len(branch_ref) > self._limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid current branch.")
        return branch_ref

    def _remaining_timeout(self) -> float:
        remaining = self._deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Checkpoint preparation exceeded the configured time limit.",
            )
        return remaining

    @staticmethod
    def _require_same_snapshot(expected: _PreparedCheckpoint, actual: _PreparedCheckpoint) -> None:
        if (
            expected.branch_ref != actual.branch_ref
            or expected.parent_oid != actual.parent_oid
            or expected.tree_oid != actual.tree_oid
            or expected.message != actual.message
            or expected.policy != actual.policy
            or expected.index_state != actual.index_state
            or expected.selected_entries != actual.selected_entries
        ):
            raise CheckpointWriter._race_error()

    @staticmethod
    def _race_error() -> ManduaError:
        return ManduaError(
            ErrorCode.POLICY_VIOLATION,
            "Repository state changed while the checkpoint was being prepared.",
            recovery="Review the current branch, index, files, and policy before retrying.",
        )

    def _validate_request(self, request: object, apply: object) -> None:
        self._remaining_timeout()
        if not isinstance(apply, bool):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The apply setting is invalid.")
        if not isinstance(request, CheckpointRequest) or not isinstance(
            request.metadata, CommitMetadata
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The checkpoint request is invalid.")
        if not isinstance(request.staged, bool) or not isinstance(request.paths, tuple):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The checkpoint selection is invalid.")
        if bool(request.paths) == request.staged:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "Choose either explicit paths or staged mode, but not both.",
            )
        if len(request.paths) > _MAX_EXPLICIT_PATHS:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The checkpoint request contains too many explicit paths.",
            )
        seen: set[PurePosixPath] = set()
        aggregate_bytes = 0
        for path in request.paths:
            self._remaining_timeout()
            if not isinstance(path, PurePosixPath):
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "The checkpoint path is invalid.")
            text = path.as_posix()
            try:
                encoded = text.encode("utf-8")
            except UnicodeEncodeError:
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED, "The checkpoint path is invalid."
                ) from None
            if (
                path.is_absolute()
                or not path.parts
                or len(text) > self._limits.max_input_chars
                or b"\x00" in encoded
                or any(
                    component in {"", ".", ".."} or component.casefold() == ".git"
                    for component in path.parts
                )
            ):
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "The checkpoint path is invalid.")
            aggregate_bytes += len(encoded) + 1
            if aggregate_bytes > _MAX_PATH_ARGUMENT_BYTES:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "Checkpoint paths exceeded the aggregate argument-byte limit.",
                )
            if path in seen:
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "Checkpoint paths must be unique.")
            seen.add(path)
        for value in (
            request.metadata.memory_type,
            request.metadata.scope,
            request.metadata.agent_id,
        ):
            if not isinstance(value, str):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED, "Required checkpoint metadata is invalid."
                )

    def _result(
        self,
        prepared: _PreparedCheckpoint,
        *,
        commit_oid: str | None,
        applied: bool,
        warnings: tuple[str, ...] = (),
    ) -> MemoryResult:
        details = {
            "parent_oid": prepared.parent_oid,
            "tree_oid": prepared.tree_oid,
            "branch": prepared.branch,
            "diff_summary": prepared.diff_summary,
        }
        if commit_oid is not None:
            details["commit_oid"] = commit_oid
        evidence_id = "checkpoint-1"
        return MemoryResult(
            operation="checkpoint",
            answer=(
                "The semantic checkpoint was applied."
                if applied
                else "The semantic checkpoint was validated and previewed."
            ),
            observed=(Claim("The proposed tree passed repository policy.", (evidence_id,)),),
            evidence=(
                Evidence(
                    id=evidence_id,
                    kind="checkpoint-preview",
                    oid=prepared.tree_oid,
                    ref=prepared.branch_ref,
                    excerpt=prepared.diff_summary or None,
                    details=details,
                ),
            ),
            history_scope=HistoryScope(
                end_oid=commit_oid or prepared.parent_oid,
                refs=(prepared.branch_ref,),
            ),
            warnings=warnings,
            changes=(
                PlannedChange(
                    action="update-ref",
                    target=prepared.branch_ref,
                    before_oid=prepared.parent_oid,
                    after_oid=commit_oid,
                ),
            ),
            applied=applied,
        )


def checkpoint_result(
    repository: Path,
    limits: QueryLimits,
    request: CheckpointRequest,
    *,
    apply: bool = False,
) -> MemoryResult:
    """Build a checkpoint result through one operation-scoped writer."""
    return CheckpointWriter(repository, limits).checkpoint(request, apply=apply)
