from datetime import datetime, timedelta, timezone
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import pandas as pd
import pytest

from src.db.as_of import AsOfSelectionService
from src.db.match_metadata import source_record_metadata
from src.db.models import Match, ResultObservation, SourceRecord
from src.features.feature_builder import build_as_of_features
from src.normalization.team_aliases import team_identity_evidence
from src.source_evidence import build_source_row_evidence
from src.temporal import PredictionMode


CUTOFF = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
TARGET_EVENT = datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc)
HISTORY_EVENT = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


class FakeAsOfRepository:
    def __init__(self, targets=(), source_rows=(), observation_rows=()):
        self.matches = {match.match_id: match for match in targets}
        self.source_rows = list(source_rows)
        self.observation_rows = list(observation_rows)

    def get_match(self, match_id):
        return self.matches.get(match_id)

    def source_records_for_event(self, event_time):
        return list(self.source_rows)

    def result_observations_for_event(self, event_time):
        return list(self.observation_rows)


def make_match(home, away, event_time, *, status="completed"):
    return Match(
        match_id=uuid4(),
        sport="football",
        league="premier-league",
        season="2526",
        home_team=home,
        away_team=away,
        event_time=event_time,
        status=status,
    )


def make_source_record(
    match,
    *,
    available_at,
    basis="verified_access",
    source_record_id=None,
    source_id=None,
):
    source_record_id = source_record_id or uuid4()
    source_id = source_id or str(uuid4())
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
        legacy_source_id=source_id,
        snapshot_id=f"test-snapshot-{source_record_id}",
        snapshot_sha256="a" * 64,
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
        "source_snapshot_id": f"test-snapshot-{source_record_id}",
        "source_snapshot_sha256": "a" * 64,
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
        source_id=source_id,
        source_revision=1,
        match_id=match.match_id,
        source_event_ts=match.event_time,
        source_available_ts=available_at,
        availability_basis=basis,
        ingest_ts=CUTOFF + timedelta(days=1),
        raw_reference=raw_reference,
    )


def make_observation(match, source_record, home_goals, away_goals, *, observed_at=CUTOFF - timedelta(minutes=1), supersedes=None, status="observed"):
    return ResultObservation(
        result_observation_id=uuid4(),
        match_id=match.match_id,
        source_record_id=source_record.source_record_id if source_record else None,
        result_data={"home_goals": home_goals, "away_goals": away_goals},
        observed_at=observed_at,
        observation_status=status,
        supersedes_observation_id=supersedes,
        resolution_reason="corrected official score" if status == "resolved" else None,
        resolved_at=observed_at if status == "resolved" else None,
        resolved_by="reviewer" if status == "resolved" else None,
    )


def mark_unordered_logical_revision(source_record, logical_id, snapshot_label):
    snapshot_id = f"snapshot-{snapshot_label}"
    snapshot_sha256 = "b" * 64
    prior_reference = json.loads(source_record.raw_reference)
    prior_evidence = prior_reference["source_row_evidence"]
    evidence = build_source_row_evidence(
        source=source_record.source,
        legacy_source_id=source_record.source_id,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        source_repository=None,
        source_file_path=None,
        filename="provider-data.csv",
        row_locator={"type": "row", "value": snapshot_label},
        raw_row_content={"legacy_source_id": source_record.source_id},
        availability={"status": "verified", "basis": "verified_access"},
        logical_identity_unavailable_reason="provider identity not supplied",
        adapter_enrichment=prior_evidence.get("adapter_enrichment"),
    )
    evidence["identity"].update({
        "provider_logical_record_id": logical_id,
        "provider_logical_record_status": "verified",
        "provider_logical_record_evidence": {
            "basis": "provider match key",
            "reference": "https://provider.invalid/match-key-contract",
        },
    })
    evidence["revision"].update({
        "value": source_record.source_revision,
        "ordering_status": "unavailable",
    })
    prior_reference.update({
        "source_snapshot_id": snapshot_id,
        "source_snapshot_sha256": snapshot_sha256,
        "source_row_evidence": evidence,
    })
    source_record.raw_reference = json.dumps(prior_reference, sort_keys=True)


def generate(targets, source_rows, observation_rows, cutoffs, mode=PredictionMode.STRICT_PRE_MATCH):
    target_sources = []
    known_source_matches = {match.match_id for _, match in source_rows if match is not None}
    for target in targets:
        if target.match_id not in known_source_matches:
            cutoff = cutoffs.get(str(target.match_id), cutoffs.get(target.match_id))
            target_sources.append((
                make_source_record(
                    target,
                    available_at=cutoff,
                    source_record_id=uuid5(NAMESPACE_URL, f"target-metadata:{target.match_id}"),
                    source_id=f"target-metadata:{target.match_id}",
                ),
                target,
            ))
    service = AsOfSelectionService(
        FakeAsOfRepository(targets, [*source_rows, *target_sources], observation_rows)
    )
    return build_as_of_features(
        targets,
        as_of_selector=service,
        prediction_cutoffs=cutoffs,
        prediction_mode=mode,
        feature_version="elo-form-v1",
        schema_version="feature-schema-v1",
        feature_generation_version="test-build",
    ).set_index("match_id")


def test_strict_features_exclude_unknown_availability_and_research_marks_it():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    unknown_source = make_source_record(historical, available_at=None, basis=None)
    result = make_observation(historical, unknown_source, 3, 0)
    cutoffs = {str(target.match_id): CUTOFF}

    strict = generate([target], [(unknown_source, historical)], [(result, historical)], cutoffs)
    retrospective = generate(
        [target],
        [(unknown_source, historical)],
        [(result, historical)],
        cutoffs,
        PredictionMode.RETROSPECTIVE_RESEARCH,
    )

    strict_row = strict.loc[str(target.match_id)]
    retrospective_row = retrospective.loc[str(target.match_id)]
    assert strict_row["home_elo"] == 1500.0
    assert pd.isna(strict_row["home_form_last3"])
    assert retrospective_row["home_elo"] > 1500.0
    assert retrospective_row["unknown_availability"]
    assert strict_row["prediction_mode"] == "STRICT_PRE_MATCH"
    assert retrospective_row["prediction_mode"] == "RETROSPECTIVE_RESEARCH"
    assert strict_row["source_record_ids_json"] == "[]"
    assert json.loads(retrospective_row["source_record_ids_json"]) == [str(unknown_source.source_record_id)]
    strict_source_reference = json.loads(strict_row["source_data_reference_json"])
    retrospective_source_reference = json.loads(retrospective_row["source_data_reference_json"])
    assert strict_source_reference["snapshot_id"].startswith("asof-selection-sha256:")
    assert strict_source_reference["source_record_ids"] == []
    assert retrospective_source_reference["source_record_ids"] == [str(unknown_source.source_record_id)]


def test_source_arriving_after_cutoff_cannot_change_strict_features():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    late_source = make_source_record(historical, available_at=CUTOFF + timedelta(seconds=1))
    late_result = make_observation(historical, late_source, 4, 0)
    cutoffs = {str(target.match_id): CUTOFF}

    without_late_data = generate([target], [], [], cutoffs)
    with_late_data = generate(
        [target], [(late_source, historical)], [(late_result, historical)], cutoffs
    )

    assert with_late_data.loc[str(target.match_id), "home_elo"] == without_late_data.loc[
        str(target.match_id), "home_elo"
    ]
    assert with_late_data.loc[str(target.match_id), "source_record_ids_json"] == "[]"


def test_late_correction_does_not_change_earlier_feature_row():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    old_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=5))
    late_source = make_source_record(historical, available_at=CUTOFF + timedelta(minutes=1))
    old_result = make_observation(
        historical,
        old_source,
        1,
        0,
        observed_at=CUTOFF - timedelta(minutes=5),
    )
    late_correction = make_observation(
        historical,
        late_source,
        0,
        1,
        observed_at=CUTOFF + timedelta(minutes=1),
        supersedes=old_result.result_observation_id,
        status="resolved",
    )
    cutoffs = {str(target.match_id): CUTOFF}

    before_correction = generate([target], [(old_source, historical)], [(old_result, historical)], cutoffs)
    with_later_correction = generate(
        [target],
        [(old_source, historical), (late_source, historical)],
        [(old_result, historical), (late_correction, historical)],
        cutoffs,
    )

    assert with_later_correction.loc[str(target.match_id), "home_elo"] == before_correction.loc[
        str(target.match_id), "home_elo"
    ]
    assert json.loads(with_later_correction.loc[str(target.match_id), "result_observation_ids_json"]) == [
        str(old_result.result_observation_id)
    ]


def test_correction_available_before_cutoff_changes_historical_feature():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    old_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=10))
    correction_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1))
    old_result = make_observation(
        historical,
        old_source,
        1,
        0,
        observed_at=CUTOFF - timedelta(minutes=10),
    )
    correction = make_observation(
        historical,
        correction_source,
        0,
        1,
        observed_at=CUTOFF - timedelta(minutes=1),
        supersedes=old_result.result_observation_id,
        status="resolved",
    )
    cutoffs = {str(target.match_id): CUTOFF}

    old_features = generate([target], [(old_source, historical)], [(old_result, historical)], cutoffs)
    corrected_features = generate(
        [target],
        [(old_source, historical), (correction_source, historical)],
        [(old_result, historical), (correction, historical)],
        cutoffs,
    )

    assert old_features.loc[str(target.match_id), "home_elo"] > 1500
    assert corrected_features.loc[str(target.match_id), "home_elo"] < 1500
    assert json.loads(corrected_features.loc[str(target.match_id), "result_observation_ids_json"]) == [
        str(correction.result_observation_id)
    ]


def test_target_result_and_future_results_never_enter_feature_history():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    target_source = make_source_record(target, available_at=CUTOFF - timedelta(minutes=10))
    target_result = make_observation(target, target_source, 5, 0, observed_at=CUTOFF - timedelta(minutes=5))
    future = make_match("Arsenal", "Burnley", TARGET_EVENT + timedelta(minutes=1))
    future_source = make_source_record(future, available_at=CUTOFF - timedelta(minutes=1))
    future_result = make_observation(future, future_source, 4, 0)
    cutoffs = {str(target.match_id): CUTOFF}

    row = generate(
        [target],
        [(target_source, target), (future_source, future)],
        [(target_result, target), (future_result, future)],
        cutoffs,
    ).loc[str(target.match_id)]

    assert row["home_elo"] == 1500.0
    assert row["source_record_ids_json"] == "[]"
    assert row["result_observation_ids_json"] == "[]"


def test_target_metadata_uses_cutoff_snapshot_not_mutated_match_fields():
    target = make_match(
        "Mutable Home",
        "Mutable Away",
        TARGET_EVENT + timedelta(days=30),
        status="completed",
    )
    scheduled_match = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    available_source = make_source_record(scheduled_match, available_at=CUTOFF - timedelta(minutes=5))
    available_source.match_id = target.match_id
    late_match = make_match(
        "Changed Home",
        "Changed Away",
        TARGET_EVENT + timedelta(days=1),
        status="completed",
    )
    late_source = make_source_record(late_match, available_at=CUTOFF + timedelta(seconds=1))
    late_source.match_id = target.match_id

    row = generate(
        [target],
        [(available_source, target), (late_source, target)],
        [],
        {str(target.match_id): CUTOFF},
    ).loc[str(target.match_id)]
    metadata = json.loads(row["target_match_metadata_json"])

    assert row["scheduled_event_time"] == TARGET_EVENT.isoformat()
    assert row["home_team"] == "Arsenal"
    assert row["away_team"] == "Chelsea"
    assert metadata["fields"]["match_status"]["value"] is None
    assert {
        evidence["source_record_id"]
        for evidence in metadata["fields"]["scheduled_event_time"]["evidence"]
    } == {str(available_source.source_record_id)}


def test_target_without_cutoff_eligible_metadata_remains_unresolved():
    target = make_match("Current Home", "Current Away", TARGET_EVENT, status="scheduled")
    source = make_source_record(target, available_at=None, basis=None)

    strict_selection = AsOfSelectionService(
        FakeAsOfRepository([target], [(source, target)], [])
    ).select(
        target,
        prediction_cutoff=CUTOFF,
        prediction_mode=PredictionMode.STRICT_PRE_MATCH,
    )
    retrospective_selection = AsOfSelectionService(
        FakeAsOfRepository([target], [(source, target)], [])
    ).select(
        target,
        prediction_cutoff=CUTOFF,
        prediction_mode=PredictionMode.RETROSPECTIVE_RESEARCH,
    )

    assert strict_selection.target_metadata.event_time is None
    assert strict_selection.target_metadata.home_team is None
    assert any(issue.code == "target_match_metadata_unresolved" for issue in strict_selection.unresolved_issues)
    assert retrospective_selection.target_metadata.event_time == TARGET_EVENT
    assert retrospective_selection.target_metadata.availability_status == "contains_unknown_availability"
    assert any(issue.code == "retrospective_unknown_availability" for issue in retrospective_selection.unresolved_issues)


def test_prior_metadata_and_result_evidence_are_separate():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1))
    observation = make_observation(historical, source, 3, 0)

    row = generate(
        [target],
        [(source, historical)],
        [(observation, historical)],
        {str(target.match_id): CUTOFF},
    ).loc[str(target.match_id)]
    prior = json.loads(row["prior_match_metadata_json"])

    assert len(prior) == 1
    assert prior[0]["match_id"] == str(historical.match_id)
    assert prior[0]["fields"]["home_team_source_name"]["value"] == "Arsenal"
    assert prior[0]["fields"]["scheduled_event_time"]["value"] == HISTORY_EVENT.isoformat()
    assert json.loads(row["result_observation_ids_json"]) == [str(observation.result_observation_id)]
    assert prior[0]["fields"]["scheduled_event_time"]["evidence"][0]["source_record_id"] == str(source.source_record_id)


def test_missing_schedule_venue_timezone_and_status_remain_unresolved():
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    source = make_source_record(historical, available_at=CUTOFF)
    reference = json.loads(source.raw_reference)
    evidence = build_source_row_evidence(
        source=source.source,
        legacy_source_id=source.source_id,
        snapshot_id=reference["source_snapshot_id"],
        snapshot_sha256=reference["source_snapshot_sha256"],
        source_repository="test/provider",
        source_file_path="season.csv",
        filename="season.csv",
        row_locator={"type": "test-row", "value": str(source.source_record_id)},
        raw_row_content={"home_team": "Arsenal", "away_team": "Burnley"},
        availability={"status": "verified", "basis": "verified_access"},
        logical_identity_unavailable_reason="provider logical identity unavailable",
        adapter_enrichment={
            "team_identity": {
                "home": team_identity_evidence("Arsenal"),
                "away": team_identity_evidence("Burnley"),
            },
            "venue_metadata": {
                "values": {"venue_name": "Sidecar Ground", "venue_timezone": "Europe/London"},
                "availability_status": "unknown",
                "content_hash": None,
            },
        },
    )
    reference["source_row_evidence"] = evidence
    for field in (
        "source_event_date_raw",
        "source_event_time_raw",
        "venue_name",
        "venue_country",
        "venue_timezone",
        "timezone_resolution_basis",
    ):
        reference.pop(field, None)
    source.raw_reference = json.dumps(reference, sort_keys=True)
    source.source_event_ts = None

    values, _ = source_record_metadata(source, "verified")

    assert values["scheduled_event_time"] == (None, "unknown_venue_metadata_availability")
    assert values["scheduled_date_raw"] == (None, "unresolved")
    assert values["scheduled_time_raw"] == (None, "unresolved")
    assert values["venue_name"] == (None, "unknown_availability")
    assert values["venue_timezone"] == (None, "unknown_availability")
    assert values["match_status"] == (None, "unresolved")
    assert values["home_team_canonical_name"][0] is None
    assert values["home_team_canonical_name"][1] == "source_name_only"


def test_team_identity_evidence_records_alias_map_version_and_digest():
    target = make_match("Manchester Utd", "Chelsea", TARGET_EVENT, status="scheduled")
    source = make_source_record(target, available_at=CUTOFF)
    values, evidence = source_record_metadata(source, "verified")
    home_identity = evidence["team_identity"]["home"]
    away_identity = evidence["team_identity"]["away"]

    assert values["home_team_canonical_name"] == ("Manchester United", "mapped_with_versioned_aliases")
    assert home_identity["mapping_version"] == "team-aliases-v1"
    assert len(home_identity["mapping_sha256"]) == 64
    assert values["away_team_canonical_name"] == (None, "source_name_only")
    assert away_identity["source_name"] == "Chelsea"


def test_elo_and_last_three_form_use_only_selected_completed_results():
    target = make_match("A", "E", TARGET_EVENT, status="scheduled")
    historical_rows = [
        (make_match("A", "B", HISTORY_EVENT - timedelta(days=20)), 3, 0),
        (make_match("C", "A", HISTORY_EVENT - timedelta(days=10)), 1, 0),
        (make_match("A", "D", HISTORY_EVENT - timedelta(days=1)), 1, 1),
    ]
    sources = []
    observations = []
    for match, home_goals, away_goals in historical_rows:
        source = make_source_record(match, available_at=CUTOFF - timedelta(days=2))
        observation = make_observation(
            match,
            source,
            home_goals,
            away_goals,
            observed_at=CUTOFF - timedelta(days=2),
        )
        sources.append((source, match))
        observations.append((observation, match))

    row = generate(
        [target],
        sources,
        observations,
        {str(target.match_id): CUTOFF},
    ).loc[str(target.match_id)]

    assert row["home_elo"] != 1500.0
    assert row["home_form_last3"] == pytest.approx((3 + 0 + 1) / 3)
    assert row["away_form_last3"] is None or pd.isna(row["away_form_last3"])


def test_different_target_matches_use_different_cutoffs():
    target_early = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    target_late = make_match("Arsenal", "Tottenham", TARGET_EVENT + timedelta(hours=1), status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    source = make_source_record(historical, available_at=CUTOFF + timedelta(minutes=1))
    observation = make_observation(historical, source, 3, 0)

    features = generate(
        [target_early, target_late],
        [(source, historical)],
        [(observation, historical)],
        {
            str(target_early.match_id): CUTOFF,
            str(target_late.match_id): CUTOFF + timedelta(minutes=2),
        },
    )

    assert features.loc[str(target_early.match_id), "home_elo"] == 1500.0
    assert features.loc[str(target_late.match_id), "home_elo"] > 1500.0
    assert features.loc[str(target_early.match_id), "prediction_cutoff"] != features.loc[
        str(target_late.match_id), "prediction_cutoff"
    ]


def test_as_of_feature_output_is_deterministic_and_preserves_feature_definition():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    source = make_source_record(historical, available_at=CUTOFF)
    observation = make_observation(historical, source, 3, 0)
    cutoffs = {str(target.match_id): CUTOFF}
    selection_rows = [(source, historical)]
    observation_rows = [(observation, historical)]

    first = generate([target], selection_rows, observation_rows, cutoffs)
    second = generate([target], list(reversed(selection_rows)), list(reversed(observation_rows)), cutoffs)

    expected_features = {
        "home_elo",
        "away_elo",
        "home_form_last3",
        "away_form_last3",
    }
    assert expected_features.issubset(first.columns)
    assert first.loc[str(target.match_id)].to_dict() == second.loc[str(target.match_id)].to_dict()
    assert first.loc[str(target.match_id), "feature_version"] == "elo-form-v1"
    assert first.loc[str(target.match_id), "schema_version"] == "feature-schema-v1"


def test_retrospective_and_strict_feature_rows_are_distinguishable():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    strict = generate(
        [target], [], [], {str(target.match_id): CUTOFF}, PredictionMode.STRICT_PRE_MATCH
    )
    retrospective = generate(
        [target], [], [], {str(target.match_id): TARGET_EVENT}, PredictionMode.RETROSPECTIVE_RESEARCH
    )

    assert strict.loc[str(target.match_id), "prediction_mode"] == "STRICT_PRE_MATCH"
    assert retrospective.loc[str(target.match_id), "prediction_mode"] == "RETROSPECTIVE_RESEARCH"
    assert strict.loc[str(target.match_id), "prediction_cutoff"] != retrospective.loc[
        str(target.match_id), "prediction_cutoff"
    ]


def test_missing_or_conflicting_provenance_fails_before_feature_output():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    first_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=2))
    second_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1))
    first_result = make_observation(
        historical,
        first_source,
        1,
        0,
        observed_at=CUTOFF - timedelta(minutes=2),
    )
    second_result = make_observation(
        historical,
        second_source,
        0,
        1,
        observed_at=CUTOFF - timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="unresolved issues: conflicting_active_results"):
        generate(
            [target],
            [(first_source, historical), (second_source, historical)],
            [(first_result, historical), (second_result, historical)],
            {str(target.match_id): CUTOFF},
        )


def test_ambiguous_source_revision_blocks_feature_generation():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    first_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=2))
    second_source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1))
    first_source.source_revision = 1
    second_source.source_revision = 99
    mark_unordered_logical_revision(first_source, "provider-match-1", "first")
    mark_unordered_logical_revision(second_source, "provider-match-1", "second")
    observations = [
        (make_observation(historical, first_source, 1, 0), historical),
        (make_observation(historical, second_source, 1, 0), historical),
    ]

    with pytest.raises(ValueError, match="ambiguous_source_revision"):
        generate(
            [target],
            [(first_source, historical), (second_source, historical)],
            observations,
            {str(target.match_id): CUTOFF},
        )


def test_strict_feature_generation_excludes_legacy_result_without_metadata_evidence():
    target = make_match("Arsenal", "Chelsea", TARGET_EVENT, status="scheduled")
    historical = make_match("Arsenal", "Burnley", HISTORY_EVENT)
    source = make_source_record(historical, available_at=CUTOFF - timedelta(minutes=1))
    source.raw_reference = None
    result = make_observation(historical, source, 3, 0)

    row = generate(
        [target],
        [(source, historical)],
        [(result, historical)],
        {str(target.match_id): CUTOFF},
    ).loc[str(target.match_id)]

    assert row["home_elo"] == 1500.0
    assert json.loads(row["result_observation_ids_json"]) == []
    assert any(
        item["reason"] == "historical_match_team_identity_unresolved"
        for item in json.loads(row["excluded_result_observations_json"])
    )
