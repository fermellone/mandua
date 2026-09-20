"""Stable error types for Mandu'a operations."""

from dataclasses import asdict
from enum import StrEnum
from typing import Any

from mandua.models import Evidence


class ErrorCode(StrEnum):
    INVALID_REPOSITORY = "invalid_repository"
    INVALID_REVISION = "invalid_revision"
    INVALID_PATH = "invalid_path"
    INCOMPLETE_HISTORY = "incomplete_history"
    MISSING_NOTES = "missing_notes"
    MISSING_OBJECT = "missing_object"
    CONFLICT = "conflict"
    POLICY_VIOLATION = "policy_violation"
    VALIDATION_FAILED = "validation_failed"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    LIMIT_EXCEEDED = "limit_exceeded"
    GIT_FAILURE = "git_failure"


class ManduaError(Exception):
    """An expected error with a stable, safe machine representation."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        evidence: tuple[Evidence, ...] = (),
        recovery: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence
        self.recovery = recovery

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned machine representation of this error."""
        return {
            "schema_version": "1.0",
            "code": self.code.value,
            "message": self.message,
            "evidence": [asdict(item) for item in self.evidence],
            "recovery": self.recovery,
        }
