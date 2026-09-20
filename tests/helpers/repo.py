"""Disposable Git repository fixtures for acceptance tests."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mandua.git_runner import GitRunner


@dataclass
class RepoBuilder:
    """Build a deterministic, disposable Git repository."""

    path: Path
    _root: Path
    _next_timestamp: datetime = field(
        default_factory=lambda: datetime(2020, 1, 1, tzinfo=UTC), repr=False
    )

    @classmethod
    def create(cls, path: Path) -> RepoBuilder:
        path.mkdir(parents=True)
        builder = cls(path=path.resolve(), _root=path.parent.resolve())
        builder.git("init", "--initial-branch=main")
        builder._configure_fixture_repository()
        builder._commit("Create an empty initial commit", allow_empty=True)
        return builder

    @classmethod
    def create_unborn(cls, path: Path) -> RepoBuilder:
        """Create a configured repository whose HEAD has no commit yet."""
        path.mkdir(parents=True)
        builder = cls(path=path.resolve(), _root=path.parent.resolve())
        builder.git("init", "--initial-branch=main")
        builder._configure_fixture_repository()
        return builder

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run Git in the fixture repository and return its textual result."""
        return subprocess.run(
            ["git", *arguments],
            cwd=self.path,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
        )

    def git_bytes(
        self, *arguments: str, input_bytes: bytes, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        """Run Git with binary fixture input for repository paths outside UTF-8."""
        return subprocess.run(
            ["git", *arguments],
            cwd=self.path,
            check=check,
            capture_output=True,
            input=input_bytes,
            env=self._environment(),
        )

    def write(self, relative_path: str, content: str) -> None:
        """Write fixture content without allowing paths outside its root."""
        destination = (self.path / relative_path).resolve()
        if not destination.is_relative_to(self.path):
            raise ValueError("Fixture paths must remain inside the repository.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    def commit(self, message: str, *, trailers: tuple[str, ...] = ()) -> str:
        """Stage all fixture changes and return the new full commit object ID."""
        self.git("add", "-A")
        complete_message = message
        if trailers:
            complete_message = f"{message}\n\n" + "\n".join(trailers)
        self._commit(complete_message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def checkout(self, reference: str) -> None:
        """Check out an existing fixture reference."""
        self.git("checkout", reference)

    def checkout_new(self, branch: str, start: str | None = None) -> None:
        """Create and check out a fixture branch from an optional commit."""
        arguments = ["checkout", "-b", branch]
        if start is not None:
            arguments.append(start)
        self.git(*arguments)

    def clone_to(self, destination: Path) -> RepoBuilder:
        """Clone this fixture into another directory under the temporary root."""
        target = destination.resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("Fixture clones must remain inside the temporary root.")
        subprocess.run(
            ["git", "clone", "--no-local", str(self.path), str(target)],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
        )
        clone = RepoBuilder(path=target, _root=self._root, _next_timestamp=self._next_timestamp)
        clone._configure_fixture_repository()
        return clone

    def clone_shallow_to(self, destination: Path, *, depth: int) -> RepoBuilder:
        """Clone this fixture through file:// so depth is enforced by Git."""
        target = destination.resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("Fixture clones must remain inside the temporary root.")
        subprocess.run(
            ["git", "clone", f"--depth={depth}", self.path.as_uri(), str(target)],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
        )
        clone = RepoBuilder(path=target, _root=self._root, _next_timestamp=self._next_timestamp)
        clone._configure_fixture_repository()
        return clone

    def service(self):
        """Open this fixture through Mandu'a's public service facade."""
        from mandua.memory_service import MemoryService

        return MemoryService.open(self.path)

    def runner(self) -> GitRunner:
        """Open this fixture through Mandu'a's bounded Git runner."""
        return GitRunner(self.path)

    def _configure_fixture_repository(self) -> None:
        self.git("config", "--local", "user.name", "Mandu'a Test")
        self.git("config", "--local", "user.email", "test@mandua.invalid")
        self.git("config", "--local", "commit.gpgSign", "false")

    def _commit(self, message: str, *, allow_empty: bool = False) -> None:
        arguments = ["commit", "--no-gpg-sign", "-m", message]
        if allow_empty:
            arguments.append("--allow-empty")
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.path,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )
        self._next_timestamp += timedelta(seconds=1)

    def _environment(self) -> dict[str, str]:
        timestamp = self._next_timestamp.isoformat().replace("+00:00", "Z")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_DATE": timestamp,
                "GIT_COMMITTER_DATE": timestamp,
            }
        )
        return environment
