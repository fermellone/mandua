"""Real-Git policy tests for proposed trees."""

from __future__ import annotations

import os
from pathlib import PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.models import QueryLimits
from mandua.policy import Policy, _parse_tree_records


def _write_invariant_config(repo) -> None:
    repo.write(
        ".mandua.toml",
        "[[invariants]]\n"
        'kind = "unique-json-field"\n'
        'path = "knowledge/irrigation-rules.json"\n'
        'field = "id"\n',
    )


def test_policy_rejects_secret_material_and_duplicate_semantic_ids(repo) -> None:
    """This fails if proposed tree validation skips secret scans or semantic uniqueness."""
    repo.write("knowledge/token.txt", "api_key = 'abcdefghijklmnopqrstuvwxyz123456'\n")
    policy = Policy.open(repo.path)

    with pytest.raises(ManduaError) as secret:
        policy.validate_selected_files((PurePosixPath("knowledge/token.txt"),))

    assert secret.value.code is ErrorCode.POLICY_VIOLATION

    _write_invariant_config(repo)
    repo.write(
        "knowledge/irrigation-rules.json",
        '[{"id":"IRR-1","duration":12},{"id":"IRR-1","duration":20}]\n',
    )
    repo.git("add", ".mandua.toml", "knowledge/irrigation-rules.json")
    tree = repo.git("write-tree").stdout.strip()
    policy = Policy.open(repo.path)

    with pytest.raises(ManduaError) as duplicate:
        policy.validate_tree(tree)

    assert duplicate.value.code is ErrorCode.POLICY_VIOLATION
    assert "duplicate value IRR-1" in duplicate.value.message


def test_tree_policy_comes_from_the_exact_proposed_tree_not_worktree(repo) -> None:
    """This fails if merge validation reads a modified worktree configuration instead of its tree."""
    policy = Policy.open(repo.path)
    _write_invariant_config(repo)
    repo.write("knowledge/irrigation-rules.json", '[{"id":"IRR-1"},{"id":"IRR-1"}]\n')
    repo.git("add", ".mandua.toml", "knowledge/irrigation-rules.json")
    tree = repo.git("write-tree").stdout.strip()
    repo.write(".mandua.toml", "not-a-real-option = true\n")

    with pytest.raises(ManduaError) as duplicate:
        policy.validate_tree(tree)

    assert duplicate.value.code is ErrorCode.POLICY_VIOLATION
    assert "duplicate value IRR-1" in duplicate.value.message


@pytest.mark.parametrize(
    "document",
    (
        "not-json\n",
        "{}\n",
        "[[]]\n",
        "[{}]\n",
        '[{"id":""}]\n',
    ),
)
def test_tree_invariant_rejects_malformed_missing_or_empty_json_values(repo, document: str) -> None:
    """This fails if declarative invariants accept malformed semantic records."""
    _write_invariant_config(repo)
    repo.write("knowledge/irrigation-rules.json", document)
    repo.git("add", ".mandua.toml", "knowledge/irrigation-rules.json")
    tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_tree(tree)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_tree_rejects_symlink_and_gitlink_under_a_knowledge_root(repo, tmp_path) -> None:
    """This fails if a proposed tree can validate non-regular knowledge objects."""
    (repo.path / "knowledge").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    os.symlink(outside, repo.path / "knowledge" / "linked.txt")
    repo.git("add", "knowledge/linked.txt")
    symlink_tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as symlink:
        Policy.open(repo.path).validate_tree(symlink_tree)

    assert symlink.value.code is ErrorCode.POLICY_VIOLATION

    repo.git("rm", "--cached", "knowledge/linked.txt")
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{head},knowledge/module")
    gitlink_tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as gitlink:
        Policy.open(repo.path).validate_tree(gitlink_tree)

    assert gitlink.value.code is ErrorCode.POLICY_VIOLATION


def test_tree_rejects_missing_or_non_tree_object_ids(repo) -> None:
    """This fails if validate_tree accepts a revision expression or missing object as a tree."""
    policy = Policy.open(repo.path)
    for tree in ("HEAD", "f" * 40):
        with pytest.raises(ManduaError) as caught:
            policy.validate_tree(tree)
        assert caught.value.code in {ErrorCode.VALIDATION_FAILED, ErrorCode.GIT_FAILURE}


def test_tree_rejects_exact_commit_and_missing_object_ids_as_invalid_trees(repo) -> None:
    """This fails if a supplied commit is peeled into a tree or absence becomes a Git error."""
    policy = Policy.open(repo.path)
    commit = repo.git("rev-parse", "HEAD").stdout.strip()
    for object_id in (commit, "f" * len(commit)):
        with pytest.raises(ManduaError) as caught:
            policy.validate_tree(object_id)
        assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_empty_proposed_tree_uses_default_policy_without_malformed_record_error(repo) -> None:
    """This fails if a valid empty Git tree is confused with malformed ls-tree output."""
    empty_tree = repo.git("write-tree").stdout.strip()

    Policy.open(repo.path).validate_tree(empty_tree)


@pytest.mark.parametrize(
    "document",
    (
        '[{"id":"IRR-1","id":"IRR-2"}]\n',
        '[{"id":1e10000}]\n',
    ),
)
def test_tree_invariant_uses_strict_json_parsing(repo, document: str) -> None:
    """This fails if duplicate object keys or non-finite numbers alter invariant meaning."""
    _write_invariant_config(repo)
    repo.write("knowledge/irrigation-rules.json", document)
    repo.git("add", ".mandua.toml", "knowledge/irrigation-rules.json")
    tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_tree(tree)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_tree_does_not_echo_a_duplicate_value_that_looks_secret(repo) -> None:
    """This fails if invariant diagnostics disclose arbitrary duplicated JSON values."""
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    _write_invariant_config(repo)
    repo.write(
        "knowledge/irrigation-rules.json",
        f'[{{"id":"{secret}"}},{{"id":"{secret}"}}]\n',
    )
    repo.git("add", ".mandua.toml", "knowledge/irrigation-rules.json")
    tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_tree(tree)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert secret not in caught.value.message


def test_tree_path_records_respect_the_callers_input_limit(repo) -> None:
    """This fails if a long tree path bypasses Policy limits before blob access."""
    path = "knowledge/" + "x" * 55
    repo.write(path, "bounded\n")
    repo.git("add", path)
    tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path, limits=QueryLimits(max_input_chars=64)).validate_tree(tree)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_tree_rejects_case_insensitive_git_path_components(repo) -> None:
    """This fails if tree parsing accepts .Git paths on case-insensitive filesystems."""
    with pytest.raises(ManduaError) as caught:
        _parse_tree_records(
            b"100644 blob 0123456789012345678901234567890123456789\tknowledge/.Git/config\x00",
            QueryLimits(),
        )

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_tree_uses_one_aggregate_deadline_across_many_blobs(repo) -> None:
    """This fails if every tree blob receives a fresh timeout budget."""
    for index in range(80):
        repo.write(f"knowledge/rules-{index}.txt", "bounded\n")
    repo.git("add", "knowledge")
    tree = repo.git("write-tree").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path, limits=QueryLimits(timeout_seconds=0.05)).validate_tree(tree)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
