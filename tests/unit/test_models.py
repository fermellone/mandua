import json
from dataclasses import fields
from inspect import signature

from mandua.errors import ErrorCode, ManduaError
from mandua.models import Claim, Confidence, Evidence, MemoryResult, QueryLimits
from mandua.renderers import render_json


def test_memory_result_serializes_the_versioned_contract() -> None:
    result = MemoryResult(
        operation="why",
        answer="The rule was introduced by the baseline decision.",
        observed=(Claim("The line exists.", ("commit-1",)),),
        evidence=(Evidence("commit-1", "commit", oid="a" * 40),),
        confidence=Confidence.HIGH,
    )

    payload = json.loads(render_json(result))

    assert payload["schema_version"] == "1.0"
    assert payload["operation"] == "why"
    assert payload["observed"][0]["evidence"] == ["commit-1"]
    assert payload["evidence"][0]["oid"] == "a" * 40
    assert payload["inferred"] == []
    assert payload["gaps"] == []
    assert payload["applied"] is False


def test_mandua_error_serializes_the_stable_error_contract() -> None:
    error = ManduaError(
        ErrorCode.MISSING_OBJECT,
        "The requested object is unavailable.",
        evidence=(Evidence("object-1", "object", oid="b" * 40),),
        recovery="Fetch the missing object before retrying.",
    )

    payload = error.to_dict()

    assert payload == {
        "schema_version": "1.0",
        "code": "missing_object",
        "message": "The requested object is unavailable.",
        "evidence": [
            {
                "id": "object-1",
                "kind": "object",
                "oid": "b" * 40,
                "ref": None,
                "path": None,
                "line": None,
                "excerpt": None,
                "details": {},
            }
        ],
        "recovery": "Fetch the missing object before retrying.",
    }


def test_query_limits_preserves_the_approved_five_field_public_contract() -> None:
    """This fails if private Git-operation controls leak into the public model."""
    expected = (
        "max_commits",
        "max_output_bytes",
        "max_excerpt_chars",
        "max_input_chars",
        "timeout_seconds",
    )

    assert tuple(field.name for field in fields(QueryLimits)) == expected
    assert tuple(signature(QueryLimits).parameters) == expected
    assert QueryLimits() == QueryLimits()
    assert repr(QueryLimits()) == (
        "QueryLimits(max_commits=500, max_output_bytes=1048576, "
        "max_excerpt_chars=400, max_input_chars=4096, timeout_seconds=10.0)"
    )
