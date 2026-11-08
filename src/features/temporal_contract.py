"""Project-level as-of rules for pre-match feature availability."""

from datetime import datetime, timezone
import re
from typing import Any

import pandas as pd

from src.temporal import AvailabilityBasis, PredictionMode

EVENT_TIME_RULE = "Use the scheduled/source event time; historical results require historical_event_time < current_event_time."
PREDICTION_CUTOFF_RULE = "A feature is usable only when availability_time <= prediction_cutoff."
UNKNOWN_AVAILABILITY_RULE = "Event or ingestion time is not a publication-time substitute; unknown availability remains unproven."
SAME_EVENT_TIME_RULE = "Results at an identical event timestamp are applied after the timestamp group, never to one another."
OPENFOOTBALL_AVAILABILITY_RULE = "OpenFootball source_available_ts is the public GitHub commit time for the exact file snapshot; it is an update bound, not proof of original publication."


def validate_prediction_mode(value: str | PredictionMode) -> PredictionMode:
    """Return a supported prediction mode or reject an unrecognized value."""
    try:
        return value if isinstance(value, PredictionMode) else PredictionMode(value)
    except (TypeError, ValueError):
        choices = ", ".join(mode.value for mode in PredictionMode)
        raise ValueError(f"prediction_mode must be one of: {choices}") from None


def validate_availability_basis(value: str | AvailabilityBasis) -> AvailabilityBasis:
    """Return a supported availability evidence basis."""
    try:
        return value if isinstance(value, AvailabilityBasis) else AvailabilityBasis(value)
    except (TypeError, ValueError):
        choices = ", ".join(basis.value for basis in AvailabilityBasis)
        raise ValueError(f"availability_basis must be one of: {choices}") from None


def validate_timezone_aware_utc(value: datetime, field_name: str) -> datetime:
    """Require an aware instant and return its UTC representation."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def validate_prediction_timing(
    prediction_mode: str | PredictionMode,
    prediction_cutoff: datetime,
    scheduled_event_time: datetime,
    predicted_at: datetime,
) -> tuple[datetime, datetime, datetime]:
    """Validate UTC instants and mode-specific cutoff/generation ordering."""
    mode = validate_prediction_mode(prediction_mode)
    cutoff_utc = validate_timezone_aware_utc(prediction_cutoff, "prediction_cutoff")
    event_utc = validate_timezone_aware_utc(scheduled_event_time, "scheduled_event_time")
    predicted_utc = validate_timezone_aware_utc(predicted_at, "predicted_at")

    if mode is PredictionMode.STRICT_PRE_MATCH:
        if cutoff_utc >= event_utc:
            raise ValueError("STRICT_PRE_MATCH requires prediction_cutoff < scheduled_event_time")
        if predicted_utc >= event_utc:
            raise ValueError("STRICT_PRE_MATCH requires predicted_at < scheduled_event_time")
        if cutoff_utc > predicted_utc:
            raise ValueError("STRICT_PRE_MATCH requires prediction_cutoff <= predicted_at")
    return cutoff_utc, event_utc, predicted_utc


def validate_historical_event_ordering(
    historical_event_time: datetime,
    scheduled_event_time: datetime,
) -> tuple[datetime, datetime]:
    """Require a historical event to precede the target event strictly."""
    historical_utc = validate_timezone_aware_utc(historical_event_time, "historical_event_time")
    target_utc = validate_timezone_aware_utc(scheduled_event_time, "scheduled_event_time")
    if historical_utc >= target_utc:
        raise ValueError("historical_event_time must be earlier than scheduled_event_time")
    return historical_utc, target_utc


def validate_sha256_hash(value: str | None, field_name: str = "sha256") -> str | None:
    """Validate a nullable lowercase SHA-256 hexadecimal digest."""
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be exactly 64 lowercase hexadecimal characters")
    return value


def resolve_source_available_at(
    *,
    availability_basis: str | AvailabilityBasis | None,
    source_available_ts: datetime | None = None,
    source_published_ts: datetime | None = None,
    first_observed_at: datetime | None = None,
    snapshot_available_at: datetime | None = None,
    snapshot_id: str | None = None,
    snapshot_sha256: str | None = None,
    source_record_id: str | None = None,
) -> datetime | None:
    """Resolve effective accessibility without treating publication/ingestion as access."""
    for field_name, value in (
        ("source_available_ts", source_available_ts),
        ("source_published_ts", source_published_ts),
        ("first_observed_at", first_observed_at),
        ("snapshot_available_at", snapshot_available_at),
    ):
        if value is not None:
            validate_timezone_aware_utc(value, field_name)

    if availability_basis is None:
        return None
    basis = validate_availability_basis(availability_basis)

    if basis is AvailabilityBasis.PROVIDER_PUBLICATION:
        if source_available_ts is not None:
            raise ValueError("provider publication alone cannot establish source availability")
        return None

    if basis is AvailabilityBasis.VERIFIED_ACCESS:
        if source_available_ts is None:
            raise ValueError("verified_access requires source_available_ts")
        return validate_timezone_aware_utc(source_available_ts, "source_available_ts")

    if basis is AvailabilityBasis.FIRST_OBSERVED:
        if first_observed_at is None:
            raise ValueError("first_observed requires first_observed_at")
        first_seen_utc = validate_timezone_aware_utc(first_observed_at, "first_observed_at")
        if source_available_ts is not None:
            available_utc = validate_timezone_aware_utc(source_available_ts, "source_available_ts")
            if available_utc != first_seen_utc:
                raise ValueError("first_observed availability must equal first_observed_at")
        return first_seen_utc

    if snapshot_available_at is None:
        raise ValueError("snapshot availability requires snapshot_available_at")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("snapshot availability requires an exact snapshot_id")
    validate_sha256_hash(snapshot_sha256, "snapshot_sha256")
    if snapshot_sha256 is None:
        raise ValueError("snapshot availability requires snapshot_sha256")
    if not isinstance(source_record_id, str) or not source_record_id.strip():
        raise ValueError("snapshot availability requires source_record_id")
    try:
        from uuid import UUID

        UUID(source_record_id)
    except (TypeError, ValueError):
        raise ValueError("snapshot availability requires a valid source_record_id UUID") from None
    snapshot_time_utc = validate_timezone_aware_utc(snapshot_available_at, "snapshot_available_at")
    if source_available_ts is not None:
        available_utc = validate_timezone_aware_utc(source_available_ts, "source_available_ts")
        if available_utc != snapshot_time_utc:
            raise ValueError("snapshot availability must equal snapshot_available_at")
    return snapshot_time_utc


def validate_availability_eligibility(
    prediction_mode: str | PredictionMode,
    prediction_cutoff: datetime,
    **availability_evidence: Any,
) -> datetime | None:
    """Validate effective availability; retrospective mode may explicitly retain unknowns."""
    mode = validate_prediction_mode(prediction_mode)
    cutoff_utc = validate_timezone_aware_utc(prediction_cutoff, "prediction_cutoff")
    available_at = resolve_source_available_at(**availability_evidence)
    if available_at is None:
        if mode is PredictionMode.STRICT_PRE_MATCH:
            raise ValueError("STRICT_PRE_MATCH requires known verified source availability")
        return None
    if available_at > cutoff_utc:
        raise ValueError("source_available_ts must be at or before prediction_cutoff")
    return available_at


def is_available(availability_time, prediction_cutoff) -> bool:
    """Return whether known availability is at or before the prediction cutoff."""
    if availability_time is None or prediction_cutoff is None:
        return False
    availability = pd.Timestamp(availability_time)
    cutoff = pd.Timestamp(prediction_cutoff)
    if pd.isna(availability) or pd.isna(cutoff):
        return False
    if (availability.tzinfo is None) != (cutoff.tzinfo is None):
        raise ValueError("availability_time and prediction_cutoff must both be timezone-naive or timezone-aware")
    return availability <= cutoff


def availability_status(source_available_ts, prediction_cutoff) -> str:
    """Classify availability without treating missing timestamps as available."""
    if source_available_ts is None or pd.isna(source_available_ts):
        return "unknown_availability"
    return "available" if is_available(source_available_ts, prediction_cutoff) else "unavailable"


def validate_prediction_cutoff(prediction_cutoff) -> pd.Timestamp:
    """Parse one explicit cutoff without silently changing its timezone."""
    if prediction_cutoff is None:
        raise ValueError("prediction_cutoff is required for explicit cutoff validation")
    try:
        cutoff = pd.Timestamp(prediction_cutoff)
    except (TypeError, ValueError) as exc:
        raise ValueError("prediction_cutoff must be a valid timestamp") from exc
    if pd.isna(cutoff):
        raise ValueError("prediction_cutoff must be a valid timestamp")
    return cutoff
