"""Real-repository acceptance tests for the Mandu'a command-line interface."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mandua.cli import main


def _metadata_arguments(*, memory_type: str = "decision") -> list[str]:
    return [
        "--message",
        "Record the tested transition",
        "--reason",
        "The repository evidence supports this transition.",
        "--memory-type",
        memory_type,
        "--scope",
        "irrigation",
        "--task-id",
        "TASK-CLI-14",
        "--decision-id",
        "DEC-CLI-14",
        "--agent-id",
        "gardener",
        "--trailer",
        "Review-State=pending",
    ]


def _run_json(repo, capsys, *arguments: str) -> tuple[int, dict[str, object]]:
    code = main(["--repo", str(repo.path), *arguments, "--format", "json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_cli_status_json_uses_only_the_public_contract(repo, capsys) -> None:
    """This fails if JSON output bypasses the stable public MemoryResult representation."""
    code, payload = _run_json(repo, capsys, "status")

    assert code == 0
    assert payload["schema_version"] == "1.0"
    assert payload["operation"] == "status"


def test_cli_executes_all_fourteen_public_subcommands(repo, tmp_path, capsys) -> None:
    """This fails if any advertised command is disconnected from its real operation."""
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    baseline = repo.commit(
        "Record the irrigation baseline\n\n"
        "Reason: Dawn reduces evaporation.\n\n"
        "Memory-Type: decision\n"
        "Scope: irrigation\n"
        "Task-ID: TASK-CLI-14\n"
        "Decision-ID: DEC-CLI-14\n"
        "Agent-ID: gardener"
    )
    repo.checkout_new("hypothesis/sensor")
    repo.write("knowledge/sensor.md", "Moisture threshold: 35%\n")
    repo.commit("Record the sensor hypothesis")
    repo.checkout("main")

    read_and_independent_write_commands = (
        ("status", ["status"]),
        ("context", ["context", "--task-id", "TASK-CLI-14"]),
        ("timeline", ["timeline", "--path", "knowledge/rules.md"]),
        ("why", ["why", "--path", "knowledge/rules.md", "--line", "1"]),
        ("origin", ["origin", "--text", "Water at dawn."]),
        ("evolution", ["evolution", "--path", "knowledge/rules.md"]),
        ("compare", ["compare", "main", "hypothesis/sensor"]),
        ("decision", ["decision", "DEC-CLI-14"]),
        ("recover", ["recover", "--query", baseline]),
        (
            "annotate",
            [
                "annotate",
                baseline,
                "--message",
                "Review confirms the baseline.",
                "--agent-id",
                "reviewer",
            ],
        ),
        (
            "integrate",
            ["integrate", "hypothesis/sensor", *_metadata_arguments(memory_type="integration")],
        ),
    )

    for expected_operation, arguments in read_and_independent_write_commands:
        code, payload = _run_json(repo, capsys, *arguments)
        assert code == 0
        assert payload["operation"] == expected_operation
        assert payload["schema_version"] == "1.0"

    repo.write("knowledge/rules.md", "Water only below 35% moisture.\n")
    content_write_commands = (
        (
            "checkpoint",
            ["checkpoint", "--path", "knowledge/rules.md", *_metadata_arguments()],
        ),
        (
            "correct",
            ["correct", baseline, "--path", "knowledge/rules.md", *_metadata_arguments()],
        ),
    )
    head_before = repo.git("rev-parse", "HEAD").stdout.strip()

    for expected_operation, arguments in content_write_commands:
        code, payload = _run_json(repo, capsys, *arguments)
        assert code == 0
        assert payload["operation"] == expected_operation
        assert payload["applied"] is False
        assert repo.git("rev-parse", "HEAD").stdout.strip() == head_before

    demo_output = tmp_path / "demo-output"
    demo_code = main(["demo", "--output", str(demo_output), "--format", "json"])
    demo_capture = capsys.readouterr()
    demo_payload = json.loads(demo_capture.out)

    assert demo_code == 0
    assert demo_capture.err == ""
    assert demo_payload["schema_version"] == "1.0"
    assert demo_payload["repository_path"] == str(demo_output.resolve() / "repository")


def test_cli_human_and_json_render_the_same_real_read_and_write_results(repo, capsys) -> None:
    """This fails if either format uses a separate service payload or mutation path."""
    human_code = main(["--repo", str(repo.path), "status"])
    human_status = capsys.readouterr()
    json_code, json_status = _run_json(repo, capsys, "status")
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    checkpoint = [
        "checkpoint",
        "--path",
        "knowledge/rules.md",
        *_metadata_arguments(),
    ]
    human_write_code = main(["--repo", str(repo.path), *checkpoint])
    human_write = capsys.readouterr()
    json_write_code, json_write = _run_json(repo, capsys, *checkpoint)

    assert human_code == json_code == 0
    assert "Answer:" in human_status.out
    assert json_status["operation"] == "status"
    assert human_status.err == ""
    assert human_write_code == json_write_code == 0
    assert "Answer:" in human_write.out
    assert json_write["operation"] == "checkpoint"
    assert json_write["applied"] is False
    assert human_write.err == ""


def test_cli_checkpoint_does_not_apply_without_apply_and_commits_with_apply(repo, capsys) -> None:
    """This fails if checkpoint preview mutates or --apply does not commit the exact path."""
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    before = repo.git("rev-parse", "HEAD").stdout.strip()
    command = [
        "checkpoint",
        "--path",
        "knowledge/rules.md",
        *_metadata_arguments(),
    ]

    preview_code, preview = _run_json(repo, capsys, *command)

    assert preview_code == 0
    assert preview["applied"] is False
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before

    applied_code, applied = _run_json(repo, capsys, *command, "--apply")

    assert applied_code == 0
    assert applied["applied"] is True
    assert repo.git("rev-parse", "HEAD").stdout.strip() != before
    assert repo.git("show", "HEAD:knowledge/rules.md").stdout == "Moisture threshold: 35%\n"


def test_cli_recover_previews_then_creates_only_the_requested_branch(repo, capsys) -> None:
    """This fails if recover creates a branch before --apply or ignores the requested name."""
    repo.checkout_new("task/lost")
    repo.write("knowledge/lost.md", "Recoverable observation\n")
    lost = repo.commit("Record a recoverable observation")
    repo.checkout("main")
    repo.git("branch", "-D", "task/lost")
    command = ["recover", "--query", lost, "--create-branch", "recovery/lost"]

    preview_code, preview = _run_json(repo, capsys, *command)

    assert preview_code == 0
    assert preview["applied"] is False
    assert repo.git("show-ref", "--verify", "refs/heads/recovery/lost", check=False).returncode != 0

    applied_code, applied = _run_json(repo, capsys, *command, "--apply")

    assert applied_code == 0
    assert applied["applied"] is True
    assert repo.git("rev-parse", "recovery/lost").stdout.strip() == lost


def test_cli_annotate_previews_then_updates_only_notes_with_apply(repo, capsys) -> None:
    """This fails if annotation preview writes notes or apply changes repository content."""
    target = repo.git("rev-parse", "HEAD").stdout.strip()
    command = [
        "annotate",
        target,
        "--message",
        "Review confirms the recorded baseline.",
        "--agent-id",
        "reviewer",
    ]

    preview_code, preview = _run_json(repo, capsys, *command)

    assert preview_code == 0
    assert preview["applied"] is False
    assert repo.git("notes", "--ref=refs/notes/review", "show", target, check=False).returncode != 0

    applied_code, applied = _run_json(repo, capsys, *command, "--apply")

    assert applied_code == 0
    assert applied["applied"] is True
    assert (
        repo.git("notes", "--ref=refs/notes/review", "show", target).stdout
        == "Review confirms the recorded baseline.\n\nAgent-ID: reviewer\n"
    )


def test_cli_integrate_previews_then_creates_a_two_parent_commit_with_apply(repo, capsys) -> None:
    """This fails if integration preview moves main or apply bypasses integration semantics."""
    before = repo.git("rev-parse", "main").stdout.strip()
    repo.checkout_new("hypothesis/sensor")
    repo.write("knowledge/sensor.md", "Moisture threshold: 35%\n")
    source = repo.commit("Record the sensor hypothesis")
    repo.checkout("main")
    command = [
        "integrate",
        "hypothesis/sensor",
        *_metadata_arguments(memory_type="integration"),
    ]

    preview_code, preview = _run_json(repo, capsys, *command)

    assert preview_code == 0
    assert preview["applied"] is False
    assert repo.git("rev-parse", "main").stdout.strip() == before

    applied_code, applied = _run_json(repo, capsys, *command, "--apply")
    commit = repo.git("rev-parse", "main").stdout.strip()

    assert applied_code == 0
    assert applied["applied"] is True
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [before, source]


def test_cli_integrate_defaults_to_the_configured_trunk_branch_for_preview_and_apply(
    repo, capsys
) -> None:
    """This fails if an omitted CLI target is converted to main before policy is loaded."""
    repo.git("branch", "-m", "trunk")
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "trunk"\n')
    before = repo.commit("Configure trunk as canonical")
    repo.checkout_new("hypothesis/trunk-sensor", before)
    repo.write("knowledge/trunk-sensor.md", "Moisture threshold: 35%\n")
    source = repo.commit("Record the trunk sensor hypothesis")
    repo.checkout("trunk")
    command = [
        "integrate",
        "hypothesis/trunk-sensor",
        *_metadata_arguments(memory_type="integration"),
    ]

    preview_code, preview = _run_json(repo, capsys, *command)

    assert preview_code == 0
    assert preview["applied"] is False
    assert preview["changes"][0]["target"] == "refs/heads/trunk"
    assert repo.git("show-ref", "--verify", "refs/heads/main", check=False).returncode != 0
    assert repo.git("rev-parse", "trunk").stdout.strip() == before

    applied_code, applied = _run_json(repo, capsys, *command, "--apply")
    commit = repo.git("rev-parse", "trunk").stdout.strip()

    assert applied_code == 0
    assert applied["applied"] is True
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [before, source]
    assert repo.git("show-ref", "--verify", "refs/heads/main", check=False).returncode != 0


def test_cli_correct_previews_then_appends_a_correction_with_apply(repo, capsys) -> None:
    """This fails if correction preview rewrites history or apply omits the preserved error link."""
    repo.write("knowledge/rules.md", "Moisture threshold: 80%\n")
    incorrect = repo.commit(
        "Record the incorrect threshold\n\n"
        "Memory-Type: decision\n"
        "Scope: irrigation\n"
        "Decision-ID: DEC-CLI-14\n"
        "Agent-ID: gardener"
    )
    repo.write("knowledge/rules.md", "Moisture threshold: 35%\n")
    command = [
        "correct",
        incorrect,
        "--path",
        "knowledge/rules.md",
        *_metadata_arguments(),
    ]

    preview_code, preview = _run_json(repo, capsys, *command)

    assert preview_code == 0
    assert preview["applied"] is False
    assert repo.git("rev-parse", "HEAD").stdout.strip() == incorrect

    applied_code, applied = _run_json(repo, capsys, *command, "--apply")
    message = repo.git("show", "-s", "--format=%B", "HEAD").stdout

    assert applied_code == 0
    assert applied["applied"] is True
    assert repo.git("merge-base", "--is-ancestor", incorrect, "HEAD").returncode == 0
    assert f"Corrects: {incorrect}\n" in message


def test_cli_real_repository_error_uses_json_stderr_and_exit_two(tmp_path, capsys) -> None:
    """This fails if a real repository validation error uses stdout or an unstable payload."""
    invalid_repository = tmp_path / "not-a-repository"
    invalid_repository.mkdir()

    code = main(["--repo", str(invalid_repository), "status", "--format", "json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert code == 2
    assert captured.out == ""
    assert payload["schema_version"] == "1.0"
    assert payload["code"] == "invalid_repository"
    assert "Traceback" not in captured.err


def test_console_script_and_module_help_are_identical_and_list_every_command() -> None:
    """This fails if packaging and python -m expose different English command surfaces."""
    repository = Path(__file__).resolve().parents[2]
    console = subprocess.run(
        ["uv", "run", "mandua", "--help"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    module = subprocess.run(
        ["uv", "run", "python", "-m", "mandua", "--help"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert console.returncode == module.returncode == 0
    assert console.stderr == module.stderr == ""
    assert console.stdout == module.stdout
    for command in (
        "status",
        "context",
        "timeline",
        "why",
        "origin",
        "evolution",
        "compare",
        "decision",
        "recover",
        "checkpoint",
        "annotate",
        "integrate",
        "correct",
        "demo",
    ):
        assert command in console.stdout
