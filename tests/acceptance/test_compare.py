"""Acceptance tests for bounded comparison of repository hypotheses."""

from __future__ import annotations

import hashlib
import math
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.memory_service import MemoryService
from mandua.models import HistoryScope, QueryLimits
from mandua.queries.comparison import compare_result
from mandua.repository import RepositoryInspector


def test_compare_reports_merge_base_exclusive_commits_and_equivalent_patches(repo) -> None:
    """This fails if replayed changes are not reported as inferred correspondence."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/sensor", start=base)
    repo.write("knowledge/rules.md", "Irrigate below 35% moisture.\n")
    left = repo.commit("Use a moisture threshold")

    repo.checkout_new("hypothesis/replayed", start=base)
    repo.write("knowledge/context.md", "Calibration complete.\n")
    repo.commit("Record calibration")
    repo.write("knowledge/rules.md", "Irrigate below 35% moisture.\n")
    right = repo.commit("Replay the moisture-threshold change")

    result = repo.service().compare("hypothesis/sensor", "hypothesis/replayed")

    assert any(item.kind == "merge-base" and item.oid == base for item in result.evidence)
    assert any(item.kind == "left-only" and item.oid == left for item in result.evidence)
    assert any(
        item.kind == "patch-correspondence"
        and item.details == {"left_oid": left, "right_oid": right}
        for item in result.evidence
    )
    assert result.inferred
    assert "does not establish commit identity or intent" in result.inferred[0].text


def test_compare_reports_bounded_state_stat_and_nul_safe_name_status(repo) -> None:
    """This fails if state differences lose paths that contain whitespace or newlines."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/left", start=base)
    repo.write("knowledge/left only\nrule.md", "Left state\n")
    repo.commit("Add a left-only state file")
    repo.checkout_new("hypothesis/right", start=base)
    repo.write("knowledge/right only\nrule.md", "Right state\n")
    repo.commit("Add a right-only state file")

    result = repo.service().compare("hypothesis/left", "hypothesis/right")

    assert any(item.kind == "state-stat" and item.excerpt for item in result.evidence)
    changed_paths = {item.path for item in result.evidence if item.kind == "state-name-status"}
    assert changed_paths == {"knowledge/left only\\nrule.md", "knowledge/right only\\nrule.md"}


def test_compare_preserves_distinct_invalid_path_bytes_in_state_evidence(repo) -> None:
    """This fails if replacement decoding collapses distinct repository paths."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    left_path = b"knowledge/invalid-\xff.md"
    right_path = b"knowledge/invalid-\xfe.md"
    repo.checkout_new("hypothesis/left", start=base)
    _commit_raw_path(repo, left_path, "Record an invalid left path")
    repo.checkout_new("hypothesis/right", start=base)
    _commit_raw_path(repo, right_path, "Record an invalid right path")

    result = repo.service().compare("hypothesis/left", "hypothesis/right")

    names = [item for item in result.evidence if item.kind == "state-name-status"]
    assert {item.path for item in names} == {
        "knowledge/invalid-\\xff.md",
        "knowledge/invalid-\\xfe.md",
    }
    assert {item.details["old_path_bytes"] for item in names if item.details["old_path"]} == {
        left_path.hex()
    }
    assert {item.details["new_path_bytes"] for item in names if item.details["new_path"]} == {
        right_path.hex()
    }


def test_compare_bounds_long_path_identity_without_ambiguous_hex(repo) -> None:
    """This fails if a bounded state path loses its full byte identity."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    raw_path = b"knowledge/" + (b"a" * 210) + b".md"
    repo.checkout_new("hypothesis/left", start=base)
    _commit_raw_path(repo, raw_path, "Record a long left path")
    repo.checkout_new("hypothesis/right", start=base)

    result = MemoryService.open(repo.path, limits=QueryLimits(max_excerpt_chars=30)).compare(
        "hypothesis/left", "hypothesis/right"
    )

    deletion = next(item for item in result.evidence if item.kind == "state-name-status")
    assert deletion.details["old_path_bytes_truncated"] is True
    assert deletion.details["old_path_bytes_sha256"] == hashlib.sha256(raw_path).hexdigest()
    assert len(deletion.details["old_path_bytes_prefix"]) % 2 == 0
    assert bytes.fromhex(deletion.details["old_path_bytes_prefix"])
    assert deletion.details["new_path"] is None
    assert deletion.details["new_path_bytes"] is None


def test_compare_restricts_histories_state_and_patch_matching_to_the_requested_path(repo) -> None:
    """This fails if a path scope is applied to only one comparison phase."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/left", start=base)
    repo.write("knowledge/in-scope.md", "Shared scoped rule\n")
    left = repo.commit("Add the scoped rule")
    repo.write("knowledge/out-of-scope.md", "Left private note\n")
    repo.commit("Add a left private note")

    repo.checkout_new("hypothesis/right", start=base)
    repo.write("knowledge/in-scope.md", "Shared scoped rule\n")
    right = repo.commit("Replay the scoped rule")
    repo.write("knowledge/out-of-scope.md", "Right private note\n")
    repo.commit("Add a right private note")

    result = repo.service().compare(
        "hypothesis/left", "hypothesis/right", path=PurePosixPath("knowledge/in-scope.md")
    )

    assert [item.oid for item in result.evidence if item.kind == "left-only"] == [left]
    assert [item.oid for item in result.evidence if item.kind == "right-only"] == [right]
    assert [item.path for item in result.evidence if item.kind == "state-name-status"] == []
    assert any(
        item.kind == "patch-correspondence"
        and item.details == {"left_oid": left, "right_oid": right}
        for item in result.evidence
    )


def test_compare_handles_identical_tips_without_fabricating_exclusive_history(repo) -> None:
    """This fails if a same-tip comparison invents a branch-only difference."""
    tip = repo.git("rev-parse", "HEAD").stdout.strip()

    result = repo.service().compare("main", "main")

    assert any(item.kind == "merge-base" and item.oid == tip for item in result.evidence)
    assert not [item for item in result.evidence if item.kind in {"left-only", "right-only"}]
    assert not [item for item in result.evidence if item.kind == "patch-correspondence"]


def test_compare_rejects_unrelated_histories_without_a_fabricated_merge_base(repo) -> None:
    """This fails if disconnected histories are represented as an ordinary comparison."""
    repo.git("checkout", "--orphan", "unrelated")
    repo.write("knowledge/unrelated.md", "Disconnected history\n")
    repo.commit("Create unrelated history")

    with pytest.raises(ManduaError) as caught:
        repo.service().compare("main", "unrelated")

    assert caught.value.code is ErrorCode.CONFLICT


def test_compare_ignores_an_unrelated_shallow_boundary_when_classifying_roots(repo) -> None:
    """This fails if an unrelated shallow ref turns complete roots into incomplete history."""
    left = _orphan_commit(repo, "left", "knowledge/left.md", "Left root\n", "Create left root")
    right = _orphan_commit(repo, "right", "knowledge/right.md", "Right root\n", "Create right root")
    repo.checkout("main")
    repo.write("knowledge/third.md", "Third history\n")
    third = repo.commit("Create unrelated third history")
    (repo.path / ".git" / "shallow").write_text(third + "\n", encoding="ascii")

    with pytest.raises(ManduaError) as caught:
        repo.service().compare(left, right)

    assert caught.value.code is ErrorCode.CONFLICT


def test_compare_ignores_a_missing_object_only_reachable_from_an_unrelated_ref(repo) -> None:
    """This fails if a third branch's missing ancestor changes complete-root classification."""
    left = _orphan_commit(repo, "left", "knowledge/left.md", "Left root\n", "Create left root")
    right = _orphan_commit(repo, "right", "knowledge/right.md", "Right root\n", "Create right root")
    third_parent = repo.git("rev-parse", "main").stdout.strip()
    repo.checkout("main")
    repo.write("knowledge/third.md", "Third history\n")
    repo.commit("Create third history")
    (repo.path / ".git" / "objects" / third_parent[:2] / third_parent[2:]).unlink()

    with pytest.raises(ManduaError) as caught:
        repo.service().compare(left, right)

    assert caught.value.code is ErrorCode.CONFLICT


def test_compare_reports_incomplete_history_for_two_shallow_tips_with_hidden_base(
    repo, tmp_path
) -> None:
    """This fails if a shallow graph is mislabeled as a confirmed unrelated history."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("left", start=base)
    repo.write("knowledge/left.md", "Left\n")
    repo.commit("Add a left shallow tip")
    repo.checkout_new("right", start=base)
    repo.write("knowledge/right.md", "Right\n")
    repo.commit("Add a right shallow tip")
    destination = tmp_path / "shallow-comparison"
    subprocess.run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-single-branch",
            "--depth=1",
            str(repo.path),
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(destination).compare("origin/left", "origin/right")

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY
    assert caught.value.evidence[0].kind == "comparison-history-scope"
    assert caught.value.recovery == "Fetch complete history before comparing revisions."


def test_compare_reports_a_missing_shared_ancestor(repo) -> None:
    """This fails if a deleted required object becomes a false merge-base conflict."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("left", start=base)
    repo.write("knowledge/left.md", "Left\n")
    repo.commit("Add a left tip")
    repo.checkout_new("right", start=base)
    repo.write("knowledge/right.md", "Right\n")
    repo.commit("Add a right tip")
    (repo.path / ".git" / "objects" / base[:2] / base[2:]).unlink()

    with pytest.raises(ManduaError) as caught:
        repo.service().compare("left", "right")

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == base
    assert caught.value.recovery == "Restore the required object before comparing revisions."


def test_compare_checks_an_unreferenced_right_oid_for_relevant_shallow_history(repo) -> None:
    """This fails if comparison diagnostics omit a supplied right commit after its ref is deleted."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("left", start=base)
    repo.write("knowledge/left.md", "Left\n")
    repo.commit("Add a left tip")
    repo.checkout_new("right", start=base)
    repo.write("knowledge/right.md", "Right\n")
    right = repo.commit("Add a right tip")
    repo.checkout("left")
    repo.git("branch", "-D", "right")
    (repo.path / ".git" / "shallow").write_text(right + "\n", encoding="ascii")

    with pytest.raises(ManduaError) as caught:
        repo.service().compare("left", right)

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY


def test_compare_inspects_a_symlinked_shallow_boundary_without_opening_git_metadata(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if comparison bypasses the bounded Git runner to read .git/shallow."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("left", start=base)
    repo.write("knowledge/left.md", "Left\n")
    repo.commit("Add a left tip")
    repo.checkout_new("right", start=base)
    repo.write("knowledge/right.md", "Right\n")
    right = repo.commit("Add a right tip")
    shallow = repo.path / ".git" / "shallow"
    external_boundary = repo.path.parent / "external-shallow-boundary"
    external_boundary.write_text(right + "\n", encoding="ascii")
    shallow.symlink_to(external_boundary)

    def forbid_metadata_open(*_args, **_kwargs):
        raise AssertionError("comparison opened repository metadata directly")

    monkeypatch.setattr(Path, "open", forbid_metadata_open)
    monkeypatch.setattr(Path, "read_bytes", forbid_metadata_open)

    with pytest.raises(ManduaError) as caught:
        repo.service().compare("left", "right")

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY


def test_compare_fails_closed_when_shallow_boundary_inspection_exceeds_its_bound(repo) -> None:
    """This fails if later shallow boundaries are silently skipped during comparison classification."""
    left = _orphan_commit(repo, "left", "knowledge/left.md", "Left root\n", "Create left root")
    right = _orphan_commit(repo, "right", "knowledge/right.md", "Right root\n", "Create right root")
    (repo.path / ".git" / "shallow").write_text(left + "\n" + right + "\n", encoding="ascii")

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits=QueryLimits(max_commits=1)).compare(left, right)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_compare_caps_each_exclusive_history_and_discloses_truncation(repo) -> None:
    """This fails if comparison can inspect more exclusive commits than its query bound."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/left", start=base)
    for number in range(3):
        repo.write(f"knowledge/left-{number}.md", f"Left {number}\n")
        repo.commit(f"Record left change {number}")
    repo.checkout_new("hypothesis/right", start=base)
    for number in range(3):
        repo.write(f"knowledge/right-{number}.md", f"Right {number}\n")
        repo.commit(f"Record right change {number}")

    result = MemoryService.open(repo.path, limits=QueryLimits(max_commits=1)).compare(
        "hypothesis/left", "hypothesis/right", limit=10
    )

    assert len([item for item in result.evidence if item.kind == "left-only"]) == 1
    assert len([item for item in result.evidence if item.kind == "right-only"]) == 1
    assert result.history_scope.truncated is True
    assert "Comparison histories are limited by the configured commit bound." in result.warnings


@pytest.mark.parametrize(("many_side", "complete_side"), [("left", "right"), ("right", "left")])
def test_compare_discloses_which_side_was_truncated_by_the_caller_limit(
    repo, many_side: str, complete_side: str
) -> None:
    """This fails if one bounded side is mislabeled as a configured-bound truncation."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new(f"hypothesis/{many_side}", start=base)
    for number in range(2):
        repo.write(f"knowledge/{many_side}-{number}.md", f"{many_side} {number}\n")
        repo.commit(f"Record {many_side} change {number}")
    repo.checkout_new(f"hypothesis/{complete_side}", start=base)
    repo.write(f"knowledge/{complete_side}.md", f"{complete_side}\n")
    repo.commit(f"Record {complete_side} change")

    result = repo.service().compare("hypothesis/left", "hypothesis/right", limit=1)

    truncation = next(
        item for item in result.evidence if item.kind == "comparison-history-truncation"
    )
    assert truncation.details == {"side": many_side, "effective_limit": 1, "source": "caller"}
    assert (
        f"{many_side.title()} comparison history is limited by the caller result limit of 1."
        in result.warnings
    )
    assert all("configured commit bound" not in warning for warning in result.warnings)


def test_compare_keeps_duplicate_patch_ids_ambiguous(repo) -> None:
    """This fails if duplicate patch IDs are arbitrarily paired as exact correspondence."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/left", start=base)
    left = _repeat_patch(repo, 2)
    repo.checkout_new("hypothesis/right", start=base)
    right = _repeat_patch(repo, 3)

    result = repo.service().compare("hypothesis/left", "hypothesis/right")

    ambiguous = next(
        item
        for item in result.evidence
        if item.kind == "patch-correspondence-ambiguous"
        and item.details["left_count"] == 2
        and item.details["right_count"] == 3
    )
    assert set(ambiguous.details["left_oids"]) == set(left)
    assert set(ambiguous.details["right_oids"]) == set(right)
    assert not [
        item
        for item in result.evidence
        if item.kind == "patch-correspondence"
        and item.details["left_oid"] in set(left)
        and item.details["right_oid"] in set(right)
    ]
    assert "Some stable patch IDs are ambiguous and were not paired." in result.warnings


def test_compare_does_not_run_repository_diff_helpers_or_text_conversion(repo, tmp_path) -> None:
    """This fails if comparison bypasses GitRunner's safe diff protections."""
    marker = tmp_path / "diff-helper-ran"
    helper = tmp_path / "diff-helper"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    helper.chmod(0o700)
    repo.git("config", "diff.external", str(helper))
    repo.git("config", "diff.malicious.textconv", str(helper))
    repo.write(".gitattributes", "*.secret diff=malicious\n")
    base = repo.commit("Configure attributed secret files")
    repo.checkout_new("hypothesis/left", start=base)
    repo.write("knowledge/rule.secret", "Never invoke repository diff helpers.\n")
    repo.commit("Add a protected left rule")
    repo.checkout_new("hypothesis/right", start=base)
    repo.write("knowledge/rule.secret", "Never invoke repository diff helpers.\n")
    repo.commit("Replay a protected right rule")

    result = repo.service().compare("hypothesis/left", "hypothesis/right")

    assert any(item.kind == "patch-correspondence" for item in result.evidence)
    assert not marker.exists()


def test_compare_rejects_invalid_revisions_and_paths(repo) -> None:
    """This fails if unsafe revisions or paths reach Git comparison commands."""
    with pytest.raises(ManduaError) as revision:
        repo.service().compare("not-a-commit", "main")
    with pytest.raises(ManduaError) as path:
        repo.service().compare("main", "main", path=PurePosixPath("../outside.md"))

    assert revision.value.code is ErrorCode.INVALID_REVISION
    assert path.value.code is ErrorCode.INVALID_PATH


@pytest.mark.parametrize("invalid_timeout", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_compare_rejects_nonfinite_and_nonpositive_deadlines(repo, invalid_timeout: float) -> None:
    """This fails if comparison derives an unbounded or non-finite aggregate deadline."""
    with pytest.raises(ManduaError) as caught:
        compare_result(
            RepositoryInspector(repo.path),
            QueryLimits(timeout_seconds=invalid_timeout),
            "main",
            "main",
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_compare_preserves_a_deadline_failure_in_shallow_history(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a timeout is relabeled as incomplete history merely because history is shallow."""
    inspector = RepositoryInspector(repo.path)
    monkeypatch.setattr(
        inspector,
        "history_scope",
        lambda *_args, **_kwargs: HistoryScope(shallow=True),
    )

    def limit_merge_base(*_args, **_kwargs):
        raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Synthetic deadline exhaustion.")

    monkeypatch.setattr(inspector, "merge_base_or_none", limit_merge_base)

    with pytest.raises(ManduaError) as caught:
        compare_result(inspector, QueryLimits(), "main", "main")

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_compare_supports_sha256_commit_object_ids_when_git_supports_them(tmp_path) -> None:
    """This fails if full SHA-256 commit IDs are rejected by comparison validation."""
    repository = tmp_path / "sha256-repository"
    initialized = subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main", str(repository)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if initialized.returncode != 0:
        pytest.skip("The installed Git does not support SHA-256 repositories.")
    subprocess.run(["git", "config", "user.name", "Mandu'a Test"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@mandua.invalid"], cwd=repository, check=True
    )
    (repository / "knowledge").mkdir()
    (repository / "knowledge" / "rules.md").write_text("A SHA-256 rule\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "Add a SHA-256 rule"], cwd=repository, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()

    subprocess.run(["git", "checkout", "-b", "left", tip], cwd=repository, check=True)
    (repository / "knowledge" / "left.md").write_text("Left SHA-256 rule\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "Add a left SHA-256 rule"], cwd=repository, check=True)
    left = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-b", "right", tip], cwd=repository, check=True)
    (repository / "knowledge" / "right.md").write_text("Right SHA-256 rule\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "Add a right SHA-256 rule"], cwd=repository, check=True)
    right = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    (repository / ".git" / "shallow").write_text(right + "\n", encoding="ascii")

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repository).compare(left, right)

    assert len(tip) == 64
    assert len(right) == 64
    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY


def _commit_raw_path(repo, raw_path: bytes, message: str) -> str:
    blob = repo.git_bytes("hash-object", "-w", "--stdin", input_bytes=b"Raw path content\n")
    repo.git_bytes(
        "update-index",
        "--add",
        "-z",
        "--index-info",
        input_bytes=b"100644 blob " + blob.stdout.strip() + b"\t" + raw_path + b"\0",
    )
    repo.git("commit", "--no-gpg-sign", "-m", message)
    return repo.git("rev-parse", "HEAD").stdout.strip()


def _repeat_patch(repo, count: int) -> list[str]:
    repo.write("knowledge/repeated.md", "base\n")
    repo.commit("Add a repeated patch fixture")
    commits: list[str] = []
    for number in range(count):
        repo.write("knowledge/repeated.md", "changed\n")
        commits.append(repo.commit(f"Apply repeated patch {number}"))
        if number + 1 < count:
            repo.write("knowledge/repeated.md", "base\n")
            repo.commit(f"Revert repeated patch {number}")
    return commits


def _orphan_commit(repo, branch: str, path: str, content: str, message: str) -> str:
    repo.git("checkout", "--orphan", branch)
    repo.git("rm", "-rf", ".", check=False)
    repo.write(path, content)
    return repo.commit(message)
