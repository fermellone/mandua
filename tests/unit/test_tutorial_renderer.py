"""Unit coverage for the manifest-driven executable tutorial."""

import shlex
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "demo" / "scenario.toml"
TUTORIAL_PATH = PROJECT_ROOT / "docs" / "tutorial.md"
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.render_tutorial as tutorial_renderer
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitRunner
from scripts.render_tutorial import (
    TutorialError,
    _closed_environment,
    _prepare_temporary_directory,
    _validate_runtime_command,
    load_documented_claim_ids,
    load_tutorial,
    main,
    render_tutorial,
)


def test_rendered_tutorial_is_current() -> None:
    """Catch a renderer or manifest change that leaves the checked-in tutorial stale."""
    expected = TUTORIAL_PATH.read_text(encoding="utf-8")
    actual = render_tutorial(SCENARIO_PATH)

    assert actual == expected


def test_renderer_uses_manifest_order_and_shell_safe_argv() -> None:
    """Catch a renderer that reorders commands or hand-rolls unsafe shell quoting."""
    tutorial = load_tutorial(SCENARIO_PATH)
    rendered = render_tutorial(SCENARIO_PATH)
    cursor = 0

    for step in tutorial.steps:
        for command in step.commands:
            line = f"$ {shlex.join(command.argv)}"
            position = rendered.find(line, cursor)
            assert position >= cursor
            cursor = position + len(line)


def test_renderer_documents_each_manifest_claim_exactly_once() -> None:
    """Catch a generated tutorial that hides, duplicates, or invents claim IDs."""
    tutorial = load_tutorial(SCENARIO_PATH)
    documented = load_documented_claim_ids(TUTORIAL_PATH)

    assert documented == tuple(tutorial.claim_ids.values())
    assert len(documented) == len(set(documented))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda source: source.replace("[tutorial]\n", '[tutorial]\nunknown = "field"\n', 1),
        lambda source: source.replace(
            'id = "initialize-output"', 'id = "initialize-repository"', 1
        ),
        lambda source: source.replace('argv = ["mkdir",', 'argv = ["curl",', 1),
        lambda source: source.replace('"{{FIXTURE_CONFIG}}"', '"prefix-{{FIXTURE_CONFIG}}"', 1),
        lambda source: source.replace('"{{FIXTURE_CONFIG}}"', '"{{UNKNOWN_FIXTURE}}"', 1),
    ),
)
def test_manifest_rejects_unknown_duplicate_or_unsafe_tutorial_data(
    tmp_path: Path, mutation
) -> None:
    """Catch permissive parsing that lets unknown commands or placeholders reach execution."""
    manifest = tmp_path / "scenario.toml"
    original = SCENARIO_PATH.read_text(encoding="utf-8")
    mutated = mutation(original)
    assert mutated != original
    manifest.write_text(mutated, encoding="utf-8")

    with pytest.raises(TutorialError):
        load_tutorial(manifest)


def test_check_mode_reports_staleness_without_writing(tmp_path: Path, capsys) -> None:
    """Catch check mode that repairs a stale file instead of failing read-only."""
    stale = tmp_path / "tutorial.md"
    stale.write_text("stale\n", encoding="utf-8")

    exit_code = main(
        [
            "--manifest",
            str(SCENARIO_PATH),
            "--output",
            str(stale),
            "--check",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert stale.read_text(encoding="utf-8") == "stale\n"
    assert captured.out == ""
    assert captured.err == (
        "The generated tutorial is stale; regenerate it with: "
        "uv run python scripts/render_tutorial.py --write\n"
    )


def test_write_then_check_round_trip_is_byte_stable(tmp_path: Path, capsys) -> None:
    """Catch write/check paths that do not compare the exact deterministic Markdown bytes."""
    generated = tmp_path / "tutorial.md"

    assert (
        main(
            [
                "--manifest",
                str(SCENARIO_PATH),
                "--output",
                str(generated),
                "--write",
            ]
        )
        == 0
    )
    first = generated.read_bytes()
    assert first == render_tutorial(SCENARIO_PATH).encode("utf-8")
    assert (
        main(
            [
                "--manifest",
                str(SCENARIO_PATH),
                "--output",
                str(generated),
                "--check",
            ]
        )
        == 0
    )
    assert generated.read_bytes() == first
    assert capsys.readouterr().err == ""


def test_closed_environment_controls_git_config_and_darwin_temp_directory(
    tmp_path: Path,
) -> None:
    """Catch ambient Git config or Darwin temp warnings that break tight output limits."""
    tutorial = load_tutorial(SCENARIO_PATH)
    executables = {
        "cp": "/bin/cp",
        "git": "/usr/bin/git",
        "mandua": "/tutorial/bin/mandua",
        "mkdir": "/bin/mkdir",
    }

    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    temporary_directory = _prepare_temporary_directory(output)
    environment = _closed_environment(
        tutorial,
        command_position=0,
        output_directory=output,
        temporary_directory=temporary_directory,
        executables=executables,
    )

    metadata = temporary_directory.lstat()
    completed = subprocess.run(
        [executables["git"], "--version"],
        cwd=output,
        env=environment,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )

    assert stat.S_ISDIR(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o700
    assert temporary_directory.parent == output
    assert environment["TMPDIR"] == str(temporary_directory)
    assert environment["GIT_CONFIG_COUNT"] == "10"
    assert environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert environment["GIT_ALLOW_PROTOCOL"] == "file"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert "confstr()" not in completed.stderr


def test_closed_environment_rejects_a_replaced_temp_directory(tmp_path: Path) -> None:
    """Catch a replaced temp path escaping the disposable tutorial output."""
    tutorial = load_tutorial(SCENARIO_PATH)
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    temporary_directory = _prepare_temporary_directory(output)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir(mode=0o700)
    temporary_directory.rmdir()
    temporary_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(TutorialError, match="temp directory is unsafe"):
        _closed_environment(
            tutorial,
            command_position=0,
            output_directory=output,
            temporary_directory=temporary_directory,
            executables={
                "cp": "/bin/cp",
                "git": "/usr/bin/git",
                "mandua": "/tutorial/bin/mandua",
                "mkdir": "/bin/mkdir",
            },
        )


def test_preexisting_output_requires_private_mode_and_current_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch reuse of a shared or foreign-owned output root as tutorial authority."""
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o755)

    with pytest.raises(TutorialError, match="private and owned"):
        tutorial_renderer._prepare_output(output)

    output.chmod(0o700)
    actual_owner = output.stat().st_uid
    monkeypatch.setattr(tutorial_renderer.os, "geteuid", lambda: actual_owner + 1)

    with pytest.raises(TutorialError, match="private and owned"):
        tutorial_renderer._prepare_output(output)


def test_output_authority_rejects_real_directory_substitution_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch same-mode output replacement by a different real directory."""
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    temporary = _prepare_temporary_directory(output)
    authority = tutorial_renderer._TutorialFilesystemAuthority.capture(output, temporary)
    displaced = tmp_path.resolve() / "displaced-output"
    launched: list[bool] = []
    marker = tmp_path.resolve() / "child-started-output"

    def record_launch(*args, **kwargs):
        launched.append(True)
        marker.write_text("started\n", encoding="utf-8")
        raise AssertionError("replaced output reached process launch")

    monkeypatch.setattr(tutorial_renderer.subprocess, "Popen", record_launch)
    output.rename(displaced)
    output.mkdir(mode=0o700)
    (output / ".tutorial-tmp").mkdir(mode=0o700)
    try:
        with pytest.raises(TutorialError, match="output authority changed"):
            tutorial_renderer._run_bounded_command(
                ("python", "-c", "pass"),
                executable=sys.executable,
                cwd=output,
                environment={},
                timeout_seconds=1,
                max_output_bytes=1024,
                filesystem_authority=authority,
            )
    finally:
        authority.close()

    assert launched == []
    assert not marker.exists()


def test_temp_authority_rejects_real_directory_substitution_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch same-mode temp replacement by a different real directory."""
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    temporary = _prepare_temporary_directory(output)
    authority = tutorial_renderer._TutorialFilesystemAuthority.capture(output, temporary)
    displaced = output / ".tutorial-tmp.displaced"
    launched: list[bool] = []
    marker = tmp_path.resolve() / "child-started-temp"

    def record_launch(*args, **kwargs):
        launched.append(True)
        marker.write_text("started\n", encoding="utf-8")
        raise AssertionError("replaced temp directory reached process launch")

    monkeypatch.setattr(tutorial_renderer.subprocess, "Popen", record_launch)
    temporary.rename(displaced)
    temporary.mkdir(mode=0o700)
    try:
        with pytest.raises(TutorialError, match="temp authority changed"):
            tutorial_renderer._run_bounded_command(
                ("python", "-c", "pass"),
                executable=sys.executable,
                cwd=output,
                environment={},
                timeout_seconds=1,
                max_output_bytes=1024,
                filesystem_authority=authority,
            )
    finally:
        authority.close()

    assert launched == []
    assert not marker.exists()


def test_internal_git_observer_revalidates_authority_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch claim verification launching hidden Git after filesystem authority loss."""
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    repository = output / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=repository,
        check=True,
        shell=False,
        capture_output=True,
    )
    temporary = _prepare_temporary_directory(output)
    authority = tutorial_renderer._TutorialFilesystemAuthority.capture(output, temporary)
    displaced = output / ".tutorial-tmp.displaced"
    launched: list[bool] = []
    marker = tmp_path.resolve() / "hidden-git-started"

    def record_launch(*args, **kwargs):
        launched.append(True)
        marker.write_text("started\n", encoding="utf-8")
        raise AssertionError("hidden Git reached process launch")

    temporary.rename(displaced)
    temporary.mkdir(mode=0o700)
    monkeypatch.setattr(tutorial_renderer.subprocess, "Popen", record_launch)
    try:
        with (
            tutorial_renderer._observe_tutorial_filesystem_authority(authority),
            pytest.raises(ManduaError) as caught,
        ):
            GitRunner(repository).run(["status", "--porcelain"])
    finally:
        authority.close()

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert "filesystem authority changed" in caught.value.message
    assert launched == []
    assert not marker.exists()


@pytest.mark.parametrize(
    "argv",
    (
        (
            "git",
            "clone",
            "--no-local",
            "--origin",
            "origin",
            "host:repository",
            "clone",
        ),
        ("git", "log", "--ext-diff", "--all"),
        ("git", "log", "--graph", "--oneline", "--decorate", "--all", "--textconv"),
        ("git", "cat-file", "--filters", "main:knowledge/example.md"),
        ("git", "-c", "credential.helper=!outside", "log", "--all"),
        ("git", "init", "--separate-git-dir=/tmp/outside", "."),
        ("git", "init", "--template=/tmp/outside", "."),
        ("git", "init", "--object-format=sha256", "."),
        ("git", "worktree", "add", "-b", "task/unsafe", "/tmp/outside", "main"),
        ("git", "branch", "--edit-description", "main"),
        ("git", "switch", "--recurse-submodules", "main"),
        ("git", "bundle", "create", "/tmp/outside", "--all"),
        ("git", "bundle", "create", "bundle", "--branches"),
        ("git", "notes", "--ref=refs/notes/review", "prune"),
        ("git", "show", "main"),
        ("git", "push", "remote.git", "main:refs/heads/main"),
        (
            "git",
            "fetch",
            "--no-tags",
            "remote.git",
            "refs/heads/main:refs/heads/main",
        ),
        (
            "git",
            "clone",
            "--no-local",
            "--origin",
            "origin",
            "remote.git",
            "/tmp/outside",
        ),
    ),
)
def test_runtime_rejects_external_transport_helpers_and_unsafe_git_shapes(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """Catch Git forms that could escape the file-only, no-helper tutorial boundary."""
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    (output / "remote.git").mkdir(mode=0o700)

    with pytest.raises(TutorialError):
        _validate_runtime_command(
            argv,
            cwd=output,
            output=output,
            fixture_root=SCENARIO_PATH.parent / "fixtures",
        )


def test_runtime_rejects_option_shaped_positional_operands_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch path operands that a child could reinterpret as command-line options."""
    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    fixture_root = tmp_path.resolve() / "fixtures"
    fixture_root.mkdir(mode=0o700)
    fixture = fixture_root / "source.txt"
    fixture.write_text("source\n", encoding="utf-8")
    option_fixture = fixture_root / "--preserve=all"
    option_fixture.write_text("option-shaped source\n", encoding="utf-8")
    monkeypatch.chdir(fixture_root)
    for option_repository in ("--receive-pack=helper", "--upload-pack=helper"):
        (output / option_repository).mkdir(mode=0o700)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    launches: list[tuple[object, ...]] = []

    def record_launch(*args, **kwargs):
        launches.append(args)
        raise AssertionError("validation reached process launch")

    monkeypatch.setattr(tutorial_renderer.subprocess, "Popen", record_launch)
    unsafe_commands = (
        ("mkdir", "-p", "--mode=000"),
        ("cp", option_fixture.name, "copied.txt"),
        ("cp", str(fixture), f"--target-directory={outside}"),
        (
            "git",
            "init",
            "--bare",
            "--initial-branch=main",
            f"--separate-git-dir={outside}",
        ),
        ("git", "worktree", "add", "-b", "task/unsafe", "--force", "main"),
        ("git", "bundle", "create", "--quiet", "--all"),
        ("git", "rm", "--cached"),
        ("git", "push", "--receive-pack=helper", "refs/heads/main:refs/heads/main"),
        (
            "git",
            "fetch",
            "--no-tags",
            "--receive-pack=helper",
            "refs/notes/review:refs/notes/review",
        ),
        (
            "git",
            "clone",
            "--no-local",
            "--origin",
            "origin",
            "--upload-pack=helper",
            "clone",
        ),
        (
            "git",
            "clone",
            "--no-local",
            "--origin",
            "origin",
            str(output / "--upload-pack=helper"),
            f"--separate-git-dir={outside}",
        ),
    )

    for argv in unsafe_commands:
        with pytest.raises(TutorialError, match="positional operand is option-shaped"):
            _validate_runtime_command(
                argv,
                cwd=output,
                output=output,
                fixture_root=fixture_root,
            )

    assert launches == []
    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert list(outside.iterdir()) == [sentinel]


def test_runner_rejects_an_option_shaped_command_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch orchestration that launches a child before positional validation completes."""
    tutorial = load_tutorial(SCENARIO_PATH)
    first_step = tutorial.steps[0]
    unsafe_command = replace(
        first_step.commands[0],
        argv=("mkdir", "-p", "--mode=000"),
    )
    unsafe_step = replace(
        first_step,
        commands=(unsafe_command, *first_step.commands[1:]),
    )
    unsafe_tutorial = replace(tutorial, steps=(unsafe_step, *tutorial.steps[1:]))
    monkeypatch.setattr(tutorial_renderer, "load_tutorial", lambda _path: unsafe_tutorial)
    launched: list[bool] = []
    marker = tmp_path.resolve() / "child-started"

    def record_launch(*args, **kwargs):
        launched.append(True)
        marker.write_text("started\n", encoding="utf-8")
        raise AssertionError("unsafe command reached process launch")

    monkeypatch.setattr(tutorial_renderer.subprocess, "Popen", record_launch)

    with pytest.raises(TutorialError, match="positional operand is option-shaped"):
        tutorial_renderer.run_tutorial(SCENARIO_PATH, tmp_path / "tutorial")

    assert launched == []
    assert not marker.exists()


def test_git_allowlist_matches_visible_grammar_and_future_entries_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch an allowed Git subcommand silently falling through without a grammar."""
    tutorial = load_tutorial(SCENARIO_PATH)
    visible_subcommands = {
        command.argv[1]
        for step in tutorial.steps
        for command in step.commands
        if command.argv[0] == "git"
    }
    assert visible_subcommands == tutorial_renderer._ALLOWED_GIT_SUBCOMMANDS
    assert tutorial_renderer._ALLOWED_GIT_SUBCOMMANDS == (
        tutorial_renderer._LOCAL_GIT_SUBCOMMANDS | tutorial_renderer._TRANSPORT_GIT_SUBCOMMANDS
    )

    output = tmp_path.resolve() / "tutorial-output"
    output.mkdir(mode=0o700)
    monkeypatch.setattr(
        tutorial_renderer,
        "_ALLOWED_GIT_SUBCOMMANDS",
        tutorial_renderer._ALLOWED_GIT_SUBCOMMANDS | {"status"},
    )
    monkeypatch.setattr(
        tutorial_renderer,
        "_LOCAL_GIT_SUBCOMMANDS",
        tutorial_renderer._LOCAL_GIT_SUBCOMMANDS | {"status"},
    )

    with pytest.raises(TutorialError, match="no executable grammar"):
        _validate_runtime_command(
            ("git", "status"),
            cwd=output,
            output=output,
            fixture_root=SCENARIO_PATH.parent / "fixtures",
        )
