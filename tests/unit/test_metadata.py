"""Metadata parsing and construction tests."""

import pytest

from mandua.errors import ErrorCode, ManduaError
from mandua.metadata import build_commit_message, parse_recorded_reason, parse_trailers
from mandua.models import QueryLimits


def test_parse_trailers_keeps_only_the_final_complete_trailer_paragraph() -> None:
    """This fails if prose or malformed final paragraphs become metadata."""
    message = (
        "Reason: Morning irrigation is gentler on seedlings.\n\n"
        "Memory-Type: decision\n"
        "Decision-ID: DEC-WATER-1\n"
        "Decision-ID: DEC-WATER-2\n\n"
        "The earlier trailer-like line remains prose: no\n\n"
        "Scope: irrigation\n"
        "Agent-ID: gardener\n"
    )

    assert parse_trailers(message) == (
        ("Scope", "irrigation"),
        ("Agent-ID", "gardener"),
    )


def test_parse_trailers_rejects_a_malformed_final_trailer_paragraph() -> None:
    """This fails if a partial final metadata block is trusted as trailers."""
    message = "Decision-ID: DEC-WATER-1\nnot a trailer\n"

    assert parse_trailers(message) == ()


def test_build_commit_message_orders_reason_and_known_trailers() -> None:
    """This fails if generated metadata is split or reordered in a commit message."""
    message = build_commit_message(
        "Adopt dawn irrigation",
        reason="Dawn reduces evaporation.",
        memory_type="decision",
        scope="irrigation",
        task_id="TASK-IRR-1",
        decision_id="DEC-IRR-1",
        agent_id="gardener",
        corrects="0123456789012345678901234567890123456789",
    )

    assert message == (
        "Adopt dawn irrigation\n\n"
        "Reason: Dawn reduces evaporation.\n\n"
        "Memory-Type: decision\n"
        "Scope: irrigation\n"
        "Task-ID: TASK-IRR-1\n"
        "Decision-ID: DEC-IRR-1\n"
        "Agent-ID: gardener\n"
        "Corrects: 0123456789012345678901234567890123456789"
    )


@pytest.mark.parametrize(
    "body",
    (
        "Reason: A quoted example.\n\nContext: This is prose.\n\nDecision-ID: DEC-1",
        "Reason: First line.\nSecond line.\n\nDecision-ID: DEC-1",
        "Reason: Earlier text.\n\nExplanation.\n\nDecision-ID: DEC-1",
        "Context prose.\n\nReason: Later text.\n\nDecision-ID: DEC-1",
        "Reason:\n\nDecision-ID: DEC-1",
        "Reason: A standalone-looking paragraph.\n\nExample: prose, not generated metadata",
        "Reason: A standalone paragraph.\n\nnot a trailer",
    ),
)
def test_parse_recorded_reason_rejects_reason_like_prose_outside_generated_position(
    body: str,
) -> None:
    """This fails if quoted, malformed, or earlier prose becomes recorded motivation."""
    assert parse_recorded_reason(body) is None


def test_parse_recorded_reason_accepts_only_the_standalone_pre_trailer_paragraph() -> None:
    """This fails if a valid generated Reason paragraph is discarded."""
    body = "Reason: Dawn reduces evaporation.\n\nDecision-ID: DEC-1"

    assert parse_recorded_reason(body) == "Dawn reduces evaporation."


def test_build_commit_message_requires_trailers_when_recording_a_reason() -> None:
    """This fails if generated reasons can be emitted without a final trailer block."""
    with pytest.raises(ManduaError) as error:
        build_commit_message("Record a reason", reason="The soil is dry.")

    assert error.value.code is ErrorCode.VALIDATION_FAILED


def test_build_commit_message_appends_validated_extra_trailers_after_canonical_order() -> None:
    """This fails if Task 10 metadata cannot preserve ordered non-canonical trailers."""
    message = build_commit_message(
        "Record a rule",
        memory_type="decision",
        scope="irrigation",
        extra_trailers=(("Evidence-ID", "OBS-1"), ("Review", "approved")),
    )

    assert message.endswith(
        "Memory-Type: decision\nScope: irrigation\nEvidence-ID: OBS-1\nReview: approved"
    )


def test_recorded_reason_round_trips_through_canonical_and_extra_trailers() -> None:
    """This fails if valid Task 10 extras make a generated Reason unparseable."""
    message = build_commit_message(
        "Record a rule",
        reason="The sensor is calibrated.",
        memory_type="decision",
        extra_trailers=(("Evidence-ID", "OBS-1"),),
    )

    assert parse_recorded_reason(message.split("\n\n", 1)[1]) == "The sensor is calibrated."


def test_build_commit_message_does_not_let_extra_trailers_authorize_a_reason() -> None:
    """This fails if extra-only metadata can make a Reason appear semantically generated."""
    with pytest.raises(ManduaError) as caught:
        build_commit_message(
            "Record a rule",
            reason="The sensor is calibrated.",
            extra_trailers=(("Evidence-ID", "OBS-1"),),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_recorded_reason_uses_the_callers_actual_limits() -> None:
    """This fails if a valid long generated reason is parsed with default limits instead."""
    limits = QueryLimits(max_input_chars=5_000)
    message = build_commit_message(
        "Record a rule", reason="x" * 4_500, memory_type="decision", limits=limits
    )

    assert parse_recorded_reason(message.split("\n\n", 1)[1], limits=limits) == "x" * 4_500


@pytest.mark.parametrize(
    "trailers",
    (
        "memory-type: decision",
        "Memory-Type:  decision",
        "Memory-Type: decision ",
        "Memory-Type: decision\n" + "\n".join(f"Extra-{index}: value" for index in range(33)),
    ),
)
def test_recorded_reason_rejects_noncanonical_or_overlong_extra_trailer_blocks(
    trailers: str,
) -> None:
    """This fails if parsing normalizes generated grammar instead of requiring it exactly."""
    assert parse_recorded_reason(f"Reason: Calibrated.\n\n{trailers}") is None


@pytest.mark.parametrize(
    "extra_trailers",
    (
        [("Evidence-ID", "OBS-1")],
        (("Memory-Type", "injected"),),
        (("reason", "injected"),),
        (("Evidence-ID", "one"), ("evidence-id", "two")),
        (("Bad Key", "value"),),
        (("Evidence-ID", "two\nlines"),),
    ),
)
def test_build_commit_message_rejects_malformed_or_conflicting_extra_trailers(
    extra_trailers: object,
) -> None:
    """This fails if extra trailers can alter generated message structure."""
    with pytest.raises(ManduaError) as caught:
        build_commit_message(
            "Record a rule",
            memory_type="decision",
            extra_trailers=extra_trailers,  # type: ignore[arg-type]
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize("subject", ("", "\n", "Adopt\na decision"))
def test_build_commit_message_rejects_empty_or_multiline_subjects(subject: str) -> None:
    """This fails if generated subjects can create ambiguous commit-message structure."""
    with pytest.raises(ManduaError) as error:
        build_commit_message(subject, memory_type="decision")

    assert error.value.code is ErrorCode.VALIDATION_FAILED
