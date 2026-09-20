"""Shared test fixtures."""

from pathlib import Path

import pytest
from helpers.repo import RepoBuilder


@pytest.fixture
def repo(tmp_path: Path) -> RepoBuilder:
    """Return a new repository with a deterministic main commit."""
    return RepoBuilder.create(tmp_path / "repository")
