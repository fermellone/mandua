"""Acceptance tests for deterministic context and timeline queries."""

from __future__ import annotations

import inspect
from pathlib import PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.memory_service import MemoryService
from mandua.models import QueryLimits
from mandua.repository import RepositoryInspector


def _message(subject: str, *trailers: str) -> str:
    """Build a fixture message with one final trailer paragraph."""
    return f"{subject}\n\n" + "\n".join(trailers)


def test_context_finds_exact_task_commits_and_branch_delta(repo) -> None:
    """This fails if context treats a Task-ID as a partial text search or includes main."""
    repo.write("knowledge/baseline.md", "Baseline observation\n")
    repo.commit("Record the baseline")
    repo.checkout_new("task/moisture-sensor")
    repo.write("knowledge/observations.md", "North bed moisture: 31%\n")
    task_commit = repo.commit(
        _message(
            "Record north-bed moisture",
            "Memory-Type: observation",
            "Scope: garden",
            "Task-ID: TASK-IRR-7",
            "Agent-ID: gardener",
        )
    )
    repo.write("knowledge/other.md", "Different task\n")
    repo.commit(
        _message(
            "Record a different task",
            "Task-ID: TASK-IRR-70",
            "Agent-ID: gardener",
        )
    )

    task_result = repo.service().context(task_id="TASK-IRR-7")
    branch_result = repo.service().context(branch="task/moisture-sensor")

    assert any(
        claim.text == "Found 1 commit for task TASK-IRR-7." for claim in task_result.observed
    )
    assert [item.oid for item in task_result.evidence if item.kind == "task-commit"] == [
        task_commit
    ]
    assert task_result.history_scope.truncated is False
    assert [item.oid for item in branch_result.evidence if item.kind == "branch-commit"] == [
        task_commit,
        repo.git("rev-parse", "HEAD").stdout.strip(),
    ]
    assert branch_result.observed[0].text == (
        "Found 2 branch-only commits for branch task/moisture-sensor."
    )


def test_branch_context_uses_the_configured_canonical_branch(repo) -> None:
    """This fails if branch deltas still resolve a hard-coded refs/heads/main."""
    repo.git("branch", "-m", "trunk")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "trunk"\n')
    repo.commit("Configure trunk as canonical")
    repo.checkout_new("task/trunk-context")
    repo.write("knowledge/trunk.md", "Trunk-relative observation\n")
    task_commit = repo.commit("Record the trunk-relative observation")

    result = repo.service().context(branch="task/trunk-context")

    assert [item.oid for item in result.evidence] == [task_commit]
    assert result.history_scope.refs == (
        "refs/heads/trunk",
        "refs/heads/task/trunk-context",
    )
    assert result.observed[0].text == ("Found 1 branch-only commit for branch task/trunk-context.")


def test_branch_context_pluralizes_zero_one_and_multiple_commits(repo) -> None:
    """This fails if branch-context prose uses one noun form for every count."""
    base = repo.git("rev-parse", "main").stdout.strip()
    repo.git("branch", "task/zero", base)
    repo.checkout_new("task/one", base)
    repo.write("knowledge/one.md", "One\n")
    repo.commit("Record one branch commit")
    repo.checkout_new("task/multiple", base)
    repo.write("knowledge/first.md", "First\n")
    repo.commit("Record the first branch commit")
    repo.write("knowledge/second.md", "Second\n")
    repo.commit("Record the second branch commit")

    service = repo.service()

    assert service.context(branch="task/zero").observed[0].text == (
        "Found 0 branch-only commits for branch task/zero."
    )
    assert service.context(branch="task/one").observed[0].text == (
        "Found 1 branch-only commit for branch task/one."
    )
    assert service.context(branch="task/multiple").observed[0].text == (
        "Found 2 branch-only commits for branch task/multiple."
    )


def test_timeline_passes_the_operation_remaining_time_to_signature_verification(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if each declared identity receives a fresh signature timeout."""
    repo.write("knowledge/signed.md", "Declared identity\n")
    repo.commit("Record a declared identity", trailers=("Agent-ID: gardener",))
    observed_timeouts: list[float | None] = []
    original = RepositoryInspector.signature_status

    def observe_timeout(self, oid, *, timeout_seconds=None):
        observed_timeouts.append(timeout_seconds)
        return original(self, oid, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(RepositoryInspector, "signature_status", observe_timeout)

    MemoryService.open(repo.path, limits=QueryLimits(timeout_seconds=2.0)).timeline(limit=1)

    assert len(observed_timeouts) == 1
    assert observed_timeouts[0] is not None
    assert 0 < observed_timeouts[0] <= 2.0


def test_context_reports_zero_task_matches_as_a_gap(repo) -> None:
    """This fails if an unmatched task ID is turned into an inferred narrative."""
    result = repo.service().context(task_id="TASK-IRR-404")

    assert result.inferred == ()
    assert result.gaps == ("No commits were found for task TASK-IRR-404.",)


def test_timeline_limits_a_path_with_spaces_and_orders_oldest_first(repo) -> None:
    """This fails if path data is split on whitespace or records remain newest first."""
    path = PurePosixPath("knowledge/observations with spaces.md")
    repo.write(path.as_posix(), "First observation\n")
    first = repo.commit("Record the first observation")
    repo.write("knowledge/unrelated.md", "Unrelated observation\n")
    repo.commit("Record an unrelated observation")
    repo.write(path.as_posix(), "Second observation\n")
    second = repo.commit("Record the second observation")

    result = repo.service().timeline(path=path, limit=10)

    assert [item.oid for item in result.evidence] == [first, second]
    assert all(item.path == path.as_posix() for item in result.evidence)


def test_timeline_caps_the_requested_limit_by_query_limits(repo) -> None:
    """This fails if a caller can exceed the configured history record bound."""
    repo.write("knowledge/observations.md", "First observation\n")
    repo.commit("Record the first observation")
    repo.write("knowledge/observations.md", "Second observation\n")
    second = repo.commit("Record the second observation")

    result = MemoryService.open(repo.path, limits=QueryLimits(max_commits=1)).timeline(limit=10)

    assert [item.oid for item in result.evidence] == [second]
    assert result.history_scope.commit_count == 1
    assert result.history_scope.truncated is True


def test_timeline_rejects_invalid_revision_ranges_and_paths(repo) -> None:
    """This fails if timeline passes invalid revision or path input through to Git."""
    with pytest.raises(ManduaError) as invalid_start:
        repo.service().timeline(start="not-a-commit")
    with pytest.raises(ManduaError) as invalid_path:
        repo.service().timeline(path=PurePosixPath("../outside.md"))

    assert invalid_start.value.code is ErrorCode.INVALID_REVISION
    assert invalid_path.value.code is ErrorCode.INVALID_PATH


def test_context_branch_limit_keeps_the_branch_tip(repo) -> None:
    """This fails if a chronological presentation drops the newest branch commit."""
    repo.checkout_new("task/tip-preservation")
    repo.write("knowledge/first.md", "First\n")
    repo.commit("Record the first branch event")
    repo.write("knowledge/middle.md", "Middle\n")
    middle = repo.commit("Record the middle branch event")
    repo.write("knowledge/tip.md", "Tip\n")
    tip = repo.commit("Record the branch tip event")

    result = repo.service().context(branch="task/tip-preservation", limit=2)

    assert [item.oid for item in result.evidence] == [middle, tip]
    assert result.history_scope.end_oid == tip


def test_default_context_limit_keeps_the_current_tip_and_reports_only_current_refs(repo) -> None:
    """This fails if default context truncation drops HEAD or reports unrelated branches."""
    repo.write("knowledge/first.md", "First\n")
    repo.commit("Record the first current event")
    repo.write("knowledge/middle.md", "Middle\n")
    middle = repo.commit("Record the middle current event")
    repo.write("knowledge/tip.md", "Tip\n")
    tip = repo.commit("Record the current tip event")
    repo.checkout_new("unrelated/history")
    repo.write("knowledge/unrelated.md", "Unrelated\n")
    repo.commit("Record unrelated history")
    repo.checkout("main")

    result = repo.service().context(limit=2)

    assert [item.oid for item in result.evidence if item.kind == "context-commit"] == [middle, tip]
    assert result.history_scope.end_oid == tip
    assert result.history_scope.refs == ("HEAD", "refs/heads/main")


def test_timeline_limit_keeps_the_end_revision(repo) -> None:
    """This fails if limiting an oldest-first timeline removes its inclusive end revision."""
    repo.write("knowledge/first.md", "First\n")
    repo.commit("Record the first timeline event")
    repo.write("knowledge/middle.md", "Middle\n")
    middle = repo.commit("Record the middle timeline event")
    repo.write("knowledge/tip.md", "Tip\n")
    tip = repo.commit("Record the timeline tip event")

    result = repo.service().timeline(limit=2)

    assert [item.oid for item in result.evidence] == [middle, tip]
    assert result.history_scope.end_oid == tip


def test_task_lookup_scans_the_global_window_before_applying_the_result_limit(repo) -> None:
    """This fails if newer non-matches hide an older exact task match inside the scan bound."""
    repo.write("knowledge/task.md", "Task observation\n")
    task_commit = repo.commit(_message("Record the task", "Task-ID: TASK-IRR-7"))
    for number in range(3):
        repo.write(f"knowledge/other-{number}.md", "Other observation\n")
        repo.commit(_message(f"Record other observation {number}", "Task-ID: TASK-IRR-70"))

    result = MemoryService.open(repo.path, limits=QueryLimits(max_commits=10)).context(
        task_id="TASK-IRR-7", limit=1
    )

    assert [item.oid for item in result.evidence] == [task_commit]
    assert result.gaps == ()


def test_task_lookup_discloses_when_the_result_limit_hides_exact_matches(repo) -> None:
    """This fails if a limited result claims that its one visible match is the only match."""
    repo.write("knowledge/first-task.md", "First task observation\n")
    repo.commit(_message("Record the first task", "Task-ID: TASK-IRR-7"))
    repo.write("knowledge/second-task.md", "Second task observation\n")
    repo.commit(_message("Record the second task", "Task-ID: TASK-IRR-7"))

    result = repo.service().context(task_id="TASK-IRR-7", limit=1)

    assert len(result.evidence) == 1
    assert result.history_scope.truncated is False
    assert "Task matches are limited by the requested result bound." in result.warnings
    assert any(claim.text == "Found 2 commits for task TASK-IRR-7." for claim in result.observed)
    assert all(claim.text != "Found 1 commit for task TASK-IRR-7." for claim in result.observed)


def test_timeline_range_excludes_start_and_includes_end(repo) -> None:
    """This fixes timeline ranges to native Git start..end semantics."""
    repo.write("knowledge/start.md", "Start\n")
    start = repo.commit("Record the range start")
    repo.write("knowledge/middle.md", "Middle\n")
    middle = repo.commit("Record the range middle")
    repo.write("knowledge/end.md", "End\n")
    end = repo.commit("Record the range end")

    result = repo.service().timeline(start=start, end=end, limit=10)

    assert [item.oid for item in result.evidence] == [middle, end]


def test_public_context_and_timeline_defaults_are_frozen() -> None:
    """This fails if callers lose the documented bounded default request size."""
    assert inspect.signature(MemoryService.context).parameters["limit"].default == 100
    timeline = inspect.signature(MemoryService.timeline).parameters
    assert timeline["end"].default == "HEAD"
    assert timeline["limit"].default == 100
