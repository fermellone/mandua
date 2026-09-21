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
    assert result["scope"]["empirical_validity_assessed"] is False
    assert result["scope"]["repository_wide_absence_established"] is False
    for option in ("bicycle", "van"):
        assert any(option in c.get("patch", "") for c in result["commits"])
    pair = inspect(repo, monkeypatch, capsys, "--left", blue, "--right", orange)
    assert "commits" not in pair and "refs" not in pair
    assert pair["comparison"]["operation"] == "compare"
    assert pair["comparison"]["view"] == "agent"
    assert "scope" in pair["comparison"]
    assert "bicycle" in pair["comparison_patch"] and "van" in pair["comparison_patch"]
    raw = inspect(repo, monkeypatch, capsys, "--left", blue, "--right", orange, "--format", "json")
    assert "history_scope" in raw["comparison"]
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


@pytest.fixture(scope="module")
def prepared_checkout(tmp_path_factory):
    checkout = tmp_path_factory.mktemp("launchers") / "source checkout"
    checkout.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, checkout / name)
    for name in ("src", "demo", "adapters", "skills"):
        shutil.copytree(ROOT / name, checkout / name, ignore=shutil.ignore_patterns("__pycache__"))
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UV_") and key not in {"VIRTUAL_ENV", "PYTHONPATH"}
    }
    if "UV_CACHE_DIR" in os.environ:
        env["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    subprocess.run(
        [
            "uv",
            "sync",
            "--offline",
            "--locked",
            "--no-editable",
            "--no-dev",
            "--python",
            sys.executable,
            "--project",
            str(checkout),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    # A changed checkout must not trigger a build while serving an installed version.
    project = checkout / "pyproject.toml"
    project.write_text(
        project.read_text().replace("hatchling>=1.27,<2", "mandua-missing-build-tool==0"),
        encoding="utf-8",
    )
    blocked_cache = checkout / "not a cache directory"
    blocked_cache.write_text("cache access must not be needed", encoding="utf-8")
    env.update(UV_OFFLINE="1", UV_CACHE_DIR=str(blocked_cache), PYTHONDONTWRITEBYTECODE="1")
    return checkout, env


@pytest.mark.parametrize("launcher", ("mandua", "alternatives", "corrections"))
def test_prepared_launchers_query_offline_without_cache_or_rebuild(
    prepared_checkout, repo, launcher
):
    checkout, env = prepared_checkout
    repo.write("rules.txt", "14 days\n")
    oid = repo.commit("Loan policy\n\nDecision-ID: DEC-LOAN-001")
    args = {
        "mandua": ["--repo", ".", "timeline", "--limit", "1", "--format", "agent"],
        "alternatives": ["."],
        "corrections": [".", "--decision", "DEC-LOAN-001", "--path", "rules.txt"],
    }
    before = repo.git("status", "--porcelain").stdout
    installed = checkout / ".venv"
    snapshot = {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in installed.rglob("*")
        if p.is_file()
    }
    result = subprocess.run(
        ["sh", str(checkout / "skills/mandua/scripts" / launcher), *args[launcher]],
        cwd=repo.path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    if launcher == "mandua":
        assert payload["operation"] == "timeline"
        assert payload["view"] == "agent"
        assert oid in result.stdout
    elif launcher == "alternatives":
        assert payload["head"] == oid
    else:
        assert payload["current"]["oid"] == oid
        assert payload["current"]["content"] == "14 days\n"
        assert payload["decision"]["view"] == "agent"
    assert repo.git("status", "--porcelain").stdout == before
    assert snapshot == {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in installed.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("launcher", ("mandua", "alternatives", "corrections"))
def test_unprepared_launchers_explain_setup_without_creating_environment(tmp_path, launcher):
    scripts = tmp_path / "skills/mandua/scripts"
    shutil.copytree(SCRIPT.parent, scripts)
    result = subprocess.run(
        ["sh", str(scripts / launcher), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "uv sync --locked --no-editable" in result.stderr
    assert not (tmp_path / ".venv").exists()
