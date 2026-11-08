"""Shared enums for temporal and prediction-mode contracts."""

import csv
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class PredictionMode(str, Enum):
    STRICT_PRE_MATCH = "STRICT_PRE_MATCH"
    RETROSPECTIVE_RESEARCH = "RETROSPECTIVE_RESEARCH"


class AvailabilityBasis(str, Enum):
    PROVIDER_PUBLICATION = "provider_publication"
    VERIFIED_ACCESS = "verified_access"
    FIRST_OBSERVED = "first_observed"
    SNAPSHOT = "snapshot"


PREDICTION_MODE_VALUES = tuple(mode.value for mode in PredictionMode)
AVAILABILITY_BASIS_VALUES = tuple(basis.value for basis in AvailabilityBasis)


def load_venue_metadata(path: Path) -> dict[str, dict[str, str]]:
    """Load explicit source_id-keyed venue evidence without inferring venues."""
    with path.open(newline="", encoding="utf-8-sig") as metadata_file:
        reader = csv.DictReader(metadata_file)
        if reader.fieldnames is None:
            raise ValueError("venue metadata CSV must have a header")
        fieldnames = {name.strip().casefold() for name in reader.fieldnames}
        required = {"source_id", "venue_name"}
        if not required.issubset(fieldnames):
            raise ValueError("venue metadata CSV requires source_id and venue_name columns")
        if not ({"venue_country", "venue_timezone"} & fieldnames):
            raise ValueError("venue metadata CSV requires venue_country or venue_timezone")

        metadata_by_source_id = {}
        for row in reader:
            normalized = {
                key.strip().casefold(): (value or "").strip()
                for key, value in row.items()
                if key is not None
            }
            source_id = normalized.get("source_id", "")
            venue_name = normalized.get("venue_name", "")
            if not source_id or not venue_name:
                raise ValueError("venue metadata source_id and venue_name values must be non-empty")
            if source_id in metadata_by_source_id:
                raise ValueError(f"duplicate venue metadata source_id: {source_id}")
            metadata_by_source_id[source_id] = {
                key: normalized[key]
                for key in ("venue_name", "venue_country", "venue_timezone")
                if normalized.get(key)
            }
    return metadata_by_source_id


def resolve_venue_timezone(
    *,
    league: str,
    venue_name: str | None,
    venue_country: str | None = None,
    venue_timezone: str | None = None,
) -> tuple[str | None, str]:
    """Resolve an IANA zone from explicit venue evidence without inferring teams' grounds."""
    if not isinstance(venue_name, str) or not venue_name.strip():
        return None, "missing_venue"

    if venue_timezone is not None and str(venue_timezone).strip():
        zone_name = str(venue_timezone).strip()
        try:
            ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            return None, "invalid_venue_timezone"
        return zone_name, "explicit_venue_timezone"

    country = str(venue_country or "").strip().casefold()
    if league.strip().casefold() == "premier-league" and country in {"england", "wales"}:
        return "Europe/London", "premier_league_england_wales_venue"
    return None, "missing_venue_timezone"


def resolve_local_event_time(local_event_time: datetime, timezone_name: str) -> datetime:
    """Convert an unambiguous naive venue-local wall time to aware UTC."""
    if not isinstance(local_event_time, datetime):
        raise ValueError("local_event_time must be a datetime")
    if local_event_time.tzinfo is not None and local_event_time.utcoffset() is not None:
        raise ValueError("local_event_time must be naive venue-local wall time")
    try:
        venue_zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError):
        raise ValueError(f"unknown IANA timezone: {timezone_name!r}") from None

    valid_utc_instants = set()
    for fold in (0, 1):
        candidate = local_event_time.replace(tzinfo=venue_zone, fold=fold)
        candidate_utc = candidate.astimezone(timezone.utc)
        round_trip = candidate_utc.astimezone(venue_zone).replace(tzinfo=None)
        if round_trip == local_event_time:
            valid_utc_instants.add(candidate_utc)

    if not valid_utc_instants:
        raise ValueError("nonexistent local event time due to timezone transition")
    if len(valid_utc_instants) > 1:
        raise ValueError("ambiguous local event time due to timezone transition")
    return next(iter(valid_utc_instants))