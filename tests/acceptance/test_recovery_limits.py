"""Acceptance tests for bounded, local-only recovery queries."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRepositoryAuthority, _observe_git_processes
from mandua.memory_service import MemoryService
from mandua.models import QueryLimits


class _InjectedRecoveryControl(BaseException):
    """Non-Mandua control flow injected after a real recovery ref update."""


def _recovery_authority_roots(repository: Path) -> dict[str, Path]:
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


def _replace_recovery_authority_root(path: Path) -> Path:
    """Replace one authority root inode without changing its fixture namespace."""
    mode = path.stat(follow_symlinks=False).st_mode
    displaced = path.with_name(f"{path.name}.mandua-recovery-authority")
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


def _restore_recovery_authority_root(path: Path, displaced: Path) -> None:
    for child in tuple(path.iterdir()):
        child.rename(displaced / child.name)
    path.rmdir()
    displaced.rename(path)


def _round3_long_recovery_branch() -> str:
    components = "/".join(f"{index:02d}-{'r' * 72}" for index in range(7))
    return f"round3/recovery-emergency/{components}"


def _round3_pack_large_recovery_metadata(repo) -> Path:
    for index in range(12):
        repo.git("branch", f"round3-recovery-packed/{index:02d}-{'p' * 40}", "HEAD")
    repo.git("pack-refs", "--all", "--prune")
    common = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    packed = common / "packed-refs"
    assert packed.stat().st_size > 512
    return packed


def _round3_recovery_planned_argv_bytes(events) -> int:
    return sum(
        sum(len(os.fsencode(argument)) + 1 for argument in event.command)
        for event in events
        if event.phase == "considered"
    )


def test_recover_previews_then_creates_a_branch_for_an_unreachable_commit(repo) -> None:
    """This fails if recovery mutates during preview or cannot atomically create its branch."""
    repo.checkout_new("task/temporary")
    repo.write("knowledge/recovered.md", "Recoverable observation\n")
    lost_oid = repo.commit("Record a recoverable observation")
    repo.checkout("main")
    repo.git("branch", "-D", "task/temporary")

    preview = repo.service().recover(query=lost_oid, create_branch="recovery/observation")

    assert preview.applied is False
    assert preview.changes[0].action == "create-ref"
    assert preview.changes[0].target == "refs/heads/recovery/observation"
    assert preview.changes[0].after_oid == lost_oid
    assert (
        repo.git("show-ref", "--verify", "refs/heads/recovery/observation", check=False).returncode
        != 0
    )

    applied = repo.service().recover(
        query=lost_oid, create_branch="recovery/observation", apply=True
    )

    assert applied.applied is True
    assert repo.git("rev-parse", "recovery/observation").stdout.strip() == lost_oid


def test_recover_reconciles_a_terminal_observer_failure_after_real_ref_creation(repo) -> None:
    """This fails if a completed recovery update is reported only as an observer failure."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "recovery/observer-interruption"
    branch_ref = f"refs/heads/{branch}"
    failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Injected terminal observation failure after recovery ref creation.",
    )
    completed_updates = []

    def interrupt_completed_update(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
            and not completed_updates
        ):
            completed_updates.append(event)
            raise failure

    with _observe_git_processes(interrupt_completed_update):
        result = repo.service().recover(query=target, create_branch=branch, apply=True)

    assert len(completed_updates) == 1
    assert completed_updates[0].arguments[3] == target
    assert result.applied is True
    assert result.changes[0].after_oid == target
    assert any("reconciled" in warning.casefold() for warning in result.warnings)
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target


def test_recover_classifies_a_third_ref_state_after_post_update_base_exception(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if non-Mandua control flow exposes a third ref state as an ordinary abort."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    tree = repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    third_oid = repo.git(
        "commit-tree",
        tree,
        "-p",
        target,
        "-m",
        "Record a concurrent recovery branch state",
    ).stdout.strip()
    branch = "recovery/base-interruption"
    branch_ref = f"refs/heads/{branch}"
    failure = _InjectedRecoveryControl("injected after real recovery ref creation")
    service = repo.service()
    original = service._inspector._runner.run_text
    completed_updates = []
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)

    def create_race_then_interrupt(arguments, **kwargs):
        nonlocal interrupted
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", branch_ref] and not interrupted:
            interrupted = True
            assert output.returncode == 0 and not output.stdout and not output.stderr
            requested_oid = arguments[3]
            assert requested_oid == target
            assert (
                repo.git(
                    "update-ref",
                    branch_ref,
                    third_oid,
                    requested_oid,
                    check=False,
                ).returncode
                == 0
            )
            raise failure
        return output

    monkeypatch.setattr(service._inspector._runner, "run_text", create_race_then_interrupt)

    with _observe_git_processes(observe_process), pytest.raises(ManduaError) as caught:
        service.recover(query=target, create_branch=branch, apply=True)

    assert interrupted is True
    assert len(completed_updates) == 1
    assert completed_updates[0].arguments[3] == target
    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.__cause__ is failure
    assert caught.value.recovery is not None
    assert branch_ref in caught.value.recovery
    assert repo.git("rev-parse", branch_ref).stdout.strip() == third_oid


def test_recover_reconciles_exact_ref_after_primary_budget_exhaustion(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spent primary budget must not prevent proof of a completed recovery mutation."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "recovery/exhausted-primary-budget"
    branch_ref = f"refs/heads/{branch}"
    failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Injected primary-budget exhaustion after recovery ref creation.",
    )
    service = repo.service()
    original = service._inspector._runner.run_text
    completed_updates = []
    post_interruption_reads = []
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)
        if (
            interrupted
            and event.phase == "completed"
            and event.arguments[-1:] == (branch_ref,)
            and event.arguments[:4] == ("rev-parse", "--verify", "--quiet", "--end-of-options")
        ):
            post_interruption_reads.append(event)

    def create_exhaust_then_raise(arguments, **kwargs):
        nonlocal interrupted
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", branch_ref] and not interrupted:
            assert output.returncode == 0 and not output.stdout and not output.stderr
            budget = service._inspector._operation_budget
            assert budget is not None
            budget.deadline = time.monotonic() - 1
            interrupted = True
            raise failure
        return output

    monkeypatch.setattr(service._inspector._runner, "run_text", create_exhaust_then_raise)

    with _observe_git_processes(observe_process):
        result = service.recover(query=target, create_branch=branch, apply=True)

    assert interrupted is True
    assert len(completed_updates) == 1
    assert len(post_interruption_reads) == 1
    assert result.applied is True
    assert result.changes[0].after_oid == target
    assert any("reconciled" in warning.casefold() for warning in result.warnings)
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target


def test_recover_reraises_base_exception_by_identity_after_exact_ref_proof(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-Mandua control flow needs an exact reread before it can be re-raised."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "recovery/exact-base-interruption"
    branch_ref = f"refs/heads/{branch}"
    failure = _InjectedRecoveryControl("injected after exact recovery ref creation")
    service = repo.service()
    original = service._inspector._runner.run_text
    completed_updates = []
    post_interruption_reads = []
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)
        if (
            interrupted
            and event.phase == "completed"
            and event.arguments[-1:] == (branch_ref,)
            and event.arguments[:4] == ("rev-parse", "--verify", "--quiet", "--end-of-options")
        ):
            post_interruption_reads.append(event)

    def create_then_interrupt(arguments, **kwargs):
        nonlocal interrupted
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", branch_ref] and not interrupted:
            assert output.returncode == 0 and not output.stdout and not output.stderr
            interrupted = True
            raise failure
        return output

    monkeypatch.setattr(service._inspector._runner, "run_text", create_then_interrupt)

    with _observe_git_processes(observe_process), pytest.raises(_InjectedRecoveryControl) as caught:
        service.recover(query=target, create_branch=branch, apply=True)

    assert caught.value is failure
    assert interrupted is True
    assert len(completed_updates) == 1
    assert len(post_interruption_reads) == 1
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target


def test_recover_reports_uncertainty_when_emergency_budget_expires(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exhausted emergency reread must disclose uncertainty without retrying mutation."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "recovery/exhausted-emergency-budget"
    branch_ref = f"refs/heads/{branch}"
    failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Injected post-update failure before emergency-budget exhaustion.",
    )
    service = repo.service()
    original_run = service._inspector._runner.run_text
    original_replace = service._inspector._replace_operation_budget
    completed_updates = []
    replacement_calls = 0
    interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)

    def create_then_raise(arguments, **kwargs):
        nonlocal interrupted
        output = original_run(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", branch_ref] and not interrupted:
            interrupted = True
            raise failure
        return output

    def expire_second_budget(operation_budget, budget_limits) -> None:
        nonlocal replacement_calls
        replacement_calls += 1
        original_replace(operation_budget, budget_limits)
        if replacement_calls == 2:
            operation_budget.deadline = time.monotonic() - 1

    monkeypatch.setattr(service._inspector._runner, "run_text", create_then_raise)
    monkeypatch.setattr(
        service._inspector,
        "_replace_operation_budget",
        expire_second_budget,
    )

    with _observe_git_processes(observe_process), pytest.raises(ManduaError) as caught:
        service.recover(query=target, create_branch=branch, apply=True)

    assert interrupted is True
    assert replacement_calls == 2
    assert len(completed_updates) == 1
    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.__cause__ is failure
    assert caught.value.recovery is not None
    assert branch_ref in caught.value.recovery
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target


def test_recover_preserves_emergency_base_interruption_after_second_exact_proof(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot emergency interruption must be re-raised after a second bounded proof."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "recovery/emergency-base-interruption"
    branch_ref = f"refs/heads/{branch}"
    primary_failure = ManduaError(
        ErrorCode.LIMIT_EXCEEDED,
        "Injected post-update failure before emergency interruption.",
    )
    emergency_failure = _InjectedRecoveryControl("injected during emergency ref proof")
    service = repo.service()
    original = service._inspector._runner.run_text
    completed_updates = []
    completed_reads = []
    primary_interrupted = False
    emergency_interrupted = False

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[-1:] == (branch_ref,)
            and event.arguments[:4] == ("rev-parse", "--verify", "--quiet", "--end-of-options")
        ):
            completed_reads.append(event)

    def interrupt_update_then_first_emergency_read(arguments, **kwargs):
        nonlocal primary_interrupted, emergency_interrupted
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", branch_ref] and not primary_interrupted:
            primary_interrupted = True
            raise primary_failure
        if (
            primary_interrupted
            and arguments[-1:] == [branch_ref]
            and arguments[:4] == ["rev-parse", "--verify", "--quiet", "--end-of-options"]
            and not emergency_interrupted
        ):
            emergency_interrupted = True
            raise emergency_failure
        return output

    monkeypatch.setattr(
        service._inspector._runner,
        "run_text",
        interrupt_update_then_first_emergency_read,
    )

    with _observe_git_processes(observe_process), pytest.raises(_InjectedRecoveryControl) as caught:
        service.recover(query=target, create_branch=branch, apply=True)

    assert caught.value is emergency_failure
    assert primary_interrupted is True
    assert emergency_interrupted is True
    assert len(completed_updates) == 1
    assert len(completed_reads) == 2
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target


@pytest.mark.parametrize("apply", (False, True))
def test_recover_rejects_an_actionable_ref_over_the_public_bound_before_mutation(
    repo, apply: bool
) -> None:
    """This fails if a display-overlong actionable target escapes or reaches update-ref."""
    oid = repo.git("rev-parse", "HEAD").stdout.strip()
    limits = QueryLimits(max_excerpt_chars=32, max_input_chars=64)
    prefix = "refs/heads/"
    branch = "recovery/" + "x" * (limits.max_input_chars - len(prefix) - len("recovery/"))
    canonical_ref = f"{prefix}{branch}"
    assert len(canonical_ref) == limits.max_input_chars
    service = MemoryService.open(repo.path, limits=limits)

    with pytest.raises(ManduaError) as caught:
        service.recover(query=oid, create_branch=branch, apply=apply)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.message == "The recovery branch exceeds the public display limit."
    assert repo.git("show-ref", "--verify", canonical_ref, check=False).returncode != 0


def test_recover_deduplicates_reflog_and_fsck_candidates(repo) -> None:
    """This fails if the same deleted commit is presented more than once across local sources."""
    repo.checkout_new("task/transient")
    repo.write("knowledge/transient.md", "Transient observation\n")
    lost_oid = repo.commit("Record transient observation")
    repo.checkout("main")
    repo.git("branch", "-D", "task/transient")

    result = repo.service().recover(query=lost_oid, create_branch="recovery/transient")

    candidates = [item for item in result.evidence if item.oid == lost_oid]
    assert len(candidates) == 1
    assert set(candidates[0].details["sources"]) >= {"reflog", "fsck"}


def test_recover_supports_read_only_selection_and_listing_without_a_branch(repo) -> None:
    """This fails if optional recovery CLI inputs cannot inspect local candidates without mutation."""
    oid = repo.git("rev-parse", "HEAD").stdout.strip()

    selected = repo.service().recover(query=oid)
    listed = repo.service().recover()

    assert selected.applied is False
    assert selected.changes == ()
    assert selected.history_scope.end_oid == oid
    assert listed.applied is False
    assert listed.changes == ()
    assert listed.evidence


def test_recover_requires_a_branch_only_when_apply_is_requested(repo) -> None:
    """This fails if apply can mutate without a caller-provided new branch name."""
    oid = repo.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=oid, apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_recover_matches_subject_text_without_returning_unbounded_control_data(repo) -> None:
    """This fails if subject search is case-sensitive or exposes unbounded repository text."""
    repo.write("knowledge/subject.md", "Subject observation\n")
    oid = repo.commit("Recover\tthis local observation " + "x" * 200)
    service = MemoryService.open(repo.path, limits=QueryLimits(max_excerpt_chars=24))

    result = service.recover(query="THIS LOCAL")

    evidence = next(item for item in result.evidence if item.oid == oid)
    assert evidence.excerpt is not None
    assert len(evidence.excerpt) <= 24
    assert "\t" not in evidence.excerpt


def test_recover_matches_the_full_ref_identity_after_rendering_is_bounded(repo) -> None:
    """This fails if an exact ref is truncated before it is used for selection."""
    ref = "refs/heads/topic/a-very-long-recovery-reference"
    oid = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("branch", ref.removeprefix("refs/heads/"))
    service = MemoryService.open(repo.path, limits=QueryLimits(max_excerpt_chars=8))

    result = service.recover(query=ref)

    assert result.history_scope.end_oid == oid
    assert result.changes == ()


def test_recover_includes_a_direct_full_oid_selection_in_evidence(repo) -> None:
    """This fails if a verifiable unreferenced commit leaves a dangling evidence claim."""
    tree = repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    oid = repo.git("commit-tree", tree, "-m", "Direct recovery object").stdout.strip()

    result = repo.service().recover(query=oid)

    assert any(item.id == f"recovery-candidate:{oid}" for item in result.evidence)


def test_recover_keeps_direct_evidence_within_the_candidate_bound(repo) -> None:
    """This fails if a direct full-OID selection adds an eleventh candidate beyond the bound."""
    tree = repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    direct_oid = repo.git(
        "commit-tree", tree, "-m", "Bounded direct recovery object"
    ).stdout.strip()
    service = MemoryService.open(repo.path, limits=QueryLimits(max_commits=1))

    result = service.recover(query=direct_oid)

    assert len([item for item in result.evidence if item.kind == "recovery-candidate"]) <= 1
    assert result.history_scope.truncated is True
    assert any(item.id == f"recovery-candidate:{direct_oid}" for item in result.evidence)


def test_recover_selects_lightweight_and_annotated_tag_refs(repo) -> None:
    """This fails if a tag ref is not strictly peeled to its commit while retaining its ref name."""
    oid = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("tag", "lightweight-recovery")
    repo.git("tag", "-a", "annotated-recovery", "-m", "Annotated recovery tag")

    lightweight = repo.service().recover(query="refs/tags/lightweight-recovery")
    annotated = repo.service().recover(query="refs/tags/annotated-recovery")

    assert lightweight.history_scope.end_oid == oid
    assert annotated.history_scope.end_oid == oid
    tagged = next(item for item in annotated.evidence if item.oid == oid)
    assert "refs/tags/annotated-recovery" in tagged.details["refs"]


@pytest.mark.parametrize("value", ["\ud800", "recovery/\ud800"])
def test_recover_rejects_unicode_surrogates_as_validation_errors(repo, value: str) -> None:
    """This fails if an unencodable query or branch leaks UnicodeEncodeError from subprocess."""
    oid = repo.git("rev-parse", "HEAD").stdout.strip()
    kwargs = {"query": value, "create_branch": "recovery/valid"}
    if value.startswith("recovery/"):
        kwargs = {"query": oid, "create_branch": value}

    with pytest.raises(ManduaError) as caught:
        repo.service().recover(**kwargs)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_recover_rejects_corrupt_packed_refs_as_a_git_failure(repo) -> None:
    """This fails if a broken ref source is misreported as an absent recovery candidate."""
    repo.git("pack-refs", "--all", "--prune")
    (repo.path / ".git" / "packed-refs").write_text("not a packed ref\n", encoding="utf-8")

    with pytest.raises(ManduaError) as caught:
        repo.service().recover()

    assert caught.value.code is ErrorCode.GIT_FAILURE


def test_recover_does_not_lazy_fetch_an_unfetched_promisor_commit(tmp_path: Path) -> None:
    """This fails if a recovery object probe fetches a known promisor commit into .git/objects."""
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    partial = tmp_path / "partial"
    environment = os.environ.copy()
    for cwd, arguments in (
        (tmp_path, ["init", "--bare", str(origin)]),
        (tmp_path, ["init", "--initial-branch=main", str(source)]),
        (source, ["config", "user.email", "test@mandua.invalid"]),
        (source, ["config", "user.name", "Mandu'a Test"]),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    (source / "knowledge.md").write_text("Initial promisor observation\n", encoding="utf-8")
    for arguments in (
        ["add", "knowledge.md"],
        ["commit", "-m", "Record initial promisor observation"],
        ["remote", "add", "origin", str(origin)],
        ["push", "origin", "main"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    for arguments in (
        ["config", "uploadpack.allowFilter", "true"],
        ["config", "uploadpack.allowAnySHA1InWant", "true"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    clone = subprocess.run(
        ["git", "clone", "--filter=blob:none", origin.as_uri(), str(partial)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert "filtering not recognized" not in clone.stderr.lower()
    assert (
        subprocess.run(
            ["git", "config", "--get", "remote.origin.promisor"],
            cwd=partial,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
        == "true"
    )
    assert (
        subprocess.run(
            ["git", "config", "--get", "remote.origin.partialclonefilter"],
            cwd=partial,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
        == "blob:none"
    )
    (source / "later.md").write_text("Unfetched promisor observation\n", encoding="utf-8")
    for arguments in (
        ["add", "later.md"],
        ["commit", "-m", "Record unfetched promisor observation"],
        ["push", "origin", "main"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    unfetched_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    before = sorted(
        path.relative_to(partial / ".git" / "objects")
        for path in (partial / ".git" / "objects").rglob("*")
        if path.is_file()
    )

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(partial).recover(query=unfetched_oid)

    after = sorted(
        path.relative_to(partial / ".git" / "objects")
        for path in (partial / ".git" / "objects").rglob("*")
        if path.is_file()
    )
    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert after == before


def test_recover_reports_truncation_after_eleven_unique_candidates(repo) -> None:
    """This fails if discovery silently scans beyond the configured candidate bound."""
    lost_oids: list[str] = []
    for number in range(11):
        branch = f"task/lost-{number}"
        repo.checkout_new(branch)
        repo.write(f"knowledge/lost-{number}.md", f"Lost observation {number}\n")
        lost_oids.append(repo.commit(f"Record lost observation {number}"))
        repo.checkout("main")
        repo.git("branch", "-D", branch)
    service = MemoryService.open(repo.path, limits=QueryLimits(max_commits=10))

    result = service.recover(query=lost_oids[-1], create_branch="recovery/bounded")

    assert result.history_scope.commit_count == 10
    assert result.history_scope.truncated is True
    assert "limited" in " ".join(result.warnings).lower()


def test_recover_reports_only_the_missing_fsck_target_and_keeps_exact_candidate_usable(
    repo,
) -> None:
    """This fails if a broken-link source commit is mislabeled missing or missing history as bounded."""
    repo.write("knowledge/a.md", "Parent observation\n")
    parent_oid = repo.commit("Record parent observation")
    repo.write("knowledge/b.md", "Child observation\n")
    child_oid = repo.commit("Record child observation")
    object_path = repo.path / ".git" / "objects" / parent_oid[:2] / parent_oid[2:]
    object_path.unlink()

    result = repo.service().recover(query=child_oid)

    assert parent_oid in result.history_scope.missing_objects
    assert child_oid not in result.history_scope.missing_objects
    assert result.history_scope.truncated is False
    assert not any("configured commit or output bound" in warning for warning in result.warnings)
    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=child_oid[:8])
    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY


def test_recover_rejects_a_full_non_commit_object_without_git_failure(repo) -> None:
    """This fails if a blob full OID is diagnosed through ambiguous cat-file stderr."""
    repo.write("knowledge/blob.md", "Blob-only observation\n")
    blob_oid = repo.git("hash-object", "-w", "knowledge/blob.md").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=blob_oid)

    assert caught.value.code is ErrorCode.INVALID_REVISION


def test_recover_rejects_a_corrupt_object_instead_of_reporting_it_missing(repo) -> None:
    """This fails if cat-file stdout says missing while stderr reports object corruption."""
    repo.write("knowledge/corrupt.md", "Corrupt observation\n")
    oid = repo.commit("Record corrupt observation")
    object_path = repo.path / ".git" / "objects" / oid[:2] / oid[2:]
    object_path.chmod(0o600)
    object_path.write_bytes(b"not a valid compressed Git object")

    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=oid)

    assert caught.value.code is ErrorCode.GIT_FAILURE


@pytest.mark.parametrize(
    ("query", "branch", "code"),
    [
        ("", "recovery/valid", ErrorCode.VALIDATION_FAILED),
        ("\x00", "recovery/valid", ErrorCode.VALIDATION_FAILED),
        ("deadbeef", "recovery/valid", ErrorCode.VALIDATION_FAILED),
        ("HEAD", "bad..branch", ErrorCode.VALIDATION_FAILED),
    ],
)
def test_recover_rejects_invalid_or_unresolvable_requests(
    repo, query: str, branch: str, code: ErrorCode
) -> None:
    """This fails if recovery accepts unsafe input or applies without one resolved target."""
    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=query, create_branch=branch, apply=True)

    assert caught.value.code is code


def test_recover_rejects_an_existing_branch_without_overwriting_it(repo) -> None:
    """This fails if recovery can overwrite a pre-existing branch ref."""
    expected = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("branch", "recovery/existing")

    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=expected, create_branch="recovery/existing")

    assert caught.value.code is ErrorCode.CONFLICT
    assert repo.git("rev-parse", "recovery/existing").stdout.strip() == expected


def test_recover_reports_a_selected_missing_object_without_git_stderr(repo) -> None:
    """This fails if a vanished reflog candidate is reported as a raw Git failure."""
    repo.checkout_new("task/missing")
    repo.write("knowledge/missing.md", "Missing observation\n")
    lost_oid = repo.commit("Record missing observation")
    repo.checkout("main")
    repo.git("branch", "-D", "task/missing")
    object_path = repo.path / ".git" / "objects" / lost_oid[:2] / lost_oid[2:]
    object_path.unlink()

    with pytest.raises(ManduaError) as caught:
        repo.service().recover(query=lost_oid, create_branch="recovery/missing")

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == lost_oid
    assert "error:" not in caught.value.message.lower()


def test_recover_discloses_a_shallow_history_scope(repo, tmp_path: Path) -> None:
    """This fails if recovery hides that local discovery cannot see earlier shallow history."""
    repo.write("knowledge/deeper.md", "Deeper observation\n")
    repo.commit("Record deeper observation")
    shallow = repo.clone_shallow_to(tmp_path / "shallow", depth=1)
    oid = shallow.git("rev-parse", "HEAD").stdout.strip()

    result = shallow.service().recover(query=oid, create_branch="recovery/shallow")

    assert result.history_scope.shallow is True
    assert any("shallow" in warning.lower() for warning in result.warnings)


def test_recover_returns_conflict_when_atomic_creation_loses_a_race(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a failed zero-old update retries and overwrites a concurrent branch creator."""
    expected = repo.git("rev-parse", "HEAD").stdout.strip()
    service = repo.service()

    def lose_race(ref: str, _oid: str, *, timeout_seconds: float) -> str:
        repo.git("branch", ref.removeprefix("refs/heads/"))
        return "conflict"

    monkeypatch.setattr(service._inspector, "create_recovery_ref", lose_race)

    with pytest.raises(ManduaError) as caught:
        service.recover(query=expected, create_branch="recovery/race", apply=True)

    assert caught.value.code is ErrorCode.CONFLICT
    assert repo.git("rev-parse", "recovery/race").stdout.strip() == expected


def test_recover_uses_the_sha256_zero_old_value_when_creating_a_branch(tmp_path: Path) -> None:
    """This fails if atomic creation assumes SHA-1 in a SHA-256 repository."""
    repository = tmp_path / "sha256-repository"
    repository.mkdir()
    environment = os.environ.copy()
    subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@mandua.invalid"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        ["git", "config", "user.name", "Mandu'a Test"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    (repository / "knowledge.md").write_text("SHA-256 observation\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "knowledge.md"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        ["git", "commit", "-m", "Record SHA-256 observation"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()

    result = MemoryService.open(repository).recover(
        query=oid, create_branch="recovery/sha256", apply=True
    )

    assert len(oid) == 64
    assert result.applied is True
    assert (
        subprocess.run(
            ["git", "rev-parse", "recovery/sha256"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
        == oid
    )


def test_recover_treats_forty_hex_sha256_text_as_a_bounded_abbreviation(tmp_path: Path) -> None:
    """This fails if a 40-character SHA-256 prefix bypasses truncated-candidate ambiguity."""
    repository = tmp_path / "sha256-abbreviation"
    repository.mkdir()
    environment = os.environ.copy()
    for arguments in (
        ["init", "--object-format=sha256", "--initial-branch=main"],
        ["config", "user.email", "test@mandua.invalid"],
        ["config", "user.name", "Mandu'a Test"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    (repository / "knowledge.md").write_text("SHA-256 bounded observation\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "knowledge.md"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        ["git", "commit", "-m", "Record SHA-256 bounded observation"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    (repository / "later.md").write_text("Later SHA-256 observation\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "later.md"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        ["git", "commit", "-m", "Record later SHA-256 observation"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    service = MemoryService.open(repository, limits=QueryLimits(max_commits=1))

    with pytest.raises(ManduaError) as caught:
        service.recover(query=oid[:40], create_branch="recovery/prefix")
    applied = service.recover(query=oid, create_branch="recovery/full", apply=True)

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY
    assert applied.applied is True
    assert applied.changes[0].after_oid == oid


@pytest.mark.parametrize(
    "failure_type",
    (_InjectedRecoveryControl, KeyboardInterrupt, SystemExit),
    ids=("custom-base", "keyboard-interrupt", "system-exit"),
)
def test_recover_preserves_direct_observer_control_after_real_ref_creation(
    repo, failure_type
) -> None:
    """A completed update observer must not convert interpreter control into applied success."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = f"recovery/direct-observer-{failure_type.__name__.lower()}"
    branch_ref = f"refs/heads/{branch}"
    failure = failure_type("direct recovery completion interruption")
    completed_updates = []
    fired = False

    def interrupt_completed_update(event) -> None:
        nonlocal fired
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)
            if not fired:
                fired = True
                raise failure

    observed: BaseException | None = None
    result = None
    with _observe_git_processes(interrupt_completed_update):
        try:
            result = repo.service().recover(query=target, create_branch=branch, apply=True)
        except BaseException as error:  # noqa: BLE001 - direct identity is the contract
            observed = error

    assert fired is True
    assert len(completed_updates) == 1
    assert completed_updates[0].arguments[3] == target
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target
    assert result is None
    assert observed is failure


def test_recover_persistent_same_instance_observer_interruption_is_not_applied(repo) -> None:
    """Repeated observer control must disclose uncertainty with the same object as its cause."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "recovery/persistent-observer-control"
    branch_ref = f"refs/heads/{branch}"
    failure = _InjectedRecoveryControl("persistent recovery completion interruption")
    completed_updates = []
    emergency_reads = []
    mutation_completed = False

    def persistently_interrupt(event) -> None:
        nonlocal mutation_completed
        is_update = (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        )
        is_emergency_read = (
            mutation_completed
            and event.phase == "completed"
            and event.arguments[:4] == ("rev-parse", "--verify", "--quiet", "--end-of-options")
            and event.arguments[-1:] == (branch_ref,)
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
            repo.service().recover(query=target, create_branch=branch, apply=True)
        except BaseException as error:  # noqa: BLE001 - uncertainty chaining is asserted
            observed = error

    assert len(completed_updates) == 1
    assert emergency_reads
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__ is failure


@pytest.mark.parametrize(
    "root_name",
    ("worktree", "git-directory", "common-directory", "object-directory"),
)
def test_recover_emergency_reconciliation_rejects_a_replaced_physical_authority_root(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_name: str
) -> None:
    """Recovery proof must not rediscover a path-equivalent physical repository replacement."""
    linked = (tmp_path / f"recovery-authority-{root_name}").resolve()
    worktree_branch = f"authority/recovery-{root_name}"
    repo.git("worktree", "add", "-b", worktree_branch, str(linked), "HEAD")
    roots = _recovery_authority_roots(linked)
    target_root = roots[root_name]
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    branch = f"recovery/replaced-{root_name}"
    branch_ref = f"refs/heads/{branch}"
    failure = _InjectedRecoveryControl(f"replaced recovery {root_name}")
    service = MemoryService.open(linked)
    original = service._inspector._runner.run_text
    displaced: Path | None = None
    completed_updates = []

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)

    def create_replace_then_interrupt(arguments, **kwargs):
        nonlocal displaced
        output = original(arguments, **kwargs)
        if arguments[:3] == ["update-ref", "--no-deref", branch_ref] and displaced is None:
            assert output.returncode == 0 and not output.stdout and not output.stderr
            displaced = _replace_recovery_authority_root(target_root)
            raise failure
        return output

    monkeypatch.setattr(service._inspector._runner, "run_text", create_replace_then_interrupt)
    observed: BaseException | None = None
    try:
        with _observe_git_processes(observe_process):
            try:
                service.recover(query=target, create_branch=branch, apply=True)
            except BaseException as error:  # noqa: BLE001 - uncertainty is asserted below
                observed = error
    finally:
        if displaced is not None:
            _restore_recovery_authority_root(target_root, displaced)

    assert len(completed_updates) == 1
    assert completed_updates[0].arguments[3] == target
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target
    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__ is failure


def test_round3_recovery_cleanup_reconciliation_warning_names_uncertainty_and_action(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applied recovery after authority cleanup failure needs a specific recovery warning."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    branch = "round3/recovery-cleanup-warning"
    branch_ref = f"refs/heads/{branch}"
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
                "Injected recovery authority cleanup failure.",
                recovery=f"Inspect {branch_ref} before another recovery attempt.",
            )

    def observe_process(event) -> None:
        if (
            event.phase == "completed"
            and event.returncode == 0
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)

    monkeypatch.setattr(GitRepositoryAuthority, "cleanup", cleanup_then_report_failure)
    with _observe_git_processes(observe_process):
        result = repo.service().recover(query=target, create_branch=branch, apply=True)

    warning = " ".join(result.warnings).casefold()
    assert len(completed_updates) == 1
    assert cleanup_calls >= 2
    assert result.applied is True
    assert "cleanup" in warning
    assert "uncertain" in warning or "inspect" in warning
    assert branch_ref in " ".join(result.warnings)
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target


def test_round3_recovery_emergency_budget_accepts_large_packed_authority_and_long_ref(
    repo, tmp_path: Path
) -> None:
    """Fresh recovery proof must size valid authority metadata and cumulative argv."""
    packed = _round3_pack_large_recovery_metadata(repo)
    branch = _round3_long_recovery_branch()
    branch_ref = f"refs/heads/{branch}"
    assert len(branch_ref) > 512
    assert repo.git("check-ref-format", branch_ref).returncode == 0
    linked = (tmp_path / "round3-recovery-emergency").resolve()
    repo.git("worktree", "add", "-b", "round3/recovery-emergency-linked", str(linked), "HEAD")
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    limits = QueryLimits(
        max_excerpt_chars=4_096,
        max_input_chars=4_096,
        timeout_seconds=10.0,
    )
    failure = _InjectedRecoveryControl("large-authority recovery mutation interruption")
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
            and event.arguments[:3] == ("update-ref", "--no-deref", branch_ref)
        ):
            completed_updates.append(event)
            if not interrupted:
                interrupted = True
                raise failure

    observed: BaseException | None = None
    with _observe_git_processes(interrupt_completed_update):
        try:
            MemoryService.open(linked, limits=limits).recover(
                query=target,
                create_branch=branch,
                apply=True,
            )
        except BaseException as error:  # noqa: BLE001 - exact control identity is asserted
            observed = error

    assert packed.stat().st_size > 512
    assert interrupted is True
    assert len(completed_updates) == 1
    assert _round3_recovery_planned_argv_bytes(considered) > 4_096
    assert len(considered) <= 96
    assert 1 <= len(emergency_considered) <= 4
    assert observed is failure
    assert repo.git("rev-parse", branch_ref).stdout.strip() == target
