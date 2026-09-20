"""Render stable Mandu'a result and error objects."""

import json
from typing import Any

from mandua.errors import ManduaError
from mandua.models import MemoryResult


def render_json(value: MemoryResult | ManduaError) -> str:
    """Render a result or error as stable JSON."""
    return json.dumps(value.to_dict(), ensure_ascii=False, sort_keys=True)


def render_human(value: MemoryResult | ManduaError) -> str:
    """Render a result or error as readable text from its machine representation."""
    payload = value.to_dict()
    if isinstance(value, ManduaError):
        return "\n\n".join(
            (
                _render_section("Error", payload["message"]),
                _render_section("Code", payload["code"]),
                _render_section("Evidence", payload["evidence"]),
                _render_section("Recovery", payload["recovery"]),
            )
        )

    return "\n\n".join(
        (
            _render_section("Operation", payload["operation"]),
            _render_section("Answer", payload["answer"]),
            _render_section("Observed", payload["observed"]),
            _render_section("Inferred", payload["inferred"]),
            _render_section("Evidence", payload["evidence"]),
            _render_section("History Scope", _render_history_scope(payload["history_scope"])),
            _render_section("Confidence", payload["confidence"]),
            _render_section("Gaps", payload["gaps"]),
            _render_section("Warnings", payload["warnings"]),
            _render_section("Changes", _render_changes(payload["changes"])),
            _render_section("Applied", _render_boolean(payload["applied"])),
        )
    )


def _render_section(title: str, value: Any) -> str:
    if isinstance(value, (list, tuple)):
        body = "\n".join(f"- {_render_value(item)}" for item in value) or "- None"
    elif value is None:
        body = "None"
    else:
        body = _render_value(value)
    return f"{title}:\n{body}"


def _render_value(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _render_history_scope(value: dict[str, Any]) -> str:
    return "\n".join(
        (
            f"Start OID: {_render_value(value['start_oid'])}",
            f"End OID: {_render_value(value['end_oid'])}",
            _render_named_items("Refs", value["refs"]),
            f"Commit Count: {_render_value(value['commit_count'])}",
            f"Truncated: {_render_boolean(value['truncated'])}",
            f"Shallow: {_render_boolean(value['shallow'])}",
            f"Notes Available: {_render_boolean(value['notes_available'])}",
            _render_named_items("Missing Objects", value["missing_objects"]),
        )
    )


def _render_named_items(title: str, values: list[Any] | tuple[Any, ...]) -> str:
    body = "\n".join(f"- {_render_value(item)}" for item in values) or "- None"
    return f"{title}:\n{body}"


def _render_changes(values: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    if not values:
        return "- None"
    return "\n".join(
        "\n".join(
            (
                f"- Action: {_render_value(value['action'])}",
                f"  Target: {_render_value(value['target'])}",
                f"  Before OID: {_render_value(value['before_oid'])}",
                f"  After OID: {_render_value(value['after_oid'])}",
            )
        )
        for value in values
    )


def _render_boolean(value: bool) -> str:
    return "Yes" if value else "No"
