"""Real-Git regressions from the independent candidate review."""

import os
from pathlib import Path, PurePosixPath

import pytest

import mandua.isolated_objects as isolation
from mandua.errors import ManduaError
from mandua.git_runner import GitRunner
from mandua.models import CheckpointRequest, CommitMetadata, QueryLimits
from mandua.writes.checkpoint import CheckpointWriter


def test_checkpoint_recovery_excludes_concurrent_git_add(repo, monkeypatch):
    repo.write("knowledge/rules.md", "original\n")
    old = repo.commit("seed")
    repo.write("knowledge/rules.md", "checkpoint\n")
    old_index = (repo.path / ".git/index").read_bytes()
    writer = CheckpointWriter(repo.path, QueryLimits(timeout_seconds=30))
    update = writer._update_ref
    rollback = writer._rollback_ref
    interruption = KeyboardInterrupt("after ref publication")
    attempts = []

    def interrupt_after_update(prepared, oid):
        update(prepared, oid)
        raise interruption

    def concurrent_add(*args, **kwargs):
        repo.write("third.md", "concurrent data\n")
        attempts.append(repo.git("add", "third.md", check=False))
        return rollback(*args, **kwargs)

    monkeypatch.setattr(writer, "_update_ref", interrupt_after_update)
    monkeypatch.setattr(writer, "_rollback_ref", concurrent_add)
    request = CheckpointRequest(
        subject="Record threshold",
        metadata=CommitMetadata(
            memory_type="decision",
            scope="irrigation",
            task_id="TASK-IRR-7",
            decision_id="DEC-IRR-1",
            agent_id="gardener",
            reason="Record a test decision.",
        ),
        paths=(PurePosixPath("knowledge/rules.md"),),
    )
    with pytest.raises(BaseException) as caught:
        writer.checkpoint(request, apply=True)
    assert attempts and all(result.returncode != 0 for result in attempts)
    assert all("index.lock" in result.stderr for result in attempts)
    assert caught.value is interruption
    assert repo.git("rev-parse", "HEAD").stdout.strip() == old
    assert (repo.path / ".git/index").read_bytes() == old_index
    assert not (repo.path / ".git/index.lock").exists()
    assert (repo.path / "third.md").read_text() == "concurrent data\n"


def test_publication_preserves_late_write_through_original_descriptor(repo, tmp_path, monkeypatch):
    repo.write("tracked.txt", "original\n")
    repo.commit("seed")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "target", str(linked), "HEAD")
    repo.write("tracked.txt", "incoming\n")
    source = repo.commit("incoming")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    descriptor = os.open(linked / "tracked.txt", os.O_WRONLY)
    link = os.link
    injected = []

    def late_write(source_name, destination_name, **kwargs):
        if (
            str(source_name).startswith(".mandua-publish-")
            and destination_name == "tracked.txt"
            and not injected
        ):
            os.write(descriptor, b"concurrent late edit\n")
            os.fsync(descriptor)
            injected.append(True)
        return link(source_name, destination_name, **kwargs)

    try:
        monkeypatch.setattr(isolation.os, "link", late_write)
        with pytest.raises(ManduaError):
            runner.run(
                ["merge", "--no-ff", "--no-commit", "--no-verify", source],
                repository_authority=authority,
            )
        assert injected
        assert os.fstat(descriptor).st_nlink > 0
        assert any(
            path.read_bytes() == b"concurrent late edit\n" for path in linked.glob(".mandua-save-*")
        )
    finally:
        os.close(descriptor)
        authority.cleanup()


def test_outer_cleanup_preserves_interruption_and_removes_private_execution(repo, monkeypatch):
    repo.write("nested/tracked.txt", "tracked\n")
    repo.commit("seed")
    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(None)
    close = os.close
    interruption = KeyboardInterrupt("original interruption")
    state = {}

    def broken_close(descriptor):
        if descriptor == state.get("descriptor") and not state.get("injected"):
            state["injected"] = True
            raise OSError("injected directory close failure")
        return close(descriptor)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(isolation.os, "close", broken_close)
            with (
                pytest.raises(BaseException) as caught,
                isolation.isolated_objects(
                    runner, authority, ["commit", "--allow-empty", "-m", "test"], None
                ) as execution,
            ):
                state["descriptor"] = execution.worktree_directories["nested"]
                private = Path(execution.directory.name)
                raise interruption
        assert caught.value is interruption
        assert state["injected"]
        assert not private.exists()
        assert any("cleanup" in note for note in interruption.__notes__)
    finally:
        if state.get("injected"):
            close(state["descriptor"])
        execution.worktree_directories.pop("nested", None)
        if private.exists():
            execution.cleanup()
        authority.cleanup()
