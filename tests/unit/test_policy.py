"""Worktree policy tests."""

from __future__ import annotations

import os
import time
from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitOutput
from mandua.models import QueryLimits, RepositoryPolicy, UniqueJsonFieldInvariant
from mandua.policy import Policy


def test_public_policy_models_are_frozen_and_use_tuple_defaults() -> None:
    """This fails if callers can mutate a policy after it has been opened."""
    policy = RepositoryPolicy()
    invariant = UniqueJsonFieldInvariant(PurePosixPath("knowledge/rules.json"), "id")

    assert isinstance(policy.knowledge_roots, tuple)
    assert isinstance(policy.invariants, tuple)
    with pytest.raises(FrozenInstanceError):
        policy.canonical_branch = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        invariant.field = "slug"  # type: ignore[misc]


@pytest.mark.parametrize(
    "path",
    (
        PurePosixPath("knowledge2/rules.md"),
        PurePosixPath("knowledge/../outside.md"),
        PurePosixPath("knowledge/.git/config"),
        PurePosixPath("/knowledge/rules.md"),
        PurePosixPath("."),
    ),
)
def test_policy_rejects_paths_outside_component_aware_knowledge_roots(
    repo, path: PurePosixPath
) -> None:
    """This fails if a string-prefix or traversal check admits a non-knowledge path."""
    policy = Policy.open(repo.path)

    with pytest.raises(ManduaError) as caught:
        policy.validate_path(path)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_policy_rejects_case_insensitive_git_path_components(repo) -> None:
    """This fails if .Git can bypass policy on a case-insensitive filesystem."""
    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_path(PurePosixPath("knowledge/.Git/config"))

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_policy_builds_one_structurally_valid_message(repo) -> None:
    """This fails if policy validation drifts from the shared message builder."""
    message = Policy.open(repo.path).validate_message(
        "Record the irrigation rule",
        reason="The sensor calibration is complete.",
        memory_type="decision",
        scope="irrigation",
        task_id="TASK-1",
        agent_id="gardener",
    )

    assert message == (
        "Record the irrigation rule\n\n"
        "Reason: The sensor calibration is complete.\n\n"
        "Memory-Type: decision\nScope: irrigation\nTask-ID: TASK-1\nAgent-ID: gardener"
    )


@pytest.mark.parametrize("subject", ("", "Two\nlines", "x" * 73))
def test_policy_rejects_invalid_commit_subjects(repo, subject: str) -> None:
    """This fails if generated Git subjects can become ambiguous or overlong."""
    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_message(subject, memory_type="decision")

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_policy_uses_builder_to_reject_trailer_injection(repo) -> None:
    """This fails if a trailer value can add an unvalidated line to a generated message."""
    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_message(
            "Record a policy", memory_type="decision", scope="safe\nCorrects: injected"
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_selected_files_reject_secrets_and_do_not_echo_values(repo) -> None:
    """This fails if a selected secret is accepted or exposed in an error message."""
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    repo.write("knowledge/token.txt", f"api_key = '{secret}'\n")

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_selected_files((PurePosixPath("knowledge/token.txt"),))

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert secret not in caught.value.message


@pytest.mark.parametrize(
    "content",
    (
        "-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----\n",
        "credential = AKIA1234567890ABCDEF\n",
    ),
)
def test_selected_files_reject_basic_secret_markers(repo, content: str) -> None:
    """This fails if private-key or AWS access-key material bypasses the basic barrier."""
    repo.write("knowledge/credential.txt", content)

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_selected_files((PurePosixPath("knowledge/credential.txt"),))

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


@pytest.mark.parametrize(
    "content",
    (
        'token = "https://example.invalid/a path?token=abc-def_1234567890"\n',
        "secret: 'contraseña con espacios y símbolos ñ 1234567890'\n",
        "api_key=https://example.invalid/a/b?x=1234567890&y=abcdef\n",
    ),
)
def test_selected_files_reject_quoted_unicode_and_url_secret_assignments(
    repo, content: str
) -> None:
    """This fails if realistic assigned secret values bypass the basic detector."""
    repo.write("knowledge/credential.txt", content)

    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path).validate_selected_files((PurePosixPath("knowledge/credential.txt"),))

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_selected_files_reject_symlink_directory_and_excess_size(repo, tmp_path) -> None:
    """This fails if selected content can escape the worktree or bypass byte limits."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (repo.path / "knowledge").mkdir()
    os.symlink(outside, repo.path / "knowledge" / "link.txt")
    repo.write("knowledge/large.txt", "x" * 33)
    repo.write(".mandua.toml", "[repository]\nmax_file_bytes = 32\n")

    output_limit = max(32, len(str(repo.path).encode("utf-8")) + 1)
    policy = Policy.open(repo.path, limits=QueryLimits(max_output_bytes=output_limit))
    for path in (
        PurePosixPath("knowledge/link.txt"),
        PurePosixPath("knowledge"),
        PurePosixPath("knowledge/large.txt"),
    ):
        with pytest.raises(ManduaError) as caught:
            policy.validate_selected_files((path,))
        assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_missing_selected_path_is_safe_and_reserved_for_future_deletions(repo) -> None:
    """This fails if a future deletion causes Policy to read a missing external path."""
    Policy.open(repo.path).validate_selected_files((PurePosixPath("knowledge/deleted.md"),))


def test_selected_file_rejects_an_equal_size_in_place_read_race(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if selected content is accepted after a same-size in-place rewrite."""
    path = repo.path / "knowledge" / "rule.txt"
    repo.write("knowledge/rule.txt", "allow\n")
    policy = Policy.open(repo.path)
    original_read = os.read
    changed = False

    def mutate_after_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        value = original_read(descriptor, count)
        if not changed:
            changed = True
            path.write_text("deny!\n", encoding="utf-8")
        return value

    monkeypatch.setattr("mandua.policy.os.read", mutate_after_read)
    with pytest.raises(ManduaError) as caught:
        policy.validate_selected_files((PurePosixPath("knowledge/rule.txt"),))

    assert caught.value.code is ErrorCode.POLICY_VIOLATION


def test_policy_rejects_surrogate_path_and_message_field_before_external_use(repo) -> None:
    """This fails if non-UTF-8 public text reaches filesystem or Git boundaries."""
    policy = Policy.open(repo.path)
    for action in (
        lambda: policy.validate_path(PurePosixPath("knowledge/\ud800.txt")),
        lambda: policy.validate_message("Record a rule", memory_type="decision", scope="\ud800"),
    ):
        with pytest.raises(ManduaError) as caught:
            action()
        assert caught.value.code in {ErrorCode.POLICY_VIOLATION, ErrorCode.VALIDATION_FAILED}


def test_policy_open_normalizes_an_unavailable_repository(tmp_path) -> None:
    """This fails if an unavailable repository escapes as a raw filesystem exception."""
    with pytest.raises(ManduaError) as caught:
        Policy.open(tmp_path / "does-not-exist")

    assert caught.value.code is ErrorCode.INVALID_REPOSITORY


def test_policy_open_rejects_a_fake_git_marker(tmp_path) -> None:
    """This fails if the presence of a .git file is mistaken for a real worktree."""
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".git").write_text("not a gitdir\n", encoding="utf-8")

    with pytest.raises(ManduaError) as caught:
        Policy.open(fake)

    assert caught.value.code is ErrorCode.INVALID_REPOSITORY


def test_policy_open_preserves_the_callers_output_limit(repo) -> None:
    """This fails if the worktree proof silently widens the configured Git output bound."""
    with pytest.raises(ManduaError) as caught:
        Policy.open(repo.path, limits=QueryLimits(max_output_bytes=1))

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_exact_tree_probe_rejects_a_sha256_prefix_before_resolution(repo) -> None:
    """This fails if a SHA-256 repository accepts a 40-hex abbreviated tree ID."""

    class Sha256Runner:
        def run_text(self, arguments, **kwargs):
            return GitOutput(stdout="sha256\n", stderr="", returncode=0)

    policy = Policy(repo.path, Sha256Runner(), RepositoryPolicy(), QueryLimits())
    with pytest.raises(ManduaError) as caught:
        policy._verify_exact_tree("a" * 40, time.monotonic() + 1)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_exact_tree_probe_treats_exit_zero_stderr_as_git_failure(repo) -> None:
    """This fails if batch diagnostics are trusted merely because the exit code is zero."""

    class DiagnosticRunner:
        def run_text(self, arguments, **kwargs):
            if arguments[0] == "rev-parse":
                return GitOutput(stdout="sha1\n", stderr="", returncode=0)
            return GitOutput(stdout="a" * 40 + " tree", stderr="corrupt object", returncode=0)

    policy = Policy(repo.path, DiagnosticRunner(), RepositoryPolicy(), QueryLimits())
    with pytest.raises(ManduaError) as caught:
        policy._verify_exact_tree("a" * 40, time.monotonic() + 1)

    assert caught.value.code is ErrorCode.GIT_FAILURE


def test_object_format_probe_treats_exit_zero_stderr_as_git_failure(repo) -> None:
    """This fails if object-format diagnostics are trusted before checking exact output."""

    class DiagnosticRunner:
        calls = 0

        def run_text(self, arguments, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return GitOutput(stdout="sha1\n", stderr="warning", returncode=0)
            raise AssertionError("The batch probe must not run after format diagnostics.")

    policy = Policy(repo.path, DiagnosticRunner(), RepositoryPolicy(), QueryLimits())
    with pytest.raises(ManduaError) as caught:
        policy._verify_exact_tree("a" * 40, time.monotonic() + 1)

    assert caught.value.code is ErrorCode.GIT_FAILURE
