"""Real-Git acceptance tests for explicit semantic checkpoints."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

import mandua.writes.checkpoint as checkpoint_module
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitOutput, GitRunner, _observe_git_processes
from mandua.memory_service import MemoryService
from mandua.models import CheckpointRequest, CommitMetadata, QueryLimits
from mandua.policy import Policy
from mandua.writes.checkpoint import CheckpointWriter


class _InjectedCheckpointControl(BaseException):
    """Non-Mandua control flow injected after a real checkpoint ref update."""


def _checkpoint_authority_roots(repository: Path) -> dict[str, Path]:
    def absolute_path(*arguments: str) -> Path:
        output = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return Path(output.stdout.strip()).resolve()

    git_directory = absolute_path("rev-parse", "--absolute-git-dir")
    common_directory = absolute_path("rev-parse", "--path-format=absolute", "--git-common-dir")
    return {
        "worktree": repository.resolve(),
        "git-directory": git_directory,
        "common-directory": common_directory,
        "object-directory": common_directory / "objects",
    }


def _replace_checkpoint_authority_root(path: Path) -> Path:
    """Replace one root inode while preserving its complete fixture namespace."""
    mode = path.stat(follow_symlinks=False).st_mode
    displaced = path.with_name(f"{path.name}.mandua-checkpoint-authority")
    assert not displaced.exists()
    path.rename(displaced)
    try:
        path.mkdir()
        path.chmod(mode & 0o777)
        for child in tuple(displaced.iterdir()):
            child.rename(path / child.name)
    except BaseException:
        if path.exists():
            for child in tuple(path.iterdir()):
                child.rename(displaced / child.name)
            path.rmdir()
        displaced.rename(path)
        raise
    return displaced


def _restore_checkpoint_authority_root(path: Path, displaced: Path) -> None:
    for child in tuple(path.iterdir()):
        child.rename(displaced / child.name)
    path.rmdir()
    displaced.rename(path)


def _descriptor_is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return True
    return False


def _round3_long_ref(namespace: str) -> str:
    components = "/".join(f"{index:02d}-{'x' * 72}" for index in range(7))
    return f"{namespace}/{components}"


def _round3_pack_large_authority_metadata(repo) -> Path:
    for index in range(12):
        repo.git("branch", f"round3-packed/{index:02d}-{'p' * 40}", "HEAD")
    repo.git("pack-refs", "--all", "--prune")
    common = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    packed = common / "packed-refs"
    assert packed.stat().st_size > 512
    return packed


def _round3_planned_argv_bytes(events) -> int:
    return sum(
        sum(len(os.fsencode(argument)) + 1 for argument in event.command)
        for event in events
        if event.phase == "considered"
    )


def _metadata() -> CommitMetadata:
    return CommitMetadata(
        memory_type="decision",
        scope="irrigation",
        task_id="TASK-IRR-7",
        decision_id="DEC-IRR-1",
        agent_id="gardener",
        reason="The sensor trial reduced water use.",
        extra_trailers=(("Review-State", "pending"),),
    )


def _request(*paths: str, staged: bool = False) -> CheckpointRequest:
    return CheckpointRequest(
        subject="Record the moisture threshold",
        metadata=_metadata(),
        paths=tuple(PurePosixPath(path) for path in paths),
        staged=staged,
    )


def test_checkpoint_previews_and_commits_only_explicit_paths(repo) -> None:
    """This fails if preview mutates HEAD or apply stages an unselected worktree path."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.write("private-notes.txt", "Do not commit this file\n")
    request = _request("knowledge/rules.md")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    preview = repo.service().checkpoint(request)

    assert preview.applied is False
    assert preview.changes[0].action == "update-ref"
    assert preview.changes[0].target == "refs/heads/main"
    assert preview.changes[0].before_oid == before
    assert preview.changes[0].after_oid is None
    assert preview.evidence[0].kind == "checkpoint-preview"
    assert preview.evidence[0].details["parent_oid"] == before
    assert preview.evidence[0].details["branch"] == "main"
    assert "knowledge/rules.md" in preview.evidence[0].details["diff_summary"]
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert repo.git("status", "--porcelain").stdout

    applied = repo.service().checkpoint(request, apply=True)

    commit = repo.git("rev-parse", "HEAD").stdout.strip()
    assert applied.applied is True
    assert applied.changes[0].after_oid == commit
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Threshold: 35%\n"
    assert repo.git("show", "HEAD:private-notes.txt", check=False).returncode != 0
    assert (repo.path / "private-notes.txt").exists()
    assert repo.git("diff", "--cached", "--", "knowledge/rules.md").stdout == ""
    assert repo.git("show", "-s", "--format=%B", "HEAD").stdout == (
        "Record the moisture threshold\n\n"
        "Reason: The sensor trial reduced water use.\n\n"
        "Memory-Type: decision\n"
        "Scope: irrigation\n"
        "Task-ID: TASK-IRR-7\n"
        "Decision-ID: DEC-IRR-1\n"
        "Agent-ID: gardener\n"
        "Review-State: pending\n"
    )


@pytest.mark.parametrize(
    "checkpoint_request",
    (
        _request(),
        _request("knowledge/rules.md", staged=True),
    ),
)
def test_checkpoint_requires_exactly_explicit_paths_or_staged_mode(
    repo, checkpoint_request
) -> None:
    """This fails if an ambiguous request can select an implicit source of content."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(checkpoint_request, apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_explicit_checkpoint_preserves_unrelated_staged_changes(repo) -> None:
    """This fails if real-index synchronization resets entries outside the selected paths."""
    repo.write("knowledge/rules.md", "Threshold: 80%\n")
    repo.write("knowledge/unrelated.md", "Unrelated baseline\n")
    repo.commit("Create checkpoint baselines")
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.write("knowledge/unrelated.md", "Unrelated staged change\n")
    repo.git("add", "knowledge/unrelated.md")

    result = repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert result.applied is True
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Threshold: 35%\n"
    assert repo.git("show", "HEAD:knowledge/unrelated.md").stdout == "Unrelated baseline\n"
    assert repo.git("diff", "--cached", "--name-only").stdout == "knowledge/unrelated.md\n"
    assert repo.git("diff", "--cached", "--", "knowledge/rules.md").stdout == ""


def test_staged_checkpoint_commits_the_exact_index_and_leaves_it_matching(repo) -> None:
    """This fails if staged mode reads the worktree or rewrites the matching real index."""
    repo.write("knowledge/rules.md", "Staged threshold: 35%\n")
    repo.write("knowledge/observation.md", "Staged observation\n")
    repo.git("add", "knowledge/rules.md", "knowledge/observation.md")
    repo.write("knowledge/rules.md", "Unstaged threshold: 40%\n")
    repo.write("private-notes.txt", "Leave this untracked\n")
    staged_tree = repo.git("write-tree").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()

    preview = repo.service().checkpoint(_request(staged=True))
    result = repo.service().checkpoint(_request(staged=True), apply=True)

    assert preview.evidence[0].oid == staged_tree
    assert result.applied is True
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Staged threshold: 35%\n"
    assert repo.git("show", "HEAD:knowledge/observation.md").stdout == "Staged observation\n"
    assert repo.git("diff", "--cached").stdout == ""
    assert repo.git("diff", "--", "knowledge/rules.md").stdout
    assert (repo.path / "private-notes.txt").exists()
    assert index_path.read_bytes() == index_before


def test_explicit_checkpoint_records_a_deletion(repo) -> None:
    """This fails if isolated staging ignores selected paths missing from the worktree."""
    repo.write("knowledge/temporary.md", "Temporary observation\n")
    repo.commit("Record a temporary observation")
    (repo.path / "knowledge" / "temporary.md").unlink()

    result = repo.service().checkpoint(_request("knowledge/temporary.md"), apply=True)

    assert result.applied is True
    assert repo.git("show", "HEAD:knowledge/temporary.md", check=False).returncode != 0
    assert repo.git("diff", "--cached").stdout == ""


def test_explicit_paths_are_literal_git_pathspecs(repo) -> None:
    """This fails if wildcard or magic-looking file names broaden isolated staging."""
    selected = (
        "knowledge/star*.md",
        "knowledge/question?.md",
        "knowledge/bracket[ab].md",
        "knowledge/:(glob)danger*.md",
    )
    unselected = (
        "knowledge/star-one.md",
        "knowledge/question-one.md",
        "knowledge/bracketa.md",
        "knowledge/danger-one.md",
    )
    for path in (*selected, *unselected):
        repo.write(path, f"Content for {path}\n")

    result = repo.service().checkpoint(_request(*selected), apply=True)

    committed = set(repo.git("ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    assert result.applied is True
    assert committed == set(selected)
    assert all((repo.path / path).exists() for path in unselected)


def test_git_runner_rejects_an_arbitrary_index_file_target(repo, tmp_path) -> None:
    """This fails if the isolated-index API can overwrite a caller-selected filesystem path."""
    target = tmp_path / "arbitrary-index"

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["read-tree", "HEAD"], index_file=target)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert not target.exists()


def test_checkpoint_rejects_detached_head_without_creating_a_commit(repo) -> None:
    """This fails if a write can proceed without one captured local branch ref."""
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("checkout", "--detach", before)
    repo.write("knowledge/rules.md", "Threshold: 35%\n")

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_rejects_a_local_branch_ref_that_does_not_point_to_a_commit(repo) -> None:
    """This fails if branch capture peels an annotated tag instead of guarding the exact ref OID."""
    repo.git("tag", "-a", "tagged-state", "-m", "Create an annotated state tag")
    tag_oid = repo.git("rev-parse", "tagged-state^{tag}").stdout.strip()
    (repo.path / ".git" / "refs" / "heads" / "tagged-state").write_text(
        tag_oid + "\n", encoding="ascii"
    )
    repo.git("symbolic-ref", "HEAD", "refs/heads/tagged-state")
    repo.write("knowledge/rules.md", "Threshold: 35%\n")

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"))

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "refs/heads/tagged-state").stdout.strip() == tag_oid


def test_checkpoint_policy_failure_does_not_move_the_branch(repo) -> None:
    """This fails if a secret-containing proposed tree reaches update-ref."""
    repo.write("knowledge/token.txt", "api_key = 'abcdefghijklmnopqrstuvwxyz123456'\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/token.txt"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert repo.git("log", "--format=%s", "--all").stdout.splitlines() == [
        "Create an empty initial commit"
    ]


def test_checkpoint_rechecks_selection_without_following_a_raced_parent_symlink(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if the post-policy selection probe follows a newly swapped parent symlink."""
    repo.write("knowledge/section/rules.md", "Threshold: 35%\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rules.md").write_text("Outside content\n", encoding="utf-8")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = Policy.validate_path
    swapped = False

    def swap_parent_after_validation(self, path):
        nonlocal swapped
        result = original(self, path)
        if not swapped:
            (repo.path / "knowledge" / "section").rename(repo.path / "knowledge" / "saved-section")
            os.symlink(outside, repo.path / "knowledge" / "section")
            swapped = True
        return result

    monkeypatch.setattr(Policy, "validate_path", swap_parent_after_validation)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/section/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert (outside / "rules.md").read_text(encoding="utf-8") == "Outside content\n"


def test_checkpoint_rejects_an_unchanged_or_unknown_explicit_path(repo) -> None:
    """This fails if a typo or unchanged file creates an empty semantic commit."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.commit("Record the existing threshold")

    for path in ("knowledge/rules.md", "knowledge/typo.md"):
        with pytest.raises(ManduaError) as caught:
            repo.service().checkpoint(_request(path), apply=True)

        assert caught.value.code is ErrorCode.VALIDATION_FAILED
        assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_does_not_treat_a_missing_directory_prefix_as_one_file(repo) -> None:
    """This fails if an exact-looking deletion path removes tracked descendants as a pathspec."""
    repo.write("knowledge/section/first.md", "First observation\n")
    repo.write("knowledge/section/second.md", "Second observation\n")
    before = repo.commit("Record section observations")
    (repo.path / "knowledge" / "section" / "first.md").unlink()
    (repo.path / "knowledge" / "section" / "second.md").unlink()
    (repo.path / "knowledge" / "section").rmdir()

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/section"), apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert repo.git("ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() == [
        "knowledge/section/first.md",
        "knowledge/section/second.md",
    ]


def test_checkpoint_does_not_replace_a_tracked_directory_through_one_file_selection(repo) -> None:
    """This fails if a directory-to-file replacement silently deletes unselected descendants."""
    repo.write("knowledge/section/first.md", "First observation\n")
    repo.write("knowledge/section/second.md", "Second observation\n")
    before = repo.commit("Record section observations")
    (repo.path / "knowledge" / "section" / "first.md").unlink()
    (repo.path / "knowledge" / "section" / "second.md").unlink()
    (repo.path / "knowledge" / "section").rmdir()
    repo.write("knowledge/section", "Replacement observation\n")

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/section"), apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_rejects_worktree_policy_not_present_in_the_proposed_tree(repo) -> None:
    """This fails if an uncommitted policy can authorize content its committed tree ignores."""
    repo.write(
        ".mandua.toml",
        '[repository]\nknowledge_roots = ["memory"]\n',
    )
    repo.write("memory/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("memory/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_does_not_execute_a_selected_path_clean_filter(repo, tmp_path) -> None:
    """This fails if isolated git add executes a repository-configured clean program."""
    repo.write(".gitattributes", "knowledge/*.md filter=untrusted\n")
    repo.commit("Declare an untrusted content filter")
    marker = tmp_path / "filter-executed"
    filter_program = tmp_path / "untrusted-filter"
    filter_program.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat\n",
        encoding="utf-8",
    )
    filter_program.chmod(0o700)
    repo.git("config", "filter.untrusted.clean", str(filter_program))
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert not marker.exists()
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_rejects_a_filter_driver_named_like_an_attribute_sentinel(
    repo, tmp_path
) -> None:
    """This fails if the text `unset` is confused with proof that no clean driver applies."""
    repo.write(".gitattributes", "knowledge/*.md filter=unset\n")
    repo.commit("Declare an ambiguously named content filter")
    marker = tmp_path / "sentinel-filter-executed"
    filter_program = tmp_path / "sentinel-filter"
    filter_program.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat\n",
        encoding="utf-8",
    )
    filter_program.chmod(0o700)
    repo.git("config", "filter.unset.clean", str(filter_program))
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert not marker.exists()
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_hashes_the_validated_descriptor_snapshot_without_filters(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if staging can reread a path through repository content transforms."""
    marker = tmp_path / "late-filter-executed"
    filter_program = tmp_path / "late-filter"
    filter_program.write_text(
        f"#!/bin/sh\ntouch {marker}\nsed 's/35/99/'\n",
        encoding="utf-8",
    )
    filter_program.chmod(0o700)
    repo.git("config", "filter.untrusted.clean", str(filter_program))
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    original = CheckpointWriter._write_isolated_tree

    def install_filter_at_staging_boundary(self, parent_oid, paths):
        attributes = repo.path / ".gitattributes"
        attributes.write_text("knowledge/*.md filter=untrusted\n", encoding="utf-8")
        try:
            return original(self, parent_oid, paths)
        finally:
            attributes.unlink()

    monkeypatch.setattr(
        CheckpointWriter,
        "_write_isolated_tree",
        install_filter_at_staging_boundary,
    )

    result = repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert result.applied is True
    assert not marker.exists()
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Threshold: 35%\n"


def test_checkpoint_detects_selected_content_race_before_ref_update(repo, monkeypatch) -> None:
    """This fails if content changed after commit-tree can still move the branch ref."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = CheckpointWriter._commit_tree

    def change_after_commit_tree(self, prepared):
        commit = original(self, prepared)
        repo.write("knowledge/rules.md", "Threshold: 40%\n")
        return commit

    monkeypatch.setattr(CheckpointWriter, "_commit_tree", change_after_commit_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_detects_policy_race_before_ref_update(repo, monkeypatch) -> None:
    """This fails if a changed worktree policy is ignored when the proposed tree is unchanged."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = CheckpointWriter._commit_tree

    def change_policy_after_commit_tree(self, prepared):
        commit = original(self, prepared)
        repo.write(".mandua.toml", '[repository]\ncanonical_branch = "trunk"\n')
        return commit

    monkeypatch.setattr(CheckpointWriter, "_commit_tree", change_policy_after_commit_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_detects_branch_race_before_ref_update(repo, monkeypatch) -> None:
    """This fails if checkout races redirect a checkpoint after its commit object is prepared."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    main_before = repo.git("rev-parse", "main").stdout.strip()
    original = CheckpointWriter._commit_tree

    def checkout_after_commit_tree(self, prepared):
        commit = original(self, prepared)
        repo.checkout_new("concurrent/branch", start=main_before)
        return commit

    monkeypatch.setattr(CheckpointWriter, "_commit_tree", checkout_after_commit_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "main").stdout.strip() == main_before
    assert repo.git("symbolic-ref", "--short", "HEAD").stdout.strip() == "concurrent/branch"


def test_checkpoint_blocks_a_concurrent_commit_at_the_locked_ref_boundary(
    repo, monkeypatch
) -> None:
    """This fails if a normal commit can cross the final HEAD/index transaction boundary."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = CheckpointWriter._update_ref
    attempts: list[tuple[int, int]] = []

    def commit_before_update(self, prepared, commit_oid):
        repo.write("knowledge/concurrent.md", "Concurrent observation\n")
        staged = repo.git("add", "knowledge/concurrent.md", check=False)
        committed = repo.git(
            "commit", "--no-gpg-sign", "-m", "Record a concurrent observation", check=False
        )
        attempts.append((staged.returncode, committed.returncode))
        return original(self, prepared, commit_oid)

    monkeypatch.setattr(CheckpointWriter, "_update_ref", commit_before_update)

    result = repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert result.applied is True
    assert attempts and all(returncode != 0 for returncode in attempts[0])
    assert repo.git("rev-parse", "HEAD").stdout.strip() != before
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Threshold: 35%\n"
    assert repo.git("show", "HEAD:knowledge/concurrent.md", check=False).returncode != 0


def test_staged_checkpoint_detects_index_race_before_ref_update(repo, monkeypatch) -> None:
    """This fails if staged content changed after commit-tree can still move the branch ref."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.git("add", "knowledge/rules.md")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = CheckpointWriter._commit_tree

    def stage_after_commit_tree(self, prepared):
        commit = original(self, prepared)
        repo.write("knowledge/concurrent.md", "Concurrent observation\n")
        repo.git("add", "knowledge/concurrent.md")
        return commit

    monkeypatch.setattr(CheckpointWriter, "_commit_tree", stage_after_commit_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request(staged=True), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_explicit_checkpoint_holds_head_and_index_locks_during_index_install(
    repo, monkeypatch
) -> None:
    """This fails if checkout or direct HEAD writers can redirect index installation."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    main_before = repo.git("rev-parse", "main").stdout.strip()
    repo.git("branch", "concurrent/branch", main_before)
    attempts: list[tuple[int, int]] = []
    original = CheckpointWriter._install_locked_index

    def race_at_index_install(self, locks, contents):
        checkout = repo.git("checkout", "concurrent/branch", check=False)
        symbolic = repo.git("symbolic-ref", "HEAD", "refs/heads/concurrent/branch", check=False)
        attempts.append((checkout.returncode, symbolic.returncode))
        return original(self, locks, contents)

    monkeypatch.setattr(CheckpointWriter, "_install_locked_index", race_at_index_install)

    result = repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert result.applied is True
    assert attempts and all(returncode != 0 for returncode in attempts[0])
    assert repo.git("symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert repo.git("rev-parse", "concurrent/branch").stdout.strip() == main_before
    assert repo.git("show", "main:knowledge/rules.md").stdout == "Threshold: 35%\n"
    assert repo.git("diff", "--cached").stdout == ""
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_rolls_back_if_head_changes_after_native_ref_update(repo, monkeypatch) -> None:
    """This fails if the native ref-to-HEAD-lock gap can redirect index installation."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    main_before = repo.git("rev-parse", "main").stdout.strip()
    repo.git("branch", "concurrent/branch", main_before)
    attempts: list[int] = []
    original = CheckpointWriter._acquire_head_lock

    def switch_head_before_lock(self, locks):
        attempts.append(
            repo.git("symbolic-ref", "HEAD", "refs/heads/concurrent/branch", check=False).returncode
        )
        return original(self, locks)

    monkeypatch.setattr(CheckpointWriter, "_acquire_head_lock", switch_head_before_lock)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert attempts == [0]
    assert repo.git("rev-parse", "main").stdout.strip() == main_before
    assert repo.git("symbolic-ref", "--short", "HEAD").stdout.strip() == "concurrent/branch"
    assert repo.git("diff", "--cached").stdout == ""
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    assert repo.git("show", "main:knowledge/rules.md", check=False).returncode != 0
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_explicit_checkpoint_blocks_a_last_boundary_index_writer(repo, monkeypatch) -> None:
    """This fails if an explicit checkpoint can overwrite or absorb a raced real index."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.write("knowledge/concurrent.md", "Concurrent observation\n")
    attempts: list[int] = []
    original = GitRunner.run_text

    def race_at_update_ref(self, arguments, **kwargs):
        if arguments[:2] == ["update-ref", "--no-deref"] and not attempts:
            attempts.append(repo.git("add", "knowledge/concurrent.md", check=False).returncode)
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", race_at_update_ref)

    result = repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert result.applied is True
    assert len(attempts) == 1 and attempts[0] != 0
    assert repo.git("diff", "--cached").stdout == ""
    assert repo.git("show", "HEAD:knowledge/concurrent.md", check=False).returncode != 0


def test_staged_checkpoint_blocks_a_last_boundary_index_writer(repo, monkeypatch) -> None:
    """This fails if staged apply releases the real index before moving the ref."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.git("add", "knowledge/rules.md")
    repo.write("knowledge/concurrent.md", "Concurrent observation\n")
    attempts: list[int] = []
    original = GitRunner.run_text

    def race_at_update_ref(self, arguments, **kwargs):
        if arguments[:2] == ["update-ref", "--no-deref"] and not attempts:
            attempts.append(repo.git("add", "knowledge/concurrent.md", check=False).returncode)
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", race_at_update_ref)

    result = repo.service().checkpoint(_request(staged=True), apply=True)

    assert result.applied is True
    assert len(attempts) == 1 and attempts[0] != 0
    assert repo.git("diff", "--cached").stdout == ""
    assert repo.git("show", "HEAD:knowledge/concurrent.md", check=False).returncode != 0


def test_checkpoint_expected_old_cas_rejects_a_last_boundary_ref_writer(repo, monkeypatch) -> None:
    """This fails if a direct ref writer can be overwritten at the native CAS boundary."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    parent_tree = repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    concurrent = repo.git(
        "commit-tree", parent_tree, "-p", before, "-m", "Record concurrent ref state"
    ).stdout.strip()
    attempts: list[int] = []
    original = GitRunner.run_text

    def race_at_update_ref(self, arguments, **kwargs):
        if arguments[:2] == ["update-ref", "--no-deref"] and not attempts:
            attempts.append(
                repo.git(
                    "update-ref", "refs/heads/main", concurrent, before, check=False
                ).returncode
            )
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", race_at_update_ref)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert attempts == [0]
    assert repo.git("rev-parse", "HEAD").stdout.strip() == concurrent
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_bounds_explicit_path_count_before_git(repo, monkeypatch) -> None:
    """This fails if an oversized request reaches Git argument construction."""
    request = _request(*(f"knowledge/item-{index}.md" for index in range(257)))
    service = repo.service()

    def unexpected_git(*args, **kwargs):
        raise AssertionError("Git must not run for an oversized path request")

    monkeypatch.setattr(GitRunner, "run_text", unexpected_git)

    with pytest.raises(ManduaError) as caught:
        service.checkpoint(request)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_checkpoint_accepts_the_explicit_path_count_boundary(repo) -> None:
    """This fails if the documented default count boundary is off by one."""
    writer = CheckpointWriter(repo.path, QueryLimits())
    writer._deadline = time.monotonic() + 1
    request = _request(*(f"knowledge/item-{index}.md" for index in range(256)))

    writer._validate_request(request, False)


def test_checkpoint_bounds_aggregate_path_bytes_before_git(repo, monkeypatch) -> None:
    """This fails if individually valid paths can create an oversized aggregate Git input."""
    component = "a" * 200
    paths = tuple(
        f"knowledge/{index}/{component}/{component}/{component}/{component}/{component}/"
        f"{component}/{component}/{component}/{component}/{component}/{component}/"
        f"{component}/{component}/{component}/{component}/{component}/{component}/leaf.md"
        for index in range(20)
    )
    service = repo.service()

    def unexpected_git(*args, **kwargs):
        raise AssertionError("Git must not run for an oversized aggregate path request")

    monkeypatch.setattr(GitRunner, "run_text", unexpected_git)

    with pytest.raises(ManduaError) as caught:
        service.checkpoint(_request(*paths))

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_checkpoint_enforces_deadline_before_path_deduplication(repo) -> None:
    """This fails if request allocation work happens after the operation deadline expires."""
    writer = CheckpointWriter(repo.path, QueryLimits())
    writer._deadline = time.monotonic() - 1

    with pytest.raises(ManduaError) as caught:
        writer._validate_request(_request("knowledge/rules.md"), False)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("failure_code", "failure_message", "expire_deadline"),
    (
        (
            ErrorCode.GIT_FAILURE,
            "Injected failure after the native ref update.",
            False,
        ),
        (
            ErrorCode.LIMIT_EXCEEDED,
            "Injected main deadline exhaustion after the native ref update.",
            True,
        ),
    ),
)
def test_checkpoint_reconciles_a_runner_exception_after_the_native_ref_update(
    repo, monkeypatch, failure_code, failure_message, expire_deadline
) -> None:
    """This fails if a raised runner call is assumed not to have moved the ref."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    interrupted_updates = 0

    def update_then_raise(arguments, **kwargs):
        nonlocal interrupted_updates
        output = original(arguments, **kwargs)
        if arguments[:2] == ["update-ref", "--no-deref"] and not interrupted_updates:
            interrupted_updates += 1
            if expire_deadline:
                writer._deadline = time.monotonic() - 1
            raise ManduaError(failure_code, failure_message)
        return output

    monkeypatch.setattr(writer._runner, "run_text", update_then_raise)

    with pytest.raises(ManduaError) as caught:
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is failure_code
    assert caught.value.message == failure_message
    assert interrupted_updates == 1
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_rolls_back_real_ref_update_after_terminal_observer_failure(repo) -> None:
    """This fails if a completed update observer failure escapes before safe rollback."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Injected terminal observation failure after checkpoint ref update.",
    )
    completed_updates = []

    def interrupt_first_completed_update(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)
            if len(completed_updates) == 1:
                raise failure

    with (
        _observe_git_processes(interrupt_first_completed_update),
        pytest.raises(ManduaError) as caught,
    ):
        CheckpointWriter(repo.path, QueryLimits()).checkpoint(
            _request("knowledge/rules.md"), apply=True
        )

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.__cause__ is failure
    assert len(completed_updates) == 2
    requested_oid = completed_updates[0].arguments[3]
    assert requested_oid != before
    assert completed_updates[0].arguments[4] == before
    assert completed_updates[1].arguments[3:5] == (before, requested_oid)
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert (
        repo.git("write-tree").stdout.strip() == repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    )
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )


def test_checkpoint_base_exception_after_real_ref_update_rolls_back_before_index_install(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if non-Mandua control flow leaves HEAD advanced before index install."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    failure = _InjectedCheckpointControl("injected after real checkpoint ref update")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    completed_updates = []
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)

    def update_then_interrupt(arguments, **kwargs):
        nonlocal interrupted
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", "refs/heads/main"] and not interrupted:
            interrupted = True
            assert output.returncode == 0 and not output.stdout and not output.stderr
            raise failure
        return output

    monkeypatch.setattr(writer._runner, "run_text", update_then_interrupt)

    with (
        _observe_git_processes(observe_process),
        pytest.raises(_InjectedCheckpointControl) as caught,
    ):
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value is failure
    assert interrupted is True
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert (
        repo.git("write-tree").stdout.strip() == repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    )
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    assert len(completed_updates) == 2
    requested_oid = completed_updates[0].arguments[3]
    assert completed_updates[0].arguments[4] == before
    assert completed_updates[1].arguments[3:5] == (before, requested_oid)


def test_checkpoint_base_exception_after_normal_ref_return_rolls_back_before_index_install(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-ref control flow must not strand HEAD ahead of the uninstalled index."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    failure = _InjectedCheckpointControl("injected after checkpoint update-ref returned")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._require_post_ref_state
    completed_updates = []
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)

    def verify_then_interrupt(prepared, commit_oid) -> None:
        nonlocal interrupted
        original(prepared, commit_oid)
        if not interrupted:
            interrupted = True
            raise failure

    monkeypatch.setattr(writer, "_require_post_ref_state", verify_then_interrupt)

    with (
        _observe_git_processes(observe_process),
        pytest.raises(_InjectedCheckpointControl) as caught,
    ):
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value is failure
    assert interrupted is True
    assert len(completed_updates) == 2
    requested_oid = completed_updates[0].arguments[3]
    assert requested_oid != before
    assert completed_updates[0].arguments[4] == before
    assert completed_updates[1].arguments[3:5] == (before, requested_oid)
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert (
        repo.git("write-tree").stdout.strip() == repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    )
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )


def test_checkpoint_preserves_a_safe_runner_failure_when_the_ref_remains_old(
    repo, monkeypatch
) -> None:
    """This fails if reconciliation replaces an original failure without a ref mutation."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    injected = ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected pre-update timeout.")
    interrupted_updates = 0

    def raise_before_update(arguments, **kwargs):
        nonlocal interrupted_updates
        if arguments[:2] == ["update-ref", "--no-deref"] and not interrupted_updates:
            interrupted_updates += 1
            raise injected
        return original(arguments, **kwargs)

    monkeypatch.setattr(writer._runner, "run_text", raise_before_update)

    with pytest.raises(ManduaError) as caught:
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value is injected
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_discloses_an_unexpected_ref_after_an_interrupted_native_update(
    repo, monkeypatch
) -> None:
    """This fails if an exceptional update masks a third-party branch value."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    parent_tree = repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    concurrent = repo.git(
        "commit-tree", parent_tree, "-p", before, "-m", "Record concurrent ref state"
    ).stdout.strip()
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    injected = ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected post-update timeout.")
    interrupted_updates = 0

    def update_change_again_then_raise(arguments, **kwargs):
        nonlocal interrupted_updates
        output = original(arguments, **kwargs)
        if arguments[:2] == ["update-ref", "--no-deref"] and not interrupted_updates:
            interrupted_updates += 1
            commit_oid = arguments[3]
            assert (
                repo.git(
                    "update-ref", "refs/heads/main", concurrent, commit_oid, check=False
                ).returncode
                == 0
            )
            raise injected
        return output

    monkeypatch.setattr(writer._runner, "run_text", update_change_again_then_raise)

    with pytest.raises(ManduaError) as caught:
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.__cause__ is injected
    assert caught.value.recovery is not None
    assert "refs/heads/main" in caught.value.recovery
    assert repo.git("rev-parse", "HEAD").stdout.strip() == concurrent
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_discloses_a_failed_interrupted_update_rollback(repo, monkeypatch) -> None:
    """This fails if rollback failure after a raised update is reported as mutation-free."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = GitRunner.run_text
    injected = ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected post-update timeout.")
    update_calls = 0

    def interrupt_then_reject_rollback(self, arguments, **kwargs):
        nonlocal update_calls
        if arguments[:2] == ["update-ref", "--no-deref"]:
            update_calls += 1
            if update_calls == 2:
                return GitOutput(stdout="", stderr="rollback failed", returncode=1)
        output = original(self, arguments, **kwargs)
        if arguments[:2] == ["update-ref", "--no-deref"] and update_calls == 1:
            raise injected
        return output

    monkeypatch.setattr(GitRunner, "run_text", interrupt_then_reject_rollback)

    with pytest.raises(ManduaError) as caught:
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.__cause__ is injected
    assert caught.value.recovery is not None
    assert "refs/heads/main" in caught.value.recovery
    assert repo.git("rev-parse", "HEAD").stdout.strip() != before
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_rolls_back_the_ref_when_selected_index_sync_fails(repo, monkeypatch) -> None:
    """This fails if an index-install failure leaves the checkpoint ref advanced."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    def fail_install(self, locks, contents):
        raise ManduaError(ErrorCode.GIT_FAILURE, "Injected index install failure.")

    monkeypatch.setattr(
        CheckpointWriter,
        "_install_locked_index",
        fail_install,
        raising=False,
    )

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


def test_checkpoint_uses_a_bounded_emergency_rollback_after_deadline_exhaustion(
    repo, monkeypatch
) -> None:
    """This fails if an expired main deadline prevents rollback after the ref already moved."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()

    def exhaust_deadline_before_sync(self, locks, contents):
        self._deadline = time.monotonic() - 1
        raise ManduaError(
            ErrorCode.LIMIT_EXCEEDED,
            "Injected selected-index synchronization timeout.",
        )

    monkeypatch.setattr(
        CheckpointWriter,
        "_install_locked_index",
        exhaust_deadline_before_sync,
        raising=False,
    )

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_discloses_uncertain_state_when_index_install_and_rollback_fail(
    repo, monkeypatch
) -> None:
    """This fails if a moved ref is reported like a mutation-free checkpoint failure."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    update_calls = 0
    original = GitRunner.run_text

    def fail_install(self, locks, contents):
        raise ManduaError(ErrorCode.GIT_FAILURE, "Injected index install failure.")

    def fail_second_update(self, arguments, **kwargs):
        nonlocal update_calls
        if arguments[:2] == ["update-ref", "--no-deref"]:
            update_calls += 1
            if update_calls == 2:
                return GitOutput(stdout="", stderr="rollback failed", returncode=1)
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(CheckpointWriter, "_install_locked_index", fail_install)
    monkeypatch.setattr(GitRunner, "run_text", fail_second_update)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert "refs/heads/main" in caught.value.recovery
    assert repo.git("rev-parse", "HEAD").stdout.strip() != before
    assert (
        repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode != 0
    )


def test_checkpoint_rejects_success_diagnostics_from_atomic_ref_update(repo, monkeypatch) -> None:
    """This fails if an anomalous update-ref response is reported as a successful apply."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = GitRunner.run_text

    def diagnose_update(self, arguments, **kwargs):
        if arguments and arguments[0] == "update-ref":
            return GitOutput(stdout="", stderr="unexpected diagnostic", returncode=0)
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", diagnose_update)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_rejects_isolated_write_tree_diagnostics(repo, monkeypatch) -> None:
    """This fails if exit-zero diagnostics from the isolated index are silently accepted."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = CheckpointWriter._run_index

    def diagnose_write_tree(self, arguments, **kwargs):
        output = original(self, arguments, **kwargs)
        if arguments == ["write-tree"]:
            return GitOutput(
                stdout=output.stdout,
                stderr="unexpected diagnostic",
                returncode=output.returncode,
            )
        return output

    monkeypatch.setattr(CheckpointWriter, "_run_index", diagnose_write_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_checkpoint_operates_from_a_linked_worktree(repo, tmp_path) -> None:
    """This fails if isolated index handling assumes .git is a directory in every worktree."""
    linked = tmp_path / "linked-worktree"
    main_before = repo.git("rev-parse", "main").stdout.strip()
    repo.git("worktree", "add", "-b", "task/checkpoint", str(linked), main_before)
    (linked / "knowledge").mkdir()
    (linked / "knowledge" / "rules.md").write_text("Threshold: 35%\n", encoding="utf-8")

    result = MemoryService.open(linked).checkpoint(_request("knowledge/rules.md"), apply=True)

    assert result.applied is True
    assert repo.git("rev-parse", "main").stdout.strip() == main_before
    assert repo.git("rev-parse", "task/checkpoint").stdout.strip() != main_before
    assert repo.git("show", "task/checkpoint:knowledge/rules.md").stdout == "Threshold: 35%\n"


def test_checkpoint_supports_sha256_repositories(tmp_path: Path) -> None:
    """This fails if checkpoint validation or atomic ref updates assume SHA-1 widths."""
    repository = tmp_path / "sha256-repository"
    init = subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main", str(repository)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert init.returncode == 0, init.stderr
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        }
    )
    for arguments in (
        ["config", "user.name", "Mandu'a Test"],
        ["config", "user.email", "test@mandua.invalid"],
        ["commit", "--allow-empty", "-m", "Create an empty initial commit"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            env=environment,
        )
    (repository / "knowledge").mkdir()
    (repository / "knowledge" / "rules.md").write_text("Threshold: 35%\n", encoding="utf-8")

    result = MemoryService.open(repository).checkpoint(_request("knowledge/rules.md"), apply=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert result.applied is True
    assert len(commit) == 64
    assert result.changes[0].after_oid == commit
    assert len(result.evidence[0].oid or "") == 64


def test_checkpoint_reconciles_an_interrupted_followup_read_after_anomalous_update_output(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real update plus an interrupted anomaly read must not strand HEAD ahead of index."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    failure = _InjectedCheckpointControl("interrupted anomalous update follow-up read")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    completed_updates = []
    anomaly_returned = False
    followup_interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)

    def anomalous_update_then_interrupt_read(arguments, **kwargs):
        nonlocal anomaly_returned, followup_interrupted
        output = original(arguments, **kwargs)
        if (
            arguments[:3] == ["update-ref", "--no-deref", "refs/heads/main"]
            and not anomaly_returned
        ):
            assert output.returncode == 0 and not output.stdout and not output.stderr
            anomaly_returned = True
            return GitOutput(stdout="unexpected update output\n", stderr="", returncode=0)
        if (
            anomaly_returned
            and not followup_interrupted
            and arguments[:4] == ["rev-parse", "--verify", "--end-of-options", "refs/heads/main"]
        ):
            followup_interrupted = True
            assert output.stdout.strip() == completed_updates[0].arguments[3]
            raise failure
        return output

    monkeypatch.setattr(writer._runner, "run_text", anomalous_update_then_interrupt_read)

    with (
        _observe_git_processes(observe_process),
        pytest.raises(_InjectedCheckpointControl) as caught,
    ):
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    assert caught.value is failure
    assert anomaly_returned is True
    assert followup_interrupted is True
    assert len(completed_updates) == 2
    requested_oid = completed_updates[0].arguments[3]
    assert completed_updates[1].arguments[3:5] == (before, requested_oid)
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert (
        repo.git("write-tree").stdout.strip() == repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    )


def test_checkpoint_does_not_roll_back_after_the_exact_next_index_was_installed(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption after the real index replace must preserve the consistent new state."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    failure = _InjectedCheckpointControl("interrupted after exact next-index installation")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original_install = writer._install_locked_index
    completed_updates = []
    installed = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)

    def install_then_interrupt(locks, contents: bytes) -> None:
        nonlocal installed
        original_install(locks, contents)
        installed = True
        assert index_path.read_bytes() == contents
        raise failure

    monkeypatch.setattr(writer, "_install_locked_index", install_then_interrupt)
    observed: BaseException | None = None
    with _observe_git_processes(observe_process):
        try:
            writer.checkpoint(_request("knowledge/rules.md"), apply=True)
        except BaseException as error:  # noqa: BLE001 - identity is the contract under test
            observed = error

    assert installed is True
    assert len(completed_updates) == 1
    requested_oid = completed_updates[0].arguments[3]
    assert requested_oid != before
    assert repo.git("rev-parse", "HEAD").stdout.strip() == requested_oid
    assert (
        repo.git("write-tree").stdout.strip()
        == repo.git("rev-parse", f"{requested_oid}^{{tree}}").stdout.strip()
    )
    assert observed is failure


@pytest.mark.parametrize("returncode", (0, 1))
@pytest.mark.parametrize("update_applies", (True, False))
def test_checkpoint_anomalous_update_preserves_concurrent_index_and_ref(
    repo, monkeypatch: pytest.MonkeyPatch, returncode: int, update_applies: bool
) -> None:
    """An anomalous result cannot authorize rollback over another writer's index."""
    index_path = repo.path / ".git" / "index"
    repo.write("knowledge/third.md", "Concurrent index state\n")
    repo.git("add", "knowledge/third.md")
    third_index = index_path.read_bytes()
    repo.git("reset", "--mixed", "HEAD")
    (repo.path / "knowledge/third.md").unlink()
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    writer = CheckpointWriter(repo.path, QueryLimits())
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    original = writer._runner.run_text
    requested = []

    def anomalous_update(arguments, **kwargs):
        if arguments[:3] == ["update-ref", "--no-deref", "refs/heads/main"] and not requested:
            if update_applies:
                original(arguments, **kwargs)
            requested.append(arguments[3])
            index_path.write_bytes(third_index)
            return GitOutput(stdout="unexpected output", stderr="", returncode=returncode)
        return original(arguments, **kwargs)

    monkeypatch.setattr(writer._runner, "run_text", anomalous_update)
    with pytest.raises(ManduaError) as caught:
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)
    assert requested
    assert repo.git("rev-parse", "HEAD").stdout.strip() == (
        requested[0] if update_applies else before
    )
    assert index_path.read_bytes() == third_index
    assert caught.value.recovery


def test_checkpoint_reports_uncertainty_for_a_third_index_after_installation(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrently replaced installed index must not trigger a destructive ref rollback."""
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    repo.write("knowledge/third.md", "Concurrent index state\n")
    repo.git("add", "knowledge/third.md")
    third_index = index_path.read_bytes()
    third_tree = repo.git("write-tree").stdout.strip()
    repo.git("reset", "--mixed", "HEAD")
    (repo.path / "knowledge" / "third.md").unlink()
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    failure = _InjectedCheckpointControl("interrupted after concurrent index replacement")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original_install = writer._install_locked_index
    completed_updates = []
    replaced = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)

    def install_replace_then_interrupt(locks, contents: bytes) -> None:
        nonlocal replaced
        original_install(locks, contents)
        index_path.write_bytes(third_index)
        replaced = True
        raise failure

    monkeypatch.setattr(writer, "_install_locked_index", install_replace_then_interrupt)
    observed: BaseException | None = None
    with _observe_git_processes(observe_process):
        try:
            writer.checkpoint(_request("knowledge/rules.md"), apply=True)
        except BaseException as error:  # noqa: BLE001 - uncertainty chaining is the contract
            observed = error

    assert replaced is True
    assert len(completed_updates) == 1
    requested_oid = completed_updates[0].arguments[3]
    assert requested_oid != before
    assert repo.git("rev-parse", "HEAD").stdout.strip() == requested_oid
    assert repo.git("write-tree").stdout.strip() == third_tree
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__ is failure


@pytest.mark.parametrize("repeat_interruption", (False, True))
def test_checkpoint_reconciles_a_cleanup_exit_interruption_after_consistent_install(
    repo, monkeypatch: pytest.MonkeyPatch, repeat_interruption: bool
) -> None:
    """The transaction context exit is still part of the post-mutation proof boundary."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    failure = _InjectedCheckpointControl("interrupted after transaction cleanup exit")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original_context = writer._locked_repository_state
    completed_updates = []
    post_interruption_events = []
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)
        if interrupted:
            post_interruption_events.append(event)

    @contextmanager
    def transaction_then_interrupt():
        nonlocal interrupted
        with original_context() as locks:
            yield locks
        if interrupted and not repeat_interruption:
            return
        interrupted = True
        raise failure

    monkeypatch.setattr(writer, "_locked_repository_state", transaction_then_interrupt)

    with (
        _observe_git_processes(observe_process),
        pytest.raises(ManduaError if repeat_interruption else _InjectedCheckpointControl) as caught,
    ):
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)

    if repeat_interruption:
        assert caught.value.__cause__ is failure
        assert "uncertain" in str(caught.value)
    else:
        assert caught.value is failure
    assert interrupted is True
    assert len(completed_updates) == 1
    requested_oid = completed_updates[0].arguments[3]
    assert post_interruption_events
    assert any(event.arguments[:1] == ("rev-parse",) for event in post_interruption_events)
    assert repo.git("rev-parse", "HEAD").stdout.strip() == requested_oid
    assert (
        repo.git("write-tree").stdout.strip()
        == repo.git("rev-parse", f"{requested_oid}^{{tree}}").stdout.strip()
    )


@pytest.mark.parametrize(
    "failure_type",
    (_InjectedCheckpointControl, KeyboardInterrupt, SystemExit),
    ids=("custom-base", "keyboard-interrupt", "system-exit"),
)
def test_checkpoint_preserves_direct_observer_control_after_real_update_and_safe_rollback(
    repo, failure_type
) -> None:
    """Completion observers must not convert interpreter control flow into Git failure."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    failure = failure_type("direct checkpoint completion interruption")
    completed_updates = []
    fired = False

    def interrupt_completed_update(event) -> None:
        nonlocal fired
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", "refs/heads/main")
        ):
            completed_updates.append(event)
            if not fired:
                fired = True
                raise failure

    observed: BaseException | None = None
    with _observe_git_processes(interrupt_completed_update):
        try:
            CheckpointWriter(repo.path, QueryLimits()).checkpoint(
                _request("knowledge/rules.md"), apply=True
            )
        except BaseException as error:  # noqa: BLE001 - direct identity is the contract
            observed = error

    assert fired is True
    assert len(completed_updates) == 2
    requested_oid = completed_updates[0].arguments[3]
    assert completed_updates[1].arguments[3:5] == (before, requested_oid)
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert observed is failure


@pytest.mark.parametrize(
    "root_name",
    ("worktree", "git-directory", "common-directory", "object-directory"),
)
def test_checkpoint_emergency_reconciliation_rejects_a_replaced_physical_authority_root(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_name: str
) -> None:
    """A path-equivalent replacement must not authorize rollback in another physical root."""
    linked = (tmp_path / f"checkpoint-authority-{root_name}").resolve()
    branch = f"authority/checkpoint-{root_name}"
    repo.git("worktree", "add", "-b", branch, str(linked), "HEAD")
    (linked / "knowledge").mkdir()
    (linked / "knowledge" / "rules.md").write_text("Threshold: 35%\n", encoding="utf-8")
    roots = _checkpoint_authority_roots(linked)
    target_root = roots[root_name]
    index_path = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    )
    index_before = index_path.read_bytes()
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    failure = _InjectedCheckpointControl(f"replaced checkpoint {root_name}")
    writer = CheckpointWriter(linked, QueryLimits())
    original = writer._runner.run_text
    completed_updates = []
    displaced: Path | None = None
    requested_oid: str | None = None

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", f"refs/heads/{branch}")
        ):
            completed_updates.append(event)

    def mutate_replace_then_interrupt(arguments, **kwargs):
        nonlocal displaced, requested_oid
        output = original(arguments, **kwargs)
        if (
            arguments[:3] == ["update-ref", "--no-deref", f"refs/heads/{branch}"]
            and displaced is None
        ):
            assert output.returncode == 0 and not output.stdout and not output.stderr
            requested_oid = arguments[3]
            displaced = _replace_checkpoint_authority_root(target_root)
            raise failure
        return output

    monkeypatch.setattr(writer._runner, "run_text", mutate_replace_then_interrupt)
    observed: BaseException | None = None
    try:
        with _observe_git_processes(observe_process):
            try:
                writer.checkpoint(_request("knowledge/rules.md"), apply=True)
            except BaseException as error:  # noqa: BLE001 - uncertainty is asserted below
                observed = error
    finally:
        if displaced is not None:
            _restore_checkpoint_authority_root(target_root, displaced)
        for name in ("HEAD.lock", "index.lock"):
            (roots["git-directory"] / name).unlink(missing_ok=True)
        for temporary in roots["git-directory"].glob(".mandua-index-*.tmp"):
            temporary.unlink(missing_ok=True)

    assert requested_oid is not None
    assert len(completed_updates) == 1
    assert requested_oid != before
    assert repo.git("rev-parse", f"refs/heads/{branch}").stdout.strip() == requested_oid
    assert index_path.read_bytes() == index_before
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__ is failure


@pytest.mark.parametrize("proof_boundary", ("ref", "head"))
def test_round3_checkpoint_rejection_is_not_classified_before_old_ref_and_head_proof(
    repo, monkeypatch: pytest.MonkeyPatch, proof_boundary: str
) -> None:
    """A synthetic rejection is provisional until both old-ref and old-HEAD proofs finish."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    branch_ref = "refs/heads/main"
    failure = _InjectedCheckpointControl(
        f"interrupted during rejected update {proof_boundary} proof"
    )
    writer = CheckpointWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    anomalous_update_returned = False
    followup_ref_completed = False
    proof_interrupted = False
    interruption_started = False
    emergency_reads = []
    requested_oid: str | None = None

    def observe_process(event) -> None:
        if (
            interruption_started
            and event.phase == "completed"
            and event.arguments[:1] in {("rev-parse",), ("symbolic-ref",)}
        ):
            emergency_reads.append(event)

    def update_then_return_rejection_then_interrupt_proof(arguments, **kwargs):
        nonlocal anomalous_update_returned, followup_ref_completed
        nonlocal interruption_started, proof_interrupted, requested_oid
        output = original(arguments, **kwargs)
        if (
            arguments[:3] == ["update-ref", "--no-deref", branch_ref]
            and not anomalous_update_returned
        ):
            assert output.returncode == 0 and not output.stdout and not output.stderr
            requested_oid = arguments[3]
            if proof_boundary == "head":
                restored = repo.git("update-ref", branch_ref, before, requested_oid, check=False)
                assert restored.returncode == 0
            anomalous_update_returned = True
            return GitOutput(stdout="", stderr="synthetic rejection", returncode=1)
        if (
            anomalous_update_returned
            and not followup_ref_completed
            and arguments[:4] == ["rev-parse", "--verify", "--end-of-options", branch_ref]
        ):
            followup_ref_completed = True
            if proof_boundary == "ref":
                proof_interrupted = True
                interruption_started = True
                raise failure
        if (
            proof_boundary == "head"
            and followup_ref_completed
            and not proof_interrupted
            and arguments[:3] == ["symbolic-ref", "--quiet", "HEAD"]
        ):
            proof_interrupted = True
            interruption_started = True
            raise failure
        return output

    monkeypatch.setattr(
        writer._runner,
        "run_text",
        update_then_return_rejection_then_interrupt_proof,
    )
    observed: BaseException | None = None
    with _observe_git_processes(observe_process):
        try:
            writer.checkpoint(_request("knowledge/rules.md"), apply=True)
        except BaseException as error:  # noqa: BLE001 - exact control identity is asserted
            observed = error

    assert anomalous_update_returned is True
    assert followup_ref_completed is True
    assert proof_interrupted is True
    assert requested_oid is not None and requested_oid != before
    assert emergency_reads, "the provisional rejection must enter emergency classification"
    assert observed is failure
    assert repo.git("rev-parse", branch_ref).stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert repo.git("ls-files", "--error-unmatch", "knowledge/rules.md", check=False).returncode


@pytest.mark.parametrize(
    "cleanup_failure",
    (
        "head-close",
        "temp-close",
        "index-close",
        "head-unlink",
        "temp-unlink",
        "index-unlink",
    ),
)
@pytest.mark.parametrize("repeat_cleanup_failure", (False, True))
def test_round3_checkpoint_body_control_and_cleanup_failure_are_both_disclosed(
    repo, monkeypatch: pytest.MonkeyPatch, cleanup_failure: str, repeat_cleanup_failure: bool
) -> None:
    """Post-ref control stays primary, but every failed lock cleanup remains actionable."""
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    index_path = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    )
    index_before = index_path.read_bytes()
    git_dir = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    failure = _InjectedCheckpointControl(f"post-ref body control plus {cleanup_failure}")
    writer = CheckpointWriter(repo.path, QueryLimits())
    original_stage = writer._stage_locked_index
    original_head_lock = writer._acquire_head_lock
    original_post_ref = writer._require_post_ref_state
    original_close = checkpoint_module.os.close
    original_unlink = writer._unlink_owned_lock
    fd_labels: dict[int, str] = {}
    close_attempts: list[str] = []
    unlink_attempts: list[str] = []
    temp_name: str | None = None
    body_interrupted = False

    def record_staged_locks(locks, contents: bytes) -> None:
        nonlocal temp_name
        original_stage(locks, contents)
        assert locks.index_fd is not None
        assert locks.index_temp_fd is not None
        assert locks.index_temp_name is not None
        fd_labels[locks.index_fd] = "index"
        fd_labels[locks.index_temp_fd] = "temp"
        temp_name = locks.index_temp_name

    def record_head_lock(locks) -> None:
        original_head_lock(locks)
        assert locks.head_fd is not None
        fd_labels[locks.head_fd] = "head"

    def verify_then_interrupt(prepared, commit_oid: str) -> None:
        nonlocal body_interrupted
        original_post_ref(prepared, commit_oid)
        body_interrupted = True
        raise failure

    def close_then_report_failure(descriptor: int) -> None:
        label = fd_labels.pop(descriptor, None)
        original_close(descriptor)
        if label is None:
            return
        close_attempts.append(label)
        if cleanup_failure == f"{label}-close":
            raise OSError(f"injected {label} close cleanup failure")

    def unlink_then_report_failure(directory_fd: int, name: str) -> bool:
        unlink_attempts.append(name)
        cleaned = original_unlink(directory_fd, name)
        label = "head" if name == "HEAD.lock" else "index" if name == "index.lock" else "temp"
        if cleanup_failure == f"{label}-unlink" and (
            repeat_cleanup_failure or unlink_attempts.count(name) == 1
        ):
            return False
        return cleaned

    monkeypatch.setattr(writer, "_stage_locked_index", record_staged_locks)
    monkeypatch.setattr(writer, "_acquire_head_lock", record_head_lock)
    monkeypatch.setattr(writer, "_require_post_ref_state", verify_then_interrupt)
    monkeypatch.setattr(checkpoint_module.os, "close", close_then_report_failure)
    monkeypatch.setattr(writer, "_unlink_owned_lock", unlink_then_report_failure)
    observed: BaseException | None = None
    try:
        writer.checkpoint(_request("knowledge/rules.md"), apply=True)
    except BaseException as error:  # noqa: BLE001 - exact control identity is asserted
        observed = error

    assert body_interrupted is True
    assert set(close_attempts) == {"head", "temp", "index"}
    assert temp_name is not None
    assert set(unlink_attempts) == {"HEAD.lock", temp_name, "index.lock"}
    if cleanup_failure == "index-unlink" and repeat_cleanup_failure:
        assert isinstance(observed, ManduaError)
        assert observed.__cause__ is failure
        assert "uncertain" in str(observed)
    else:
        assert observed is failure
    disclosure = " ".join(getattr(failure, "__notes__", ())).casefold()
    assert "cleanup" in disclosure
    assert "uncertain" in disclosure or "inspect" in disclosure
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before
    assert index_path.read_bytes() == index_before
    assert not (git_dir / "HEAD.lock").exists()
    assert not (git_dir / "index.lock").exists()
    assert not tuple(git_dir.glob(".mandua-index-*.tmp"))


@pytest.mark.parametrize(
    "root_name",
    ("worktree", "git-directory", "common-directory", "object-directory"),
)
@pytest.mark.parametrize("replacement_boundary", ("before-final-object", "after-capture"))
def test_round3_checkpoint_binds_physical_authority_before_final_mutation(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_name: str,
    replacement_boundary: str,
) -> None:
    """Every final object/admin operation must reject a replaced bound repository root."""
    linked = (tmp_path / f"round3-checkpoint-{replacement_boundary}-{root_name}").resolve()
    branch = f"round3/checkpoint-{replacement_boundary}-{root_name}"
    repo.git("worktree", "add", "-b", branch, str(linked), "HEAD")
    (linked / "knowledge").mkdir(exist_ok=True)
    (linked / "knowledge" / "rules.md").write_text("Threshold: 35%\n", encoding="utf-8")
    roots = _checkpoint_authority_roots(linked)
    target_root = roots[root_name]
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    index_path = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    )
    index_before = index_path.read_bytes()
    failure = _InjectedCheckpointControl(
        f"checkpoint reached replaced {root_name} at {replacement_boundary}"
    )
    writer = CheckpointWriter(linked, QueryLimits())
    original_open = GitRunner.open_repository_authority
    original_commit = writer._commit_tree
    displaced: Path | None = None
    captured_authorities = []
    captured_descriptors: list[int] = []
    post_replacement_mutators = []
    direct_admin_reached = False
    descriptors_closed = False

    def track_authority_open(runner, *args, **kwargs):
        nonlocal displaced
        authority = original_open(runner, *args, **kwargs)
        captured_authorities.append(authority)
        captured_descriptors.extend(authority.file_descriptors)
        if replacement_boundary == "after-capture" and displaced is None:
            displaced = _replace_checkpoint_authority_root(target_root)
        return authority

    def replace_before_final_commit(prepared):
        nonlocal displaced
        if replacement_boundary == "before-final-object" and displaced is None:
            displaced = _replace_checkpoint_authority_root(target_root)
        original_commit(prepared)
        raise failure

    def stop_if_direct_admin_is_reached(_locks, _contents: bytes) -> None:
        nonlocal direct_admin_reached
        direct_admin_reached = True
        raise failure

    def observe_process(event) -> None:
        if (
            displaced is not None
            and event.phase == "considered"
            and event.arguments[:1]
            in {
                ("hash-object",),
                ("read-tree",),
                ("update-index",),
                ("write-tree",),
                ("commit-tree",),
                ("update-ref",),
            }
        ):
            post_replacement_mutators.append(event)

    monkeypatch.setattr(GitRunner, "open_repository_authority", track_authority_open)
    if replacement_boundary == "before-final-object":
        monkeypatch.setattr(writer, "_commit_tree", replace_before_final_commit)
    else:
        monkeypatch.setattr(writer, "_stage_locked_index", stop_if_direct_admin_is_reached)

    observed: BaseException | None = None
    try:
        with _observe_git_processes(observe_process):
            try:
                writer.checkpoint(_request("knowledge/rules.md"), apply=True)
            except BaseException as error:  # noqa: BLE001 - authority/control outcome is asserted
                observed = error
        descriptors_closed = bool(captured_descriptors) and all(
            _descriptor_is_closed(descriptor) for descriptor in captured_descriptors
        )
    finally:
        if displaced is not None:
            _restore_checkpoint_authority_root(target_root, displaced)
        for name in ("HEAD.lock", "index.lock"):
            (roots["git-directory"] / name).unlink(missing_ok=True)
        for temporary in roots["git-directory"].glob(".mandua-index-*.tmp"):
            temporary.unlink(missing_ok=True)

    assert displaced is not None
    assert captured_authorities, "authority must be bound before final checkpoint preparation"
    assert not post_replacement_mutators
    assert direct_admin_reached is False
    assert descriptors_closed is True
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert repo.git("rev-parse", f"refs/heads/{branch}").stdout.strip() == before
    assert index_path.read_bytes() == index_before


def test_round3_checkpoint_emergency_budget_accepts_large_packed_authority_and_long_ref(
    repo, tmp_path: Path
) -> None:
    """Fresh rollback proof must size valid authority metadata and cumulative argv explicitly."""
    packed = _round3_pack_large_authority_metadata(repo)
    branch = _round3_long_ref("round3/checkpoint-emergency")
    branch_ref = f"refs/heads/{branch}"
    assert len(branch_ref) > 512
    assert repo.git("check-ref-format", branch_ref).returncode == 0
    linked = (tmp_path / "round3-checkpoint-emergency").resolve()
    repo.git("worktree", "add", "-b", branch, str(linked), "HEAD")
    (linked / "knowledge").mkdir(exist_ok=True)
    (linked / "knowledge" / "rules.md").write_text("Threshold: 35%\n", encoding="utf-8")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    index_path = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    )
    index_before = index_path.read_bytes()
    limits = QueryLimits(
        max_excerpt_chars=4_096,
        max_input_chars=4_096,
        timeout_seconds=10.0,
    )
    failure = _InjectedCheckpointControl("large-authority checkpoint mutation interruption")
    considered = []
    emergency_considered = []
    completed_updates = []
    interrupted = False

    def interrupt_completed_update(event) -> None:
        nonlocal interrupted
        if event.phase == "considered":
            considered.append(event)
            if interrupted:
                emergency_considered.append(event)
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)
            if not interrupted:
                interrupted = True
                raise failure

    observed: BaseException | None = None
    with _observe_git_processes(interrupt_completed_update):
        try:
            CheckpointWriter(linked, limits).checkpoint(_request("knowledge/rules.md"), apply=True)
        except BaseException as error:  # noqa: BLE001 - exact control identity is asserted
            observed = error

    assert packed.stat().st_size > 512
    assert interrupted is True
    assert len(completed_updates) >= 1
    assert _round3_planned_argv_bytes(considered) > 4_096
    assert len(considered) <= 96
    assert 1 <= len(emergency_considered) <= 8
    assert observed is failure
    assert repo.git("rev-parse", branch_ref).stdout.strip() == before
    assert index_path.read_bytes() == index_before
