"""Configuration validation tests for repository policy."""

from __future__ import annotations

import math
import os
import time
from pathlib import PurePosixPath

import pytest

from mandua.config import load_worktree_policy, parse_repository_policy
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitOutput
from mandua.models import QueryLimits


def test_configuration_defaults_are_immutable_and_use_knowledge_root(repo) -> None:
    """This fails if an absent configuration does not produce safe immutable defaults."""
    policy = load_worktree_policy(repo.path, QueryLimits())

    assert policy.canonical_branch == "main"
    assert policy.notes_ref == "refs/notes/review"
    assert policy.knowledge_roots == (PurePosixPath("knowledge"),)
    assert policy.invariants == ()


@pytest.mark.parametrize(
    "source",
    (
        "unknown = true\n",
        "[repository]\nunknown = true\n",
        "[repository]\nmax_file_bytes = true\n",
        '[repository]\nknowledge_roots = ["knowledge", 1]\n',
        '[repository]\ncanonical_branch = "main..unsafe"\n',
        '[repository]\ncanonical_branch = ".hidden"\n',
        '[repository]\ncanonical_branch = "-unsafe"\n',
        '[repository]\ncanonical_branch = "main\\u0001unsafe"\n',
        '[repository]\nnotes_ref = "refs/heads/main"\n',
        '[repository]\nknowledge_roots = ["../outside"]\n',
        '[repository]\nknowledge_roots = ["knowledge/.Git/private"]\n',
        '[[invariants]]\nkind = "python"\npath = "knowledge/rules.json"\nfield = "id"\n',
        '[[invariants]]\nkind = "unique-json-field"\npath = "knowledge/.git/rules.json"\nfield = "id"\n',
        '[[invariants]]\nkind = "unique-json-field"\npath = "knowledge/rules.json"\nfield = ""\n',
    ),
)
def test_configuration_rejects_unknown_or_unsafe_schema_values(source: str) -> None:
    """This fails if malformed configuration can broaden the policy surface."""
    with pytest.raises(ManduaError) as caught:
        parse_repository_policy(source.encode("utf-8"), QueryLimits())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_worktree_configuration_rejects_an_equal_size_in_place_read_race(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if configuration bytes can be hybrid-read after an in-place rewrite."""
    config = repo.path / ".mandua.toml"
    config.write_text('[repository]\ncanonical_branch = "main"\n', encoding="utf-8")
    original_read = os.read
    changed = False

    def mutate_after_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        value = original_read(descriptor, count)
        if not changed:
            changed = True
            config.write_text('[repository]\ncanonical_branch = "mint"\n', encoding="utf-8")
        return value

    monkeypatch.setattr("mandua.config.os.read", mutate_after_read)
    with pytest.raises(ManduaError) as caught:
        load_worktree_policy(repo.path, QueryLimits())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_tree_configuration_uses_one_absolute_deadline_for_all_git_reads() -> None:
    """This fails if each config ls-tree, size, and blob call receives a fresh budget."""

    class DelayedRunner:
        def run(self, arguments, *, timeout_seconds=None):
            time.sleep(0.02)
            return GitOutput(
                stdout=b"100644 blob 0123456789012345678901234567890123456789\t.mandua.toml\x00",
                stderr=b"",
                returncode=0,
            )

        def run_text(self, arguments, *, timeout_seconds=None):
            time.sleep(0.02)
            return GitOutput(stdout="1", stderr="", returncode=0)

        def show_blob(self, object_id, path, *, timeout_seconds=None):
            time.sleep(0.02)
            return GitOutput(stdout=b"\n", stderr=b"", returncode=0)

    from mandua.config import load_tree_policy

    with pytest.raises(ManduaError) as caught:
        load_tree_policy(
            DelayedRunner(),
            "0" * 40,
            QueryLimits(timeout_seconds=0.05),
            deadline=time.monotonic() + 0.05,
        )

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_absent_tree_configuration_rechecks_deadline_after_default_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This fails if an absent config returns after parsing defaults without a deadline check."""

    class EmptyRunner:
        def run(self, arguments, *, timeout_seconds=None):
            return GitOutput(stdout=b"", stderr=b"", returncode=0)

    def delayed_defaults(contents: bytes, limits: QueryLimits):
        time.sleep(0.02)
        from mandua.models import RepositoryPolicy

        return RepositoryPolicy()

    monkeypatch.setattr("mandua.config.parse_repository_policy", delayed_defaults)
    from mandua.config import load_tree_policy

    with pytest.raises(ManduaError) as caught:
        load_tree_policy(
            EmptyRunner(),
            "0" * 40,
            QueryLimits(timeout_seconds=0.01),
            deadline=time.monotonic() + 0.01,
        )

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


@pytest.mark.parametrize("timeout_seconds", (0.0, math.nan, math.inf))
def test_tree_configuration_rejects_an_explicit_invalid_compatibility_timeout(
    timeout_seconds: float,
) -> None:
    """This fails if an explicit timeout silently falls back to the configured default."""

    class EmptyRunner:
        def run(self, arguments, *, timeout_seconds=None):
            return GitOutput(stdout=b"", stderr=b"", returncode=0)

    from mandua.config import load_tree_policy

    with pytest.raises(ManduaError) as caught:
        load_tree_policy(EmptyRunner(), "0" * 40, QueryLimits(), timeout_seconds=timeout_seconds)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_configuration_deduplicates_roots_and_exact_invariants() -> None:
    """This fails if repeated declarative paths create divergent policy state."""
    policy = parse_repository_policy(
        b"[repository]\n"
        b'knowledge_roots = ["knowledge", "knowledge", "reference"]\n'
        b"[[invariants]]\n"
        b'kind = "unique-json-field"\npath = "knowledge/rules.json"\nfield = "id"\n'
        b"[[invariants]]\n"
        b'kind = "unique-json-field"\npath = "knowledge/rules.json"\nfield = "id"\n',
        QueryLimits(),
    )

    assert policy.knowledge_roots == (PurePosixPath("knowledge"), PurePosixPath("reference"))
    assert len(policy.invariants) == 1


def test_worktree_configuration_does_not_follow_a_symlink_outside_repository(
    repo, tmp_path
) -> None:
    """This fails if a worktree policy can be read from outside the repository."""
    outside = tmp_path / "outside.toml"
    outside.write_text('[repository]\ncanonical_branch = "other"\n', encoding="utf-8")
    os.symlink(outside, repo.path / ".mandua.toml")

    with pytest.raises(ManduaError) as caught:
        load_worktree_policy(repo.path, QueryLimits())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_worktree_configuration_normalizes_encoding_and_size_errors(repo) -> None:
    """This fails if invalid or oversized configuration reaches the TOML parser unbounded."""
    config = repo.path / ".mandua.toml"
    config.write_bytes(b"\xff")

    with pytest.raises(ManduaError) as encoding_error:
        load_worktree_policy(repo.path, QueryLimits())

    assert encoding_error.value.code is ErrorCode.VALIDATION_FAILED
    assert "\ufffd" not in encoding_error.value.message

    config.write_bytes(b"x" * 33)
    with pytest.raises(ManduaError) as size_error:
        load_worktree_policy(repo.path, QueryLimits(max_output_bytes=32))

    assert size_error.value.code is ErrorCode.VALIDATION_FAILED
