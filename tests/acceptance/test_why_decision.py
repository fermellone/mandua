"""Acceptance tests for evidence-grounded line provenance and decisions."""

from pathlib import PurePosixPath

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.memory_service import MemoryService
from mandua.models import Confidence, QueryLimits
from mandua.queries import provenance
from mandua.queries.provenance import decision_result
from mandua.repository import RepositoryInspector


def _message(subject: str, body: str = "", *trailers: str) -> str:
    """Build a fixture commit message with optional prose and a final trailer block."""
    paragraphs = [subject]
    if body:
        paragraphs.append(body)
    if trailers:
        paragraphs.append("\n".join(trailers))
    return "\n\n".join(paragraphs)


class _AncestrySpyInspector(RepositoryInspector):
    """Count causal checks while keeping all repository reads real."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ancestry_pairs: list[tuple[str, str]] = []
        self.ancestry_timeouts: list[float | None] = []
        self.raise_timeout = False

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        raise AssertionError(
            "Provenance must use already observed OIDs without resolving them again."
        )

    def is_ancestor_oids(
        self, ancestor: str, descendant: str, *, timeout_seconds: float | None = None
    ) -> bool:
        self.ancestry_pairs.append((ancestor, descendant))
        self.ancestry_timeouts.append(timeout_seconds)
        if self.raise_timeout:
            raise ManduaError(
                ErrorCode.LIMIT_EXCEEDED, "Git command exceeded the configured time limit."
            )
        return True


class _RecordingAncestryInspector(RepositoryInspector):
    """Record causal checks while preserving real Git ancestry behavior."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ancestry_pairs: list[tuple[str, str]] = []

    def is_ancestor_oids(
        self, ancestor: str, descendant: str, *, timeout_seconds: float | None = None
    ) -> bool:
        self.ancestry_pairs.append((ancestor, descendant))
        return super().is_ancestor_oids(
            ancestor,
            descendant,
            timeout_seconds=timeout_seconds,
        )


def test_why_links_a_line_to_recorded_reason(repo) -> None:
    """This fails if line blame loses an explicit reason recorded by its commit."""
    repo.write("knowledge/rules.md", "Water the north bed at dawn.\n")
    oid = repo.commit(
        _message(
            "Adopt dawn irrigation",
            "Reason: Dawn reduces evaporation.",
            "Memory-Type: decision",
            "Scope: irrigation",
            "Decision-ID: DEC-IRR-1",
            "Agent-ID: gardener",
        )
    )

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert result.evidence[0].oid == oid
    reason_claim = next(
        claim for claim in result.observed if "Dawn reduces evaporation." in claim.text
    )
    reason_evidence = next(item for item in result.evidence if item.id == reason_claim.evidence[0])
    assert reason_evidence.kind == "commit-metadata"
    assert reason_evidence.excerpt == "Reason: Dawn reduces evaporation."
    decision_claim = next(claim for claim in result.observed if "DEC-IRR-1" in claim.text)
    decision_evidence = next(
        item for item in result.evidence if item.id == decision_claim.evidence[0]
    )
    assert decision_evidence.kind == "commit-metadata"
    assert decision_evidence.excerpt == "Decision-ID: DEC-IRR-1"
    assert result.gaps == ()


def test_why_parses_generated_reasons_with_the_operation_input_limit(repo) -> None:
    """This fails if provenance discards a reason valid for its caller's larger limit."""
    reason = "x" * 4_500
    repo.write("knowledge/rules.md", "Water the north bed at dawn.\n")
    repo.commit(
        _message(
            "Adopt dawn irrigation",
            f"Reason: {reason}",
            "Memory-Type: decision",
        )
    )

    result = MemoryService.open(repo.path, limits=QueryLimits(max_input_chars=5_000)).why(
        path=PurePosixPath("knowledge/rules.md"), line=1
    )

    assert any("Recorded reason:" in claim.text for claim in result.observed)


def test_why_reports_unknown_reason_without_invention(repo) -> None:
    """This fails if a changed line is treated as evidence of an unrecorded motive."""
    baseline = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Baseline review.", baseline)
    repo.write("knowledge/rules.md", "Water for twelve minutes.\n")
    repo.commit("Change irrigation duration")

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert "No reason was recorded for this change." in result.gaps
    assert result.inferred == ()
    assert result.confidence is Confidence.MEDIUM
    assert result.warnings == ()


def test_why_rejects_reason_like_body_prose_without_inventing_motivation(repo) -> None:
    """This fails if a Reason-looking example outside the generated position becomes evidence."""
    baseline = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.git("notes", "--ref=refs/notes/review", "add", "-m", "Baseline review.", baseline)
    repo.write("knowledge/rules.md", "Water when the bed is dry.\n")
    repo.commit(
        _message(
            "Describe irrigation examples",
            "Reason: This quoted example is not a decision.\nMore explanatory prose follows.",
            "Decision-ID: DEC-IRR-EXAMPLE",
        )
    )

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert all("quoted example" not in claim.text for claim in result.observed)
    assert any("DEC-IRR-EXAMPLE" in claim.text for claim in result.observed)


def test_why_reports_missing_note_ref_without_claiming_no_reason(repo) -> None:
    """This fails if an unavailable review ref becomes a certainty that motivation is absent."""
    repo.write("knowledge/rules.md", "Water when the bed is dry.\n")
    repo.commit("Adopt moisture-triggered irrigation")

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert "No reason was recorded for this change." not in result.gaps
    assert any("Review notes are unavailable" in warning for warning in result.warnings)
    assert result.confidence is Confidence.LOW


def test_why_reports_a_corrupt_note_ref_without_claiming_no_reason(repo) -> None:
    """This fails if a corrupt review ref is treated as an ordinary missing target note."""
    repo.write("knowledge/rules.md", "Water when the bed is dry.\n")
    repo.write("knowledge/note-source.txt", "Not a note commit.\n")
    repo.commit("Adopt moisture-triggered irrigation")
    blob_oid = repo.git("rev-parse", "HEAD:knowledge/note-source.txt").stdout.strip()
    repo.git("update-ref", "refs/notes/review", blob_oid)

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert "No reason was recorded for this change." not in result.gaps
    assert any("Review note lookup failed" in warning for warning in result.warnings)
    assert result.confidence is Confidence.LOW


def test_why_treats_a_review_note_as_recorded_evidence(repo) -> None:
    """This fails if an attached review is ignored as a recorded explanation."""
    repo.write("knowledge/rules.md", "Water only when the bed is dry.\n")
    oid = repo.commit("Adopt moisture-triggered irrigation")
    repo.git(
        "notes",
        "--ref=refs/notes/review",
        "add",
        "-m",
        "Review confirms that the sensor threshold was checked.",
        oid,
    )

    result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)

    assert any(item.kind == "review-note" and item.oid == oid for item in result.evidence)
    assert result.gaps == ()


def test_why_validates_a_positive_line_and_repository_inputs(repo) -> None:
    """This fails if invalid provenance inputs reach Git blame unchanged."""
    with pytest.raises(ManduaError) as line_error:
        repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=0)
    with pytest.raises(ManduaError) as path_error:
        repo.service().why(path=PurePosixPath("../rules.md"), line=1)
    with pytest.raises(ManduaError) as revision_error:
        repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1, revision="missing")

    assert line_error.value.code is ErrorCode.VALIDATION_FAILED
    assert path_error.value.code is ErrorCode.INVALID_PATH
    assert revision_error.value.code is ErrorCode.INVALID_REVISION


def test_why_attributes_the_first_and_last_valid_lines(repo) -> None:
    """This fails if blame only handles one boundary of a multi-line repository file."""
    repo.write("knowledge/rules.md", "Water at dawn.\nRecord the moisture level.\n")
    first = repo.commit(_message("Add irrigation rules", "Reason: Baseline.", "Decision-ID: DEC-1"))
    repo.write("knowledge/rules.md", "Water at dawn.\nRecord the moisture level at dusk.\n")
    last = repo.commit(
        _message("Refine observation rule", "Reason: Better timing.", "Decision-ID: DEC-2")
    )

    first_result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=1)
    last_result = repo.service().why(path=PurePosixPath("knowledge/rules.md"), line=2)

    assert first_result.evidence[0].oid == first
    assert last_result.evidence[0].oid == last


def test_decision_matches_exact_ids_and_reports_later_corrections(repo) -> None:
    """This fails if decision matching uses a partial search or hides a correction link."""
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    decision = repo.commit(
        _message(
            "Adopt dawn irrigation",
            "Memory-Type: decision",
            "Decision-ID: DEC-IRR-1",
            "Agent-ID: gardener",
        )
    )
    repo.write("knowledge/other.md", "Different decision.\n")
    repo.commit(_message("Record another decision", "Decision-ID: DEC-IRR-10"))
    repo.write("knowledge/rules.md", "Water at sunset.\n")
    correction = repo.commit(
        _message(
            "Correct irrigation schedule",
            "Decision-ID: DEC-IRR-2",
            f"Corrects: {decision}",
        )
    )

    result = repo.service().decision("DEC-IRR-1")

    assert [item.oid for item in result.evidence if item.kind == "decision-commit"] == [decision]
    assert [item.oid for item in result.evidence if item.kind == "decision-correction"] == [
        correction
    ]
    assert result.gaps == ()


def test_decision_preserves_repeated_trailers_and_matches_a_later_value(repo) -> None:
    """This fails if repeated metadata is collapsed before exact decision matching."""
    unmatched = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    decision = repo.commit(
        _message(
            "Record multiple decision references",
            "",
            "Decision-ID: DEC-IRR-OLD",
            "Decision-ID: DEC-IRR-2",
            "Corrects: first-link",
            f"Corrects: {unmatched}",
        )
    )
    inspector = _AncestrySpyInspector(repo.path)

    result = decision_result(inspector, QueryLimits(), "DEC-IRR-2")
    evidence = next(item for item in result.evidence if item.oid == decision)

    assert evidence.kind == "decision-commit"
    assert evidence.details["decision_id"] == ("DEC-IRR-OLD", "DEC-IRR-2")
    assert evidence.details["corrects"] == ("first-link", unmatched)
    assert "verified_corrects" not in evidence.details
    assert inspector.ancestry_pairs == []
    assert "An invalid Corrects link was omitted." not in result.warnings


def test_decision_uses_one_unique_commit_budget_and_hides_omitted_pairs(repo) -> None:
    """This fails if separate slices return a correction for a decision outside one result budget."""
    repo.write("knowledge/first.md", "First decision.\n")
    older = repo.commit(_message("Record older decision", "Decision-ID: DEC-IRR-1"))
    repo.write("knowledge/correction.md", "Correction.\n")
    repo.commit(_message("Correct older decision", f"Corrects: {older}"))
    repo.write("knowledge/newer.md", "Newer decision.\n")
    newer = repo.commit(_message("Record newer decision", "Decision-ID: DEC-IRR-1"))

    result = repo.service().decision("DEC-IRR-1", limit=1)

    assert [item.oid for item in result.evidence] == [newer]
    assert len({item.oid for item in result.evidence}) <= 1
    assert "Decision results are limited by the requested result bound." in result.warnings


def test_decision_omits_non_descendant_corrects_links_with_a_warning(repo) -> None:
    """This fails if a chronological-looking but non-causal Corrects link is labeled later."""
    repo.checkout_new("decision-branch")
    repo.write("knowledge/branch.md", "Branch decision.\n")
    decision = repo.commit(_message("Record branch decision", "Decision-ID: DEC-BRANCH-1"))
    repo.checkout("main")
    repo.write("knowledge/main.md", "Independent main commit.\n")
    unrelated = repo.commit(_message("Reference an unrelated decision", f"Corrects: {decision}"))

    result = repo.service().decision("DEC-BRANCH-1")

    assert all(item.oid != unrelated for item in result.evidence)
    assert "A Corrects link was not causally later and was omitted." in result.warnings


def test_decision_deduplicates_repeated_corrects_pairs_before_ancestry_checks(repo) -> None:
    """This fails if repeated untrusted Corrects values trigger repeated Git ancestry work."""
    repo.write("knowledge/decision.md", "Decision.\n")
    decision = repo.commit(_message("Record a decision", "Decision-ID: DEC-IRR-PAIR"))
    repo.write("knowledge/correction.md", "Correction.\n")
    correction = repo.commit(
        _message(
            "Correct the decision",
            "",
            f"Corrects: {decision}",
            f"Corrects: {decision}",
        )
    )
    inspector = _AncestrySpyInspector(repo.path)

    result = decision_result(inspector, QueryLimits(), "DEC-IRR-PAIR")

    assert inspector.ancestry_pairs == [(decision, correction)]
    assert any(item.oid == correction for item in result.evidence)


def test_decision_fairly_reserves_one_check_per_selected_correction(repo) -> None:
    """This fails if one multi-link selected correction starves another selected correction."""
    repo.write("knowledge/first.md", "First decision.\n")
    first = repo.commit(_message("Record first decision", "Decision-ID: DEC-IRR-FAIR"))
    repo.write("knowledge/second.md", "Second decision.\n")
    second = repo.commit(_message("Record second decision", "Decision-ID: DEC-IRR-FAIR"))
    repo.write("knowledge/older-correction.md", "Older correction.\n")
    older_correction = repo.commit(
        _message(
            "Correct the first decision",
            "",
            "Memory-Type: correction",
            "Decision-ID: DEC-IRR-FAIR",
            f"Corrects: {first}",
        )
    )
    repo.write("knowledge/newer-correction.md", "Newer correction.\n")
    newer_correction = repo.commit(
        _message(
            "Correct both decisions",
            "",
            "Memory-Type: correction",
            "Decision-ID: DEC-IRR-FAIR",
            f"Corrects: {second}",
            f"Corrects: {first}",
        )
    )
    inspector = _RecordingAncestryInspector(repo.path, limits=QueryLimits(max_commits=10))

    result = decision_result(
        inspector,
        QueryLimits(max_commits=10),
        "DEC-IRR-FAIR",
        limit=4,
    )

    primary = tuple(
        item for item in result.evidence if item.kind in {"decision-commit", "decision-correction"}
    )
    assert [
        (
            item.oid,
            item.kind,
            item.details.get("corrects"),
            item.details.get("verified_corrects"),
        )
        for item in primary
    ] == [
        (first, "decision-commit", None, None),
        (second, "decision-commit", None, None),
        (older_correction, "decision-correction", (first,), (first,)),
        (newer_correction, "decision-correction", (second, first), (second,)),
    ]
    assert inspector.ancestry_pairs == [
        (second, newer_correction),
        (first, older_correction),
    ]
    assert len({item.oid for item in primary}) == 4
    assert (
        "Some Corrects links were not checked because the ancestry-check budget was exhausted."
        in result.warnings
    )
    assert (
        "Some Corrects links could not be verified as causal within the ancestry-check budget."
        in result.gaps
    )


def test_decision_checks_an_external_correction_before_deferred_selected_links(repo) -> None:
    """This fails if a selected extra link consumes an external correction's result slot."""
    repo.write("knowledge/first.md", "First decision.\n")
    first = repo.commit(_message("Record first decision", "Decision-ID: DEC-IRR-EXTERNAL"))
    repo.write("knowledge/second.md", "Second decision.\n")
    second = repo.commit(_message("Record second decision", "Decision-ID: DEC-IRR-EXTERNAL"))
    repo.write("knowledge/selected-correction.md", "Selected correction.\n")
    selected_correction = repo.commit(
        _message(
            "Correct both selected decisions",
            "",
            "Memory-Type: correction",
            "Decision-ID: DEC-IRR-EXTERNAL",
            f"Corrects: {second}",
            f"Corrects: {first}",
        )
    )
    repo.write("knowledge/external-correction.md", "External correction.\n")
    external_correction = repo.commit(
        _message("Correct the first decision externally", f"Corrects: {first}")
    )
    inspector = _RecordingAncestryInspector(repo.path, limits=QueryLimits(max_commits=10))

    result = decision_result(
        inspector,
        QueryLimits(max_commits=10),
        "DEC-IRR-EXTERNAL",
        limit=4,
    )

    primary = tuple(
        item for item in result.evidence if item.kind in {"decision-commit", "decision-correction"}
    )
    assert [(item.oid, item.kind) for item in primary] == [
        (first, "decision-commit"),
        (second, "decision-commit"),
        (selected_correction, "decision-correction"),
        (external_correction, "decision-correction"),
    ]
    assert inspector.ancestry_pairs == [
        (second, selected_correction),
        (first, external_correction),
    ]
    assert primary[2].details["verified_corrects"] == (second,)
    assert primary[3].details["verified_corrects"] == (first,)
    assert len({item.oid for item in primary}) == 4
    assert (
        "Some Corrects links were not checked because the ancestry-check budget was exhausted."
        in result.warnings
    )


def test_decision_stops_causal_checks_at_the_shared_count_budget(repo) -> None:
    """This fails if correction candidates can exceed one query-wide ancestry-check budget."""
    repo.write("knowledge/decision.md", "Decision.\n")
    decision = repo.commit(_message("Record a decision", "Decision-ID: DEC-IRR-BUDGET"))
    for number in range(4):
        repo.write(f"knowledge/correction-{number}.md", f"Correction {number}.\n")
        repo.commit(_message(f"Correct decision {number}", f"Corrects: {decision}"))
    inspector = _AncestrySpyInspector(repo.path, limits=QueryLimits(max_commits=10))

    result = decision_result(
        inspector,
        QueryLimits(max_commits=10),
        "DEC-IRR-BUDGET",
        limit=3,
    )

    assert len(inspector.ancestry_pairs) == 2
    assert len({item.oid for item in result.evidence}) <= 3
    assert (
        "Some Corrects links were not checked because the ancestry-check budget was exhausted."
        in result.warnings
    )
    assert (
        "Some Corrects links could not be verified as causal within the ancestry-check budget."
        in result.gaps
    )


def test_decision_stops_causal_checks_at_the_shared_wall_clock_budget(repo, monkeypatch) -> None:
    """This fails if individual Git timeouts can extend causal checking past one query deadline."""
    repo.write("knowledge/decision.md", "Decision.\n")
    decision = repo.commit(_message("Record a decision", "Decision-ID: DEC-IRR-TIME"))
    for number in range(2):
        repo.write(f"knowledge/correction-{number}.md", f"Correction {number}.\n")
        repo.commit(_message(f"Correct decision {number}", f"Corrects: {decision}"))
    inspector = _AncestrySpyInspector(repo.path, limits=QueryLimits(max_commits=10))
    moments = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(provenance.time, "monotonic", lambda: next(moments))

    result = decision_result(
        inspector,
        QueryLimits(max_commits=10, timeout_seconds=1.0),
        "DEC-IRR-TIME",
        limit=3,
    )

    assert len(inspector.ancestry_pairs) == 1
    assert inspector.ancestry_timeouts == [1.0]
    assert (
        "Some Corrects links were not checked because the ancestry-check budget was exhausted."
        in result.warnings
    )


def test_decision_discloses_a_final_ancestry_timeout_without_labeling_a_correction(repo) -> None:
    """This fails if a final Git ancestry timeout is hidden by the earlier deadline check."""
    repo.write("knowledge/decision.md", "Decision.\n")
    decision = repo.commit(_message("Record a decision", "Decision-ID: DEC-IRR-TIMEOUT"))
    repo.write("knowledge/correction.md", "Correction.\n")
    correction = repo.commit(_message("Correct decision", f"Corrects: {decision}"))
    inspector = _AncestrySpyInspector(repo.path)
    inspector.raise_timeout = True

    result = decision_result(inspector, QueryLimits(), "DEC-IRR-TIMEOUT")

    assert len(inspector.ancestry_timeouts) == 1
    assert 0 < inspector.ancestry_timeouts[0] <= QueryLimits().timeout_seconds
    assert all(item.oid != correction for item in result.evidence)
    assert (
        "Some Corrects links were not checked because the ancestry-check budget was exhausted."
        in result.warnings
    )


def test_decision_marks_only_verified_pairs_causal_when_repeated_corrects_are_mixed(repo) -> None:
    """This fails if a correction's unchecked Corrects value is presented as verified causality."""
    repo.write("knowledge/first.md", "First decision.\n")
    first = repo.commit(_message("Record first decision", "Decision-ID: DEC-IRR-MIXED"))
    repo.write("knowledge/second.md", "Second decision.\n")
    second = repo.commit(_message("Record second decision", "Decision-ID: DEC-IRR-MIXED"))
    repo.write("knowledge/correction.md", "Correction.\n")
    correction = repo.commit(
        _message(
            "Correct one selected decision",
            "",
            f"Corrects: {second}",
            f"Corrects: {first}",
        )
    )
    inspector = _AncestrySpyInspector(repo.path, limits=QueryLimits(max_commits=10))

    result = decision_result(
        inspector,
        QueryLimits(max_commits=10),
        "DEC-IRR-MIXED",
        limit=3,
    )
    evidence = next(item for item in result.evidence if item.oid == correction)

    assert evidence.details["corrects"] == (second, first)
    assert evidence.details["verified_corrects"] == (second,)
    assert inspector.ancestry_pairs == [(second, correction)]
    assert (
        "Some Corrects links could not be verified as causal within the ancestry-check budget."
        in result.gaps
    )


def test_decision_reports_no_exact_match_after_a_bounded_scan(repo) -> None:
    """This fails if an absent exact decision is explained with a guessed decision story."""
    repo.write("knowledge/rules.md", "Water at dawn.\n")
    repo.commit(_message("Record a different decision", "Decision-ID: DEC-IRR-10"))

    result = repo.service().decision("DEC-IRR-1")

    assert result.inferred == ()
    assert result.gaps == ("No commits were found for decision DEC-IRR-1.",)


def test_decision_discloses_a_truncated_history_scan(repo) -> None:
    """This fails if a bounded decision scan claims completeness beyond its Git window."""
    repo.write("knowledge/first.md", "First decision.\n")
    repo.commit(_message("Record first decision", "Decision-ID: DEC-IRR-1"))
    repo.write("knowledge/second.md", "Second decision.\n")
    repo.commit(_message("Record second decision", "Decision-ID: DEC-IRR-2"))

    result = MemoryService.open(repo.path, limits=QueryLimits(max_commits=1)).decision("DEC-IRR-1")

    assert result.history_scope.truncated is True
    assert "Decision history is limited by the configured commit bound." in result.warnings
