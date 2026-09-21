"""Offline checks of the portable skill; never invoke pi or a model."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/mandua/scripts/alternatives.py"
spec = importlib.util.spec_from_file_location("skill_alternatives", SCRIPT)
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def inspect(repo, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(repo.path), *args])
    helper.main()
    return json.loads(capsys.readouterr().out)


def test_unrelated_names_reasons_notes_and_targeted_comparison(repo, monkeypatch, capsys):
    repo.git("branch", "-m", "trunk")
    repo.write("dispatch.txt", "pending\n")
    repo.commit("Baseline")
    repo.checkout_new("blue")
    repo.write("dispatch.txt", "bicycle\n")
    blue = repo.commit("Bicycle option")
    repo.checkout("trunk")
    repo.checkout_new("orange")
    repo.write("dispatch.txt", "van\n")
    orange = repo.commit("Van option")
    repo.checkout("trunk")
    repo.git("merge", "--no-ff", "blue", "-m", "Choose bicycle\n\nReason: Narrow streets.")
    repo.git("notes", "--ref=review", "add", "-m", "Reviewed by dispatcher")
    before = repo.git("status", "--porcelain").stdout
    result = inspect(repo, monkeypatch, capsys)
    assert any(r["ref"] == "refs/heads/trunk" for r in result["refs"])
    assert any("Reason: Narrow streets." in c["message"] for c in result["commits"])
    assert any("dispatcher" in c.get("review", "") for c in result["commits"])
    for option in ("bicycle", "van"):
        assert any(option in c.get("patch", "") for c in result["commits"])
    pair = inspect(repo, monkeypatch, capsys, "--left", blue, "--right", orange)
    assert "commits" not in pair and "refs" not in pair
    assert pair["comparison"]["operation"] == "compare"
    assert "history_scope" in pair["comparison"]
    assert "bicycle" in pair["comparison_patch"] and "van" in pair["comparison_patch"]
    assert repo.git("status", "--porcelain").stdout == before


def test_limits_and_missing_evidence_are_explicit(repo, monkeypatch, capsys):
    repo.write("large.txt", "x" * 3000)
    repo.commit("Long message\n\n" + "y" * 2000)
    result = inspect(repo, monkeypatch, capsys, "--limit", "1")
    assert any(x.startswith("history:") for x in result["limits"])
    assert any(x.startswith("message:") for x in result["limits"])
    assert any(x.startswith("patch:") for x in result["limits"])
    assert result["scope"]["review_notes_available"] is False
    for n in range(34):
        repo.git("branch", f"option-{n}")
    result = inspect(repo, monkeypatch, capsys)
    assert len(result["refs"]) == 32
    assert any(x.startswith("refs:") for x in result["limits"])


def test_invalid_selection_and_missing_repository_fail(repo, monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit):
        inspect(repo, monkeypatch, capsys, "--left", "HEAD")
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path / "absent")])
    with pytest.raises(RuntimeError):
        helper.main()


def test_launchers_relocate_with_spaces_and_preserve_cwd(tmp_path):
    # A recording uv stub checks launcher routing, not pi or model behavior.
    checkout = tmp_path / "source checkout"
    scripts = checkout / "skills/mandua/scripts"
    shutil.copytree(SCRIPT.parent, scripts)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n', encoding="utf-8")
    stub.chmod(0o755)
    cwd = tmp_path / "separate playground"
    cwd.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    for name in ("mandua", "alternatives", "corrections"):
        result = subprocess.run(
            ["sh", str(scripts / name), "--help"],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        assert result[:6] == [
            str(cwd),
            "run",
            "--no-editable",
            "--locked",
            "--project",
            str(checkout),
        ]
        assert result[-1] == "--help"
        assert result[6] == ("mandua" if name == "mandua" else "python")
        if name != "mandua":
            assert result[7] == str(scripts / f"{name}.py")
