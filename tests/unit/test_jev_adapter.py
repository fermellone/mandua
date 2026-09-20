"""Tests for the TypeSafe AI Jev adapter harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.jev.cli import main as cli_main
from adapters.jev.client import (
    ChoiceQuestion,
    JevResponse,
    NoulQuestion,
    ScoreQuestion,
    TypeSafeJevClient,
)
from adapters.jev.harness import ManduaJevHarness, RouteDecision


def test_question_serialization() -> None:
    choice = ChoiceQuestion(options=("status", "why"), criteria="Select operation")
    assert choice.to_dict() == {
        "type": "choice",
        "options": ["status", "why"],
        "criteria": "Select operation",
    }

    noul = NoulQuestion(criteria="Is this a mutation?")
    assert noul.to_dict() == {"type": "noul", "criteria": "Is this a mutation?"}

    score = ScoreQuestion(min_val=0.0, max_val=10.0, criteria="Risk score")
    assert score.to_dict() == {
        "type": "score",
        "min": 0.0,
        "max": 10.0,
        "criteria": "Risk score",
    }


def test_mock_client_evaluation() -> None:
    client = TypeSafeJevClient(mock_mode=True)
    questions = {
        "op": ChoiceQuestion(options=("status", "why", "timeline")),
        "is_write": NoulQuestion(criteria="Is this a mutation or write?"),
        "risk": ScoreQuestion(min_val=0, max_val=10, criteria="Risk level"),
    }

    # Test "why" routing
    response = client.evaluate("Why did this value change in the rules file?", questions)
    assert isinstance(response, JevResponse)
    assert response.get_choice("op") == "why"
    assert response.get_confidence("op") >= 0.8
    assert response.get_noul("is_write") < 0.5
    assert response.get_score("risk") <= 5.0

    # Test "timeline" routing
    response = client.evaluate("Show me the timeline and history", questions)
    assert response.get_choice("op") == "timeline"

    # Test "status" routing
    response = client.evaluate("What is the current repository status and clean tree?", questions)
    assert response.get_choice("op") == "status"


def test_missing_question_key_raises_key_error() -> None:
    client = TypeSafeJevClient(mock_mode=True)
    response = client.evaluate("dummy state", {"op": ChoiceQuestion(options=("a", "b"))})
    with pytest.raises(KeyError):
        response.get_choice("non_existent")


def test_harness_routing_and_tiers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    client = TypeSafeJevClient(mock_mode=True)
    harness = ManduaJevHarness(repo_root, client=client)

    # Safe read operation -> AUTO_EXECUTE
    route = harness.route_intent("Check repository status")
    assert isinstance(route, RouteDecision)
    assert route.operation == "status"
    assert route.execution_tier == "AUTO_EXECUTE"
    assert not route.is_mutation

    # Mutation / write intent -> REQUIRE_CONFIRMATION or ESCALATE_TO_HUMAN
    mutation_route = harness.route_intent("Record a checkpoint commit with message 'fix: test'")
    assert mutation_route.operation == "checkpoint"
    assert mutation_route.is_mutation


def test_harness_status_execution() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    client = TypeSafeJevClient(mock_mode=True)
    harness = ManduaJevHarness(repo_root, client=client)

    result = harness.evaluate_and_run("Please inspect repository status")
    assert result.executed is True
    assert result.route.operation == "status"
    assert result.memory_result is not None
    assert result.memory_result.operation == "status"
    assert result.memory_result.confidence.value == "high"

    # Verify serialization
    data = result.to_dict()
    assert data["route"]["operation"] == "status"
    assert data["executed"] is True
    assert data["memory_result"]["schema_version"] == "1.0"


def test_cli_execution_human_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = Path(__file__).resolve().parents[2]

    # Test human format
    exit_code = cli_main(["--repo", str(repo_root), "Check status", "--mock"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "Jev Decision: operation='status'" in captured
    assert "Mandu'a Result (status):" in captured

    # Test JSON format
    exit_code = cli_main(["--repo", str(repo_root), "Check status", "--format", "json", "--mock"])
    assert exit_code == 0
    captured_json = capsys.readouterr().out
    data = json.loads(captured_json)
    assert data["route"]["operation"] == "status"
    assert data["memory_result"]["operation"] == "status"
