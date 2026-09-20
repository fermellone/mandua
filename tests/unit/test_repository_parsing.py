"""Repository inspector tests against disposable Git repositories."""

from __future__ import annotations

import math

import pytest
from helpers.repo import RepoBuilder

from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitOutput, GitRunner
from mandua.memory_service import MemoryService
from mandua.models import QueryLimits
from mandua.repository import RepositoryInspector


def test_recovery_commit_probe_rejects_stderr_even_when_batch_output_says_missing(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if corrupt-object diagnostics are relabeled as an expected missing object."""
    oid = "a" * 40
    inspector = RepositoryInspector(repo.path)
    monkeypatch.setattr(
        inspector._runner,
        "run_text",
        lambda *_args, **_kwargs: GitOutput(
            stdout=f"{oid} missing\n",
            stderr="error: inflate: data stream error\n",
            returncode=0,
        ),
    )

    with pytest.raises(ManduaError) as caught:
        inspector.recovery_commit_available(oid, timeout_seconds=1.0)

    assert caught.value.code is ErrorCode.GIT_FAILURE


@pytest.mark.parametrize("oid", ("a" * 40, "b" * 64))
def test_signature_status_classifies_full_width_missing_commit_objects(
    repo, monkeypatch: pytest.MonkeyPatch, oid: str
) -> None:
    """This fails if verify exit status is mistaken for proof that a missing commit is unsigned."""
    inspector = RepositoryInspector(repo.path)
    calls: list[tuple[str, ...]] = []

    def missing_probe(arguments, **_kwargs):
        calls.append(tuple(arguments))
        return GitOutput(stdout=f"{oid} missing\n", stderr="", returncode=0)

    def unverifiable(arguments, **_kwargs):
        calls.append(tuple(arguments))
        return GitOutput(
            stdout=b"", stderr=f"error: {oid}: unable to read file.\n".encode(), returncode=1
        )

    monkeypatch.setattr(
        inspector._runner,
        "run_text",
        missing_probe,
    )
    monkeypatch.setattr(inspector._runner, "run", unverifiable)

    with pytest.raises(ManduaError) as caught:
        inspector.signature_status(oid)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == oid
    assert calls == [
        ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
        ("verify-commit", "--raw", "--end-of-options", oid),
        ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
    ]


@pytest.mark.parametrize("oid", ("c" * 40, "d" * 64))
def test_signature_status_parses_present_commits_after_a_fail_closed_verifier_boundary(
    repo, monkeypatch: pytest.MonkeyPatch, oid: str
) -> None:
    """This fails if a repository-selected signature helper can run after object classification."""
    inspector = RepositoryInspector(repo.path)
    probe_calls: list[tuple[str, ...]] = []
    verification_calls: list[tuple[str, ...]] = []

    def classified(arguments, **_kwargs):
        probe_calls.append(tuple(arguments))
        return GitOutput(stdout=f"{oid} commit\n", stderr="", returncode=0)

    def unverifiable(arguments, **_kwargs):
        verification_calls.append(tuple(arguments))
        return GitOutput(stdout=b"", stderr=b"unverifiable", returncode=1)

    monkeypatch.setattr(inspector._runner, "run_text", classified)
    monkeypatch.setattr(inspector._runner, "run", unverifiable)

    assert inspector.signature_status(oid) is False
    assert verification_calls == [("verify-commit", "--raw", "--end-of-options", oid)]
    assert probe_calls == [
        ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
        ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
    ]


@pytest.mark.parametrize(
    ("oid", "diagnostic"),
    (
        ("e" * 40, "error: {oid}: unable to read file.\n"),
        ("f" * 64, "fatal: corrupt object {oid}\n"),
    ),
)
def test_signature_status_retains_exact_bounded_missing_diagnostics_across_restoration(
    repo, monkeypatch: pytest.MonkeyPatch, oid: str, diagnostic: str
) -> None:
    """This fails if an exact verifier missing diagnostic is discarded after restoration."""
    limits = QueryLimits(max_output_bytes=321)
    inspector = RepositoryInspector(repo.path, limits=limits)
    probe_calls: list[tuple[str, ...]] = []
    verification_kwargs: list[dict[str, object]] = []

    def present(arguments, **_kwargs):
        probe_calls.append(tuple(arguments))
        return GitOutput(stdout=f"{oid} commit\n", stderr="", returncode=0)

    def temporarily_missing(_arguments, **kwargs):
        verification_kwargs.append(kwargs)
        return GitOutput(
            stdout=b"",
            stderr=diagnostic.format(oid=oid).encode(),
            returncode=1,
        )

    monkeypatch.setattr(inspector._runner, "run_text", present)
    monkeypatch.setattr(inspector._runner, "run", temporarily_missing)

    with pytest.raises(ManduaError) as caught:
        inspector.signature_status(oid)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == oid
    assert len(probe_calls) == 2
    assert verification_kwargs[0]["max_output_bytes"] == limits.max_output_bytes
    assert verification_kwargs[0]["isolated_configuration"] is True


def test_signature_status_does_not_match_an_oid_embedded_in_a_longer_hex_token(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if substring ambiguity turns an unrelated verifier diagnostic into missing."""
    oid = "a" * 40
    inspector = RepositoryInspector(repo.path)
    monkeypatch.setattr(
        inspector._runner,
        "run_text",
        lambda *_args, **_kwargs: GitOutput(stdout=f"{oid} commit\n", stderr="", returncode=0),
    )
    monkeypatch.setattr(
        inspector._runner,
        "run",
        lambda *_args, **_kwargs: GitOutput(
            stdout=b"",
            stderr=f"error: f{oid}f: unable to read file.\n".encode(),
            returncode=1,
        ),
    )

    assert inspector.signature_status(oid) is False


def test_signature_status_keeps_a_present_non_commit_distinct_from_missing(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if an exact type mismatch is mislabeled as an unsigned or missing commit."""
    oid = "b" * 40
    inspector = RepositoryInspector(repo.path)
    probe_calls: list[tuple[str, ...]] = []

    def blob_probe(arguments, **_kwargs):
        probe_calls.append(tuple(arguments))
        return GitOutput(stdout=f"{oid} blob\n", stderr="", returncode=0)

    monkeypatch.setattr(inspector._runner, "run_text", blob_probe)
    monkeypatch.setattr(
        inspector._runner,
        "run",
        lambda *_args, **_kwargs: GitOutput(
            stdout=b"",
            stderr=f"error: {oid}: cannot verify a non-tag object of type blob.\n".encode(),
            returncode=1,
        ),
    )

    with pytest.raises(ManduaError) as caught:
        inspector.signature_status(oid)

    assert caught.value.code is ErrorCode.INVALID_REVISION
    assert len(probe_calls) == 2


def test_status_reports_a_detached_head_without_inventing_a_branch(repo) -> None:
    """This fails if detached repositories are represented as an attached branch."""
    commit = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout(commit)

    status = RepositoryInspector(repo.path).status()

    assert status.detached is True
    assert status.branch is None
    assert status.history_scope.end_oid == commit
    assert status.history_scope.refs[0] == "HEAD"
    assert status.worktrees == (str(repo.path),)


def test_status_omits_an_unresolvable_upstream(repo) -> None:
    """This fails if a configured but absent remote branch is reported as resolved."""
    repo.git("remote", "add", "origin", repo.path.as_uri())
    repo.git("config", "branch.main.remote", "origin")
    repo.git("config", "branch.main.merge", "refs/heads/main")

    status = RepositoryInspector(repo.path).status()

    assert status.upstream is None
    assert status.upstream_oid is None


def test_status_reports_an_unborn_repository_without_a_history_endpoint(tmp_path) -> None:
    """This fails if an unborn HEAD is fabricated as a commit history endpoint."""
    unborn = RepoBuilder.create_unborn(tmp_path / "unborn")

    result = unborn.service().status()

    assert result.history_scope.end_oid is None
    assert result.history_scope.start_oid is None
    assert result.history_scope.commit_count == 0


def test_status_discloses_file_protocol_shallow_history(repo, tmp_path) -> None:
    """This fails if a shallow clone is treated as complete repository history."""
    repo.write("history.txt", "one\n")
    repo.commit("Add first history entry")
    repo.write("history.txt", "two\n")
    repo.commit("Add second history entry")
    shallow = repo.clone_shallow_to(tmp_path / "shallow", depth=1)

    result = shallow.service().status()

    assert result.history_scope.shallow is True
    assert result.history_scope.commit_count == 1
    assert "History is shallow; earlier commits may be unavailable." in result.warnings


def test_status_emits_conflict_evidence_for_an_unmerged_path(repo) -> None:
    """This fails if unmerged porcelain records are hidden from the status result."""
    repo.write("knowledge/conflict.md", "Base\n")
    base = repo.commit("Add merge fixture")
    repo.checkout_new("feature", base)
    repo.write("knowledge/conflict.md", "Feature\n")
    repo.commit("Change fixture on feature")
    repo.checkout("main")
    repo.write("knowledge/conflict.md", "Main\n")
    repo.commit("Change fixture on main")
    repo.git("merge", "feature", check=False)

    result = repo.service().status()

    assert any(
        item.path == "knowledge/conflict.md" and item.details["state"] == "conflicted"
        for item in result.evidence
    )


def test_status_bounds_untrusted_paths_and_excerpts(repo) -> None:
    """This fails if a path from Git can exceed the configured result limits."""
    path = f"knowledge/{'x' * 80}.md"
    repo.write(path, "Bounded path\n")

    result = MemoryService.open(repo.path, limits=QueryLimits(max_excerpt_chars=20)).status()
    evidence = next(item for item in result.evidence if item.details["state"] == "untracked")

    assert evidence.path is not None and len(evidence.path) <= 20
    assert evidence.excerpt is not None and len(evidence.excerpt) <= 20


def test_status_reports_the_bounded_reachable_history_scope(repo) -> None:
    """This fails if the history count hides truncation or reports an unbounded start."""
    repo.write("history.txt", "one\n")
    repo.commit("Add first history entry")
    repo.write("history.txt", "two\n")
    repo.commit("Add second history entry")
    expected_start = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("history.txt", "three\n")
    repo.commit("Add third history entry")

    result = MemoryService.open(repo.path, limits=QueryLimits(max_commits=2)).status()

    assert result.history_scope.refs == ("HEAD", "refs/heads/main")
    assert result.history_scope.commit_count == 2
    assert result.history_scope.truncated is True
    assert result.history_scope.start_oid == expected_start


def test_status_discloses_failed_history_access_without_an_object_id_in_stderr(repo) -> None:
    """This fails if a failed traversal is represented as a complete empty history."""
    repo.checkout_new("missing")
    repo.write("missing.md", "Missing object\n")
    missing_commit = repo.commit("Add a soon-missing object")
    repo.checkout("main")
    object_path = repo.path / ".git" / "objects" / missing_commit[:2] / missing_commit[2:]
    object_path.unlink()

    result = repo.service().status()

    assert "History access failed; the reported scope may be incomplete." in result.warnings
    assert "History traversal failed before a complete scope could be established." in result.gaps


def test_status_reports_copy_records_as_staged_with_copy_metadata(repo) -> None:
    """This fails if porcelain copy records are mislabeled as renames."""
    source = "".join(f"Copy this content {number}\n" for number in range(20))
    repo.write("knowledge/source.md", source)
    repo.commit("Add copy source")
    repo.write("knowledge/copied.md", source)
    repo.write("knowledge/source.md", f"{source}Updated source\n")
    repo.git("add", "knowledge/source.md", "knowledge/copied.md")
    repo.git("config", "status.renames", "copies")

    result = repo.service().status()
    copied = next(item for item in result.evidence if item.path == "knowledge/copied.md")

    assert copied.details["state"] == "staged"
    assert copied.details["copy_from"] == "knowledge/source.md"


def test_status_evidence_ids_do_not_collide_for_truncated_paths(repo) -> None:
    """This fails if evidence identity is derived only from a truncated path prefix."""
    prefix = f"knowledge/{'x' * 60}"
    repo.write(f"{prefix}-one.md", "First\n")
    repo.write(f"{prefix}-two.md", "Second\n")

    result = MemoryService.open(repo.path, limits=QueryLimits(max_excerpt_chars=20)).status()
    evidence = [item for item in result.evidence if item.details["state"] == "untracked"]

    assert len(evidence) == 2
    assert evidence[0].path == evidence[1].path
    assert evidence[0].id != evidence[1].id


def test_history_scope_uses_ref_snapshot_object_ids_after_refs_move(repo) -> None:
    """This fails if the bounded traversal reuses a live ref after its count probe."""
    initial = repo.git("rev-parse", "HEAD").stdout.strip()

    class RefMovingGitRunner(GitRunner):
        moved = False

        def run_text(
            self,
            arguments: list[str],
            *,
            check: bool = True,
            timeout_seconds: float | None = None,
            input_bytes: bytes = b"",
            max_output_bytes: int | None = None,
        ):
            output = super().run_text(
                arguments,
                check=check,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
                max_output_bytes=max_output_bytes,
            )
            if arguments[:2] == ["rev-list", "--count"] and not self.moved:
                self.moved = True
                repo.write("later.md", "A later commit\n")
                repo.commit("Move main during inspection")
            return output

    inspector = RepositoryInspector(repo.path, limits=QueryLimits(max_commits=1))
    inspector._runner = RefMovingGitRunner(repo.path, limits=QueryLimits(max_commits=1))

    status = inspector.status()

    assert status.history_scope.end_oid == initial
    assert status.history_scope.commit_count == 1
    assert status.history_scope.start_oid == initial


@pytest.mark.parametrize(
    "payload",
    [b"A\0path", b"BOGUS\nSTATUS\0path\0", b"R100\0old-path\0"],
)
def test_comparison_name_status_rejects_corrupted_nul_framing(repo, payload: bytes) -> None:
    """This fails if malformed name-status bytes become fabricated state evidence."""
    with pytest.raises(ManduaError) as caught:
        RepositoryInspector(repo.path)._parse_comparison_name_status(payload)

    assert caught.value.code is ErrorCode.GIT_FAILURE


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"0" * 40 + b" missing\n",
        b"0" * 40 + b" commit not-a-size\n",
        b"\xff" + b"0" * 40 + b" commit 0\n\n",
    ],
)
def test_comparison_shallow_batch_parser_rejects_malformed_git_records(
    repo, payload: bytes
) -> None:
    """This fails if malformed cat-file output can silently claim complete shallow history."""
    with pytest.raises(ManduaError) as caught:
        RepositoryInspector._parse_batched_commit_objects(payload, ("0" * 40,))

    assert caught.value.code is ErrorCode.GIT_FAILURE


def test_comparison_shallow_parser_rejects_a_commit_without_a_valid_tree_header(repo) -> None:
    """This fails if malformed raw headers can be mistaken for a complete root commit."""
    with pytest.raises(ManduaError) as caught:
        RepositoryInspector._raw_commit_has_parent(b"tree\n\n")

    assert caught.value.code is ErrorCode.GIT_FAILURE


@pytest.mark.parametrize("invalid_timeout", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_repository_inspector_rejects_nonfinite_and_nonpositive_timeouts(
    repo, invalid_timeout: float
) -> None:
    """This fails if inspector-level deadlines bypass the GitRunner timeout contract."""
    with pytest.raises(ManduaError) as caught:
        RepositoryInspector(repo.path).resolve_commit("HEAD", timeout_seconds=invalid_timeout)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
