from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.features.temporal_contract import (
    resolve_source_available_at,
    validate_availability_basis,
    validate_availability_eligibility,
    validate_historical_event_ordering,
    validate_prediction_mode,
    validate_prediction_timing,
    validate_sha256_hash,
    validate_timezone_aware_utc,
)
from src.temporal import AvailabilityBasis, PredictionMode


CUTOFF = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc)
PREDICTED_AT = datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc)
SNAPSHOT_SHA256 = "a" * 64


def test_prediction_modes_are_centrally_defined():
    assert validate_prediction_mode("STRICT_PRE_MATCH") is PredictionMode.STRICT_PRE_MATCH
    assert validate_prediction_mode("RETROSPECTIVE_RESEARCH") is PredictionMode.RETROSPECTIVE_RESEARCH
    assert validate_prediction_mode(PredictionMode.STRICT_PRE_MATCH) is PredictionMode.STRICT_PRE_MATCH


def test_invalid_prediction_mode_is_rejected():
    with pytest.raises(ValueError, match="prediction_mode must be one of"):
        validate_prediction_mode("IN_PLAY")


def test_naive_timestamp_is_rejected_and_aware_offset_is_normalized_to_utc():
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_timezone_aware_utc(datetime(2026, 11, 1, 10, 0), "test_time")

    normalized = validate_timezone_aware_utc(
        datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))),
        "test_time",
    )
    assert normalized == CUTOFF
    assert normalized.utcoffset() == timedelta(0)


def test_strict_pre_match_timing_accepts_valid_order():
    assert validate_prediction_timing(
        PredictionMode.STRICT_PRE_MATCH,
        CUTOFF,
        KICKOFF,
        PREDICTED_AT,
    ) == (CUTOFF, KICKOFF, PREDICTED_AT)


@pytest.mark.parametrize(
    ("cutoff", "event_time", "predicted_at", "message"),
    [
        (KICKOFF, KICKOFF, PREDICTED_AT, "prediction_cutoff < scheduled_event_time"),
        (CUTOFF, KICKOFF, KICKOFF, "predicted_at < scheduled_event_time"),
        (PREDICTED_AT, KICKOFF, CUTOFF, "prediction_cutoff <= predicted_at"),
    ],
)
def test_strict_pre_match_timing_rejects_invalid_order(cutoff, event_time, predicted_at, message):
    with pytest.raises(ValueError, match=message):
        validate_prediction_timing(
            PredictionMode.STRICT_PRE_MATCH,
            cutoff,
            event_time,
            predicted_at,
        )


def test_retrospective_mode_is_explicit_and_does_not_apply_pre_match_inequalities():
    later_generation = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    assert validate_prediction_timing(
        PredictionMode.RETROSPECTIVE_RESEARCH,
        KICKOFF,
        KICKOFF,
        later_generation,
    ) == (KICKOFF, KICKOFF, later_generation)


def test_historical_event_must_be_strictly_earlier():
    historical = KICKOFF - timedelta(seconds=1)
    assert validate_historical_event_ordering(historical, KICKOFF) == (historical, KICKOFF)
    with pytest.raises(ValueError, match="must be earlier"):
        validate_historical_event_ordering(KICKOFF, KICKOFF)
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_historical_event_ordering(datetime(2026, 11, 1, 10, 0), KICKOFF)


def test_availability_at_cutoff_is_eligible():
    available = validate_availability_eligibility(
        PredictionMode.STRICT_PRE_MATCH,
        CUTOFF,
        availability_basis=AvailabilityBasis.VERIFIED_ACCESS,
        source_available_ts=CUTOFF,
    )
    assert available == CUTOFF


def test_availability_after_cutoff_is_rejected():
    with pytest.raises(ValueError, match="at or before prediction_cutoff"):
        validate_availability_eligibility(
            PredictionMode.STRICT_PRE_MATCH,
            CUTOFF,
            availability_basis="verified_access",
            source_available_ts=CUTOFF + timedelta(microseconds=1),
        )


def test_unknown_availability_fails_strict_but_is_explicitly_allowed_for_research():
    with pytest.raises(ValueError, match="requires known verified source availability"):
        validate_availability_eligibility(
            PredictionMode.STRICT_PRE_MATCH,
            CUTOFF,
            availability_basis=None,
        )
    assert validate_availability_eligibility(
        PredictionMode.RETROSPECTIVE_RESEARCH,
        CUTOFF,
        availability_basis=None,
    ) is None


def test_first_observed_is_accepted_as_conservative_availability_boundary():
    first_seen = CUTOFF - timedelta(minutes=2)
    assert validate_availability_eligibility(
        PredictionMode.STRICT_PRE_MATCH,
        CUTOFF,
        availability_basis="first_observed",
        first_observed_at=first_seen,
        source_available_ts=first_seen,
    ) == first_seen


def test_snapshot_availability_requires_exact_snapshot_and_source_evidence():
    source_record_id = str(uuid4())
    assert validate_availability_eligibility(
        PredictionMode.STRICT_PRE_MATCH,
        CUTOFF,
        availability_basis="snapshot",
        snapshot_available_at=CUTOFF,
        snapshot_id="openfootball-commit-abc123",
        snapshot_sha256=SNAPSHOT_SHA256,
        source_record_id=source_record_id,
    ) == CUTOFF

    with pytest.raises(ValueError, match="snapshot_id"):
        resolve_source_available_at(
            availability_basis="snapshot",
            snapshot_available_at=CUTOFF,
            snapshot_sha256=SNAPSHOT_SHA256,
            source_record_id=source_record_id,
        )
    with pytest.raises(ValueError, match="snapshot_sha256"):
        resolve_source_available_at(
            availability_basis="snapshot",
            snapshot_available_at=CUTOFF,
            snapshot_id="snapshot-1",
            source_record_id=source_record_id,
        )


def test_provider_publication_alone_does_not_establish_accessibility():
    with pytest.raises(ValueError, match="requires known verified source availability"):
        validate_availability_eligibility(
            PredictionMode.STRICT_PRE_MATCH,
            CUTOFF,
            availability_basis="provider_publication",
            source_published_ts=CUTOFF - timedelta(minutes=3),
        )


def test_ingestion_time_is_not_an_availability_input():
    with pytest.raises(TypeError):
        resolve_source_available_at(
            availability_basis=None,
            ingest_ts=CUTOFF,
        )


def test_invalid_availability_basis_is_rejected():
    with pytest.raises(ValueError, match="availability_basis must be one of"):
        validate_availability_basis("ingestion_time")


def test_sha256_hash_validation():
    assert validate_sha256_hash(SNAPSHOT_SHA256) == SNAPSHOT_SHA256
    assert validate_sha256_hash(None) is None
    for invalid_hash in ("A" * 64, "a" * 63, "g" * 64, ""):
        with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
            validate_sha256_hash(invalid_hash)
