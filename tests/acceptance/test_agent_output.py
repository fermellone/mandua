"""The agent view must not turn a filtered lookup into proof of absence."""

import json

from mandua.cli import main


def test_decision_view_keeps_citations_without_claiming_a_content_search(repo, capsys):
    repo.write("loans.txt", "14 days\n")
    decision = repo.commit("Select short loans\n\nDecision-ID: LIB-1")
    repo.git("notes", "--ref=review", "add", "-m", "Committee approves the policy.")
    repo.write("measurements.csv", "month,loans\nJanuary,120\nFebruary,150\n")
    repo.commit("Record circulation measurements")
    before = repo.git("status", "--porcelain").stdout

    code = main(["--repo", str(repo.path), "decision", "LIB-1", "--format", "agent"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["view"] == "agent"
    assert payload["scope"]["repository_wide_absence_established"] is False
    assert payload["scope"]["empirical_validity_assessed"] is False
    assert "file contents" in payload["scope"]["not_searched"]
    assert any(e["oid"] == decision for e in payload["evidence"])
    assert any("Committee approves" in e.get("excerpt", "") for e in payload["evidence"])
    assert "changed_paths" not in captured.out
    assert "commit_count" not in captured.out
    assert "confidence" not in payload
    assert "measurements.csv" not in captured.out
    assert "evidence" not in payload["query_observations"][0]
    assert repo.git("status", "--porcelain").stdout == before

    # The stable diagnostic contract remains available, including actual measurements.
    assert main(["--repo", str(repo.path), "decision", "LIB-1", "--format", "json"]) == 0
    raw = json.loads(capsys.readouterr().out)
    assert raw["schema_version"] == "1.0"
    assert raw["history_scope"]["commit_count"] == 4
    assert raw["observed"][0]["evidence"] == []
    assert (repo.path / "measurements.csv").read_text().endswith("February,150\n")


def test_empty_decision_view_reports_a_lookup_gap_not_repository_wide_absence(repo, capsys):
    assert main(["--repo", str(repo.path), "decision", "UNKNOWN", "--format", "agent"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence"] == []
    assert payload["scope"]["limitations"]
    assert payload["scope"]["repository_wide_absence_established"] is False


def test_agent_view_error_preserves_nonzero_exit_and_recovery(tmp_path, capsys):
    code = main(["--repo", str(tmp_path / "missing"), "status", "--format", "agent"])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["code"]
    assert error["message"]
    assert "recovery" in error
