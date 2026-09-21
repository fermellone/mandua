"""Offline correction evidence checks on a repository unrelated to the demo."""

import importlib.util
from pathlib import Path

import pytest

from mandua.errors import ManduaError

SCRIPT = Path(__file__).resolve().parents[2] / "skills/mandua/scripts/corrections.py"
spec = importlib.util.spec_from_file_location("skill_corrections", SCRIPT)
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def record(repo, value, *trailers):
    repo.write("shipping window.txt", value)
    return repo.commit("Update dispatch window", trailers=("Decision-ID: SHIP-7", *trailers))


def test_correction_original_and_current_are_distinct_and_read_only(repo):
    wrong = record(repo, "90 minutes\n")
    fixed = record(repo, "30 minutes\n", f"Corrects: {wrong}")
    latest = record(repo, "45 minutes\n")
    repo.write("shipping window.txt", "uncommitted\n")
    before = repo.git("status", "--porcelain").stdout
    result = helper.inspect(repo.path, "SHIP-7", "shipping window.txt")
    assert result["current"]["oid"] == latest
    assert result["current"]["content"] == "45 minutes\n"
    snapshots = {r["oid"]: r for r in result["records"]}
    assert snapshots[wrong]["content"] == "90 minutes\n"
    assert snapshots[fixed]["content"] == "30 minutes\n"
    assert snapshots[fixed]["ancestor_of_current"] is True
    assert any(
        wrong in e["details"].get("verified_corrects", []) for e in result["decision"]["evidence"]
    )
    assert repo.git("status", "--porcelain").stdout == before
    assert repo.git("rev-parse", "HEAD").stdout.strip() == latest
    assert (
        helper.inspect(repo.path, "SHIP-7", "shipping window.txt", wrong)["current"]["content"]
        == "90 minutes\n"
    )


def test_unmerged_correction_is_not_current(repo):
    wrong = record(repo, "90\n")
    repo.checkout_new("proposal")
    fixed = record(repo, "30\n", f"Corrects: {wrong}")
    repo.checkout("main")
    result = helper.inspect(repo.path, "SHIP-7", "shipping window.txt")
    assert result["current"]["content"] == "90\n"
    assert next(r for r in result["records"] if r["oid"] == fixed)["ancestor_of_current"] is False


def test_invalid_link_and_missing_content_are_disclosed(repo):
    record(repo, "30\n", "Corrects: " + "f" * 40)
    result = helper.inspect(repo.path, "SHIP-7", "missing.txt")
    assert not result["current"]["available"]
    assert result["limits"]
    assert result["decision"]["gaps"] or result["decision"]["warnings"]
    assert not any(e["details"].get("verified_corrects") for e in result["decision"]["evidence"])
    with pytest.raises(ManduaError):
        helper.inspect(repo.path, "SHIP-7", "shipping window.txt", "not-a-revision")
    with pytest.raises(ManduaError):
        helper.inspect(repo.path, "SHIP-7", "../outside")


def test_clipping_and_missing_decision_are_explicit(repo):
    record(repo, "x" * 4000)
    result = helper.inspect(repo.path, "SHIP-7", "shipping window.txt")
    assert result["current"]["truncated"] is True
    assert len(result["current"]["content"]) == 3000
    assert result["limits"]
    absent = helper.inspect(repo.path, "UNKNOWN", "shipping window.txt")
    assert absent["decision"]["gaps"]
    assert absent["records"] == []


def test_snapshot_and_decision_result_limits_are_disclosed(repo):
    for n in range(13):
        record(repo, f"{n}\n")
    result = helper.inspect(repo.path, "SHIP-7", "shipping window.txt")
    assert len(result["records"]) == 12
    assert any("12 historical" in limit for limit in result["limits"])
    limited = helper.inspect(repo.path, "SHIP-7", "shipping window.txt", limit=1)
    assert limited["decision"]["warnings"]
