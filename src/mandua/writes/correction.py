"""Record a correction as a constrained append-only checkpoint."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mandua.errors import ErrorCode, ManduaError
from mandua.metadata import parse_trailers
from mandua.models import (
    CheckpointRequest,
    CommitMetadata,
    CorrectionRequest,
    MemoryResult,
    QueryLimits,
)
from mandua.writes.checkpoint import CheckpointWriter, _PreparedCheckpoint


class CorrectionWriter(CheckpointWriter):
    """Apply checkpoint guarantees while preserving and linking one prior error."""

    def __init__(self, repository: Path, limits: QueryLimits) -> None:
        super().__init__(repository, limits)
        self._incorrect_revision = ""

    def correct(self, request: CorrectionRequest, *, apply: bool = False) -> MemoryResult:
        """Preview or append one correction on the configured canonical branch."""
        if not isinstance(request, CorrectionRequest) or not isinstance(
            request.metadata, CommitMetadata
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The correction request is invalid.")
        if not isinstance(request.incorrect_revision, str):
            raise ManduaError(ErrorCode.INVALID_REVISION, "The revision is invalid.")
        self._incorrect_revision = request.incorrect_revision
        checkpoint_request = CheckpointRequest(
            subject=request.subject,
            metadata=replace(request.metadata, memory_type="correction"),
            paths=request.paths,
            staged=request.staged,
        )
        return super().checkpoint(checkpoint_request, apply=apply)

    def _prepare(self, request: CheckpointRequest) -> _PreparedCheckpoint:
        prepared = super()._prepare(request)
        if prepared.branch != prepared.policy.canonical_branch:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "A correction requires the configured canonical branch to be checked out.",
            )
        incorrect_oid = self._validate_oid(
            self._runner.resolve_commit(
                self._incorrect_revision,
                timeout_seconds=self._remaining_timeout(),
            )
        )
        ancestry = self._runner.run_text(
            [
                "merge-base",
                "--is-ancestor",
                "--end-of-options",
                incorrect_oid,
                prepared.parent_oid,
            ],
            check=False,
            timeout_seconds=self._remaining_timeout(),
        )
        if ancestry.returncode == 1 and not ancestry.stdout and not ancestry.stderr:
            raise ManduaError(
                ErrorCode.INVALID_REVISION,
                "The corrected revision is not an ancestor of the canonical branch.",
            )
        if ancestry.returncode != 0 or ancestry.stdout or ancestry.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify correction ancestry.")
        message = self._policy_for_remaining(prepared.policy).validate_message(
            request.subject,
            reason=request.metadata.reason,
            memory_type="correction",
            scope=request.metadata.scope,
            task_id=request.metadata.task_id,
            decision_id=request.metadata.decision_id,
            agent_id=request.metadata.agent_id,
            corrects=incorrect_oid,
            extra_trailers=request.metadata.extra_trailers,
        )
        return replace(prepared, message=message)

    def _result(
        self,
        prepared: _PreparedCheckpoint,
        *,
        commit_oid: str | None,
        applied: bool,
        warnings: tuple[str, ...] = (),
    ) -> MemoryResult:
        result = super()._result(
            prepared, commit_oid=commit_oid, applied=applied, warnings=warnings
        )
        corrects_oid = next(
            value for key, value in parse_trailers(prepared.message) if key == "Corrects"
        )
        evidence = result.evidence[0]
        details = {**evidence.details, "corrects_oid": corrects_oid}
        return replace(
            result,
            operation="correct",
            answer=(
                "The append-only correction was applied."
                if applied
                else "The append-only correction was validated and previewed."
            ),
            evidence=(replace(evidence, kind="correction-preview", details=details),),
        )


def correction_result(
    repository: Path,
    limits: QueryLimits,
    request: CorrectionRequest,
    *,
    apply: bool = False,
) -> MemoryResult:
    """Build a correction result through one operation-scoped writer."""
    return CorrectionWriter(repository, limits).correct(request, apply=apply)
