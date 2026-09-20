from mandua.models import (
    Claim,
    Confidence,
    Evidence,
    HistoryScope,
    MemoryResult,
    PlannedChange,
)
from mandua.renderers import render_human


def test_human_renderer_exposes_each_result_section() -> None:
    result = MemoryResult(
        operation="status",
        answer="The repository is clean.",
        observed=(Claim("The worktree has no changes.", ("status-1",)),),
        inferred=(Claim("No checkpoint is needed.", ()),),
        evidence=(Evidence("status-1", "status"),),
        gaps=("No remote state was inspected.",),
        warnings=("History is shallow.",),
    )

    rendered = render_human(result)

    assert "Answer:" in rendered
    assert "The repository is clean." in rendered
    assert "Observed:" in rendered
    assert "The worktree has no changes." in rendered
    assert "Inferred:" in rendered
    assert "No checkpoint is needed." in rendered
    assert "Evidence:" in rendered
    assert "status-1" in rendered
    assert "Gaps:" in rendered
    assert "No remote state was inspected." in rendered
    assert "Warnings:" in rendered
    assert "History is shallow." in rendered


def test_human_renderer_discloses_truncated_history_scope() -> None:
    result = MemoryResult(
        operation="timeline",
        answer="Only the newest two commits were inspected.",
        history_scope=HistoryScope(
            start_oid="a" * 40,
            end_oid="b" * 40,
            refs=("refs/heads/main", "refs/tags/archive"),
            commit_count=2,
            truncated=True,
            shallow=True,
            notes_available=False,
            missing_objects=("c" * 40,),
        ),
        confidence=Confidence.LOW,
        gaps=("Earlier commits may exist.",),
        warnings=("History was limited.",),
    )

    rendered = render_human(result)

    assert (
        rendered
        == """Operation:
timeline

Answer:
Only the newest two commits were inspected.

Observed:
- None

Inferred:
- None

Evidence:
- None

History Scope:
Start OID: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
End OID: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
Refs:
- refs/heads/main
- refs/tags/archive
Commit Count: 2
Truncated: Yes
Shallow: Yes
Notes Available: No
Missing Objects:
- cccccccccccccccccccccccccccccccccccccccc

Confidence:
low

Gaps:
- Earlier commits may exist.

Warnings:
- History was limited.

Changes:
- None

Applied:
No"""
    )


def test_human_renderer_discloses_unapplied_preview_and_default_scope() -> None:
    result = MemoryResult(
        operation="checkpoint",
        answer="The semantic checkpoint was validated and previewed.",
        changes=(
            PlannedChange(
                action="update-ref",
                target="refs/heads/main",
                before_oid="d" * 40,
            ),
        ),
        applied=False,
    )

    rendered = render_human(result)

    assert (
        rendered
        == """Operation:
checkpoint

Answer:
The semantic checkpoint was validated and previewed.

Observed:
- None

Inferred:
- None

Evidence:
- None

History Scope:
Start OID: None
End OID: None
Refs:
- None
Commit Count: 0
Truncated: No
Shallow: No
Notes Available: No
Missing Objects:
- None

Confidence:
high

Gaps:
- None

Warnings:
- None

Changes:
- Action: update-ref
  Target: refs/heads/main
  Before OID: dddddddddddddddddddddddddddddddddddddddd
  After OID: None

Applied:
No"""
    )
