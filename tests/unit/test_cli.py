"""Unit tests for the complete Mandu'a command-line boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath

import pytest

from mandua import cli
from mandua.errors import ErrorCode, ManduaError
from mandua.models import (
    AnnotationRequest,
    CheckpointRequest,
    CommitMetadata,
    CorrectionRequest,
    IntegrationRequest,
    MemoryResult,
)


class _RecordingService:
    """Record the typed public calls made by the CLI."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.canonical_branch = "main"

    def __getattr__(self, name: str):
        def operation(*args: object, **kwargs: object) -> MemoryResult:
            self.calls.append((name, args, kwargs))
            return MemoryResult(operation=name, answer=f"Rendered {name}.")

        return operation


def _install_service(monkeypatch, service: _RecordingService) -> list[Path]:
    opened: list[Path] = []

    class Factory:
        @classmethod
        def open(cls, repository: Path) -> _RecordingService:
            opened.append(repository)
            return service

    monkeypatch.setattr(cli, "MemoryService", Factory)
    return opened


@pytest.mark.parametrize(
    ("arguments", "operation", "expected_args", "expected_kwargs"),
    (
        (["status"], "status", (), {}),
        (
            ["context", "--task-id", "TASK-7", "--branch", "task/sensor", "--limit", "7"],
            "context",
            (),
            {"task_id": "TASK-7", "branch": "task/sensor", "limit": 7},
        ),
        (
            [
                "timeline",
                "--path",
                "knowledge/rules.md",
                "--from",
                "start-ref",
                "--to",
                "end-ref",
                "--limit",
                "8",
            ],
            "timeline",
            (),
            {
                "path": PurePosixPath("knowledge/rules.md"),
                "start": "start-ref",
                "end": "end-ref",
                "limit": 8,
            },
        ),
        (
            [
                "why",
                "--path",
                "knowledge/rules.md",
                "--line",
                "3",
                "--revision",
                "topic",
            ],
            "why",
            (),
            {
                "path": PurePosixPath("knowledge/rules.md"),
                "line": 3,
                "revision": "topic",
            },
        ),
        (
            [
                "origin",
                "--text",
                "Water at dawn.",
                "--path",
                "knowledge/rules.md",
                "--limit",
                "9",
            ],
            "origin",
            ("Water at dawn.",),
            {"path": PurePosixPath("knowledge/rules.md"), "limit": 9},
        ),
        (
            ["evolution", "--path", "knowledge/rules.md", "--limit", "10"],
            "evolution",
            (PurePosixPath("knowledge/rules.md"),),
            {"limit": 10},
        ),
        (
            [
                "compare",
                "hypothesis/left",
                "hypothesis/right",
                "--path",
                "knowledge/rules.md",
                "--limit",
                "11",
            ],
            "compare",
            ("hypothesis/left", "hypothesis/right"),
            {"path": PurePosixPath("knowledge/rules.md"), "limit": 11},
        ),
        (
            ["decision", "DEC-IRR-1", "--limit", "12"],
            "decision",
            ("DEC-IRR-1",),
            {"limit": 12},
        ),
        (
            ["recover", "--query", "lost observation", "--create-branch", "recovery/lost"],
            "recover",
            (),
            {"query": "lost observation", "create_branch": "recovery/lost", "apply": False},
        ),
    ),
)
def test_cli_maps_each_read_operation_to_the_public_service_contract(
    monkeypatch,
    capsys,
    arguments,
    operation,
    expected_args,
    expected_kwargs,
) -> None:
    """This fails if a CLI spelling, conversion, or default reaches the wrong service call."""
    service = _RecordingService()
    opened = _install_service(monkeypatch, service)

    code = cli.main(["--repo", "relative-repository", *arguments, "--format", "json"])
    captured = capsys.readouterr()

    assert code == 0
    assert opened == [Path("relative-repository")]
    assert service.calls == [(operation, expected_args, expected_kwargs)]
    assert json.loads(captured.out)["operation"] == operation
    assert captured.err == ""


def test_cli_preserves_documented_read_defaults(monkeypatch, capsys) -> None:
    """This fails if CLI defaults diverge from the frozen MemoryService defaults."""
    service = _RecordingService()
    _install_service(monkeypatch, service)
    cases = (
        (["context"], "context", {"task_id": None, "branch": None, "limit": 100}),
        (
            ["timeline"],
            "timeline",
            {"path": None, "start": None, "end": "HEAD", "limit": 100},
        ),
        (
            ["why", "--path", "knowledge/rules.md", "--line", "1"],
            "why",
            {"path": PurePosixPath("knowledge/rules.md"), "line": 1, "revision": "HEAD"},
        ),
        (["origin", "--text", "rule"], "origin", {"path": None, "limit": 100}),
        (
            ["evolution", "--path", "knowledge/rules.md"],
            "evolution",
            {"limit": 100},
        ),
        (["compare", "main", "topic"], "compare", {"path": None, "limit": 100}),
        (["decision", "DEC-1"], "decision", {"limit": 500}),
        (
            ["recover"],
            "recover",
            {"query": None, "create_branch": None, "apply": False},
        ),
    )

    for arguments, operation, expected_kwargs in cases:
        assert cli.main(["--repo", ".", *arguments, "--format", "json"]) == 0
        capsys.readouterr()
        name, _args, kwargs = service.calls[-1]
        assert name == operation
        assert kwargs == expected_kwargs


def test_cli_builds_a_typed_checkpoint_request_with_repeatable_metadata(
    monkeypatch, capsys
) -> None:
    """This fails if checkpoint paths or trailers are collapsed or a Namespace crosses the API."""
    service = _RecordingService()
    _install_service(monkeypatch, service)

    code = cli.main(
        [
            "--repo",
            ".",
            "checkpoint",
            "--path",
            "knowledge/rules.md",
            "--path",
            "knowledge/observations.md",
            "--message",
            "Record the moisture threshold",
            "--reason",
            "The sensor trial reduced water use.",
            "--memory-type",
            "decision",
            "--scope",
            "irrigation",
            "--task-id",
            "TASK-IRR-7",
            "--decision-id",
            "DEC-IRR-1",
            "--agent-id",
            "gardener",
            "--trailer",
            "Review-State=pending",
            "--trailer",
            "Reviewer=operator",
            "--apply",
            "--format",
            "json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    name, args, kwargs = service.calls[0]
    assert name == "checkpoint"
    assert kwargs == {"apply": True}
    assert len(args) == 1
    request = args[0]
    assert isinstance(request, CheckpointRequest)
    assert not isinstance(request, argparse.Namespace)
    assert request == CheckpointRequest(
        subject="Record the moisture threshold",
        metadata=CommitMetadata(
            memory_type="decision",
            scope="irrigation",
            agent_id="gardener",
            task_id="TASK-IRR-7",
            decision_id="DEC-IRR-1",
            reason="The sensor trial reduced water use.",
            extra_trailers=(("Review-State", "pending"), ("Reviewer", "operator")),
        ),
        paths=(
            PurePosixPath("knowledge/rules.md"),
            PurePosixPath("knowledge/observations.md"),
        ),
    )


def test_cli_builds_typed_annotation_integration_and_correction_requests(
    monkeypatch, capsys
) -> None:
    """This fails if any mutation forwards parser internals or loses its public fields."""
    service = _RecordingService()
    _install_service(monkeypatch, service)
    metadata = [
        "--message",
        "Apply the recorded transition",
        "--reason",
        "The recorded evidence supports the transition.",
        "--memory-type",
        "decision",
        "--scope",
        "irrigation",
        "--task-id",
        "TASK-8",
        "--decision-id",
        "DEC-8",
        "--agent-id",
        "gardener",
        "--trailer",
        "Review-State=pending",
    ]

    assert (
        cli.main(
            [
                "--repo",
                ".",
                "annotate",
                "HEAD~1",
                "--message",
                "Review confirms the rule.",
                "--agent-id",
                "reviewer",
                "--apply",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--repo",
                ".",
                "integrate",
                "hypothesis/sensor",
                *metadata,
                "--apply",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--repo",
                ".",
                "correct",
                "incorrect-ref",
                "--staged",
                *metadata,
                "--apply",
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    annotation = service.calls[0]
    assert annotation == (
        "annotate",
        (AnnotationRequest("HEAD~1", "Review confirms the rule.", "reviewer"),),
        {"apply": True},
    )
    integration = service.calls[1]
    assert integration[0] == "integrate"
    assert integration[2] == {"apply": True}
    assert integration[1] == (
        IntegrationRequest(
            source="hypothesis/sensor",
            target="main",
            subject="Apply the recorded transition",
            metadata=CommitMetadata(
                memory_type="decision",
                scope="irrigation",
                task_id="TASK-8",
                decision_id="DEC-8",
                agent_id="gardener",
                reason="The recorded evidence supports the transition.",
                extra_trailers=(("Review-State", "pending"),),
            ),
        ),
    )
    correction = service.calls[2]
    assert correction[0] == "correct"
    assert correction[2] == {"apply": True}
    assert correction[1] == (
        CorrectionRequest(
            incorrect_revision="incorrect-ref",
            subject="Apply the recorded transition",
            metadata=CommitMetadata(
                memory_type="decision",
                scope="irrigation",
                task_id="TASK-8",
                decision_id="DEC-8",
                agent_id="gardener",
                reason="The recorded evidence supports the transition.",
                extra_trailers=(("Review-State", "pending"),),
            ),
            staged=True,
        ),
    )
    assert all(
        not isinstance(value, argparse.Namespace)
        for _name, args, kwargs in service.calls
        for value in (*args, *kwargs.values())
    )


def test_cli_converts_correction_paths_and_explicit_target(monkeypatch, capsys) -> None:
    """This fails if repeatable correction paths or an explicit integration target are lost."""
    service = _RecordingService()
    _install_service(monkeypatch, service)
    common = [
        "--message",
        "Record the transition",
        "--memory-type",
        "decision",
        "--scope",
        "irrigation",
        "--agent-id",
        "gardener",
    ]

    assert (
        cli.main(
            [
                "--repo",
                ".",
                "integrate",
                "source",
                "--target",
                "canonical",
                *common,
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--repo",
                ".",
                "correct",
                "bad",
                "--path",
                "knowledge/one.md",
                "--path",
                "knowledge/two.md",
                *common,
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    integration = service.calls[0][1][0]
    correction = service.calls[1][1][0]
    assert isinstance(integration, IntegrationRequest)
    assert integration.target == "canonical"
    assert isinstance(correction, CorrectionRequest)
    assert correction.paths == (
        PurePosixPath("knowledge/one.md"),
        PurePosixPath("knowledge/two.md"),
    )
    assert correction.staged is False


def test_cli_omitted_integration_target_comes_from_the_open_service_policy(
    monkeypatch, capsys
) -> None:
    """This fails if argparse hard-codes main before repository policy is available."""
    service = _RecordingService()
    service.canonical_branch = "trunk"
    _install_service(monkeypatch, service)

    code = cli.main(
        [
            "--repo",
            ".",
            "integrate",
            "source",
            "--message",
            "Record the transition",
            "--memory-type",
            "integration",
            "--scope",
            "irrigation",
            "--agent-id",
            "gardener",
            "--format",
            "json",
        ]
    )

    assert code == 0
    capsys.readouterr()
    request = service.calls[0][1][0]
    assert isinstance(request, IntegrationRequest)
    assert request.target == "trunk"


@pytest.mark.parametrize(
    "arguments",
    (
        ["status"],
        ["--repo", ".", "status", "--repo", "elsewhere"],
        ["--repo", ".", "--format", "json", "status"],
        ["--repo", ".", "checkpoint", "--message", "Missing selection"],
        [
            "--repo",
            ".",
            "checkpoint",
            "--path",
            "knowledge/rules.md",
            "--staged",
            "--message",
            "Ambiguous selection",
        ],
        ["--repo", ".", "correct", "bad", "--message", "Missing selection"],
        [
            "--repo",
            ".",
            "correct",
            "bad",
            "--path",
            "knowledge/rules.md",
            "--staged",
            "--message",
            "Ambiguous selection",
        ],
        ["--repo", ".", "recover", "--apply"],
        ["--repo", ".", "context", "--limit", "not-an-integer"],
        ["--repo", ".", "context", "--limit", "0"],
        ["--repo", ".", "checkpoint", "--staged"],
        [
            "--repo",
            ".",
            "checkpoint",
            "--staged",
            "--message",
            "Record a checkpoint",
            "--memory-type",
            "decision",
            "--scope",
            "irrigation",
            "--agent-id",
            "gardener",
            "--trailer",
            "missing-separator",
        ],
    ),
)
def test_cli_rejects_invalid_user_input_with_exit_two(arguments, capsys) -> None:
    """This fails if invalid syntax reaches the service or escapes as SystemExit."""
    code = cli.main(arguments)
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert "error:" in captured.err.lower()
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("code", "expected_exit"),
    (
        (ErrorCode.INVALID_REPOSITORY, 2),
        (ErrorCode.INVALID_REVISION, 2),
        (ErrorCode.INVALID_PATH, 2),
        (ErrorCode.MISSING_NOTES, 2),
        (ErrorCode.LIMIT_EXCEEDED, 2),
        (ErrorCode.INCOMPLETE_HISTORY, 3),
        (ErrorCode.UNSUPPORTED_CAPABILITY, 3),
        (ErrorCode.GIT_FAILURE, 4),
        (ErrorCode.MISSING_OBJECT, 4),
        (ErrorCode.CONFLICT, 5),
        (ErrorCode.POLICY_VIOLATION, 5),
        (ErrorCode.VALIDATION_FAILED, 5),
    ),
)
@pytest.mark.parametrize("output_format", ("human", "json"))
def test_cli_renders_stable_expected_errors_to_stderr(
    monkeypatch, capsys, code, expected_exit, output_format
) -> None:
    """This fails if an expected error changes channel, shape, or documented exit class."""
    error = ManduaError(code, "A safe failure occurred.", recovery="Use a valid local input.")

    class Factory:
        @classmethod
        def open(cls, repository: Path):
            raise error

    monkeypatch.setattr(cli, "MemoryService", Factory)

    exit_code = cli.main(["--repo", ".", "status", "--format", output_format])
    captured = capsys.readouterr()

    assert exit_code == expected_exit
    assert captured.out == ""
    assert "Traceback" not in captured.err
    if output_format == "json":
        assert json.loads(captured.err) == error.to_dict()
    else:
        assert "Error:\nA safe failure occurred." in captured.err
        assert f"Code:\n{code.value}" in captured.err


def test_cli_uses_human_output_by_default(monkeypatch, capsys) -> None:
    """This fails if the default output bypasses the shared MemoryResult renderer."""
    service = _RecordingService()
    _install_service(monkeypatch, service)

    assert cli.main(["--repo", ".", "status"]) == 0
    captured = capsys.readouterr()

    assert "Answer:\nRendered status." in captured.out
    assert captured.err == ""


def test_cli_hides_unexpected_exception_details_by_default(monkeypatch, capsys) -> None:
    """This fails if a non-debug run leaks a stack trace or internal exception text."""
    monkeypatch.delenv("MANDUA_DEBUG", raising=False)
    monkeypatch.delenv("CI", raising=False)

    class Factory:
        @classmethod
        def open(cls, repository: Path):
            raise RuntimeError("private internal detail")

    monkeypatch.setattr(cli, "MemoryService", Factory)

    code = cli.main(["--repo", ".", "status", "--format", "json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert code == 4
    assert captured.out == ""
    assert payload["code"] == "git_failure"
    assert payload["message"] == "An unexpected internal error occurred."
    assert "private internal detail" not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("ci_value", "traceback_visible"),
    ((None, True), ("", False), ("1", False)),
)
def test_cli_allows_debug_tracebacks_only_outside_ci(
    monkeypatch, capsys, ci_value, traceback_visible
) -> None:
    """This fails if debug is ignored outside CI or can leak a traceback inside CI."""
    monkeypatch.setenv("MANDUA_DEBUG", "1")
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)

    class Factory:
        @classmethod
        def open(cls, repository: Path):
            raise RuntimeError("debug-only detail")

    monkeypatch.setattr(cli, "MemoryService", Factory)

    code = cli.main(["--repo", ".", "status", "--format", "human"])
    captured = capsys.readouterr()

    assert code == 4
    assert captured.out == ""
    assert ("Traceback" in captured.err) is traceback_visible
    assert ("debug-only detail" in captured.err) is traceback_visible


@pytest.mark.parametrize("control_flow", (KeyboardInterrupt(), SystemExit(9)))
def test_cli_does_not_catch_process_control_flow_as_an_ordinary_exception(
    monkeypatch, capsys, control_flow
) -> None:
    """This fails if KeyboardInterrupt or SystemExit is converted into a CLI error object."""

    class Factory:
        @classmethod
        def open(cls, repository: Path):
            raise control_flow

    monkeypatch.setattr(cli, "MemoryService", Factory)

    with pytest.raises(type(control_flow)) as caught:
        cli.main(["--repo", ".", "status", "--format", "json"])

    captured = capsys.readouterr()
    assert caught.value is control_flow
    assert captured.out == ""
    assert captured.err == ""
