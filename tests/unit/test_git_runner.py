"""Unit tests for bounded Git process execution."""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import pytest

import mandua.git_runner as git_runner_module
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import (
    GitOperationBudget,
    GitRepositoryAuthority,
    GitRunner,
    _GitBudgetLimits,
)
from mandua.models import QueryLimits
from mandua.queries import provenance


class _SelectorSetupAbort(BaseException):
    """A non-Exception setup interruption used to prove unconditional process cleanup."""


class _ObserverDirectAbort(BaseException):
    """Direct observer control flow whose exact identity must survive prior process failure."""


def test_arguments_are_not_interpreted_by_a_shell(repo, tmp_path: Path) -> None:
    """This fails if a command argument is ever concatenated into a shell command."""
    marker = tmp_path / "shell-was-used"
    runner = GitRunner(repo.path)

    runner.run_text(["rev-parse", f"HEAD;touch {marker}"], check=False)

    assert not marker.exists()


def test_controlled_environment_disables_lazy_fetches(repo) -> None:
    """This fails if a local object probe can ask a promisor remote to fetch missing objects."""
    environment = GitRunner(repo.path)._environment()

    assert environment["GIT_NO_LAZY_FETCH"] == "1"


def test_output_over_the_limit_is_rejected(repo) -> None:
    """This fails if captured Git output is read before its byte limit is enforced."""
    runner = GitRunner(repo.path, max_output_bytes=32)
    repo.write("large.txt", "x" * 128)
    repo.commit("Add a bounded-output fixture")

    with pytest.raises(ManduaError) as caught:
        runner.run_text(["show", "HEAD:large.txt"])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_combined_output_limit_is_checked_before_either_stream_is_read(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if individually bounded streams are read before their total is bounded."""
    helper = tmp_path / "git-combined-output"
    helper.write_text(
        "#!/bin/sh\nprintf 'xxxxxxxxxxxxxxxxxxxx'\nprintf 'yyyyyyyyyyyyyyyyyyyy' >&2\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, max_output_bytes=32).run(["combined-output"])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("setup_error", "expected_error"),
    (
        (OSError("injected selector setup failure"), ManduaError),
        (_SelectorSetupAbort("injected selector setup abort"), _SelectorSetupAbort),
    ),
    ids=("oserror", "baseexception"),
)
def test_selector_setup_failure_closes_kills_and_reaps_the_started_process(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    """This fails if selector setup can escape the post-launch cleanup boundary."""
    from mandua.git_runner import _observe_git_processes

    helper = tmp_path / "git-wait-for-selector-setup"
    helper.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_selector_setup():
        raise setup_error

    monkeypatch.setattr(git_runner_module.subprocess, "Popen", capture_process)
    monkeypatch.setattr(git_runner_module.selectors, "DefaultSelector", fail_selector_setup)
    events = []

    try:
        with _observe_git_processes(events.append), pytest.raises(expected_error) as caught:
            GitRunner(repo.path).run(["wait-for-selector-setup"])

        assert len(processes) == 1
        process = processes[0]
        assert process.poll() is not None
        assert process.returncode is not None
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed
        assert process.stderr is not None and process.stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        assert [event.phase for event in events] == ["considered", "failed"]
        assert events[0].attempt is events[1].attempt
        assert events[1].returncode == process.returncode
        if isinstance(setup_error, OSError):
            assert isinstance(caught.value, ManduaError)
            assert caught.value.code is ErrorCode.GIT_FAILURE
            assert caught.value.message == "Git process output could not be captured."
        else:
            assert caught.value is setup_error
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1.0)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def test_selector_setup_cleanup_uncertainty_preserves_the_setup_failure(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if cleanup uncertainty masks the selector setup failure classification."""
    helper = tmp_path / "git-wait-for-selector-cleanup"
    helper.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        git_runner_module.selectors,
        "DefaultSelector",
        lambda: (_ for _ in ()).throw(OSError("injected selector setup failure")),
    )
    runner = GitRunner(repo.path)
    bounded_wait = runner._wait_for_process

    def reap_then_report_uncertainty(process: subprocess.Popen[bytes]) -> None:
        bounded_wait(process)
        raise ManduaError(
            ErrorCode.GIT_FAILURE,
            "Injected cleanup uncertainty.",
            recovery="The child state could not be verified.",
        )

    monkeypatch.setattr(runner, "_wait_for_process", reap_then_report_uncertainty)

    with pytest.raises(ManduaError) as caught:
        runner.run(["wait-for-selector-cleanup"])

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.message == "Git process output could not be captured."
    assert caught.value.recovery is not None
    assert "cleanup" in caught.value.recovery.casefold()
    assert "uncertain" in caught.value.recovery.casefold()


def test_private_process_observer_records_success_and_nonzero_exit(repo) -> None:
    from mandua.git_runner import _observe_git_processes

    events = []
    runner = GitRunner(repo.path)

    with _observe_git_processes(events.append):
        runner.run(["rev-parse", "HEAD"])
        runner.run(["rev-parse", "does-not-exist"], check=False)

    assert [event.phase for event in events] == [
        "considered",
        "completed",
        "considered",
        "failed",
    ]
    assert events[0].attempt is events[1].attempt
    assert events[2].attempt is events[3].attempt
    assert events[0].arguments == ("rev-parse", "HEAD")
    assert events[0].cwd == repo.path.resolve()
    assert events[0].command[0] == "git"
    assert events[1].returncode == 0
    assert events[3].returncode != 0


def test_private_process_observer_records_start_failure(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mandua.git_runner import _observe_git_processes

    events = []

    def fail_start(*_args, **_kwargs):
        raise OSError("injected start failure")

    monkeypatch.setattr(git_runner_module.subprocess, "Popen", fail_start)
    with _observe_git_processes(events.append), pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["rev-parse", "HEAD"])

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert [event.phase for event in events] == ["considered", "failed"]
    assert events[0].attempt is events[1].attempt
    assert events[1].returncode is None


def test_private_process_observer_failure_prevents_launch(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mandua.git_runner import _observe_git_processes

    def reject(_event) -> None:
        raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "No audit slot is available.")

    def forbidden_launch(*_args, **_kwargs):
        raise AssertionError("observer rejection did not prevent process launch")

    monkeypatch.setattr(git_runner_module.subprocess, "Popen", forbidden_launch)
    with _observe_git_processes(reject), pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["rev-parse", "HEAD"])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_private_process_observer_is_nested_and_context_scoped(repo) -> None:
    from mandua.git_runner import _observe_git_processes

    outer = []
    inner = []
    runner = GitRunner(repo.path)

    with _observe_git_processes(outer.append):
        runner.run(["rev-parse", "HEAD"])
        with _observe_git_processes(inner.append):
            runner.run(["rev-parse", "HEAD"])
        runner.run(["rev-parse", "HEAD"])
    runner.run(["rev-parse", "HEAD"])

    assert [event.phase for event in outer] == [
        "considered",
        "completed",
        "considered",
        "completed",
    ]
    assert [event.phase for event in inner] == ["considered", "completed"]


def test_terminal_observer_failure_does_not_mask_the_output_limit(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mandua.git_runner import _observe_git_processes

    helper = tmp_path / "git-observed-overflow"
    helper.write_text(
        "#!/bin/sh\nprintf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    events = []

    def fail_terminal(event) -> None:
        events.append(event)
        if event.phase == "failed":
            raise RuntimeError("injected terminal observation failure")

    with _observe_git_processes(fail_terminal), pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, max_output_bytes=32).run(["observed-overflow"])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert caught.value.recovery is not None
    assert "observation" in caught.value.recovery.casefold()
    assert [event.phase for event in events] == ["considered", "failed"]


def test_terminal_observer_failure_preserves_check_false_semantics(repo) -> None:
    from mandua.git_runner import _observe_git_processes

    def fail_terminal(event) -> None:
        if event.phase == "failed":
            raise RuntimeError("injected terminal observation failure")

    with _observe_git_processes(fail_terminal), pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["rev-parse", "does-not-exist"], check=False)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.message == "Git process observation failed at completion."
    assert caught.value.evidence[0].details["process_phase"] == "failed"
    assert caught.value.evidence[0].details["exit_code"] != 0


def test_terminal_observer_failure_after_success_discloses_both_states(repo) -> None:
    from mandua.git_runner import _observe_git_processes

    def fail_terminal(event) -> None:
        if event.phase == "completed":
            raise RuntimeError("injected terminal observation failure")

    with _observe_git_processes(fail_terminal), pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(["rev-parse", "HEAD"])

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.message == "Git process observation failed at completion."
    assert caught.value.evidence[0].kind == "GitObservationFailure"
    assert caught.value.evidence[0].details == {
        "exit_code": 0,
        "process_phase": "completed",
    }


@pytest.mark.parametrize(
    "failure_type",
    (_ObserverDirectAbort, KeyboardInterrupt, SystemExit),
    ids=("custom-base", "keyboard-interrupt", "system-exit"),
)
def test_round3_checked_nonzero_preserves_direct_observer_control_with_git_failure_chained(
    repo, failure_type
) -> None:
    """This fails if a prior checked Git error masks direct terminal-observer control flow."""
    from mandua.git_runner import _observe_git_processes

    failure = failure_type("direct observer control after checked nonzero completion")
    failed_events = []

    def interrupt_failed_completion(event) -> None:
        if event.phase == "failed" and event.returncode not in {None, 0}:
            failed_events.append(event)
            raise failure

    observed: BaseException | None = None
    with _observe_git_processes(interrupt_failed_completion):
        try:
            GitRunner(repo.path).run(["rev-parse", "does-not-exist"])
        except BaseException as error:  # noqa: BLE001 - exact control identity is the contract
            observed = error

    assert len(failed_events) == 1
    assert observed is failure
    assert isinstance(observed.__cause__, ManduaError)
    assert observed.__cause__.code is ErrorCode.GIT_FAILURE
    assert observed.__cause__.evidence[0].details["subcommand"] == "rev-parse"


@pytest.mark.parametrize(
    "failure_type",
    (_ObserverDirectAbort, KeyboardInterrupt, SystemExit),
    ids=("custom-base", "keyboard-interrupt", "system-exit"),
)
def test_round3_capture_failure_preserves_direct_observer_control_with_capture_error_chained(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type,
) -> None:
    """This fails if a real bounded-capture error masks direct observer control flow."""
    from mandua.git_runner import _observe_git_processes

    helper = tmp_path / "git-observer-capture-overflow"
    helper.write_text(
        "#!/bin/sh\nprintf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    failure = failure_type("direct observer control after capture failure")
    failed_events = []

    def interrupt_failed_capture(event) -> None:
        if event.phase == "failed" and event.returncode is not None:
            failed_events.append(event)
            raise failure

    observed: BaseException | None = None
    with _observe_git_processes(interrupt_failed_capture):
        try:
            GitRunner(repo.path, max_output_bytes=32).run(["observer-capture-overflow"])
        except BaseException as error:  # noqa: BLE001 - exact control identity is the contract
            observed = error

    assert len(failed_events) == 1
    assert observed is failure
    assert isinstance(observed.__cause__, ManduaError)
    assert observed.__cause__.code is ErrorCode.LIMIT_EXCEEDED
    assert "output" in observed.__cause__.message.casefold()


def test_round3_persistent_observer_control_discloses_each_distinct_prior_process_error(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One reused control object must stay primary across checked and capture failures."""
    from mandua.git_runner import _observe_git_processes

    helper = tmp_path / "git-persistent-observer-overflow"
    helper.write_text(
        "#!/bin/sh\nprintf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    failure = _ObserverDirectAbort("persistent direct observer control")
    failed_events = []
    observed: list[BaseException] = []
    prior_errors: list[BaseException | None] = []

    def persistently_interrupt(event) -> None:
        if event.phase == "failed" and event.returncode is not None:
            failed_events.append(event)
            raise failure

    operations = (
        lambda: GitRunner(repo.path).run(["rev-parse", "does-not-exist"]),
        lambda: GitRunner(repo.path, max_output_bytes=32).run(["persistent-observer-overflow"]),
    )
    with _observe_git_processes(persistently_interrupt):
        for operation in operations:
            try:
                operation()
            except BaseException as error:  # noqa: BLE001 - exact repeated identity is asserted
                observed.append(error)
                prior_errors.append(failure.__cause__)

    assert len(failed_events) == 2
    assert observed == [failure, failure]
    assert [error.code for error in prior_errors if isinstance(error, ManduaError)] == [
        ErrorCode.GIT_FAILURE,
        ErrorCode.LIMIT_EXCEEDED,
    ]
    assert all(isinstance(error, ManduaError) for error in prior_errors)


@pytest.mark.parametrize("arguments", [[], ["rev-parse", "bad\x00value"]])
def test_invalid_argument_vectors_are_rejected_before_git_runs(repo, arguments: list[str]) -> None:
    """This fails if empty commands or NUL bytes reach the Git executable."""
    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run(arguments)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_arguments_over_the_input_limit_are_rejected(repo) -> None:
    """This fails if an oversized untrusted argument reaches Git."""
    limits = QueryLimits(max_input_chars=8)

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, limits=limits).run(["rev-parse", "x" * 9])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_git_failures_expose_only_sanitized_evidence(repo) -> None:
    """This fails if Git's raw error text is surfaced through ManduaError."""
    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).run_text(["rev-parse", "does-not-exist"])

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.evidence[0].kind == "GitFailure"
    assert caught.value.evidence[0].details["exit_code"] != 0
    assert caught.value.evidence[0].details["subcommand"] == "rev-parse"
    assert len(caught.value.evidence[0].excerpt or "") <= QueryLimits().max_excerpt_chars


def test_bounded_git_diagnostics_cannot_be_mistaken_for_a_full_object_identity(repo) -> None:
    """This fails if truncation turns hostile hexadecimal diagnostics into a valid object ID."""
    runner = GitRunner(repo.path, limits=QueryLimits(max_excerpt_chars=40))
    error = runner._git_failure("show", 1, b"a" * 80)

    excerpt = error.evidence[0].excerpt
    assert excerpt is not None
    assert len(excerpt) == 40
    assert not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", excerpt)
    assert excerpt.endswith("...")


def test_resolve_commit_uses_a_commit_revision(repo) -> None:
    """This fails if a revision is not resolved to a full commit object ID."""
    expected = repo.git("rev-parse", "HEAD").stdout.strip()

    assert GitRunner(repo.path).resolve_commit("HEAD") == expected


@pytest.mark.parametrize(
    ("object_id", "path", "code"),
    [
        ("not-an-object", PurePosixPath("file.txt"), ErrorCode.INVALID_REVISION),
        ("0" * 40, PurePosixPath("../file.txt"), ErrorCode.INVALID_PATH),
    ],
)
def test_show_blob_rejects_invalid_object_or_path(
    repo, object_id: str, path: PurePosixPath, code: ErrorCode
) -> None:
    """This fails if unsafe object names or paths are passed to Git show."""
    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path).show_blob(object_id, path)

    assert caught.value.code is code


def test_timeout_is_reported_as_a_limit_error(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a timed-out Git child is not killed and normalized to a limit error."""
    marker = tmp_path / "timeout-child-finished"
    helper = tmp_path / "git-wait-past-timeout"
    helper.write_text(f"#!/bin/sh\nsleep 5\ntouch '{marker}'\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, timeout_seconds=0.05).run(["wait-past-timeout"])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert not marker.exists()


def test_runner_clock_is_isolated_from_a_query_clock_patch(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a query's simulated clock also controls Git subprocess accounting."""
    with monkeypatch.context() as query_clock:

        def query_monotonic() -> float:
            raise AssertionError("GitRunner consulted the query clock")

        query_clock.setattr(provenance.time, "monotonic", query_monotonic)

        output = GitRunner(repo.path).run(["status"])

    assert output.returncode == 0


def test_runner_never_waits_unbounded_for_an_unreapable_child(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if emergency child cleanup can exceed its finite reap windows."""
    waits: list[float] = []

    class UnreapableProcess:
        pid = 999_999

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> None:
            waits.append(timeout)
            raise subprocess.TimeoutExpired(["git"], timeout)

    monkeypatch.setattr(os, "killpg", lambda *_: None)
    runner = GitRunner(repo.path)

    with pytest.raises(ManduaError) as caught:
        runner._wait_for_process(UnreapableProcess())  # type: ignore[arg-type]

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert waits == [1.0, 1.0]


def test_run_uses_a_smaller_per_call_timeout_without_changing_the_default(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a caller's remaining shared deadline is ignored by GitRunner."""
    helper = tmp_path / "git-wait-for-call-timeout"
    helper.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    runner = GitRunner(repo.path, timeout_seconds=10.0)

    with pytest.raises(ManduaError) as caught:
        runner.run(["wait-for-call-timeout"], timeout_seconds=0.05)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert runner.run(["status"]).returncode == 0


def test_temporary_index_cleanup_failure_is_distinct_and_revokes_authority(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if cleanup uncertainty is mislabeled as an index creation failure."""
    runner = GitRunner(repo.path)
    real_directory = TemporaryDirectory(prefix="mandua-index-cleanup-test-")

    class CleanupFailure:
        name = real_directory.name

        def __enter__(self) -> str:
            return self.name

        def cleanup(self) -> None:
            real_directory.cleanup()
            raise OSError("injected index cleanup failure")

        def __exit__(self, *_: object) -> None:
            self.cleanup()

    monkeypatch.setattr("mandua.git_runner._PrivateDirectory", lambda **_: CleanupFailure())

    with pytest.raises(ManduaError) as error, runner.temporary_index():
        pass

    assert error.value.code is ErrorCode.GIT_FAILURE
    assert error.value.message == "The temporary Git index cleanup could not be verified."
    assert error.value.recovery is not None
    assert runner._active_indexes == set()


def test_temporary_index_setup_base_exception_cleans_and_preserves_the_failure(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if setup-time BaseException bypasses cleanup or is rewritten after cleanup."""
    runner = GitRunner(repo.path)
    real_directory = TemporaryDirectory(prefix="mandua-index-setup-abort-")
    cleanup_calls = 0

    class SetupAbort(BaseException):
        pass

    failure = SetupAbort("injected setup abort")

    class TrackedDirectory:
        name = real_directory.name

        def cleanup(self) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            real_directory.cleanup()

    monkeypatch.setattr("mandua.git_runner._PrivateDirectory", lambda **_: TrackedDirectory())
    monkeypatch.setattr("mandua.git_runner.os.chmod", lambda *_: (_ for _ in ()).throw(failure))

    try:
        with pytest.raises(SetupAbort) as error, runner.temporary_index():
            pass

        assert error.value is failure
        assert cleanup_calls == 1
        assert not Path(real_directory.name).exists()
        assert runner._active_indexes == set()
    finally:
        real_directory.cleanup()


def test_temporary_index_setup_and_cleanup_double_failure_reports_uncertainty(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if cleanup failure exposes the setup abort while directory state is uncertain."""
    runner = GitRunner(repo.path)
    real_directory = TemporaryDirectory(prefix="mandua-index-double-failure-")
    cleanup_calls = 0

    class SetupAbort(BaseException):
        pass

    class CleanupFailureDirectory:
        name = real_directory.name

        def cleanup(self) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            real_directory.cleanup()
            raise OSError("injected cleanup failure")

    monkeypatch.setattr(
        "mandua.git_runner._PrivateDirectory",
        lambda **_: CleanupFailureDirectory(),
    )
    monkeypatch.setattr(
        "mandua.git_runner.os.chmod",
        lambda *_: (_ for _ in ()).throw(SetupAbort("injected setup abort")),
    )

    try:
        with pytest.raises(ManduaError) as error, runner.temporary_index():
            pass

        assert error.value.code is ErrorCode.GIT_FAILURE
        assert error.value.message == "The temporary Git index cleanup could not be verified."
        assert error.value.recovery is not None
        assert cleanup_calls == 1
        assert not Path(real_directory.name).exists()
        assert runner._active_indexes == set()
    finally:
        real_directory.cleanup()


def test_temporary_index_body_base_exception_still_cleans_and_revokes_authority(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if broad setup handling weakens the existing body cleanup guarantee."""
    runner = GitRunner(repo.path)
    real_directory = TemporaryDirectory(prefix="mandua-index-body-abort-")
    cleanup_calls = 0

    class BodyAbort(BaseException):
        pass

    failure = BodyAbort("injected body abort")

    class TrackedDirectory:
        name = real_directory.name

        def cleanup(self) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            real_directory.cleanup()

    monkeypatch.setattr("mandua.git_runner._PrivateDirectory", lambda **_: TrackedDirectory())

    try:
        with pytest.raises(BodyAbort) as error, runner.temporary_index():
            raise failure

        assert error.value is failure
        assert cleanup_calls == 1
        assert not Path(real_directory.name).exists()
        assert runner._active_indexes == set()
    finally:
        real_directory.cleanup()


@pytest.mark.parametrize("invalid_timeout", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_configured_and_per_call_timeouts_must_be_finite_and_positive(
    repo, invalid_timeout: float
) -> None:
    """This fails if a non-finite timeout can make a Git process bound ineffective."""
    with pytest.raises(ManduaError) as configured:
        GitRunner(repo.path, timeout_seconds=invalid_timeout)
    with pytest.raises(ManduaError) as per_call:
        GitRunner(repo.path).run(["status"], timeout_seconds=invalid_timeout)

    assert configured.value.code is ErrorCode.VALIDATION_FAILED
    assert per_call.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    "constructor",
    (
        lambda repository, limits: GitRunner(repository, limits=limits),
        lambda _repository, limits: GitOperationBudget(limits),
    ),
    ids=("runner", "budget"),
)
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
def test_runner_and_budget_reject_unrepresentable_query_limits_without_launching_git(
    repo,
    monkeypatch: pytest.MonkeyPatch,
    constructor,
    field: str,
    invalid_kind: str,
    expected_message: str,
) -> None:
    """This fails if direct constructors disagree with the public limit boundary."""
    invalid_value = 10**10_000 if invalid_kind == "huge" else Fraction(1, 2)
    limits = replace(QueryLimits(), **{field: invalid_value})
    launches = 0

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        raise AssertionError("Git must not launch for an unrepresentable query limit")

    monkeypatch.setattr(git_runner_module.subprocess, "Popen", forbidden_launch)

    with pytest.raises(ManduaError) as caught:
        constructor(repo.path, limits)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.message == expected_message
    assert launches == 0


@pytest.mark.parametrize("invalid_kind", ("huge", "fraction"))
def test_per_call_timeout_uses_the_same_representable_type_boundary_before_launch(
    repo,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    """This fails if per-command timeout normalization can overflow or accept other Reals."""
    invalid_timeout = 10**10_000 if invalid_kind == "huge" else Fraction(1, 2)
    runner = GitRunner(repo.path)
    launches = 0

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        raise AssertionError("Git must not launch for an unrepresentable timeout")

    monkeypatch.setattr(git_runner_module.subprocess, "Popen", forbidden_launch)

    with pytest.raises(ManduaError) as caught:
        runner.run(["status"], timeout_seconds=invalid_timeout)  # type: ignore[arg-type]

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.message == "The Git timeout must be finite and positive."
    assert launches == 0


def test_real_combined_output_is_stopped_before_the_child_can_finish(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if stdout and stderr can grow to completion before enforcement."""
    marker = tmp_path / "output-child-finished"
    helper = tmp_path / "git-output-flood"
    helper.write_text(
        "#!/bin/sh\n"
        "count=0\n"
        'while [ "$count" -lt 20000 ]; do\n'
        "  printf '0123456789abcdef0123456789abcdef\\n'\n"
        "  printf 'fedcba9876543210fedcba9876543210\\n' >&2\n"
        "  count=$((count + 1))\n"
        "done\n"
        f"touch '{marker}'\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(ManduaError) as caught:
        GitRunner(repo.path, max_output_bytes=1_024).run(["output-flood"])

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert not marker.exists()


def test_timeout_output_is_charged_to_a_budget_shared_across_runners(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if bytes emitted before timeout disappear from aggregate accounting."""
    slow = tmp_path / "git-output-then-wait"
    slow.write_text(
        "#!/bin/sh\n"
        "count=0\n"
        'while [ "$count" -lt 20 ]; do\n'
        "  printf '01234567890123456789'\n"
        "  count=$((count + 1))\n"
        "done\n"
        "sleep 5\n",
        encoding="utf-8",
    )
    slow.chmod(0o700)
    small = tmp_path / "git-output-small"
    small.write_text(
        "#!/bin/sh\n"
        "count=0\n"
        'while [ "$count" -lt 10 ]; do\n'
        "  printf 'abcdefghijklmnopqrst'\n"
        "  count=$((count + 1))\n"
        "done\n",
        encoding="utf-8",
    )
    small.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    limits = QueryLimits(timeout_seconds=2.0)
    private_limits = _GitBudgetLimits(
        max_processes=4,
        max_output_bytes=512,
        max_input_bytes=1_024,
    )
    budget = GitOperationBudget(limits, budget_limits=private_limits)

    with pytest.raises(ManduaError) as timed_out:
        GitRunner(
            repo.path,
            limits=limits,
            operation_budget=budget,
            _budget_limits=private_limits,
        ).run(["output-then-wait"], timeout_seconds=0.5)
    assert budget._output_remaining <= 112
    with pytest.raises(ManduaError) as exhausted:
        GitRunner(
            repo.path,
            limits=limits,
            operation_budget=budget,
            _budget_limits=private_limits,
        ).run(["output-small"])

    assert timed_out.value.code is ErrorCode.LIMIT_EXCEEDED
    assert exhausted.value.code is ErrorCode.LIMIT_EXCEEDED


def test_operation_budget_charges_complete_argv_before_process_launch(repo) -> None:
    """This fails if repeated Git argument arrays bypass aggregate input accounting."""
    from mandua.git_runner import _observe_git_processes

    limits = QueryLimits(timeout_seconds=2.0)
    arguments = ["rev-parse", "HEAD"]
    probe_runner = GitRunner(repo.path, limits=limits)
    command = probe_runner._command(probe_runner._safe_arguments(arguments))
    argument_bytes = sum(len(os.fsencode(argument)) + 1 for argument in command)
    private_limits = _GitBudgetLimits(
        max_processes=4,
        max_output_bytes=1_024,
        max_input_bytes=argument_bytes,
    )
    budget = GitOperationBudget(limits, budget_limits=private_limits)
    runner = GitRunner(
        repo.path,
        limits=limits,
        operation_budget=budget,
        _budget_limits=private_limits,
    )
    events = []

    with _observe_git_processes(events.append):
        runner.run(arguments)
        with pytest.raises(ManduaError) as caught:
            runner.run(arguments)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert caught.value.message == "The aggregate Git input byte limit was exceeded."
    assert [event.phase for event in events] == ["considered", "completed"]


def _open_authority_layout(
    repo, tmp_path: Path, *, linked: bool
) -> tuple[GitRunner, GitRepositoryAuthority, dict[str, Path]]:
    repository = repo.path
    if linked:
        repository = (tmp_path / "authority-linked-worktree").resolve()
        repo.git("worktree", "add", "-b", "authority-linked", str(repository))
    git_directory = Path(
        subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    ).resolve()
    common_directory = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    ).resolve()
    if linked:
        assert git_directory != common_directory
    else:
        assert git_directory == common_directory
    runner = GitRunner(repository)
    authority = runner.open_repository_authority(runner.resolve_commit("HEAD"))
    return (
        runner,
        authority,
        {
            "worktree": repository,
            "git": git_directory,
            "common": common_directory,
            "objects": common_directory / "objects",
        },
    )


def _replace_directory_now(path: Path, *, replacement_kind: str = "directory") -> Path:
    displaced = path.with_name(f"{path.name}.mandua-captured")
    path.rename(displaced)
    try:
        if replacement_kind == "directory":
            path.mkdir()
        elif replacement_kind == "symlink":
            path.symlink_to(displaced, target_is_directory=True)
        elif replacement_kind == "fifo":
            os.mkfifo(path)
        else:
            raise AssertionError(f"unexpected replacement kind: {replacement_kind}")
    except BaseException:
        displaced.rename(path)
        raise
    return displaced


def _restore_replaced_directory(path: Path, displaced: Path) -> None:
    metadata = os.lstat(path)
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif metadata:
        path.unlink()
    displaced.rename(path)


@contextmanager
def _replaced_directory(path: Path, *, replacement_kind: str = "directory") -> Iterator[None]:
    displaced = _replace_directory_now(path, replacement_kind=replacement_kind)
    try:
        yield
    finally:
        _restore_replaced_directory(path, displaced)


@pytest.mark.parametrize("linked", (False, True), ids=("ordinary", "linked"))
@pytest.mark.parametrize("root_name", ("worktree", "git", "common", "objects"))
def test_repository_authority_rejects_a_replaced_root_before_starting_git(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked: bool,
    root_name: str,
) -> None:
    """This fails if a captured root can redirect Git before the command starts."""
    runner, authority, roots = _open_authority_layout(repo, tmp_path, linked=linked)
    marker = tmp_path / f"started-{root_name}-{linked}"
    fake_bin = tmp_path / f"fake-bin-{root_name}-{linked}"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        '#!/bin/sh\n: > "$MANDUA_AUTHORITY_TEST_MARKER"\nexit 0\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("MANDUA_AUTHORITY_TEST_MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    observed: ManduaError | None = None
    try:
        with _replaced_directory(roots[root_name]):
            if root_name == "worktree":
                replacement_git = roots[root_name] / ".git"
                if linked:
                    replacement_git.write_text(
                        "gitdir: harmless-test-only-path\n", encoding="utf-8"
                    )
                else:
                    replacement_git.mkdir()
            try:
                runner.run(["--version"], repository_authority=authority)
            except ManduaError as error:
                observed = error
    finally:
        authority.cleanup()

    assert observed is not None
    assert observed.code is ErrorCode.GIT_FAILURE
    assert not marker.exists()


@pytest.mark.parametrize(
    ("root_name", "replacement_kind"),
    (("common", "symlink"), ("git", "fifo")),
)
def test_repository_authority_rejects_indirect_or_special_root_replacements(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_name: str,
    replacement_kind: str,
) -> None:
    """This fails if a replaced authority root may become a symlink or special file."""
    runner, authority, roots = _open_authority_layout(repo, tmp_path, linked=True)
    marker = tmp_path / f"started-{root_name}-{replacement_kind}"
    fake_bin = tmp_path / f"fake-bin-{root_name}-{replacement_kind}"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        '#!/bin/sh\n: > "$MANDUA_AUTHORITY_TEST_MARKER"\nexit 0\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("MANDUA_AUTHORITY_TEST_MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    observed: ManduaError | None = None
    try:
        with _replaced_directory(roots[root_name], replacement_kind=replacement_kind):
            try:
                runner.run(["--version"], repository_authority=authority)
            except ManduaError as error:
                observed = error
    finally:
        authority.cleanup()

    assert observed is not None
    assert observed.code is ErrorCode.GIT_FAILURE
    assert not marker.exists()


@pytest.mark.parametrize("completion", ("normal", "exceptional"))
def test_repository_authority_rechecks_linked_roots_after_command_completion(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: str,
) -> None:
    """This fails if a root replacement during Git survives either completion path."""
    runner, authority, roots = _open_authority_layout(repo, tmp_path, linked=True)
    target = roots["git" if completion == "normal" else "common"]
    original_capture = runner._capture_bounded_process
    displaced: Path | None = None

    class InjectedCaptureFailure(RuntimeError):
        pass

    def capture_then_replace(*args, **kwargs):
        nonlocal displaced
        output = original_capture(*args, **kwargs)
        displaced = _replace_directory_now(target)
        if completion == "exceptional":
            raise InjectedCaptureFailure("injected after completed Git capture")
        return output

    monkeypatch.setattr(runner, "_capture_bounded_process", capture_then_replace)
    observed: BaseException | None = None
    try:
        try:
            runner.run(["--version"], repository_authority=authority)
        except BaseException as error:  # noqa: BLE001 - the exception path is the contract
            observed = error
    finally:
        if displaced is not None:
            _restore_replaced_directory(target, displaced)
        authority.cleanup()

    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE


def test_repository_authority_rejects_a_replaced_private_projection_before_start(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if replacing the literal GIT_COMMON_DIR can start a Git command."""
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    marker = tmp_path / "private-projection-command-started"
    fake_bin = tmp_path / "private-projection-fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        '#!/bin/sh\n: > "$MANDUA_AUTHORITY_TEST_MARKER"\nexit 0\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("MANDUA_AUTHORITY_TEST_MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    observed: ManduaError | None = None
    try:
        with _replaced_directory(authority.common_directory):
            try:
                runner.run(["--version"], repository_authority=authority)
            except ManduaError as error:
                observed = error
    finally:
        authority.cleanup()

    assert observed is not None
    assert observed.code is ErrorCode.GIT_FAILURE
    assert not marker.exists()


@pytest.mark.parametrize("completion", ("normal", "exceptional"))
def test_repository_authority_rechecks_private_projection_after_completion(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: str,
) -> None:
    """This fails if GIT_COMMON_DIR replacement survives either completion path."""
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    target = authority.common_directory
    original_capture = runner._capture_bounded_process
    displaced: Path | None = None

    class InjectedCaptureFailure(RuntimeError):
        pass

    def capture_then_replace(*args, **kwargs):
        nonlocal displaced
        output = original_capture(*args, **kwargs)
        displaced = _replace_directory_now(target)
        if completion == "exceptional":
            raise InjectedCaptureFailure("injected after completed Git capture")
        return output

    monkeypatch.setattr(runner, "_capture_bounded_process", capture_then_replace)
    observed: BaseException | None = None
    try:
        try:
            runner.run(["--version"], repository_authority=authority)
        except BaseException as error:  # noqa: BLE001 - the exception path is the contract
            observed = error
    finally:
        if displaced is not None:
            _restore_replaced_directory(target, displaced)
        authority.cleanup()

    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE


def test_repository_authority_cleanup_preserves_a_replaced_private_projection(
    repo, tmp_path: Path
) -> None:
    """This fails if cleanup deletes an untrusted replacement or claims exact removal."""
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    target = authority.common_directory
    descriptors = authority.file_descriptors
    displaced = _replace_directory_now(target)
    sentinel = target / "untrusted-replacement"
    sentinel.write_text("preserve\n", encoding="utf-8")
    observed: ManduaError | None = None

    try:
        try:
            authority.cleanup()
        except ManduaError as error:
            observed = error

        assert observed is not None
        assert observed.code is ErrorCode.GIT_FAILURE
        assert sentinel.read_text(encoding="utf-8") == "preserve\n"
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert authority not in runner._active_repository_authorities
    finally:
        if target.exists():
            shutil.rmtree(target)
        if displaced.exists():
            shutil.rmtree(displaced)


def test_repository_authority_cleanup_preserves_a_replacement_raced_after_validation(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if cleanup recursively deletes a root replaced after its last check."""
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    target = authority.common_directory
    displaced = target.with_name(f"{target.name}.cleanup-race-captured")
    sentinel = target / "replacement-must-survive"
    original_validate = runner._validate_authority_path
    private_validations = 0

    def validate_then_replace(source) -> None:
        nonlocal private_validations
        original_validate(source)
        if source is authority._private_common_path:
            private_validations += 1
            if private_validations == 2:
                target.rename(displaced)
                target.mkdir()
                sentinel.write_text("preserve replacement\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_validate_authority_path", validate_then_replace)

    try:
        with pytest.raises(ManduaError) as caught:
            authority.cleanup()

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert sentinel.read_text(encoding="utf-8") == "preserve replacement\n"
    finally:
        monkeypatch.setattr(runner, "_validate_authority_path", original_validate)
        if target.exists():
            shutil.rmtree(target)
        if displaced.exists():
            shutil.rmtree(displaced)


def test_repository_authority_cleanup_restores_a_replacement_raced_at_detach(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if atomic detach removes the wrong inode after its internal root check."""
    _runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    target = authority.common_directory
    displaced = target.with_name(f"{target.name}.detach-race-captured")
    sentinel = target / "replacement-must-survive"
    original_rename = git_runner_module.os.rename
    injected = False

    def replace_immediately_before_detach(source, destination, *args, **kwargs) -> None:
        nonlocal injected
        if (
            source == target.name
            and destination == "captured"
            and kwargs.get("src_dir_fd") is not None
            and not injected
        ):
            injected = True
            original_rename(target, displaced)
            target.mkdir()
            sentinel.write_text("preserve detach replacement\n", encoding="utf-8")
        original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(git_runner_module.os, "rename", replace_immediately_before_detach)

    try:
        with pytest.raises(ManduaError) as caught:
            authority.cleanup()

        assert injected is True
        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert sentinel.read_text(encoding="utf-8") == "preserve detach replacement\n"
        assert displaced.is_dir()
    finally:
        monkeypatch.setattr(git_runner_module.os, "rename", original_rename)
        if target.exists():
            shutil.rmtree(target)
        if displaced.exists():
            shutil.rmtree(displaced)


def test_repository_authority_setup_cleanup_preserves_a_replacement_race(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if setup cleanup can delete a replacement after validating its root."""
    runner = GitRunner(repo.path)
    original_authority = git_runner_module.GitRepositoryAuthority
    original_validate = runner._validate_authority_path
    target: Path | None = None
    displaced: Path | None = None
    sentinel: Path | None = None

    class InjectedSetupAbort(BaseException):
        pass

    def abort_after_projection(runner_arg, directory, **kwargs):
        nonlocal target
        del runner_arg, kwargs
        target = Path(directory.name)
        raise InjectedSetupAbort("injected after private projection capture")

    def validate_then_replace(source) -> None:
        nonlocal displaced, sentinel
        original_validate(source)
        if target is not None and source.path == target and displaced is None:
            displaced = target.with_name(f"{target.name}.setup-race-captured")
            target.rename(displaced)
            target.mkdir()
            sentinel = target / "replacement-must-survive"
            sentinel.write_text("preserve setup replacement\n", encoding="utf-8")

    monkeypatch.setattr(git_runner_module, "GitRepositoryAuthority", abort_after_projection)
    monkeypatch.setattr(runner, "_validate_authority_path", validate_then_replace)

    try:
        with pytest.raises(ManduaError) as caught:
            runner.open_repository_authority(runner.resolve_commit("HEAD"))

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert caught.value.message == "The private Git authority cleanup could not be verified."
        assert sentinel is not None
        assert sentinel.read_text(encoding="utf-8") == "preserve setup replacement\n"
    finally:
        monkeypatch.setattr(git_runner_module, "GitRepositoryAuthority", original_authority)
        monkeypatch.setattr(runner, "_validate_authority_path", original_validate)
        if target is not None and target.exists():
            shutil.rmtree(target)
        if displaced is not None and displaced.exists():
            shutil.rmtree(displaced)


def test_repository_authority_setup_base_exception_closes_root_descriptors(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if partial root capture leaks a descriptor after a BaseException."""
    runner = GitRunner(repo.path)
    original_capture = runner._capture_authority_path
    captured_descriptors: list[int] = []

    class InjectedSetupAbort(BaseException):
        pass

    def capture_then_abort(path: Path, label: str):
        if captured_descriptors:
            raise InjectedSetupAbort("injected during authority root capture")
        captured = original_capture(path, label)
        captured_descriptors.append(captured.descriptor)
        return captured

    monkeypatch.setattr(runner, "_capture_authority_path", capture_then_abort)

    with pytest.raises(ManduaError) as caught:
        runner.open_repository_authority(runner.resolve_commit("HEAD"))

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert len(captured_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(captured_descriptors[0])
    assert runner._active_repository_authorities == set()


def test_repository_authority_cleanup_base_exception_closes_every_descriptor(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if failed private-directory removal leaves authority descriptors active."""
    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(runner.resolve_commit("HEAD"))
    private_path = authority.common_directory
    descriptors = authority.file_descriptors

    class InjectedCleanupAbort(BaseException):
        pass

    def fail_cleanup() -> None:
        raise InjectedCleanupAbort("injected private-directory cleanup failure")

    monkeypatch.setattr(authority._directory, "cleanup", fail_cleanup)
    try:
        with pytest.raises(ManduaError) as caught:
            authority.cleanup()

        assert caught.value.code is ErrorCode.GIT_FAILURE
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert authority not in runner._active_repository_authorities
    finally:
        if private_path.exists():
            shutil.rmtree(private_path)


@pytest.mark.parametrize("failure_kind", ("oserror", "base-exception"))
def test_repository_authority_rechecks_roots_when_process_start_raises(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    """This fails if a Popen exception bypasses the final authority observation."""
    runner, authority, roots = _open_authority_layout(repo, tmp_path, linked=True)
    target = roots["git"]
    displaced: Path | None = None

    class InjectedProcessStartAbort(BaseException):
        pass

    failure: BaseException = (
        OSError("injected process-start failure")
        if failure_kind == "oserror"
        else InjectedProcessStartAbort("injected process-start control flow")
    )

    def replace_then_fail(*args, **kwargs):
        nonlocal displaced
        displaced = _replace_directory_now(target)
        raise failure

    monkeypatch.setattr("mandua.git_runner.subprocess.Popen", replace_then_fail)
    observed: ManduaError | None = None
    try:
        try:
            runner.run(["--version"], repository_authority=authority)
        except ManduaError as error:
            observed = error
    finally:
        if displaced is not None:
            _restore_replaced_directory(target, displaced)
        authority.cleanup()

    assert observed is not None
    assert observed.code is ErrorCode.GIT_FAILURE
    assert "authority path changed concurrently" in observed.message


@pytest.mark.parametrize(
    "child_name",
    (
        "config",
        "info",
        "refs",
        "objects",
        "logs",
        "worktrees",
        "packed-refs",
        "shallow",
    ),
)
def test_repository_authority_rejects_private_child_changes_before_process_start(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_name: str,
) -> None:
    """This fails if a private trust-bearing child can change before Git starts."""
    repo.git("pack-refs", "--all")
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    child = authority.common_directory / child_name
    marker = tmp_path / f"private-child-command-started-{child_name}"
    fake_bin = tmp_path / f"private-child-bin-{child_name}"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        '#!/bin/sh\n: > "$MANDUA_AUTHORITY_TEST_MARKER"\nexit 0\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("MANDUA_AUTHORITY_TEST_MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    restore: tuple[str, object]
    if child_name == "config":
        original = child.read_bytes()
        child.write_bytes(original + b"[alias]\n\tx = status\n")
        restore = ("file", original)
    elif child_name == "info":
        addition = child / "attributes"
        addition.write_bytes(b"* filter=unsafe\n")
        restore = ("addition", addition)
    elif child_name in {"refs", "objects", "logs", "worktrees"}:
        displaced = child.with_name(f"{child.name}.mandua-captured")
        replacement_target = tmp_path / f"replacement-{child_name}"
        replacement_target.mkdir()
        child.rename(displaced)
        child.symlink_to(replacement_target, target_is_directory=True)
        restore = ("symlink", displaced)
    elif child_name == "packed-refs":
        original = child.read_bytes()
        child.write_bytes(original + b"# tampered\n")
        restore = ("file", original)
    else:
        assert child_name == "shallow"
        child.write_text("0" * 40 + "\n", encoding="ascii")
        restore = ("addition", child)

    observed: ManduaError | None = None
    try:
        try:
            runner.run(["--version"], repository_authority=authority)
        except ManduaError as error:
            observed = error
    finally:
        kind, value = restore
        if kind == "file":
            assert isinstance(value, bytes)
            child.write_bytes(value)
        elif kind == "addition":
            assert isinstance(value, Path)
            value.unlink()
        else:
            assert kind == "symlink"
            assert isinstance(value, Path)
            child.unlink()
            value.rename(child)
        authority.cleanup()

    assert observed is not None
    assert observed.code is ErrorCode.GIT_FAILURE
    assert not marker.exists()


@pytest.mark.parametrize("completion", ("normal", "exceptional", "process-start"))
def test_repository_authority_rechecks_private_children_at_every_process_boundary(
    repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: str,
) -> None:
    """This fails if private config mutation survives a process completion boundary."""
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    config = authority.common_directory / "config"
    original_config = config.read_bytes()

    class InjectedCaptureFailure(RuntimeError):
        pass

    if completion == "process-start":

        def mutate_then_fail(*args, **kwargs):
            config.write_bytes(original_config + b"[alias]\n\tx = status\n")
            raise OSError("injected process-start failure")

        monkeypatch.setattr("mandua.git_runner.subprocess.Popen", mutate_then_fail)
    else:
        original_capture = runner._capture_bounded_process

        def capture_then_mutate(*args, **kwargs):
            output = original_capture(*args, **kwargs)
            config.write_bytes(original_config + b"[alias]\n\tx = status\n")
            if completion == "exceptional":
                raise InjectedCaptureFailure("injected after private config mutation")
            return output

        monkeypatch.setattr(runner, "_capture_bounded_process", capture_then_mutate)

    observed: BaseException | None = None
    try:
        try:
            runner.run(["--version"], repository_authority=authority)
        except BaseException as error:  # noqa: BLE001 - control flow is the contract
            observed = error
    finally:
        config.write_bytes(original_config)
        authority.cleanup()

    assert isinstance(observed, ManduaError)
    assert observed.code is ErrorCode.GIT_FAILURE
    assert "private" in observed.message.lower()


def test_repository_authority_private_namespace_scan_is_finite(
    repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if additions are accumulated before the private namespace bound is checked."""
    runner, authority, _ = _open_authority_layout(repo, tmp_path, linked=True)
    captured_entry_count = len(tuple(authority.common_directory.iterdir()))
    additions = [authority.common_directory / f"addition-{index}" for index in range(3)]
    for addition in additions:
        addition.write_bytes(b"untrusted\n")
    monkeypatch.setattr(
        git_runner_module,
        "_MAX_AUTHORITY_DIRECTORY_ENTRIES",
        captured_entry_count + 1,
        raising=False,
    )

    try:
        with pytest.raises(ManduaError) as caught:
            runner.run(["--version"], repository_authority=authority)
    finally:
        for addition in additions:
            addition.unlink()
        authority.cleanup()

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_repository_authority_cleanup_preserves_a_replaced_private_child(repo) -> None:
    """This fails if cleanup removes a private child replacement it did not capture."""
    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(runner.resolve_commit("HEAD"))
    root = authority.common_directory
    config = root / "config"
    displaced = root / "config.mandua-captured"
    config.rename(displaced)
    config.write_bytes(b"untrusted replacement\n")

    try:
        with pytest.raises(ManduaError) as caught:
            authority.cleanup()

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert config.read_bytes() == b"untrusted replacement\n"
        assert displaced.exists()
    finally:
        if root.exists():
            shutil.rmtree(root)


def _replace_index_with_copy(path: Path) -> Path:
    displaced = path.with_name("index.mandua-captured")
    contents = path.read_bytes()
    path.rename(displaced)
    path.write_bytes(contents)
    return displaced


@pytest.mark.parametrize("completion", ("normal", "exceptional"))
def test_temporary_index_rechecks_snapshot_after_read_only_command(
    repo, monkeypatch: pytest.MonkeyPatch, completion: str
) -> None:
    """This fails if a read-only index consumer can return after index replacement."""
    runner = GitRunner(repo.path)
    original_capture = runner._capture_bounded_process
    displaced: Path | None = None

    class InjectedCaptureFailure(RuntimeError):
        pass

    with runner.temporary_index() as index_file:
        runner.run(["read-tree", "HEAD"], index_file=index_file)

        def capture_then_replace(*args, **kwargs):
            nonlocal displaced
            output = original_capture(*args, **kwargs)
            displaced = _replace_index_with_copy(index_file.path)
            if completion == "exceptional":
                raise InjectedCaptureFailure("injected after index replacement")
            return output

        monkeypatch.setattr(runner, "_capture_bounded_process", capture_then_replace)
        observed: BaseException | None = None
        try:
            runner.run(
                ["check-attr", "--cached", "-z", "--all", "--stdin"],
                input_bytes=b"README\x00",
                index_file=index_file,
            )
        except BaseException as error:  # noqa: BLE001 - control flow is the contract
            observed = error
        finally:
            if displaced is not None:
                index_file.path.unlink()
                displaced.rename(index_file.path)

        assert isinstance(observed, ManduaError)
        assert observed.code is ErrorCode.GIT_FAILURE


def test_temporary_index_rejects_same_inode_content_mutation_before_consumer(repo) -> None:
    """This fails if index identity is checked without a bounded content digest."""
    runner = GitRunner(repo.path)

    with runner.temporary_index() as index_file:
        runner.run(["read-tree", "HEAD"], index_file=index_file)
        original = index_file.path.read_bytes()
        alternate_contents = bytes([original[0] ^ 0xFF]) + original[1:]
        before = index_file.path.stat()
        with index_file.path.open("r+b") as stream:
            stream.seek(0)
            stream.write(alternate_contents)
            stream.truncate()
        after = index_file.path.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

        observed: ManduaError | None = None
        try:
            runner.run(
                ["check-attr", "--cached", "-z", "--all", "--stdin"],
                input_bytes=b"README\x00",
                index_file=index_file,
            )
        except ManduaError as error:
            observed = error
        finally:
            with index_file.path.open("r+b") as stream:
                stream.seek(0)
                stream.write(original)
                stream.truncate()

        assert observed is not None
        assert observed.code is ErrorCode.GIT_FAILURE


def test_write_tree_records_its_cache_tree_mutation_before_read_only_consumers(repo) -> None:
    """This fails if Git's legitimate write-tree refresh leaves a stale index snapshot."""
    runner = GitRunner(repo.path)

    with runner.temporary_index() as index_file:
        runner.run(["read-tree", "HEAD"], index_file=index_file)

        tree = runner.run_text(["write-tree"], index_file=index_file).stdout.strip()
        after = runner._index_states[index_file.path].snapshot
        runner.run(
            ["check-attr", "--cached", "-z", "--all", "--stdin"],
            input_bytes=b"README\x00",
            index_file=index_file,
        )

        assert tree == repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
        assert after == runner._capture_index_snapshot(runner._index_states[index_file.path])


@pytest.mark.parametrize("failure_kind", ("oserror", "base-exception"))
def test_temporary_index_rechecks_parent_when_process_start_fails(
    repo, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    """This fails if a parent replacement is hidden behind a generic process-start error."""
    runner = GitRunner(repo.path)

    with runner.temporary_index() as index_file:
        runner.run(["read-tree", "HEAD"], index_file=index_file)
        parent = index_file.path.parent
        displaced: Path | None = None

        class InjectedProcessStartAbort(BaseException):
            pass

        failure: BaseException = (
            OSError("injected index process-start failure")
            if failure_kind == "oserror"
            else InjectedProcessStartAbort("injected index process-start control flow")
        )

        def replace_parent_then_fail(*args, **kwargs):
            nonlocal displaced
            displaced = _replace_directory_now(parent)
            shutil.copy2(displaced / "index", parent / "index")
            raise failure

        monkeypatch.setattr("mandua.git_runner.subprocess.Popen", replace_parent_then_fail)
        observed: ManduaError | None = None
        try:
            runner.run(["write-tree"], index_file=index_file)
        except ManduaError as error:
            observed = error
        finally:
            if displaced is not None:
                _restore_replaced_directory(parent, displaced)

        assert observed is not None
        assert observed.code is ErrorCode.GIT_FAILURE
        assert "index" in observed.message.lower()
        assert "changed" in observed.message.lower()


def test_process_start_base_exception_is_preserved_after_stable_authority_rechecks(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if stable revalidation rewrites non-error process-start control flow."""
    runner = GitRunner(repo.path)
    authority = runner.open_repository_authority(runner.resolve_commit("HEAD"))

    class InjectedProcessStartAbort(BaseException):
        pass

    failure = InjectedProcessStartAbort("injected stable process-start control flow")

    try:
        with runner.temporary_index() as index_file:
            runner.run(["read-tree", "HEAD"], index_file=index_file)

            def interrupt_process_start(*args, **kwargs):
                raise failure

            monkeypatch.setattr("mandua.git_runner.subprocess.Popen", interrupt_process_start)
            with pytest.raises(InjectedProcessStartAbort) as caught:
                runner.run(
                    ["write-tree"],
                    index_file=index_file,
                    repository_authority=authority,
                )

            assert caught.value is failure
    finally:
        authority.cleanup()


def test_temporary_index_rejects_unexpected_namespace_additions(repo) -> None:
    """This fails if an unrelated sibling can appear between producer and consumer."""
    runner = GitRunner(repo.path)

    with runner.temporary_index() as index_file:
        runner.run(["read-tree", "HEAD"], index_file=index_file)
        addition = index_file.path.parent / "untrusted-sibling"
        addition.write_bytes(b"untrusted\n")
        try:
            with pytest.raises(ManduaError) as caught:
                runner.run(["write-tree"], index_file=index_file)
        finally:
            addition.unlink()

    assert caught.value.code is ErrorCode.GIT_FAILURE


def test_temporary_index_cleanup_preserves_a_replaced_index(repo) -> None:
    """This fails if temporary-index cleanup deletes an untrusted replacement."""
    runner = GitRunner(repo.path)
    root: Path | None = None
    replacement: Path | None = None

    try:
        with pytest.raises(ManduaError) as caught, runner.temporary_index() as index_file:
            runner.run(["read-tree", "HEAD"], index_file=index_file)
            root = index_file.path.parent
            _replace_index_with_copy(index_file.path)
            replacement = index_file.path

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert replacement is not None
        assert replacement.exists()
    finally:
        if root is not None and root.exists():
            shutil.rmtree(root)


def test_temporary_index_cleanup_preserves_a_root_replaced_after_validation(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if index cleanup recursively deletes a raced root replacement."""
    runner = GitRunner(repo.path)
    original_require = runner._require_index_snapshot
    root: Path | None = None
    displaced: Path | None = None
    sentinel: Path | None = None
    armed = False

    def validate_then_replace(state, expected) -> None:
        nonlocal armed, displaced, sentinel
        original_require(state, expected)
        if armed:
            armed = False
            displaced = state.path.parent.with_name(
                f"{state.path.parent.name}.cleanup-race-captured"
            )
            state.path.parent.rename(displaced)
            state.path.parent.mkdir()
            sentinel = state.path.parent / "replacement-must-survive"
            sentinel.write_text("preserve index replacement\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_require_index_snapshot", validate_then_replace)

    try:
        with pytest.raises(ManduaError) as caught, runner.temporary_index() as index_file:
            runner.run(["read-tree", "HEAD"], index_file=index_file)
            root = index_file.path.parent
            armed = True

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert sentinel is not None
        assert sentinel.read_text(encoding="utf-8") == "preserve index replacement\n"
    finally:
        monkeypatch.setattr(runner, "_require_index_snapshot", original_require)
        if root is not None and root.exists():
            shutil.rmtree(root)
        if displaced is not None and displaced.exists():
            shutil.rmtree(displaced)


def test_temporary_index_cleanup_restores_a_replacement_raced_at_detach(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if index detach deletes a replacement raced after its internal check."""
    runner = GitRunner(repo.path)
    original_rename = git_runner_module.os.rename
    root: Path | None = None
    displaced: Path | None = None
    sentinel: Path | None = None
    injected = False

    def replace_immediately_before_detach(source, destination, *args, **kwargs) -> None:
        nonlocal injected, displaced, sentinel
        if (
            root is not None
            and source == root.name
            and destination == "captured"
            and kwargs.get("src_dir_fd") is not None
            and not injected
        ):
            injected = True
            displaced = root.with_name(f"{root.name}.detach-race-captured")
            original_rename(root, displaced)
            root.mkdir()
            sentinel = root / "replacement-must-survive"
            sentinel.write_text("preserve detach replacement\n", encoding="utf-8")
        original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(git_runner_module.os, "rename", replace_immediately_before_detach)

    try:
        with pytest.raises(ManduaError) as caught, runner.temporary_index() as index_file:
            runner.run(["read-tree", "HEAD"], index_file=index_file)
            root = index_file.path.parent

        assert injected is True
        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert sentinel is not None
        assert sentinel.read_text(encoding="utf-8") == "preserve detach replacement\n"
        assert displaced is not None
        assert displaced.is_dir()
    finally:
        monkeypatch.setattr(git_runner_module.os, "rename", original_rename)
        if root is not None and root.exists():
            shutil.rmtree(root)
        if displaced is not None and displaced.exists():
            shutil.rmtree(displaced)


def test_temporary_index_setup_cleanup_preserves_a_replaced_root(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if setup failure cleanup recursively deletes an untrusted root."""
    runner = GitRunner(repo.path)
    original_chmod = git_runner_module.os.chmod
    target: Path | None = None
    displaced: Path | None = None
    sentinel: Path | None = None

    class InjectedSetupAbort(BaseException):
        pass

    def replace_then_abort(path, mode) -> None:
        nonlocal target, displaced, sentinel
        candidate = Path(path)
        if candidate.name.startswith("mandua-git-index-"):
            target = candidate
            displaced = candidate.with_name(f"{candidate.name}.setup-race-captured")
            candidate.rename(displaced)
            candidate.mkdir()
            sentinel = candidate / "replacement-must-survive"
            sentinel.write_text("preserve setup replacement\n", encoding="utf-8")
            raise InjectedSetupAbort("injected temporary-index setup abort")
        original_chmod(path, mode)

    monkeypatch.setattr(git_runner_module.os, "chmod", replace_then_abort)

    try:
        with pytest.raises(ManduaError) as caught, runner.temporary_index():
            pass

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert caught.value.message == "The temporary Git index cleanup could not be verified."
        assert sentinel is not None
        assert sentinel.read_text(encoding="utf-8") == "preserve setup replacement\n"
    finally:
        monkeypatch.setattr(git_runner_module.os, "chmod", original_chmod)
        if target is not None and target.exists():
            shutil.rmtree(target)
        if displaced is not None and displaced.exists():
            shutil.rmtree(displaced)
