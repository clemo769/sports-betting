"""Deterministic historical source and result selection at a prediction cutoff."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.db.models import Match, ResultObservation, SourceRecord
from src.db.match_metadata import ResolvedMatchMetadata, resolve_match_metadata
from src.features.temporal_contract import (
    resolve_source_available_at,
    validate_prediction_mode,
    validate_timezone_aware_utc,
)
from src.source_evidence import read_source_revision_evidence
from src.temporal import AvailabilityBasis, PredictionMode


class AsOfSelectionError(ValueError):
    """Raised when source or correction history cannot be reconstructed safely."""


@dataclass(frozen=True)
class AsOfIssue:
    code: str
    message: str
    match_id: UUID | None = None
    observation_id: UUID | None = None
    source_record_id: UUID | None = None


@dataclass(frozen=True)
class AsOfTarget:
    """Minimal target event input when a persisted Match object is unavailable."""

    match_id: UUID
    scheduled_event_time: datetime


@dataclass(frozen=True)
class AsOfExclusion:
    record_type: str
    record_id: UUID
    match_id: UUID | None
    reason: str


@dataclass(frozen=True)
class SelectedSourceRecord:
    source_record_id: UUID
    match_id: UUID
    event_time: datetime | None
    source: str
    source_id: str
    source_revision: int
    availability_basis: str | None
    availability_status: str
    source_available_at: datetime | None
    source_published_at: datetime | None
    first_observed_at: datetime | None
    snapshot_available_at: datetime | None
    source_available_ref: str | None
    source_snapshot_id: str | None
    source_snapshot_sha256: str | None


@dataclass(frozen=True)
class SelectedResultObservation:
    result_observation_id: UUID
    match_id: UUID
    event_time: datetime | None
    home_team: str | None
    away_team: str | None
    source_record_id: UUID | None
    result_data: dict[str, Any]
    observed_at: datetime
    observation_status: str
    supersedes_observation_id: UUID | None
    availability_status: str
    correction_state: str


@dataclass(frozen=True)
class AsOfSelection:
    target_match_id: UUID
    prediction_cutoff: datetime
    prediction_mode: PredictionMode
    target_metadata: ResolvedMatchMetadata
    prior_match_metadata: tuple[ResolvedMatchMetadata, ...]
    selected_source_records: tuple[SelectedSourceRecord, ...]
    excluded_source_records: tuple[AsOfExclusion, ...]
    selected_result_observations: tuple[SelectedResultObservation, ...]
    excluded_result_observations: tuple[AsOfExclusion, ...]
    unresolved_issues: tuple[AsOfIssue, ...]


class AsOfSelectionDataSource(Protocol):
    def get_match(self, match_id: UUID) -> Match | None: ...

    def source_records_for_event(self, event_time: datetime | None) -> list[tuple[SourceRecord, Match | None]]: ...

    def result_observations_for_event(
        self, event_time: datetime | None
    ) -> list[tuple[ResultObservation, Match]]: ...


class AsOfSelectionRepository:
    """Read-only SQLAlchemy query boundary for as-of selection."""

    def __init__(self, session: Session):
        self.session = session

    def get_match(self, match_id: UUID) -> Match | None:
        return self.session.get(Match, match_id)

    def source_records_for_event(
        self, event_time: datetime | None
    ) -> list[tuple[SourceRecord, Match | None]]:
        statement = select(SourceRecord, Match).outerjoin(
            Match, SourceRecord.match_id == Match.match_id
        )
        if event_time is not None:
            statement = statement.where(
                or_(SourceRecord.match_id.is_(None), Match.event_time <= event_time)
            )
        return [(record, match) for record, match in self.session.execute(statement).all()]

    def result_observations_for_event(
        self, event_time: datetime | None
    ) -> list[tuple[ResultObservation, Match]]:
        statement = select(ResultObservation, Match).join(
            Match, ResultObservation.match_id == Match.match_id
        )
        if event_time is not None:
            statement = statement.where(Match.event_time <= event_time)
        return [(observation, match) for observation, match in self.session.execute(statement).all()]


class AsOfSelectionService:
    """Select eligible historical source rows and result states as of one cutoff."""

    def __init__(self, data_source: AsOfSelectionDataSource):
        self.data_source = data_source

    def select(
        self,
        target_match: Match | AsOfTarget | UUID,
        *,
        prediction_cutoff: datetime,
        prediction_mode: str | PredictionMode,
    ) -> AsOfSelection:
        mode = validate_prediction_mode(prediction_mode)
        if isinstance(target_match, UUID):
            target = self.data_source.get_match(target_match)
            if target is None:
                raise AsOfSelectionError(f"target match {target_match} does not exist")
            target_match_id = target.match_id
        elif isinstance(target_match, AsOfTarget):
            target_match_id = target_match.match_id
        else:
            target_match_id = target_match.match_id
        if not isinstance(target_match_id, UUID):
            raise AsOfSelectionError("target match must have a UUID match_id")

        cutoff_utc = validate_timezone_aware_utc(prediction_cutoff, "prediction_cutoff")
        source_rows = self.data_source.source_records_for_event(None)
        source_rows = sorted(source_rows, key=self._source_row_key)
        selected_sources: list[SelectedSourceRecord] = []
        excluded_sources: list[AsOfExclusion] = []
        issues: list[AsOfIssue] = []
        source_decisions: dict[UUID, tuple[bool, str, str, datetime | None]] = {}
        source_by_id: dict[UUID, SourceRecord] = {}
        revision_evidence_by_id = {}

        for record, match in source_rows:
            if record.source_record_id in source_by_id:
                raise AsOfSelectionError(f"duplicate source_record_id {record.source_record_id} in selection input")
            source_by_id[record.source_record_id] = record
            try:
                reference = self._raw_reference(record)
                revision_evidence_by_id[record.source_record_id] = read_source_revision_evidence(
                    reference,
                    source=record.source,
                    legacy_source_id=record.source_id,
                    snapshot_id=reference.get("source_snapshot_id") or reference.get("snapshot_id"),
                    snapshot_sha256=(
                        reference.get("source_snapshot_sha256")
                        or reference.get("snapshot_sha256")
                    ),
                    source_revision=record.source_revision,
                )
            except ValueError as error:
                revision_evidence_by_id[record.source_record_id] = None
                issues.append(AsOfIssue(
                    "invalid_source_revision_evidence",
                    str(error),
                    match.match_id if match is not None else None,
                    source_record_id=record.source_record_id,
                ))
            if match is None or record.match_id is None:
                reason = "unlinked_source_record"
                excluded_sources.append(AsOfExclusion("source_record", record.source_record_id, None, reason))
                source_decisions[record.source_record_id] = (False, "unknown", reason, None)
                continue

            try:
                available_at, snapshot_id, snapshot_sha256 = self._availability(record)
            except ValueError as error:
                reason = "invalid_availability_evidence"
                excluded_sources.append(AsOfExclusion("source_record", record.source_record_id, match.match_id, reason))
                source_decisions[record.source_record_id] = (False, "invalid", reason, None)
                issues.append(AsOfIssue(reason, str(error), match.match_id, source_record_id=record.source_record_id))
                continue

            if available_at is None:
                if mode is PredictionMode.STRICT_PRE_MATCH:
                    reason = "unknown_source_availability"
                    excluded_sources.append(AsOfExclusion("source_record", record.source_record_id, match.match_id, reason))
                    source_decisions[record.source_record_id] = (False, "unknown", reason, None)
                else:
                    source_decisions[record.source_record_id] = (True, "unknown", "", None)
                    selected_sources.append(
                        self._selected_source_record(record, match.match_id, "unknown", None, snapshot_id, snapshot_sha256)
                    )
                    issues.append(AsOfIssue(
                        "retrospective_unknown_availability",
                        "Selected in RETROSPECTIVE_RESEARCH with unknown availability; not strict eligible.",
                        match.match_id,
                        source_record_id=record.source_record_id,
                    ))
                continue

            if available_at > cutoff_utc:
                reason = "source_available_after_cutoff"
                excluded_sources.append(AsOfExclusion("source_record", record.source_record_id, match.match_id, reason))
                source_decisions[record.source_record_id] = (False, "known_after_cutoff", reason, available_at)
                continue

            source_decisions[record.source_record_id] = (True, "verified", "", available_at)
            selected_sources.append(
                self._selected_source_record(record, match.match_id, "verified", available_at, snapshot_id, snapshot_sha256)
            )

        self._resolve_source_revision_groups(
            source_rows,
            revision_evidence_by_id,
            source_decisions,
            selected_sources,
            excluded_sources,
            issues,
        )

        selected_source_by_id = {row.source_record_id: row for row in selected_sources}
        metadata_candidates: dict[UUID, list[tuple[SourceRecord, str]]] = defaultdict(list)
        for record, match in source_rows:
            if match is None or record.match_id is None or record.source_record_id not in selected_source_by_id:
                continue
            availability_status = source_decisions[record.source_record_id][1]
            metadata_candidates[match.match_id].append((record, availability_status))
        metadata_by_match = {
            match_id: resolve_match_metadata(match_id, records)
            for match_id, records in metadata_candidates.items()
        }
        target_metadata = metadata_by_match.get(
            target_match_id, resolve_match_metadata(target_match_id, [])
        )
        target_event_time = target_metadata.event_time
        if target_event_time is None:
            issues.append(AsOfIssue(
                "target_match_metadata_unresolved",
                "Scheduled event time is not reconstructable from source evidence eligible at the cutoff.",
                target_match_id,
            ))
        elif mode is PredictionMode.STRICT_PRE_MATCH and cutoff_utc >= target_event_time:
            raise ValueError("STRICT_PRE_MATCH requires prediction_cutoff < scheduled_event_time")
        if (
            target_metadata.home_team is None
            or target_metadata.away_team is None
        ):
            issues.append(AsOfIssue(
                "target_match_team_identity_unresolved",
                "Target source team names are missing from cutoff-eligible evidence.",
                target_match_id,
            ))

        eligible_prior_metadata: dict[UUID, ResolvedMatchMetadata] = {}
        eligible_source_ids = set()
        for match_id, metadata in metadata_by_match.items():
            if match_id == target_match_id:
                continue
            historical_event_time = metadata.event_time
            if historical_event_time is None:
                issues.append(AsOfIssue(
                    "prior_match_metadata_unresolved",
                    "Historical scheduled event time is not reconstructable from cutoff-eligible source evidence.",
                    match_id,
                ))
                continue
            if target_event_time is None or historical_event_time >= target_event_time:
                reason = "target_event_time_unresolved" if target_event_time is None else "event_not_strictly_prior_to_target"
                for record, _ in metadata_candidates[match_id]:
                    excluded_sources.append(AsOfExclusion("source_record", record.source_record_id, match_id, reason))
                    source_decisions[record.source_record_id] = (
                        False,
                        source_decisions[record.source_record_id][1],
                        reason,
                        source_decisions[record.source_record_id][3],
                    )
                continue
            eligible_prior_metadata[match_id] = metadata
            eligible_source_ids.update(
                record.source_record_id for record, _ in metadata_candidates[match_id]
            )

        selected_sources = [
            row for row in selected_sources if row.source_record_id in eligible_source_ids
        ]
        selected_source_by_id = {row.source_record_id: row for row in selected_sources}
        prior_match_metadata = tuple(
            eligible_prior_metadata[match_id]
            for match_id in sorted(eligible_prior_metadata, key=str)
        )

        observation_rows = self.data_source.result_observations_for_event(None)
        observation_rows = sorted(observation_rows, key=self._observation_row_key)
        all_observations: dict[UUID, tuple[ResultObservation, Match, datetime]] = {}
        excluded_observations: list[AsOfExclusion] = []
        visible_observations: dict[
            UUID, tuple[ResultObservation, Match, datetime, str]
        ] = {}

        for observation, match in observation_rows:
            if observation.result_observation_id in all_observations:
                raise AsOfSelectionError(
                    f"duplicate result_observation_id {observation.result_observation_id} in selection input"
                )
            observed_at = validate_timezone_aware_utc(observation.observed_at, "observed_at")
            all_observations[observation.result_observation_id] = (observation, match, observed_at)
            metadata = eligible_prior_metadata.get(match.match_id)
            event_time = metadata.event_time if metadata is not None else None

            if match.match_id == target_match_id:
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation.result_observation_id, match.match_id,
                    "target_match_result_excluded",
                ))
            elif metadata is None or event_time is None:
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation.result_observation_id, match.match_id,
                    "historical_match_metadata_unresolved",
                ))
            elif (
                metadata.home_team is None
                or metadata.away_team is None
            ):
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation.result_observation_id, match.match_id,
                    "historical_match_team_identity_unresolved",
                ))
                issues.append(AsOfIssue(
                    "prior_match_team_identity_unresolved",
                    "Prior source team names are missing from cutoff-eligible evidence.",
                    match.match_id,
                    observation_id=observation.result_observation_id,
                ))
            elif event_time >= target_event_time:
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation.result_observation_id, match.match_id,
                    "event_not_strictly_prior_to_target",
                ))
            elif observed_at > cutoff_utc:
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation.result_observation_id, match.match_id,
                    "observation_recorded_after_cutoff",
                ))
            else:
                availability_status = self._observation_availability_status(
                    observation,
                    source_decisions,
                    mode,
                    match.match_id,
                )
                if availability_status is None:
                    excluded_observations.append(AsOfExclusion(
                        "result_observation", observation.result_observation_id, match.match_id,
                        "result_source_unavailable_as_of_cutoff",
                    ))
                    continue
                visible_observations[observation.result_observation_id] = (
                    observation,
                    match,
                    observed_at,
                    availability_status,
                )

        children_by_parent: dict[UUID, list[UUID]] = defaultdict(list)
        for observation_id, (observation, match, observed_at, _) in visible_observations.items():
            parent_id = observation.supersedes_observation_id
            if parent_id is None:
                continue
            parent_entry = all_observations.get(parent_id)
            if parent_entry is None:
                raise AsOfSelectionError(
                    f"observation {observation_id} references missing superseded observation {parent_id}"
                )
            parent, parent_match, parent_observed_at = parent_entry
            if parent_match.match_id != match.match_id:
                raise AsOfSelectionError(
                    f"observation {observation_id} supersedes an observation from another match"
                )
            if parent_observed_at > observed_at:
                raise AsOfSelectionError(
                    f"observation {observation_id} supersedes a later-recorded observation {parent_id}"
                )
            if parent_observed_at > cutoff_utc:
                raise AsOfSelectionError(
                    f"observation {observation_id} supersedes observation {parent_id} not recorded by cutoff"
                )
            if parent_id not in visible_observations:
                raise AsOfSelectionError(
                    f"observation {observation_id} supersedes observation {parent_id} excluded from as-of history"
                )
            children_by_parent[parent_id].append(observation_id)

        self._validate_correction_graph(visible_observations, children_by_parent)
        for parent_id, child_ids in children_by_parent.items():
            if len(child_ids) > 1:
                raise AsOfSelectionError(
                    f"conflicting supersession branch from observation {parent_id}: "
                    f"{', '.join(str(child_id) for child_id in sorted(child_ids, key=str))}"
                )

        leaf_ids = set(visible_observations) - set(children_by_parent)
        active_by_match: dict[UUID, list[SelectedResultObservation]] = defaultdict(list)
        for observation_id, (observation, match, observed_at, availability_status) in visible_observations.items():
            metadata = eligible_prior_metadata[match.match_id]
            event_time = metadata.event_time
            if observation_id not in leaf_ids:
                child_id = children_by_parent[observation_id][0]
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation_id, match.match_id,
                    f"superseded_as_of_cutoff_by:{child_id}",
                ))
                continue

            if observation.observation_status == "disputed":
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation_id, match.match_id, "disputed_as_of_cutoff"
                ))
                issues.append(AsOfIssue(
                    "disputed_result_as_of_cutoff",
                    "The active result observation is disputed at the cutoff.",
                    match.match_id,
                    observation_id=observation_id,
                ))
                continue
            if observation.observation_status == "superseded":
                excluded_observations.append(AsOfExclusion(
                    "result_observation", observation_id, match.match_id,
                    "superseded_status_without_successor_as_of_cutoff",
                ))
                issues.append(AsOfIssue(
                    "superseded_without_successor",
                    "Observation is marked superseded but has no successor recorded by the cutoff.",
                    match.match_id,
                    observation_id=observation_id,
                ))
                continue
            if observation.observation_status not in {"observed", "resolved"}:
                raise AsOfSelectionError(
                    f"observation {observation_id} has unsupported status {observation.observation_status!r}"
                )

            if observation.observation_status == "resolved":
                if observation.resolved_at is None or not observation.resolved_by or not observation.resolution_reason:
                    raise AsOfSelectionError(f"resolved observation {observation_id} lacks resolution metadata")
                resolved_at = validate_timezone_aware_utc(observation.resolved_at, "resolved_at")
                if resolved_at > cutoff_utc:
                    excluded_observations.append(AsOfExclusion(
                        "result_observation", observation_id, match.match_id,
                        "resolution_completed_after_cutoff",
                    ))
                    issues.append(AsOfIssue(
                        "resolution_after_cutoff",
                        "Observation was not resolved as of the cutoff.",
                        match.match_id,
                        observation_id=observation_id,
                    ))
                    continue

            selected_observation = SelectedResultObservation(
                result_observation_id=observation_id,
                match_id=match.match_id,
                event_time=event_time,
                home_team=metadata.home_team,
                away_team=metadata.away_team,
                source_record_id=observation.source_record_id,
                result_data=dict(observation.result_data),
                observed_at=observed_at,
                observation_status=observation.observation_status,
                supersedes_observation_id=observation.supersedes_observation_id,
                availability_status=availability_status,
                correction_state=("resolved" if observation.observation_status == "resolved" else "observed"),
            )
            active_by_match[match.match_id].append(selected_observation)

        selected_observations = []
        for match_id, rows in active_by_match.items():
            rows.sort(key=lambda row: (row.observed_at, str(row.result_observation_id)))
            payloads = {json.dumps(row.result_data, sort_keys=True, separators=(",", ":")) for row in rows}
            if len(rows) > 1 and len(payloads) > 1:
                issues.append(AsOfIssue(
                    "conflicting_active_results",
                    "Multiple independent active result observations disagree at the cutoff.",
                    match_id,
                ))
            selected_observations.extend(rows)

        selected_sources.sort(key=self._selected_source_key)
        selected_observations.sort(
            key=lambda row: (row.observed_at, str(row.match_id), str(row.result_observation_id))
        )
        used_prior_match_ids = {row.match_id for row in selected_observations}
        prior_match_metadata = tuple(
            metadata
            for metadata in prior_match_metadata
            if metadata.match_id in used_prior_match_ids
        )
        return AsOfSelection(
            target_match_id=target_match_id,
            prediction_cutoff=cutoff_utc,
            prediction_mode=mode,
            target_metadata=target_metadata,
            prior_match_metadata=prior_match_metadata,
            selected_source_records=tuple(selected_sources),
            excluded_source_records=tuple(sorted(excluded_sources, key=self._exclusion_key)),
            selected_result_observations=tuple(selected_observations),
            excluded_result_observations=tuple(sorted(excluded_observations, key=self._exclusion_key)),
            unresolved_issues=tuple(sorted(issues, key=self._issue_key)),
        )

    @staticmethod
    def _raw_reference(record: SourceRecord) -> dict[str, Any]:
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

    def _availability(self, record: SourceRecord) -> tuple[datetime | None, str | None, str | None]:
        raw_reference = self._raw_reference(record)
        source_snapshot_sha256 = raw_reference.get("source_snapshot_sha256")
        if source_snapshot_sha256 is None:
            source_snapshot_sha256 = raw_reference.get("snapshot_sha256")
        source_snapshot_id = raw_reference.get("source_snapshot_id") or raw_reference.get("snapshot_id")
        basis = record.availability_basis
        available_at = resolve_source_available_at(
            availability_basis=basis,
            source_available_ts=record.source_available_ts,
            source_published_ts=record.source_published_ts,
            first_observed_at=record.first_observed_at,
            snapshot_available_at=record.snapshot_available_at,
            snapshot_id=source_snapshot_id,
            snapshot_sha256=source_snapshot_sha256,
            source_record_id=str(record.source_record_id),
        )
        if basis == AvailabilityBasis.SNAPSHOT.value:
            commit_sha = raw_reference.get("source_commit_sha")
            if not record.source_available_ref:
                raise AsOfSelectionError(
                    f"snapshot source record {record.source_record_id} lacks source_available_ref"
                )
            if commit_sha is not None and record.source_available_ref != commit_sha:
                raise AsOfSelectionError(
                    f"snapshot source record {record.source_record_id} commit reference mismatch"
                )
        return available_at, source_snapshot_id, source_snapshot_sha256

    @staticmethod
    def _resolve_source_revision_groups(
        source_rows: list[tuple[SourceRecord, Match | None]],
        evidence_by_id: dict[UUID, Any],
        source_decisions: dict[UUID, tuple[bool, str, str, datetime | None]],
        selected_sources: list[SelectedSourceRecord],
        excluded_sources: list[AsOfExclusion],
        issues: list[AsOfIssue],
    ) -> None:
        groups: dict[tuple[str, str], list[tuple[SourceRecord, Match | None, Any]]] = defaultdict(list)
        for record, match in source_rows:
            evidence = evidence_by_id.get(record.source_record_id)
            if evidence is None or evidence.logical_identity_status != "verified":
                continue
            logical_id = evidence.provider_logical_record_id
            if logical_id is not None:
                groups[(record.source, logical_id)].append((record, match, evidence))

        selected_by_id = {row.source_record_id: row for row in selected_sources}
        for (_, logical_id), entries in sorted(groups.items()):
            candidates = [
                entry for entry in entries
                if source_decisions.get(entry[0].source_record_id, (False, "", "", None))[0]
            ]
            if len(candidates) < 2:
                continue

            record_ids = sorted((str(entry[0].source_record_id) for entry in candidates))
            match_ids = {entry[1].match_id for entry in candidates if entry[1] is not None}
            ambiguity_message = (
                f"Provider logical record {logical_id!r} has multiple cutoff-eligible source rows "
                f"without a uniquely verifiable replacement chain: {', '.join(record_ids)}"
            )
            if len(match_ids) != 1 or any(entry[1] is None for entry in candidates):
                issues.append(AsOfIssue(
                    "ambiguous_source_revision",
                    ambiguity_message + "; candidates are linked to different or missing matches.",
                    source_record_id=UUID(record_ids[0]),
                ))
                continue
            if any(source_decisions[entry[0].source_record_id][1] == "unknown" for entry in candidates):
                issues.append(AsOfIssue(
                    "ambiguous_source_revision",
                    ambiguity_message + "; at least one candidate has unknown availability.",
                    next(iter(match_ids)),
                    source_record_id=UUID(record_ids[0]),
                ))
                continue

            if any(entry[2].ordering_status != "verified" for entry in candidates):
                issues.append(AsOfIssue(
                    "ambiguous_source_revision",
                    ambiguity_message + "; revision ordering evidence is unavailable.",
                    next(iter(match_ids)),
                    source_record_id=UUID(record_ids[0]),
                ))
                continue

            # Validate the full evidence chain, including rows outside this cutoff;
            # cutoff eligibility is applied only after the explicit chain is proven.
            entries_by_identity = {
                entry[2].snapshot_row_identity: entry for entry in entries
            }
            if len(entries_by_identity) != len(entries):
                issues.append(AsOfIssue(
                    "ambiguous_source_revision",
                    ambiguity_message + "; snapshot-row identities are not unique.",
                    next(iter(match_ids)),
                    source_record_id=UUID(record_ids[0]),
                ))
                continue

            children_by_parent: dict[str, list[str]] = defaultdict(list)
            roots = []
            invalid_edge = False
            for record, match, evidence in entries:
                parent_identity = evidence.supersedes_snapshot_row_identity
                if parent_identity is None:
                    roots.append(evidence.snapshot_row_identity)
                    continue
                parent_entry = entries_by_identity.get(parent_identity)
                if (
                    parent_entry is None
                    or parent_entry[0].source != record.source
                    or parent_entry[2].provider_logical_record_id != logical_id
                    or parent_entry[1] is None
                    or match is None
                    or parent_entry[1].match_id != match.match_id
                ):
                    invalid_edge = True
                    break
                children_by_parent[parent_identity].append(evidence.snapshot_row_identity)

            if invalid_edge or len(roots) != 1 or any(len(children) != 1 for children in children_by_parent.values()):
                issues.append(AsOfIssue(
                    "ambiguous_source_revision",
                    ambiguity_message + "; predecessor evidence is missing, cross-match, branched, or incomplete.",
                    next(iter(match_ids)),
                    source_record_id=UUID(record_ids[0]),
                ))
                continue

            ordered_identities = []
            current_identity = roots[0]
            while current_identity not in ordered_identities:
                ordered_identities.append(current_identity)
                children = children_by_parent.get(current_identity, [])
                if not children:
                    break
                current_identity = children[0]
            if (
                len(ordered_identities) != len(entries)
                or (ordered_identities and ordered_identities[-1] in children_by_parent)
            ):
                issues.append(AsOfIssue(
                    "ambiguous_source_revision",
                    ambiguity_message + "; predecessor evidence contains a cycle or disconnected chain.",
                    next(iter(match_ids)),
                    source_record_id=UUID(record_ids[0]),
                ))
                continue

            eligible_ids = {entry[0].source_record_id for entry in candidates}
            ordered_candidates = [
                entries_by_identity[row_identity][0]
                for row_identity in ordered_identities
                if entries_by_identity[row_identity][0].source_record_id in eligible_ids
            ]
            if not ordered_candidates:
                continue
            winner = ordered_candidates[-1]
            for record, match, _ in candidates:
                if record.source_record_id == winner.source_record_id:
                    continue
                selected_by_id.pop(record.source_record_id, None)
                selected, availability_status, _, available_at = source_decisions[record.source_record_id]
                if selected:
                    reason = f"source_revision_superseded_as_of_cutoff_by:{winner.source_record_id}"
                    source_decisions[record.source_record_id] = (
                        False,
                        availability_status,
                        reason,
                        available_at,
                    )
                    excluded_sources.append(AsOfExclusion(
                        "source_record", record.source_record_id,
                        match.match_id if match is not None else None, reason,
                    ))

        selected_sources[:] = sorted(
            selected_by_id.values(), key=AsOfSelectionService._selected_source_key
        )

    @staticmethod
    def _selected_source_record(
        record: SourceRecord,
        match_id: UUID,
        availability_status: str,
        available_at: datetime | None,
        snapshot_id: str | None,
        snapshot_sha256: str | None,
    ) -> SelectedSourceRecord:
        return SelectedSourceRecord(
            source_record_id=record.source_record_id,
            match_id=match_id,
            event_time=(
                validate_timezone_aware_utc(record.source_event_ts, "source_event_ts")
                if record.source_event_ts is not None else None
            ),
            source=record.source,
            source_id=record.source_id,
            source_revision=record.source_revision,
            availability_basis=record.availability_basis,
            availability_status=availability_status,
            source_available_at=available_at,
            source_published_at=record.source_published_ts,
            first_observed_at=record.first_observed_at,
            snapshot_available_at=record.snapshot_available_at,
            source_available_ref=record.source_available_ref,
            source_snapshot_id=snapshot_id,
            source_snapshot_sha256=snapshot_sha256,
        )

    @staticmethod
    def _observation_availability_status(
        observation: ResultObservation,
        source_decisions: dict[UUID, tuple[bool, str, str, datetime | None]],
        mode: PredictionMode,
        match_id: UUID,
    ) -> str | None:
        if observation.source_record_id is None:
            if mode is PredictionMode.STRICT_PRE_MATCH:
                return None
            return "unknown"
        decision = source_decisions.get(observation.source_record_id)
        if decision is None:
            raise AsOfSelectionError(
                f"result observation {observation.result_observation_id} references an unloaded source record"
            )
        selected, availability_status, reason, _ = decision
        if not selected:
            return None
        if availability_status == "unknown":
            if mode is PredictionMode.STRICT_PRE_MATCH:
                return None
            return "unknown"
        if reason:
            return None
        return "verified"

    @staticmethod
    def _validate_correction_graph(
        observations: dict[UUID, tuple[ResultObservation, Match, datetime, str]],
        children_by_parent: dict[UUID, list[UUID]],
    ) -> None:
        state: dict[UUID, int] = {}

        def visit(observation_id: UUID) -> None:
            current_state = state.get(observation_id, 0)
            if current_state == 1:
                raise AsOfSelectionError(f"supersession cycle detected at observation {observation_id}")
            if current_state == 2:
                return
            state[observation_id] = 1
            observation = observations[observation_id][0]
            parent_id = observation.supersedes_observation_id
            if parent_id is not None and parent_id in observations:
                visit(parent_id)
            state[observation_id] = 2

        for observation_id in sorted(observations, key=str):
            visit(observation_id)

    @staticmethod
    def _source_row_key(row: tuple[SourceRecord, Match | None]) -> tuple[str, ...]:
        record, match = row
        return (
            str(match.match_id) if match is not None else "",
            record.source,
            record.source_id,
            str(record.raw_reference or ""),
            str(record.source_revision),
            str(record.source_record_id),
        )

    @staticmethod
    def _observation_row_key(row: tuple[ResultObservation, Match]) -> tuple[str, ...]:
        observation, match = row
        return (
            observation.observed_at.isoformat(),
            str(match.match_id),
            str(observation.result_observation_id),
        )

    @staticmethod
    def _selected_source_key(row: SelectedSourceRecord) -> tuple[str, ...]:
        return (
            row.event_time.isoformat() if row.event_time is not None else "",
            str(row.match_id),
            row.source,
            row.source_id,
            str(row.source_revision),
            str(row.source_record_id),
        )

    @staticmethod
    def _exclusion_key(row: AsOfExclusion) -> tuple[str, ...]:
        return (row.record_type, str(row.match_id or ""), str(row.record_id), row.reason)

    @staticmethod
    def _issue_key(row: AsOfIssue) -> tuple[str, ...]:
        return (row.code, str(row.match_id or ""), str(row.observation_id or ""), row.message)
