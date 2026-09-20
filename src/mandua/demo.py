"""Build the retained, deterministic Mandu'a community-garden demonstration."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import tomllib
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRunner, _GitProcessEvent, _observe_git_processes
from mandua.isolated_objects import isolated_git_arguments
from mandua.memory_service import MemoryService
from mandua.metadata import build_commit_message
from mandua.models import (
    AnnotationRequest,
    CheckpointRequest,
    CommitMetadata,
    CorrectionRequest,
    IntegrationRequest,
    MemoryResult,
    QueryLimits,
)
from mandua.policy import Policy

_SCHEMA_VERSION = "1.0"
_MAX_MANIFEST_BYTES = 65_536
# Exact launcher evidence adds a bounded worker/descriptor prefix to write attempts.
_MAX_REPORT_BYTES = 2_097_152
_MAX_FIXTURE_ENTRIES = 64
_MAX_GIT_OUTPUT_BYTES = 1_048_576
_MAX_ARGUMENT_CHARS = 16_384
_MAX_OPERATIONS = 2_048
_MAX_TUTORIAL_STEPS = 20
_MAX_TUTORIAL_COMMANDS = 128
_MAX_TUTORIAL_STEP_COMMANDS = 32
_MAX_TUTORIAL_ARGUMENTS = 64
_MAX_TUTORIAL_ARGUMENT_BYTES = 32_768
_MAX_TUTORIAL_COMMAND_OUTPUT_BYTES = 1_048_576
_MAX_TUTORIAL_TOTAL_OUTPUT_BYTES = 8_388_608
_MAX_TUTORIAL_TIMEOUT_SECONDS = 60
_SERVICE_TIMEOUT_SECONDS = 30.0
_REVIEW_MESSAGE = "Review confirms the sensor-threshold irrigation decision."
_REVIEW_AGENT = "community-reviewer"
_AGENT_ID = "mandua-demo"
_FILE_TRANSPORT_COMMANDS = frozenset({"clone", "fetch", "push"})
_LOCAL_GIT_COMMANDS = frozenset(
    {
        "add",
        "branch",
        "bundle",
        "cat-file",
        "check-attr",
        "check-ref-format",
        "commit",
        "commit-tree",
        "config",
        "diff",
        "diff-tree",
        "for-each-ref",
        "fsck",
        "hash-object",
        "init",
        "log",
        "ls-files",
        "ls-tree",
        "merge",
        "merge-base",
        "merge-tree",
        "notes",
        "patch-id",
        "read-tree",
        "reflog",
        "rev-list",
        "rev-parse",
        "show",
        "status",
        "switch",
        "symbolic-ref",
        "update-index",
        "update-ref",
        "verify-commit",
        "verify-pack",
        "worktree",
        "write-tree",
    }
)
_REMOTE_BEARING_OPTIONS = frozenset(
    {
        "--config-env",
        "--exec",
        "--receive-pack",
        "--remote",
        "--server-option",
        "--upload-pack",
    }
)
_DEMO_CONFIG_KEYS = frozenset(
    {
        "commit.gpgSign",
        "core.autocrlf",
        "core.editor",
        "core.filemode",
        "core.hooksPath",
        "core.safecrlf",
        "credential.helper",
        "i18n.commitEncoding",
        "i18n.logOutputEncoding",
        "merge.verifySignatures",
        "sequence.editor",
        "tag.gpgSign",
        "user.email",
        "user.name",
    }
)
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_FULL_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_DECLARED_FILE_TRANSPORT: ContextVar[str | None] = ContextVar(
    "mandua_demo_file_transport", default=None
)
_EXPECTED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "identity",
        "repository",
        "story",
        "timestamps",
        "claims",
        "tutorial",
    }
)
_EXPECTED_IDENTITY = frozenset({"name", "email"})
_EXPECTED_REPOSITORY = frozenset({"canonical_branch", "notes_ref"})
_EXPECTED_STORY = frozenset(
    {
        "task_id",
        "task_branch",
        "decision_id",
        "sensor_hypothesis",
        "schedule_hypothesis",
        "deleted_task_branch",
        "recovery_branch",
    }
)
_EXPECTED_TIMESTAMPS = frozenset({"start", "step_seconds"})
_EXPECTED_CLAIMS = frozenset(
    {
        "decision",
        "comparison",
        "integration",
        "review",
        "correction",
        "deleted_origin",
        "recovery",
        "malicious_history",
    }
)
_EXPECTED_TUTORIAL = frozenset(
    {
        "schema_version",
        "title",
        "introduction",
        "canonical_claims",
        "claims",
        "limits",
        "steps",
    }
)
_EXPECTED_TUTORIAL_CLAIMS = frozenset(
    {"missing_reason", "inferred_correspondence", "history_limit"}
)
_EXPECTED_TUTORIAL_LIMITS = frozenset(
    {
        "max_commands",
        "max_step_commands",
        "max_argument_count",
        "max_argument_bytes",
        "max_command_output_bytes",
        "max_total_output_bytes",
        "timeout_seconds",
    }
)
_EXPECTED_TUTORIAL_STEP = frozenset(
    {"id", "title", "explanation", "expected_evidence", "claims", "commands"}
)
_EXPECTED_TUTORIAL_COMMAND = frozenset({"id", "cwd", "argv", "expected_exit"})
_TUTORIAL_EXECUTABLES = frozenset({"git", "mandua", "cp", "mkdir"})
_TUTORIAL_PLACEHOLDERS = frozenset(
    {
        "{{OUTPUT}}",
        "{{REPO}}",
        "{{WORKTREE}}",
        "{{REMOTE}}",
        "{{CLONE}}",
        "{{FIXTURE_CONFIG}}",
        "{{FIXTURE_BASELINE_RULES}}",
        "{{FIXTURE_OBSERVATIONS}}",
        "{{FIXTURE_SENSOR_RULES}}",
        "{{FIXTURE_SCHEDULE_RULES}}",
        "{{FIXTURE_MISTAKE_RULES}}",
        "{{FIXTURE_CORRECTION_RULES}}",
        "{{FIXTURE_DELETED_RULE}}",
        "{{FIXTURE_MALICIOUS_NOTE}}",
    }
)
_TUTORIAL_CWD_PLACEHOLDERS = frozenset(
    {"{{OUTPUT}}", "{{REPO}}", "{{WORKTREE}}", "{{REMOTE}}", "{{CLONE}}"}
)
_EXPECTED_FIXTURES = frozenset(
    {
        "baseline/.mandua.toml",
        "baseline/knowledge/irrigation-rules.json",
        "baseline/knowledge/observations.md",
        "hypothesis-sensor/knowledge/irrigation-rules.json",
        "hypothesis-schedule/knowledge/irrigation-rules.json",
        "mistake/knowledge/irrigation-rules.json",
        "correction/knowledge/irrigation-rules.json",
        "deleted-phrase/knowledge/temporary-rule.md",
        "malicious-history/knowledge/untrusted-note.md",
    }
)
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "output_path",
        "repository_path",
        "writing_worktree_path",
        "remote_path",
        "clone_path",
        "bundle_path",
        "report_path",
        "temporary_output",
        "output_retained",
        "commit_ids",
        "claims",
        "notes_status",
        "pushed_refs",
        "bundle_verified",
        "network_accessed",
        "operation_log_complete",
        "operation_log",
    }
)
_CLAIM_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_IDENTIFIER = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_SERVICE_ENVIRONMENT_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class _GitPolicyDecision:
    allowed: bool
    transport: str | None


def _is_local_path_shape(candidate: str) -> bool:
    return candidate.startswith(("/", "./", "../")) or bool(_WINDOWS_DRIVE_PATH.match(candidate))


def _is_scp_transport(candidate: str) -> bool:
    """Match Git's colon-before-slash SCP form while excluding explicit local paths."""
    if not candidate or "://" in candidate or _is_local_path_shape(candidate):
        return False
    user_separator = candidate.find("@")
    host_start = user_separator + 1 if user_separator >= 0 else 0
    if user_separator == 0 or candidate.count("@") > 1:
        return False
    if candidate[host_start : host_start + 1] == "[":
        host_end = candidate.find("]", host_start + 1)
        if host_end < 0 or candidate[host_end + 1 : host_end + 2] != ":":
            return False
        separator = host_end + 1
        host = candidate[host_start : host_end + 1]
        if len(host) < 3 or any(character.isspace() for character in host):
            return False
    else:
        separator = candidate.find(":", host_start)
        if separator < 0:
            return False
        host = candidate[host_start:separator]
        if not host or re.fullmatch(r"[A-Za-z0-9._-]+", host) is None:
            return False
    first_slash = min(
        (position for position in (candidate.find("/"), candidate.find("\\")) if position >= 0),
        default=-1,
    )
    return separator > 0 and (first_slash < 0 or separator < first_slash)


def _is_safe_local_object_path(arguments: tuple[str, ...], index: int, candidate: str) -> bool:
    if arguments[0] != "show" or len(arguments) != 2 or index != 1:
        return False
    object_id, separator, path = candidate.partition(":")
    if not separator or _FULL_OBJECT_ID.fullmatch(object_id) is None:
        return False
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _rejected_transport(arguments: tuple[str, ...]) -> str:
    for index, argument in enumerate(arguments):
        candidate = argument
        if "=" in argument and argument.split("=", 1)[0] in _REMOTE_BEARING_OPTIONS:
            candidate = argument.split("=", 1)[1]
        elif argument in _REMOTE_BEARING_OPTIONS and index + 1 < len(arguments):
            candidate = arguments[index + 1]
        if _is_scp_transport(candidate) and not _is_safe_local_object_path(
            arguments, index, candidate
        ):
            return "ssh"
        if _is_local_path_shape(candidate):
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme:
            return parsed.scheme.casefold()
    return "blocked"


def _same_refspec(value: str, prefix: str) -> bool:
    if value.count(":") != 1:
        return False
    source, destination = value.split(":", 1)
    return source == destination and source.startswith(prefix) and "*" not in source


def _valid_file_transport_shape(arguments: tuple[str, ...], url: str) -> bool:
    if arguments.count(url) != 1:
        return False
    command = arguments[0] if arguments else ""
    if command == "push":
        return (
            len(arguments) == 4
            and arguments[1] == "--porcelain"
            and arguments[2] == url
            and (
                _same_refspec(arguments[3], "refs/heads/")
                or _same_refspec(arguments[3], "refs/notes/")
            )
        )
    if command == "clone":
        return (
            len(arguments) == 6
            and arguments[1:4] == ("--no-local", "--origin", "origin")
            and arguments[4] == url
        )
    if command == "fetch":
        return (
            len(arguments) == 4
            and arguments[1] == "--no-tags"
            and arguments[2] == url
            and _same_refspec(arguments[3], "refs/notes/")
        )
    return False


def _git_policy(arguments: tuple[str, ...], declared_file_url: str | None) -> _GitPolicyDecision:
    if not arguments:
        return _GitPolicyDecision(False, "blocked")
    command = arguments[0]
    if command in _FILE_TRANSPORT_COMMANDS:
        if declared_file_url is not None and _valid_file_transport_shape(
            arguments, declared_file_url
        ):
            return _GitPolicyDecision(True, "file")
        return _GitPolicyDecision(False, _rejected_transport(arguments))
    if declared_file_url is not None:
        return _GitPolicyDecision(False, "blocked")
    if command == "--version":
        return _GitPolicyDecision(len(arguments) == 1, None if len(arguments) == 1 else "blocked")
    if command not in _LOCAL_GIT_COMMANDS:
        return _GitPolicyDecision(False, _rejected_transport(arguments))
    for index, argument in enumerate(arguments[1:], start=1):
        if (
            argument in _REMOTE_BEARING_OPTIONS
            or any(argument.startswith(f"{option}=") for option in _REMOTE_BEARING_OPTIONS)
            or "://" in argument
            or (
                _is_scp_transport(argument)
                and not _is_safe_local_object_path(arguments, index, argument)
            )
        ):
            return _GitPolicyDecision(False, _rejected_transport(arguments))
    if command == "config" and not (
        arguments == ("config", "--null", "--list", "--includes")
        or (len(arguments) == 4 and arguments[1] == "--local" and arguments[2] in _DEMO_CONFIG_KEYS)
    ):
        return _GitPolicyDecision(False, "blocked")
    if command == "verify-commit" and not (
        len(arguments) == 4
        and arguments[1:3] == ("--raw", "--end-of-options")
        and _FULL_OBJECT_ID.fullmatch(arguments[3]) is not None
    ):
        return _GitPolicyDecision(False, "blocked")
    return _GitPolicyDecision(True, None)


def _audit_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """Keep transport evidence while excluding repository search text from the report."""
    audited = list(command)
    redact_next = False
    arguments = isolated_git_arguments(command)
    start = len(command) - len(arguments) if arguments is not None else 0
    for index, argument in enumerate(audited):
        if index < start:
            continue
        if redact_next:
            audited[index] = "<redacted-search-data>"
            redact_next = False
        elif argument in {"-G", "-S"}:
            redact_next = True
        elif argument.startswith(("-G", "-S")) and len(argument) > 2:
            audited[index] = f"{argument[:2]}<redacted-search-data>"
    return tuple(audited)


@contextmanager
def _declared_file_transport(url: str) -> Iterator[None]:
    token = _DECLARED_FILE_TRANSPORT.set(url)
    try:
        yield
    finally:
        _DECLARED_FILE_TRANSPORT.reset(token)


@dataclass(frozen=True, slots=True)
class DemoOperation:
    """One bounded record for a Git process considered by transport policy."""

    sequence: int
    argv: tuple[str, ...]
    cwd: str
    transport: str | None
    status: str
    returncode: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "transport": self.transport,
            "status": self.status,
            "returncode": self.returncode,
        }

    @classmethod
    def from_dict(cls, payload: object) -> DemoOperation:
        if not isinstance(payload, dict) or set(payload) != {
            "sequence",
            "argv",
            "cwd",
            "transport",
            "status",
            "returncode",
        }:
            raise _demo_error("The demo report contains an invalid operation record.")
        sequence = payload["sequence"]
        argv = payload["argv"]
        cwd = payload["cwd"]
        transport = payload["transport"]
        status_value = payload["status"]
        returncode = payload["returncode"]
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or "\x00" in item for item in argv)
            or isolated_git_arguments(argv) is None
            or not isinstance(cwd, str)
            or not cwd
            or (transport is not None and (not isinstance(transport, str) or not transport))
            or status_value not in {"completed", "failed", "rejected"}
            or (
                returncode is not None
                and (not isinstance(returncode, int) or isinstance(returncode, bool))
            )
            or (status_value == "completed" and returncode != 0)
            or (status_value == "failed" and returncode == 0)
            or (
                status_value == "rejected"
                and (returncode is not None or transport in {None, "file"})
            )
        ):
            raise _demo_error("The demo report contains an invalid operation record.")
        return cls(
            sequence=sequence,
            argv=tuple(argv),
            cwd=cwd,
            transport=transport,
            status=status_value,
            returncode=returncode,
        )


class _OperationLog:
    """Retain and seal a finite lifecycle record for every demo Git process."""

    def __init__(self, *, max_entries: int = _MAX_OPERATIONS) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise _demo_error("The demo operation-log bound is invalid.")
        self._max_entries = max_entries
        self._entries: list[DemoOperation] = []
        self._pending: dict[object, int] = {}
        self._output_root: Path | None = None
        self._sealed = False

    def bind_output_root(self, output_root: Path) -> None:
        try:
            resolved = Path(output_root).resolve(strict=True)
        except OSError:
            raise _demo_error("The demo output directory is unavailable.") from None
        if not resolved.is_dir():
            raise _demo_error("The demo output directory is unavailable.")
        if self._output_root is not None and self._output_root != resolved:
            raise _demo_error("The demo operation log is already bound to another output.")
        self._output_root = resolved

    @property
    def entries(self) -> tuple[DemoOperation, ...]:
        return tuple(self._entries)

    @property
    def network_accessed(self) -> bool:
        """Derive access conservatively from every recorded transport attempt."""
        return any(
            entry.transport is not None and entry.transport != "file" for entry in self._entries
        )

    def reject(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        transport: str | None,
    ) -> None:
        self._reserve_capacity()
        self._entries.append(
            DemoOperation(
                sequence=len(self._entries) + 1,
                argv=_audit_command(argv),
                cwd=os.fspath(cwd),
                transport=transport or "blocked",
                status="rejected",
                returncode=None,
            )
        )

    def observe(self, event: _GitProcessEvent) -> None:
        if not isinstance(event, _GitProcessEvent):
            raise _demo_error("The demo received invalid Git process evidence.")
        if event.phase == "considered":
            self._reserve_capacity()
            cwd = self._validated_cwd(event.cwd)
            decision = _git_policy(tuple(event.arguments), _DECLARED_FILE_TRANSPORT.get())
            audit_command = _audit_command(event.command)
            if not decision.allowed:
                self._entries.append(
                    DemoOperation(
                        sequence=len(self._entries) + 1,
                        argv=audit_command,
                        cwd=os.fspath(cwd),
                        transport=decision.transport or "blocked",
                        status="rejected",
                        returncode=None,
                    )
                )
                raise _demo_error("The demo rejected a transport-capable Git invocation.")
            if event.attempt in self._pending:
                raise _demo_error("The demo received a duplicate Git process attempt.")
            self._entries.append(
                DemoOperation(
                    sequence=len(self._entries) + 1,
                    argv=audit_command,
                    cwd=os.fspath(cwd),
                    transport=decision.transport,
                    status="pending",
                    returncode=None,
                )
            )
            self._pending[event.attempt] = len(self._entries) - 1
            return
        if event.phase not in {"completed", "failed"}:
            raise _demo_error("The demo received an invalid Git process phase.")
        index = self._pending.get(event.attempt)
        if index is None:
            raise _demo_error("The demo Git process was not reserved before launch.")
        pending = self._entries[index]
        cwd = self._validated_cwd(event.cwd)
        if pending.argv != _audit_command(event.command) or pending.cwd != os.fspath(cwd):
            raise _demo_error("The demo Git process changed after audit reservation.")
        if event.phase == "completed" and event.returncode != 0:
            raise _demo_error("The demo received an invalid successful Git process result.")
        self._entries[index] = DemoOperation(
            sequence=pending.sequence,
            argv=pending.argv,
            cwd=pending.cwd,
            transport=pending.transport,
            status=event.phase,
            returncode=event.returncode,
        )
        del self._pending[event.attempt]

    def seal(self) -> tuple[DemoOperation, ...]:
        if (
            self._sealed
            or self._pending
            or any(entry.status == "pending" for entry in self._entries)
        ):
            raise _demo_error("The demo Git operation log is incomplete.")
        self._sealed = True
        return tuple(self._entries)

    def _reserve_capacity(self) -> None:
        if self._sealed:
            raise _demo_error("The demo Git operation log is already sealed.")
        if len(self._entries) >= self._max_entries:
            raise _demo_error("The demo exceeded its bounded Git operation log.")

    def _validated_cwd(self, cwd: Path) -> Path:
        if self._output_root is None:
            raise _demo_error("The demo operation log has no output authority.")
        try:
            resolved = Path(cwd).resolve(strict=True)
        except OSError:
            raise _demo_error("A demo Git working directory is unavailable.") from None
        if not resolved.is_dir() or not resolved.is_relative_to(self._output_root):
            raise _demo_error("Demo Git operations must remain inside the output directory.")
        return resolved


@dataclass(frozen=True, slots=True)
class _GitResult:
    stdout: bytes

    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


class _GitExecutor:
    """Run only bounded local Git commands and fail closed around transports."""

    def __init__(self, output_root: Path, operation_log: _OperationLog) -> None:
        try:
            self._output_root = Path(output_root).resolve(strict=True)
        except OSError:
            raise _demo_error("The demo output directory is unavailable.") from None
        self._log = operation_log
        self._log.bind_output_root(self._output_root)

    def run(
        self,
        cwd: Path,
        *arguments: str,
        input_bytes: bytes | None = None,
        timestamp: datetime | None = None,
    ) -> _GitResult:
        resolved_cwd = self._validated_cwd(cwd)
        command = self._command(arguments)
        decision = _git_policy(arguments, None)
        if not decision.allowed:
            self._log.reject(
                argv=command,
                cwd=resolved_cwd,
                transport=decision.transport,
            )
            raise _demo_error("The demo rejected a transport-capable Git invocation.")
        with _observe_git_processes(self._log.observe):
            return self._run(
                resolved_cwd,
                arguments,
                input_bytes=input_bytes,
                timestamp=timestamp,
            )

    def run_transport(
        self,
        cwd: Path,
        url: str,
        *arguments: str,
        input_bytes: bytes | None = None,
        timestamp: datetime | None = None,
    ) -> _GitResult:
        resolved_cwd = self._validated_cwd(cwd)
        command = self._command(arguments)
        scheme = urlsplit(url).scheme.casefold() or "unknown"
        try:
            self._validate_file_transport(url, arguments)
        except ManduaError:
            self._log.reject(
                argv=command,
                cwd=resolved_cwd,
                transport=scheme,
            )
            raise
        with (
            _declared_file_transport(url),
            _observe_git_processes(self._log.observe),
        ):
            return self._run(
                resolved_cwd,
                arguments,
                input_bytes=input_bytes,
                timestamp=timestamp,
            )

    def _run(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None,
        timestamp: datetime | None,
    ) -> _GitResult:
        resolved_cwd = self._validated_cwd(cwd)
        runner = GitRunner(
            resolved_cwd,
            limits=QueryLimits(
                max_output_bytes=_MAX_GIT_OUTPUT_BYTES,
                max_input_chars=_MAX_ARGUMENT_CHARS,
                timeout_seconds=60.0,
            ),
        )
        argument_list = list(arguments)
        payload = input_bytes if input_bytes is not None else b""
        runner._validate_arguments(argument_list)
        runner._validate_input(payload)
        safe_arguments = tuple(runner._safe_arguments(argument_list))
        command = self._command(safe_arguments)
        environment = self._environment(runner, timestamp)
        try:
            completed = runner._run_prepared_process(
                arguments=argument_list,
                command=command,
                cwd=resolved_cwd,
                environment=environment,
                check=True,
                input_bytes=payload,
                timeout_seconds=60.0,
                max_output_bytes=_MAX_GIT_OUTPUT_BYTES,
            )
        except OSError:
            raise _demo_error("A bounded local Git operation could not complete.") from None
        return _GitResult(stdout=completed.stdout)

    def _validated_cwd(self, cwd: Path) -> Path:
        try:
            resolved = Path(cwd).resolve(strict=True)
        except OSError:
            raise _demo_error("A demo Git working directory is unavailable.") from None
        if not resolved.is_dir() or not resolved.is_relative_to(self._output_root):
            raise _demo_error("Demo Git operations must remain inside the output directory.")
        return resolved

    def _validate_file_transport(self, url: object, arguments: tuple[str, ...]) -> None:
        if not isinstance(url, str) or not url or url not in arguments:
            raise _demo_error("The demo Git transport declaration is invalid.")
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise _demo_error("The demo permits only hostless file transport.")
        try:
            endpoint = Path(unquote(parsed.path)).resolve(strict=True)
        except OSError:
            raise _demo_error("The demo file-transport endpoint is unavailable.") from None
        if not endpoint.is_relative_to(self._output_root):
            raise _demo_error("The demo file transport must remain inside the output directory.")
        if not arguments or arguments[0] not in _FILE_TRANSPORT_COMMANDS:
            raise _demo_error("The declared demo transport command is invalid.")
        if not _valid_file_transport_shape(arguments, url):
            raise _demo_error("The declared demo file-transport shape is invalid.")
        if arguments[0] == "clone":
            destination = Path(arguments[-1]).resolve(strict=False)
            if not destination.is_relative_to(self._output_root):
                raise _demo_error("The demo clone destination must remain inside the output.")

    @staticmethod
    def _command(arguments: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not arguments
            or any(not isinstance(item, str) or "\x00" in item for item in arguments)
            or sum(len(item) for item in arguments) > _MAX_ARGUMENT_CHARS
        ):
            raise _demo_error("The demo Git argument list is invalid or too large.")
        return (
            "git",
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.autocrlf=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "credential.helper=",
            "-c",
            "merge.verifySignatures=false",
            *arguments,
        )

    def _environment(self, runner: GitRunner, timestamp: datetime | None) -> dict[str, str]:
        temporary = self._output_root / ".tmp"
        temporary.mkdir(exist_ok=True)
        environment = runner._environment(isolated_configuration=True)
        environment.update(
            {
                "GIT_ALLOW_PROTOCOL": "file",
                "TMPDIR": os.fspath(temporary),
            }
        )
        if timestamp is not None:
            rendered = _render_timestamp(timestamp)
            environment["GIT_AUTHOR_DATE"] = rendered
            environment["GIT_COMMITTER_DATE"] = rendered
        return environment


@dataclass(frozen=True, slots=True)
class TutorialLimits:
    """Finite execution limits declared by the tutorial manifest."""

    max_commands: int
    max_step_commands: int
    max_argument_count: int
    max_argument_bytes: int
    max_command_output_bytes: int
    max_total_output_bytes: int
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class TutorialCommand:
    """One visible command represented only as an argument array."""

    id: str
    cwd: str
    argv: tuple[str, ...]
    expected_exit: int


@dataclass(frozen=True, slots=True)
class TutorialStep:
    """One ordered explanatory step and its executable commands."""

    id: str
    title: str
    explanation: str
    expected_evidence: tuple[str, ...]
    claim_keys: tuple[str, ...]
    commands: tuple[TutorialCommand, ...]


@dataclass(frozen=True, slots=True)
class TutorialManifest:
    """Strict tutorial projection loaded from the canonical demo manifest."""

    schema_version: str
    title: str
    introduction: tuple[str, ...]
    steps: tuple[TutorialStep, ...]
    claim_ids: dict[str, str]
    limits: TutorialLimits
    identity_name: str
    identity_email: str
    canonical_branch: str
    notes_ref: str
    task_id: str
    decision_id: str
    task_branch: str
    sensor_hypothesis: str
    schedule_hypothesis: str
    deleted_task_branch: str
    recovery_branch: str
    start: datetime
    step_seconds: int

    @property
    def command_count(self) -> int:
        return sum(len(step.commands) for step in self.steps)


@dataclass(frozen=True, slots=True)
class _ScenarioManifest:
    identity_name: str
    identity_email: str
    canonical_branch: str
    notes_ref: str
    task_id: str
    task_branch: str
    decision_id: str
    sensor_hypothesis: str
    schedule_hypothesis: str
    deleted_task_branch: str
    recovery_branch: str
    start: datetime
    step_seconds: int
    claim_ids: dict[str, str]
    tutorial: TutorialManifest

    def timestamp(self, position: int) -> datetime:
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise _demo_error("The demo timestamp position is invalid.")
        return self.start + timedelta(seconds=self.step_seconds * position)


@dataclass(frozen=True, slots=True)
class _ScenarioResources:
    """One immutable, descriptor-captured manifest and exact fixture inventory."""

    manifest: _ScenarioManifest
    fixtures: tuple[tuple[str, bytes], ...]

    def fixture(self, relative: str) -> bytes:
        for name, contents in self.fixtures:
            if name == relative:
                return contents
        raise _demo_error("The requested versioned demo fixture is unavailable.")


@dataclass(frozen=True, slots=True)
class _BoundSourceCheckout:
    """One continuously held source checkout and its selected demo directory."""

    checkout: Path
    checkout_descriptor: int
    checkout_state: tuple[int, int, int, int, int, int, int]
    demo_descriptor: int
    demo_metadata: os.stat_result


@dataclass(frozen=True, slots=True)
class DemoReport:
    """Versioned public result for one retained demo build."""

    output_path: Path
    repository_path: Path
    writing_worktree_path: Path
    remote_path: Path
    clone_path: Path
    bundle_path: Path
    report_path: Path
    temporary_output: bool
    commit_ids: dict[str, str]
    claims: dict[str, dict[str, object]]
    notes_status: dict[str, object]
    pushed_refs: tuple[str, ...]
    bundle_verified: bool
    network_accessed: bool
    operation_log: tuple[DemoOperation, ...]
    schema_version: str = _SCHEMA_VERSION
    output_retained: bool = True
    operation_log_complete: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return the stable, path-explicit machine representation."""
        return {
            "schema_version": self.schema_version,
            "output_path": os.fspath(self.output_path),
            "repository_path": os.fspath(self.repository_path),
            "writing_worktree_path": os.fspath(self.writing_worktree_path),
            "remote_path": os.fspath(self.remote_path),
            "clone_path": os.fspath(self.clone_path),
            "bundle_path": os.fspath(self.bundle_path),
            "report_path": os.fspath(self.report_path),
            "temporary_output": self.temporary_output,
            "output_retained": self.output_retained,
            "commit_ids": self.commit_ids,
            "claims": self.claims,
            "notes_status": self.notes_status,
            "pushed_refs": list(self.pushed_refs),
            "bundle_verified": self.bundle_verified,
            "network_accessed": self.network_accessed,
            "operation_log_complete": self.operation_log_complete,
            "operation_log": [entry.to_dict() for entry in self.operation_log],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def to_human(self) -> str:
        """Render human output from exactly the report object used for JSON."""
        return "\n".join(
            (
                f"Demo output retained: {self.output_path}",
                f"Demo repository: {self.repository_path}",
                f"Demo report: {self.report_path}",
                f"Demo bundle: {self.bundle_path}",
                f"Network accessed: {'yes' if self.network_accessed else 'no'}",
            )
        )

    @classmethod
    def from_path(cls, path: Path) -> DemoReport:
        """Read and validate one generated report without executing repository text."""
        source = Path(path)
        try:
            metadata = os.lstat(source)
        except OSError:
            raise _demo_error("The demo report is unavailable.") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_REPORT_BYTES:
            raise _demo_error("The demo report must be one bounded regular file.")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise _demo_error("The demo report is invalid JSON.") from None
        if not isinstance(payload, dict) or set(payload) != _REPORT_KEYS:
            raise _demo_error("The demo report has an invalid schema.")
        if payload["schema_version"] != _SCHEMA_VERSION:
            raise _demo_error("The demo report schema version is unsupported.")
        paths = {
            name: _report_path(payload[name], name)
            for name in (
                "output_path",
                "repository_path",
                "writing_worktree_path",
                "remote_path",
                "clone_path",
                "bundle_path",
                "report_path",
            )
        }
        commit_ids = _string_mapping(payload["commit_ids"], "commit IDs")
        claims = _claims_mapping(payload["claims"])
        notes_status = _object_mapping(payload["notes_status"], "notes status")
        pushed_refs = payload["pushed_refs"]
        operations = payload["operation_log"]
        booleans = (
            payload["temporary_output"],
            payload["output_retained"],
            payload["bundle_verified"],
            payload["network_accessed"],
            payload["operation_log_complete"],
        )
        if (
            any(not isinstance(item, bool) for item in booleans)
            or not isinstance(pushed_refs, list)
            or any(not isinstance(item, str) or not item for item in pushed_refs)
            or not isinstance(operations, list)
            or not operations
            or len(operations) > _MAX_OPERATIONS
        ):
            raise _demo_error("The demo report contains invalid values.")
        if payload["operation_log_complete"] is not True:
            raise _demo_error("The demo report Git operation log is incomplete.")
        operation_log = tuple(DemoOperation.from_dict(item) for item in operations)
        if tuple(item.sequence for item in operation_log) != tuple(
            range(1, len(operation_log) + 1)
        ):
            raise _demo_error("The demo report operation sequence is invalid.")
        normalized_output = Path(os.path.normpath(paths["output_path"]))
        if any(
            not Path(item.cwd).is_absolute()
            or not Path(os.path.normpath(item.cwd)).is_relative_to(normalized_output)
            for item in operation_log
        ):
            raise _demo_error("The demo report operation path is outside its output.")
        derived_network = any(
            item.transport is not None and item.transport != "file" for item in operation_log
        )
        if payload["network_accessed"] is not derived_network:
            raise _demo_error("The demo report network status does not match its operation log.")
        return cls(
            **paths,
            temporary_output=payload["temporary_output"],
            output_retained=payload["output_retained"],
            commit_ids=commit_ids,
            claims=claims,
            notes_status=notes_status,
            pushed_refs=tuple(pushed_refs),
            bundle_verified=payload["bundle_verified"],
            network_accessed=payload["network_accessed"],
            operation_log=operation_log,
        )


class DemoScenario:
    """Build the one canonical community-garden history from versioned fixtures."""

    def __init__(self, scenario_path: Path | None = None) -> None:
        self._resources = (
            _capture_scenario_resources(Path(scenario_path))
            if scenario_path is not None
            else _default_scenario_resources()
        )

    def build(self, output: Path | None = None) -> DemoReport:
        """Build, verify, retain, and report one deterministic local-only demo."""
        manifest = self._resources.manifest
        output_root, temporary_output = _prepare_output(output)
        try:
            operation_log = _OperationLog()
            git = _GitExecutor(output_root, operation_log)
            with _observe_git_processes(operation_log.observe):
                return self._build_prepared(
                    manifest=manifest,
                    resources=self._resources,
                    output_root=output_root,
                    temporary_output=temporary_output,
                    operation_log=operation_log,
                    git=git,
                )
        except ManduaError as error:
            if not temporary_output:
                raise
            recovery = f"The partial demo output is retained at {output_root}."
            if error.recovery is not None:
                recovery = f"{error.recovery} {recovery}"
            raise ManduaError(
                error.code,
                error.message,
                evidence=error.evidence,
                recovery=recovery,
            ) from error
        except BaseException as error:
            if temporary_output:
                error.add_note(f"The partial demo output is retained at {output_root}.")
            raise

    def _build_prepared(
        self,
        *,
        manifest: _ScenarioManifest,
        resources: _ScenarioResources,
        output_root: Path,
        temporary_output: bool,
        operation_log: _OperationLog,
        git: _GitExecutor,
    ) -> DemoReport:
        repository = output_root / "repository"
        writing_worktree = output_root / "worktrees" / "writing-task"
        remote = output_root / "remote.git"
        clone = output_root / "clone"
        bundle = output_root / "mandua-demo.bundle"
        report_path = output_root / "report.json"

        repository.mkdir()
        git.run(repository, "init", f"--initial-branch={manifest.canonical_branch}")
        self._configure_repository(git, repository, manifest)
        _install_fixture_tree(resources, "baseline", repository)
        _require_manifest_policy(repository, manifest)
        baseline = self._commit(
            git,
            repository,
            manifest,
            position=0,
            subject="Record the community garden baseline",
            reason="The gardeners need a shared irrigation starting point.",
            memory_type="observation",
        )

        writing_worktree.parent.mkdir()
        git.run(
            repository,
            "worktree",
            "add",
            "-b",
            manifest.task_branch,
            os.fspath(writing_worktree),
            baseline,
        )
        git.run(repository, "branch", manifest.sensor_hypothesis, baseline)
        git.run(repository, "branch", manifest.schedule_hypothesis, baseline)

        git.run(writing_worktree, "switch", manifest.sensor_hypothesis)
        _install_fixture_tree(resources, "hypothesis-sensor", writing_worktree)
        sensor = self._commit(
            git,
            writing_worktree,
            manifest,
            position=1,
            subject="Record the sensor-threshold hypothesis",
            reason="Soil-moisture readings can adapt irrigation to each bed.",
            memory_type="hypothesis",
            decision_id=manifest.decision_id,
        )

        git.run(writing_worktree, "switch", manifest.schedule_hypothesis)
        _install_fixture_tree(resources, "hypothesis-schedule", writing_worktree)
        schedule = self._commit(
            git,
            writing_worktree,
            manifest,
            position=2,
            subject="Record the fixed-schedule hypothesis",
            reason="A fixed morning schedule is operationally simple.",
            memory_type="hypothesis",
        )
        git.run(writing_worktree, "switch", manifest.task_branch)

        comparison = _service(repository).compare(
            manifest.sensor_hypothesis,
            manifest.schedule_hypothesis,
            path=PurePosixPath("knowledge/irrigation-rules.json"),
        )
        integration = self._integrate(repository, manifest, position=3)
        integration_oid = _result_commit_oid(integration, "integration")
        annotation = self._annotate(repository, manifest, integration_oid, position=4)

        _install_fixture_tree(resources, "mistake", repository)
        incorrect = self._checkpoint(
            repository,
            manifest,
            position=5,
            subject="Record the incorrectly transcribed threshold",
            reason="A transcription mistakenly recorded an eighty-percent threshold.",
            memory_type="decision",
            path=PurePosixPath("knowledge/irrigation-rules.json"),
            decision_id=manifest.decision_id,
        )
        incorrect_oid = _result_commit_oid(incorrect, "checkpoint")

        _install_fixture_tree(resources, "correction", repository)
        correction = self._correct(
            repository,
            manifest,
            incorrect_oid,
            position=6,
        )
        correction_oid = _result_commit_oid(correction, "correction")

        _install_fixture_tree(resources, "deleted-phrase", repository)
        phrase_added = self._checkpoint(
            repository,
            manifest,
            position=7,
            subject="Record a temporary noon watering rule",
            reason="The temporary rule preserves a short-lived operational proposal.",
            memory_type="observation",
            path=PurePosixPath("knowledge/temporary-rule.md"),
        )
        phrase_added_oid = _result_commit_oid(phrase_added, "checkpoint")
        temporary_rule = repository / "knowledge" / "temporary-rule.md"
        temporary_rule.unlink()
        phrase_removed = self._checkpoint(
            repository,
            manifest,
            position=8,
            subject="Remove the temporary noon watering rule",
            reason="The chosen sensor threshold supersedes the temporary proposal.",
            memory_type="observation",
            path=PurePosixPath("knowledge/temporary-rule.md"),
        )
        phrase_removed_oid = _result_commit_oid(phrase_removed, "checkpoint")

        git.run(
            repository,
            "switch",
            "-c",
            manifest.deleted_task_branch,
            phrase_removed_oid,
        )
        _install_fixture_tree(resources, "malicious-history", repository)
        recoverable = self._commit(
            git,
            repository,
            manifest,
            position=9,
            subject="Record an untrusted historical note",
            reason="The quoted text is retained as data for safe provenance review.",
            memory_type="observation",
        )
        git.run(repository, "switch", manifest.canonical_branch)
        git.run(repository, "branch", "-D", manifest.deleted_task_branch)

        recovery_preview = _service(repository).recover(
            query=recoverable,
            create_branch=manifest.recovery_branch,
        )
        _require_recovery_candidate(recovery_preview, recoverable)
        recovery = _service(repository).recover(
            query=recoverable,
            create_branch=manifest.recovery_branch,
            apply=True,
        )

        decision = _service(repository).decision(manifest.decision_id)
        deleted_phrase = _fixture_text(
            resources, "deleted-phrase/knowledge/temporary-rule.md"
        ).rstrip("\n")
        deleted_origin = _service(repository).origin(
            deleted_phrase,
            path=PurePosixPath("knowledge/temporary-rule.md"),
        )
        malicious_quote = _quoted_fixture_text(
            resources, "malicious-history/knowledge/untrusted-note.md"
        )
        incorrect_threshold = _fixture_threshold(
            resources, "mistake/knowledge/irrigation-rules.json"
        )
        corrected_threshold = _fixture_threshold(
            resources, "correction/knowledge/irrigation-rules.json"
        )
        malicious_origin = _service(repository).origin(
            malicious_quote,
            path=PurePosixPath("knowledge/untrusted-note.md"),
        )
        claims = _build_claims(
            manifest,
            decision=decision,
            comparison=comparison,
            integration=integration,
            annotation=annotation,
            correction=correction,
            deleted_origin=deleted_origin,
            recovery=recovery,
            malicious_origin=malicious_origin,
            deleted_phrase=deleted_phrase,
            malicious_quote=malicious_quote,
            incorrect_threshold=incorrect_threshold,
            corrected_threshold=corrected_threshold,
            recoverable_oid=recoverable,
        )

        source_note = git.run(
            repository,
            "notes",
            f"--ref={manifest.notes_ref}",
            "show",
            integration_oid,
        ).text()
        _require_review_note(source_note)

        git.run(
            output_root,
            "init",
            "--bare",
            f"--initial-branch={manifest.canonical_branch}",
            os.fspath(remote),
        )
        remote_uri = remote.as_uri()
        main_ref = f"refs/heads/{manifest.canonical_branch}"
        git.run_transport(
            repository,
            remote_uri,
            "push",
            "--porcelain",
            remote_uri,
            f"{main_ref}:{main_ref}",
        )
        git.run_transport(
            repository,
            remote_uri,
            "push",
            "--porcelain",
            remote_uri,
            f"{manifest.notes_ref}:{manifest.notes_ref}",
        )
        pushed_refs = tuple(
            line
            for line in git.run(
                remote,
                "for-each-ref",
                "--format=%(refname)",
            )
            .text()
            .splitlines()
            if line
        )
        expected_refs = (main_ref, manifest.notes_ref)
        if pushed_refs != expected_refs:
            raise _demo_error("The demo bare remote contains an unexpected ref.")

        git.run_transport(
            output_root,
            remote_uri,
            "clone",
            "--no-local",
            "--origin",
            "origin",
            remote_uri,
            os.fspath(clone),
        )
        git.run_transport(
            clone,
            remote_uri,
            "fetch",
            "--no-tags",
            remote_uri,
            f"{manifest.notes_ref}:{manifest.notes_ref}",
        )
        clone_note = git.run(
            clone,
            "notes",
            f"--ref={manifest.notes_ref}",
            "show",
            integration_oid,
        ).text()
        _require_review_note(clone_note)

        git.run(repository, "bundle", "create", os.fspath(bundle), "--all")
        git.run(repository, "bundle", "verify", os.fspath(bundle))

        commit_ids = {
            "baseline": baseline,
            "sensor-hypothesis": sensor,
            "schedule-hypothesis": schedule,
            "integration": integration_oid,
            "incorrect-threshold": incorrect_oid,
            "correction": correction_oid,
            "deleted-phrase-added": phrase_added_oid,
            "deleted-phrase-removed": phrase_removed_oid,
            "recoverable-task": recoverable,
        }
        notes_status: dict[str, object] = {
            "ref": manifest.notes_ref,
            "target_oid": integration_oid,
            "message": _REVIEW_MESSAGE,
            "visible_in_source": True,
            "visible_in_clone": True,
        }
        sealed_operations = operation_log.seal()
        report = DemoReport(
            output_path=output_root,
            repository_path=repository,
            writing_worktree_path=writing_worktree,
            remote_path=remote,
            clone_path=clone,
            bundle_path=bundle,
            report_path=report_path,
            temporary_output=temporary_output,
            commit_ids=commit_ids,
            claims=claims,
            notes_status=notes_status,
            pushed_refs=pushed_refs,
            bundle_verified=True,
            network_accessed=operation_log.network_accessed,
            operation_log=sealed_operations,
        )
        try:
            report_path.write_text(report.to_json(), encoding="utf-8", newline="\n")
        except OSError:
            raise _demo_error("The demo report could not be written.") from None
        written = DemoReport.from_path(report_path)
        if written != report:
            raise _demo_error("The written demo report does not match the returned report.")
        return report

    @staticmethod
    def _configure_repository(
        git: _GitExecutor, repository: Path, manifest: _ScenarioManifest
    ) -> None:
        settings = (
            ("user.name", manifest.identity_name),
            ("user.email", manifest.identity_email),
            ("core.autocrlf", "false"),
            ("core.filemode", "false"),
            ("core.safecrlf", "true"),
            ("core.hooksPath", os.devnull),
            ("core.editor", "true"),
            ("sequence.editor", "true"),
            ("commit.gpgSign", "false"),
            ("tag.gpgSign", "false"),
            ("merge.verifySignatures", "false"),
            ("credential.helper", ""),
            ("i18n.commitEncoding", "UTF-8"),
            ("i18n.logOutputEncoding", "UTF-8"),
        )
        for key, value in settings:
            git.run(repository, "config", "--local", key, value)

    @staticmethod
    def _commit(
        git: _GitExecutor,
        repository: Path,
        manifest: _ScenarioManifest,
        *,
        position: int,
        subject: str,
        reason: str,
        memory_type: str,
        decision_id: str | None = None,
    ) -> str:
        message = build_commit_message(
            subject,
            reason=reason,
            memory_type=memory_type,
            scope="irrigation",
            task_id=manifest.task_id,
            decision_id=decision_id,
            agent_id=_AGENT_ID,
        )
        git.run(repository, "add", "-A", "--")
        git.run(
            repository,
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "--cleanup=verbatim",
            "-F",
            "-",
            input_bytes=(message + "\n").encode("utf-8"),
            timestamp=manifest.timestamp(position),
        )
        return git.run(repository, "rev-parse", "HEAD").text().strip()

    @staticmethod
    def _integrate(repository: Path, manifest: _ScenarioManifest, *, position: int) -> MemoryResult:
        request = IntegrationRequest(
            source=manifest.sensor_hypothesis,
            target=manifest.canonical_branch,
            subject="Choose the sensor-threshold irrigation rule",
            metadata=CommitMetadata(
                memory_type="integration",
                scope="irrigation",
                task_id=manifest.task_id,
                decision_id=manifest.decision_id,
                agent_id=_AGENT_ID,
                reason="The comparison favors a bed-specific moisture threshold.",
            ),
        )
        with _service_environment(
            manifest.timestamp(position), manifest.identity_name, manifest.identity_email
        ):
            return _service(repository).integrate(request, apply=True)

    @staticmethod
    def _annotate(
        repository: Path,
        manifest: _ScenarioManifest,
        revision: str,
        *,
        position: int,
    ) -> MemoryResult:
        request = AnnotationRequest(
            revision=revision,
            message=_REVIEW_MESSAGE,
            agent_id=_REVIEW_AGENT,
        )
        with _service_environment(
            manifest.timestamp(position), manifest.identity_name, manifest.identity_email
        ):
            return _service(repository).annotate(request, apply=True)

    @staticmethod
    def _checkpoint(
        repository: Path,
        manifest: _ScenarioManifest,
        *,
        position: int,
        subject: str,
        reason: str,
        memory_type: str,
        path: PurePosixPath,
        decision_id: str | None = None,
    ) -> MemoryResult:
        request = CheckpointRequest(
            subject=subject,
            metadata=CommitMetadata(
                memory_type=memory_type,
                scope="irrigation",
                task_id=manifest.task_id,
                decision_id=decision_id,
                agent_id=_AGENT_ID,
                reason=reason,
            ),
            paths=(path,),
        )
        with _service_environment(
            manifest.timestamp(position), manifest.identity_name, manifest.identity_email
        ):
            return _service(repository).checkpoint(request, apply=True)

    @staticmethod
    def _correct(
        repository: Path,
        manifest: _ScenarioManifest,
        incorrect_oid: str,
        *,
        position: int,
    ) -> MemoryResult:
        request = CorrectionRequest(
            incorrect_revision=incorrect_oid,
            subject="Correct the irrigation threshold to thirty-five percent",
            metadata=CommitMetadata(
                memory_type="correction",
                scope="irrigation",
                task_id=manifest.task_id,
                decision_id=manifest.decision_id,
                agent_id=_AGENT_ID,
                reason="The sensor record confirms a thirty-five-percent threshold.",
            ),
            paths=(PurePosixPath("knowledge/irrigation-rules.json"),),
        )
        with _service_environment(
            manifest.timestamp(position), manifest.identity_name, manifest.identity_email
        ):
            return _service(repository).correct(request, apply=True)


def _default_scenario_resources() -> _ScenarioResources:
    """Capture the one maintained scenario from a checkout or an installed wheel."""
    package_root = files("mandua")
    if not isinstance(package_root, Path):
        raise _demo_error("Installed demo resources must be filesystem-backed.")
    try:
        package_path = _unambiguous_absolute_path(package_root)
        package_descriptor = _open_absolute_directory_no_follow(package_path)
    except OSError:
        raise _demo_error("The packaged demo scenario cannot be inspected.") from None
    try:
        package_state = _directory_state(os.fstat(package_descriptor))
        try:
            packaged_metadata = os.stat("_demo", dir_fd=package_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            resources = _capture_source_checkout_resources(package_path, package_descriptor)
        else:
            packaged_descriptor = _open_directory_at(
                package_descriptor, "_demo", expected=packaged_metadata
            )
            try:
                resources = _capture_scenario_root(packaged_descriptor, packaged_metadata)
            finally:
                os.close(packaged_descriptor)
        if _directory_state(os.fstat(package_descriptor)) != package_state:
            raise OSError("package directory changed during scenario selection")
        return resources
    except OSError:
        raise _demo_error("The packaged demo resource directory is invalid.") from None
    finally:
        os.close(package_descriptor)


def _source_checkout_scenario_path(package_root: Path) -> Path:
    """Bind the unpackaged fallback to the imported module's real source checkout."""
    try:
        package_path = _unambiguous_absolute_path(package_root)
        package_descriptor = _open_absolute_directory_no_follow(package_path)
    except OSError:
        raise _demo_error("The source demo checkout cannot be identified.") from None
    try:
        with _open_bound_source_checkout(package_path, package_descriptor) as bound:
            _capture_scenario_root(bound.demo_descriptor, bound.demo_metadata)
            _revalidate_bound_source_checkout(bound)
            return bound.checkout / "demo" / "scenario.toml"
    except OSError:
        raise _demo_error("The source demo checkout cannot be identified.") from None
    finally:
        os.close(package_descriptor)


def _capture_source_checkout_resources(
    package_path: Path, package_descriptor: int
) -> _ScenarioResources:
    with _open_bound_source_checkout(package_path, package_descriptor) as bound:
        resources = _capture_scenario_root(bound.demo_descriptor, bound.demo_metadata)
        _revalidate_bound_source_checkout(bound)
        return resources


@contextmanager
def _open_bound_source_checkout(
    package_path: Path, package_descriptor: int
) -> Iterator[_BoundSourceCheckout]:
    bound = _bind_source_checkout(package_path, package_descriptor)
    with ExitStack() as descriptors:
        descriptors.callback(os.close, bound.checkout_descriptor)
        descriptors.callback(os.close, bound.demo_descriptor)
        yield bound


def _bind_source_checkout(package_path: Path, package_descriptor: int) -> _BoundSourceCheckout:
    module_path = _unambiguous_absolute_path(Path(__file__))
    if (
        module_path != package_path / "demo.py"
        or package_path.name != "mandua"
        or package_path.parent.name != "src"
    ):
        raise OSError("imported module is not a source checkout")
    checkout = package_path.parent.parent
    with ExitStack() as pending:
        checkout_descriptor = _open_absolute_directory_no_follow(checkout)
        pending.callback(os.close, checkout_descriptor)
        checkout_state = _directory_state(os.fstat(checkout_descriptor))
        src_metadata = os.stat("src", dir_fd=checkout_descriptor, follow_symlinks=False)
        src_descriptor = _open_directory_at(checkout_descriptor, "src", expected=src_metadata)
        try:
            rebound_metadata = os.stat("mandua", dir_fd=src_descriptor, follow_symlinks=False)
            rebound = _open_directory_at(src_descriptor, "mandua", expected=rebound_metadata)
            try:
                if _entry_identity(os.fstat(rebound)) != _entry_identity(
                    os.fstat(package_descriptor)
                ):
                    raise OSError("source package identity mismatch")
            finally:
                os.close(rebound)
        finally:
            os.close(src_descriptor)
        metadata = os.stat("pyproject.toml", dir_fd=checkout_descriptor, follow_symlinks=False)
        try:
            project = tomllib.loads(
                _read_regular_at(
                    checkout_descriptor,
                    "pyproject.toml",
                    _MAX_MANIFEST_BYTES,
                    expected=metadata,
                ).decode("utf-8", "strict")
            )
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise _demo_error("The source demo checkout metadata is invalid.") from None
        project_table = project.get("project")
        if (
            not isinstance(project_table, dict)
            or not isinstance(project_table.get("name"), str)
            or project_table["name"] != "mandua-memory"
        ):
            raise _demo_error("The source demo checkout metadata is invalid.")
        try:
            _validate_source_checkout_descriptor(checkout_descriptor, checkout)
        except (OSError, UnicodeDecodeError):
            raise _demo_error("The source demo checkout Git marker is invalid.") from None
        demo_metadata = os.stat("demo", dir_fd=checkout_descriptor, follow_symlinks=False)
        demo_descriptor = _open_directory_at(
            checkout_descriptor,
            "demo",
            expected=demo_metadata,
        )
        pending.callback(os.close, demo_descriptor)
        bound = _BoundSourceCheckout(
            checkout=checkout,
            checkout_descriptor=checkout_descriptor,
            checkout_state=checkout_state,
            demo_descriptor=demo_descriptor,
            demo_metadata=demo_metadata,
        )
        _revalidate_bound_source_checkout(bound)
        pending.pop_all()
        return bound


def _revalidate_bound_source_checkout(bound: _BoundSourceCheckout) -> None:
    if _directory_state(os.fstat(bound.checkout_descriptor)) != bound.checkout_state:
        raise OSError("source checkout changed during demo binding")
    selected = os.stat(
        "demo",
        dir_fd=bound.checkout_descriptor,
        follow_symlinks=False,
    )
    opened = os.fstat(bound.demo_descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or _entry_identity(selected) != _entry_identity(bound.demo_metadata)
        or _entry_identity(opened) != _entry_identity(bound.demo_metadata)
    ):
        raise OSError("source demo identity changed during binding")


def _validate_source_checkout_marker(marker: Path, checkout: Path) -> None:
    """Require a descriptor-bound standard Git directory or exact linked-worktree marker."""
    invalid = "The source demo checkout Git marker is invalid."
    try:
        checkout_path = _unambiguous_absolute_path(checkout)
        marker_path = _unambiguous_absolute_path(marker)
        if marker_path != checkout_path / ".git":
            raise OSError("marker is not the checkout child")
        checkout_descriptor = _open_absolute_directory_no_follow(checkout_path)
        try:
            _validate_source_checkout_descriptor(checkout_descriptor, checkout_path)
        finally:
            os.close(checkout_descriptor)
    except (OSError, UnicodeDecodeError):
        raise _demo_error(invalid) from None


def _validate_source_checkout_descriptor(checkout_descriptor: int, checkout_path: Path) -> None:
    checkout_state = _directory_state(os.fstat(checkout_descriptor))
    marker_path = checkout_path / ".git"
    marker_metadata = os.stat(".git", dir_fd=checkout_descriptor, follow_symlinks=False)
    if stat.S_ISDIR(marker_metadata.st_mode):
        git_descriptor = _open_directory_at(checkout_descriptor, ".git", expected=marker_metadata)
        try:
            _validate_common_git_directory(git_descriptor, require_worktrees=False)
        finally:
            os.close(git_descriptor)
        if _directory_state(os.fstat(checkout_descriptor)) != checkout_state:
            raise OSError("checkout changed during Git validation")
        return
    if not stat.S_ISREG(marker_metadata.st_mode):
        raise OSError("invalid marker type")
    marker_payload = _read_regular_at(checkout_descriptor, ".git", 4_096, expected=marker_metadata)
    git_directory = _parse_locator(
        marker_payload,
        prefix="gitdir: ",
        base=checkout_path,
    )
    if git_directory.parent.name != "worktrees" or git_directory.parent.parent == Path("/"):
        raise OSError("invalid linked-worktree target shape")
    git_descriptor = _open_absolute_directory_no_follow(git_directory)
    try:
        target_state = _directory_state(os.fstat(git_descriptor))
        head_metadata = os.stat("HEAD", dir_fd=git_descriptor, follow_symlinks=False)
        head_payload = _read_regular_at(git_descriptor, "HEAD", 4_096, expected=head_metadata)
        _validate_git_head(head_payload)
        backlink_metadata = os.stat("gitdir", dir_fd=git_descriptor, follow_symlinks=False)
        backlink_payload = _read_regular_at(
            git_descriptor, "gitdir", 4_096, expected=backlink_metadata
        )
        backlink = _parse_locator(
            backlink_payload,
            prefix="",
            base=git_directory,
        )
        if backlink != marker_path:
            raise OSError("linked-worktree backlink mismatch")
        _validate_backlink_endpoint(
            backlink,
            checkout_descriptor=checkout_descriptor,
            marker_metadata=marker_metadata,
            marker_payload=marker_payload,
        )
        commondir_metadata = os.stat("commondir", dir_fd=git_descriptor, follow_symlinks=False)
        commondir_payload = _read_regular_at(
            git_descriptor, "commondir", 4_096, expected=commondir_metadata
        )
        common_directory = _parse_locator(
            commondir_payload,
            prefix="",
            base=git_directory,
        )
        if common_directory != git_directory.parent.parent:
            raise OSError("linked-worktree commondir mismatch")

        common_descriptor = _open_absolute_directory_no_follow(common_directory)
        try:
            common_state = _directory_state(os.fstat(common_descriptor))
            common_head_metadata = os.stat("HEAD", dir_fd=common_descriptor, follow_symlinks=False)
            common_head_payload = _read_regular_at(
                common_descriptor,
                "HEAD",
                4_096,
                expected=common_head_metadata,
            )
            common_config_metadata = os.stat(
                "config", dir_fd=common_descriptor, follow_symlinks=False
            )
            common_config_payload = _read_regular_at(
                common_descriptor,
                "config",
                _MAX_MANIFEST_BYTES,
                expected=common_config_metadata,
            )
            worktrees_descriptor = _validate_common_git_directory(
                common_descriptor, require_worktrees=True
            )
            if worktrees_descriptor is None:
                raise OSError("linked-worktree collection is unavailable")
            try:
                rebound_metadata = os.stat(
                    git_directory.name,
                    dir_fd=worktrees_descriptor,
                    follow_symlinks=False,
                )
                rebound = _open_directory_at(
                    worktrees_descriptor,
                    git_directory.name,
                    expected=rebound_metadata,
                )
                try:
                    if _entry_identity(os.fstat(rebound)) != _entry_identity(
                        os.fstat(git_descriptor)
                    ):
                        raise OSError("linked-worktree target mismatch")
                finally:
                    os.close(rebound)
            finally:
                os.close(worktrees_descriptor)
            if (
                _read_regular_at(
                    common_descriptor,
                    "HEAD",
                    4_096,
                    expected=common_head_metadata,
                )
                != common_head_payload
                or _read_regular_at(
                    common_descriptor,
                    "config",
                    _MAX_MANIFEST_BYTES,
                    expected=common_config_metadata,
                )
                != common_config_payload
                or _directory_state(os.fstat(common_descriptor)) != common_state
            ):
                raise OSError("common Git authority changed during topology validation")
        finally:
            os.close(common_descriptor)
        if (
            _read_regular_at(git_descriptor, "HEAD", 4_096, expected=head_metadata) != head_payload
            or _read_regular_at(git_descriptor, "gitdir", 4_096, expected=backlink_metadata)
            != backlink_payload
            or _read_regular_at(
                git_descriptor,
                "commondir",
                4_096,
                expected=commondir_metadata,
            )
            != commondir_payload
            or _directory_state(os.fstat(git_descriptor)) != target_state
        ):
            raise OSError("linked-worktree target changed during validation")
        if (
            _read_regular_at(checkout_descriptor, ".git", 4_096, expected=marker_metadata)
            != marker_payload
        ):
            raise OSError("linked-worktree marker changed during validation")
        _validate_backlink_endpoint(
            backlink,
            checkout_descriptor=checkout_descriptor,
            marker_metadata=marker_metadata,
            marker_payload=marker_payload,
        )
        if _directory_state(os.fstat(checkout_descriptor)) != checkout_state:
            raise OSError("checkout changed during Git validation")
    finally:
        os.close(git_descriptor)


def _validate_scenario_tree(scenario_path: Path) -> None:
    """Positively validate one filesystem-backed manifest and its exact fixture tree."""
    _capture_scenario_resources(scenario_path)


def _unambiguous_absolute_path(path: Path) -> Path:
    raw = os.fspath(path)
    if not raw or "\x00" in raw:
        raise OSError("invalid filesystem path")
    candidate = path if path.is_absolute() else Path.cwd() / path
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise OSError("ambiguous filesystem path")
    return candidate


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _entry_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _directory_state(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        *_entry_identity(value),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
) -> int:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise OSError("invalid directory entry")
    before = (
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if expected is None
        else expected
    )
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("directory entry is indirect or invalid")
    descriptor = os.open(
        name,
        _directory_open_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISDIR(after.st_mode) or _entry_identity(before) != _entry_identity(after):
            raise OSError("directory entry changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_absolute_directory_no_follow(path: Path) -> int:
    candidate = _unambiguous_absolute_path(path)
    descriptor = os.open("/", _directory_open_flags())
    try:
        for component in candidate.parts[1:]:
            child = _open_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    maximum: int,
    *,
    expected: os.stat_result | None = None,
) -> bytes:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 0
    ):
        raise OSError("invalid regular-file read")
    before = (
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if expected is None
        else expected
    )
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_size > maximum:
        raise OSError("regular file is indirect, invalid, or oversized")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
            raise OSError("regular file changed while opening")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    contents = b"".join(chunks)
    if (
        len(contents) > maximum
        or len(contents) != before.st_size
        or _file_identity(before) != _file_identity(final)
    ):
        raise OSError("regular file changed while reading")
    return contents


def _snapshot_fixture_directory(
    descriptor: int,
    *,
    prefix: str = "",
    count: list[int] | None = None,
) -> dict[str, bytes]:
    counter = count if count is not None else [0]
    before_state = _directory_state(os.fstat(descriptor))
    remaining = _MAX_FIXTURE_ENTRIES - counter[0]
    if remaining < 0:
        raise OSError("fixture directory contains too many entries")
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for _ in range(remaining + 1):
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                name = entry.name
                if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                    raise OSError("fixture directory contains an invalid entry")
                entries.append((name, entry.stat(follow_symlinks=False)))
    except OSError:
        raise OSError("fixture directory cannot be scanned") from None
    if len(entries) > remaining:
        raise OSError("fixture directory contains too many entries")
    if _directory_state(os.fstat(descriptor)) != before_state:
        raise OSError("fixture directory changed while scanning")
    counter[0] += len(entries)
    captured: dict[str, bytes] = {}
    for name, metadata in sorted(entries, key=lambda item: item[0]):
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISREG(metadata.st_mode):
            captured[relative] = _read_regular_at(
                descriptor,
                name,
                _MAX_REPORT_BYTES,
                expected=metadata,
            )
        elif stat.S_ISDIR(metadata.st_mode):
            child = _open_directory_at(descriptor, name, expected=metadata)
            try:
                captured.update(_snapshot_fixture_directory(child, prefix=relative, count=counter))
            finally:
                os.close(child)
        else:
            raise OSError("fixture directory contains an indirect or invalid entry")
    if _directory_state(os.fstat(descriptor)) != before_state:
        raise OSError("fixture directory changed while capturing")
    return captured


def _capture_scenario_resources(scenario_path: Path) -> _ScenarioResources:
    """Atomically snapshot one bounded maintained tree through no-follow descriptors."""
    try:
        scenario = _unambiguous_absolute_path(scenario_path)
        if scenario.name != "scenario.toml":
            raise OSError("unexpected scenario filename")
        root_descriptor = _open_absolute_directory_no_follow(scenario.parent)
        try:
            return _capture_scenario_root(root_descriptor, os.fstat(root_descriptor))
        finally:
            os.close(root_descriptor)
    except OSError:
        raise _demo_error("The maintained demo resource tree is invalid or unavailable.") from None


def _capture_scenario_root(
    root_descriptor: int,
    expected_root: os.stat_result,
) -> _ScenarioResources:
    """Capture immutable bytes from the exact already-bound scenario root."""
    opened_root = os.fstat(root_descriptor)
    if not stat.S_ISDIR(opened_root.st_mode) or _entry_identity(opened_root) != _entry_identity(
        expected_root
    ):
        raise OSError("scenario root identity mismatch")
    root_state = _directory_state(opened_root)
    manifest_metadata = os.stat("scenario.toml", dir_fd=root_descriptor, follow_symlinks=False)
    manifest_payload = _read_regular_at(
        root_descriptor,
        "scenario.toml",
        _MAX_MANIFEST_BYTES,
        expected=manifest_metadata,
    )
    fixture_metadata = os.stat("fixtures", dir_fd=root_descriptor, follow_symlinks=False)
    fixture_descriptor = _open_directory_at(root_descriptor, "fixtures", expected=fixture_metadata)
    try:
        fixture_payloads = _snapshot_fixture_directory(fixture_descriptor)
    finally:
        os.close(fixture_descriptor)
    if _directory_state(os.fstat(root_descriptor)) != root_state:
        raise OSError("scenario root changed during capture")
    if set(fixture_payloads) != _EXPECTED_FIXTURES:
        raise _demo_error("The versioned demo fixture inventory is incomplete or unexpected.")
    manifest = _parse_manifest_payload(manifest_payload)
    return _ScenarioResources(
        manifest=manifest,
        fixtures=tuple(sorted(fixture_payloads.items())),
    )


def _parse_locator(payload: bytes, *, prefix: str, base: Path) -> Path:
    decoded = payload.decode("utf-8", "strict")
    value = decoded.removeprefix(prefix).removesuffix("\n")
    if (
        decoded != f"{prefix}{value}\n"
        or not value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or value.startswith("//")
        or os.path.normpath(value) != value
    ):
        raise OSError("invalid administrative locator")
    path = Path(value) if value.startswith("/") else Path(os.path.normpath(base / value))
    if not path.is_absolute() or path == Path("/"):
        raise OSError("ambiguous administrative locator")
    return path


def _validate_backlink_endpoint(
    endpoint: Path,
    *,
    checkout_descriptor: int,
    marker_metadata: os.stat_result,
    marker_payload: bytes,
) -> None:
    """Bind a linked-worktree backlink to the exact open checkout marker."""
    if endpoint.name != ".git":
        raise OSError("linked-worktree backlink does not name the checkout marker")
    parent_descriptor = _open_absolute_directory_no_follow(endpoint.parent)
    try:
        if _entry_identity(os.fstat(parent_descriptor)) != _entry_identity(
            os.fstat(checkout_descriptor)
        ):
            raise OSError("linked-worktree backlink parent mismatch")
        endpoint_metadata = os.stat(
            endpoint.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _entry_identity(endpoint_metadata) != _entry_identity(marker_metadata)
            or _file_identity(endpoint_metadata) != _file_identity(marker_metadata)
            or _read_regular_at(
                parent_descriptor,
                endpoint.name,
                4_096,
                expected=endpoint_metadata,
            )
            != marker_payload
        ):
            raise OSError("linked-worktree backlink endpoint mismatch")
    finally:
        os.close(parent_descriptor)


def _validate_git_head(payload: bytes) -> None:
    decoded = payload.decode("utf-8", "strict")
    value = decoded.removesuffix("\n")
    if decoded != f"{value}\n" or not value:
        raise OSError("invalid Git HEAD")
    if value.startswith("ref: "):
        reference = value.removeprefix("ref: ")
        if not reference.startswith("refs/heads/") or not _valid_ref(reference):
            raise OSError("invalid symbolic Git HEAD")
    elif _FULL_OBJECT_ID.fullmatch(value) is None:
        raise OSError("invalid detached Git HEAD")


def _validate_common_git_directory(descriptor: int, *, require_worktrees: bool) -> int | None:
    directory_state = _directory_state(os.fstat(descriptor))
    head_metadata = os.stat("HEAD", dir_fd=descriptor, follow_symlinks=False)
    head_payload = _read_regular_at(descriptor, "HEAD", 4_096, expected=head_metadata)
    _validate_git_head(head_payload)
    config_metadata = os.stat("config", dir_fd=descriptor, follow_symlinks=False)
    config_payload = _read_regular_at(
        descriptor,
        "config",
        _MAX_MANIFEST_BYTES,
        expected=config_metadata,
    )
    for name in ("objects", "refs"):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        child = _open_directory_at(descriptor, name, expected=metadata)
        os.close(child)
    worktrees_descriptor: int | None = None
    try:
        if require_worktrees:
            worktrees_metadata = os.stat("worktrees", dir_fd=descriptor, follow_symlinks=False)
            worktrees_descriptor = _open_directory_at(
                descriptor, "worktrees", expected=worktrees_metadata
            )
        if (
            _read_regular_at(descriptor, "HEAD", 4_096, expected=head_metadata) != head_payload
            or _read_regular_at(
                descriptor,
                "config",
                _MAX_MANIFEST_BYTES,
                expected=config_metadata,
            )
            != config_payload
            or _directory_state(os.fstat(descriptor)) != directory_state
        ):
            raise OSError("common Git directory changed during validation")
        return worktrees_descriptor
    except BaseException:
        if worktrees_descriptor is not None:
            os.close(worktrees_descriptor)
        raise


def _service(repository: Path) -> MemoryService:
    return MemoryService.open(
        repository,
        limits=QueryLimits(timeout_seconds=_SERVICE_TIMEOUT_SECONDS),
    )


@contextmanager
def _service_environment(
    timestamp: datetime, identity_name: str, identity_email: str
) -> Iterator[None]:
    """Give service-created commits deterministic identity time and closed config."""
    values = {
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_AUTHOR_DATE": _render_timestamp(timestamp),
        "GIT_AUTHOR_EMAIL": identity_email,
        "GIT_AUTHOR_NAME": identity_name,
        "GIT_COMMITTER_DATE": _render_timestamp(timestamp),
        "GIT_COMMITTER_EMAIL": identity_email,
        "GIT_COMMITTER_NAME": identity_name,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    with _SERVICE_ENVIRONMENT_LOCK:
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _render_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _demo_error("Demo timestamps must use UTC.")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_manifest(path: Path) -> _ScenarioManifest:
    payload = _read_regular_file(path, _MAX_MANIFEST_BYTES, "scenario manifest")
    return _parse_manifest_payload(payload)


def _parse_manifest_payload(payload: bytes) -> _ScenarioManifest:
    """Parse one already bounded and physically captured scenario manifest."""
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise _demo_error("The demo scenario manifest is invalid TOML.") from None
    top = _strict_table(document, _EXPECTED_TOP_LEVEL, "scenario")
    if top["schema_version"] != _SCHEMA_VERSION:
        raise _demo_error("The demo scenario schema version is unsupported.")
    identity = _strict_table(top["identity"], _EXPECTED_IDENTITY, "identity")
    repository = _strict_table(top["repository"], _EXPECTED_REPOSITORY, "repository")
    story = _strict_table(top["story"], _EXPECTED_STORY, "story")
    timestamps = _strict_table(top["timestamps"], _EXPECTED_TIMESTAMPS, "timestamps")
    claim_values = _strict_table(top["claims"], _EXPECTED_CLAIMS, "claims")

    identity_name = _line(identity["name"], "The demo identity name is invalid.")
    identity_email = _line(identity["email"], "The demo identity email is invalid.")
    if "@" not in identity_email or identity_email.startswith("@") or identity_email.endswith("@"):
        raise _demo_error("The demo identity email is invalid.")
    canonical_branch = _branch(repository["canonical_branch"], "canonical branch")
    notes_ref = _notes_ref(repository["notes_ref"])
    task_id = _identifier(story["task_id"], "task ID")
    decision_id = _identifier(story["decision_id"], "decision ID")
    task_branch = _branch(story["task_branch"], "task branch")
    sensor_hypothesis = _branch(story["sensor_hypothesis"], "sensor hypothesis")
    schedule_hypothesis = _branch(story["schedule_hypothesis"], "schedule hypothesis")
    deleted_task_branch = _branch(story["deleted_task_branch"], "deleted task branch")
    recovery_branch = _branch(story["recovery_branch"], "recovery branch")
    branches = {
        canonical_branch,
        task_branch,
        sensor_hypothesis,
        schedule_hypothesis,
        deleted_task_branch,
        recovery_branch,
    }
    if len(branches) != 6 or sensor_hypothesis == schedule_hypothesis:
        raise _demo_error("Demo branch names must be distinct.")

    start_value = timestamps["start"]
    step_seconds = timestamps["step_seconds"]
    if not isinstance(start_value, str) or not start_value.endswith("Z"):
        raise _demo_error("The demo timestamp start must be an explicit UTC value.")
    try:
        start = datetime.fromisoformat(start_value)
    except ValueError:
        raise _demo_error("The demo timestamp start is invalid.") from None
    if (
        start.utcoffset() != timedelta(0)
        or not isinstance(step_seconds, int)
        or isinstance(step_seconds, bool)
        or step_seconds <= 0
        or step_seconds > 3600
    ):
        raise _demo_error("The demo timestamp sequence is invalid.")

    claim_ids: dict[str, str] = {}
    for name, value in claim_values.items():
        if not isinstance(value, str) or _CLAIM_ID.fullmatch(value) is None:
            raise _demo_error("A demo claim ID is invalid.")
        claim_ids[name] = value
    if len(set(claim_ids.values())) != len(claim_ids):
        raise _demo_error("Demo claim IDs must be unique.")
    tutorial = _parse_tutorial_manifest(
        top["tutorial"],
        canonical_claim_ids=claim_ids,
        identity_name=identity_name,
        identity_email=identity_email,
        canonical_branch=canonical_branch,
        notes_ref=notes_ref,
        task_id=task_id,
        decision_id=decision_id,
        task_branch=task_branch,
        sensor_hypothesis=sensor_hypothesis,
        schedule_hypothesis=schedule_hypothesis,
        deleted_task_branch=deleted_task_branch,
        recovery_branch=recovery_branch,
        start=start,
        step_seconds=step_seconds,
    )
    return _ScenarioManifest(
        identity_name=identity_name,
        identity_email=identity_email,
        canonical_branch=canonical_branch,
        notes_ref=notes_ref,
        task_id=task_id,
        task_branch=task_branch,
        decision_id=decision_id,
        sensor_hypothesis=sensor_hypothesis,
        schedule_hypothesis=schedule_hypothesis,
        deleted_task_branch=deleted_task_branch,
        recovery_branch=recovery_branch,
        start=start,
        step_seconds=step_seconds,
        claim_ids=claim_ids,
        tutorial=tutorial,
    )


def load_tutorial_manifest(path: Path) -> TutorialManifest:
    """Load the one strict tutorial projection from the canonical scenario manifest."""
    return _load_manifest(Path(path)).tutorial


def _parse_tutorial_manifest(
    value: object,
    *,
    canonical_claim_ids: dict[str, str],
    identity_name: str,
    identity_email: str,
    canonical_branch: str,
    notes_ref: str,
    task_id: str,
    decision_id: str,
    task_branch: str,
    sensor_hypothesis: str,
    schedule_hypothesis: str,
    deleted_task_branch: str,
    recovery_branch: str,
    start: datetime,
    step_seconds: int,
) -> TutorialManifest:
    tutorial = _strict_table(value, _EXPECTED_TUTORIAL, "tutorial")
    if tutorial["schema_version"] != _SCHEMA_VERSION:
        raise _demo_error("The tutorial schema version is unsupported.")
    title = _tutorial_text(tutorial["title"], "title", limit=200)
    introduction = _tutorial_text_list(
        tutorial["introduction"], "introduction", minimum=1, maximum=8, text_limit=600
    )

    canonical_keys = _tutorial_string_list(
        tutorial["canonical_claims"],
        "canonical claim keys",
        minimum=len(_EXPECTED_CLAIMS),
        maximum=len(_EXPECTED_CLAIMS),
    )
    if len(set(canonical_keys)) != len(canonical_keys) or set(canonical_keys) != set(
        _EXPECTED_CLAIMS
    ):
        raise _demo_error("The tutorial canonical claim keys are invalid.")

    additional_values = _strict_table(
        tutorial["claims"], _EXPECTED_TUTORIAL_CLAIMS, "tutorial claims"
    )
    additional_claim_ids: dict[str, str] = {}
    for name, claim_id in additional_values.items():
        if not isinstance(claim_id, str) or _CLAIM_ID.fullmatch(claim_id) is None:
            raise _demo_error("A tutorial claim ID is invalid.")
        additional_claim_ids[name] = claim_id
    all_claim_values = tuple(canonical_claim_ids.values()) + tuple(additional_claim_ids.values())
    if len(set(all_claim_values)) != len(all_claim_values):
        raise _demo_error("Tutorial claim IDs must be unique across the scenario.")

    limits = _parse_tutorial_limits(tutorial["limits"])
    raw_steps = tutorial["steps"]
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > _MAX_TUTORIAL_STEPS:
        raise _demo_error("The tutorial step count is invalid.")

    available_claim_ids = {**canonical_claim_ids, **additional_claim_ids}
    steps: list[TutorialStep] = []
    step_ids: set[str] = set()
    command_ids: set[str] = set()
    ordered_claim_keys: list[str] = []
    command_count = 0
    for raw_step in raw_steps:
        step = _strict_table(raw_step, _EXPECTED_TUTORIAL_STEP, "tutorial step")
        step_id = _tutorial_id(step["id"], "step")
        if step_id in step_ids:
            raise _demo_error("Tutorial step IDs must be unique.")
        step_ids.add(step_id)
        step_title = _tutorial_text(step["title"], "step title", limit=200)
        explanation = _tutorial_text(step["explanation"], "step explanation", limit=800)
        expected_evidence = _tutorial_text_list(
            step["expected_evidence"],
            "expected evidence",
            minimum=1,
            maximum=8,
            text_limit=500,
        )
        claim_keys = _tutorial_string_list(
            step["claims"], "step claim keys", minimum=0, maximum=len(available_claim_ids)
        )
        if len(set(claim_keys)) != len(claim_keys):
            raise _demo_error("Tutorial step claim keys must be unique.")
        if any(key not in available_claim_ids for key in claim_keys):
            raise _demo_error("A tutorial step references an unknown claim key.")
        for key in claim_keys:
            if key in ordered_claim_keys:
                raise _demo_error("Every tutorial claim must be documented exactly once.")
            ordered_claim_keys.append(key)

        raw_commands = step["commands"]
        if (
            not isinstance(raw_commands, list)
            or not raw_commands
            or len(raw_commands) > limits.max_step_commands
        ):
            raise _demo_error("A tutorial step command count is invalid.")
        commands: list[TutorialCommand] = []
        for raw_command in raw_commands:
            command = _parse_tutorial_command(raw_command, limits)
            if command.id in command_ids:
                raise _demo_error("Tutorial command IDs must be unique.")
            command_ids.add(command.id)
            commands.append(command)
            command_count += 1
        steps.append(
            TutorialStep(
                id=step_id,
                title=step_title,
                explanation=explanation,
                expected_evidence=expected_evidence,
                claim_keys=claim_keys,
                commands=tuple(commands),
            )
        )

    if command_count > limits.max_commands:
        raise _demo_error("The tutorial command count exceeds its declared bound.")
    if set(ordered_claim_keys) != set(available_claim_ids) or len(ordered_claim_keys) != len(
        available_claim_ids
    ):
        raise _demo_error("Every tutorial claim must be documented exactly once.")
    ordered_claim_ids = {
        claim_key: available_claim_ids[claim_key] for claim_key in ordered_claim_keys
    }
    return TutorialManifest(
        schema_version=_SCHEMA_VERSION,
        title=title,
        introduction=introduction,
        steps=tuple(steps),
        claim_ids=ordered_claim_ids,
        limits=limits,
        identity_name=identity_name,
        identity_email=identity_email,
        canonical_branch=canonical_branch,
        notes_ref=notes_ref,
        task_id=task_id,
        decision_id=decision_id,
        task_branch=task_branch,
        sensor_hypothesis=sensor_hypothesis,
        schedule_hypothesis=schedule_hypothesis,
        deleted_task_branch=deleted_task_branch,
        recovery_branch=recovery_branch,
        start=start,
        step_seconds=step_seconds,
    )


def _parse_tutorial_limits(value: object) -> TutorialLimits:
    raw = _strict_table(value, _EXPECTED_TUTORIAL_LIMITS, "tutorial limits")
    bounds = {
        "max_commands": _MAX_TUTORIAL_COMMANDS,
        "max_step_commands": _MAX_TUTORIAL_STEP_COMMANDS,
        "max_argument_count": _MAX_TUTORIAL_ARGUMENTS,
        "max_argument_bytes": _MAX_TUTORIAL_ARGUMENT_BYTES,
        "max_command_output_bytes": _MAX_TUTORIAL_COMMAND_OUTPUT_BYTES,
        "max_total_output_bytes": _MAX_TUTORIAL_TOTAL_OUTPUT_BYTES,
        "timeout_seconds": _MAX_TUTORIAL_TIMEOUT_SECONDS,
    }
    parsed: dict[str, int] = {}
    for name, maximum in bounds.items():
        item = raw[name]
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0 or item > maximum:
            raise _demo_error(f"The tutorial {name} limit is invalid.")
        parsed[name] = item
    if parsed["max_step_commands"] > parsed["max_commands"]:
        raise _demo_error("The tutorial command limits are inconsistent.")
    if parsed["max_command_output_bytes"] > parsed["max_total_output_bytes"]:
        raise _demo_error("The tutorial output limits are inconsistent.")
    return TutorialLimits(**parsed)


def _parse_tutorial_command(value: object, limits: TutorialLimits) -> TutorialCommand:
    raw = _strict_table(value, _EXPECTED_TUTORIAL_COMMAND, "tutorial command")
    command_id = _tutorial_id(raw["id"], "command")
    cwd = raw["cwd"]
    if not isinstance(cwd, str) or cwd not in _TUTORIAL_CWD_PLACEHOLDERS:
        raise _demo_error("A tutorial command working directory is invalid.")
    argv = _tutorial_string_list(
        raw["argv"], "command argument array", minimum=1, maximum=limits.max_argument_count
    )
    if argv[0] not in _TUTORIAL_EXECUTABLES:
        raise _demo_error("A tutorial command executable is outside the allowlist.")
    argument_bytes = 0
    for argument in argv:
        try:
            encoded = argument.encode("utf-8")
        except UnicodeEncodeError:
            raise _demo_error("A tutorial command argument is invalid UTF-8.") from None
        if not encoded or b"\x00" in encoded or len(encoded) > 4_096:
            raise _demo_error("A tutorial command argument is invalid.")
        argument_bytes += len(encoded)
        if ("{{" in argument or "}}" in argument) and argument not in _TUTORIAL_PLACEHOLDERS:
            raise _demo_error("A tutorial command placeholder is unknown or embedded.")
    if argument_bytes > limits.max_argument_bytes:
        raise _demo_error("A tutorial command exceeds its argument byte bound.")
    expected_exit = raw["expected_exit"]
    if (
        not isinstance(expected_exit, int)
        or isinstance(expected_exit, bool)
        or expected_exit < 0
        or expected_exit > 255
    ):
        raise _demo_error("A tutorial command expected exit code is invalid.")
    return TutorialCommand(
        id=command_id,
        cwd=cwd,
        argv=argv,
        expected_exit=expected_exit,
    )


def _tutorial_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _CLAIM_ID.fullmatch(value) is None or len(value) > 80:
        raise _demo_error(f"A tutorial {label} ID is invalid.")
    return value


def _tutorial_text(value: object, label: str, *, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or len(value) > limit
    ):
        raise _demo_error(f"The tutorial {label} is invalid.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _demo_error(f"The tutorial {label} is invalid.") from None
    return value


def _tutorial_text_list(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int,
    text_limit: int,
) -> tuple[str, ...]:
    values = _tutorial_string_list(value, label, minimum=minimum, maximum=maximum)
    return tuple(_tutorial_text(item, label, limit=text_limit) for item in values)


def _tutorial_string_list(
    value: object, label: str, *, minimum: int, maximum: int
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) > maximum
        or any(not isinstance(item, str) for item in value)
    ):
        raise _demo_error(f"The tutorial {label} is invalid.")
    return tuple(value)


def _strict_table(value: object, keys: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _demo_error(f"The demo {name} table has unknown or missing fields.")
    return value


def _line(value: object, message: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value) > 200
    ):
        raise _demo_error(message)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _demo_error(message) from None
    return value


def _identifier(value: object, name: str) -> str:
    checked = _line(value, f"The demo {name} is invalid.")
    if _IDENTIFIER.fullmatch(checked) is None:
        raise _demo_error(f"The demo {name} is invalid.")
    return checked


def _branch(value: object, name: str) -> str:
    checked = _line(value, f"The demo {name} is invalid.")
    if checked.startswith(("-", "refs/")) or not _valid_ref(checked):
        raise _demo_error(f"The demo {name} is invalid.")
    return checked


def _notes_ref(value: object) -> str:
    checked = _line(value, "The demo notes ref is invalid.")
    if not checked.startswith("refs/notes/") or not _valid_ref(checked):
        raise _demo_error("The demo notes ref is invalid.")
    return checked


def _valid_ref(value: str) -> bool:
    return not (
        any(
            character.isspace() or ord(character) < 32 or character in "~^:?*[\\"
            for character in value
        )
        or value.endswith((".", "/"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            not component
            or component in {".", "..", "@"}
            or component.startswith(".")
            or component.endswith(".lock")
            for component in value.split("/")
        )
    )


def _validate_fixture_inventory(root: Path) -> None:
    try:
        resolved = root.resolve(strict=True)
    except OSError:
        raise _demo_error("The versioned demo fixture directory is unavailable.") from None
    if not resolved.is_dir():
        raise _demo_error("The versioned demo fixture directory is invalid.")
    inventory: set[str] = set()
    for path in resolved.rglob("*"):
        try:
            metadata = os.lstat(path)
        except OSError:
            raise _demo_error("A versioned demo fixture cannot be inspected.") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _demo_error("Versioned demo fixtures must not contain symbolic links.")
        if stat.S_ISREG(metadata.st_mode):
            inventory.add(path.relative_to(resolved).as_posix())
        elif not stat.S_ISDIR(metadata.st_mode):
            raise _demo_error("Versioned demo fixtures must contain only regular files.")
    if inventory != _EXPECTED_FIXTURES:
        raise _demo_error("The versioned demo fixture inventory is incomplete or unexpected.")
    for relative in _EXPECTED_FIXTURES:
        _read_regular_file(resolved / relative, _MAX_REPORT_BYTES, "fixture")


def _install_fixture_tree(resources: _ScenarioResources, fixture: str, destination: Path) -> None:
    prefix = f"{fixture}/"
    selected = sorted(path for path in _EXPECTED_FIXTURES if path.startswith(prefix))
    if not selected:
        raise _demo_error("The requested versioned demo fixture is unavailable.")
    for relative in selected:
        target_relative = relative.removeprefix(prefix)
        target = destination / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        contents = resources.fixture(relative)
        try:
            target.write_bytes(contents)
            target.chmod(0o644)
        except OSError:
            raise _demo_error("A versioned demo fixture could not be installed.") from None


def _fixture_text(resources: _ScenarioResources, relative: str) -> str:
    try:
        return resources.fixture(relative).decode("utf-8")
    except UnicodeDecodeError:
        raise _demo_error("A versioned demo fixture must be valid UTF-8.") from None


def _quoted_fixture_text(resources: _ScenarioResources, relative: str) -> str:
    lines = [
        line[2:]
        for line in _fixture_text(resources, relative).splitlines()
        if line.startswith("> ")
    ]
    if len(lines) != 1 or not lines[0]:
        raise _demo_error("The untrusted-history fixture must contain one quoted data line.")
    return lines[0]


def _fixture_threshold(resources: _ScenarioResources, relative: str) -> int:
    try:
        document = json.loads(_fixture_text(resources, relative))
    except json.JSONDecodeError:
        raise _demo_error("An irrigation fixture must contain valid JSON.") from None
    if (
        not isinstance(document, list)
        or len(document) != 1
        or not isinstance(document[0], dict)
        or not isinstance(document[0].get("threshold_percent"), int)
        or isinstance(document[0]["threshold_percent"], bool)
    ):
        raise _demo_error("An irrigation fixture must contain one numeric threshold.")
    return document[0]["threshold_percent"]


def _read_regular_file(path: Path, maximum: int, label: str) -> bytes:
    try:
        before = os.lstat(path)
    except OSError:
        raise _demo_error(f"The demo {label} is unavailable.") from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise _demo_error(f"The demo {label} must be one bounded regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _demo_error(f"The demo {label} cannot be opened safely.") from None
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or _file_identity(before) != _file_identity(after):
            raise _demo_error(f"The demo {label} changed while it was opened.")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    contents = b"".join(chunks)
    if (
        len(contents) > maximum
        or len(contents) != before.st_size
        or _file_identity(before) != _file_identity(final)
    ):
        raise _demo_error(f"The demo {label} changed while it was read.")
    return contents


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _prepare_output(output: Path | None) -> tuple[Path, bool]:
    if output is None:
        try:
            return Path(tempfile.mkdtemp(prefix="mandua-demo-")).resolve(strict=True), True
        except OSError:
            raise _demo_error("A temporary demo output directory could not be created.") from None
    target = Path(output)
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        try:
            target.mkdir(parents=True, exist_ok=False)
        except OSError:
            raise _demo_error("The explicit demo output directory could not be created.") from None
    except OSError:
        raise _demo_error("The explicit demo output target cannot be inspected.") from None
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _demo_error("The explicit demo output must be a real directory.")
        try:
            if next(target.iterdir(), None) is not None:
                raise _demo_error("The explicit demo output directory must be empty.")
        except OSError:
            raise _demo_error("The explicit demo output directory cannot be inspected.") from None
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise _demo_error("The explicit demo output directory is unavailable.") from None
    if not resolved.is_dir():
        raise _demo_error("The explicit demo output directory is invalid.")
    return resolved, False


def _require_manifest_policy(repository: Path, manifest: _ScenarioManifest) -> None:
    policy = Policy.open(repository).repository_policy
    if (
        policy.canonical_branch != manifest.canonical_branch
        or policy.notes_ref != manifest.notes_ref
    ):
        raise _demo_error("The baseline policy diverges from the canonical scenario manifest.")


def _result_commit_oid(result: MemoryResult, operation: str) -> str:
    expected = {
        "integration": "integrate",
        "checkpoint": "checkpoint",
        "correction": "correct",
    }.get(operation)
    if expected is None or result.operation != expected:
        raise _demo_error("A demo service mutation returned the wrong operation.")
    for evidence in result.evidence:
        commit_oid = evidence.details.get("commit_oid")
        if isinstance(commit_oid, str) and re.fullmatch(
            r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit_oid
        ):
            return commit_oid
    raise _demo_error("A demo service mutation did not report its commit ID.")


def _require_recovery_candidate(result: MemoryResult, expected: str) -> None:
    if not any(
        item.kind == "recovery-candidate" and item.oid == expected for item in result.evidence
    ):
        raise _demo_error("The deleted task commit was not recoverable through MemoryService.")


def _require_review_note(value: str) -> None:
    expected = f"{_REVIEW_MESSAGE}\n\nAgent-ID: {_REVIEW_AGENT}\n"
    if value != expected:
        raise _demo_error("The configured review note is not visible exactly.")


def _build_claims(
    manifest: _ScenarioManifest,
    *,
    decision: MemoryResult,
    comparison: MemoryResult,
    integration: MemoryResult,
    annotation: MemoryResult,
    correction: MemoryResult,
    deleted_origin: MemoryResult,
    recovery: MemoryResult,
    malicious_origin: MemoryResult,
    deleted_phrase: str,
    malicious_quote: str,
    incorrect_threshold: int,
    corrected_threshold: int,
    recoverable_oid: str,
) -> dict[str, dict[str, object]]:
    decision_items = tuple(
        item
        for item in decision.evidence
        if item.kind in {"decision-commit", "decision-correction"} and item.oid is not None
    )
    if not decision_items:
        raise _demo_error("The canonical decision claim has no service evidence.")
    merge_base = _one_evidence(comparison, "merge-base")
    integration_item = _one_evidence(integration, "integration-preview")
    annotation_item = _one_evidence(annotation, "annotation-preview")
    correction_item = _one_evidence(correction, "correction-preview")
    added = _one_evidence(deleted_origin, "content-added")
    removed = _one_evidence(deleted_origin, "content-removed")
    recovery_item = next(
        (
            item
            for item in recovery.evidence
            if item.kind == "recovery-candidate" and item.oid == recoverable_oid
        ),
        None,
    )
    malicious = _one_evidence(malicious_origin, "content-added")
    review_item = next(
        (
            item
            for item in decision.evidence
            if item.kind == "review-note" and item.oid == annotation_item.oid
        ),
        None,
    )
    if recovery_item is None or review_item is None:
        raise _demo_error("A named demo claim is missing public service evidence.")
    parent_oids = integration_item.details.get("parent_oids")
    commit_oid = integration_item.details.get("commit_oid")
    corrects_oid = correction_item.details.get("corrects_oid")
    correction_oid = correction_item.details.get("commit_oid")
    if (
        not isinstance(parent_oids, list)
        or any(not isinstance(item, str) for item in parent_oids)
        or not isinstance(commit_oid, str)
        or not isinstance(corrects_oid, str)
        or not isinstance(correction_oid, str)
        or added.oid is None
        or removed.oid is None
        or malicious.oid is None
        or merge_base.oid is None
    ):
        raise _demo_error("A named demo claim contains malformed public service evidence.")
    return {
        manifest.claim_ids["decision"]: {
            "decision_id": manifest.decision_id,
            "selected_hypothesis": manifest.sensor_hypothesis,
            "commit_oids": [item.oid for item in decision_items],
        },
        manifest.claim_ids["comparison"]: {
            "merge_base_oid": merge_base.oid,
            "left_oid": merge_base.details["left_oid"],
            "right_oid": merge_base.details["right_oid"],
        },
        manifest.claim_ids["integration"]: {
            "commit_oid": commit_oid,
            "parent_oids": parent_oids,
            "source": manifest.sensor_hypothesis,
            "target": manifest.canonical_branch,
        },
        manifest.claim_ids["review"]: {
            "target_oid": annotation_item.oid,
            "notes_ref": annotation_item.ref,
            "message": _REVIEW_MESSAGE,
        },
        manifest.claim_ids["correction"]: {
            "incorrect_oid": corrects_oid,
            "correction_oid": correction_oid,
            "incorrect_threshold_percent": incorrect_threshold,
            "corrected_threshold_percent": corrected_threshold,
        },
        manifest.claim_ids["deleted_origin"]: {
            "phrase": deleted_phrase,
            "added_oid": added.oid,
            "removed_oid": removed.oid,
        },
        manifest.claim_ids["recovery"]: {
            "candidate_oid": recovery_item.oid,
            "deleted_branch": manifest.deleted_task_branch,
            "recovered_branch": manifest.recovery_branch,
        },
        manifest.claim_ids["malicious_history"]: {
            "commit_oid": malicious.oid,
            "path": malicious.path,
            "quoted_text": malicious_quote,
            "classification": "untrusted-data",
        },
    }


def _one_evidence(result: MemoryResult, kind: str):
    matches = tuple(item for item in result.evidence if item.kind == kind)
    if len(matches) != 1:
        raise _demo_error(f"The demo expected exactly one {kind} evidence record.")
    return matches[0]


def _report_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise _demo_error(f"The demo report {name} is invalid.")
    path = Path(value)
    if not path.is_absolute():
        raise _demo_error(f"The demo report {name} must be absolute.")
    return path


def _string_mapping(value: object, name: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(key, str) or not key or not isinstance(item, str) or not item
            for key, item in value.items()
        )
    ):
        raise _demo_error(f"The demo report {name} are invalid.")
    return dict(value)


def _claims_mapping(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or not value:
        raise _demo_error("The demo report claims are invalid.")
    claims: dict[str, dict[str, object]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, dict):
            raise _demo_error("The demo report claims are invalid.")
        claims[key] = item
    return claims


def _object_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _demo_error(f"The demo report {name} is invalid.")
    return dict(value)


def _demo_error(message: str) -> ManduaError:
    return ManduaError(ErrorCode.VALIDATION_FAILED, message)
