"""Release-metadata checks for the open-source proof of concept."""

from __future__ import annotations

import email.parser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DOCUMENTS = (
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ACKNOWLEDGMENTS.md",
    "docs/prior-art.md",
    "docs/tutorial.md",
    "docs/conformance.md",
)
PUBLIC_OPERATIONS = {
    "annotate",
    "checkpoint",
    "compare",
    "context",
    "correct",
    "decision",
    "demo",
    "evolution",
    "integrate",
    "origin",
    "recover",
    "status",
    "timeline",
    "why",
}
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
PRIOR_ART_URLS = {
    "https://github.com/faugustdev/git-context-controller",
    "https://github.com/Growth-Kinetics/DiffMem",
    "https://github.com/Substr8-Labs/gam",
    "https://www.npmjs.com/package/git-mem",
}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _project() -> dict[str, object]:
    return tomllib.loads(_read("pyproject.toml"))["project"]


def _public_help() -> str:
    completed = subprocess.run(
        [sys.executable, "-m", "mandua", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return completed.stdout


def _make_recipes() -> tuple[set[str], dict[str, tuple[str, ...]]]:
    lines = _read("Makefile").splitlines()
    phony: set[str] = set()
    recipes: dict[str, tuple[str, ...]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(".PHONY:"):
            phony.update(line.partition(":")[2].split())
        if line and not line.startswith(("\t", ".")) and line.endswith(":"):
            target = line[:-1]
            commands: list[str] = []
            index += 1
            while index < len(lines) and lines[index].startswith("\t"):
                commands.append(lines[index][1:])
                index += 1
            recipes[target] = tuple(commands)
            continue
        index += 1
    return phony, recipes


def _archive_suffixes(names: set[str]) -> set[str]:
    return {name.partition("/")[2] for name in names if "/" in name}


def _build_test_wheel(destination: Path) -> Path:
    output = destination / "distributions"
    completed = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", os.fspath(output), "--no-progress"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = sorted(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _install_test_wheel(wheel: Path, destination: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "UV_OFFLINE": "1",
        }
    )
    environment.pop("PYTHONPATH", None)
    venv = destination / "wheel-environment"
    python = venv / "bin" / "python"
    cli = venv / "bin" / "mandua"
    created = subprocess.run(
        ["uv", "venv", os.fspath(venv), "--python", sys.executable],
        cwd=destination,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            os.fspath(python),
            "--no-deps",
            os.fspath(wheel),
        ],
        cwd=destination,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    located = subprocess.run(
        [os.fspath(python), "-c", "import mandua; print(mandua.__file__)"],
        cwd=destination,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert located.returncode == 0, located.stderr
    package_root = Path(located.stdout.strip()).resolve(strict=True).parent
    return python, cli, package_root, environment


def _external_demo_tree(canonical: Path, destination: Path) -> Path:
    shutil.copytree(canonical, destination)
    scenario = destination / "scenario.toml"
    scenario.write_text(
        scenario.read_text(encoding="utf-8").replace(
            'decision_id = "DEC-IRR-001"', 'decision_id = "DEC-EXT-999"'
        ),
        encoding="utf-8",
    )
    (destination / "fixtures/baseline/knowledge/observations.md").write_text(
        "# EXTERNAL INSTALLED TREE MUST NOT BE CONSUMED\n",
        encoding="utf-8",
    )
    return destination


def _parse_workflow_scalar(value: str) -> object:
    value = value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {"null", "~"}:
        return None
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("[") and value.endswith("]"):
        return [
            _parse_workflow_scalar(item)
            for item in value.removeprefix("[").removesuffix("]").split(",")
        ]
    return value


def _parse_workflow() -> dict[str, object]:
    """Parse the deliberately small workflow subset into typed Python values."""
    source_lines = _read(".github/workflows/ci.yml").splitlines()
    assert all("\t" not in line for line in source_lines)
    lines = [
        (len(line) - len(line.lstrip(" ")), line.lstrip(" "))
        for line in source_lines
        if line.strip() and not line.lstrip().startswith("#")
    ]

    def key_value(text: str) -> tuple[str, str]:
        key, separator, value = text.partition(":")
        assert separator
        parsed_key = _parse_workflow_scalar(key)
        assert isinstance(parsed_key, str) and parsed_key
        return parsed_key, value.strip()

    def mapping(
        index: int,
        indent: int,
        first: str | None = None,
    ) -> tuple[dict[str, object], int]:
        result: dict[str, object] = {}
        pending = first
        while pending is not None or (
            index < len(lines)
            and lines[index][0] == indent
            and not lines[index][1].startswith("- ")
        ):
            if pending is None:
                text = lines[index][1]
                index += 1
            else:
                text = pending
                pending = None
            key, value = key_value(text)
            assert key not in result
            if value:
                result[key] = _parse_workflow_scalar(value)
            elif index < len(lines) and lines[index][0] > indent:
                result[key], index = node(index, lines[index][0])
            else:
                result[key] = None
        return result, index

    def sequence(index: int, indent: int) -> tuple[list[object], int]:
        result: list[object] = []
        while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
            first = lines[index][1].removeprefix("- ")
            index += 1
            if ":" in first:
                item, index = mapping(index, indent + 2, first)
            else:
                item = _parse_workflow_scalar(first)
            result.append(item)
        return result, index

    def node(index: int, indent: int) -> tuple[object, int]:
        if lines[index][1].startswith("- "):
            return sequence(index, indent)
        return mapping(index, indent)

    parsed, consumed = mapping(0, 0)
    assert consumed == len(lines)
    return parsed


def test_public_documents_and_package_metadata_are_aligned() -> None:
    """This fails if the distribution identity or required public document is absent."""
    project = _project()
    readme = _read("README.md")

    assert project["name"] == "mandua-memory"
    assert project["version"] == "0.1.0"
    assert project["description"] == "Verifiable agent memory, backed by Git."
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.11"
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["dependencies"] == []
    assert project["scripts"] == {"mandua": "mandua.cli:main"}
    assert "Verifiable agent memory, backed by Git." in readme
    assert "reference implementation of a strict, Git-evidence-backed memory model" in " ".join(
        readme.split()
    )
    for path in PUBLIC_DOCUMENTS:
        assert (ROOT / path).is_file(), path


def test_apache_license_is_the_unmodified_canonical_text() -> None:
    """This fails if the license is abbreviated, personalized, or otherwise modified."""
    payload = (ROOT / "LICENSE").read_bytes()

    assert len(payload) == 11_358
    assert hashlib.sha256(payload).hexdigest() == APACHE_2_SHA256
    assert payload.startswith(b"\n                                 Apache License\n")
    assert b"   END OF TERMS AND CONDITIONS\n" in payload
    assert b"   APPENDIX: How to apply the Apache License to your work.\n" in payload
    assert payload.endswith(b"   limitations under the License.\n")


def test_readme_operation_table_matches_the_real_public_help() -> None:
    """This fails if a documented operation is missing from or invented beyond CLI help."""
    help_text = _public_help()
    help_operations = {
        match.group("operation")
        for match in re.finditer(
            r"^    (?P<operation>[a-z][a-z-]+)\s{2,}[^\n]+$", help_text, re.MULTILINE
        )
    }
    readme_operations = {
        match.group("operation")
        for match in re.finditer(
            r"^\| `(?P<operation>[a-z][a-z-]+)` \|", _read("README.md"), re.MULTILINE
        )
    }

    assert help_operations == PUBLIC_OPERATIONS
    assert readme_operations == help_operations


def test_readme_contains_runnable_local_workflows_and_the_stable_result_shape() -> None:
    """This fails if the public quick paths or JSON adapter boundary become misleading."""
    readme = _read("README.md")
    match = re.search(
        r"<!-- memory-result-example -->\s*```json\n(?P<payload>\{.*?\})\n```",
        readme,
        re.DOTALL,
    )

    assert "## 90-second local demo" in readme
    assert "uv sync --locked" in readme
    assert "make demo" in readme
    assert "make tutorial-check" in readme
    assert "docs/tutorial.md" in readme
    assert "--apply" in readme
    assert "refs/notes/review" in readme
    assert "git fetch" in readme
    assert match is not None
    payload = json.loads(match.group("payload"))
    assert set(payload) == {
        "answer",
        "applied",
        "changes",
        "confidence",
        "evidence",
        "gaps",
        "history_scope",
        "inferred",
        "observed",
        "operation",
        "schema_version",
        "warnings",
    }
    assert payload["schema_version"] == "1.0"
    assert payload["operation"] == "status"
    assert payload["applied"] is False


def test_readme_does_not_make_unverified_product_or_installation_claims() -> None:
    """This fails if release copy overstates novelty, maturity, support, or publication."""
    readme = _read("README.md")
    folded = readme.casefold()
    forbidden = (
        "mandu'a is the first git-backed",
        "first git-backed memory",
        "production-ready for",
        "validated on windows",
        "windows is validated",
        "agent-id authenticates",
        "agent-id verifies identity",
        "mandu'a is an mcp server",
        "mandu'a is a codex skill",
        "live openrouter support is included",
        "available on pypi",
        "published on pypi",
        "pip install mandua",
        "installation requires no network",
    )

    assert all(claim not in folded for claim in forbidden)
    assert "proof of concept, not a production-ready service" in folded
    assert "windows has not been validated" in folded
    assert "declared operational identity, not authentication" in folded
    assert "includes an experimental pi skill, but no mcp server" in folded
    manifest = json.loads(_read("package.json"))
    assert manifest["pi"]["skills"] == ["./skills/mandua"]
    assert (ROOT / "skills/mandua/SKILL.md").is_file()
    assert "does not include live openrouter calls" in folded
    assert "dependency installation may require network access" in folded


def test_prior_art_uses_only_approved_sources_and_states_the_comparison_boundary() -> None:
    """This fails if prior-art links drift or independent implementation is overstated."""
    document = _read("docs/prior-art.md")
    folded = " ".join(document.casefold().split())
    urls = {match.rstrip(".,)") for match in re.findall(r"https://[^\s>]+", document)}

    assert urls == PRIOR_ART_URLS
    assert "Accessed: 2026-08-31" in document
    assert "Git Context Controller" in document
    assert "DiffMem" in document
    assert "GAM" in document
    assert "git-mem" in document
    assert "separate memory branches" in folded
    assert "general design category" in folded
    assert "independently written" in folded
    assert "not legal or trademark clearance" in folded
    assert "no source repository is inferred for the npm package" in folded


def test_public_authored_documents_are_english_and_cross_links_resolve() -> None:
    """This fails if maintained public prose adds known non-English copy or dead local links."""
    authored = (
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "ACKNOWLEDGMENTS.md",
        "docs/prior-art.md",
        "docs/tutorial.md",
        "docs/conformance.md",
        "docs/pi.md",
        "docs/try-demo.md",
        "docs/demo-story.md",
        "docs/skill-registration.md",
    )
    known_non_english_phrases = (
        "este proyecto",
        "para contribuir",
        "todos los",
        "prueba de concepto",
        "repositorio de",
        "seguridad del",
    )

    for path in authored:
        document = _read(path)
        document.encode("ascii")
        assert not any(phrase in document.casefold() for phrase in known_non_english_phrases), path
        for link in re.findall(r"\[[^\]]+\]\((?!https?://)([^)#]+)(?:#[^)]+)?\)", document):
            target = ((ROOT / path).parent / link).resolve()
            assert target.is_relative_to(ROOT)
            assert target.exists(), f"{path}: {link}"


def test_contribution_and_security_guidance_match_the_poc_reality() -> None:
    """This fails if contribution or vulnerability guidance weakens an approved boundary."""
    contributing = _read("CONTRIBUTING.md")
    security = _read("SECURITY.md")

    for required in (
        "English",
        "uv",
        "test-driven development",
        "RED",
        "GREEN",
        "real disposable Git repositories",
        "Memory-Type",
        "Scope",
        "Task-ID",
        "Agent-ID",
        "independently",
        "Do not copy third-party code, prose, diagrams, or templates",
        "untrusted",
    ):
        assert required in contributing
    assert "pip install" not in contributing.casefold()

    for required in (
        "No production release is currently supported",
        "private reporting endpoint",
        "Do not open a public issue",
        "untrusted data",
        "rotate the secret first",
        "history cleanup",
        "proof of concept",
        "not a production security boundary",
    ):
        assert required in security
    assert not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", security)
    assert "private vulnerability reporting is enabled" not in security.casefold()


def test_ci_matrix_runs_only_the_approved_credential_free_gates() -> None:
    """This fails if CI loses a platform or introduces an unapproved secret/network call."""
    workflow = _read(".github/workflows/ci.yml")
    document = _parse_workflow()
    assert set(document) == {"name", "on", "permissions", "jobs"}
    assert document["on"] == {"push": None, "pull_request": None}
    assert document["permissions"] == {"contents": "read"}
    jobs = document["jobs"]
    assert isinstance(jobs, dict) and set(jobs) == {"verify"}
    verify = jobs["verify"]
    assert isinstance(verify, dict)
    assert set(verify) == {"name", "runs-on", "timeout-minutes", "strategy", "steps"}
    assert verify["runs-on"] == "${{ matrix.os }}"
    assert verify["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "os": ["ubuntu-latest", "macos-latest"],
            "python-version": ["3.11"],
        },
    }
    steps = verify["steps"]
    assert isinstance(steps, list)
    assert steps[0] == {
        "name": "Check out the repository",
        "uses": "actions/checkout@v4",
        "with": {"persist-credentials": False},
    }
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1] == {
        "name": "Install uv and Python",
        "uses": "astral-sh/setup-uv@v6",
        "with": {"python-version": "3.11"},
    }
    assert steps[2:] == [
        {"name": "Sync the locked environment", "run": "uv sync --locked"},
        {
            "name": "Run format, lint, unit, and real-Git acceptance checks",
            "run": "make check",
        },
        {"name": "Run the credential-free local demo", "run": "make demo"},
        {
            "name": "Verify the generated executable tutorial",
            "run": "make tutorial-check",
        },
    ]
    assert all("env" not in step for step in steps)
    serialized = json.dumps(document, sort_keys=True).casefold()
    forbidden = (
        "secrets.",
        "github.token",
        "github_token",
        "gh_token",
        '"token"',
        '"password"',
        "openrouter",
        "api_key",
        "curl ",
        "wget ",
        "pip ",
    )
    assert all(token not in serialized for token in forbidden)
    assert "\t" not in workflow


def test_make_targets_preserve_conformance_and_release_gates() -> None:
    """This fails if a public make target silently drops or changes a release check."""
    phony, recipes = _make_recipes()
    expected = {
        "sync": ("uv sync --locked",),
        "test": ("uv run pytest -q",),
        "check": (
            "uv run ruff format --check .",
            "uv run ruff check .",
            "uv run pytest -q",
        ),
        "build": ("uv build",),
        "demo": ("uv run mandua demo",),
        "tutorial-check": (
            "uv run python scripts/render_tutorial.py --check",
            "uv run pytest tests/acceptance/test_tutorial.py -q",
        ),
        "conformance-check": (
            "uv run pytest tests/unit/test_conformance_manifest.py tests/acceptance/test_conformance.py tests/acceptance/test_security_boundaries.py -q",
        ),
    }

    assert recipes == expected
    assert phony == set(expected)


def test_standard_local_build_outputs_are_ignored_without_broad_patterns() -> None:
    """This fails if the release build dirties Git or ignores unrelated user content."""
    ignored = (
        "dist/mandua_memory-0.1.0-py3-none-any.whl",
        "build/lib/mandua/__init__.py",
        "src/mandua_memory.egg-info/PKG-INFO",
    )
    for path in ignored:
        completed = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", path],
            cwd=ROOT,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0, path

    root_ignore = _read(".gitignore").splitlines()
    assert "dist/" in root_ignore
    assert "build/" in root_ignore
    assert "*.egg-info/" in root_ignore
    assert "*" not in root_ignore
    assert ".env" not in root_ignore


def test_package_classifiers_urls_and_lock_are_truthful() -> None:
    """This fails if package metadata invents support, publication, or unlocked dependencies."""
    project = _project()
    classifiers = set(project["classifiers"])
    urls = project.get("urls", {})
    lock = tomllib.loads(_read("uv.lock"))
    locked_project = next(item for item in lock["package"] if item["name"] == "mandua-memory")

    assert {
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Topic :: Software Development :: Version Control :: Git",
    } <= classifiers
    assert not any("Windows" in classifier for classifier in classifiers)
    assert isinstance(urls, dict)
    for value in urls.values():
        assert isinstance(value, str) and value.startswith("https://")
        assert not re.search(
            r"(?:example\.(?:com|org|invalid)|placeholder|todo)", value, re.IGNORECASE
        )
    assert locked_project["version"] == project["version"]
    assert locked_project["source"] == {"editable": "."}
    assert {item["name"] for item in locked_project["dev-dependencies"]["dev"]} == {
        "pytest",
        "ruff",
    }


def test_wheel_and_sdist_are_self_contained_offline_poc_artifacts(tmp_path: Path) -> None:
    """This fails if either archive cannot carry the declared local proof of concept."""
    output = tmp_path / "distributions"
    completed = subprocess.run(
        ["uv", "build", "--out-dir", os.fspath(output), "--no-progress"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = sorted(output.glob("*.whl"))
    sdists = sorted(output.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = set(archive.namelist())
        metadata_name = next(name for name in wheel_names if name.endswith(".dist-info/METADATA"))
        license_name = next(
            name for name in wheel_names if name.endswith(".dist-info/licenses/LICENSE")
        )
        metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_name))
        assert archive.read(license_name) == (ROOT / "LICENSE").read_bytes()
    assert {
        "mandua/__init__.py",
        "mandua/__main__.py",
        "mandua/_demo/scenario.toml",
        "mandua/_demo/fixtures/baseline/.mandua.toml",
        "mandua/_demo/fixtures/baseline/knowledge/irrigation-rules.json",
        "mandua/_demo/fixtures/baseline/knowledge/observations.md",
        "mandua/_demo/fixtures/correction/knowledge/irrigation-rules.json",
        "mandua/_demo/fixtures/deleted-phrase/knowledge/temporary-rule.md",
        "mandua/_demo/fixtures/hypothesis-schedule/knowledge/irrigation-rules.json",
        "mandua/_demo/fixtures/hypothesis-sensor/knowledge/irrigation-rules.json",
        "mandua/_demo/fixtures/malicious-history/knowledge/untrusted-note.md",
        "mandua/_demo/fixtures/mistake/knowledge/irrigation-rules.json",
        "mandua/cli.py",
        "mandua/demo.py",
        "mandua/memory_service.py",
        "adapters/jev/__init__.py",
        "adapters/jev/client.py",
        "adapters/jev/harness.py",
    } <= wheel_names
    assert metadata["Name"] == "mandua-memory"
    assert metadata["Version"] == "0.1.0"
    assert metadata["License-Expression"] == "Apache-2.0"
    assert metadata["Requires-Python"] == ">=3.11"
    assert metadata.get_all("License-File") == ["LICENSE"]
    assert metadata.get_all("Project-URL") == [
        "Repository, https://github.com/fermellone/mandua",
        "Issues, https://github.com/fermellone/mandua/issues",
        "Documentation, https://github.com/fermellone/mandua#readme",
    ]

    with tarfile.open(sdists[0], mode="r:gz") as archive:
        sdist_names = _archive_suffixes(set(archive.getnames()))
        license_member = next(
            member for member in archive.getmembers() if member.name.endswith("/LICENSE")
        )
        extracted = archive.extractfile(license_member)
        assert extracted is not None
        assert extracted.read() == (ROOT / "LICENSE").read_bytes()
    assert {
        ".githooks/commit-msg",
        ".githooks/pre-commit",
        ".githooks/pre-push",
        ".githooks/pre-rebase",
        "ACKNOWLEDGMENTS.md",
        "adapters/jev/README.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "Makefile",
        "README.md",
        "SECURITY.md",
        "demo/scenario.toml",
        "docs/conformance.md",
        "docs/prior-art.md",
        "docs/tutorial.md",
        "pyproject.toml",
        "scripts/render_tutorial.py",
        "src/mandua/cli.py",
        "tests/acceptance/test_conformance.py",
        "tests/acceptance/test_tutorial.py",
        "tests/unit/test_project_metadata.py",
        "uv.lock",
        "package.json",
        "docs/pi.md",
        "skills/mandua/SKILL.md",
        "docs/try-demo.md",
        "docs/demo-story.md",
        "docs/skill-registration.md",
        "skills/mandua/scripts/mandua",
        "skills/mandua/scripts/alternatives",
        "skills/mandua/scripts/alternatives.py",
        "skills/mandua/scripts/corrections",
        "skills/mandua/scripts/corrections.py",
    } <= sdist_names

    environment = os.environ.copy()
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "UV_OFFLINE": "1",
        }
    )
    environment.pop("PYTHONPATH", None)
    venv = tmp_path / "wheel-environment"
    installed_python = venv / "bin" / "python"
    installed_cli = venv / "bin" / "mandua"
    created = subprocess.run(
        ["uv", "venv", os.fspath(venv), "--python", sys.executable],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            os.fspath(installed_python),
            "--no-deps",
            os.fspath(wheels[0]),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    module_result = subprocess.run(
        [
            os.fspath(installed_python),
            "-c",
            "import mandua.demo; print(mandua.demo.__file__)",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert module_result.returncode == 0, module_result.stderr
    installed_module = Path(module_result.stdout.strip()).resolve(strict=True)
    packaged_scenario = installed_module.parent / "_demo" / "scenario.toml"
    assert packaged_scenario.is_file()
    collision_scenario = installed_module.parents[2] / "demo" / "scenario.toml"
    assert collision_scenario != packaged_scenario
    assert collision_scenario.is_relative_to(tmp_path)
    collision_scenario.parent.mkdir(parents=True)
    collision_scenario.write_text('schema_version = "unrelated-collision"\n', encoding="utf-8")

    help_result = subprocess.run(
        [os.fspath(installed_cli), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert help_result.stderr == ""
    assert all(f"    {operation}" in help_result.stdout for operation in PUBLIC_OPERATIONS)

    demo_output = tmp_path / "installed-wheel-demo"
    demo_result = subprocess.run(
        [
            os.fspath(installed_cli),
            "demo",
            "--output",
            os.fspath(demo_output),
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert demo_result.returncode == 0, demo_result.stderr
    assert demo_result.stderr == ""
    report = json.loads(demo_result.stdout)
    assert set(report["claims"]) == {
        "comparison",
        "correction",
        "decision",
        "deleted-origin",
        "integration",
        "malicious-history",
        "recovery",
        "review",
    }
    assert report["claims"]["decision"]["decision_id"] == "DEC-IRR-001"
    assert report["bundle_verified"] is True
    assert report["network_accessed"] is False
    assert report["operation_log_complete"] is True
    assert Path(report["report_path"]).is_file()
    assert Path(report["bundle_path"]).is_file()
    assert {entry["transport"] for entry in report["operation_log"]} <= {None, "file"}
    bundle_result = subprocess.run(
        ["git", "bundle", "verify", report["bundle_path"]],
        cwd=report["repository_path"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert bundle_result.returncode == 0, bundle_result.stdout + bundle_result.stderr


@pytest.mark.parametrize("linked_root", ("_demo", "fixtures"))
def test_fresh_offline_wheel_rejects_symbolic_link_demo_roots(
    tmp_path: Path, linked_root: str
) -> None:
    """This fails if an installed package root can redirect the canonical story externally."""
    wheel = _build_test_wheel(tmp_path)
    case = tmp_path / f"installed-{linked_root}"
    case.mkdir()
    _python, cli, package_root, environment = _install_test_wheel(wheel, case)
    canonical = package_root / "_demo"
    external_demo = _external_demo_tree(canonical, case / "external-demo")
    target = canonical if linked_root == "_demo" else canonical / "fixtures"
    external = external_demo if linked_root == "_demo" else external_demo / "fixtures"
    saved = target.with_name(f"{target.name}.canonical")
    target.rename(saved)
    target.symlink_to(external, target_is_directory=True)
    output = case / "attacked-output"

    completed = subprocess.run(
        [os.fspath(cli), "demo", "--output", os.fspath(output), "--format", "json"],
        cwd=case,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )

    assert completed.returncode == 5, (
        f"unexpected exit {completed.returncode}; stderr={completed.stderr[:2_000]!r}"
    )
    error = json.loads(completed.stderr)
    assert error["code"] == "validation_failed"
    assert not output.exists()
    assert external.is_dir()


@pytest.mark.parametrize("replaced_root", ("_demo", "fixtures"))
def test_fresh_offline_wheel_uses_captured_bytes_after_real_root_replacement(
    tmp_path: Path, replaced_root: str
) -> None:
    """This fails if an installed demo reopens a replacement after selecting its resources."""
    wheel = _build_test_wheel(tmp_path)
    case = tmp_path / f"installed-{replaced_root}"
    case.mkdir()
    python, _cli, package_root, environment = _install_test_wheel(wheel, case)
    canonical = package_root / "_demo"
    external_demo = _external_demo_tree(canonical, case / "external-demo")
    output = case / "attacked-output"
    script = """
import json
import os
import sys
from pathlib import Path

from mandua.demo import DemoScenario

package_root = Path(sys.argv[1])
external_demo = Path(sys.argv[2])
output = Path(sys.argv[3])
root_name = sys.argv[4]
scenario = DemoScenario()
canonical = package_root / "_demo"
target = canonical if root_name == "_demo" else canonical / "fixtures"
external = external_demo if root_name == "_demo" else external_demo / "fixtures"
saved = target.with_name(f"{target.name}.captured")
target.rename(saved)
external.rename(target)
try:
    report = scenario.build(output)
    print(report.to_json())
finally:
    target.rename(external)
    saved.rename(target)
"""

    completed = subprocess.run(
        [
            os.fspath(python),
            "-c",
            script,
            os.fspath(package_root),
            os.fspath(external_demo),
            os.fspath(output),
            replaced_root,
        ],
        cwd=case,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["claims"]["decision"]["decision_id"] == "DEC-IRR-001"
    observations = subprocess.run(
        [
            "git",
            "--no-pager",
            "show",
            f"{report['commit_ids']['baseline']}:knowledge/observations.md",
        ],
        cwd=report["repository_path"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert observations.returncode == 0, observations.stderr
    assert observations.stdout == _read("demo/fixtures/baseline/knowledge/observations.md")
    assert "EXTERNAL INSTALLED TREE" not in observations.stdout
