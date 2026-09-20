"""Public facade for Mandu'a memory operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mandua.bounds import validate_query_limits
from mandua.git_runner import GitOperationBudget, _GitBudgetLimits
from mandua.models import (
    AnnotationRequest,
    CheckpointRequest,
    CorrectionRequest,
    IntegrationRequest,
    MemoryResult,
    QueryLimits,
    RepositoryPolicy,
)
from mandua.policy import Policy
from mandua.queries.comparison import compare_result
from mandua.queries.provenance import decision_result, evolution_result, origin_result, why_result
from mandua.queries.recovery import recover_result
from mandua.queries.state import context_result, status_result, timeline_result
from mandua.repository import RepositoryInspector
from mandua.writes.checkpoint import checkpoint_result
from mandua.writes.correction import correction_result
from mandua.writes.integration import _integration_outcome
from mandua.writes.notes import annotate_result

_READ_MAX_GIT_PROCESSES = 512
_READ_MAX_GIT_INPUT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class _ServiceRepositoryState:
    inspector: RepositoryInspector
    policy: RepositoryPolicy


class MemoryService:
    """Expose bounded memory operations for one repository."""

    def __init__(
        self,
        repository: Path,
        inspector: RepositoryInspector,
        limits: QueryLimits,
        repository_policy: RepositoryPolicy,
    ) -> None:
        self._repository = repository
        self._limits = limits
        self._repository_state = _ServiceRepositoryState(inspector, repository_policy)

    @property
    def _inspector(self) -> RepositoryInspector:
        return self._repository_state.inspector

    @property
    def _repository_policy(self) -> RepositoryPolicy:
        return self._repository_state.policy

    def _install_repository_policy(self, policy: RepositoryPolicy) -> None:
        """Atomically replace retained policy-dependent state without reloading the worktree."""
        inspector = RepositoryInspector(
            self._repository,
            limits=self._limits,
            notes_ref=policy.notes_ref,
        )
        self._repository_state = _ServiceRepositoryState(inspector, policy)

    @property
    def canonical_branch(self) -> str:
        """Return the canonical branch from this service's one loaded policy."""
        return self._repository_policy.canonical_branch

    @property
    def repository_policy(self) -> RepositoryPolicy:
        """Return the immutable repository policy retained by this service."""
        return self._repository_policy

    def _read_budget(self) -> tuple[GitOperationBudget, _GitBudgetLimits]:
        """Create one fresh aggregate allowance for a public read operation."""
        private_limits = _GitBudgetLimits(
            max_processes=_READ_MAX_GIT_PROCESSES,
            max_output_bytes=self._limits.max_output_bytes,
            max_input_bytes=_READ_MAX_GIT_INPUT_BYTES,
        )
        budget = GitOperationBudget(self._limits, budget_limits=private_limits)
        return budget, private_limits

    def _read_inspector(self) -> RepositoryInspector:
        """Create one fresh inspector and aggregate budget for a public read operation."""
        budget, private_limits = self._read_budget()
        return RepositoryInspector(
            self._repository,
            limits=self._limits,
            notes_ref=self._repository_policy.notes_ref,
            operation_budget=budget,
            _budget_limits=private_limits,
        )

    def _recovery_inspector(self) -> RepositoryInspector:
        """Budget the retained inspector used across recovery mutation reconciliation."""
        budget, private_limits = self._read_budget()
        self._inspector._replace_operation_budget(budget, private_limits)
        return self._inspector

    @classmethod
    def open(cls, repo: Path, *, limits: QueryLimits | None = None) -> MemoryService:
        """Open a validated repository without changing its state."""
        configured_limits = validate_query_limits(QueryLimits() if limits is None else limits)
        repository = Path(repo).resolve()
        policy = Policy.open(repository, limits=configured_limits)
        inspector = RepositoryInspector(
            repository,
            limits=configured_limits,
            notes_ref=policy.repository_policy.notes_ref,
        )
        inspector.validate()
        return cls(repository, inspector, configured_limits, policy.repository_policy)

    def status(self) -> MemoryResult:
        """Report index and worktree status alongside bounded history scope."""
        return status_result(self._read_inspector(), self._limits)

    def context(
        self,
        *,
        task_id: str | None = None,
        branch: str | None = None,
        limit: int | None = 100,
    ) -> MemoryResult:
        """Reconstruct bounded context for an exact task, branch, or current state."""
        return context_result(
            self._read_inspector(),
            self._limits,
            task_id=task_id,
            branch=branch,
            limit=limit,
            canonical_branch=self._repository_policy.canonical_branch,
        )

    def timeline(
        self,
        *,
        path: PurePosixPath | None = None,
        start: str | None = None,
        end: str | None = "HEAD",
        limit: int | None = 100,
    ) -> MemoryResult:
        """Return bounded chronological history for an optional path and commit range."""
        return timeline_result(
            self._read_inspector(),
            self._limits,
            path=path,
            start=start,
            end=end,
            limit=limit,
        )

    def why(
        self,
        *,
        path: PurePosixPath,
        line: int,
        revision: str = "HEAD",
    ) -> MemoryResult:
        """Explain a line through its recorded Git provenance only."""
        return why_result(
            self._read_inspector(),
            self._limits,
            path=path,
            line=line,
            revision=revision,
        )

    def decision(self, decision_id: str, *, limit: int | None = 500) -> MemoryResult:
        """Find commits with one exact Decision-ID and later correction links."""
        return decision_result(self._read_inspector(), self._limits, decision_id, limit=limit)

    def origin(
        self,
        text: str,
        *,
        path: PurePosixPath | None = None,
        limit: int | None = 100,
    ) -> MemoryResult:
        """Find exact added and removed content across bounded local history."""
        return origin_result(self._read_inspector(), self._limits, text, path=path, limit=limit)

    def evolution(self, path: PurePosixPath, *, limit: int | None = 100) -> MemoryResult:
        """Follow one path through bounded rename and deletion history."""
        return evolution_result(self._read_inspector(), self._limits, path, limit=limit)

    def compare(
        self,
        left: str,
        right: str,
        *,
        path: PurePosixPath | None = None,
        limit: int | None = 100,
    ) -> MemoryResult:
        """Compare two hypotheses through bounded exclusive history and state evidence."""
        return compare_result(
            self._read_inspector(),
            self._limits,
            left,
            right,
            path=path,
            limit=limit,
        )

    def recover(
        self,
        *,
        query: str | None = None,
        create_branch: str | None = None,
        apply: bool = False,
    ) -> MemoryResult:
        """Preview or atomically create a new local branch for one recovered commit."""
        return recover_result(
            self._recovery_inspector(),
            self._limits,
            query=query,
            create_branch=create_branch,
            apply=apply,
        )

    def checkpoint(self, request: CheckpointRequest, *, apply: bool = False) -> MemoryResult:
        """Preview or atomically apply one explicit semantic checkpoint."""
        return checkpoint_result(
            self._repository,
            self._limits,
            request,
            apply=apply,
        )

    def correct(self, request: CorrectionRequest, *, apply: bool = False) -> MemoryResult:
        """Preview or append one correction on the configured canonical branch."""
        return correction_result(
            self._repository,
            self._limits,
            request,
            apply=apply,
        )

    def annotate(self, request: AnnotationRequest, *, apply: bool = False) -> MemoryResult:
        """Preview or append one review annotation to the configured Git notes ref."""
        return annotate_result(
            self._repository,
            self._limits,
            request,
            apply=apply,
        )

    def integrate(self, request: IntegrationRequest, *, apply: bool = False) -> MemoryResult:
        """Preview or apply one validated non-fast-forward branch integration."""
        outcome = _integration_outcome(
            self._repository,
            self._limits,
            request,
            apply=apply,
            repository_policy=self._repository_policy,
        )
        if outcome.applied_policy is not None:
            self._install_repository_policy(outcome.applied_policy)
        return outcome.result
