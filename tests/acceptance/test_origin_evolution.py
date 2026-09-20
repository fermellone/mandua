"""Acceptance tests for bounded content-origin and path-evolution queries."""

import hashlib
import os
from pathlib import PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.memory_service import MemoryService
from mandua.models import QueryLimits


def test_origin_finds_when_absent_content_was_added_and_removed(repo) -> None:
    """This fails if pickaxe evidence omits content no longer present at HEAD."""
    phrase = "Water every bed at noon."
    repo.write("knowledge/rules.md", phrase + "\n")
    added = repo.commit("Add the temporary noon rule")
    repo.write("knowledge/rules.md", "Water only when soil is dry.\n")
    removed = repo.commit("Remove the temporary noon rule")

    result = repo.service().origin(phrase, path=PurePosixPath("knowledge/rules.md"))

    assert [item.oid for item in result.evidence if item.kind == "content-added"] == [added]
    assert [item.oid for item in result.evidence if item.kind == "content-removed"] == [removed]


def test_evolution_follows_a_rename(repo) -> None:
    """This fails if path history drops the previous name while following a rename."""
    repo.write("knowledge/rules.md", "Water only when soil is dry.\n")
    repo.commit("Add the irrigation rules")
    repo.git("mv", "knowledge/rules.md", "knowledge/irrigation-rules.md")
    rename_oid = repo.commit("Rename the irrigation rules")

    result = repo.service().evolution(PurePosixPath("knowledge/irrigation-rules.md"))

    assert any(
        item.oid == rename_oid and item.details["status"].startswith("R")
        for item in result.evidence
    )
    assert any(item.path == "knowledge/rules.md" for item in result.evidence)


def test_origin_uses_exact_content_lines_not_patch_headers(repo) -> None:
    """This fails if a diff file header is misreported as a matching addition."""
    phrase = "++ b/knowledge/rules.md"
    repo.write("knowledge/rules.md", phrase + "\n")
    commit = repo.commit("Add a header-shaped rule")

    result = repo.service().origin(phrase)

    assert [(item.oid, item.path, item.excerpt) for item in result.evidence] == [
        (commit, "knowledge/rules.md", phrase)
    ]


def test_origin_counts_fixed_substrings_and_duplicate_occurrences(repo) -> None:
    """This fails if pickaxe evidence requires a query to occupy one whole patch line."""
    phrase = "bed"
    repo.write("knowledge/rules.md", "Water each bed; every bed needs water.\n")
    commit = repo.commit("Add repeated irrigation wording")

    result = repo.service().origin(phrase)

    evidence = next(item for item in result.evidence if item.oid == commit)
    assert evidence.kind == "content-added"
    assert evidence.details["occurrence_count"] == 2
    assert evidence.details["old_hunk_occurrences"] == 0
    assert evidence.details["new_hunk_occurrences"] == 2


def test_origin_counts_a_fixed_multiline_occurrence(repo) -> None:
    """This fails if a fixed query spanning adjacent patch lines is not counted."""
    phrase = "Water every\nbed at noon"
    repo.write("knowledge/rules.md", phrase + "\n")
    commit = repo.commit("Add a multiline irrigation rule")

    result = repo.service().origin(phrase)

    evidence = next(item for item in result.evidence if item.oid == commit)
    assert evidence.kind == "content-added"
    assert evidence.details["occurrence_count"] == 1


def test_origin_reports_the_net_count_for_a_replacement(repo) -> None:
    """This fails if replacements report full sides rather than their changed count."""
    phrase = "bed"
    repo.write("knowledge/rules.md", "bed bed\n")
    repo.commit("Add two bed references")
    repo.write("knowledge/rules.md", "bed bed bed\n")
    replacement = repo.commit("Add one bed reference")

    result = repo.service().origin(phrase)

    evidence = next(item for item in result.evidence if item.oid == replacement)
    assert evidence.kind == "content-added"
    assert evidence.details["occurrence_count"] == 1
    assert evidence.details["net_hunk_occurrences"] == 1
    assert evidence.details["old_hunk_occurrences"] == 2
    assert evidence.details["new_hunk_occurrences"] == 3


def test_origin_counts_a_multiline_match_crossing_unchanged_context(repo) -> None:
    """This fails if zero-context hunks omit an unchanged line needed by pickaxe."""
    phrase = "Water every\nstable tail"
    repo.write("knowledge/rules.md", phrase + "\n")
    repo.commit("Add a cross-line irrigation rule")
    repo.write("knowledge/rules.md", "Water always\nstable tail\n")
    changed = repo.commit("Change the irrigation frequency")

    result = repo.service().origin(phrase)

    evidence = next(item for item in result.evidence if item.oid == changed)
    assert evidence.kind == "content-removed"
    assert evidence.details["occurrence_count"] == 1
    assert evidence.details["old_hunk_occurrences"] == 1
    assert evidence.details["new_hunk_occurrences"] == 0


def test_origin_preserves_crlf_and_control_boundaries(repo) -> None:
    """This fails if patch parsing normalizes CRLF or record-separator bytes."""
    phrase = "Water every\r\nstable\x1etail"
    repo.write("knowledge/rules.md", phrase + "\n")
    repo.commit("Add a CRLF irrigation rule")
    repo.write("knowledge/rules.md", "Water always\r\nstable\x1etail\n")
    changed = repo.commit("Change the CRLF irrigation rule")

    result = repo.service().origin(phrase)

    evidence = next(item for item in result.evidence if item.oid == changed)
    assert evidence.kind == "content-removed"
    assert evidence.excerpt == phrase
    assert evidence.details["old_hunk_occurrences"] == 1


def test_origin_preserves_eof_boundaries_after_shared_context(repo) -> None:
    """This fails if a no-newline marker after context fabricates a fixed occurrence."""
    phrase = "needle\n"
    repo.write(
        "knowledge/rules.md",
        "needle\nfirst gap\nsecond gap\nthird gap\npre-eof\nneedle",
    )
    repo.commit("Add separated irrigation markers")
    repo.write(
        "knowledge/rules.md",
        "gone\nfirst gap\nsecond gap\nthird gap\npost-eof\nneedle",
    )
    changed = repo.commit("Change separated irrigation markers")

    result = repo.service().origin(phrase)

    evidence = next(item for item in result.evidence if item.oid == changed)
    assert evidence.kind == "content-removed"
    assert evidence.details["net_hunk_occurrences"] == -1
    assert evidence.details["old_hunk_occurrences"] == 1
    assert evidence.details["new_hunk_occurrences"] == 0


def test_origin_rejects_invalid_text_before_running_pickaxe(repo) -> None:
    """This fails if empty, NUL, or oversized text reaches a Git argument."""
    service = MemoryService.open(repo.path, limits=QueryLimits(max_input_chars=100))

    with pytest.raises(ManduaError) as empty:
        service.origin("")
    with pytest.raises(ManduaError) as nul:
        service.origin("wet\x00bed")
    with pytest.raises(ManduaError) as oversized:
        service.origin("a" * 101)

    assert empty.value.code is ErrorCode.VALIDATION_FAILED
    assert nul.value.code is ErrorCode.VALIDATION_FAILED
    assert oversized.value.code is ErrorCode.VALIDATION_FAILED


def test_origin_does_not_run_external_diff_or_textconv(repo, tmp_path) -> None:
    """This fails if the patch search bypasses GitRunner's safe diff protections."""
    marker = tmp_path / "diff-helper-ran"
    helper = tmp_path / "diff-helper"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    helper.chmod(0o700)
    repo.git("config", "diff.external", str(helper))
    repo.git("config", "diff.malicious.textconv", str(helper))
    repo.write(".gitattributes", "*.secret diff=malicious\n")
    phrase = "Never invoke repository diff helpers."
    repo.write("knowledge/rules.secret", phrase + "\n")
    commit = repo.commit("Add a protected irrigation rule")

    result = repo.service().origin(phrase, path=PurePosixPath("knowledge/rules.secret"))

    assert [item.oid for item in result.evidence] == [commit]
    assert not marker.exists()


def test_evolution_preserves_renamed_paths_with_spaces(repo) -> None:
    """This fails if name-status parsing splits a renamed path on whitespace."""
    repo.write("knowledge/watering rules.md", "Water only when soil is dry.\n")
    repo.commit("Add spaced irrigation rules")
    repo.git("mv", "knowledge/watering rules.md", "knowledge/irrigation rules.md")
    rename_oid = repo.commit("Rename spaced irrigation rules")

    result = repo.service().evolution(PurePosixPath("knowledge/irrigation rules.md"))

    rename = next(item for item in result.evidence if item.oid == rename_oid)
    assert rename.path == "knowledge/watering rules.md"
    assert rename.details["old_path"] == "knowledge/watering rules.md"
    assert rename.details["new_path"] == "knowledge/irrigation rules.md"


@pytest.mark.parametrize(
    ("raw_path", "rendered_path"),
    [
        (b'knowledge/quote"name.md', 'knowledge/quote"name.md'),
        (b"knowledge/back\\slash.md", "knowledge/back\\\\slash.md"),
        (b"knowledge/line\nname.md", "knowledge/line\\nname.md"),
        ("knowledge/caf\u00e9.md".encode(), "knowledge/caf\u00e9.md"),
        (b"knowledge/invalid-\xff.md", "knowledge/invalid-\\xff.md"),
    ],
)
def test_origin_and_evolution_preserve_special_path_identity(repo, raw_path, rendered_path) -> None:
    """This fails if Git quoted or NUL-delimited paths lose their byte identity."""
    path_text = os.fsdecode(raw_path)
    path = PurePosixPath(path_text)
    phrase = "Water only when soil is dry."
    if b"\xff" in raw_path:
        blob = repo.git_bytes("hash-object", "-w", "--stdin", input_bytes=(phrase + "\n").encode())
        repo.git_bytes(
            "update-index",
            "--add",
            "-z",
            "--index-info",
            input_bytes=b"100644 blob " + blob.stdout.strip() + b"\t" + raw_path + b"\0",
        )
        repo._commit("Add a specially named irrigation rule")
        commit = repo.git("rev-parse", "HEAD").stdout.strip()
    else:
        repo.write(path_text, phrase + "\n")
        commit = repo.commit("Add a specially named irrigation rule")

    origin = repo.service().origin(phrase, path=path)
    evolution = repo.service().evolution(path)

    origin_evidence = next(item for item in origin.evidence if item.oid == commit)
    evolution_evidence = next(item for item in evolution.evidence if item.oid == commit)
    assert origin_evidence.path == rendered_path
    assert origin_evidence.details["path_bytes"] == raw_path.hex()
    assert evolution_evidence.path == rendered_path
    assert evolution_evidence.details["new_path"] == rendered_path
    assert evolution_evidence.details["new_path_bytes"] == raw_path.hex()


def test_long_path_identity_uses_a_valid_hex_prefix_and_digest(repo) -> None:
    """This fails if a bounded byte identity exposes ellipsis inside hexadecimal data."""
    raw_path = b"knowledge/" + (b"a" * 210) + b".md"
    path_text = os.fsdecode(raw_path)
    path = PurePosixPath(path_text)
    phrase = "Water only when soil is dry."
    repo.write(path_text, phrase + "\n")
    commit = repo.commit("Add a long named irrigation rule")

    origin = repo.service().origin(phrase, path=path)
    evolution = repo.service().evolution(path)

    origin_evidence = next(item for item in origin.evidence if item.oid == commit)
    evolution_evidence = next(item for item in evolution.evidence if item.oid == commit)
    for details, prefix_name, digest_name in (
        (origin_evidence.details, "path_bytes_prefix", "path_bytes_sha256"),
        (evolution_evidence.details, "new_path_bytes_prefix", "new_path_bytes_sha256"),
    ):
        assert details.get("path_bytes") is None
        assert details.get("new_path_bytes") is None
        assert details[prefix_name]
        assert len(details[prefix_name]) % 2 == 0
        assert bytes.fromhex(details[prefix_name])
        assert details[digest_name] == hashlib.sha256(raw_path).hexdigest()


@pytest.mark.parametrize("separator", ("\u202e", "\u2028", "\u2029"))
def test_path_rendering_escapes_nonprintable_unicode_boundaries(repo, separator) -> None:
    """This fails if an untrusted Unicode display boundary reaches evidence literally."""
    raw_path = f"knowledge/hidden{separator}name.md".encode()
    path_text = os.fsdecode(raw_path)
    path = PurePosixPath(path_text)
    phrase = "Water only when soil is dry."
    repo.write(path_text, phrase + "\n")
    commit = repo.commit("Add a bidi named irrigation rule")

    result = repo.service().origin(phrase, path=path)

    evidence = next(item for item in result.evidence if item.oid == commit)
    assert evidence.path == f"knowledge/hidden\\u{ord(separator):04x}name.md"


def test_evolution_reports_a_delete_and_the_history_boundary(repo) -> None:
    """This fails if a deleted path is presented as having unbounded follow history."""
    repo.write("knowledge/rules.md", "Water only when soil is dry.\n")
    repo.commit("Add irrigation rules")
    (repo.path / "knowledge/rules.md").unlink()
    deleted = repo.commit("Delete irrigation rules")

    result = repo.service().evolution(PurePosixPath("knowledge/rules.md"))

    deletion = next(item for item in result.evidence if item.oid == deleted)
    assert deletion.details["status"] == "D"
    assert deletion.details["old_path"] == "knowledge/rules.md"
    assert deletion.details["new_path"] is None
    assert any("deletion" in gap.lower() for gap in result.gaps)


def test_origin_and_evolution_disclose_the_configured_commit_bound(repo) -> None:
    """This fails if either query claims a complete history after a bounded scan."""
    phrase = "Record the same irrigation observation."
    repo.write("knowledge/first.md", phrase + "\n")
    repo.commit("Add first observation")
    repo.write("knowledge/second.md", phrase + "\n")
    newest = repo.commit("Add second observation")

    service = MemoryService.open(repo.path, limits=QueryLimits(max_commits=1))
    origin = service.origin(phrase)
    evolution = service.evolution(PurePosixPath("knowledge/second.md"))

    assert [item.oid for item in origin.evidence] == [newest]
    assert origin.history_scope.truncated is True
    assert any("commit bound" in warning.lower() for warning in origin.warnings)
    assert evolution.history_scope.truncated is True
    assert any("commit bound" in warning.lower() for warning in evolution.warnings)
