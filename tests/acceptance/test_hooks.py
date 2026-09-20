"""Real-Git acceptance tests for versioned shared policy hooks."""

from __future__ import annotations

import importlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers.repo import RepoBuilder

from mandua import cli
from mandua.models import QueryLimits

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VALID_MESSAGE = (
    "Record the shared hook policy\n\n"
    "Memory-Type: implementation\n"
    "Scope: hooks\n"
    "Task-ID: POC-15\n"
    "Agent-ID: codex\n"
)


def _run_hook(
    repository: Path,
    name: str,
    arguments: tuple[str, ...] = (),
    stdin: str = "",
) -> int:
    hooks = importlib.import_module("mandua.hooks")
    return hooks.run_hook(repository, name, arguments, stdin)


def _git_directory(repo) -> Path:
    return Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())


def _write_message(repo, contents: str = _VALID_MESSAGE, *, name: str = "COMMIT_EDITMSG") -> Path:
    path = _git_directory(repo) / name
    path.write_text(contents, encoding="utf-8")
    return path


def _object_width(repo) -> int:
    object_format = repo.git("rev-parse", "--show-object-format").stdout.strip()
    return {"sha1": 40, "sha256": 64}[object_format]


def _advance(repo, text: str = "Current rule\n") -> tuple[str, str]:
    previous = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("knowledge/rules.md", text)
    current = repo.commit("Record the current rule")
    return previous, current


def _commit_with_missing_policy_blob(repo, parent: str) -> str:
    missing_blob = "f" * _object_width(repo)
    record = f"100644 blob {missing_blob}\t.mandua.toml\n".encode("ascii")
    tree = repo.git_bytes("mktree", "--missing", input_bytes=record).stdout.decode("ascii").strip()
    return repo.git(
        "commit-tree", tree, "-p", parent, "-m", "Reference unavailable policy"
    ).stdout.strip()


def _create_sha256_repo(path: Path) -> RepoBuilder:
    initialized = subprocess.run(
        [
            "git",
            "init",
            "--object-format=sha256",
            "--initial-branch=main",
            str(path),
        ],
        cwd=path.parent,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert initialized.returncode == 0, (
        "git init --object-format=sha256 failed: "
        f"stdout={initialized.stdout!r}; stderr={initialized.stderr!r}"
    )
    builder = RepoBuilder(path=path.resolve(), _root=path.parent.resolve())
    builder.git("config", "--local", "user.name", "Mandu'a Test")
    builder.git("config", "--local", "user.email", "test@mandua.invalid")
    builder.git("config", "--local", "commit.gpgSign", "false")
    builder.git(
        "commit",
        "--no-gpg-sign",
        "--allow-empty",
        "-m",
        "Create an empty SHA-256 initial commit",
    )
    return builder


def _git_path(repo, name: str) -> Path:
    value = Path(repo.git("rev-parse", "--git-path", name).stdout.strip())
    return value if value.is_absolute() else repo.path / value


def _file_snapshot(path: Path) -> tuple[int, int, int, int, bytes]:
    metadata = path.stat(follow_symlinks=False)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        path.read_bytes(),
    )


def _primary_object_inventory(repo) -> tuple[str, ...]:
    output = repo.git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)").stdout
    return tuple(sorted(output.splitlines()))


def _replace_after_git_directory_discovery(
    monkeypatch,
    repository: Path,
    replacement: Callable[[], None],
) -> None:
    hooks = importlib.import_module("mandua.hooks")
    original = hooks.GitRunner.run_text
    replaced = False

    def run_text(runner, arguments, **kwargs):
        nonlocal replaced
        output = original(runner, arguments, **kwargs)
        if (
            not replaced
            and runner._repository.resolve() == repository.resolve()
            and arguments == ["rev-parse", "--absolute-git-dir"]
        ):
            replaced = True
            replacement()
        return output

    monkeypatch.setattr(hooks.GitRunner, "run_text", run_text)


def test_pre_commit_validates_the_staged_tree_not_unstaged_worktree(repo, capsys) -> None:
    """This fails if pre-commit substitutes worktree bytes for the exact current index."""
    repo.write(".mandua.toml", "[repository]\nmax_file_bytes = 1024\n")
    repo.write("knowledge/rules.md", "Staged safe rule\n")
    repo.git("add", ".mandua.toml", "knowledge/rules.md")
    repo.write(".mandua.toml", "not-valid-toml = [\n")
    repo.write("knowledge/rules.md", "api_key = 'abcdefghijklmnopqrstuvwxyz123456'\n")
    staged_before = repo.git("diff", "--cached", "--binary").stdout
    worktree_before = (repo.path / "knowledge" / "rules.md").read_bytes()

    assert _run_hook(repo.path, "pre-commit") == 0
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""
    assert repo.git("diff", "--cached", "--binary").stdout == staged_before
    assert (repo.path / "knowledge" / "rules.md").read_bytes() == worktree_before


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    (
        ("Staged safe rule\n", 0),
        ("api_key = 'abcdefghijklmnopqrstuvwxyz123456'\n", 1),
    ),
)
def test_pre_commit_preserves_the_raw_index_and_primary_object_database(
    repo, capsys, contents: str, expected_code: int
) -> None:
    """This fails if staged validation writes cache-tree data or primary Git objects."""
    repo.write("knowledge/rules.md", contents)
    repo.git("add", "knowledge/rules.md")
    index = _git_path(repo, "index")
    index_before = _file_snapshot(index)
    objects_before = _primary_object_inventory(repo)

    assert _run_hook(repo.path, "pre-commit") == expected_code
    capsys.readouterr()

    assert _file_snapshot(index) == index_before
    assert _primary_object_inventory(repo) == objects_before


def test_pre_commit_rejects_an_external_only_blob_from_repository_alternates(
    repo, tmp_path, capsys
) -> None:
    """This fails if staged policy can trust an object outside the bound primary ODB."""
    external = repo.clone_to(tmp_path / "external-objects")
    external.write("external-only.md", "External-only staged knowledge\n")
    object_id = external.git("hash-object", "-w", "external-only.md").stdout.strip()
    primary_objects = _git_path(repo, "objects")
    assert not (primary_objects / object_id[:2] / object_id[2:]).exists()
    info = primary_objects / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{_git_path(external, 'objects')}\n", encoding="utf-8")
    repo.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{object_id},knowledge/external-only.md",
    )
    assert repo.git("cat-file", "-t", object_id).stdout == "blob\n"

    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err


def test_pre_commit_rejects_persistent_http_alternate_metadata(repo, capsys) -> None:
    """This fails if hook setup permits a network-capable alternate object authority."""
    info = _git_path(repo, "objects") / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "http-alternates").write_text("https://invalid.example/objects\n", encoding="utf-8")

    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err


def test_pre_commit_rechecks_alternates_after_staged_index_inspection(
    repo, tmp_path, monkeypatch, capsys
) -> None:
    """This fails if persistent alternate metadata can appear after the setup guard."""
    hooks = importlib.import_module("mandua.hooks")
    external = repo.clone_to(tmp_path / "late-external-objects")
    external_objects = _git_path(external, "objects")
    info = _git_path(repo, "objects") / "info"
    original = hooks.GitRunner.run
    installed = False

    def run(runner, arguments, **kwargs):
        nonlocal installed
        output = original(runner, arguments, **kwargs)
        if not installed and arguments and arguments[0] == "ls-files":
            installed = True
            info.mkdir(parents=True, exist_ok=True)
            (info / "alternates").write_text(f"{external_objects}\n", encoding="utf-8")
        return output

    monkeypatch.setattr(hooks.GitRunner, "run", run)

    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err


def test_pre_commit_rejects_intent_to_add_without_mutating_the_index(repo, capsys) -> None:
    """This fails if an intent-to-add record is mistaken for a staged empty blob."""
    repo.write("draft.txt", "Unstaged draft content\n")
    repo.git("add", "-N", "draft.txt")
    index = _git_path(repo, "index")
    index_before = _file_snapshot(index)
    objects_before = _primary_object_inventory(repo)

    assert _run_hook(repo.path, "pre-commit") == 1
    capsys.readouterr()

    assert _file_snapshot(index) == index_before
    assert _primary_object_inventory(repo) == objects_before


def test_pre_commit_rejects_genuine_v4_intent_to_add_without_mutation(repo, capsys) -> None:
    """This fails if v4 path decoding misses Git's preserved intent-to-add flag."""
    repo.write("draft.txt", "Unstaged draft content\n")
    repo.git("add", "-N", "draft.txt")
    repo.git("update-index", "--index-version", "4")
    index = _git_path(repo, "index")
    index_before = _file_snapshot(index)
    raw_index = index_before[4]
    assert raw_index[:4] == b"DIRC"
    assert int.from_bytes(raw_index[4:8], "big") == 4
    debug = repo.git("ls-files", "--debug", "--", "draft.txt").stdout
    assert "flags: 20004000" in debug
    objects_before = _primary_object_inventory(repo)

    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "Intent-to-add index entries are not allowed." in captured.err
    assert "Traceback" not in captured.err
    assert _file_snapshot(index) == index_before
    assert _primary_object_inventory(repo) == objects_before


@pytest.mark.parametrize("mode", ("100644", "160000"))
def test_pre_commit_rejects_missing_objects_outside_knowledge_roots(
    repo, capsys, mode: str
) -> None:
    """This fails if an outside index entry bypasses required object availability checks."""
    missing = "f" * _object_width(repo)
    repo.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{missing},outside/missing-{mode}",
    )

    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("mode", ("100644", "160000"))
def test_pre_commit_rejects_index_mode_and_actual_object_type_mismatches(
    repo, capsys, mode: str
) -> None:
    """This fails if an index mode is trusted without probing the real object type."""
    if mode == "100644":
        object_id = repo.git("rev-parse", "HEAD").stdout.strip()
    else:
        repo.write("outside-blob.txt", "Blob stored under a gitlink mode\n")
        object_id = repo.git("hash-object", "-w", "outside-blob.txt").stdout.strip()
    repo.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{object_id},outside/type-mismatch-{mode}",
    )

    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err


def test_pre_commit_allows_a_present_commit_backed_gitlink_outside_knowledge(repo, capsys) -> None:
    """This preserves real gitlink semantics when the referenced commit is locally available."""
    commit = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},outside/present-module",
    )

    assert _run_hook(repo.path, "pre-commit") == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("failure", ("size", "mode", "path", "invariant", "secret"))
def test_pre_commit_rejects_each_shared_tree_policy_failure(repo, capsys, failure: str) -> None:
    """This fails if a staged policy dimension is omitted from hook validation."""
    if failure == "size":
        repo.write(
            ".mandua.toml",
            "[repository]\nmax_file_bytes = 4\n",
        )
        repo.write("knowledge/rules.md", "12345")
        repo.git("add", ".mandua.toml", "knowledge/rules.md")
    elif failure == "mode":
        outside = repo.path.parent / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (repo.path / "knowledge").mkdir()
        os.symlink(outside, repo.path / "knowledge" / "linked.txt")
        repo.git("add", "knowledge/linked.txt")
    elif failure == "path":
        repo.write(
            ".mandua.toml",
            '[repository]\nknowledge_roots = ["knowledge/../outside"]\n',
        )
        repo.git("add", ".mandua.toml")
    elif failure == "invariant":
        repo.write(
            ".mandua.toml",
            "[[invariants]]\n"
            'kind = "unique-json-field"\n'
            'path = "knowledge/rules.json"\n'
            'field = "id"\n',
        )
        repo.write("knowledge/rules.json", '[{"id":"IRR-1"},{"id":"IRR-1"}]\n')
        repo.git("add", ".mandua.toml", "knowledge/rules.json")
    else:
        repo.write("knowledge/token.txt", "api_key = 'abcdefghijklmnopqrstuvwxyz123456'\n")
        repo.git("add", "knowledge/token.txt")

    staged_before = repo.git("diff", "--cached", "--binary").stdout
    assert _run_hook(repo.path, "pre-commit") == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err
    assert captured.err.isascii()
    assert "Traceback" not in captured.err
    assert repo.git("diff", "--cached", "--binary").stdout == staged_before


def test_commit_msg_accepts_one_bounded_semantic_message(repo, capsys) -> None:
    """This fails if the Git-supplied administrative file cannot use shared metadata policy."""
    message = _write_message(repo)

    assert _run_hook(repo.path, "commit-msg", (str(message),)) == 0
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""
    assert message.read_text(encoding="utf-8") == _VALID_MESSAGE


def test_commit_msg_accepts_a_valid_subject_containing_a_colon(repo, capsys) -> None:
    """This fails if a conventional subject is mistaken for a trailer-only message."""
    message = _write_message(repo, "feat: record the shared policy\n")

    assert _run_hook(repo.path, "commit-msg", (str(message),)) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "contents",
    (
        "x" * 73 + "\n",
        "Record policy\n\nMemory-Type:  implementation\n",
        "Record policy\nMemory-Type: implementation\n",
        "Record policy\n\nArbitrary body text.\n\nMemory-Type: implementation\n",
        "Record policy\n\nAgent-ID: codex\nMemory-Type: implementation\n",
        "Record policy\n\nMemory-Type: implementation\n\n",
    ),
)
def test_commit_msg_rejects_messages_outside_the_existing_subject_trailer_contract(
    repo, capsys, contents: str
) -> None:
    """This fails if commit-msg accepts a subject or trailer shape shared policy rejects."""
    message = _write_message(repo, contents)

    assert _run_hook(repo.path, "commit-msg", (str(message),)) == 1
    captured = capsys.readouterr()
    assert captured.err
    assert "Traceback" not in captured.err


def test_commit_msg_rejects_outside_traversal_symlink_and_nonregular_paths(
    repo, tmp_path, capsys
) -> None:
    """This fails if lexical containment is mistaken for opened administrative authority."""
    git_directory = _git_directory(repo)
    outside = tmp_path / "outside-message"
    outside.write_text(_VALID_MESSAGE, encoding="utf-8")
    nested = git_directory / "nested"
    nested.mkdir()
    inside = _write_message(repo)
    symlink = git_directory / "linked-message"
    os.symlink(outside, symlink)
    directory = git_directory / "message-directory"
    directory.mkdir()
    cases = (
        str(outside),
        str(nested / ".." / inside.name),
        str(symlink),
        str(directory),
    )

    for path in cases:
        assert _run_hook(repo.path, "commit-msg", (path,)) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err
        assert "Traceback" not in captured.err


def test_commit_msg_rejects_an_oversized_administrative_file(repo, capsys) -> None:
    """This fails if message reads are not bounded before metadata validation."""
    message = _write_message(repo, "x" * 1_048_577)

    assert _run_hook(repo.path, "commit-msg", (str(message),)) == 1
    captured = capsys.readouterr()
    assert "byte limit" in captured.err.lower()


def test_commit_msg_accepts_the_actual_linked_worktree_git_directory(
    repo, tmp_path, capsys
) -> None:
    """This fails if linked-worktree administration is confused with the common Git directory."""
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "linked-topic", str(linked))
    linked_git_directory = Path(
        subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
    )
    message = linked_git_directory / "COMMIT_EDITMSG"
    message.write_text(_VALID_MESSAGE, encoding="utf-8")

    assert _run_hook(linked, "commit-msg", (str(message),)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""


def test_commit_msg_rejects_a_whole_git_directory_replaced_after_discovery(
    repo, tmp_path, monkeypatch, capsys
) -> None:
    """This fails if the message is reopened through a replacement normal `.git` path."""
    message = _write_message(repo, "x" * 73 + "\n")
    git_directory = _git_directory(repo)
    displaced = tmp_path / "displaced-git-directory"
    replacement = tmp_path / "replacement-git-directory"
    replacement.mkdir()
    (replacement / message.name).write_text(_VALID_MESSAGE, encoding="utf-8")

    def replace() -> None:
        os.rename(git_directory, displaced)
        os.rename(replacement, git_directory)

    _replace_after_git_directory_discovery(monkeypatch, repo.path, replace)
    try:
        code = _run_hook(repo.path, "commit-msg", (str(message),))
    finally:
        os.rename(git_directory, replacement)
        os.rename(displaced, git_directory)

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err
    assert "Traceback" not in captured.err


def test_commit_msg_rejects_a_linked_worktree_git_target_replaced_after_discovery(
    repo, tmp_path, monkeypatch, capsys
) -> None:
    """This fails if a linked locator can lead to a substituted administrative target."""
    linked = tmp_path / "linked-target-race"
    repo.git("worktree", "add", "-b", "linked-target-race", str(linked))
    git_directory = Path(
        subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
    )
    message = git_directory / "COMMIT_EDITMSG"
    message.write_text("x" * 73 + "\n", encoding="utf-8")
    displaced = tmp_path / "displaced-linked-git-directory"
    replacement = tmp_path / "replacement-linked-git-directory"
    replacement.mkdir()
    (replacement / message.name).write_text(_VALID_MESSAGE, encoding="utf-8")

    def replace() -> None:
        os.rename(git_directory, displaced)
        os.rename(replacement, git_directory)

    _replace_after_git_directory_discovery(monkeypatch, linked, replace)
    try:
        code = _run_hook(linked, "commit-msg", (str(message),))
    finally:
        os.rename(git_directory, replacement)
        os.rename(displaced, git_directory)

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err
    assert "Traceback" not in captured.err


def test_commit_msg_rejects_a_linked_worktree_locator_replaced_after_discovery(
    repo, tmp_path, monkeypatch, capsys
) -> None:
    """This fails if a changed linked-worktree locator is not revalidated through the read."""
    linked = tmp_path / "linked-locator-race"
    repo.git("worktree", "add", "-b", "linked-locator-race", str(linked))
    message = (
        Path(
            subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"],
                cwd=linked,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.strip()
        )
        / "COMMIT_EDITMSG"
    )
    message.write_text(_VALID_MESSAGE, encoding="utf-8")
    locator = linked / ".git"
    locator_before = locator.read_bytes()
    replacement_target = tmp_path / "replacement-locator-target"
    replacement_target.mkdir()

    def replace() -> None:
        locator.write_text(f"gitdir: {replacement_target}\n", encoding="utf-8")

    _replace_after_git_directory_discovery(monkeypatch, linked, replace)
    try:
        code = _run_hook(linked, "commit-msg", (str(message),))
    finally:
        locator.write_bytes(locator_before)

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err
    assert "Traceback" not in captured.err


def test_pre_rebase_rejects_canonical_main(repo, capsys) -> None:
    """This fails if canonical history can enter Git's rewrite path."""
    repo.checkout("main")

    assert _run_hook(repo.path, "pre-rebase", ("HEAD~1",)) == 1
    captured = capsys.readouterr()
    assert "canonical" in captured.err.lower()


def test_pre_rebase_ignores_an_unstaged_canonical_branch_rename(repo, capsys) -> None:
    """This fails if mutable policy can authorize rebasing the committed canonical branch."""
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')

    assert _run_hook(repo.path, "pre-rebase", ("HEAD~1",)) == 1
    captured = capsys.readouterr()
    assert "canonical" in captured.err.lower()


def test_pre_rebase_reads_policy_from_the_explicit_branch_tip(repo, capsys) -> None:
    """This fails if an explicit target inherits policy from the checked-out branch."""
    repo.checkout_new("caller")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    repo.commit("Configure caller policy")

    assert _run_hook(repo.path, "pre-rebase", ("main", "main")) == 1
    captured = capsys.readouterr()
    assert "canonical" in captured.err.lower()


def test_pre_rebase_rejects_a_real_branch_tip_change_during_policy_validation(repo, capsys) -> None:
    """This fails if the second real tip check is removed or accepts stale branch policy."""
    from mandua.git_runner import _observe_git_processes

    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("topic", base)
    repo.checkout_new("replacement", base)
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "topic"\n')
    replacement = repo.commit("Make topic canonical")
    repo.checkout("main")
    moved = False

    def move_tip_after_policy_read(event) -> None:
        nonlocal moved
        if (
            not moved
            and event.phase == "completed"
            and event.arguments == ("ls-tree", "-z", base, "--", ".mandua.toml")
        ):
            repo.git("update-ref", "refs/heads/topic", replacement, base)
            moved = True

    with _observe_git_processes(move_tip_after_policy_read):
        code = _run_hook(repo.path, "pre-rebase", ("main", "topic"))

    captured = capsys.readouterr()
    assert moved
    assert repo.git("rev-parse", "refs/heads/topic").stdout.strip() == replacement
    assert code == 1
    assert captured.out == ""
    assert captured.err == "mandua: The branch to rebase changed during policy validation.\n"
    assert captured.err.isascii()
    assert len(captured.err.encode("utf-8")) <= 512


def test_pre_rebase_fails_closed_when_the_branch_policy_blob_is_unavailable(repo, capsys) -> None:
    """This fails if branch protection substitutes checkout bytes for a missing policy blob."""
    parent = repo.git("rev-parse", "HEAD").stdout.strip()
    commit = _commit_with_missing_policy_blob(repo, parent)
    repo.git("update-ref", "refs/heads/main", commit)

    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 1
    captured = capsys.readouterr()
    assert captured.err == "mandua: The committed repository policy object is unavailable.\n"


def test_pre_rebase_fails_closed_on_corrupt_committed_policy_not_mutable_policy(
    repo, capsys
) -> None:
    """This fails if valid checkout bytes hide corrupt policy at the branch tip."""
    repo.write(".mandua.toml", "not-valid-toml = [\n")
    repo.commit("Record corrupt policy")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')

    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 1
    captured = capsys.readouterr()
    assert captured.err == "mandua: The repository configuration is invalid TOML.\n"


def test_pre_rebase_fails_closed_on_oversized_committed_policy(repo, capsys) -> None:
    """This fails if exact committed policy reads are not bounded before parsing."""
    oversized = "x" * (QueryLimits().max_output_bytes + 1)
    repo.write(".mandua.toml", oversized)
    repo.commit("Record oversized policy")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')

    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 1
    captured = capsys.readouterr()
    assert captured.err == "mandua: The committed repository policy exceeds the byte limit.\n"


def test_pre_rebase_allows_a_side_branch_and_detached_head(repo, capsys) -> None:
    """This fails if protection expands beyond the configured current canonical branch."""
    repo.checkout_new("topic")
    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 0
    assert capsys.readouterr().err == ""

    repo.git("checkout", "--detach")
    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 0
    assert capsys.readouterr().err == ""


def test_pre_rebase_uses_the_configured_canonical_branch(repo, capsys) -> None:
    """This fails if protection hard-codes main instead of consuming repository policy."""
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    repo.commit("Configure canonical history")
    repo.checkout_new("stable")

    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 1
    captured = capsys.readouterr()
    assert "canonical" in captured.err.lower()


def test_real_pre_rebase_wrapper_protects_the_explicit_canonical_target(repo) -> None:
    """This fails if `git rebase other main` checks the caller branch instead of main."""
    repo.checkout_new("other")
    repo.write("knowledge/other.md", "Other history\n")
    repo.commit("Record other history")
    repo.checkout("main")
    repo.write("knowledge/main.md", "Canonical history\n")
    main_before = repo.commit("Record canonical history")
    repo.checkout_new("topic", main_before)
    repo.git("config", "core.hooksPath", str(_PROJECT_ROOT / ".githooks"))

    result = repo.git("rebase", "other", "main", check=False)

    assert result.returncode != 0
    assert repo.git("rev-parse", "refs/heads/main").stdout.strip() == main_before


def test_real_pre_rebase_wrapper_allows_an_explicit_side_branch_from_main(repo) -> None:
    """This fails if `git rebase other topic` is blocked only because HEAD is main."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("other", base)
    repo.write("knowledge/other.md", "Other history\n")
    repo.commit("Record other history")
    repo.checkout_new("topic", base)
    repo.write("knowledge/topic.md", "Topic history\n")
    topic_before = repo.commit("Record topic history")
    repo.checkout("main")
    repo.git("config", "core.hooksPath", str(_PROJECT_ROOT / ".githooks"))

    result = repo.git("rebase", "other", "topic", check=False)

    assert result.returncode == 0, result.stderr
    assert repo.git("rev-parse", "refs/heads/topic").stdout.strip() != topic_before


def test_pre_rebase_rejects_an_invalid_explicit_target_and_allows_root_on_topic(
    repo, capsys
) -> None:
    """This fails if target resolution is skipped or Git's empty root upstream is rejected."""
    repo.checkout_new("topic")

    assert _run_hook(repo.path, "pre-rebase", ("main", "missing-target")) == 1
    assert capsys.readouterr().err
    assert _run_hook(repo.path, "pre-rebase", ("",)) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("config_environment", ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"))
def test_hooks_ignore_hostile_global_and_system_config_includes(
    repo, tmp_path, monkeypatch, capsys, config_environment: str
) -> None:
    """This fails if an included locator/helper config changes hook semantics or runs code."""
    repo.checkout_new("topic")
    attacker = RepoBuilder.create(tmp_path / f"attacker-{config_environment.lower()}")
    marker = tmp_path / f"helper-ran-{config_environment.lower()}"
    helper = tmp_path / f"hostile-helper-{config_environment.lower()}"
    helper.write_text(f'#!/bin/sh\nprintf hostile > "{marker}"\n', encoding="utf-8")
    helper.chmod(0o755)
    included = tmp_path / f"included-{config_environment.lower()}.config"
    included.write_text(
        "[core]\n"
        f'\tworktree = "{attacker.path}"\n'
        f'\tfsmonitor = "{helper}"\n'
        f'\thooksPath = "{helper}"\n'
        "[credential]\n"
        f'\thelper = "!{helper}"\n'
        "[malformed\n",
        encoding="utf-8",
    )
    hostile = tmp_path / f"hostile-{config_environment.lower()}.config"
    hostile.write_text(f'[include]\n\tpath = "{included}"\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setenv(config_environment, str(hostile))

    assert _run_hook(repo.path, "pre-rebase", ("main",)) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not marker.exists()


def test_pre_push_allows_main_creation_only_with_the_exact_zero_width(repo, capsys) -> None:
    """This fails if a malformed creation sentinel bypasses object-format validation."""
    local = repo.git("rev-parse", "HEAD").stdout.strip()
    width = _object_width(repo)
    valid = f"refs/heads/main {local} refs/heads/main {'0' * width}\n"
    wrong = f"refs/heads/main {local} refs/heads/main {'0' * (64 if width == 40 else 40)}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), valid) == 0
    assert capsys.readouterr().err == ""
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), wrong) == 1
    assert capsys.readouterr().err


def test_pre_push_new_canonical_branch_uses_committed_policy_not_checkout_bytes(
    repo, capsys
) -> None:
    """This fails if safe branch creation depends on corrupt mutable policy bytes."""
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    local = repo.commit("Configure stable canonical history")
    repo.write(".mandua.toml", "not-valid-toml = [\n")
    zeros = "0" * _object_width(repo)
    line = f"refs/heads/stable {local} refs/heads/stable {zeros}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 0
    assert capsys.readouterr().err == ""


def test_pre_push_sha256_uses_committed_policy_for_canonical_branch_lifecycle(
    tmp_path, capsys
) -> None:
    """This fails if committed-policy lookup truncates 64-character object IDs."""
    repo = _create_sha256_repo(tmp_path / "sha256-repository")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    created = repo.commit("Configure stable canonical history")
    repo.write("knowledge/rules.md", "SHA-256 canonical history\n")
    advanced = repo.commit("Advance SHA-256 canonical history")
    repo.write(".mandua.toml", "not-valid-toml = [\n")
    zeros = "0" * 64
    creation = f"refs/heads/main {created} refs/heads/stable {zeros}\n"
    fast_forward = f"refs/heads/main {advanced} refs/heads/stable {created}\n"
    deletion = f"(delete) {zeros} refs/heads/stable {advanced}\n"

    assert repo.git("rev-parse", "--show-object-format").stdout == "sha256\n"
    assert len(created) == 64
    assert len(advanced) == 64
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), creation) == 0
    assert capsys.readouterr().err == ""
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), fast_forward) == 0
    assert capsys.readouterr().err == ""
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), deletion) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "delete" in captured.err.lower()
    assert captured.err.isascii()
    assert len(captured.err.encode("utf-8")) <= 512


def test_pre_push_allows_fast_forward_and_rejects_non_fast_forward_main(repo, capsys) -> None:
    """This fails if canonical pushes are not classified through local commit ancestry."""
    previous, current = _advance(repo)
    fast_forward = f"refs/heads/main {current} refs/heads/main {previous}\n"
    non_fast_forward = f"refs/heads/main {previous} refs/heads/main {current}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), fast_forward) == 0
    assert capsys.readouterr().err == ""
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), non_fast_forward) == 1
    captured = capsys.readouterr()
    assert "fast-forward" in captured.err.lower()


def test_pre_push_rejects_main_deletion(repo, capsys) -> None:
    """This fails if an all-zero local object can delete canonical history."""
    remote = repo.git("rev-parse", "HEAD").stdout.strip()
    zeros = "0" * _object_width(repo)
    line = f"(delete) {zeros} refs/heads/main {remote}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
    captured = capsys.readouterr()
    assert "delete" in captured.err.lower()


def test_pre_push_ignores_an_unstaged_policy_rename_when_deleting_canonical(repo, capsys) -> None:
    """This fails if mutable policy can authorize deleting the committed canonical ref."""
    remote = repo.git("rev-parse", "HEAD").stdout.strip()
    zeros = "0" * _object_width(repo)
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    line = f"(delete) {zeros} refs/heads/main {remote}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
    captured = capsys.readouterr()
    assert "delete" in captured.err.lower()


def test_pre_push_ignores_an_unstaged_policy_rename_on_non_fast_forward_canonical(
    repo, capsys
) -> None:
    """This fails if mutable policy can authorize rewriting the committed canonical ref."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("knowledge/main.md", "Canonical history\n")
    remote = repo.commit("Advance canonical history")
    repo.checkout_new("replacement", base)
    repo.write("knowledge/replacement.md", "Replacement history\n")
    local = repo.commit("Create replacement history")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    line = f"refs/heads/replacement {local} refs/heads/main {remote}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
    captured = capsys.readouterr()
    assert "fast-forward" in captured.err.lower()


def test_pre_push_rejects_a_canonical_policy_transition_on_the_existing_ref(repo, capsys) -> None:
    """This fails if one fast-forward can silently stop protecting its canonical ref."""
    remote = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    local = repo.commit("Attempt to rename canonical history")
    line = f"refs/heads/main {local} refs/heads/main {remote}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
    captured = capsys.readouterr()
    assert "canonical branch cannot change" in captured.err.lower()


def test_pre_push_fails_closed_when_either_committed_policy_blob_is_unavailable(
    repo, capsys
) -> None:
    """This fails if an existing update skips the old or new committed policy object."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    missing = _commit_with_missing_policy_blob(repo, base)
    base_tree = repo.git("rev-parse", f"{base}^{{tree}}").stdout.strip()
    descendant = repo.git(
        "commit-tree", base_tree, "-p", missing, "-m", "Restore available policy"
    ).stdout.strip()
    lines = (
        f"refs/heads/main {missing} refs/heads/main {base}\n",
        f"refs/heads/main {descendant} refs/heads/main {missing}\n",
    )

    for line in lines:
        assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
        captured = capsys.readouterr()
        assert captured.err == "mandua: The committed repository policy object is unavailable.\n"


@pytest.mark.parametrize(
    "line",
    (
        "only three fields here\n",
        "one two three four five\n",
        "refs/heads/main not-an-oid refs/heads/main also-not-an-oid\n",
        "refs/heads/main 0000000000000000000000000000000000000000 bad-ref 0000000000000000000000000000000000000000\n",
    ),
)
def test_pre_push_rejects_malformed_lines(repo, capsys, line: str) -> None:
    """This fails if one malformed stdin record is ignored or partially parsed."""
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
    captured = capsys.readouterr()
    assert captured.err
    assert "Traceback" not in captured.err


def test_pre_push_fails_closed_on_unknown_or_noncommit_main_objects(repo, capsys) -> None:
    """This fails if ancestry accepts unavailable or type-confused object IDs."""
    previous, current = _advance(repo)
    width = _object_width(repo)
    unknown = "f" * width
    blob = repo.git("hash-object", "knowledge/rules.md").stdout.strip()
    lines = (
        f"refs/heads/main {unknown} refs/heads/main {previous}\n",
        f"refs/heads/main {current} refs/heads/main {unknown}\n",
        f"refs/heads/main {blob} refs/heads/main {previous}\n",
    )

    for line in lines:
        assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 1
        captured = capsys.readouterr()
        assert captured.err
        assert "Traceback" not in captured.err


def test_pre_push_checks_every_line_and_fails_closed_on_unknown_new_branch_objects(
    repo, capsys
) -> None:
    """This fails if a new branch can hide unavailable policy or a later malformed line."""
    previous, current = _advance(repo)
    width = _object_width(repo)
    unknown = "e" * width
    side = f"refs/heads/topic {current} refs/heads/topic {'0' * width}\n"
    unknown_side = f"refs/heads/unknown {unknown} refs/heads/unknown {'0' * width}\n"
    main = f"refs/heads/main {current} refs/heads/main {previous}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), side + main) == 0
    assert capsys.readouterr().err == ""
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), unknown_side + main) == 1
    assert capsys.readouterr().err
    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), side + main + "broken\n") == 1
    assert capsys.readouterr().err


def test_pre_push_preserves_unrelated_annotated_tag_creation(repo, capsys) -> None:
    """This fails if canonical branch policy is incorrectly imposed on tag objects."""
    repo.git("tag", "--annotate", "v1", "--message", "Version one")
    tag = repo.git("rev-parse", "refs/tags/v1").stdout.strip()
    zeros = "0" * _object_width(repo)
    line = f"refs/tags/v1 {tag} refs/tags/v1 {zeros}\n"

    assert _run_hook(repo.path, "pre-push", ("origin", "unused"), line) == 0
    assert capsys.readouterr().err == ""


def test_local_bare_remote_uses_the_versioned_pre_push_wrapper(repo, tmp_path) -> None:
    """This fails if the installed wrapper cannot allow FF and block a real forced rewrite."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    repo.git("remote", "add", "origin", str(remote))
    repo.git("config", "core.hooksPath", str(_PROJECT_ROOT / ".githooks"))

    assert repo.git("push", "origin", "main", check=False).returncode == 0
    remote_before = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("knowledge/rules.md", "New rule\n")
    remote_after = repo.commit("Advance canonical history")
    assert repo.git("push", "origin", "main", check=False).returncode == 0

    repo.checkout_new("replacement", remote_before)
    repo.write("knowledge/replacement.md", "Replacement history\n")
    repo.commit("Create replacement history")
    rejected = repo.git("push", "--force", "origin", "replacement:main", check=False)

    assert rejected.returncode != 0
    observed_remote = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    assert observed_remote == remote_after


def test_hidden_cli_uses_git_from_cwd_and_ignores_caller_git_dir(repo, tmp_path) -> None:
    """This fails if hidden dispatch trusts a caller-controlled repository environment."""
    other = RepoBuilder.create(tmp_path / "other")
    other.checkout_new("topic")
    environment = os.environ.copy()
    environment["GIT_DIR"] = str(_git_directory(other))

    result = subprocess.run(
        ["mandua", "policy-hook", "pre-rebase", "HEAD~1"],
        cwd=repo.path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "canonical" in result.stderr.lower()
    assert "Traceback" not in result.stderr


def test_public_help_keeps_fourteen_visible_operations_and_repo_contract(capsys) -> None:
    """This fails if the hidden hook becomes public or weakens the required --repo surface."""
    result = subprocess.run(
        ["mandua", "--help"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    operations = (
        "status",
        "context",
        "timeline",
        "why",
        "origin",
        "evolution",
        "compare",
        "decision",
        "recover",
        "checkpoint",
        "annotate",
        "integrate",
        "correct",
        "demo",
    )

    assert len(operations) == 14
    assert "policy-hook" not in result.stdout
    assert all(operation in result.stdout for operation in operations)
    assert cli.main(["status"]) == 2
    captured = capsys.readouterr()
    assert "--repo" in captured.err
