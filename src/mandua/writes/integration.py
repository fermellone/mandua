"""Preview and apply validated non-fast-forward integrations."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from mandua.config import load_tree_policy
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import (
    GitOperationBudget,
    GitOutput,
    GitRepositoryAuthority,
    GitRunner,
    _GitBudgetLimits,
    _RepositoryAuthorityLayout,
)
from mandua.models import (
    Claim,
    CommitMetadata,
    Evidence,
    HistoryScope,
    IntegrationRequest,
    MemoryResult,
    PlannedChange,
    QueryLimits,
    RepositoryPolicy,
)
from mandua.policy import Policy

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_OBJECT_ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?:[0-9a-f]{24})?(?![0-9a-f])")
_MAX_ATTRIBUTE_PATHS = 4_096
_MAX_CLOSURE_OBJECTS = 16_384
_MAX_PACK_DIRECTORY_ENTRIES = 256
_MAX_PACK_INDEXES = 64
_SAFE_MERGE_ATTRIBUTES = frozenset({b"text", b"binary", b"union"})
_UNSAFE_ATTRIBUTES = frozenset({b"filter", b"diff", b"working-tree-encoding"})
_MERGE_STATE_NAMES = (
    "MERGE_HEAD",
    "MERGE_MSG",
    "MERGE_MODE",
    "AUTO_MERGE",
    "MERGE_AUTOSTASH",
)
_MERGE_SUCCESS_STDERR = "Automatic merge went well; stopped before committing as requested\n"
_EMERGENCY_TIMEOUT_SECONDS = 2.0
_EMERGENCY_MAX_PROCESSES = 64
_EMERGENCY_MAX_OUTPUT_BYTES = 1_048_576
_EMERGENCY_MAX_INPUT_BYTES = 65_536
_MAIN_MAX_GIT_PROCESSES = 512
_MAIN_MAX_GIT_OUTPUT_BYTES = 1_048_576
_MAIN_MAX_GIT_INPUT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class _PreparedIntegration:
    request: IntegrationRequest
    source_ref: str
    target_ref: str
    source_oid: str
    target_oid: str
    proposed_tree: str
    diff_summary: str
    message: str
    invariant_count: int
    target_policy: RepositoryPolicy
    proposed_policy: RepositoryPolicy
    attribute_digest: str
    proposed_attribute_digest: str
    configuration_digest: str
    delta_paths: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class _IntegrationOutcome:
    result: MemoryResult
    applied_policy: RepositoryPolicy | None


@dataclass(frozen=True, slots=True)
class _IgnoredState:
    digest: str
    path_count: int
    content_bytes: int


class IntegrationWriter:
    """Validate one exact pair of local branch tips and optionally merge it."""

    def __init__(
        self,
        repository: Path,
        limits: QueryLimits,
        *,
        repository_policy: RepositoryPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._limits = limits
        self._repository_policy = repository_policy
        private_limits = _GitBudgetLimits(
            max_processes=_MAIN_MAX_GIT_PROCESSES,
            max_output_bytes=_MAIN_MAX_GIT_OUTPUT_BYTES,
            max_input_bytes=_MAIN_MAX_GIT_INPUT_BYTES,
        )
        self._budget = GitOperationBudget(limits, budget_limits=private_limits)
        self._runner = GitRunner(
            repository,
            limits=limits,
            operation_budget=self._budget,
            _budget_limits=private_limits,
        )
        self._deadline = self._budget.deadline
        self._object_width: int | None = None
        self._repository_authority: GitRepositoryAuthority | None = None
        self._repository_layout: _RepositoryAuthorityLayout | None = None
        self._ignored_state: _IgnoredState | None = None

    def integrate(self, request: IntegrationRequest, *, apply: bool = False) -> MemoryResult:
        """Return a non-mutating preview or apply its freshly recomputed integration."""
        return self._integrate_outcome(request, apply=apply).result

    def _integrate_outcome(
        self, request: IntegrationRequest, *, apply: bool = False
    ) -> _IntegrationOutcome:
        """Retain the exact validated policy only when its integration was proven applied."""
        self._validate_request(request, apply)
        self._require_no_alternates()
        if not self._runner.capabilities.merge_tree_write_tree:
            raise ManduaError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "This Git version does not support merge-tree --write-tree.",
            )
        prepared: _PreparedIntegration | None = None
        result: MemoryResult | None = None
        try:
            prepared = self._prepare(request)
            if not apply:
                result = self._result(prepared, commit_oid=None, applied=False)
            else:
                result = self._apply(prepared)
        except BaseException as operation_error:
            try:
                self._cleanup_operation_resources()
            except BaseException as cleanup_error:
                raise cleanup_error from operation_error
            raise

        assert prepared is not None
        assert result is not None
        try:
            self._cleanup_operation_resources()
        except BaseException as cleanup_error:
            if not result.applied:
                raise
            commit_oid = self._reconcile_commit_exception(prepared, cleanup_error)
            if not isinstance(cleanup_error, ManduaError):
                raise
            return _IntegrationOutcome(
                result=self._result(
                    prepared,
                    commit_oid=commit_oid,
                    applied=True,
                    warnings=(
                        (
                            "The integration commit was applied exactly, but cleanup of its private "
                            "Git authority reported a failure; the final commit was independently "
                            "verified."
                        ),
                    ),
                ),
                applied_policy=prepared.proposed_policy,
            )
        return _IntegrationOutcome(
            result=result,
            applied_policy=prepared.proposed_policy if result.applied else None,
        )

    def _apply(self, prepared: _PreparedIntegration) -> MemoryResult:
        """Apply one prepared integration through the private Git authority."""

        self._require_apply_preconditions(prepared)
        self._require_mutation_boundary(prepared)
        self._require_final_mutation_refs(prepared)
        self._require_apply_preconditions(prepared)
        self._require_ignored_state(prepared, capture=True)
        merge_started = False
        commit_attempted = False
        try:
            merge_started = True
            merge_output = self._runner.run_text(
                [
                    "merge",
                    "--no-ff",
                    "--no-commit",
                    "--no-verify",
                    "--no-overwrite-ignore",
                    prepared.source_oid,
                ],
                check=False,
                timeout_seconds=self._remaining_timeout(),
                isolated_configuration=True,
                repository_authority=self._required_repository_authority(),
            )
            if merge_output.returncode == 0:
                self._validate_successful_merge_output(prepared, merge_output)
            if merge_output.returncode != 0:
                if merge_output.returncode == 1:
                    original = ManduaError(
                        ErrorCode.CONFLICT,
                        "The checked-out merge conflicted after the validated preview.",
                        evidence=(self._command_evidence("merge-conflict", merge_output),),
                    )
                else:
                    original = ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Git could not stage the validated integration.",
                        evidence=(self._command_evidence("merge-failure", merge_output),),
                    )
                self._abort_and_raise(prepared, original)
            self._require_precommit_state(prepared)
            self._require_commit_boundary(prepared)
            try:
                commit_attempted = True
                commit_output = self._runner.run_text(
                    ["commit", "--no-verify", "--cleanup=verbatim", "-F", "-"],
                    check=False,
                    input_bytes=prepared.message.encode(),
                    timeout_seconds=self._remaining_timeout(),
                    isolated_configuration=True,
                    repository_authority=self._required_repository_authority(),
                )
            except BaseException as error:
                commit_oid = self._reconcile_commit_exception(prepared, error)
                if not isinstance(error, ManduaError):
                    raise
                return self._result(
                    prepared,
                    commit_oid=commit_oid,
                    applied=True,
                    warnings=(
                        (
                            "Git reported an exception after commit; the exact tree, parents, refs, "
                            "clean state, and completed merge were independently verified."
                        ),
                    ),
                )
            if commit_output.returncode != 0:
                original = ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git could not create the integration commit.",
                    evidence=(self._command_evidence("commit-failure", commit_output),),
                )
                commit_oid = self._reconcile_commit_exception(prepared, original)
                return self._result(
                    prepared,
                    commit_oid=commit_oid,
                    applied=True,
                    warnings=(
                        (
                            "Git reported failure after commit; the exact tree, parents, refs, "
                            "clean state, and completed merge were independently verified."
                        ),
                    ),
                )
        except BaseException as error:
            if (
                merge_started
                and not commit_attempted
                and not (
                    isinstance(error, ManduaError)
                    and error.message == "Integration failed and rollback could not be verified."
                )
            ):
                self._abort_and_raise(prepared, error)
            raise

        try:
            commit_oid = self._capture_target_after_commit(prepared)
            self._verify_final_commit(prepared, commit_oid)
        except BaseException as error:
            commit_oid = self._reconcile_commit_exception(prepared, error)
            if not isinstance(error, ManduaError):
                raise
            return self._result(
                prepared,
                commit_oid=commit_oid,
                applied=True,
                warnings=(
                    (
                        "Git reported an exception during final verification; the exact tree, "
                        "parents, refs, clean state, and completed merge were independently verified."
                    ),
                ),
            )
        return self._result(prepared, commit_oid=commit_oid, applied=True)

    def _cleanup_operation_resources(self) -> None:
        resource = self._repository_authority
        self._repository_authority = None
        if resource is not None:
            self._release_operation_resources(resource)

    def _release_operation_resources(self, resource: GitRepositoryAuthority) -> None:
        resource.cleanup()

    def _required_repository_authority(self) -> GitRepositoryAuthority:
        if self._repository_authority is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The private Git authority is unavailable.",
                recovery="Retry the integration from a clean repository state.",
            )
        return self._repository_authority

    def _pin_repository_attributes(self, treeish_oid: str) -> None:
        self._runner.set_repository_attribute_source(
            self._required_repository_authority(), treeish_oid
        )

    @contextmanager
    def _emergency_resources(self, prepared: _PreparedIntegration) -> Iterator[float]:
        timeout = min(float(self._limits.timeout_seconds), _EMERGENCY_TIMEOUT_SECONDS)
        limits = replace(self._limits, timeout_seconds=timeout)
        private_limits = _GitBudgetLimits(
            max_processes=_EMERGENCY_MAX_PROCESSES,
            max_output_bytes=_EMERGENCY_MAX_OUTPUT_BYTES,
            max_input_bytes=_EMERGENCY_MAX_INPUT_BYTES,
        )
        budget = GitOperationBudget(limits, budget_limits=private_limits)
        runner = GitRunner(
            self._repository,
            limits=limits,
            operation_budget=budget,
            _budget_limits=private_limits,
        )
        if self._repository_layout is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "The captured Git authority layout is unavailable for recovery.",
            )
        authority = runner.open_repository_authority(
            prepared.target_oid,
            expected_layout=self._repository_layout,
        )
        original_runner = self._runner
        original_budget = self._budget
        original_deadline = self._deadline
        original_authority = self._repository_authority
        self._runner = runner
        self._budget = budget
        self._deadline = budget.deadline
        self._repository_authority = authority
        try:
            yield budget.deadline
        finally:
            try:
                authority.cleanup()
            finally:
                self._runner = original_runner
                self._budget = original_budget
                self._deadline = original_deadline
                self._repository_authority = original_authority

    def _prepare(self, request: IntegrationRequest) -> _PreparedIntegration:
        self._require_no_alternates()
        source_ref = self._branch_ref(request.source, "source")
        target_ref = self._branch_ref(request.target, "target")
        if source_ref == target_ref:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "Integration source and target branches must be different.",
            )
        target_oid = self._capture_branch_tip(target_ref)
        source_oid = self._capture_branch_tip(source_ref)
        if source_oid == target_oid:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "Integration source and target already name the same commit.",
            )
        target_tree = self._commit_tree(target_oid)
        target_policy = load_tree_policy(
            self._runner,
            target_tree,
            self._limits,
            timeout_seconds=self._remaining_timeout(),
        )
        if self._repository_policy is not None and target_policy != self._repository_policy:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The integration target policy differs from the service policy.",
            )
        if request.target != target_policy.canonical_branch:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The integration target must be the configured canonical branch.",
            )
        policy = self._policy(target_policy)
        message = (
            policy.validate_message(
                request.subject,
                reason=request.metadata.reason,
                memory_type=request.metadata.memory_type,
                scope=request.metadata.scope,
                task_id=request.metadata.task_id,
                decision_id=request.metadata.decision_id,
                agent_id=request.metadata.agent_id,
                extra_trailers=request.metadata.extra_trailers,
            )
            + "\n"
        )
        self._reject_noop_or_unrelated(target_oid, source_oid)
        configuration_digest = self._scan_external_configuration()
        attribute_digest = self._scan_attributes(target_oid, source_oid)
        self._repository_authority = self._runner.open_repository_authority(target_oid)
        self._repository_layout = self._repository_authority.layout
        proposed_tree = self._merge_tree(target_oid, source_oid)
        if self._scan_attributes(target_oid, source_oid) != attribute_digest:
            raise self._race_error("Effective Git attributes changed during integration preview.")
        if self._scan_external_configuration() != configuration_digest:
            raise self._race_error("Git helper configuration changed during integration preview.")
        self._pin_repository_attributes(proposed_tree)
        proposed_attribute_digest = self._scan_attribute_trees(
            ((b"proposed", proposed_tree),), closed_authority=True
        )
        self._pin_repository_attributes(target_oid)
        proposed_policy = load_tree_policy(
            self._runner,
            proposed_tree,
            self._limits,
            timeout_seconds=self._remaining_timeout(),
        )
        if proposed_policy.canonical_branch != request.target:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The proposed tree changes the configured canonical integration target.",
            )
        self._policy(proposed_policy).validate_tree(
            proposed_tree,
            invariant_error_code=ErrorCode.CONFLICT,
        )
        diff_summary = self._diff_summary(target_oid, proposed_tree)
        delta_paths = self._tree_delta_paths(target_oid, proposed_tree)
        self._require_ref_tips(target_ref, target_oid, source_ref, source_oid)
        return _PreparedIntegration(
            request=request,
            source_ref=source_ref,
            target_ref=target_ref,
            source_oid=source_oid,
            target_oid=target_oid,
            proposed_tree=proposed_tree,
            diff_summary=diff_summary,
            message=message,
            invariant_count=len(proposed_policy.invariants),
            target_policy=target_policy,
            proposed_policy=proposed_policy,
            attribute_digest=attribute_digest,
            proposed_attribute_digest=proposed_attribute_digest,
            configuration_digest=configuration_digest,
            delta_paths=delta_paths,
        )

    def _reject_noop_or_unrelated(self, target_oid: str, source_oid: str) -> None:
        source_is_ancestor = self._runner.run_text(
            ["merge-base", "--is-ancestor", source_oid, target_oid],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if source_is_ancestor.returncode == 0 and not source_is_ancestor.stdout:
            if source_is_ancestor.stderr:
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid ancestry output.")
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The integration source is already contained in the target history.",
            )
        if (
            source_is_ancestor.returncode != 1
            or source_is_ancestor.stdout
            or source_is_ancestor.stderr
        ):
            self._raise_missing_or_git_failure(
                source_is_ancestor, "Git could not verify integration ancestry."
            )
        merge_base = self._runner.run_text(
            ["merge-base", target_oid, source_oid],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if merge_base.returncode == 0:
            self._strict_stdout_oid(merge_base, "Git returned an invalid merge base.")
            return
        if merge_base.returncode != 1 or merge_base.stdout or merge_base.stderr:
            self._raise_missing_or_git_failure(
                merge_base, "Git could not find the integration merge base."
            )
        shallow = self._runner.run_text(
            ["rev-parse", "--is-shallow-repository"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if shallow.returncode != 0 or shallow.stderr or shallow.stdout not in {"true\n", "false\n"}:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid shallow-state result."
            )
        if shallow.stdout == "true\n":
            raise ManduaError(
                ErrorCode.INCOMPLETE_HISTORY,
                "The shallow repository cannot prove that both histories are related.",
                recovery="Deepen the local history without asking Mandu'a to fetch implicitly.",
            )
        raise ManduaError(
            ErrorCode.CONFLICT,
            "The integration branches do not share a complete local history.",
        )

    def _merge_tree(self, target_oid: str, source_oid: str) -> str:
        output = self._runner.run(
            ["merge-tree", "--write-tree", "--messages", "-z", target_oid, source_oid],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        if output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git merge-tree returned unexpected diagnostics.",
                evidence=(self._binary_command_evidence("merge-tree-stderr", output),),
            )
        if output.returncode not in {0, 1}:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git merge-tree failed while preparing the integration.",
                evidence=(self._binary_command_evidence("merge-tree-failure", output),),
            )
        if b"\x00" not in output.stdout:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed output.")
        raw_tree, records = output.stdout.split(b"\x00", 1)
        try:
            tree = raw_tree.decode("ascii")
        except UnicodeDecodeError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git merge-tree returned an invalid tree ID."
            ) from None
        self._validate_oid(tree, "Git merge-tree returned an invalid tree ID.")
        messages = self._parse_merge_tree_records(records, returncode=output.returncode)
        if output.returncode == 0:
            return tree
        raise ManduaError(
            ErrorCode.CONFLICT,
            "The proposed integration has textual merge conflicts.",
            evidence=(
                Evidence(
                    id="integration-conflict",
                    kind="merge-conflict",
                    oid=tree,
                    excerpt=messages.decode("utf-8", errors="replace")[
                        : self._limits.max_excerpt_chars
                    ]
                    or None,
                    details={"exit_code": 1},
                ),
            ),
        )

    def _parse_merge_tree_records(self, value: bytes, *, returncode: int) -> bytes:
        if not value.endswith(b"\x00"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed output.")
        fields = value.split(b"\x00")
        try:
            separator = fields.index(b"")
        except ValueError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed conflict records."
            ) from None
        conflict_records = fields[:separator]
        message_fields = fields[separator + 1 :]
        if not message_fields or message_fields[-1] != b"":
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed message records."
            )
        message_fields.pop()
        if returncode == 0 and conflict_records:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git merge-tree returned conflicts with a successful exit."
            )
        for record in conflict_records:
            metadata, separator_byte, path = record.partition(b"\t")
            parts = metadata.split(b" ")
            if (
                separator_byte != b"\t"
                or not path
                or len(path) > self._limits.max_input_chars
                or len(parts) != 3
                or re.fullmatch(rb"[0-7]{6}", parts[0]) is None
                or re.fullmatch(rb"[0-9a-f]+", parts[1]) is None
                or len(parts[1]) != self._object_id_width()
                or parts[2] not in {b"1", b"2", b"3"}
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git merge-tree returned malformed conflict records.",
                )

        rendered: list[bytes] = []
        index = 0
        saw_conflict = False
        while index < len(message_fields):
            raw_count = message_fields[index]
            index += 1
            try:
                count = int(raw_count.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed message records."
                ) from None
            if (
                count < 1
                or count > _MAX_ATTRIBUTE_PATHS
                or raw_count != str(count).encode("ascii")
                or index + count + 2 > len(message_fields)
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed message records."
                )
            paths = message_fields[index : index + count]
            index += count
            message_type = message_fields[index]
            message = message_fields[index + 1]
            index += 2
            if (
                any(not path or len(path) > self._limits.max_input_chars for path in paths)
                or not message_type
                or len(message_type) > self._limits.max_input_chars
                or not message
            ):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed message records."
                )
            try:
                decoded_type = message_type.decode("ascii")
            except UnicodeDecodeError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git merge-tree returned malformed message records."
                ) from None
            is_conflict = decoded_type.startswith("CONFLICT (")
            saw_conflict = saw_conflict or is_conflict
            if returncode == 0 and decoded_type != "Auto-merging":
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git merge-tree returned an unexpected successful message type.",
                )
            rendered.append(message_type + b": " + message)
        if returncode == 1 and not (conflict_records or saw_conflict):
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git merge-tree reported a conflict without conflict records.",
            )
        return b"".join(rendered)

    def _scan_attributes(self, target_oid: str, source_oid: str) -> str:
        return self._scan_attribute_trees(
            ((b"target", target_oid), (b"source", source_oid)),
            closed_authority=False,
        )

    def _scan_attribute_trees(
        self,
        sources: tuple[tuple[bytes, str], ...],
        *,
        closed_authority: bool,
    ) -> str:
        digest = hashlib.sha256()
        authority = self._required_repository_authority() if closed_authority else None
        for label, commit_oid in sources:
            self._remaining_timeout()
            paths = self._tree_paths(commit_oid)
            digest.update(label + b"\x00" + commit_oid.encode("ascii") + b"\x00")
            if not paths:
                continue
            payload = b"".join(path + b"\x00" for path in paths)
            if len(payload) > self._limits.max_output_bytes:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "Integration attribute paths exceeded the aggregate byte limit.",
                )
            with self._runner.temporary_index() as index_file:
                read_tree = self._runner.run(
                    ["read-tree", commit_oid],
                    check=False,
                    index_file=index_file,
                    timeout_seconds=self._remaining_timeout(),
                    isolated_configuration=closed_authority,
                    repository_authority=authority,
                )
                if read_tree.returncode != 0 or read_tree.stdout or read_tree.stderr:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git could not prepare exact attribute inspection."
                    )
                attributes = self._runner.run(
                    ["check-attr", "--cached", "-z", "--all", "--stdin"],
                    check=False,
                    input_bytes=payload,
                    index_file=index_file,
                    literal_pathspecs=True,
                    timeout_seconds=self._remaining_timeout(),
                    isolated_configuration=closed_authority,
                    repository_authority=authority,
                )
            if attributes.returncode != 0 or attributes.stderr:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git could not inspect effective integration attributes."
                )
            self._validate_attribute_records(attributes.stdout, frozenset(paths))
            digest.update(attributes.stdout)
            digest.update(b"\x00")
        return digest.hexdigest()

    def _scan_external_configuration(self) -> str:
        output = self._runner.run(
            ["config", "--null", "--list", "--includes"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not inspect helper configuration.")
        records = output.stdout.split(b"\x00")
        if records and records[-1] == b"":
            records.pop()
        for record in records:
            if record.count(b"\n") != 1:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned malformed helper configuration."
                )
            key, value = record.split(b"\n", 1)
            if not key or b"\x00" in value:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned malformed helper configuration."
                )
            folded = key.lower()
            unsafe = (
                (folded.startswith(b"merge.") and folded.endswith(b".driver"))
                or (
                    folded.startswith(b"filter.")
                    and not folded.startswith(b"filter.lfs.")
                    and folded.rsplit(b".", 1)[-1] in {b"clean", b"smudge", b"process"}
                )
                or (
                    folded.startswith(b"diff.")
                    and folded.rsplit(b".", 1)[-1] in {b"command", b"textconv"}
                )
                or folded in {b"include.path"}
                or (folded.startswith(b"includeif.") and folded.endswith(b".path"))
            )
            if unsafe:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Integration rejects configured merge, filter, and diff helper programs.",
                )
        return hashlib.sha256(output.stdout).hexdigest()

    def _tree_paths(self, commit_oid: str) -> tuple[bytes, ...]:
        output = self._runner.run(
            ["ls-tree", "-r", "-z", "--name-only", commit_oid],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not enumerate integration paths.")
        if not output.stdout:
            return ()
        if not output.stdout.endswith(b"\x00"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned malformed integration paths.")
        paths = tuple(output.stdout[:-1].split(b"\x00"))
        if len(paths) > _MAX_ATTRIBUTE_PATHS:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The integration contains too many paths for safe attribute inspection.",
            )
        if any(not path or len(path) > self._limits.max_input_chars for path in paths) or len(
            set(paths)
        ) != len(paths):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned malformed integration paths.")
        return paths

    def _tree_delta_paths(self, target_oid: str, tree_oid: str) -> tuple[bytes, ...]:
        output = self._runner.run(
            ["diff", "--name-only", "-z", "--no-renames", target_oid, tree_oid],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git could not enumerate proposed integration changes."
            )
        if not output.stdout:
            return ()
        if not output.stdout.endswith(b"\x00"):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed proposed integration paths."
            )
        paths = tuple(output.stdout[:-1].split(b"\x00"))
        if len(paths) > _MAX_ATTRIBUTE_PATHS:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The proposed integration changes too many paths for safe application.",
            )
        if any(not path or len(path) > self._limits.max_input_chars for path in paths) or len(
            set(paths)
        ) != len(paths):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed proposed integration paths."
            )
        return paths

    def _capture_relevant_ignored_state(self, prepared: _PreparedIntegration) -> _IgnoredState:
        candidate_paths: set[bytes] = set()
        for delta_path in prepared.delta_paths:
            components = delta_path.split(b"/")
            candidate_paths.update(
                b"/".join(components[:index]) for index in range(1, len(components) + 1)
            )
        ordered_candidates = tuple(sorted(candidate_paths))
        if len(ordered_candidates) > _MAX_ATTRIBUTE_PATHS:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "The proposed integration requires too many ignored-path collision checks.",
            )
        if not ordered_candidates:
            return _IgnoredState(hashlib.sha256().hexdigest(), 0, 0)
        output = self._runner.run(
            [
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                *(os.fsdecode(path) for path in ordered_candidates),
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            literal_pathspecs=True,
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git could not enumerate ignored untracked paths."
            )
        if not output.stdout:
            return _IgnoredState(hashlib.sha256().hexdigest(), 0, 0)
        if not output.stdout.endswith(b"\x00"):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed ignored untracked paths."
            )
        ignored_paths = tuple(output.stdout[:-1].split(b"\x00"))
        if any(not self._valid_raw_repository_path(path) for path in ignored_paths) or len(
            set(ignored_paths)
        ) != len(ignored_paths):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed ignored untracked paths."
            )
        relevant = tuple(
            path
            for path in ignored_paths
            if any(self._paths_collide(path, delta) for delta in prepared.delta_paths)
        )
        if len(relevant) > _MAX_ATTRIBUTE_PATHS:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Relevant ignored content exceeded the safe path-count limit.",
            )
        digest = hashlib.sha256()
        content_bytes = 0
        for path in relevant:
            record, size = self._fingerprint_ignored_path(path)
            content_bytes += size
            if content_bytes > self._limits.max_output_bytes:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "Relevant ignored content exceeded the safe fingerprint byte limit.",
                )
            digest.update(len(path).to_bytes(8, "big"))
            digest.update(path)
            digest.update(len(record).to_bytes(8, "big"))
            digest.update(record)
        return _IgnoredState(digest.hexdigest(), len(relevant), content_bytes)

    def _fingerprint_ignored_path(self, path: bytes) -> tuple[bytes, int]:
        root = self._repository.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.fsencode(root), directory_flags)
        try:
            parts = path.split(b"/")
            for component in parts[:-1]:
                next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            name = parts[-1]
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            mode = stat.S_IFMT(metadata.st_mode)
            header = f"{mode:o}:{metadata.st_mode & 0o7777:o}:{metadata.st_size}:".encode()
            if stat.S_ISREG(metadata.st_mode):
                file_descriptor = os.open(
                    name,
                    flags | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    observed = os.fstat(file_descriptor)
                    if (
                        observed.st_dev != metadata.st_dev
                        or observed.st_ino != metadata.st_ino
                        or observed.st_size != metadata.st_size
                    ):
                        raise ManduaError(
                            ErrorCode.POLICY_VIOLATION,
                            "Relevant ignored content changed during safety inspection.",
                        )
                    if observed.st_size > self._limits.max_output_bytes:
                        raise ManduaError(
                            ErrorCode.LIMIT_EXCEEDED,
                            "Relevant ignored content exceeded the safe fingerprint byte limit.",
                        )
                    content = bytearray()
                    while len(content) <= self._limits.max_output_bytes:
                        chunk = os.read(
                            file_descriptor,
                            min(65_536, self._limits.max_output_bytes + 1 - len(content)),
                        )
                        if not chunk:
                            break
                        content.extend(chunk)
                    if len(content) > self._limits.max_output_bytes:
                        raise ManduaError(
                            ErrorCode.LIMIT_EXCEEDED,
                            "Relevant ignored content exceeded the safe fingerprint byte limit.",
                        )
                finally:
                    os.close(file_descriptor)
                return header + hashlib.sha256(content).digest(), len(content)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(name, dir_fd=descriptor)
                raw_target = os.fsencode(target)
                if len(raw_target) > self._limits.max_output_bytes:
                    raise ManduaError(
                        ErrorCode.LIMIT_EXCEEDED,
                        "Relevant ignored symlink data exceeded the safe fingerprint byte limit.",
                    )
                return header + hashlib.sha256(raw_target).digest(), len(raw_target)
            if stat.S_ISDIR(metadata.st_mode):
                return header, 0
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Integration rejects relevant ignored special filesystem entries.",
            )
        except ManduaError:
            raise
        except OSError:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Relevant ignored content changed during safety inspection.",
            ) from None
        finally:
            os.close(descriptor)

    @staticmethod
    def _paths_collide(first: bytes, second: bytes) -> bool:
        return first == second or first.startswith(second + b"/") or second.startswith(first + b"/")

    def _valid_raw_repository_path(self, path: bytes) -> bool:
        return (
            bool(path)
            and len(path) <= self._limits.max_input_chars
            and not path.startswith(b"/")
            and all(part not in {b"", b".", b".."} for part in path.split(b"/"))
        )

    def _require_ignored_state(self, prepared: _PreparedIntegration, *, capture: bool) -> None:
        state = self._capture_relevant_ignored_state(prepared)
        if state.path_count:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Ignored untracked content collides with the proposed integration tree.",
                evidence=(
                    Evidence(
                        id="ignored-integration-collision",
                        kind="ignored-path-collision",
                        details={
                            "path_count": state.path_count,
                            "content_bytes": state.content_bytes,
                            "fingerprint": state.digest,
                        },
                    ),
                ),
                recovery="Move or preserve the ignored content before retrying integration.",
            )
        if capture:
            self._ignored_state = state

    def _require_captured_ignored_state(self, prepared: _PreparedIntegration) -> None:
        expected = self._ignored_state
        if expected is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The ignored-path pre-mutation state was not captured."
            )
        observed = self._capture_relevant_ignored_state(prepared)
        if observed != expected:
            raise self._race_error("Relevant ignored content changed during integration apply.")

    def _require_no_alternates(self) -> Path:
        if self._repository_authority is not None:
            return self._runner.primary_object_directory(self._repository_authority)
        output = self._runner.run_text(
            ["rev-parse", "--path-format=absolute", "--git-path", "objects"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid repository object directory."
            )
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n" or "\x00" in lines[0]:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid repository object directory."
            )
        object_directory = Path(lines[0])
        try:
            metadata = os.lstat(object_directory)
            resolved = object_directory.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The repository object directory is unavailable."
            ) from None
        if (
            not object_directory.is_absolute()
            or resolved != object_directory
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ManduaError(
                ErrorCode.INCOMPLETE_HISTORY,
                "Integration rejects an indirect repository object store.",
                recovery="Use a complete self-contained local object store before retrying.",
            )
        info_directory = object_directory / "info"
        try:
            info_metadata = os.lstat(info_directory)
        except FileNotFoundError:
            info_metadata = None
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Repository alternate metadata could not be inspected."
            ) from None
        if info_metadata is not None and (
            stat.S_ISLNK(info_metadata.st_mode) or not stat.S_ISDIR(info_metadata.st_mode)
        ):
            raise ManduaError(
                ErrorCode.INCOMPLETE_HISTORY,
                "Integration rejects indirect repository alternate metadata.",
                recovery="Use a complete self-contained local object store before retrying.",
            )
        for name in ("alternates", "http-alternates"):
            try:
                os.lstat(info_directory / name)
            except FileNotFoundError:
                continue
            except OSError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Repository alternate metadata could not be inspected."
                ) from None
            raise ManduaError(
                ErrorCode.INCOMPLETE_HISTORY,
                "Integration rejects repository alternate object stores.",
                recovery="Use a complete self-contained local object store before retrying.",
            )
        return object_directory

    def _require_complete_primary_closure(self, commit_oid: str) -> None:
        """Prove every object reachable from a commit exists in the primary object store."""
        object_directory = self._require_no_alternates()
        reachable_output = self._runner.run(
            [
                "rev-list",
                "--objects",
                "--no-object-names",
                "--missing=print",
                commit_oid,
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        if reachable_output.returncode != 0 or reachable_output.stderr:
            raise self._incomplete_closure_error(
                "Git could not enumerate the complete integration history."
            )
        reachable: set[str] = set()
        for raw_line in reachable_output.stdout.splitlines():
            missing = raw_line.startswith(b"?")
            raw_oid = raw_line[1:] if missing else raw_line
            try:
                oid = raw_oid.decode("ascii")
            except UnicodeDecodeError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git returned malformed reachable-object output.",
                ) from None
            if missing:
                self._validate_oid(oid, "Git returned malformed reachable-object output.")
                raise self._incomplete_closure_error(
                    "The integration commit references a missing object.", oid=oid
                )
            self._validate_oid(oid, "Git returned malformed reachable-object output.")
            reachable.add(oid)
            if len(reachable) > _MAX_CLOSURE_OBJECTS:
                raise ManduaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "The integration history contains too many objects for closure proof.",
                )
        if commit_oid not in reachable:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git omitted the integration commit from closure output."
            )

        self._require_primary_objects_present(object_directory, reachable)

        payload = b"".join(oid.encode("ascii") + b"\n" for oid in sorted(reachable))
        inspected = self._runner.run(
            ["cat-file", "--batch-check"],
            check=False,
            input_bytes=payload,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        if inspected.returncode != 0 or inspected.stderr:
            raise self._incomplete_closure_error(
                "Git could not inspect every reachable integration object."
            )
        records = inspected.stdout.splitlines()
        if len(records) != len(reachable):
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed closure object metadata."
            )
        allowed_types = {b"blob", b"commit", b"tag", b"tree"}
        for expected_oid, record in zip(sorted(reachable), records, strict=True):
            fields = record.split(b" ")
            if (
                len(fields) != 3
                or fields[0] != expected_oid.encode("ascii")
                or fields[1] not in allowed_types
                or not fields[2].isdigit()
            ):
                raise self._incomplete_closure_error(
                    "Git returned incomplete reachable-object metadata.", oid=expected_oid
                )
        self._require_primary_objects_present(object_directory, reachable)
        self._require_no_alternates()

    def _require_primary_objects_present(self, object_directory: Path, reachable: set[str]) -> None:
        packed = self._primary_packed_object_ids(object_directory)
        root_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            object_descriptor = os.open(object_directory, root_flags)
        except OSError:
            raise self._incomplete_closure_error(
                "The primary object directory became unavailable during closure proof."
            ) from None
        try:
            for oid in reachable:
                if oid in packed or self._primary_loose_object_exists(object_descriptor, oid):
                    continue
                raise self._incomplete_closure_error(
                    "A reachable integration object is absent from the primary object store.",
                    oid=oid,
                )
        finally:
            os.close(object_descriptor)

    def _primary_packed_object_ids(self, object_directory: Path) -> frozenset[str]:
        pack_directory = object_directory / "pack"
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            pack_descriptor = os.open(pack_directory, directory_flags)
        except FileNotFoundError:
            return frozenset()
        except OSError:
            raise self._incomplete_closure_error(
                "The primary pack directory is indirect or invalid."
            ) from None
        packed: set[str] = set()
        try:
            directory_identity = self._pack_directory_identity(pack_directory, pack_descriptor)
            names, index_names = self._scan_pack_directory(pack_descriptor)
            for index_name in index_names:
                self._remaining_timeout()
                if self._pack_directory_identity(pack_directory, pack_descriptor) != (
                    directory_identity
                ):
                    raise self._incomplete_closure_error(
                        "The primary pack directory changed during closure proof."
                    )
                stem = index_name.removesuffix(".idx")
                if re.fullmatch(rf"pack-[0-9a-f]{{{self._object_id_width()}}}", stem) is None:
                    raise self._incomplete_closure_error(
                        "The primary pack index has an invalid name."
                    )
                pack_name = f"{stem}.pack"
                index_identity = self._regular_entry_identity(pack_descriptor, index_name)
                pack_identity = self._require_valid_pack(pack_descriptor, pack_name)
                index_path = pack_directory / index_name
                output = self._runner.run(
                    ["verify-pack", "-v", os.fspath(index_path)],
                    check=False,
                    timeout_seconds=self._remaining_timeout(),
                    isolated_configuration=True,
                    repository_authority=self._required_repository_authority(),
                )
                if output.returncode != 0 or output.stderr:
                    raise self._incomplete_closure_error(
                        "Git could not verify a primary object pack."
                    )
                saw_ok = False
                for raw_line in output.stdout.splitlines():
                    first, _, _ = raw_line.partition(b" ")
                    if first.endswith(b".pack:") and raw_line.endswith(b": ok"):
                        saw_ok = True
                        continue
                    try:
                        candidate = first.decode("ascii")
                    except UnicodeDecodeError:
                        candidate = ""
                    if _OBJECT_ID.fullmatch(candidate) is not None:
                        self._validate_oid(candidate, "Git returned malformed primary pack output.")
                        packed.add(candidate)
                        if len(packed) > _MAX_CLOSURE_OBJECTS:
                            raise ManduaError(
                                ErrorCode.LIMIT_EXCEEDED,
                                "The primary object packs exceed the closure object bound.",
                            )
                        continue
                    if raw_line.startswith((b"non delta:", b"chain length =")):
                        continue
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git returned malformed primary pack output."
                    )
                if not saw_ok:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git omitted primary pack verification status."
                    )
                if self._regular_entry_identity(pack_descriptor, index_name) != index_identity or (
                    self._regular_entry_identity(pack_descriptor, pack_name) != pack_identity
                ):
                    raise self._incomplete_closure_error(
                        "A primary object pack changed during closure proof."
                    )
                if self._pack_directory_identity(pack_directory, pack_descriptor) != (
                    directory_identity
                ):
                    raise self._incomplete_closure_error(
                        "The primary pack directory changed during closure proof."
                    )
            final_names, final_indexes = self._scan_pack_directory(pack_descriptor)
            if final_names != names or final_indexes != index_names:
                raise self._incomplete_closure_error(
                    "The primary pack inventory changed during closure proof."
                )
            if self._pack_directory_identity(pack_directory, pack_descriptor) != directory_identity:
                raise self._incomplete_closure_error(
                    "The primary pack directory changed during closure proof."
                )
        finally:
            os.close(pack_descriptor)
        return frozenset(packed)

    def _scan_pack_directory(self, pack_descriptor: int) -> tuple[frozenset[str], tuple[str, ...]]:
        names: set[str] = set()
        index_names: list[str] = []
        scan_descriptor = -1
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            scan_descriptor = os.open(".", flags, dir_fd=pack_descriptor)
            with os.scandir(scan_descriptor) as entries:
                for entry in entries:
                    self._remaining_timeout()
                    if not isinstance(entry.name, str):
                        raise self._incomplete_closure_error(
                            "The primary pack directory contains an invalid entry."
                        )
                    names.add(entry.name)
                    if len(names) > _MAX_PACK_DIRECTORY_ENTRIES:
                        raise ManduaError(
                            ErrorCode.LIMIT_EXCEEDED,
                            "The primary pack directory contains too many entries.",
                        )
                    if entry.name.endswith(".idx"):
                        index_names.append(entry.name)
                        if len(index_names) > _MAX_PACK_INDEXES:
                            raise ManduaError(
                                ErrorCode.LIMIT_EXCEEDED,
                                "The primary pack directory contains too many pack indexes.",
                            )
        except ManduaError:
            raise
        except OSError:
            raise self._incomplete_closure_error(
                "The primary pack directory could not be inspected."
            ) from None
        finally:
            if scan_descriptor >= 0:
                try:
                    os.close(scan_descriptor)
                except OSError:
                    pass
        return frozenset(names), tuple(sorted(index_names))

    def _pack_directory_identity(
        self, pack_directory: Path, pack_descriptor: int
    ) -> tuple[int, int, int, int, int]:
        self._remaining_timeout()
        try:
            descriptor_metadata = os.fstat(pack_descriptor)
            path_metadata = os.stat(pack_directory, follow_symlinks=False)
        except OSError:
            raise self._incomplete_closure_error(
                "The primary pack directory became unavailable during closure proof."
            ) from None
        if (
            not stat.S_ISDIR(descriptor_metadata.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
            or descriptor_metadata.st_dev != path_metadata.st_dev
            or descriptor_metadata.st_ino != path_metadata.st_ino
        ):
            raise self._incomplete_closure_error(
                "The primary pack directory changed during closure proof."
            )
        return (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
            descriptor_metadata.st_size,
            descriptor_metadata.st_mtime_ns,
            descriptor_metadata.st_ctime_ns,
        )

    def _require_valid_pack(
        self, pack_descriptor: int, pack_name: str
    ) -> tuple[int, int, int, int, int]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(pack_name, flags, dir_fd=pack_descriptor)
        except OSError:
            raise self._incomplete_closure_error(
                "A primary object-pack entry is unavailable."
            ) from None
        try:
            before = os.fstat(descriptor)
            path_metadata = os.stat(pack_name, dir_fd=pack_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or not stat.S_ISREG(path_metadata.st_mode)
                or before.st_dev != path_metadata.st_dev
                or before.st_ino != path_metadata.st_ino
            ):
                raise self._incomplete_closure_error("A primary object-pack entry is indirect.")
            digest_size = self._object_id_width() // 2
            if before.st_size <= digest_size:
                raise self._incomplete_closure_error("A primary object pack has malformed content.")
            hasher = hashlib.sha1() if digest_size == 20 else hashlib.sha256()
            remaining = before.st_size - digest_size
            while remaining:
                self._remaining_timeout()
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    raise self._incomplete_closure_error(
                        "A primary object pack has truncated content."
                    )
                hasher.update(chunk)
                remaining -= len(chunk)
            trailer = os.read(descriptor, digest_size + 1)
            if len(trailer) != digest_size or trailer != hasher.digest():
                raise self._incomplete_closure_error(
                    "A primary object pack has an invalid checksum."
                )
            after = os.fstat(descriptor)
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            observed_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if observed_identity != identity:
                raise self._incomplete_closure_error(
                    "A primary object pack changed during closure proof."
                )
            return identity
        finally:
            os.close(descriptor)

    @staticmethod
    def _regular_entry_identity(
        parent_descriptor: int, name: str
    ) -> tuple[int, int, int, int, int]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise ManduaError(
                ErrorCode.MISSING_OBJECT, "A primary object-pack entry is unavailable."
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
                    ErrorCode.MISSING_OBJECT, "A primary object-pack entry is indirect."
                )
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        finally:
            os.close(descriptor)

    def _primary_loose_object_exists(self, object_descriptor: int, oid: str) -> bool:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fanout_descriptor = os.open(oid[:2], directory_flags, dir_fd=object_descriptor)
        except FileNotFoundError:
            return False
        except OSError:
            raise self._incomplete_closure_error(
                "A primary loose-object directory is indirect or invalid.", oid=oid
            ) from None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(oid[2:], flags, dir_fd=fanout_descriptor)
            except FileNotFoundError:
                return False
            except OSError:
                raise self._incomplete_closure_error(
                    "A primary loose-object entry is indirect or invalid.", oid=oid
                ) from None
            try:
                metadata = os.fstat(descriptor)
                path_metadata = os.stat(oid[2:], dir_fd=fanout_descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not stat.S_ISREG(path_metadata.st_mode)
                    or metadata.st_dev != path_metadata.st_dev
                    or metadata.st_ino != path_metadata.st_ino
                ):
                    raise self._incomplete_closure_error(
                        "A primary loose-object entry is indirect or invalid.", oid=oid
                    )
                return True
            finally:
                os.close(descriptor)
        finally:
            os.close(fanout_descriptor)

    @staticmethod
    def _incomplete_closure_error(message: str, *, oid: str | None = None) -> ManduaError:
        evidence = (
            Evidence(
                id="integration-closure",
                kind="missing-reachable-object",
                oid=oid,
            ),
        )
        return ManduaError(
            ErrorCode.MISSING_OBJECT,
            message,
            evidence=evidence,
            recovery="Restore the complete primary object store before continuing.",
        )

    def _validate_attribute_records(self, value: bytes, paths: frozenset[bytes]) -> None:
        if not value:
            return
        if not value.endswith(b"\x00"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned malformed attribute records.")
        fields = value[:-1].split(b"\x00")
        if len(fields) % 3:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned malformed attribute records.")
        for index in range(0, len(fields), 3):
            path, attribute, raw_value = fields[index : index + 3]
            if path not in paths or not attribute or not raw_value:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned malformed attribute records."
                )
            if attribute in _UNSAFE_ATTRIBUTES:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Integration paths must not use filters, diff drivers, or working-tree encoding.",
                )
            if attribute == b"merge" and raw_value not in _SAFE_MERGE_ATTRIBUTES:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "Integration paths may use only explicit text, binary, or union merge drivers.",
                )

    def _require_apply_preconditions(self, prepared: _PreparedIntegration) -> None:
        self._require_no_alternates()
        target_ref, head_oid = self._capture_head()
        if target_ref != prepared.target_ref or head_oid != prepared.target_oid:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Applying an integration requires the exact captured target branch to be checked out.",
            )
        if self._merge_state_entries():
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Applying an integration requires no existing merge state.",
            )
        self._require_clean_repository()
        self._require_ref_tips(
            prepared.target_ref,
            prepared.target_oid,
            prepared.source_ref,
            prepared.source_oid,
        )
        self._require_ignored_state(prepared, capture=False)

    def _require_mutation_boundary(self, prepared: _PreparedIntegration) -> None:
        self._require_apply_preconditions(prepared)
        configuration_digest = self._scan_external_configuration()
        if configuration_digest != prepared.configuration_digest:
            raise self._race_error("Git helper configuration changed before integration apply.")
        digest = self._scan_attributes(prepared.target_oid, prepared.source_oid)
        if digest != prepared.attribute_digest:
            raise self._race_error("Effective Git attributes changed before integration apply.")
        self._require_apply_preconditions(prepared)
        self._require_no_alternates()
        self._require_ignored_state(prepared, capture=True)

    def _require_final_mutation_refs(self, prepared: _PreparedIntegration) -> None:
        try:
            head_ref, head_oid = self._capture_head()
            target_oid = self._capture_branch_tip(prepared.target_ref)
            source_oid = self._capture_branch_tip(prepared.source_ref)
        except ManduaError as error:
            raise self._ambiguous_mutation_boundary_error(
                prepared, f"state inspection failed: {error.message}"
            ) from None
        if (
            head_ref != prepared.target_ref
            or head_oid != prepared.target_oid
            or target_oid != prepared.target_oid
            or source_oid != prepared.source_oid
        ):
            raise self._ambiguous_mutation_boundary_error(
                prepared,
                (f"HEAD={head_ref}@{head_oid}; target={target_oid}; source={source_oid}"),
            )

    def _reconcile_commit_exception(
        self, prepared: _PreparedIntegration, original: BaseException
    ) -> str:
        interruption: BaseException | None = None
        for _ in range(2):
            try:
                with self._emergency_resources(prepared) as deadline:
                    commit_oid = self._reconcile_commit_exception_emergency(
                        prepared, original, deadline
                    )
            except BaseException as error:
                if error is original:
                    raise
                if isinstance(error, ManduaError):
                    if error.message == "The integration commit state could not be verified.":
                        if interruption is not None:
                            raise error from interruption
                        if not isinstance(original, ManduaError):
                            raise error from original
                        raise
                    ambiguous = self._ambiguous_post_commit_error(prepared, error.message)
                    if interruption is not None:
                        raise ambiguous from interruption
                    if not isinstance(original, ManduaError):
                        raise ambiguous from original
                    raise ambiguous from None
                if interruption is not None:
                    raise self._ambiguous_post_commit_error(
                        prepared, "post-commit state classification was interrupted twice"
                    ) from interruption
                interruption = error
                continue
            if interruption is not None:
                raise interruption
            return commit_oid
        raise self._ambiguous_post_commit_error(
            prepared, "post-commit state classification did not complete"
        ) from interruption

    def _reconcile_commit_exception_emergency(
        self,
        prepared: _PreparedIntegration,
        original: BaseException,
        deadline: float,
    ) -> str:
        try:
            self._require_no_alternates()
            head_ref, head_oid = self._capture_head_emergency(deadline)
            target_oid = self._capture_ref_emergency(prepared.target_ref, deadline)
            source_oid = self._capture_ref_emergency(prepared.source_ref, deadline)
            self._pin_repository_attributes(
                prepared.proposed_tree
                if head_ref == prepared.target_ref
                and target_oid == head_oid
                and head_oid != prepared.target_oid
                else prepared.target_oid
            )
            status = self._runner.run(
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                check=False,
                timeout_seconds=self._emergency_remaining(deadline),
                isolated_configuration=True,
                repository_authority=self._required_repository_authority(),
            )
            if status.returncode != 0 or status.stderr:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git could not inspect state after the commit command exception.",
                )
            merge_state = self._merge_state_entries(
                timeout_seconds=self._emergency_remaining(deadline)
            )
            ignored_state = self._capture_relevant_ignored_state(prepared)
        except ManduaError as error:
            try:
                if self._merge_is_in_progress_emergency(deadline):
                    self._abort_and_raise(prepared, original)
            except ManduaError as abort_error:
                if abort_error is original:
                    raise
                raise self._ambiguous_post_commit_error(prepared, abort_error.message) from None
            raise self._ambiguous_post_commit_error(prepared, error.message) from None

        observed = (
            f"HEAD={head_ref}@{head_oid}; target={target_oid}; source={source_oid}; "
            f"status_bytes={len(status.stdout)}; merge_state={','.join(merge_state) or 'none'}"
        )
        if (
            head_ref != prepared.target_ref
            or target_oid != head_oid
            or source_oid != prepared.source_oid
        ):
            raise self._ambiguous_post_commit_error(prepared, observed)
        if head_oid == prepared.target_oid:
            if merge_state:
                self._abort_and_raise(prepared, original)
            if status.stdout or self._ignored_state is None or ignored_state != self._ignored_state:
                raise self._ambiguous_post_commit_error(prepared, observed)
            raise original
        try:
            tree, parents, message = self._commit_metadata_emergency(head_oid, deadline)
        except ManduaError as error:
            raise self._ambiguous_post_commit_error(prepared, error.message) from None
        if (
            status.stdout
            or merge_state
            or tree != prepared.proposed_tree
            or parents != (prepared.target_oid, prepared.source_oid)
            or message != prepared.message.encode()
            or self._ignored_state is None
            or ignored_state != self._ignored_state
        ):
            raise self._ambiguous_post_commit_error(prepared, observed)
        try:
            self._require_complete_primary_closure(head_oid)
        except ManduaError as error:
            raise self._ambiguous_post_commit_error(prepared, error.message) from None
        self._require_final_applied_refs(prepared, head_oid, deadline=deadline)
        return head_oid

    def _require_precommit_state(self, prepared: _PreparedIntegration) -> None:
        self._require_no_alternates()
        self._require_captured_ignored_state(prepared)
        target_ref, head_oid = self._capture_head()
        if target_ref != prepared.target_ref or head_oid != prepared.target_oid:
            self._abort_and_raise(
                prepared,
                self._race_error("HEAD changed while the integration merge was staged."),
            )
        self._require_ref_tips(
            prepared.target_ref,
            prepared.target_oid,
            prepared.source_ref,
            prepared.source_oid,
        )
        written = self._runner.run_text(
            ["write-tree"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        tree = self._strict_stdout_oid(written, "Git returned an invalid staged integration tree.")
        if tree != prepared.proposed_tree:
            self._abort_and_raise(
                prepared,
                self._race_error("The staged integration tree differs from its validated preview."),
            )
        proposed_policy = load_tree_policy(
            self._runner,
            tree,
            self._limits,
            timeout_seconds=self._remaining_timeout(),
        )
        if proposed_policy.canonical_branch != prepared.request.target:
            self._abort_and_raise(
                prepared,
                ManduaError(
                    ErrorCode.POLICY_VIOLATION,
                    "The staged tree changes the configured canonical integration target.",
                ),
            )
        try:
            self._policy(proposed_policy).validate_tree(
                tree,
                invariant_error_code=ErrorCode.CONFLICT,
            )
        except ManduaError as error:
            self._abort_and_raise(prepared, error)
        target_ref, head_oid = self._capture_head()
        if target_ref != prepared.target_ref or head_oid != prepared.target_oid:
            raise self._race_error("HEAD changed after integration policy validation.")
        self._require_ref_tips(
            prepared.target_ref,
            prepared.target_oid,
            prepared.source_ref,
            prepared.source_oid,
        )
        self._require_no_alternates()
        self._require_captured_ignored_state(prepared)
        final_written = self._runner.run_text(
            ["write-tree"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        final_tree = self._strict_stdout_oid(
            final_written, "Git returned an invalid final staged integration tree."
        )
        if final_tree != prepared.proposed_tree:
            raise self._race_error("The staged integration tree changed after policy validation.")
        self._pin_repository_attributes(final_tree)
        staged_attribute_digest = self._scan_attribute_trees(
            ((b"proposed", final_tree),), closed_authority=True
        )
        if staged_attribute_digest != prepared.proposed_attribute_digest:
            raise self._race_error(
                "Effective attributes of the staged integration tree changed before commit."
            )

    def _require_commit_boundary(self, prepared: _PreparedIntegration) -> None:
        self._require_no_alternates()
        self._require_captured_ignored_state(prepared)
        target_ref, head_oid = self._capture_head()
        if target_ref != prepared.target_ref or head_oid != prepared.target_oid:
            self._abort_and_raise(
                prepared,
                self._race_error("HEAD changed at the final integration commit boundary."),
            )
        self._require_ref_tips(
            prepared.target_ref,
            prepared.target_oid,
            prepared.source_ref,
            prepared.source_oid,
        )
        written = self._runner.run_text(
            ["write-tree"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        tree = self._strict_stdout_oid(
            written, "Git returned an invalid commit-boundary integration tree."
        )
        if tree != prepared.proposed_tree:
            self._abort_and_raise(
                prepared,
                self._race_error("The staged integration tree changed at the commit boundary."),
            )

    def _validate_successful_merge_output(
        self, prepared: _PreparedIntegration, output: GitOutput[str]
    ) -> None:
        stdout_lines = output.stdout.splitlines(keepends=True)
        if (
            output.stderr != _MERGE_SUCCESS_STDERR
            or output.warnings
            or any(
                not line.startswith("Auto-merging ") or not line.endswith("\n") or "\x00" in line
                for line in stdout_lines
            )
        ):
            self._abort_and_raise(
                prepared,
                ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "Git returned unexpected diagnostics after staging the integration.",
                    evidence=(self._command_evidence("merge-diagnostics", output),),
                ),
            )

    def _capture_target_after_commit(self, prepared: _PreparedIntegration) -> str:
        target_ref, head_oid = self._capture_head()
        if target_ref != prepared.target_ref:
            raise self._ambiguous_post_commit_error(prepared, "HEAD no longer names the target.")
        return head_oid

    def _verify_final_commit(self, prepared: _PreparedIntegration, commit_oid: str) -> None:
        self._require_no_alternates()
        self._require_ref_tip(prepared.target_ref, commit_oid)
        self._require_ref_tip(prepared.source_ref, prepared.source_oid)
        try:
            tree, parents, message = self._commit_metadata(
                commit_oid,
                timeout_seconds=self._remaining_timeout(),
                repository_authority=self._required_repository_authority(),
            )
        except ManduaError as error:
            raise self._ambiguous_post_commit_error(prepared, error.message) from None
        if (
            tree != prepared.proposed_tree
            or parents != (prepared.target_oid, prepared.source_oid)
            or message != prepared.message.encode()
        ):
            raise self._ambiguous_post_commit_error(
                prepared,
                "The integration commit does not match the validated tree, parents, and message.",
            )
        merge_state = self._merge_state_entries()
        if merge_state:
            raise self._ambiguous_post_commit_error(
                prepared,
                "The integration commit exists but merge state remains: " + ", ".join(merge_state),
            )
        self._pin_repository_attributes(prepared.proposed_tree)
        self._require_clean_repository(post_commit=True)
        self._require_captured_ignored_state(prepared)
        try:
            self._require_complete_primary_closure(commit_oid)
        except ManduaError as error:
            raise self._ambiguous_post_commit_error(prepared, error.message) from None
        self._require_final_applied_refs(prepared, commit_oid)

    def _require_final_applied_refs(
        self,
        prepared: _PreparedIntegration,
        commit_oid: str,
        *,
        deadline: float | None = None,
    ) -> None:
        def timeout() -> float:
            return (
                self._remaining_timeout()
                if deadline is None
                else self._emergency_remaining(deadline)
            )

        authority = self._required_repository_authority()
        symbolic = self._runner.run_text(
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            timeout_seconds=timeout(),
            isolated_configuration=True,
            repository_authority=authority,
        )
        head_ref = self._strict_branch_ref(
            symbolic, "Git returned an invalid final checked-out branch."
        )
        head_oid = self._capture_closed_ref("HEAD", timeout())
        target_oid = self._capture_closed_ref(prepared.target_ref, timeout())
        source_oid = self._capture_closed_ref(prepared.source_ref, timeout())
        if (
            head_ref != prepared.target_ref
            or head_oid != commit_oid
            or target_oid != commit_oid
            or source_oid != prepared.source_oid
        ):
            raise self._ambiguous_post_commit_error(
                prepared,
                (f"final HEAD={head_ref}@{head_oid}; target={target_oid}; source={source_oid}"),
            )

    def _capture_closed_ref(self, reference: str, timeout_seconds: float) -> str:
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--end-of-options", reference],
            check=False,
            timeout_seconds=timeout_seconds,
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        return self._strict_stdout_oid(output, "Git returned an invalid final branch tip.")

    def _commit_metadata_emergency(
        self, commit_oid: str, deadline: float
    ) -> tuple[str, tuple[str, ...], bytes]:
        return self._commit_metadata(
            commit_oid,
            timeout_seconds=self._emergency_remaining(deadline),
            repository_authority=(
                self._repository_authority if self._repository_authority is not None else None
            ),
        )

    def _commit_metadata(
        self,
        commit_oid: str,
        *,
        timeout_seconds: float,
        repository_authority: GitRepositoryAuthority | None,
    ) -> tuple[str, tuple[str, ...], bytes]:
        output = self._runner.run(
            ["cat-file", "commit", commit_oid],
            check=False,
            timeout_seconds=timeout_seconds,
            isolated_configuration=repository_authority is not None,
            repository_authority=repository_authority,
        )
        if output.returncode != 0 or output.stderr or b"\x00" in output.stdout:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Git returned malformed integration commit metadata.",
            )
        raw_headers, separator, message = output.stdout.partition(b"\n\n")
        if separator != b"\n\n" or not raw_headers or not message:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed integration commit metadata."
            )
        tree: str | None = None
        parents: list[str] = []
        saw_header = False
        for line in raw_headers.split(b"\n"):
            if line.startswith(b" "):
                if not saw_header:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Git returned malformed integration commit metadata.",
                    )
                continue
            key, field_separator, raw_value = line.partition(b" ")
            if not field_separator or not key or not raw_value or not key.isascii():
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned malformed integration commit metadata."
                )
            saw_header = True
            if key not in {b"tree", b"parent"}:
                continue
            try:
                value = raw_value.decode("ascii")
            except UnicodeDecodeError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned malformed integration commit metadata."
                ) from None
            self._validate_oid(value, "Git returned malformed integration commit metadata.")
            if key == b"tree":
                if tree is not None:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE,
                        "Git returned malformed integration commit metadata.",
                    )
                tree = value
            else:
                parents.append(value)
        if tree is None:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned malformed integration commit metadata."
            )
        return tree, tuple(parents), message

    def _abort_and_raise(self, prepared: _PreparedIntegration, original: BaseException) -> None:
        try:
            with self._emergency_resources(prepared) as deadline:
                try:
                    if self._prove_exact_pre_state_emergency(prepared, deadline):
                        raise original
                except BaseException as proof_error:
                    if proof_error is original:
                        raise
                    # A failed or interrupted observation is not evidence of mutation, but it is
                    # also not rollback proof. Continue to the bounded abort path unconditionally.
                self._abort_and_raise_emergency(prepared, original, deadline)
        except BaseException as error:
            if error is original:
                raise
            if not isinstance(error, ManduaError):
                interruption = error
                try:
                    with self._emergency_resources(prepared) as deadline:
                        exact_pre_state = self._prove_exact_pre_state_emergency(prepared, deadline)
                except BaseException:  # noqa: BLE001 - a second interruption is ambiguous
                    raise self._rollback_interruption_error(prepared) from interruption
                if exact_pre_state:
                    raise original
                raise self._rollback_interruption_error(prepared) from interruption
            if error.message == "Integration failed and rollback could not be verified.":
                raise
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Integration failed and rollback could not be verified.",
                evidence=(
                    Evidence(
                        id="integration-rollback",
                        kind="rollback-failure",
                        details={
                            "target_ref": prepared.target_ref,
                            "captured_target_oid": prepared.target_oid,
                            "captured_source_oid": prepared.source_oid,
                            "abort_error": error.message,
                        },
                    ),
                ),
                recovery="Run git merge --abort, then inspect HEAD, both branch refs, and status.",
            ) from None

    def _abort_and_raise_emergency(
        self,
        prepared: _PreparedIntegration,
        original: BaseException,
        deadline: float,
    ) -> None:
        details: dict[str, object] = {
            "target_ref": prepared.target_ref,
            "captured_target_oid": prepared.target_oid,
            "captured_source_oid": prepared.source_oid,
            "abort_exit_code": None,
            "abort_stdout": None,
            "abort_stderr": None,
            "abort_error": None,
            "observed_head_ref": None,
            "observed_head_oid": None,
            "observed_target_oid": None,
            "observed_source_oid": None,
            "observed_status": None,
            "observed_status_bytes": None,
            "observed_status_sha256": None,
            "observed_status_stderr": None,
            "observed_status_exit_code": None,
            "observed_merge_state": None,
            "expected_ignored_fingerprint": (
                self._ignored_state.digest if self._ignored_state is not None else None
            ),
            "observed_ignored_fingerprint": None,
            "observed_ignored_paths": None,
            "observed_ignored_bytes": None,
            "observation_errors": (),
        }
        errors: list[str] = []
        abort_succeeded = False
        try:
            output = self._runner.run_text(
                ["merge", "--abort"],
                check=False,
                timeout_seconds=self._emergency_remaining(deadline),
                isolated_configuration=True,
                repository_authority=self._required_repository_authority(),
            )
            details["abort_exit_code"] = output.returncode
            details["abort_stdout"] = output.stdout[: self._limits.max_excerpt_chars]
            details["abort_stderr"] = output.stderr[: self._limits.max_excerpt_chars]
            abort_succeeded = output.returncode == 0 and not output.stdout and not output.stderr
        except ManduaError as error:
            details["abort_error"] = error.message
            errors.append(f"abort: {error.message}")

        try:
            symbolic = self._runner.run_text(
                ["symbolic-ref", "--quiet", "HEAD"],
                check=False,
                timeout_seconds=self._emergency_remaining(deadline),
                isolated_configuration=True,
                repository_authority=self._required_repository_authority(),
            )
            details["observed_head_ref"] = self._strict_branch_ref(
                symbolic, "Git returned an invalid checked-out branch."
            )
        except ManduaError as error:
            errors.append(f"HEAD ref: {error.message}")
        try:
            details["observed_head_oid"] = self._capture_ref_emergency("HEAD", deadline)
        except ManduaError as error:
            errors.append(f"HEAD: {error.message}")
        try:
            details["observed_target_oid"] = self._capture_ref_emergency(
                prepared.target_ref, deadline
            )
        except ManduaError as error:
            errors.append(f"target: {error.message}")
        try:
            details["observed_source_oid"] = self._capture_ref_emergency(
                prepared.source_ref, deadline
            )
        except ManduaError as error:
            errors.append(f"source: {error.message}")
        try:
            status = self._runner.run(
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                check=False,
                timeout_seconds=self._emergency_remaining(deadline),
                isolated_configuration=True,
                repository_authority=self._required_repository_authority(),
            )
            details["observed_status_exit_code"] = status.returncode
            details["observed_status"] = status.stdout.hex()[: self._limits.max_excerpt_chars]
            details["observed_status_bytes"] = len(status.stdout)
            details["observed_status_sha256"] = hashlib.sha256(status.stdout).hexdigest()
            details["observed_status_stderr"] = status.stderr.decode("utf-8", errors="replace")[
                : self._limits.max_excerpt_chars
            ]
            if status.returncode != 0 or status.stderr:
                errors.append("status: Git returned invalid status output.")
        except ManduaError as error:
            errors.append(f"status: {error.message}")
        try:
            details["observed_merge_state"] = self._merge_state_entries(
                timeout_seconds=self._emergency_remaining(deadline)
            )
        except ManduaError as error:
            errors.append(f"merge state: {error.message}")
        observed_ignored: _IgnoredState | None = None
        try:
            observed_ignored = self._capture_relevant_ignored_state(prepared)
            details["observed_ignored_fingerprint"] = observed_ignored.digest
            details["observed_ignored_paths"] = observed_ignored.path_count
            details["observed_ignored_bytes"] = observed_ignored.content_bytes
        except ManduaError as error:
            errors.append(f"ignored content: {error.message}")

        details["observation_errors"] = tuple(errors)
        exact_pre_state = (
            abort_succeeded
            and not errors
            and details["observed_head_ref"] == prepared.target_ref
            and details["observed_head_oid"] == prepared.target_oid
            and details["observed_target_oid"] == prepared.target_oid
            and details["observed_source_oid"] == prepared.source_oid
            and details["observed_status_exit_code"] == 0
            and details["observed_status"] == ""
            and details["observed_merge_state"] == ()
            and self._ignored_state is not None
            and observed_ignored == self._ignored_state
        )
        if exact_pre_state:
            raise original
        raise ManduaError(
            ErrorCode.GIT_FAILURE,
            "Integration failed and rollback could not be verified.",
            evidence=(
                Evidence(
                    id="integration-rollback",
                    kind="rollback-failure",
                    details=details,
                ),
            ),
            recovery="Run git merge --abort, then inspect HEAD, both branch refs, and status.",
        ) from original

    def _prove_exact_pre_state_emergency(
        self, prepared: _PreparedIntegration, deadline: float
    ) -> bool:
        self._require_no_alternates()
        head_ref, head_oid = self._capture_head_emergency(deadline)
        target_oid = self._capture_ref_emergency(prepared.target_ref, deadline)
        source_oid = self._capture_ref_emergency(prepared.source_ref, deadline)
        self._pin_repository_attributes(prepared.target_oid)
        status = self._runner.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=False,
            timeout_seconds=self._emergency_remaining(deadline),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        merge_state = self._merge_state_entries(timeout_seconds=self._emergency_remaining(deadline))
        ignored_state = self._capture_relevant_ignored_state(prepared)
        return (
            head_ref == prepared.target_ref
            and head_oid == prepared.target_oid
            and target_oid == prepared.target_oid
            and source_oid == prepared.source_oid
            and status.returncode == 0
            and not status.stdout
            and not status.stderr
            and not merge_state
            and self._ignored_state is not None
            and ignored_state == self._ignored_state
        )

    @staticmethod
    def _rollback_interruption_error(prepared: _PreparedIntegration) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "Integration failed and rollback could not be verified.",
            evidence=(
                Evidence(
                    id="integration-rollback",
                    kind="rollback-failure",
                    details={
                        "target_ref": prepared.target_ref,
                        "captured_target_oid": prepared.target_oid,
                        "captured_source_oid": prepared.source_oid,
                        "observation_errors": ("rollback proof was interrupted",),
                    },
                ),
            ),
            recovery="Run git merge --abort, then inspect HEAD, both branch refs, and status.",
        )

    def _capture_head(self) -> tuple[str, str]:
        symbolic = self._runner.run_text(
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if symbolic.returncode == 1 and not symbolic.stdout and not symbolic.stderr:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Applying an integration requires HEAD to name the target branch.",
            )
        ref = self._strict_branch_ref(symbolic, "Git returned an invalid checked-out branch.")
        return ref, self._capture_branch_tip(ref)

    def _capture_head_emergency(self, deadline: float) -> tuple[str, str]:
        symbolic = self._runner.run_text(
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            timeout_seconds=self._emergency_remaining(deadline),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        ref = self._strict_branch_ref(symbolic, "Git returned an invalid checked-out branch.")
        return ref, self._capture_ref_emergency(ref, deadline)

    def _capture_branch_tip(self, branch_ref: str) -> str:
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--end-of-options", branch_ref],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        oid = self._strict_stdout_oid(output, "A local integration branch is unavailable.")
        object_type = self._runner.run_text(
            ["cat-file", "-t", oid],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if (
            object_type.returncode == 0
            and not object_type.stderr
            and object_type.stdout == "commit\n"
        ):
            return oid
        if object_type.returncode == 0 and not object_type.stderr:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "An integration branch must point directly to a commit.",
            )
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not inspect an integration branch tip.")

    def _capture_ref_emergency(self, branch_ref: str, deadline: float) -> str:
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--end-of-options", branch_ref],
            check=False,
            timeout_seconds=self._emergency_remaining(deadline),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        return self._strict_stdout_oid(output, "Git returned an invalid branch tip.")

    def _require_ref_tips(
        self,
        target_ref: str,
        target_oid: str,
        source_ref: str,
        source_oid: str,
    ) -> None:
        self._require_ref_tip(target_ref, target_oid)
        self._require_ref_tip(source_ref, source_oid)

    def _require_ref_tip(self, branch_ref: str, expected_oid: str) -> None:
        actual = self._capture_branch_tip(branch_ref)
        if actual != expected_oid:
            raise self._race_error("An integration branch changed during validation.")

    def _require_clean_repository(self, *, post_commit: bool = False) -> None:
        output = self._runner.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
            isolated_configuration=True,
            repository_authority=self._required_repository_authority(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify repository cleanliness.")
        if output.stdout:
            if post_commit:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE,
                    "The integration commit exists but the repository is not clean.",
                    recovery="Inspect the target branch, index, and worktree before continuing.",
                )
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "Applying an integration requires a completely clean index and worktree.",
            )

    def _merge_is_in_progress(self) -> bool:
        return bool(self._merge_state_entries())

    def _merge_is_in_progress_emergency(self, deadline: float) -> bool:
        return bool(self._merge_state_entries(timeout_seconds=self._emergency_remaining(deadline)))

    def _merge_state_entries(self, *, timeout_seconds: float | None = None) -> tuple[str, ...]:
        timeout = timeout_seconds if timeout_seconds is not None else self._remaining_timeout()
        authority = self._repository_authority
        output = self._runner.run_text(
            ["rev-parse", "--absolute-git-dir"],
            check=False,
            timeout_seconds=timeout,
            isolated_configuration=authority is not None,
            repository_authority=authority,
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid merge-state output.")
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n" or "\x00" in lines[0]:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid merge-state output.")
        directory = Path(lines[0])
        try:
            resolved = directory.resolve(strict=True)
        except OSError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "The Git merge-state directory is unavailable."
            ) from None
        if not directory.is_absolute() or resolved != directory or not directory.is_dir():
            raise ManduaError(ErrorCode.GIT_FAILURE, "The Git merge-state directory is invalid.")
        present: list[str] = []
        for name in _MERGE_STATE_NAMES:
            try:
                os.lstat(directory / name)
            except FileNotFoundError:
                continue
            except OSError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git merge state could not be inspected."
                ) from None
            present.append(name)
        return tuple(present)

    def _commit_tree(self, commit_oid: str) -> str:
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--end-of-options", f"{commit_oid}^{{tree}}"],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        return self._strict_stdout_oid(output, "Git returned an invalid commit tree.")

    def _diff_summary(self, target_oid: str, tree_oid: str) -> str:
        output = self._runner.run_text(
            [
                "diff",
                f"--stat=80,80,{self._limits.max_commits}",
                "--no-ext-diff",
                "--no-textconv",
                target_oid,
                tree_oid,
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git could not summarize the integration tree."
            )
        return output.stdout[: self._limits.max_excerpt_chars]

    def _branch_ref(self, value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value.startswith(("-", "refs/"))
            or "\x00" in value
            or len(value) > self._limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, f"The integration {label} is invalid.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, f"The integration {label} is invalid."
            ) from None
        branch_ref = f"refs/heads/{value}"
        output = self._runner.run_text(
            ["check-ref-format", branch_ref],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if output.returncode != 0 or output.stdout or output.stderr:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, f"The integration {label} is invalid.")
        return branch_ref

    def _validate_request(self, request: object, apply: object) -> None:
        self._remaining_timeout()
        if not isinstance(apply, bool):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The apply setting is invalid.")
        if not isinstance(request, IntegrationRequest) or not isinstance(
            request.metadata, CommitMetadata
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The integration request is invalid.")
        for value in (
            request.metadata.memory_type,
            request.metadata.scope,
            request.metadata.agent_id,
        ):
            if not isinstance(value, str):
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED, "Required integration metadata is invalid."
                )

    def _policy(self, repository_policy: RepositoryPolicy) -> Policy:
        limits = replace(self._limits, timeout_seconds=self._remaining_timeout())
        return Policy(
            self._repository,
            GitRunner(
                self._repository,
                limits=limits,
                operation_budget=self._budget,
            ),
            repository_policy,
            limits,
        )

    def _object_id_width(self) -> int:
        if self._object_width is None:
            output = self._runner.run_text(
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

    def _validate_oid(self, value: str, message: str) -> str:
        if (
            not isinstance(value, str)
            or _OBJECT_ID.fullmatch(value) is None
            or len(value) != self._object_id_width()
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        return value

    def _strict_stdout_oid(self, output: GitOutput[str], message: str) -> str:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        return self._validate_oid(lines[0], message)

    def _strict_branch_ref(self, output: GitOutput[str], message: str) -> str:
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        ref = lines[0]
        if not ref.startswith("refs/heads/") or ref == "refs/heads/":
            raise ManduaError(ErrorCode.GIT_FAILURE, message)
        return ref

    def _command_evidence(self, evidence_id: str, output: GitOutput[str]) -> Evidence:
        excerpt = (output.stderr or output.stdout)[: self._limits.max_excerpt_chars]
        return Evidence(
            id=evidence_id,
            kind="git-command",
            excerpt=excerpt or None,
            details={"exit_code": output.returncode},
        )

    def _binary_command_evidence(self, evidence_id: str, output: GitOutput[bytes]) -> Evidence:
        raw = output.stderr or output.stdout
        excerpt = raw.decode("utf-8", errors="replace")[: self._limits.max_excerpt_chars]
        return Evidence(
            id=evidence_id,
            kind="git-command",
            excerpt=excerpt or None,
            details={"exit_code": output.returncode},
        )

    def _raise_missing_or_git_failure(self, output: GitOutput[str], message: str) -> None:
        missing = tuple(
            dict.fromkeys(
                match.group(0)
                for match in _OBJECT_ID_IN_TEXT.finditer(output.stderr.casefold())
                if len(match.group(0)) == self._object_id_width()
            )
        )
        if missing:
            object_id = missing[0]
            raise ManduaError(
                ErrorCode.MISSING_OBJECT,
                "Integration requires a missing Git history object.",
                evidence=(
                    Evidence(
                        id=f"missing-object:{object_id}",
                        kind="missing-object",
                        oid=object_id,
                        details={"object_oid": object_id},
                    ),
                ),
                recovery="Restore the required local object before retrying integration.",
            )
        raise ManduaError(ErrorCode.GIT_FAILURE, message)

    def _remaining_timeout(self) -> float:
        remaining = self._deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Integration exceeded the configured shared time limit.",
            )
        return remaining

    @staticmethod
    def _emergency_remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED,
                "Integration rollback exceeded the emergency time limit.",
            )
        return remaining

    @staticmethod
    def _race_error(message: str) -> ManduaError:
        return ManduaError(
            ErrorCode.POLICY_VIOLATION,
            message,
            recovery="Review both branches, attributes, index, and worktree before retrying.",
        )

    @staticmethod
    def _ambiguous_post_commit_error(
        prepared: _PreparedIntegration, observation: str
    ) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "The integration commit state could not be verified.",
            evidence=(
                Evidence(
                    id="integration-post-commit",
                    kind="post-commit-verification",
                    details={
                        "target_ref": prepared.target_ref,
                        "captured_target_oid": prepared.target_oid,
                        "captured_source_oid": prepared.source_oid,
                        "expected_tree": prepared.proposed_tree,
                        "observed": observation,
                    },
                ),
            ),
            recovery="Inspect the target ref, HEAD, commit tree, parents, and merge state.",
        )

    @staticmethod
    def _ambiguous_mutation_boundary_error(
        prepared: _PreparedIntegration, observation: str
    ) -> ManduaError:
        return ManduaError(
            ErrorCode.GIT_FAILURE,
            "Integration refs changed at the final mutation boundary.",
            evidence=(
                Evidence(
                    id="integration-mutation-boundary",
                    kind="ref-race",
                    details={
                        "target_ref": prepared.target_ref,
                        "captured_target_oid": prepared.target_oid,
                        "source_ref": prepared.source_ref,
                        "captured_source_oid": prepared.source_oid,
                        "observed": observation,
                    },
                ),
            ),
            recovery="Inspect HEAD and both branch refs before retrying integration.",
        )

    def _result(
        self,
        prepared: _PreparedIntegration,
        *,
        commit_oid: str | None,
        applied: bool,
        warnings: tuple[str, ...] = (),
    ) -> MemoryResult:
        evidence_id = "integration-1"
        details = {
            "parent_oids": [prepared.target_oid, prepared.source_oid],
            "proposed_tree": prepared.proposed_tree,
            "source_ref": prepared.source_ref,
            "target_ref": prepared.target_ref,
            "diff_summary": prepared.diff_summary,
            "invariants_validated": prepared.invariant_count,
        }
        if commit_oid is not None:
            details["commit_oid"] = commit_oid
        return MemoryResult(
            operation="integrate",
            answer=(
                "The validated non-fast-forward integration was applied."
                if applied
                else "The non-fast-forward integration was validated and previewed."
            ),
            observed=(
                Claim(
                    "Both immutable parents and the proposed tree passed validation.",
                    (evidence_id,),
                ),
            ),
            evidence=(
                Evidence(
                    id=evidence_id,
                    kind="integration-preview",
                    oid=prepared.proposed_tree,
                    ref=prepared.target_ref,
                    excerpt=prepared.diff_summary or None,
                    details=details,
                ),
            ),
            history_scope=HistoryScope(
                start_oid=prepared.target_oid,
                end_oid=commit_oid or prepared.target_oid,
                refs=(prepared.target_ref, prepared.source_ref),
            ),
            changes=(
                PlannedChange(
                    action="update-ref",
                    target=prepared.target_ref,
                    before_oid=prepared.target_oid,
                    after_oid=commit_oid,
                ),
            ),
            warnings=warnings,
            applied=applied,
        )


def integration_result(
    repository: Path,
    limits: QueryLimits,
    request: IntegrationRequest,
    *,
    apply: bool = False,
    repository_policy: RepositoryPolicy | None = None,
) -> MemoryResult:
    """Build one integration result through an operation-scoped writer."""
    return _integration_outcome(
        repository,
        limits,
        request,
        apply=apply,
        repository_policy=repository_policy,
    ).result


def _integration_outcome(
    repository: Path,
    limits: QueryLimits,
    request: IntegrationRequest,
    *,
    apply: bool = False,
    repository_policy: RepositoryPolicy | None = None,
) -> _IntegrationOutcome:
    """Build an internal result carrying the exact policy proven by an applied integration."""
    return IntegrationWriter(
        repository,
        limits,
        repository_policy=repository_policy,
    )._integrate_outcome(request, apply=apply)
