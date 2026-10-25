"""Persist linker output without inventing source timestamp semantics."""
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import math
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from src.db.models import Match, ResultObservation, SourceRecord
from src.db.repositories import MatchRepository, SourceRecordRepository


def _require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value


def _aware_source_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    return None


def _goal_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
        goals = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if goals < 0 or goals != value:
        raise ValueError(f"goal value must be a non-negative integer, got {value!r}")
    return goals


def persist_linked_records(
    session: Session,
    records: Iterable[Mapping[str, Any]],
    event_times: Mapping[str | UUID, datetime],
) -> dict[str, int]:
    """Persist matches and source records in the caller's transaction.

    ``event_times`` is an explicit mapping from canonical match ID to a verified,
    timezone-aware event instant. Naive source-local dates are never promoted to
    UTC. This function does not commit; the caller owns transaction boundaries.
    """
    rows = [dict(record) for record in records]
    batch_time = datetime.now(timezone.utc)
    source_identities = [
        (str(row.get("source") or "").strip(), str(row.get("source_id") or "").strip())
        for row in rows
    ]
    if len(source_identities) != len(set(source_identities)):
        raise ValueError("input contains duplicate source and source_id identities")

    matched_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("match_status") == "matched":
            canonical_id = str(row.get("canonical_match_id") or "")
            if not canonical_id:
                raise ValueError("matched records require canonical_match_id")
            matched_groups[canonical_id].append(row)

    prepared_matches: dict[UUID, Match] = {}
    for canonical_id, group in matched_groups.items():
        sources = {str(row.get("source") or "").strip() for row in group}
        sources.discard("")
        if len(sources) < 2:
            raise ValueError(
                f"canonical match {canonical_id} is marked matched without two independent sources"
            )
        event_time = event_times.get(canonical_id)
        if event_time is None:
            try:
                event_time = event_times.get(UUID(canonical_id))
            except ValueError:
                pass
        if event_time is None:
            raise ValueError(f"missing verified event time for canonical match {canonical_id}")
        event_time = _require_aware(event_time, f"event_times[{canonical_id}]")
        first = group[0]
        required = ("league", "season", "home_team", "away_team")
        if any(not first.get(field) for field in required):
            raise ValueError(f"canonical match {canonical_id} is missing required match fields")
        match_id = UUID(canonical_id)
        completed = all(
            row.get("home_goals") is not None and row.get("away_goals") is not None
            for row in group
        )
        prepared_matches[match_id] = Match(
            match_id=match_id,
            sport="football",
            league=str(first["league"]),
            season=str(first["season"]),
            home_team=str(first["home_team"]),
            away_team=str(first["away_team"]),
            event_time=event_time,
            status="completed" if completed else "scheduled",
        )

    match_repository = MatchRepository(session)
    source_repository = SourceRecordRepository(session)
    inserted_matches = 0
    inserted_source_records = 0
    linked_existing_source_records = 0
    inserted_result_observations = 0

    for match_id, match in prepared_matches.items():
        existing = match_repository.get(match_id)
        if existing is None:
            session.add(match)
            inserted_matches += 1
        elif (
            existing.league != match.league
            or existing.season != match.season
            or existing.home_team.strip().lower() != match.home_team.strip().lower()
            or existing.away_team.strip().lower() != match.away_team.strip().lower()
            or existing.event_time != match.event_time
        ):
            raise ValueError(f"canonical match {match_id} conflicts with the persisted match")

    for row in rows:
        source = str(row.get("source") or "").strip()
        source_id = str(row.get("source_id") or "").strip()
        if not source or not source_id:
            raise ValueError("every source record requires source and source_id")
        linked_match_id = None
        if row.get("match_status") == "matched":
            linked_match_id = UUID(str(row["canonical_match_id"]))
        source_record_id = uuid5(NAMESPACE_URL, f"{source}:{source_id}:1")
        existing_record = source_repository.find_identity(source, source_id, 1)
        if existing_record is not None:
            if (
                existing_record.match_id is None
                and linked_match_id is not None
            ):
                existing_record.match_id = linked_match_id
                linked_existing_source_records += 1
            elif (
                existing_record.match_id is not None
                and linked_match_id is not None
                and existing_record.match_id != linked_match_id
            ):
                raise ValueError(f"source record {source}:{source_id} is linked to a different match")
            source_record_id = existing_record.source_record_id
        else:
            available_at = _aware_source_timestamp(row.get("source_available_ts"))
            if available_at is not None and available_at > batch_time:
                raise ValueError(f"source availability timestamp is in the future for {source}:{source_id}")
            session.add(
                SourceRecord(
                    source_record_id=source_record_id,
                    source=source,
                    source_id=source_id,
                    source_revision=1,
                    match_id=linked_match_id,
                    source_event_ts=_aware_source_timestamp(row.get("source_event_ts")),
                    source_available_ts=available_at,
                    source_available_ref=row.get("source_available_ref"),
                    ingest_ts=batch_time,
                    raw_reference=row.get("raw_reference"),
                    original_filename=row.get("original_filename"),
                    parser_version=row.get("parser_version"),
                )
            )
            inserted_source_records += 1

        home_goals = _goal_value(row.get("home_goals"))
        away_goals = _goal_value(row.get("away_goals"))
        if linked_match_id is not None and home_goals is not None and away_goals is not None:
            observation_id = uuid5(
                NAMESPACE_URL,
                f"result:{source}:{source_id}:1:{home_goals}:{away_goals}",
            )
            if session.get(ResultObservation, observation_id) is None:
                session.add(
                    ResultObservation(
                        result_observation_id=observation_id,
                        match_id=linked_match_id,
                        source_record_id=source_record_id,
                        result_data={"home_goals": home_goals, "away_goals": away_goals},
                        observed_at=batch_time,
                        observation_status="observed",
                    )
                )
                inserted_result_observations += 1

    session.flush()
    return {
        "matches_inserted": inserted_matches,
        "source_records_inserted": inserted_source_records,
        "source_records_linked": linked_existing_source_records,
        "result_observations_inserted": inserted_result_observations,
    }