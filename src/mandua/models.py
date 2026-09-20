"""Stable public model types for Mandu'a operations."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    kind: str
    oid: str | None = None
    ref: str | None = None
    path: str | None = None
    line: int | None = None
    excerpt: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistoryScope:
    start_oid: str | None = None
    end_oid: str | None = None
    refs: tuple[str, ...] = ()
    commit_count: int = 0
    truncated: bool = False
    shallow: bool = False
    notes_available: bool = False
    missing_objects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedChange:
    action: str
    target: str
    before_oid: str | None = None
    after_oid: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryResult:
    operation: str
    answer: str
    observed: tuple[Claim, ...] = ()
    inferred: tuple[Claim, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    history_scope: HistoryScope = field(default_factory=HistoryScope)
    confidence: Confidence = Confidence.HIGH
    gaps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    changes: tuple[PlannedChange, ...] = ()
    applied: bool = False
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned machine representation of this result."""
        payload = asdict(self)
        payload["confidence"] = self.confidence.value
        return payload


@dataclass(frozen=True, slots=True)
class QueryLimits:
    max_commits: int = 500
    max_output_bytes: int = 1_048_576
    max_excerpt_chars: int = 400
    max_input_chars: int = 4_096
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class CommitMetadata:
    """Semantic metadata recorded in a generated Git commit message."""

    memory_type: str
    scope: str
    agent_id: str
    task_id: str | None = None
    decision_id: str | None = None
    reason: str | None = None
    extra_trailers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    """Content selection and metadata for one explicit memory checkpoint."""

    subject: str
    metadata: CommitMetadata
    paths: tuple[PurePosixPath, ...] = ()
    staged: bool = False


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """One preserved error and the content that records its correction."""

    incorrect_revision: str
    subject: str
    metadata: CommitMetadata
    paths: tuple[PurePosixPath, ...] = ()
    staged: bool = False


@dataclass(frozen=True, slots=True)
class AnnotationRequest:
    """One bounded review annotation addressed to an exact resolved commit."""

    revision: str
    message: str
    agent_id: str


@dataclass(frozen=True, slots=True)
class IntegrationRequest:
    """Two exact local branches and metadata for one validated integration."""

    source: str
    target: str
    subject: str
    metadata: CommitMetadata


@dataclass(frozen=True, slots=True)
class UniqueJsonFieldInvariant:
    """Require unique non-empty scalar values for one JSON object field."""

    path: PurePosixPath
    field: str
    kind: str = "unique-json-field"


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    """Immutable repository policy loaded from an optional TOML file."""

    canonical_branch: str = "main"
    notes_ref: str = "refs/notes/review"
    knowledge_roots: tuple[PurePosixPath, ...] = (PurePosixPath("knowledge"),)
    max_file_bytes: int = 1_048_576
    invariants: tuple[UniqueJsonFieldInvariant, ...] = ()
