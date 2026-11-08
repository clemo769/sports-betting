"""Resolve historical match metadata from cutoff-eligible source-row evidence."""

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any
from uuid import UUID

import pandas as pd

from src.db.models import SourceRecord
from src.features.temporal_contract import validate_timezone_aware_utc
from src.temporal import resolve_local_event_time


@dataclass(frozen=True)
class MetadataField:
    value: Any
    status: str
    evidence: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        value = self.value.isoformat() if isinstance(self.value, datetime) else self.value
        return {
            "value": value,
            "status": self.status,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ResolvedMatchMetadata:
    match_id: UUID
    fields: dict[str, MetadataField]
    availability_status: str

    def field(self, name: str) -> MetadataField:
        return self.fields[name]

    @property
    def event_time(self) -> datetime | None:
        value = self.field("scheduled_event_time").value
        return value if isinstance(value, datetime) else None

    @property
    def home_team(self) -> str | None:
        return (
            self.field("home_team_canonical_name").value
            or self.field("home_team_source_name").value
        )

    @property
    def away_team(self) -> str | None:
        return (
            self.field("away_team_canonical_name").value
            or self.field("away_team_source_name").value
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": str(self.match_id),
            "availability_status": self.availability_status,
            "fields": {
                name: self.fields[name].as_dict()
                for name in sorted(self.fields)
            },
        }


_FIELDS = (
    "home_team_source_name",
    "away_team_source_name",
    "home_team_canonical_name",
    "away_team_canonical_name",
    "scheduled_date_raw",
    "scheduled_time_raw",
    "scheduled_event_time",
    "venue_name",
    "venue_country",
    "venue_timezone",
    "timezone_resolution_basis",
    "match_status",
)


def _reference(record: SourceRecord) -> dict[str, Any]:
    value = record.raw_reference
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _raw_row_fields(evidence: dict[str, Any]) -> dict[str, Any]:
    row = evidence.get("row")
    content = row.get("raw_content") if isinstance(row, dict) else None
    if isinstance(content, dict) and isinstance(content.get("columns"), list) and isinstance(content.get("values"), list):
        if len(content["columns"]) == len(content["values"]):
            return {
                str(column).strip().casefold(): value
                for column, value in zip(content["columns"], content["values"])
            }
    if isinstance(content, dict):
        return {str(key).strip().casefold(): value for key, value in content.items()}
    return {}


def _nonempty(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _pick(raw: dict[str, Any], reference: dict[str, Any], *names: str) -> Any | None:
    for name in names:
        key = name.casefold()
        if key in raw:
            value = _nonempty(raw[key])
            if value is not None and not isinstance(value, (dict, list)):
                return value
        if name in reference:
            value = _nonempty(reference[name])
            if value is not None and not isinstance(value, (dict, list)):
                return value
    return None


def _raw_venue_value(raw: dict[str, Any], *names: str) -> Any | None:
    venue = raw.get("venue")
    if not isinstance(venue, dict):
        return None
    return _pick(venue, {}, *names)


def _team_field(
    evidence: dict[str, Any],
    raw: dict[str, Any],
    side: str,
    *source_names: str,
) -> tuple[Any | None, Any | None, str, dict[str, Any] | None]:
    adapter = evidence.get("adapter_enrichment")
    team_identity = adapter.get("team_identity") if isinstance(adapter, dict) else None
    identity = team_identity.get(side) if isinstance(team_identity, dict) else None
    source_name = None
    canonical_name = None
    status = "unresolved"
    identity_details = None
    if isinstance(identity, dict):
        source_name = _nonempty(identity.get("source_name"))
        canonical_name = _nonempty(identity.get("resolved_name"))
        if identity.get("mapping_version") and identity.get("mapping_sha256"):
            status = identity.get("resolution_status", "unresolved")
        identity_details = identity
    if source_name is None:
        source_name = _pick(raw, {}, *source_names)
    if canonical_name is None:
        status = "alias_resolution_unavailable"
    return source_name, canonical_name, status, identity_details


def source_record_metadata(record: SourceRecord, availability_status: str) -> tuple[dict[str, tuple[Any | None, str]], dict[str, Any]]:
    """Extract evidence from one source row; never read fields from Match."""
    reference = _reference(record)
    evidence = reference.get("source_row_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    raw = _raw_row_fields(evidence)
    adapter = evidence.get("adapter_enrichment")
    adapter = adapter if isinstance(adapter, dict) else {}
    venue_enrichment = adapter.get("venue_metadata")
    venue_enrichment = venue_enrichment if isinstance(venue_enrichment, dict) else {}
    sidecar_values = venue_enrichment.get("values")
    sidecar_unknown = bool(sidecar_values) and venue_enrichment.get("availability_status") != "verified"

    home_source, home_canonical, home_status, home_identity = _team_field(
        evidence, raw, "home", "HomeTeam", "team1", "home_team"
    )
    away_source, away_canonical, away_status, away_identity = _team_field(
        evidence, raw, "away", "AwayTeam", "team2", "away_team"
    )
    if home_status == "source_name_only":
        home_canonical = None
    if away_status == "source_name_only":
        away_canonical = None

    event_time = None
    event_time_status = "unresolved"
    if record.source_event_ts is not None:
        try:
            event_time = validate_timezone_aware_utc(record.source_event_ts, "source_event_ts")
            event_time_status = "resolved_from_persisted_source_event_ts"
        except ValueError:
            event_time = None
            event_time_status = "invalid_source_event_ts"

    date_raw = _pick(raw, reference, "Date", "date", "source_event_date_raw")
    time_raw = _pick(raw, reference, "Time", "time", "source_event_time_raw")
    raw_timezone_name = (
        _pick(raw, {}, "venue_timezone", "timezone", "time zone", "tz")
        or _raw_venue_value(raw, "venue_timezone", "timezone", "timeZone", "tz")
    )
    timezone_name = raw_timezone_name or _pick(
        {}, reference, "venue_timezone", "timezone", "time zone", "tz"
    )
    timezone_basis = _pick(raw, reference, "timezone_resolution_basis")
    raw_venue_name = _pick(raw, {}, "venue_name", "venue", "ground", "stadium")
    raw_venue_name = raw_venue_name or _raw_venue_value(raw, "venue_name", "name", "stadium")
    raw_venue_country = _pick(raw, {}, "venue_country", "country")
    raw_venue_country = raw_venue_country or _raw_venue_value(raw, "venue_country", "country")
    if sidecar_unknown and isinstance(sidecar_values, dict):
        timezone_name = raw_timezone_name
        reference_venue_name = raw_venue_name
        reference_venue_country = raw_venue_country
        if raw_timezone_name is None:
            timezone_basis = None
    else:
        reference_venue_name = raw_venue_name or _pick(
            {}, reference, "venue_name", "venue", "ground", "stadium"
        )
        reference_venue_country = raw_venue_country or _pick(
            {}, reference, "venue_country", "country"
        )

    if sidecar_unknown:
        event_time = None
        event_time_status = "unknown_venue_metadata_availability"

    if event_time is None and date_raw is not None and time_raw is not None and timezone_name is not None:
        try:
            local_time = pd.to_datetime(f"{date_raw} {time_raw}", dayfirst="/" in str(date_raw), errors="raise").to_pydatetime()
            if local_time.tzinfo is None or local_time.utcoffset() is None:
                event_time = resolve_local_event_time(local_time, str(timezone_name))
                event_time_status = "resolved_from_raw_schedule_and_recorded_timezone"
        except (TypeError, ValueError, OverflowError):
            event_time = None
            event_time_status = "raw_schedule_unresolved"

    status_raw = _pick(raw, {}, "fixture_status", "event_status", "status")
    venue_status = "source_row_evidence" if reference_venue_name or reference_venue_country else (
        "unknown_availability" if sidecar_unknown else "unresolved"
    )
    timezone_status = "source_row_evidence" if raw_timezone_name else (
        "unknown_availability" if sidecar_unknown else "unresolved"
    )

    values: dict[str, tuple[Any | None, str]] = {
        "home_team_source_name": (home_source, "source_row_evidence" if home_source is not None else "unresolved"),
        "away_team_source_name": (away_source, "source_row_evidence" if away_source is not None else "unresolved"),
        "home_team_canonical_name": (home_canonical, home_status),
        "away_team_canonical_name": (away_canonical, away_status),
        "scheduled_date_raw": (str(date_raw) if date_raw is not None else None, "source_row_evidence" if date_raw is not None else "unresolved"),
        "scheduled_time_raw": (str(time_raw) if time_raw is not None else None, "source_row_evidence" if time_raw is not None else "unresolved"),
        "scheduled_event_time": (event_time, event_time_status),
        "venue_name": (reference_venue_name, venue_status),
        "venue_country": (reference_venue_country, venue_status if reference_venue_country is not None else "unresolved"),
        "venue_timezone": (timezone_name, timezone_status),
        "timezone_resolution_basis": (timezone_basis, "source_row_evidence" if timezone_basis is not None else "unresolved"),
        "match_status": (str(status_raw) if status_raw is not None else None, "source_row_evidence" if status_raw is not None else "unresolved"),
    }
    snapshot = evidence.get("snapshot") if isinstance(evidence.get("snapshot"), dict) else {}
    row = evidence.get("row") if isinstance(evidence.get("row"), dict) else {}
    row_identity = evidence.get("identity") if isinstance(evidence.get("identity"), dict) else {}
    evidence_ref = {
        "source_record_id": str(record.source_record_id),
        "source": record.source,
        "legacy_source_id": record.source_id,
        "snapshot_row_identity": row_identity.get("snapshot_scoped_row_id"),
        "snapshot_id": snapshot.get("snapshot_id") or reference.get("source_snapshot_id"),
        "snapshot_sha256": snapshot.get("snapshot_sha256") or reference.get("source_snapshot_sha256"),
        "availability_basis": record.availability_basis,
        "source_available_at": record.source_available_ts.isoformat() if record.source_available_ts else None,
        "availability_status": availability_status,
        "row_content_sha256": row.get("content_sha256"),
        "team_identity": {
            "home": home_identity,
            "away": away_identity,
        },
    }
    return values, evidence_ref


def resolve_match_metadata(
    match_id: UUID,
    candidates: list[tuple[SourceRecord, str]],
) -> ResolvedMatchMetadata:
    """Resolve each field by agreement among cutoff-eligible row snapshots."""
    per_field: dict[str, list[tuple[Any, str, dict[str, Any]]]] = {name: [] for name in _FIELDS}
    availability_statuses = set()
    for record, availability_status in candidates:
        values, evidence_ref = source_record_metadata(record, availability_status)
        availability_statuses.add(availability_status)
        for name, (value, status) in values.items():
            if value is not None:
                per_field[name].append((value, status, evidence_ref))

    fields: dict[str, MetadataField] = {}
    for name, observations in per_field.items():
        if not observations:
            fields[name] = MetadataField(None, "unresolved", ())
            continue
        canonical_values = {
            json.dumps(
                value.isoformat() if isinstance(value, datetime) else value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for value, _, _ in observations
        }
        evidence_refs = tuple(sorted(
            (evidence_ref for _, _, evidence_ref in observations),
            key=lambda item: (item["source"], item["source_record_id"]),
        ))
        if len(canonical_values) > 1:
            fields[name] = MetadataField(None, "conflict", evidence_refs)
            continue
        value, _, _ = observations[0]
        statuses = {status for _, status, _ in observations}
        status = next(iter(statuses)) if len(statuses) == 1 else "corroborated"
        fields[name] = MetadataField(value, status, evidence_refs)

    if availability_statuses == {"verified"}:
        overall_availability = "verified"
    elif "unknown" in availability_statuses:
        overall_availability = "contains_unknown_availability"
    elif availability_statuses:
        overall_availability = "mixed_availability"
    else:
        overall_availability = "unresolved"
    return ResolvedMatchMetadata(match_id, fields, overall_availability)
