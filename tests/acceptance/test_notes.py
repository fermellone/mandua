"""Acceptance tests for review metamemory stored in a dedicated Git notes ref."""

from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import pytest

import mandua.git_runner as git_runner_module
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitOutput, GitRepositoryAuthority, GitRunner, _observe_git_processes
from mandua.memory_service import MemoryService
from mandua.models import AnnotationRequest, QueryLimits
from mandua.renderers import render_json
from mandua.repository import RepositoryInspector
from mandua.writes.notes import NoteWriter


class _InjectedNoteControl(BaseException):
    """Non-Mandua control flow injected after a real notes ref update."""


def _notes_authority_roots(repository: Path) -> dict[str, Path]:
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


def _replace_notes_authority_root(path: Path) -> Path:
    """Replace one authority root inode without changing its fixture namespace."""
    mode = path.stat(follow_symlinks=False).st_mode
    displaced = path.with_name(f"{path.name}.mandua-notes-authority")
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


def _restore_notes_authority_root(path: Path, displaced: Path) -> None:
    for child in tuple(path.iterdir()):
        child.rename(displaced / child.name)
    path.rmdir()
    displaced.rename(path)


def _notes_descriptor_is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return True
    return False


def _round3_long_notes_ref() -> str:
    components = "/".join(f"{index:02d}-{'n' * 72}" for index in range(7))
    return f"refs/notes/round3-emergency/{components}"


def _round3_pack_large_notes_metadata(repo) -> Path:
    for index in range(12):
        repo.git("branch", f"round3-notes-packed/{index:02d}-{'p' * 40}", "HEAD")
    repo.git("pack-refs", "--all", "--prune")
    common = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    packed = common / "packed-refs"
    assert packed.stat().st_size > 512
    return packed


def _round3_notes_planned_argv_bytes(events) -> int:
    return sum(
        sum(len(os.fsencode(argument)) + 1 for argument in event.command)
        for event in events
        if event.phase == "considered"
    )


def _annotation(
    target: str, message: str = "Review confirms the recorded rule."
) -> AnnotationRequest:
    return AnnotationRequest(target, message, "reviewer")


def _note(repo, target: str, *, ref: str = "refs/notes/review") -> str:
    return repo.git("notes", f"--ref={ref}", "show", target).stdout


def _notes_commit(
    repo,
    entries: dict[bytes, tuple[bytes, bytes]],
    *,
    parents: tuple[str, ...] = (),
    message: str = "Record a review-note fixture",
) -> str:
    """Build one real Git-notes commit from explicit mode/path/content entries."""
    root: dict[bytes, object] = {}
    for path, (mode, contents) in entries.items():
        parts = path.split(b"/")
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            assert isinstance(child, dict)
            node = child
        blob = repo.git_bytes("hash-object", "-w", "--stdin", input_bytes=contents).stdout.strip()
        node[parts[-1]] = (mode, blob)

    def write_tree(node: dict[bytes, object]) -> bytes:
        records: list[bytes] = []
        for name, value in sorted(node.items()):
            if isinstance(value, dict):
                object_id = write_tree(value)
                records.append(b"040000 tree " + object_id + b"\t" + name + b"\x00")
            else:
                assert isinstance(value, tuple)
                mode, object_id = value
                records.append(mode + b" blob " + object_id + b"\t" + name + b"\x00")
        return repo.git_bytes("mktree", "-z", input_bytes=b"".join(records)).stdout.strip()

    arguments = ["commit-tree", write_tree(root).decode("ascii")]
    for parent in parents:
        arguments.extend(("-p", parent))
    arguments.extend(("-m", message))
    return repo.git(*arguments).stdout.strip()


def _deep_note_path(object_id: str) -> bytes:
    encoded = object_id.encode("ascii")
    return b"/".join((encoded[:2], encoded[2:4], encoded[4:]))


def _tree_object(repo, entries: dict[bytes, tuple[bytes, bytes]]) -> bytes:
    records = []
    for path, (mode, object_id) in sorted(entries.items()):
        object_type = {b"040000": b"tree", b"160000": b"commit"}.get(mode, b"blob")
        records.append(mode + b" " + object_type + b" " + object_id + b"\t" + path + b"\x00")
    return repo.git_bytes("mktree", "-z", "--missing", input_bytes=b"".join(records)).stdout.strip()


def _notes_commit_from_tree(
    repo,
    tree_oid: bytes,
    *,
    parents: tuple[str, ...] = (),
    message: str = "Record an explicit review-note tree",
) -> str:
    arguments = ["commit-tree", tree_oid.decode("ascii")]
    for parent in parents:
        arguments.extend(("-p", parent))
    arguments.extend(("-m", message))
    return repo.git(*arguments).stdout.strip()


def _notes_commit_with_empty_subtree(
    repo,
    target: str,
    note: bytes,
    *,
    nested: bool = False,
    parents: tuple[str, ...] = (),
) -> str:
    note_blob = repo.git_bytes("hash-object", "-w", "--stdin", input_bytes=note).stdout.strip()
    empty_tree = _tree_object(repo, {})
    sentinel_tree = (
        _tree_object(repo, {b"inner-empty": (b"040000", empty_tree)}) if nested else empty_tree
    )
    root_tree = _tree_object(
        repo,
        {
            target.encode("ascii"): (b"100644", note_blob),
            b"empty-sentinel": (b"040000", sentinel_tree),
        },
    )
    return _notes_commit_from_tree(
        repo,
        root_tree,
        parents=parents,
        message="Record an explicit empty review-note subtree",
    )


def _raw_notes_commit(
    repo,
    entries: dict[bytes, tuple[bytes, str]],
    *,
    message: str = "Record a raw review-note fixture",
) -> str:
    """Build a real tree without letting mktree correct mode-derived object types."""
    tree_body = b"".join(
        mode + b" " + path + b"\x00" + bytes.fromhex(object_id)
        for path, (mode, object_id) in sorted(entries.items())
    )
    tree_oid = repo.git_bytes(
        "hash-object",
        "-t",
        "tree",
        "--literally",
        "-w",
        "--stdin",
        input_bytes=tree_body,
    ).stdout.strip()
    return repo.git("commit-tree", tree_oid.decode("ascii"), "-m", message).stdout.strip()


def _repository_and_notes_state(repo) -> tuple[str, str, str, str]:
    return (
        repo.git("rev-parse", "HEAD").stdout,
        repo.git("write-tree").stdout,
        repo.git("status", "--porcelain=v2").stdout,
        repo.git("rev-parse", "refs/notes/review").stdout,
    )


def test_annotation_preview_and_apply_change_only_the_configured_notes_ref(repo) -> None:
    """This fails if annotation mutates HEAD, the index, or the worktree instead of only notes."""
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    target = repo.commit("Record the reviewed irrigation rule")
    repo.write("knowledge/unstaged.md", "Keep this worktree change.\n")
    repo.write("knowledge/staged.md", "Keep this staged change.\n")
    repo.git("add", "knowledge/staged.md")
    head_before = repo.git("rev-parse", "HEAD").stdout
    index_before = repo.git("write-tree").stdout
    status_before = repo.git("status", "--porcelain=v2").stdout

    preview = repo.service().annotate(_annotation(target))

    assert preview.applied is False
    assert preview.changes[0].target == "refs/notes/review"
    assert preview.changes[0].before_oid is None
    assert repo.git("notes", "--ref=refs/notes/review", "show", target, check=False).returncode != 0

    applied = repo.service().annotate(_annotation(target), apply=True)

    assert applied.applied is True
    assert applied.changes[0].after_oid is not None
    assert _note(repo, target) == ("Review confirms the recorded rule.\n\nAgent-ID: reviewer\n")
    assert repo.git("rev-parse", "HEAD").stdout == head_before
    assert repo.git("write-tree").stdout == index_before
    assert repo.git("status", "--porcelain=v2").stdout == status_before


def test_annotation_appends_without_replacing_an_existing_native_note(repo) -> None:
    """This fails if a second review replaces instead of appending to native note content."""
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    target = repo.commit("Record the irrigation time")
    repo.git(
        "notes",
        "--ref=refs/notes/review",
        "add",
        "-m",
        "Existing native review.",
        target,
    )

    repo.service().annotate(
        AnnotationRequest(target, "Second review confirms the timing.", "second-reviewer"),
        apply=True,
    )

    assert _note(repo, target) == (
        "Existing native review.\n\n"
        "Second review confirms the timing.\n\n"
        "Agent-ID: second-reviewer\n"
    )


def test_annotation_matches_native_append_for_an_empty_existing_note(repo) -> None:
    """This fails if an empty native note gains a leading separator during append."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    empty_blob = repo.git_bytes("hash-object", "-w", "--stdin", input_bytes=b"").stdout.strip()
    tree = repo.git_bytes(
        "mktree",
        input_bytes=b"100644 blob " + empty_blob + b"\t" + target.encode() + b"\n",
    ).stdout.strip()
    notes_commit = repo.git(
        "commit-tree", tree.decode(), "-m", "Record an empty note"
    ).stdout.strip()
    repo.git("update-ref", "refs/notes/review", notes_commit)

    repo.service().annotate(_annotation(target), apply=True)

    assert _note(repo, target) == "Review confirms the recorded rule.\n\nAgent-ID: reviewer\n"


def test_candidate_reads_existing_note_from_the_captured_notes_commit(repo, monkeypatch) -> None:
    """This fails if candidate content is read through a ref that can move after capture."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Existing review.", target)
    original = GitRunner.run

    def reject_live_note_reads(self, arguments, **kwargs):
        if arguments[:1] == ["notes"] and "show" in arguments:
            raise AssertionError("Annotation candidates must read the captured notes commit.")
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run", reject_live_note_reads)

    result = repo.service().annotate(_annotation(target), apply=True)

    assert result.applied is True
    assert _note(repo, target).count("Existing review.") == 1


@pytest.mark.parametrize("apply", (False, True), ids=("preview", "apply"))
def test_annotation_rejects_an_explicit_empty_subtree_without_mutation(repo, apply) -> None:
    """This fails if preview accepts or apply silently drops an explicit empty subtree."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_commit = _notes_commit_with_empty_subtree(
        repo,
        target,
        b"Existing valid review.\n",
    )
    repo.git("update-ref", "refs/notes/review", notes_commit)
    fsck = repo.git("fsck", "--full", "--no-dangling")
    assert fsck.stdout == ""
    assert fsck.stderr == ""
    repo.write("knowledge/staged.md", "Preserve this staged change.\n")
    repo.git("add", "knowledge/staged.md")
    repo.write("knowledge/unstaged.md", "Preserve this unstaged change.\n")
    before = _repository_and_notes_state(repo)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=apply)

    assert error.value.code is ErrorCode.GIT_FAILURE
    assert error.value.recovery is not None
    assert _repository_and_notes_state(repo) == before


def test_annotation_rejects_a_nested_empty_only_subtree(repo) -> None:
    """This fails if recursive enumeration sees only the outer empty-only directory."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_commit = _notes_commit_with_empty_subtree(
        repo,
        target,
        b"Existing valid review.\n",
        nested=True,
    )
    repo.git("update-ref", "refs/notes/review", notes_commit)
    before = _repository_and_notes_state(repo)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target))

    assert error.value.code is ErrorCode.GIT_FAILURE
    assert error.value.recovery is not None
    assert _repository_and_notes_state(repo) == before


def test_stale_sibling_empty_subtree_fails_closed_after_candidate_publication(
    repo, monkeypatch
) -> None:
    """This fails if repair reports success after dropping a stale sibling's empty subtree."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Existing review A.", target)
    initial = repo.git("rev-parse", "refs/notes/review").stdout.strip()
    stale_commit = _notes_commit_with_empty_subtree(
        repo,
        target,
        b"Stale native review C.\n",
        parents=(initial,),
    )
    original = GitRunner.run_text
    injected = False

    def overwrite_first_publication(self, arguments, **kwargs):
        nonlocal injected
        output = original(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and arguments[2] == "refs/notes/review" and not injected:
            injected = True
            repo.git("update-ref", "refs/notes/review", stale_commit)
        return output

    monkeypatch.setattr(GitRunner, "run_text", overwrite_first_publication)
    head_before = repo.git("rev-parse", "HEAD").stdout
    index_before = repo.git("write-tree").stdout
    status_before = repo.git("status", "--porcelain=v2").stdout

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    assert injected is True
    assert error.value.code is ErrorCode.GIT_FAILURE
    assert error.value.recovery is not None
    assert repo.git("rev-parse", "refs/notes/review").stdout.strip() == stale_commit
    assert repo.git("rev-parse", "HEAD").stdout == head_before
    assert repo.git("write-tree").stdout == index_before
    assert repo.git("status", "--porcelain=v2").stdout == status_before


def test_annotation_preserves_nonempty_fanout_subtrees_and_sentinel_objects(repo) -> None:
    """This fails if tree-node validation rejects or rewrites representable notes-tree data."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    missing_gitlink = "e" * len(target)
    assert repo.git("cat-file", "-e", missing_gitlink, check=False).returncode != 0
    existing_note = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Existing fanout review.\n"
    ).stdout.strip()
    arbitrary_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"arbitrary subtree sentinel\n"
    ).stdout.strip()
    executable_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"executable sentinel\n"
    ).stdout.strip()
    symlink_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"sentinel-target"
    ).stdout.strip()
    path_parts = _deep_note_path(target).split(b"/")
    note_leaf_tree = _tree_object(repo, {path_parts[2]: (b"100644", existing_note)})
    note_middle_tree = _tree_object(repo, {path_parts[1]: (b"040000", note_leaf_tree)})
    arbitrary_tree = _tree_object(repo, {b"leaf": (b"100644", arbitrary_blob)})
    root_tree = _tree_object(
        repo,
        {
            path_parts[0]: (b"040000", note_middle_tree),
            b"arbitrary-sentinel": (b"040000", arbitrary_tree),
            b"executable-sentinel": (b"100755", executable_blob),
            b"gitlink-sentinel": (b"160000", target.encode("ascii")),
            b"missing-gitlink-sentinel": (b"160000", missing_gitlink.encode("ascii")),
            b"symlink-sentinel": (b"120000", symlink_blob),
        },
    )
    notes_commit = _notes_commit_from_tree(repo, root_tree)
    repo.git("update-ref", "refs/notes/review", notes_commit)

    result = repo.service().annotate(_annotation(target), apply=True)

    assert result.applied is True
    assert _note(repo, target) == (
        "Existing fanout review.\n\nReview confirms the recorded rule.\n\nAgent-ID: reviewer\n"
    )
    tree_records = repo.git_bytes(
        "ls-tree", "-r", "-t", "-z", "refs/notes/review", input_bytes=b""
    ).stdout
    records = {
        path: metadata
        for record in tree_records.removesuffix(b"\x00").split(b"\x00")
        for metadata, separator, path in (record.partition(b"\t"),)
        if separator
    }
    expected = {
        b"arbitrary-sentinel": b"040000 tree " + arbitrary_tree,
        b"arbitrary-sentinel/leaf": b"100644 blob " + arbitrary_blob,
        b"executable-sentinel": b"100755 blob " + executable_blob,
        b"gitlink-sentinel": b"160000 commit " + target.encode("ascii"),
        b"missing-gitlink-sentinel": b"160000 commit " + missing_gitlink.encode("ascii"),
        b"symlink-sentinel": b"120000 blob " + symlink_blob,
    }
    assert {path: records[path] for path in expected} == expected
    assert _deep_note_path(target) in records


def test_annotation_rejects_a_regular_note_entry_that_references_a_commit(repo) -> None:
    """This fails if ls-tree's mode-derived blob label is trusted as the actual object type."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    note_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Existing valid review.\n"
    ).stdout.strip()
    malformed = _raw_notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", note_blob.decode("ascii")),
            b"malformed-sentinel": (b"100644", target),
        },
        message="Record a commit disguised as a note blob",
    )
    repo.git("update-ref", "refs/notes/review", malformed)
    before = _repository_and_notes_state(repo)
    fsck_before = repo.git("fsck", "--full", "--no-dangling", check=False)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    fsck_after = repo.git("fsck", "--full", "--no-dangling", check=False)
    assert error.value.code is ErrorCode.GIT_FAILURE
    assert _repository_and_notes_state(repo) == before
    assert (fsck_after.returncode, fsck_after.stdout, fsck_after.stderr) == (
        fsck_before.returncode,
        fsck_before.stdout,
        fsck_before.stderr,
    )


def test_annotation_rejects_a_missing_non_gitlink_note_object(repo) -> None:
    """This fails if a missing regular entry is copied into a new reachable notes commit."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    missing_oid = "f" * len(target)
    assert repo.git("cat-file", "-e", missing_oid, check=False).returncode != 0
    note_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Existing valid review.\n"
    ).stdout.strip()
    malformed = _raw_notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", note_blob.decode("ascii")),
            b"missing-sentinel": (b"100644", missing_oid),
        },
        message="Record a missing regular note object",
    )
    repo.git("update-ref", "refs/notes/review", malformed)
    before = _repository_and_notes_state(repo)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    assert error.value.code is ErrorCode.GIT_FAILURE
    assert _repository_and_notes_state(repo) == before


@pytest.mark.parametrize("malformation", ("diagnostic", "reordered"))
def test_annotation_rejects_misleading_batched_object_probe_output(
    repo, monkeypatch, malformation
) -> None:
    """This fails if batch diagnostics or reordered object records are accepted as proof."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    target_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Existing valid review.\n"
    ).stdout.strip()
    sentinel_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Valid sentinel.\n"
    ).stdout.strip()
    notes_commit = _raw_notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", target_blob.decode("ascii")),
            b"valid-sentinel": (b"100755", sentinel_blob.decode("ascii")),
        },
    )
    repo.git("update-ref", "refs/notes/review", notes_commit)
    before = _repository_and_notes_state(repo)
    original = GitRunner.run

    def corrupt_probe(self, arguments, **kwargs):
        if arguments == ["cat-file", "--batch-check=%(objectname) %(objecttype)"]:
            object_ids = kwargs["input_bytes"].splitlines()
            records = [object_id + b" blob\n" for object_id in object_ids]
            if malformation == "reordered":
                records.reverse()
            return GitOutput(
                stdout=b"".join(records),
                stderr=(
                    b"injected corruption diagnostic\n" if malformation == "diagnostic" else b""
                ),
                returncode=0,
            )
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run", corrupt_probe)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    assert error.value.code is ErrorCode.GIT_FAILURE
    assert _repository_and_notes_state(repo) == before


def test_snapshot_tree_and_object_probe_share_one_output_byte_budget(repo) -> None:
    """This fails if one bounded tree read and one bounded probe can multiply the byte limit."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    entries: dict[bytes, tuple[bytes, str]] = {}
    for number in range(6):
        blob = repo.git_bytes(
            "hash-object", "-w", "--stdin", input_bytes=f"Review {number}.\n".encode()
        ).stdout.strip()
        path = target.encode("ascii") if number == 0 else f"sentinel-{number}".encode()
        entries[path] = (b"100644", blob.decode("ascii"))
    notes_commit = _raw_notes_commit(repo, entries)
    repo.git("update-ref", "refs/notes/review", notes_commit)
    tree_size = len(repo.git_bytes("ls-tree", "-r", "-z", notes_commit, input_bytes=b"").stdout)
    service = MemoryService.open(repo.path, limits=QueryLimits(max_output_bytes=tree_size + 1))

    with pytest.raises(ManduaError) as error:
        service.annotate(_annotation(target), apply=True)

    assert error.value.code is ErrorCode.LIMIT_EXCEEDED
    assert repo.git("rev-parse", "refs/notes/review").stdout.strip() == notes_commit


def test_annotation_preserves_valid_modes_missing_gitlinks_and_native_interoperability(
    repo,
) -> None:
    """This fails if strict type validation rejects legitimate notes-tree sentinel modes."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    missing_gitlink = "e" * len(target)
    assert repo.git("cat-file", "-e", missing_gitlink, check=False).returncode != 0
    target_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Existing valid review.\n"
    ).stdout.strip()
    executable_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"executable sentinel\n"
    ).stdout.strip()
    symlink_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"sentinel-target"
    ).stdout.strip()
    notes_commit = _raw_notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", target_blob.decode("ascii")),
            b"executable-sentinel": (b"100755", executable_blob.decode("ascii")),
            b"gitlink-sentinel": (b"160000", target),
            b"missing-gitlink-sentinel": (b"160000", missing_gitlink),
            b"symlink-sentinel": (b"120000", symlink_blob.decode("ascii")),
        },
        message="Record valid mode sentinels",
    )
    repo.git("update-ref", "refs/notes/review", notes_commit)

    result = repo.service().annotate(_annotation(target), apply=True)
    repo.git(
        "notes",
        "--ref=refs/notes/review",
        "append",
        "-m",
        "Native follow-up review.",
        target,
    )

    note = _note(repo, target)
    assert result.applied is True
    assert note.count("Existing valid review.") == 1
    assert note.count("Review confirms the recorded rule.") == 1
    assert note.count("Native follow-up review.") == 1
    tree = repo.git("ls-tree", "-r", "refs/notes/review").stdout
    assert "100755 blob" in tree and "executable-sentinel" in tree
    assert "120000 blob" in tree and "symlink-sentinel" in tree
    assert tree.count("160000 commit") == 2


def test_review_note_requires_an_explicit_fetch_and_then_appears_in_why(repo, tmp_path) -> None:
    """This fails if reads fetch implicitly or a normal clone incorrectly includes review notes."""
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    target = repo.commit("Record the reviewed irrigation rule")
    repo.service().annotate(_annotation(target), apply=True)

    clone = repo.clone_to(tmp_path / "without-notes")
    before = clone.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert before.history_scope.notes_available is False
    assert any("may exist elsewhere" in warning for warning in before.warnings)
    assert all(item.kind != "review-note" for item in before.evidence)

    clone.git("fetch", "origin", "refs/notes/review:refs/notes/review")
    after = clone.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert after.history_scope.notes_available is True
    assert any(item.kind == "review-note" and item.oid == target for item in after.evidence)


def test_custom_configured_notes_ref_is_used_for_writes_and_queries(repo) -> None:
    """This fails if annotation or provenance silently hard-codes refs/notes/review."""
    repo.write(
        ".mandua.toml",
        '[repository]\nnotes_ref = "refs/notes/mandua-reviews"\n',
    )
    repo.write("knowledge/rules.md", "Water at dusk.\n")
    target = repo.commit("Record a custom review ref")

    repo.service().annotate(_annotation(target), apply=True)
    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert _note(repo, target, ref="refs/notes/mandua-reviews").startswith("Review confirms")
    assert (
        repo.git("show-ref", "--verify", "--quiet", "refs/notes/review", check=False).returncode
        == 1
    )
    assert result.history_scope.notes_available is True
    assert any(
        item.kind == "review-note" and item.ref == "refs/notes/mandua-reviews"
        for item in result.evidence
    )


def test_available_notes_ref_without_a_target_note_is_not_reported_as_missing(repo) -> None:
    """This fails if no note for one target is confused with a locally absent notes ref."""
    baseline = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Baseline review.", baseline)
    repo.write("knowledge/rules.md", "Water only when dry.\n")
    repo.commit("Record an unreviewed rule")

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert result.history_scope.notes_available is True
    assert all("may exist elsewhere" not in warning for warning in result.warnings)
    assert "No reason was recorded for this change." in result.gaps


def test_decision_includes_bounded_review_notes_for_relevant_commits(repo) -> None:
    """This fails if decision retrieval omits review evidence attached to a matching commit."""
    repo.write("knowledge/decision.md", "Use drip irrigation.\n")
    target = repo.commit(
        "Adopt drip irrigation\n\n"
        "Memory-Type: decision\n"
        "Scope: irrigation\n"
        "Decision-ID: DEC-NOTE-1\n"
        "Agent-ID: planner"
    )
    repo.service().annotate(_annotation(target, "Review confirms the drip decision."), apply=True)

    result = repo.service().decision("DEC-NOTE-1")

    assert result.history_scope.notes_available is True
    assert any(
        item.kind == "review-note"
        and item.oid == target
        and item.ref == "refs/notes/review"
        and item.excerpt is not None
        and "Review confirms the drip decision." in item.excerpt
        for item in result.evidence
    )


@pytest.mark.parametrize(
    ("annotation_request", "code"),
    [
        (object(), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", "", "reviewer"), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", " Review text.", "reviewer"), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", "Review\ntext.", "reviewer"), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", "Review\ttext.", "reviewer"), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", "Review text.", ""), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", "Review text.", "reviewer\nother"), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("HEAD", "Review text.", "reviewer\x1b"), ErrorCode.VALIDATION_FAILED),
        (AnnotationRequest("missing", "Review text.", "reviewer"), ErrorCode.INVALID_REVISION),
    ],
)
def test_annotation_rejects_invalid_structural_inputs(repo, annotation_request, code) -> None:
    """This fails if malformed request data reaches Git notes or is silently normalized."""
    with pytest.raises(ManduaError) as error:
        repo.service().annotate(annotation_request)

    assert error.value.code is code


def test_annotation_honors_caller_bounds_without_probabilistic_language_detection(repo) -> None:
    """This fails if note validation ignores caller bounds or invents a language classifier."""
    service = MemoryService.open(repo.path, limits=QueryLimits(max_input_chars=32))

    with pytest.raises(ManduaError) as oversized:
        service.annotate(AnnotationRequest("HEAD", "x" * 33, "reviewer"))

    accepted = service.annotate(
        AnnotationRequest("HEAD", "Review confirmed by Ñandú ✅.", "réviewer-★")
    )

    assert oversized.value.code is ErrorCode.VALIDATION_FAILED
    assert accepted.applied is False


def test_native_append_overlapping_candidate_publication_is_preserved(repo, monkeypatch) -> None:
    """This fails if publication overwrites a native append made after candidate construction."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Shared base review A.", target)
    original = GitRunner.run_text
    injected = False

    def append_before_publication(self, arguments, **kwargs):
        nonlocal injected
        if arguments[:1] == ["update-ref"] and "refs/notes/review" in arguments and not injected:
            injected = True
            repo.git(
                "notes",
                "--ref=refs/notes/review",
                "append",
                "-m",
                "Concurrent native review.",
                target,
            )
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", append_before_publication)

    result = repo.service().annotate(_annotation(target), apply=True)

    note = _note(repo, target)
    assert injected is True
    assert result.applied is True
    assert note.count("Shared base review A.") == 1
    assert note.count("Concurrent native review.") == 1
    assert note.count("Review confirms the recorded rule.") == 1


def test_stale_native_publication_after_success_is_repaired_before_return(
    repo, monkeypatch
) -> None:
    """This fails if a stale native writer can overwrite a successful candidate at verification."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    original = GitRunner.run_text
    injected = False

    native_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Concurrent native review.\n"
    ).stdout.strip()
    native_tree = repo.git_bytes(
        "mktree",
        input_bytes=b"100644 blob " + native_blob + b"\t" + target.encode() + b"\n",
    ).stdout.strip()
    native_commit = repo.git(
        "commit-tree", native_tree.decode(), "-m", "Record stale native review"
    ).stdout.strip()

    def overwrite_after_success(self, arguments, **kwargs):
        nonlocal injected
        output = original(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and "refs/notes/review" in arguments and not injected:
            injected = True
            repo.git("update-ref", "refs/notes/review", native_commit)
        return output

    monkeypatch.setattr(GitRunner, "run_text", overwrite_after_success)

    result = repo.service().annotate(_annotation(target), apply=True)

    note = _note(repo, target)
    assert injected is True
    assert result.applied is True
    assert note.count("Concurrent native review.") == 1
    assert note.count("Review confirms the recorded rule.") == 1


def test_stale_sibling_repair_unions_candidate_and_current_note_trees(repo, monkeypatch) -> None:
    """This fails if stale-state repair drops prior, other-target, or deep-fanout notes."""
    other_target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("knowledge/reviewed.md", "Reviewed fact.\n")
    target = repo.commit("Record a fact for stale-sibling review")
    candidate_parent = _notes_commit(
        repo,
        {
            _deep_note_path(target): (b"100644", b"Existing review A.\n"),
            _deep_note_path(other_target): (b"100644", b"Candidate-only other review.\n"),
        },
        message="Record candidate-side review memory",
    )
    repo.git("update-ref", "refs/notes/review", candidate_parent)
    stale_commit = _notes_commit(
        repo,
        {target.encode("ascii"): (b"100644", b"Stale native review C.\n")},
        message="Record stale native sibling",
    )
    original = GitRunner.run_text
    first_candidate: str | None = None

    def overwrite_first_publication(self, arguments, **kwargs):
        nonlocal first_candidate
        output = original(self, arguments, **kwargs)
        if (
            arguments[:1] == ["update-ref"]
            and arguments[2] == "refs/notes/review"
            and first_candidate is None
        ):
            first_candidate = arguments[3]
            repo.git("update-ref", "refs/notes/review", stale_commit)
        return output

    monkeypatch.setattr(GitRunner, "run_text", overwrite_first_publication)

    result = repo.service().annotate(_annotation(target), apply=True)

    assert result.applied is True
    assert _note(repo, target) == (
        "Existing review A.\n\n"
        "Review confirms the recorded rule.\n\n"
        "Agent-ID: reviewer\n\n"
        "Stale native review C.\n"
    )
    assert _note(repo, other_target) == "Candidate-only other review.\n"
    final_ref = repo.git("rev-parse", "refs/notes/review").stdout.strip()
    assert first_candidate is not None
    assert (
        repo.git("merge-base", "--is-ancestor", first_candidate, final_ref, check=False).returncode
        == 0
    )
    assert (
        repo.git("merge-base", "--is-ancestor", stale_commit, final_ref, check=False).returncode
        == 0
    )
    target_paths = [
        path
        for path in repo.git("ls-tree", "-r", "--name-only", final_ref).stdout.splitlines()
        if path.replace("/", "") == target
    ]
    assert len(target_paths) == 1
    assert target_paths[0].count("/") >= 2


def test_stale_sibling_repair_fails_closed_on_an_unmergeable_note_mode(repo, monkeypatch) -> None:
    """This fails if a conflicting logical note entry is silently chosen from one sibling."""
    other_target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("knowledge/reviewed.md", "Reviewed fact.\n")
    target = repo.commit("Record a fact for conflicting-note review")
    initial = _notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", b"Existing review A.\n"),
            other_target.encode("ascii"): (b"100644", b"Candidate-side other note.\n"),
        },
    )
    repo.git("update-ref", "refs/notes/review", initial)
    stale_commit = _notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", b"Stale native review C.\n"),
            other_target.encode("ascii"): (b"120000", b"conflicting-note-target"),
        },
        message="Record a conflicting stale sibling",
    )
    original = GitRunner.run_text
    injected = False

    def overwrite_first_publication(self, arguments, **kwargs):
        nonlocal injected
        output = original(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and arguments[2] == "refs/notes/review" and not injected:
            injected = True
            repo.git("update-ref", "refs/notes/review", stale_commit)
        return output

    monkeypatch.setattr(GitRunner, "run_text", overwrite_first_publication)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    assert injected is True
    assert error.value.code is ErrorCode.GIT_FAILURE
    assert error.value.recovery is not None
    assert repo.git("rev-parse", "refs/notes/review").stdout.strip() == stale_commit


def test_stale_sibling_union_rejects_a_regular_entry_backed_by_a_commit(repo, monkeypatch) -> None:
    """This fails if repair trusts a stale tree's mode-derived type after snapshot validation."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Existing review A.", target)
    stale_blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Stale native review C.\n"
    ).stdout.strip()
    stale_commit = _raw_notes_commit(
        repo,
        {
            target.encode("ascii"): (b"100644", stale_blob.decode("ascii")),
            b"malformed-stale-sentinel": (b"100644", target),
        },
        message="Record a malformed stale sibling",
    )
    original_run_text = GitRunner.run_text
    original_snapshot = NoteWriter._read_note_snapshot
    injected = False

    def overwrite_first_publication(self, arguments, **kwargs):
        nonlocal injected
        output = original_run_text(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and arguments[2] == "refs/notes/review" and not injected:
            injected = True
            repo.git("update-ref", "refs/notes/review", stale_commit)
        return output

    def bypass_only_stale_snapshot(self, notes_ref_oid, target_oid, *, deadline):
        if notes_ref_oid == stale_commit:
            return b"Stale native review C.\n", target_oid.encode("ascii")
        return original_snapshot(self, notes_ref_oid, target_oid, deadline=deadline)

    monkeypatch.setattr(GitRunner, "run_text", overwrite_first_publication)
    monkeypatch.setattr(NoteWriter, "_read_note_snapshot", bypass_only_stale_snapshot)
    head_before = repo.git("rev-parse", "HEAD").stdout
    index_before = repo.git("write-tree").stdout
    status_before = repo.git("status", "--porcelain=v2").stdout

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    assert injected is True
    assert error.value.code is ErrorCode.GIT_FAILURE
    assert repo.git("rev-parse", "refs/notes/review").stdout.strip() == stale_commit
    assert repo.git("rev-parse", "HEAD").stdout == head_before
    assert repo.git("write-tree").stdout == index_before
    assert repo.git("status", "--porcelain=v2").stdout == status_before


def test_stale_sibling_repair_has_one_aggregate_tree_byte_budget(repo, monkeypatch) -> None:
    """This fails if independently bounded sibling trees can multiply the repair byte budget."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    initial_entries = {
        target.encode("ascii"): (b"100644", b"Existing review A.\n"),
        **{
            f"{number:040x}".encode("ascii"): (b"100644", f"Candidate {number}.\n".encode())
            for number in range(1, 6)
        },
    }
    stale_entries = {
        target.encode("ascii"): (b"100644", b"Stale native review C.\n"),
        **{
            f"{number:040x}".encode("ascii"): (b"100644", f"Stale {number}.\n".encode())
            for number in range(101, 106)
        },
    }
    initial = _notes_commit(repo, initial_entries, message="Record bounded candidate notes")
    stale_commit = _notes_commit(repo, stale_entries, message="Record bounded stale notes")
    repo.git("update-ref", "refs/notes/review", initial)
    original = GitRunner.run_text
    injected = False

    def overwrite_first_publication(self, arguments, **kwargs):
        nonlocal injected
        output = original(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and arguments[2] == "refs/notes/review" and not injected:
            injected = True
            repo.git("update-ref", "refs/notes/review", stale_commit)
        return output

    monkeypatch.setattr(GitRunner, "run_text", overwrite_first_publication)
    service = MemoryService.open(repo.path, limits=QueryLimits(max_output_bytes=1_000))

    with pytest.raises(ManduaError) as error:
        service.annotate(_annotation(target), apply=True)

    assert injected is True
    assert error.value.code is ErrorCode.LIMIT_EXCEEDED
    assert repo.git("rev-parse", "refs/notes/review").stdout.strip() == stale_commit


def test_two_overlapping_mandua_appends_preserve_both_reviews(repo, monkeypatch) -> None:
    """This fails if two candidates based on one ref state can overwrite each other."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Shared base review A.", target)
    first = repo.service()
    second = repo.service()
    original = GitRunner.run_text
    publication_barrier = threading.Barrier(2)
    thread_state = threading.local()

    def synchronize_first_publication(self, arguments, **kwargs):
        is_publication = (arguments[:1] == ["notes"] and "append" in arguments) or (
            arguments[:1] == ["update-ref"] and "refs/notes/review" in arguments
        )
        if is_publication and not getattr(thread_state, "published", False):
            thread_state.published = True
            publication_barrier.wait(timeout=5)
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", synchronize_first_publication)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            future.result()
            for future in (
                executor.submit(
                    first.annotate,
                    AnnotationRequest(target, "First overlapping review.", "reviewer-one"),
                    apply=True,
                ),
                executor.submit(
                    second.annotate,
                    AnnotationRequest(target, "Second overlapping review.", "reviewer-two"),
                    apply=True,
                ),
            )
        )

    note = _note(repo, target)
    assert all(result.applied for result in results)
    assert note.count("Shared base review A.") == 1
    assert note.count("First overlapping review.") == 1
    assert note.count("Second overlapping review.") == 1
    assert repo.git("for-each-ref", "--format=%(refname)", "refs/notes").stdout.splitlines() == [
        "refs/notes/review"
    ]
    common_dir = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    assert not (common_dir / "refs/notes/review.lock").exists()


def test_post_success_runner_exception_is_reconciled_as_an_applied_annotation(
    repo, monkeypatch
) -> None:
    """This fails if a post-success runner exception falsely reports that no mutation occurred."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    original = GitRunner.run_text
    injected = False

    def run_then_raise(self, arguments, **kwargs):
        nonlocal injected
        result = original(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and "refs/notes/review" in arguments and not injected:
            injected = True
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected post-success timeout.")
        return result

    monkeypatch.setattr(GitRunner, "run_text", run_then_raise)

    result = repo.service().annotate(_annotation(target), apply=True)

    assert result.applied is True
    assert any("reconciled" in warning for warning in result.warnings)
    assert "Review confirms the recorded rule." in _note(repo, target)


def test_terminal_observer_failure_after_real_notes_update_is_reconciled_as_applied(
    repo,
) -> None:
    """This fails if a completed notes update is reported only as an observer failure."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_ref = "refs/notes/review"
    failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Injected terminal observation failure after notes publication.",
    )
    completed_updates = []

    def interrupt_completed_update(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", notes_ref)
            and not completed_updates
        ):
            completed_updates.append(event)
            raise failure

    with _observe_git_processes(interrupt_completed_update):
        result = repo.service().annotate(_annotation(target), apply=True)

    candidate_oid = completed_updates[0].arguments[3]
    assert len(completed_updates) == 1
    assert result.applied is True
    assert result.changes[0].after_oid == candidate_oid
    assert any("reconciled" in warning.casefold() for warning in result.warnings)
    assert repo.git("rev-parse", notes_ref).stdout.strip() == candidate_oid
    assert _note(repo, target).count("Review confirms the recorded rule.") == 1


def test_post_update_base_exception_proves_descendant_notes_publication_before_reraise(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if non-Mandua control flow bypasses bounded descendant classification."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_ref = "refs/notes/review"
    head_before = repo.git("rev-parse", "HEAD").stdout
    index_before = repo.git("write-tree").stdout
    status_before = repo.git("status", "--porcelain=v2").stdout
    failure = _InjectedNoteControl("injected after real notes publication")
    writer = NoteWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    completed_updates = []
    post_interruption_events = []
    candidate_oid: str | None = None
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", notes_ref)
        ):
            completed_updates.append(event)
        if interrupted:
            post_interruption_events.append(event)

    def publish_descendant_then_interrupt(arguments, **kwargs):
        nonlocal candidate_oid, interrupted
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", notes_ref] and not interrupted:
            candidate_oid = arguments[3]
            assert output.returncode == 0 and not output.stdout and not output.stderr
            repo.git(
                "notes",
                f"--ref={notes_ref}",
                "append",
                "-m",
                "Concurrent review after interrupted publication.",
                target,
            )
            interrupted = True
            raise failure
        return output

    monkeypatch.setattr(writer._runner, "run_text", publish_descendant_then_interrupt)

    with _observe_git_processes(observe_process), pytest.raises(_InjectedNoteControl) as caught:
        writer.annotate(_annotation(target), apply=True)

    assert caught.value is failure
    assert interrupted is True
    assert candidate_oid is not None
    assert len(completed_updates) == 1
    assert completed_updates[0].arguments[3] == candidate_oid
    assert any(
        event.arguments[:2] == ("merge-base", "--is-ancestor") for event in post_interruption_events
    )
    final_ref = repo.git("rev-parse", notes_ref).stdout.strip()
    assert final_ref != candidate_oid
    assert (
        repo.git("merge-base", "--is-ancestor", candidate_oid, final_ref, check=False).returncode
        == 0
    )
    note = _note(repo, target)
    assert note.count("Review confirms the recorded rule.") == 1
    assert note.count("Concurrent review after interrupted publication.") == 1
    assert repo.git("rev-parse", "HEAD").stdout == head_before
    assert repo.git("write-tree").stdout == index_before
    assert repo.git("status", "--porcelain=v2").stdout == status_before


def test_post_success_descendant_ref_proves_the_candidate_was_published(repo, monkeypatch) -> None:
    """This fails if a native follow-up hides proof that the interrupted candidate was published."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    original = GitRunner.run_text
    injected = False

    def publish_append_then_raise(self, arguments, **kwargs):
        nonlocal injected
        result = original(self, arguments, **kwargs)
        if arguments[:1] == ["update-ref"] and "refs/notes/review" in arguments and not injected:
            injected = True
            repo.git(
                "notes",
                "--ref=refs/notes/review",
                "append",
                "-m",
                "Concurrent review after publication.",
                target,
            )
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected post-success timeout.")
        return result

    monkeypatch.setattr(GitRunner, "run_text", publish_append_then_raise)

    result = repo.service().annotate(_annotation(target), apply=True)

    note = _note(repo, target)
    assert injected is True
    assert result.applied is True
    assert any("reconciled" in warning for warning in result.warnings)
    assert note.count("Review confirms the recorded rule.") == 1
    assert note.count("Concurrent review after publication.") == 1


def test_identical_concurrent_note_cannot_reconcile_an_intercepted_publication(
    repo, monkeypatch
) -> None:
    """This fails if matching note text is mistaken for this invocation's ref transaction."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    note_body = b"Review confirms the recorded rule.\n\nAgent-ID: reviewer\n"
    failure = ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected interrupted publication.")
    injected = False

    original = GitRunner.run_text

    def intercept(self, arguments, **kwargs):
        nonlocal injected
        is_publication = (arguments[:1] == ["notes"] and "append" in arguments) or (
            arguments[:1] == ["update-ref"] and "refs/notes/review" in arguments
        )
        if is_publication and not injected:
            injected = True
            repo.git_bytes(
                "notes",
                "--ref=refs/notes/review",
                "add",
                "--no-stripspace",
                "-F",
                "-",
                target,
                input_bytes=note_body,
            )
            raise failure
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", intercept)

    with pytest.raises(ManduaError) as error:
        repo.service().annotate(_annotation(target), apply=True)

    assert injected is True
    assert error.value.code in {ErrorCode.GIT_FAILURE, ErrorCode.POLICY_VIOLATION}
    assert error.value.__cause__ is failure
    assert error.value.recovery is not None
    assert _note(repo, target) == note_body.decode()


def test_oversized_review_note_never_escapes_the_query_output_bound(repo) -> None:
    """This fails if untrusted note content can bypass the configured Git output limit."""
    repo.write("knowledge/rules.md", "Water in the morning.\n")
    target = repo.commit("Record the morning rule")
    repo.git(
        "notes",
        "--ref=refs/notes/review",
        "add",
        "-m",
        "x" * 2_000,
        target,
    )
    service = MemoryService.open(
        repo.path,
        limits=QueryLimits(max_output_bytes=1_024, max_excerpt_chars=100),
    )

    with pytest.raises(ManduaError) as error:
        service.why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert error.value.code is ErrorCode.LIMIT_EXCEEDED


def test_malformed_missing_note_diagnostic_is_a_lookup_failure(repo, monkeypatch) -> None:
    """This fails if arbitrary Git failure output is accepted as an ordinary missing note."""
    baseline = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Baseline review.", baseline)
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    target = repo.commit("Record a target without a note")
    original = GitRunner.run_text

    def malformed_missing_note(self, arguments, **kwargs):
        if arguments[:1] == ["notes"] and "show" in arguments:
            return GitOutput(stdout="", stderr="unexpected diagnostic\n", returncode=1)
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", malformed_missing_note)

    status = RepositoryInspector(repo.path).review_note_status(target)

    assert status.ref_available is True
    assert status.lookup_failed is True


@pytest.mark.parametrize("invalid_limit", (0, -1, True, 1.5, 1_025))
def test_review_note_output_limit_must_be_a_positive_configured_integer(
    repo, invalid_limit
) -> None:
    """This fails if an aggregate caller can widen or disable the note byte bound."""
    inspector = RepositoryInspector(repo.path, limits=QueryLimits(max_output_bytes=1_024))

    with pytest.raises(ManduaError) as error:
        inspector.review_note_status("HEAD", max_output_bytes=invalid_limit)

    assert error.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    "unsafe_character",
    (
        "\u0085",
        "\u0378",
        "\u200e",
        "\u2028",
        "\u2029",
        "\u202e",
        "\u2066",
        "\ue000",
        "\ufeff",
    ),
)
def test_annotation_rejects_unicode_controls_formats_and_line_separators(
    repo, unsafe_character
) -> None:
    """This fails if Unicode can create hidden direction changes or logical note lines."""
    with pytest.raises(ManduaError) as message_error:
        repo.service().annotate(
            AnnotationRequest("HEAD", f"Review{unsafe_character}text.", "reviewer")
        )
    with pytest.raises(ManduaError) as agent_error:
        repo.service().annotate(
            AnnotationRequest("HEAD", "Review text.", f"reviewer{unsafe_character}id")
        )

    assert message_error.value.code is ErrorCode.VALIDATION_FAILED
    assert agent_error.value.code is ErrorCode.VALIDATION_FAILED


def test_annotation_reports_temporary_index_creation_failure_without_cleanup_uncertainty(
    repo, monkeypatch
) -> None:
    """This fails if NoteWriter rewrites an index setup failure as uncertain cleanup."""
    service = repo.service()
    original = git_runner_module._PrivateDirectory

    def fail_index_creation(*args, **kwargs):
        if kwargs.get("prefix") == "mandua-git-index-":
            raise OSError("injected index creation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(git_runner_module, "_PrivateDirectory", fail_index_creation)

    with pytest.raises(ManduaError) as error:
        service.annotate(_annotation("HEAD"), apply=True)

    assert error.value.code is ErrorCode.GIT_FAILURE
    assert error.value.message == "The temporary Git index could not be created."
    assert error.value.recovery is None


def test_decision_review_evidence_respects_aggregate_count_and_byte_bounds(
    repo, monkeypatch
) -> None:
    """This fails if each matching commit can add another independently bounded note."""
    targets: list[str] = []
    for number in range(6):
        repo.write(f"knowledge/decision-{number}.md", f"Decision {number}.\n")
        target = repo.commit(
            f"Record bounded decision {number}\n\n"
            "Memory-Type: decision\n"
            "Scope: irrigation\n"
            "Decision-ID: DEC-NOTE-BOUNDS\n"
            "Agent-ID: planner"
        )
        targets.append(target)
        repo.git(
            "notes",
            "--ref=refs/notes/review",
            "add",
            "-m",
            f"Review {number}: " + "x" * 320,
            target,
        )
    limits = QueryLimits(
        max_commits=20,
        max_output_bytes=6_000,
        max_excerpt_chars=300,
    )
    original = RepositoryInspector.review_note_status
    note_budgets: list[int] = []
    note_timeouts: list[float] = []

    def observe_aggregate_budget(self, revision, **kwargs):
        note_budgets.append(kwargs["max_output_bytes"])
        note_timeouts.append(kwargs["timeout_seconds"])
        return original(self, revision, **kwargs)

    monkeypatch.setattr(RepositoryInspector, "review_note_status", observe_aggregate_budget)

    result = MemoryService.open(repo.path, limits=limits).decision("DEC-NOTE-BOUNDS", limit=8)
    serialized = render_json(result).encode()
    commit_evidence = tuple(item for item in result.evidence if item.kind == "decision-commit")

    assert {item.oid for item in commit_evidence} == set(targets)
    assert len(result.evidence) <= 8
    assert len(serialized) <= limits.max_output_bytes
    assert any("review note evidence" in warning.lower() for warning in result.warnings)
    assert len(note_budgets) == 2
    assert note_budgets[1] < note_budgets[0]
    assert note_timeouts[1] <= note_timeouts[0]


def test_annotated_tag_notes_ref_is_consistently_unavailable(repo) -> None:
    """This fails if history peels a tag that note lookup correctly rejects as non-direct."""
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    target = repo.commit("Record the reviewed dawn rule")
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Reviewed.", target)
    notes_commit = repo.git("rev-parse", "refs/notes/review").stdout.strip()
    repo.git("tag", "-a", "notes-wrapper", "-m", "Wrap the notes commit", notes_commit)
    tag_oid = repo.git("rev-parse", "refs/tags/notes-wrapper").stdout.strip()
    repo.git("update-ref", "refs/notes/review", tag_oid)
    repo.git("tag", "-d", "notes-wrapper")

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert result.history_scope.notes_available is False
    assert all(item.kind != "review-note" for item in result.evidence)
    assert any("lookup failed" in warning.lower() for warning in result.warnings)


def test_annotation_works_from_a_linked_worktree(repo, tmp_path) -> None:
    """This fails if notes resolve .git only as a directory instead of supporting gitfiles."""
    linked_path = tmp_path / "linked"
    repo.git("worktree", "add", "-b", "review-worktree", str(linked_path))
    target = repo.git("rev-parse", "HEAD").stdout.strip()

    result = MemoryService.open(linked_path).annotate(_annotation(target), apply=True)

    assert result.applied is True
    assert "Review confirms" in _note(repo, target)


def test_annotation_supports_sha256_repositories_when_git_supports_them(tmp_path) -> None:
    """This fails if annotation assumes SHA-1 widths for targets or notes refs."""
    path = tmp_path / "sha256-notes"
    init = subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main", str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if init.returncode != 0:
        pytest.skip("This Git build does not support SHA-256 repositories.")
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Mandu'a Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@mandua.invalid"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "Initial commit"],
        check=True,
        capture_output=True,
    )
    target = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "notes",
            "--ref=refs/notes/review",
            "add",
            "-m",
            "Existing SHA-256 review.",
            target,
        ],
        check=True,
        capture_output=True,
    )

    result = MemoryService.open(path).annotate(_annotation(target), apply=True)
    note = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "notes",
            "--ref=refs/notes/review",
            "show",
            target,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert len(target) == 64
    assert result.changes[0].after_oid is not None
    assert len(result.changes[0].after_oid) == 64
    assert note.count("Existing SHA-256 review.") == 1
    assert note.count("Review confirms the recorded rule.") == 1


def test_annotation_rejects_a_sha256_commit_disguised_as_a_note_blob(tmp_path) -> None:
    """This fails if actual object-type validation assumes SHA-1 batch records."""
    path = tmp_path / "sha256-malformed-notes"
    init = subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main", str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if init.returncode != 0:
        pytest.skip("This Git build does not support SHA-256 repositories.")
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Mandu'a Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@mandua.invalid"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "Initial commit"],
        check=True,
        capture_output=True,
    )
    target = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    note_blob = subprocess.run(
        ["git", "-C", str(path), "hash-object", "-w", "--stdin"],
        check=True,
        capture_output=True,
        input=b"Existing valid SHA-256 review.\n",
    ).stdout.strip()
    tree_body = b"".join(
        b"100644 " + name + b"\x00" + object_id
        for name, object_id in sorted(
            (
                (b"malformed-sentinel", bytes.fromhex(target)),
                (target.encode("ascii"), bytes.fromhex(note_blob.decode("ascii"))),
            )
        )
    )
    tree_oid = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "hash-object",
            "-t",
            "tree",
            "--literally",
            "-w",
            "--stdin",
        ],
        check=True,
        capture_output=True,
        input=tree_body,
    ).stdout.strip()
    notes_commit = subprocess.run(
        ["git", "-C", str(path), "commit-tree", tree_oid.decode("ascii"), "-m", "Malformed"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(path), "update-ref", "refs/notes/review", notes_commit],
        check=True,
    )

    with pytest.raises(ManduaError) as error:
        MemoryService.open(path).annotate(_annotation(target), apply=True)

    assert error.value.code is ErrorCode.GIT_FAILURE
    after = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "refs/notes/review"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert after == notes_commit


@pytest.mark.parametrize(
    "failure_type",
    (_InjectedNoteControl, KeyboardInterrupt, SystemExit),
    ids=("custom-base", "keyboard-interrupt", "system-exit"),
)
def test_notes_preserves_direct_observer_control_after_real_publication(repo, failure_type) -> None:
    """A completed publication observer must not turn interpreter control into success."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_ref = "refs/notes/review"
    failure = failure_type("direct notes completion interruption")
    completed_updates = []
    fired = False

    def interrupt_completed_update(event) -> None:
        nonlocal fired
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", notes_ref)
        ):
            completed_updates.append(event)
            if not fired:
                fired = True
                raise failure

    observed: BaseException | None = None
    result = None
    with _observe_git_processes(interrupt_completed_update):
        try:
            result = NoteWriter(repo.path, QueryLimits()).annotate(_annotation(target), apply=True)
        except BaseException as error:  # noqa: BLE001 - direct identity is the contract
            observed = error

    assert fired is True
    assert len(completed_updates) == 1
    candidate_oid = completed_updates[0].arguments[3]
    assert repo.git("rev-parse", notes_ref).stdout.strip() == candidate_oid
    assert _note(repo, target).count("Review confirms the recorded rule.") == 1
    assert result is None
    assert observed is failure


def test_notes_persistent_same_instance_observer_interruption_is_not_applied(
    repo,
) -> None:
    """Repeated observer control flow must end in bounded uncertainty with its identity as cause."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_ref = "refs/notes/review"
    failure = _InjectedNoteControl("persistent notes completion interruption")
    completed_updates = []
    emergency_reads = []
    mutation_completed = False

    def persistently_interrupt(event) -> None:
        nonlocal mutation_completed
        is_update = (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", notes_ref)
        )
        is_emergency_read = (
            mutation_completed
            and event.phase == "completed"
            and event.arguments[:4] == ("rev-parse", "--verify", "--quiet", "--end-of-options")
            and event.arguments[-1:] == (notes_ref,)
        )
        if is_update:
            completed_updates.append(event)
            mutation_completed = True
            raise failure
        if is_emergency_read:
            emergency_reads.append(event)
            raise failure

    observed: BaseException | None = None
    with _observe_git_processes(persistently_interrupt):
        try:
            NoteWriter(repo.path, QueryLimits()).annotate(_annotation(target), apply=True)
        except BaseException as error:  # noqa: BLE001 - uncertainty chaining is asserted
            observed = error

    assert len(completed_updates) == 1
    assert emergency_reads
    candidate_oid = completed_updates[0].arguments[3]
    assert repo.git("rev-parse", notes_ref).stdout.strip() == candidate_oid
    assert _note(repo, target).count("Review confirms the recorded rule.") == 1
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__ is failure


def test_notes_rejected_publication_preserves_the_failed_reread_error_by_identity(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An emergency old-state proof must re-raise the original rejected-path read failure."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_ref = "refs/notes/review"
    head_before = repo.git("rev-parse", "HEAD").stdout
    index_before = repo.git("write-tree").stdout
    status_before = repo.git("status", "--porcelain=v2").stdout
    original_failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "injected failed normal reread after rejected notes publication",
    )
    writer = NoteWriter(repo.path, QueryLimits())
    original = writer._runner.run_text
    rejected = False
    normal_read_failed = False
    publication_calls = 0

    def reject_then_interrupt_normal_read(arguments, **kwargs):
        nonlocal rejected, normal_read_failed, publication_calls
        if arguments[:3] == ["update-ref", "--no-deref", notes_ref] and not rejected:
            publication_calls += 1
            candidate_oid = arguments[3]
            expected_oid = arguments[4]
            repo.git("update-ref", notes_ref, candidate_oid, expected_oid)
            output = original(arguments, **kwargs)
            assert output.returncode != 0
            repo.git("update-ref", "-d", notes_ref, candidate_oid)
            rejected = True
            return output
        output = original(arguments, **kwargs)
        if (
            rejected
            and not normal_read_failed
            and arguments[:4] == ["rev-parse", "--verify", "--quiet", "--end-of-options"]
            and arguments[-1:] == [notes_ref]
        ):
            assert output.returncode == 1 and not output.stdout and not output.stderr
            normal_read_failed = True
            raise original_failure
        return output

    monkeypatch.setattr(writer._runner, "run_text", reject_then_interrupt_normal_read)
    observed: BaseException | None = None
    try:
        writer.annotate(_annotation(target), apply=True)
    except BaseException as error:  # noqa: BLE001 - original identity is the contract
        observed = error

    assert rejected is True
    assert normal_read_failed is True
    assert publication_calls == 1
    assert repo.git("show-ref", "--verify", notes_ref, check=False).returncode != 0
    assert repo.git("rev-parse", "HEAD").stdout == head_before
    assert repo.git("write-tree").stdout == index_before
    assert repo.git("status", "--porcelain=v2").stdout == status_before
    assert observed is original_failure


@pytest.mark.parametrize(
    "root_name",
    ("worktree", "git-directory", "common-directory", "object-directory"),
)
def test_notes_emergency_reconciliation_rejects_a_replaced_physical_authority_root(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_name: str
) -> None:
    """Publication proof must not rediscover a path-equivalent repository replacement."""
    linked = (tmp_path / f"notes-authority-{root_name}").resolve()
    branch = f"authority/notes-{root_name}"
    repo.git("worktree", "add", "-b", branch, str(linked), "HEAD")
    roots = _notes_authority_roots(linked)
    target_root = roots[root_name]
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    notes_ref = "refs/notes/review"
    failure = _InjectedNoteControl(f"replaced notes {root_name}")
    writer = NoteWriter(linked, QueryLimits())
    original = writer._runner.run_text
    displaced: Path | None = None
    candidate_oid: str | None = None

    def publish_replace_then_interrupt(arguments, **kwargs):
        nonlocal displaced, candidate_oid
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", notes_ref] and displaced is None:
            assert output.returncode == 0 and not output.stdout and not output.stderr
            candidate_oid = arguments[3]
            displaced = _replace_notes_authority_root(target_root)
            raise failure
        return output

    monkeypatch.setattr(writer._runner, "run_text", publish_replace_then_interrupt)
    observed: BaseException | None = None
    try:
        try:
            writer.annotate(_annotation(target), apply=True)
        except BaseException as error:  # noqa: BLE001 - uncertainty is asserted below
            observed = error
    finally:
        if displaced is not None:
            _restore_notes_authority_root(target_root, displaced)

    assert candidate_oid is not None
    assert repo.git("rev-parse", notes_ref).stdout.strip() == candidate_oid
    assert _note(repo, target).count("Review confirms the recorded rule.") == 1
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__ is failure


@pytest.mark.parametrize(
    "root_name",
    ("worktree", "git-directory", "common-directory", "object-directory"),
)
@pytest.mark.parametrize("replacement_boundary", ("before-first-object", "after-capture"))
def test_round3_notes_use_bound_physical_authority_for_candidate_objects(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_name: str,
    replacement_boundary: str,
) -> None:
    """Hash/index/tree/commit preparation must not run against a replacement layout."""
    linked = (tmp_path / f"round3-notes-{replacement_boundary}-{root_name}").resolve()
    branch = f"round3/notes-{replacement_boundary}-{root_name}"
    repo.git("worktree", "add", "-b", branch, str(linked), "HEAD")
    roots = _notes_authority_roots(linked)
    target_root = roots[root_name]
    writer = NoteWriter(linked, QueryLimits())
    original_open = GitRunner.open_repository_authority
    original_build = writer._build_candidate
    failure = _InjectedNoteControl(
        f"notes candidate reached replaced {root_name} at {replacement_boundary}"
    )
    displaced: Path | None = None
    captured_authorities = []
    captured_descriptors: list[int] = []
    post_replacement_mutators = []
    descriptors_closed = False

    def track_authority_open(runner, *args, **kwargs):
        nonlocal displaced
        authority = original_open(runner, *args, **kwargs)
        captured_authorities.append(authority)
        captured_descriptors.extend(authority.file_descriptors)
        if replacement_boundary == "after-capture" and displaced is None:
            displaced = _replace_notes_authority_root(target_root)
        return authority

    def replace_then_build_candidate(prepared):
        nonlocal displaced
        if replacement_boundary == "before-first-object" and displaced is None:
            displaced = _replace_notes_authority_root(target_root)
        original_build(prepared)
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
    monkeypatch.setattr(writer, "_build_candidate", replace_then_build_candidate)
    observed: BaseException | None = None
    try:
        with _observe_git_processes(observe_process):
            try:
                writer.annotate(_annotation("HEAD"), apply=True)
            except BaseException as error:  # noqa: BLE001 - authority/control outcome is asserted
                observed = error
        descriptors_closed = bool(captured_descriptors) and all(
            _notes_descriptor_is_closed(descriptor) for descriptor in captured_descriptors
        )
    finally:
        if displaced is not None:
            _restore_notes_authority_root(target_root, displaced)

    assert displaced is not None
    assert captured_authorities, "notes authority must precede candidate object creation"
    assert not post_replacement_mutators
    assert descriptors_closed is True
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert repo.git("rev-parse", "--verify", "refs/notes/review", check=False).returncode != 0


def test_round3_notes_cleanup_reconciliation_warning_names_uncertainty_and_action(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applied publication after authority cleanup failure needs a specific recovery warning."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    notes_ref = "refs/notes/review"
    original_cleanup = GitRepositoryAuthority.cleanup
    cleanup_calls = 0
    completed_updates = []

    def cleanup_then_report_failure(authority) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        original_cleanup(authority)
        if cleanup_calls == 1:
            raise ManduaError(
                ErrorCode.GIT_FAILURE,
                "Injected review-note authority cleanup failure.",
                recovery=f"Inspect {notes_ref} before another annotation.",
            )

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", notes_ref)
        ):
            completed_updates.append(event)

    monkeypatch.setattr(GitRepositoryAuthority, "cleanup", cleanup_then_report_failure)
    with _observe_git_processes(observe_process):
        result = repo.service().annotate(_annotation(target), apply=True)

    warning = " ".join(result.warnings).casefold()
    assert len(completed_updates) == 1
    assert cleanup_calls >= 2
    assert result.applied is True
    assert "cleanup" in warning
    assert "uncertain" in warning or "inspect" in warning
    assert notes_ref in " ".join(result.warnings) or "review notes" in warning
    assert _note(repo, target).startswith("Review confirms")


def test_round3_notes_emergency_budget_accepts_large_packed_authority_and_long_ref(
    repo, tmp_path: Path
) -> None:
    """Fresh publication proof must size valid authority metadata and cumulative argv."""
    notes_ref = _round3_long_notes_ref()
    assert len(notes_ref) > 512
    assert repo.git("check-ref-format", notes_ref).returncode == 0
    repo.write(".mandua.toml", f'[repository]\nnotes_ref = "{notes_ref}"\n')
    target = repo.commit("Configure the long bounded review-notes ref")
    packed = _round3_pack_large_notes_metadata(repo)
    linked = (tmp_path / "round3-notes-emergency").resolve()
    repo.git("worktree", "add", "-b", "round3/notes-emergency-linked", str(linked), "HEAD")
    limits = QueryLimits(
        max_excerpt_chars=4_096,
        max_input_chars=4_096,
        timeout_seconds=10.0,
    )
    failure = _InjectedNoteControl("large-authority notes mutation interruption")
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
            and event.arguments[:3] == ("update-ref", "--no-deref", notes_ref)
        ):
            completed_updates.append(event)
            if not interrupted:
                interrupted = True
                raise failure

    observed: BaseException | None = None
    with _observe_git_processes(interrupt_completed_update):
        try:
            NoteWriter(linked, limits).annotate(_annotation(target), apply=True)
        except BaseException as error:  # noqa: BLE001 - exact control identity is asserted
            observed = error

    assert packed.stat().st_size > 512
    assert interrupted is True
    assert len(completed_updates) == 1
    assert _round3_notes_planned_argv_bytes(considered) > 4_096
    assert len(considered) <= 96
    assert 1 <= len(emergency_considered) <= 8
    assert observed is failure
    assert repo.git("rev-parse", notes_ref).returncode == 0
    assert _note(repo, target, ref=notes_ref).startswith("Review confirms")
