"""Acceptance tests for GitRunner against real disposable repositories."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import PurePosixPath

import pytest

import mandua.git_runner as runner_module
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRunner


def test_isolated_commit_does_not_launch_automatic_maintenance(repo, tmp_path, monkeypatch):
    """No maintenance child may outlive a private commit's publication boundary."""
    repo.git("config", "maintenance.auto", "true")
    trace = tmp_path / "git-trace.jsonl"
    runner = GitRunner(repo.path)
    original_environment = runner._environment

    def traced_environment(**kwargs):
        environment = original_environment(**kwargs)
        environment["GIT_TRACE2_EVENT"] = str(trace)
        return environment

    monkeypatch.setattr(runner, "_environment", traced_environment)
    authority = runner.open_repository_authority(None)
    try:
        runner.run(
            ["commit", "--allow-empty", "--no-verify", "-m", "No background maintenance"],
            repository_authority=authority,
        )
    finally:
        authority.cleanup()
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any(event.get("event") == "start" for event in events)
    assert not any(
        event.get("event") == "child_start"
        and any(arg in {"maintenance", "gc"} for arg in event.get("argv", []))
        for event in events
    )


def test_captured_authority_can_write_objects_refs_and_index(repo, tmp_path):
    """The launch safety boundary must preserve successful ordinary writes."""
    repo.write("tracked.txt", "A nonempty index\n")
    repo.commit("Seed successful authority writes")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "launch-success", str(linked), "HEAD")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    try:
        blob = runner.run_text(
            ["hash-object", "-w", "--stdin"],
            input_bytes=b"captured object",
            repository_authority=authority,
        ).stdout.strip()
        assert repo.git("cat-file", "blob", blob).stdout == "captured object"
        head = repo.git("rev-parse", "HEAD").stdout.strip()
        runner.run(
            ["update-ref", "refs/heads/launch-created", head], repository_authority=authority
        )
        assert repo.git("rev-parse", "refs/heads/launch-created").stdout.strip() == head
        runner.run(["read-tree", "--empty"], repository_authority=authority)
        assert runner.run_text(["ls-files"]).stdout == ""
    finally:
        authority.cleanup()


@pytest.mark.parametrize("root_number", (0, 1, 2, 3, 4))
@pytest.mark.parametrize("operation", ("object", "ref", "index", "commit", "merge"))
def test_authority_launch_never_writes_into_replacement_root(
    repo, tmp_path, monkeypatch, root_number, operation
):
    """Replacing a captured root at Popen must not redirect an object write."""
    repo.write("tracked.txt", "A nonempty index\n")
    repo.commit("Seed launch authority tests")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "launch-proof", str(linked), "HEAD")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    source = head
    if operation == "merge":
        repo.write("merged.txt", "Content from the source branch\n")
        source = repo.commit("Seed a merge")
    arguments = {
        "object": ["hash-object", "-w", "--stdin"],
        "ref": ["update-ref", "refs/heads/launch-created", head],
        "index": ["read-tree", "--empty"],
        "commit": ["commit", "--allow-empty", "--no-verify", "-m", "Isolated launch commit"],
        "merge": ["merge", "--no-ff", "--no-commit", "--no-verify", source],
    }[operation]
    root = authority._authority_paths[root_number].path
    displaced = root.with_name(root.name + "-displaced")
    original_popen = runner_module.subprocess.Popen
    replaced = False

    def snapshot(path):
        return {
            str(p.relative_to(path)): p.read_bytes()
            for p in path.rglob("*")
            if p.is_file() and not p.is_symlink()
        }

    before = None

    def replace_then_launch(*args, **kwargs):
        nonlocal replaced, before
        if not replaced and arguments[0] in args[0]:
            root.rename(displaced)
            shutil.copytree(displaced, root, symlinks=True)
            before = snapshot(root)
            replaced = True
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", replace_then_launch)
    try:
        with pytest.raises(ManduaError):
            runner.run(
                arguments,
                input_bytes=b"launch race unique payload" if operation == "object" else b"",
                repository_authority=authority,
            )
        assert replaced
        assert snapshot(root) == before
    finally:
        if replaced:
            shutil.rmtree(root)
            displaced.rename(root)
        authority.cleanup()


@pytest.mark.parametrize("bound_repository", (True, False))
def test_temporary_index_launch_cannot_write_a_replacement_parent(
    repo, monkeypatch, bound_repository
):
    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(None) if bound_repository else None
    try:
        with runner.temporary_index() as index:
            runner.run(["read-tree", "--empty"], index_file=index, repository_authority=authority)
            parent = index.path.parent
            displaced = parent.with_name(parent.name + "-displaced")
            original_popen = runner_module.subprocess.Popen
            replaced = False
            before = index.path.read_bytes()

            def replace_then_launch(*args, **kwargs):
                nonlocal replaced
                if not replaced and "read-tree" in args[0]:
                    parent.rename(displaced)
                    shutil.copytree(displaced, parent)
                    replaced = True
                return original_popen(*args, **kwargs)

            repo.write("tracked.txt", "Seed a different index\n")
            head = repo.commit("Seed index launch")
            monkeypatch.setattr(runner_module.subprocess, "Popen", replace_then_launch)
            try:
                with pytest.raises(ManduaError):
                    runner.run(
                        ["read-tree", head], index_file=index, repository_authority=authority
                    )
                assert replaced
                assert index.path.read_bytes() == before
            finally:
                monkeypatch.setattr(runner_module.subprocess, "Popen", original_popen)
                if replaced:
                    shutil.rmtree(parent)
                    displaced.rename(parent)
    finally:
        if authority is not None:
            authority.cleanup()


def test_isolated_merge_preserves_worktree_when_publication_index_is_locked(
    repo, tmp_path, monkeypatch
):
    repo.write("tracked.txt", "Original\n")
    repo.commit("Seed publication locking")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "publication-lock", str(linked), "HEAD")
    repo.write("merged.txt", "Must not leak before index ownership\n")
    source = repo.commit("Seed merge side effect")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    index = authority.git_directory / "index"
    before = index.read_bytes()
    lock = authority.git_directory / "index.lock"
    original_popen = runner_module.subprocess.Popen

    def lock_then_launch(*args, **kwargs):
        if "merge" in args[0]:
            lock.write_bytes(b"another writer owns this lock")
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", lock_then_launch)
    try:
        with pytest.raises(ManduaError):
            runner.run(
                ["merge", "--no-ff", "--no-commit", "--no-verify", source],
                repository_authority=authority,
            )
        assert not (linked / "merged.txt").exists()
        assert index.read_bytes() == before
        assert lock.read_bytes() == b"another writer owns this lock"
        assert not (authority.git_directory / "MERGE_HEAD").exists()
    finally:
        lock.unlink(missing_ok=True)
        authority.cleanup()


def test_isolated_merge_restores_files_if_index_publication_fails(repo, tmp_path, monkeypatch):
    import mandua.isolated_objects as isolation

    repo.write("tracked.txt", "Original\n")
    repo.commit("Seed interrupted publication")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "publication-failure", str(linked), "HEAD")
    repo.write("merged.txt", "Must be restored if index publication fails\n")
    source = repo.commit("Seed interrupted merge")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    index = authority.git_directory / "index"
    before = index.read_bytes()
    original_replace = isolation.os.replace
    injected = False

    def interrupt_index_publication(source_name, destination_name, **kwargs):
        nonlocal injected
        if source_name == "index.lock" and not injected:
            injected = True
            raise OSError("injected index publication failure")
        return original_replace(source_name, destination_name, **kwargs)

    monkeypatch.setattr(isolation.os, "replace", interrupt_index_publication)
    try:
        with pytest.raises(ManduaError):
            runner.run(
                ["merge", "--no-ff", "--no-commit", "--no-verify", source],
                repository_authority=authority,
            )
        assert injected
        assert not (linked / "merged.txt").exists()
        assert index.read_bytes() == before
        assert not (authority.git_directory / "MERGE_HEAD").exists()
    finally:
        authority.cleanup()


@pytest.mark.parametrize("replacement", ("file", "directory"))
def test_merge_publication_preserves_concurrent_worktree_replacement(
    repo, tmp_path, monkeypatch, replacement
):
    import mandua.isolated_objects as isolation

    repo.write("knowledge/tracked.txt", "Original\n")
    repo.commit("Seed concurrent publication")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "concurrent-publication", str(linked), "HEAD")
    repo.write("knowledge/tracked.txt", "Incoming change\n")
    source = repo.commit("Change tracked content")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    original_replace = isolation.ObjectExecution._replace_entry
    injected = False

    def concurrent_replace(self, root, path, value, *args, **kwargs):
        nonlocal injected
        if root == authority._authority_paths[0].descriptor and not injected:
            injected = True
            if replacement == "directory":
                (linked / "knowledge").rename(linked / "knowledge-captured")
                (linked / "knowledge").mkdir()
            (linked / "knowledge/tracked.txt").write_text("Concurrent change must survive\n")
        return original_replace(self, root, path, value, *args, **kwargs)

    monkeypatch.setattr(isolation.ObjectExecution, "_replace_entry", concurrent_replace)
    try:
        with pytest.raises(ManduaError):
            runner.run(
                ["merge", "--no-ff", "--no-commit", "--no-verify", source],
                repository_authority=authority,
            )
        assert injected
        assert (linked / "knowledge/tracked.txt").read_text() == "Concurrent change must survive\n"
        assert not (authority.git_directory / "MERGE_HEAD").exists()
    finally:
        authority.cleanup()


def test_merge_preserves_original_when_detach_succeeds_then_raises(repo, tmp_path, monkeypatch):
    import mandua.isolated_objects as isolation

    repo.write("tracked.txt", "Original must survive\n")
    repo.commit("Seed detach interruption")
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "detach-interruption", str(linked), "HEAD")
    repo.write("tracked.txt", "Incoming change\n")
    source = repo.commit("Change tracked content")
    runner = GitRunner(linked)
    authority = runner.open_repository_authority(None)
    original_replace = isolation.os.replace
    injected = False

    def detach_then_raise(source_name, destination_name, **kwargs):
        nonlocal injected
        result = original_replace(source_name, destination_name, **kwargs)
        if (
            source_name == "tracked.txt"
            and str(destination_name).startswith(".mandua-save-")
            and not injected
        ):
            injected = True
            raise OSError("injected interruption after successful detach")
        return result

    monkeypatch.setattr(isolation.os, "replace", detach_then_raise)
    try:
        with pytest.raises(ManduaError):
            runner.run(
                ["merge", "--no-ff", "--no-commit", "--no-verify", source],
                repository_authority=authority,
            )
        assert injected
        assert (linked / "tracked.txt").read_text() == "Original must survive\n"
    finally:
        authority.cleanup()


def test_ref_publication_never_appends_to_a_replacement_log_root(repo, monkeypatch):
    from contextlib import contextmanager

    import mandua.isolated_objects as isolation

    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(None)
    logs = authority.layout.common_directory / "logs"
    displaced = logs.with_name("captured-logs")
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    original_parent = isolation.ObjectExecution._parent
    replaced = False
    before = None

    @contextmanager
    def replace_logs(self, root, path, *, create=False):
        nonlocal replaced, before
        if create and root != self.root and path.endswith("refs/heads/log-proof") and not replaced:
            logs.rename(displaced)
            shutil.copytree(displaced, logs)
            before = {
                str(p.relative_to(logs)): p.read_bytes() for p in logs.rglob("*") if p.is_file()
            }
            replaced = True
        with original_parent(self, root, path, create=create) as pair:
            yield pair

    monkeypatch.setattr(isolation.ObjectExecution, "_parent", replace_logs)
    try:
        with pytest.raises(ManduaError):
            runner.run(["update-ref", "refs/heads/log-proof", head], repository_authority=authority)
        assert replaced
        assert {
            str(p.relative_to(logs)): p.read_bytes() for p in logs.rglob("*") if p.is_file()
        } == before
    finally:
        if replaced:
            shutil.rmtree(logs)
            displaced.rename(logs)
        authority.cleanup()


def test_show_blob_reads_a_committed_file_from_a_real_repository(repo) -> None:
    """This fails if object-and-path blob reads do not work against a real repository."""
    repo.write("knowledge/rule.txt", "Water only when the soil is dry.\n")
    commit = repo.commit("Record a watering rule")

    output = GitRunner(repo.path).show_blob(commit, PurePosixPath("knowledge/rule.txt"))

    assert output.stdout == b"Water only when the soil is dry.\n"


def test_capabilities_are_detected_from_the_installed_git(repo) -> None:
    """This fails if capability detection guesses a Git version instead of probing it."""
    capabilities = GitRunner(repo.path).capabilities

    assert capabilities.version
    assert isinstance(capabilities.merge_tree_write_tree, bool)


def test_arguments_cannot_override_the_controlled_hooks_path(repo, tmp_path) -> None:
    """This fails if untrusted global Git options can re-enable repository hooks."""
    marker = tmp_path / "untrusted-hook-ran"
    hooks = tmp_path / "untrusted-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(
            ["-c", f"core.hooksPath={hooks}", "commit", "--allow-empty", "-m", "Attempt a hook"],
            check=False,
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert not marker.exists()


def test_diff_does_not_run_a_repository_configured_external_helper(repo, tmp_path) -> None:
    """This fails if a read-only diff executes local diff.external configuration."""
    marker = tmp_path / "external-diff-ran"
    helper = tmp_path / "external-diff"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    repo.git("config", "diff.external", str(helper))
    repo.write("tracked.txt", "before\n")
    repo.commit("Add tracked content")
    repo.write("tracked.txt", "after\n")

    GitRunner(repo.path).run(["diff"])

    assert not marker.exists()


def test_textconv_cannot_be_requested_for_a_repository_blob(repo, tmp_path) -> None:
    """This fails if a caller can re-enable a repository text-conversion driver."""
    marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv"
    helper.write_text(f'#!/bin/sh\ntouch {marker}\ncat "$1"\n', encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    repo.git("config", "diff.malicious.textconv", str(helper))
    repo.write(".gitattributes", "*.secret diff=malicious\n")
    repo.write("record.secret", "secret content\n")
    commit = repo.commit("Add an attributed secret")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["show", "--textconv", f"{commit}:record.secret"])

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert not marker.exists()


def test_cat_file_cannot_enable_a_repository_text_conversion_helper(repo, tmp_path) -> None:
    """This fails if a non-diff command can invoke a local textconv helper."""
    marker = tmp_path / "cat-file-textconv-ran"
    helper = tmp_path / "cat-file-textconv"
    helper.write_text(f'#!/bin/sh\ntouch {marker}\ncat "$1"\n', encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    repo.git("config", "diff.malicious.textconv", str(helper))
    repo.write(".gitattributes", "*.secret diff=malicious\n")
    repo.write("record.secret", "secret content\n")
    commit = repo.commit("Add an attributed secret")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["cat-file", "--textconv", f"{commit}:record.secret"])

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert not marker.exists()


def test_grep_cannot_enable_a_repository_text_conversion_helper(repo, tmp_path) -> None:
    """This fails if a second non-diff command can invoke a local textconv helper."""
    marker = tmp_path / "grep-textconv-ran"
    helper = tmp_path / "grep-textconv"
    helper.write_text(f'#!/bin/sh\ntouch {marker}\ncat "$1"\n', encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    repo.git("config", "diff.malicious.textconv", str(helper))
    repo.write(".gitattributes", "*.secret diff=malicious\n")
    repo.write("record.secret", "secret content\n")
    commit = repo.commit("Add an attributed secret")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["grep", "--textconv", "secret", commit])

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert not marker.exists()


def test_path_named_textconv_after_separator_is_not_an_option(repo) -> None:
    """This fails if a path after Git's separator is interpreted as an unsafe option."""
    repo.write("--textconv", "before\n")
    repo.commit("Add a path that resembles an option")
    repo.write("--textconv", "after\n")

    output = GitRunner(repo.path).run(["diff", "--", "--textconv"])

    assert b"after" in output.stdout


def test_repo_builder_creates_reproducible_main_history_and_clones(repo, tmp_path) -> None:
    """This fails if fixture history is not usable for a real clone-based acceptance flow."""
    repo.write("notes.txt", "A reproducible fixture.\n")
    commit = repo.commit("Add a fixture note")
    clone = repo.clone_to(tmp_path / "clone")

    assert repo.git("branch", "--show-current").stdout.strip() == "main"
    assert clone.git("rev-parse", "HEAD").stdout.strip() == commit


def test_cloned_fixture_commits_without_global_or_system_configuration(
    repo, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a clone depends on identity or signing settings outside its repository."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable in (
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
    ):
        monkeypatch.delenv(variable, raising=False)
    clone = repo.clone_to(tmp_path / "deterministic-clone")
    clone.write("clone.txt", "A clone can commit deterministically.\n")

    clone.commit("Commit from the clone")

    assert clone.git("config", "--local", "user.name").stdout.strip() == "Mandu'a Test"
    assert clone.git("config", "--local", "user.email").stdout.strip() == "test@mandua.invalid"
    assert clone.git("config", "--local", "commit.gpgSign").stdout.strip() == "false"
