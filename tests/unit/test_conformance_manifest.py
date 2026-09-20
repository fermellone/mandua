"""Structural checks for the human-readable conformance ownership map."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFORMANCE = ROOT / "docs" / "conformance.md"
ACCEPTANCE = ROOT / "tests" / "acceptance" / "test_conformance.py"
ROW = re.compile(
    r"^\|\s*(?P<number>\d{2})\s*\|\s*(?P<guarantee>[^|]+?)\s*\|\s*"
    r"`(?P<node>[^`]+)`\s*\|\s*(?P<evidence>[^|]+?)\s*\|\s*"
    r"(?P<limit>[^|]+?)\s*\|$"
)


def _rows() -> list[dict[str, str]]:
    return [
        match.groupdict()
        for line in CONFORMANCE.read_text(encoding="utf-8").splitlines()
        if (match := ROW.fullmatch(line))
    ]


def _case_functions() -> set[str]:
    tree = ast.parse(ACCEPTANCE.read_text(encoding="utf-8"), filename=str(ACCEPTANCE))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_case_")
    }


def test_conformance_table_has_one_ordered_row_for_every_approved_case() -> None:
    rows = _rows()

    assert [row["number"] for row in rows] == [f"{number:02d}" for number in range(1, 19)]
    assert len({row["number"] for row in rows}) == 18
    assert all(row["guarantee"].strip() for row in rows)
    assert all(row["evidence"].strip() for row in rows)
    assert all(row["limit"].strip() for row in rows)


def test_each_table_node_names_one_existing_and_unique_case_function() -> None:
    rows = _rows()
    functions = _case_functions()
    prefix = "tests/acceptance/test_conformance.py::"
    mapped = [row["node"].removeprefix(prefix) for row in rows]

    assert all(row["node"].startswith(prefix) for row in rows)
    assert set(mapped) == functions
    assert len(mapped) == len(set(mapped)) == 18
