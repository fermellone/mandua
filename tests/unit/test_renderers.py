import json

from mandua.models import (
    Claim,
    Confidence,
    Evidence,
    HistoryScope,
    MemoryResult,
    PlannedChange,
)
from mandua.renderers import render_agent, render_human


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


def test_agent_view_preserves_limits_citations_and_inferences_without_mutation():
    result = MemoryResult(
        operation="why",
        answer="Recorded provenance of the selected line.",
        observed=(Claim("Recorded reason: reduce waiting.", ("reason-1",)),),
        inferred=(Claim("Possible tradeoff.", ("reason-1",)),),
        evidence=(
            Evidence(
                "reason-1",
                "commit-metadata",
                oid="a" * 40,
                path="rules.txt",
                line=2,
                excerpt="Reason: reduce waiting.",
            ),
        ),
        history_scope=HistoryScope(truncated=True, shallow=True, missing_objects=("b" * 40,)),
        gaps=("A review could not be read.",),
        warnings=("Excerpt was clipped.",),
    )
    before = result.to_dict()
    output = json.loads(render_agent(result))
    assert output["query_observations"][0]["citations"] == ["reason-1"]
    assert output["inferences"][0]["text"] == "Possible tradeoff."
    assert output["evidence"][0]["path"] == "rules.txt"
    assert output["evidence"][0]["line"] == 2
    limits = " ".join(output["scope"]["limitations"])
    for word in ("truncated", "shallow", "unavailable", "review", "clipped"):
        assert word in limits
    assert result.to_dict() == before


def test_agent_view_preserves_write_preview_and_application_state():
    for applied in (False, True):
        result = MemoryResult(
            operation="checkpoint",
            answer="Checkpoint result.",
            changes=(PlannedChange("update-ref", "refs/heads/main"),),
            applied=applied,
        )
        output = json.loads(render_agent(result))
        assert output["write_state"]["applied"] is applied
        assert output["write_state"]["changes"][0]["target"] == "refs/heads/main"


def test_agent_view_retains_chronology_and_revision_selection_as_evidence():
    result = MemoryResult(
        operation="timeline",
        answer="Selected history.",
        evidence=(
            Evidence(
                "commit-1",
                "commit",
                oid="a" * 40,
                details={"parents": ("b" * 40,), "author_time": 1577836800, "changed_paths": ()},
            ),
        ),
        history_scope=HistoryScope(start_oid="b" * 40, end_oid="a" * 40, refs=("topic",)),
    )
    output = json.loads(render_agent(result))
    assert output["evidence"][0]["attributes"]["parents"] == ["b" * 40]
    assert output["evidence"][0]["attributes"]["author_time"] == 1577836800
    assert output["scope"]["selection"]["refs"] == ["topic"]
    assert output["scope"]["selection"]["end_oid"] == "a" * 40


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
