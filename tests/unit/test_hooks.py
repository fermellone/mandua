"""Unit tests for the versioned Git hook boundary."""

from __future__ import annotations

import argparse
import importlib
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mandua import cli

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WRAPPERS = {
    "pre-commit": b'#!/bin/sh\nexec mandua policy-hook pre-commit "$@"\n',
    "commit-msg": b'#!/bin/sh\nexec mandua policy-hook commit-msg "$@"\n',
    "pre-rebase": b'#!/bin/sh\nexec mandua policy-hook pre-rebase "$@"\n',
    "pre-push": b'#!/bin/sh\nexec mandua policy-hook pre-push "$@"\n',
}
_VALID_MESSAGE = (
    "Record the shared hook policy\n\n"
    "Memory-Type: implementation\n"
    "Scope: hooks\n"
    "Task-ID: POC-15\n"
    "Agent-ID: codex\n"
)


def _hooks():
    return importlib.import_module("mandua.hooks")


def _message_path(repo, contents: str = _VALID_MESSAGE) -> Path:
    git_directory = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    path = git_directory / "COMMIT_EDITMSG"
    path.write_text(contents, encoding="utf-8")
    return path


def test_versioned_hook_wrappers_are_exact_tiny_posix_executables() -> None:
    """This fails if a wrapper duplicates policy, changes bytes, or loses execution mode."""
    for name, expected in _WRAPPERS.items():
        path = _PROJECT_ROOT / ".githooks" / name
        assert path.read_bytes() == expected
        assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_versioned_hook_wrappers_have_tracked_executable_modes() -> None:
    """This fails if checkout metadata cannot preserve executable hooks."""
    output = subprocess.run(
        ["git", "ls-files", "--stage", "--", ".githooks"],
        cwd=_PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout

    records = [line for line in output.splitlines() if line]
    assert len(records) == 4
    assert all(line.startswith("100755 ") for line in records)


@pytest.mark.parametrize(
    ("name", "arguments", "stdin"),
    (
        ("unknown", (), ""),
        ("pre-commit", ("unexpected",), ""),
        ("pre-commit", (), "unexpected input"),
        ("commit-msg", (), ""),
        ("commit-msg", ("one", "two"), ""),
        ("pre-rebase", (), ""),
        ("pre-rebase", ("one", "two", "three"), ""),
        ("pre-push", ("origin",), ""),
        ("pre-push", ("origin", "url", "extra"), ""),
    ),
)
def test_run_hook_rejects_invalid_hook_boundaries_without_tracebacks(
    repo, capsys, name: str, arguments: tuple[str, ...], stdin: str
) -> None:
    """This fails if malformed hook input escapes the one-bit safe process contract."""
    code = _hooks().run_hook(repo.path, name, arguments, stdin)
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert captured.err
    assert captured.err.isascii()
    assert len(captured.err.encode("utf-8")) <= 512
    assert "Traceback" not in captured.err


def test_run_hook_does_not_echo_untrusted_oversized_input(repo, capsys) -> None:
    """This fails if bounded diagnostics disclose attacker-controlled hook input."""
    marker = "private-marker-" + "x" * 1_100_000

    code = _hooks().run_hook(repo.path, "pre-push", ("origin", "unused"), marker)
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "private-marker" not in captured.err
    assert len(captured.err.encode("utf-8")) <= 512


def test_commit_msg_rejects_a_file_replaced_during_the_bounded_read(
    repo, monkeypatch, capsys
) -> None:
    """This fails if an opened message can be substituted before final identity validation."""
    hooks = _hooks()
    git_runner = importlib.import_module("mandua.git_runner")
    message = _message_path(repo)
    replacement = message.with_name("replacement-message")
    target = message.stat()
    original_read = git_runner.os.read
    replaced = False

    def replace_during_read(descriptor: int, maximum: int) -> bytes:
        nonlocal replaced
        contents = original_read(descriptor, maximum)
        metadata = os.fstat(descriptor)
        if (
            not replaced
            and contents
            and metadata.st_dev == target.st_dev
            and metadata.st_ino == target.st_ino
        ):
            replaced = True
            replacement.write_text(_VALID_MESSAGE, encoding="utf-8")
            os.replace(replacement, message)
        return contents

    monkeypatch.setattr(git_runner.os, "read", replace_during_read)

    assert hooks.run_hook(repo.path, "commit-msg", (str(message),), "") == 1
    captured = capsys.readouterr()
    assert "changed" in captured.err.lower()
    assert "Traceback" not in captured.err


def test_hidden_cli_forwards_typed_cwd_argv_and_empty_non_push_stdin(
    repo, monkeypatch, capsys
) -> None:
    """This fails if hidden dispatch exposes argparse internals or reads pre-rebase stdin."""
    calls: list[tuple[Path, str, tuple[str, ...], str]] = []

    def fake_run_hook(repository: Path, name: str, arguments: tuple[str, ...], stdin: str) -> int:
        calls.append((repository, name, arguments, stdin))
        assert not isinstance(arguments, argparse.Namespace)
        return 0

    class UnreadableInput(io.StringIO):
        def read(self, *args, **kwargs):
            raise AssertionError("non-push hooks must not read stdin")

    monkeypatch.chdir(repo.path)
    monkeypatch.setattr(cli, "run_hook", fake_run_hook, raising=False)
    monkeypatch.setattr(sys, "stdin", UnreadableInput("ignored"))

    assert cli.main(["policy-hook", "pre-rebase", "HEAD~1"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert calls == [(repo.path, "pre-rebase", ("HEAD~1",), "")]


def test_hidden_cli_forwards_bounded_pre_push_stdin(repo, monkeypatch, capsys) -> None:
    """This fails if pre-push loses stdin records or forwards an untyped parser object."""
    calls: list[tuple[Path, str, tuple[str, ...], str]] = []
    line = "refs/heads/topic " + "1" * 40 + " refs/heads/topic " + "0" * 40 + "\n"

    def fake_run_hook(repository: Path, name: str, arguments: tuple[str, ...], stdin: str) -> int:
        calls.append((repository, name, arguments, stdin))
        return 0

    monkeypatch.chdir(repo.path)
    monkeypatch.setattr(cli, "run_hook", fake_run_hook, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))

    assert cli.main(["policy-hook", "pre-push", "origin", "unused"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert calls == [(repo.path, "pre-push", ("origin", "unused"), line)]
