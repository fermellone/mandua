"""Real-Git acceptance tests for append-only corrections."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.models import CommitMetadata, CorrectionRequest
from mandua.writes.checkpoint import CheckpointWriter


def _metadata(
    *,
    memory_type: str = "decision",
    reason: str | None = "The calibrated sensor disproved the earlier threshold.",
    extra_trailers: tuple[tuple[str, str], ...] = (),
) -> CommitMetadata:
    return CommitMetadata(
        memory_type=memory_type,
        scope="irrigation",
        task_id="TASK-IRR-9",
        decision_id="DEC-IRR-2",
        agent_id="gardener",
        reason=reason,
        extra_trailers=extra_trailers,
    )


def _request(
    incorrect_revision: str,
    *,
    metadata: CommitMetadata | None = None,
    paths: tuple[PurePosixPath, ...] = (PurePosixPath("knowledge/rules.md"),),
    staged: bool = False,
    subject: str = "Correct the moisture threshold",
) -> CorrectionRequest:
    return CorrectionRequest(
        incorrect_revision=incorrect_revision,
        subject=subject,
        metadata=metadata or _metadata(),
        paths=paths,
        staged=staged,
    )


def _record_incorrect_threshold(repo) -> str:
    repo.write("knowledge/rules.md", "Moisture threshold: 80%\n")
    return repo.commit(
        "Record the initial threshold\n\n"
        "Memory-Type: decision\n"
        "Scope: irrigation\n"
        "Decision-ID: DEC-IRR-2\n"
        "Agent-ID: gardener"
    )


def test_correction_preview_is_read_only_and_matches_the_applied_tree(repo) -> None:
    """This fails if preview moves state or validates a different tree than apply."""
    wrong = _record_incorrect_threshold(repo)
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    request = _request(wrong)
    head_before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    status_before = repo.git("status", "--porcelain=v1").stdout
    index_before = index_path.read_bytes()

    preview = repo.service().correct(request)

    assert preview.operation == "correct"
    assert preview.applied is False
    assert preview.changes[0].before_oid == wrong
    assert preview.changes[0].after_oid is None
    assert repo.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert index_path.read_bytes() == index_before
    assert repo.git("status", "--porcelain=v1").stdout == status_before

    applied = repo.service().correct(request, apply=True)

    assert applied.operation == "correct"
    assert applied.applied is True
    assert applied.evidence[0].oid == preview.evidence[0].oid
    assert applied.evidence[0].details["parent_oid"] == preview.evidence[0].details["parent_oid"]
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Moisture threshold: 35%\n"


def test_correction_expands_revision_and_forces_canonical_metadata(repo) -> None:
    """This fails if callers can keep another type or record an abbreviated correction link."""
    wrong = _record_incorrect_threshold(repo)
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")

    repo.service().correct(_request(wrong[:12]), apply=True)

    message = repo.git("show", "-s", "--format=%B", "HEAD").stdout
    assert message == (
        "Correct the moisture threshold\n\n"
        "Reason: The calibrated sensor disproved the earlier threshold.\n\n"
        "Memory-Type: correction\n"
        "Scope: irrigation\n"
        "Task-ID: TASK-IRR-9\n"
        "Decision-ID: DEC-IRR-2\n"
        "Agent-ID: gardener\n"
        f"Corrects: {wrong}\n"
    )
    assert message.count("Memory-Type:") == 1
    assert message.count("Corrects:") == 1


def test_correction_rejects_a_commit_outside_canonical_history(repo) -> None:
    """This fails if a reachable side-branch commit can be labeled as a main correction target."""
    main_before = repo.git("rev-parse", "main").stdout.strip()
    repo.checkout_new("side/error", start=main_before)
    repo.write("knowledge/rules.md", "Side threshold: 80%\n")
    unrelated = repo.commit("Record an unrelated side-branch threshold")
    repo.checkout("main")
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")

    with pytest.raises(ManduaError) as caught:
        repo.service().correct(_request(unrelated), apply=True)

    assert caught.value.code is ErrorCode.INVALID_REVISION
    assert repo.git("rev-parse", "main").stdout.strip() == main_before


def test_correction_requires_the_checked_out_canonical_branch(repo) -> None:
    """This fails if append-only corrections can advance a non-canonical branch."""
    wrong = _record_incorrect_threshold(repo)
    repo.checkout_new("task/correction", start=wrong)
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")

    with pytest.raises(ManduaError) as caught:
        repo.service().correct(_request(wrong), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "task/correction").stdout.strip() == wrong
    assert repo.git("rev-parse", "main").stdout.strip() == wrong


def test_correction_preserves_queryable_error_and_attributes_the_new_truth(repo) -> None:
    """This fails if correction rewrites history or provenance invents an unrecorded reason."""
    wrong = _record_incorrect_threshold(repo)
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Initial review.", wrong)
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    request = _request(wrong, metadata=_metadata(reason=None))

    repo.service().correct(request, apply=True)
    correction = repo.git("rev-parse", "HEAD").stdout.strip()

    assert repo.git("merge-base", "--is-ancestor", wrong, correction).returncode == 0
    assert repo.git("cat-file", "-t", wrong).stdout == "commit\n"
    decision = repo.service().decision("DEC-IRR-2", limit=2)
    why = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)
    primary_evidence = [
        item
        for item in decision.evidence
        if item.kind in {"decision-commit", "decision-correction"}
    ]
    assert [
        (
            item.oid,
            item.kind,
            item.details.get("corrects"),
            item.details.get("verified_corrects"),
        )
        for item in primary_evidence
    ] == [
        (wrong, "decision-commit", None, None),
        (correction, "decision-correction", (wrong,), (wrong,)),
    ]
    assert why.evidence[0].oid == correction
    assert all("Recorded reason:" not in claim.text for claim in why.observed)
    assert why.inferred == ()


def test_correction_staged_mode_commits_only_the_index(repo) -> None:
    """This fails if correction staged mode reads unstaged content or rewrites the index."""
    wrong = _record_incorrect_threshold(repo)
    repo.write("knowledge/rules.md", "Staged threshold: 35%\n")
    repo.git("add", "knowledge/rules.md")
    repo.write("knowledge/rules.md", "Unstaged threshold: 40%\n")
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()

    result = repo.service().correct(
        _request(wrong, paths=(), staged=True),
        apply=True,
    )

    assert result.applied is True
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Staged threshold: 35%\n"
    assert repo.git("diff", "--cached").stdout == ""
    assert repo.git("diff", "--", "knowledge/rules.md").stdout
    assert index_path.read_bytes() == index_before


@pytest.mark.parametrize(
    ("subject", "metadata"),
    (
        ("Correct the threshold\n\nCorrects: forged", _metadata()),
        (
            "Correct the moisture threshold",
            _metadata(reason="A reason.\n\nCorrects: forged"),
        ),
        (
            "Correct the moisture threshold",
            _metadata(extra_trailers=(("Corrects", "forged"),)),
        ),
        (
            "Correct the moisture threshold",
            CommitMetadata(
                memory_type="decision",
                scope="irrigation\nCorrects: forged",
                task_id="TASK-IRR-9",
                decision_id="DEC-IRR-2",
                agent_id="gardener",
            ),
        ),
    ),
)
def test_correction_rejects_reserved_trailer_injection(
    repo, subject: str, metadata: CommitMetadata
) -> None:
    """This fails if untrusted metadata can add or duplicate the generated Corrects trailer."""
    wrong = _record_incorrect_threshold(repo)
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")

    with pytest.raises(ManduaError) as caught:
        repo.service().correct(_request(wrong, subject=subject, metadata=metadata), apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "HEAD").stdout.strip() == wrong


def test_correction_inherits_checkpoint_selection_race_protection(repo, monkeypatch) -> None:
    """This fails if correction bypasses checkpoint's final selected-content recheck."""
    wrong = _record_incorrect_threshold(repo)
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    original = CheckpointWriter._commit_tree

    def change_after_commit_tree(self, prepared):
        commit = original(self, prepared)
        repo.write("knowledge/rules.md", "Moisture threshold: 40%\n")
        return commit

    monkeypatch.setattr(CheckpointWriter, "_commit_tree", change_after_commit_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().correct(_request(wrong), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == wrong
