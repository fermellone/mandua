"""Bounded versioned Git hooks over Mandu'a's shared repository policy."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from mandua.config import parse_repository_policy
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import (
    GitOperationBudget,
    GitOutput,
    GitRepositoryAuthority,
    GitRunner,
)
from mandua.metadata import parse_trailers
from mandua.models import QueryLimits, RepositoryPolicy
from mandua.policy import Policy

_HOOK_NAMES = frozenset({"pre-commit", "commit-msg", "pre-rebase", "pre-push"})
_CANONICAL_TRAILERS = {
    "Memory-Type": "memory_type",
    "Scope": "scope",
    "Task-ID": "task_id",
    "Decision-ID": "decision_id",
    "Agent-ID": "agent_id",
    "Corrects": "corrects",
}
_REF_FORBIDDEN = frozenset(" ~^:?*[\\")
_HEX_OBJECT_ID = re.compile(r"[0-9a-fA-F]+")
_MAX_PUSH_LINES = 512
_MAX_DIAGNOSTIC_CHARS = 400
_POLICY_PATH = ".mandua.toml"


@dataclass(frozen=True, slots=True)
class _AuthorityRunner:
    """Route every hook Git query through one isolated, physically bound authority."""

    base_runner: GitRunner
    authority: GitRepositoryAuthority

    def require_primary_objects(self) -> None:
        """Reject indirect object roots while preserving this physical authority."""
        self.base_runner.primary_object_directory(self.authority)

    def run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
        input_bytes: bytes = b"",
        max_output_bytes: int | None = None,
        literal_pathspecs: bool = False,
    ) -> GitOutput[bytes]:
        self.require_primary_objects()
        try:
            return self.base_runner.run(
                arguments,
                check=check,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
                max_output_bytes=max_output_bytes,
                literal_pathspecs=literal_pathspecs,
                isolated_configuration=True,
                repository_authority=self.authority,
            )
        finally:
            self.require_primary_objects()

    def run_text(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
        input_bytes: bytes = b"",
        max_output_bytes: int | None = None,
        literal_pathspecs: bool = False,
    ) -> GitOutput[str]:
        self.require_primary_objects()
        try:
            return self.base_runner.run_text(
                arguments,
                check=check,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
                max_output_bytes=max_output_bytes,
                literal_pathspecs=literal_pathspecs,
                isolated_configuration=True,
                repository_authority=self.authority,
            )
        finally:
            self.require_primary_objects()


@dataclass(frozen=True, slots=True)
class _HookContext:
    repository: Path
    base_runner: GitRunner
    runner: _AuthorityRunner
    authority: GitRepositoryAuthority
    limits: QueryLimits

    def require_primary_objects(self) -> None:
        """Reject repository-local alternate object roots for this hook operation."""
        self.runner.require_primary_objects()

    def default_policy(self) -> Policy:
        """Return policy mechanics without consulting mutable worktree configuration."""
        return Policy(self.repository, self.runner, RepositoryPolicy(), self.limits)


@dataclass(frozen=True, slots=True)
class _PushUpdate:
    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str


def run_hook(
    repository: Path,
    name: str,
    argv: tuple[str, ...],
    stdin: str,
) -> int:
    """Run one typed hook boundary, writing one bounded rejection to stderr."""
    try:
        limits = QueryLimits()
        _validate_boundary(name, argv, stdin, limits)
        context = _open_repository(repository, limits, bind_index=name == "pre-commit")
        try:
            if name == "pre-commit":
                _run_pre_commit(context)
            elif name == "commit-msg":
                _run_commit_msg(context, argv[0])
            elif name == "pre-rebase":
                _run_pre_rebase(context, argv)
            elif name == "pre-push":
                _run_pre_push(context, stdin)
            else:  # The boundary validation above makes this unreachable.
                raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook name is invalid.")
        finally:
            try:
                context.require_primary_objects()
            finally:
                context.authority.cleanup()
        return 0
    except ManduaError as error:
        _write_rejection(error.message)
        return 1
    except Exception:  # noqa: BLE001 - hooks expose one safe operational failure bit.
        _write_rejection("The Git hook failed because of an unexpected internal error.")
        return 1


def repository_from_cwd(cwd: Path) -> Path:
    """Resolve one hook repository from its execution cwd through controlled Git."""
    context = _open_repository(cwd, QueryLimits(), bind_index=False)
    try:
        return context.repository
    finally:
        context.authority.cleanup()


def _validate_boundary(
    name: object,
    argv: object,
    stdin: object,
    limits: QueryLimits,
) -> None:
    if not isinstance(name, str) or name not in _HOOK_NAMES:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook name is invalid.")
    if not isinstance(argv, tuple) or any(not isinstance(value, str) for value in argv):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook arguments are invalid.")
    expected = {
        "pre-commit": (0, 0),
        "commit-msg": (1, 1),
        "pre-rebase": (1, 2),
        "pre-push": (2, 2),
    }[name]
    if not expected[0] <= len(argv) <= expected[1]:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook arguments are invalid.")
    aggregate = 0
    for index, value in enumerate(argv):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ManduaError(
                ErrorCode.VALIDATION_FAILED, "The Git hook arguments are invalid."
            ) from None
        aggregate += len(encoded)
        if (
            (not value and not (name == "pre-rebase" and index == 0))
            or len(value) > limits.max_input_chars
            or b"\x00" in encoded
            or "\n" in value
            or "\r" in value
            or aggregate > limits.max_output_bytes
        ):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook arguments are invalid.")
    if not isinstance(stdin, str):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook input is invalid.")
    if len(stdin) > limits.max_output_bytes:
        raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "The Git hook input exceeds the byte limit.")
    try:
        encoded_stdin = stdin.encode("utf-8")
    except UnicodeEncodeError:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook input is invalid.") from None
    if len(encoded_stdin) > limits.max_output_bytes:
        raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "The Git hook input exceeds the byte limit.")
    if "\x00" in stdin:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook input is invalid.")
    if name != "pre-push" and stdin:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The Git hook input is invalid.")


def _open_repository(
    repository: object,
    limits: QueryLimits,
    *,
    bind_index: bool,
) -> _HookContext:
    if not isinstance(repository, Path):
        raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")
    try:
        root = repository.resolve(strict=True)
    except OSError:
        raise ManduaError(
            ErrorCode.INVALID_REPOSITORY, "The repository directory is unavailable."
        ) from None
    if not root.is_dir():
        raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")
    runner = GitRunner(root, limits=limits, operation_budget=GitOperationBudget(limits))
    authority = runner.open_repository_authority(None, bind_index=bind_index)
    authority_runner = _AuthorityRunner(runner, authority)
    try:
        output = authority_runner.run_text(["rev-parse", "--show-toplevel"], check=False)
        line = _one_git_line(output, "Git could not determine the hook repository.")
        top_level = Path(line).resolve(strict=True)
        if top_level != root:
            raise ManduaError(ErrorCode.INVALID_REPOSITORY, "The repository directory is invalid.")
        runner.validate_repository_authority(authority)
        return _HookContext(root, runner, authority_runner, authority, limits)
    except BaseException:
        authority.cleanup()
        raise


def _run_pre_commit(context: _HookContext) -> None:
    index_contents = context.base_runner.repository_index_snapshot(context.authority)
    context.default_policy().validate_staged_index(index_contents)


def _run_commit_msg(context: _HookContext, argument: str) -> None:
    message = _read_commit_message(context, argument)
    _validate_commit_message(context.default_policy(), message)


def _read_commit_message(context: _HookContext, argument: str) -> str:
    raw_path = Path(argument)
    if ".." in raw_path.parts:
        raise ManduaError(
            ErrorCode.POLICY_VIOLATION,
            "The commit message path is outside the Git administrative directory.",
        )
    git_directory = context.authority.git_directory
    candidate = raw_path if raw_path.is_absolute() else context.repository / raw_path
    candidate = Path(os.path.abspath(candidate))
    if candidate.parent != git_directory or candidate.name in {"", ".", ".."}:
        raise ManduaError(
            ErrorCode.POLICY_VIOLATION,
            "The commit message path is outside the Git administrative directory.",
        )
    contents = context.base_runner.read_repository_administrative_file(
        context.authority,
        candidate.name,
        maximum=context.limits.max_output_bytes,
    )
    context.base_runner.validate_repository_authority(context.authority)
    try:
        decoded = contents.decode("utf-8")
    except UnicodeDecodeError:
        raise ManduaError(
            ErrorCode.VALIDATION_FAILED, "The commit message must be valid UTF-8."
        ) from None
    return decoded.removesuffix("\n")


def _validate_commit_message(policy: Policy, message: str) -> None:
    if not message or "\x00" in message or "\r" in message:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit message is invalid.")
    prefix, trailer_separator, trailer_block = message.rpartition("\n\n")
    trailers = parse_trailers(trailer_block) if trailer_separator else ()
    if not trailers:
        prefix = message
    subject, separator, body = prefix.partition("\n\n")
    if "\n" in subject or (not trailers and (separator or "\n" in prefix)):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit message is invalid.")
    reason: str | None = None
    if separator:
        if "\n" in body or not body.startswith("Reason: "):
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit message is invalid.")
        reason = body.removeprefix("Reason: ")
    values: dict[str, str | None] = {name: None for name in _CANONICAL_TRAILERS.values()}
    extras: list[tuple[str, str]] = []
    for key, value in trailers:
        field = _CANONICAL_TRAILERS.get(key)
        if field is None:
            extras.append((key, value))
            continue
        if values[field] is not None:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit trailers are invalid.")
        values[field] = value
    rebuilt = policy.validate_message(
        subject,
        reason=reason,
        memory_type=values["memory_type"],
        scope=values["scope"],
        task_id=values["task_id"],
        decision_id=values["decision_id"],
        agent_id=values["agent_id"],
        corrects=values["corrects"],
        extra_trailers=tuple(extras),
    )
    if rebuilt != message:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "The commit message is invalid.")


def _run_pre_rebase(context: _HookContext, argv: tuple[str, ...]) -> None:
    branch_ref = _branch_to_rebase(context, argv)
    if branch_ref is None:
        return
    width = _object_width(context.runner)
    branch_tip = _resolve_branch_tip(context.runner, branch_ref, width)
    policy = _load_committed_policy(context, branch_tip, width)
    if _resolve_branch_tip(context.runner, branch_ref, width) != branch_tip:
        raise ManduaError(
            ErrorCode.GIT_FAILURE, "The branch to rebase changed during policy validation."
        )
    if branch_ref == f"refs/heads/{policy.canonical_branch}":
        raise ManduaError(
            ErrorCode.POLICY_VIOLATION, "The canonical branch cannot be rebased or rewritten."
        )


def _branch_to_rebase(context: _HookContext, argv: tuple[str, ...]) -> str | None:
    if len(argv) == 1:
        output = context.runner.run_text(["symbolic-ref", "--quiet", "HEAD"], check=False)
        if output.returncode == 1 and not output.stdout and not output.stderr:
            return None
        branch_ref = _one_git_line(output, "Git returned an invalid current branch.")
    else:
        output = context.runner.run_text(
            [
                "rev-parse",
                "--symbolic-full-name",
                "--verify",
                "--end-of-options",
                argv[1],
            ],
            check=False,
        )
        if output.returncode != 0 or output.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not resolve the branch to rebase.")
        if not output.stdout:
            return None
        branch_ref = _one_git_line(output, "Git returned an invalid branch to rebase.")
    _validate_full_ref(branch_ref)
    if not branch_ref.startswith("refs/heads/"):
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git did not identify a local branch to rebase.")
    return branch_ref


def _run_pre_push(context: _HookContext, stdin: str) -> None:
    width = _object_width(context.runner)
    updates = _parse_push_updates(stdin, width, context.limits)
    zeros = "0" * width
    for update in updates:
        local_zero = update.local_oid == zeros
        remote_zero = update.remote_oid == zeros
        if (update.local_ref == "(delete)") != local_zero:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "A pre-push update is malformed.")
        if local_zero and remote_zero:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "A pre-push update is malformed.")
        if not update.remote_ref.startswith("refs/heads/"):
            continue
        if remote_zero:
            _load_committed_policy(context, update.local_oid, width)
            continue

        old_policy = _load_committed_policy(context, update.remote_oid, width)
        old_canonical_ref = f"refs/heads/{old_policy.canonical_branch}"
        if local_zero:
            if update.remote_ref == old_canonical_ref:
                raise ManduaError(
                    ErrorCode.POLICY_VIOLATION, "The canonical branch cannot be deleted."
                )
            continue

        new_policy = _load_committed_policy(context, update.local_oid, width)
        new_canonical_ref = f"refs/heads/{new_policy.canonical_branch}"
        was_canonical = update.remote_ref == old_canonical_ref
        becomes_canonical = update.remote_ref == new_canonical_ref
        if was_canonical != becomes_canonical:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The canonical branch cannot change during an existing push.",
            )
        if not was_canonical:
            continue
        ancestry = context.runner.run_text(
            ["merge-base", "--is-ancestor", update.remote_oid, update.local_oid],
            check=False,
        )
        if ancestry.stdout or ancestry.stderr:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid ancestry result.")
        if ancestry.returncode == 1:
            raise ManduaError(
                ErrorCode.POLICY_VIOLATION,
                "The canonical branch update must be a fast-forward.",
            )
        if ancestry.returncode != 0:
            raise ManduaError(ErrorCode.GIT_FAILURE, "Git could not verify canonical ancestry.")


def _resolve_branch_tip(runner: _AuthorityRunner, branch_ref: str, width: int) -> str:
    output = runner.run_text(["rev-parse", "--verify", "--end-of-options", branch_ref], check=False)
    branch_tip = _one_git_line(output, "Git could not resolve the branch tip to rebase.")
    if len(branch_tip) != width or _HEX_OBJECT_ID.fullmatch(branch_tip) is None:
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid branch tip to rebase.")
    return branch_tip.lower()


def _load_committed_policy(context: _HookContext, commit_oid: str, width: int) -> RepositoryPolicy:
    _require_commit(context.runner, commit_oid)
    listing = context.runner.run(["ls-tree", "-z", commit_oid, "--", _POLICY_PATH], check=False)
    if listing.returncode != 0 or listing.stderr:
        raise _unavailable_policy_object()
    if not listing.stdout:
        return parse_repository_policy(b"", context.limits)

    records = listing.stdout.split(b"\x00")
    if len(records) != 2 or records[1]:
        raise _invalid_policy_entry()
    try:
        header, raw_path = records[0].split(b"\t", 1)
        mode, object_type, raw_object_id = header.split(b" ")
        object_id = raw_object_id.decode("ascii")
    except (UnicodeDecodeError, ValueError):
        raise _invalid_policy_entry() from None
    if (
        mode != b"100644"
        or object_type != b"blob"
        or raw_path != _POLICY_PATH.encode("ascii")
        or len(object_id) != width
        or _HEX_OBJECT_ID.fullmatch(object_id) is None
    ):
        raise _invalid_policy_entry()

    size_output = context.runner.run_text(
        ["cat-file", "-s", object_id], check=False, max_output_bytes=128
    )
    if size_output.returncode != 0 or size_output.stderr:
        raise _unavailable_policy_object()
    size_lines = size_output.stdout.splitlines()
    if (
        len(size_lines) != 1
        or size_output.stdout != f"{size_lines[0]}\n"
        or not size_lines[0].isdecimal()
    ):
        raise _unavailable_policy_object()
    size = int(size_lines[0])
    if size > context.limits.max_output_bytes:
        raise ManduaError(
            ErrorCode.LIMIT_EXCEEDED,
            "The committed repository policy exceeds the byte limit.",
        )

    contents = context.runner.run(
        ["cat-file", "blob", object_id],
        check=False,
        max_output_bytes=context.limits.max_output_bytes,
    )
    if contents.returncode != 0 or contents.stderr or len(contents.stdout) != size:
        raise _unavailable_policy_object()
    return parse_repository_policy(contents.stdout, context.limits)


def _unavailable_policy_object() -> ManduaError:
    return ManduaError(
        ErrorCode.POLICY_VIOLATION,
        "The committed repository policy object is unavailable.",
    )


def _invalid_policy_entry() -> ManduaError:
    return ManduaError(
        ErrorCode.POLICY_VIOLATION,
        "The committed repository policy entry is invalid.",
    )


def _parse_push_updates(value: str, width: int, limits: QueryLimits) -> tuple[_PushUpdate, ...]:
    updates: list[_PushUpdate] = []
    for raw_line in value.split("\n"):
        if raw_line == "":
            continue
        if len(updates) >= _MAX_PUSH_LINES:
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "The pre-push input has too many lines.")
        fields = raw_line.split()
        if len(fields) != 4:
            raise ManduaError(ErrorCode.VALIDATION_FAILED, "A pre-push line is malformed.")
        local_ref, local_oid, remote_ref, remote_oid = fields
        if any(len(field) > limits.max_input_chars for field in fields):
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "A pre-push field exceeds the input limit.")
        if local_ref not in {"(delete)", "HEAD"}:
            _validate_full_ref(local_ref)
        _validate_full_ref(remote_ref)
        _validate_full_oid(local_oid, width)
        _validate_full_oid(remote_oid, width)
        updates.append(_PushUpdate(local_ref, local_oid, remote_ref, remote_oid))
    return tuple(updates)


def _validate_full_ref(value: str) -> None:
    components = value.split("/")
    if (
        not value.startswith("refs/")
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in _REF_FORBIDDEN for character in value)
        or any(
            not component
            or component in {".", ".."}
            or component.startswith(".")
            or component.endswith((".", ".lock"))
            for component in components
        )
    ):
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "A pre-push ref is malformed.")


def _validate_full_oid(value: str, width: int) -> None:
    if len(value) != width or _HEX_OBJECT_ID.fullmatch(value) is None:
        raise ManduaError(ErrorCode.VALIDATION_FAILED, "A pre-push object ID is malformed.")


def _object_width(runner: GitRunner) -> int:
    output = runner.run_text(["rev-parse", "--show-object-format"], check=False)
    value = _one_git_line(output, "Git returned an invalid object format.")
    width = {"sha1": 40, "sha256": 64}.get(value)
    if width is None:
        raise ManduaError(ErrorCode.GIT_FAILURE, "Git returned an invalid object format.")
    return width


def _require_commit(runner: GitRunner, object_id: str) -> None:
    output = runner.run_text(["cat-file", "-t", object_id], check=False)
    if output.returncode != 0 or output.stderr or output.stdout != "commit\n":
        raise ManduaError(
            ErrorCode.POLICY_VIOLATION,
            "A committed repository policy source is unavailable or is not a commit.",
        )


def _one_git_line(output: GitOutput[str], message: str) -> str:
    lines = output.stdout.splitlines()
    if (
        output.returncode != 0
        or output.stderr
        or len(lines) != 1
        or output.stdout != f"{lines[0]}\n"
        or not lines[0]
    ):
        raise ManduaError(ErrorCode.GIT_FAILURE, message)
    return lines[0]


def _write_rejection(message: str) -> None:
    safe = message.encode("ascii", errors="replace").decode("ascii")[:_MAX_DIAGNOSTIC_CHARS]
    print(f"mandua: {safe}", file=sys.stderr)
