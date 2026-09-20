"""Read repository state through the bounded Git runner."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mandua.bounds import (
    bound_history_scope,
    normalize_timeout_seconds,
    validate_query_limits,
)
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import (
    GitOperationBudget,
    GitOutput,
    GitRepositoryAuthority,
    GitRunner,
    _GitBudgetLimits,
    _RepositoryAuthorityLayout,
)
from mandua.metadata import parse_trailers
from mandua.models import Evidence, HistoryScope, QueryLimits

_OBJECT_ID = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])")
_FULL_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_LOG_RECORD = re.compile(rb"\0\0([0-9a-f]{40}|[0-9a-f]{64})\0")
_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64}) [0-9]+ [0-9]+(?: [0-9]+)?$")
_COMPARISON_NAME_STATUS = re.compile(rb"(?:[ADMTUXB]|[RC](?:100|[1-9][0-9]?|0))$")
_UNAVAILABLE_SIGNATURE_DIAGNOSTIC = re.compile(
    r"\b(?:bad object|corrupt(?:ed|ion)?|missing|unable to read|could not read)\b",
    re.IGNORECASE,
)
_MAX_HISTORY_REFS = 256
_MAX_HISTORY_REVISION_ARGUMENT_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class StatusEntry:
    """One observed index or working-tree state for a path."""

    path: str
    state: str
    previous_path: str | None = None
    copy_from: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryStatus:
    """A bounded snapshot of repository state and the history it can inspect."""

    branch: str | None
    head_oid: str | None
    upstream: str | None
    upstream_oid: str | None
    detached: bool
    unborn: bool
    entries: tuple[StatusEntry, ...]
    worktrees: tuple[str, ...]
    history_scope: HistoryScope
    history_access_failed: bool = False


@dataclass(frozen=True, slots=True)
class CommitRecord:
    """One bounded commit record returned by a read-only history query."""

    oid: str
    parents: tuple[str, ...] = ()
    author_time: int = 0
    subject: str = ""
    body: str = ""
    paths: tuple[str, ...] = ()
    trailers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BlameLine:
    """One line attribution read from Git blame porcelain output."""

    oid: str
    content: str


@dataclass(frozen=True, slots=True)
class ReviewNote:
    """The availability and bounded content of one local review note lookup."""

    content: str | None
    ref_available: bool
    lookup_failed: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _NotesRefState:
    """Direct-commit availability of the configured notes ref."""

    oid: str | None
    available: bool
    lookup_failed: bool = False


@dataclass(frozen=True, slots=True)
class ComparisonNameStatus:
    """One NUL-delimited name-status record from a state comparison."""

    status: str
    old_path: bytes | None
    new_path: bytes | None


@dataclass(frozen=True, slots=True)
class ComparisonState:
    """Bounded stat and NUL-safe names describing two resolved tree states."""

    stat: str
    names: tuple[ComparisonNameStatus, ...]
    paths_decoded_with_replacement: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryCommitStatus:
    """The local type and availability of one full object ID used by recovery."""

    kind: str
    missing_objects: tuple[str, ...] = ()


class RepositoryInspector:
    """Inspect one Git worktree without changing its index, files, or refs."""

    def __init__(
        self,
        repository: Path,
        *,
        limits: QueryLimits | None = None,
        notes_ref: str = "refs/notes/review",
        operation_budget: GitOperationBudget | None = None,
        _budget_limits: _GitBudgetLimits | None = None,
    ) -> None:
        self._limits = validate_query_limits(QueryLimits() if limits is None else limits)
        self._notes_ref = self._validated_notes_ref(notes_ref)
        self._operation_budget = operation_budget
        self._runner = GitRunner(
            repository,
            limits=self._limits,
            operation_budget=operation_budget,
            _budget_limits=_budget_limits,
        )
        self._object_width: int | None = None
        self._recovery_authority: GitRepositoryAuthority | None = None

    @property
    def notes_ref(self) -> str:
        """Return the validated local review-notes ref used by this inspector."""
        return self._notes_ref

    def remaining_operation_timeout(self) -> float:
        """Return the remaining shared read allowance, or the configured standalone timeout."""
        if self._operation_budget is None:
            return float(self._limits.timeout_seconds)
        return self._operation_budget.remaining_timeout()

    def _replace_operation_budget(
        self,
        operation_budget: GitOperationBudget,
        budget_limits: _GitBudgetLimits,
    ) -> None:
        """Start a fresh aggregate budget while preserving the retained inspector boundary."""
        if (
            not isinstance(operation_budget, GitOperationBudget)
            or not isinstance(budget_limits, _GitBudgetLimits)
            or operation_budget.limits != budget_limits
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git operation budget is invalid.")
        self._operation_budget = operation_budget
        self._runner._operation_budget = operation_budget
        self._runner._git_limits = budget_limits

    def open_recovery_authority(
        self, *, expected_layout: _RepositoryAuthorityLayout | None = None
    ) -> GitRepositoryAuthority:
        """Bind recovery commands to one physical repository layout."""
        return self._runner.open_repository_authority(
            None,
            expected_layout=expected_layout,
        )

    def plan_recovery_ref_emergency_budget(
        self,
        repository_layout: _RepositoryAuthorityLayout,
        branch_ref: str,
        *,
        object_id_length: int,
    ) -> _GitBudgetLimits:
        """Bound one fresh recovery-ref proof from its captured layout and exact argv."""
        return self._runner.plan_repository_reopen_budget(
            repository_layout,
            (
                (
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "--end-of-options",
                    branch_ref,
                ),
            ),
            expected_output_bytes=object_id_length + 1,
        )

    def _replace_recovery_authority(
        self, authority: GitRepositoryAuthority | None
    ) -> GitRepositoryAuthority | None:
        """Activate one already bound authority while preserving the prior scope."""
        if authority is not None and not isinstance(authority, GitRepositoryAuthority):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git authority is invalid.")
        previous = self._recovery_authority
        self._recovery_authority = authority
        return previous

    def validate(self, *, timeout_seconds: float | None = None) -> None:
        """Reject paths that do not name a Git worktree."""
        output = self._runner.run_text(
            ["rev-parse", "--is-inside-work-tree"],
            check=False,
            timeout_seconds=timeout_seconds,
        )
        if output.returncode != 0 or output.stdout.strip() != "true":
            from mandua.errors import ErrorCode, ManduaError

            raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")

    def recovery_source(
        self,
        source: str,
        *,
        max_output_bytes: int,
        timeout_seconds: float,
    ) -> GitOutput[bytes]:
        """Read one fixed, local-only recovery source without interpreting its data."""
        commands = {
            "refs": [
                "for-each-ref",
                "--format=%00%(objectname)%00%(objecttype)%00%(*objectname)%00%(*objecttype)%00%(refname)%00%(subject)%00",
                "refs/heads",
                "refs/tags",
                "refs/remotes",
            ],
            "reflog": ["reflog", "show", "--all", "--format=%x00%H%x00%s%x00"],
            "fsck": ["fsck", "--unreachable", "--no-reflogs", "--no-progress"],
        }
        arguments = commands.get(source)
        if arguments is None:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery source is invalid.")
        return self._runner.run(
            arguments,
            check=False,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

    def recovery_commit_available(
        self, oid: str, *, timeout_seconds: float
    ) -> RecoveryCommitStatus:
        """Classify one local object without revision parsing or ambiguous human stderr."""
        if _FULL_OBJECT_ID.fullmatch(oid) is None:
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        output = self._runner.run_text(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            check=False,
            timeout_seconds=timeout_seconds,
            input_bytes=f"{oid}\n".encode("ascii"),
            isolated_configuration=True,
        )
        records = output.stdout.splitlines()
        fields = records[0].split() if len(records) == 1 else []
        if (
            not output.stderr
            and len(fields) == 2
            and fields[0].lower() == oid.lower()
            and fields[1] == "missing"
        ):
            return RecoveryCommitStatus(kind="missing", missing_objects=(oid.lower(),))
        diagnostic_objects = self._missing_objects(output.stderr)
        if output.returncode != 0 or output.stderr:
            if oid.lower() in diagnostic_objects:
                return RecoveryCommitStatus(kind="missing", missing_objects=(oid.lower(),))
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify the recovery commit.")
        if len(records) != 1:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid recovery object record."
            )
        if len(fields) != 2 or fields[0].lower() != oid.lower():
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid recovery object record."
            )
        object_type = fields[1]
        if object_type == "commit":
            return RecoveryCommitStatus(kind="commit")
        if object_type == "missing":
            return RecoveryCommitStatus(kind="missing", missing_objects=(oid.lower(),))
        if object_type in {"blob", "tree", "tag"}:
            return RecoveryCommitStatus(kind="non_commit")
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid recovery object record.")

    def recovery_object_id_length(
        self,
        *,
        timeout_seconds: float,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> int:
        """Return the repository object ID width without resolving user input."""
        repository_authority = repository_authority or self._recovery_authority
        output = self._runner.run_text(
            ["rev-parse", "--show-object-format"],
            check=False,
            timeout_seconds=timeout_seconds,
            isolated_configuration=repository_authority is not None,
            repository_authority=repository_authority,
        )
        if output.returncode != 0:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not determine the object format.")
        width = {"sha1": 40, "sha256": 64}.get(output.stdout.strip())
        if width is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an unsupported object format.")
        return width

    def recovery_branch_ref(self, branch: str, *, timeout_seconds: float) -> str:
        """Validate a branch shorthand and return its canonical local ref name."""
        if (
            not isinstance(branch, str)
            or not branch
            or "\x00" in branch
            or len(branch) > self._limits.max_input_chars
            or branch.startswith("-")
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery branch is invalid.")
        output = self._runner.run_text(
            ["check-ref-format", "--branch", branch],
            check=False,
            timeout_seconds=timeout_seconds,
        )
        canonical_branch = output.stdout.strip()
        if output.returncode != 0 or not canonical_branch or "\x00" in canonical_branch:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery branch is invalid.")
        canonical_ref = f"refs/heads/{canonical_branch}"
        if len(canonical_ref) > self._limits.max_excerpt_chars:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The recovery branch exceeds the public display limit.",
            )
        return canonical_ref

    def recovery_ref_exists(
        self,
        ref: str,
        *,
        timeout_seconds: float,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> bool:
        """Check one already canonical local branch ref without resolving a revision expression."""
        repository_authority = repository_authority or self._recovery_authority
        if not ref.startswith("refs/heads/"):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery ref is invalid.")
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", ref],
            check=False,
            timeout_seconds=timeout_seconds,
            isolated_configuration=repository_authority is not None,
            repository_authority=repository_authority,
        )
        if output.returncode == 0:
            return True
        if output.returncode == 1:
            return False
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify the recovery branch.")

    def recovery_ref_oid(
        self,
        ref: str,
        *,
        object_id_length: int,
        timeout_seconds: float,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> str | None:
        """Read one exact recovery ref as absent or one full-width object ID."""
        repository_authority = repository_authority or self._recovery_authority
        if (
            not ref.startswith("refs/heads/")
            or object_id_length not in {40, 64}
            or isinstance(object_id_length, bool)
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery ref is invalid.")
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", ref],
            check=False,
            timeout_seconds=timeout_seconds,
            isolated_configuration=repository_authority is not None,
            repository_authority=repository_authority,
        )
        if output.returncode == 1 and not output.stdout and not output.stderr:
            return None
        lines = output.stdout.splitlines()
        if (
            output.returncode != 0
            or output.stderr
            or len(lines) != 1
            or output.stdout != f"{lines[0]}\n"
            or len(lines[0]) != object_id_length
            or _FULL_OBJECT_ID.fullmatch(lines[0]) is None
        ):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid recovery ref value.")
        return lines[0].lower()

    def create_recovery_ref(
        self,
        ref: str,
        oid: str,
        *,
        timeout_seconds: float,
        repository_authority: GitRepositoryAuthority | None = None,
    ) -> str:
        """Atomically create a branch ref only when it is currently absent."""
        repository_authority = repository_authority or self._recovery_authority
        if not ref.startswith("refs/heads/") or _FULL_OBJECT_ID.fullmatch(oid) is None:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The recovery ref is invalid.")
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        expected_length = self.recovery_object_id_length(
            timeout_seconds=self._remaining_timeout(deadline),
            repository_authority=repository_authority,
        )
        if len(oid) != expected_length:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an unsupported object format.")
        output = self._runner.run_text(
            ["update-ref", "--no-deref", ref, oid.lower(), "0" * expected_length],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
            isolated_configuration=repository_authority is not None,
            repository_authority=repository_authority,
        )
        if output.returncode == 0:
            return "created"
        if self.recovery_ref_exists(
            ref,
            timeout_seconds=self._remaining_timeout(deadline),
            repository_authority=repository_authority,
        ):
            return "conflict"
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not create the recovery branch.")

    def status(self) -> RepositoryStatus:
        """Read porcelain state and disclose the scope of reachable history."""
        self.validate()
        raw_status = self._runner.run(
            ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"]
        ).stdout
        headers, entries = self._parse_porcelain(raw_status)
        head_oid = self._resolved_head()
        configured_upstream = headers.get("branch.upstream")
        upstream_oid = self._resolve_upstream(configured_upstream)
        upstream = configured_upstream if upstream_oid is not None else None
        history_scope, history_access_failed = self._history_scope(head_oid)
        return RepositoryStatus(
            branch=self._branch(headers),
            head_oid=head_oid,
            upstream=upstream,
            upstream_oid=upstream_oid,
            detached=headers.get("branch.head") == "(detached)",
            unborn=headers.get("branch.oid") == "(initial)",
            entries=entries,
            worktrees=self._worktrees(),
            history_scope=history_scope,
            history_access_failed=history_access_failed,
        )

    def history_scope(
        self, head_oid: str | None = None, *, timeout_seconds: float | None = None
    ) -> HistoryScope:
        """Describe the bounded commit graph reachable from local repository refs."""
        end_oid = head_oid if head_oid is not None else self._resolved_head()
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + self._effective_timeout(timeout_seconds)
        )
        history_scope, _ = self._history_scope(end_oid, deadline=deadline)
        return history_scope

    def comparison_history_scope(
        self, left: str, right: str, *, timeout_seconds: float | None = None
    ) -> HistoryScope:
        """Describe bounded history availability reachable from exactly two commit roots."""
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        self.validate(timeout_seconds=self._remaining_timeout(deadline))
        if not _FULL_OBJECT_ID.fullmatch(left) or not _FULL_OBJECT_ID.fullmatch(right):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        revisions = (left, right)
        count_probe = self._runner.run_text(
            [
                "rev-list",
                "--count",
                f"--max-count={self._limits.max_commits + 1}",
                "--end-of-options",
                *revisions,
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
        )
        missing_objects = self._missing_objects(count_probe.stderr)
        if count_probe.returncode != 0:
            return self.bound_history_scope(
                HistoryScope(
                    end_oid=right,
                    refs=revisions,
                    shallow=(
                        False
                        if missing_objects
                        else self._comparison_has_reachable_shallow_boundary(
                            revisions, deadline=deadline
                        )
                    ),
                    notes_available=self._notes_available(deadline=deadline),
                    missing_objects=missing_objects,
                )
            )
        probed_count = int(count_probe.stdout.strip() or "0")
        truncated = probed_count > self._limits.max_commits
        commits = self._runner.run_text(
            [
                "rev-list",
                f"--max-count={self._limits.max_commits}",
                "--end-of-options",
                *revisions,
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
        )
        missing_objects += tuple(
            item for item in self._missing_objects(commits.stderr) if item not in missing_objects
        )
        commit_oids = tuple(line for line in commits.stdout.splitlines() if line)
        return self.bound_history_scope(
            HistoryScope(
                start_oid=commit_oids[-1] if commit_oids else None,
                end_oid=right,
                refs=revisions,
                commit_count=min(probed_count, self._limits.max_commits),
                truncated=truncated,
                shallow=self._comparison_has_reachable_shallow_boundary(
                    revisions, deadline=deadline
                ),
                notes_available=self._notes_available(deadline=deadline),
                missing_objects=missing_objects,
            )
        )

    def resolve_commit(self, revision: str, *, timeout_seconds: float | None = None) -> str:
        """Resolve a caller-supplied revision before a history query uses it."""
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        self.validate(timeout_seconds=self._remaining_timeout(deadline))
        return self._runner.resolve_commit(
            revision, timeout_seconds=self._remaining_timeout(deadline)
        )

    def signature_status(self, oid: str, *, timeout_seconds: float | None = None) -> bool:
        """Run bounded fail-closed verification and prove the target is still a commit."""
        if _FULL_OBJECT_ID.fullmatch(oid) is None:
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        normalized = oid.lower()
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        before = self.recovery_commit_available(
            normalized,
            timeout_seconds=self._remaining_timeout(deadline),
        )
        verification = self._runner.run(
            ["verify-commit", "--raw", "--end-of-options", normalized],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
            max_output_bytes=self._limits.max_output_bytes,
            isolated_configuration=True,
        )
        after = self.recovery_commit_available(
            normalized,
            timeout_seconds=self._remaining_timeout(deadline),
        )
        if (
            before.kind == "missing"
            or after.kind == "missing"
            or self._verification_reports_unavailable(verification, normalized)
        ):
            raise ManduaError(
                ErrorCode.MISSING_OBJECT,
                f"Git object {normalized} is missing or corrupt.",
                evidence=(
                    Evidence(
                        id=f"missing-object:{normalized}",
                        kind="missing-object",
                        oid=normalized,
                        details={"object_oid": normalized},
                    ),
                ),
            )
        if before.kind != "commit" or after.kind != "commit":
            raise ManduaError(ErrorCode.INVALID_REVISION, "The signature target is not a commit.")
        # The deterministic PoC has no trusted verifier or trust-root policy. Git still parses the
        # exact signature command above, but controlled command-line config replaces every
        # baseline verifier program with the non-executable OS sink after repository precedence.
        # Therefore even return code zero cannot become an authentication claim here.
        return False

    @staticmethod
    def _verification_reports_unavailable(output: GitOutput[bytes], oid: str) -> bool:
        """Recognize only an exact target OID in one bounded unavailable-object diagnostic."""
        for payload in (output.stdout, output.stderr):
            for line in payload.decode("utf-8", errors="replace").splitlines():
                if _UNAVAILABLE_SIGNATURE_DIAGNOSTIC.search(line) is None:
                    continue
                exact_oids = {match.group(0).lower() for match in _OBJECT_ID.finditer(line)}
                if oid in exact_oids:
                    return True
        return False

    def bound_history_scope(self, scope: HistoryScope) -> HistoryScope:
        """Bound repository-derived ref labels before they enter a public result."""
        return bound_history_scope(scope, self._limits.max_excerpt_chars)

    def merge_base(self, left: str, right: str) -> str:
        """Return the merge base between already resolved commit object IDs."""
        merge_base = self.merge_base_or_none(left, right)
        if merge_base is None:
            raise ManduaError(ErrorCode.INVALID_REVISION, "The revisions have no merge base.")
        return merge_base

    def merge_base_or_none(
        self, left: str, right: str, *, timeout_seconds: float | None = None
    ) -> str | None:
        """Return a merge base, or None when two resolved histories are disconnected."""
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        self.validate(timeout_seconds=self._remaining_timeout(deadline))
        if not _FULL_OBJECT_ID.fullmatch(left) or not _FULL_OBJECT_ID.fullmatch(right):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        output = self._runner.run_text(
            ["merge-base", "--end-of-options", left, right],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
        )
        if output.returncode == 0 and output.stdout.strip():
            return output.stdout.strip()
        if output.returncode == 1:
            return None
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not determine the merge base.")

    def validate_path(self, path: PurePosixPath) -> None:
        """Reject a path that cannot be safely passed as repository path data."""
        self._validate_path(path)

    def exclusive_commit_oids(
        self,
        merge_base: str,
        tip: str,
        *,
        path: PurePosixPath | None = None,
        limit: int | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[str, ...]:
        """Return newest-first bounded commits in one merge-base-exclusive history."""
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        self.validate(timeout_seconds=self._remaining_timeout(deadline))
        if not _FULL_OBJECT_ID.fullmatch(merge_base) or not _FULL_OBJECT_ID.fullmatch(tip):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        if path is not None:
            self._validate_path(path)
        arguments = [
            "rev-list",
            f"--max-count={self._bounded_log_limit(limit)}",
            "--end-of-options",
            f"{merge_base}..{tip}",
        ]
        if path is not None:
            arguments.extend(("--", path.as_posix()))
        output = self._runner.run_text(
            arguments,
            timeout_seconds=self._remaining_timeout(deadline),
            literal_pathspecs=path is not None,
        )
        commits = tuple(
            line for line in output.stdout.splitlines() if _FULL_OBJECT_ID.fullmatch(line)
        )
        if len(commits) != len(tuple(line for line in output.stdout.splitlines() if line)):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid commit record.")
        return commits

    def comparison_state(
        self,
        left: str,
        right: str,
        *,
        path: PurePosixPath | None = None,
        timeout_seconds: float | None = None,
    ) -> ComparisonState:
        """Return bounded stat and NUL-safe name-status records for two resolved trees."""
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        self.validate(timeout_seconds=self._remaining_timeout(deadline))
        if not _FULL_OBJECT_ID.fullmatch(left) or not _FULL_OBJECT_ID.fullmatch(right):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        if path is not None:
            self._validate_path(path)
        stat_arguments = [
            "diff",
            f"--stat=80,80,{self._limits.max_commits}",
            "--no-ext-diff",
            "--no-textconv",
            left,
            right,
        ]
        name_arguments = [
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            left,
            right,
        ]
        if path is not None:
            stat_arguments.extend(("--", path.as_posix()))
            name_arguments.extend(("--", path.as_posix()))
        stat = self._runner.run_text(
            stat_arguments,
            timeout_seconds=self._remaining_timeout(deadline),
            literal_pathspecs=path is not None,
        ).stdout
        names, paths_decoded_with_replacement = self._parse_comparison_name_status(
            self._runner.run(
                name_arguments,
                timeout_seconds=self._remaining_timeout(deadline),
                literal_pathspecs=path is not None,
            ).stdout
        )
        return ComparisonState(
            stat=stat,
            names=names,
            paths_decoded_with_replacement=paths_decoded_with_replacement,
        )

    def stable_patch_id(
        self,
        commit: str,
        *,
        path: PurePosixPath | None = None,
        timeout_seconds: float | None = None,
    ) -> str | None:
        """Return a bounded stable patch ID for an already resolved commit."""
        if not _FULL_OBJECT_ID.fullmatch(commit):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        if path is not None:
            self._validate_path(path)
        return self._runner.stable_patch_id(commit, path, timeout_seconds=timeout_seconds)

    def log(
        self,
        revisions: tuple[str, ...],
        *,
        path: PurePosixPath | None = None,
        limit: int | None = None,
        reverse: bool = False,
    ) -> tuple[CommitRecord, ...]:
        """Read a bounded commit history through one NUL-delimited Git log call."""
        if not revisions:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "At least one revision is required.")
        bounded_limit = self._bounded_log_limit(limit)
        if path is not None:
            self._validate_path(path)
        arguments = [
            "log",
            "--no-decorate",
            f"--max-count={bounded_limit}",
            "--name-only",
            "-z",
            "--format=%x00%x00%H%x00%P%x00%at%x00%s%x00%b%x00",
        ]
        if reverse:
            arguments.append("--reverse")
        all_refs, revision_arguments = _split_log_revisions(revisions)
        arguments.extend((*all_refs, "--end-of-options", *revision_arguments))
        if path is not None:
            arguments.extend(("--", path.as_posix()))
        return self._parse_log(
            self._runner.run(arguments, literal_pathspecs=path is not None).stdout
        )

    def patch_log(
        self,
        text: str,
        *,
        context_lines: int = 0,
        path: PurePosixPath | None = None,
        limit: int | None = None,
    ) -> bytes:
        """Read bounded fixed-string pickaxe patches with caller-bounded context."""
        bounded_limit = self._bounded_log_limit(limit)
        if (
            not isinstance(context_lines, int)
            or isinstance(context_lines, bool)
            or context_lines < 0
            or context_lines > self._limits.max_input_chars
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The patch context limit is invalid.")
        if path is not None:
            self._validate_path(path)
        arguments = [
            "log",
            "--all",
            "--no-decorate",
            f"--max-count={bounded_limit}",
            f"--unified={context_lines}",
            "-S",
            text,
            "--format=%x00%x00%H%x00",
            "--patch",
            "--end-of-options",
        ]
        if path is not None:
            arguments.extend(("--", path.as_posix()))
        return self._runner.run(arguments, literal_pathspecs=path is not None).stdout

    def follow_path_log(self, path: PurePosixPath, *, limit: int | None = None) -> bytes:
        """Read bounded, NUL-delimited name-status history while following one path."""
        bounded_limit = self._bounded_log_limit(limit)
        self._validate_path(path)
        return self._runner.run(
            [
                "log",
                "--follow",
                "--name-status",
                "-z",
                "--no-decorate",
                f"--max-count={bounded_limit}",
                "--format=%x00%x00%H%x00",
                "--end-of-options",
                "--",
                path.as_posix(),
            ],
            literal_pathspecs=True,
        ).stdout

    def blame_line(self, revision: str, path: PurePosixPath, line: int) -> BlameLine:
        """Return the validated commit that last changed one line at a revision."""
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The line number must be positive.")
        self._validate_path(path)
        revision_oid = self.resolve_commit(revision)
        output = self._runner.run_text(
            [
                "blame",
                "--line-porcelain",
                "-L",
                f"{line},{line}",
                revision_oid,
                "--",
                path.as_posix(),
            ],
            literal_pathspecs=True,
        )
        lines = output.stdout.splitlines()
        if not lines:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git blame returned an invalid record.")
        first_line, *remaining = lines
        header = _BLAME_HEADER.fullmatch(first_line)
        if header is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git blame returned an invalid record.")
        content = next((item[1:] for item in remaining if item.startswith("\t")), "")
        return BlameLine(oid=header.group(1), content=content)

    def review_note(self, revision: str) -> str | None:
        """Read a local review note without fetching, updating, or creating a note ref."""
        return self.review_note_status(revision).content

    def review_note_status(
        self,
        revision: str,
        *,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
    ) -> ReviewNote:
        """Distinguish an absent note ref, no target note, and failed note lookup."""
        deadline = time.monotonic() + self._effective_timeout(timeout_seconds)
        output_limit = self._effective_review_output_limit(max_output_bytes)
        revision_oid = self.resolve_commit(
            revision, timeout_seconds=self._remaining_timeout(deadline)
        )
        ref_state = self._notes_ref_state(deadline=deadline)
        if not ref_state.available:
            return ReviewNote(
                content=None,
                ref_available=False,
                lookup_failed=ref_state.lookup_failed,
            )
        note = self._runner.run_text(
            ["notes", f"--ref={self._notes_ref}", "show", "--end-of-options", revision_oid],
            check=False,
            timeout_seconds=self._remaining_timeout(deadline),
            max_output_bytes=output_limit,
        )
        if note.returncode == 0 and not note.stderr:
            return ReviewNote(
                content=note.stdout or None,
                ref_available=True,
                warnings=note.warnings,
            )
        expected_missing = f"error: no note found for object {revision_oid}.\n"
        if note.returncode == 1 and not note.stdout and note.stderr == expected_missing:
            return ReviewNote(content=None, ref_available=True)
        return ReviewNote(content=None, ref_available=True, lookup_failed=True)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Return whether two resolved commits have a causal ancestor relationship."""
        ancestor_oid = self.resolve_commit(ancestor)
        descendant_oid = self.resolve_commit(descendant)
        return self.is_ancestor_oids(ancestor_oid, descendant_oid)

    def is_ancestor_oids(
        self, ancestor: str, descendant: str, *, timeout_seconds: float | None = None
    ) -> bool:
        """Check already observed full commit OIDs without resolving them again."""
        if not _FULL_OBJECT_ID.fullmatch(ancestor) or not _FULL_OBJECT_ID.fullmatch(descendant):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The object ID is invalid.")
        output = self._runner.run_text(
            ["merge-base", "--is-ancestor", "--end-of-options", ancestor, descendant],
            check=False,
            timeout_seconds=timeout_seconds,
        )
        if output.returncode in {0, 1}:
            return output.returncode == 0
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify commit ancestry.")

    def _history_scope(
        self, end_oid: str | None, *, deadline: float | None = None
    ) -> tuple[HistoryScope, bool]:
        refs, revisions, access_failed, missing_objects, refs_truncated = self._history_refs(
            end_oid, deadline=deadline
        )
        if not revisions:
            return self.bound_history_scope(
                HistoryScope(
                    end_oid=end_oid,
                    refs=refs,
                    truncated=refs_truncated,
                    shallow=self._is_shallow(deadline=deadline),
                    notes_available=self._notes_available(deadline=deadline),
                    missing_objects=missing_objects,
                )
            ), access_failed

        count_probe = self._history_run_text(
            [
                "rev-list",
                "--count",
                f"--max-count={self._limits.max_commits + 1}",
                "--end-of-options",
                *revisions,
            ],
            check=False,
            deadline=deadline,
        )
        missing_objects += tuple(
            item
            for item in self._missing_objects(count_probe.stderr)
            if item not in missing_objects
        )
        if count_probe.returncode != 0:
            return self.bound_history_scope(
                HistoryScope(
                    end_oid=end_oid,
                    refs=refs,
                    truncated=refs_truncated,
                    shallow=self._is_shallow(deadline=deadline),
                    notes_available=self._notes_available(deadline=deadline),
                    missing_objects=missing_objects,
                )
            ), True

        probed_count = int(count_probe.stdout.strip() or "0")
        truncated = refs_truncated or probed_count > self._limits.max_commits
        bounded_count = min(probed_count, self._limits.max_commits)
        oldest = self._history_run_text(
            [
                "rev-list",
                f"--max-count={self._limits.max_commits}",
                "--end-of-options",
                *revisions,
            ],
            check=False,
            deadline=deadline,
        )
        missing_objects += tuple(
            item for item in self._missing_objects(oldest.stderr) if item not in missing_objects
        )
        start_oid = (
            oldest.stdout.splitlines()[-1] if oldest.returncode == 0 and oldest.stdout else None
        )
        return self.bound_history_scope(
            HistoryScope(
                start_oid=start_oid,
                end_oid=end_oid,
                refs=refs,
                commit_count=bounded_count,
                truncated=truncated,
                shallow=self._is_shallow(deadline=deadline),
                notes_available=self._notes_available(deadline=deadline),
                missing_objects=missing_objects,
            )
        ), access_failed or oldest.returncode != 0

    def _bounded_log_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._limits.max_commits
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit limit must be positive.")
        return min(limit, self._limits.max_commits + 1)

    @classmethod
    def _parse_log(cls, payload: bytes) -> tuple[CommitRecord, ...]:
        records: list[CommitRecord] = []
        markers = tuple(_LOG_RECORD.finditer(payload))
        for index, marker in enumerate(markers):
            next_start = markers[index + 1].start() if index + 1 < len(markers) else len(payload)
            fields = payload[marker.end() : next_start].split(b"\0", 4)
            if len(fields) != 5:
                continue
            parents, author_time, subject, body, raw_paths = fields
            if raw_paths.startswith(b"\n"):
                raw_paths = raw_paths[1:]
            decoded_body = cls._decode_path(body)
            records.append(
                CommitRecord(
                    oid=marker.group(1).decode("ascii"),
                    parents=tuple(cls._decode_path(parent) for parent in parents.split() if parent),
                    author_time=int(author_time or b"0"),
                    subject=cls._decode_path(subject),
                    body=decoded_body,
                    paths=tuple(cls._decode_path(item) for item in raw_paths.split(b"\0") if item),
                    trailers=parse_trailers(decoded_body),
                )
            )
        return tuple(records)

    def _validate_path(self, path: PurePosixPath) -> None:
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

    @classmethod
    def _parse_comparison_name_status(
        cls, payload: bytes
    ) -> tuple[tuple[ComparisonNameStatus, ...], bool]:
        if not payload:
            return (), False
        if not payload.endswith(b"\0"):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid name-status record.")
        fields = iter(payload[:-1].split(b"\0"))
        records: list[ComparisonNameStatus] = []
        for raw_status in fields:
            if not raw_status:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an invalid name-status record."
                )
            if _COMPARISON_NAME_STATUS.fullmatch(raw_status) is None:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an invalid name-status record."
                )
            status = raw_status.decode("ascii")
            old_path: bytes | None = None
            new_path: bytes | None = None
            if status.startswith(("R", "C")):
                old_path = cls._next_comparison_path(fields)
                new_path = cls._next_comparison_path(fields)
            else:
                new_path = cls._next_comparison_path(fields)
                if status.startswith("D"):
                    old_path, new_path = new_path, None
            if not status or (old_path is None and new_path is None):
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an invalid name-status record."
                )
            records.append(
                ComparisonNameStatus(status=status, old_path=old_path, new_path=new_path)
            )
        return tuple(records), False

    @staticmethod
    def _next_comparison_path(fields: object) -> bytes:
        raw_path = next(fields, None)  # type: ignore[arg-type]
        if not raw_path:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid name-status record.")
        return raw_path

    def _effective_timeout(self, requested: float | None) -> float:
        configured = self._validate_timeout_value(self._limits.timeout_seconds)
        if requested is None:
            return configured
        normalized_request = self._validate_timeout_value(requested)
        return min(normalized_request, configured)

    @staticmethod
    def _validate_timeout_value(value: object) -> float:
        return normalize_timeout_seconds(
            value,
            message="The Git timeout must be finite and positive.",
        )

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not math.isfinite(remaining) or remaining <= 0:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED, "Git command exceeded the configured time limit."
            )
        return remaining

    def _parse_porcelain(self, payload: bytes) -> tuple[dict[str, str], tuple[StatusEntry, ...]]:
        headers: dict[str, str] = {}
        entries: list[StatusEntry] = []
        fields = iter(payload.split(b"\0"))
        for raw_record in fields:
            if not raw_record:
                continue
            record = raw_record.decode("utf-8", errors="replace")
            if record.startswith("# "):
                key, _, value = record[2:].partition(" ")
                headers[key] = value
            elif record.startswith("1 "):
                metadata = record.split(" ", 8)
                entries.extend(self._ordinary_entries(metadata[1], metadata[8]))
            elif record.startswith("2 "):
                metadata = record.split(" ", 9)
                previous = next(fields, b"").decode("utf-8", errors="replace")
                entries.extend(
                    self._ordinary_entries(
                        metadata[1], metadata[9], previous, is_copy=metadata[8].startswith("C")
                    )
                )
            elif record.startswith("u "):
                metadata = record.split(" ", 10)
                entries.append(StatusEntry(path=metadata[10], state="conflicted"))
            elif record.startswith("? "):
                entries.append(StatusEntry(path=record[2:], state="untracked"))
        return headers, tuple(entries)

    @staticmethod
    def _ordinary_entries(
        xy: str, path: str, previous_path: str | None = None, *, is_copy: bool = False
    ) -> list[StatusEntry]:
        entries: list[StatusEntry] = []
        index, worktree = xy
        if index != ".":
            entries.append(
                StatusEntry(
                    path=path,
                    state=("renamed" if index == "R" else "deleted" if index == "D" else "staged"),
                    previous_path=None if is_copy else previous_path,
                    copy_from=previous_path if is_copy else None,
                )
            )
        if worktree != ".":
            entries.append(
                StatusEntry(
                    path=path,
                    state="deleted" if worktree == "D" else "modified",
                    previous_path=previous_path,
                )
            )
        return entries

    def _worktrees(self) -> tuple[str, ...]:
        output = self._runner.run(["worktree", "list", "--porcelain", "-z"])
        return tuple(
            self._decode_path(record[len(b"worktree ") :])
            for record in output.stdout.split(b"\0")
            if record.startswith(b"worktree ")
        )

    @staticmethod
    def _branch(headers: dict[str, str]) -> str | None:
        branch = headers.get("branch.head")
        return None if branch in {None, "(detached)", "(unknown)"} else branch

    def _resolve_upstream(self, upstream: str | None) -> str | None:
        if upstream is None:
            return None
        resolved = self._runner.run_text(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{upstream}^{{commit}}"],
            check=False,
        )
        return resolved.stdout.strip() if resolved.returncode == 0 else None

    def _resolved_head(self) -> str | None:
        output = self._runner.run_text(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False
        )
        return output.stdout.strip() if output.returncode == 0 else None

    def _history_refs(
        self, head_oid: str | None, *, deadline: float | None
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool, tuple[str, ...], bool]:
        ref_limit = min(self._limits.max_commits, _MAX_HISTORY_REFS)
        output = self._history_run(
            [
                "for-each-ref",
                "--sort=refname",
                f"--count={ref_limit + 1}",
                "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(*objectname)%00%(*objecttype)%00",
                "refs/heads",
                "refs/tags",
                "refs/remotes",
            ],
            check=False,
            deadline=deadline,
        )
        refs: list[str] = []
        revisions: list[str] = []
        seen_revisions: set[str] = set()
        revision_argument_bytes = 0
        missing_objects = self._missing_objects(self._decode_path(output.stderr))
        access_failed = output.returncode != 0
        records = tuple(record for record in output.stdout.split(b"\n") if record)
        refs_truncated = len(records) > ref_limit
        if head_oid is not None and _FULL_OBJECT_ID.fullmatch(head_oid):
            refs.append("HEAD")
            revisions.append(head_oid)
            seen_revisions.add(head_oid)
            revision_argument_bytes = len(head_oid.encode("ascii")) + 1
        elif head_oid is not None:
            access_failed = True
        for record in records[:ref_limit]:
            fields = record.split(b"\0")
            if len(fields) != 6 or fields[-1]:
                access_failed = True
                continue
            raw_ref, object_id, object_type, peeled_id, peeled_type, _ = fields
            raw_commit_oid = (
                object_id
                if object_type == b"commit"
                else peeled_id
                if peeled_type == b"commit"
                else None
            )
            if raw_commit_oid is None:
                continue
            try:
                commit_oid = raw_commit_oid.decode("ascii")
            except UnicodeDecodeError:
                access_failed = True
                continue
            if not raw_ref or _FULL_OBJECT_ID.fullmatch(commit_oid) is None:
                access_failed = True
                continue
            ref = self._decode_path(raw_ref)
            if commit_oid in seen_revisions:
                refs.append(ref)
                continue
            argument_bytes = len(commit_oid.encode("ascii")) + 1
            if revision_argument_bytes + argument_bytes > _MAX_HISTORY_REVISION_ARGUMENT_BYTES:
                refs_truncated = True
                continue
            refs.append(ref)
            revisions.append(commit_oid)
            seen_revisions.add(commit_oid)
            revision_argument_bytes += argument_bytes
        return (
            tuple(refs),
            tuple(revisions),
            access_failed,
            missing_objects,
            refs_truncated,
        )

    def _comparison_has_reachable_shallow_boundary(
        self, roots: tuple[str, str], *, deadline: float
    ) -> bool:
        shallow = self._history_run_text(
            ["rev-parse", "--is-shallow-repository"], deadline=deadline
        )
        if shallow.stdout.strip() != "true":
            return False
        visible_roots = self._runner.run_text(
            ["rev-list", "--max-parents=0", "--end-of-options", *roots],
            timeout_seconds=self._remaining_timeout(deadline),
        )
        root_oids = tuple(line for line in visible_roots.stdout.splitlines() if line)
        if not root_oids or any(not _FULL_OBJECT_ID.fullmatch(root) for root in root_oids):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned invalid visible history roots.")
        if len(root_oids) > self._limits.max_commits:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED, "Git visible history roots exceeded the commit limit."
            )
        raw_commits = self._runner.run(
            ["cat-file", "--batch"],
            input_bytes=("\n".join(root_oids) + "\n").encode("ascii"),
            timeout_seconds=self._remaining_timeout(deadline),
        ).stdout
        return any(
            self._raw_commit_has_parent(raw_commit)
            for raw_commit in self._parse_batched_commit_objects(raw_commits, root_oids)
        )

    @staticmethod
    def _parse_batched_commit_objects(
        payload: bytes, requested_oids: tuple[str, ...]
    ) -> tuple[bytes, ...]:
        """Parse Git cat-file --batch commit records without accepting partial framing."""
        records: list[bytes] = []
        cursor = 0
        for requested_oid in requested_oids:
            line_end = payload.find(b"\n", cursor)
            if line_end < 0:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an incomplete commit record."
                )
            header = payload[cursor:line_end].split()
            cursor = line_end + 1
            try:
                returned_oid = header[0].decode("ascii") if header else ""
            except UnicodeDecodeError:
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an invalid commit record."
                ) from None
            if (
                len(header) != 3
                or returned_oid.lower() != requested_oid.lower()
                or header[1] != b"commit"
                or not header[2].isdigit()
            ):
                raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid commit record.")
            size = int(header[2])
            end = cursor + size
            if end >= len(payload) or payload[end : end + 1] != b"\n":
                raise ManduaError(
                    ErrorCode.GIT_FAILURE, "Git returned an incomplete commit record."
                )
            records.append(payload[cursor:end])
            cursor = end + 1
        if cursor != len(payload):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned unexpected commit records.")
        return tuple(records)

    @staticmethod
    def _raw_commit_has_parent(payload: bytes) -> bool:
        """Return whether a raw commit header identifies a non-root commit."""
        headers, separator, _body = payload.partition(b"\n\n")
        if not separator:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned a commit without headers.")
        header_lines = headers.split(b"\n")
        if not header_lines or not header_lines[0].startswith(b"tree "):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid commit tree.")
        try:
            tree = header_lines[0][len(b"tree ") :].decode("ascii")
        except UnicodeDecodeError:
            raise ManduaError(
                ErrorCode.GIT_FAILURE, "Git returned an invalid commit tree."
            ) from None
        if not _FULL_OBJECT_ID.fullmatch(tree):
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid commit tree.")
        has_parent = False
        for header in header_lines[1:]:
            if header.startswith(b"parent "):
                parent = header[len(b"parent ") :]
                try:
                    parent_text = parent.decode("ascii")
                except UnicodeDecodeError:
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git returned an invalid commit parent."
                    ) from None
                if not _FULL_OBJECT_ID.fullmatch(parent_text):
                    raise ManduaError(
                        ErrorCode.GIT_FAILURE, "Git returned an invalid commit parent."
                    )
                has_parent = True
        return has_parent

    def _is_shallow(self, *, deadline: float | None) -> bool:
        output = self._history_run_text(
            ["rev-parse", "--is-shallow-repository"],
            deadline=deadline,
        )
        return output.stdout.strip() == "true"

    def _notes_available(self, *, deadline: float | None) -> bool:
        return self._notes_ref_state(deadline=deadline).available

    def _notes_ref_state(self, *, deadline: float | None) -> _NotesRefState:
        width = self._object_id_width(deadline=deadline)
        raw_ref = self._history_run_text(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", self._notes_ref],
            check=False,
            deadline=deadline,
            max_output_bytes=min(self._limits.max_output_bytes, 128),
        )
        if raw_ref.returncode == 1 and not raw_ref.stdout and not raw_ref.stderr:
            return _NotesRefState(oid=None, available=False)
        raw_oid = self._strict_output_oid(raw_ref, width)
        if raw_oid is None:
            return _NotesRefState(oid=None, available=False, lookup_failed=True)
        peeled_ref = self._history_run_text(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{self._notes_ref}^{{commit}}",
            ],
            check=False,
            deadline=deadline,
            max_output_bytes=min(self._limits.max_output_bytes, 128),
        )
        if self._strict_output_oid(peeled_ref, width) != raw_oid:
            return _NotesRefState(oid=raw_oid, available=False, lookup_failed=True)
        return _NotesRefState(oid=raw_oid, available=True)

    def _object_id_width(self, *, deadline: float | None) -> int:
        if self._object_width is not None:
            return self._object_width
        output = self._history_run_text(
            ["rev-parse", "--show-object-format"],
            check=False,
            deadline=deadline,
            max_output_bytes=min(self._limits.max_output_bytes, 32),
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
        width = {"sha1\n": 40, "sha256\n": 64}.get(output.stdout)
        if width is None:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
        self._object_width = width
        return width

    @staticmethod
    def _strict_output_oid(output: GitOutput[str], width: int) -> str | None:
        if output.returncode != 0 or output.stderr:
            return None
        lines = output.stdout.splitlines()
        if len(lines) != 1 or output.stdout != f"{lines[0]}\n":
            return None
        oid = lines[0]
        if _FULL_OBJECT_ID.fullmatch(oid) is None or len(oid) != width:
            return None
        return oid

    def _effective_review_output_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._limits.max_output_bytes
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or requested < 1
            or requested > self._limits.max_output_bytes
        ):
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED,
                "The review-note output limit must be a positive bounded integer.",
            )
        return requested

    def _validated_notes_ref(self, value: object) -> str:
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ManduaError(
                    ErrorCode.VALIDATION_FAILED, "The review notes ref is invalid."
                ) from None
        if (
            not isinstance(value, str)
            or not value.startswith("refs/notes/")
            or len(value) > self._limits.max_input_chars
            or "\x00" in value
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
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The review notes ref is invalid.")
        parts = value.split("/")
        if any(
            not part or part in {".", "..", "@"} or part.startswith(".") or part.endswith(".lock")
            for part in parts
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The review notes ref is invalid.")
        return value

    def _history_run_text(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        deadline: float | None,
        max_output_bytes: int | None = None,
    ) -> GitOutput[str]:
        if deadline is None:
            if max_output_bytes is None:
                return self._runner.run_text(arguments, check=check)
            return self._runner.run_text(
                arguments,
                check=check,
                max_output_bytes=max_output_bytes,
            )
        if max_output_bytes is None:
            return self._runner.run_text(
                arguments,
                check=check,
                timeout_seconds=self._remaining_timeout(deadline),
            )
        return self._runner.run_text(
            arguments,
            check=check,
            timeout_seconds=self._remaining_timeout(deadline),
            max_output_bytes=max_output_bytes,
        )

    def _history_run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        deadline: float | None,
    ) -> GitOutput[bytes]:
        if deadline is None:
            return self._runner.run(arguments, check=check)
        return self._runner.run(
            arguments,
            check=check,
            timeout_seconds=self._remaining_timeout(deadline),
        )

    @staticmethod
    def _decode_path(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")

    @staticmethod
    def _missing_objects(stderr: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(match.group(0).lower() for match in _OBJECT_ID.finditer(stderr)))


def _split_log_revisions(revisions: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Allow only the internal all-refs selector before Git's option separator."""
    return (
        tuple(revision for revision in revisions if revision == "--all"),
        tuple(revision for revision in revisions if revision != "--all"),
    )
