"""Acceptance tests for repository status queries."""

from __future__ import annotations

import pytest

import mandua.git_runner as git_runner_module
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRunner, _observe_git_processes
from mandua.memory_service import MemoryService
from mandua.models import QueryLimits


def test_status_distinguishes_index_worktree_and_history_limits(repo) -> None:
    """This fails if repository status collapses staged and working tree changes."""
    repo.write("knowledge/rules.md", "Current rule\n")
    repo.git("add", "knowledge/rules.md")
    repo.write("knowledge/rules.md", "Working tree rule\n")
    repo.write("knowledge/untracked.md", "Untracked observation\n")

    result = repo.service().status()
    payload = result.to_dict()

    assert result.operation == "status"
    assert any(item["details"]["state"] == "staged" for item in payload["evidence"])
    assert any(item["details"]["state"] == "modified" for item in payload["evidence"])
    assert any(item["details"]["state"] == "untracked" for item in payload["evidence"])
    assert result.history_scope.shallow is False
    assert result.history_scope.end_oid == repo.git("rev-parse", "HEAD").stdout.strip()


def test_status_caps_many_refs_without_per_ref_process_fanout(repo) -> None:
    """This fails if history scope launches one rev-parse process for every local ref."""
    for number in range(40):
        repo.git("branch", f"many/ref-{number:02d}", "HEAD")
    service = MemoryService.open(repo.path, limits=QueryLimits(max_commits=5))
    events = []

    with _observe_git_processes(events.append):
        result = service.status()

    launches = [event for event in events if event.phase == "considered"]
    assert len(launches) <= 16
    assert len(result.history_scope.refs) <= 6
    assert result.history_scope.truncated is True
    assert "History scope is limited by configured commit or display bounds." in result.warnings


def test_status_preserves_a_unicode_line_separator_inside_a_ref_name(repo) -> None:
    """This fails if a text split turns one valid non-ASCII ref into two records."""
    branch = "many/unicode-\u2028-separator"
    repo.git("branch", branch, "HEAD")

    result = repo.service().status()

    assert f"refs/heads/{branch}" in result.history_scope.refs
    assert "History access failed; the reported scope may be incomplete." not in result.warnings


def test_status_uses_one_total_deadline_across_injected_slow_runner_calls(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if each Git command receives a fresh public-read timeout."""
    service = MemoryService.open(repo.path, limits=QueryLimits(timeout_seconds=1.0))
    clock = 0.0
    calls = 0
    original_run = GitRunner.run

    def slow_run(self, *args, **kwargs):
        nonlocal clock, calls
        calls += 1
        clock += 0.4
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(git_runner_module, "_monotonic", lambda: clock)
    monkeypatch.setattr(GitRunner, "run", slow_run)

    with pytest.raises(ManduaError) as caught:
        service.status()

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert caught.value.message == "The aggregate Git operation time limit was exceeded."
    assert calls == 3


def test_status_preserves_renamed_paths_with_spaces_and_reports_staged_deletions(repo) -> None:
    """This fails if porcelain parsing loses NUL-delimited rename paths or deletes."""
    repo.write("knowledge/source rule.md", "Renamed rule\n")
    repo.write("knowledge/removed rule.md", "Removed rule\n")
    repo.commit("Add status fixtures")
    repo.git("mv", "knowledge/source rule.md", "knowledge/renamed rule.md")
    (repo.path / "knowledge/removed rule.md").unlink()
    repo.git("add", "-u")

    result = repo.service().status()
    evidence = result.to_dict()["evidence"]

    renamed = next(item for item in evidence if item["details"]["state"] == "renamed")
    assert renamed["path"] == "knowledge/renamed rule.md"
    assert renamed["details"]["previous_path"] == "knowledge/source rule.md"
    assert any(
        item["details"]["state"] == "deleted" and item["path"] == "knowledge/removed rule.md"
        for item in evidence
    )
