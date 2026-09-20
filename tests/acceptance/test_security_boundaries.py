"""Adversarial boundaries for untrusted Git repositories and history data."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import stat
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path, PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRunner, _observe_git_processes
from mandua.memory_service import MemoryService
from mandua.models import CommitMetadata, IntegrationRequest, QueryLimits
from mandua.policy import Policy
from mandua.repository import RepositoryInspector


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_message"),
    (
        *(
            (field, invalid, f"The {field} query limit must be a positive integer.")
            for field in (
                "max_commits",
                "max_output_bytes",
                "max_excerpt_chars",
                "max_input_chars",
            )
            for invalid in (-1, 0, True, "1")
        ),
        (
            "timeout_seconds",
            -1.0,
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            0.0,
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            True,
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            "1.0",
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            math.nan,
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            math.inf,
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            -math.inf,
            "The timeout_seconds query limit must be finite and positive.",
        ),
    ),
)
def test_invalid_public_query_limits_are_rejected_before_any_git_process(
    repo,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_value: object,
    expected_message: str,
) -> None:
    """This fails if malformed public limits reach Git or leak a Python exception."""
    limits = replace(QueryLimits(), **{field: invalid_value})
    launches = 0

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        raise AssertionError("Git must not launch for invalid public query limits")

    monkeypatch.setattr("mandua.git_runner.subprocess.Popen", forbidden_launch)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits=limits)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.message == expected_message
    assert launches == 0


@pytest.mark.parametrize(
    ("field", "invalid_kind", "expected_message"),
    (
        (
            "timeout_seconds",
            "huge",
            "The timeout_seconds query limit must be finite and positive.",
        ),
        (
            "timeout_seconds",
            "fraction",
            "The timeout_seconds query limit must be finite and positive.",
        ),
        *(
            (
                field,
                "huge",
                f"The {field} query limit exceeds the supported maximum.",
            )
            for field in (
                "max_commits",
                "max_output_bytes",
                "max_excerpt_chars",
                "max_input_chars",
            )
        ),
    ),
    ids=(
        "huge-timeout",
        "fraction-timeout",
        "huge-max-commits",
        "huge-max-output-bytes",
        "huge-max-excerpt-chars",
        "huge-max-input-chars",
    ),
)
def test_unrepresentable_public_query_limits_are_rejected_before_any_git_process(
    repo,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_kind: str,
    expected_message: str,
) -> None:
    """This fails if a public limit reaches Git or unsafe Python representation work."""
    invalid_value = 10**10_000 if invalid_kind == "huge" else Fraction(1, 2)
    limits = replace(QueryLimits(), **{field: invalid_value})
    launches = 0

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        raise AssertionError("Git must not launch for an unrepresentable public query limit")

    monkeypatch.setattr("mandua.git_runner.subprocess.Popen", forbidden_launch)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits=limits)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.message == expected_message
    assert launches == 0


def test_non_query_limits_object_is_rejected_before_any_git_process(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if the public service reads attributes from an arbitrary limits object."""
    launches = 0

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        raise AssertionError("Git must not launch for an invalid limits object")

    monkeypatch.setattr("mandua.git_runner.subprocess.Popen", forbidden_launch)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits="invalid")  # type: ignore[arg-type]

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.message == "The query limits object is invalid."
    assert launches == 0


def _write_sentinel_program(path: Path, marker: Path, *, filter_program: bool = False) -> None:
    body = ["#!/bin/sh", f"touch {shlex.quote(os.fspath(marker))}"]
    if filter_program:
        body.append("cat")
    elif path.name.endswith("textconv"):
        body.append('cat "$1"')
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _integration_request(source: str = "hypothesis/filter") -> IntegrationRequest:
    return IntegrationRequest(
        source=source,
        target="main",
        subject="Integrate the filtered hypothesis",
        metadata=CommitMetadata(
            memory_type="integration",
            scope="garden",
            agent_id="gardener",
            task_id="TASK-SECURITY-1",
            decision_id="DEC-SECURITY-1",
            reason="The proposed history was reviewed.",
        ),
    )


def _fake_signed_commit(repo) -> str:
    """Write one syntactically signed commit without trusting a signing program."""
    tree = repo.git("write-tree").stdout.strip()
    parent = repo.git("rev-parse", "HEAD").stdout.strip()
    raw = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        "author Mandu'a Test <test@mandua.invalid> 1577836802 +0000\n"
        "committer Mandu'a Test <test@mandua.invalid> 1577836802 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n"
        " untrusted repository-controlled signature\n"
        " -----END PGP SIGNATURE-----\n"
        "\n"
        "Record a hostile declared identity\n"
        "\n"
        "Agent-ID: gardener\n"
    ).encode()
    commit = repo.git_bytes("hash-object", "-t", "commit", "-w", "--stdin", input_bytes=raw)
    oid = commit.stdout.decode("ascii").strip()
    repo.git("update-ref", "refs/heads/main", oid, parent)
    repo.git("reset", "--hard", oid)
    return oid


def test_inherited_git_configuration_is_removed_before_controlled_configuration(
    repo, tmp_path, monkeypatch
) -> None:
    marker = tmp_path / "inherited-hook-ran"
    hooks = tmp_path / "inherited-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    _write_sentinel_program(hook, marker)
    repo.git("config", "core.hooksPath", str(hooks))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", str(hook))

    result = repo.runner().run_text(
        ["commit", "--allow-empty", "--no-gpg-sign", "-m", "Exercise controlled hooks"]
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_diff_log_show_and_blame_do_not_execute_external_diff_or_textconv(repo, tmp_path) -> None:
    external_marker = tmp_path / "external-diff-ran"
    textconv_marker = tmp_path / "textconv-ran"
    external = tmp_path / "external-diff"
    textconv = tmp_path / "untrusted-textconv"
    _write_sentinel_program(external, external_marker)
    _write_sentinel_program(textconv, textconv_marker)
    repo.write(".gitattributes", "knowledge/*.md diff=untrusted\n")
    repo.write("knowledge/rules.md", "Baseline rule.\n")
    base = repo.commit("Record the baseline rule")
    repo.checkout_new("hypothesis/left", base)
    repo.write("knowledge/rules.md", "Left rule.\n")
    repo.commit("Record the left rule")
    repo.checkout_new("hypothesis/right", base)
    repo.write("knowledge/rules.md", "Right rule.\n")
    repo.commit("Record the right rule")
    repo.git("config", "diff.external", str(external))
    repo.git("config", "diff.untrusted.textconv", str(textconv))

    service = repo.service()
    service.compare("hypothesis/left", "hypothesis/right")
    service.origin("Left rule.")
    service.why(
        path=PurePosixPath("knowledge/rules.md"),
        line=1,
        revision="hypothesis/right",
    )

    assert not external_marker.exists()
    assert not textconv_marker.exists()


def test_integration_rejects_clean_and_smudge_filters_before_worktree_mutation(
    repo, tmp_path
) -> None:
    repo.write(".gitattributes", "knowledge/*.md filter=untrusted\n")
    repo.write("knowledge/rules.md", "Baseline rule.\n")
    base = repo.commit("Declare the untrusted filter attribute")
    repo.checkout_new("hypothesis/filter", base)
    repo.write("knowledge/rules.md", "Proposed rule.\n")
    repo.commit("Record the filtered hypothesis")
    repo.checkout("main")
    marker = tmp_path / "filter-ran"
    helper = tmp_path / "untrusted-filter"
    _write_sentinel_program(helper, marker, filter_program=True)
    repo.git("config", "filter.untrusted.clean", str(helper))
    repo.git("config", "filter.untrusted.smudge", str(helper))
    repo.git("config", "filter.untrusted.required", "true")
    before = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_integration_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "main").stdout.strip() == before
    assert not marker.exists()
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_leading_dash_revision_is_rejected_before_git_launch(repo, monkeypatch) -> None:
    launched = False

    def forbidden_popen(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("Git must not launch for an option-shaped revision")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    with pytest.raises(ManduaError) as caught:
        repo.runner().resolve_commit("--upload-pack=untrusted")

    assert caught.value.code is ErrorCode.INVALID_REVISION
    assert launched is False


def test_repository_paths_are_literal_bounded_and_unambiguous(repo) -> None:
    literal = PurePosixPath("knowledge/path with spaces\nand newline.md")
    repo.write(literal.as_posix(), "Literal path contents.\n")
    commit = repo.commit("Record a literal unusual path")

    result = repo.service().timeline(path=literal)

    assert [item.oid for item in result.evidence] == [commit]
    invalid = (
        PurePosixPath("../outside.md"),
        PurePosixPath("/absolute.md"),
        PurePosixPath(".git/config"),
        PurePosixPath(":(glob)knowledge/*.md"),
        PurePosixPath(":!knowledge/rules.md"),
    )
    for path in invalid:
        with pytest.raises(ManduaError) as caught:
            repo.service().timeline(path=path)
        assert caught.value.code is ErrorCode.INVALID_PATH
        with pytest.raises(ManduaError) as policy_caught:
            Policy.open(repo.path).validate_path(path)
        assert policy_caught.value.code is ErrorCode.POLICY_VIOLATION


def test_untrusted_messages_notes_blobs_refs_and_errors_are_bounded(repo) -> None:
    limits = QueryLimits(
        max_commits=10,
        max_output_bytes=32_768,
        max_excerpt_chars=48,
        max_input_chars=4_096,
    )
    long_subject = "S" * 2_000
    long_agent = "A" * 2_000
    long_path = f"knowledge/{'P' * 120}.md"
    long_branch = "r" * 120
    long_ref = f"refs/heads/{long_branch}"
    repo.write(long_path, "Needle\n" + ("B" * 8_000) + "\n")
    commit = repo.commit(long_subject, trailers=(f"Agent-ID: {long_agent}",))
    repo.git(
        "notes",
        "--ref=refs/notes/review",
        "add",
        "-m",
        "N" * 2_000,
        commit,
    )
    repo.git("branch", long_branch, commit)
    service = MemoryService.open(repo.path, limits=limits)

    def assert_bounded(value: object) -> None:
        if isinstance(value, str):
            assert len(value) <= limits.max_excerpt_chars or re.fullmatch(
                r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value
            )
        elif isinstance(value, dict):
            for nested in value.values():
                assert_bounded(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                assert_bounded(nested)

    status = service.status()
    context = service.context()
    timeline = service.timeline(path=PurePosixPath(long_path), limit=1)
    why = service.why(path=PurePosixPath(long_path), line=1)
    origin = service.origin("Needle", path=PurePosixPath(long_path))
    recovery = service.recover(query=commit, create_branch="recovery/public-preview")

    for result in (status, context, timeline, why, origin, recovery):
        encoded = json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= limits.max_output_bytes
        assert all(len(ref) <= limits.max_excerpt_chars for ref in result.history_scope.refs)
        for item in result.evidence:
            if item.excerpt is not None:
                assert len(item.excerpt) <= limits.max_excerpt_chars
            if item.ref is not None:
                assert len(item.ref) <= limits.max_excerpt_chars
            if item.path is not None:
                assert len(item.path) <= limits.max_excerpt_chars
            assert_bounded(item.details)
        assert_bounded(result.to_dict()["changes"])

    public_payload = json.dumps(
        [result.to_dict() for result in (status, context, timeline, why, origin, recovery)],
        ensure_ascii=False,
    )
    assert long_path not in public_payload
    assert long_subject not in public_payload
    for hostile_run in ("P", "S", "A", "B", "N", "r"):
        assert hostile_run * 100 not in public_payload
    assert long_ref not in public_payload
    assert status.history_scope.truncated is True
    assert timeline.history_scope.truncated is True
    assert any(
        ref.startswith("refs/heads/rr") and ref.endswith("...") for ref in status.history_scope.refs
    )
    assert "History scope is limited by configured commit or display bounds." in status.warnings

    tiny = GitRunner(repo.path, limits=QueryLimits(max_output_bytes=128, max_excerpt_chars=32))
    with pytest.raises(ManduaError) as caught:
        tiny.run_text(["log", "-1", "--format=%B"])
    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert len(caught.value.message) <= 128
    hostile_error = "E" * 2_000
    bounded_error = tiny._git_failure("log", 1, hostile_error.encode())
    error_payload = json.dumps(bounded_error.to_dict(), ensure_ascii=False)
    assert "E" * 100 not in error_payload
    assert_bounded(bounded_error.evidence[0].excerpt)


def test_missing_and_corrupt_objects_do_not_expose_unbounded_diagnostics(repo) -> None:
    repo.write("knowledge/missing.md", "Missing blob.\n")
    commit = repo.commit("Record a soon-missing blob")
    blob = repo.git("rev-parse", f"{commit}:knowledge/missing.md").stdout.strip()
    object_path = repo.path / ".git" / "objects" / blob[:2] / blob[2:]
    object_path.unlink()

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, limits=QueryLimits(max_excerpt_chars=32)).show_blob(
            commit, PurePosixPath("knowledge/missing.md")
        )

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert blob in caught.value.message
    assert len(caught.value.message) <= 160
    assert all(item.excerpt is None or len(item.excerpt) <= 32 for item in caught.value.evidence)


def test_missing_object_diagnostics_require_a_delimited_parsed_oid(repo) -> None:
    oid = "a" * 40
    embedded = f"fatal: bad object {oid}f"

    assert repo.runner()._diagnostic_names_object(embedded.encode("ascii"), oid) is False
    assert RepositoryInspector._missing_objects(embedded) == ()
    assert (
        repo.runner()._diagnostic_names_object(f"fatal: bad object {oid}".encode("ascii"), oid)
        is True
    )
    assert RepositoryInspector._missing_objects(f"fatal: bad object {oid}") == (oid,)


def test_corrupt_loose_blob_is_reported_as_one_bounded_missing_object(repo) -> None:
    repo.write("knowledge/corrupt.md", "Corrupt blob.\n")
    commit = repo.commit("Record a soon-corrupt blob")
    blob = repo.git("rev-parse", f"{commit}:knowledge/corrupt.md").stdout.strip()
    object_path = repo.path / ".git" / "objects" / blob[:2] / blob[2:]
    object_path.chmod(0o600)
    object_path.write_bytes(b"not-a-valid-zlib-object")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, limits=QueryLimits(max_excerpt_chars=24)).show_blob(
            commit, PurePosixPath("knowledge/corrupt.md")
        )

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert blob in caught.value.message
    assert len(caught.value.evidence) == 1
    assert caught.value.evidence[0].oid == blob
    assert caught.value.evidence[0].details == {"object_oid": blob}
    assert len(caught.value.evidence[0].excerpt or "") <= 24


@pytest.mark.parametrize("signature_format", ("openpgp", "x509", "ssh"))
def test_repository_signature_configuration_cannot_execute_or_forge_identity(
    repo, tmp_path, signature_format: str
) -> None:
    marker = tmp_path / "repository-verifier-ran"
    verifier = tmp_path / "repository-verifier"
    _write_sentinel_program(verifier, marker)
    verifier.write_text(
        verifier.read_text(encoding="utf-8")
        + "printf '[GNUPG:] GOODSIG 0000000000000000 hostile\\n'\n"
        + "printf '[GNUPG:] TRUST_ULTIMATE 0 pgp\\n'\n"
        + "exit 0\n",
        encoding="utf-8",
    )
    for key in (
        "gpg.program",
        "gpg.openpgp.program",
        "gpg.x509.program",
        "gpg.ssh.program",
        "gpg.ssh.defaultKeyCommand",
        "gpg.ssh.allowedSignersFile",
        "gpg.ssh.revocationFile",
    ):
        repo.git("config", "--local", key, str(verifier))
    repo.git("config", "--local", "gpg.format", signature_format)
    repo.git("config", "--local", "gpg.minTrustLevel", "undefined")
    repo.git("config", "--local", "log.showSignature", "true")
    commit = _fake_signed_commit(repo)
    observed_processes: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def observe(event) -> None:
        if event.phase == "considered":
            observed_processes.append((event.arguments, event.command))

    with _observe_git_processes(observe):
        result = repo.service().timeline(limit=1)

    identity = next(item for item in result.evidence if item.oid == commit)
    assert identity.details["agent_id"] == "gardener"
    assert identity.details["signature_verified"] is False
    assert not marker.exists()
    verification_commands = [
        command for arguments, command in observed_processes if arguments[0] == "verify-commit"
    ]
    assert len(verification_commands) == 1
    command = verification_commands[0]
    controlled = tuple(command[index + 1] for index, value in enumerate(command) if value == "-c")
    assert f"gpg.program={os.devnull}" in controlled
    assert f"gpg.openpgp.program={os.devnull}" in controlled
    assert f"gpg.x509.program={os.devnull}" in controlled
    assert f"gpg.ssh.program={os.devnull}" in controlled
    assert f"gpg.ssh.allowedSignersFile={os.devnull}" in controlled
    assert f"gpg.ssh.revocationFile={os.devnull}" in controlled
    assert "gpg.ssh.defaultKeyCommand=" in controlled
    assert "gpg.format=openpgp" in controlled
    assert "gpg.minTrustLevel=fully" in controlled
    assert "log.showSignature=false" in controlled


def test_missing_declared_identity_commit_is_not_reported_as_unsigned(repo) -> None:
    repo.write("knowledge/identity.md", "Declared identity.\n")
    commit = repo.commit("Record identity", trailers=("Agent-ID: gardener",))
    object_path = repo.path / ".git" / "objects" / commit[:2] / commit[2:]
    object_path.unlink()

    commands: list[str] = []

    def observe(event) -> None:
        if event.phase == "considered":
            commands.append(event.arguments[0])

    with _observe_git_processes(observe), pytest.raises(ManduaError) as caught:
        RepositoryInspector(repo.path).signature_status(commit)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == commit
    assert commands == ["cat-file", "verify-commit", "cat-file"]


def test_commit_deleted_between_verification_and_object_probe_is_missing(repo) -> None:
    repo.write("knowledge/raced-identity.md", "Declared identity.\n")
    commit = repo.commit("Record raced identity", trailers=("Agent-ID: gardener",))
    object_path = repo.path / ".git" / "objects" / commit[:2] / commit[2:]

    commands: list[str] = []

    def delete_during_verification(event) -> None:
        if event.phase == "considered":
            commands.append(event.arguments[0])
        if event.phase == "considered" and event.arguments[0] == "verify-commit":
            object_path.unlink()

    with _observe_git_processes(delete_during_verification), pytest.raises(ManduaError) as caught:
        RepositoryInspector(repo.path).signature_status(commit)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == commit
    assert commands == ["cat-file", "verify-commit", "cat-file"]


def test_commit_temporarily_missing_during_verification_stays_missing_after_restoration(
    repo,
) -> None:
    repo.write("knowledge/restored-identity.md", "Declared identity.\n")
    commit = repo.commit("Record restored identity", trailers=("Agent-ID: gardener",))
    object_path = repo.path / ".git" / "objects" / commit[:2] / commit[2:]
    object_bytes = object_path.read_bytes()
    object_mode = stat.S_IMODE(object_path.stat().st_mode)
    commands: list[str] = []

    def disappear_and_restore(event) -> None:
        if event.phase == "considered":
            commands.append(event.arguments[0])
        if event.phase == "considered" and event.arguments[0] == "verify-commit":
            object_path.unlink()
        if event.phase == "failed" and event.arguments[0] == "verify-commit":
            object_path.write_bytes(object_bytes)
            object_path.chmod(object_mode)

    with _observe_git_processes(disappear_and_restore), pytest.raises(ManduaError) as caught:
        RepositoryInspector(repo.path).signature_status(commit)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == commit
    assert commands == ["cat-file", "verify-commit", "cat-file"]
    assert repo.git("cat-file", "-t", commit).stdout.strip() == "commit"


def test_repeated_signature_status_reverifies_and_rechecks_commit_availability(repo) -> None:
    repo.write("knowledge/cached-identity.md", "Declared identity.\n")
    commit = repo.commit("Record cached identity", trailers=("Agent-ID: gardener",))
    inspector = RepositoryInspector(repo.path)
    verified: list[str] = []

    def observe(event) -> None:
        if event.phase == "considered" and event.arguments[0] == "verify-commit":
            verified.append(event.arguments[-1])

    with _observe_git_processes(observe):
        assert inspector.signature_status(commit) is False
        object_path = repo.path / ".git" / "objects" / commit[:2] / commit[2:]
        object_path.unlink()

        with pytest.raises(ManduaError) as caught:
            inspector.signature_status(commit)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == commit
    assert verified == [commit, commit]


def test_corrupt_declared_identity_commit_is_missing(repo) -> None:
    repo.write("knowledge/corrupt-identity.md", "Declared identity.\n")
    commit = repo.commit("Record corrupt identity", trailers=("Agent-ID: gardener",))
    object_path = repo.path / ".git" / "objects" / commit[:2] / commit[2:]
    object_path.chmod(0o600)
    object_path.write_bytes(b"not-a-valid-zlib-object")

    with pytest.raises(ManduaError) as caught:
        RepositoryInspector(repo.path).signature_status(commit)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == commit


def test_sha256_missing_declared_identity_commit_is_classified(tmp_path) -> None:
    repository = tmp_path / "sha256-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main"],
        cwd=repository,
        check=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        }
    )
    for key, value in (("user.name", "Mandu'a Test"), ("user.email", "test@mandua.invalid")):
        subprocess.run(
            ["git", "config", "--local", key, value],
            cwd=repository,
            check=True,
            capture_output=True,
            env=environment,
        )
    subprocess.run(
        ["git", "commit", "--allow-empty", "--no-gpg-sign", "-m", "Agent-ID: gardener"],
        cwd=repository,
        check=True,
        capture_output=True,
        env=environment,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    assert len(commit) == 64
    object_path = repository / ".git" / "objects" / commit[:2] / commit[2:]
    object_path.unlink()

    with pytest.raises(ManduaError) as caught:
        RepositoryInspector(repository).signature_status(commit)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == commit


def test_incomplete_history_scope_preserves_shallow_and_missing_state(repo, tmp_path) -> None:
    repo.write("knowledge/first.md", "First.\n")
    repo.commit("Record first")
    repo.write("knowledge/second.md", "Second.\n")
    repo.commit("Record second")
    shallow = repo.clone_shallow_to(tmp_path / "shallow", depth=1)

    scope = shallow.service().status().history_scope

    assert scope.shallow is True
    assert scope.commit_count == 1
    assert scope.start_oid == scope.end_oid


def test_large_history_bounds_process_output_and_result_evidence(repo) -> None:
    for number in range(25):
        repo.write("knowledge/history.md", f"Observation {number}.\n")
        repo.commit(f"Record bounded observation {number}")
    limits = QueryLimits(max_commits=7, max_output_bytes=16_384, max_excerpt_chars=40)

    result = MemoryService.open(repo.path, limits=limits).timeline(limit=100)

    assert len(result.evidence) == 7
    assert result.history_scope.truncated is True
    assert len(json.dumps(result.to_dict()).encode("utf-8")) <= limits.max_output_bytes
