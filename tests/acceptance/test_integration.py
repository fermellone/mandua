"""Real-Git acceptance tests for validated non-fast-forward integrations."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from helpers.repo import RepoBuilder

import mandua.git_runner as git_runner_module
import mandua.writes.integration as integration_module
from mandua.errors import ErrorCode, ManduaError
from mandua.git_runner import GitCapabilities, GitOutput, GitRunner
from mandua.memory_service import MemoryService
from mandua.models import AnnotationRequest, CommitMetadata, IntegrationRequest, QueryLimits
from mandua.policy import Policy
from mandua.writes.integration import IntegrationWriter


class _InjectedIntegrationControl(BaseException):
    """Test-only non-Mandua control flow used at mutation boundaries."""


def _request(
    *,
    source: str = "hypothesis/sensor",
    target: str = "main",
    subject: str = "Adopt sensor-based irrigation",
) -> IntegrationRequest:
    return IntegrationRequest(
        source=source,
        target=target,
        subject=subject,
        metadata=CommitMetadata(
            memory_type="integration",
            scope="irrigation",
            task_id="TASK-IRR-7",
            decision_id="DEC-IRR-1",
            agent_id="gardener",
            reason="The sensor hypothesis reduced water use.",
        ),
    )


def _repository_state(repo) -> tuple[str, str, str, str]:
    return (
        repo.git("rev-parse", "HEAD").stdout.strip(),
        repo.git("rev-parse", "refs/heads/main").stdout.strip(),
        repo.git("status", "--porcelain=v1", "--untracked-files=all").stdout,
        repo.git("for-each-ref", "--format=%(refname) %(objectname)").stdout,
    )


def _create_source(repo, *, path: str = "knowledge/source.md") -> str:
    repo.checkout_new("hypothesis/sensor")
    repo.write(path, "Sensor observation\n")
    source = repo.commit("Record the sensor hypothesis")
    repo.checkout("main")
    return source


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o700)


def _advance_ref(repo, branch_ref: str, parent_oid: str, message: str) -> str:
    tree = repo.git("show", "-s", "--format=%T", parent_oid).stdout.strip()
    commit = repo.git("commit-tree", tree, "-p", parent_oid, "-m", message).stdout.strip()
    repo.git("update-ref", branch_ref, commit, parent_oid)
    return commit


def _prepare_clean_divergence(repo) -> tuple[str, str]:
    base = repo.git("rev-parse", "main").stdout.strip()
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/source.md", "Sensor observation\n")
    source = repo.commit("Record the source tip")
    repo.checkout("main")
    repo.write("knowledge/target.md", "Canonical observation\n")
    target = repo.commit("Record the target tip")
    return target, source


def _path_identity(path: Path) -> tuple[int, int, int]:
    metadata = os.stat(path, follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _replace_directory_root_preserving_children(path: Path) -> Path:
    """Replace only one directory root while retaining every child inode and path."""
    metadata = os.stat(path, follow_symlinks=False)
    displaced = path.with_name(f"{path.name}.mandua-root-captured")
    path.rename(displaced)
    try:
        path.mkdir()
        path.chmod(stat.S_IMODE(metadata.st_mode))
        for child in tuple(displaced.iterdir()):
            child.rename(path / child.name)
    except BaseException:
        if path.exists():
            for child in tuple(path.iterdir()):
                child.rename(displaced / child.name)
            path.rmdir()
        displaced.rename(path)
        raise
    return displaced


def _restore_directory_root(path: Path, displaced: Path) -> None:
    for child in tuple(path.iterdir()):
        child.rename(displaced / child.name)
    path.rmdir()
    displaced.rename(path)


def test_integration_preview_is_non_mutating_and_apply_forces_two_parents(repo) -> None:
    """This fails if preview mutates state or a descendant source is fast-forwarded."""
    repo.checkout_new("hypothesis/sensor")
    repo.write("knowledge/irrigation-rules.json", '[{"id":"IRR-1","threshold":35}]\n')
    source = repo.commit("Use a moisture threshold")
    repo.checkout("main")
    before = _repository_state(repo)

    preview = repo.service().integrate(_request())

    assert preview.applied is False
    assert preview.changes[0].action == "update-ref"
    assert preview.changes[0].target == "refs/heads/main"
    assert preview.changes[0].before_oid == before[0]
    assert preview.changes[0].after_oid is None
    assert preview.evidence[0].details["parent_oids"] == [before[0], source]
    assert preview.evidence[0].details["proposed_tree"]
    assert _repository_state(repo) == before

    applied = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "refs/heads/main").stdout.strip()
    parents = repo.git("show", "-s", "--format=%P", commit).stdout.strip().split()
    assert applied.applied is True
    assert applied.warnings == ()
    assert applied.changes[0].after_oid == commit
    assert parents == [before[0], source]
    assert (
        repo.git("show", "-s", "--format=%T", commit).stdout.strip()
        == (preview.evidence[0].details["proposed_tree"])
    )
    assert repo.git("status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_integration_applies_a_clean_diverged_merge_with_captured_parent_order(repo) -> None:
    """This fails if a real three-way merge reverses or drops either captured parent."""
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/sensor.md", "Moisture threshold: 35%\n")
    source = repo.commit("Record the sensor hypothesis")
    repo.checkout("main")
    repo.write("knowledge/weather.md", "Rain forecast: dry\n")
    target = repo.commit("Record the weather observation")

    preview = repo.service().integrate(_request())
    applied = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "main").stdout.strip()
    assert applied.applied is True
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [
        target,
        source,
    ]
    assert (
        repo.git("show", "-s", "--format=%T", commit).stdout.strip()
        == (preview.evidence[0].details["proposed_tree"])
    )


def test_integration_reports_a_textual_conflict_without_mutation(repo) -> None:
    """This fails if conflict evidence is lost or merge state reaches the repository."""
    repo.write("knowledge/rules.md", "Threshold: 30%\n")
    base = repo.commit("Record the baseline threshold")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.commit("Raise the threshold from sensor evidence")
    repo.checkout("main")
    repo.write("knowledge/rules.md", "Threshold: 25%\n")
    repo.commit("Lower the threshold from field evidence")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.CONFLICT
    assert caught.value.evidence
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_rejects_duplicate_rule_ids_before_mutation(repo) -> None:
    """This fails if semantic validation runs after the target branch is changed."""
    repo.write(
        ".mandua.toml",
        "[[invariants]]\n"
        'kind = "unique-json-field"\n'
        'path = "knowledge/irrigation-rules.json"\n'
        'field = "id"\n',
    )
    repo.write("knowledge/irrigation-rules.json", '[{"id":"IRR-1","threshold":35}]\n')
    base = repo.commit("Configure unique irrigation rule identifiers")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write(
        "knowledge/irrigation-rules.json",
        '[{"id":"IRR-1","threshold":35},{"id":"IRR-1","threshold":80}]\n',
    )
    repo.commit("Create duplicate irrigation rules")
    repo.checkout("main")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.CONFLICT
    assert _repository_state(repo) == before


def test_integration_reports_an_absent_merge_tree_capability(repo, monkeypatch) -> None:
    """This fails if integration silently assumes merge-tree --write-tree support."""
    _create_source(repo)
    monkeypatch.setattr(
        GitRunner,
        "capabilities",
        property(
            lambda self: GitCapabilities(version="git version test", merge_tree_write_tree=False)
        ),
    )
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    assert _repository_state(repo) == before


@pytest.mark.parametrize("dirty_kind", ("modified", "staged", "untracked"))
def test_integration_apply_requires_a_completely_clean_repository(repo, dirty_kind) -> None:
    """This fails if any tracked, staged, or untracked state can enter the merge transaction."""
    _create_source(repo)
    if dirty_kind == "modified":
        repo.write("tracked.md", "Baseline\n")
        repo.commit("Record a tracked file")
        repo.write("tracked.md", "Modified\n")
    elif dirty_kind == "staged":
        repo.write("staged.md", "Staged\n")
        repo.git("add", "staged.md")
    else:
        repo.write("untracked.md", "Untracked\n")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert _repository_state(repo) == before


def test_integration_apply_rejects_detached_head(repo) -> None:
    """This fails if a detached checkout can receive a canonical integration."""
    _create_source(repo)
    before = repo.git("rev-parse", "main").stdout.strip()
    repo.git("checkout", "--detach", before)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "main").stdout.strip() == before
    assert repo.git("rev-parse", "HEAD").stdout.strip() == before


def test_integration_apply_rejects_the_wrong_checked_out_branch(repo) -> None:
    """This fails if apply mutates target main while another branch owns the worktree."""
    source = _create_source(repo)
    repo.checkout_new("work/other")
    before_main = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "main").stdout.strip() == before_main
    assert repo.git("rev-parse", "hypothesis/sensor").stdout.strip() == source


@pytest.mark.parametrize(
    ("source", "target"),
    (
        ("--upload-pack=evil", "main"),
        ("refs/heads/hypothesis/sensor", "main"),
        ("hypothesis/sensor", "refs/heads/main"),
        ("hypothesis/sensor", "-main"),
    ),
)
def test_integration_rejects_option_like_or_noncanonical_branch_inputs(
    repo, source, target
) -> None:
    """This fails if caller text can be reinterpreted as an option or alternate ref namespace."""
    _create_source(repo)
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(source=source, target=target), apply=True)

    assert caught.value.code in {ErrorCode.VALIDATION_FAILED, ErrorCode.POLICY_VIOLATION}
    assert _repository_state(repo) == before


def test_integration_rejects_a_source_already_contained_by_target(repo) -> None:
    """This fails if an ancestor source creates a meaningless two-parent commit."""
    repo.checkout_new("hypothesis/sensor")
    source = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout("main")
    repo.write("knowledge/main.md", "Canonical observation\n")
    target = repo.commit("Advance canonical history")

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.git("rev-parse", "main").stdout.strip() == target
    assert repo.git("rev-parse", "hypothesis/sensor").stdout.strip() == source


def test_integration_rejects_unrelated_complete_histories(repo) -> None:
    """This fails if merge-tree is allowed to synthesize ancestry for unrelated roots."""
    main = repo.git("rev-parse", "main").stdout.strip()
    repo.git("checkout", "--orphan", "hypothesis/sensor")
    repo.write("knowledge/unrelated.md", "Unrelated observation\n")
    repo.commit("Create an unrelated history")
    repo.checkout("main")

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.CONFLICT
    assert repo.git("rev-parse", "main").stdout.strip() == main


def test_integration_reports_shallow_history_without_fetching(tmp_path) -> None:
    """This fails if absent shallow ancestry is reported as proven unrelated history."""
    origin = RepoBuilder.create(tmp_path / "origin")
    base = origin.git("rev-parse", "main").stdout.strip()
    origin.checkout_new("hypothesis/sensor", base)
    origin.write("knowledge/source.md", "Sensor observation\n")
    source = origin.commit("Record the source tip")
    origin.checkout("main")
    origin.write("knowledge/target.md", "Canonical observation\n")
    origin.commit("Record the target tip")
    clone = origin.clone_shallow_to(tmp_path / "shallow", depth=1)
    clone.git(
        "fetch",
        "--depth=1",
        "origin",
        f"{source}:refs/heads/hypothesis/sensor",
    )
    before = clone.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        clone.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY
    assert clone.git("rev-parse", "main").stdout.strip() == before


def test_integration_reports_a_missing_required_history_object(repo) -> None:
    """This fails if known missing ancestry is collapsed into a generic Git failure."""
    repo.write("knowledge/base.md", "Base observation\n")
    base = repo.commit("Record the shared base")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/source.md", "Sensor observation\n")
    repo.commit("Record the source tip")
    repo.checkout("main")
    repo.write("knowledge/target.md", "Canonical observation\n")
    before = repo.commit("Record the target tip")
    object_path = repo.path / ".git" / "objects" / base[:2] / base[2:]
    assert object_path.is_file()
    object_path.unlink()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.MISSING_OBJECT
    assert caught.value.evidence[0].oid == base
    assert repo.git("rev-parse", "main").stdout.strip() == before


@pytest.mark.parametrize(
    "attribute_text",
    (
        "knowledge/*.md merge=evil\n",
        "[attr]unsafe merge=evil\nknowledge/*.md unsafe\n",
        "knowledge/*.md filter=evil\n",
        "knowledge/*.md diff=evil\n",
        "knowledge/*.md working-tree-encoding=UTF-16\n",
    ),
)
def test_integration_rejects_unsafe_tree_attributes_before_helpers_run(
    repo, tmp_path, attribute_text
) -> None:
    """This fails if a tracked attribute or macro can invoke repository code."""
    marker = tmp_path / "helper-executed"
    helper = tmp_path / "evil-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$2" > "$1"')
    repo.git("config", "merge.evil.driver", f"{helper} %O %A %B %L %P")
    repo.git("config", "filter.evil.smudge", str(helper))
    repo.git("config", "diff.evil.command", str(helper))
    repo.write("knowledge/base.md", "Base\n")
    repo.commit("Record the attribute test path")
    repo.write(".gitattributes", attribute_text)
    repo.git("add", ".gitattributes")
    repo._commit("Declare integration attributes")
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("source-observation.md", "Sensor observation\n")
    repo.git("add", "source-observation.md")
    repo._commit("Record the source tip")
    repo.checkout("main")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert not marker.exists()
    assert _repository_state(repo) == before


def test_integration_rejects_unsafe_info_attributes_before_helpers_run(repo, tmp_path) -> None:
    """This fails if the highest-precedence info attributes escape exact-tree inspection."""
    _create_source(repo)
    marker = tmp_path / "info-helper-executed"
    helper = tmp_path / "info-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$2" > "$1"')
    repo.git("config", "merge.evil.driver", f"{helper} %O %A %B %L %P")
    attributes = repo.path / ".git" / "info" / "attributes"
    attributes.write_text("knowledge/*.md merge=evil\n", encoding="utf-8")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert not marker.exists()
    assert _repository_state(repo) == before


def test_integration_rejects_unsafe_global_attributes_before_helpers_run(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if a configured global attributes file is omitted from the threat model."""
    _create_source(repo)
    marker = tmp_path / "global-helper-executed"
    helper = tmp_path / "global-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$2" > "$1"')
    attributes = tmp_path / "global-attributes"
    attributes.write_text("knowledge/*.md merge=evil\n", encoding="utf-8")
    config = tmp_path / "global-config"
    config.write_text(
        "[core]\n"
        f"\tattributesFile = {attributes}\n"
        '[merge "evil"]\n'
        f"\tdriver = {helper} %O %A %B %L %P\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.fspath(config))
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert not marker.exists()
    assert _repository_state(repo) == before


def test_integration_accepts_the_explicit_builtin_union_driver(repo) -> None:
    """This fails if fail-closed driver checks reject Git's named union builtin."""
    repo.write(".gitattributes", "knowledge/observations.txt merge=union\n")
    repo.write("knowledge/observations.txt", "Baseline\n")
    base = repo.commit("Configure union observations")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/observations.txt", "Baseline\nSensor\n")
    repo.commit("Add the sensor observation")
    repo.checkout("main")
    repo.write("knowledge/observations.txt", "Baseline\nWeather\n")
    repo.commit("Add the weather observation")

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    merged = repo.git("show", "HEAD:knowledge/observations.txt").stdout
    assert "Sensor\n" in merged
    assert "Weather\n" in merged


def test_integration_ignores_a_caller_supplied_attribute_tree(repo, monkeypatch) -> None:
    """This fails if GIT_ATTR_SOURCE can replace either captured tree during validation."""
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record caller attribute baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", baseline.replace("Rule 2: base", "Rule 2: sensor"))
    source = repo.commit("Change source under exact tree attributes")
    repo.checkout("main")
    repo.write("knowledge/rules.md", baseline.replace("Rule 9: base", "Rule 9: canonical"))
    target = repo.commit("Change target under exact tree attributes")
    repo.checkout_new("attacker/attributes", target)
    repo.write(".gitattributes", "knowledge/rules.md merge=binary\n")
    repo.commit("Create an unrelated caller-selected attribute tree")
    repo.checkout("main")
    monkeypatch.setenv("GIT_ATTR_SOURCE", "refs/heads/attacker/attributes")

    result = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "main").stdout.strip()
    assert result.applied is True
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [
        target,
        source,
    ]


def test_integration_bypasses_repository_hooks_and_editors(repo, tmp_path) -> None:
    """This fails if merge, commit, hooks, or editors execute repository-provided programs."""
    _create_source(repo)
    marker = tmp_path / "repository-code-executed"
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    _write_executable(hook, f"touch '{marker}'\nexit 1")
    post_merge = repo.path / ".git" / "hooks" / "post-merge"
    _write_executable(post_merge, f"touch '{marker}'\nexit 1")
    editor = tmp_path / "evil-editor"
    _write_executable(editor, f"touch '{marker}'\nexit 1")
    repo.git("config", "core.editor", str(editor))

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()


def test_integration_enforces_bounded_path_enumeration_output(repo) -> None:
    """This fails if attribute path enumeration can exceed the shared byte budget."""
    repo.checkout_new("hypothesis/sensor")
    for index in range(40):
        repo.write(f"knowledge/path-{index:03d}.md", "Observation\n")
    repo.commit("Record many bounded paths")
    repo.checkout("main")
    limits = QueryLimits(max_output_bytes=512)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits=limits).integrate(_request())

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_integration_supports_sha256_object_ids_when_git_does(tmp_path) -> None:
    """This fails if strict merge parsing hard-codes SHA-1 object widths."""
    path = tmp_path / "sha256-repository"
    path.mkdir()
    initialized = subprocess.run(
        ["git", "init", "--object-format=sha256", "--initial-branch=main"],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if initialized.returncode != 0:
        pytest.skip("This Git build does not support SHA-256 repositories.")
    sha_repo = RepoBuilder(path=path.resolve(), _root=tmp_path.resolve())
    sha_repo._configure_fixture_repository()
    sha_repo._commit("Create an empty initial commit", allow_empty=True)
    source = _create_source(sha_repo)

    result = sha_repo.service().integrate(_request(), apply=True)

    commit = sha_repo.git("rev-parse", "main").stdout.strip()
    assert result.applied is True
    assert len(commit) == len(source) == 64
    assert sha_repo.git("show", "-s", "--format=%P", commit).stdout.strip().split()[1] == source


def test_integration_from_a_linked_target_worktree_preserves_the_sibling(repo, tmp_path) -> None:
    """This fails if per-worktree HEAD, index, or merge state is read from the common directory."""
    _create_source(repo)
    repo.checkout("hypothesis/sensor")
    sibling_head = repo.git("rev-parse", "HEAD").stdout.strip()
    linked_path = tmp_path / "target-worktree"
    repo.git("worktree", "add", str(linked_path), "main")
    linked = RepoBuilder(path=linked_path.resolve(), _root=tmp_path.resolve())

    result = linked.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert repo.git("rev-parse", "HEAD").stdout.strip() == sibling_head
    assert repo.git("status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert linked.git("status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_integration_changes_only_target_ref_and_merged_worktree_content(repo) -> None:
    """This fails if integration mutates notes, unrelated refs, or unrelated tracked files."""
    repo.write("outside.txt", "Unrelated tracked content\n")
    baseline = repo.commit("Record unrelated tracked content")
    repo.git("branch", "archive", baseline)
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Reviewed", baseline)
    _create_source(repo)
    refs_before = repo.git(
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads/archive",
        "refs/notes/review",
    ).stdout
    content_before = (repo.path / "outside.txt").read_bytes()

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert (
        repo.git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads/archive",
            "refs/notes/review",
        ).stdout
        == refs_before
    )
    assert (repo.path / "outside.txt").read_bytes() == content_before


def test_integration_apply_recomputes_after_an_earlier_preview(repo) -> None:
    """This fails if apply reuses a stale preview instead of capturing current branch tips."""
    first = _create_source(repo)
    preview = repo.service().integrate(_request())
    repo.checkout("hypothesis/sensor")
    repo.write("knowledge/later.md", "Later sensor evidence\n")
    second = repo.commit("Record later sensor evidence")
    repo.checkout("main")

    applied = repo.service().integrate(_request(), apply=True)

    parents = repo.git("show", "-s", "--format=%P", "main").stdout.strip().split()
    assert applied.applied is True
    assert parents[1] == second
    assert parents[1] != first
    assert (
        applied.evidence[0].details["proposed_tree"]
        != (preview.evidence[0].details["proposed_tree"])
    )


@pytest.mark.parametrize("raced_branch", ("source", "target"))
def test_integration_rejects_ref_races_during_prepare(repo, monkeypatch, raced_branch) -> None:
    """This fails if preview returns evidence for tips that no longer own their refs."""
    target, source = _prepare_clean_divergence(repo)
    original = IntegrationWriter._merge_tree
    raced: dict[str, str] = {}

    def race_after_tree(self, target_oid, source_oid):
        tree = original(self, target_oid, source_oid)
        if raced_branch == "source":
            raced["oid"] = _advance_ref(
                repo, "refs/heads/hypothesis/sensor", source, "Race the source ref"
            )
        else:
            raced["oid"] = _advance_ref(repo, "refs/heads/main", target, "Race the target ref")
        return tree

    monkeypatch.setattr(IntegrationWriter, "_merge_tree", race_after_tree)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request())

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    branch = "hypothesis/sensor" if raced_branch == "source" else "main"
    assert repo.git("rev-parse", branch).stdout.strip() == raced["oid"]


@pytest.mark.parametrize("boundary", ("mutation", "precommit"))
@pytest.mark.parametrize("raced_branch", ("source", "target"))
def test_integration_does_not_overwrite_ref_races_after_merge_preparation(
    repo, monkeypatch, boundary, raced_branch
) -> None:
    """This fails if an apply race is overwritten or reported as a verified rollback."""
    target, source = _prepare_clean_divergence(repo)
    method_name = (
        "_require_mutation_boundary" if boundary == "mutation" else "_require_precommit_state"
    )
    original = getattr(IntegrationWriter, method_name)
    raced: dict[str, str] = {}

    def race_at_boundary(self, prepared):
        if boundary == "mutation":
            original(self, prepared)
        if raced_branch == "source":
            raced["oid"] = _advance_ref(
                repo, prepared.source_ref, source, f"Race source at {boundary}"
            )
        else:
            raced["oid"] = _advance_ref(
                repo, prepared.target_ref, target, f"Race target at {boundary}"
            )
        if boundary == "precommit":
            return original(self, prepared)
        return None

    monkeypatch.setattr(IntegrationWriter, method_name, race_at_boundary)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    branch = "hypothesis/sensor" if raced_branch == "source" else "main"
    assert repo.git("rev-parse", branch).stdout.strip() == raced["oid"]


def test_integration_rechecks_refs_after_the_second_policy_validation(repo, monkeypatch) -> None:
    """This fails if a late source race can be committed after exact-tree validation."""
    target, source = _prepare_clean_divergence(repo)
    original = Policy.validate_tree
    validations = 0
    raced: dict[str, str] = {}

    def race_after_validation(self, tree, **kwargs):
        nonlocal validations
        result = original(self, tree, **kwargs)
        validations += 1
        if validations == 2:
            raced["oid"] = _advance_ref(
                repo,
                "refs/heads/hypothesis/sensor",
                source,
                "Race source after pre-commit policy validation",
            )
        return result

    monkeypatch.setattr(Policy, "validate_tree", race_after_validation)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert repo.git("rev-parse", "main").stdout.strip() == target
    assert repo.git("rev-parse", "hypothesis/sensor").stdout.strip() == raced["oid"]


@pytest.mark.parametrize("raced_branch", ("source", "target"))
def test_integration_reports_post_commit_ref_races_as_ambiguous_without_rewriting_them(
    repo, monkeypatch, raced_branch
) -> None:
    """This fails if verification rewrites a third state created after the commit."""
    _prepare_clean_divergence(repo)
    original = IntegrationWriter._capture_target_after_commit
    raced: dict[str, str] = {}

    def race_after_commit(self, prepared):
        integration_commit = original(self, prepared)
        if raced_branch == "source":
            raced["oid"] = _advance_ref(
                repo,
                prepared.source_ref,
                prepared.source_oid,
                "Race source after integration commit",
            )
        else:
            raced["oid"] = _advance_ref(
                repo,
                prepared.target_ref,
                integration_commit,
                "Race target after integration commit",
            )
        return integration_commit

    monkeypatch.setattr(IntegrationWriter, "_capture_target_after_commit", race_after_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    branch = "hypothesis/sensor" if raced_branch == "source" else "main"
    assert repo.git("rev-parse", branch).stdout.strip() == raced["oid"]


def test_integration_uses_immutable_attributes_after_a_clean_preview(repo, monkeypatch) -> None:
    """This fails if a late info attribute can replace the immutable merge authority."""
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    source_text = baseline.replace("Rule 2: base", "Rule 2: sensor")
    target_text = baseline.replace("Rule 9: base", "Rule 9: canonical")
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record merge race baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", source_text)
    repo.commit("Change the first rule")
    repo.checkout("main")
    repo.write("knowledge/rules.md", target_text)
    repo.commit("Change the second rule")
    original = IntegrationWriter._require_mutation_boundary

    def install_binary_attribute_after_scan(self, prepared):
        original(self, prepared)
        attributes = repo.path / ".git" / "info" / "attributes"
        attributes.write_text("knowledge/rules.md merge=binary\n", encoding="utf-8")

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_binary_attribute_after_scan
    )

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    merged = repo.git("show", "HEAD:knowledge/rules.md").stdout
    assert "Rule 2: sensor\n" in merged
    assert "Rule 9: canonical\n" in merged
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_prevents_a_raced_info_attribute_from_executing_a_helper(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if an attribute created after scanning can launch a merge driver."""
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    source_text = baseline.replace("Rule 2: base", "Rule 2: sensor")
    target_text = baseline.replace("Rule 9: base", "Rule 9: canonical")
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record helper race baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", source_text)
    repo.commit("Change the first rule")
    repo.checkout("main")
    repo.write("knowledge/rules.md", target_text)
    repo.commit("Change the second rule")
    marker = tmp_path / "raced-helper-executed"
    helper = tmp_path / "raced-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    repo.git("config", "merge.evil.driver", f"{helper} %O %A %B %L %P")
    original = IntegrationWriter._require_mutation_boundary

    def install_unsafe_attribute_after_scan(self, prepared):
        original(self, prepared)
        attributes = repo.path / ".git" / "info" / "attributes"
        attributes.write_text("knowledge/rules.md merge=evil\n", encoding="utf-8")

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_unsafe_attribute_after_scan
    )

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code in {ErrorCode.POLICY_VIOLATION, ErrorCode.GIT_FAILURE}
    assert not marker.exists()


def test_integration_isolates_helper_configuration_across_the_merge_transaction(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if a Git-config race can enter the closed merge authority."""
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    source_text = baseline.replace("Rule 2: base", "Rule 2: sensor")
    target_text = baseline.replace("Rule 9: base", "Rule 9: canonical")
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record configuration race baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", source_text)
    repo.commit("Change the source rule")
    repo.checkout("main")
    repo.write("knowledge/rules.md", target_text)
    repo.commit("Change the target rule")
    marker = tmp_path / "configuration-race-helper-executed"
    helper = tmp_path / "configuration-race-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    original = IntegrationWriter._require_mutation_boundary
    config_attempt: dict[str, int] = {}

    def race_configuration_after_scan(self, prepared):
        original(self, prepared)
        configured = repo.git(
            "config",
            "merge.evil.driver",
            f"{helper} %O %A %B %L %P",
            check=False,
        )
        config_attempt["returncode"] = configured.returncode
        attributes = repo.path / ".git" / "info" / "attributes"
        attributes.write_text("knowledge/rules.md merge=evil\n", encoding="utf-8")

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", race_configuration_after_scan
    )

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert config_attempt["returncode"] == 0
    assert not marker.exists()
    assert not (repo.path / ".git" / "config.lock").exists()
    merged = repo.git("show", "HEAD:knowledge/rules.md").stdout
    assert "Rule 2: sensor\n" in merged
    assert "Rule 9: canonical\n" in merged
    assert _raw_commit_message(repo) == _expected_integration_message()


def test_integration_aborts_and_preserves_the_original_precommit_error(repo, monkeypatch) -> None:
    """This fails if a verified successful abort hides the original managed-policy failure."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.POLICY_VIOLATION, "Injected pre-commit policy failure.")

    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert caught.value.message == "Injected pre-commit policy failure."
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


@pytest.mark.parametrize("abort_failure", ("result", "exception"))
def test_integration_reports_abort_failures_without_claiming_rollback(
    repo, monkeypatch, abort_failure
) -> None:
    """This fails if a failed or interrupted merge --abort is reported as clean rollback."""
    _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text

    def fail_abort(self, arguments, **kwargs):
        if arguments == ["merge", "--abort"]:
            if abort_failure == "exception":
                raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected abort interruption.")
            return GitOutput(stdout="", stderr="injected abort failure\n", returncode=1)
        return original_run_text(self, arguments, **kwargs)

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.POLICY_VIOLATION, "Injected pre-commit policy failure.")

    monkeypatch.setattr(GitRunner, "run_text", fail_abort)
    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert "merge --abort" in caught.value.recovery
    details = caught.value.evidence[0].details
    assert details["observed_head_ref"] == "refs/heads/main"
    assert details["observed_head_oid"]
    assert details["observed_target_oid"]
    assert details["observed_source_oid"]
    assert details["observed_status"] is not None
    assert details["observed_merge_state"]
    assert (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_detects_residue_after_a_successful_abort(repo, monkeypatch) -> None:
    """This fails if abort output is trusted without checking clean untracked state."""
    _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text

    def leave_residue_after_abort(self, arguments, **kwargs):
        output = original_run_text(self, arguments, **kwargs)
        if arguments == ["merge", "--abort"]:
            repo.write("abort-residue.txt", "Unexpected residue\n")
        return output

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.POLICY_VIOLATION, "Injected pre-commit policy failure.")

    monkeypatch.setattr(GitRunner, "run_text", leave_residue_after_abort)
    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert (repo.path / "abort-residue.txt").exists()
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_aborts_after_a_runner_exception_following_successful_merge(
    repo, monkeypatch
) -> None:
    """This fails if an exception after merge success leaves staged merge state behind."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    original_run_text = GitRunner.run_text
    injected = False

    def raise_after_merge(self, arguments, **kwargs):
        nonlocal injected
        output = original_run_text(self, arguments, **kwargs)
        if arguments and arguments[0] == "merge" and "--no-commit" in arguments and not injected:
            injected = True
            raise ManduaError(ErrorCode.GIT_FAILURE, "Injected post-merge exception.")
        return output

    monkeypatch.setattr(GitRunner, "run_text", raise_after_merge)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.message == "Injected post-merge exception."
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_aborts_post_merge_exception_after_the_shared_deadline_expires(
    repo, monkeypatch
) -> None:
    """This fails if deadline exhaustion prevents emergency abort-state inspection."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    original_run_text = GitRunner.run_text
    original_merge_check = IntegrationWriter._merge_is_in_progress
    injected = False
    expired = False

    def raise_after_merge(self, arguments, **kwargs):
        nonlocal injected
        output = original_run_text(self, arguments, **kwargs)
        if arguments and arguments[0] == "merge" and "--no-commit" in arguments and not injected:
            injected = True
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected post-merge timeout.")
        return output

    def expire_before_merge_state_check(self):
        nonlocal expired
        if not expired:
            expired = True
            self._deadline = 0.0
        return original_merge_check(self)

    monkeypatch.setattr(GitRunner, "run_text", raise_after_merge)
    monkeypatch.setattr(IntegrationWriter, "_merge_is_in_progress", expire_before_merge_state_check)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert caught.value.message == "Injected post-merge timeout."
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_reconciles_an_exception_after_successful_commit(repo, monkeypatch) -> None:
    """This fails if a proven exact commit is reported as failure after runner interruption."""
    target, source = _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text
    injected = False

    def raise_after_commit(self, arguments, **kwargs):
        nonlocal injected
        output = original_run_text(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            assert output.returncode == 0
            raise ManduaError(ErrorCode.GIT_FAILURE, "Injected post-commit exception.")
        return output

    monkeypatch.setattr(GitRunner, "run_text", raise_after_commit)

    result = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "main").stdout.strip()
    assert result.applied is True
    assert result.changes[0].after_oid == commit
    assert result.warnings
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [
        target,
        source,
    ]


def test_integration_reconciles_success_after_the_shared_deadline_expires(
    repo, monkeypatch
) -> None:
    """This fails if post-success proof incorrectly reuses the exhausted main deadline."""
    target, source = _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text
    original_reconcile = IntegrationWriter._reconcile_commit_exception
    injected = False

    def raise_after_commit(self, arguments, **kwargs):
        nonlocal injected
        output = original_run_text(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            assert output.returncode == 0
            raise ManduaError(ErrorCode.LIMIT_EXCEEDED, "Injected expired shared deadline.")
        return output

    def expire_before_reconciliation(self, prepared, original):
        self._deadline = 0.0
        return original_reconcile(self, prepared, original)

    monkeypatch.setattr(GitRunner, "run_text", raise_after_commit)
    monkeypatch.setattr(
        IntegrationWriter, "_reconcile_commit_exception", expire_before_reconciliation
    )

    result = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "main").stdout.strip()
    assert result.applied is True
    assert result.warnings
    assert result.changes[0].after_oid == commit
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [
        target,
        source,
    ]


@pytest.mark.parametrize(
    "malformed_kind",
    ("oid", "newline", "stderr"),
)
def test_integration_rejects_malformed_merge_tree_diagnostics(
    repo, monkeypatch, malformed_kind
) -> None:
    """This fails if merge-tree output is accepted without exact framing and diagnostics."""
    _create_source(repo)
    original_run = GitRunner.run

    def malformed_merge_tree(self, arguments, **kwargs):
        if arguments[:3] == ["merge-tree", "--write-tree", "--messages"]:
            if malformed_kind == "oid":
                return GitOutput(stdout=b"not-an-object-id\n", stderr=b"", returncode=0)
            if malformed_kind == "newline":
                return GitOutput(stdout=b"a" * 40, stderr=b"", returncode=0)
            return GitOutput(stdout=b"a" * 40 + b"\n", stderr=b"unexpected\n", returncode=0)
        return original_run(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run", malformed_merge_tree)
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert _repository_state(repo) == before


def test_integration_bounds_conflict_message_evidence(repo) -> None:
    """This fails if conflict diagnostics escape the configured evidence excerpt bound."""
    repo.write("knowledge/rules.md", "Threshold: 30%\n")
    base = repo.commit("Record the conflict baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", "Threshold: 35%\n")
    repo.commit("Record source conflict")
    repo.checkout("main")
    repo.write("knowledge/rules.md", "Threshold: 25%\n")
    repo.commit("Record target conflict")
    limits = QueryLimits(max_excerpt_chars=32)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits=limits).integrate(_request())

    assert caught.value.code is ErrorCode.CONFLICT
    assert caught.value.evidence[0].excerpt is not None
    assert len(caught.value.evidence[0].excerpt) <= 32


def test_integration_bounds_effective_attribute_output(repo) -> None:
    """This fails if many effective attributes can bypass the shared output ceiling."""
    attributes = "".join(
        f"knowledge/source.md attr{index:03d}=value{index:03d}\n" for index in range(100)
    )
    repo.write(".gitattributes", attributes)
    repo.commit("Declare many bounded attributes")
    _create_source(repo)
    limits = QueryLimits(max_output_bytes=1_024)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path, limits=limits).integrate(_request())

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_integration_preserves_raw_path_bytes_until_policy_fails_closed(repo) -> None:
    """This fails if non-UTF-8 Git paths are lossy-decoded before safe rejection."""
    repo.checkout_new("hypothesis/sensor")
    raw_relative = b"knowledge/raw-\xff.md"
    blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Raw path observation\n"
    ).stdout.strip()
    repo.git_bytes(
        "update-index",
        "-z",
        "--index-info",
        input_bytes=b"100644 " + blob + b"\t" + raw_relative + b"\x00",
    )
    repo._commit("Record a raw-byte path")
    repo.checkout("main")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert _repository_state(repo) == before


def test_integration_rejects_same_branch_tips(repo) -> None:
    """This fails if equal source and target tips can create an empty merge commit."""
    repo.git("branch", "hypothesis/sensor", "main")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert _repository_state(repo) == before


def test_integration_enforces_the_tree_configured_canonical_target(repo) -> None:
    """This fails if request.target overrides the repository's canonical branch policy."""
    repo.write(".mandua.toml", '[repository]\ncanonical_branch = "stable"\n')
    base = repo.commit("Configure stable as canonical")
    repo.git("branch", "stable", base)
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/source.md", "Sensor observation\n")
    repo.commit("Record the source tip")
    repo.checkout("main")
    before = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert repo.git("rev-parse", "main").stdout.strip() == before


def test_integration_rejects_a_target_policy_inconsistent_with_the_open_service(repo) -> None:
    """This fails if integration silently replaces the policy retained by MemoryService."""
    _create_source(repo)
    before = _repository_state(repo)
    repo.write(".mandua.toml", '[repository]\nnotes_ref = "refs/notes/worktree-review"\n')
    service = repo.service()

    with pytest.raises(ManduaError) as caught:
        service.integrate(_request())

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert caught.value.message == "The integration target policy differs from the service policy."
    assert _repository_state(repo)[:2] == before[:2]


def test_applied_policy_refreshes_same_service_reads_without_preview_or_failure_leaks(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This fails if a successful integration leaves the service on its old notes policy."""
    service = repo.service()
    original_policy = service.repository_policy
    repo.checkout_new("hypothesis/notes-policy")
    repo.write(
        ".mandua.toml",
        '[repository]\nnotes_ref = "refs/notes/integrated-review"\n',
    )
    repo.write("knowledge/integrated-policy.md", "Review this integrated policy.\n")
    reviewed_commit = repo.commit("Propose a dedicated integrated review ref")
    repo.checkout("main")
    request = _request(
        source="hypothesis/notes-policy",
        subject="Adopt the dedicated integrated review ref",
    )

    preview = service.integrate(request)

    assert preview.applied is False
    assert service.repository_policy == original_policy

    original_apply = IntegrationWriter._apply

    def fail_after_prepare(_writer, _prepared):
        raise ManduaError(ErrorCode.GIT_FAILURE, "Injected pre-apply failure.")

    monkeypatch.setattr(IntegrationWriter, "_apply", fail_after_prepare)
    with pytest.raises(ManduaError) as caught:
        service.integrate(request, apply=True)
    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert service.repository_policy == original_policy
    monkeypatch.setattr(IntegrationWriter, "_apply", original_apply)

    applied = service.integrate(request, apply=True)
    service.annotate(
        AnnotationRequest(
            revision=reviewed_commit,
            message="Review confirms the integrated notes policy.",
            agent_id="reviewer",
        ),
        apply=True,
    )
    same_service = service.why(
        path=PurePosixPath("knowledge/integrated-policy.md"),
        line=1,
    )
    fresh_service = MemoryService.open(repo.path).why(
        path=PurePosixPath("knowledge/integrated-policy.md"),
        line=1,
    )

    assert applied.applied is True
    assert service.repository_policy.notes_ref == "refs/notes/integrated-review"
    assert same_service == fresh_service
    assert any(
        item.kind == "review-note" and item.ref == "refs/notes/integrated-review"
        for item in same_service.evidence
    )


def test_applied_policy_refreshes_same_service_for_the_next_integration(repo) -> None:
    """This fails if a second integration compares its target to stale retained policy."""
    service = repo.service()
    repo.checkout_new("hypothesis/notes-policy")
    repo.write(
        ".mandua.toml",
        '[repository]\nnotes_ref = "refs/notes/integrated-review"\n',
    )
    repo.write("knowledge/integrated-policy.md", "Use the integrated review ref.\n")
    repo.commit("Propose a dedicated integrated review ref")
    repo.checkout("main")
    first_request = _request(
        source="hypothesis/notes-policy",
        subject="Adopt the dedicated integrated review ref",
    )

    first = service.integrate(first_request, apply=True)

    repo.checkout_new("hypothesis/follow-up")
    repo.write("knowledge/follow-up.md", "Retain the integrated policy.\n")
    repo.commit("Propose a follow-up under the integrated policy")
    repo.checkout("main")
    second_request = _request(
        source="hypothesis/follow-up",
        subject="Adopt the policy-aware follow-up",
    )
    same_service = service.integrate(second_request)
    fresh_service = MemoryService.open(repo.path).integrate(second_request)

    assert first.applied is True
    assert same_service == fresh_service


def test_integration_preview_isolates_local_helper_configuration_before_merge_tree(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if preview lets a raced local helper enter merge-tree authority."""
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record preview race baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", baseline.replace("Rule 2: base", "Rule 2: sensor"))
    repo.commit("Change the source rule")
    repo.checkout("main")
    repo.write("knowledge/rules.md", baseline.replace("Rule 9: base", "Rule 9: canonical"))
    repo.commit("Change the target rule")
    marker = tmp_path / "preview-race-helper-executed"
    helper = tmp_path / "preview-race-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    original = IntegrationWriter._merge_tree
    config_attempt: dict[str, int] = {}

    def race_preview_configuration(self, target_oid, source_oid):
        configured = repo.git(
            "config",
            "merge.evil.driver",
            f"{helper} %O %A %B %L %P",
            check=False,
        )
        config_attempt["returncode"] = configured.returncode
        attributes = repo.path / ".git" / "info" / "attributes"
        attributes.write_text("knowledge/rules.md merge=evil\n", encoding="utf-8")
        return original(self, target_oid, source_oid)

    monkeypatch.setattr(IntegrationWriter, "_merge_tree", race_preview_configuration)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request())

    assert caught.value.code in {ErrorCode.POLICY_VIOLATION, ErrorCode.GIT_FAILURE}
    assert config_attempt["returncode"] == 0
    assert not marker.exists()
    assert not (repo.path / ".git" / "config.lock").exists()


def test_integration_isolates_merge_from_a_raced_global_helper(repo, tmp_path, monkeypatch) -> None:
    """This fails if a global config race can enter the closed merge authority."""
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record global helper race baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", baseline.replace("Rule 2: base", "Rule 2: sensor"))
    repo.commit("Change the source rule")
    repo.checkout("main")
    repo.write("knowledge/rules.md", baseline.replace("Rule 9: base", "Rule 9: canonical"))
    repo.commit("Change the target rule")
    marker = tmp_path / "global-race-helper-executed"
    helper = tmp_path / "global-race-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    attributes = tmp_path / "global-race-attributes"
    attributes.write_text("knowledge/rules.md merge=evil\n", encoding="utf-8")
    config = tmp_path / "global-race-config"
    config.write_text(
        "[core]\n"
        f"\tattributesFile = {attributes}\n"
        '[merge "evil"]\n'
        f"\tdriver = {helper} %O %A %B %L %P\n",
        encoding="utf-8",
    )
    original = IntegrationWriter._require_mutation_boundary

    def race_global_configuration(self, prepared):
        original(self, prepared)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.fspath(config))

    monkeypatch.setattr(IntegrationWriter, "_require_mutation_boundary", race_global_configuration)
    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()
    merged = repo.git("show", "HEAD:knowledge/rules.md").stdout
    assert "Rule 2: sensor\n" in merged
    assert "Rule 9: canonical\n" in merged
    assert _raw_commit_message(repo) == _expected_integration_message()
    assert not (repo.path / ".git" / "config.lock").exists()


def test_integration_rejects_successful_merge_tree_with_unexpected_messages(
    repo, monkeypatch
) -> None:
    """This fails if success diagnostics after the exact tree OID are silently ignored."""
    _create_source(repo)
    original = GitRunner.run

    def add_success_message(self, arguments, **kwargs):
        output = original(self, arguments, **kwargs)
        if arguments[:3] == ["merge-tree", "--write-tree", "--messages"]:
            assert output.returncode == 0
            return GitOutput(
                stdout=output.stdout + b"unexpected merge-tree message\n",
                stderr=output.stderr,
                returncode=0,
            )
        return output

    monkeypatch.setattr(GitRunner, "run", add_success_message)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request())

    assert caught.value.code is ErrorCode.GIT_FAILURE


def test_integration_aborts_successful_merge_with_unexpected_stderr(repo, monkeypatch) -> None:
    """This fails if successful but anomalous merge diagnostics bypass rollback."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    original = GitRunner.run_text

    def add_merge_stderr(self, arguments, **kwargs):
        output = original(self, arguments, **kwargs)
        if arguments and arguments[0] == "merge" and "--no-commit" in arguments:
            assert output.returncode == 0
            return GitOutput(
                stdout=output.stdout,
                stderr="unexpected merge diagnostic\n",
                returncode=0,
            )
        return output

    monkeypatch.setattr(GitRunner, "run_text", add_merge_stderr)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert _repository_state(repo) == before


def test_integration_rejects_residual_merge_metadata_after_commit(repo, monkeypatch) -> None:
    """This fails if a non-MERGE_HEAD state file survives final verification."""
    _prepare_clean_divergence(repo)
    original = GitRunner.run_text

    def leave_merge_message(self, arguments, **kwargs):
        output = original(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and output.returncode == 0:
            (repo.path / ".git" / "MERGE_MSG").write_text(
                "Residual merge state\n", encoding="utf-8"
            )
        return output

    monkeypatch.setattr(GitRunner, "run_text", leave_merge_message)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None


def test_integration_reconciles_nonzero_output_after_a_successful_commit(repo, monkeypatch) -> None:
    """This fails if an exact committed post-state is mistaken for a failed rollback."""
    target, source = _prepare_clean_divergence(repo)
    original = GitRunner.run_text
    injected = False

    def report_failure_after_commit(self, arguments, **kwargs):
        nonlocal injected
        output = original(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            assert output.returncode == 0
            return GitOutput(
                stdout=output.stdout,
                stderr="injected post-success diagnostic\n",
                returncode=1,
            )
        return output

    monkeypatch.setattr(GitRunner, "run_text", report_failure_after_commit)

    result = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "main").stdout.strip()
    assert result.applied is True
    assert result.warnings
    assert result.changes[0].after_oid == commit
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [
        target,
        source,
    ]


def _prepare_ignored_collision(repo, *, source_path: str = "ignored/value.md") -> str:
    repo.write(".gitignore", "/ignored/\n")
    base = repo.commit("Ignore local integration artifacts")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write(source_path, "Tracked source value\n")
    repo.git("add", "-f", source_path)
    source = repo.git("commit", "--no-gpg-sign", "-m", "Add the tracked source value")
    assert source.returncode == 0
    repo.checkout("main")
    return repo.git("rev-parse", "hypothesis/sensor").stdout.strip()


def _write_raw_file(repository: Path, relative: bytes, contents: bytes) -> None:
    root = os.fsencode(repository)
    parent, _, _ = relative.rpartition(b"/")
    if parent:
        os.makedirs(root + b"/" + parent, exist_ok=True)
    descriptor = os.open(
        root + b"/" + relative,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(descriptor, contents)
    finally:
        os.close(descriptor)


def _expected_integration_message(subject: str = "Adopt sensor-based irrigation") -> str:
    return (
        f"{subject}\n\n"
        "Reason: The sensor hypothesis reduced water use.\n\n"
        "Memory-Type: integration\n"
        "Scope: irrigation\n"
        "Task-ID: TASK-IRR-7\n"
        "Decision-ID: DEC-IRR-1\n"
        "Agent-ID: gardener\n"
    )


def _raw_commit_message(repo, revision: str = "HEAD") -> str:
    raw = repo.git("cat-file", "commit", revision).stdout
    _, separator, message = raw.partition("\n\n")
    assert separator == "\n\n"
    return message


def _prepare_helper_race(repo) -> tuple[str, str]:
    baseline = "".join(f"Rule {index}: base\n" for index in range(1, 11))
    repo.write("knowledge/rules.md", baseline)
    base = repo.commit("Record closed-authority baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/rules.md", baseline.replace("Rule 2: base", "Rule 2: sensor"))
    source = repo.commit("Change the source rule")
    repo.checkout("main")
    repo.write("knowledge/rules.md", baseline.replace("Rule 9: base", "Rule 9: canonical"))
    target = repo.commit("Change the target rule")
    return target, source


def _append_custom_driver(config: Path, helper: Path) -> None:
    with config.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[merge "evil"]\n\tdriver = {helper} %O %A %B %L %P\n')


def test_integration_rejects_an_exact_ignored_file_collision_before_apply(repo) -> None:
    """This fails if a source addition overwrites an ignored local file."""
    _prepare_ignored_collision(repo)
    original = b"Private ignored value\n"
    _write_raw_file(repo.path, b"ignored/value.md", original)
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert (repo.path / "ignored/value.md").read_bytes() == original
    assert _repository_state(repo) == before


@pytest.mark.parametrize(
    ("source_path", "ignored_path"),
    (
        ("ignored/new/item.md", b"ignored/new"),
        ("ignored/new", b"ignored/new/local.bin"),
    ),
)
def test_integration_rejects_ignored_file_directory_prefix_collisions(
    repo, source_path, ignored_path
) -> None:
    """This fails if file/directory prefix collisions can destroy ignored content."""
    _prepare_ignored_collision(repo, source_path=source_path)
    original = b"Prefix collision value\n"
    _write_raw_file(repo.path, ignored_path, original)
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    descriptor = os.open(os.fsencode(repo.path) + b"/" + ignored_path, os.O_RDONLY)
    try:
        assert os.read(descriptor, len(original) + 1) == original
    finally:
        os.close(descriptor)
    assert _repository_state(repo) == before


def test_integration_never_claims_rollback_after_an_ignored_collision_loss(
    repo, monkeypatch
) -> None:
    """This fails if refs/status hide ignored data loss after merge --abort."""
    _prepare_ignored_collision(repo)
    original = b"Ignored value that must survive abort\n"
    _write_raw_file(repo.path, b"ignored/value.md", original)

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.CONFLICT, "Injected pre-commit failure.")

    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.message != "Injected pre-commit failure."
    assert (repo.path / "ignored/value.md").read_bytes() == original


def test_integration_preserves_arbitrary_bytes_in_ignored_collision_detection(repo) -> None:
    """This fails if ignored-path inventory decodes raw Git path bytes lossily."""
    repo.write(".gitignore", "/ignored/\n")
    base = repo.commit("Ignore raw local artifacts")
    repo.checkout_new("hypothesis/sensor", base)
    raw_path = b"ignored/raw-\n.md"
    blob = repo.git_bytes(
        "hash-object", "-w", "--stdin", input_bytes=b"Tracked raw source value\n"
    ).stdout.strip()
    repo.git_bytes(
        "update-index",
        "-z",
        "--index-info",
        input_bytes=b"100644 " + blob + b"\t" + raw_path + b"\x00",
    )
    repo._commit("Add a raw-byte source path")
    repo.checkout("main")
    original = b"Private raw ignored value\n"
    _write_raw_file(repo.path, raw_path, original)
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    descriptor = os.open(os.fsencode(repo.path) + b"/" + raw_path, os.O_RDONLY)
    try:
        assert os.read(descriptor, len(original) + 1) == original
    finally:
        os.close(descriptor)
    assert _repository_state(repo) == before


def test_integration_bounds_relevant_ignored_file_fingerprinting(repo) -> None:
    """This fails if hashing an ignored collision can exceed the operation byte bound."""
    _prepare_ignored_collision(repo)
    _write_raw_file(repo.path, b"ignored/value.md", b"x" * 1_048_577)
    before = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert repo.git("rev-parse", "main").stdout.strip() == before
    assert (repo.path / "ignored/value.md").stat().st_size == 1_048_577


def test_integration_rechecks_ignored_collisions_at_the_final_mutation_boundary(
    repo, monkeypatch
) -> None:
    """This fails if an ignored file raced after prepare can reach checkout mutation."""
    _prepare_ignored_collision(repo)
    original_boundary = IntegrationWriter._require_mutation_boundary
    raced = b"Raced ignored value\n"

    def install_collision_before_final_boundary(self, prepared):
        result = original_boundary(self, prepared)
        _write_raw_file(repo.path, b"ignored/value.md", raced)
        return result

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_collision_before_final_boundary
    )
    before = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert (repo.path / "ignored/value.md").read_bytes() == raced
    assert repo.git("rev-parse", "main").stdout.strip() == before


def test_integration_preview_never_executes_a_raced_local_info_merge_driver(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if merge-tree reads mutable repository config or info attributes."""
    _prepare_helper_race(repo)
    marker = tmp_path / "preview-local-helper-executed"
    helper = tmp_path / "preview-local-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    original_merge_tree = IntegrationWriter._merge_tree

    def install_helper_before_merge_tree(self, target_oid, source_oid):
        _append_custom_driver(repo.path / ".git" / "config", helper)
        (repo.path / ".git" / "info" / "attributes").write_text(
            "knowledge/rules.md merge=evil\n", encoding="utf-8"
        )
        return original_merge_tree(self, target_oid, source_oid)

    monkeypatch.setattr(IntegrationWriter, "_merge_tree", install_helper_before_merge_tree)

    with pytest.raises(ManduaError):
        repo.service().integrate(_request())

    assert not marker.exists()


def test_integration_apply_never_executes_a_raced_local_info_merge_driver(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if current-worktree merge reads config/info after the final scan."""
    _prepare_helper_race(repo)
    marker = tmp_path / "apply-local-helper-executed"
    helper = tmp_path / "apply-local-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    original_boundary = IntegrationWriter._require_mutation_boundary

    def install_helper_after_final_scan(self, prepared):
        result = original_boundary(self, prepared)
        _append_custom_driver(repo.path / ".git" / "config", helper)
        (repo.path / ".git" / "info" / "attributes").write_text(
            "knowledge/rules.md merge=evil\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_helper_after_final_scan
    )

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()
    merged = repo.git("show", "HEAD:knowledge/rules.md").stdout
    assert "Rule 2: sensor\n" in merged
    assert "Rule 9: canonical\n" in merged


def test_integration_apply_never_executes_a_raced_included_merge_driver(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if a late local include becomes sensitive-command authority."""
    _prepare_helper_race(repo)
    marker = tmp_path / "included-helper-executed"
    helper = tmp_path / "included-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    included = tmp_path / "included-config"
    included.write_text(f'[merge "evil"]\n\tdriver = {helper} %O %A %B %L %P\n', encoding="utf-8")
    original_boundary = IntegrationWriter._require_mutation_boundary

    def install_include_after_final_scan(self, prepared):
        result = original_boundary(self, prepared)
        with (repo.path / ".git" / "config").open("a", encoding="utf-8") as stream:
            stream.write(f"\n[include]\n\tpath = {included}\n")
        (repo.path / ".git" / "info" / "attributes").write_text(
            "knowledge/rules.md merge=evil\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_include_after_final_scan
    )

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()


def test_integration_apply_never_executes_raced_worktree_configuration(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if linked-worktree config becomes merge authority after validation."""
    _prepare_helper_race(repo)
    repo.git("config", "extensions.worktreeConfig", "true")
    repo.checkout("hypothesis/sensor")
    linked_path = tmp_path / "closed-authority-target"
    repo.git("worktree", "add", str(linked_path), "main")
    linked = RepoBuilder(path=linked_path.resolve(), _root=tmp_path.resolve())
    marker = tmp_path / "worktree-helper-executed"
    helper = tmp_path / "worktree-helper"
    _write_executable(helper, f'touch \'{marker}\'\ncat "$3" > "$2"')
    original_boundary = IntegrationWriter._require_mutation_boundary

    def install_worktree_helper_after_scan(self, prepared):
        result = original_boundary(self, prepared)
        config_path = Path(
            linked.git(
                "rev-parse", "--path-format=absolute", "--git-path", "config.worktree"
            ).stdout.strip()
        )
        _append_custom_driver(config_path, helper)
        common = Path(linked.git("rev-parse", "--git-common-dir").stdout.strip()).resolve()
        (common / "info" / "attributes").write_text(
            "knowledge/rules.md merge=evil\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_worktree_helper_after_scan
    )

    result = linked.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()


def _install_alternate_only_source(repo, tmp_path) -> Path:
    alternate = repo.clone_to(tmp_path / "alternate-source")
    alternate.checkout_new("hypothesis/sensor", "main")
    alternate.write("knowledge/alternate.md", "Alternate-only observation\n")
    source = alternate.commit("Record the alternate-only source")
    info = repo.path / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{alternate.path / '.git' / 'objects'}\n", encoding="utf-8")
    repo.git("update-ref", "refs/heads/hypothesis/sensor", source)
    return alternate.path


def test_integration_rejects_an_alternate_only_source_before_revision_resolution(
    repo, tmp_path
) -> None:
    """This fails if a committed integration can depend on a disposable alternate store."""
    _install_alternate_only_source(repo, tmp_path)
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY
    assert _repository_state(repo) == before


@pytest.mark.parametrize(
    ("name", "symlink"), (("alternates", False), ("alternates", True), ("http-alternates", False))
)
def test_integration_rejects_any_repository_alternate_authority(
    repo, tmp_path, name, symlink
) -> None:
    """This fails if empty, malformed, HTTP, or symlinked alternate metadata is trusted."""
    _create_source(repo)
    info = repo.path / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    path = info / name
    if symlink:
        external = tmp_path / "external-alternates"
        external.write_text("malformed alternate\n", encoding="utf-8")
        path.symlink_to(external)
    else:
        path.write_text(
            "" if name == "alternates" else "https://invalid.example/objects\n", encoding="utf-8"
        )
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY
    assert _repository_state(repo) == before


def test_integration_rechecks_alternates_at_the_final_mutation_boundary(repo, monkeypatch) -> None:
    """This fails if alternate metadata can appear after exact revisions were captured."""
    _create_source(repo)
    original_boundary = IntegrationWriter._require_mutation_boundary

    def install_alternate_before_final_boundary(self, prepared):
        result = original_boundary(self, prepared)
        info = repo.path / ".git" / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_alternate_before_final_boundary
    )
    before = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.INCOMPLETE_HISTORY
    assert repo.git("rev-parse", "main").stdout.strip() == before


@pytest.mark.parametrize(
    ("subject", "comment_character"),
    (("Adopt sensor-based irrigation", "M"), ("# Preserve comment-like subject", "#")),
)
def test_integration_commits_the_complete_canonical_message_verbatim(
    repo, subject, comment_character
) -> None:
    """This fails if local cleanup/comment config removes canonical message bytes."""
    _create_source(repo)
    repo.git("config", "commit.cleanup", "strip")
    repo.git("config", "core.commentChar", comment_character)

    result = repo.service().integrate(_request(subject=subject), apply=True)

    assert result.applied is True
    assert _raw_commit_message(repo) == _expected_integration_message(subject)


@pytest.mark.parametrize("outcome", ("success", "nonzero", "exception"))
def test_integration_never_reports_applied_for_a_commit_with_the_wrong_raw_message(
    repo, monkeypatch, outcome
) -> None:
    """This fails if ordinary or emergency verification omits exact commit-message bytes."""
    _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text
    injected = False

    def commit_the_wrong_message(self, arguments, **kwargs):
        nonlocal injected
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            kwargs["input_bytes"] = b"Tampered integration message\n"
            output = original_run_text(self, arguments, **kwargs)
            assert output.returncode == 0
            if outcome == "nonzero":
                return GitOutput(stdout=output.stdout, stderr="injected nonzero\n", returncode=1)
            if outcome == "exception":
                raise ManduaError(ErrorCode.GIT_FAILURE, "Injected post-commit exception.")
            return output
        return original_run_text(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", commit_the_wrong_message)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert _raw_commit_message(repo) == "Tampered integration message\n"


def test_integration_preview_never_creates_repository_configuration_locks(
    repo, monkeypatch
) -> None:
    """This fails if a read-only preview transiently writes config.lock files."""
    _create_source(repo)
    git_directory = Path(repo.git("rev-parse", "--absolute-git-dir").stdout.strip())
    config = git_directory / "config"
    unrelated_lock = git_directory / "mandua-unrelated.lock"
    unrelated_lock.write_bytes(b"unrelated lock contents\n")
    before = {
        "HEAD": (git_directory / "HEAD").read_bytes(),
        "index": (git_directory / "index").read_bytes(),
        "config": config.read_bytes(),
        "unrelated": unrelated_lock.read_bytes(),
    }
    original_merge_tree = IntegrationWriter._merge_tree

    def inspect_preview_admin_state(self, target_oid, source_oid):
        assert not (git_directory / "config.lock").exists()
        assert not (git_directory / "config.worktree.lock").exists()
        return original_merge_tree(self, target_oid, source_oid)

    monkeypatch.setattr(IntegrationWriter, "_merge_tree", inspect_preview_admin_state)

    result = repo.service().integrate(_request())

    assert result.applied is False
    assert (git_directory / "HEAD").read_bytes() == before["HEAD"]
    assert (git_directory / "index").read_bytes() == before["index"]
    assert config.read_bytes() == before["config"]
    assert unrelated_lock.read_bytes() == before["unrelated"]


def test_integration_reconciles_resource_cleanup_failure_after_an_exact_commit(
    repo, monkeypatch
) -> None:
    """This fails if temporary cleanup masks an independently provable applied commit."""
    target, source = _prepare_clean_divergence(repo)

    def cleanup_then_fail(self, resource):
        resource.cleanup()
        raise ManduaError(ErrorCode.GIT_FAILURE, "Injected authority cleanup failure.")

    monkeypatch.setattr(
        IntegrationWriter, "_release_operation_resources", cleanup_then_fail, raising=False
    )

    result = repo.service().integrate(_request(), apply=True)

    commit = repo.git("rev-parse", "main").stdout.strip()
    assert result.applied is True
    assert result.warnings
    assert repo.git("show", "-s", "--format=%P", commit).stdout.strip().split() == [
        target,
        source,
    ]


def test_integration_capability_probes_share_the_operation_process_budget(
    repo, monkeypatch
) -> None:
    """This fails if capability detection starts an independent process allowance."""
    _create_source(repo)
    before = _repository_state(repo)
    monkeypatch.setattr(integration_module, "_MAIN_MAX_GIT_PROCESSES", 1)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path).integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert _repository_state(repo) == before


def test_integration_aggregates_git_output_across_small_commands_and_policy_runners(
    repo, monkeypatch
) -> None:
    """This fails if each runner and subprocess receives a fresh output allowance."""
    _create_source(repo)
    before = _repository_state(repo)
    monkeypatch.setattr(integration_module, "_MAIN_MAX_GIT_OUTPUT_BYTES", 512)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path).integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert _repository_state(repo) == before


def test_integration_aggregates_git_stdin_across_attribute_policy_and_commit_calls(
    repo, monkeypatch
) -> None:
    """This fails if individually-small Git stdin payloads each receive a fresh allowance."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    monkeypatch.setattr(integration_module, "_MAIN_MAX_GIT_INPUT_BYTES", 256)

    with pytest.raises(ManduaError) as caught:
        MemoryService.open(repo.path).integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED
    assert _repository_state(repo) == before


def test_integration_emergency_abort_has_a_finite_process_budget(repo, monkeypatch) -> None:
    """This fails if rollback silently receives an unlimited fresh process allowance."""
    _prepare_clean_divergence(repo)
    target = repo.git("rev-parse", "main").stdout.strip()

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.CONFLICT, "Injected pre-commit failure.")

    monkeypatch.setattr(integration_module, "_EMERGENCY_MAX_PROCESSES", 1, raising=False)
    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert "merge --abort" in caught.value.recovery
    assert repo.git("rev-parse", "main").stdout.strip() == target


def test_integration_emergency_reconciliation_has_a_finite_process_budget(
    repo, monkeypatch
) -> None:
    """This fails if post-success proof can bypass its own finite process allowance."""
    _prepare_clean_divergence(repo)
    target = repo.git("rev-parse", "main").stdout.strip()
    original = GitRunner.run_text
    injected = False

    def raise_after_commit(self, arguments, **kwargs):
        nonlocal injected
        output = original(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            assert output.returncode == 0
            raise ManduaError(ErrorCode.GIT_FAILURE, "Injected post-commit exception.")
        return output

    monkeypatch.setattr(integration_module, "_EMERGENCY_MAX_PROCESSES", 1, raising=False)
    monkeypatch.setattr(GitRunner, "run_text", raise_after_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert repo.git("rev-parse", "main").stdout.strip() != target


def _loose_object_path(repo, object_id: str) -> Path:
    objects = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-path", "objects").stdout.strip()
    )
    return objects / object_id[:2] / object_id[2:]


@pytest.mark.parametrize("missing_kind", ("source-parent", "source-blob"))
@pytest.mark.parametrize("outcome", ("success", "nonzero", "exception"))
def test_integration_never_reports_applied_with_an_incomplete_reachable_closure(
    repo, tmp_path, monkeypatch, missing_kind, outcome
) -> None:
    """This fails if final proof checks only the new commit and its header."""
    _prepare_clean_divergence(repo)
    source = repo.git("rev-parse", "hypothesis/sensor").stdout.strip()
    missing_oid = (
        source
        if missing_kind == "source-parent"
        else repo.git("rev-parse", "hypothesis/sensor:knowledge/source.md").stdout.strip()
    )
    primary_object = _loose_object_path(repo, missing_oid)
    assert primary_object.is_file()
    alternate = repo.clone_to(tmp_path / f"closure-{missing_kind}-{outcome}")
    alternate_objects = alternate.path / ".git" / "objects"
    alternates = repo.path / ".git" / "objects" / "info" / "alternates"
    original = GitRunner.run_text
    injected = False

    def remove_reachable_object_around_commit(self, arguments, **kwargs):
        nonlocal injected
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(f"{alternate_objects}\n", encoding="utf-8")
            primary_object.unlink()
            output = original(self, arguments, **kwargs)
            assert output.returncode == 0
            alternates.unlink()
            if outcome == "nonzero":
                return GitOutput(stdout=output.stdout, stderr="injected nonzero\n", returncode=1)
            if outcome == "exception":
                raise ManduaError(ErrorCode.GIT_FAILURE, "Injected post-commit exception.")
            return output
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", remove_reachable_object_around_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code in {ErrorCode.GIT_FAILURE, ErrorCode.MISSING_OBJECT}
    assert caught.value.recovery is not None
    assert not alternates.exists()
    assert repo.git("fsck", "--connectivity-only", check=False).returncode != 0


def _append_filter_driver(config: Path, helper: Path) -> None:
    with config.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[filter "evil"]\n\tclean = {helper}\n')


@pytest.mark.parametrize("configuration_source", ("repository", "global", "worktree"))
def test_integration_status_uses_closed_late_helper_authority(
    repo, tmp_path, monkeypatch, configuration_source
) -> None:
    """This fails if a pre-merge status call reads mutable helper authority."""
    _prepare_helper_race(repo)
    marker = tmp_path / f"status-{configuration_source}-helper-executed"
    helper = tmp_path / f"status-{configuration_source}-helper"
    _write_executable(helper, f"touch '{marker}'\ncat")
    if configuration_source == "repository":
        config = repo.path / ".git" / "config"
    elif configuration_source == "global":
        config = tmp_path / "late-global-config"
        config.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    else:
        repo.git("config", "extensions.worktreeConfig", "true")
        config = Path(
            repo.git(
                "rev-parse", "--path-format=absolute", "--git-path", "config.worktree"
            ).stdout.strip()
        )
        config.write_text("", encoding="utf-8")
    original_boundary = IntegrationWriter._require_mutation_boundary

    def install_filter_after_final_scan(self, prepared):
        result = original_boundary(self, prepared)
        _append_filter_driver(config, helper)
        (repo.path / ".git" / "info" / "attributes").write_text(
            "knowledge/rules.md filter=evil\n", encoding="utf-8"
        )
        os.utime(repo.path / "knowledge" / "rules.md", None)
        return result

    monkeypatch.setattr(
        IntegrationWriter, "_require_mutation_boundary", install_filter_after_final_scan
    )

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()


@pytest.mark.parametrize("phase", ("abort", "final", "emergency"))
def test_integration_status_remains_closed_during_failure_and_final_proof(
    repo, tmp_path, monkeypatch, phase
) -> None:
    """This fails if abort or post-commit proof executes a raced clean filter."""
    _prepare_helper_race(repo)
    marker = tmp_path / f"status-{phase}-helper-executed"
    helper = tmp_path / f"status-{phase}-helper"
    _write_executable(helper, f"touch '{marker}'\ncat")
    installed = False

    def install_filter() -> None:
        nonlocal installed
        if installed:
            return
        installed = True
        _append_filter_driver(repo.path / ".git" / "config", helper)
        (repo.path / ".git" / "info" / "attributes").write_text(
            "knowledge/rules.md filter=evil\n", encoding="utf-8"
        )
        os.utime(repo.path / "knowledge" / "rules.md", None)

    if phase == "abort":

        def fail_before_commit(self, prepared):
            install_filter()
            raise ManduaError(ErrorCode.CONFLICT, "Injected pre-commit failure.")

        monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)
        with pytest.raises(ManduaError):
            repo.service().integrate(_request(), apply=True)
    else:
        original = GitRunner.run_text

        def install_after_commit(self, arguments, **kwargs):
            output = original(self, arguments, **kwargs)
            if arguments[:2] == ["commit", "--no-verify"] and not installed:
                install_filter()
                if phase == "emergency":
                    raise ManduaError(ErrorCode.GIT_FAILURE, "Injected post-commit exception.")
            return output

        monkeypatch.setattr(GitRunner, "run_text", install_after_commit)
        result = repo.service().integrate(_request(), apply=True)
        assert result.applied is True

    assert not marker.exists()


@pytest.mark.parametrize(
    "macro_definition",
    (
        "[attr]unsafe filter=evil",
        "[attr]unsafe diff=evil",
        "[attr]unsafe working-tree-encoding=UTF-16",
        "[attr]unsafe merge=evil",
    ),
)
def test_integration_rejects_unsafe_attributes_composed_only_in_the_proposed_tree(
    repo, macro_definition
) -> None:
    """This fails if only the two parent trees receive effective-attribute scans."""
    repo.write("knowledge/rules.md", "Baseline rule\n")
    base = repo.commit("Record the attribute composition baseline")
    repo.checkout_new("hypothesis/sensor", base)
    repo.write("knowledge/.gitattributes", "rules.md unsafe\n")
    repo.commit("Use a locally harmless attribute macro")
    repo.checkout("main")
    repo.write(".gitattributes", f"{macro_definition}\n")
    repo.commit("Define a locally unused attribute macro")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request())

    assert caught.value.code is ErrorCode.POLICY_VIOLATION
    assert _repository_state(repo) == before


@pytest.mark.parametrize(
    "trace_variable",
    ("GIT_TRACE", "GIT_TRACE_SETUP", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"),
)
def test_integration_preview_strips_every_path_writing_git_trace_family_variable(
    repo, monkeypatch, trace_variable
) -> None:
    """This fails if caller tracing can write during a mutation-free preview."""
    _create_source(repo)
    trace_path = repo.path / ".git" / f"mandua-{trace_variable.lower()}"
    before = _repository_state(repo)
    monkeypatch.setenv(trace_variable, str(trace_path))

    result = repo.service().integrate(_request())

    assert result.applied is False
    assert not trace_path.exists()
    monkeypatch.delenv(trace_variable)
    assert _repository_state(repo) == before


@pytest.mark.parametrize("entry_name", ("packed-refs", "shallow", "worktrees"))
def test_integration_rejects_indirect_optional_administrative_projection_entries(
    repo, tmp_path, entry_name
) -> None:
    """This fails if a private authority follows optional administrative symlinks."""
    _create_source(repo)
    common = Path(
        repo.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    )
    source = common / entry_name
    external = tmp_path / f"external-{entry_name}"
    if entry_name == "packed-refs":
        repo.git("pack-refs", "--all")
        source.rename(external)
    elif entry_name == "shallow":
        external.write_text(
            f"{repo.git('rev-list', '--max-parents=0', 'HEAD').stdout.strip()}\n",
            encoding="ascii",
        )
    else:
        external.mkdir()
    source.symlink_to(external, target_is_directory=entry_name == "worktrees")
    before = _repository_state(repo)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request())

    assert caught.value.code in {ErrorCode.GIT_FAILURE, ErrorCode.INCOMPLETE_HISTORY}
    assert _repository_state(repo) == before


@pytest.mark.parametrize("apply", (False, True))
def test_integration_supports_valid_repositories_without_reflog_administration(repo, apply) -> None:
    """This fails if an absent logs directory is mistaken for repository corruption."""
    _create_source(repo)
    repo.git("config", "core.logAllRefUpdates", "false")
    logs = repo.path / ".git" / "logs"
    shutil.rmtree(logs)
    before = _repository_state(repo)

    result = repo.service().integrate(_request(), apply=apply)

    assert result.applied is apply
    assert not logs.exists()
    if not apply:
        assert _repository_state(repo) == before


def test_integration_ignores_large_unrelated_ignored_inventories(repo) -> None:
    """This fails if irrelevant ignored paths consume the collision-path count bound."""
    repo.write(".gitignore", "/local-cache/\n")
    repo.commit("Ignore unrelated local cache entries")
    _create_source(repo)
    cache = repo.path / "local-cache"
    cache.mkdir()
    for index in range(4_097):
        (cache / f"entry-{index:04d}").write_bytes(b"unrelated\n")

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert len(tuple(cache.iterdir())) == 4_097


def test_integration_base_exception_after_staged_merge_aborts_exactly(repo, monkeypatch) -> None:
    """This fails if non-Mandua control flow bypasses pre-commit rollback proof."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    failure = _InjectedIntegrationControl("injected after staged merge")

    def interrupt_after_merge(self, prepared):
        raise failure

    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", interrupt_after_merge)

    with pytest.raises(_InjectedIntegrationControl) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value is failure
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_proves_exact_pre_state_after_merge_process_never_starts(
    repo, monkeypatch
) -> None:
    """This fails if no-process-start control flow escapes without bounded state proof."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    failure = _InjectedIntegrationControl("injected merge process-start abort")
    original_popen = git_runner_module.subprocess.Popen
    original_proof = IntegrationWriter._prove_exact_pre_state_emergency
    proof_calls = 0
    injected = False

    def interrupt_merge_process_start(command, *args, **kwargs):
        nonlocal injected
        if "merge" in command and "--no-commit" in command and not injected:
            injected = True
            raise failure
        return original_popen(command, *args, **kwargs)

    def record_pre_state_proof(self, prepared, deadline):
        nonlocal proof_calls
        proof_calls += 1
        return original_proof(self, prepared, deadline)

    monkeypatch.setattr(git_runner_module.subprocess, "Popen", interrupt_merge_process_start)
    monkeypatch.setattr(
        IntegrationWriter, "_prove_exact_pre_state_emergency", record_pre_state_proof
    )

    with pytest.raises(_InjectedIntegrationControl) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value is failure
    assert proof_calls == 1
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_never_preserves_an_interruption_with_dirty_state_and_no_merge_marker(
    repo, monkeypatch
) -> None:
    """This fails if marker-free staged state bypasses abort and rollback verification."""
    _prepare_clean_divergence(repo)
    source = repo.git("rev-parse", "refs/heads/hypothesis/sensor").stdout.strip()
    failure = _InjectedIntegrationControl("injected marker-free staged state")
    original_run_text = GitRunner.run_text
    injected = False

    def dirty_index_without_starting_merge(self, arguments, **kwargs):
        nonlocal injected
        if arguments[:2] == ["merge", "--no-ff"] and not injected:
            injected = True
            repo.git("read-tree", source)
            assert not (repo.path / ".git" / "MERGE_HEAD").exists()
            assert repo.git("status", "--porcelain=v1").stdout
            raise failure
        return original_run_text(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", dirty_index_without_starting_merge)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert "merge --abort" in caught.value.recovery
    assert caught.value.__cause__ is failure
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()
    assert repo.git("status", "--porcelain=v1").stdout


def test_integration_base_exception_before_commit_process_rolls_back_exactly(
    repo, monkeypatch
) -> None:
    """This fails if a commit-attempt interruption leaves the staged merge behind."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    failure = _InjectedIntegrationControl("injected before commit process start")
    original_run_text = GitRunner.run_text

    def interrupt_before_commit(self, arguments, **kwargs):
        if arguments[:2] == ["commit", "--no-verify"]:
            raise failure
        return original_run_text(self, arguments, **kwargs)

    monkeypatch.setattr(GitRunner, "run_text", interrupt_before_commit)

    with pytest.raises(_InjectedIntegrationControl) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value is failure
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_base_exception_after_real_commit_classifies_ambiguous_refs(
    repo, monkeypatch
) -> None:
    """This fails if post-commit control flow escapes before exact ref classification."""
    _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text
    failure = _InjectedIntegrationControl("injected after real commit")
    injected = False

    def interrupt_after_commit(self, arguments, **kwargs):
        nonlocal injected
        output = original_run_text(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and not injected:
            injected = True
            source = repo.git("rev-parse", "refs/heads/hypothesis/sensor").stdout.strip()
            _advance_ref(repo, "refs/heads/hypothesis/sensor", source, "Race the source ref")
            raise failure
        return output

    monkeypatch.setattr(GitRunner, "run_text", interrupt_after_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert caught.value.__cause__ is failure


def test_integration_base_exception_during_final_verification_is_classified(
    repo, monkeypatch
) -> None:
    """This fails if final verification control flow leaves committed state unclassified."""
    _prepare_clean_divergence(repo)
    original_metadata = IntegrationWriter._commit_metadata
    failure = _InjectedIntegrationControl("injected during final verification")
    injected = False

    def interrupt_metadata(self, commit_oid, **kwargs):
        nonlocal injected
        metadata = original_metadata(self, commit_oid, **kwargs)
        if not injected:
            injected = True
            source = repo.git("rev-parse", "refs/heads/hypothesis/sensor").stdout.strip()
            _advance_ref(repo, "refs/heads/hypothesis/sensor", source, "Race final verification")
            raise failure
        return metadata

    monkeypatch.setattr(IntegrationWriter, "_commit_metadata", interrupt_metadata)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert caught.value.__cause__ is failure


def test_integration_base_exception_during_abort_proof_gets_one_fresh_observation(
    repo, monkeypatch
) -> None:
    """This fails if one interrupted rollback proof cannot establish exact pre-state afresh."""
    _prepare_clean_divergence(repo)
    before = _repository_state(repo)
    failure = _InjectedIntegrationControl("injected during abort inspection")
    original_capture = IntegrationWriter._capture_ref_emergency
    injected = False

    def interrupt_abort_observation(self, branch_ref, deadline):
        nonlocal injected
        if not injected:
            injected = True
            raise failure
        return original_capture(self, branch_ref, deadline)

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.CONFLICT, "Injected pre-commit failure.")

    monkeypatch.setattr(IntegrationWriter, "_capture_ref_emergency", interrupt_abort_observation)
    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.CONFLICT
    assert caught.value.message == "Injected pre-commit failure."
    assert _repository_state(repo) == before
    assert not (repo.path / ".git" / "MERGE_HEAD").exists()


def test_integration_second_base_exception_during_abort_proof_is_ambiguous(
    repo, monkeypatch
) -> None:
    """This fails if rollback proof retries indefinitely or exposes a second interruption."""
    _prepare_clean_divergence(repo)
    failures = [
        _InjectedIntegrationControl("first abort observation interruption"),
        _InjectedIntegrationControl("second abort observation interruption"),
    ]

    def interrupt_both_observations(self, branch_ref, deadline):
        if failures:
            raise failures.pop(0)
        raise AssertionError("rollback observation retried more than once")

    def fail_before_commit(self, prepared):
        raise ManduaError(ErrorCode.CONFLICT, "Injected pre-commit failure.")

    monkeypatch.setattr(IntegrationWriter, "_capture_ref_emergency", interrupt_both_observations)
    monkeypatch.setattr(IntegrationWriter, "_require_precommit_state", fail_before_commit)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert "merge --abort" in caught.value.recovery
    assert isinstance(caught.value.__cause__, _InjectedIntegrationControl)


def test_integration_base_exception_during_reconciliation_gets_one_fresh_classification(
    repo, monkeypatch
) -> None:
    """This fails if reconciliation control flow bypasses a fresh bounded state proof."""
    _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text
    original_closure = IntegrationWriter._require_complete_primary_closure
    failure = _InjectedIntegrationControl("injected during reconciliation")
    commit_interrupted = False
    closure_interrupted = False

    def fail_after_commit(self, arguments, **kwargs):
        nonlocal commit_interrupted
        output = original_run_text(self, arguments, **kwargs)
        if arguments[:2] == ["commit", "--no-verify"] and not commit_interrupted:
            commit_interrupted = True
            raise ManduaError(ErrorCode.GIT_FAILURE, "Injected commit wrapper failure.")
        return output

    def interrupt_reconciliation(self, commit_oid):
        nonlocal closure_interrupted
        original_closure(self, commit_oid)
        if not closure_interrupted:
            closure_interrupted = True
            source = repo.git("rev-parse", "refs/heads/hypothesis/sensor").stdout.strip()
            _advance_ref(repo, "refs/heads/hypothesis/sensor", source, "Race reconciliation")
            raise failure

    monkeypatch.setattr(GitRunner, "run_text", fail_after_commit)
    monkeypatch.setattr(
        IntegrationWriter, "_require_complete_primary_closure", interrupt_reconciliation
    )

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert caught.value.__cause__ is failure


def test_integration_base_exception_during_resource_cleanup_cannot_bypass_classification(
    repo, monkeypatch
) -> None:
    """This fails if cleanup control flow escapes before committed refs are classified."""
    _prepare_clean_divergence(repo)
    failure = _InjectedIntegrationControl("injected during operation cleanup")

    def cleanup_then_interrupt(self, resource):
        resource.cleanup()
        source = repo.git("rev-parse", "refs/heads/hypothesis/sensor").stdout.strip()
        _advance_ref(repo, "refs/heads/hypothesis/sensor", source, "Race cleanup classification")
        raise failure

    monkeypatch.setattr(IntegrationWriter, "_release_operation_resources", cleanup_then_interrupt)

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("verification_path", ("ordinary", "emergency"))
@pytest.mark.parametrize("ref_kind", ("head", "target", "source"))
def test_integration_final_applied_boundary_rechecks_all_refs_after_closure(
    repo, monkeypatch, verification_path, ref_kind
) -> None:
    """This fails if closure-time ref movement can still return applied=True."""
    _prepare_clean_divergence(repo)
    original_run_text = GitRunner.run_text
    original_closure = IntegrationWriter._require_complete_primary_closure
    commit_interrupted = False
    ref_moved = False

    def fail_after_commit_for_emergency(self, arguments, **kwargs):
        nonlocal commit_interrupted
        output = original_run_text(self, arguments, **kwargs)
        if (
            verification_path == "emergency"
            and arguments[:2] == ["commit", "--no-verify"]
            and not commit_interrupted
        ):
            commit_interrupted = True
            raise ManduaError(ErrorCode.GIT_FAILURE, "Enter emergency reconciliation.")
        return output

    def move_ref_after_closure(self, commit_oid):
        nonlocal ref_moved
        original_closure(self, commit_oid)
        if ref_moved:
            return
        ref_moved = True
        if ref_kind == "head":
            repo.git("branch", "race/final-head", commit_oid)
            repo.git("symbolic-ref", "HEAD", "refs/heads/race/final-head")
        elif ref_kind == "target":
            target = repo.git("rev-parse", "refs/heads/main").stdout.strip()
            _advance_ref(repo, "refs/heads/main", target, "Race final target snapshot")
        else:
            source = repo.git("rev-parse", "refs/heads/hypothesis/sensor").stdout.strip()
            _advance_ref(repo, "refs/heads/hypothesis/sensor", source, "Race final source snapshot")

    monkeypatch.setattr(GitRunner, "run_text", fail_after_commit_for_emergency)
    monkeypatch.setattr(
        IntegrationWriter, "_require_complete_primary_closure", move_ref_after_closure
    )

    with pytest.raises(ManduaError) as caught:
        repo.service().integrate(_request(), apply=True)

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.recovery is not None


def test_integration_rejects_read_tree_to_check_attr_index_replacement(repo, monkeypatch) -> None:
    """This fails if exact-tree attributes can consume a replaced temporary index."""
    _create_source(repo)
    before = _repository_state(repo)
    original_run = GitRunner.run
    injected = False
    index_root: Path | None = None

    def replace_index_after_read_tree(self, arguments, **kwargs):
        nonlocal injected, index_root
        output = original_run(self, arguments, **kwargs)
        index_file = kwargs.get("index_file")
        if arguments and arguments[0] == "read-tree" and index_file is not None and not injected:
            injected = True
            index_root = index_file.path.parent
            _replace_path = index_file.path.with_name("index.mandua-captured")
            contents = index_file.path.read_bytes()
            index_file.path.rename(_replace_path)
            index_file.path.write_bytes(contents)
        return output

    monkeypatch.setattr(GitRunner, "run", replace_index_after_read_tree)

    try:
        with pytest.raises(ManduaError) as caught:
            repo.service().integrate(_request())

        assert caught.value.code is ErrorCode.GIT_FAILURE
        assert _repository_state(repo) == before
    finally:
        if index_root is not None and index_root.exists():
            shutil.rmtree(index_root)


def test_integration_cannot_source_a_raced_runner_post_commit_hook(
    repo, tmp_path, monkeypatch
) -> None:
    """This fails if the effective controlled hooks path remains a mutable directory."""
    _create_source(repo)
    marker = tmp_path / "raced-runner-post-commit-hook"
    original_command = GitRunner._command
    installed = False

    def install_hook_at_effective_path(self, arguments, **kwargs):
        nonlocal installed
        command = original_command(self, arguments, **kwargs)
        if arguments and arguments[0] == "commit" and not installed:
            installed = True
            configured = next(
                item for item in command if item.startswith("core.hooksPath=")
            ).partition("=")[2]
            hooks = Path(configured)
            if hooks.is_dir():
                _write_executable(hooks / "post-commit", f"touch '{marker}'")
        return command

    monkeypatch.setattr(GitRunner, "_command", install_hook_at_effective_path)

    result = repo.service().integrate(_request(), apply=True)

    assert result.applied is True
    assert not marker.exists()


def _pack_reachable_repository(repo) -> Path:
    repo.git("repack", "-Ad")
    repo.git("prune-packed")
    pack_directory = repo.path / ".git" / "objects" / "pack"
    assert tuple(pack_directory.glob("*.idx"))
    count = next(
        line.partition(": ")[2]
        for line in repo.git("count-objects", "-v").stdout.splitlines()
        if line.startswith("count: ")
    )
    assert count == "0"
    return pack_directory


def _open_closure_writer(repo) -> tuple[IntegrationWriter, str]:
    writer = IntegrationWriter(repo.path, QueryLimits())
    commit_oid = repo.git("rev-parse", "HEAD").stdout.strip()
    writer._repository_authority = writer._runner.open_repository_authority(commit_oid)
    return writer, commit_oid


@pytest.mark.parametrize("path", ("ordinary", "emergency"))
def test_integration_closure_rejects_a_redirected_linked_worktree_locator(
    repo, tmp_path: Path, path: str
) -> None:
    """This fails if closure or emergency reopening rediscovers authority through `.git`."""
    _prepare_clean_divergence(repo)
    attacker = repo.clone_to(tmp_path / f"locator-attacker-{path}")
    repo.checkout("hypothesis/sensor")
    linked_path = tmp_path / f"locator-linked-{path}"
    repo.git("worktree", "add", str(linked_path), "main")
    linked = RepoBuilder(path=linked_path.resolve(), _root=tmp_path.resolve())
    writer = IntegrationWriter(linked.path, QueryLimits())
    prepared = writer._prepare(_request())
    locator = linked.path / ".git"
    captured_locator = locator.read_bytes()

    try:
        locator.write_text(f"gitdir: {attacker.path / '.git'}\n", encoding="utf-8")
        with pytest.raises(ManduaError) as caught:
            if path == "ordinary":
                writer._require_complete_primary_closure(prepared.target_oid)
            else:
                with writer._emergency_resources(prepared):
                    pass
    finally:
        locator.write_bytes(captured_locator)
        writer._cleanup_operation_resources()

    assert caught.value.code is ErrorCode.GIT_FAILURE


@pytest.mark.parametrize(
    ("root_name", "layout_field", "authority_label"),
    (
        ("worktree", "worktree", "worktree"),
        ("per-worktree-git", "git_directory", "worktree Git directory"),
        ("common", "common_directory", "common Git directory"),
        ("objects", "object_directory", "object directory"),
    ),
)
def test_integration_emergency_reopen_rejects_a_replaced_linked_authority_root(
    repo,
    tmp_path: Path,
    root_name: str,
    layout_field: str,
    authority_label: str,
) -> None:
    """This fails if recovery can classify state through a replaced physical root."""
    _prepare_clean_divergence(repo)
    repo.checkout("hypothesis/sensor")
    linked_path = tmp_path / f"root-identity-linked-{root_name}"
    repo.git("worktree", "add", str(linked_path), "main")
    linked = RepoBuilder(path=linked_path.resolve(), _root=tmp_path.resolve())
    writer = IntegrationWriter(linked.path, QueryLimits())
    prepared = writer._prepare(_request())
    writer._require_ignored_state(prepared, capture=True)
    layout = writer._repository_layout
    assert layout is not None
    roots = {
        "worktree": layout.worktree,
        "per-worktree-git": layout.git_directory,
        "common": layout.common_directory,
        "objects": layout.object_directory,
    }
    before = {name: _path_identity(path) for name, path in roots.items()}
    locator = linked.path / ".git"
    locator_contents = locator.read_bytes()
    locator_identity = _path_identity(locator)
    assert locator_identity[2] == stat.S_IFREG
    target = getattr(layout, layout_field)
    displaced = _replace_directory_root_preserving_children(target)

    try:
        after = {name: _path_identity(path) for name, path in roots.items()}
        assert after[root_name] != before[root_name]
        assert all(after[name] == before[name] for name in roots if name != root_name)
        assert locator.read_bytes() == locator_contents
        assert _path_identity(locator) == locator_identity

        with (
            pytest.raises(ManduaError) as caught,
            writer._emergency_resources(prepared) as deadline,
        ):
            assert writer._prove_exact_pre_state_emergency(prepared, deadline) is True
    finally:
        _restore_directory_root(target, displaced)
        writer._cleanup_operation_resources()

    assert caught.value.code is ErrorCode.GIT_FAILURE
    assert caught.value.message == (
        f"The repository {authority_label} authority path changed before authority reopening."
    )


def test_integration_closure_accepts_real_packed_only_reachable_objects(repo) -> None:
    """This fails if complete primary history is accepted only as loose objects."""
    repo.write("knowledge/packed.md", "Packed reachable content\n")
    repo.commit("Create packed reachable history")
    _pack_reachable_repository(repo)
    writer, commit_oid = _open_closure_writer(repo)

    try:
        writer._require_complete_primary_closure(commit_oid)
    finally:
        writer._cleanup_operation_resources()


def test_integration_closure_rejects_an_indirect_pack_index(repo) -> None:
    """This fails if primary closure follows an indirect pack index."""
    repo.write("knowledge/packed.md", "Packed reachable content\n")
    repo.commit("Create packed reachable history")
    pack_directory = _pack_reachable_repository(repo)
    index_path = next(pack_directory.glob("*.idx"))
    displaced = index_path.with_suffix(".idx.mandua-captured")
    index_path.rename(displaced)
    index_path.symlink_to(displaced.name)
    writer, commit_oid = _open_closure_writer(repo)

    try:
        with pytest.raises(ManduaError) as caught:
            writer._require_complete_primary_closure(commit_oid)
    finally:
        writer._cleanup_operation_resources()
        index_path.unlink()
        displaced.rename(index_path)

    assert caught.value.code in {ErrorCode.GIT_FAILURE, ErrorCode.MISSING_OBJECT}


def test_integration_closure_rejects_malformed_pack_content(repo) -> None:
    """This fails if primary closure trusts a corrupt pack after its index is discovered."""
    repo.write("knowledge/packed.md", "Packed reachable content\n")
    repo.commit("Create packed reachable history")
    pack_directory = _pack_reachable_repository(repo)
    pack_path = next(pack_directory.glob("*.pack"))
    original_mode = stat.S_IMODE(pack_path.stat().st_mode)
    original = pack_path.read_bytes()
    corrupted = bytearray(original)
    corrupted[len(corrupted) // 2] ^= 0x01
    pack_path.chmod(0o600)
    pack_path.write_bytes(corrupted)
    writer, commit_oid = _open_closure_writer(repo)

    try:
        with pytest.raises(ManduaError) as caught:
            writer._require_complete_primary_closure(commit_oid)
    finally:
        writer._cleanup_operation_resources()
        pack_path.write_bytes(original)
        pack_path.chmod(original_mode)

    assert caught.value.code in {ErrorCode.GIT_FAILURE, ErrorCode.MISSING_OBJECT}


def test_integration_closure_caps_pack_inventory_before_accumulation(repo, monkeypatch) -> None:
    """This fails if pack-directory names are eagerly materialized before a finite cap."""
    repo.write("knowledge/packed.md", "Packed reachable content\n")
    repo.commit("Create packed reachable history")
    _pack_reachable_repository(repo)
    monkeypatch.setattr(integration_module, "_MAX_PACK_DIRECTORY_ENTRIES", 1, raising=False)
    writer, commit_oid = _open_closure_writer(repo)

    try:
        with pytest.raises(ManduaError) as caught:
            writer._require_complete_primary_closure(commit_oid)
    finally:
        writer._cleanup_operation_resources()

    assert caught.value.code is ErrorCode.LIMIT_EXCEEDED


def test_integration_closure_rejects_pack_directory_replacement_during_verification(
    repo, monkeypatch
) -> None:
    """This fails if an absolute verify-pack path can outlive its captured directory."""
    repo.write("knowledge/packed.md", "Packed reachable content\n")
    repo.commit("Create packed reachable history")
    pack_directory = _pack_reachable_repository(repo)
    displaced = pack_directory.with_name("pack.mandua-captured")
    writer, commit_oid = _open_closure_writer(repo)
    original_run = writer._runner.run
    replaced = False

    def replace_pack_directory_after_verify(arguments, **kwargs):
        nonlocal replaced
        output = original_run(arguments, **kwargs)
        if arguments and arguments[0] == "verify-pack" and not replaced:
            replaced = True
            pack_directory.rename(displaced)
            shutil.copytree(displaced, pack_directory)
        return output

    monkeypatch.setattr(writer._runner, "run", replace_pack_directory_after_verify)

    try:
        with pytest.raises(ManduaError) as caught:
            writer._require_complete_primary_closure(commit_oid)
    finally:
        writer._cleanup_operation_resources()
        if displaced.exists():
            shutil.rmtree(displaced)

    assert caught.value.code in {ErrorCode.GIT_FAILURE, ErrorCode.MISSING_OBJECT}
