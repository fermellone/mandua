"""Complete English command-line interface for Mandu'a."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import NoReturn

from mandua.demo import DemoReport, DemoScenario
from mandua.errors import ErrorCode, ManduaError
from mandua.hooks import run_hook
from mandua.memory_service import MemoryService
from mandua.models import (
    AnnotationRequest,
    CheckpointRequest,
    CommitMetadata,
    CorrectionRequest,
    IntegrationRequest,
    MemoryResult,
    QueryLimits,
)
from mandua.renderers import render_agent, render_human, render_json

_INCOMPLETE_EXIT_CODES = {
    ErrorCode.INCOMPLETE_HISTORY,
    ErrorCode.UNSUPPORTED_CAPABILITY,
}
_GIT_EXIT_CODES = {
    ErrorCode.GIT_FAILURE,
    ErrorCode.MISSING_OBJECT,
}
_CONFLICT_EXIT_CODES = {
    ErrorCode.CONFLICT,
    ErrorCode.POLICY_VIOLATION,
    ErrorCode.VALIDATION_FAILED,
}


class _UsageError(Exception):
    """One argparse validation failure that main converts to exit code two."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _ArgumentParser(argparse.ArgumentParser):
    """Return parser failures through main without treating SystemExit as ordinary failure."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(self, message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one Mandu'a operation and return its stable process exit code."""
    raw_arguments = tuple(sys.argv[1:] if argv is None else argv)
    if raw_arguments and raw_arguments[0] == "policy-hook":
        return _run_policy_hook_cli(raw_arguments[1:])
    parser = _build_parser()
    try:
        arguments = parser.parse_args(raw_arguments)
        _validate_cross_options(parser, arguments)
    except _UsageError as error:
        _render_usage_error(error)
        return 2

    try:
        if arguments.operation == "demo":
            report = DemoScenario().build(arguments.output)
            _write_demo_report(report, arguments.output_format)
        else:
            service = MemoryService.open(arguments.repo)
            result = _run_operation(service, arguments)
            _write_result(result, arguments.output_format)
        return 0
    except ManduaError as error:
        _write_error(error, arguments.output_format)
        return _exit_code(error.code)
    except Exception:  # noqa: BLE001 - ordinary failures need one stable CLI boundary.
        error = ManduaError(
            ErrorCode.GIT_FAILURE,
            "An unexpected internal error occurred.",
            recovery="Set MANDUA_DEBUG=1 outside CI to view diagnostic details.",
        )
        _write_error(error, arguments.output_format)
        if os.environ.get("MANDUA_DEBUG") == "1" and "CI" not in os.environ:
            traceback.print_exc(file=sys.stderr)
        return 4


def _run_policy_hook_cli(arguments: tuple[str, ...]) -> int:
    """Dispatch one hidden hook without changing the public argparse surface."""
    if not arguments or not isinstance(arguments[0], str):
        _write_hook_cli_error("The Git hook invocation is invalid.")
        return 1
    name = arguments[0]
    try:
        stdin = ""
        if name == "pre-push":
            stdin = sys.stdin.read(QueryLimits().max_output_bytes + 1)
            if not isinstance(stdin, str):
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook input is invalid.")
        return run_hook(Path.cwd(), name, tuple(arguments[1:]), stdin)
    except ManduaError as error:
        _write_hook_cli_error(error.message)
        return 1
    except Exception:  # noqa: BLE001 - hidden hooks never expose traceback details.
        _write_hook_cli_error("The Git hook failed because of an unexpected internal error.")
        return 1


def _write_hook_cli_error(message: str) -> None:
    safe = message.encode("ascii", errors="replace").decode("ascii")[:400]
    print(f"mandua: {safe}", file=sys.stderr)


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="mandua",
        description="Derive verifiable, local memory from one Git repository.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--repo",
        type=Path,
        metavar="PATH",
        help="repository to inspect or update; required before repository operations",
    )
    operations = parser.add_subparsers(
        dest="operation",
        required=True,
        metavar="OPERATION",
        title="operations",
    )

    status = _add_operation(operations, "status", "report repository and history status")
    _add_format(status)

    demo = _add_operation(operations, "demo", "build the reproducible local garden demo")
    demo.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="new or empty directory to retain; otherwise a temporary directory is retained",
    )
    _add_format(demo, allow_agent=False)

    context = _add_operation(operations, "context", "reconstruct bounded working context")
    context.add_argument("--task-id", help="exact Task-ID trailer to select")
    context.add_argument("--branch", help="local branch whose delta should be reconstructed")
    context.add_argument("--limit", type=_positive_integer, default=100, metavar="COUNT")
    _add_format(context)

    timeline = _add_operation(operations, "timeline", "show bounded chronological history")
    timeline.add_argument("--path", help="optional repository-relative path")
    timeline.add_argument("--from", dest="start", help="exclusive starting revision")
    timeline.add_argument("--to", dest="end", default="HEAD", help="inclusive ending revision")
    timeline.add_argument("--limit", type=_positive_integer, default=100, metavar="COUNT")
    _add_format(timeline)

    why = _add_operation(operations, "why", "explain one line from recorded provenance")
    why.add_argument("--path", required=True, help="repository-relative file path")
    why.add_argument("--line", required=True, type=_positive_integer, metavar="NUMBER")
    why.add_argument("--revision", default="HEAD", help="revision containing the line")
    _add_format(why)

    origin = _add_operation(operations, "origin", "find additions and removals of exact text")
    origin.add_argument("--text", required=True, help="exact text to find")
    origin.add_argument("--path", help="optional repository-relative path")
    origin.add_argument("--limit", type=_positive_integer, default=100, metavar="COUNT")
    _add_format(origin)

    evolution = _add_operation(operations, "evolution", "follow a path through local history")
    evolution.add_argument("--path", required=True, help="repository-relative path")
    evolution.add_argument("--limit", type=_positive_integer, default=100, metavar="COUNT")
    _add_format(evolution)

    compare = _add_operation(operations, "compare", "compare two revisions through Git evidence")
    compare.add_argument("left", metavar="LEFT", help="left revision")
    compare.add_argument("right", metavar="RIGHT", help="right revision")
    compare.add_argument("--path", help="optional repository-relative path")
    compare.add_argument("--limit", type=_positive_integer, default=100, metavar="COUNT")
    _add_format(compare)

    decision = _add_operation(operations, "decision", "find one exact decision and corrections")
    decision.add_argument("decision_id", metavar="DECISION_ID", help="exact Decision-ID value")
    decision.add_argument("--limit", type=_positive_integer, default=500, metavar="COUNT")
    _add_format(decision)

    recover = _add_operation(operations, "recover", "find local recovery candidates")
    recover.add_argument("--query", help="full object ID, ref, or bounded subject text")
    recover.add_argument("--create-branch", metavar="BRANCH", help="new local recovery branch")
    recover.add_argument("--apply", action="store_true", help="create the requested branch")
    _add_format(recover)

    checkpoint = _add_operation(
        operations,
        "checkpoint",
        "preview or record one explicit semantic checkpoint",
    )
    _add_selection(checkpoint)
    _add_metadata(checkpoint)
    checkpoint.add_argument("--apply", action="store_true", help="create the checkpoint commit")
    _add_format(checkpoint)

    annotate = _add_operation(
        operations,
        "annotate",
        "preview or append one review annotation",
    )
    annotate.add_argument("revision", metavar="REVISION", help="commit to annotate")
    annotate.add_argument("--message", required=True, help="English review message")
    annotate.add_argument("--agent-id", required=True, help="declared operational identity")
    annotate.add_argument("--apply", action="store_true", help="append the review note")
    _add_format(annotate)

    integrate = _add_operation(
        operations,
        "integrate",
        "preview or apply one validated branch integration",
    )
    integrate.add_argument("source", metavar="SOURCE", help="local source branch")
    integrate.add_argument(
        "--target",
        help="local target branch (default: configured canonical branch)",
    )
    _add_metadata(integrate)
    integrate.add_argument("--apply", action="store_true", help="create the integration commit")
    _add_format(integrate)

    correct = _add_operation(
        operations,
        "correct",
        "preview or append one correction to canonical history",
    )
    correct.add_argument(
        "incorrect_revision",
        metavar="INCORRECT_REVISION",
        help="canonical commit whose recorded claim is corrected",
    )
    _add_selection(correct)
    _add_metadata(correct)
    correct.add_argument("--apply", action="store_true", help="create the correction commit")
    _add_format(correct)

    return parser


def _add_operation(
    operations: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> _ArgumentParser:
    return operations.add_parser(
        name,
        help=help_text,
        description=help_text.capitalize() + ".",
        allow_abbrev=False,
    )


def _add_format(parser: argparse.ArgumentParser, *, allow_agent: bool = True) -> None:
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("human", "json", "agent") if allow_agent else ("human", "json"),
        default="human",
        help="output format (default: human)",
    )


def _add_selection(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--path",
        dest="paths",
        action="append",
        metavar="PATH",
        help="repository-relative path; repeat for multiple paths",
    )
    selection.add_argument(
        "--staged",
        action="store_true",
        help="use the current index instead of worktree paths",
    )


def _add_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--message", required=True, help="English commit subject")
    parser.add_argument("--reason", help="English reason recorded in the commit body")
    parser.add_argument("--memory-type", required=True, help="Memory-Type trailer value")
    parser.add_argument("--scope", required=True, help="Scope trailer value")
    parser.add_argument("--task-id", help="optional Task-ID trailer value")
    parser.add_argument("--decision-id", help="optional Decision-ID trailer value")
    parser.add_argument("--agent-id", required=True, help="declared operational identity")
    parser.add_argument(
        "--trailer",
        dest="trailers",
        action="append",
        type=_metadata_trailer,
        default=[],
        metavar="NAME=VALUE",
        help="additional metadata trailer; repeat to preserve multiple values",
    )


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _metadata_trailer(value: str) -> tuple[str, str]:
    name, separator, trailer_value = value.partition("=")
    if not separator or not name or not trailer_value:
        raise argparse.ArgumentTypeError("must use NAME=VALUE with non-empty fields")
    return name, trailer_value


def _validate_cross_options(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    if arguments.operation != "demo" and arguments.repo is None:
        parser.error("--repo is required before repository operations")
    if arguments.operation == "demo" and arguments.repo is not None:
        parser.error("demo does not accept --repo")
    if arguments.operation == "recover" and arguments.apply and not arguments.create_branch:
        parser.error("recover --apply requires --create-branch")


def _run_operation(service: MemoryService, arguments: argparse.Namespace) -> MemoryResult:
    operation = arguments.operation
    if operation == "status":
        return service.status()
    if operation == "context":
        return service.context(
            task_id=arguments.task_id,
            branch=arguments.branch,
            limit=arguments.limit,
        )
    if operation == "timeline":
        return service.timeline(
            path=_path_or_none(arguments.path),
            start=arguments.start,
            end=arguments.end,
            limit=arguments.limit,
        )
    if operation == "why":
        return service.why(
            path=PurePosixPath(arguments.path),
            line=arguments.line,
            revision=arguments.revision,
        )
    if operation == "origin":
        return service.origin(
            arguments.text,
            path=_path_or_none(arguments.path),
            limit=arguments.limit,
        )
    if operation == "evolution":
        return service.evolution(PurePosixPath(arguments.path), limit=arguments.limit)
    if operation == "compare":
        return service.compare(
            arguments.left,
            arguments.right,
            path=_path_or_none(arguments.path),
            limit=arguments.limit,
        )
    if operation == "decision":
        return service.decision(arguments.decision_id, limit=arguments.limit)
    if operation == "recover":
        return service.recover(
            query=arguments.query,
            create_branch=arguments.create_branch,
            apply=arguments.apply,
        )
    if operation == "checkpoint":
        request = CheckpointRequest(
            subject=arguments.message,
            metadata=_metadata_from(arguments),
            paths=_paths(arguments.paths),
            staged=arguments.staged,
        )
        return service.checkpoint(request, apply=arguments.apply)
    if operation == "annotate":
        request = AnnotationRequest(
            revision=arguments.revision,
            message=arguments.message,
            agent_id=arguments.agent_id,
        )
        return service.annotate(request, apply=arguments.apply)
    if operation == "integrate":
        request = IntegrationRequest(
            source=arguments.source,
            target=(arguments.target if arguments.target is not None else service.canonical_branch),
            subject=arguments.message,
            metadata=_metadata_from(arguments),
        )
        return service.integrate(request, apply=arguments.apply)
    if operation == "correct":
        request = CorrectionRequest(
            incorrect_revision=arguments.incorrect_revision,
            subject=arguments.message,
            metadata=_metadata_from(arguments),
            paths=_paths(arguments.paths),
            staged=arguments.staged,
        )
        return service.correct(request, apply=arguments.apply)
    raise AssertionError(f"Unhandled operation: {operation}")


def _metadata_from(arguments: argparse.Namespace) -> CommitMetadata:
    return CommitMetadata(
        memory_type=arguments.memory_type,
        scope=arguments.scope,
        agent_id=arguments.agent_id,
        task_id=arguments.task_id,
        decision_id=arguments.decision_id,
        reason=arguments.reason,
        extra_trailers=tuple(arguments.trailers),
    )


def _path_or_none(value: str | None) -> PurePosixPath | None:
    return PurePosixPath(value) if value is not None else None


def _paths(values: list[str] | None) -> tuple[PurePosixPath, ...]:
    return tuple(PurePosixPath(value) for value in values or ())


def _write_result(result: MemoryResult, output_format: str) -> None:
    renderer = {"json": render_json, "agent": render_agent}.get(output_format, render_human)
    print(renderer(result))


def _write_demo_report(report: DemoReport, output_format: str) -> None:
    print(report.to_json().rstrip("\n") if output_format == "json" else report.to_human())


def _write_error(error: ManduaError, output_format: str) -> None:
    renderer = {"json": render_json, "agent": render_agent}.get(output_format, render_human)
    print(renderer(error), file=sys.stderr)


def _render_usage_error(error: _UsageError) -> None:
    print(error.parser.format_usage().rstrip(), file=sys.stderr)
    print(f"{error.parser.prog}: error: {error.message}", file=sys.stderr)


def _exit_code(code: ErrorCode) -> int:
    if code in _INCOMPLETE_EXIT_CODES:
        return 3
    if code in _GIT_EXIT_CODES:
        return 4
    if code in _CONFLICT_EXIT_CODES:
        return 5
    return 2
