"""Failure boundaries of descriptor-owned Git execution."""

import os
import threading

import pytest

import mandua.isolated_objects as isolation
from mandua.errors import ManduaError
from mandua.git_runner import GitRunner


def test_audit_preserves_launcher_and_rejects_changed_worker():
    from mandua.demo import DemoOperation, _audit_command

    command = isolation.isolated_command(["git", "log", "-S", "private query"], 42)
    audited = _audit_command(tuple(command))
    assert audited[:7] == tuple(command[:7])
    assert audited[-1] == "<redacted-search-data>"
    record = DemoOperation(1, audited, "/private/tmp", None, "completed", 0)
    assert DemoOperation.from_dict(record.to_dict()) == record
    changed = record.to_dict()
    changed["argv"][4] = "print('unrecognized worker')"
    with pytest.raises(ManduaError):
        DemoOperation.from_dict(changed)


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    os.mkfifo(tmp_path / "entry")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    errors = []
    started = threading.Event()

    def read_entry():
        started.set()
        try:
            isolation._read(parent, "entry", 1024)
        except Exception as error:  # noqa: BLE001 - forward worker failure to the test
            errors.append(error)

    thread = threading.Thread(target=read_entry, daemon=True)
    try:
        thread.start()
        assert started.wait(timeout=1)
        thread.join(timeout=1)
        blocked = thread.is_alive()
        if blocked:
            # Release a regressed blocking reader so the test never leaks a thread.
            writer = os.open(tmp_path / "entry", os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            thread.join(timeout=1)
        assert not blocked
        assert len(errors) == 1
        assert isinstance(errors[0], ManduaError)
    finally:
        os.close(parent)


def test_lock_cleanup_preserves_interruption_and_attempts_every_lock(repo, monkeypatch):
    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(None)
    original_unlink = isolation.os.unlink
    interruption = KeyboardInterrupt("publication interrupted")
    try:
        with isolation.isolated_objects(
            runner, authority, ["commit", "--allow-empty", "-m", "test"], None
        ) as execution:

            def fail_one_cleanup(name, **kwargs):
                if name == "HEAD.lock":
                    raise OSError("injected lock cleanup failure")
                return original_unlink(name, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(isolation.os, "unlink", fail_one_cleanup)
                with pytest.raises(KeyboardInterrupt) as caught, execution._publication_locks():
                    raise interruption
            assert caught.value is interruption
            assert any("lock cleanup" in note for note in interruption.__notes__)
            assert not (authority.git_directory / "index.lock").exists()
            assert not (authority.layout.common_directory / "packed-refs.lock").exists()
            assert not (authority.layout.common_directory / "refs/heads/main.lock").exists()
            (authority.git_directory / "HEAD.lock").unlink()
    finally:
        authority.cleanup()
