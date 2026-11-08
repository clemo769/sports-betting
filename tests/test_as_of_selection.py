from datetime import datetime, timedelta, timezone
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.db.as_of import (
    AsOfSelectionError,
    AsOfSelectionRepository,
    AsOfSelectionService,
    AsOfTarget,
)
from src.db.models import Match, ResultObservation, SourceRecord
from src.normalization.team_aliases import team_identity_evidence
from src.source_evidence import build_source_row_evidence
from src.temporal import PredictionMode


CUTOFF = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
TARGET_EVENT = datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc)
HISTORICAL_EVENT = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
SNAPSHOT_SHA = "a" * 64


class FakeAsOfRepository:
    def __init__(self, target, source_rows=(), observation_rows=(), extra_matches=()):
        self.target = target
        self.source_rows = list(source_rows)
        self.observation_rows = list(observation_rows)
        self.matches = {target.match_id: target}
        self.matches.update({match.match_id: match for match in extra_matches})

    def get_match(self, match_id):
        return self.matches.get(match_id)

    def source_records_for_event(self, event_time):
        return list(self.source_rows)

    def result_observations_for_event(self, event_time):
        return list(self.observation_rows)


def make_match(*, match_id=None, event_time=HISTORICAL_EVENT, status="completed"):
    return Match(
        match_id=match_id or uuid4(),
        sport="football",
        league="premier-league",
        season="2526",
        home_team="Arsenal",
        away_team="Chelsea",
        event_time=event_time,
        status=status,
    )


def make_source_record(
    match,
    *,
    source_id=None,
    available_at=CUTOFF - timedelta(minutes=1),
    basis="verified_access",
    source_record_id=None,
    raw_reference=None,
    source_available_ref="access-proof",
):
    if raw_reference is None and basis == "snapshot":
        raw_reference = json.dumps({
            "source_snapshot_id": "snapshot-1",
            "source_snapshot_sha256": SNAPSHOT_SHA,
            "source_commit_sha": source_available_ref,
        })
    source_record_id = source_record_id or uuid4()
    legacy_source_id = source_id or str(uuid4())
    snapshot_id = "snapshot-1" if basis == "snapshot" else f"test-snapshot-{source_record_id}"
    raw_row = {
        "date": match.event_time.date().isoformat(),
        "time": match.event_time.strftime("%H:%M"),
        "home_team": match.home_team,
        "away_team": match.away_team,
        "venue_name": "Test Ground",
        "venue_country": "England",
        "venue_timezone": "UTC",
    }
    evidence = build_source_row_evidence(
        source="test-provider",
        legacy_source_id=legacy_source_id,
        snapshot_id=snapshot_id,
        snapshot_sha256=SNAPSHOT_SHA,
        source_repository="test/provider",
        source_file_path="season.json",
        filename="season.json",
        row_locator={"type": "test-row", "value": str(source_record_id)},
        raw_row_content=raw_row,
        availability={"status": "fixture", "basis": basis},
        logical_identity_unavailable_reason="test fixture does not model a provider logical ID",
        adapter_enrichment={
            "team_identity": {
                "home": team_identity_evidence(match.home_team),
                "away": team_identity_evidence(match.away_team),
            },
        },
    )
    evidence["revision"].update({"value": 1, "ordering_status": "unavailable"})
    raw_reference = json.dumps({
        "source_snapshot_id": snapshot_id,
        "source_snapshot_sha256": SNAPSHOT_SHA,
        "source_event_date_raw": raw_row["date"],
        "source_event_time_raw": raw_row["time"],
        "venue_name": "Test Ground",
        "venue_country": "England",
        "venue_timezone": "UTC",
        "timezone_resolution_basis": "test fixture explicit timezone",
        "source_row_evidence": evidence,
    }, sort_keys=True)
    return SourceRecord(
        source_record_id=source_record_id,
        source="test-provider",
        source_id=legacy_source_id,
        source_revision=1,
        match_id=match.match_id,
        source_event_ts=match.event_time,
        source_available_ts=available_at,
        source_published_ts=None,
        first_observed_at=available_at if basis == "first_observed" else None,
        snapshot_available_at=available_at if basis == "snapshot" else None,
        availability_basis=basis,
        source_available_ref=source_available_ref,
        ingest_ts=CUTOFF + timedelta(days=1),
        raw_reference=raw_reference,
    )


def make_observation(
    match,
    *,
    observation_id=None,
    source_record_id=None,
    observed_at=CUTOFF - timedelta(minutes=1),
    supersedes=None,
    result_data=None,
    status="observed",
    resolved_at=None,
):
    return ResultObservation(
        result_observation_id=observation_id or uuid4(),
        match_id=match.match_id,
        source_record_id=source_record_id,
        result_data=result_data or {"home_goals": 1, "away_goals": 0},
        observed_at=observed_at,
        observation_status=status,
        supersedes_observation_id=supersedes,
        resolution_reason="verified correction" if status in {"superseded", "disputed", "resolved"} else None,
        resolved_at=resolved_at,
        resolved_by="reviewer" if status == "resolved" else None,
    )


def attach_revision_evidence(
    record,
    *,
    logical_id=None,
    ordering_status="unavailable",
    supersedes_snapshot_row_identity=None,
    snapshot_label=None,
):
    snapshot_id = snapshot_label or f"snapshot-{record.source_record_id}"
    snapshot_sha256 = SNAPSHOT_SHA
    prior_reference = json.loads(record.raw_reference)
    prior_evidence = prior_reference["source_row_evidence"]
    evidence = build_source_row_evidence(
        source=record.source,
        legacy_source_id=record.source_id,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        source_repository=None,
        source_file_path=None,
        filename="provider-data.json",
        row_locator={"type": "row", "value": str(record.source_record_id)},
        raw_row_content={"source_id": record.source_id, "revision": record.source_revision},
        availability={"status": "verified", "basis": "verified_access"},
        logical_identity_unavailable_reason="provider identity not supplied",
        adapter_enrichment=prior_evidence.get("adapter_enrichment"),
    )
    if logical_id is not None:
        evidence["identity"].update({
            "provider_logical_record_id": logical_id,
            "provider_logical_record_status": "verified",
            "provider_logical_record_evidence": {
                "basis": "provider match key",
                "reference": "https://provider.invalid/match-key-contract",
            },
        })
    evidence["revision"].update({
        "value": record.source_revision,
        "ordering_status": ordering_status,
    })
    if ordering_status == "verified":
        evidence["revision"].update({
            "evidence_basis": "provider revision history",
            "ordering_evidence_ref": "https://provider.invalid/revision-history",
            "update_contract": "complete_replacement",
            "supersedes_snapshot_row_identity": supersedes_snapshot_row_identity,
        })
    prior_reference.update({
        "source_snapshot_id": snapshot_id,
        "source_snapshot_sha256": snapshot_sha256,
        "source_row_evidence": evidence,
    })
    record.raw_reference = json.dumps(prior_reference, sort_keys=True)
    return evidence["identity"]["snapshot_scoped_row_id"]


def select(target, source_rows=(), observation_rows=(), *, mode=PredictionMode.STRICT_PRE_MATCH, cutoff=CUTOFF, extra_matches=()):
    target_match = target if isinstance(target, Match) else None
    if isinstance(target, AsOfTarget):
        target_match = make_match(event_time=target.scheduled_event_time, status="scheduled")
        target_match.match_id = target.match_id
    matches_with_evidence = {
        match.match_id for _, match in source_rows if match is not None
    }
    metadata_matches_by_id = {}
    for metadata_match in [target_match, *extra_matches, *(item[1] for item in observation_rows)]:
        if (
            metadata_match is not None
            and metadata_match.match_id not in matches_with_evidence
        ):
            metadata_matches_by_id.setdefault(metadata_match.match_id, metadata_match)
    metadata_matches = [metadata_matches_by_id[key] for key in sorted(metadata_matches_by_id, key=str)]
    for metadata_match in metadata_matches:
        availability = cutoff if metadata_match.match_id == getattr(target_match, "match_id", None) else None
        basis = "verified_access" if availability is not None else None
        source_id = f"fixture-metadata:{metadata_match.match_id}"
        metadata_source_id = uuid5(NAMESPACE_URL, source_id)
        source_rows = [
            *source_rows,
            (
                make_source_record(
                    metadata_match,
                    source_id=source_id,
                    source_record_id=metadata_source_id,
                    available_at=availability,
                    basis=basis,
                ),
                metadata_match,
            ),
        ]
    repository = FakeAsOfRepository(target, source_rows, observation_rows, extra_matches)
    return AsOfSelectionService(repository).select(
        target,
        prediction_cutoff=cutoff,
        prediction_mode=mode,
    )


def test_source_available_exactly_at_cutoff_is_selected():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(historical, available_at=CUTOFF)

    result = select(target, [(record, historical)])

    assert [row.source_record_id for row in result.selected_source_records] == [record.source_record_id]
    assert result.selected_source_records[0].availability_status == "verified"


def test_same_legacy_source_id_across_snapshots_remains_independent_evidence():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    first = make_source_record(historical, source_id="season.json:0", available_at=CUTOFF - timedelta(minutes=2))
    second = make_source_record(historical, source_id="season.json:0", available_at=CUTOFF - timedelta(minutes=1))
    first.source_revision = 1
    second.source_revision = 99
    attach_revision_evidence(first, snapshot_label="snapshot-a")
    attach_revision_evidence(second, snapshot_label="snapshot-b")
    observations = [
        (make_observation(historical, source_record_id=first.source_record_id), historical),
        (make_observation(historical, source_record_id=second.source_record_id), historical),
    ]

    result = select(
        target,
        [(first, historical), (second, historical)],
        observations,
    )

    assert {row.source_record_id for row in result.selected_source_records} == {
        first.source_record_id,
        second.source_record_id,
    }
    assert [row.source_id for row in result.selected_source_records] == ["season.json:0"] * 2
    assert {row.result_observation_id for row in result.selected_result_observations} == {
        row[0].result_observation_id for row in observations
    }
    assert not any(issue.code == "ambiguous_source_revision" for issue in result.unresolved_issues)


def test_unordered_numeric_revisions_are_ambiguous_not_selected_by_max():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    low = make_source_record(historical, source_id="logical-1", available_at=CUTOFF - timedelta(minutes=2))
    high = make_source_record(historical, source_id="logical-1", available_at=CUTOFF - timedelta(minutes=1))
    low.source_revision = 1
    high.source_revision = 99
    attach_revision_evidence(low, logical_id="provider-match-1", snapshot_label="snapshot-low")
    attach_revision_evidence(high, logical_id="provider-match-1", snapshot_label="snapshot-high")

    result = select(target, [(low, historical), (high, historical)])

    assert {row.source_record_id for row in result.selected_source_records} == {
        low.source_record_id,
        high.source_record_id,
    }
    assert any(issue.code == "ambiguous_source_revision" for issue in result.unresolved_issues)


def test_verified_predecessor_evidence_selects_chain_leaf_not_largest_integer():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    predecessor = make_source_record(historical, source_id="legacy-row", available_at=CUTOFF - timedelta(minutes=2))
    successor = make_source_record(historical, source_id="legacy-row", available_at=CUTOFF - timedelta(minutes=1))
    predecessor.source_revision = 99
    successor.source_revision = 1
    predecessor_identity = attach_revision_evidence(
        predecessor,
        logical_id="provider-match-1",
        ordering_status="verified",
        snapshot_label="snapshot-predecessor",
    )
    attach_revision_evidence(
        successor,
        logical_id="provider-match-1",
        ordering_status="verified",
        supersedes_snapshot_row_identity=predecessor_identity,
        snapshot_label="snapshot-successor",
    )
    prior_observation = make_observation(
        historical,
        source_record_id=predecessor.source_record_id,
        result_data={"home_goals": 1, "away_goals": 0},
    )
    successor_observation = make_observation(
        historical,
        source_record_id=successor.source_record_id,
        observed_at=CUTOFF,
        result_data={"home_goals": 2, "away_goals": 0},
    )
    source_rows = [(predecessor, historical), (successor, historical)]
    observation_rows = [(prior_observation, historical), (successor_observation, historical)]

    first = select(target, source_rows, observation_rows)
    shuffled = select(target, list(reversed(source_rows)), list(reversed(observation_rows)))

    assert [row.source_record_id for row in first.selected_source_records] == [successor.source_record_id]
    assert [row.result_observation_id for row in first.selected_result_observations] == [
        successor_observation.result_observation_id
    ]
    assert first.selected_source_records == shuffled.selected_source_records
    assert first.selected_result_observations == shuffled.selected_result_observations
    assert not any(issue.code == "ambiguous_source_revision" for issue in first.unresolved_issues)


def test_unavailable_successor_cannot_displace_cutoff_eligible_predecessor():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    predecessor = make_source_record(historical, source_id="legacy-row", available_at=CUTOFF - timedelta(minutes=2))
    successor = make_source_record(historical, source_id="legacy-row", available_at=CUTOFF + timedelta(minutes=1))
    predecessor.source_revision = 1
    successor.source_revision = 2
    predecessor_identity = attach_revision_evidence(
        predecessor,
        logical_id="provider-match-2",
        ordering_status="verified",
        snapshot_label="snapshot-before",
    )
    attach_revision_evidence(
        successor,
        logical_id="provider-match-2",
        ordering_status="verified",
        supersedes_snapshot_row_identity=predecessor_identity,
        snapshot_label="snapshot-after",
    )
    old_observation = make_observation(
        historical, source_record_id=predecessor.source_record_id
    )
    late_observation = make_observation(
        historical,
        source_record_id=successor.source_record_id,
        observed_at=CUTOFF + timedelta(minutes=1),
        result_data={"home_goals": 2, "away_goals": 0},
    )

    result = select(
        target,
        [(predecessor, historical), (successor, historical)],
        [(old_observation, historical), (late_observation, historical)],
    )

    assert [row.source_record_id for row in result.selected_source_records] == [predecessor.source_record_id]
    assert [row.result_observation_id for row in result.selected_result_observations] == [
        old_observation.result_observation_id
    ]
    assert any(
        row.record_id == successor.source_record_id
        and row.reason == "source_available_after_cutoff"
        for row in result.excluded_source_records
    )


def test_retrospective_unknown_availability_and_revision_ambiguity_are_separate():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    known = make_source_record(historical, source_id="legacy-row", available_at=CUTOFF - timedelta(minutes=1))
    unknown = make_source_record(historical, source_id="legacy-row", available_at=None, basis=None)
    attach_revision_evidence(known, logical_id="provider-match-3", snapshot_label="snapshot-known")
    attach_revision_evidence(unknown, logical_id="provider-match-3", snapshot_label="snapshot-unknown")

    strict = select(target, [(known, historical), (unknown, historical)])
    retrospective = select(
        target,
        [(known, historical), (unknown, historical)],
        mode=PredictionMode.RETROSPECTIVE_RESEARCH,
    )

    assert [row.source_record_id for row in strict.selected_source_records] == [known.source_record_id]
    assert not any(issue.code == "ambiguous_source_revision" for issue in strict.unresolved_issues)
    assert {row.source_record_id for row in retrospective.selected_source_records} == {
        known.source_record_id,
        unknown.source_record_id,
    }
    retrospective_codes = {issue.code for issue in retrospective.unresolved_issues}
    assert "ambiguous_source_revision" in retrospective_codes
    assert "retrospective_unknown_availability" in retrospective_codes


def test_independent_providers_corroborate_or_conflict_without_revision_linkage():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    first_source = make_source_record(historical, source_id="first:1")
    second_source = make_source_record(historical, source_id="second:1")
    first_source.source = "provider-a"
    second_source.source = "provider-b"
    first_observation = make_observation(
        historical, source_record_id=first_source.source_record_id,
        result_data={"home_goals": 1, "away_goals": 0},
    )
    matching_observation = make_observation(
        historical, source_record_id=second_source.source_record_id,
        result_data={"home_goals": 1, "away_goals": 0},
    )

    matching = select(
        target,
        [(first_source, historical), (second_source, historical)],
        [(first_observation, historical), (matching_observation, historical)],
    )
    conflicting_observation = make_observation(
        historical, source_record_id=second_source.source_record_id,
        result_data={"home_goals": 0, "away_goals": 1},
    )
    conflicting = select(
        target,
        [(first_source, historical), (second_source, historical)],
        [(first_observation, historical), (conflicting_observation, historical)],
    )

    assert len(matching.selected_result_observations) == 2
    assert not any(issue.code == "conflicting_active_results" for issue in matching.unresolved_issues)
    assert any(issue.code == "conflicting_active_results" for issue in conflicting.unresolved_issues)
    assert not any(issue.code == "ambiguous_source_revision" for issue in matching.unresolved_issues)


def test_source_available_after_cutoff_is_excluded():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(historical, available_at=CUTOFF + timedelta(seconds=1))

    result = select(target, [(record, historical)])

    assert not result.selected_source_records
    assert result.excluded_source_records[0].reason == "source_available_after_cutoff"


def test_unknown_availability_is_excluded_in_strict_mode():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(historical, available_at=None, basis=None)

    result = select(target, [(record, historical)])

    assert not result.selected_source_records
    assert result.excluded_source_records[0].reason == "unknown_source_availability"


def test_unknown_availability_is_marked_in_retrospective_mode():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(historical, available_at=None, basis=None)

    result = select(
        target,
        [(record, historical)],
        mode=PredictionMode.RETROSPECTIVE_RESEARCH,
    )

    assert result.prediction_mode is PredictionMode.RETROSPECTIVE_RESEARCH
    assert result.selected_source_records[0].availability_status == "unknown"
    assert any(issue.code == "retrospective_unknown_availability" for issue in result.unresolved_issues)


def test_provider_publication_only_is_not_strict_availability():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(
        historical,
        available_at=None,
        basis="provider_publication",
        source_available_ref=None,
    )
    record.source_published_ts = CUTOFF - timedelta(hours=1)

    result = select(target, [(record, historical)])

    assert not result.selected_source_records
    assert result.excluded_source_records[0].reason == "unknown_source_availability"


def test_first_observed_evidence_is_eligible_at_its_boundary():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    first_seen = CUTOFF - timedelta(minutes=2)
    record = make_source_record(historical, available_at=first_seen, basis="first_observed")

    result = select(target, [(record, historical)])

    assert result.selected_source_records[0].availability_status == "verified"
    assert result.selected_source_records[0].source_available_at == first_seen


def test_immutable_snapshot_evidence_is_eligible_with_snapshot_identity_and_hash():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(historical, basis="snapshot", source_available_ref="commit-sha")

    result = select(target, [(record, historical)])

    selected = result.selected_source_records[0]
    assert selected.availability_basis == "snapshot"
    assert selected.source_snapshot_id == "snapshot-1"
    assert selected.source_snapshot_sha256 == SNAPSHOT_SHA
    assert selected.source_available_ref == "commit-sha"


def test_snapshot_with_mismatched_commit_reference_is_excluded_with_issue():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    record = make_source_record(historical, basis="snapshot", source_available_ref="different-sha")
    record.raw_reference = json.dumps({
        "source_snapshot_id": "snapshot-1",
        "source_snapshot_sha256": SNAPSHOT_SHA,
        "source_commit_sha": "expected-commit-sha",
    })

    result = select(target, [(record, historical)])

    assert not result.selected_source_records
    assert result.excluded_source_records[0].reason == "invalid_availability_evidence"
    assert any(issue.code == "invalid_availability_evidence" for issue in result.unresolved_issues)


def test_historical_event_at_or_after_target_is_excluded():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    same_time = make_match(event_time=TARGET_EVENT)
    later = make_match(event_time=TARGET_EVENT + timedelta(minutes=1))
    records = [
        (make_source_record(same_time), same_time),
        (make_source_record(later), later),
    ]

    result = select(target, records, extra_matches=[same_time, later])

    assert not result.selected_source_records
    assert {row.reason for row in result.excluded_source_records} == {"event_not_strictly_prior_to_target"}


def test_target_match_source_and_result_are_excluded():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    source_record = make_source_record(target, available_at=CUTOFF - timedelta(minutes=1))
    observation = make_observation(target, source_record_id=source_record.source_record_id)

    result = select(target, [(source_record, target)], [(observation, target)])

    assert not result.selected_source_records
    assert not result.selected_result_observations
    assert result.target_metadata.event_time == TARGET_EVENT
    assert result.excluded_result_observations[0].reason == "target_match_result_excluded"


def test_result_observation_recorded_exactly_at_cutoff_is_selected():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    source_record = make_source_record(historical, available_at=CUTOFF)
    observation = make_observation(
        historical,
        source_record_id=source_record.source_record_id,
        observed_at=CUTOFF,
    )

    result = select(target, [(source_record, historical)], [(observation, historical)])

    assert [row.result_observation_id for row in result.selected_result_observations] == [
        observation.result_observation_id
    ]


def test_result_observation_recorded_after_cutoff_is_excluded():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    source_record = make_source_record(historical)
    observation = make_observation(
        historical,
        source_record_id=source_record.source_record_id,
        observed_at=CUTOFF + timedelta(seconds=1),
    )

    result = select(target, [(source_record, historical)], [(observation, historical)])

    assert not result.selected_result_observations
    assert result.excluded_result_observations[0].reason == "observation_recorded_after_cutoff"


def test_completed_mutable_match_without_source_metadata_does_not_admit_result():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match(status="completed")
    observation = make_observation(historical)
    target_source = make_source_record(target, available_at=CUTOFF)
    repository = FakeAsOfRepository(
        target,
        [(target_source, target)],
        [(observation, historical)],
        [historical],
    )

    result = AsOfSelectionService(repository).select(
        target,
        prediction_cutoff=CUTOFF,
        prediction_mode=PredictionMode.STRICT_PRE_MATCH,
    )

    assert result.selected_result_observations == ()
    assert any(
        item.record_id == observation.result_observation_id
        and item.reason == "historical_match_metadata_unresolved"
        for item in result.excluded_result_observations
    )


def test_correction_after_cutoff_does_not_replace_prior_state():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    prior_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=5))
    correction_source = make_source_record(historical, available_at=CUTOFF + timedelta(minutes=1))
    prior = make_observation(
        historical,
        source_record_id=prior_source.source_record_id,
        observed_at=CUTOFF - timedelta(minutes=5),
        result_data={"home_goals": 1, "away_goals": 0},
    )
    correction = make_observation(
        historical,
        source_record_id=correction_source.source_record_id,
        observed_at=CUTOFF + timedelta(minutes=1),
        supersedes=prior.result_observation_id,
        result_data={"home_goals": 2, "away_goals": 0},
        status="resolved",
        resolved_at=CUTOFF + timedelta(minutes=1),
    )

    result = select(
        target,
        [(prior_source, historical), (correction_source, historical)],
        [(prior, historical), (correction, historical)],
    )

    assert [row.result_observation_id for row in result.selected_result_observations] == [
        prior.result_observation_id
    ]
    assert result.selected_result_observations[0].result_data == {"home_goals": 1, "away_goals": 0}


def test_unavailable_correction_cannot_supersede_prior_result_at_cutoff():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    prior_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=5))
    late_source = make_source_record(historical, available_at=CUTOFF + timedelta(minutes=1))
    prior = make_observation(
        historical,
        source_record_id=prior_source.source_record_id,
        observed_at=CUTOFF - timedelta(minutes=5),
        result_data={"home_goals": 1, "away_goals": 0},
    )
    correction = make_observation(
        historical,
        source_record_id=late_source.source_record_id,
        observed_at=CUTOFF - timedelta(minutes=2),
        supersedes=prior.result_observation_id,
        result_data={"home_goals": 2, "away_goals": 0},
        status="resolved",
        resolved_at=CUTOFF - timedelta(minutes=2),
    )

    result = select(
        target,
        [(prior_source, historical), (late_source, historical)],
        [(prior, historical), (correction, historical)],
    )

    assert [row.result_observation_id for row in result.selected_result_observations] == [
        prior.result_observation_id
    ]
    assert any(
        row.record_id == correction.result_observation_id
        and row.reason == "result_source_unavailable_as_of_cutoff"
        for row in result.excluded_result_observations
    )


def test_correction_recorded_before_cutoff_replaces_predecessor():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    prior_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=5))
    correction_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1))
    prior = make_observation(
        historical,
        source_record_id=prior_source.source_record_id,
        observed_at=CUTOFF - timedelta(minutes=5),
        result_data={"home_goals": 1, "away_goals": 0},
    )
    correction = make_observation(
        historical,
        source_record_id=correction_source.source_record_id,
        observed_at=CUTOFF - timedelta(minutes=1),
        supersedes=prior.result_observation_id,
        result_data={"home_goals": 2, "away_goals": 0},
        status="resolved",
        resolved_at=CUTOFF - timedelta(minutes=1),
    )

    result = select(
        target,
        [(prior_source, historical), (correction_source, historical)],
        [(prior, historical), (correction, historical)],
    )

    assert [row.result_observation_id for row in result.selected_result_observations] == [
        correction.result_observation_id
    ]
    assert result.selected_result_observations[0].correction_state == "resolved"
    assert result.excluded_result_observations[0].reason == (
        f"superseded_as_of_cutoff_by:{correction.result_observation_id}"
    )


def test_multiple_correction_chain_selects_only_latest_as_of_leaf():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    records = [
        make_source_record(historical, available_at=CUTOFF - timedelta(minutes=20)),
        make_source_record(historical, available_at=CUTOFF - timedelta(minutes=10)),
        make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1)),
    ]
    observations = [
        make_observation(historical, source_record_id=records[0].source_record_id, observed_at=CUTOFF - timedelta(minutes=20)),
        make_observation(
            historical,
            source_record_id=records[1].source_record_id,
            observed_at=CUTOFF - timedelta(minutes=10),
            supersedes=None,
            status="resolved",
            resolved_at=CUTOFF - timedelta(minutes=10),
        ),
        make_observation(
            historical,
            source_record_id=records[2].source_record_id,
            observed_at=CUTOFF - timedelta(minutes=1),
            status="resolved",
            resolved_at=CUTOFF - timedelta(minutes=1),
        ),
    ]
    observations[1].supersedes_observation_id = observations[0].result_observation_id
    observations[2].supersedes_observation_id = observations[1].result_observation_id

    result = select(
        target,
        [(record, historical) for record in records],
        [(observation, historical) for observation in observations],
    )

    assert [row.result_observation_id for row in result.selected_result_observations] == [
        observations[2].result_observation_id
    ]


def test_invalid_supersession_reference_fails_explicitly():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    observation = make_observation(historical, supersedes=uuid4())

    with pytest.raises(AsOfSelectionError, match="references missing superseded observation"):
        select(
            target,
            observation_rows=[(observation, historical)],
            mode=PredictionMode.RETROSPECTIVE_RESEARCH,
        )


def test_cross_match_supersession_fails_explicitly():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    other_historical = make_match(event_time=HISTORICAL_EVENT - timedelta(days=1))
    prior = make_observation(other_historical)
    correction = make_observation(historical, supersedes=prior.result_observation_id)

    with pytest.raises(AsOfSelectionError, match="another match"):
        select(
            target,
            observation_rows=[(prior, other_historical), (correction, historical)],
            extra_matches=[other_historical],
            mode=PredictionMode.RETROSPECTIVE_RESEARCH,
        )


def test_supersession_cycle_fails_explicitly():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    first = make_observation(historical)
    second = make_observation(historical)
    first.supersedes_observation_id = second.result_observation_id
    second.supersedes_observation_id = first.result_observation_id

    with pytest.raises(AsOfSelectionError, match="later-recorded|cycle"):
        select(
            target,
            observation_rows=[(first, historical), (second, historical)],
            mode=PredictionMode.RETROSPECTIVE_RESEARCH,
        )


def test_conflicting_supersession_branch_fails_explicitly():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    prior = make_observation(historical)
    first_correction = make_observation(historical, supersedes=prior.result_observation_id)
    second_correction = make_observation(historical, supersedes=prior.result_observation_id)
    second_correction.observed_at = first_correction.observed_at

    with pytest.raises(AsOfSelectionError, match="conflicting supersession branch"):
        select(
            target,
            observation_rows=[
                (prior, historical),
                (first_correction, historical),
                (second_correction, historical),
            ],
            mode=PredictionMode.RETROSPECTIVE_RESEARCH,
        )


def test_observations_recorded_at_cutoff_have_stable_ordering():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    historical = make_match()
    observations = [
        make_observation(historical, observed_at=CUTOFF, result_data={"home_goals": 0, "away_goals": 0}),
        make_observation(historical, observed_at=CUTOFF, result_data={"home_goals": 1, "away_goals": 0}),
    ]

    first = select(
        target,
        observation_rows=[(row, historical) for row in observations],
        mode=PredictionMode.RETROSPECTIVE_RESEARCH,
    )
    second = select(
        target,
        observation_rows=[(row, historical) for row in reversed(observations)],
        mode=PredictionMode.RETROSPECTIVE_RESEARCH,
    )

    assert [row.result_observation_id for row in first.selected_result_observations] == [
        row.result_observation_id for row in second.selected_result_observations
    ]
    assert any(issue.code == "conflicting_active_results" for issue in first.unresolved_issues)


def test_source_record_order_is_stable_independent_of_repository_order():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    earlier_match = make_match(event_time=HISTORICAL_EVENT - timedelta(days=1))
    later_match = make_match(event_time=HISTORICAL_EVENT)
    earlier_record = make_source_record(earlier_match, source_id="earlier")
    later_record = make_source_record(later_match, source_id="later")
    rows = [(later_record, later_match), (earlier_record, earlier_match)]

    first = select(target, rows)
    second = select(target, list(reversed(rows)))

    first_ids = [record.source_record_id for record in first.selected_source_records]
    second_ids = [record.source_record_id for record in second.selected_source_records]
    assert first_ids == second_ids == [earlier_record.source_record_id, later_record.source_record_id]


def test_naive_cutoff_and_naive_observation_times_are_rejected():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    with pytest.raises(ValueError, match="prediction_cutoff must be a timezone-aware datetime"):
        select(target, cutoff=datetime(2026, 11, 1, 10, 0))

    historical = make_match()
    observation = make_observation(historical, observed_at=datetime(2026, 11, 1, 9, 0))
    with pytest.raises(ValueError, match="observed_at must be a timezone-aware datetime"):
        select(target, observation_rows=[(observation, historical)])


def test_strict_cutoff_at_or_after_target_event_is_rejected():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    with pytest.raises(ValueError, match="prediction_cutoff < scheduled_event_time"):
        select(target, cutoff=TARGET_EVENT)
    with pytest.raises(ValueError, match="prediction_cutoff < scheduled_event_time"):
        select(target, cutoff=TARGET_EVENT + timedelta(seconds=1))


def test_target_uuid_uses_repository_lookup():
    target = make_match(event_time=TARGET_EVENT, status="scheduled")
    repository = FakeAsOfRepository(target)

    result = AsOfSelectionService(repository).select(
        target.match_id,
        prediction_cutoff=CUTOFF,
        prediction_mode=PredictionMode.STRICT_PRE_MATCH,
    )

    assert result.target_match_id == target.match_id


def test_target_event_value_can_be_selected_without_a_persisted_match():
    target_id = uuid4()
    target = AsOfTarget(match_id=target_id, scheduled_event_time=TARGET_EVENT)
    result = select(target)

    assert result.target_match_id == target_id
    assert result.prediction_cutoff == CUTOFF


def test_sql_repository_returns_all_revisions_and_observations_for_selector_filtering():
    class EmptyResult:
        def all(self):
            return []

    class RecordingSession:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))
            return EmptyResult()

    session = RecordingSession()
    repository = AsOfSelectionRepository(session)
    repository.source_records_for_event(TARGET_EVENT)
    repository.result_observations_for_event(TARGET_EVENT)

    assert len(session.statements) == 2
    source_query, observation_query = session.statements
    assert "source_record.source_revision" in source_query
    assert "result_observation.supersedes_observation_id" in observation_query
    assert "LIMIT" not in source_query.upper()
    assert "LIMIT" not in observation_query.upper()
    assert "ORDER BY" not in source_query.upper()
    assert "ORDER BY" not in observation_query.upper()
