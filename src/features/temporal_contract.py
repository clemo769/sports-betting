"""Project-level as-of rules for pre-match feature availability."""

import pandas as pd

EVENT_TIME_RULE = "Use the scheduled/source event time; historical results require historical_event_time < current_event_time."
PREDICTION_CUTOFF_RULE = "A feature is usable only when availability_time <= prediction_cutoff."
UNKNOWN_AVAILABILITY_RULE = "Event or ingestion time is not a publication-time substitute; unknown availability remains unproven."
SAME_EVENT_TIME_RULE = "Results at an identical event timestamp are applied after the timestamp group, never to one another."
OPENFOOTBALL_AVAILABILITY_RULE = "OpenFootball source_available_ts is the public GitHub commit time for the exact file snapshot; it is an update bound, not proof of original publication."


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
